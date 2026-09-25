#!/usr/bin/env python3
"""养护材料交付自检流程：示例数据准备 → 后端接口自检 → 前端构建检查。

三个环节串成一条可重复执行的流程，每次运行都会给出各环节通过情况；
某个环节失败时会指明属于数据问题、接口问题还是构建问题，并提示如何只重跑该环节。

用法：
    python3 scripts/check_pipeline.py              # 顺序跑全部三个环节
    python3 scripts/check_pipeline.py --only data  # 只重跑数据准备
    python3 scripts/check_pipeline.py --only api   # 只重跑接口自检
    python3 scripts/check_pipeline.py --only build # 只重跑前端构建

退出码：0 表示所选环节全部通过，1 表示存在未通过环节。
脚本本身只依赖标准库；接口自检环节会另外调用 backend/.venv 里的解释器起服务。
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"

SEED_PATH = BACKEND / "app" / "seed.py"
SERVICE_PATH = BACKEND / "app" / "services" / "material.py"
ROUTER_PATH = BACKEND / "app" / "routers" / "material.py"
VIEW_PATH = FRONTEND / "src" / "views" / "material" / "index.vue"

# 环节失败时的归类：让使用者一眼分清是数据问题、接口问题还是构建问题
STAGE_KIND = {
    "data": "数据问题（示例数据或字段口径）",
    "api": "接口问题（后端服务）",
    "build": "构建问题（前端构建）",
}


# ---------------------------------------------------------------------------
# 公共小工具
# ---------------------------------------------------------------------------

def _literal_assignments(path: Path) -> dict[str, Any]:
    """用 AST 提取模块顶层的字面量赋值。

    不 import 目标模块，这样即使后端依赖（fastapi 等）没装，数据环节也能跑。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        try:
            parsed = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                found[target.id] = parsed
    return found


def _vue_string_array(text: str, name: str) -> list[str]:
    """从 Vue 单文件组件里取出 `const <name> = [...]` 形式的字符串数组。"""
    match = re.search(rf"const\s+{name}\s*=\s*\[(?P<body>.*?)\]", text, re.S)
    if not match:
        return []
    return re.findall(r'"([^"]*)"', match.group("body"))


def _http(method: str, url: str, payload: dict[str, Any] | None = None,
          timeout: float = 10) -> tuple[int, Any]:
    """最小的 HTTP 调用：返回 (状态码, 解析后的 JSON)，4xx/5xx 也正常返回不抛异常。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ---------------------------------------------------------------------------
# 环节一：数据准备 —— 养护材料示例数据与字段口径校验
# ---------------------------------------------------------------------------

def check_data() -> list[str]:
    """校验养护材料示例数据，并核对散在 seed / service / router / 前端页面里的字段口径。"""
    problems: list[str] = []

    def report(ok: bool, label: str, detail: str = "") -> None:
        print(f"  {'✓' if ok else '✗'} {label}" + (f"：{detail}" if detail and not ok else ""))
        if not ok:
            problems.append(f"{label}：{detail}" if detail else label)

    seed_rows = _literal_assignments(SEED_PATH).get("SEED_ROWS", {})
    rows = seed_rows.get("material") or []
    service = _literal_assignments(SERVICE_PATH)
    router = _literal_assignments(ROUTER_PATH)
    view_text = VIEW_PATH.read_text(encoding="utf-8")

    required = service.get("REQUIRED_FIELDS", [])
    status_order = service.get("STATUS_ORDER", [])
    action_rules = service.get("ACTION_RULES", {})
    list_fields = router.get("LIST_FIELDS", [])
    statuses = router.get("STATUSES", [])
    view_columns = _vue_string_array(view_text, "columns")
    view_statuses = _vue_string_array(view_text, "statuses")
    view_actions = _vue_string_array(view_text, "actions")

    report(bool(rows), "示例数据已准备", "seed.py 里缺少 material 示例数据")
    if not rows:
        return problems

    ids = [row.get("id") for row in rows]
    report(len(set(ids)) == len(ids), "记录 id 不重复", f"重复 id：{sorted({i for i in ids if ids.count(i) > 1})}")

    missing = {
        f"id={row.get('id')} 缺 {field}"
        for row in rows for field in list_fields if field not in row
    }
    report(not missing, "每条记录都覆盖列表字段", "；".join(sorted(missing)))

    extra = {
        f"id={row.get('id')} 多出 {key}"
        for row in rows for key in row
        if key not in list_fields and key not in {"id", "status", "pending", "abnormal"}
    }
    report(not extra, "示例数据没有列表之外的散落字段", "；".join(sorted(extra)))

    blank = {
        f"id={row.get('id')} 的 {field} 为空"
        for row in rows for field in required
        if not str(row.get(field) or "").strip()
    }
    report(not blank, f"必填字段（{'、'.join(required)}）均已填写", "；".join(sorted(blank)))

    bad_stock = {
        f"id={row.get('id')} 的结存数量={row.get('结存数量')!r}"
        for row in rows
        if not isinstance(row.get("结存数量"), (int, float)) or row.get("结存数量", 0) < 0
    }
    bad_unit = {f"id={row.get('id')}" for row in rows if not str(row.get("计量单位") or "").strip()}
    report(not bad_stock and not bad_unit, "库存结存数量为非负数字且计量单位齐全",
           "；".join(sorted(bad_stock)) + ("；计量单位缺失：" + "、".join(sorted(bad_unit)) if bad_unit else ""))

    bad_status = {
        f"id={row.get('id')} 的状态={row.get('status')!r}"
        for row in rows if row.get("status") not in status_order
    }
    report(not bad_status, "每条记录状态都在允许的状态序列里", "；".join(sorted(bad_status)))

    bad_target = {f"{action}→{target}" for action, target in action_rules.items() if target not in status_order}
    report(not bad_target, "动作目标状态都在状态序列里", "；".join(sorted(bad_target)))

    report(list_fields == view_columns, "列表字段与前端页面列口径一致",
           f"后端 LIST_FIELDS={list_fields}，前端 columns={view_columns}")
    report(statuses == status_order == view_statuses, "状态序列在 service / router / 前端三处一致",
           f"service={status_order}，router={statuses}，前端={view_statuses}")
    report(sorted(action_rules) == sorted(view_actions), "可执行动作与前端页面一致",
           f"后端 ACTION_RULES={sorted(action_rules)}，前端 actions={view_actions}")

    return problems


# ---------------------------------------------------------------------------
# 环节二：接口自检 —— 起真实服务，跑养护材料接口的关键路径
# ---------------------------------------------------------------------------

def _backend_python() -> str | None:
    """找一个能 import fastapi/uvicorn 的解释器：优先 backend/.venv，其次当前解释器。"""
    candidates = [BACKEND / ".venv" / "bin" / "python", Path(sys.executable)]
    for candidate in candidates:
        if not candidate.exists():
            continue
        probe = subprocess.run(
            [str(candidate), "-c", "import fastapi, uvicorn"],
            capture_output=True,
        )
        if probe.returncode == 0:
            return str(candidate)
    return None


def check_api() -> list[str]:
    problems: list[str] = []

    def report(ok: bool, label: str, detail: str = "") -> None:
        print(f"  {'✓' if ok else '✗'} {label}" + (f"：{detail}" if detail and not ok else ""))
        if not ok:
            problems.append(f"{label}：{detail}" if detail else label)

    python = _backend_python()
    if python is None:
        report(False, "后端运行环境可用",
               "backend/.venv 缺失或损坏，请先执行 make install 修复环境")
        return problems
    print(f"  · 使用解释器 {python}")

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [python, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=BACKEND, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        deadline = time.time() + 30
        healthy = False
        while time.time() < deadline:
            if process.poll() is not None:
                break
            try:
                status, body = _http("GET", f"{base}/api/health", timeout=2)
                if status == 200 and body.get("ok"):
                    healthy = True
                    break
            except (OSError, json.JSONDecodeError):
                time.sleep(0.3)
        report(healthy, "服务启动并通过健康检查")
        if not healthy:
            output = ""
            if process.poll() is not None and process.stdout is not None:
                output = process.stdout.read() or ""
            if output:
                print("  服务启动日志末尾：\n    " + "\n    ".join(output.strip().splitlines()[-10:]))
            return problems

        list_fields = _literal_assignments(ROUTER_PATH).get("LIST_FIELDS", [])

        status, page = _http("GET", f"{base}/api/material")
        items = page.get("items", []) if isinstance(page, dict) else []
        report(status == 200 and page.get("total", 0) >= 1 and items,
               "列表接口返回示例数据", f"HTTP {status}，返回：{str(page)[:200]}")
        if not items:
            return problems
        first = items[0]
        missing = [field for field in list_fields if field not in first]
        report(not missing, "列表记录包含全部列表字段", f"缺少：{'、'.join(missing)}")

        query = urllib.parse.urlencode({"status": "已冻结"})
        status, page = _http("GET", f"{base}/api/material?{query}")
        frozen = page.get("items", []) if isinstance(page, dict) else []
        report(status == 200 and frozen and all(row.get("status") == "已冻结" for row in frozen),
               "按状态过滤（已冻结）生效", f"HTTP {status}，返回：{str(page)[:200]}")

        query = urllib.parse.urlencode({"keyword": "MATE-0001"})
        status, page = _http("GET", f"{base}/api/material?{query}")
        report(status == 200 and page.get("total") == 1,
               "按材料编号检索（MATE-0001）命中一条", f"HTTP {status}，total={page.get('total') if isinstance(page, dict) else page}")

        status, detail = _http("GET", f"{base}/api/material/1")
        report(status == 200 and "材料编号" in detail,
               "明细接口返回材料编号", f"HTTP {status}，返回：{str(detail)[:200]}")

        status, _ = _http("GET", f"{base}/api/material/99999")
        report(status == 404, "缺失明细返回 404", f"HTTP {status}")

        status, _ = _http("GET", f"{base}/api/material?size=500")
        report(status == 400, "超限分页（size=500）被拦下", f"HTTP {status}")

        status, created = _http("POST", f"{base}/api/material", {
            "values": {"材料编号": "MATE-9001", "材料名称": "接口自检材料", "规格型号": "ZX-9001"},
        })
        new_id = (created.get("entry") or {}).get("id") if isinstance(created, dict) else None
        report(status == 200 and created.get("ok") and new_id,
               "登记养护材料成功", f"HTTP {status}，返回：{str(created)[:200]}")

        status, rejected = _http("POST", f"{base}/api/material", {"values": {"材料名称": "缺字段"}})
        report(status == 200 and not rejected.get("ok") and "缺少必填字段" in rejected.get("message", ""),
               "缺字段登记被拦下并说明原因", f"HTTP {status}，返回：{str(rejected)[:200]}")

        if new_id:
            status, acted = _http("POST", f"{base}/api/material/{new_id}/actions",
                                  {"values": {"action": "冻结材料"}})
            report(status == 200 and acted.get("ok") and (acted.get("entry") or {}).get("status") == "已冻结",
                   "冻结材料动作生效", f"HTTP {status}，返回：{str(acted)[:200]}")

            status, bad_action = _http("POST", f"{base}/api/material/{new_id}/actions",
                                       {"values": {"action": "不存在的动作"}})
            report(status == 200 and not bad_action.get("ok"),
                   "非法动作被拦下", f"HTTP {status}，返回：{str(bad_action)[:200]}")

        status, page = _http("GET", f"{base}/api/material")
        status2, exported = _http("GET", f"{base}/api/material/export")
        report(status == 200 and status2 == 200 and exported.get("total") == page.get("total"),
               "导出接口与列表总量一致",
               f"列表 total={page.get('total') if isinstance(page, dict) else page}，导出 total={exported.get('total') if isinstance(exported, dict) else exported}")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    return problems


# ---------------------------------------------------------------------------
# 环节三：构建检查 —— 前端类型检查 + 生产构建
# ---------------------------------------------------------------------------

def check_build() -> list[str]:
    problems: list[str] = []

    def report(ok: bool, label: str, detail: str = "") -> None:
        print(f"  {'✓' if ok else '✗'} {label}" + (f"：{detail}" if detail and not ok else ""))
        if not ok:
            problems.append(f"{label}：{detail}" if detail else label)

    if shutil.which("npm") is None:
        report(False, "npm 可用", "未找到 npm，请先安装 Node.js")
        return problems
    if not (FRONTEND / "node_modules").is_dir():
        report(False, "前端依赖已安装", "frontend/node_modules 不存在，请先执行 npm install（或 make install）")
        return problems

    print("  · 执行 npm run build（vue-tsc 类型检查 + vite 构建）")
    try:
        process = subprocess.run(
            ["npm", "run", "build"], cwd=FRONTEND,
            capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        report(False, "前端构建在 10 分钟内完成", "构建超时")
        return problems
    if process.returncode != 0:
        tail = "\n    ".join((process.stdout + "\n" + process.stderr).strip().splitlines()[-15:])
        report(False, "npm run build 通过", f"退出码 {process.returncode}，输出末尾：\n    {tail}")
    else:
        report(True, "npm run build 通过")
    return problems


# ---------------------------------------------------------------------------
# 流程编排
# ---------------------------------------------------------------------------

STAGES: dict[str, tuple[str, Callable[[], list[str]]]] = {
    "data": ("数据准备（养护材料示例数据）", check_data),
    "api": ("接口自检（养护材料后端接口）", check_api),
    "build": ("构建检查（前端生产构建）", check_build),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="养护材料交付自检流程")
    parser.add_argument("--only", choices=sorted(STAGES), metavar="环节",
                        help="只重跑指定环节：data（数据准备）/ api（接口自检）/ build（构建检查）")
    args = parser.parse_args()

    selected = [args.only] if args.only else list(STAGES)
    results: dict[str, list[str]] = {}
    for index, key in enumerate(selected, start=1):
        title, check = STAGES[key]
        print(f"\n===== [{index}/{len(selected)}] {title} =====")
        started = time.time()
        try:
            problems = check()
        except Exception as exc:  # 环节自身出错也归类展示，不让流程直接崩掉
            problems = [f"环节执行异常：{exc}"]
        results[key] = problems
        elapsed = time.time() - started
        if problems:
            print(f"  → 未通过，属于{STAGE_KIND[key]}（用时 {elapsed:.1f}s）")
        else:
            print(f"  → 通过（用时 {elapsed:.1f}s）")

    print("\n===== 环节通过情况 =====")
    failed = [key for key in selected if results[key]]
    for key in selected:
        title, _ = STAGES[key]
        if results[key]:
            print(f"  ✗ {title}：未通过 → {STAGE_KIND[key]}，共 {len(results[key])} 项")
        else:
            print(f"  ✓ {title}：通过")

    if failed:
        print("\n存在未通过环节，修复后可只重跑对应环节：")
        for key in failed:
            print(f"  python3 scripts/check_pipeline.py --only {key}    # 或 make check-{key}")
        return 1
    print("\n所选环节全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

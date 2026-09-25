"""环节二：养护材料后端接口自检。

临时起一个真实的后端服务（随机空闲端口，不占用 8000 开发端口，互不影响），
逐项调用养护材料接口：列表、筛选、明细、登记、状态流转、导出、分页保护。
服务在检查结束后自动关闭，登记/流转产生的改动只留在临时进程内存里。

退出码：0 通过；2 数据问题（接口返回的内容与预期不符）；
        3 构建问题（服务起不来、HTTP 状态码异常、连接失败等）。
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"

EXIT_OK = 0
EXIT_DATA = 2
EXIT_BUILD = 3

STARTUP_TIMEOUT = 20  # 秒

# 每条检查结果：(检查项, 是否通过, 失败类别, 失败说明)，类别为 "数据问题" 或 "构建问题"
results: list[tuple[str, bool, str, str]] = []


def check(label: str, ok: bool, kind: str = "数据问题", detail: str = "") -> None:
    results.append((label, ok, kind, detail))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def call(base: str, method: str, path: str, payload: dict[str, Any] | None = None,
         query: dict[str, Any] | None = None) -> tuple[int, Any]:
    """调一次接口，返回 (HTTP 状态码, 解析后的 JSON)。连接层异常会向上抛。"""
    url = base + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            return error.code, json.loads(body)
        except ValueError:
            return error.code, {"raw": body}


def wait_ready(base: str, process: subprocess.Popen[str]) -> bool:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            status, payload = call(base, "GET", "/api/health")
            if status == 200 and payload.get("ok"):
                return True
        except (urllib.error.URLError, ConnectionError, OSError, ValueError):
            time.sleep(0.3)
    return False


def run_checks(base: str, list_fields: list[str], statuses: list[str]) -> None:
    # 1. 健康检查
    status, payload = call(base, "GET", "/api/health")
    check("健康检查 /api/health", status == 200 and payload.get("ok") is True,
          "构建问题" if status != 200 else "数据问题", f"状态码={status}，返回={payload}")

    # 2. 列表：字段齐全、规格型号非空、结存数量为数字
    status, payload = call(base, "GET", "/api/material")
    if status != 200:
        check("列表接口可访问", False, "构建问题", f"状态码={status}，返回={payload}")
        return  # 列表都拿不到，后续检查没有意义
    items = payload.get("items") or []
    check("列表返回示例数据", payload.get("total", 0) >= 3 and len(items) >= 3,
          "数据问题", f"total={payload.get('total')}，items={len(items)}")
    missing_keys = [row.get("id") for row in items if any(field not in row for field in list_fields)]
    check("列表记录覆盖全部展示字段", not missing_keys,
          "数据问题", f"缺字段的记录 id：{missing_keys}，要求字段：{list_fields}")
    bad_spec = [row.get("id") for row in items if not str(row.get("规格型号") or "").strip()]
    check("列表记录规格型号非空", not bad_spec, "数据问题", f"规格型号为空的记录 id：{bad_spec}")
    bad_stock = [row.get("id") for row in items
                 if not isinstance(row.get("结存数量"), (int, float)) or isinstance(row.get("结存数量"), bool)]
    check("列表记录结存数量为数字", not bad_stock, "数据问题", f"结存数量异常的记录 id：{bad_stock}")

    # 3. 关键词筛选
    status, payload = call(base, "GET", "/api/material", query={"keyword": "MATE-0001"})
    check("按材料编号筛选", status == 200 and payload.get("total") == 1
          and payload["items"][0].get("材料编号") == "MATE-0001",
          "构建问题" if status != 200 else "数据问题",
          f"状态码={status}，total={payload.get('total')}")

    # 4. 状态筛选
    status, payload = call(base, "GET", "/api/material", query={"status": "已冻结"})
    frozen = payload.get("items") or []
    check("按状态筛选（已冻结）", status == 200 and len(frozen) >= 1
          and all(row.get("status") == "已冻结" for row in frozen),
          "构建问题" if status != 200 else "数据问题",
          f"状态码={status}，命中 {len(frozen)} 条")

    # 5. 明细
    status, payload = call(base, "GET", "/api/material/1")
    check("读取单条明细", status == 200 and "规格型号" in payload and "结存数量" in payload,
          "构建问题" if status != 200 else "数据问题", f"状态码={status}，返回={payload}")

    # 6. 不存在的明细要返回 404
    status, _ = call(base, "GET", "/api/material/999999")
    check("缺失明细返回 404", status == 404, "构建问题", f"状态码={status}")

    # 7. 缺字段登记要被拦下并说明原因
    status, payload = call(base, "POST", "/api/material",
                           {"values": {"材料编号": "MATE-9001", "材料名称": "自检材料"}})
    check("缺规格型号登记被拦截", status == 200 and payload.get("ok") is False
          and "规格型号" in str(payload.get("message", "")),
          "构建问题" if status != 200 else "数据问题", f"状态码={status}，返回={payload}")

    # 8. 正常登记
    status, payload = call(base, "POST", "/api/material",
                           {"values": {"材料编号": "MATE-9001", "材料名称": "自检材料",
                                       "规格型号": "自检-50kg/桶"}})
    created = payload.get("entry") or {}
    created_id = created.get("id")
    check("完整字段登记成功", status == 200 and payload.get("ok") is True
          and created.get("status") == statuses[0] and created_id,
          "构建问题" if status != 200 else "数据问题", f"状态码={status}，返回={payload}")
    if not created_id:
        return  # 没有新记录 id，动作流转无法自检

    # 9. 状态流转：冻结 → 解冻 → 耗尽，非法动作被拦
    for action, expect in (("冻结材料", "已冻结"), ("解冻材料", "正常可用"), ("登记耗尽", "已耗尽")):
        status, payload = call(base, "POST", f"/api/material/{created_id}/actions",
                               {"values": {"action": action}})
        entry = payload.get("entry") or {}
        check(f"动作「{action}」流转到「{expect}」",
              status == 200 and payload.get("ok") is True and entry.get("status") == expect,
              "构建问题" if status != 200 else "数据问题",
              f"状态码={status}，流转后状态={entry.get('status')}")
    status, payload = call(base, "POST", f"/api/material/{created_id}/actions",
                           {"values": {"action": "不存在的动作"}})
    check("非法动作被拦截", status == 200 and payload.get("ok") is False,
          "构建问题" if status != 200 else "数据问题", f"状态码={status}，返回={payload}")

    # 10. 导出
    status, payload = call(base, "GET", "/api/material/export")
    exported = payload.get("items") or []
    check("导出全量清单", status == 200 and payload.get("module") == "material"
          and payload.get("total") == len(exported) >= 4,
          "构建问题" if status != 200 else "数据问题",
          f"状态码={status}，total={payload.get('total')}，items={len(exported)}")

    # 11. 分页保护：size 超限要返回 400
    status, _ = call(base, "GET", "/api/material", query={"size": 500})
    check("分页上限保护（size>200 返回 400）", status == 400, "构建问题", f"状态码={status}")


def main() -> int:
    sys.path.insert(0, str(BACKEND_DIR))
    try:
        from app.routers.material import LIST_FIELDS
        from app.services.material import STATUS_ORDER
    except ImportError as error:
        print(f"✗ 后端模块导入失败：{error}")
        print("  属于构建问题，请先执行 make install 装好后端依赖。")
        return EXIT_BUILD

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    print(f"临时后端服务启动中：{base}（检查结束自动关闭）")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(BACKEND_DIR),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        if not wait_ready(base, process):
            output = ""
            if process.stdout is not None:
                output = process.stdout.read() or ""
            print("✗ 后端服务在预期时间内未能就绪，属于构建问题。服务输出：")
            print("\n".join(f"  {line}" for line in output.strip().splitlines()[-15:]))
            return EXIT_BUILD
        print(f"服务已就绪，开始逐项自检（状态序列：{' → '.join(STATUS_ORDER)}）\n")
        try:
            run_checks(base, LIST_FIELDS, STATUS_ORDER)
        except (urllib.error.URLError, ConnectionError, OSError) as error:
            check("接口连接稳定", False, "构建问题", f"请求异常：{error}")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()

    failures = [item for item in results if not item[1]]
    for label, ok, kind, detail in results:
        mark = "✓" if ok else "✗"
        line = f"{mark} {label}"
        if not ok:
            line += f" —— [{kind}] {detail}"
        print(line)
    print(f"\n共 {len(results)} 项检查，通过 {len(results) - len(failures)} 项，失败 {len(failures)} 项。")
    if failures:
        kinds = "、".join(sorted({item[2] for item in failures}))
        print(f"结论：接口自检未通过，失败类别：{kinds}，请按上面的明细处理。")
        return EXIT_DATA if all(item[2] == "数据问题" for item in failures) else EXIT_BUILD
    print("结论：养护材料接口自检全部通过。")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

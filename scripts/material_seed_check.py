"""环节一：养护材料示例数据准备与字段一致性校验。

把散落在各处的养护材料字段口径拉到一起来回核对，改字段时不用再人工逐处比对：

1. 种子数据能正常加载进内存仓库（起服务前"数据已备好"的前置条件）；
2. 每条养护材料记录字段齐全、类型正确：材料编号/材料名称/规格型号非空，
   结存数量为非负数字，状态值在允许的序列里；
3. 字段口径一致：种子数据 ↔ 业务规则(services) ↔ 接口字段(routers) ↔ 前端页面(views)。

退出码：0 通过；2 数据问题；3 构建/环境问题（依赖缺失、模块导入失败等）。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
FRONTEND_VIEW = REPO_ROOT / "frontend" / "src" / "views" / "material" / "index.vue"

sys.path.insert(0, str(BACKEND_DIR))

EXIT_OK = 0
EXIT_DATA = 2
EXIT_BUILD = 3

# 每条检查结果：(检查项, 是否通过, 失败说明)
results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append((label, ok, detail))


def parse_vue_const_array(source: str, name: str) -> list[str] | None:
    """从 index.vue 里取出 `const <name> = [...]` 的字符串数组。"""
    match = re.search(rf"const\s+{name}\s*=\s*(\[.*?\])", source, re.S)
    if not match:
        return None
    try:
        value = json.loads(match.group(1))
    except ValueError:
        return None
    return [str(item) for item in value] if isinstance(value, list) else None


def main() -> int:
    try:
        from app.routers.material import LIST_FIELDS, STATUSES
        from app.seed import SEED_ROWS
        from app.services.material import ACTION_RULES, REQUIRED_FIELDS, STATUS_ORDER
        from app.store import Store
    except ImportError as error:
        print(f"✗ 后端模块导入失败：{error}")
        print("  属于构建/环境问题，请先执行 make install 装好后端依赖。")
        return EXIT_BUILD

    # ── 1. 种子数据能加载进仓库 ─────────────────────────────
    rows = SEED_ROWS.get("material") or []
    check("种子数据存在且非空", bool(rows), "SEED_ROWS['material'] 为空或缺失")

    store_ok = True
    store_detail = ""
    try:
        loaded = Store().rows("material")
        if len(loaded) != len(rows):
            store_ok = False
            store_detail = f"仓库加载后剩 {len(loaded)} 条，种子数据是 {len(rows)} 条"
    except Exception as error:  # noqa: BLE001 - 加载失败要归为数据问题报出来
        store_ok = False
        store_detail = f"仓库加载种子数据报错：{error}"
    check("种子数据可加载进内存仓库", store_ok, store_detail)

    # ── 2. 逐行校验字段 ─────────────────────────────────────
    ids: list[object] = []
    for row in rows:
        row_id = row.get("id", "?")
        ids.append(row_id)
        missing = [field for field in REQUIRED_FIELDS if not str(row.get(field) or "").strip()]
        check(
            f"第 {row_id} 行必填字段齐全（{'、'.join(REQUIRED_FIELDS)}）",
            not missing,
            f"缺：{'、'.join(missing)}",
        )
        stock = row.get("结存数量")
        check(
            f"第 {row_id} 行结存数量为非负数字",
            isinstance(stock, (int, float)) and not isinstance(stock, bool) and stock >= 0,
            f"结存数量={stock!r}",
        )
        check(
            f"第 {row_id} 行计量单位非空",
            bool(str(row.get("计量单位") or "").strip()),
            f"计量单位={row.get('计量单位')!r}",
        )
        check(
            f"第 {row_id} 行状态值合法",
            row.get("status") in STATUS_ORDER,
            f"status={row.get('status')!r}，允许值：{'、'.join(STATUS_ORDER)}",
        )
        absent = [field for field in LIST_FIELDS if field not in row]
        check(
            f"第 {row_id} 行覆盖列表展示字段",
            not absent,
            f"缺：{'、'.join(absent)}（列表页会显示为 —）",
        )
    check("记录 id 不重复", len(set(ids)) == len(ids), f"id 列表：{ids}")

    # ── 3. 后端三处口径互相对齐 ─────────────────────────────
    check(
        "路由状态枚举与业务状态序列一致",
        STATUSES == STATUS_ORDER,
        f"路由={STATUSES}，业务={STATUS_ORDER}",
    )
    check(
        "必填字段都在列表展示字段里",
        all(field in LIST_FIELDS for field in REQUIRED_FIELDS),
        f"必填={REQUIRED_FIELDS}，列表={LIST_FIELDS}",
    )
    unknown_targets = [target for target in ACTION_RULES.values() if target not in STATUS_ORDER]
    check(
        "动作目标状态都在状态序列里",
        not unknown_targets,
        f"越界状态：{unknown_targets}",
    )

    # ── 4. 前端页面与后端口径对齐 ───────────────────────────
    if not FRONTEND_VIEW.exists():
        check("前端养护材料页面存在", False, f"找不到 {FRONTEND_VIEW}")
    else:
        source = FRONTEND_VIEW.read_text(encoding="utf-8")
        vue_columns = parse_vue_const_array(source, "columns")
        vue_statuses = parse_vue_const_array(source, "statuses")
        vue_actions = parse_vue_const_array(source, "actions")
        check(
            "前端列表列与后端列表字段一致",
            vue_columns == LIST_FIELDS,
            f"前端={vue_columns}，后端={LIST_FIELDS}",
        )
        check(
            "前端状态枚举与业务状态序列一致",
            vue_statuses == STATUS_ORDER,
            f"前端={vue_statuses}，业务={STATUS_ORDER}",
        )
        check(
            "前端动作与后端动作规则一致",
            vue_actions == list(ACTION_RULES),
            f"前端={vue_actions}，后端={list(ACTION_RULES)}",
        )

    # ── 汇总 ────────────────────────────────────────────────
    failures = [item for item in results if not item[1]]
    for label, ok, detail in results:
        mark = "✓" if ok else "✗"
        line = f"{mark} {label}"
        if not ok and detail:
            line += f" —— {detail}"
        print(line)
    print(f"\n共 {len(results)} 项检查，通过 {len(results) - len(failures)} 项，失败 {len(failures)} 项。")
    if failures:
        print("结论：示例数据准备未通过，属于数据问题，请按上面的明细修正种子数据或字段口径。")
        return EXIT_DATA
    print("结论：养护材料示例数据已备好，字段口径前后端一致。")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

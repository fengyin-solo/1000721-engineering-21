"""养护材料流水线：示例数据准备 → 后端接口自检 → 前端构建检查。

三个环节串成一条可重复执行的流程，每次运行都给出各环节的通过情况；
某个环节失败时会标明是数据问题还是构建问题，并给出只重跑该环节的命令。

用法：
    python3 scripts/run_pipeline.py                # 顺序跑全部环节
    python3 scripts/run_pipeline.py --only seed    # 只重跑某个环节：seed / api / frontend
    make check / make check-seed / make check-api / make check-frontend（等价入口）

环节脚本约定退出码：0 通过；2 数据问题；3 构建问题。前端构建非零一律按构建问题处理。
整体退出码：所选环节全部通过为 0，否则为 1。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = REPO_ROOT / "backend" / ".venv" / "bin" / "python"
FRONTEND_DIR = REPO_ROOT / "frontend"

KIND_DATA = "数据问题"
KIND_BUILD = "构建问题"


@dataclass
class Stage:
    key: str
    name: str
    rerun: str  # 只重跑该环节的命令提示
    command: list[str] = field(default_factory=list)
    cwd: Path = REPO_ROOT


@dataclass
class StageResult:
    stage: Stage
    ok: bool
    kind: str  # "通过" / 数据问题 / 构建问题
    output: str
    seconds: float
    note: str = ""


def stages() -> list[Stage]:
    return [
        Stage(
            key="seed",
            name="示例数据准备",
            rerun="make check-seed",
            command=[str(VENV_PYTHON), str(REPO_ROOT / "scripts" / "material_seed_check.py")],
        ),
        Stage(
            key="api",
            name="后端接口自检",
            rerun="make check-api",
            command=[str(VENV_PYTHON), str(REPO_ROOT / "scripts" / "material_api_check.py")],
        ),
        Stage(
            key="frontend",
            name="前端构建检查",
            rerun="make check-frontend",
            command=["npm", "run", "build"],
            cwd=FRONTEND_DIR,
        ),
    ]


def classify(exit_code: int, stage_key: str) -> str:
    # 前端构建（vue-tsc/vite）的退出码语义与自检脚本不同：
    # tsc 编译错误就退出 2，但它仍是构建问题；前端环节任何非零都算构建问题。
    if stage_key == "frontend":
        return KIND_BUILD
    return {2: KIND_DATA, 3: KIND_BUILD}.get(exit_code, KIND_BUILD)


def run_stage(stage: Stage) -> StageResult:
    if stage.key in ("seed", "api") and not VENV_PYTHON.exists():
        return StageResult(
            stage=stage, ok=False, kind=KIND_BUILD, output="", seconds=0.0,
            note=f"找不到后端虚拟环境 {VENV_PYTHON}，请先执行 make install",
        )
    if stage.key == "frontend" and not (FRONTEND_DIR / "node_modules").exists():
        print("  提示：frontend/node_modules 不存在，先执行 npm install …")
        install = subprocess.run(
            ["npm", "install", "--no-audit", "--no-fund"],
            cwd=str(FRONTEND_DIR), capture_output=True, text=True,
        )
        if install.returncode != 0:
            return StageResult(
                stage=stage, ok=False, kind=KIND_BUILD,
                output=(install.stdout or "") + (install.stderr or ""), seconds=0.0,
                note="npm install 失败，请检查网络与依赖配置",
            )
    started = time.monotonic()
    try:
        completed = subprocess.run(
            stage.command, cwd=str(stage.cwd), capture_output=True, text=True,
        )
    except FileNotFoundError as error:
        return StageResult(
            stage=stage, ok=False, kind=KIND_BUILD, output="", seconds=0.0,
            note=f"命令不可用：{error}（npm/python 未安装或不在 PATH）",
        )
    elapsed = time.monotonic() - started
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    ok = completed.returncode == 0
    return StageResult(
        stage=stage, ok=ok, kind="通过" if ok else classify(completed.returncode, stage.key),
        output=output.strip(), seconds=elapsed,
    )


def print_summary(results: list[StageResult]) -> None:
    print("\n" + "=" * 60)
    print("流水线汇总")
    print("=" * 60)
    for result in results:
        mark = "✓" if result.ok else "✗"
        status = "通过" if result.ok else f"失败（{result.kind}）"
        print(f"{mark} {result.stage.name:<10} {status}  用时 {result.seconds:.1f}s")
        if result.note:
            print(f"    {result.note}")
        if not result.ok:
            print(f"    只重跑该环节：{result.stage.rerun}")
    failed = [result for result in results if not result.ok]
    print("-" * 60)
    if failed:
        kinds = "、".join(sorted({result.kind for result in failed}))
        print(f"结果：未通过（{kinds}）。修复后可只重跑失败环节，不必整条重跑。")
    else:
        print("结果：全部通过。养护材料页面与接口可照常使用。")


def main() -> int:
    parser = argparse.ArgumentParser(description="养护材料流水线：数据准备 → 接口自检 → 前端构建")
    parser.add_argument("--only", choices=[stage.key for stage in stages()],
                        help="只跑指定环节（seed/api/frontend），用于失败后重跑")
    args = parser.parse_args()

    selected = stages()
    if args.only:
        selected = [stage for stage in selected if stage.key == args.only]

    print("养护材料流水线：" + " → ".join(stage.name for stage in selected))
    results: list[StageResult] = []
    for index, stage in enumerate(selected, start=1):
        print(f"\n[{index}/{len(selected)}] {stage.name} …")
        result = run_stage(stage)
        if result.output:
            print("\n".join(f"  {line}" for line in result.output.splitlines()))
        mark = "✓" if result.ok else "✗"
        status = "通过" if result.ok else f"失败（{result.kind}）"
        print(f"{mark} {stage.name}{status}，用时 {result.seconds:.1f}s")
        results.append(result)

    print_summary(results)
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    if not shutil.which("npm"):
        print("提示：当前环境找不到 npm，前端构建检查环节会失败。", file=sys.stderr)
    sys.exit(main())

#!/usr/bin/env python
"""Skill 用的一键脚本：解析日报 → 结论 + 图 + 表。

为什么单独提供这个脚本（而不是让 Skill 直接调 CLI）：
Skill 的使用者往往只想要"结论"，而这个脚本把 CLI 的三个动作合成一步，
并把最容易出错的"仓库根目录/环境"问题在开头就检查掉，报错也写成可操作的中文提示。

用法：
    python skill/ddr-parse/scripts/run.py <日报文件或目录> [--out out]

输出：
    out/<文件名>.json        标准化数据（对齐 WITSML DrillReport）
    out/<文件名>_时间分解.csv 时间分解表
    out/<文件名>_时效分解.html 时效分解图（自包含，可直接打开）
    stdout                   时效小结（给人看的结论）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DISCLAIMER = (
    "提示：本仓库默认样本为计算机合成数据（按 IADC 标准构造），不代表真实井数据。"
    "若你提供的是真实日报，解析结论可用于工程参考，但引擎的准确率数字来自合成评测集，"
    "不能直接外推到未经适配的真实格式。"
)


def _bootstrap() -> None:
    """确保能 import ddr：先试已安装的包，再回落到仓库内的 src/。"""
    try:
        import ddr  # noqa: F401

        return
    except ImportError:
        pass
    here = Path(__file__).resolve()
    for parent in here.parents:
        src = parent / "src"
        if (src / "ddr" / "__init__.py").exists():
            sys.path.insert(0, str(src))
            return
    print(
        "错误：找不到 ddr 包。请在仓库根目录执行 `pip install -e \".[all]\"`，\n"
        "或设置 PYTHONPATH=src 后重试。",
        file=sys.stderr,
    )
    raise SystemExit(2)


_bootstrap()

from ddr.chart import write_csv, write_figure_html, write_summary_text  # noqa: E402
from ddr.pipeline import parse_files  # noqa: E402
from ddr.reader import SUPPORTED_SUFFIXES, ReaderError  # noqa: E402


def collect(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    if target.is_dir():
        return sorted(p for p in target.rglob("*") if p.suffix.lower() in SUPPORTED_SUFFIXES)
    return []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="解析钻井日报 → 结论 + 图 + 表")
    ap.add_argument("target", help="日报文件或包含日报的目录")
    ap.add_argument("--out", default="out", help="输出目录（默认 out/）")
    ap.add_argument("--quiet-disclaimer", action="store_true", help="不打印数据来源声明")
    args = ap.parse_args(argv)

    target = Path(args.target)
    files = collect(target)
    if not files:
        print(
            f"没有在 {target} 找到可解析的日报文件（支持：{sorted(SUPPORTED_SUFFIXES)}）",
            file=sys.stderr,
        )
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"待解析：{len(files)} 份日报")
    if not args.quiet_disclaimer:
        print(DISCLAIMER)
    print()

    results = parse_files(files)
    rc = 0
    for f, res in zip(files, results):
        try:
            payload = res.report.to_dict()
        except Exception as exc:
            # 解析结果不完整（如 source/time_verification 缺失）也要说清楚，不能静默跳过
            print(f"✗ {f.name}：结果不完整（{type(exc).__name__}: {exc}）", file=sys.stderr)
            rc = 1
            continue

        stem = f.stem
        (out_dir / f"{stem}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        write_csv(payload, out_dir / f"{stem}_时间分解.csv")
        write_figure_html(
            payload,
            out_dir / f"{stem}_时效分解.html",
            extra_note=f"源文件：{f.name}　（由 DDR Parser 生成）",
        )

        print("=" * 72)
        print(write_summary_text(payload))
        if res.warnings:
            print("\n解析提示（需人工确认的项）：")
            for w in res.warnings:
                print(f"  · {w}")
        print()

    print("=" * 72)
    print(f"输出目录：{out_dir.resolve()}")
    print("  *.json 标准化数据 | *_时间分解.csv 时效表 | *_时效分解.html 时效图")
    return rc


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReaderError as exc:
        print(f"读取失败：{exc}", file=sys.stderr)
        raise SystemExit(3)

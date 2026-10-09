"""命令行入口。

对应项目计划书 阶段 0 交付物："写一个脚本：输入 PDF → 输出标准化 JSON"，
以及"生成第一张时效分解饼图"。所有子命令共用同一套解析引擎，
保证 CLI、Skill、看板三条入口的行为完全一致。

用法示例：
    ddr parse 日报.pdf                       # 解析单份 → JSON
    ddr parse 日报.pdf --csv out/t.csv        # 同时导出时间分解 CSV
    ddr chart 日报.pdf                        # 生成时效分解图 HTML
    ddr batch data/samples/samples            # 批量解析整个目录
    ddr stats  --data data/samples            # 数据集特征统计（覆盖/合规率/字段可用率）
    ddr check  data/samples/dataset.json      # 数据集 ground truth 自检
    ddr eval   data/samples                   # 跑评测出准确率报告
    ddr robust                                # 跑畸形日报鲁棒性测试
    ddr info                                  # 打印支持的模板与操作码字典
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import __version__
from .codes import load_code_map
from .detect import TEMPLATE_SIGNATURES
from .extract import TEMPLATE_SPECS
from .model import validate_payload
from .pipeline import parse_file, parse_files
from .reader import SUPPORTED_SUFFIXES, ReaderError


def _json_out(payload: dict, out: Path | None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"已写入 {out}")
    else:
        print(text)


def cmd_parse(args) -> int:
    src = Path(args.input)
    if not src.exists():
        print(f"文件不存在：{src}", file=sys.stderr)
        return 2
    try:
        res = parse_file(src, template_hint=args.template)
    except ReaderError as exc:
        print(f"读取失败：{exc}", file=sys.stderr)
        return 3

    payload = res.report.to_dict()
    if not args.no_validate:
        errors = validate_payload(payload)
        if errors:
            print("⚠ JSON Schema 校验未通过：", file=sys.stderr)
            for e in errors[:20]:
                print(f"  - {e}", file=sys.stderr)
            if args.strict:
                return 4

    if args.out or not getattr(args, "no_json", False):
        _json_out(payload, Path(args.out) if args.out else None)

    if args.csv:
        from .chart import write_csv

        write_csv(payload, args.csv)
        print(f"已写入 {args.csv}")
    if args.chart:
        from .chart import write_figure_html, write_summary_text

        write_figure_html(payload, args.chart, extra_note=f"源文件：{src.name}")
        print(f"已写入 {args.chart}")
        print()
        print(write_summary_text(payload))
    if args.summary and not args.chart:
        from .chart import write_summary_text

        print()
        print(write_summary_text(payload))

    if res.warnings and not args.quiet:
        print("\n解析提示：", file=sys.stderr)
        for w in res.warnings[:12]:
            print(f"  · {w}", file=sys.stderr)
    return 0


def cmd_batch(args) -> int:
    root = Path(args.input)
    if not root.exists():
        print(f"路径不存在：{root}", file=sys.stderr)
        return 2
    files = (
        [root]
        if root.is_file()
        else sorted(p for p in root.rglob("*") if p.suffix.lower() in SUPPORTED_SUFFIXES)
    )
    if not files:
        print(f"{root} 下没有可解析的日报文件（支持 {sorted(SUPPORTED_SUFFIXES)}）", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    results = parse_files(files)
    ok = fail = 0
    summary: list[dict] = []
    for f, res in zip(files, results):
        payload = res.report.to_dict()
        target = out_dir / f"{f.stem}.json"
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tv = payload["time_verification"]
        summary.append(
            {
                "file": f.name,
                "template": res.detection.template_id,
                "entries": len(payload["time_log"]),
                "sum_hours": tv["sum_hours"],
                "valid_24h": tv["valid"],
                "npt_hours": round(
                    sum(e["hours"] for e in payload["time_log"] if e["is_npt"]), 2
                ),
            }
        )
        ok += 1
        if not tv["valid"]:
            fail += 1
    (out_dir / "_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"已解析 {ok} 份 → {out_dir}")
    print(f"其中时间分解不合规（未加总到 24 h）的有 {fail} 份 —— 详见 _summary.json")
    for row in summary[:10]:
        flag = "OK " if row["valid_24h"] else "违规"
        print(
            f"  {flag} {row['file'][:44]:<44} {row['template']:<14} "
            f"{row['entries']:>3} 条  {row['sum_hours']:>6.2f} h  NPT {row['npt_hours']:>5.2f} h"
        )
    if len(summary) > 10:
        print(f"  ... 其余 {len(summary) - 10} 份见 _summary.json")
    return 0


def cmd_chart(args) -> int:
    """生成时效分解图与 CSV。

    刻意不打印 JSON——图表场景下用户要的是结论与文件，不是几百行结构化数据。
    """
    args.out = None
    args.csv = args.csv
    args.chart = args.chart
    args.summary = True
    args.no_json = True
    args.no_validate = False
    args.strict = False
    args.quiet = False
    return cmd_parse(args)


def cmd_check(args) -> int:
    from .datacheck import DatasetError, check_dataset_file

    try:
        res = check_dataset_file(args.data)
    except DatasetError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    print(f"已检查 {res.checked_wells} 口井 / {res.checked_days} 份日报，问题 {len(res.problems)} 处")
    for p in res.problems[:40]:
        print("  ✗", p)
    return 0 if res.ok else 1


def cmd_eval(args) -> int:
    from .evaluate import evaluate_dataset, render_markdown

    try:
        rep = evaluate_dataset(args.data)
    except (FileNotFoundError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    # 没有可评测样本：报错退出，而不是产出一份"准确率 —"的报告让人误以为跑通了
    if rep.empty:
        print(
            f"错误：{args.data} 没有可评测的样本。"
            + (f"manifest 中 {len(rep.missing)} 条登记项的样本文件缺失。" if rep.missing else "items 为空。"),
            file=sys.stderr,
        )
        return 2

    md = render_markdown(rep)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    overall = rep.overall()
    print(f"总体准确率 {overall['accuracy']:.2%}（{overall['ok']}/{overall['total']} 项断言）")
    for g, s in sorted(rep.group_stats().items(), key=lambda kv: (kv[1]["accuracy"] or 0)):
        acc = "—" if s["accuracy"] is None else f"{s['accuracy']:.2%}"
        print(f"  {g:<12} {acc:>8}  ({s['ok']}/{s['total']})")
    for s in rep.samples:
        if s.error:
            print(f"  ⚠ {s.file}: {s.error}", file=sys.stderr)
    if rep.missing:
        print(f"  ⚠ 跳过 {len(rep.missing)} 条登记项（样本文件缺失）", file=sys.stderr)
    print(f"报告已写入 {out}")
    return 0


def cmd_robust(args) -> int:
    from .robustness import render_report, run_all

    results, work = run_all()
    md = render_report(results)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    bad = 0
    for r in results:
        passed = sum(1 for a in r.assertions if a[1])
        flag = "OK  " if not r.error and passed == len(r.assertions) else "FAIL"
        if flag == "FAIL":
            bad += 1
        print(f"  {flag} {r.name:<18} {passed}/{len(r.assertions)} {r.error or ''}")
    print(f"报告已写入 {out}")
    import shutil

    shutil.rmtree(work, ignore_errors=True)
    return 1 if bad else 0


def cmd_stats(args) -> int:
    from .stats import compute_stats, render_text

    try:
        st = compute_stats(args.data)
    except (FileNotFoundError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    if st.sample_count == 0:
        print(f"错误：{args.data} 的 manifest 里没有样本（items 为空）", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(st.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_text(st))
        print()
        print("提示：`--json` 可拿到机器可读版本；`ddr eval` 可拿到字段准确率。")
    return 0


def cmd_privacy(args) -> int:
    """私有数据闸门：阻止真实日报被误提交进版本库。

    退出码 1 表示发现必须处理的问题 —— CI 里据此阻断合并。
    """
    from .privacy import render_text, scan_dataset

    res = scan_dataset(args.data)
    print(render_text(res, args.data))
    if args.json:
        print()
        print(
            json.dumps(
                {
                    "data_dir": str(args.data),
                    "scanned_items": res.scanned_items,
                    "scanned_days": res.scanned_days,
                    "allowlist_size": res.allowlist_size,
                    "ok": res.ok,
                    "findings": [
                        {
                            "severity": f.severity,
                            "location": f.location,
                            "message": f.message,
                            "evidence": f.evidence,
                        }
                        for f in res.findings
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0 if res.ok else 1


def cmd_info(args) -> int:
    cm = load_code_map()
    print(f"ddr-parser {__version__}")
    print(f"支持的文件类型：{sorted(SUPPORTED_SUFFIXES)}")
    print()
    print("已知日报模板：")
    for sig in TEMPLATE_SIGNATURES:
        spec = TEMPLATE_SPECS.get(sig.template_id)
        print(f"  - {sig.template_id:<14} {sig.label}（默认单位制：{sig.unit_hint}）")
        if spec:
            print(f"      区块级单位覆盖：header={spec.header_unit_overrides or '{}'} "
                  f"bit={spec.bit_unit_overrides or '{}'} mud={spec.mud_unit_overrides or '{}'}")
    print()
    print(f"操作码字典（{len(cm.codes)} 项）：")
    for code, spec in sorted(cm.codes.items()):
        print(f"  {code:<14} {spec.label_zh:<16} 类别={spec.default_category:<11} 别名 {len(spec.aliases)} 条")
    return 0


def build_parser():
    import argparse

    ap = argparse.ArgumentParser(
        prog="ddr",
        description="钻井日报解析引擎（DDR Parser）—— PDF/Excel → 标准化 JSON → 时效分解图",
    )
    ap.add_argument("--version", action="version", version=f"ddr-parser {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("parse", help="解析单份日报 → 标准化 JSON")
    p.add_argument("input")
    p.add_argument("--out", help="JSON 输出路径（默认打印到终端）")
    p.add_argument("--csv", help="同时导出时间分解 CSV")
    p.add_argument("--chart", help="同时生成时效分解图 HTML")
    p.add_argument("--summary", action="store_true", help="打印时效小结")
    p.add_argument("--template", help="强制指定模板 ID（跳过模板识别）")
    p.add_argument(
        "--no-json",
        action="store_true",
        help="不打印标准化 JSON（只要结论/图/表时用，避免刷屏）",
    )
    p.add_argument("--no-validate", action="store_true", help="跳过 JSON Schema 校验")
    p.add_argument("--strict", action="store_true", help="Schema 校验失败时返回非零退出码")
    p.add_argument("--quiet", action="store_true", help="不打印解析提示")
    p.set_defaults(func=cmd_parse)

    p = sub.add_parser("chart", help="生成时效分解图 HTML 与 CSV")
    p.add_argument("input")
    p.add_argument("--chart", default=None, help="HTML 输出路径")
    p.add_argument("--csv", default=None, help="CSV 输出路径")
    p.set_defaults(func=cmd_chart)

    p = sub.add_parser("batch", help="批量解析目录下的日报")
    p.add_argument("input")
    p.add_argument("--out", default="out/parsed")
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("check", help="校验数据集 ground truth 与日报内容是否一致")
    p.add_argument("--data", default="data/samples/dataset.json")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("eval", help="跑评测集并输出分字段准确率报告")
    p.add_argument("--data", default="data/samples")
    p.add_argument("--out", default="data/samples/evaluation_report.md")
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("robust", help="跑畸形日报鲁棒性测试")
    p.add_argument("--out", default="data/samples/robustness_report.md")
    p.set_defaults(func=cmd_robust)

    p = sub.add_parser("stats", help="统计数据集特征（覆盖范围、合规率、字段可用率）")
    p.add_argument("--data", default="data/samples")
    p.add_argument("--json", action="store_true", help="输出 JSON 而非文本")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("privacy", help="私有数据闸门：检查样本是否含未脱敏的真实信息")
    p.add_argument("--data", default="data/samples")
    p.add_argument("--json", action="store_true", help="输出 JSON 而非文本")
    p.set_defaults(func=cmd_privacy)

    p = sub.add_parser("info", help="打印支持的模板与操作码字典")
    p.set_defaults(func=cmd_info)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    for attr, default in (
        ("out", None), ("csv", None), ("chart", None), ("summary", False),
        ("template", None), ("no_validate", False), ("strict", False), ("quiet", False),
        ("no_json", False), ("json", False),
    ):
        if not hasattr(args, attr):
            setattr(args, attr, default)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

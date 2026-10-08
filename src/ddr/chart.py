"""时效分解可视化（项目计划书 阶段 0 交付物：第一张时效分解饼图）。

输出自包含的 HTML（plotly 内嵌 JS），不依赖网络即可打开查看。
图表口径与 KPI 指标严格对应（productive / flat / npt 三类），
颜色固定，便于跨井对比时不会因为配色变化误读。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# 固定配色：productive=绿（推进目标）、flat=灰蓝（计划内非钻进）、npt=红（非计划停工）
CATEGORY_COLORS = {
    "productive": "#2E9E5B",
    "flat": "#5B7C99",
    "npt": "#C0392B",
    "UNKNOWN": "#B0B0B0",
}
CATEGORY_LABELS = {
    "productive": "有效生产时间",
    "flat": "Flat Time 计划内非钻进",
    "npt": "NPT 非生产时间",
    "UNKNOWN": "未解释/未识别",
}


def _require_plotly():
    try:
        import plotly.graph_objects as go  # type: ignore
        from plotly.subplots import make_subplots  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("缺少 plotly：pip install plotly（或 pip install -e .[dash]）") from exc
    return go, make_subplots


def _aggregate(payload: dict[str, Any]) -> dict[str, float]:
    agg: dict[str, float] = {}
    for e in payload.get("time_log", []):
        cat = e.get("category") or ("npt" if e.get("is_npt") else "flat")
        if e.get("code") == "UNKNOWN":
            cat = "UNKNOWN"
        agg[cat] = round(agg.get(cat, 0.0) + float(e.get("hours") or 0), 2)
    return agg


def category_figure(payload: dict[str, Any], *, title: str = "24 小时时效分解"):
    """时效分解饼图 + 按操作码的条形图。返回 plotly Figure。"""
    go, make_subplots = _require_plotly()
    agg = _aggregate(payload)
    order = [c for c in ("productive", "flat", "npt", "UNKNOWN") if c in agg]
    total = sum(agg.values()) or 1.0

    fig = make_subplots(
        rows=1,
        cols=2,
        specs=[[{"type": "domain"}, {"type": "xy"}]],
        column_widths=[0.42, 0.58],
        subplot_titles=("时间类别占比（IADC 24 小时硬约束）", "各操作码时长"),
    )

    fig.add_trace(
        go.Pie(
            labels=[CATEGORY_LABELS[c] for c in order],
            values=[agg[c] for c in order],
            marker={"colors": [CATEGORY_COLORS[c] for c in order]},
            hole=0.45,
            textinfo="label+percent",
            textfont={"size": 11},
            sort=False,
        ),
        row=1,
        col=1,
    )

    codes: dict[str, float] = {}
    for e in payload.get("time_log", []):
        code = e.get("code") or "UNKNOWN"
        codes[code] = round(codes.get(code, 0.0) + float(e.get("hours") or 0), 2)
    code_items = sorted(codes.items(), key=lambda kv: -kv[1])
    fig.add_trace(
        go.Bar(
            x=[v for _k, v in code_items],
            y=[k for k, _v in code_items],
            orientation="h",
            marker_color="#3D6E9E",
            text=[f"{v:.1f} h" for _k, v in code_items],
            textposition="outside",
        ),
        row=1,
        col=2,
    )

    npt_h = agg.get("npt", 0.0)
    fig.update_layout(
        title=f"{title}　|　合计 {total:.2f} h　|　NPT {npt_h:.2f} h（{npt_h / total:.1%}，行业基准 20%–25%）",
        showlegend=False,
        height=460,
        margin={"l": 60, "r": 60, "t": 80, "b": 40},
        font={"size": 11},
    )
    fig.update_yaxes(autorange="reversed", row=1, col=2)
    return fig


def write_figure_html(
    payload: dict[str, Any],
    out_path: str | Path,
    *,
    title: str = "24 小时时效分解",
    extra_note: str = "",
) -> Path:
    """把时效分解图写成自包含 HTML（可直接用浏览器打开，无需服务器）。"""
    fig = category_figure(payload, title=title)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    html = fig.to_html(include_plotlyjs="inline", full_html=True)
    if extra_note:
        note = f'<p style="font:12px/1.6 sans-serif;color:#666;margin:8px 24px">{extra_note}</p>'
        html = html.replace("<body>", f"<body>{note}", 1)
    out.write_text(html, encoding="utf-8")
    return out


def write_csv(payload: dict[str, Any], out_path: str | Path) -> Path:
    """把时间分解导出 CSV（项目计划书 阶段 0 交付物：PDF → 时间分解表 → CSV）。"""
    import csv

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "序号", "开始", "结束", "历时(h)", "操作码", "类别", "作业内容",
        "是否NPT", "NPT归因", "NPT责任", "ILT动作", "时长来源",
    ]
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(fields)
        for i, e in enumerate(payload.get("time_log", []), 1):
            w.writerow(
                [
                    i,
                    e.get("start") or "",
                    e.get("end") or "",
                    e.get("hours"),
                    e.get("code"),
                    e.get("category"),
                    e.get("operation") or "",
                    "是" if e.get("is_npt") else "否",
                    e.get("npt_category") or "",
                    e.get("npt_responsibility") or "",
                    e.get("ilt_action") or "",
                    e.get("duration_source") or "",
                ]
            )
    return out


def write_summary_text(payload: dict[str, Any]) -> str:
    """给终端/对话场景用的纯文本时效小结。"""
    agg = _aggregate(payload)
    total = sum(agg.values()) or 1.0
    tv = payload.get("time_verification") or {}
    lines = [
        f"井号：{payload['well'].get('well_name')}　日报编号：{payload['report'].get('report_no')}　"
        f"日期：{(payload['report'].get('dtim_start') or '')[:10]}",
        f"井深 MD：{payload['report'].get('md_m')} m　当日进尺：{payload['report'].get('progress_m')} m　"
        f"平均钻速：{payload['report'].get('rop_av_m_per_h')} m/h",
        "",
        f"时间分解合计：{tv.get('sum_hours')} h　合规（IADC 24h）：{'是' if tv.get('valid') else '否'}",
    ]
    for cat in ("productive", "flat", "npt", "UNKNOWN"):
        if cat in agg:
            lines.append(f"  {CATEGORY_LABELS[cat]}: {agg[cat]:.2f} h（{agg[cat] / total:.1%}）")
    if tv.get("unaccounted_hours"):
        lines.append(f"  ⚠ 日报未解释缺口：{tv['unaccounted_hours']} h（已补记 UNKNOWN，请人工回查原件）")
    for m in tv.get("messages", []):
        lines.append(f"  · {m}")
    return "\n".join(lines)


def _main(argv: list[str] | None = None) -> int:
    import argparse

    from .pipeline import parse_file

    ap = argparse.ArgumentParser(description="生成时效分解图（HTML）与 CSV")
    ap.add_argument("input", help="日报文件（PDF/XLSX）")
    ap.add_argument("--html", default=None, help="输出 HTML 路径")
    ap.add_argument("--csv", default=None, help="输出 CSV 路径")
    args = ap.parse_args(argv)

    res = parse_file(args.input)
    payload = res.report.to_dict()
    stem = Path(args.input).stem
    html = Path(args.html) if args.html else Path("out") / f"{stem}_时效分解.html"
    csv_path = Path(args.csv) if args.csv else Path("out") / f"{stem}_时间分解.csv"
    write_figure_html(payload, html, extra_note="合成/真实日报样本 · 由 DDR Parser 生成")
    write_csv(payload, csv_path)
    print(write_summary_text(payload))
    print(f"\n图：{html}\n表：{csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

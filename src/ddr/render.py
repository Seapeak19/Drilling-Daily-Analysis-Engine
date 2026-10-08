"""把合成日报渲染为真实版式的 PDF / Excel 样本。

三种格式刻意在**版式、语言、单位制、时间写法**上互不相同，用于验证解析器的
格式无关性（项目计划书 8.1「日报格式碎片化」风险）：

| 模板 ID | 版式 | 语言 | 单位制 | 时间写法 |
|---|---|---|---|---|
| `cn_vertical`   | A4 纵向，表头单列纵向排布       | 中文 | 公制 | HH:MM + 小时 |
| `iadc_classic`  | A4 横向，IADC 表头两列 + 分区 | 英文 | **英制** | HH:MM + 累计 |
| `regional_xls`  | Excel 多区块，合并单元格标题   | 中文 | 公制 | HH:MM 起止 |

⚠ 全部为计算机合成数据，不代表任何真实井或真实作业记录。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .fonts import register_cjk_fonts

TEMPLATES: dict[str, dict[str, str]] = {
    "cn_vertical": {"label": "中文纵向版（作业者标准）", "format": "pdf", "units": "metric", "lang": "zh"},
    "iadc_classic": {"label": "IADC Classic（英制）", "format": "pdf", "units": "imperial", "lang": "en"},
    "regional_xls": {"label": "区域公司 Excel 版", "format": "xlsx", "units": "metric", "lang": "zh"},
}

FT_PER_M = 3.280839895
LB_PER_KGF = 2.2046226
GCC_PER_PPG = 0.1198264


# --------------------------------------------------------------------- 单位换算
def m2ft(x: float | None) -> float | None:
    return round(x * FT_PER_M, 2) if x is not None else None


def kg2klbf(kgf: float | None) -> float | None:
    return round(kgf * LB_PER_KGF / 1000.0, 1) if kgf is not None else None


def gcc2ppg(x: float | None) -> float | None:
    """g/cm3 → ppg（磅/加仑）。1 ppg = 0.1198264 g/cm3。"""
    if x is None:
        return None
    return round(x / GCC_PER_PPG, 2)


def lps2gpm(x: float | None) -> float | None:
    if x is None:
        return None
    return round(x / 0.0630902, 1)


def m3_to_bbl(x: float | None) -> float | None:
    if x is None:
        return None
    return round(x / 0.1589873, 0)


def fmt(x: Any, nd: int = 2, dash: str = "—") -> str:
    if x is None or x == "":
        return dash
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def _txt(s: Any) -> str:
    return str(s if s is not None else "")


class FontUnavailableError(RuntimeError):
    """系统中找不到可用的中文字体，无法渲染中文日报样本。"""


def _ensure_cjk_font() -> None:
    """渲染中文样本前必须确认中文字体可用。

    找不到字体时 reportlab 会静默退回 Helvetica，产出的是**满屏方块的 PDF**：
    样本看起来生成了，实则不可用，还会污染评测集。因此这里显式失败。
    （fonts.has_cjk_font 的注释早就写明"调用方应警告"，但此前没有任何地方调用它。）
    """
    from .fonts import has_cjk_font

    if not has_cjk_font():
        raise FontUnavailableError(
            "系统中找不到可用的中文字体（已尝试 simhei / NotoSansSC / Deng / msyh / simsun 等）。"
            "渲染中文日报样本至少需要一种。Linux 上可执行："
            "apt-get install fonts-noto-cjk fonts-wqy-zenhei"
        )


# --------------------------------------------------------------------- PDF 基座
def _build_pdf(path: Path, flowables: list[Any], *, landscape: bool, title: str, margins: float = 0.55) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.pagesizes import landscape as ls
    from reportlab.platypus import SimpleDocTemplate

    _ensure_cjk_font()
    path.parent.mkdir(parents=True, exist_ok=True)
    pagesize = ls(A4) if landscape else A4
    doc = SimpleDocTemplate(
        str(path),
        pagesize=pagesize,
        title=title,
        author="DDR Parser Sample Generator",
        subject="合成钻井日报样本（非真实数据）",
        leftMargin=margins * 72,
        rightMargin=margins * 72,
        topMargin=margins * 72,
        bottomMargin=margins * 72,
    )
    doc.build(flowables)


def _table_style():
    from reportlab.lib import colors
    from reportlab.platypus import TableStyle

    reg, bold = register_cjk_fonts()
    return TableStyle(
        [
            ("FONTNAME", (0, 0), (-1, 0), bold),
            ("FONTNAME", (0, 1), (-1, -1), reg),
            ("FONTSIZE", (0, 0), (-1, -1), 6.4),
            ("LEADING", (0, 0), (-1, -1), 8.0),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.Color(0.87, 0.90, 0.94)),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.Color(0.35, 0.35, 0.35)),
            ("TOPPADDING", (0, 0), (-1, -1), 1.6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1.6),
        ]
    )


# ---------------------------------------------------------------- 模板一：中文纵向
def render_cn_vertical(payload: dict[str, Any], path: Path) -> Path:
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

    reg, bold = register_cjk_fonts()
    well, rep = payload["well"], payload["report"]
    h1 = ParagraphStyle("h1", fontName=bold, fontSize=13, leading=16, alignment=1)
    sec = ParagraphStyle("sec", fontName=bold, fontSize=8, leading=10, spaceBefore=4, spaceAfter=2)
    body = ParagraphStyle("body", fontName=reg, fontSize=7, leading=9.5)
    small = ParagraphStyle("small", fontName=reg, fontSize=6, leading=8, textColor=colors.Color(0.35, 0.35, 0.35))
    cell = ParagraphStyle("cell", fontName=reg, fontSize=6.2, leading=7.6)

    story: list[Any] = [
        Paragraph("钻 井 日 报", h1),
        Paragraph(
            "（合成样本，非真实数据 · 模板 cn_vertical · 生成器 DDR Parser）",
            ParagraphStyle("sub", fontName=reg, fontSize=6.5, leading=8, alignment=1, textColor=colors.grey),
        ),
        Spacer(1, 3 * mm),
    ]

    # 表头：单列纵向排布（故意不用左右两栏，考验版面分析）
    hdr_rows = [
        ["井 号", _txt(well.get("well_name"))],
        ["构 造", _txt(well.get("field"))],
        ["作业者", _txt(well.get("operator"))],
        ["钻井承包商", _txt(well.get("contractor"))],
        ["钻 机", _txt(well.get("rig"))],
        ["日报编号", f"第 {rep.get('report_no')} 号"],
        ["钻井天数", f"{fmt(rep.get('etim_spud_days'), 1)} 天"],
        ["作业日期", f"{rep.get('dtim_start', '')[:10]} 00:00 至 {rep.get('dtim_end', '')[:10]} 00:00"],
        ["井 深 (MD)", f"{fmt(rep.get('md_m'))} m"],
        ["垂 深 (TVD)", f"{fmt(rep.get('tvd_m'))} m"],
        ["当日进尺", f"{fmt(rep.get('progress_m'))} m"],
        ["井眼直径", f"{fmt(rep.get('hole_diameter_in'), 2)} in"],
        ["平均钻速", f"{fmt(rep.get('rop_av_m_per_h'))} m/h"],
        ["钻进时间", f"{fmt(rep.get('etim_drill_h'))} h"],
        ["井 况", _txt(rep.get("well_status"))],
    ]
    ht = Table(hdr_rows, colWidths=[70, 145])
    ht.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (0, -1), bold),
                ("FONTNAME", (1, 0), (1, -1), reg),
                ("FONTSIZE", (0, 0), (-1, -1), 6.6),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.Color(0.4, 0.4, 0.4)),
                ("BACKGROUND", (0, 0), (0, -1), colors.Color(0.93, 0.94, 0.96)),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 1.4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.4),
            ]
        )
    )
    story += [ht, Spacer(1, 3 * mm)]

    # 时间分解
    story.append(Paragraph("二、二十四小时时间分解", sec))
    rows = [["序号", "开始", "结束", "历时(h)", "作业内容", "时间类别"]]
    cat_zh = {"productive": "有效生产", "flat": "计划内(Flat)", "npt": "NPT 非生产"}
    for i, e in enumerate(payload["time_log"], 1):
        rows.append(
            [
                str(i),
                _txt(e.get("start")),
                _txt(e.get("end")),
                fmt(e.get("hours")),
                Paragraph(_txt(e.get("operation")), cell),
                cat_zh.get(e.get("category") or "", "—"),
            ]
        )
    total = round(sum(float(e.get("hours") or 0) for e in payload["time_log"]), 2)
    rows.append(["", "", "合计", fmt(total), "必须等于 24.00 小时（IADC 硬约束）", ""])
    tt = Table(rows, colWidths=[22, 34, 34, 38, 300, 62], repeatRows=1)
    style = _table_style().getCommands()
    style += [
        ("FONTNAME", (0, len(rows) - 1), (-1, len(rows) - 1), bold),
        ("SPAN", (4, len(rows) - 1), (5, len(rows) - 1)),
        ("BACKGROUND", (0, len(rows) - 1), (-1, len(rows) - 1), colors.Color(0.90, 0.93, 0.90)),
    ]
    tt.setStyle(TableStyle(style))
    story += [tt, Spacer(1, 2.5 * mm)]

    # 钻头记录
    if payload.get("bit_records"):
        story.append(Paragraph("三、钻头记录", sec))
        brows = [["序号", "尺寸(in)", "型号", "入井(m)", "出井(m)", "进尺(m)", "时间(h)", "钻速(m/h)", "磨损分级"]]
        for b in payload["bit_records"]:
            brows.append(
                [
                    str(b.get("bit_no")),
                    fmt(b.get("size_in")),
                    f"{b.get('make', '')} {b.get('model', '')}",
                    fmt(b.get("depth_in_m")),
                    fmt(b.get("depth_out_m")),
                    fmt(b.get("footage_m")),
                    fmt(b.get("hours"), 1),
                    fmt(b.get("rop_m_per_h")),
                    _txt(b.get("dull_grade")),
                ]
            )
        bt = Table(brows, colWidths=[22, 40, 92, 55, 55, 50, 48, 55, 73], repeatRows=1)
        bt.setStyle(_table_style())
        story += [bt, Spacer(1, 2.5 * mm)]

    # 泥浆性能
    if payload.get("mud"):
        story.append(Paragraph("四、泥浆性能", sec))
        mrows = [["取样点", "井深(m)", "密度(g/cm3)", "漏斗粘度(s)", "失水(ml)", "pH", "氯根(mg/L)", "固相(%)"]]
        point_zh = {"flowline": "出口", "suction": "入口", "shaker": "振动筛", "pit": "泥浆罐", "other": "其他"}
        for m_ in payload["mud"]:
            mrows.append(
                [
                    point_zh.get(m_.get("sample_point"), _txt(m_.get("sample_point"))),
                    fmt(m_.get("depth_m"), 1),
                    fmt(m_.get("density_gcc"), 2),
                    fmt(m_.get("funnel_viscosity_s"), 0),
                    fmt(m_.get("fl_ml"), 1),
                    fmt(m_.get("ph"), 1),
                    fmt(m_.get("chlorides_mg_l"), 0),
                    fmt(m_.get("solids_pct"), 1),
                ]
            )
        mt = Table(mrows, colWidths=[52, 62, 70, 66, 52, 40, 74, 74], repeatRows=1)
        mt.setStyle(_table_style())
        story += [mt, Spacer(1, 2.5 * mm)]

    # 备注
    story.append(Paragraph("五、备注（按时间顺序）", sec))
    for r in payload.get("remarks", []):
        story.append(Paragraph(f"{r.get('time_hint', '')}　{r.get('text', '')}", body))
    story.append(Spacer(1, 2 * mm))
    if rep.get("sum_24hr"):
        story.append(Paragraph("六、本日作业摘要", sec))
        story.append(Paragraph(_txt(rep["sum_24hr"]), body))
    if rep.get("plan_24hr"):
        story.append(Paragraph("七、下步计划", sec))
        story.append(Paragraph(_txt(rep["plan_24hr"]), body))
    story.append(Spacer(1, 2 * mm))
    story.append(
        Paragraph(
            "本报表由 DDR Parser 样本生成器合成，全部数值为虚构，用于解析引擎开发与评测，不具备任何工程或商务效力。",
            small,
        )
    )
    _build_pdf(path, story, landscape=False, title=f"DDR sample {well.get('well_name')} #{rep.get('report_no')}")
    return path


# ------------------------------------------------------------- 模板二：IADC 英制
def render_iadc_classic(payload: dict[str, Any], path: Path) -> Path:
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

    reg, bold = register_cjk_fonts()
    well, rep = payload["well"], payload["report"]
    imperial = rep.get("unit_system") == "imperial"

    h1 = ParagraphStyle("h1", fontName=bold, fontSize=12, leading=14, alignment=1)
    sec = ParagraphStyle("sec", fontName=bold, fontSize=8, leading=10, spaceBefore=4, spaceAfter=2)
    body = ParagraphStyle("body", fontName=reg, fontSize=6.8, leading=8.6)
    cell = ParagraphStyle("cell", fontName=reg, fontSize=6.2, leading=7.6)

    story: list[Any] = [
        Paragraph("DAILY DRILLING REPORT", h1),
        Paragraph(
            "SYNTHETIC SAMPLE — not real well data · template iadc_classic",
            ParagraphStyle("sub", fontName=reg, fontSize=6.2, leading=7.6, alignment=1, textColor=colors.grey),
        ),
        Spacer(1, 3 * mm),
    ]

    # 英制模板：沿井深量一律换算为 ft，钻速 ft/hr，钻压 klbf，密度 ppg，排量 gpm。
    # 钻头/井眼直径仍用英寸（英制体系本身如此），不做换算。
    if imperial:
        md_s = m2ft(rep.get("md_m"))
        tvd_s = m2ft(rep.get("tvd_m"))
        rop_s = round(rep["rop_av_m_per_h"] / 0.3048, 1) if rep.get("rop_av_m_per_h") else None
        unit_md, unit_rop = "ft", "ft/hr"
    else:
        md_s, tvd_s = rep.get("md_m"), rep.get("tvd_m")
        rop_s, unit_md, unit_rop = rep.get("rop_av_m_per_h"), "m", "m/h"

    left = [
        ["Operator", _txt(well.get("operator"))],
        ["Contractor", _txt(well.get("contractor"))],
        ["Rig", _txt(well.get("rig"))],
        ["Well Name", _txt(well.get("well_name"))],
        ["API / UWI No.", _txt(well.get("api_number"))],
        ["Field", _txt(well.get("field"))],
        ["Country / State", f"{_txt(well.get('country'))} / {_txt(well.get('state'))}"],
    ]
    right = [
        ["Report No.", str(rep.get("report_no"))],
        ["Report Date", f"{rep.get('dtim_start', '')[:10]} 00:00 – {rep.get('dtim_end', '')[:10]} 00:00"],
        ["Spud Date", _txt((rep.get("dtim_spud") or "")[:10])],
        ["Days Since Spud", fmt(rep.get("etim_spud_days"), 1)],
        ["Measured Depth", f"{fmt(md_s)} {unit_md}"],
        ["True Vertical Depth", f"{fmt(tvd_s)} {unit_md}"],
        ["Avg. ROP", f"{fmt(rop_s, 1)} {unit_rop}"],
    ]
    grid = [["HOLE & WELL DATA", "", "", ""]]
    for i in range(max(len(left), len(right))):
        l = ["", ""] if i >= len(left) else left[i]
        r = ["", ""] if i >= len(right) else right[i]
        grid.append([l[0], l[1], r[0], r[1]])
    ht = Table(grid, colWidths=[95, 190, 105, 130])
    ht.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, 0), bold),
                ("SPAN", (0, 0), (-1, 0)),
                ("BACKGROUND", (0, 0), (-1, 0), colors.Color(0.85, 0.88, 0.92)),
                ("FONTNAME", (0, 1), (0, -1), bold),
                ("FONTNAME", (2, 1), (2, -1), bold),
                ("FONTNAME", (1, 1), (1, -1), reg),
                ("FONTNAME", (3, 1), (3, -1), reg),
                ("FONTSIZE", (0, 0), (-1, -1), 6.4),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.Color(0.4, 0.4, 0.4)),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 1.4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.4),
            ]
        )
    )
    story += [ht, Spacer(1, 3 * mm)]

    story.append(Paragraph("1. TIME BREAKDOWN (24 HOURS)", sec))
    rows = [["#", "From", "To", "Hours", "Cum.", "Operation / Description", "Class"]]
    cls_map = {"productive": "Productive", "flat": "Flat Time", "npt": "NPT"}
    cum = 0.0
    for i, e in enumerate(payload["time_log"], 1):
        cum = round(cum + float(e.get("hours") or 0), 2)
        rows.append(
            [
                str(i),
                _txt(e.get("start")),
                _txt(e.get("end")),
                fmt(e.get("hours")),
                fmt(cum),
                Paragraph(_txt(e.get("operation")), cell),
                cls_map.get(e.get("category") or "", "—"),
            ]
        )
    total = round(sum(float(e.get("hours") or 0) for e in payload["time_log"]), 2)
    rows.append(["", "", "TOTAL", fmt(total), "", "Must equal 24.00 hours (IADC hard constraint)", ""])
    tt = Table(rows, colWidths=[20, 36, 36, 38, 38, 280, 72], repeatRows=1)
    st = _table_style().getCommands()
    st += [
        ("FONTNAME", (0, len(rows) - 1), (-1, len(rows) - 1), bold),
        ("SPAN", (5, len(rows) - 1), (6, len(rows) - 1)),
        ("BACKGROUND", (0, len(rows) - 1), (-1, len(rows) - 1), colors.Color(0.90, 0.93, 0.90)),
    ]
    tt.setStyle(TableStyle(st))
    story += [tt, Spacer(1, 2.5 * mm)]

    if payload.get("bit_records"):
        story.append(Paragraph("2. BIT RECORD", sec))
        brows = [
            [
                "Bit No.", "Size (in)", "Make / Model", "Type", "IADC", "Depth In (m)", "Depth Out (m)",
                "Footage (m)", "Hours", "ROP (m/hr)", "Dull Grade",
            ]
        ]
        for b in payload["bit_records"]:
            brows.append(
                [
                    str(b.get("bit_no")),
                    fmt(b.get("size_in")),
                    f"{b.get('make', '')} {b.get('model', '')}",
                    _txt(b.get("type")),
                    _txt(b.get("iadc_code")),
                    fmt(b.get("depth_in_m")),
                    fmt(b.get("depth_out_m")),
                    fmt(b.get("footage_m")),
                    fmt(b.get("hours"), 1),
                    fmt(b.get("rop_m_per_h"), 1),
                    _txt(b.get("dull_grade")),
                ]
            )
        bt = Table(brows, colWidths=[34, 38, 88, 34, 34, 60, 62, 60, 38, 52, 80], repeatRows=1)
        bt.setStyle(_table_style())
        story += [bt, Spacer(1, 2.5 * mm)]

    if payload.get("mud"):
        # 泥浆密度单位与数值口径必须一致：英制模板把密度换算成 ppg 后再标 MW (ppg)，
        # 否则会出现"标注 ppg、数值却是 g/cm³"的自相矛盾日报（会让解析器的单位判定无法验证）。
        story.append(Paragraph("3. MUD PROPERTIES", sec))
        dens_header = "MW (ppg)" if imperial else "MW (g/cm3)"
        mrows = [
            [
                "Sample Point", "Depth (ft)", dens_header, "Funnel Vis (s)", "API FL (ml)", "pH",
                "Chlorides (mg/L)", "Solids (%)",
            ]
        ]
        for m_ in payload["mud"]:
            mrows.append(
                [
                    _txt(m_.get("sample_point")),
                    fmt(m2ft(m_.get("depth_m")) if imperial else m_.get("depth_m"), 1),
                    fmt(gcc2ppg(m_.get("density_gcc")) if imperial else m_.get("density_gcc")),
                    fmt(m_.get("funnel_viscosity_s"), 0),
                    fmt(m_.get("fl_ml"), 1),
                    fmt(m_.get("ph"), 1),
                    fmt(m_.get("chlorides_mg_l"), 0),
                    fmt(m_.get("solids_pct"), 1),
                ]
            )
        mt = Table(mrows, colWidths=[86, 62, 66, 74, 70, 40, 84, 66], repeatRows=1)
        mt.setStyle(_table_style())
        story += [mt, Spacer(1, 2.5 * mm)]

    story.append(Paragraph("4. REMARKS (chronological)", sec))
    for r in payload.get("remarks", []):
        story.append(Paragraph(f"{r.get('time_hint', '')}  {r.get('text', '')}", body))

    if rep.get("sum_24hr"):
        story.append(Paragraph("5. SUMMARY OF 24 HOUR OPERATIONS", sec))
        story.append(Paragraph(_txt(rep["sum_24hr"]), body))
    if rep.get("plan_24hr"):
        story.append(Paragraph("6. PLAN FOR NEXT 24 HOURS", sec))
        story.append(Paragraph(_txt(rep["plan_24hr"]), body))
    story.append(Spacer(1, 2 * mm))
    story.append(
        Paragraph(
            "Synthetic sample generated by DDR Parser for engineering development and evaluation only. "
            "All values are fictitious. Not valid for any operational or commercial purpose.",
            ParagraphStyle("note", fontName=reg, fontSize=6, leading=7.5, textColor=colors.grey),
        )
    )
    _build_pdf(path, story, landscape=True, title=f"DDR sample {well.get('well_name')} #{rep.get('report_no')}")
    return path


# ---------------------------------------------------------- 模板三：区域 Excel 版
def render_regional_xls(payload: dict[str, Any], path: Path) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    well, rep = payload["well"], payload["report"]
    wb = Workbook()
    ws = wb.active
    ws.title = "钻井日报"

    thin = Side(style="thin", color="808080")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    head_fill = PatternFill("solid", fgColor="DCE6F1")
    warn_fill = PatternFill("solid", fgColor="FFF2CC")
    title_font = Font(name="黑体", size=14, bold=True)
    head_font = Font(name="宋体", size=9, bold=True)
    body_font = Font(name="宋体", size=9)

    ws.merge_cells("A1:F1")
    ws["A1"] = "钻 井 日 报"
    ws["A1"].font = title_font
    ws["A1"].alignment = Alignment(horizontal="center", vertical="center")

    ws.merge_cells("A2:F2")
    ws["A2"] = "合成样本，非真实数据 · 模板 regional_xls · DDR Parser 生成"
    ws["A2"].font = Font(name="宋体", size=8, italic=True, color="808080")
    ws["A2"].alignment = Alignment(horizontal="center")

    r = 4
    hdr_pairs = [
        ("井号", well.get("well_name"), "日报编号", f"第 {rep.get('report_no')} 号"),
        ("构造", well.get("field"), "作业日期", f"{rep.get('dtim_start', '')[:10]} ~ {rep.get('dtim_end', '')[:10]}"),
        ("作业者", well.get("operator"), "钻井天数", f"{fmt(rep.get('etim_spud_days'), 1)} 天"),
        ("钻井承包商", well.get("contractor"), "井深 MD (m)", fmt(rep.get("md_m"))),
        ("钻机", well.get("rig"), "垂深 TVD (m)", fmt(rep.get("tvd_m"))),
        ("井况", rep.get("well_status"), "当日进尺 (m)", fmt(rep.get("progress_m"))),
        ("井眼直径 (in)", fmt(rep.get("hole_diameter_in")), "平均钻速 (m/h)", fmt(rep.get("rop_av_m_per_h"))),
    ]
    for k1, v1, k2, v2 in hdr_pairs:
        for col, val, fnt, fill in (
            (1, k1, head_font, head_fill),
            (2, v1, body_font, None),
            (4, k2, head_font, head_fill),
            (5, v2, body_font, None),
        ):
            cell = ws.cell(r, col, val)
            cell.font = fnt
            cell.border = border
            if fill:
                cell.fill = fill
        r += 1

    r += 1
    ws.cell(r, 1, "时间分解（24 小时）").font = head_font
    r += 1
    for c, name in enumerate(["起", "止", "历时(h)", "作业内容", "类别", "备注"], 1):
        cell = ws.cell(r, c, name)
        cell.font = head_font
        cell.fill = head_fill
        cell.border = border
        cell.alignment = Alignment(horizontal="center")
    r += 1
    cat_zh = {"productive": "生产", "flat": "计划内", "npt": "NPT"}
    for e in payload["time_log"]:
        cls = e.get("category") or ""
        vals = [
            e.get("start"),
            e.get("end"),
            float(e.get("hours") or 0),
            e.get("operation"),
            cat_zh.get(cls, "—"),
            e.get("npt_responsibility") or "",
        ]
        for c, v in enumerate(vals, 1):
            cell = ws.cell(r, c, v)
            cell.font = body_font
            cell.border = border
            if cls == "npt":
                cell.fill = warn_fill
        r += 1
    total = round(sum(float(e.get("hours") or 0) for e in payload["time_log"]), 2)
    ws.cell(r, 2, "合计").font = head_font
    ws.cell(r, 3, total).font = head_font
    ws.cell(r, 4, "必须等于 24.00 小时").font = head_font
    r += 2

    if payload.get("bit_records"):
        ws.cell(r, 1, "钻头记录").font = head_font
        r += 1
        for c, name in enumerate(
            ["序号", "尺寸(in)", "厂家", "型号", "入井(m)", "出井(m)", "进尺(m)", "时间(h)", "钻速(m/h)", "磨损分级"], 1
        ):
            cell = ws.cell(r, c, name)
            cell.font = head_font
            cell.fill = head_fill
            cell.border = border
        r += 1
        for b in payload["bit_records"]:
            for c, v in enumerate(
                [
                    b.get("bit_no"), b.get("size_in"), b.get("make"), b.get("model"),
                    b.get("depth_in_m"), b.get("depth_out_m"), b.get("footage_m"),
                    b.get("hours"), b.get("rop_m_per_h"), b.get("dull_grade"),
                ],
                1,
            ):
                cell = ws.cell(r, c, v)
                cell.font = body_font
                cell.border = border
            r += 1
        r += 1

    if payload.get("mud"):
        ws.cell(r, 1, "泥浆性能").font = head_font
        r += 1
        for c, name in enumerate(
            ["取样点", "井深(m)", "密度(g/cm3)", "漏斗粘度(s)", "失水(ml)", "pH", "氯根(mg/L)", "固相(%)"], 1
        ):
            cell = ws.cell(r, c, name)
            cell.font = head_font
            cell.fill = head_fill
            cell.border = border
        r += 1
        point_zh = {"flowline": "出口", "suction": "入口", "shaker": "振动筛", "pit": "泥浆罐", "other": "其他"}
        for m_ in payload["mud"]:
            for c, v in enumerate(
                [
                    point_zh.get(m_.get("sample_point"), m_.get("sample_point")),
                    m_.get("depth_m"), m_.get("density_gcc"), m_.get("funnel_viscosity_s"),
                    m_.get("fl_ml"), m_.get("ph"), m_.get("chlorides_mg_l"), m_.get("solids_pct"),
                ],
                1,
            ):
                cell = ws.cell(r, c, v)
                cell.font = body_font
                cell.border = border
            r += 1
        r += 1

    ws.cell(r, 1, "备注").font = head_font
    r += 1
    for rem in payload.get("remarks", []):
        ws.cell(r, 1, f"{rem.get('time_hint', '')} {rem.get('text', '')}").font = body_font
        r += 1
    r += 1
    if rep.get("sum_24hr"):
        ws.cell(r, 1, "本日作业摘要").font = head_font
        r += 1
        ws.cell(r, 1, rep["sum_24hr"]).font = body_font
        r += 2
    if rep.get("plan_24hr"):
        ws.cell(r, 1, "下步计划").font = head_font
        r += 1
        ws.cell(r, 1, rep["plan_24hr"]).font = body_font

    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 16
    ws.column_dimensions["C"].width = 12
    ws.column_dimensions["D"].width = 46
    ws.column_dimensions["E"].width = 12
    ws.column_dimensions["F"].width = 16

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(path))
    return path


RENDERERS = {
    "cn_vertical": (render_cn_vertical, "pdf"),
    "iadc_classic": (render_iadc_classic, "pdf"),
    "regional_xls": (render_regional_xls, "xlsx"),
}


def render_all(dataset: dict[str, Any], out_dir: str | Path) -> list[dict[str, Any]]:
    """把数据集渲染成多格式样本文件，返回 manifest 条目列表。"""
    out = Path(out_dir)
    samples_dir = out / "samples"
    truth_dir = out / "ground_truth"
    manifest: list[dict[str, Any]] = []

    for w in dataset["samples"]:
        well = w["well"]
        for d in w["days"]:
            payload = d["payload"]
            # 模板由工况决定（与 simulate.template_for_phase 保持一致），
            # 保证同一份日报在三种格式下承载同一批数据，评测结论才可归因。
            tpl_id = d["truth"].get("template_id") or list(TEMPLATES)[d["day_index"] % len(TEMPLATES)]
            renderer, ext = RENDERERS[tpl_id]
            stem = f"{well['well_name']}_D{d['truth']['report_no']:02d}_{d['report_date']}".replace("/", "-").replace(" ", "")
            fpath = samples_dir / f"{stem}.{ext}"
            renderer(payload, fpath)

            tpath = truth_dir / f"{stem}.truth.json"
            tpath.parent.mkdir(parents=True, exist_ok=True)
            tpath.write_text(
                json.dumps(
                    {
                        "_disclaimer": "合成数据 ground truth，仅用于评测解析器，不代表真实井数据。",
                        "template_id": tpl_id,
                        "source_file": str(fpath.relative_to(out)).replace("\\", "/"),
                        # 井级信息同时放在顶层，评测器不必回头读 manifest
                        "well_name": well["well_name"],
                        "operator": well.get("operator"),
                        "contractor": well.get("contractor"),
                        "rig": well.get("rig"),
                        "field": well.get("field"),
                        "report_date": d["report_date"],
                        "truth": d["truth"],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            manifest.append(
                {
                    "file": str(fpath.relative_to(out)).replace("\\", "/"),
                    "truth": str(tpath.relative_to(out)).replace("\\", "/"),
                    "template_id": tpl_id,
                    "well_name": well["well_name"],
                    "operator": well.get("operator"),
                    "contractor": well.get("contractor"),
                    "rig": well.get("rig"),
                    "field": well.get("field"),
                    "report_no": d["truth"]["report_no"],
                    "report_date": d["report_date"],
                    "phase": d["truth"]["phase"],
                    "unit_system": payload["report"]["unit_system"],
                    "synthetic": True,
                }
            )

    (out / "manifest.json").write_text(
        json.dumps(
            {
                "_disclaimer": dataset.get("_disclaimer", ""),
                "count": len(manifest),
                "templates": TEMPLATES,
                "items": manifest,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    import argparse

    from .simulate import build_dataset

    ap = argparse.ArgumentParser(description="渲染合成日报样本（PDF/Excel）")
    ap.add_argument("--out", default="data/samples")
    ap.add_argument("--wells", type=int, default=3)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--seed", type=int, default=20261008)
    ap.add_argument("--npt-probability", type=float, default=0.45)
    args = ap.parse_args(argv)

    out = Path(args.out)
    ds = build_dataset(
        wells=args.wells, days_per_well=args.days, seed=args.seed, npt_probability=args.npt_probability
    )
    out.mkdir(parents=True, exist_ok=True)
    (out / "dataset.json").write_text(json.dumps(ds, ensure_ascii=False, indent=2), encoding="utf-8")
    items = render_all(ds, out)
    print(f"已渲染 {len(items)} 份日报样本 → {out}")
    for it in items[:5]:
        print("  ", it["template_id"], it["file"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

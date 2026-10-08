"""解析流水线：文件 → 标准化 JSON（对齐 WITSML DrillReport）。

流程（对应项目计划书 3.1 Layer 2）：
    reader 读取 → detect 模板/区块识别 → extract 规则抽取 →
    header 表头字段与单位归一 → validate 校验（24h 加总/数值合理性）→ 输出 JSON

设计取舍：
- **数值字段一律规则抽取**，LLM 只在 llm.py 中处理 Remarks 文本，且输出后强制交叉校验；
- 校验失败不抛异常，而是把问题写进 source.warnings 与 time_verification.messages，
  让工程师能看到"这份日报哪里不对"，而不是拿到一个静默出错的 JSON。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .codes import CodeMap, load_code_map
from .detect import BLOCK_PLAN, BLOCK_SUMMARY, Detection, Segmented, detect_template, segment
from .extract import (
    TEMPLATE_SPECS,
    HeaderExtraction,
    TemplateSpec,
    extract_bit_records,
    extract_header,
    extract_mud,
    extract_remarks,
    extract_time_log,
)
from .llm import extract_events
from .model import DailyReport, ParseSource, ReportHeader, Well
from .normalize import resolve_number, to_int
from .reader import Document, read_document
from .validate import ValidationResult, validate_report

# ------------------------------------------------------------------ 表头字段解析
_DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y", "%Y.%m.%d",
    "%Y年%m月%d日", "%d-%b-%Y", "%d %b %Y", "%b %d, %Y", "%Y%m%d",
)
_DATETIME_PAIR_RE = re.compile(
    r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})"
    r"(?:\s+(\d{1,2}:\d{2}))?"
    r"\s*(?:[-–~至到]|to)\s*"
    r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})"
    r"(?:\s+(\d{1,2}:\d{2}))?"
)
_TZ_RE = re.compile(r"([+-]\d{2}:?\d{2})\s*$")


def _parse_date(text: str | None) -> date | None:
    if not text:
        return None
    s = str(text).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def _iso(d: date | None, clock: str = "00:00", tz: str | None = None) -> str | None:
    if d is None:
        return None
    base = f"{d.isoformat()}T{clock}:00"
    return f"{base}{tz}" if tz else base


@dataclass
class ParseResult:
    report: DailyReport
    detection: Detection
    segmentation: Segmented
    header: HeaderExtraction
    validation: ValidationResult
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.report.to_dict()


def parse_files(
    paths: list[str | Path],
    *,
    cm: CodeMap | None = None,
    template_hint: str | None = None,
) -> list[ParseResult]:
    """按井分组、按日期排序后批量解析，并把相邻日报的井深接续起来。

    为什么要接续：很多日报只写"当前井深"，当日进尺需要由"本期井深 − 上期井深"
    反推。这是行业里核对日报的常规做法，不接续就无法得到当日进尺，
    也就无法计算平均钻速与平点定位。
    """
    cm = cm or load_code_map()
    results: list[ParseResult] = []
    for p in paths:
        results.append(parse_file(p, cm=cm, template_hint=template_hint))

    # 按 (井名, 日期) 排序后接续
    order = sorted(
        range(len(results)),
        key=lambda i: (
            results[i].report.well.well_name or "",
            results[i].report.report.dtim_start or "",
        ),
    )
    prev_md: dict[str, float] = {}
    for i in order:
        rep = results[i].report
        r = rep.report
        key = rep.well.well_name or ""
        if r.md_m and (r.md_in_start_m is None or r.progress_m is None):
            last = prev_md.get(key)
            if last is not None and r.md_m >= last:
                r.md_in_start_m = round(last, 2)
                r.progress_m = round(r.md_m - last, 2)
        if r.etim_drill_h is None and rep.time_log:
            dh = round(sum(e.hours for e in rep.time_log if e.code in ("DRILL", "REAM")), 2)
            if dh > 0:
                r.etim_drill_h = dh
        if r.rop_av_m_per_h is None and r.etim_drill_h and r.progress_m:
            r.rop_av_m_per_h = round(r.progress_m / r.etim_drill_h, 2)
        if r.md_m:
            prev_md[key] = r.md_m
    return results


def parse_document(
    doc: Document,
    *,
    cm: CodeMap | None = None,
    template_hint: str | None = None,
    event_extractor: Any = None,
) -> ParseResult:
    cm = cm or load_code_map()
    detection = detect_template(doc)
    if template_hint and template_hint in TEMPLATE_SPECS:
        detection.template_id = template_hint
        detection.label = TEMPLATE_SPECS[template_hint].label
    spec = TEMPLATE_SPECS.get(detection.template_id) or TemplateSpec(
        template_id="unknown", label="通用抽取", header_labels=[]
    )

    seg = segment(doc, detection)
    warnings: list[str] = list(detection.notes) + list(seg.notes) + list(doc.warnings)

    # ---- 表头
    header = extract_header(doc, seg, spec, cm=cm)
    warnings.extend(header.warnings)
    well, report = _build_header(header, spec, detection)

    # ---- 时间分解
    entries, tw = extract_time_log(doc, seg, spec, cm=cm, detection=detection)
    warnings.extend(tw)

    # ---- 钻头 / 泥浆 / 备注
    bits, bw = extract_bit_records(doc, seg, spec, detection=detection)
    warnings.extend(bw)
    mud, mw = extract_mud(doc, seg, spec, detection=detection)
    warnings.extend(mw)
    remarks = extract_remarks(seg)

    # ---- 摘要与计划（原文保留，不解析）
    report.sum_24hr = " ".join(seg.block(BLOCK_SUMMARY)).strip() or None
    report.plan_24hr = " ".join(seg.block(BLOCK_PLAN)).strip() or None

    # ---- 派生字段（在时间分解抽完之后才能算）
    # 1) 钻进时间：日报不一定单列该字段，但时间分解里已经编码了钻进/扩眼时长
    if report.etim_drill_h is None and entries:
        drill_h = round(sum(e.hours for e in entries if e.code in ("DRILL", "REAM")), 2)
        if drill_h > 0:
            report.etim_drill_h = drill_h
    # 2) 当日进尺：表头没写时用 井深 − 上一份日报井深 反推（由调用方提供上一日井深）
    if report.progress_m is None and report.md_in_start_m is not None and report.md_m:
        report.progress_m = round(report.md_m - report.md_in_start_m, 2)
    # 3) 平均钻速：进尺 ÷ 钻进时间
    if report.rop_av_m_per_h is None and report.etim_drill_h and report.progress_m:
        report.rop_av_m_per_h = round(report.progress_m / report.etim_drill_h, 2)

    out = DailyReport(well=well, report=report, time_log=entries, bit_records=bits, mud=mud, remarks=remarks)
    # ---- Remarks → 结构化事件（LLM 或确定性后端，数值强制 span 校验）
    try:
        out.events = extract_events(remarks, extractor=event_extractor, cm=cm)
    except Exception as exc:  # 事件抽取失败不应阻断整份日报的解析
        warnings.append(f"备注事件抽取失败，已跳过（{type(exc).__name__}: {exc}）")
    out.source = ParseSource(
        file_name=doc.path.name,
        file_type=doc.file_type if doc.file_type in ("pdf", "xlsx", "xml", "image") else "unknown",
        parser=f"rule:{detection.template_id}@0.1.0",
        template_id=detection.template_id if detection.known else None,
        page_count=len(doc.pages),
        warnings=[],
    )

    validation = validate_report(out, cm=cm)
    return ParseResult(
        report=out,
        detection=detection,
        segmentation=seg,
        header=header,
        validation=validation,
        warnings=warnings,
    )


def parse_file(
    path: str | Path,
    *,
    cm: CodeMap | None = None,
    template_hint: str | None = None,
    event_extractor: Any = None,
) -> ParseResult:
    doc = read_document(path)
    return parse_document(doc, cm=cm, template_hint=template_hint, event_extractor=event_extractor)


# ------------------------------------------------------------------ 表头装配
def _build_header(header: HeaderExtraction, spec: TemplateSpec, detection: Detection) -> tuple[Well, ReportHeader]:
    v = header.values
    tpl = spec.template_id
    well = Well(
        well_name=(v.get("well_name") or "").strip(),
        operator=_clean_org(v.get("operator")),
        contractor=_clean_org(v.get("contractor")),
        rig=_clean(v.get("rig")),
        field=_clean(v.get("field")),
        api_number=_clean(v.get("api_number")),
        country=None,
        state=None,
    )
    if v.get("country_state"):
        parts = [p.strip() for p in str(v["country_state"]).split("/")]
        well.country = parts[0] or None
        well.state = parts[1] if len(parts) > 1 else None

    def num(key: str, kind: str, field_key: str, label_hint: str = "") -> float | None:
        raw = v.get(key)
        if raw is None:
            return None
        res = resolve_number(
            raw,
            field_kind=kind,
            label=label_hint or key,
            template_id=tpl,
            field_key=field_key,
            source_unit_override=spec.header_unit_overrides.get(field_key),
        )
        return res.si

    md = num("md_m", "length", "depth", "井深 (m)")
    tvd = num("tvd_m", "length", "depth", "垂深 (m)")
    progress = num("progress_m", "length", "footage", "当日进尺 (m)")
    hole = num("hole_diameter_in", "diameter", "hole_diameter", "井眼直径 (in)")
    rop = num("rop_av_m_per_h", "speed", "rop", "钻速 (m/h)")

    tz: str | None = None
    raw_date = v.get("report_date")
    start_iso = end_iso = None
    if raw_date:
        m = _DATETIME_PAIR_RE.search(str(raw_date))
        if m:
            d1, c1, d2, c2 = _parse_date(m.group(1)), m.group(2), _parse_date(m.group(3)), m.group(4)
            tz_m = _TZ_RE.search(str(raw_date))
            tz = tz_m.group(1) if tz_m else None
            start_iso = _iso(d1, c1 or "00:00", tz)
            end_iso = _iso(d2, c2 or "00:00", tz)
        else:
            d = _parse_date(str(raw_date))
            start_iso = _iso(d, "00:00", tz)
            end_iso = _iso(d + timedelta(days=1), "00:00", tz) if d else None

    spud = _parse_date(v.get("dtim_spud"))
    report = ReportHeader(
        dtim_start=start_iso or "",
        dtim_end=end_iso or "",
        md_m=round(md, 2) if md is not None else 0.0,
        report_no=to_int(v.get("report_no")),
        report_type="DDR",
        tvd_m=round(tvd, 2) if tvd is not None else None,
        progress_m=round(progress, 2) if progress is not None else None,
        hole_diameter_in=round(hole, 2) if hole is not None else None,
        dtim_spud=_iso(spud, "00:00", tz),
        etim_spud_days=to_float_safe(v.get("etim_spud_days")),
        rop_av_m_per_h=round(rop, 2) if rop is not None else None,
        well_status=_clean(v.get("well_status")),
        unit_system=detection.unit_system,
    )
    if report.md_in_start_m is None and report.progress_m is not None and report.md_m:
        report.md_in_start_m = round(report.md_m - report.progress_m, 2)
    return well, report


def to_float_safe(text: Any) -> float | None:
    if text is None:
        return None
    q = re.search(r"[+-]?\d+(?:[.,]\d+)?", str(text))
    return float(q.group().replace(",", ".")) if q else None


def _clean(s: Any) -> str | None:
    if s is None:
        return None
    v = re.sub(r"\s+", " ", str(s)).strip(" \u3000:：")
    return v or None


def _clean_org(s: Any) -> str | None:
    """清洗公司名：去掉标签残留与多余说明括号。"""
    v = _clean(s)
    if not v:
        return None
    v = re.sub(r"^(作业者|钻井承包商|承包商|operator|contractor)\s*[:：]?\s*", "", v, flags=re.IGNORECASE)
    return v or None

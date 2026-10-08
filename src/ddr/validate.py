"""校验层：24 小时加总校验、钟点连续性校验、数值合理性校验、单位一致性。

行业日报最容易出错、也最容易被忽视的合规点就是时间分解加总。
IADC 硬约束：时间分解必须加总为 24.00 小时（容差 ±0.01）。

本模块只做"判定 + 记录"，不静默篡改数据：
- 缺口不会被悄悄摊到其他条目上，而是显式补一条 UNKNOWN 记录并保留 delta_hours；
- 重叠/空洞/非单调等异常全部写进 messages，供人工复核。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .codes import CodeMap, load_code_map
from .model import (
    STANDARD_PERIOD_H,
    TIME_TOLERANCE_H,
    DailyReport,
    TimeEntry,
    TimeVerification,
)

_CLOCK_RE = re.compile(r"^(\d{1,2}):([0-5]\d)$")


def clock_to_hours(text: str | None) -> float | None:
    """HH:MM → 自报告期起点的分钟数/60。"24:00" 记为 24.0。"""
    if not text:
        return None
    m = _CLOCK_RE.match(str(text).strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h == 24 and mi == 0:
        return 24.0
    if h > 23:
        return None
    return h + mi / 60.0


# ------------------------------------------------------------------ 时间校验
def verify_time_log(
    entries: list[TimeEntry],
    *,
    expected_hours: float = STANDARD_PERIOD_H,
    tolerance: float = TIME_TOLERANCE_H,
    fill_gap: bool = True,
) -> TimeVerification:
    msgs: list[str] = []
    sum_hours = round(sum(float(e.hours or 0.0) for e in entries), 4)
    delta = round(sum_hours - expected_hours, 4)

    # --- 钟点连续性（仅当条目几乎都带起止钟点时才有意义）
    clocked = [e for e in entries if e.start and e.end]
    continuity_ok: bool | None = None
    overlap = 0.0
    if len(clocked) >= max(2, int(0.8 * len(entries))) and clocked:
        spans: list[tuple[float, float]] = []
        for e in clocked:
            s = clock_to_hours(e.start)
            t = clock_to_hours(e.end)
            if s is None or t is None:
                continue
            if t < s:  # 跨报告期终点（例如 23:00 → 01:00 记在次日）按期末 24:00 处理
                t = expected_hours
            spans.append((s, t))
        spans.sort()
        cursor = 0.0
        gaps: list[tuple[float, float]] = []
        for s, t in spans:
            if s > cursor + tolerance:
                gaps.append((cursor, s))
            if s < cursor - tolerance:
                overlap += min(cursor, t) - s
            cursor = max(cursor, t)
        if cursor < expected_hours - tolerance:
            gaps.append((cursor, expected_hours))
        continuity_ok = not gaps and overlap <= tolerance
        if gaps:
            msgs.append(
                "钟点不连续，存在 %d 处空洞，合计 %.2f h：%s"
                % (
                    len(gaps),
                    sum(b - a for a, b in gaps),
                    "; ".join(f"{a:.2f}h→{b:.2f}h" for a, b in gaps[:6]),
                )
            )
        if overlap > tolerance:
            msgs.append(f"钟点存在重叠，合计 {overlap:.2f} h（同一时段被重复计两次）")

    # --- 时长与钟点互校
    for i, e in enumerate(entries):
        s = clock_to_hours(e.start)
        t = clock_to_hours(e.end)
        if s is None or t is None:
            continue
        span = (t - s) if t >= s else (expected_hours - s)
        if span > 0 and abs(span - float(e.hours or 0.0)) > tolerance:
            msgs.append(
                f"第 {i + 1} 条时长与钟点不符：{e.start}-{e.end} 应为 {span:.2f} h，"
                f"日报记 {float(e.hours or 0.0):.2f} h"
            )

    unaccounted: float | None = None
    original_ok = abs(delta) <= tolerance  # 以日报原文为准判定合规性，补记缺口不算"改对"
    if abs(delta) > tolerance:
        if delta < 0:
            unaccounted = round(-delta, 4)
            msgs.append(
                f"时间分解缺口 {unaccounted:.2f} h：加总 {sum_hours:.2f} h，"
                f"不足 {expected_hours:.2f} h（合规硬约束不通过）"
            )
            if fill_gap:
                entries.append(
                    TimeEntry(
                        hours=unaccounted,
                        code="UNKNOWN",
                        category="flat",
                        is_npt=False,
                        operation="日报未解释的时间缺口（解析器补记，非日报原文）",
                        duration_source="derived_from_total",
                    )
                )
                sum_hours = round(sum(float(e.hours or 0.0) for e in entries), 4)
                msgs.append(
                    f"已补记 UNKNOWN {unaccounted:.2f} h 使加总为 {sum_hours:.2f} h；"
                    "原始缺口保留在 unaccounted_hours，请人工回查日报原件"
                )
        else:
            msgs.append(
                f"时间分解超计 {delta:.2f} h：加总 {sum_hours:.2f} h，超过 {expected_hours:.2f} h"
            )

    valid = original_ok

    return TimeVerification(
        sum_hours=round(sum(float(e.hours or 0.0) for e in entries), 4),
        valid=valid,
        expected_hours=expected_hours,
        delta_hours=round(delta, 4),
        tolerance_hours=tolerance,
        unaccounted_hours=unaccounted,
        clock_continuity_ok=continuity_ok,
        overlap_hours=round(overlap, 4) if overlap else 0.0,
        messages=msgs,
    )


# ------------------------------------------------------------------ 数值校验
@dataclass
class NumericIssue:
    field: str
    severity: str  # error | warning
    message: str


def verify_numeric(report: DailyReport, *, cm: CodeMap | None = None) -> list[NumericIssue]:
    cm = cm or load_code_map()
    issues: list[NumericIssue] = []
    r = report.report

    # 井深单调递增
    if r.md_in_start_m is not None and r.md_m and r.md_m + 1e-9 < r.md_in_start_m:
        issues.append(
            NumericIssue(
                "report.md_m",
                "error",
                f"井深倒退：报告期起点 {r.md_in_start_m} m > 期末 {r.md_m} m",
            )
        )

    # 进尺一致性
    if r.md_in_start_m is not None and r.md_m and r.progress_m is not None:
        expect = round(r.md_m - r.md_in_start_m, 2)
        if abs(expect - r.progress_m) > 0.55:  # 允许 0.5 m 内四舍五入差
            issues.append(
                NumericIssue(
                    "report.progress_m",
                    "warning",
                    f"当日进尺 {r.progress_m} m 与井深差 {expect} m 不一致",
                )
            )

    # TVD ≤ MD
    if r.tvd_m is not None and r.md_m and r.tvd_m > r.md_m + 1e-9:
        issues.append(NumericIssue("report.tvd_m", "error", f"垂深 {r.tvd_m} m 大于测量井深 {r.md_m} m"))

    # ROP 交叉校验：ROP 应约等于 进尺 / 钻进时间
    drill_h = sum(
        float(e.hours or 0.0) for e in report.time_log if e.code in ("DRILL", "REAM")
    )
    if r.rop_av_m_per_h is not None and r.progress_m and drill_h > 0.1:
        calc = r.progress_m / drill_h
        if calc > 0 and abs(calc - r.rop_av_m_per_h) / max(calc, 1e-6) > 0.35:
            issues.append(
                NumericIssue(
                    "report.rop_av_m_per_h",
                    "warning",
                    f"平均钻速 {r.rop_av_m_per_h} m/h 与「进尺÷钻进时间」={calc:.2f} m/h 偏差超 35%",
                )
            )

    # 时间条目
    for i, e in enumerate(report.time_log):
        if float(e.hours or 0.0) < 0:
            issues.append(NumericIssue(f"time_log[{i}].hours", "error", "时长为负"))
        if e.category == "npt" and not e.is_npt:
            issues.append(NumericIssue(f"time_log[{i}].is_npt", "error", "category=npt 但 is_npt=false"))
        if e.is_npt and e.category != "npt":
            issues.append(NumericIssue(f"time_log[{i}].category", "error", "is_npt=true 但 category≠npt"))
        if e.code == "UNKNOWN" and e.duration_source == "reported":
            issues.append(
                NumericIssue(f"time_log[{i}].code", "warning", f"操作描述未识别：{e.operation!r}")
            )
        if e.code == "REPAIR" and not e.npt_category:
            issues.append(NumericIssue(f"time_log[{i}].npt_category", "warning", "设备类 NPT 未给归因大类"))

    # 钻头记录
    prev_out: float | None = None
    for b in report.bit_records:
        if b.depth_in_m is not None and b.depth_out_m is not None and b.depth_out_m < b.depth_in_m:
            issues.append(NumericIssue(f"bit_records[{b.bit_no}].depth_out_m", "error", "出井深度小于入井深度"))
        if b.footage_m is not None and b.depth_in_m is not None and b.depth_out_m is not None:
            calc = round(b.depth_out_m - b.depth_in_m, 2)
            if abs(calc - b.footage_m) > 0.55:
                issues.append(
                    NumericIssue(
                        f"bit_records[{b.bit_no}].footage_m",
                        "warning",
                        f"进尺 {b.footage_m} m 与深度差 {calc} m 不一致",
                    )
                )
        if b.footage_m and b.hours and b.rop_m_per_h is None:
            b.rop_m_per_h = round(b.footage_m / b.hours, 2)
        if b.rop_m_per_h and b.footage_m and b.hours:
            calc = b.footage_m / b.hours
            if calc > 0 and abs(calc - b.rop_m_per_h) / calc > 0.2:
                issues.append(
                    NumericIssue(
                        f"bit_records[{b.bit_no}].rop_m_per_h",
                        "warning",
                        f"钻速 {b.rop_m_per_h} 与「进尺÷时间」={calc:.2f} 偏差超 20%",
                    )
                )
        if prev_out is not None and b.depth_in_m is not None and b.depth_in_m + 0.55 < prev_out:
            issues.append(
                NumericIssue(
                    f"bit_records[{b.bit_no}].depth_in_m",
                    "warning",
                    f"入井深度 {b.depth_in_m} m 小于上一只钻头出井深度 {prev_out} m",
                )
            )
        if b.depth_out_m is not None:
            prev_out = b.depth_out_m

    # 泥浆
    for i, m in enumerate(report.mud):
        if m.density_gcc is not None and not (0.5 <= m.density_gcc <= 3.0):
            issues.append(
                NumericIssue(f"mud[{i}].density_gcc", "warning", f"密度 {m.density_gcc} g/cm³ 超出合理区间 0.5–3.0")
            )
        if m.fl_ml is not None and m.fl_ml < 0:
            issues.append(NumericIssue(f"mud[{i}].fl_ml", "error", "失水为负"))

    return issues


# ------------------------------------------------------------------ 一致性汇总
@dataclass
class ValidationResult:
    ok: bool
    time: TimeVerification | None
    issues: list[NumericIssue] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[NumericIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[NumericIssue]:
        return [i for i in self.issues if i.severity == "warning"]


def validate_report(
    report: DailyReport,
    *,
    cm: CodeMap | None = None,
    expected_hours: float = STANDARD_PERIOD_H,
) -> ValidationResult:
    """校验并就地回填 report.time_verification；时间缺口按需补记 UNKNOWN。"""
    tv = verify_time_log(report.time_log, expected_hours=expected_hours)
    report.time_verification = tv
    issues = verify_numeric(report, cm=cm)
    messages = list(tv.messages) + [f"[{i.severity}] {i.field}: {i.message}" for i in issues]
    ok = tv.valid and not any(i.severity == "error" for i in issues)
    return ValidationResult(ok=ok, time=tv, issues=issues, messages=messages)

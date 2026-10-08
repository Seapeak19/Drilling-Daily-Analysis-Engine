"""数据集统计（`ddr stats`）。

为什么需要它：样本入库后，别人克隆下来第一件想做的事是"这批样本长什么样、
覆盖了哪些工况、评测基线是多少"，而不是自己去翻 JSON。
这个模块把入库的样本变成**可检视的资产**：模板/工况/单位制覆盖、
时间分解合规率、NPT 与 ILT 分布、以及各字段的可用率（哪些字段是空的）。

字段可用率尤其重要：它直接回答"这批数据能不能支撑某个分析"——
例如钻头记录只在 6/21 份日报里出现，那么"钻头性能对标"就不能靠这批样本验证。
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 表头数值字段清单（用于统计"有多少份日报真的写了这个字段"）
HEADER_NUMERIC_FIELDS = (
    "md_m", "tvd_m", "progress_m", "rop_av_m_per_h", "etim_drill_h",
    "hole_diameter_in", "etim_spud_days", "report_no",
)


@dataclass
class DatasetStats:
    data_dir: Path
    sample_count: int = 0
    well_count: int = 0
    wells: list[str] = field(default_factory=list)
    date_range: tuple[str, str] | None = None
    templates: Counter = field(default_factory=Counter)
    unit_systems: Counter = field(default_factory=Counter)
    phases: Counter = field(default_factory=Counter)
    synthetic_flags: Counter = field(default_factory=Counter)
    time_compliant: int = 0
    time_violations: list[tuple[str, float]] = field(default_factory=list)
    sum_hours_total: float = 0.0
    entries_total: int = 0
    npt_hours_total: float = 0.0
    npt_days: int = 0
    connections_total: int = 0
    connection_days: int = 0
    categories_total: dict[str, float] = field(default_factory=lambda: {"productive": 0.0, "flat": 0.0, "npt": 0.0})
    header_field_present: Counter = field(default_factory=Counter)
    bit_days: int = 0
    mud_days: int = 0
    remarks_total: int = 0
    warnings: list[str] = field(default_factory=list)

    # ---------------------------------------------------------------- 输出
    @property
    def compliance_rate(self) -> float | None:
        return (self.time_compliant / self.sample_count) if self.sample_count else None

    @property
    def npt_rate(self) -> float | None:
        if not self.sum_hours_total:
            return None
        return self.npt_hours_total / self.sum_hours_total

    def to_dict(self) -> dict[str, Any]:
        return {
            "data_dir": str(self.data_dir),
            "sample_count": self.sample_count,
            "well_count": self.well_count,
            "wells": self.wells,
            "date_range": list(self.date_range) if self.date_range else None,
            "templates": dict(self.templates),
            "unit_systems": dict(self.unit_systems),
            "phases": dict(self.phases),
            "synthetic": dict(self.synthetic_flags),
            "time_compliance": {
                "compliant": self.time_compliant,
                "total": self.sample_count,
                "rate": self.compliance_rate,
                "violations": [{"file": f, "sum_hours": h} for f, h in self.time_violations],
            },
            "time_totals": {
                "sum_hours": round(self.sum_hours_total, 2),
                "entries": self.entries_total,
                "by_category": {k: round(v, 2) for k, v in self.categories_total.items()},
            },
            "npt": {
                "hours": round(self.npt_hours_total, 2),
                "rate": self.npt_rate,
                "days_with_npt": self.npt_days,
            },
            "ilt": {
                "connections": self.connections_total,
                "days_with_connections": self.connection_days,
            },
            "coverage": {
                "bit_record_days": self.bit_days,
                "mud_days": self.mud_days,
                "remarks_total": self.remarks_total,
                "header_field_present": dict(self.header_field_present),
            },
            "warnings": self.warnings,
        }


def load_manifest(data_dir: str | Path) -> dict[str, Any]:
    """读取数据集的 manifest，失败时给出可操作提示而不是崩溃栈。"""
    p = Path(data_dir) / "manifest.json"
    if not p.exists():
        raise FileNotFoundError(
            f"没找到 {p}。该命令需要一个含 manifest.json 的数据集目录，"
            "例如 data/samples（可用 `python -m ddr.render --out data/samples` 生成）。"
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{p} 不是合法 JSON（第 {exc.lineno} 行：{exc.msg}）") from exc
    if not isinstance(data, dict) or "items" not in data:
        raise ValueError(f"{p} 结构不符合预期：顶层应为对象且含 'items' 键")
    return data


def compute_stats(data_dir: str | Path) -> DatasetStats:
    """读取 manifest.json 与各样本的 ground truth，统计数据集特征。

    以 ground truth 为统计口径（它是评测标尺，也是唯一能保证"与日报内容一致"的一侧）。
    """
    data_dir = Path(data_dir)
    manifest = load_manifest(data_dir)
    items = manifest.get("items", [])
    st = DatasetStats(data_dir=data_dir, sample_count=len(items))

    well_names: set[str] = set()
    dates: list[str] = []
    conn_by_day: list[int] = []

    for it in items:
        well_names.add(it.get("well_name") or "")
        if it.get("report_date"):
            dates.append(it["report_date"])
        st.templates[it.get("template_id") or "unknown"] += 1
        st.unit_systems[it.get("unit_system") or "unknown"] += 1
        st.synthetic_flags["synthetic" if it.get("synthetic") else "real"] += 1

        truth = _load_truth(data_dir / it["truth"])
        if truth is None:
            st.warnings.append(f"{it.get('file')}: ground truth 读取失败或不存在，统计中跳过")
            continue

        st.phases[truth.get("phase") or "unknown"] += 1

        # --- 24 小时合规性（以 ground truth 的 sum_hours 为准）
        sum_h = float(truth.get("sum_hours") or 0.0)
        st.sum_hours_total += sum_h
        if abs(sum_h - 24.0) <= 0.01:
            st.time_compliant += 1
        else:
            st.time_violations.append((it.get("file", "?"), sum_h))

        # --- 时间条目与类别分布
        entries = truth.get("time_log") or []
        st.entries_total += len(entries)
        for e in entries:
            cat = "npt" if e.get("npt_category") else _category_of_code(e.get("code"))
            st.categories_total[cat] = round(st.categories_total.get(cat, 0.0) + float(e.get("hours") or 0), 2)

        # --- NPT
        npt_h = float(truth.get("npt_hours") or 0.0)
        st.npt_hours_total += npt_h
        if npt_h > 0:
            st.npt_days += 1

        # --- ILT（接单根）
        conn = int(truth.get("connection_count") or 0)
        st.connections_total += conn
        conn_by_day.append(conn)
        if conn > 0:
            st.connection_days += 1

        # --- 区块覆盖
        if truth.get("bit_records"):
            st.bit_days += 1
        if truth.get("mud"):
            st.mud_days += 1
        st.remarks_total += len(truth.get("remarks") or [])

        # --- 表头字段可用率
        for f in HEADER_NUMERIC_FIELDS:
            if truth.get(f) is not None:
                st.header_field_present[f] += 1

    st.well_count = len([w for w in well_names if w])
    st.wells = sorted(w for w in well_names if w)
    if dates:
        st.date_range = (min(dates), max(dates))
    if conn_by_day:
        st.connection_days = sum(1 for c in conn_by_day if c > 0)
    return st


def _load_truth(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return doc.get("truth") or doc


def _category_of_code(code: str | None) -> str:
    """按 code-map 语义判定类别（避免为了统计去 import 整个 codes 模块的开销）。"""
    npt_codes = {"REPAIR", "FISH", "STUCK", "WELL_CONTROL", "WEATHER", "SAFETY"}
    prod_codes = {"DRILL", "REAM"}
    if code in npt_codes:
        return "npt"
    if code in prod_codes:
        return "productive"
    return "flat"


def render_text(st: DatasetStats) -> str:
    pct = lambda x: "—" if x is None else f"{x:.1%}"  # noqa: E731
    lines = [
        f"数据集：{st.data_dir}",
        f"样本 {st.sample_count} 份　井 {st.well_count} 口　"
        + (f"日期 {st.date_range[0]} ~ {st.date_range[1]}" if st.date_range else "日期未知"),
        f"数据性质：{dict(st.synthetic_flags)}",
        "",
        "【模板与单位制覆盖】",
    ]
    for tpl, n in st.templates.most_common():
        lines.append(f"  {tpl:<16} {n:>3} 份")
    lines.append(f"  单位制：{dict(st.unit_systems)}")
    lines.append("")
    lines.append("【工况分布】")
    for ph, n in st.phases.most_common():
        lines.append(f"  {ph:<16} {n:>3} 份")
    lines.append("")
    lines.append("【时间分解合规性（IADC 24 小时硬约束）】")
    lines.append(f"  合规 {st.time_compliant}/{st.sample_count}（{pct(st.compliance_rate)}）")
    for f, h in st.time_violations[:10]:
        lines.append(f"    ✗ {f}：加总 {h} h")
    lines.append("")
    lines.append("【时间分布】")
    tot = st.sum_hours_total or 1.0
    for cat, label in (("productive", "有效生产"), ("flat", "计划内 Flat"), ("npt", "NPT 非生产")):
        h = st.categories_total.get(cat, 0.0)
        lines.append(f"  {label:<12} {h:>8.2f} h（{h / tot:.1%}）")
    lines.append(f"  合计 {st.sum_hours_total:>8.2f} h　条目 {st.entries_total} 条"
                 f"（均 {st.entries_total / max(st.sample_count, 1):.1f} 条/份）")
    lines.append("")
    lines.append("【NPT 与 ILT】")
    lines.append(f"  NPT {st.npt_hours_total:.2f} h（占总时间 {pct(st.npt_rate)}，行业基准 20%–25%）")
    lines.append(f"  含 NPT 的日报 {st.npt_days}/{st.sample_count} 份")
    lines.append(f"  接单根（ILT 基础）{st.connections_total} 次，出现在 {st.connection_days}/{st.sample_count} 份日报")
    lines.append("")
    lines.append("【区块与字段可用率（决定这批数据能验证什么分析）】")
    lines.append(f"  含钻头记录的日报   {st.bit_days}/{st.sample_count}"
                 f"{'　← 钻头性能对标样本量偏少' if st.bit_days < st.sample_count / 3 else ''}")
    lines.append(f"  含泥浆性能的日报   {st.mud_days}/{st.sample_count}")
    lines.append(f"  备注条目合计       {st.remarks_total} 条")
    for f in HEADER_NUMERIC_FIELDS:
        n = st.header_field_present.get(f, 0)
        flag = "" if n == st.sample_count else f"　← 有 {st.sample_count - n} 份未写该字段"
        lines.append(f"  {f:<20} {n:>3}/{st.sample_count}{flag}")
    if st.warnings:
        lines.append("")
        lines.append("【提示】")
        for w in st.warnings[:10]:
            lines.append(f"  · {w}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="统计数据集特征（模板/工况覆盖、时效合规率、字段可用率）")
    ap.add_argument("--data", default="data/samples")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而非文本")
    args = ap.parse_args(argv)

    try:
        st = compute_stats(args.data)
    except FileNotFoundError as exc:
        print(f"错误：{exc}")
        return 2
    if args.json:
        print(json.dumps(st.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_text(st))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

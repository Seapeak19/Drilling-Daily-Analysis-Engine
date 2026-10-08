"""标准化数据模型（对齐 WITSML v2.0 DrillReport）与 JSON Schema 校验。

设计要点：
1. 输出一律 SI/公制；原始读数保留在 raw 字段，保证可追溯。
2. time_log 显式保留 category 与 is_npt，并用 time_verification 强制 24 小时加总约束
   —— 这是行业日报最容易出错、也最容易被忽视的合规点。
3. 表头缺失字段统一写 None，不写空字符串，避免下游把"没读到"当成"读到了空值"。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schema"
REPORT_SCHEMA_PATH = SCHEMA_DIR / "ddr-report.schema.json"
SCHEMA_VERSION = "1.0.0"

TIME_TOLERANCE_H = 0.01
STANDARD_PERIOD_H = 24.0
CATEGORIES = ("productive", "flat", "npt")


# --------------------------------------------------------------------- 子结构
@dataclass
class ParseSource:
    file_name: str
    file_type: str = "unknown"
    parser: str = ""
    parsed_at: str = ""
    template_id: str | None = None
    page_count: int | None = None
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.parsed_at:
            self.parsed_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class Well:
    well_name: str = ""
    wellbore_name: str | None = None
    api_number: str | None = None
    uwi: str | None = None
    operator: str | None = None
    contractor: str | None = None
    rig: str | None = None
    field: str | None = None
    country: str | None = None
    state: str | None = None
    county: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    elevation_ground_m: float | None = None
    elevation_kelly_bushing_m: float | None = None


@dataclass
class ReportHeader:
    dtim_start: str = ""
    dtim_end: str = ""
    md_m: float = 0.0
    report_no: int | None = None
    report_type: str | None = "DDR"
    tvd_m: float | None = None
    md_in_start_m: float | None = None
    progress_m: float | None = None
    hole_diameter_in: float | None = None
    casing_diameter_in: float | None = None
    dtim_spud: str | None = None
    etim_spud_days: float | None = None
    rop_av_m_per_h: float | None = None
    dist_drill_m: float | None = None
    etim_drill_h: float | None = None
    etim_ream_h: float | None = None
    well_status: str | None = None
    status_comment: str | None = None
    sum_24hr: str | None = None
    plan_24hr: str | None = None
    day_number: int | None = None
    unit_system: str = "unknown"


@dataclass
class TimeEntry:
    hours: float
    code: str
    category: str
    is_npt: bool
    start: str | None = None
    end: str | None = None
    operation: str | None = None
    npt_category: str | None = None
    npt_responsibility: str | None = None
    is_ilt_candidate: bool = False
    ilt_action: str | None = None
    duration_source: str = "reported"


@dataclass
class TimeVerification:
    sum_hours: float
    valid: bool
    expected_hours: float = STANDARD_PERIOD_H
    delta_hours: float = 0.0
    tolerance_hours: float = TIME_TOLERANCE_H
    unaccounted_hours: float | None = None
    clock_continuity_ok: bool | None = None
    overlap_hours: float | None = None
    messages: list[str] = field(default_factory=list)


@dataclass
class BitRecord:
    bit_no: int
    size_in: float | None = None
    make: str | None = None
    model: str | None = None
    type: str | None = None
    iadc_code: str | None = None
    nozzles_32nds: str | None = None
    depth_in_m: float | None = None
    depth_out_m: float | None = None
    footage_m: float | None = None
    hours: float | None = None
    rop_m_per_h: float | None = None
    wob_kgf: float | None = None
    rpm: float | None = None
    flow_lps: float | None = None
    dull_grade: str | None = None
    dull_grade_parsed: dict[str, Any] | None = None
    hours_source: str | None = None


@dataclass
class MudProperty:
    sample_point: str
    depth_m: float | None = None
    density_gcc: float | None = None
    funnel_viscosity_s: float | None = None
    pv_mpas: float | None = None
    yp_pa: float | None = None
    gel_10s_pa: float | None = None
    gel_10min_pa: float | None = None
    fl_ml: float | None = None
    ph: float | None = None
    chlorides_mg_l: float | None = None
    sand_pct: float | None = None
    oil_water_ratio: str | None = None
    solids_pct: float | None = None
    mud_type: str | None = None
    volume_m3: float | None = None
    raw: dict[str, Any] | None = None


@dataclass
class Remark:
    seq: int
    text: str
    time_hint: str | None = None
    extraction_method: str = "none"
    llm_confidence: float | None = None


@dataclass
class Event:
    event_type: str
    description: str
    source_remark_seq: int
    start: str | None = None
    end: str | None = None
    hours: float | None = None
    npt_category: str | None = None
    npt_responsibility: str | None = None
    evidence_span: str | None = None
    extraction_method: str = "none"
    llm_confidence: float | None = None
    verified: bool = False


@dataclass
class BhaComponent:
    seq: int
    component: str
    od_in: float | None = None
    id_in: float | None = None
    length_m: float | None = None
    cum_length_m: float | None = None


@dataclass
class Kpi:
    productive_hours: float | None = None
    flat_hours: float | None = None
    npt_hours: float | None = None
    productive_pct: float | None = None
    flat_pct: float | None = None
    npt_pct: float | None = None
    etim_drill_h: float | None = None


# ------------------------------------------------------------------ 顶层容器
@dataclass
class DailyReport:
    well: Well = field(default_factory=Well)
    report: ReportHeader = field(default_factory=ReportHeader)
    time_log: list[TimeEntry] = field(default_factory=list)
    time_verification: TimeVerification | None = None
    bit_records: list[BitRecord] = field(default_factory=list)
    mud: list[MudProperty] = field(default_factory=list)
    remarks: list[Remark] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    bha: list[BhaComponent] | None = None
    kpi: Kpi | None = None
    source: ParseSource | None = None
    schema_version: str = SCHEMA_VERSION

    # ------------------------------------------------------------ 序列化
    def to_dict(self) -> dict[str, Any]:
        """输出符合 ddr-report.schema.json 的纯数据字典。"""
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "source": asdict(self.source) if self.source else None,
            "well": asdict(self.well),
            "report": asdict(self.report),
            "time_log": [asdict(e) for e in self.time_log],
            "time_verification": asdict(self.time_verification) if self.time_verification else None,
            "bit_records": [asdict(b) for b in self.bit_records],
            "mud": [asdict(m) for m in self.mud],
            "remarks": [asdict(r) for r in self.remarks],
            "events": [asdict(e) for e in self.events],
            "bha": [asdict(c) for c in self.bha] if self.bha is not None else None,
            "kpi": asdict(self.kpi) if self.kpi else None,
        }
        if payload["source"] is None:
            raise ValueError("source 缺失：DailyReport 必须带解析溯源信息")
        if payload["time_verification"] is None:
            raise ValueError("time_verification 缺失：必须执行 24 小时加总校验后再输出")
        return payload

    def to_json(self, *, indent: int | None = 2, ensure_ascii: bool = False) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=ensure_ascii)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DailyReport":
        """从字典还原（用于评测集 ground truth 读取）。"""
        return _from_dict(data)


def _filter_kwargs(dc: type, data: dict[str, Any]) -> dict[str, Any]:
    allowed = set(dc.__dataclass_fields__)  # type: ignore[attr-defined]
    return {k: v for k, v in data.items() if k in allowed}


def _from_dict(data: dict[str, Any]) -> DailyReport:
    rep = DailyReport()
    rep.schema_version = data.get("schema_version", SCHEMA_VERSION)
    if data.get("source"):
        rep.source = ParseSource(**_filter_kwargs(ParseSource, data["source"]))
    if data.get("well"):
        rep.well = Well(**_filter_kwargs(Well, data["well"]))
    if data.get("report"):
        rep.report = ReportHeader(**_filter_kwargs(ReportHeader, data["report"]))
    rep.time_log = [TimeEntry(**_filter_kwargs(TimeEntry, e)) for e in data.get("time_log", [])]
    if data.get("time_verification"):
        rep.time_verification = TimeVerification(**_filter_kwargs(TimeVerification, data["time_verification"]))
    rep.bit_records = [BitRecord(**_filter_kwargs(BitRecord, b)) for b in data.get("bit_records", [])]
    rep.mud = [MudProperty(**_filter_kwargs(MudProperty, m)) for m in data.get("mud", [])]
    rep.remarks = [Remark(**_filter_kwargs(Remark, r)) for r in data.get("remarks", [])]
    rep.events = [Event(**_filter_kwargs(Event, e)) for e in data.get("events", [])]
    if data.get("bha") is not None:
        rep.bha = [BhaComponent(**_filter_kwargs(BhaComponent, c)) for c in data["bha"]]
    if data.get("kpi"):
        rep.kpi = Kpi(**_filter_kwargs(Kpi, data["kpi"]))
    return rep


# --------------------------------------------------------------- Schema 校验
def load_schema(path: str | Path | None = None) -> dict[str, Any]:
    p = Path(path) if path else REPORT_SCHEMA_PATH
    return json.loads(p.read_text(encoding="utf-8"))


def validate_payload(payload: dict[str, Any], *, path: str | Path | None = None) -> list[str]:
    """用 JSON Schema 校验输出。返回错误消息列表，空列表表示通过。"""
    try:
        import jsonschema  # type: ignore
    except ImportError:  # 无 jsonschema 时退化为轻量自检
        return _lightweight_check(payload)

    validator = jsonschema.Draft202012Validator(load_schema(path))
    errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
    out: list[str] = []
    for err in errors:
        loc = "/".join(str(p) for p in err.absolute_path) or "<root>"
        out.append(f"{loc}: {err.message}")
    return out


def _lightweight_check(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in ("schema_version", "source", "well", "report", "time_log", "time_verification"):
        if payload.get(key) is None:
            errors.append(f"{key}: 必填字段缺失")
    for i, entry in enumerate(payload.get("time_log") or []):
        if entry.get("category") not in CATEGORIES:
            errors.append(f"time_log/{i}/category: 非法类别 {entry.get('category')!r}")
    return errors

"""单位判定与数值解析：所有"数值 + 单位"的归一都集中在这里。

单位判定的优先级（必须严格遵守，否则英制/公制混用的日报会静默出错）：
1. **值自带单位**：单元格写成 "2350.00 m" / "5.2 klbf" → 直接用该单位换算；
2. **标签/列头自带单位**：表头写成 "井深 (m)" / "Depth (ft)" / "MW (g/cm3)"。
   这是**该列自己的声明**，比模板级默认更具体——混用日报里同一份文件的不同列
   单位可以不同，因此标签单位必须优先于模板默认；
3. **模板/区块配置的源单位**：模板整体用什么单位制记录哪些字段（见 TEMPLATE_SOURCE_UNITS）；
4. 仍无法判定 → 返回 None 并记警告，**绝不默认公制**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .units import (
    density_to_gcc,
    diameter_to_in,
    flow_to_lps,
    force_to_kgf,
    length_to_m,
    parse_quantity,
    pressure_to_kpa,
    speed_to_m_per_h,
    viscosity_to_mpas,
    volume_to_m3,
)

# 标签里用括号或斜杠标注的单位，如 "井深 (MD) (m)"、"Depth (ft)"、"MW (ppg)"
_LABEL_UNIT_RE = re.compile(
    r"[（(\[]\s*(?P<u>"
    r"m|ft|in|mm|cm|km|kn|kN|kgf|kg|klbf|kips?|lbf|lb|lbs|knm|kn·m|ft-?lb|ft·lb|"
    r"psi|kpa|mpa|bar|pa|g/cm3|g/cc|gcc|ppg|kg/m3|mpas|cp|pas|l/s|lps|gpm|l/min|m3/min|"
    r"m3|m³|bbl|gal|l|升|方|c|℃|f|℉|s|min|h|hr|hrs|小时|分钟|天|%|"
    r"m/h|ft/hr|m/hr|ft/h|ml|mg/l|mg/L|天|度"
    r")\s*[)）\]]",
    re.IGNORECASE,
)

# 单位类别 → 换算函数（用于根据"标签单位"把值换算成 SI）
CONVERTERS = {
    "length": length_to_m,
    "diameter": diameter_to_in,
    "force": force_to_kgf,
    "density": density_to_gcc,
    "viscosity": viscosity_to_mpas,
    "flow": flow_to_lps,
    "volume": volume_to_m3,
    "pressure": pressure_to_kpa,
    "speed": speed_to_m_per_h,
}

# 单位字符串 → 所属类别（用于判断"值自带单位"和"标签单位"能否用于该字段）
UNIT_KIND: dict[str, str] = {}
for _u in ("m", "米", "meter", "meters", "metre", "ft", "英尺", "feet", "foot", "mm", "毫米", "km", "千米", "公里", "in", "英寸", "inch", "inches", '"'):
    UNIT_KIND[_u] = "length"
for _u in ("kn", "千牛", "kilonewton", "n", "牛", "newton", "kgf", "公斤力", "kg", "kgs", "lbf", "磅力", "lb", "lbs", "klbf", "klbs", "kips", "kip", "千磅", "dan", "十牛", "decanewton"):
    UNIT_KIND[_u] = "force"
for _u in ("knm", "千牛米", "ftlbf", "ftlb", "ft-lbs", "lbft", "英尺磅", "nm", "牛米"):
    UNIT_KIND[_u] = "torque"
for _u in ("gcc", "g/cm3", "sg", "克/立方厘米", "ppg", "lb/gal", "磅/加仑", "kg/m3", "千克/立方米"):
    UNIT_KIND[_u] = "density"
for _u in ("mpas", "cp", "厘泊", "pas", "帕秒"):
    UNIT_KIND[_u] = "viscosity"
for _u in ("lps", "l/s", "升/秒", "gpm", "加仑/分", "lpm", "l/min", "升/分", "m3/min", "方/分"):
    UNIT_KIND[_u] = "flow"
for _u in ("m3", "方", "立方米", "bbl", "桶", "gal", "加仑", "l", "升"):
    UNIT_KIND[_u] = "volume"
for _u in ("kpa", "千帕", "psi", "磅/平方英寸", "mpa", "兆帕", "bar", "巴", "pa", "帕"):
    UNIT_KIND[_u] = "pressure"
for _u in ("m/h", "m/hr", "米/小时", "mperh", "ft/hr", "ft/h", "英尺/小时", "m/min", "ft/min", "m/d", "ft/d"):
    UNIT_KIND[_u] = "speed"

# 各数值字段期望的 SI 目标类别（决定能否用标签/值单位换算）
FIELD_UNIT_KIND: dict[str, str] = {
    "depth": "length",
    "hole_diameter": "diameter",
    "casing_diameter": "diameter",
    "bit_size": "diameter",
    "footage": "length",
    "wob": "force",
    "density": "density",
    "viscosity": "viscosity",
    "flow": "flow",
    "volume": "volume",
    "pressure": "pressure",
    "speed": "speed",
}

# 模板级源单位配置：模板整体用什么单位制记录哪些字段。
# 只有"标签/值里都没有单位"时才需要用到它，但它必须是显式配置而非猜测。
TEMPLATE_SOURCE_UNITS: dict[str, dict[str, str]] = {
    "cn_vertical": {"depth": "m", "footage": "m", "bit_size": "in", "hole_diameter": "in", "wob": "kN", "rop": "m/h"},
    "regional_xls": {"depth": "m", "footage": "m", "bit_size": "in", "hole_diameter": "in", "wob": "kN", "rop": "m/h"},
    "iadc_classic": {
        # 表头/钻头区块按公制记录（作业者沿用公制数值，只是模板是英文的），
        # 泥浆区块用英制 ppg/ft —— 这正是真实日报里最常见的"混用"情形。
        "depth": "m", "footage": "m", "bit_size": "in", "hole_diameter": "in",
        "wob": "kN", "rop": "m/h",
        "density": "ppg", "flow": "gpm", "volume": "bbl",
    },
}


@dataclass
class NumberResult:
    si: float | None
    raw: float | None
    unit: str | None
    unit_source: str  # value | label | template | unknown
    notes: list[str] = field(default_factory=list)
    ok: bool = True


def label_unit(label: str) -> str | None:
    """从字段标签里取单位，如 "井深 (MD) (m)" → 'm'，"MW (ppg)" → 'ppg'。"""
    if not label:
        return None
    m = _LABEL_UNIT_RE.search(label)
    return m.group("u") if m else None


def _convert(value: float, unit: str | None, kind: str) -> float | None:
    """把值按其单位换算到该字段的 SI 目标单位。unit 为 None 时视为已是 SI。"""
    fn = CONVERTERS.get(kind)
    if fn is None:
        return value
    try:
        return fn(value, unit)
    except Exception:
        return None


def resolve_number(
    text: Any,
    *,
    field_kind: str,
    label: str = "",
    template_id: str = "",
    field_key: str = "",
    source_unit_override: str | None = None,
) -> NumberResult:
    """把一个单元格/标签文本解析为该字段的 SI 数值。

    source_unit_override：显式指定的源单位，优先级仅次于"值自带单位"。
    供模板在**同一份日报里不同区块用不同单位制**时使用（如英制模板的表头用公制、
    泥浆区块用 ppg），避免模板级默认单位把某个区块的值算错。

    field_kind 为 FIELD_UNIT_KIND 里的类别（如 'length'）。对于无量纲字段（比例、时间、计数）
    调用方应直接用 parse_quantity / parse_float，不必走本函数。
    """
    notes: list[str] = []
    if text is None or str(text).strip() in ("", "-", "—", "/", "N/A", "n/a", "NA", "none"):
        return NumberResult(None, None, None, "unknown", ok=True)

    q = parse_quantity(str(text))
    if q is None:
        return NumberResult(None, None, None, "unknown", notes=[f"无法解析数值：{text!r}"], ok=False)

    # ① 值自带单位（最可靠：就是这一格自己写的）
    if q.unit:
        kind_of_value = UNIT_KIND.get(q.unit.lower())
        if kind_of_value == field_kind or (field_kind == "diameter" and kind_of_value == "length"):
            si = _convert(q.value, q.unit, field_kind)
            if si is not None:
                return NumberResult(si, q.value, q.unit, "value", notes)
        elif kind_of_value is not None:
            notes.append(
                f"值自带单位 {q.unit!r} 属于 {kind_of_value}，与字段期望的 {field_kind} 不符，已忽略该单位"
            )

    # ② 模板对该字段的显式源单位覆盖（区块级单位差异，最具体的一层配置）
    if source_unit_override:
        si = _convert(q.value, source_unit_override, field_kind)
        if si is not None:
            return NumberResult(si, q.value, source_unit_override, "template-override", notes)

    # ③ 标签/列头自带单位（该列自己声明的单位，比模板默认更具体）
    lu = label_unit(label)
    if lu:
        kind_of_label = UNIT_KIND.get(lu.lower())
        if kind_of_label == field_kind or (field_kind == "diameter" and kind_of_label == "length"):
            si = _convert(q.value, lu, field_kind)
            if si is not None:
                return NumberResult(si, q.value, lu, "label", notes)

    # ④ 模板配置的源单位（不可变配置，不随表头文字漂移）
    tpl = TEMPLATE_SOURCE_UNITS.get(template_id, {})
    tu = tpl.get(field_key) or tpl.get(field_kind)
    if tu:
        si = _convert(q.value, tu, field_kind)
        if si is not None:
            notes.append(
                f"单位取自模板 {template_id} 的默认源单位 {tu!r}（该列/该值未标注单位）"
            )
            return NumberResult(si, q.value, tu, "template", notes)

    # ⑤ 无法判定单位：不猜，交给上层处理
    notes.append(
        f"无法确定单位（字段类别 {field_kind}，标签 {label!r}，模板 {template_id or 'unknown'}）。"
        "已保留原始数值但未做 SI 换算，请人工核对或补充模板源单位配置。"
    )
    return NumberResult(None, q.value, None, "unknown", notes=notes, ok=False)


# ------------------------------------------------------------------ 时间解析
_CLOCK_RE = re.compile(r"\b([01]?\d|2[0-4]):([0-5]\d)\b")
_HOURS_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(?:h|hr|hrs|小时|hours?)?\s*$", re.IGNORECASE)
_MINUTES_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(?:min|分钟|minutes?)\s*$", re.IGNORECASE)
_DHMS_RE = re.compile(
    r"^\s*(?:(\d+)\s*(?:d|天))?\s*(?:(\d+)\s*(?:h|小时|hr))?\s*(?:(\d+)\s*(?:min|分钟|m))?\s*$",
    re.IGNORECASE,
)


def clock_to_hours(text: str | None) -> float | None:
    """'06:30' → 6.5；'24:00' → 24.0。"""
    if not text:
        return None
    m = _CLOCK_RE.search(str(text))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h == 24 and mi == 0:
        return 24.0
    if h > 23:
        return None
    return round(h + mi / 60.0, 4)


def duration_to_hours(text: str | None) -> float | None:
    """把各种时长写法归一到小时：'6.5'、'6.5 h'、'1h30min'、'45 min'、'1 天 2 小时'。"""
    if text is None:
        return None
    s = str(text).strip()
    if not s or s in ("-", "—", "/"):
        return None
    s = s.replace(",", ".")

    m = _MINUTES_RE.match(s)
    if m:
        return round(float(m.group(1)) / 60.0, 4)

    m = _HOURS_RE.match(s)
    if m:
        return round(float(m.group(1)), 4)

    m = _DHMS_RE.match(s)
    if m and any(m.groups()):
        d = float(m.group(1) or 0)
        h = float(m.group(2) or 0)
        mi = float(m.group(3) or 0)
        return round(d * 24 + h + mi / 60.0, 4)
    return None


def extract_time_cell(text: str | None) -> tuple[float | None, str]:
    """解析时长单元格。返回 (小时, 来源)。

    先按"纯时长"解析，再退化为钟点（此时需要配合起止时间由上层换算）。
    """
    h = duration_to_hours(text)
    if h is not None:
        return h, "duration"
    return None, "none"


def to_float(text: Any) -> float | None:
    q = parse_quantity(None if text is None else str(text))
    return q.value if q else None


def to_int(text: Any) -> int | None:
    v = to_float(text)
    return int(round(v)) if v is not None else None

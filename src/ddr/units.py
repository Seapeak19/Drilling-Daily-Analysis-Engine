"""单位归一模块。

原则：
- 所有输出统一为 SI/公制（m, kN·m, kgf, g/cm³, MPa·s, kPa, L/s …）。
- 原始读数保留在 raw/source 字段，绝不静默丢弃原始值。
- 允许显式指定源单位，也支持从表头/单元格文本中识别单位后缀（如 "2350 m" / "7710 ft"）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ------------------------------------------------------------------ 换算系数
# 长度
M_PER_FT = 0.3048
MM_PER_IN = 25.4
# 力
KN_PER_KGF = 0.00980665
KN_PER_LBF = 0.004448222
KGF_PER_LBF = 0.45359237  # 1 lbf = 0.45359237 kgf
# 扭矩
KNM_PER_FTLBF = 0.001355818
# 压力
KPA_PER_PSI = 6.894757
# 密度
GCC_PER_PPG = 0.1198264
# 粘度
MPAS_PER_CP = 1.0
# 排量
LPS_PER_GPM = 0.0630902
# 体积
M3_PER_BBL = 0.1589873
M3_PER_GAL = 0.003785412


class UnitError(ValueError):
    """无法识别的单位或不可换算的单位对。"""


def _norm_unit(unit: str | None) -> str:
    if not unit:
        return ""
    u = str(unit).strip().lower()
    u = u.replace("³", "3").replace("·", "").replace(" ", "")
    u = u.replace("℃", "c").replace("℉", "f")
    return u


# ------------------------------------------------------------------ 单值换算
def length_to_m(value: float, unit: str | None) -> float:
    u = _norm_unit(unit)
    if u in ("", "m", "米", "meter", "meters", "metre"):
        return float(value)
    if u in ("ft", "英尺", "feet", "foot", "'"):
        return float(value) * M_PER_FT
    if u in ("in", "英寸", "inch", '"', "inches"):
        return float(value) * MM_PER_IN / 1000.0
    if u in ("mm", "毫米"):
        return float(value) / 1000.0
    if u in ("km", "千米", "公里"):
        return float(value) * 1000.0
    raise UnitError(f"无法换算的长度单位: {unit!r}")


def diameter_to_in(value: float, unit: str | None) -> float:
    u = _norm_unit(unit)
    if u in ("", "in", "英寸", "inch", '"', "inches"):
        return float(value)
    if u in ("mm", "毫米"):
        return float(value) / MM_PER_IN
    if u in ("m", "米"):
        return float(value) * 1000.0 / MM_PER_IN
    if u in ("ft", "英尺"):
        return float(value) * 12.0
    raise UnitError(f"无法换算的直径单位: {unit!r}")


def force_to_kgf(value: float, unit: str | None) -> float:
    u = _norm_unit(unit)
    if u in ("", "kgf", "公斤力", "kg", "kgs"):
        return float(value)
    if u in ("kn", "千牛", "kilonewton"):
        return float(value) / KN_PER_KGF
    if u in ("n", "牛", "newton"):
        return float(value) / 1000.0 / KN_PER_KGF
    # 千磅必须先于磅判断，否则 'klbf' 会被 'lbf' 分支吃掉（真实踩过的坑）
    if u in ("klbf", "klbs", "kips", "kip", "千磅"):
        return float(value) * 1000.0 * KGF_PER_LBF
    if u in ("lbf", "磅力", "lb", "lbs"):
        return float(value) * KGF_PER_LBF
    if u in ("dan", "十牛", "decanewton"):
        return float(value) * 10.0 / 1000.0 / KN_PER_KGF
    raise UnitError(f"无法换算的力单位: {unit!r}")


def torque_to_knm(value: float, unit: str | None) -> float:
    u = _norm_unit(unit)
    if u in ("", "knm", "千牛米"):
        return float(value)
    if u in ("ftlbf", "ftlb", "英尺磅", "ft-lbs", "lbft"):
        return float(value) * KNM_PER_FTLBF
    if u in ("nm", "牛米"):
        return float(value) / 1000.0
    raise UnitError(f"无法换算的扭矩单位: {unit!r}")


def pressure_to_kpa(value: float, unit: str | None) -> float:
    u = _norm_unit(unit)
    if u in ("", "kpa", "千帕"):
        return float(value)
    if u in ("psi", "磅/平方英寸", "lb/in2"):
        return float(value) * KPA_PER_PSI
    if u in ("mpa", "兆帕"):
        return float(value) * 1000.0
    if u in ("bar", "巴"):
        return float(value) * 100.0
    if u in ("pa", "帕"):
        return float(value) / 1000.0
    raise UnitError(f"无法换算的压力单位: {unit!r}")


def density_to_gcc(value: float, unit: str | None) -> float:
    u = _norm_unit(unit)
    if u in ("", "gcc", "g/cm3", "sg", "克/立方厘米"):
        return float(value)
    if u in ("ppg", "lb/gal", "磅/加仑"):
        return float(value) * GCC_PER_PPG
    if u in ("kg/m3", "千克/立方米"):
        return float(value) / 1000.0
    raise UnitError(f"无法换算的密度单位: {unit!r}")


def viscosity_to_mpas(value: float, unit: str | None) -> float:
    u = _norm_unit(unit)
    if u in ("", "mpas", "cp", "厘泊"):
        return float(value)
    if u in ("pas", "帕秒"):
        return float(value) * 1000.0
    raise UnitError(f"无法换算的粘度单位: {unit!r}")


def flow_to_lps(value: float, unit: str | None) -> float:
    u = _norm_unit(unit)
    if u in ("", "lps", "l/s", "升/秒"):
        return float(value)
    if u in ("gpm", "加仑/分"):
        return float(value) * LPS_PER_GPM
    if u in ("lpm", "l/min", "升/分"):
        return float(value) / 60.0
    if u in ("m3/min", "方/分"):
        return float(value) * 1000.0 / 60.0
    raise UnitError(f"无法换算的排量单位: {unit!r}")


def volume_to_m3(value: float, unit: str | None) -> float:
    u = _norm_unit(unit)
    if u in ("", "m3", "方", "立方米"):
        return float(value)
    if u in ("bbl", "桶"):
        return float(value) * M3_PER_BBL
    if u in ("gal", "加仑"):
        return float(value) * M3_PER_GAL
    if u in ("l", "升"):
        return float(value) / 1000.0
    raise UnitError(f"无法换算的体积单位: {unit!r}")


def temperature_to_c(value: float, unit: str | None) -> float:
    u = _norm_unit(unit)
    if u in ("", "c", "摄氏"):
        return float(value)
    if u in ("f", "华氏"):
        return (float(value) - 32.0) * 5.0 / 9.0
    raise UnitError(f"无法换算的温度单位: {unit!r}")


def speed_to_m_per_h(value: float, unit: str | None) -> float:
    """机械钻速一类"长度/时间"单位 → m/h。

    注意：步速类字段不能用 length_to_m 直接换算。
    1 ft/hr = 0.3048 m/h（时间单位相同，只有长度分子需要换算）。
    """
    u = _norm_unit(unit)
    if u in ("", "m/h", "m/hr", "米/小时"):
        return float(value)
    if u in ("ft/hr", "ft/h", "英尺/小时"):
        return float(value) * M_PER_FT
    if u in ("m/min", "米/分"):
        return float(value) * 60.0
    if u in ("ft/min", "英尺/分"):
        return float(value) * M_PER_FT * 60.0
    if u in ("m/d", "米/天"):
        return float(value) / 24.0
    if u in ("ft/d", "英尺/天"):
        return float(value) * M_PER_FT / 24.0
    raise UnitError(f"无法换算的钻速单位: {unit!r}")


# --------------------------------------------------- 带单位的文本解析工具
# 单位词元允许含斜杠、上标与点号（g/cm3、ft/hr、kn·m、lb/gal），
# 否则 "1.25 g/cm3" 会因为吃到斜杠而整体解析失败（真实踩过的坑）。
_UNIT_TOKEN = r"[A-Za-z0-9°/³·²%\u4e00-\u9fff][A-Za-z0-9°/³·²%\-_.\u4e00-\u9fff]{0,11}"
_NUM_UNIT_RE = re.compile(rf"^\s*(?P<num>[+-]?\d+(?:[.,]\d+)?)\s*(?P<unit>{_UNIT_TOKEN})?\s*$")


@dataclass
class Quantity:
    value: float
    unit: str | None
    raw: str

    def as_m(self) -> float:
        return length_to_m(self.value, self.unit)


def parse_quantity(text: str | None) -> Quantity | None:
    """从 "2350.0 m" / "7710 ft" / "1.25" 这类单元格文本解析数值与单位。"""
    if text is None:
        return None
    s = str(text).strip()
    if not s or s in ("-", "—", "/", "N/A", "n/a", "NA"):
        return None
    s = s.replace(",", "")
    m = _NUM_UNIT_RE.match(s)
    if not m:
        # 兜底：抽取第一个数字
        m2 = re.search(r"[+-]?\d+(?:\.\d+)?", s)
        if not m2:
            return None
        return Quantity(float(m2.group()), None, str(text))
    return Quantity(float(m.group("num")), m.group("unit"), str(text))


def parse_float(text: str | None) -> float | None:
    q = parse_quantity(text)
    return q.value if q else None


def parse_int(text: str | None) -> int | None:
    v = parse_float(text)
    return int(round(v)) if v is not None else None

"""单位换算测试：用换算系数的独立复算值断言，避免"实现与测试同源"的假通过。"""

from __future__ import annotations

import math

import pytest

from ddr.units import (
    UnitError,
    density_to_gcc,
    diameter_to_in,
    flow_to_lps,
    force_to_kgf,
    length_to_m,
    parse_quantity,
    pressure_to_kpa,
    speed_to_m_per_h,
    volume_to_m3,
)

# 换算系数按行业标准独立列出（不引用实现里的常量）
FT = 0.3048
IN = 0.0254
LBF = 4.4482216152605  # N
KGF = 9.80665  # N
PSI = 6.894757
PPG = 119.8264  # kg/m³


class TestLength:
    def test_metre_identity(self):
        assert length_to_m(2350.0, "m") == 2350.0
        assert length_to_m(2350.0, None) == 2350.0
        assert length_to_m(2350.0, "米") == 2350.0

    def test_feet(self):
        assert length_to_m(1000.0, "ft") == pytest.approx(1000 * FT)
        assert length_to_m(7710.0, "英尺") == pytest.approx(7710 * FT, rel=1e-9)

    def test_inch_and_mm(self):
        assert length_to_m(12.0, "in") == pytest.approx(12 * IN)
        assert length_to_m(500.0, "mm") == pytest.approx(0.5)

    def test_unknown_unit_raises_not_guesses(self):
        # 关键行为：不认识就报错，绝不默认公制
        with pytest.raises(UnitError):
            length_to_m(1.0, "furlong")


class TestDiameter:
    def test_inch_identity(self):
        assert diameter_to_in(12.25, "in") == 12.25
        assert diameter_to_in(12.25, None) == 12.25

    def test_mm_to_inch(self):
        assert diameter_to_in(311.15, "mm") == pytest.approx(311.15 / 25.4, rel=1e-6)

    def test_metre_to_inch(self):
        assert diameter_to_in(0.31115, "m") == pytest.approx(0.31115 * 1000 / 25.4, rel=1e-6)


class TestForce:
    def test_kgf_identity(self):
        assert force_to_kgf(100.0, "kgf") == 100.0

    def test_kn_to_kgf(self):
        # 1 kN = 1000/9.80665 kgf ≈ 101.9716
        assert force_to_kgf(100.0, "kN") == pytest.approx(100 * 1000 / KGF, rel=1e-9)

    def test_klbf_to_kgf(self):
        # 1 klbf = 1000 lbf = 4448.22 N = 453.592 kgf
        assert force_to_kgf(10.0, "klbf") == pytest.approx(10 * 1000 * LBF / KGF, rel=1e-9)

    def test_kgf_and_lbf_are_both_force_units(self):
        # 行业习惯：钻压的 lbf 与 kgf 数值近似相等（kgf 是 lbf 的公制对应量）
        assert force_to_kgf(50.0, "kgf") == 50.0
        assert force_to_kgf(50.0, "lbf") == pytest.approx(50 * LBF / KGF, rel=1e-9)


class TestDensity:
    def test_gcc_identity(self):
        assert density_to_gcc(1.25, "g/cm3") == 1.25
        assert density_to_gcc(1.25, "gcc") == 1.25

    def test_ppg(self):
        # 1 ppg = 119.8264 kg/m³ = 0.1198264 g/cm³
        assert density_to_gcc(10.0, "ppg") == pytest.approx(10 * PPG / 1000, rel=1e-9)

    def test_typical_mud_roundtrip(self):
        # 典型钻井液 1.25 g/cm³ ≈ 10.43 ppg
        ppg = 1.25 / (PPG / 1000)
        assert ppg == pytest.approx(10.43, abs=0.01)
        assert density_to_gcc(ppg, "ppg") == pytest.approx(1.25, rel=1e-9)


class TestSpeed:
    def test_metre_per_hour_identity(self):
        assert speed_to_m_per_h(12.5, "m/h") == 12.5
        assert speed_to_m_per_h(12.5, None) == 12.5

    def test_feet_per_hour(self):
        # 关键回归点：钻速不能用 length_to_m 直接换算（那是只换分子）
        assert speed_to_m_per_h(100.0, "ft/hr") == pytest.approx(100 * FT)

    def test_not_confused_with_length(self):
        # 30 ft/hr 是 9.14 m/h，不是 9.14 m
        assert speed_to_m_per_h(30.0, "ft/hr") == pytest.approx(9.144, abs=1e-3)
        assert length_to_m(30.0, "ft") == pytest.approx(9.144, abs=1e-3)


class TestFlowPressureVolume:
    def test_flow_gpm(self):
        assert flow_to_lps(100.0, "gpm") == pytest.approx(100 * 0.0630902, rel=1e-6)

    def test_pressure_psi(self):
        assert pressure_to_kpa(1000.0, "psi") == pytest.approx(1000 * PSI, rel=1e-9)

    def test_volume_bbl(self):
        assert volume_to_m3(100.0, "bbl") == pytest.approx(100 * 0.1589873, rel=1e-6)


class TestParseQuantity:
    @pytest.mark.parametrize(
        "text,value,unit",
        [
            ("2350.0 m", 2350.0, "m"),
            ("7,710.5 ft", 7710.5, "ft"),
            ("1.25 g/cm3", 1.25, "g/cm3"),
            ("12.25", 12.25, None),
            ("50 klbf", 50.0, "klbf"),
        ],
    )
    def test_parses(self, text, value, unit):
        q = parse_quantity(text)
        assert q is not None
        assert q.value == pytest.approx(value)
        assert (q.unit or None) == unit

    @pytest.mark.parametrize("text", ["", "  ", "-", "—", "/", "N/A", "n/a"])
    def test_empty_forms_return_none(self, text):
        assert parse_quantity(text) is None

    def test_negative_values(self):
        q = parse_quantity("-12.5")
        assert q is not None and q.value == -12.5

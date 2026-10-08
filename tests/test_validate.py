"""校验层测试：24 小时硬约束、钟点连续性、数值合理性。

这是本项目的合规核心（项目计划书 2.1：时间必须加总为 24.00 小时）。
关键行为约定：
- 日报原文不合规 → valid=False（补记缺口不算"改对"）；
- 缺口显式补记 UNKNOWN 并保留 unaccounted_hours，不做静默摊平；
- 超计要与缺口区分开（一个是漏记、一个是重复计）。
"""

from __future__ import annotations

import pytest

from ddr.model import DailyReport, ReportHeader, TimeEntry, Well
from ddr.validate import clock_to_hours, validate_report, verify_time_log


def E(hours, start=None, end=None, code="DRILL", category="productive", **kw):
    return TimeEntry(hours=hours, code=code, category=category, is_npt=(category == "npt"),
                     start=start, end=end, **kw)


class TestClock:
    @pytest.mark.parametrize(
        "text,expected",
        [("00:00", 0.0), ("06:30", 6.5), ("12:00", 12.0), ("23:59", 23.9833333), ("24:00", 24.0)],
    )
    def test_valid(self, text, expected):
        assert clock_to_hours(text) == pytest.approx(expected, abs=1e-6)

    @pytest.mark.parametrize("text", ["", None, "25:00", "12:60", "abc", "12"])
    def test_invalid(self, text):
        assert clock_to_hours(text) is None


class TestPerfect24h:
    def test_exact_sum_is_valid(self):
        entries = [E(6.5, "00:00", "06:30"), E(8.0, "06:30", "14:30"), E(9.5, "14:30", "24:00")]
        tv = verify_time_log(entries)
        assert tv.valid is True
        assert tv.sum_hours == 24.0
        assert tv.delta_hours == 0.0
        assert tv.unaccounted_hours is None
        assert tv.clock_continuity_ok is True

    def test_tolerance_boundary(self):
        # 容差 ±0.01 h：加总落在容差内算合规
        assert verify_time_log([E(23.995, "00:00", "24:00"), E(0.005)]).valid is True
        # 加总超出容差即不合规
        assert verify_time_log([E(23.9, "00:00", "24:00"), E(0.05)]).valid is False

    def test_entry_level_drift_is_reported_even_when_total_ok(self):
        """单条时长与钟点不符要单独报出来——总分对了不代表每条都对。"""
        tv = verify_time_log([E(23.98, "00:00", "24:00"), E(0.02)])
        assert tv.valid is True
        assert any("与钟点不符" in m for m in tv.messages)


class TestGap:
    def test_gap_detected_and_filled(self):
        entries = [E(10.0, "00:00", "10:00"), E(12.5, "10:00", "22:30")]
        tv = verify_time_log(entries)
        assert tv.valid is False, "原文加总不足 24 h，必须判为不合规"
        assert tv.unaccounted_hours == pytest.approx(1.5)
        # 补记后加总恢复 24.00，供下游分析使用
        assert tv.sum_hours == pytest.approx(24.0)
        assert len(entries) == 3
        fill = entries[-1]
        assert fill.code == "UNKNOWN"
        assert fill.duration_source == "derived_from_total"

    def test_gap_not_silently_spread(self):
        """缺口不能被摊到其他条目上——那会篡改日报原文。"""
        entries = [E(10.0, "00:00", "10:00"), E(12.5, "10:00", "22:30")]
        original = [e.hours for e in entries]
        verify_time_log(entries)
        assert [e.hours for e in entries[:2]] == original

    def test_gap_message_mentions_amount(self):
        entries = [E(10.0), E(12.5)]
        tv = verify_time_log(entries)
        assert any("1.50" in m for m in tv.messages)
        assert any("缺口" in m for m in tv.messages)

    def test_fill_can_be_disabled(self):
        entries = [E(10.0), E(12.5)]
        tv = verify_time_log(entries, fill_gap=False)
        assert len(entries) == 2
        assert tv.sum_hours == pytest.approx(22.5)
        assert tv.unaccounted_hours == pytest.approx(1.5)


class TestOverCount:
    def test_over_24_detected(self):
        entries = [E(13.0, "00:00", "13:00"), E(12.0, "13:00", "25:00")]
        tv = verify_time_log(entries)
        assert tv.valid is False
        assert tv.delta_hours == pytest.approx(1.0)
        assert tv.unaccounted_hours is None, "超计不是缺口，不应写成 unaccounted"
        assert any("超计" in m for m in tv.messages)

    def test_over_count_not_trimmed(self):
        entries = [E(13.0), E(12.0)]
        verify_time_log(entries)
        assert len(entries) == 2 and sum(e.hours for e in entries) == 25.0


class TestClockContinuity:
    def test_hole_in_clock_detected(self):
        entries = [E(6.0, "00:00", "06:00"), E(6.0, "08:00", "14:00"), E(10.0, "14:00", "24:00")]
        tv = verify_time_log(entries)
        assert tv.clock_continuity_ok is False
        assert any("空洞" in m or "不连续" in m for m in tv.messages)

    def test_overlap_detected(self):
        entries = [E(8.0, "00:00", "08:00"), E(8.0, "06:00", "14:00"), E(10.0, "14:00", "24:00")]
        tv = verify_time_log(entries)
        assert tv.clock_continuity_ok is False
        assert any("重叠" in m for m in tv.messages)

    def test_span_hours_mismatch_reported(self):
        entries = [E(5.0, "00:00", "06:00"), E(18.0, "06:00", "24:00")]
        tv = verify_time_log(entries)
        assert any("与钟点不符" in m for m in tv.messages)

    def test_no_clocks_means_continuity_unknown(self):
        entries = [E(12.0), E(12.0)]
        tv = verify_time_log(entries)
        assert tv.clock_continuity_ok is None


class TestNumericValidation:
    def _report(self, **rep_kw) -> DailyReport:
        r = DailyReport(well=Well(well_name="T-1"), report=ReportHeader(dtim_start="2026-01-01T00:00:00",
                                                                        dtim_end="2026-01-02T00:00:00"))
        for k, v in rep_kw.items():
            setattr(r.report, k, v)
        return r

    def test_depth_regression_detected(self):
        r = self._report(md_m=2300.0, md_in_start_m=2350.0)
        res = validate_report(r)
        assert any(i.field == "report.md_m" and i.severity == "error" for i in res.issues)

    def test_tvd_greater_than_md_detected(self):
        r = self._report(md_m=2300.0, tvd_m=2400.0)
        res = validate_report(r)
        assert any(i.field == "report.tvd_m" for i in res.issues)

    def test_footage_inconsistency_warns(self):
        r = self._report(md_m=2300.0, md_in_start_m=2100.0, progress_m=150.0)
        res = validate_report(r)
        assert any(i.field == "report.progress_m" and i.severity == "warning" for i in res.issues)

    def test_rop_cross_check(self):
        r = self._report(md_m=2300.0, md_in_start_m=2100.0, progress_m=200.0, rop_av_m_per_h=50.0)
        r.time_log = [E(10.0, code="DRILL")]
        res = validate_report(r)
        # 实算 200/10 = 20 m/h，日报写 50 → 偏差超阈值
        assert any(i.field == "report.rop_av_m_per_h" for i in res.issues)

    def test_npt_flag_inconsistency_is_error(self):
        r = self._report(md_m=2300.0)
        r.time_log = [TimeEntry(hours=24.0, code="REPAIR", category="npt", is_npt=False)]
        res = validate_report(r)
        assert any("is_npt" in i.field for i in res.issues)

    def test_bit_footage_consistency(self):
        from ddr.model import BitRecord

        r = self._report(md_m=2300.0)
        r.time_log = [E(24.0)]
        r.bit_records = [BitRecord(bit_no=1, depth_in_m=1800.0, depth_out_m=2300.0, footage_m=400.0)]
        res = validate_report(r)
        assert any("footage_m" in i.field for i in res.issues)

    def test_bit_depth_reversal_is_error(self):
        from ddr.model import BitRecord

        r = self._report(md_m=2300.0)
        r.time_log = [E(24.0)]
        r.bit_records = [BitRecord(bit_no=1, depth_in_m=2300.0, depth_out_m=1800.0)]
        res = validate_report(r)
        assert any("depth_out_m" in i.field and i.severity == "error" for i in res.issues)

    def test_mud_density_out_of_range_warns(self):
        from ddr.model import MudProperty

        r = self._report(md_m=2300.0)
        r.time_log = [E(24.0)]
        r.mud = [MudProperty(sample_point="flowline", density_gcc=9.9)]
        res = validate_report(r)
        assert any("density_gcc" in i.field for i in res.issues)


class TestValidationAggregation:
    def test_valid_report_passes(self):
        from ddr.model import BitRecord

        r = DailyReport(well=Well(well_name="T-1"), report=ReportHeader(dtim_start="2026-01-01T00:00:00",
                                                                       dtim_end="2026-01-02T00:00:00",
                                                                       md_m=2300.0, md_in_start_m=2100.0,
                                                                       progress_m=200.0))
        r.time_log = [E(10.0, "00:00", "10:00"), E(14.0, "10:00", "24:00", category="flat", code="CIRCULATE")]
        r.bit_records = [BitRecord(bit_no=1, depth_in_m=1800.0, depth_out_m=2300.0, footage_m=500.0, hours=50.0)]
        res = validate_report(r)
        assert res.ok is True
        assert not res.errors

    def test_time_verification_backfilled(self):
        r = DailyReport(well=Well(well_name="T-1"), report=ReportHeader(dtim_start="2026-01-01T00:00:00",
                                                                       dtim_end="2026-01-02T00:00:00"))
        r.time_log = [E(24.0, "00:00", "24:00")]
        validate_report(r)
        assert r.time_verification is not None
        assert r.time_verification.valid is True

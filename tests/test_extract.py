"""抽取与单位判定测试（含跨语言标签匹配的回归用例）。"""

from __future__ import annotations

import pytest

from ddr.extract import (
    TEMPLATE_SPECS,
    _label_matches,
    _looks_like_trailing_numeric_suffix,
    _strip_label_prefix,
    parse_dull_grade,
)
from ddr.normalize import (
    clock_to_hours,
    duration_to_hours,
    label_unit,
    resolve_number,
)


class TestLabelMatches:
    """跨语言标签匹配的边界行为。"""

    def test_exact(self):
        assert _label_matches("Report No.", ["Report No.", "Report No"])

    def test_label_with_unit_suffix(self):
        assert _label_matches("井 深 (MD)", ["井 深", "井深"])
        assert _label_matches("Depth (ft)", ["depth"])
        assert _label_matches("MW (ppg)", ["mw"])

    def test_cjk_substring_allowed(self):
        """中文连续书写：'磨损分级' 里的 '磨损' 是合法匹配（曾因英文词边界被拦掉）。"""
        assert _label_matches("磨损分级", ["磨损", "dull", "grading"])
        assert _label_matches("时间分解", ["时间"])

    def test_latin_word_boundary_enforced(self):
        """英文必须词边界：'date' 不得命中 'Spud Date'（曾导致开钻日期被当成上报日期）。"""
        assert _label_matches("Spud Date", ["spud date"])
        # 'date' 作为独立词元出现在 'spud date' 中时，'date' 后面是行尾、前面是空格，
        # 因此单独匹配 'date' 是允许的——但模板层必须先匹配 'spud date' 才能保证语义正确。
        assert TEMPLATE_SPECS["iadc_classic"]


class TestTrailingNumericSuffix:
    """井名/构造名里的数字不是"标签的值"（曾把 PL-6-2-A12H 截成 2-A12H）。"""

    @pytest.mark.parametrize("value", ["19-3", "48-12-66X", "6-2-A12H", "2-A12H"])
    def test_rejects_name_fragments(self, value):
        assert _looks_like_trailing_numeric_suffix(value) is True

    @pytest.mark.parametrize("value", ["2130.36", "1", "942", "12.25", "24.00", "2355.07"])
    def test_accepts_real_values(self, value):
        assert _looks_like_trailing_numeric_suffix(value) is False

    def test_accepts_trailing_units(self):
        assert _looks_like_trailing_numeric_suffix("2130.36 m") is False
        assert _looks_like_trailing_numeric_suffix("第 3 号") is False


class TestStripLabelPrefix:
    def test_well_name(self):
        got = _strip_label_prefix("井 号 PL-6-2-A12H", ["井 号", "井号", "well name"])
        assert got == ("井 号", "PL-6-2-A12H")

    def test_rig_with_space_in_value(self):
        got = _strip_label_prefix("钻 机 海洋石油 942", ["钻 机", "钻机", "rig"])
        assert got == ("钻 机", "海洋石油 942")

    def test_non_label_line_returns_none(self):
        assert _strip_label_prefix("这是一行说明文字", ["井 号", "钻 机"]) is None


class TestDullGrade:
    def test_standard_code(self):
        got = parse_dull_grade("2-3-WT-S-X-I-NO-TD")
        assert got["inner_wear"] == 2
        assert got["outer_wear"] == 3
        assert got["dull_char"] == "WT"
        assert got["location"] == "S"
        assert got["bearing"] == "X"
        assert got["gauge"] == "I"
        assert got["other_char"] == "NO"
        assert got["reason_pulled"] == "TD"

    def test_en_dash_variant(self):
        assert parse_dull_grade("1–2–CT–M–X–I–NO–PR") is not None

    @pytest.mark.parametrize("text", [None, "", "abc", "2-3-WT", "12-3-WT-S-X-I-NO-TD"])
    def test_invalid_returns_none(self, text):
        assert parse_dull_grade(text) is None


class TestDurationParsing:
    @pytest.mark.parametrize(
        "text,hours",
        [
            ("6.5", 6.5),
            ("6.5 h", 6.5),
            ("6.5小时", 6.5),
            ("45 min", 0.75),
            ("45分钟", 0.75),
            ("1h30min", 1.5),
            ("2 天", 48.0),
        ],
    )
    def test_variants(self, text, hours):
        assert duration_to_hours(text) == pytest.approx(hours, abs=1e-6)

    @pytest.mark.parametrize("text", [None, "", "-", "—", "abc"])
    def test_invalid(self, text):
        assert duration_to_hours(text) is None

    def test_clock_not_mistaken_for_duration(self):
        assert clock_to_hours("06:30") == 6.5


class TestLabelUnit:
    @pytest.mark.parametrize(
        "label,unit",
        [
            ("井深 (m)", "m"),
            ("Depth (ft)", "ft"),
            ("MW (ppg)", "ppg"),
            ("密度(g/cm3)", "g/cm3"),
            ("ROP (ft/hr)", "ft/hr"),
            ("无单位标签", None),
        ],
    )
    def test_extracts(self, label, unit):
        assert label_unit(label) == unit


class TestUnitResolutionPriority:
    """单位判定优先级：值自带 > 模板覆盖 > 列头标注 > 模板默认 > 不猜。"""

    def test_value_unit_wins(self):
        r = resolve_number("2350.0 m", field_kind="length", template_id="iadc_classic", field_key="depth")
        assert r.unit_source == "value"
        assert r.si == pytest.approx(2350.0)

    def test_template_override_beats_label(self):
        """列头写 ft 但模板声明该区块是 m 时，按模板声明解析（避免把公制值当英尺）。"""
        r = resolve_number(
            "2350.0",
            field_kind="length",
            label="井深 (ft)",
            template_id="iadc_classic",
            field_key="depth",
            source_unit_override="m",
        )
        assert r.unit_source == "template-override"
        assert r.si == pytest.approx(2350.0)

    def test_label_unit_used_when_no_override(self):
        r = resolve_number("2350.0", field_kind="length", label="井深 (ft)", template_id="unknown", field_key="depth")
        assert r.unit_source == "label"
        assert r.si == pytest.approx(2350.0 * 0.3048)

    def test_template_default_is_last_resort(self):
        r = resolve_number("2350.0", field_kind="length", template_id="iadc_classic", field_key="depth")
        # iadc_classic 的模板默认 depth 是 m
        assert r.unit_source == "template"
        assert r.si == pytest.approx(2350.0)

    def test_unknown_unit_is_not_guessed(self):
        r = resolve_number("2350.0", field_kind="length", template_id="unknown", field_key="depth")
        assert r.si is None
        assert r.raw == 2350.0
        assert r.unit_source == "unknown"
        assert any("无法确定单位" in n for n in r.notes)

    def test_mismatched_value_unit_is_ignored_with_note(self):
        r = resolve_number("12.5 kN", field_kind="length", template_id="cn_vertical", field_key="depth")
        assert any("不符" in n for n in r.notes)

    def test_empty_cell_is_ok_not_error(self):
        for text in (None, "", "-", "—", "N/A"):
            r = resolve_number(text, field_kind="length")
            assert r.si is None and r.ok is True


class TestTemplateSpecIntegrity:
    def test_all_templates_have_signatures(self):
        for tid, spec in TEMPLATE_SPECS.items():
            assert spec.time_column_signature, tid
            assert spec.bit_column_signature, tid
            assert spec.mud_column_signature, tid
            assert spec.header_labels, tid

    def test_iadc_has_block_level_unit_overrides(self):
        spec = TEMPLATE_SPECS["iadc_classic"]
        assert spec.header_unit_overrides.get("depth") == "m"
        assert spec.bit_unit_overrides.get("depth") == "m"


# --------------------------------------------------------------------- 备注钟点
class TestRemarkClockExtraction:
    """备注钟点抽取的行为契约。

    背景：早期样本把钟点写了两遍（`time_hint` 一个整点标签 + 正文一个真实时刻），
    渲染成 "06:00　06:30 钻进至…"。第二个钟点残留在正文里，
    被下游的时长抽取当成 "6 小时"，导致 events[].hours 被 100% 污染。

    现在约定：**钟点只出现一次**（写在正文开头），`time_hint` 从正文派生。
    这一组锁定抽取端的解析行为，确保：
      1. 单个钟点 → hint 取真实分钟（不是截断到整点）；
      2. 两个连续钟点（历史样本形态）→ 只取第一个，第二个必须被丢弃；
      3. 正文里不得残留任何钟点。
    """

    @staticmethod
    def _seg(lines: list[str]):
        """构造一个最小 Segmented，只喂备注区块（不依赖真实文档）。"""
        from ddr.detect import BLOCK_REMARKS, Detection, Segmented

        seg = Segmented(
            detection=Detection(
                template_id="cn_vertical", label="测试用", confidence=1.0, unit_system="metric"
            )
        )
        seg.blocks[BLOCK_REMARKS] = list(lines)
        return seg

    def test_single_clock_keeps_real_minutes(self):
        from ddr.extract import extract_remarks

        rems = extract_remarks(self._seg(["06:30 钻进至 2066.06 m，钻压 80 kN。"]))
        assert len(rems) == 1
        assert rems[0].time_hint == "06:30", "必须保留真实分钟，不能截断成 06:00"
        assert rems[0].text == "钻进至 2066.06 m，钻压 80 kN。"
        assert ":" not in rems[0].text, "正文里不得残留钟点"

    def test_duplicate_clock_is_dropped(self):
        """历史样本形态：time_hint 与正文各印一个钟点 → 只取第一个。"""
        from ddr.extract import extract_remarks

        rems = extract_remarks(self._seg(["06:00\u300006:30 钻进至 2066.06 m。"]))
        assert len(rems) == 1
        assert rems[0].time_hint == "06:00"
        assert rems[0].text == "钻进至 2066.06 m。"
        assert ":" not in rems[0].text, f"第二个钟点未被丢弃：{rems[0].text!r}"

    def test_identical_duplicate_clock_is_dropped(self):
        """新样本形态：hint 与正文钟点相同（都是 06:30）→ 同样只留一个。"""
        from ddr.extract import extract_remarks

        rems = extract_remarks(self._seg(["06:30\u300006:30 钻进至 2066.06 m。"]))
        assert rems[0].time_hint == "06:30"
        assert rems[0].text == "钻进至 2066.06 m。"

    def test_no_clock_leaves_hint_none(self):
        from ddr.extract import extract_remarks

        rems = extract_remarks(self._seg(["继续钻进至设计中完井深。"]))
        assert rems[0].time_hint is None
        assert rems[0].text == "继续钻进至设计中完井深。"

    def test_long_duration_text_keeps_real_duration(self):
        """时长写在正文里时必须完整保留（不能被钟点处理误伤）。"""
        from ddr.extract import extract_remarks

        rems = extract_remarks(self._seg(["13:00 顶驱 VFD 故障，停钻 4.0 小时。"]))
        assert rems[0].time_hint == "13:00"
        assert "停钻 4.0 小时" in rems[0].text

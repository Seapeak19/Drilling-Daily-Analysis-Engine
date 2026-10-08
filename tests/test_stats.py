"""数据集统计测试。

这些断言的作用是**防止数据集本身退化**：如果哪天改了模拟器或模板，
导致工况覆盖变少、时间不再严格加总、或某个区块整块消失，
测试会立刻失败，而不是等到评测数字莫名其妙地变了才发现。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ddr.stats import HEADER_NUMERIC_FIELDS, compute_stats, render_text
from ddr.render import render_all
from ddr.simulate import build_dataset


@pytest.fixture(scope="module")
def stats_dir(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("stats_samples")
    ds = build_dataset(wells=3, days_per_well=7)
    render_all(ds, out)
    return out


class TestDatasetStats:
    def test_counts(self, stats_dir):
        st = compute_stats(stats_dir)
        assert st.sample_count == 21
        assert st.well_count == 3
        assert len(st.wells) == 3

    def test_all_templates_covered(self, stats_dir):
        st = compute_stats(stats_dir)
        assert set(st.templates) == {"cn_vertical", "iadc_classic", "regional_xls"}
        # 三种模板都要有足够样本，否则某一种格式的评测结论不成立
        for tpl, n in st.templates.items():
            assert n >= 3, f"{tpl} 只有 {n} 份样本"

    def test_all_phases_covered(self, stats_dir):
        """五种工况都要出现，否则时间编码字典里的部分分支没被样本覆盖。"""
        st = compute_stats(stats_dir)
        assert {"surface", "drilling", "trip_out", "trip_in", "casing"} <= set(st.phases)

    def test_time_always_sums_to_24(self, stats_dir):
        st = compute_stats(stats_dir)
        assert st.time_compliant == st.sample_count
        assert st.time_violations == []
        assert st.sum_hours_total == pytest.approx(24.0 * st.sample_count, abs=0.01)

    def test_category_hours_add_up(self, stats_dir):
        st = compute_stats(stats_dir)
        total = sum(st.categories_total.values())
        assert total == pytest.approx(st.sum_hours_total, abs=0.02)

    def test_bit_and_mud_coverage(self, stats_dir):
        st = compute_stats(stats_dir)
        assert st.mud_days == st.sample_count, "每份日报都应有泥浆性能"
        assert st.bit_days > 0, "至少要有换钻头的日报"
        assert st.remarks_total > 0

    def test_header_fields_present(self, stats_dir):
        """表头数值字段要么全部有值，要么明确知道有几份缺——不能出现"以为有、其实是 0"的情况。"""
        st = compute_stats(stats_dir)
        for field in HEADER_NUMERIC_FIELDS:
            present = st.header_field_present.get(field, 0)
            assert present > 0, f"{field} 在所有样本里都缺——ground truth 可能漏写了"
        # 当日进尺、井深、日期这类字段必须每份都有
        for field in ("md_m", "tvd_m", "progress_m", "report_no"):
            assert st.header_field_present[field] == st.sample_count, field

    def test_npt_and_ilt_present(self, stats_dir):
        st = compute_stats(stats_dir)
        assert st.npt_hours_total > 0
        assert st.npt_days > 0
        assert st.npt_rate is not None and 0 < st.npt_rate < 0.5
        assert st.connections_total > 0
        assert st.connection_days > 0

    def test_synthetic_flag_reported(self, stats_dir):
        """数据性质必须能被统计出来——这是对外说明"样本是合成的"的依据。"""
        st = compute_stats(stats_dir)
        assert st.synthetic_flags.get("synthetic") == st.sample_count

    def test_serializable_and_renderable(self, stats_dir):
        st = compute_stats(stats_dir)
        d = st.to_dict()
        json.dumps(d, ensure_ascii=False)  # 必须能落盘
        text = render_text(st)
        assert "时间分解合规性" in text
        assert "NPT" in text

    def test_missing_manifest_raises_actionable_error(self, tmp_path):
        with pytest.raises(FileNotFoundError) as ei:
            compute_stats(tmp_path)
        assert "manifest.json" in str(ei.value)
        assert "ddr.render" in str(ei.value), "报错要告诉用户怎么生成，而不是只说找不到"


class TestCategoryClassification:
    def test_known_codes(self):
        from ddr.stats import _category_of_code

        assert _category_of_code("DRILL") == "productive"
        assert _category_of_code("REAM") == "productive"
        assert _category_of_code("TRIP_OUT") == "flat"
        assert _category_of_code("CASING") == "flat"
        assert _category_of_code("STUCK") == "npt"
        assert _category_of_code("REPAIR") == "npt"
        assert _category_of_code(None) == "flat"

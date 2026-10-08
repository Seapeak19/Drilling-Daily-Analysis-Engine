"""端到端测试：合成样本 → 解析 → Schema 校验 → 评测达标判定。

这些测试把 M0/M1 的验收判据变成可自动执行的检查：
- M0：「1 种日报格式解析成功，关键字段准确率 ≥ 90%」
- M1：「3 种格式支持，字段准确率报告」
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ddr.datacheck import check_dataset_file
from ddr.evaluate import evaluate_dataset
from ddr.model import validate_payload
from ddr.pipeline import parse_file, parse_files
from ddr.render import render_all
from ddr.simulate import build_dataset

# 整个模块共用一个临时数据集，避免重复渲染 21 份 PDF（较慢）
pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def dataset_dir(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("samples")
    ds = build_dataset(wells=3, days_per_well=7)
    render_all(ds, out)
    (out / "dataset.json").write_text(json.dumps(ds, ensure_ascii=False), encoding="utf-8")
    return out


class TestDatasetGroundTruth:
    def test_ground_truth_matches_report_content(self, dataset_dir):
        """标尺自身必须与日报内容一致，否则准确率是假的。"""
        res = check_dataset_file(dataset_dir / "dataset.json")
        assert res.problems == [], "\n".join(res.problems[:10])
        assert res.checked_days == 21

    def test_three_templates_present(self, dataset_dir):
        manifest = json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))
        templates = {it["template_id"] for it in manifest["items"]}
        assert templates == {"cn_vertical", "iadc_classic", "regional_xls"}

    def test_manifest_declares_synthetic(self, dataset_dir):
        manifest = json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))
        assert "合成" in manifest["_disclaimer"]
        assert all(it["synthetic"] for it in manifest["items"])


class TestParseEndToEnd:
    def test_schema_valid(self, dataset_dir):
        manifest = json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))
        for it in manifest["items"][:3]:
            res = parse_file(dataset_dir / it["file"])
            errors = validate_payload(res.report.to_dict())
            assert errors == [], f"{it['file']}: {errors[:3]}"

    def test_every_sample_sums_to_24h(self, dataset_dir):
        manifest = json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))
        for it in manifest["items"]:
            res = parse_file(dataset_dir / it["file"])
            tv = res.report.time_verification
            assert tv is not None
            assert tv.sum_hours == pytest.approx(24.0, abs=0.01), it["file"]
            assert tv.valid is True, it["file"]

    def test_time_entries_match_truth_count(self, dataset_dir):
        manifest = json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))
        for it in manifest["items"]:
            truth = json.loads((dataset_dir / it["truth"]).read_text(encoding="utf-8"))["truth"]
            res = parse_file(dataset_dir / it["file"])
            assert len(res.report.time_log) == len(truth["time_log"]), it["file"]

    def test_template_detection_matches_manifest(self, dataset_dir):
        manifest = json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))
        for it in manifest["items"]:
            res = parse_file(dataset_dir / it["file"])
            assert res.detection.template_id == it["template_id"], it["file"]


class TestAcceptanceCriteria:
    def test_key_field_accuracy_at_least_90pct(self, dataset_dir):
        """M0 验收判据：关键数值字段准确率 ≥ 90%。"""
        rep = evaluate_dataset(dataset_dir)
        overall = rep.overall()
        assert overall["accuracy"] is not None
        assert overall["accuracy"] >= 0.90, f"实际 {overall['accuracy']:.2%}"

    def test_header_group_accuracy(self, dataset_dir):
        rep = evaluate_dataset(dataset_dir)
        stats = rep.group_stats()
        assert stats["表头/井信息"]["accuracy"] >= 0.90

    def test_time_breakdown_group_accuracy(self, dataset_dir):
        rep = evaluate_dataset(dataset_dir)
        stats = rep.group_stats()
        assert stats["时间分解"]["accuracy"] >= 0.90

    def test_no_parse_crashes(self, dataset_dir):
        rep = evaluate_dataset(dataset_dir)
        assert [s.file for s in rep.samples if s.error] == []


class TestMultiFileChaining:
    def test_daily_footage_derived_from_previous_depth(self, dataset_dir):
        """当日进尺要靠相邻日报井深接续反推，批量解析才能拿到。"""
        manifest = json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))
        cn_items = [it for it in manifest["items"] if it["template_id"] == "cn_vertical"][:3]
        paths = [dataset_dir / it["file"] for it in cn_items]
        results = parse_files(paths)
        for it, res in zip(cn_items, results):
            truth = json.loads((dataset_dir / it["truth"]).read_text(encoding="utf-8"))["truth"]
            assert res.report.report.progress_m == pytest.approx(truth["progress_m"], abs=0.02), it["file"]

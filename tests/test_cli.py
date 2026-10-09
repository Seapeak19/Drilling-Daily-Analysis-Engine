"""命令行入口测试（`cli.py`）。

## 为什么这组测试重要

CLI 是**用户与 Skill 的第一入口**，也是** CI 与自动化脚本的接口**。
它坏了的表现分两类，都很难察觉：

1. **退出码不对**。`ddr privacy` / `ddr check` / `ddr robust` 的退出码是
   CI 的闸门信号（非 0 即阻断）。如果 `privacy` 在发现问题时仍返回 0，
   CI 就会放行真实数据入库 —— 而这正是唯一"一旦发生就无法挽回"的事故。
   所以退出码是**契约**，不是实现细节。
2. **参数解析漂移**。`main()` 会给缺失属性填默认值；子命令之间共享
   `args` 命名空间。`chart` 子命令甚至会 monkey-patch 一批属性后转发给
   `cmd_parse` —— 少补一个就是 `AttributeError`。

## 测试方式

直接调用 `main(argv)` 并断言返回码与输出，**不启子进程**：
更快，且能覆盖到函数级行为。这里刻意不真的写文件到仓库 ——
需要输出文件时一律用 `tmp_path`（否则会把工作区弄脏，
而 CI 有一道"检查过程不得改动仓库文件"的断言）。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ddr.cli import build_parser, main

SAMPLES = Path("data/samples")
SAMPLE_PDF = SAMPLES / "samples" / "PL-6-2-A12H_D01_2026-03-01.pdf"


# --------------------------------------------------------------------- 造边界输入
@pytest.fixture(scope="module")
def broken_pdf(tmp_path_factory) -> Path:
    """一个存在但无法解析的 PDF —— 用于触发读取失败（退出码 3）。"""
    d = tmp_path_factory.mktemp("broken")
    p = d / "broken.pdf"
    p.write_bytes(b"%PDF-1.4 this is not a real pdf")
    return p


@pytest.fixture(scope="module")
def scanned_pdf(tmp_path_factory) -> Path:
    """无文本层的纯图片 PDF（模拟扫描件）。"""
    import pymupdf

    d = tmp_path_factory.mktemp("scanned")
    p = d / "scanned.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40))
    pix.set_rect(pix.irect, (200, 200, 200))
    page.insert_image(pymupdf.Rect(72, 72, 400, 400), pixmap=pix)
    doc.save(str(p))
    doc.close()
    return p


@pytest.fixture(scope="module")
def sample_file() -> Path:
    if not SAMPLE_PDF.exists():
        pytest.skip("样本日报不存在")
    return SAMPLE_PDF


# --------------------------------------------------------------------- 解析器构建
class TestArgumentParser:
    def test_subcommands_are_registered(self):
        """子命令集合是稳定接口，脚本与文档都依赖它。"""
        ap = build_parser()
        actions = [a for a in ap._actions if hasattr(a, "choices") and a.choices]
        subs = set()
        for a in actions:
            subs |= set(a.choices)
        assert {
            "parse", "chart", "batch", "check", "eval", "robust", "stats", "privacy", "info",
        } <= subs, f"缺少子命令：{subs}"

    def test_no_subcommand_is_an_error(self):
        """不带子命令必须报错退出（required=True），而不是静默成功。"""
        with pytest.raises(SystemExit):
            main([])

    def test_version_flag(self, capsys):
        with pytest.raises(SystemExit) as ei:
            main(["--version"])
        assert ei.value.code == 0
        assert "ddr-parser" in capsys.readouterr().out

    def test_parse_requires_input(self):
        with pytest.raises(SystemExit):
            main(["parse"])


# --------------------------------------------------------------------- info
class TestInfoCommand:
    def test_returns_zero_and_lists_templates_and_codes(self, capsys):
        assert main(["info"]) == 0
        out = capsys.readouterr().out
        for tpl in ("cn_vertical", "iadc_classic", "regional_xls"):
            assert tpl in out, f"info 应列出模板 {tpl}"
        assert "操作码字典" in out
        assert "支持的文件类型" in out


# --------------------------------------------------------------------- parse
class TestParseCommand:
    def test_missing_file_returns_2(self, capsys):
        """文件不存在属于"用户输入错误"，退出码 2（与读取失败 3 区分）。"""
        rc = main(["parse", "不存在的文件.pdf"])
        assert rc == 2
        assert "不存在" in capsys.readouterr().err

    def test_broken_pdf_returns_3(self, broken_pdf, capsys):
        """读取失败属于"文件本身有问题"，退出码 3。"""
        rc = main(["parse", str(broken_pdf)])
        assert rc == 3
        assert "读取失败" in capsys.readouterr().err

    def test_scanned_pdf_reports_actionable_error(self, scanned_pdf, capsys):
        """扫描件必须明确要求 OCR，而不是静默产出空 JSON。"""
        rc = main(["parse", str(scanned_pdf)])
        assert rc == 3
        err = capsys.readouterr().err
        assert "读取失败" in err

    def test_success_returns_0_and_prints_json(self, sample_file, capsys):
        rc = main(["parse", str(sample_file)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["well"]["well_name"]
        assert payload["time_verification"]["sum_hours"] > 0

    def test_out_writes_file_not_stdout(self, sample_file, tmp_path, capsys):
        """--out 时 JSON 写文件，stdout 只留一行提示（不是几百行数据）。"""
        target = tmp_path / "report.json"
        rc = main(["parse", str(sample_file), "--out", str(target)])
        assert rc == 0
        assert target.exists()
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["well"]["well_name"]
        out = capsys.readouterr().out
        assert "已写入" in out
        assert len(out) < 200, "--out 时不应把整个 JSON 打到终端"

    def test_summary_prints_conclusions(self, sample_file, capsys):
        rc = main(["parse", str(sample_file), "--summary", "--no-json"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "时间分解合计" in out
        assert "NPT" in out
        assert "IADC" in out

    def test_no_json_suppresses_payload(self, sample_file, capsys):
        rc = main(["parse", str(sample_file), "--no-json"])
        assert rc == 0
        assert capsys.readouterr().out.strip() == ""

    def test_csv_export(self, sample_file, tmp_path, capsys):
        target = tmp_path / "t.csv"
        rc = main(["parse", str(sample_file), "--csv", str(target), "--no-json", "--quiet"])
        assert rc == 0
        assert target.exists()
        assert "已写入" in capsys.readouterr().out

    def test_chart_export(self, sample_file, tmp_path, capsys):
        target = tmp_path / "t.html"
        rc = main(["parse", str(sample_file), "--chart", str(target), "--quiet"])
        assert rc == 0
        assert target.exists()
        assert target.stat().st_size > 0

    def test_quiet_suppresses_parse_warnings(self, sample_file, capsys):
        """--quiet 只压解析提示，不压结论。"""
        main(["parse", str(sample_file), "--no-json", "--quiet"])
        assert "解析提示" not in capsys.readouterr().err

    def test_invalid_template_hint_is_ignored_not_crash(self, sample_file, capsys):
        """给了不存在的模板 ID 时应回退到自动识别，而不是崩溃。

        `parse_document` 只在 hint 存在于 TEMPLATE_SPECS 时才采用它。
        """
        rc = main(["parse", str(sample_file), "--template", "不存在的模板", "--no-json", "--quiet"])
        assert rc == 0

    def test_strict_mode_passes_on_good_sample(self, sample_file, capsys):
        """--strict 在 Schema 通过时不应影响退出码。"""
        rc = main(["parse", str(sample_file), "--strict", "--no-json", "--quiet"])
        assert rc == 0


# --------------------------------------------------------------------- chart
class TestChartCommand:
    """`chart` 会 monkey-patch 一批属性后转发给 cmd_parse。

    这是最容易被后续改动打破的地方：少补一个属性就是 AttributeError。
    """

    def test_chart_subcommand_works(self, sample_file, tmp_path, capsys):
        html = tmp_path / "c.html"
        csv = tmp_path / "c.csv"
        rc = main(["chart", str(sample_file), "--chart", str(html), "--csv", str(csv)])
        assert rc == 0
        assert html.exists() and csv.exists()
        out = capsys.readouterr().out
        assert "时间分解合计" in out, "chart 应同时给出结论"
        assert not out.lstrip().startswith("{"), "chart 不应打印 JSON"

    def test_chart_default_paths_use_temp_dir(self, sample_file, tmp_path, monkeypatch, capsys):
        """不带 --chart/--csv 时应正常返回（不生成文件也不崩溃）。"""
        rc = main(["chart", str(sample_file)])
        assert rc == 0

    def test_chart_reports_missing_file(self, capsys):
        assert main(["chart", "不存在.pdf"]) == 2


# --------------------------------------------------------------------- batch
class TestBatchCommand:
    def test_batch_on_sample_dir(self, tmp_path, capsys):
        rc = main(["batch", str(SAMPLES / "samples"), "--out", str(tmp_path)])
        assert rc == 0
        summary = json.loads((tmp_path / "_summary.json").read_text(encoding="utf-8"))
        assert len(summary) == 21, f"应处理 21 份样本，实际 {len(summary)}"
        out = capsys.readouterr().out
        assert "已解析 21 份" in out

    def test_batch_reports_noncompliant_count(self, tmp_path, capsys):
        main(["batch", str(SAMPLES / "samples"), "--out", str(tmp_path)])
        assert "时间分解不合规" in capsys.readouterr().out

    def test_batch_single_file(self, sample_file, tmp_path, capsys):
        rc = main(["batch", str(sample_file), "--out", str(tmp_path)])
        assert rc == 0
        assert len(json.loads((tmp_path / "_summary.json").read_text(encoding="utf-8"))) == 1

    def test_batch_missing_path_returns_2(self, tmp_path, capsys):
        assert main(["batch", "不存在的目录", "--out", str(tmp_path)]) == 2
        assert "不存在" in capsys.readouterr().err

    def test_batch_empty_dir_returns_2(self, tmp_path, capsys):
        """目录里没有可解析文件时必须报错，而不是产出空结果让人以为跑通了。"""
        empty = tmp_path / "empty"
        empty.mkdir()
        assert main(["batch", str(empty), "--out", str(tmp_path / "o")]) == 2
        assert "没有可解析" in capsys.readouterr().err


# --------------------------------------------------------------------- check
class TestCheckCommand:
    def test_check_clean_dataset_returns_0(self, capsys):
        rc = main(["check", "--data", str(SAMPLES / "dataset.json")])
        assert rc == 0
        assert "问题 0 处" in capsys.readouterr().out

    def test_check_missing_dataset_returns_2(self, tmp_path, capsys):
        rc = main(["check", "--data", str(tmp_path / "none.json")])
        assert rc == 2
        assert "错误" in capsys.readouterr().err

    def test_check_corrupt_dataset_returns_2(self, tmp_path, capsys):
        bad = tmp_path / "bad.json"
        bad.write_text("{ not json", encoding="utf-8")
        assert main(["check", "--data", str(bad)]) == 2


# --------------------------------------------------------------------- privacy
class TestPrivacyCommand:
    """私有数据闸门的退出码是**安全契约**。

    若它在发现问题时返回 0，CI 就会放行真实日报入库 ——
    而推送到公开仓库后清理需要重写 Git 历史，几乎无法彻底清除。
    """

    def test_clean_dataset_returns_0(self, capsys):
        rc = main(["privacy", "--data", str(SAMPLES)])
        assert rc == 0
        assert "未发现私有数据风险" in capsys.readouterr().out

    def test_private_data_returns_1(self, tmp_path, capsys):
        """构造一份含可疑主体名与 synthetic=false 的数据集，必须返回 1。"""
        data = tmp_path / "ds"
        (data / "samples").mkdir(parents=True)
        (data / "ground_truth").mkdir(parents=True)
        (data / "manifest.json").write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "file": "samples/REAL-1.pdf",
                            "truth": "ground_truth/REAL-1.truth.json",
                            "well_name": "大庆-12-3",
                            "operator": "某石油有限公司",
                            "contractor": "某钻探工程有限公司",
                            "synthetic": False,
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (data / "ground_truth" / "REAL-1.truth.json").write_text(
            json.dumps({"well_name": "大庆-12-3", "truth": {}}, ensure_ascii=False),
            encoding="utf-8",
        )
        rc = main(["privacy", "--data", str(data)])
        assert rc == 1, "含真实数据的样本集必须让闸门失败"
        assert "synthetic=false" in capsys.readouterr().out

    def test_json_output_is_machine_readable(self, capsys):
        rc = main(["privacy", "--data", str(SAMPLES), "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        payload = json.loads(out[out.index("{") :])
        assert payload["ok"] is True
        assert payload["scanned_items"] == 21
        assert payload["findings"] == []


# --------------------------------------------------------------------- stats
class TestStatsCommand:
    def test_stats_text_output(self, capsys):
        assert main(["stats", "--data", str(SAMPLES)]) == 0
        out = capsys.readouterr().out
        assert "数据性质" in out or "数据集" in out
        assert "ddr eval" in out, "应提示下一步可以跑评测"

    def test_stats_json_output(self, capsys):
        assert main(["stats", "--data", str(SAMPLES), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["sample_count"] == 21

    def test_stats_missing_dataset_returns_2(self, tmp_path, capsys):
        assert main(["stats", "--data", str(tmp_path)]) == 2
        assert "错误" in capsys.readouterr().err


# --------------------------------------------------------------------- eval / robust
class TestEvalAndRobustCommands:
    def test_eval_writes_report_to_given_path(self, tmp_path, capsys):
        """--out 必须生效：默认路径是已入库文件，CI 里会弄脏工作区。"""
        out = tmp_path / "ev.md"
        rc = main(["eval", "--data", str(SAMPLES), "--out", str(out)])
        assert rc == 0
        assert out.exists()
        assert "准确率" in out.read_text(encoding="utf-8")
        assert "总体准确率" in capsys.readouterr().out

    def test_eval_missing_dataset_returns_2(self, tmp_path, capsys):
        assert main(["eval", "--data", str(tmp_path), "--out", str(tmp_path / "x.md")]) == 2
        assert "错误" in capsys.readouterr().err

    def test_eval_on_empty_manifest_returns_2(self, tmp_path, capsys):
        """空数据集必须报错，而不是产出"准确率 —"的报告让人以为跑通了。"""
        data = tmp_path / "empty"
        data.mkdir()
        (data / "manifest.json").write_text(json.dumps({"items": []}), encoding="utf-8")
        rc = main(["eval", "--data", str(data), "--out", str(tmp_path / "x.md")])
        assert rc == 2
        assert "没有可评测的样本" in capsys.readouterr().err

    def test_robust_writes_report_and_passes(self, tmp_path, capsys):
        out = tmp_path / "rb.md"
        rc = main(["robust", "--out", str(out)])
        assert rc == 0
        assert out.exists()
        text = out.read_text(encoding="utf-8")
        assert "鲁棒性" in text
        # 逐用例打印 OK/FAIL；7 个畸形日报用例应全部 OK
        printed = capsys.readouterr().out
        assert printed.count("OK") == 7, f"应有 7 个用例通过：{printed}"
        assert "FAIL" not in printed, f"不应有失败用例：{printed}"


# --------------------------------------------------------------------- 副作用
class TestNoSideEffects:
    """CLI 不应在**未指定 --out** 时改动仓库文件（CI 有一道断言依赖它）。"""

    def test_parse_without_out_does_not_write(self, sample_file, capsys):
        before = sorted(p.name for p in Path(".").iterdir())
        main(["parse", str(sample_file), "--summary", "--quiet"])
        capsys.readouterr()
        assert sorted(p.name for p in Path(".").iterdir()) == before

    def test_privacy_does_not_write(self, capsys):
        before = sorted(p.name for p in Path(".").iterdir())
        main(["privacy", "--data", str(SAMPLES)])
        capsys.readouterr()
        assert sorted(p.name for p in Path(".").iterdir()) == before

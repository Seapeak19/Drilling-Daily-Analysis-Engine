"""错误路径回归测试。

为什么单独测"报错"：真实使用中，用户给的往往是损坏文件、扫描件、旧格式、
路径写错。这些场景如果抛出裸崩溃栈，用户既看不懂也不知道怎么办；
如果静默返回空结果，更危险——会被当成"这份日报没问题"。

因此这里锁定的契约是：
1. 一律抛 `ReaderError` / `DatasetError` 等**可识别的异常类型**，不抛底层库异常；
2. 错误信息里必须包含**文件名**与**下一步动作**；
3. 绝不返回一个"看起来正常"的空文档。
"""

from __future__ import annotations

import json

import pytest

from ddr.datacheck import DatasetError, check_dataset_file, load_dataset
from ddr.evaluate import Report, evaluate_dataset, load_manifest, render_markdown
from ddr.reader import ReaderError, read_document
from ddr.stats import compute_stats, load_manifest as stats_load_manifest


# --------------------------------------------------------------------- 造边界文件
@pytest.fixture(scope="module")
def edge_dir(tmp_path_factory):
    import pymupdf

    d = tmp_path_factory.mktemp("edge")
    # 纯图片 PDF（模拟扫描件）
    doc = pymupdf.open()
    page = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40))
    pix.set_rect(pix.irect, (200, 200, 200))
    page.insert_image(pymupdf.Rect(72, 72, 400, 400), pixmap=pix)
    doc.save(str(d / "scanned.pdf"))
    doc.close()
    # 空 PDF（无文字也无图片）
    doc = pymupdf.open()
    doc.new_page()
    doc.save(str(d / "empty.pdf"))
    doc.close()
    # 损坏的 PDF
    (d / "broken.pdf").write_bytes(b"%PDF-1.4 this is not a real pdf")
    # 旧版 .xls
    (d / "old.xls").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32)
    # 不支持的格式
    (d / "report.txt").write_text("不是日报", encoding="utf-8")
    (d / "noext").write_text("x", encoding="utf-8")
    # 空目录
    (d / "emptydir").mkdir()
    # 坏 JSON
    (d / "bad.json").write_text("{ not json", encoding="utf-8")
    # 缺 items 键的 manifest
    (d / "nokeys").mkdir()
    (d / "nokeys" / "manifest.json").write_text(json.dumps({"note": "x"}), encoding="utf-8")
    # items 为空的 manifest
    (d / "emptyitems").mkdir()
    (d / "emptyitems" / "manifest.json").write_text(json.dumps({"items": []}), encoding="utf-8")
    # items 指向不存在的文件
    (d / "missingfile").mkdir()
    (d / "missingfile" / "manifest.json").write_text(
        json.dumps({"items": [{"file": "nope.pdf", "truth": "nope.json"}]}), encoding="utf-8"
    )
    return d


class TestReaderErrors:
    def test_missing_file(self, edge_dir):
        with pytest.raises(ReaderError) as ei:
            read_document(edge_dir / "不存在.pdf")
        assert "不存在.pdf" in str(ei.value)

    def test_scanned_pdf_says_ocr_needed(self, edge_dir):
        with pytest.raises(ReaderError) as ei:
            read_document(edge_dir / "scanned.pdf")
        msg = str(ei.value)
        assert "无文本层" in msg
        assert "OCR" in msg, "报错必须给出下一步动作"
        assert "scanned.pdf" in msg

    def test_empty_pdf_is_distinguished_from_scan(self, edge_dir):
        """空文档与扫描件要分开报——两者的处理办法完全不同。"""
        with pytest.raises(ReaderError) as ei:
            read_document(edge_dir / "empty.pdf")
        assert "空文档" in str(ei.value)
        assert "OCR" not in str(ei.value)

    def test_broken_pdf_is_wrapped(self, edge_dir):
        """损坏文件不能把底层库异常直接抛给用户。"""
        with pytest.raises(ReaderError) as ei:
            read_document(edge_dir / "broken.pdf")
        msg = str(ei.value)
        assert "损坏" in msg
        assert "broken.pdf" in msg
        # 必须是我们自己的异常类型，且应带原始异常做因果链
        assert isinstance(ei.value, ReaderError)
        assert ei.value.__cause__ is not None

    def test_legacy_xls_actionable(self, edge_dir):
        with pytest.raises(ReaderError) as ei:
            read_document(edge_dir / "old.xls")
        assert ".xlsx" in str(ei.value)

    def test_unsupported_suffix(self, edge_dir):
        with pytest.raises(ReaderError) as ei:
            read_document(edge_dir / "report.txt")
        assert "暂不支持" in str(ei.value)
        assert ".pdf" in str(ei.value), "报错要列出支持的格式"

    def test_no_suffix(self, edge_dir):
        with pytest.raises(ReaderError):
            read_document(edge_dir / "noext")


class TestDatasetErrors:
    def test_missing_file(self, edge_dir):
        with pytest.raises(DatasetError) as ei:
            load_dataset(edge_dir / "nope.json")
        assert "ddr.render" in str(ei.value), "报错要告诉用户怎么生成数据集"

    def test_bad_json(self, edge_dir):
        with pytest.raises(DatasetError) as ei:
            check_dataset_file(edge_dir / "bad.json")
        assert "合法 JSON" in str(ei.value)
        assert "行" in str(ei.value), "报错应给出行列位置"

    def test_wrong_structure(self, edge_dir):
        (edge_dir / "wrongstruct.json").write_text(json.dumps({"foo": 1}), encoding="utf-8")
        with pytest.raises(DatasetError) as ei:
            load_dataset(edge_dir / "wrongstruct.json")
        assert "samples" in str(ei.value)


class TestManifestErrors:
    def test_evaluate_missing_manifest(self, edge_dir):
        with pytest.raises(FileNotFoundError) as ei:
            evaluate_dataset(edge_dir / "emptydir")
        assert "manifest.json" in str(ei.value)
        assert "ddr.render" in str(ei.value)

    def test_stats_missing_manifest(self, edge_dir):
        with pytest.raises(FileNotFoundError):
            compute_stats(edge_dir / "emptydir")

    def test_missing_items_key(self, edge_dir):
        with pytest.raises(ValueError) as ei:
            load_manifest(edge_dir / "nokeys")
        assert "items" in str(ei.value)
        with pytest.raises(ValueError):
            stats_load_manifest(edge_dir / "nokeys")

    def test_empty_items_yields_empty_report(self, edge_dir):
        """空数据集要产出可读报告，而不是让格式化 None 崩掉。"""
        rep = evaluate_dataset(edge_dir / "emptyitems")
        assert rep.empty
        md = render_markdown(rep)
        assert "没有可评测的样本" in md

    def test_missing_sample_files_are_recorded_not_crashed(self, edge_dir):
        """manifest 登记的样本缺失时，逐条记录并跳过——不能让整次评测崩掉。"""
        rep = evaluate_dataset(edge_dir / "missingfile")
        assert rep.empty
        assert len(rep.missing) == 1
        assert "nope.pdf" in rep.missing[0]
        assert "没有可评测的样本" in render_markdown(rep)

    def test_stats_warns_on_missing_truth(self, edge_dir):
        st = compute_stats(edge_dir / "missingfile")
        assert st.sample_count == 1
        assert any("nope.pdf" in w for w in st.warnings)

    def test_empty_report_overall_is_none_not_crash(self):
        r = Report()
        assert r.overall()["accuracy"] is None
        assert "没有可评测的样本" in render_markdown(r)


class TestFontGuard:
    """字体缺失时必须显式失败，而不是产出满屏方块的中文 PDF。

    reportlab 找不到字体时会静默退回 Helvetica，结果是一份"看起来生成了、
    实则全是方块"的样本，还会污染评测集。这道守卫用于阻止该静默降级。
    """

    def test_usable_when_fonts_present(self):
        from ddr.render import _ensure_cjk_font

        _ensure_cjk_font()  # 本机有中文字体，应通过

    def test_raises_when_no_cjk_font(self, monkeypatch):
        from reportlab.pdfbase import pdfmetrics

        from ddr import fonts, render

        # 模拟"系统无中文字体"：清空候选 + 清缓存 + 从 reportlab 注册表移除已注册字体
        monkeypatch.setattr(fonts, "FONT_CANDIDATES", [])
        monkeypatch.setattr(fonts, "BOLD_CANDIDATES", [])
        fonts.register_cjk_fonts.cache_clear()
        for name in (fonts.REGULAR_NAME, fonts.BOLD_NAME):
            pdfmetrics._fonts.pop(name, None)
        try:
            assert fonts.has_cjk_font() is False
            with pytest.raises(render.FontUnavailableError) as ei:
                render._ensure_cjk_font()
            assert "中文字体" in str(ei.value)
            assert "fonts-noto-cjk" in str(ei.value), "报错要给出可执行的安装命令"
        finally:
            fonts.register_cjk_fonts.cache_clear()

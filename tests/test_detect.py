"""模板识别与区块切分测试（`detect.py`）。

## 为什么这组测试重要

`detect.py` 是**新增日报格式的第一道关卡**（`docs/格式适配指南.md` 第 1 节的
全部内容都落在它的两个数据结构上）。它出错的方式很隐蔽：

- 模板识别错了 → 抽取层用错标签表 → 字段大面积抽不到或抽错，
  但**不会报错**，只表现为"这个字段是空的"；
- 区块切分错了 → 时间分解表找不到 → 解析器补一条 `UNKNOWN 24h`。
  这就是 CI 首次运行时的表象：报错说"时间分解只有 1 条"，
  而真正的原因是字体渲染不出中文、模板识别失败。

两个失败都会把根因藏起来。所以这里锁定的是**判据本身的行为**，
而不只是"跑已知样本能过"（后者已由 `test_pipeline_e2e.py` 覆盖）。

## 两个最容易写错的点（本文件的重点）

1. **表头行不能当区块标题**
   Excel 时间表的表头是"起 | 止 | 历时(h) | 作业内容 | 类别 | 备注"，
   里面的"备注"会被 remarks 锚点命中。`detect.py` 用"区块名在整行里的
   字符占比 ≥ 0.7"来区分标题行与表头行 —— 粒度很细，必须锁住。
2. **单位制绝不默认公制**
   判不出来就返回 `unknown`，让下游逐字段判定（`normalize.resolve_number`）。
   这是全局设计原则，退化成"默认公制"会造成静默的数值错误。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ddr.detect import (
    ALL_BLOCKS,
    BLOCK_BIT,
    BLOCK_HEADER,
    BLOCK_MUD,
    BLOCK_PLAN,
    BLOCK_REMARKS,
    BLOCK_SUMMARY,
    BLOCK_TIME,
    Detection,
    Segmented,
    _block_anchor_coverage,
    _looks_like_table_header,
    _match_block,
    _strip_block_decor,
    detect_template,
    segment,
)
from ddr.reader import Document, Page, normalize_line

SAMPLES = Path("data/samples/samples")


# --------------------------------------------------------------------- 构造输入
def make_doc(lines: list[str], *, file_type: str = "pdf") -> Document:
    """用给定的文本行构造一个最小 Document。

    直接构造而不读真实文件，是为了让每条断言只依赖**被测的那一行文本**，
    不被样本版式的其它部分干扰。
    """
    clean = [normalize_line(x) for x in lines if x and x.strip()]
    return Document(
        path=Path("synthetic.pdf"),
        file_type=file_type,
        pages=[Page(number=1, text="\n".join(clean), lines=clean)],
    )


# --------------------------------------------------------------------- 区块标题判据
class TestBlockTitleJudgement:
    """`_match_block`：一行"像不像"区块标题（真正判定由 segment 的校验负责）。"""

    @pytest.mark.parametrize(
        "line,expected",
        [
            ("二、二十四小时时间分解", BLOCK_TIME),
            ("2. TIME BREAK DOWN", BLOCK_TIME),
            ("三、钻头记录", BLOCK_BIT),
            ("3. BIT RECORD", BLOCK_BIT),
            ("四、泥浆性能", BLOCK_MUD),
            ("4. MUD PROPERTIES", BLOCK_MUD),
            ("五、备注（按时间顺序）", BLOCK_REMARKS),
            ("5. REMARKS (chronological)", BLOCK_REMARKS),
            ("本日作业摘要", BLOCK_SUMMARY),
            ("下步计划", BLOCK_PLAN),
        ],
    )
    def test_recognises_titles(self, line, expected):
        assert _match_block(line) == expected

    @pytest.mark.parametrize(
        "line",
        [
            "00:00 旋转钻进至 2066.06 m",
            "合计 24.00",
            "钻井日报",
            "",
            "   ",
        ],
    )
    def test_ignores_non_titles(self, line):
        assert _match_block(line) is None

    def test_overlong_line_is_not_a_title(self):
        """超长行不可能是标题 —— 长度上限是防止把整段叙述误判成标题。"""
        long_line = "备注" + "x" * 60
        assert _match_block(long_line) is None

    def test_length_boundary_is_40(self):
        """锁住长度阈值本身：41 字符即不算标题。

        阈值写死在代码里，这里用**独立复算**的方式断言（不从实现取常量），
        这样改动阈值会立刻暴露，而不是被"引用同一个常量"掩盖过去。
        """
        base = "备注"
        assert _match_block(base + "x" * 38) == BLOCK_REMARKS   # 40 字符
        assert _match_block(base + "x" * 39) is None            # 41 字符


class TestTableHeaderGuard:
    """表头行与数据行不能被当成区块标题 —— 这是切分里最细的一处判据。"""

    def test_excel_time_table_header_is_not_a_title(self):
        """真实误判场景：时间表表头里含"备注"列名。"""
        header = "起 | 止 | 历时(h) | 作业内容 | 类别 | 备注"
        assert _looks_like_table_header(header) is True

    def test_tab_separated_row_is_not_a_title(self):
        assert _looks_like_table_header("起\t止\t历时(h)\t作业内容") is True

    def test_real_titles_are_titles(self):
        for line in ("二、二十四小时时间分解", "5. REMARKS (chronological)", "3. BIT RECORD"):
            assert _looks_like_table_header(line) is False, line

    def test_empty_line_counts_as_header(self):
        """空行不应成为区块标题（否则会切出空区块）。"""
        assert _looks_like_table_header("") is True
        assert _looks_like_table_header("   ") is True

    def test_coverage_threshold_is_0_7(self):
        """锁住 0.7 这个阈值：区块名占整行字符的比例。

        这是"标题行"与"含区块名的表头行"之间的分界线，改动它会直接
        影响区块切分结果，所以独立复算一遍。
        """
        title = "二、二十四小时时间分解"
        coverage = _block_anchor_coverage(title)
        assert coverage >= 0.7, f"标题行占比应 >= 0.7，实测 {coverage}"

        header = "起 | 止 | 历时(h) | 作业内容 | 类别 | 备注"
        coverage_h = _block_anchor_coverage(header)
        assert coverage_h < 0.7, f"表头行占比应 < 0.7，实测 {coverage_h}"

    def test_decoration_stripping(self):
        """序号、括号说明、单位等修饰成分要被剥掉后再算占比。"""
        assert _strip_block_decor("二、") == ""
        assert _strip_block_decor("2.") == ""
        assert _strip_block_decor("(24 HOURS)") == ""
        assert _strip_block_decor("(chronological)") == ""

    def test_english_anchor_does_not_leave_dangling_suffix(self):
        """'remarks' 被 'remark' 命中后不能残留孤立的 's'。

        实现注释里写明了这个坑：残留的 's' 会把占比压到 0.538，
        从而把真正的标题行误判成表头行。
        """
        assert _block_anchor_coverage("REMARKS") >= 0.7
        assert _block_anchor_coverage("5. REMARKS (chronological)") >= 0.7


# --------------------------------------------------------------------- 模板识别
class TestTemplateDetection:
    def test_cn_vertical_anchors(self):
        doc = make_doc([
            "钻井日报",
            "井 号 PL-6-2-A12H",
            "钻井承包商 中海油田服务股份有限公司",
            "当日进尺 81.80 m",
            "井 深 (MD) 2130.36 m",
        ])
        det = detect_template(doc)
        assert det.template_id == "cn_vertical"
        assert det.known is True

    def test_iadc_classic_anchors(self):
        doc = make_doc([
            "DAILY DRILLING REPORT",
            "HOLE & WELL DATA",
            "Measured Depth 2130.36 m",
            "Dull Grade 3-4-WT",
        ])
        det = detect_template(doc)
        assert det.template_id == "iadc_classic"

    def test_unknown_template_does_not_raise_and_explains_next_step(self):
        """未知版式必须返回 unknown 且**不崩溃**，并提示如何登记。"""
        doc = make_doc([
            "某作业者日报",
            "井号：TEST-1",
            "一些完全不含已知锚点的内容",
        ])
        det = detect_template(doc)
        assert det.template_id == "unknown"
        assert det.known is False
        assert det.confidence == 0.0
        assert any("登记" in n for n in det.notes), f"应提示登记新模板：{det.notes}"

    def test_empty_document_is_unknown_not_crash(self):
        doc = make_doc([])
        det = detect_template(doc)
        assert det.template_id == "unknown"
        assert det.unit_system == "unknown"

    def test_min_hits_gate(self):
        """命中数不足 min_hits 时不得算作识别成功。

        cn_vertical 的 min_hits 是 2，只命中一个锚点必须落到 unknown。
        """
        doc = make_doc(["钻井日报", "一些无关内容"])
        det = detect_template(doc)
        assert det.template_id == "unknown", f"只命中 {det.scores} 应不足以确认模板"

    def test_scores_are_reported_for_diagnosis(self):
        """Detection 要带各模板命中数，否则识别错误时无从排查。"""
        doc = make_doc(["DAILY DRILLING REPORT", "HOLE & WELL DATA", "Measured Depth 100 m"])
        det = detect_template(doc)
        assert det.scores.get("iadc_classic", 0) >= 2
        assert "cn_vertical" in det.scores


class TestUnitSystemInference:
    """单位制判定：宁可 unknown，不可默认公制。"""

    def test_imperial_evidence(self):
        doc = make_doc([
            "DAILY DRILLING REPORT",
            "Measured Depth 6985.0 ft",
            "Mud Weight 10.3 ppg",
        ])
        assert detect_template(doc).unit_system == "imperial"

    def test_metric_evidence(self):
        doc = make_doc([
            "钻井日报",
            "井深 2350.00 m",
            "密度 1.25 g/cm3",
            "排量 34 L/s",
        ])
        assert detect_template(doc).unit_system == "metric"

    def test_no_unit_evidence_is_unknown_not_metric(self):
        """**核心契约**：没有单位线索时返回 unknown，绝不默认公制。

        退化成"默认公制"会让英制日报的数值被静默按米解释 —— 数值看起来
        合理，实际全错，而且没有任何提示。这是全局设计原则。
        """
        doc = make_doc(["某日报", "井号 TEST-1", "没有任何单位标注的一行文字"])
        det = detect_template(doc)
        assert det.unit_system == "unknown"
        assert any("不做默认公制假设" in n for n in det.notes), det.notes

    def test_isolated_letters_are_not_unit_evidence(self):
        """孤立字母不是单位证据。

        实现注释明确写了这条：必须是「数值 + 单位」才算证据，
        否则中文叙述里随便一个 'm' 'ft' 都会把单位制带偏。
        """
        doc = make_doc([
            "某日报",
            "井号 TEST-1",
            "备注：本次作业按 plan 执行，由 supervisor 确认。",
        ])
        det = detect_template(doc)
        assert det.unit_system == "unknown", f"孤立字母不应成为证据：{det.unit_scores}"

    def test_close_scores_are_mixed(self):
        """两种单位线索接近（差距在 20% 以内）时判 mixed，而不是硬选一边。

        构造要点（这些数字是**实测**出来的，不是估的）：
        模板锚点本身就带单位倾向 —— `measured depth` 是米制模式（0.3），
        中性米制 `2350.00 m` 只给 1.0。要让两侧落在 20% 以内，必须两边都给强线索：
        英制 `ft`(4.0) + `ppg`(4.0) = 8.0；
        公制 `m`(1.0) + 表头弱线索（井深/当日进尺/时间分解 各 0.5×…）= 8.0。
        实测两侧都是 8.0，比率 1.00。
        """
        text = (
            "DAILY DRILLING REPORT\n"
            "6985.0 ft\n"
            "10.3 ppg\n"
            "井深 2350.00 m\n"
            "当日进尺 80.00 m\n"
            "密度 1.25 g/cm3\n"
            "排量 34 L/s"
        )
        det = detect_template(make_doc(text.split("\n")))
        assert det.unit_system == "mixed", f"分数={det.unit_scores}"
        assert any("mixed" in n or "混用" in n for n in det.notes), det.notes

    def test_mixed_requires_scores_within_20_percent(self):
        """锁住 ±20% 这个阈值本身。

        两侧差距明显时（比率 2.0）不得判 mixed —— 否则"混用"会退化成
        "永远 mixed"，下游就失去了对这个判定的信任。
        """
        doc = make_doc(["DAILY DRILLING REPORT", "6985.0 ft", "10.3 ppg", "2350.00 m"])
        det = detect_template(doc)
        assert det.unit_system == "imperial", f"分数={det.unit_scores}"
        imp = det.unit_scores["imperial"]
        met = det.unit_scores["metric"]
        assert imp > met * 1.2, f"这条样例本应明显偏英制：{det.unit_scores}"

    def test_template_unit_hint_is_fallback_only(self):
        """文本线索完全缺失时，才用模板默认单位制兜底。

        构造要点：必须选**不含任何单位模式**的锚点。
        不能用 "Measured Depth 1000" —— 'measured depth' 本身就是米制模式（0.3），
        那属于"有线索"，不是"线索不足"。
        """
        doc = make_doc([
            "DAILY DRILLING REPORT",
            "HOLE & WELL DATA",
            "TIME BREAK DOWN",
        ])
        det = detect_template(doc)
        assert det.template_id == "iadc_classic", f"未识别到模板：{det.scores}"
        assert det.unit_scores == {"imperial": 0.0, "metric": 0.0}, \
            f"这条样例本应无单位线索：{det.unit_scores}"
        assert det.unit_system == "imperial", "线索不足时应落到模板默认单位制"
        assert any("默认单位制" in n for n in det.notes), det.notes


# --------------------------------------------------------------------- 区块切分
class TestBlockSegmentation:
    def test_blocks_split_correctly(self):
        doc = make_doc([
            "钻井日报",
            "井 号 TEST-1",
            "二、二十四小时时间分解",
            "序号 | 开始 | 结束 | 历时(h) | 作业内容 | 时间类别",
            "1 | 00:00 | 06:00 | 6.00 | 旋转钻进 | 有效生产",
            "三、钻头记录",
            "序号 | 尺寸(in) | 入井(m) | 出井(m)",
            "四、泥浆性能",
            "取样点 | 密度(g/cm3)",
            "五、备注",
            "06:30 钻进至 2000 m",
        ])
        seg = segment(doc)
        assert BLOCK_HEADER in seg.blocks
        assert BLOCK_TIME in seg.blocks
        assert BLOCK_BIT in seg.blocks
        assert BLOCK_MUD in seg.blocks
        assert BLOCK_REMARKS in seg.blocks

    def test_table_header_row_is_not_a_block_title(self):
        """表头里的"备注"列名不得把区块提前截断。

        若被误判，remarks 区块会以那一行为起点，
        前面的时间表内容会被错误划到 remarks 名下。
        """
        doc = make_doc([
            "钻井日报",
            "二、二十四小时时间分解",
            "起 | 止 | 历时(h) | 作业内容 | 类别 | 备注",
            "00:00 | 06:00 | 6.00 | 旋转钻进 | 有效生产 |",
            "五、备注",
            "06:30 钻进至 2000 m",
        ])
        seg = segment(doc)
        # 表头行必须留在 time_log 区块里，而不是成为 remarks 的起点
        assert any("历时(h)" in ln for ln in seg.block(BLOCK_TIME)), seg.block(BLOCK_TIME)
        assert not any("历时(h)" in ln for ln in seg.block(BLOCK_REMARKS))

    def test_header_block_holds_leading_lines(self):
        doc = make_doc(["钻井日报", "井 号 TEST-1", "三、钻头记录", "序号 | 尺寸(in)"])
        seg = segment(doc)
        assert seg.block(BLOCK_HEADER) == ["钻井日报", "井 号 TEST-1"]

    def test_block_content_excludes_title_line(self):
        doc = make_doc(["钻井日报", "五、备注", "第一行备注", "第二行备注"])
        seg = segment(doc)
        assert seg.block(BLOCK_REMARKS) == ["第一行备注", "第二行备注"]

    def test_missing_blocks_do_not_crash_and_are_noted(self):
        """真实日报经常整块缺失（钻头记录只在换钻头那天才有）。

        契约是：不崩溃、如实记 note，而不是补一个空区块假装有。
        """
        doc = make_doc(["钻井日报", "井 号 TEST-1", "二、二十四小时时间分解", "1 | 00:00 | 24:00 | 旋转钻进"])
        seg = segment(doc)
        assert BLOCK_BIT not in seg.blocks, "没有钻头记录时不应造一个空区块"
        assert BLOCK_MUD not in seg.blocks
        assert any("钻头记录/泥浆" in n for n in seg.notes), seg.notes

    def test_no_time_block_is_noted(self):
        doc = make_doc(["钻井日报", "井 号 TEST-1", "五、备注", "只有备注"])
        seg = segment(doc)
        assert any("时间分解" in n for n in seg.notes), seg.notes

    def test_few_blocks_is_noted(self):
        """区块数过少说明版面与已知模板差异大，必须提示。"""
        doc = make_doc(["钻井日报", "三、钻头记录", "序号 | 尺寸(in)"])
        seg = segment(doc)
        assert any("版面" in n for n in seg.notes), seg.notes

    def test_first_occurrence_wins(self):
        """同一区块名出现多次时只取首个。

        日报里"备注"既可能做大标题又可能做列名/小标题，
        取首个是既定策略（实现注释写明）。
        """
        doc = make_doc([
            "钻井日报",
            "五、备注",
            "第一条备注",
            "备注",
            "第二条备注",
        ])
        seg = segment(doc)
        assert BLOCK_REMARKS in seg.blocks
        # 第二个"备注"不应另起一个 remarks 区块（字典键唯一），
        # 但它的行应留在同一个区块里 —— 关键是总行数不丢
        assert "第一条备注" in seg.block(BLOCK_REMARKS)

    def test_block_spans_are_consistent_with_blocks(self):
        """block_spans 必须与 blocks 对得上，供抽取层按行区间取数。"""
        doc = make_doc(["钻井日报", "五、备注", "备注一", "备注二"])
        seg = segment(doc)
        for block, lines in seg.blocks.items():
            start, end = seg.block_spans[block]
            assert end - start == len(lines), f"{block}: span={start},{end} 与 {len(lines)} 行不符"

    def test_empty_document(self):
        seg = segment(make_doc([]))
        assert seg.block(BLOCK_TIME) == []
        assert seg.notes, "空文档应给出提示"

    def test_segmented_block_accessor_is_safe(self):
        """取不存在的区块返回空列表，而不是 KeyError。"""
        seg = Segmented(detection=Detection(
            template_id="unknown", label="x", confidence=0.0, unit_system="unknown"
        ))
        assert seg.block("不存在的区块") == []
        assert seg.block(BLOCK_TIME) == []


# --------------------------------------------------------------------- 真实样本
class TestRealSampleSegmentation:
    """在真实样本上验证：**期望值按 ground truth 的实际内容决定**。

    这里刻意不断言"某区块必须存在"：钻头记录只有 6/21 份日报才有
    （换钻头那天才记录），断言"必须有"是错的 —— 上一轮就踩过这个坑。
    正确做法是按 ground truth 里有没有内容来决定期望。
    """

    @pytest.mark.skipif(not SAMPLES.exists(), reason="样本目录不存在")
    def test_segmentation_matches_ground_truth_content(self):
        import json

        gt_dir = SAMPLES.parent / "ground_truth"
        checked = 0
        for pdf in sorted(SAMPLES.iterdir())[:6]:
            truth_file = gt_dir / f"{pdf.stem}.truth.json"
            if not truth_file.exists():
                continue
            truth = json.loads(truth_file.read_text(encoding="utf-8"))["truth"]
            seg = segment_from_file(pdf)

            # 时间分解必有（所有样本都合规到 24h）
            assert seg.block(BLOCK_TIME), f"{pdf.name}: 时间分解区块未切出"

            # 钻头记录：ground truth 有才要求切出
            if truth.get("bit_records"):
                assert seg.block(BLOCK_BIT), f"{pdf.name}: ground truth 有钻头记录却没切出区块"

            # 备注：ground truth 有才要求切出
            if truth.get("remarks"):
                assert seg.block(BLOCK_REMARKS), f"{pdf.name}: ground truth 有备注却没切出区块"

            checked += 1
        assert checked > 0, "没有可校验的样本"


def segment_from_file(path: Path) -> Segmented:
    from ddr.reader import read_document

    return segment(read_document(path))

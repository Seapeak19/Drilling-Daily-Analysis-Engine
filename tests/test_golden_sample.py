"""黄金样本测试：打破「模拟器 → 渲染器 → 解析器」的闭环自洽。

## 为什么必须单独有这一组

仓库主评测集（`ddr eval`）的 ground truth 与日报 PDF **同源于 `simulate.py`**：

    simulate.py ──┬──▶ dataset.json（ground truth）
                  └──▶ render.py ──▶ 日报 PDF ──▶ parser ──▶ 与 ground truth 比对

这条链证明的是「解析器能从**自己这套模板**里把值读回来」，而不是
「解析器读到的就是日报上**印着的**值」。如果渲染环节把 2350.0 印成了 2530.0，
而 ground truth 仍来自模拟器，那么这种偏差**可能被判定为正确**，
`ddr check` 也发现不了 —— 它比对的是 ground truth 与 PDF 的**语法一致性**，
不是"印出来的数字对不对"。

## 本文件的做法

手工写一份日报数据，**期望值全部是字面量**（`EXPECT_*` 常量，不经任何代码计算），
然后 渲染 → 解析 → 断言解析结果等于字面量。这样：

- 解析器与期望值**没有共同来源**，闭环被打断；
- 渲染器一旦写错数字/错列/错单位，测试必然失败（这正是主评测集测不到的那一段）；
- 数值刻意选成**非整数**（2066.06 而不是 2100），防止"四舍五入恰好撞对"。

> **维护纪律**：修改本文件时，`EXPECT_*` 常量只能按"日报上应该印什么"来定，
> **绝不能**从解析结果或模拟器反推着填 —— 那样就把闭环又接回去了，
> 这组测试也就失去了唯一价值。
"""

from __future__ import annotations

import pytest

from ddr.model import validate_payload
from ddr.pipeline import parse_file
from ddr.render import render_cn_vertical

# --------------------------------------------------------------------- 期望值（字面量）
# 井信息
EXPECT_WELL_NAME = "SYN-7-3-A9H"
EXPECT_FIELD = "XX 构造"
EXPECT_OPERATOR = "某合成作业者"
EXPECT_CONTRACTOR = "某合成钻井承包商"
EXPECT_RIG = "SYN-000"

# 表头数值（刻意非整数）
EXPECT_REPORT_NO = 7
EXPECT_MD_M = 2066.06
EXPECT_TVD_M = 1888.88
EXPECT_PROGRESS_M = 88.8
EXPECT_HOLE_DIAMETER_IN = 17.5
# 钻井天数与开钻日期：2026-02-22 开钻 → 2026-03-05 为第 11 天
EXPECT_ETIM_SPUD_DAYS = 11.3
EXPECT_DTIM_SPUD = "2026-02-22"

# 时间分解：必须加总 24.00；类别刻意覆盖 productive / flat / npt 三类
TIME_LOG = [
    ("00:00", "04:00", 4.0, "DRILL", "productive", "旋转钻进"),
    ("04:00", "06:30", 2.5, "REAM", "productive", "扩眼划眼"),
    ("06:30", "08:00", 1.5, "TRIP_OUT", "flat", "起钻"),
    ("08:00", "10:30", 2.5, "REPAIR", "npt", "顶驱 VFD 故障，配件在途"),
    ("10:30", "12:00", 1.5, "CASING", "flat", "下套管"),
    ("12:00", "16:00", 4.0, "WELL_CONTROL", "npt", "井涌，压井作业"),
    ("16:00", "18:00", 2.0, "CEMENT", "flat", "固井"),
    ("18:00", "24:00", 6.0, "DRILL", "productive", "旋转钻进"),
]
EXPECT_SUM_HOURS = 24.0
EXPECT_PRODUCTIVE_H = 12.5   # 4.0 + 2.5 + 6.0
EXPECT_FLAT_H = 5.0          # 1.5 + 1.5 + 2.0
EXPECT_NPT_H = 6.5           # 2.5 + 4.0
EXPECT_TIME_ENTRIES = 8

# 以下三项由上面推出，必须保持自洽 —— 这是真实日报的内在关系，
# 也是 validate.py 会校验的约束（钻进时间 = DRILL+REAM；钻速 ≈ 进尺÷钻进时间）：
#   表头"钻进时间" = 时间分解里 DRILL + REAM 的合计 = 4.0 + 2.5 + 6.0
EXPECT_ETIM_DRILL_H = 12.5
#   平均钻速 = 当日进尺 ÷ 钻进时间 = 88.8 / 12.5
EXPECT_ROP_M_PER_H = 7.1

# 钻头记录：进尺必须等于 出井 − 入井（2066.06 − 1634.36 = 431.70）
EXPECT_BIT_DEPTH_IN_M = 1634.36
EXPECT_BIT_FOOTAGE_M = 431.7
EXPECT_BIT_DEPTH_OUT_M = 2066.06
EXPECT_BIT_DULL_GRADE = "3-4-WT-S-X-I-NO-TD"
#   钻头钻速是**该钻头自己的**机械钻速，不等于全井平均钻速：
#   进尺 ÷ 钻头使用时间 = 431.7 / 41.8 = 10.33
EXPECT_BIT_ROP_M_PER_H = 10.33

# 泥浆（公制模板，原样输出，无换算）
EXPECT_MUD_DENSITY_FLOWLINE = 1.23
EXPECT_MUD_DENSITY_SUCTION = 1.21
EXPECT_MUD_VISCOSITY = 47.0
EXPECT_MUD_FL_ML = 4.6


def build_payload() -> dict:
    """构造一份手工日报数据。

    这里的数值全部来自上面的字面量常量，**不引用 simulate.py** ——
    这正是本测试与主评测集的根本区别。
    """
    return {
        "schema_version": "1.0.0",
        "well": {
            "well_name": EXPECT_WELL_NAME,
            "field": EXPECT_FIELD,
            "operator": EXPECT_OPERATOR,
            "contractor": EXPECT_CONTRACTOR,
            "rig": EXPECT_RIG,
        },
        "report": {
            "report_no": EXPECT_REPORT_NO,
            "dtim_start": "2026-03-05T00:00:00",
            "dtim_end": "2026-03-06T00:00:00",
            "md_m": EXPECT_MD_M,
            "tvd_m": EXPECT_TVD_M,
            "progress_m": EXPECT_PROGRESS_M,
            "hole_diameter_in": EXPECT_HOLE_DIAMETER_IN,
            "rop_av_m_per_h": EXPECT_ROP_M_PER_H,
            "etim_drill_h": EXPECT_ETIM_DRILL_H,
            "etim_spud_days": EXPECT_ETIM_SPUD_DAYS,
            "dtim_spud": EXPECT_DTIM_SPUD,
            "well_status": "钻进",
            "unit_system": "metric",
            "sum_24hr": "旋转钻进至 2066.06 m。",
            "plan_24hr": "继续钻进。",
        },
        "time_log": [
            {
                "start": s,
                "end": e,
                "hours": h,
                "code": code,
                "category": cat,
                "is_npt": cat == "npt",
                "operation": op,
            }
            for s, e, h, code, cat, op in TIME_LOG
        ],
        "bit_records": [
            {
                "bit_no": 3,
                "size_in": 12.25,
                "make": "SYN",
                "model": "SM-1",
                "depth_in_m": EXPECT_BIT_DEPTH_IN_M,
                "depth_out_m": EXPECT_BIT_DEPTH_OUT_M,
                "footage_m": EXPECT_BIT_FOOTAGE_M,
                "hours": 41.8,
                "rop_m_per_h": EXPECT_BIT_ROP_M_PER_H,
                "dull_grade": EXPECT_BIT_DULL_GRADE,
            }
        ],
        "mud": [
            {
                "sample_point": "flowline",
                "depth_m": 2066.1,
                "density_gcc": EXPECT_MUD_DENSITY_FLOWLINE,
                "funnel_viscosity_s": EXPECT_MUD_VISCOSITY,
                "fl_ml": EXPECT_MUD_FL_ML,
                "ph": 8.4,
            },
            {
                "sample_point": "suction",
                "depth_m": 2066.1,
                "density_gcc": EXPECT_MUD_DENSITY_SUCTION,
                "funnel_viscosity_s": 52.0,
                "fl_ml": EXPECT_MUD_FL_ML,
                "ph": 8.6,
            },
        ],
        "remarks": [],
    }


@pytest.fixture(scope="module")
def golden_parsed(tmp_path_factory):
    """渲染黄金样本日报并解析一次，供本模块所有断言复用。"""
    work = tmp_path_factory.mktemp("golden")
    pdf = work / "golden_cn_vertical.pdf"
    render_cn_vertical(build_payload(), pdf)
    return parse_file(pdf)


# --------------------------------------------------------------------- 模板识别
class TestGoldenTemplateDetection:
    def test_template_recognised(self, golden_parsed):
        """渲染出来的版式必须能被识别成 cn_vertical。

        若这条失败，后面所有字段断言都失去意义（可能整份都没抽到）。
        """
        assert golden_parsed.detection.template_id == "cn_vertical"

    def test_no_severe_warnings(self, golden_parsed):
        """不应出现"退化为文本行解析"这类降级警告。"""
        joined = " ".join(golden_parsed.warnings)
        assert "退化" not in joined, f"出现降级解析：{golden_parsed.warnings}"


# --------------------------------------------------------------------- 表头
class TestGoldenHeader:
    """逐字段断言表头。数值必须是日报上印出来的原值，不能是"接近的整数"。"""

    @pytest.mark.parametrize(
        "field,expected",
        [
            ("well_name", EXPECT_WELL_NAME),
            ("field", EXPECT_FIELD),
            ("operator", EXPECT_OPERATOR),
            ("contractor", EXPECT_CONTRACTOR),
            ("rig", EXPECT_RIG),
        ],
    )
    def test_text_fields(self, golden_parsed, field, expected):
        got = getattr(golden_parsed.report.well, field)
        assert got == expected, f"{field}: 得到 {got!r}，日报上印的是 {expected!r}"

    def test_report_no(self, golden_parsed):
        assert golden_parsed.report.report.report_no == EXPECT_REPORT_NO

    @pytest.mark.parametrize(
        "attr,expected",
        [
            ("md_m", EXPECT_MD_M),
            ("tvd_m", EXPECT_TVD_M),
            ("progress_m", EXPECT_PROGRESS_M),
            ("hole_diameter_in", EXPECT_HOLE_DIAMETER_IN),
            ("rop_av_m_per_h", EXPECT_ROP_M_PER_H),
            ("etim_drill_h", EXPECT_ETIM_DRILL_H),
            ("etim_spud_days", EXPECT_ETIM_SPUD_DAYS),
        ],
    )
    def test_numeric_fields_exact(self, golden_parsed, attr, expected):
        """数值字段必须**精确**相等。

        不是"约等于"：如果渲染或解析发生列错位、单位误判，
        结果往往会变成另一个量级，用容差断言会掩盖问题。
        """
        got = getattr(golden_parsed.report.report, attr)
        assert got == pytest.approx(expected, abs=0.01), \
            f"{attr}: 得到 {got}，日报上印的是 {expected}"

    def test_dates(self, golden_parsed):
        r = golden_parsed.report.report
        assert r.dtim_start.startswith("2026-03-05"), r.dtim_start
        assert r.dtim_end.startswith("2026-03-06"), r.dtim_end
        # 注意：cn_vertical 版式**不输出**开钻日期（只有作业日期），
        # 所以这里不断言 dtim_spud —— 断言它等于"日报上印着的值"是错的，
        # 因为日报上根本没印。这条注释是刻意留的：黄金样本只断言真实印出来的内容。

    def test_no_validation_issues(self, golden_parsed):
        """自洽的日报不应产生任何数值合理性告警。

        这一条专门锁"内部一致性"：手写样本最容易出现
        「表头钻进时间 ≠ 时间分解里 DRILL+REAM 合计」或
        「平均钻速 ≠ 进尺÷钻进时间」这类矛盾 —— 而真实日报里这两条
        恰恰是核对日报质量的主要抓手，所以样本必须自洽。
        """
        msgs = golden_parsed.validation.messages
        assert not msgs, f"自洽日报不应有告警，实际：{msgs}"


# --------------------------------------------------------------------- 时间分解
class TestGoldenTimeLog:
    def test_entry_count(self, golden_parsed):
        assert len(golden_parsed.report.time_log) == EXPECT_TIME_ENTRIES

    def test_sum_is_24h_and_valid(self, golden_parsed):
        tv = golden_parsed.report.time_verification
        assert tv.sum_hours == pytest.approx(EXPECT_SUM_HOURS, abs=0.01)
        assert tv.valid is True
        assert tv.unaccounted_hours in (None, 0.0)

    @pytest.mark.parametrize(
        "index,start,end,hours,code,category,operation",
        [(i, *row) for i, row in enumerate(TIME_LOG)],
    )
    def test_each_entry(self, golden_parsed, index, start, end, hours, code, category, operation):
        """逐条核对：钟点、历时、操作码、类别、作业内容。

        合计行（"必须等于 24.00 小时"那行）若被误当作数据行，条目数会变 9，
        这里逐条比对能立刻暴露。
        """
        e = golden_parsed.report.time_log[index]
        assert e.start == start, f"第{index + 1}条 start"
        assert e.end == end, f"第{index + 1}条 end"
        assert e.hours == pytest.approx(hours, abs=0.01), f"第{index + 1}条 hours"
        assert e.code == code, f"第{index + 1}条 code：得到 {e.code}"
        assert e.category == category, f"第{index + 1}条 category：得到 {e.category}"
        assert e.operation and operation in e.operation, f"第{index + 1}条 operation"

    def test_category_totals(self, golden_parsed):
        """三类时间的小计必须与手工计算一致（防类别错分）。"""
        tl = golden_parsed.report.time_log
        prod = sum(e.hours for e in tl if e.category == "productive")
        flat = sum(e.hours for e in tl if e.category == "flat")
        npt = sum(e.hours for e in tl if e.category == "npt")
        assert prod == pytest.approx(EXPECT_PRODUCTIVE_H, abs=0.01), f"有效生产 {prod}"
        assert flat == pytest.approx(EXPECT_FLAT_H, abs=0.01), f"Flat Time {flat}"
        assert npt == pytest.approx(EXPECT_NPT_H, abs=0.01), f"NPT {npt}"

    def test_npt_flags(self, golden_parsed):
        """is_npt 必须与类别一致 —— 它直接驱动 NPT 率，错了会误导成本对账。"""
        for e in golden_parsed.report.time_log:
            assert e.is_npt == (e.category == "npt"), \
                f"{e.operation}: is_npt={e.is_npt} 但 category={e.category}"


# --------------------------------------------------------------------- 钻头 / 泥浆
class TestGoldenBitAndMud:
    def test_bit_record(self, golden_parsed):
        bits = golden_parsed.report.bit_records
        assert len(bits) == 1
        b = bits[0]
        assert b.depth_out_m == pytest.approx(EXPECT_BIT_DEPTH_OUT_M, abs=0.01)
        assert b.footage_m == pytest.approx(EXPECT_BIT_FOOTAGE_M, abs=0.01)
        assert b.dull_grade == EXPECT_BIT_DULL_GRADE

    def test_mud_flowline(self, golden_parsed):
        mud = golden_parsed.report.mud
        flow = next((m for m in mud if m.sample_point == "flowline"), None)
        assert flow is not None, f"未取到出口泥浆：{[m.sample_point for m in mud]}"
        assert flow.density_gcc == pytest.approx(EXPECT_MUD_DENSITY_FLOWLINE, abs=0.005)
        assert flow.funnel_viscosity_s == pytest.approx(EXPECT_MUD_VISCOSITY, abs=0.1)
        assert flow.fl_ml == pytest.approx(EXPECT_MUD_FL_ML, abs=0.05)

    def test_mud_suction_is_distinct(self, golden_parsed):
        """入口/出口两个取样点不能被合并成一条 —— 否则密度对比失去意义。"""
        mud = golden_parsed.report.mud
        suction = next((m for m in mud if m.sample_point == "suction"), None)
        assert suction is not None
        assert suction.density_gcc == pytest.approx(EXPECT_MUD_DENSITY_SUCTION, abs=0.005)
        assert suction.density_gcc != pytest.approx(EXPECT_MUD_DENSITY_FLOWLINE, abs=0.005)


# --------------------------------------------------------------------- Schema
class TestGoldenSchema:
    def test_payload_passes_schema(self, golden_parsed):
        """解析结果必须通过 JSON Schema 校验（数据模型自洽）。"""
        errors = validate_payload(golden_parsed.report.to_dict())
        assert not errors, f"Schema 校验失败：{errors[:5]}"


# --------------------------------------------------------------------- 反向哨兵
class TestGoldenIsNotVacuous:
    """防止"测试其实什么都没测"。

    如果解析器返回空对象，上面很多断言会因为 None != 期望值 而失败——
    但也可能有人为了让测试变绿而放宽断言。这里显式锁住"确实取到了东西"。
    """

    def test_actually_parsed_content(self, golden_parsed):
        d = golden_parsed.report.to_dict()
        assert d["well"]["well_name"], "井名为空说明根本没解析出内容"
        assert d["report"]["md_m"] > 0
        assert len(d["time_log"]) == EXPECT_TIME_ENTRIES
        assert d["time_verification"]["sum_hours"] > 0

    def test_renderer_wrote_expected_text(self, tmp_path):
        """直接检查渲染出的 PDF 文本层里确实印着期望值。

        这一条是整组测试的支点：它证明"期望值确实被印到了日报上"，
        从而让上面的解析断言成为对**渲染与解析两侧**的共同约束。
        """
        import pymupdf

        pdf = tmp_path / "probe.pdf"
        render_cn_vertical(build_payload(), pdf)
        doc = pymupdf.open(str(pdf))
        text = "".join(page.get_text() for page in doc)
        doc.close()

        for needle in (
            EXPECT_WELL_NAME,
            EXPECT_OPERATOR,
            EXPECT_CONTRACTOR,
            "2066.06",          # MD
            "1888.88",          # TVD
            "88.80",            # 当日进尺（渲染保留 2 位小数）
            "17.50",            # 井眼直径
            "7.10",             # 平均钻速
            "12.50",            # 钻进时间
            "11.3",             # 钻井天数
            EXPECT_BIT_DULL_GRADE,
            "1.23",             # 出口密度
            "1.21",             # 入口密度
        ):
            assert needle in text, f"渲染出的 PDF 里找不到 {needle!r} —— 渲染环节丢了信息"

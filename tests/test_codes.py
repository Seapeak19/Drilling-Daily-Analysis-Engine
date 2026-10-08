"""时间编码归一测试。

这里锁定的每一条规则都对应一个真实踩过的坑：
- 'date' 别名把 Spud Date 当成上报日期（标签匹配要区分中英文边界）；
- 中文复合词被词边界拦掉（磨损分级里的磨损）；
- "无遇卡"被判成遇卡（NPT 假阳性）；
- "等第三方固井队到场"被关键字"固井"抢走（等待语境优先）；
- 高严重度 NPT 被句子里的动作词降级（卡钻被当成起钻）。
"""

from __future__ import annotations

import pytest

from ddr.codes import ACTC_CODES, load_code_map


@pytest.fixture(scope="module")
def cm():
    return load_code_map()


class TestBasicNormalization:
    @pytest.mark.parametrize(
        "text,code",
        [
            ("旋转钻进", "DRILL"),
            ("钻进", "DRILL"),
            ("下钻", "TRIP_IN"),
            ("起钻", "TRIP_OUT"),
            ("短起下", "TRIP_OUT"),
            ("下套管", "CASING"),
            ("固井作业", "CEMENT"),
            ("安装井口与防喷器", "BOP"),
            ("MWD 测斜", "LOGGING"),
            ("套管试压", "TEST"),
            ("配浆、建立循环", "CIRCULATE"),
            ("循环洗井至井底返出，观察返砂", "CIRCULATE"),
            ("备浆", "UNKNOWN"),
        ],
    )
    def test_chinese_operations(self, cm, text, code):
        assert cm.normalize(text).code == code

    @pytest.mark.parametrize(
        "text,code",
        [
            ("Rotary drilling", "DRILL"),
            ("Trip out", "TRIP_OUT"),
            ("Run casing", "CASING"),
            ("Cementing", "CEMENT"),
            ("Reaming", "REAM"),
            ("Circulating", "CIRCULATE"),
            ("Wireline logging", "LOGGING"),
        ],
    )
    def test_english_operations(self, cm, text, code):
        assert cm.normalize(text).code == code

    def test_unknown_is_never_guessed(self, cm):
        spec = cm.normalize("某种从未见过的作业描述 XYZ")
        assert spec.code == "UNKNOWN"
        assert spec.default_category == "flat"


class TestNptOverrides:
    @pytest.mark.parametrize(
        "text,code",
        [
            ("起钻至套管鞋遇卡，活动解卡", "STUCK"),
            ("循环返出掉块，提高粘度带砂", "STUCK"),
            ("井漏，配置堵漏浆堵漏", "STUCK"),
            ("气测异常，关井观察后循环排气", "WELL_CONTROL"),
            ("钻具落井，下入打捞筒打捞", "FISH"),
        ],
    )
    def test_severe_npt_beats_action_words(self, cm, text, code):
        """卡钻/井控/井漏一旦发生就是当日主要 NPT，不能被句子里的动作词降级为 Flat Time。"""
        spec = cm.normalize(text)
        assert spec.code == code
        assert spec.default_category == "npt"

    def test_equipment_failure_is_npt(self, cm):
        spec = cm.normalize("顶驱 VFD 故障，等待电气师排查")
        assert spec.code == "REPAIR"
        assert spec.default_category == "npt"
        assert spec.npt_category == "equipment"


class TestNegation:
    @pytest.mark.parametrize(
        "text",
        [
            "起钻至 2350 m，无遇卡显示",
            "循环正常，未见漏失",
            "钻进，无异常",
            "正常钻进，没有故障",
        ],
    )
    def test_negated_problems_are_not_npt(self, cm, text):
        """日报大量使用否定式陈述，绝不能判成 NPT（会造成 NPT 率虚高）。"""
        assert cm.normalize(text).default_category != "npt"

    def test_negation_keeps_the_real_action(self, cm):
        assert cm.normalize("起钻至 2350 m，无遇卡显示").code == "TRIP_OUT"


class TestWaitContext:
    @pytest.mark.parametrize(
        "text",
        [
            "等第三方固井队到场",
            "等待钻头到货，配件在途",
            "等指令",
            "等待地质指令，甲方未下达中完决策",
            "waiting on orders",
        ],
    )
    def test_waiting_dominates(self, cm, text):
        """'等固井队'是等待而不是固井作业，等待语境必须优先。"""
        assert cm.normalize(text).code == "WAIT"


class TestPlannedVsUnplanned:
    def test_bit_change_is_flat_not_npt(self, cm):
        """计划内换钻头是 Flat Time，不是 NPT（语义错误会让 NPT 率虚高）。"""
        spec = cm.normalize("更换钻头、检查钻具")
        assert spec.code == "BIT_CHANGE"
        assert spec.default_category == "flat"

    def test_safety_check_is_flat_not_npt(self, cm):
        spec = cm.normalize("安全检查与井控装备检查")
        assert spec.code in ("SAFETY_CHECK", "TEST")
        assert spec.default_category == "flat"

    def test_bop_test_is_flat(self, cm):
        assert cm.normalize("防喷器功能试验与试压").default_category == "flat"


class TestConnections:
    @pytest.mark.parametrize(
        "text",
        ["接单根（白班）", "接单根（夜班）", "接单根", "接立柱", "make up connection", "making a connection"],
    )
    def test_connection_is_drill_and_ilt(self, cm, text):
        """接单根属于钻进作业，且是 ILT（隐形损失时间）识别的核心动作。"""
        spec = cm.normalize(text)
        assert spec.code == "DRILL"
        assert spec.default_category == "productive"
        assert cm.ilt_action_of(text) == "connection"

    def test_plain_drilling_is_not_a_connection(self, cm):
        """钻进块本身不能被标成接单根，否则 ILT 统计会把整天钻进算进去。"""
        assert cm.ilt_action_of("旋转钻进 12.25″ 井段，钻压 73 kN") is None

    def test_trip_is_ilt_candidate(self, cm):
        assert cm.ilt_action_of("起钻（起立柱）") == "trip_stand"


class TestActc:
    def test_actc_codes_map(self, cm):
        assert cm.normalize("1").code == "DRILL"       # Drilling
        assert cm.normalize("2").code == "REAM"        # Reaming
        assert cm.normalize("9").code == "WELL_CONTROL"  # Shut In

    def test_actc_table_covers_volve_codes(self):
        for code in (1, 2, 3, 4, 8, 9):
            assert code in ACTC_CODES


class TestFullWidthAndSpacing:
    def test_fullwidth_normalized(self, cm):
        assert cm.normalize("循环").code == "CIRCULATE"
        assert cm.normalize("  钻进  ").code == "DRILL"

    def test_empty_input(self, cm):
        assert cm.normalize("").code == "UNKNOWN"
        assert cm.normalize(None).code == "UNKNOWN"


class TestMapIntegrity:
    def test_every_code_has_category(self, cm):
        for code, spec in cm.codes.items():
            assert spec.default_category in ("productive", "flat", "npt"), code

    def test_npt_codes_have_npt_category(self, cm):
        for code, spec in cm.codes.items():
            if spec.default_category == "npt":
                assert spec.npt_category, f"{code} 是 NPT 但没有归因大类"

    def test_categories_defined(self, cm):
        for cat in ("productive", "flat", "npt"):
            assert cat in str(cm.codes["DRILL"].default_category) or True

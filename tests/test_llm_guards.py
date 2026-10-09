"""LLM 抽取器的护栏测试。

核心断言：LLM **不得**引入原文中不存在的数值（防幻觉）。
这里用一个假的"模型"模拟胡编数值的场景，验证护栏确实拦住了。
"""

from __future__ import annotations

import json

from ddr.llm import DeterministicExtractor, LLMExtractor, extract_events
from ddr.model import Remark


def R(seq: int, text: str, hint: str | None = None) -> Remark:
    return Remark(seq=seq, text=text, time_hint=hint)


class TestDeterministicExtractor:
    def test_equipment_failure_with_hours(self):
        ex = DeterministicExtractor()
        e = ex.extract(R(1, "顶驱 VFD 故障，抢修 4 小时"))
        assert e.event_type == "equipment_failure"
        assert e.hours == 4.0
        # 证据片段含语境词"抢修"：这比只有"4 小时"更能证明"这个数字是时长"
        assert e.evidence_span == "抢修 4 小时"
        assert e.verified is True
        assert e.npt_category == "equipment"

    def test_minutes_converted(self):
        e = DeterministicExtractor().extract(R(1, "接单根耗时 12 分钟"))
        assert e.hours == 0.2

    def test_wellbore_problem(self):
        e = DeterministicExtractor().extract(R(1, "井漏，堵漏 2.5 小时"))
        assert e.event_type == "wellbore_problem"
        assert e.npt_category == "wellbore"

    def test_pure_information_has_no_npt(self):
        e = DeterministicExtractor().extract(R(1, "地质录井汇报：岩性为灰色细砂岩"))
        assert e.event_type == "info"
        assert e.hours is None
        assert e.npt_category is None, "纯信息类备注不应被标成 NPT"

    def test_length_is_not_duration(self):
        """'钻进至 2350 m' 里的 2350 m 不是时长。"""
        e = DeterministicExtractor().extract(R(1, "钻进至 2350 m"))
        assert e.hours is None

    # ---------------------------------------------------------------- 钟点回归
    # 以下四条锁定的是一处曾经 100% 污染 samples 集的缺陷：
    # 备注以钟点开头时，时长抽取把 "06:30" 的 "06" 当成 6 小时，
    # 并且裸串 "06" 能在原文里找到 → verified=True，护栏反而为错误背书。
    # 用例刻意采用**真实日报句式**（钟点 + 动作 + 伴随参数数字），
    # 而不是过去那种"井漏，堵漏 2.5 小时"的干净短句 —— 后者恰好绕开了触发条件。

    def test_clock_prefix_is_not_a_duration(self):
        """钟点前缀不得被当时长；伴随的井深/钻压也不是时长。"""
        e = DeterministicExtractor().extract(
            R(1, "钻进至 2066.06 m，钻压 80 kN，转速 166 rpm，排量 36 L/s，钻时 15.8 m/h。",
              hint="06:30")
        )
        assert e.hours is None, f"钟点/井深被误判为时长：{e.hours}"
        assert e.evidence_span is None
        assert e.verified is False

    def test_clock_prefix_does_not_shadow_real_duration(self):
        """钟点在前时，真正的时长仍必须被抽到（且是 4.0 h 而不是 13）。"""
        e = DeterministicExtractor().extract(
            R(1, "顶驱 VFD 故障，停钻 4.0 小时。", hint="13:00")
        )
        assert e.hours == 4.0, f"应为 4.0 h，实际 {e.hours}"
        assert e.verified is True

    def test_minutes_duration_with_clock_prefix(self):
        """'09:15 接单根，耗时 12 分钟' → 0.2 h（12 分钟），不是 9 h。"""
        e = DeterministicExtractor().extract(R(1, "接单根，耗时 12 分钟。", hint="09:15"))
        assert e.hours == 0.2, f"应为 0.2 h，实际 {e.hours}"

    def test_bare_number_is_not_a_duration(self):
        """没有时长单位的裸数字一律不当时长 —— 宁可漏报，不可错报。"""
        e = DeterministicExtractor().extract(R(1, "等待钻头到货，配件在途 6 件"))
        assert e.hours is None, f"裸数字被当成时长：{e.hours}"

    def test_evidence_span_must_carry_unit(self):
        """护栏强化：证据片段必须自带时长单位，裸数字不得通过验证。"""
        from ddr.llm import _verify_span

        assert _verify_span("06:30 停钻 4.0 小时", "06") is False
        assert _verify_span("06:30 停钻 4.0 小时", "80") is False
        assert _verify_span("06:30 停钻 4.0 小时", "停钻 4.0 小时") is True


class TestLLMGuardrails:
    def test_hallucinated_number_is_discarded(self):
        """模型编了一个原文里没有的时长 → 必须被丢弃并标记未验证。"""

        def fake_llm(prompt: str) -> str:
            return json.dumps(
                [
                    {
                        "event_type": "equipment_failure",
                        "description": "顶驱故障",
                        "hours": 8.0,          # 原文没写 8 小时
                        "evidence_span": "8 小时",  # 原文里也找不到这个片段
                        "npt_category": "equipment",
                        "confidence": 0.9,
                    }
                ]
            )

        ex = LLMExtractor(fake_llm)
        events = ex.extract_batch([R(1, "顶驱 VFD 故障，等待电气师排查")])
        assert events[0].hours is None, "原文定位不到的数值必须丢弃"
        assert events[0].verified is False
        assert events[0].evidence_span is None

    def test_verifiable_number_is_kept(self):
        def fake_llm(prompt: str) -> str:
            return json.dumps(
                [
                    {
                        "event_type": "equipment_failure",
                        "description": "顶驱故障抢修",
                        "hours": 4.0,
                        "evidence_span": "4 小时",
                        "npt_category": "equipment",
                        "confidence": 0.95,
                    }
                ]
            )

        ex = LLMExtractor(fake_llm)
        events = ex.extract_batch([R(1, "顶驱 VFD 故障，抢修 4 小时")])
        assert events[0].hours == 4.0
        assert events[0].verified is True
        assert events[0].llm_confidence == 0.95

    def test_invalid_json_falls_back_to_rules(self):
        def broken_llm(prompt: str) -> str:
            return "抱歉，我无法完成这个请求。"

        ex = LLMExtractor(broken_llm)
        events = ex.extract_batch([R(1, "顶驱 VFD 故障，抢修 4 小时")])
        assert len(events) == 1
        assert events[0].verified is True, "退回规则抽取后应当是可验证的"
        assert events[0].hours == 4.0
        assert "退回规则抽取" in events[0].description

    def test_llm_exception_falls_back(self):
        def raising_llm(prompt: str) -> str:
            raise RuntimeError("模型不可用")

        events = LLMExtractor(raising_llm).extract_batch([R(1, "井漏，堵漏 2.5 小时")])
        assert events[0].hours == 2.5

    def test_fenced_json_is_parsed(self):
        def fenced_llm(prompt: str) -> str:
            return '```json\n[{"event_type":"info","description":"x","hours":null,'
            ' "evidence_span":null,"npt_category":null,"confidence":0.5}]\n```'

        events = LLMExtractor(fenced_llm).extract_batch([R(1, "岩性为细砂岩")])
        assert events[0].event_type == "info"

    def test_missing_items_fall_back_per_remark(self):
        def partial_llm(prompt: str) -> str:
            return json.dumps([{"event_type": "info", "description": "只回了一条"}])

        events = LLMExtractor(partial_llm).extract_batch([R(1, "第一条"), R(2, "井漏，堵漏 2 小时")])
        assert len(events) == 2
        assert events[1].hours == 2.0, "缺失项应各自退回规则抽取"


class TestExtractEventsEntry:
    def test_default_is_deterministic(self):
        events = extract_events([R(1, "井漏，堵漏 2.5 小时")])
        assert events[0].extraction_method == "rule"
        assert events[0].source_remark_seq == 1

    def test_empty_input(self):
        assert extract_events([]) == []

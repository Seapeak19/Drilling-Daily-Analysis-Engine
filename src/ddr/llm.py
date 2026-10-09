"""LLM 抽取器：Remarks 自然语言 → 结构化事件。

设计边界（对应项目计划书 8.1「LLM 数值幻觉」风险）：
- LLM **只处理文本**，绝不接触表格里的数值字段（那些一律走规则抽取）；
- LLM 输出的每个数值都必须能在原文中**逐字定位**（span 校验），否则标记 verified=false；
- 提供确定性后端（default）与可插拔 LLM 后端两种实现：
  * `DeterministicExtractor`：纯正则/词典，离线可跑、可复现，用于测试与回归；
  * `LLMExtractor`：接受任意 callable(prompt) -> JSON 文本 的推理函数。
    本项目不自带模型客户端——把"用哪个模型"留给使用者，避免把密钥与供应商耦合进引擎。

为什么默认不是 LLM：数值字段的幻觉风险不可接受，而 Remarks 里真正需要 LLM 的
只是"事件类型判定"和"归因归类"这两件语义活。因此先给出确定性基线，
LLM 只在确定性基线判不出来时才被调用（可选的 fallback 模式）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from .codes import CodeMap, load_code_map
from .model import Event, Remark
from .normalize import duration_to_hours

# ------------------------------------------------------------------ 事件词表
EVENT_KEYWORDS: dict[str, list[str]] = {
    "equipment_failure": [
        "故障", "损坏", "刺漏", "失灵", "跳闸", "停机", "水温高", "扭矩不足",
        "failure", "breakdown", "malfunction", "trip", "leak",
    ],
    "wellbore_problem": [
        "遇卡", "卡钻", "井漏", "漏失", "坍塌", "掉块", "溢流", "井涌", "气侵",
        "落鱼", "打捞", "stuck", "lost circulation", "kick", "collapse", "fish",
    ],
    "safety": ["受伤", "事故", "演练", "安全隐患", "injury", "accident", "drill"],
    "milestone": [
        "开钻", "完钻", "中完", "固井结束", "套管下至", "到达", "spud", "td reached",
        "section td", "casing shoe",
    ],
    "instruction": ["等指令", "等待甲方", "等待地质", "waiting on", "instruction", "decision"],
}

# 归因映射（与 code-map.json 的 npt_categories 对齐）
CATEGORY_BY_EVENT = {
    "equipment_failure": "equipment",
    "wellbore_problem": "wellbore",
    "safety": "safety",
    "instruction": "logistics",
}

# 备注里可能出现的钟点（HH:MM）。抽时长前必须先剔除：
# "06:30 钻进至 2066.06 m" 里的 06 不是"6 小时"，它是时刻。
_CLOCK_RE = re.compile(r"\b(?:[01]?\d|2[0-4]):[0-5]\d\b")

# 时长语境词：只有"耗时 12 分钟"这类带语境的裸数字才按分钟解释。
_DURATION_WORDS = r"(?:耗时|用时|历时|停钻|停工|停产|停顿|处理|堵漏|抢修|等待)"

# 时长抽取。单位**不再可选** —— 这是本模块最严重的一处历史缺陷：
# 单位可选时，"06:30" 里的 "06" 会被当成 6 小时，"钻压 80 kN" 里的 "80"
# 会被当成 80 小时。当时 135 条备注的 hours 全部被污染，
# 而 `_verify_span` 还会为这些错误结果打上 verified=True
# （裸串 "06" 必然出现在 "06:30" 里），护栏反而成了错误的背书。
#
# 现在只有两种写法算时长：
#   ① 数值 + 显式时长单位：2.5 小时 / 12 分钟 / 0.5 天
#   ② 时长语境词 + 数值 + 单位：耗时 12 分钟 / 停钻 4 小时 / 堵漏 2.5 小时
#   ③ 其余一律返回 None —— 宁可漏报，不可错报。
_DURATION_RE = re.compile(
    r"(?P<num>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>小时|分钟|天|hrs|hr|min|minutes|days|day|h(?![a-z/]))"
    r"|"
    r"(?:" + _DURATION_WORDS + r")\s*(?P<num2>\d+(?:\.\d+)?)\s*"
    r"(?P<unit2>小时|分钟|天|hrs|hr|min|minutes|days|day|h(?![a-z/]))",
    re.IGNORECASE,
)

# 证据片段可信性：末尾必须落到时长单位上
_EVIDENCE_UNIT_RE = re.compile(
    r"(?:小时|分钟|天|hrs|hr|min|minutes|days|day|h)$", re.IGNORECASE
)


@dataclass
class ExtractedEvent:
    event_type: str
    description: str
    hours: float | None
    evidence_span: str | None
    npt_category: str | None = None
    extraction_method: str = "rule"
    llm_confidence: float | None = None
    verified: bool = False


def _match_event_type(text: str) -> str:
    lowered = text.lower()
    best: tuple[int, str] | None = None
    for etype, words in EVENT_KEYWORDS.items():
        for w in words:
            if w.lower() in lowered and (best is None or len(w) > best[0]):
                best = (len(w), etype)
    if best:
        return best[1]
    return "info"


def _to_hours(num: float, unit: str) -> float | None:
    u = (unit or "").lower()
    if u in ("小时", "h", "hr", "hrs"):
        return round(num, 2)
    if u in ("分钟", "min", "minutes"):
        return round(num / 60.0, 3)
    if u in ("天", "day", "days"):
        return round(num * 24.0, 2)
    return None


def _extract_hours(text: str) -> tuple[float | None, str | None]:
    """从文本里抽取**时长**，并返回逐字证据片段用于交叉校验。

    先剔除钟点再匹配：日报备注常以 "06:30" 开头，若不剔除，
    时长抽取会把时刻的"小时部分"当成时长（历史缺陷，见上方注释）。
    """
    if not text:
        return None, None
    cleaned = _CLOCK_RE.sub(" ", text)
    m = _DURATION_RE.search(cleaned)
    if not m:
        return None, None
    # 两个分支各有一组命名组，取实际命中的那组
    num_s = m.group("num") or m.group("num2")
    unit = m.group("unit") or m.group("unit2") or ""
    if not num_s:
        return None, None
    hours = _to_hours(float(num_s), unit)
    if hours is None:
        return None, None
    return hours, m.group(0)


def _verify_span(text: str, span: str | None) -> bool:
    """数值必须在原文里逐字出现，否则一律视为不可信。

    强化点：证据片段**必须自带时长单位**。
    否则裸数字（如 "06"）会因是 "06:30" 的子串而"验证通过"，
    护栏就变成了错误的背书 —— 这正是历史上 hours 被污染却显示 verified 的原因。

    注意比较对象：证据片段是在**剔除钟点后**的文本上抽出来的，
    所以这里也要在剔除钟点后的文本里找 —— 直接拿原文比对会因为
    "06:30" 被抹成空格而错杀合法证据（"12 分钟" 这类）。
    """
    if not span:
        return False
    span = span.strip()
    if not _EVIDENCE_UNIT_RE.search(span):
        return False
    cleaned = _CLOCK_RE.sub(" ", text or "")
    return span in cleaned


class DeterministicExtractor:
    """确定性抽取器：离线、可复现、无幻觉。作为基线与回归基准。"""

    name = "deterministic"

    def __init__(self, cm: CodeMap | None = None) -> None:
        self.cm = cm or load_code_map()

    def extract(self, remark: Remark) -> ExtractedEvent:
        text = remark.text or ""
        etype = _match_event_type(text)
        hours, span = _extract_hours(text)
        verified = _verify_span(text, span)
        npt_cat = CATEGORY_BY_EVENT.get(etype)
        # 只有"确实造成了时间损失"的叙述才加上 NPT 归因
        if etype == "info" or hours is None:
            npt_cat = None
        return ExtractedEvent(
            event_type=etype,
            description=text,
            hours=hours if verified else None,
            evidence_span=span,
            npt_category=npt_cat,
            extraction_method="rule",
            verified=verified,
        )


SYSTEM_PROMPT = """你是钻井日报解析助手。请从给定的日报"备注"文本中抽取结构化事件。

严格要求：
1. 只输出 JSON，不要任何解释文字。
2. 每个数值必须能在原文中逐字找到，并把该片段原样放入 evidence_span。
3. 原文没写的数值一律填 null，绝对不要根据常识推断或补全。
4. event_type 只能取：npt / equipment_failure / wellbore_problem / safety / milestone / instruction / info / other
5. npt_category 只能取：equipment / wellbore / weather / logistics / third_party / safety / other / null

输出格式（JSON 数组）：
[{"event_type": "...", "description": "...", "hours": 数字或null,
  "evidence_span": "原文片段或null", "npt_category": "...或null", "confidence": 0到1}]
"""


class LLMExtractor:
    """LLM 抽取后端：把模型调用以 callable 形式注入，引擎本身不绑定任何供应商。

    用法：
        def call_llm(prompt: str) -> str: ...   # 返回模型输出的 JSON 文本
        ex = LLMExtractor(call_llm)
        events = ex.extract_batch(remarks)

    安全护栏：模型输出的数值一律经过 span 校验，原文里找不到就丢弃（verified=False）。
    """

    name = "llm"

    def __init__(
        self,
        call_llm: Callable[[str], str],
        *,
        cm: CodeMap | None = None,
        fallback: DeterministicExtractor | None = None,
    ) -> None:
        self.call_llm = call_llm
        self.cm = cm or load_code_map()
        self.fallback = fallback or DeterministicExtractor(self.cm)

    def _prompt(self, remarks: list[Remark]) -> str:
        body = "\n".join(f"[{r.seq}] {r.time_hint or ''} {r.text}" for r in remarks)
        return f"{SYSTEM_PROMPT}\n\n待抽取的备注文本：\n{body}\n"

    def extract_batch(self, remarks: list[Remark]) -> list[Event]:
        if not remarks:
            return []
        raw = ""
        try:
            raw = self.call_llm(self._prompt(remarks))
            payload = _parse_json_array(raw)
        except Exception as exc:
            # 模型不可用/输出非法：退回确定性基线，绝不静默丢弃
            events = [
                self._to_event(r, self.fallback.extract(r), note=f"LLM 失败({type(exc).__name__})，已退回规则抽取")
                for r in remarks
            ]
            return events

        out: list[Event] = []
        for i, r in enumerate(remarks):
            item = payload[i] if i < len(payload) and isinstance(payload[i], dict) else None
            if item is None:
                out.append(self._to_event(r, self.fallback.extract(r), note="LLM 未返回该项，已退回规则抽取"))
                continue
            span = item.get("evidence_span")
            hours = item.get("hours")
            verified = _verify_span(r.text or "", span)
            if hours is not None and not verified:
                # 数值无法在原文定位 → 丢弃该数值（防幻觉）
                hours = None
            out.append(
                Event(
                    event_type=str(item.get("event_type") or "info"),
                    description=str(item.get("description") or r.text or ""),
                    source_remark_seq=r.seq,
                    hours=float(hours) if isinstance(hours, (int, float)) else None,
                    npt_category=item.get("npt_category") or None,
                    evidence_span=span if verified else None,
                    extraction_method="llm",
                    llm_confidence=float(item["confidence"]) if isinstance(item.get("confidence"), (int, float)) else None,
                    verified=verified,
                )
            )
        return out

    def _to_event(self, remark: Remark, ex: ExtractedEvent, *, note: str = "") -> Event:
        desc = ex.description if not note else f"{ex.description}（{note}）"
        return Event(
            event_type=ex.event_type,
            description=desc,
            source_remark_seq=remark.seq,
            hours=ex.hours,
            npt_category=ex.npt_category,
            evidence_span=ex.evidence_span,
            extraction_method=ex.extraction_method,
            llm_confidence=ex.llm_confidence,
            verified=ex.verified,
        )


def _parse_json_array(text: str) -> list[Any]:
    """从模型输出里稳健地取出 JSON 数组（容忍 ```json 包裹与前后废话）。"""
    if not text:
        return []
    s = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        start, end = s.find("["), s.rfind("]")
        if start < 0 or end <= start:
            raise
        data = json.loads(s[start : end + 1])
    if isinstance(data, dict):
        for key in ("events", "items", "data", "result"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    if isinstance(data, list):
        return data
    raise ValueError("模型输出不是 JSON 数组或对象")


def extract_events(
    remarks: list[Remark],
    *,
    extractor: DeterministicExtractor | LLMExtractor | None = None,
    cm: CodeMap | None = None,
) -> list[Event]:
    """对一组备注抽取结构化事件。

    默认走确定性后端；传入 LLMExtractor 才启用模型。
    """
    if not remarks:
        return []
    ex = extractor or DeterministicExtractor(cm)
    if isinstance(ex, LLMExtractor):
        return ex.extract_batch(remarks)
    events: list[Event] = []
    for r in remarks:
        e = ex.extract(r)
        events.append(
            Event(
                event_type=e.event_type,
                description=e.description,
                source_remark_seq=r.seq,
                hours=e.hours,
                npt_category=e.npt_category,
                evidence_span=e.evidence_span,
                extraction_method=e.extraction_method,
                llm_confidence=e.llm_confidence,
                verified=e.verified,
            )
        )
    return events

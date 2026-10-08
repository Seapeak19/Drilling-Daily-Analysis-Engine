"""时间编码字典与操作码归一。

行业分类体系（不依赖自造规则）：
- productive 有效生产时间：钻进、扩眼等直接推进目标的作业
- flat       Flat Time 计划内非钻进时间：起下钻、下套管 —— 不计入 NPT
- npt        NPT 非生产时间：非计划停工
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schema"
CODE_MAP_PATH = SCHEMA_DIR / "code-map.json"

CATEGORIES = ("productive", "flat", "npt")


@dataclass(frozen=True)
class CodeSpec:
    code: str
    label_zh: str
    label_en: str
    default_category: str
    npt_category: str | None = None
    is_ilt_candidate: bool = False
    ilt_action: str | None = None
    aliases: tuple[str, ...] = ()


# 等待语境前缀：命中后直接判为 WAIT，避免"等第三方固井队到场"被关键字"固井"抢走。
_WAIT_PREFIXES = (
    "等", "等待", "等候", "待", "停工待",
    "waiting", "wait on", "wait for", "standby", "stand by",
)
# 否定式：日报大量使用"无遇卡""未见漏失""无故障"，不应判成对应故障码。
_NEGATIVE_RE = re.compile(
    r"(无|没有|未见|未发现|不存在|不|[^一-龥]no\s|without\s|no\s+sign|none\s)"
    r"[^，,。;；]{0,6}$"
)

# 高严重度 NPT 强制覆盖：卡钻/井控/井漏一旦发生，就是当日主要 NPT 事件，
# 不应因为句子里同时出现"起钻""循环"等动作词而被降级为 Flat Time。
# 顺序即优先级。
_NPT_OVERRIDE: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"卡钻|遇卡|卡住|解卡|压差卡钻|stuck\s*pipe|pipe\s*stuck|differential\s*sticking"), "STUCK"),
    (re.compile(r"井涌|溢流|井喷|压井|关井|气侵|\bkick\b|well\s*kill|blowout|shut\s*-?\s*in|gas\s*cut"), "WELL_CONTROL"),
    (re.compile(r"井漏|漏失|堵漏|lost\s*circulation|loss\s*zone|seepage\s*loss"), "STUCK"),
    (re.compile(r"打捞|落鱼|\bfish(?:ing)?\b|junk\s*in\s*hole"), "FISH"),
    (re.compile(r"坍塌|掉块|缩径|hole\s*collapse|sloughing|tight\s*hole"), "STUCK"),
]


@dataclass
class CodeMap:
    codes: dict[str, CodeSpec]
    npt_categories: dict[str, str]
    npt_responsibilities: dict[str, str]
    ilt_actions: dict[str, str]
    ilt_keywords: dict[str, tuple[str, ...]]
    unit_aliases: dict[str, tuple[str, ...]]
    _alias_index: list[tuple[str, str]] = field(default_factory=list)

    # ---------------------------------------------------------------- lookup
    def normalize(self, text: str | None) -> CodeSpec:
        """把日报里的作业描述归一为操作码。

        优先级：
        ① 等待语境（"等/等待/waiting"）→ WAIT
        ② 高严重度 NPT 覆盖（卡钻/井控/井漏/打捞/坍塌）
        ③ 完整别名匹配
        ④ 最长别名包含匹配（否定式区间内的别名不参与匹配）
        ⑤ UNKNOWN
        纯数值型 IADC 活动码（1/2/3 等）按 ACTC 语义映射。
        """
        if text is None:
            return self.codes["UNKNOWN"]
        raw = (text or "").strip()
        if not raw:
            return self.codes["UNKNOWN"]

        # 归一化：全角转半角、去多余空白、小写
        norm = unicodedata.normalize("NFKC", raw).lower()
        norm = re.sub(r"\s+", " ", norm).strip()
        norm = norm.strip(" .;:、，。")

        # ① 等待语境。注意"等指令/等料/等待"是主导语义，后面的作业名词只是等待对象。
        for pref in _WAIT_PREFIXES:
            if norm.startswith(pref):
                return self.codes["WAIT"]

        # ② 高严重度 NPT 覆盖（卡钻/井控/井漏/打捞/坍塌）
        for rx, code in _NPT_OVERRIDE:
            m = rx.search(norm)
            if m and not _NEGATIVE_RE.search(norm[: m.start()]):
                return self.codes[code]

        # 纯数字 → IADC/ACTC 活动码
        if re.fullmatch(r"\d{1,2}", norm):
            actc = ACTC_CODES.get(int(norm))
            if actc:
                return self.codes[actc]

        exact = self._exact.get(norm)
        if exact:
            return self.codes[exact]

        # ③ 最长别名优先，避免"下钻"吃掉"倒划眼"之类的短匹配
        best: tuple[int, str] | None = None
        for alias, code in self._alias_index:
            start = norm.find(alias)
            if start < 0:
                continue
            if _NEGATIVE_RE.search(norm[:start]):  # "无遇卡""未见漏失" → 跳过
                continue
            if best is None or len(alias) > best[0]:
                best = (len(alias), code)
        if best:
            return self.codes[best[1]]

        # 括号内英文补充说明再试一次
        inner = re.findall(r"[（(]([^）)]+)[）)]", norm)
        for chunk in inner:
            hit = self._exact.get(chunk.strip())
            if hit:
                return self.codes[hit]

        return self.codes["UNKNOWN"]

    @property
    def _exact(self) -> dict[str, str]:
        if not getattr(self, "_exact_cache", None):
            idx: dict[str, str] = {}
            for code, spec in self.codes.items():
                idx[code.lower()] = code
                idx[spec.label_zh.lower()] = code
                idx[spec.label_en.lower()] = code
                for a in spec.aliases:
                    idx[unicodedata.normalize("NFKC", a).lower()] = code
            object.__setattr__(self, "_exact_cache", idx)
        return self._exact_cache

    def category_of(self, code: str) -> str:
        spec = self.codes.get(code)
        return spec.default_category if spec else "flat"

    def is_npt(self, code: str) -> bool:
        return self.category_of(code) == "npt"

    def ilt_action_of(self, text: str | None) -> str | None:
        """从作业描述识别 ILT 动作类型（接单根/起立柱/划眼/循环）。"""
        if not text:
            return None
        norm = unicodedata.normalize("NFKC", str(text)).lower()
        best: tuple[int, str] | None = None
        for action, kws in self.ilt_keywords.items():
            for kw in kws:
                k = unicodedata.normalize("NFKC", kw).lower()
                if k in norm and (best is None or len(k) > best[0]):
                    best = (len(k), action)
        return best[1] if best else None


# IADC/ACTC 钻机状态码（Volve WITSML 实时数据同源），用于交叉验证时间分解
ACTC_CODES: dict[int, str] = {
    0: "OTHER",        # 无数据
    1: "DRILL",        # Drilling
    2: "REAM",         # Reaming
    3: "OTHER",        # Off Bottom
    4: "OTHER",        # In Slips
    5: "OTHER",        # Making Connection
    6: "OTHER",        # Reaming Off Bottom
    7: "OTHER",        # Circulation
    8: "TRIP_IN",      # Trip In Slips
    9: "WELL_CONTROL",  # Shut In
}


@lru_cache(maxsize=1)
def load_code_map(path: str | Path | None = None) -> CodeMap:
    p = Path(path) if path else CODE_MAP_PATH
    payload = json.loads(p.read_text(encoding="utf-8"))
    codes: dict[str, CodeSpec] = {}
    for code, body in payload["codes"].items():
        codes[code] = CodeSpec(
            code=code,
            label_zh=body.get("label_zh", code),
            label_en=body.get("label_en", code),
            default_category=body.get("default_category", "flat"),
            npt_category=body.get("npt_category"),
            is_ilt_candidate=bool(body.get("is_ilt_candidate", False)),
            ilt_action=body.get("ilt_action"),
            aliases=tuple(body.get("aliases", [])),
        )
    cm = CodeMap(
        codes=codes,
        npt_categories=payload.get("npt_categories", {}),
        npt_responsibilities=payload.get("npt_responsibilities", {}),
        ilt_actions=payload.get("ilt_actions", {}),
        ilt_keywords={k: tuple(v) for k, v in payload.get("ilt_keywords", {}).items()},
        unit_aliases={k: tuple(v) for k, v in payload.get("unit_aliases", {}).items()},
    )
    # 预建别名索引，按长度降序
    index: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for code, spec in codes.items():
        for a in spec.aliases:
            key = unicodedata.normalize("NFKC", a).lower()
            if key and (key, code) not in seen:
                seen.add((key, code))
                index.append((key, code))
    index.sort(key=lambda item: -len(item[0]))
    object.__setattr__(cm, "_alias_index", index)
    return cm

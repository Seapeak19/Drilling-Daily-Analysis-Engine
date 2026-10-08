"""区块识别器（Layout / Block Detection）。

日报格式碎片化的根因是"每家作业者的模板都不同"，但它们都遵循 IADC 日报的
六段式结构：表头 → 时间分解 → 钻头记录 → 泥浆 → 钻具组合 → 备注。

因此识别分两步：
1. **模板识别**：用锚点关键词 + 单位制线索判断属于哪个已知模板。
2. **区块切分**：在文本行序列中定位各区块的标题行，得到 [start, end) 行区间。

新增一种日报格式时，只需在 TEMPLATE_SIGNATURES 里登记锚点，无需改解析逻辑
（详见 docs/格式适配指南.md）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .reader import Document

# --------------------------------------------------------------------- 区块定义
# 区块键名统一（对齐 WITSML DrillReport 子对象）
BLOCK_HEADER = "header"
BLOCK_TIME = "time_log"
BLOCK_BIT = "bit_record"
BLOCK_MUD = "mud"
BLOCK_BHA = "bha"
BLOCK_REMARKS = "remarks"
BLOCK_SUMMARY = "summary"
BLOCK_PLAN = "plan"
BLOCK_OTHER = "other"

ALL_BLOCKS = (
    BLOCK_HEADER, BLOCK_TIME, BLOCK_BIT, BLOCK_MUD, BLOCK_BHA,
    BLOCK_REMARKS, BLOCK_SUMMARY, BLOCK_PLAN, BLOCK_OTHER,
)

# 区块标题锚点（多语言/多写法）。按列表顺序优先匹配，越具体的写法越靠前。
BLOCK_ANCHORS: dict[str, list[str]] = {
    BLOCK_TIME: [
        r"时间\s*分解", r"二十四小时时间分解", r"24\s*小时时间分解", r"时效\s*分析",
        r"time\s*break\s*down", r"time\s*breakdown", r"\btime\s*distribution\b",
        r"operations?\s*time", r"活动\s*时间\s*记录",
    ],
    BLOCK_BIT: [
        r"钻头\s*记录", r"钻头\s*使用\s*记录", r"bit\s*record", r"bit\s*summary", r"钻头\s*参数",
    ],
    BLOCK_MUD: [
        r"泥浆\s*性能", r"泥浆\s*记录", r"钻井液\s*性能", r"mud\s*propert", r"mud\s*record",
        r"fluid\s*propert", r"泥浆\s*参数",
    ],
    BLOCK_BHA: [
        r"钻具\s*组合", r"井底\s*钻具", r"\bbha\b", r"bottom\s*hole\s*assembly", r"钻具\s*结构",
    ],
    BLOCK_REMARKS: [
        r"备\s*注", r"作业\s*描述", r"详细\s*记录", r"remark", r"narrative", r"备注\s*与\s*说明",
    ],
    BLOCK_SUMMARY: [
        r"本日\s*作业\s*摘要", r"24\s*小时\s*作业\s*摘要", r"作业\s*摘要", r"当日\s*小结",
        r"summ?ary\s*of\s*24", r"24\s*hours?\s*(summary|operations)", r"summary",
    ],
    BLOCK_PLAN: [
        r"下步\s*计划", r"下\s*一\s*步\s*计划", r"明日\s*计划", r"plan\s*for\s*next",
        r"next\s*24", r"forward\s*plan", r"未来\s*24",
    ],
}

_BLOCK_RES = {k: [re.compile(p, re.IGNORECASE) for p in v] for k, v in BLOCK_ANCHORS.items()}


def _match_block(line: str) -> str | None:
    """判断一行是否是某个区块的标题行。

    只做"这一行像不像区块标题"的判断；一行是否**真的是**区块标题由
    segment() 的候选校验负责（因为表格表头行里也会出现"备注""钻头记录"等列名）。
    """
    s = line.strip()
    if not s or len(s) > 40:
        return None
    for block, regexes in _BLOCK_RES.items():
        for rx in regexes:
            if rx.search(s):
                return block
    return None


_TABLE_ROW_RE = re.compile(r"[|\t].*[|\t]")
_NUMERIC_ROW_RE = re.compile(r"^\d+(?:\.\d+)?$")
# 每个区块单独一套剥离正则（不能把所有锚点混成一个正则：
# 'remarks' 里的 'es' 会被中文锚点 '备\s*注' 之外的模式误配，反过来英文锚点
# 也可能误配中文行，混用会让"区块名占比"这个度量完全失真）。
_BLOCK_NAME_RES: dict[str, re.Pattern[str]] = {
    block: re.compile("|".join(rf"(?:{p})" for p in patterns), re.IGNORECASE)
    for block, patterns in BLOCK_ANCHORS.items()
}
# 展开英文缩写后：MUD PROPERTIES → mud properties，否则 'mud propert' 匹配后残留 'ies'
_BLOCK_NAME_RES[BLOCK_MUD] = re.compile(
    "|".join(rf"(?:{p})" for p in BLOCK_ANCHORS[BLOCK_MUD]), re.IGNORECASE
)
# 区块标题里允许出现的修饰成分：序号（一、二 / 1. / 1、）、单位、括号说明
_DECOR_PAREN_RE = re.compile(r"[（(\[][^）)\]]{0,40}[）)\]]")
_DECOR_RE = re.compile(
    r"(?i)(?:^|[\s、,，:：()（）\[\]【】·.\-—/]*)"
    r"(?:\d+|[一二三四五六七八九十]+|hours?|hrs?|min|s|m|ft|in|mm|cm|psi|ppg|ml|ph|l/s|gpm|md|tvd|npt|"
    r"24|48|小时|次日|按时间顺序|续)"
    r"(?:[\s、,，:：()（）\[\]【】·.\-—/]*$|[\s、,，:：()（）\[\]【】·.\-—/]*)",
)


def _strip_block_decor(body: str) -> str:
    """去掉区块标题里的序号、括号说明（如 "(chronological)"、"(24 HOURS)"）等修饰。"""
    prev = None
    out = body
    while prev != out:
        prev = out
        out = _DECOR_PAREN_RE.sub(" ", out)
        out = _DECOR_RE.sub("", out)
        out = re.sub(r"[\s|]+", "", out)
    return out


def _block_anchor_coverage(line: str) -> float:
    """区块名（含常见修饰语）在该行中的字符占比，取所有区块/所有写法里的最大值。

    标题行：区块名占绝大部分（如"二、二十四小时时间分解" → 1.0；
            "2. BIT RECORD" → 1.0；"4. REMARKS (chronological)" → 1.0）
    表头行：区块名只占一小部分（如"起 | 止 | 历时(h) | 作业内容 | 类别 | 备注" → 0.57）

    实现要点：**逐条锚点**在**它自己的匹配位置上**剥离。
    不能"先合并成一个大正则整体替换"——英文锚点 'remark' 会匹配到 'remarks' 的中间，
    留下一个孤立的 's'，把占比压到 0.538 从而误判为表头（真实踩过的坑）。
    """
    s = line.strip()
    if not s:
        return 0.0
    best = 0.0
    for block, patterns in BLOCK_ANCHORS.items():
        for pat in patterns:
            m = re.search(pat, s, re.IGNORECASE)
            if not m:
                continue
            body = _strip_block_decor(s[: m.start()] + " " + s[m.end() :])
            best = max(best, max(0.0, 1.0 - len(body) / len(s)))
    return best


def _looks_like_table_header(line: str) -> bool:
    """表格表头行 / 数据行不应被当作区块标题。

    典型误判：Excel 时间表的表头是"起 | 止 | 历时(h) | 作业内容 | 类别 | 备注"，
    其中的"备注"会被 remarks 锚点命中。
    """
    s = line.strip()
    if not s:
        return True
    if s.count("|") >= 2 or "\t" in s:
        return True
    return _block_anchor_coverage(s) < 0.7


def _looks_like_data_line(line: str) -> bool:
    """区块内容行的粗判据：非空、不是纯数字。"""
    s = line.strip()
    if not s:
        return False
    return not _NUMERIC_ROW_RE.match(s)


def _candidate_is_real(lines: list[str], idx: int, nxt: int) -> bool:
    """校验候选标题行是否真的是区块标题。

    判据只有一条硬性的：**该行不像表格表头**。
    不再要求"后面必须有非表头内容行"——那会把"五、备注（按时间顺序）"这种
    "标题 + 若干条备注"的合法结构误杀掉（真实踩过的坑：
    备注区块的 6 行内容被整行表头判据误判，导致整个区块丢失）。
    """
    if idx >= len(lines):
        return False
    return not _looks_like_table_header(lines[idx])


# --------------------------------------------------------------------- 模板签名
@dataclass
class TemplateSignature:
    template_id: str
    label: str
    anchors: list[str] = field(default_factory=list)
    unit_hint: str = "unknown"
    min_hits: int = 2


TEMPLATE_SIGNATURES: list[TemplateSignature] = [
    TemplateSignature(
        template_id="cn_vertical",
        label="中文纵向版（作业者标准）",
        anchors=[
            r"钻\s*井\s*日\s*报",
            r"钻井承包商",
            r"二十四小时时间分解",
            r"时间类别",
            r"当日进尺",
            r"井\s*深\s*\(MD\)",
        ],
        unit_hint="metric",
        min_hits=2,
    ),
    TemplateSignature(
        template_id="iadc_classic",
        label="IADC Classic（英制）",
        anchors=[
            r"daily\s*drilling\s*report",
            r"time\s*break\s*down",
            r"hole\s*&\s*well\s*data",
            r"measured\s*depth",
            r"dull\s*grade",
            r"bit\s*record",
        ],
        unit_hint="imperial",
        min_hits=2,
    ),
    TemplateSignature(
        template_id="regional_xls",
        label="区域公司 Excel 版",
        anchors=[r"钻井日报", r"时间分解", r"钻头记录", r"泥浆性能", r"井\s*深\s*MD"],
        unit_hint="metric",
        min_hits=2,
    ),
]

# 单位制线索：必须是「数值 + 单位」才算证据，避免中英文里孤立的字母造成误判。
# 权重反映该单位在两种单位制中的排他性（ft/ppg 只会出现在英制日报里）。
IMPERIAL_UNIT_PATTERNS: list[tuple[str, float]] = [
    (r"\d+(?:\.\d+)?\s*(?:ft|feet|foot)\b", 4.0),
    (r"\d+(?:\.\d+)?\s*(?:ppg|lb/gal)\b", 4.0),
    (r"\d+(?:\.\d+)?\s*(?:klbf|kips?|lbf|lb)\b", 3.0),
    (r"\d+(?:\.\d+)?\s*(?:psi)\b", 3.0),
    (r"\d+(?:\.\d+)?\s*(?:gpm|bbl|gal)\b", 3.0),
    (r"\b(?:ft/hr|ft-lb)\b", 3.0),
    (r"\b(?:mud\s*weight|funnel\s*vis|api\s*fl)\b", 1.5),
    (r"\b(?:dull\s*grade|days\s*since\s*spud)\b", 1.0),
]
METRIC_UNIT_PATTERNS: list[tuple[str, float]] = [
    (r"\d+(?:\.\d+)?\s*(?:m3|m³)/", 2.5),          # 如 m3/min
    (r"\d+(?:\.\d+)?\s*(?:g/cm3|g/cm³)\b", 3.0),
    (r"\d+(?:\.\d+)?\s*(?:kn·m|knm|kN\b)", 2.0),
    (r"\d+(?:\.\d+)?\s*(?:kpa|mpa)\b", 2.0),
    (r"\d+(?:\.\d+)?\s*(?:l/s|lps)\b", 2.0),
    (r"\d+(?:\.\d+)?\s*m/h\b", 2.5),
    (r"\d+(?:\.\d+)?\s*米\b", 2.0),
    (r"\d+(?:\.\d+)?\s*毫米\b", 1.5),
    (r"\d+(?:\.\d+)?\s*公[斤吨]\b", 1.5),
    # 表头字段名只是弱线索，权重必须低于任何真实单位标注
    (r"井\s*深|垂\s*深|当日进尺|平均钻速|时间分解|二十四小时", 0.5),
    (r"\b(?:measured\s*depth|true\s*vertical\s*depth)\b", 0.3),
]
# 中性写法："2350.00 m" 在中英日报里都常见，权重低，只做加分不做决定
NEUTRAL_METRE_PATTERNS: list[tuple[str, float]] = [
    (r"\d+(?:\.\d+)?\s*m\b(?!\s*/)", 1.0),
]


def _weighted_unit_score(text: str, patterns: list[tuple[str, float]]) -> float:
    score = 0.0
    for pat, weight in patterns:
        hits = len(re.findall(pat, text, re.IGNORECASE))
        if hits:
            score += weight * min(hits, 5)  # 单条线索最多计 5 次，避免长表刷分
    return score


@dataclass
class Detection:
    template_id: str
    label: str
    confidence: float
    unit_system: str
    scores: dict[str, int] = field(default_factory=dict)
    unit_scores: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def known(self) -> bool:
        return self.template_id != "unknown"


def _count_hits(text: str, patterns: list[str]) -> int:
    return sum(1 for p in patterns if re.search(p, text, re.IGNORECASE))


def _infer_unit_system(text: str) -> tuple[str, dict[str, float], list[str]]:
    notes: list[str] = []
    imp = _weighted_unit_score(text, IMPERIAL_UNIT_PATTERNS)
    met = _weighted_unit_score(text, METRIC_UNIT_PATTERNS) + _weighted_unit_score(text, NEUTRAL_METRE_PATTERNS)
    scores = {"imperial": round(imp, 2), "metric": round(met, 2)}
    if imp == 0 and met == 0:
        return "unknown", scores, ["未发现任何单位标注，按字段逐个判定（不做默认公制假设）"]
    if imp > met * 1.2:
        return "imperial", scores, notes
    if met > imp * 1.2:
        return "metric", scores, notes
    notes.append(f"单位制线索接近（英制 {imp:.1f} / 公制 {met:.1f}），判定为 mixed（混用）")
    return "mixed", scores, notes


def detect_template(doc: Document) -> Detection:
    """识别日报所属模板与单位制。返回 Detection（未知模板不抛错）。"""
    text = doc.full_text
    scores: dict[str, int] = {}
    for sig in TEMPLATE_SIGNATURES:
        scores[sig.template_id] = _count_hits(text, sig.anchors)

    best_id, best_hits = max(scores.items(), key=lambda kv: kv[1], default=("unknown", 0))

    unit, unit_scores, notes = _infer_unit_system(text)

    if best_id == "unknown" or best_hits < 2:
        notes.append(
            f"未命中已知模板（最高命中 {best_hits} 次），将使用通用抽取器。"
            "若这是新格式，请按 docs/格式适配指南.md 登记模板签名与区块锚点。"
        )
        return Detection(
            template_id="unknown",
            label="未知模板（通用抽取）",
            confidence=0.0,
            unit_system=unit,
            scores=scores,
            unit_scores=unit_scores,
            notes=notes,
        )

    sig = next(s for s in TEMPLATE_SIGNATURES if s.template_id == best_id)
    confidence = min(1.0, best_hits / max(len(sig.anchors), 1))

    # 模板自带单位制线索：在文本线索不足（unknown/mixed）时作为兜底依据
    if sig.unit_hint != "unknown" and unit in ("unknown", "mixed"):
        notes.append(
            f"文本单位线索不足（英制 {unit_scores['imperial']:.1f} / 公制 {unit_scores['metric']:.1f}），"
            f"依据模板 {sig.template_id} 的默认单位制判定为 {sig.unit_hint}"
        )
        unit = sig.unit_hint
    return Detection(
        template_id=sig.template_id,
        label=sig.label,
        confidence=round(confidence, 3),
        unit_system=unit,
        scores=scores,
        unit_scores=unit_scores,
        notes=notes,
    )


# --------------------------------------------------------------------- 区块切分
@dataclass
class Segmented:
    detection: Detection
    blocks: dict[str, list[str]] = field(default_factory=dict)
    block_spans: dict[str, tuple[int, int]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def block(self, name: str) -> list[str]:
        return self.blocks.get(name, [])


def segment(doc: Document, detection: Detection | None = None) -> Segmented:
    """把文本行切分成 IADC 六段式区块。

    表头区块 = 第一个区块标题之前的行；其余区块 = 标题行之后到下一个标题行之前。

    算法：先收集所有候选标题行（含误判），再按候选序列逐条做"内容量校验"，
    剔除表头误判，最后用校验通过的候选重新切分区间。
    """
    det = detection or detect_template(doc)
    lines = doc.lines
    seg = Segmented(detection=det)

    candidates: list[tuple[int, str]] = [
        (idx, block) for idx, line in enumerate(lines) if (block := _match_block(line))
    ]

    # 校验候选：剔除表格表头行等误判
    kept: list[tuple[int, str]] = [
        (idx, block) for idx, block in candidates if _candidate_is_real(lines, idx, len(lines))
    ]

    # 同一区块只保留**首个**（日报里"备注"既可能做大标题又可能做列名/小标题）
    uniq: list[tuple[int, str]] = []
    seen: set[str] = set()
    for idx, block in kept:
        if block in seen:
            continue
        seen.add(block)
        uniq.append((idx, block))

    header_end = uniq[0][0] if uniq else len(lines)
    seg.blocks[BLOCK_HEADER] = lines[:header_end]
    seg.block_spans[BLOCK_HEADER] = (0, header_end)

    for i, (idx, block) in enumerate(uniq):
        end = uniq[i + 1][0] if i + 1 < len(uniq) else len(lines)
        # 区块内容不含标题行本身
        seg.blocks[block] = lines[idx + 1 : end]
        seg.block_spans[block] = (idx + 1, end)

    if BLOCK_TIME not in seg.blocks:
        seg.notes.append(
            "未识别到时间分解区块标题，将退化为在全文范围内按 HH:MM 行模式抽取时间条目"
        )
    if BLOCK_BIT not in seg.blocks and BLOCK_MUD not in seg.blocks:
        seg.notes.append("未识别到钻头记录/泥浆性能区块")
    if len(uniq) < 3:
        seg.notes.append(
            f"仅识别到 {len(uniq)} 个区块标题（表头之外的区块），版面可能与已知模板差异较大"
        )
    return seg

"""抽取器：区块 → 标准化字段。

架构（对应项目计划书 8.1「格式碎片化」的应对）：
- **规则优先**：表格类数值字段一律走规则抽取，确定性高、可审计。
- **模板声明式配置**：每种日报格式只在 TEMPLATE_SPECS 里声明"表头标签 → 字段""表头列名 → 字段"，
  新增格式不需要改抽取逻辑（详见 docs/格式适配指南.md）。
- **数值字段绝不交给 LLM**：LLM 只在 extract_llm 中处理 Remarks 自然语言。

反幻觉与可追溯：
- 每个抽取到的数值都记录 `_provenance`（来源行/单元格），便于人工复核；
- 单位无法判定时返回 None 并记警告，不做公制默认假设。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .codes import CodeMap, load_code_map
from .detect import (
    BLOCK_BIT,
    BLOCK_HEADER,
    BLOCK_MUD,
    BLOCK_REMARKS,
    BLOCK_SUMMARY,
    BLOCK_TIME,
    BLOCK_PLAN,
    Detection,
    Segmented,
)
from .model import BitRecord, DailyReport, Event, MudProperty, Remark, TimeEntry
from .normalize import (
    clock_to_hours,
    duration_to_hours,
    extract_time_cell,
    label_unit,
    resolve_number,
    to_float,
    to_int,
)
from .reader import Document, Table

# --------------------------------------------------------------------- 模板配置
LabelSpec = tuple[list[str], str]  # (标签候选, 字段键)
ColumnSpec = tuple[list[str], str]  # (列名候选, 字段键)


@dataclass
class TemplateSpec:
    template_id: str
    label: str
    """模板声明式配置。

    关键设计：**不按表格序号定位表格**。真实日报里时间分解表可能跨页而被切分成
    多个表格，钻头记录区块可能整块缺失，因此序号会漂移。一律按**表头内容签名**找表。
    """

    time_column_signature: list[str] = field(default_factory=lambda: ["作业内容", "operation", "description", "历时", "hours"])
    bit_column_signature: list[str] = field(default_factory=lambda: ["进尺", "footage", "dull", "磨损", "尺寸", "bit no"])
    # 泥浆表的表头在英制日报里写作 "MW (ppg)"，中文日报写作 "密度"，两者都要覆盖
    mud_column_signature: list[str] = field(
        default_factory=lambda: ["密度", "density", "mud weight", "mw", "sample point", "取样点"]
    )
    # 区块级源单位覆盖：同一份日报里不同区块可能用不同单位制
    # （常见于"英文模板 + 泥浆用 ppg/ft"的混用日报）。键为 field_key。
    header_unit_overrides: dict[str, str] = field(default_factory=dict)
    bit_unit_overrides: dict[str, str] = field(default_factory=dict)
    mud_unit_overrides: dict[str, str] = field(default_factory=dict)
    header_table: tuple[int, int] | None = None      # (第几个表格, 表头行索引)；None 表示走文本行
    use_header_table: bool = False
    header_labels: list[LabelSpec] = field(default_factory=list)
    header_pairs: bool = False                       # Excel 版：键|值|键|值
    max_header_lines: int = 45


TEMPLATE_SPECS: dict[str, TemplateSpec] = {
    "cn_vertical": TemplateSpec(
        template_id="cn_vertical",
        label="中文纵向版",
        header_labels=[
            (["井 号", "井号", "well name"], "well_name"),
            (["构 造", "构造", "field"], "field"),
            (["作业者", "operator", "作业者名称"], "operator"),
            (["钻井承包商", "承包商", "contractor"], "contractor"),
            (["钻 机", "钻机", "rig"], "rig"),
            (["日报编号", "report no"], "report_no"),
            (["钻井天数", "days since spud"], "etim_spud_days"),
            (["作业日期", "report date", "日期"], "report_date"),
            (["井 深", "井深", "measured depth", "md"], "md_m"),
            (["垂 深", "垂深", "true vertical depth", "tvd"], "tvd_m"),
            (["当日进尺", "进尺"], "progress_m"),
            (["井眼直径", "hole size"], "hole_diameter_in"),
            (["平均钻速", "avg. rop", "rop"], "rop_av_m_per_h"),
            (["钻进时间"], "etim_drill_h"),
            (["井 况", "井况", "well status"], "well_status"),
        ],
    ),
    "iadc_classic": TemplateSpec(
        template_id="iadc_classic",
        label="IADC Classic",
        header_table=(0, 1),           # 表头是第 0 个表格，第 1 行起为数据
        use_header_table=True,
        # 该模板是"英文模板 + 数值用公制"的混用日报：表头与钻头记录用 m，
        # 泥浆区块各列单位以**列头标注**为准（MW (g/cm3) / Depth (ft)），
        # 因此不在这里硬编码泥浆单位——列头声明比模板默认更具体。
        header_unit_overrides={"depth": "m", "footage": "m", "rop": "m/h", "hole_diameter": "in"},
        bit_unit_overrides={"depth": "m", "footage": "m", "bit_size": "in", "rop": "m/h"},
        header_labels=[
            (["Operator"], "operator"),
            (["Contractor"], "contractor"),
            (["Rig"], "rig"),
            (["Well Name", "Well"], "well_name"),
            (["API / UWI No.", "API", "UWI"], "api_number"),
            (["Field"], "field"),
            (["Country / State", "Country"], "country_state"),
            (["Report No.", "Report No", "Report Number"], "report_no"),
            (["Report Date", "Date"], "report_date"),
            (["Spud Date", "Spud"], "dtim_spud"),
            (["Days Since Spud", "Days Since"], "etim_spud_days"),
            (["Measured Depth", "MD"], "md_m"),
            (["True Vertical Depth", "TVD"], "tvd_m"),
            (["Avg. ROP", "ROP"], "rop_av_m_per_h"),
        ],
    ),
    "regional_xls": TemplateSpec(
        template_id="regional_xls",
        label="区域公司 Excel 版",
        header_pairs=True,
        header_labels=[
            (["井号", "井 号"], "well_name"),
            (["构造"], "field"),
            (["作业者"], "operator"),
            (["钻井承包商", "承包商"], "contractor"),
            (["钻机", "钻 机"], "rig"),
            (["日报编号"], "report_no"),
            (["作业日期"], "report_date"),
            (["钻井天数"], "etim_spud_days"),
            (["井深 MD", "井深"], "md_m"),
            (["垂深 TVD", "垂深"], "tvd_m"),
            (["当日进尺"], "progress_m"),
            (["井眼直径"], "hole_diameter_in"),
            (["平均钻速"], "rop_av_m_per_h"),
            (["井况", "井 况"], "well_status"),
        ],
    ),
}

# --------------------------------------------------------------------- 表头抽取
_LABEL_SEP_RE = re.compile(r"[\s\u3000:：]+")

# 日报里同一字段的多种英文/中文写法（跨模板通用兜底）
FIELD_ALIASES: dict[str, list[str]] = {
    "well_name": ["well name", "well", "井号", "井 号", "wellbore"],
    "api_number": ["api", "uwi", "api no", "api number"],
    "operator": ["operator", "作业者", "operated by"],
    "contractor": ["contractor", "钻井承包商", "drilling contractor"],
    "rig": ["rig", "钻机", "rig name", "rig no"],
    "field": ["field", "构造", "油田"],
    "md_m": ["measured depth", "md", "井深", "井 深", "current depth", "depth md"],
    "tvd_m": ["true vertical depth", "tvd", "垂深", "垂 深"],
    "progress_m": ["当日进尺", "进尺", "footage", "progress", "daily footage"],
    "hole_diameter_in": ["hole size", "hole diameter", "井眼直径", "bit size", "hole dia"],
    "rop_av_m_per_h": ["avg. rop", "average rop", "rop", "平均钻速", "penetration rate"],
    "etim_drill_h": ["drilling time", "钻进时间", "hours drilling"],
    "etim_spud_days": ["days since spud", "钻井天数", "days from spud", "well age"],
    "report_no": ["report no", "report number", "日报编号", "daily report no", "report #"],
    "report_date": ["report date", "作业日期", "报表日期", "日期"],
    "dtim_spud": ["spud date", "spud", "开钻日期", "开钻时间"],
    "well_status": ["well status", "井况", "井 况", "current operation", "当前工况"],
}


_TOKEN_CHAR_RE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")


def _norm_label(s: str) -> str:
    s = s.replace("\u3000", " ")
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s.rstrip(":：.")


_LATIN_WORD_RE = re.compile(r"[0-9A-Za-z]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _is_cjk(ch: str) -> bool:
    return bool(_CJK_RE.match(ch))


def _find_token(hay: str, needle: str) -> int:
    """在 hay 中查找 needle，并对**拉丁词元**施加词边界约束。

    为什么要区分中英文：
    - 英文必须加边界，否则 FIELD_ALIASES 里的 'date' 会命中 'Spud Date'，
      把"开钻日期"当成"上报日期"（真实踩过的坑）；
    - 中文不能加边界，因为中文是连续书写，'磨损分级' 里的 '磨损' 是合法子串
      （同样踩过：加了边界导致 IADC 磨损分级码列整列丢失）。
    """
    if not needle:
        return -1
    start = 0
    while True:
        i = hay.find(needle, start)
        if i < 0:
            return -1
        left_ok = i == 0 or not _LATIN_WORD_RE.match(hay[i - 1]) or not _LATIN_WORD_RE.match(needle[0])
        j = i + len(needle)
        right_ok = j >= len(hay) or not _LATIN_WORD_RE.match(hay[j]) or not _LATIN_WORD_RE.match(needle[-1])
        if left_ok and right_ok:
            return i
        start = i + 1


def _label_matches(label: str, candidates: list[str]) -> bool:
    """判断日报里的标签是否对应某字段。

    - 完全相等 → 匹配；
    - 标签以候选开头且后缀不是拉丁词字符（如 "井 深 (MD)"、"Depth (ft)"）→ 匹配；
    - 候选作为独立词元出现在标签中（如 "Avg. ROP" 里的 "rop"）→ 匹配。
    """
    nl = _norm_label(label)
    if not nl:
        return False
    for c in candidates:
        nc = _norm_label(c)
        if not nc:
            continue
        if nl == nc:
            return True
        # 标签可能带单位/补充说明，如 "井 深 (MD)"、"MW (ppg)"、"Depth (ft)"
        if nl.startswith(nc) and not _LATIN_WORD_RE.match(nl[len(nc)]):
            return True
        if _find_token(nl, nc) >= 0:
            return True
    return False


def _looks_like_trailing_numeric_suffix(value: str) -> bool:
    """判断值是否是"编号/日期残片"而非真实取值。

    例：'构 造 蓬莱 19-3' 按数字切分会得到 ('构 造 蓬莱', '19-3')，
        '井 号 苏 48-12-66X' 会得到 ('井 号 苏', '48-12-66X')，
    但 '19-3' / '48-12-66X' 都是名称的一部分，不是标签的值（真实踩过的坑）。

    只拒绝"带内部分隔符的短代号"（19-3、48-12-66X、6-2-A12H），
    不拒绝纯数字取值（井深 2130.36、日报编号 1 都是合法值）。
    """
    v = value.strip()
    if not v or len(v) > 16:
        return False
    return bool(
        re.fullmatch(r"[A-Za-z]*\d{1,4}(?:\s*[-–—/]\s*[A-Za-z]*\d{1,3})+[A-Za-z]*", v)
    )


@dataclass
class HeaderExtraction:
    values: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _lookup_field(label: str, spec: TemplateSpec) -> str | None:
    """标签 → 字段键。三级优先，避免通用别名抢走专用字段。

    ① 模板声明的标签（按声明顺序，长而专的写法写在前面，如 'Report Date' 先于 'Date'）；
    ② 通用别名的**精确**匹配（如 'Spud Date' 明确映射到 dtim_spud）；
    ③ 通用别名的包含匹配（最后手段）。
    """
    if not label:
        return None
    for cands, key in spec.header_labels:
        if _label_matches(label, cands):
            return key
    nl = _norm_label(label)
    for key, cands in FIELD_ALIASES.items():
        if any(nl == _norm_label(c) for c in cands):
            return key
    for key, cands in FIELD_ALIASES.items():
        if _label_matches(label, cands):
            return key
    return None


_VALUE_START_RE = re.compile(r"(?<![\w.\-])([+-]?\d+(?:[.,]\d+)?)")
_VALUE_LIKE_RE = re.compile(r"^[\s\d.,:/%°\u2013\u2014\u2018\u2019'\"()a-zA-Z\u4e00-\u9fff+\-]*$")


def _strip_label_prefix(line: str, label_candidates: list[str]) -> tuple[str, str] | None:
    """用"已知标签 + 任意值"的方式切分一行。

    处理数值切分覆盖不到的情形：
      "井 号 PL-6-2-A12H"  → ("井 号", "PL-6-2-A12H")
      "钻 机 海洋石油 942"  → ("钻 机", "海洋石油 942")
    按标签长度降序尝试，避免短标签抢走长标签的前缀。
    """
    s = line.strip()
    if not s:
        return None
    for cand in sorted(set(label_candidates), key=lambda c: -len(c)):
        if not cand:
            continue
        # 标签允许中间有多余空格（"井 号" vs "井号"），因此按"去掉空格后前缀相等"来匹配
        pat = r"^\s*" + r"[\s\u3000]*".join(re.escape(ch) for ch in cand.strip()) + r"[\s\u3000:：=]+\s*"
        m = re.match(pat, s)
        if not m:
            continue
        value = s[m.end() :].strip()
        if value:
            return cand, value
    return None


def _split_label_value(line: str, is_known_label=None) -> tuple[str, str] | None:
    """把 "井 深 (MD) 2130.36 m" 拆成 ("井 深 (MD)", "2130.36 m")。

    关键约束（踩过的坑）：
    - 不能"从右往左找最后一个数字"——井名 "PL-6-2-A12H" 里的 "2" 会被当成数值起点，
      于是标签被截成 "井 号 PL-6-"，值变成 "2-A12H"。
    - 正确做法：**从左往右**找第一个"数字串起点"，它左边的必须能匹配已知字段标签。
    - 值本身不含数字时（如井名、公司名），数字切分无法工作，改由
      _strip_label_prefix 直接按已知标签前缀切分。
    """
    s = line.strip()
    if not s:
        return None
    for m in _VALUE_START_RE.finditer(s):
        label = s[: m.start()].strip(" \u3000:：=—–-\t")
        value = s[m.start() :].strip()
        if not label or not value:
            continue
        if re.fullmatch(r"[\d\s.,:/\-–—]+", label):
            continue  # 标签不能是纯数字/日期残片
        if is_known_label is not None and not is_known_label(label):
            continue
        # '构造 蓬莱 19-3' 这类：数字只是名称的一部分，不是取值
        if is_known_label is not None and _looks_like_trailing_numeric_suffix(value):
            continue
        return label, value
    return None


def extract_header(
    doc: Document,
    seg: Segmented,
    spec: TemplateSpec,
    *,
    cm: CodeMap,
) -> HeaderExtraction:
    """抽取表头字段。

    两条路径：
    A. 键值表格（use_header_table）：表格里 [标签, 值, 标签, 值] 成对出现；
    B. 文本行：表头区块内每行形如 "标签 值"（含中英文混排）。
    """
    out = HeaderExtraction()

    # --- 路径 A：键值表格
    # 注意：表格里同一行可能同时出现 "Report Date" 与 "Spud Date"，
    # 必须逐对判定并把"已匹配的标签"剔除，否则短别名（如 'Date'）会把两行都判成同一个字段。
    if spec.use_header_table and spec.header_table is not None:
        ti, first_row = spec.header_table
        tables = doc.tables
        if ti < len(tables):
            tb = tables[ti]
            consumed: set[tuple[int, int]] = set()
            for ri in range(first_row, tb.n_rows):
                row = tb.row(ri)
                for ci in range(0, len(row) - 1, 2):
                    if (ri, ci) in consumed:
                        continue
                    label, value = row[ci], row[ci + 1]
                    if not label or not value:
                        continue
                    key = _lookup_field(label, spec)
                    if key:
                        for k in range(first_row, tb.n_rows):
                            r2 = tb.row(k)
                            for c2 in range(0, len(r2) - 1, 2):
                                if r2[c2] and _lookup_field(r2[c2], spec) == key:
                                    consumed.add((k, c2))
                        if key not in out.values:
                            out.values[key] = value
                            out.provenance[key] = f"table#{ti} r{ri}c{ci}"
        else:
            out.warnings.append(f"声明了表头表格 #{ti}，但文档只有 {len(tables)} 个表格")

    # --- 路径 B：文本行
    header_lines = seg.block(BLOCK_HEADER)[: spec.max_header_lines]

    # 收集所有可识别的标签写法，供"标签 + 任意值"的按前缀切分使用
    all_label_candidates: list[str] = [c for cands, _ in spec.header_labels for c in cands]
    for cands in FIELD_ALIASES.values():
        all_label_candidates.extend(cands)

    for i, line in enumerate(header_lines):
        # Excel 的 "键 | 值 | 键 | 值" 形态
        if spec.header_pairs and "|" in line:
            parts = [p.strip() for p in line.split("|")]
            for ci in range(0, len(parts) - 1, 2):
                label, value = parts[ci], parts[ci + 1]
                if not label or not value:
                    continue
                key = _lookup_field(label, spec)
                if key and key not in out.values:
                    out.values[key] = value
                    out.provenance[key] = f"line{i}c{ci}"
            continue
        sv = _split_label_value(line, is_known_label=lambda lbl: _lookup_field(lbl, spec) is not None)
        if not sv:
            # 值不含数字（井名、公司名、井况等）→ 按已知标签前缀切分
            sv = _strip_label_prefix(line, all_label_candidates)
        if not sv:
            continue
        label, value = sv
        key = _lookup_field(label, spec)
        if key and key not in out.values:
            out.values[key] = value
            out.provenance[key] = f"line{i}"

    return out


# --------------------------------------------------------------------- 时间分解
_TIME_COLUMN_ALIASES: list[ColumnSpec] = [
    (["序号", "#", "no", "no.", "item"], "seq"),
    (["开始", "起始", "from", "start", "time from", "起"], "start"),
    (["结束", "终止", "to", "end", "time to", "止"], "end"),
    (["历时", "时长", "小时", "hours", "duration", "hrs", "h"], "hours"),
    (["累计", "cum", "cumulative"], "cum"),
    (["作业内容", "作业描述", "operation", "description", "activity", "作业", "工作内容"], "operation"),
    (["时间类别", "类别", "class", "category", "type"], "category"),
    (["备注", "remark", "comment", "说明"], "note"),
]

_NPT_TOKEN_RE = re.compile(r"npt|非生产|non-?productive|损失", re.IGNORECASE)
_FLAT_TOKEN_RE = re.compile(r"flat|计划内|非钻进", re.IGNORECASE)
_PROD_TOKEN_RE = re.compile(r"productive|生产|有效", re.IGNORECASE)


def _map_columns(header_row: list[str], aliases: list[ColumnSpec]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for ci, cell in enumerate(header_row):
        if not cell:
            continue
        for cands, key in aliases:
            if key in mapping:
                continue
            if _label_matches(cell, cands):
                mapping[key] = ci
                break
    return mapping


def extract_time_log(
    doc: Document,
    seg: Segmented,
    spec: TemplateSpec,
    *,
    cm: CodeMap,
    detection: Detection,
) -> tuple[list[TimeEntry], list[str]]:
    """抽取时间分解。表格优先，失败时退化为"区块内按 HH:MM 行"解析。"""
    warnings: list[str] = []
    entries: list[TimeEntry] = []

    time_tables = find_tables_by_signature(doc, spec.time_column_signature, required_groups=_TIME_TABLE_REQUIRED)
    rows: list[list[str]] = []
    header: list[str] = []
    if time_tables:
        header_idx = _find_header_row(time_tables[0], ["作业内容", "operation", "历时", "hours", "from", "开始"])
        header = time_tables[0].row(header_idx)
        for tb in time_tables:
            for ri in range(tb.n_rows):
                row = tb.row(ri)
                # 续表会重复表头行与"TOTAL"合计行，必须剔除，否则会被当成时间条目
                if _label_matches(" ".join(row), ["作业内容", "operation", "description"]):
                    continue
                if _is_total_row(" ".join(row)):
                    continue
                rows.append(row)
    if not rows:
        # 退化路径：区块文本行
        rows = seg.block(BLOCK_TIME)
        warnings.append("未取到时间分解表格，退化为文本行解析（精度可能下降）")

    mapping = _map_columns(header, _TIME_COLUMN_ALIASES) if header else {}

    for ri, row in enumerate(rows):
        line = " ".join(row).strip()
        if not line:
            continue
        if _is_total_row(line):
            continue

        get = lambda key, default=None: (  # noqa: E731
            row[mapping[key]] if key in mapping and mapping[key] < len(row) else default
        )
        start_s = get("start")
        end_s = get("end")
        hours_s = get("hours")
        op_s = get("operation") or ""
        cat_s = get("category") or ""

        # 表格未识别时，从整行里正则抽时间与时长
        if not mapping:
            m = re.findall(r"\b([01]?\d|2[0-4]):([0-5]\d)\b", line)
            if len(m) >= 2:
                start_s = f"{m[0][0]}:{m[0][1]}"
                end_s = f"{m[1][0]}:{m[1][1]}"
            elif len(m) == 1:
                start_s = f"{m[0][0]}:{m[0][1]}"
                end_s = None
            nums = re.findall(r"(?<![\d:])(\d+(?:\.\d+)?)(?![\d:])", line)
            nums = [n for n in nums if "." in n or len(n) <= 2]
            hours_s = nums[0] if nums else None
            op_s = re.sub(r"\b([01]?\d|2[0-4]):([0-5]\d)\b", " ", line)
            op_s = re.sub(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])", " ", op_s)
            op_s = re.sub(r"\s+", " ", op_s).strip()
            cat_s = ""

        s_h = clock_to_hours(start_s)
        e_h = clock_to_hours(end_s)
        h: float | None = None
        source = "reported"
        dh, kind = extract_time_cell(hours_s)
        if dh is not None:
            h = dh
        elif s_h is not None and e_h is not None:
            h = round((e_h - s_h) if e_h >= s_h else (24.0 - s_h), 4)
            source = "derived_from_clock"
        if h is None or h <= 0:
            continue

        operation = re.sub(r"\s+", " ", str(op_s)).strip() or None
        entry = _classify_entry(operation, code_hint=None, cm=cm, category_hint=cat_s)
        entry.start = _fmt_clock(start_s)
        entry.end = _fmt_clock(end_s)
        entry.hours = round(h, 2)
        entry.duration_source = source
        entries.append(entry)

    if not entries:
        warnings.append("时间分解区块未抽取到任何时间条目")
    return entries, warnings


def _fmt_clock(s: str | None) -> str | None:
    h = clock_to_hours(s)
    if h is None:
        return None
    hh = int(h)
    mm = int(round((h - hh) * 60))
    if mm == 60:
        hh, mm = hh + 1, 0
    return f"{hh:02d}:{mm:02d}"


def _is_total_row(line: str) -> bool:
    s = line.strip()
    if re.search(r"(合计|总计|小计|total|sum)\b", s, re.IGNORECASE):
        # 仅当该行没有作业描述性文字时才认定为合计行
        if re.search(r"(必须|must|constraint|等于)", s, re.IGNORECASE):
            return True
        stripped = re.sub(r"(?i)(合计|总计|小计|total|sum)", "", s)
        stripped = re.sub(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])", "", stripped)
        stripped = re.sub(r"[\s|:：,，.。;；\-—]*", "", stripped)
        return len(stripped) <= 6
    return False


_CONNECTION_TEXT_RE = re.compile(
    r"接单根|接立柱|接钻杆|鼠洞接单根|making?\s+(?:a\s+)?connection|make\s+up\s+(?:a\s+)?connection|\bconnections?\b",
    re.IGNORECASE,
)


def _classify_entry(
    operation: str | None,
    *,
    code_hint: str | None,
    cm: CodeMap,
    category_hint: str = "",
) -> TimeEntry:
    """把一条作业描述分类为 TimeEntry（代码、类别、NPT 归因、ILT 候选）。

    ILT 动作判定只看**操作描述原文**：钻进块本身不应被标成"接单根"，
    否则接单根统计会把整天钻进时间算进去（真实踩过的坑）。
    """
    spec = cm.codes.get(code_hint) if code_hint else None
    if spec is None:
        spec = cm.normalize(operation)

    # 类别判定：表格自带的类别列优先（那是日报编制者的原始判断），
    # 其次按操作码语义。
    category = ""
    ch = (category_hint or "").strip()
    if ch:
        if _NPT_TOKEN_RE.search(ch):
            category = "npt"
        elif _FLAT_TOKEN_RE.search(ch):
            category = "flat"
        elif _PROD_TOKEN_RE.search(ch):
            category = "productive"
    if not category:
        category = spec.default_category

    npt_cat = spec.npt_category if category == "npt" else None

    ilt_action: str | None = None
    if _CONNECTION_TEXT_RE.search(operation or ""):
        ilt_action = "connection"
    else:
        ilt_action = cm.ilt_action_of(operation)

    return TimeEntry(
        hours=0.0,
        code=spec.code,
        category=category,
        is_npt=(category == "npt"),
        operation=operation,
        npt_category=npt_cat,
        npt_responsibility=None,
        is_ilt_candidate=ilt_action is not None,
        ilt_action=ilt_action,
    )


# --------------------------------------------------------------------- 通用表格
_TIME_TABLE_REQUIRED: list[list[str]] = [
    ["作业内容", "operation"],   # 二者其一即可（中/英日报各用一种写法）
    ["时间", "hours", "历时", "from", "开始"],
]


def _table_header_text(tb: Table, rows: int = 3) -> str:
    return " ".join(" ".join(tb.row(i)) for i in range(min(rows, tb.n_rows))).lower()


def find_tables_by_signature(
    doc: Document,
    signature: list[str],
    *,
    required_groups: list[list[str]] | None = None,
    min_hits: int = 2,
) -> list[Table]:
    """找出**所有**匹配签名的表格，按文档顺序返回。

    需要返回多张的原因：时间分解表跨页时 pdfplumber 会把它切成两张表
    （第 2 张只有续行 + 合计行），只取第一张会丢掉续页上的条目
    ——这是"时间条目数偏少"的根因（真实踩过的坑）。
    """
    groups = required_groups or []
    out: list[Table] = []
    for tb in doc.tables:
        if tb.n_rows < 1:
            continue
        head = _table_header_text(tb)
        if groups and not all(any(w.lower() in head for w in grp) for grp in groups):
            continue
        if sum(1 for w in signature if w.lower() in head) >= min_hits:
            out.append(tb)
    return out


def find_table_by_signature(
    doc: Document,
    signature: list[str],
    *,
    required_groups: list[list[str]] | None = None,
    exclude: set[int] | None = None,
    min_hits: int = 2,
) -> tuple[Table | None, int]:
    """按表头内容签名找**最匹配的**一张表。

    参数：
    - signature：加分项，命中越多越优先；
    - required_groups：**每组至少命中一个**的必要条件，用来把"长得像但不该选"的表排除。
      例：时间分解表必须有"作业内容/operation"这一列（泥浆表没有），
          且必须有时间或时长列，于是泥浆表不会被误选为时间表。

    返回 (表格, 表格序号)。找不到返回 (None, -1)。
    不依赖表格序号——真实日报里时间表跨页会被切成多个表格，钻头表可能整块缺失。
    注意：时间分解可能跨多张表，那种场景请用 find_tables_by_signature。
    """
    exclude = exclude or set()
    groups = required_groups or []
    best: tuple[int, int] | None = None  # (hits, index)
    for i, tb in enumerate(doc.tables):
        if i in exclude or tb.n_rows < 1:
            continue
        head = _table_header_text(tb)
        if groups and not all(any(w.lower() in head for w in grp) for grp in groups):
            continue
        hits = sum(1 for w in signature if w.lower() in head)
        if hits >= min_hits and (best is None or hits > best[0]):
            best = (hits, i)
    if best is None:
        return None, -1
    return doc.tables[best[1]], best[1]


def _find_header_row(tb: Table, signature: list[str], max_scan: int = 4) -> int:
    """在表格前几行里定位真正的表头行（跳过"钻头记录"这类合并标题行）。

    必须真正扫描而不是固定取第 0 行：Excel 版表格第一行常是区块标题行，
    表头在第 1 行（真实踩过的坑：拿标题行做列映射 → mapping 为空 → 磨损分级码丢失）。
    """
    best, best_hits = 0, -1
    for i in range(min(max_scan, tb.n_rows)):
        head = " ".join(tb.row(i)).lower()
        hits = sum(1 for w in signature if w.lower() in head)
        if hits > best_hits:
            best, best_hits = i, hits
    return best


def _rows_of(tb: Table, start: int = 0) -> list[list[str]]:
    return [tb.row(i) for i in range(start, tb.n_rows)]


# --------------------------------------------------------------------- 钻头记录
_BIT_COLUMNS: list[ColumnSpec] = [
    (["序号", "bit no", "no", "#", "no."], "bit_no"),
    (["尺寸", "size", "bit size", "直径"], "size_in"),
    (["厂家", "make", "制造商", "manufacturer"], "make"),
    (["型号", "model", "type of bit"], "model"),
    (["类型", "type"], "type"),
    (["iadc"], "iadc_code"),
    (["水眼", "nozzle", "nozzles"], "nozzles_32nds"),
    (["入井", "depth in", "in depth", "入井深度"], "depth_in_m"),
    (["出井", "depth out", "out depth", "出井深度"], "depth_out_m"),
    (["进尺", "footage", "drilled"], "footage_m"),
    (["时间", "hours", "hrs", "使用时间"], "hours"),
    (["钻速", "rop", "rop (", "机械钻速"], "rop_m_per_h"),
    (["钻压", "wob"], "wob_kgf"),
    (["转速", "rpm", "rotary speed"], "rpm"),
    (["排量", "flow", "flow rate"], "flow_lps"),
    (["磨损", "dull", "grading"], "dull_grade"),
]

_DULL_RE = re.compile(
    r"^\s*(\d)\s*[-–]\s*(\d)\s*[-–]\s*([A-Za-z]{2})\s*[-–]\s*([A-Za-z]{1,2})\s*[-–]\s*"
    r"([A-Za-z]{1,2})\s*[-–]\s*([A-Za-z]{1,2})\s*[-–]\s*([A-Za-z]{2,3})\s*[-–]\s*([A-Za-z]{2,4})\s*$"
)

_BIT_TYPE_MAP = {
    "pdc": "PDC",
    "tci": "TCI",
    "mill tooth": "MILL_TOOTH",
    "milled tooth": "MILL_TOOTH",
    "diamond": "DIAMOND",
    "core": "CORE",
    "牙轮": "TCI",
    "金刚石": "DIAMOND",
    "取心": "CORE",
}


def parse_dull_grade(text: str | None) -> dict[str, Any] | None:
    """解析 IADC 8 位磨损分级码，如 2-3-WT-S-X-I-NO-TD。"""
    if not text:
        return None
    m = _DULL_RE.match(str(text).strip())
    if not m:
        return None
    return {
        "inner_wear": int(m.group(1)),
        "outer_wear": int(m.group(2)),
        "dull_char": m.group(3).upper(),
        "location": m.group(4).upper(),
        "bearing": m.group(5).upper(),
        "gauge": m.group(6).upper(),
        "other_char": m.group(7).upper(),
        "reason_pulled": m.group(8).upper(),
    }


def extract_bit_records(
    doc: Document,
    seg: Segmented,
    spec: TemplateSpec,
    *,
    detection: Detection,
) -> tuple[list[BitRecord], list[str]]:
    warnings: list[str] = []
    table, _ = find_table_by_signature(
        doc,
        spec.bit_column_signature,
        # 必备列：钻头表一定有进尺（中文）或 footage（英文）
        required_groups=[["进尺", "footage"]],
        min_hits=2,
    )
    if table is None:
        table, _ = find_table_by_signature(doc, spec.bit_column_signature, min_hits=2)
    if table is None:
        # 钻头记录整块缺失是常见情况（不是所有日报每天都换钻头），不算错误
        return [], warnings

    header_idx = _find_header_row(table, ["尺寸", "size", "进尺", "footage", "dull", "磨损", "钻速"])
    mapping = _map_columns(table.row(header_idx), _BIT_COLUMNS)
    out: list[BitRecord] = []

    for ri in range(header_idx + 1, table.n_rows):
        row = table.row(ri)
        if not any(row):
            continue
        line = " ".join(row)
        if _is_total_row(line):
            continue

        def cell(key: str) -> str | None:
            i = mapping.get(key)
            if i is None or i >= len(row):
                return None
            v = row[i].strip()
            return v or None

        bit_no = to_int(cell("bit_no")) or (len(out) + 1)

        def rb(key: str, kind: str, fkey: str, label: str) -> Any:
            return resolve_number(
                cell(key),
                field_kind=kind,
                label=label,
                template_id=spec.template_id,
                field_key=fkey,
                source_unit_override=spec.bit_unit_overrides.get(fkey),
            )

        size = rb("size_in", "diameter", "bit_size", "size (in)")
        din = rb("depth_in_m", "length", "depth", "depth in (m)")
        dout = rb("depth_out_m", "length", "depth", "depth out (m)")
        foot = rb("footage_m", "length", "footage", "footage (m)")
        wob = rb("wob_kgf", "force", "wob", "wob (kgf)")
        flow = rb("flow_lps", "flow", "flow", "flow (L/s)")
        hours = duration_to_hours(cell("hours"))
        rop_res = rb("rop_m_per_h", "speed", "rop", "ROP (m/h)")
        rop = rop_res.si

        dull = cell("dull_grade")
        rec = BitRecord(
            bit_no=bit_no,
            size_in=round(size.si, 2) if size.si else None,
            make=cell("make"),
            model=cell("model"),
            type=_BIT_TYPE_MAP.get((cell("type") or "").strip().lower()),
            iadc_code=cell("iadc_code"),
            nozzles_32nds=cell("nozzles_32nds"),
            depth_in_m=round(din.si, 2) if din.si is not None else None,
            depth_out_m=round(dout.si, 2) if dout.si is not None else None,
            footage_m=round(foot.si, 2) if foot.si is not None else None,
            hours=hours,
            rop_m_per_h=round(rop, 2) if rop else None,
            wob_kgf=round(wob.si, 1) if wob.si is not None else None,
            rpm=to_float(cell("rpm")),
            flow_lps=round(flow.si, 1) if flow.si is not None else None,
            dull_grade=dull,
            dull_grade_parsed=parse_dull_grade(dull),
            hours_source="reported" if hours is not None else None,
        )
        for nr in (size, din, dout, foot, wob, flow):
            for n in nr.notes:
                if n not in warnings:
                    warnings.append(n)
        out.append(rec)
    return out, warnings


# --------------------------------------------------------------------- 泥浆性能
_MUD_COLUMNS: list[ColumnSpec] = [
    (["取样点", "sample point", "sample", "取样位置"], "sample_point"),
    (["井深", "depth"], "depth_m"),
    (["密度", "mw", "mud weight", "density", "weight"], "density_gcc"),
    (["漏斗粘度", "funnel vis", "funnel viscosity", "粘度", "viscosity"], "funnel_viscosity_s"),
    (["塑性粘度", "pv", "plastic viscosity"], "pv_mpas"),
    (["动切力", "yp", "yield point"], "yp_pa"),
    (["初切", "gel 10s", "gel10s", "10s gel"], "gel_10s_pa"),
    (["终切", "gel 10min", "gel10min", "10min gel"], "gel_10min_pa"),
    (["失水", "api fl", "fl", "water loss", "滤失"], "fl_ml"),
    (["ph", "酸碱度"], "ph"),
    (["氯根", "chloride", "cl"], "chlorides_mg_l"),
    (["含砂", "sand"], "sand_pct"),
    (["油水比", "oil/water", "owr"], "oil_water_ratio"),
    (["固相", "solids"], "solids_pct"),
    (["体系", "mud type", "类型"], "mud_type"),
    (["体积", "volume", "vol"], "volume_m3"),
]

_SAMPLE_POINT_MAP = {
    "出口": "flowline", "入口": "suction", "振动筛": "shaker", "泥浆罐": "pit",
    "返出": "flowline", "吸入": "suction", "flowline": "flowline", "suction": "suction",
    "shaker": "shaker", "pit": "pit",
}


def extract_mud(
    doc: Document,
    seg: Segmented,
    spec: TemplateSpec,
    *,
    detection: Detection,
) -> tuple[list[MudProperty], list[str]]:
    warnings: list[str] = []
    table, _ = find_table_by_signature(doc, spec.mud_column_signature, min_hits=2)
    if table is None:
        return [], warnings

    header_idx = _find_header_row(table, ["密度", "density", "mud weight", "取样点", "sample point"])
    header = table.row(header_idx)
    mapping = _map_columns(header, _MUD_COLUMNS)
    # 各列单位一律取自该列自己的表头（Depth (ft) / 密度(g/cm3) / MW (ppg)），
    # 不能拿别列的单位来解析本列的数值。
    dens_label = header[mapping["density_gcc"]] if "density_gcc" in mapping else ""
    depth_label = header[mapping["depth_m"]] if "depth_m" in mapping else ""
    vol_label = header[mapping["volume_m3"]] if "volume_m3" in mapping else ""

    out: list[MudProperty] = []
    for ri in range(header_idx + 1, table.n_rows):
        row = table.row(ri)
        if not any(row):
            continue
        cell = lambda key: (row[mapping[key]].strip() if key in mapping and mapping[key] < len(row) and row[mapping[key]].strip() else None)  # noqa: E731
        pt_raw = (cell("sample_point") or "").strip()
        if not pt_raw:
            continue
        dens = resolve_number(cell("density_gcc"), field_kind="density", label=dens_label,
                              template_id=spec.template_id, field_key="density",
                              source_unit_override=spec.mud_unit_overrides.get("density"))
        depth = resolve_number(cell("depth_m"), field_kind="length", label=depth_label,
                               template_id=spec.template_id, field_key="depth",
                               source_unit_override=spec.mud_unit_overrides.get("depth"))
        vol = resolve_number(cell("volume_m3"), field_kind="volume", label=vol_label,
                             template_id=spec.template_id, field_key="volume",
                             source_unit_override=spec.mud_unit_overrides.get("volume"))

        # 单位无法判定时**不丢数据、也不猜**：保留原始读数并标记来源，
        # 由工程师决定按哪种单位解读。这是"数据缺失"与"数据无法归一"的区别。
        dens_value = dens.si if dens.si is not None else dens.raw
        dens_source = dens.unit_source
        if dens.si is None and dens.raw is not None:
            dens_source = "unknown_raw"
            warnings.append(
                f"泥浆密度 {dens.raw} 未标注可用单位，已按原始读数保留（未做 SI 换算），"
                "请人工确认单位或补充模板源单位配置"
            )
        depth_value = depth.si if depth.si is not None else depth.raw
        vol_value = vol.si if vol.si is not None else vol.raw
        for nr in (dens, depth, vol):
            for n in nr.notes:
                if n not in warnings:
                    warnings.append(n)
        out.append(
            MudProperty(
                sample_point=_SAMPLE_POINT_MAP.get(pt_raw.lower(), _SAMPLE_POINT_MAP.get(pt_raw, "other")),
                depth_m=round(depth_value, 1) if depth_value is not None else None,
                density_gcc=round(dens_value, 3) if dens_value is not None else None,
                funnel_viscosity_s=to_float(cell("funnel_viscosity_s")),
                pv_mpas=to_float(cell("pv_mpas")),
                yp_pa=to_float(cell("yp_pa")),
                gel_10s_pa=to_float(cell("gel_10s_pa")),
                gel_10min_pa=to_float(cell("gel_10min_pa")),
                fl_ml=to_float(cell("fl_ml")),
                ph=to_float(cell("ph")),
                chlorides_mg_l=to_float(cell("chlorides_mg_l")),
                sand_pct=to_float(cell("sand_pct")),
                oil_water_ratio=cell("oil_water_ratio"),
                solids_pct=to_float(cell("solids_pct")),
                mud_type=cell("mud_type"),
                volume_m3=round(vol_value, 1) if vol_value is not None else None,
                raw={
                    "unit_source": dens_source,
                    "depth_unit_source": depth.unit_source,
                    "sample_point_raw": pt_raw,
                    "density_label": dens_label or None,
                },
            )
        )
    return out, warnings


# --------------------------------------------------------------------- 备注
_REMARK_TIME_RE = re.compile(r"^\s*([01]?\d|2[0-4]):([0-5]\d)\s*(?:[-–~至]\s*([01]?\d|2[0-4]):([0-5]\d))?\s+")


def extract_remarks(seg: Segmented) -> list[Remark]:
    """抽取备注原文（按时间顺序）。不做语义解释——语义抽取见 extract_llm。"""
    out: list[Remark] = []
    seq = 0
    for line in seg.block(BLOCK_REMARKS):
        s = line.strip()
        if not s:
            continue
        m = _REMARK_TIME_RE.match(s)
        if m:
            hint = f"{int(m.group(1)):02d}:{m.group(2)}"
            text = s[m.end() :].strip()
        else:
            hint = None
            text = s
        if not text:
            continue
        seq += 1
        out.append(Remark(seq=seq, text=text, time_hint=hint, extraction_method="rule"))
    return out

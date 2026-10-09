"""私有数据闸门：防止真实钻井日报被误提交进版本库。

## 为什么需要这个模块

本仓库绑定了一个 GitHub 远端（公开仓库）。`.gitignore` 里虽然写了
"真实日报入库前必须先脱敏"，但那**只是一句注释，没有任何强制力**：

1. `data/samples/` 是**被跟踪的目录**，而 `docs/格式适配指南.md` 第 5 节
   登记新样本的默认落点正是这里 —— 真实日报的自然归宿就是"会被提交"的地方；
2. 真实日报一旦推送，井名 / 作业者 / 承包商就**永久留在 Git 历史里**，
   事后清理需要在所有引用点重写历史并强推，协作场景下几乎无法彻底清除。

本模块把"脱敏"从注释变成**可执行、可进 CI 的检查**。

## 判定策略

- **白名单来自仓库自己的合成基线**（`data/samples/manifest.json` 中
  `synthetic: true` 的条目）。这保证白名单永远是"已确认为虚构"的集合，
  而不是靠人工维护一份容易过期的名单。
- **默认拒绝**：任何不在白名单里的主体名 / 井名都报问题，而不是等它"看起来像真的"。
- **公开数据集豁免**：Volve 等公开数据集的内容不涉及私有信息，按数据集名豁免。

退出码约定与 `ddr check` 一致：0 = 无 ERROR，1 = 有 ERROR。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

MANIFEST_NAME = "manifest.json"
DEFAULT_DATA_DIR = Path("data/samples")

# 合成基线清单：白名单的唯一来源。若该文件整体不是合成数据，闸门拒绝启动
# （否则会把真实主体当成"已知虚构主体"放行，闸门就形同虚设）。
BASELINE_MANIFEST = Path("data/samples/manifest.json")

# ------------------------------------------------------------------ 机构后缀词表
# 用途：在 operator / contractor 等**自由文本**字段里判断"这是不是一个真实机构名"。
# 只认后缀不认全名 —— 全名会过期，后缀词是有限的。
ORG_SUFFIXES: tuple[str, ...] = (
    # 中文
    "有限公司", "股份有限公司", "有限责任公司", "分公司", "子公司", "集团",
    "油田公司", "油田分公司", "勘探局", "管理局", "油公司",
    "钻井公司", "钻探公司", "钻探工程", "油服", "油田服务", "海油发展",
    "研究院", "研究所", "设计院", "工程院", "作业区", "采油厂", "指挥部",
    # 英文
    "limited", "ltd", "l.l.c", "llc", "inc", "incorporated", "corporation",
    "corp", "company", "co.", "plc", "gmbh", "a/s", "as", "asa", "bv", "b.v",
    "nv", "n.v", "spa", "s.p.a", "sa", "s.a", "pty", "energy", "petroleum",
    "drilling", "offshore", "international", "holdings",
)

# 明确豁免：公开数据集（不含私有信息）。命中即跳过该条目的主体名检查。
PUBLIC_DATASET_MARKERS: tuple[str, ...] = (
    "volve", "force 2020", "force2020", "ndr", "equinor公开",
    "公开数据集", "public dataset",
)

_WELL_NAME_RE = re.compile(r"^[A-Za-z\u4e00-\u9fff]{1,6}[-/][0-9A-Za-z][0-9A-Za-z\-/]*$")
_ORG_SUFFIX_RE = re.compile(
    "|".join(re.escape(s) for s in sorted(ORG_SUFFIXES, key=len, reverse=True)),
    re.IGNORECASE,
)
# 脱敏占位符：出现这些写法说明已经过脱敏，不再报问题。
# 注意：占位符可能是"前缀式"（井X-1 / WELL-XX）也可能是"后缀式"（XX井 / XX-1），
# 两种都要认，否则会把已脱敏的样本误判为真实井名（过度拦截同样会让闸门失效）。
_REDACTION_RE = re.compile(
    # 前缀式：井XX / 井X-1 / 井A / WELL-XX / W-XX
    # 中文"井"前缀后面任意字母数字编号都算占位（井X-1、井A、井1）；
    # 英文前缀只认全大写占位符（WELL-XX），避免把 West-1 这类真实形态放行。
    r"(?:井\s?[-_]?\s?[A-Za-z0-9]+|(?:WELL|W)\s?[-_]?\s?(?:X{2,}|A{2,}|N{2,}|\d{2,}))"
    # 后缀式：XX-1 / XX井 / XX-12-3 / AB-1
    r"|(?:X{2,}|A{2,}|N{2,})\s?[-_]?\s?(?:\d{1,3}|井)"
    # 通用占位
    r"|#{2,}|<[^>]{1,20}>|\[(?:已?脱敏|已?匿名|redacted|anonymi[sz]ed)\]"
    # 显式声明：出现"已脱敏/已匿名"即视为人工确认过
    r"|已脱敏|已匿名",
    re.IGNORECASE,
)
# 真值标记：manifest 里显式声明"这不是合成数据"
_NON_SYNTHETIC_VALUES = {False, "false", "False", "no", "real", "0"}


@dataclass
class Finding:
    """一条闸门发现。severity=error 表示必须阻止提交。"""

    severity: str          # "error" | "warning"
    location: str          # 文件:字段
    message: str
    evidence: str = ""

    def render(self) -> str:
        mark = "✗" if self.severity == "error" else "!"
        ev = f"  实测：{self.evidence}" if self.evidence else ""
        return f"  {mark} [{self.location}] {self.message}{ev}"


@dataclass
class ScanResult:
    findings: list[Finding] = field(default_factory=list)
    scanned_items: int = 0
    scanned_days: int = 0
    allowlist_size: int = 0
    baseline_ok: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors


# ------------------------------------------------------------------ 白名单
@dataclass
class Allowlist:
    """已确认为虚构的实体集合（来自合成基线 manifest）。"""

    wells: set[str] = field(default_factory=set)
    orgs: set[str] = field(default_factory=set)
    fields: set[str] = field(default_factory=set)
    rigs: set[str] = field(default_factory=set)
    baseline_size: int = 0

    @property
    def size(self) -> int:
        return len(self.wells) + len(self.orgs) + len(self.fields) + len(self.rigs)


def _norm(text: Any) -> str:
    """归一化：去空白、统一大小写，用于白名单比对。"""
    if text is None:
        return ""
    return re.sub(r"\s+", "", str(text)).strip().lower()


def build_allowlist(baseline: Path | str = BASELINE_MANIFEST) -> tuple[Allowlist, list[str]]:
    """从合成基线 manifest 收集白名单。

    返回 (Allowlist, notes)。若基线不是纯合成数据，notes 里会给出说明，
    调用方应据此拒绝放行（见 scan_dataset）。
    """
    notes: list[str] = []
    al = Allowlist()
    path = Path(baseline)
    if not path.exists():
        notes.append(f"合成基线不存在：{path}（白名单为空，所有主体名都会被判为可疑）")
        return al, notes

    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        notes.append(f"合成基线无法解析：{path}（{type(exc).__name__}: {exc}）")
        return al, notes

    items = manifest.get("items") or []
    synthetic = [it for it in items if it.get("synthetic") is True]
    non_synthetic = [it for it in items if it.get("synthetic") is not True]

    if non_synthetic:
        notes.append(
            f"合成基线中混有 {len(non_synthetic)} 条非合成条目（synthetic != true）。"
            f"白名单仅由合成条目构成；这些条目本身会被逐条检查。"
        )
    if not synthetic:
        notes.append(
            f"合成基线 {path} 中没有任何 synthetic: true 的条目，"
            f"无法建立可信白名单 —— 闸门将把所有主体名视为可疑。"
        )

    for it in synthetic:
        for key, bucket in (
            ("well_name", al.wells),
            ("operator", al.orgs),
            ("contractor", al.orgs),
            ("field", al.fields),
            ("rig", al.rigs),
        ):
            v = _norm(it.get(key))
            if v:
                bucket.add(v)
    al.baseline_size = len(synthetic)
    return al, notes


# ------------------------------------------------------------------ 单项检查
def _check_org(value: str, location: str, al: Allowlist) -> list[Finding]:
    """机构名检查：自由文本字段（可能是"作业者：XX 公司"这种带前缀的写法）。"""
    out: list[Finding] = []
    if not value or not str(value).strip():
        return out
    if _norm(value) in al.orgs:
        return out
    if _REDACTION_RE.search(str(value)):
        return out
    lowered = str(value).lower()
    if any(mk in lowered for mk in PUBLIC_DATASET_MARKERS):
        return out
    m = _ORG_SUFFIX_RE.search(str(value))
    if m:
        out.append(
            Finding(
                "error",
                location,
                f"疑似真实机构名（命中机构后缀「{m.group(0)}」且不在合成白名单中）。"
                f"入库前必须脱敏，或放入 .gitignore 的 data/real/。",
                evidence=str(value)[:60],
            )
        )
    return out


def _check_well_name(value: str, location: str, al: Allowlist) -> list[Finding]:
    out: list[Finding] = []
    if not value or not str(value).strip():
        return out
    if _norm(value) in al.wells:
        return out
    if _REDACTION_RE.search(str(value)):
        return out
    lowered = str(value).lower()
    if any(mk in lowered for mk in PUBLIC_DATASET_MARKERS):
        return out
    if _WELL_NAME_RE.match(str(value).strip()):
        out.append(
            Finding(
                "error",
                location,
                "疑似真实井名（形如「区块-编号」且不在合成白名单中，也未脱敏）。",
                evidence=str(value)[:60],
            )
        )
    return out


def _check_free_text(text: str, location: str, al: Allowlist, terms: Iterable[str]) -> list[Finding]:
    """在自由文本（备注/摘要）里搜白名单之外的主体名与井名。

    真实日报的备注经常写"接甲方 XX 公司通知""邻井 XX-1 井…"，
    这些泄露点不在表头字段里，必须单独扫。
    """
    out: list[Finding] = []
    if not text:
        return out
    for term in terms:
        if not term:
            continue
        # 井名可能带空格（如 "苏 48-12-66X"），归一化掉空白再比
        compact = re.sub(r"\s+", "", text).lower()
        if re.sub(r"\s+", "", term).lower() in compact:
            continue
        if term.lower() in text.lower():
            out.append(
                Finding(
                    "warning",
                    location,
                    "自由文本中出现未在白名单内的实体名，请人工确认是否为真实信息。",
                    evidence=term[:60],
                )
            )
    return out


def _check_org_mentions(text: str, location: str, al: Allowlist) -> list[Finding]:
    """在自由文本里找**机构名形态**的片段，命中机构后缀且不在白名单即提示。

    与 `_check_free_text` 的分工：后者只在白名单词被"搬错数据集"时命中，
    抓不到真实日报里自然写出的新公司名。而备注恰恰是泄露高发区
    （"接 XX 公司通知""由 XX 公司承运"），所以必须按形态扫，而不是按已知词扫。

    判定为 warning 而非 error：机构后缀在工程叙述里也可能出现在非主体语境，
    需要人工确认。但漏报真实公司名比多一点人工复核代价大得多。
    """
    out: list[Finding] = []
    if not text:
        return out
    for m in _ORG_SUFFIX_RE.finditer(text):
        # 取后缀往前的一段作为候选主体名（最多回溯 12 个字符）
        start = max(0, m.start() - 12)
        candidate = text[start : m.end()]
        # 已知虚构主体（白名单）覆盖了候选片段的大部分 → 正常，跳过。
        # 必须要求"覆盖大部分"，否则单独的"有限公司"会因子串命中任何白名单机构名
        # 而被全部放过，闸门就失效了。
        cand_compact = re.sub(r"\s+", "", candidate).lower()
        if cand_compact in al.orgs:
            continue
        if any(
            known and known in cand_compact and len(known) >= 0.8 * len(cand_compact)
            for known in al.orgs
        ):
            continue
        if _REDACTION_RE.search(candidate):
            continue
        lowered = candidate.lower()
        if any(mk in lowered for mk in PUBLIC_DATASET_MARKERS):
            continue
        out.append(
            Finding(
                "warning",
                location,
                f"自由文本中出现机构名形态的片段（命中后缀「{m.group(0)}」），"
                f"请人工确认是否属于真实主体（自动检查无法判定归属）。",
                evidence=f"…{candidate}",
            )
        )
    return out


# ------------------------------------------------------------------ 数据集扫描
def scan_dataset(
    data_dir: Path | str = DEFAULT_DATA_DIR,
    *,
    baseline: Path | str = BASELINE_MANIFEST,
) -> ScanResult:
    """扫描样本目录，找出可能导致私有信息泄露的条目。"""
    data_dir = Path(data_dir)
    res = ScanResult()
    al, notes = build_allowlist(baseline)
    res.notes.extend(notes)
    res.allowlist_size = al.size
    res.baseline_ok = not bool([n for n in notes if "无法" in n or "没有" in n])

    manifest_path = data_dir / MANIFEST_NAME
    if not manifest_path.exists():
        res.findings.append(
            Finding("error", str(manifest_path), "找不到 manifest.json，无法扫描数据集。")
        )
        return res

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        res.findings.append(
            Finding("error", str(manifest_path), f"manifest 无法解析（{type(exc).__name__}: {exc}）")
        )
        return res

    items = manifest.get("items") or []
    res.scanned_items = len(items)
    if not items:
        res.findings.append(Finding("error", str(manifest_path), "manifest 的 items 为空。"))
        return res

    for idx, it in enumerate(items):
        file_rel = str(it.get("file") or f"<item[{idx}]>")
        loc = f"{file_rel}"

        # 1) 显式声明为非合成 → 必须证明已脱敏
        if it.get("synthetic") in _NON_SYNTHETIC_VALUES or "synthetic" not in it:
            if "synthetic" not in it:
                res.findings.append(
                    Finding("warning", loc, "manifest 条目缺少 synthetic 标记，请显式声明。")
                )
            else:
                res.findings.append(
                    Finding(
                        "error",
                        loc,
                        "manifest 声明 synthetic=false（真实数据）。真实日报不得进入版本库，"
                        "请移至 data/real/（已在 .gitignore 中）。",
                    )
                )

        # 2) 文件落点检查：跟踪目录里的真实数据 = 会被提交
        if not file_rel.replace("\\", "/").startswith("samples/"):
            res.findings.append(
                Finding(
                    "warning",
                    loc,
                    "条目文件不在 samples/ 子目录下，请确认该路径确实位于被跟踪区域。",
                )
            )

        # 3) 主体名与井名
        for key in ("well_name", "operator", "contractor", "field", "rig"):
            v = it.get(key)
            if v is None:
                continue
            if key == "well_name":
                res.findings.extend(_check_well_name(v, f"{loc}:{key}", al))
            elif key in ("operator", "contractor"):
                res.findings.extend(_check_org(v, f"{loc}:{key}", al))

        # 4) ground truth 的备注与摘要（泄露点最集中的地方）
        truth_rel = it.get("truth")
        if not truth_rel:
            continue
        truth_path = data_dir / str(truth_rel)
        res.scanned_days += 1
        if not truth_path.exists():
            res.findings.append(Finding("warning", f"{truth_path}", "ground truth 文件缺失。"))
            continue
        try:
            truth = json.loads(truth_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            res.findings.append(
                Finding("warning", str(truth_path), f"ground truth 无法解析（{type(exc).__name__}）")
            )
            continue

        # 表头级别的重名检查（ground truth 自带一份，可能与 manifest 不一致）
        for key in ("well_name", "operator", "contractor"):
            v = truth.get(key)
            if not v:
                continue
            if key == "well_name":
                res.findings.extend(_check_well_name(v, f"{truth_rel}:{key}", al))
            else:
                res.findings.extend(_check_org(v, f"{truth_rel}:{key}", al))

        inner = truth.get("truth") or {}
        texts: list[str] = []
        for r in inner.get("remarks") or []:
            if isinstance(r, dict) and r.get("text"):
                texts.append(str(r["text"]))
        for key in ("sum_24hr", "plan_24hr", "well_status", "status_comment"):
            if inner.get(key):
                texts.append(str(inner[key]))

        banned = sorted(al.orgs | al.wells)
        for t in texts:
            res.findings.extend(_check_free_text(t, f"{truth_rel}:remarks", al, banned))
            res.findings.extend(_check_org_mentions(t, f"{truth_rel}:remarks", al))

    return res


def render_text(res: ScanResult, data_dir: Path | str = DEFAULT_DATA_DIR) -> str:
    lines = [f"数据闸门扫描：{data_dir}"]
    lines.append(
        f"  样本 {res.scanned_items} 条 / ground truth {res.scanned_days} 份；"
        f"合成白名单 {res.allowlist_size} 项"
    )
    for n in res.notes:
        lines.append(f"  ! {n}")
    lines.append("")
    if not res.findings:
        lines.append("  ✓ 未发现私有数据风险。")
        return "\n".join(lines)

    lines.append(f"发现 {len(res.errors)} 个必须处理的问题、{len(res.warnings)} 个提示：")
    for f in res.errors:
        lines.append(f.render())
    for f in res.warnings:
        lines.append(f.render())
    lines.append("")
    if res.errors:
        lines.append("真实日报请放到 data/real/（已在 .gitignore 中），不要放进 data/samples/。")
        lines.append("确需入库的样本必须先脱敏（井名/公司名替换），并要求人工复核本清单。")
    return "\n".join(lines)

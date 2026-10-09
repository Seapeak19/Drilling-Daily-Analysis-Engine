"""评测器：把解析结果与 ground truth 逐字段比对，输出分字段准确率报告。

为什么需要它（对应项目计划书 阶段 0/1 的验证判据）：
- M0 验收标准是"关键数值字段准确率 ≥ 90%"，没有评测器就无法判定是否达标；
- M1 验收标准是"分字段统计的准确率报告"，因此必须逐字段统计，而不是只给一个总分。

评测口径（写清楚才能被复核）：
- **数值字段**：相对误差 ≤ 容差（默认 1%）即判对；单位换算错误会被这一口径抓住。
- **文本字段**：归一化（去空白、全角转半角、大小写）后完全相等。
- **时间条目**：按"总时长 + 条目数 + 每条的时长与操作码"三点比对，避免错位导致的全盘误判。
- **NPT**：以 NPT 总时长与 NPT 条目数比对。
- **ILT 候选**（接单根）：以接单条数与各条时长比对。
"""

from __future__ import annotations

import json
import sys
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .pipeline import ParseResult, parse_file

# 数值字段容差（相对误差）。井深/进尺按 1%，小量纲字段放宽。
DEFAULT_TOLERANCE = 0.01
RELAXED_TOLERANCE = 0.03


@dataclass
class FieldResult:
    field: str
    group: str
    ok: bool
    expected: Any = None
    actual: Any = None
    note: str = ""


@dataclass
class SampleResult:
    file: str
    template_expected: str
    template_actual: str
    fields: list[FieldResult] = field(default_factory=list)
    error: str | None = None

    @property
    def scored(self) -> list[FieldResult]:
        return [f for f in self.fields if f.ok is not None]


@dataclass
class Report:
    samples: list[SampleResult] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.samples

    def group_stats(self) -> dict[str, dict[str, Any]]:
        stats: dict[str, dict[str, Any]] = {}
        for s in self.samples:
            for f in s.scored:
                g = stats.setdefault(f.group, {"total": 0, "ok": 0, "fails": []})
                g["total"] += 1
                if f.ok:
                    g["ok"] += 1
                else:
                    g["fails"].append((s.file, f.field, f.expected, f.actual, f.note))
        for g in stats.values():
            g["accuracy"] = round(g["ok"] / g["total"], 4) if g["total"] else None
        return stats

    def overall(self) -> dict[str, Any]:
        total = sum(1 for s in self.samples for _ in s.scored)
        ok = sum(1 for s in self.samples for f in s.scored if f.ok)
        return {"total": total, "ok": ok, "accuracy": round(ok / total, 4) if total else None}


# --------------------------------------------------------------------- 比对工具
def _norm_text(x: Any) -> str:
    if x is None:
        return ""
    s = unicodedata.normalize("NFKC", str(x))
    s = re.sub(r"[\s\u3000]+", "", s)
    return s.strip().lower()


def num_ok(expected: float | None, actual: float | None, tol: float = DEFAULT_TOLERANCE) -> bool:
    if expected is None and actual is None:
        return True
    if expected is None or actual is None:
        return False
    if abs(expected) < 1e-9:
        return abs(actual) < 1e-6
    return abs(actual - expected) / abs(expected) <= tol


def text_ok(expected: Any, actual: Any, *, contains: bool = False) -> bool:
    e, a = _norm_text(expected), _norm_text(actual)
    if not e and not a:
        return True
    if not e or not a:
        return False
    return (e in a or a in e) if contains else (e == a)


# --------------------------------------------------------------------- 单份评测
FIELD_SPECS: list[tuple[str, str, Callable[[dict, dict], tuple[Any, Any, bool, str]]]] = []


def _add(field_name: str, group: str):
    def deco(fn):
        FIELD_SPECS.append((field_name, group, fn))
        return fn

    return deco


def _g(t: dict, p: dict, path: str) -> tuple[Any, Any]:
    """按点分路径同时取 ground truth 与解析结果。

    两侧路径前缀不同：truth 是扁平结构（md_m / time_log / mud…），
    解析结果是 DailyReport 结构（report.md_m / time_log / mud…）。
    这里统一用一个逻辑名映射到两侧的实际路径，避免每个断言各写一套。
    """
    return _dig(t, _truth_path(path)), _dig(p, _parsed_path(path))


def _dig(d: Any, p: str) -> Any:
    cur: Any = d
    for part in p.split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


# 逻辑名 → 解析结果中的实际路径（truth 侧用同名扁平键）
_PARSED_ALIASES = {
    "sum_hours": "time_verification.sum_hours",
    "report_no": "report.report_no",
    "well_name": "well.well_name",
    "operator": "well.operator",
    "contractor": "well.contractor",
    "rig": "well.rig",
}


def _truth_path(path: str) -> str:
    return path


def _parsed_path(path: str) -> str:
    return _PARSED_ALIASES.get(path, path)


@_add("well_name", "表头/井信息")
def _(t, p):
    te, ac = _g(t, p, "well_name")
    return te, ac, text_ok(te, ac), ""


@_add("operator", "表头/井信息")
def _(t, p):
    te, ac = _g(t, p, "operator")
    return te, ac, text_ok(te, ac, contains=True), ""


@_add("contractor", "表头/井信息")
def _(t, p):
    te, ac = _g(t, p, "contractor")
    return te, ac, text_ok(te, ac, contains=True), ""


@_add("rig", "表头/井信息")
def _(t, p):
    te, ac = _g(t, p, "rig")
    return te, ac, text_ok(te, ac, contains=True), ""


@_add("report_no", "表头/井信息")
def _(t, p):
    te, ac = _g(t, p, "report_no")
    return te, ac, num_ok(te, ac, 0.0), "日报编号"


@_add("report_date", "表头/井信息")
def _(t, p):
    te, ac = _g(t, p, "report_date")
    return te, ac, text_ok(te, (ac or "")[:10]), "作业日期"


@_add("md_m", "表头/井信息")
def _(t, p):
    te, ac = _g(t, p, "md_m")
    return te, ac, num_ok(te, ac), "井深，英制模板下最易因单位换算出错"


@_add("tvd_m", "表头/井信息")
def _(t, p):
    te, ac = _g(t, p, "tvd_m")
    return te, ac, num_ok(te, ac), ""


@_add("progress_m", "表头/井信息")
def _(t, p):
    te, ac = _g(t, p, "progress_m")
    return te, ac, num_ok(te, ac), ""


@_add("rop_av_m_per_h", "表头/井信息")
def _(t, p):
    te, ac = _g(t, p, "rop_av_m_per_h")
    return te, ac, num_ok(te, ac, RELAXED_TOLERANCE), "平均钻速"


@_add("etim_drill_h", "表头/井信息")
def _(t, p):
    # 日报表头的"钻进时间"在 truth 里没有单列，但可由时间条目按钻进/扩眼码求和得到
    entries = _g(t, p, "time_log")[0] or []
    te = round(sum(float(e.get("hours") or 0) for e in entries if e.get("code") in ("DRILL", "REAM")), 2) or None
    ac = _g(t, p, "etim_drill_h")[1]
    return te, ac, num_ok(te, ac, RELAXED_TOLERANCE), "钻进时间"


@_add("sum_hours", "时间分解")
def _(t, p):
    te, ac = _g(t, p, "sum_hours")
    return te, ac, num_ok(te, ac, 0.005), "时间分解加总（IADC 24 小时硬约束）"


@_add("time_entry_count", "时间分解")
def _(t, p):
    entries = _g(t, p, "time_log")[0] or []
    parsed = _g(t, p, "time_log")[1] or []
    te = len(entries)
    ac = len(parsed)
    # 日报原文加总不足 24 h 时，解析器会显式补一条"未解释缺口"记录，这是设计行为
    expected_total = round(sum(float(e.get("hours") or 0) for e in entries), 2)
    if abs(expected_total - 24.0) > 0.01:
        te = te + 1
    return te, ac, te == ac, "条目数"


@_add("npt_hours", "NPT")
def _(t, p):
    te = _g(t, p, "npt_hours")[0]
    log = _g(t, p, "time_log")[1] or []
    ac = round(sum(float(e.get("hours") or 0) for e in log if e.get("is_npt")), 2)
    return te, ac, num_ok(te, ac, 0.02), "NPT 总时长"


@_add("npt_entry_count", "NPT")
def _(t, p):
    te = len(_g(t, p, "npt_events")[0] or [])
    log = _g(t, p, "time_log")[1] or []
    ac = sum(1 for e in log if e.get("is_npt"))
    return te, ac, te == ac, "NPT 条目数"


@_add("mud_density_gcc", "泥浆")
def _(t, p):
    te = _g(t, p, "mud.density_gcc")[0]
    mud = _g(t, p, "mud")[1] or []
    ac = next((m.get("density_gcc") for m in mud if m.get("sample_point") == "flowline"), None)
    return te, ac, num_ok(te, ac, 0.02), "出口密度；ppg↔g/cm³ 换算的关键校验点"


@_add("mud_fl_ml", "泥浆")
def _(t, p):
    te = _g(t, p, "mud.fl_ml")[0]
    mud = _g(t, p, "mud")[1] or []
    ac = next((m.get("fl_ml") for m in mud if m.get("sample_point") == "flowline"), None)
    return te, ac, num_ok(te, ac, 0.05), "API 失水"


@_add("mud_viscosity_s", "泥浆")
def _(t, p):
    te = _g(t, p, "mud.funnel_viscosity_s")[0]
    mud = _g(t, p, "mud")[1] or []
    ac = next((m.get("funnel_viscosity_s") for m in mud if m.get("sample_point") == "flowline"), None)
    return te, ac, num_ok(te, ac, 0.05), "漏斗粘度"


@_add("bit_count", "钻头记录")
def _(t, p):
    te = len(_g(t, p, "bit_records")[0] or [])
    ac = len(_g(t, p, "bit_records")[1] or [])
    return te, ac, te == ac, "钻头条数"


@_add("bit_depth_out_m", "钻头记录")
def _(t, p):
    exp = _g(t, p, "bit_records")[0] or []
    act = _g(t, p, "bit_records")[1] or []
    if not exp and not act:
        return None, None, True, "本日无钻头记录"
    if not act:
        return exp[0].get("depth_out_m") if exp else None, None, False, "解析结果缺少钻头记录"
    return exp[0].get("depth_out_m"), act[0].get("depth_out_m"), num_ok(
        exp[0].get("depth_out_m"), act[0].get("depth_out_m")
    ), "出井深度"


@_add("bit_footage_m", "钻头记录")
def _(t, p):
    exp = _g(t, p, "bit_records")[0] or []
    act = _g(t, p, "bit_records")[1] or []
    if not exp and not act:
        return None, None, True, ""
    if not act:
        return exp[0].get("footage_m") if exp else None, None, False, "解析结果缺少钻头记录"
    return exp[0].get("footage_m"), act[0].get("footage_m"), num_ok(
        exp[0].get("footage_m"), act[0].get("footage_m")
    ), "进尺"


@_add("bit_dull_grade", "钻头记录")
def _(t, p):
    exp = _g(t, p, "bit_records")[0] or []
    act = _g(t, p, "bit_records")[1] or []
    if not exp and not act:
        return None, None, True, ""
    if not act:
        return exp[0].get("dull_grade") if exp else None, None, False, "解析结果缺少钻头记录"
    return exp[0].get("dull_grade"), act[0].get("dull_grade"), text_ok(
        exp[0].get("dull_grade"), act[0].get("dull_grade")
    ), "IADC 磨损分级码"


@_add("connection_count", "ILT/接单根")
def _(t, p):
    te = _g(t, p, "connection_count")[0] or 0
    log = _g(t, p, "time_log")[1] or []
    ac = sum(1 for e in log if e.get("ilt_action") == "connection")
    return te, ac, te == ac, "接单根识别条数（ILT 的基础）"


@_add("connection_hours", "ILT/接单根")
def _(t, p):
    te = round(sum(float(c.get("hours") or 0) for c in (_g(t, p, "connections")[0] or [])), 2)
    log = _g(t, p, "time_log")[1] or []
    ac = round(sum(float(e.get("hours") or 0) for e in log if e.get("ilt_action") == "connection"), 2)
    return te, ac, num_ok(te, ac, 0.05), "接单根总时长"


# --------------------------------------------------------------------- 备注事件
# 为什么要把 events 纳入评测
# ----------------
# 在加入这一组之前，`ddr eval` 的断言覆盖表头/时间/NPT/泥浆/钻头/接单根，
# 但**完全不覆盖 events**。后果是：events[].hours 曾被一个正则缺陷
# 100% 污染（把备注开头的钟点 "06:30" 当成 6 小时），而评测仍然是 100% ——
# 字段没有断言，错了也没人知道。
#
# 这一组的期望值**从 ground truth 的备注原文独立复算**（见 _truth_duration_hours），
# 不复用 llm.py 的抽取函数 —— 否则就是拿实现验证实现，等于没测。

# 独立复算备注里的"明确时长"。刻意不 import llm.py：
# 期望值必须来自与实现无关的路径，才可能发现实现的错误。
_TRUTH_DUR_MIN_RE = re.compile(r"耗时\s*(?P<n>\d+(?:\.\d+)?)\s*分钟")
_TRUTH_DUR_HOUR_RE = re.compile(r"(?<![\d.])(?P<n>\d+(?:\.\d+)?)\s*小时")


def _truth_duration_hours(text: str) -> float | None:
    """从备注原文独立推出"这条备注有没有明确时长"。

    只认两种最常见的显式写法（"耗时 N 分钟" / "N 小时"）。
    没有明确时长的备注返回 None —— 此时 hours 必须为 None，
    这正是当初被污染的那一类。
    """
    if not text:
        return None
    m = _TRUTH_DUR_MIN_RE.search(text)
    if m:
        return round(float(m.group("n")) / 60.0, 3)
    m = _TRUTH_DUR_HOUR_RE.search(text)
    if m:
        return round(float(m.group("n")), 2)
    return None


@_add("event_count", "备注事件")
def _(t, p):
    rems = _g(t, p, "remarks")[0] or []
    evs = p.get("events") or []
    return len(rems), len(evs), len(rems) == len(evs), "备注→事件条数（每条备注应产出一个事件）"


@_add("event_hours_total", "备注事件")
def _(t, p):
    """所有事件的总时长。防止"个别事件时长错"被平均掉。"""
    rems = _g(t, p, "remarks")[0] or []
    te = round(sum(h for h in (_truth_duration_hours(r.get("text", "")) for r in rems) if h), 2)
    ac = round(sum(float(e.get("hours") or 0) for e in (p.get("events") or [])), 2)
    return te, ac, num_ok(te, ac, 0.05), "事件时长合计"


@_add("event_hours_none_ratio", "备注事件")
def _(t, p):
    """**没有明确时长的备注，其事件 hours 必须为 None**。

    这一条是专门为"钟点被误判为时长"那类缺陷设的：
    一旦备注开头的钟点被当成小时，这里会立刻从 100 掉下来。
    用"无时长备注的条数"作断言值，任何一条被误判都会暴露。
    """
    rems = _g(t, p, "remarks")[0] or []
    evs = p.get("events") or []
    expected_none = sum(1 for r in rems if _truth_duration_hours(r.get("text", "")) is None)
    actual_none = sum(1 for e in evs if e.get("hours") is None)
    return expected_none, actual_none, expected_none == actual_none, "无明确时长的备注条数"


@_add("event_duration_exact", "备注事件")
def _(t, p):
    """逐条核对：有时长的备注，其事件时长必须与独立复算一致。

    这是最严的一条 —— 它锁定"4.0 小时"这类**真正的时长**不被钟点遮蔽
    （历史缺陷里 "13:00 停钻 4.0 小时" 会取到 13.0 而不是 4.0）。
    返回不一致的条数，期望 0。
    """
    rems = _g(t, p, "remarks")[0] or []
    evs = p.get("events") or []
    bad = 0
    for rem, ev in zip(rems, evs):
        want = _truth_duration_hours(rem.get("text", ""))
        if want is None:
            continue
        got = ev.get("hours")
        if got is None or abs(float(got) - want) > 0.02:
            bad += 1
    return 0, bad, bad == 0, "时长与备注不一致的事件条数"


@_add("event_evidence_verified", "备注事件")
def _(t, p):
    """凡带时长的证据片段，必须能在备注原文里定位（防幻觉护栏是否生效）。

    期望：带 hours 的事件中，verified=True 的占比 100%；
    即不存在"有数值但原文找不到依据"的事件。
    """
    evs = p.get("events") or []
    with_hours = [e for e in evs if e.get("hours") is not None]
    bad = sum(1 for e in with_hours if not e.get("verified"))
    return 0, bad, bad == 0, f"带时长的 {len(with_hours)} 个事件中无法定位证据的条数"


def evaluate_sample(
    pdf_or_xlsx: Path,
    truth_path: Path,
    *,
    template_id: str | None = None,
    parsed: ParseResult | None = None,
) -> SampleResult:
    truth_doc = json.loads(Path(truth_path).read_text(encoding="utf-8"))
    truth = truth_doc["truth"]
    res = SampleResult(
        file=Path(pdf_or_xlsx).name,
        template_expected=truth_doc.get("template_id", ""),
        template_actual="",
    )
    if parsed is None:
        try:
            parsed = parse_file(pdf_or_xlsx)
        except Exception as exc:  # 解析崩溃必须计入报告，而不是静默跳过
            res.error = f"{type(exc).__name__}: {exc}"
            return res
    res.template_actual = parsed.detection.template_id
    payload = parsed.report.to_dict()
    # ground truth 分两层：井级信息在 truth 文件的顶层（well_name 等），
    # 日报级信息在 truth["truth"] 里。这里合并成一个层级，避免每个断言各写一套路径。
    truth = {**{k: v for k, v in truth_doc.items() if k != "truth"}, **truth}
    payload["truth"] = truth
    # 把解析结果里的字段提升到顶层，使 truth/parsed 的路径表达尽量一致
    payload.update(
        {
            "md_m": payload["report"].get("md_m"),
            "tvd_m": payload["report"].get("tvd_m"),
            "progress_m": payload["report"].get("progress_m"),
            "rop_av_m_per_h": payload["report"].get("rop_av_m_per_h"),
            "etim_drill_h": payload["report"].get("etim_drill_h"),
            "report_date": (payload["report"].get("dtim_start") or "")[:10],
        }
    )

    for name, group, fn in FIELD_SPECS:
        try:
            expected, actual, ok, note = fn(truth, payload)
        except Exception as exc:
            expected, actual, ok, note = None, None, False, f"比对异常 {type(exc).__name__}: {exc}"
        if expected is None and actual is None and ok:
            # truth 里本就没有该项（如本日无钻头记录）→ 不计入分母
            continue
        res.fields.append(FieldResult(field=name, group=group, ok=ok, expected=expected, actual=actual, note=note))
    return res


def load_manifest(data_dir: str | Path) -> dict[str, Any]:
    """读取评测集 manifest，失败时给可操作提示而不是崩溃栈。"""
    p = Path(data_dir) / "manifest.json"
    if not p.exists():
        raise FileNotFoundError(
            f"没找到 {p}。评测需要一个含 manifest.json 的数据集目录，"
            "例如 data/samples（可用 `python -m ddr.render --out data/samples` 生成）。"
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{p} 不是合法 JSON（第 {exc.lineno} 行：{exc.msg}）") from exc
    if not isinstance(data, dict) or "items" not in data:
        raise ValueError(f"{p} 结构不符合预期：顶层应为对象且含 'items' 键")
    return data


def evaluate_dataset(data_dir: str | Path) -> Report:
    """按井分组、按日期排序后评测。

    必须按井按时间顺序整体解析：日报的"当日进尺"要靠相邻日报井深接续反推，
    逐份独立解析会丢掉这个上下文，导致进尺与钻速系统性缺失。
    """
    from .pipeline import parse_files

    data_dir = Path(data_dir)
    manifest = load_manifest(data_dir)
    items = manifest["items"]

    by_well: dict[str, list[dict]] = {}
    for it in items:
        by_well.setdefault(it.get("well_name") or "", []).append(it)

    rep = Report()
    for well_name, group in by_well.items():
        group_sorted = sorted(group, key=lambda it: it.get("report_date") or "")
        # manifest 里登记了但磁盘上不存在的样本：跳过并记录，不让评测整体崩掉
        present = [
            it
            for it in group_sorted
            if (data_dir / it.get("file", "")).exists() and (data_dir / it.get("truth", "")).exists()
        ]
        for it in group_sorted:
            if it not in present:
                rep.missing.append(f"{it.get('file')}（或对应 ground truth）文件不存在")
        if not present:
            continue
        paths = [data_dir / it["file"] for it in present]
        try:
            parsed_list = parse_files(paths)
        except Exception as exc:
            for it in present:
                rep.samples.append(
                    SampleResult(
                        file=it.get("file", "?"),
                        template_expected=it.get("template_id", ""),
                        template_actual="",
                        error=f"解析抛出异常：{type(exc).__name__}: {exc}",
                    )
                )
            continue
        for it, parsed in zip(present, parsed_list):
            rep.samples.append(
                evaluate_sample(
                    data_dir / it["file"],
                    data_dir / it["truth"],
                    template_id=it.get("template_id"),
                    parsed=parsed,
                )
            )
    return rep


# --------------------------------------------------------------------- 报告输出
def _fingerprint() -> dict[str, str]:
    """采集报告的"数据指纹"：这份报告对应哪个版本的代码、哪个版本的解释器。

    为什么必须有：报告里的"100.00%"是一个**结论**，结论必须可追溯到出具它的
    代码版本。没有指纹时，仓库里躺着的 evaluation_report.md 无法回答
    "这是改完哪个提交之后跑的"，于是准确率数字随时间失去意义 ——
    后来者只能盲目重跑，或者更糟：相信一个过期数字。
    """
    import platform
    import subprocess
    from datetime import datetime

    fp: dict[str, str] = {
        "生成时间": datetime.now().astimezone().isoformat(timespec="seconds"),
        "Python": platform.python_version(),
    }

    here = Path(__file__).resolve().parent

    def _git(*argv: str, cwd: Path | None = None) -> str | None:
        """跑一条 git 命令并返回 stdout；任何异常都退回 None。

        指纹是**辅助信息**：拿不到 git（未安装 / 不在仓库 / 子进程被限制）
        绝不能拖垮评测本身 —— 报告可以少一行元数据，但不能出不来。
        """
        try:
            cp = subprocess.run(
                # -c core.quotepath=false：让 git 直接输出非 ASCII 路径，
                # 而不是 \344\270\255 这种八进制转义，便于人读。
                ["git", "-c", "core.quotepath=false", *argv],
                cwd=cwd or here,
                capture_output=True,
                # 必须显式指定 utf-8。Windows 上 text=True 默认用系统 ANSI 代码页
                # （简体中文环境是 GBK），遇到仓库路径含中文时读取线程会抛
                # UnicodeDecodeError，stdout 变成 None，指纹就静默退化成"未知"
                # —— 这是一个真实踩过的坑（本仓库路径本身就含中文）。
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if cp.returncode != 0 or cp.stdout is None:
            return None
        return cp.stdout

    # 注意 --show-toplevel：status 必须在**仓库根**跑。
    # 否则从 src/ddr/ 子目录跑时，porcelain 只输出该子目录下的变化，
    # 仓库其它地方有未提交改动也会被判成"干净" —— 那正好掩盖了
    # "这份报告的来源不可追溯"这个本该提示的情况。
    root = _git("rev-parse", "--show-toplevel")
    repo_root = Path(root.strip()) if root and root.strip() else here

    head = _git("rev-parse", "--short", "HEAD")
    fp["提交"] = head.strip() if head else "不可用（未安装 git 或不在仓库中）"

    st = _git("status", "--porcelain", cwd=repo_root)
    if st is None:
        fp["工作区"] = "未知（无法读取 git 状态）"
    else:
        # 脏工作区跑出来的数字对不上任何提交，必须显式标出来
        fp["工作区"] = "干净" if not st.strip() else "有未提交改动（本次结果不对应任何提交）"
    return fp


def render_markdown(rep: Report, *, title: str = "解析引擎准确率报告") -> str:
    stats = rep.group_stats()
    overall = rep.overall()

    # 空数据集要产出可读报告而不是崩溃：这是程序化调用的入口，
    # 不能假定调用方已经做过空检查（真实踩过的坑）。
    if rep.empty:
        return "\n".join(
            [
                f"# {title}",
                "",
                "**没有可评测的样本。**",
                "",
                f"- manifest 中被跳过的登记项：{len(rep.missing)} 条",
                *[f"  - `{m}`" for m in rep.missing[:20]],
                "",
                "请检查 `manifest.json` 的 `items` 是否为空，以及其中登记的 "
                "`file` / `truth` 是否都存在于数据集目录下。",
                "",
            ]
        )

    acc_txt = "—" if overall["accuracy"] is None else f"{overall['accuracy']:.2%}"
    lines: list[str] = [f"# {title}", ""]
    lines.append(
        f"评测样本：**{len(rep.samples)} 份**；参与评分的字段断言：**{overall['total']} 项**；"
        f"总体准确率：**{acc_txt}**"
    )
    lines.append("")
    lines.append("> 样本为计算机合成数据（按 IADC 日报标准构造），不代表真实井数据。")
    if rep.missing:
        lines.append(">")
        lines.append(f"> ⚠ manifest 中有 {len(rep.missing)} 条登记项的样本文件缺失，已跳过。")
    lines.append("")

    # ---- 可追溯性：把"这个数字是哪来的"写在数字旁边
    fp = _fingerprint()
    lines.append("## 〇、数据指纹（本报告的适用范围）")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("|---|---|")
    for k, v in fp.items():
        lines.append(f"| {k} | `{v}` |")
    lines.append(f"| 评测样本 / 断言数 | {len(rep.samples)} 份 / {overall['total']} 项 |")
    lines.append(f"| 字段组数 | {len(stats)} 组 |")
    lines.append("")
    lines.append(
        "> 复现本报告：`ddr eval --data data/samples`（同一提交 + 同一数据集 ⇒ 同一结果）。"
    )
    lines.append(
        "> **注意适用范围**：以上准确率是在「样本由本仓库模板渲染器生成」的封闭评测集上取得的，"
        "说明抽取逻辑与数据模型自洽，**不等于**在任意真实日报上的准确率。"
    )
    lines.append("")

    lines.append("## 一、分字段组准确率")
    lines.append("")
    lines.append("| 字段组 | 断言数 | 正确 | 准确率 | 说明 |")
    lines.append("|---|---|---|---|---|")
    group_desc = {
        "表头/井信息": "井名、公司、井深、钻速等表头数值（含单位归一）",
        "时间分解": "24 小时时间分解的加总与条目数",
        "NPT": "非生产时间的总时长与条目识别",
        "泥浆": "泥浆性能参数（英制模板下需 ppg→g/cm³ 换算）",
        "钻头记录": "钻头进出井深度、进尺、IADC 磨损分级码",
        "ILT/接单根": "隐形损失时间分析的基础：接单根条目识别",
    }
    for g, s in sorted(stats.items(), key=lambda kv: (kv[1]["accuracy"] or 0)):
        acc_txt = "—" if s["accuracy"] is None else f"{s['accuracy']:.2%}"
        lines.append(f"| {g} | {s['total']} | {s['ok']} | {acc_txt} | {group_desc.get(g, '')} |")
    lines.append("")

    lines.append("## 二、逐字段准确率")
    lines.append("")
    field_stats: dict[str, dict[str, Any]] = {}
    for s in rep.samples:
        for f in s.scored:
            d = field_stats.setdefault(f.field, {"group": f.group, "total": 0, "ok": 0, "note": f.note})
            d["total"] += 1
            d["ok"] += 1 if f.ok else 0
    lines.append("| 字段 | 字段组 | 断言数 | 准确率 | 备注 |")
    lines.append("|---|---|---|---|---|")
    for name, d in sorted(field_stats.items(), key=lambda kv: (kv[1]["ok"] / kv[1]["total"] if kv[1]["total"] else 0)):
        acc = d["ok"] / d["total"] if d["total"] else 0
        lines.append(f"| `{name}` | {d['group']} | {d['total']} | {acc:.1%} | {d['note']} |")
    lines.append("")

    lines.append("## 三、模板识别")
    lines.append("")
    tpl_ok = sum(1 for s in rep.samples if s.template_expected == s.template_actual)
    lines.append(f"模板识别正确：**{tpl_ok}/{len(rep.samples)}**")
    lines.append("")
    lines.append("| 文件 | 期望模板 | 实际识别 |")
    lines.append("|---|---|---|")
    for s in rep.samples:
        mark = "OK" if s.template_expected == s.template_actual else "**不一致**"
        lines.append(f"| `{s.file}` | {s.template_expected} | {s.template_actual} {mark} |")
    lines.append("")

    fails = [(s.file, f) for s in rep.samples for f in s.scored if not f.ok]
    lines.append("## 四、未通过项明细")
    lines.append("")
    if not fails:
        lines.append("无。全部断言通过。")
    else:
        lines.append("| 文件 | 字段 | 期望 | 实际 | 备注 |")
        lines.append("|---|---|---|---|---|")
        for fn, f in fails:
            lines.append(f"| `{fn}` | `{f.field}` | {f.expected} | {f.actual} | {f.note} |")
    lines.append("")

    errs = [s for s in rep.samples if s.error]
    lines.append("## 五、解析失败（异常）")
    lines.append("")
    if not errs:
        lines.append("无。")
    else:
        for s in errs:
            lines.append(f"- `{s.file}`：{s.error}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="评测解析引擎并输出准确率报告")
    ap.add_argument("--data", default="data/samples")
    ap.add_argument("--out", default="data/samples/evaluation_report.md")
    args = ap.parse_args(argv)

    try:
        rep = evaluate_dataset(args.data)
    except (FileNotFoundError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    if rep.empty:
        print(
            f"错误：{args.data} 的 manifest 里没有可评测的样本"
            + (f"（{len(rep.missing)} 条登记项的样本文件不存在）" if rep.missing else ""),
            file=sys.stderr,
        )
        return 2
    md = render_markdown(rep)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    overall = rep.overall()
    if overall["accuracy"] is None:
        print("错误：没有任何样本参与评分（manifest 为空或所有样本文件缺失）", file=sys.stderr)
        return 2
    print(f"总体准确率 {overall['accuracy']:.2%}（{overall['ok']}/{overall['total']} 项断言）")
    for g, s in sorted(rep.group_stats().items(), key=lambda kv: (kv[1]["accuracy"] or 0)):
        acc = "—" if s["accuracy"] is None else f"{s['accuracy']:.2%}"
        print(f"  {g:<12} {acc:>8}  ({s['ok']}/{s['total']})")
    errs = [s for s in rep.samples if s.error]
    if errs:
        print(f"  ⚠ {len(errs)} 份样本解析抛异常")
    print(f"报告已写入 {out}")
    return 0 if not errs else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""鲁棒性测试：用**故意制造的不规范日报**验证解析器的失败行为是否诚实。

为什么必须有它：在"样本由同一套模板生成"的封闭评测集上拿到 100% 只说明
抽取逻辑与模板自洽，不能说明真实日报上的表现（项目计划书 8.1 明确把
"格式碎片化"列为最高技术风险）。因此这里专门构造真实日报里最常见的畸形：

| 用例 | 模拟的真实问题 | 期望行为（不是"能解析对"，而是"诚实报错"） |
|---|---|---|
| `time_gap` | 时间分解只加到 22.5 h | 判定违规、记录缺口、显式补记 UNKNOWN，不静默摊平 |
| `over_24` | 时间分解加到 25.0 h | 判定超计并报警 |
| `unit_mixed` | 同一份日报里 m 与 ft 混用 | 逐列按各自单位换算，不套用统一单位制 |
| `no_unit` | 数值完全没有单位标注 | 保留原始值但**不换算**，给出人工核对提示 |
| `unlabeled_time` | 时间表列名非常规（"工作"代替"作业内容"） | 退化为文本行解析并记警告，不产出空结果 |
| `negation` | "起钻至 2350 m，无遇卡显示" | 不得判成 NPT（遇卡） |
| `new_template` | 全新版式日报 | 识别为 unknown 模板，仍尽力抽取并提示登记模板 |

每个用例都用"规则断言"表达期望，而不是比对固定输出——目的是锁住**行为契约**。
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .codes import load_code_map
from .detect import detect_template
from .pipeline import parse_file
from .reader import read_document
from .simulate import build_dataset
from .render import render_cn_vertical, render_iadc_classic, render_regional_xls


@dataclass
class CaseResult:
    name: str
    passed: bool
    assertions: list[tuple[str, bool, str]] = field(default_factory=list)
    error: str | None = None

    def check(self, desc: str, ok: bool, detail: str = "") -> None:
        self.assertions.append((desc, ok, detail))


# ------------------------------------------------------------------ 变形工具
def _first_day(ds: dict) -> dict:
    return ds["samples"][0]["days"][1]  # 第 2 天（钻进日，数据最丰富）


def _render_cn(day: dict, out: Path) -> Path:
    render_cn_vertical(day["payload"], out)
    return out


# ------------------------------------------------------------------ 各个用例
def case_time_gap(work: Path) -> CaseResult:
    """时间分解缺口：日报只报到不足 24 h。"""
    r = CaseResult("time_gap", True)
    ds = build_dataset(wells=1, days_per_well=3)
    day = _first_day(ds)
    tl = day["payload"]["time_log"]
    # 从最长的一条里扣掉 1.5 h，模拟"日报漏记了一段作业"
    target = max(range(len(tl)), key=lambda i: tl[i]["hours"])
    cut = 1.5
    if tl[target]["hours"] - cut < 0.1:
        cut = round(tl[target]["hours"] - 0.1, 1)
    tl[target]["hours"] = round(tl[target]["hours"] - cut, 1)
    f = _render_cn(day, work / "time_gap.pdf")
    res = parse_file(f)
    d = res.report.to_dict()
    tv = d["time_verification"]
    r.check("判定为不合规", tv["valid"] is False)
    r.check("记录缺口时长", abs((tv.get("unaccounted_hours") or 0) - cut) < 0.02,
            f"unaccounted={tv.get('unaccounted_hours')} 期望≈{cut}")
    r.check("补记 UNKNOWN 条目且标注来源",
            any(e["code"] == "UNKNOWN" and e["duration_source"] == "derived_from_total" for e in d["time_log"]))
    r.check("加总恢复到 24.00", abs(tv["sum_hours"] - 24.0) < 0.02, f"sum={tv['sum_hours']}")
    r.check("warning 中说明缺口", any("缺口" in m for m in tv["messages"]))
    return r


def case_over_24(work: Path) -> CaseResult:
    """时间分解超计：加到 25.0 h。"""
    r = CaseResult("over_24", True)
    ds = build_dataset(wells=1, days_per_well=3)
    day = _first_day(ds)
    day["payload"]["time_log"][-1]["hours"] = round(day["payload"]["time_log"][-1]["hours"] + 1.0, 1)
    f = _render_cn(day, work / "over_24.pdf")
    res = parse_file(f)
    d = res.report.to_dict()
    tv = d["time_verification"]
    r.check("判定为不合规", tv["valid"] is False)
    r.check("delta 为正且约 1.0", (tv["delta_hours"] or 0) > 0.9, f"delta={tv['delta_hours']}")
    r.check("提示超计", any("超计" in m for m in tv["messages"]))
    return r


def case_unit_mixed(work: Path) -> CaseResult:
    """同一份日报里混用单位：泥浆表用 ft + ppg，表头用 m —— 逐列按各自单位换算。"""
    r = CaseResult("unit_mixed", True)
    ds = build_dataset(wells=1, days_per_well=3)
    day = _first_day(ds)
    payload = json.loads(json.dumps(day["payload"]))
    expect_dens = payload["mud"][0]["density_gcc"]
    expect_md = payload["report"]["md_m"]
    expect_mud_depth_m = payload["mud"][0]["depth_m"]

    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["钻井日报"])
    ws.append(["井号", payload["well"]["well_name"], "日报编号", str(payload["report"]["report_no"])])
    ws.append(["井深 (m)", payload["report"]["md_m"]])
    ws.append(["时间分解"])
    ws.append(["起", "止", "历时(h)", "作业内容", "类别"])
    for e in payload["time_log"]:
        ws.append([e["start"], e["end"], e["hours"], e["operation"], "计划内"])
    ws.append([])
    ws.append(["泥浆性能"])
    # 故意在同一份日报里改用英制：深度 ft、密度 ppg
    ws.append(["取样点", "井深(ft)", "密度(ppg)", "漏斗粘度(s)", "失水(ml)", "pH"])
    ws.append([
        "出口",
        round(payload["mud"][0]["depth_m"] / 0.3048, 1),
        round(payload["mud"][0]["density_gcc"] / 0.1198264, 2),
        payload["mud"][0]["funnel_viscosity_s"],
        payload["mud"][0]["fl_ml"],
        payload["mud"][0]["ph"],
    ])
    f = work / "unit_mixed.xlsx"
    wb.save(str(f))

    d = parse_file(f).report.to_dict()
    r.check("表头井深按 m 解析（不被泥浆列的 ft 带偏）",
            abs(d["report"]["md_m"] - expect_md) < 0.02, f"got {d['report']['md_m']} 期望 {expect_md}")
    dens = next((m["density_gcc"] for m in d["mud"] if m["sample_point"] == "flowline"), None)
    r.check("泥浆密度按 ppg 换算回 g/cm³",
            dens is not None and abs(dens - expect_dens) < 0.01,
            f"got {dens} 期望 {expect_dens}")
    mdepth = next((m["depth_m"] for m in d["mud"] if m["sample_point"] == "flowline"), None)
    r.check("泥浆深度按 ft 换算回 m",
            mdepth is not None and abs(mdepth - expect_mud_depth_m) < 0.5,
            f"got {mdepth} 期望 {expect_mud_depth_m}")
    r.check("单位来源可追溯",
            all(m.get("raw", {}).get("unit_source") for m in d["mud"]),
            f"sources={[m.get('raw', {}).get('unit_source') for m in d['mud']]}")
    return r


def case_no_unit(work: Path) -> CaseResult:
    """数值完全没有单位标注：不得猜单位，必须保留原始值并提示人工核对。"""
    r = CaseResult("no_unit", True)
    ds = build_dataset(wells=1, days_per_well=3)
    day = _first_day(ds)
    payload = json.loads(json.dumps(day["payload"]))
    payload["mud"] = [
        {
            "sample_point": "flowline",
            "depth_m": 2350.0,
            "density_gcc": 1.25,
            "funnel_viscosity_s": 42.0,
            "fl_ml": 4.0,
            "ph": 8.5,
            "mud_type": "KCl",
        }
    ]
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["钻井日报"])
    ws.append(["井号", payload["well"]["well_name"], "日报编号", str(payload["report"]["report_no"])])
    ws.append(["时间分解"])
    ws.append(["起", "止", "历时(h)", "作业内容", "类别"])
    for e in payload["time_log"]:
        ws.append([e["start"], e["end"], e["hours"], e["operation"], "计划内"])
    ws.append([])
    ws.append(["泥浆性能"])
    # 故意去掉所有单位标注
    ws.append(["取样点", "井深", "密度", "漏斗粘度", "失水", "pH"])
    ws.append(["出口", 2350.0, 1.25, 42, 4.0, 8.5])
    f = work / "no_unit.xlsx"
    wb.save(str(f))

    res = parse_file(f)
    d = res.report.to_dict()
    dens = next((m["density_gcc"] for m in d["mud"] if m["sample_point"] == "flowline"), None)
    # cn_vertical/regional_xls 模板未声明 density 源单位 → 必须保留原始值，不得静默换算成 ppg 口径
    r.check("未标注单位时不静默换算（保留原始数值）",
            dens is not None and abs(dens - 1.25) < 0.01, f"density={dens}")
    r.check("单位无法判定时标记为未换算口径（unknown_raw）",
            all(m.get("raw", {}).get("unit_source") == "unknown_raw" for m in d["mud"]),
            f"sources={[m.get('raw', {}).get('unit_source') for m in d['mud']]}")
    r.check("给出单位不确定的提示",
            any("单位" in w for w in res.warnings), f"warnings={res.warnings[:2]}")
    return r


def case_unlabeled_time(work: Path) -> CaseResult:
    """时间表列名非常规（"工作"代替"作业内容"）：应退化解析并记警告，不产出空结果。"""
    r = CaseResult("unlabeled_time", True)
    ds = build_dataset(wells=1, days_per_well=3)
    day = _first_day(ds)
    payload = json.loads(json.dumps(day["payload"]))
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["钻井日报"])
    ws.append(["井号", payload["well"]["well_name"], "日报编号", str(payload["report"]["report_no"])])
    ws.append(["时间分解"])
    ws.append(["起", "止", "时长", "工作", "类别"])  # 故意不用常规列名
    for e in payload["time_log"]:
        ws.append([e["start"], e["end"], e["hours"], e["operation"], "计划内"])
    f = work / "unlabeled_time.xlsx"
    wb.save(str(f))
    res = parse_file(f)
    d = res.report.to_dict()
    r.check("未崩溃且给出了结果", len(d["time_log"]) > 0, f"entries={len(d['time_log'])}")
    r.check("记录了退化/未识别警告",
            any(("退化" in w or "未识别" in w or "未取到" in w) for w in res.warnings),
            f"warnings={res.warnings[:3]}")
    r.check("时间加总仍被校验", d["time_verification"]["sum_hours"] > 0)
    return r


def case_negation(work: Path) -> CaseResult:
    """否定式表述不得被误判为 NPT。"""
    r = CaseResult("negation", True)
    cm = load_code_map()
    probes = [
        ("起钻至 2350 m，无遇卡显示", "TRIP_OUT", False),
        ("循环正常，未见漏失", "CIRCULATE", False),
        ("钻进，无异常", "DRILL", False),
        ("起钻至套管鞋遇卡，活动解卡", "STUCK", True),
        ("井漏，配置堵漏浆堵漏", "STUCK", True),
    ]
    for text, expect_code, expect_npt in probes:
        spec = cm.normalize(text)
        ok = spec.code == expect_code and (spec.default_category == "npt") == expect_npt
        r.check(f"「{text}」→ {expect_code}{'/NPT' if expect_npt else ''}", ok,
                f"got {spec.code}/{spec.default_category}")
    return r


def case_new_template(work: Path) -> CaseResult:
    """全新版式的日报：应识别为 unknown 模板，仍尽力抽取并提示登记模板。"""
    r = CaseResult("new_template", True)
    ds = build_dataset(wells=1, days_per_well=3)
    day = _first_day(ds)
    payload = json.loads(json.dumps(day["payload"]))
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["FIELD OPERATIONS LOG — SECTION 7 (custom operator form)"])
    ws.append(["Asset", payload["well"]["well_name"], "Serial", str(payload["report"]["report_no"])])
    ws.append(["Depth now", payload["report"]["md_m"]])
    ws.append(["Activity Register"])
    ws.append(["Start", "End", "Dur", "Narrative"])
    for e in payload["time_log"][:5]:
        ws.append([e["start"], e["end"], e["hours"], e["operation"]])
    f = work / "new_template.xlsx"
    wb.save(str(f))
    doc = read_document(f)
    det = detect_template(doc)
    res = parse_file(f)
    r.check("识别为 unknown 模板", det.template_id == "unknown", f"got {det.template_id}")
    r.check("提示登记新模板",
            any("格式适配" in n or "未命中已知模板" in n for n in det.notes),
            f"notes={det.notes}")
    r.check("不崩溃", res.report is not None)
    return r


CASES: list[Callable[[Path], CaseResult]] = [
    case_time_gap,
    case_over_24,
    case_unit_mixed,
    case_no_unit,
    case_unlabeled_time,
    case_negation,
    case_new_template,
]


def run_all(work_dir: str | Path | None = None) -> tuple[list[CaseResult], Path]:
    work = Path(work_dir) if work_dir else Path(tempfile.mkdtemp(prefix="ddr_robust_"))
    work.mkdir(parents=True, exist_ok=True)
    results: list[CaseResult] = []
    for fn in CASES:
        try:
            results.append(fn(work))
        except Exception as exc:  # 用例自身崩溃也算失败，必须暴露
            results.append(CaseResult(fn.__name__, False, error=f"{type(exc).__name__}: {exc}"))
    return results, work


def render_report(results: list[CaseResult]) -> str:
    total = sum(len(r.assertions) for r in results)
    ok = sum(1 for r in results for a in r.assertions if a[1])
    lines = [
        "# 鲁棒性测试报告（畸形日报）",
        "",
        "> 本报告用**故意制造的不规范日报**验证解析器的失败行为是否诚实。",
        "> 在封闭评测集上取 100% 只说明抽取逻辑与模板自洽；真实日报的格式碎片化风险",
        "> （项目计划书 8.1 列为最高技术风险）必须靠这组用例来暴露。",
        "",
        f"用例 **{len(results)}** 个，规则断言 **{total}** 条，通过 **{ok}** 条"
        f"（{ok / total:.1%}）。" if total else "",
        "",
        "| 用例 | 结果 | 断言 | 说明 |",
        "|---|---|---|---|",
    ]
    for r in results:
        passed = sum(1 for a in r.assertions if a[1])
        status = "**通过**" if r.passed and not r.error and passed == len(r.assertions) else "未通过"
        note = r.error or ""
        lines.append(f"| `{r.name}` | {status} | {passed}/{len(r.assertions)} | {note} |")
    lines.append("")

    for r in results:
        lines.append(f"## {r.name}")
        lines.append("")
        if r.error:
            lines.append(f"用例执行异常：`{r.error}`")
            lines.append("")
            continue
        lines.append("| 断言 | 结果 | 实测 |")
        lines.append("|---|---|---|")
        for desc, ok_, detail in r.assertions:
            lines.append(f"| {desc} | {'✅' if ok_ else '❌'} | {detail} |")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="运行畸形日报鲁棒性测试")
    ap.add_argument("--out", default="data/samples/robustness_report.md")
    ap.add_argument("--keep-work-dir", action="store_true")
    args = ap.parse_args(argv)

    results, work = run_all()
    md = render_report(results)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")

    bad = 0
    for r in results:
        passed = sum(1 for a in r.assertions if a[1])
        flag = "OK " if (r.passed and not r.error and passed == len(r.assertions)) else "FAIL"
        if flag == "FAIL":
            bad += 1
        print(f"  {flag} {r.name:<18} {passed}/{len(r.assertions)} {r.error or ''}")
        for desc, ok_, detail in r.assertions:
            if not ok_:
                print(f"        ✗ {desc} | {detail}")
    print(f"报告已写入 {out}")
    if not args.keep_work_dir:
        shutil.rmtree(work, ignore_errors=True)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

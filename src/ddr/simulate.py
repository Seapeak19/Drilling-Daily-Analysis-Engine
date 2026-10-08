"""日报样本模拟器。

为什么需要它：阶段 0 的最大风险是"拿不到真实日报"（见项目计划书 7.3）。
本模块按 IADC 日报标准与行业时间编码体系，合成"格式真实、数值自洽、答案已知"
的日报样本，用于在真实数据到位前跑通并验证解析闭环。

⚠ 数据来源性质声明：本模块生成的**全部**样本均为计算机合成数据，
  不代表任何真实井、真实公司或真实作业记录。井名、公司名均为虚构。
  真实日报到位后，只需把它们放入 samples/ 目录并补充 ground truth，
  评测流程（evaluate.py）即可原样复用，无需改动解析器。

数值自洽性保证：
- 时间分解加总严格等于 24.00 h（按 0.1 h 颗粒度构造）
- 井深逐日单调递增，当日进尺 = 期末井深 − 期初井深
- ROP = 进尺 ÷ 钻进时间
- 钻头进/出井深度与相邻钻头衔接，进尺 = 出井 − 入井

ILT 信号刻意植入：白班接单根耗时显著短于夜班（行业案例中的典型班组差异），
且单次差异很小（分钟级）——这正是传统 15–30 分钟精度日报无法捕捉的部分。
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .codes import load_code_map

# --------------------------------------------------------------------- 数据池
WELL_POOL: list[dict[str, Any]] = [
    {
        "well_name": "PL-6-2-A12H",
        "field": "蓬莱 19-3",
        "operator": "中海石油（中国）有限公司天津分公司",
        "contractor": "中海油田服务股份有限公司",
        "rig": "海洋石油 942",
        "country": "中国",
        "state": "渤海",
        "api_number": "CN-BH-PL6-2A12H",
        "latitude": 38.4217,
        "longitude": 120.0833,
        "elevation_ground_m": 26.5,
        "elevation_kelly_bushing_m": 52.0,
        "hole_diameter_in": 12.25,
        "mud_type": "KCl-聚合物钻井液",
        "unit_system": "metric",
        "tz": "+08:00",
        # 白班接单根快、夜班慢（ILT 可识别差异）；均值保持在 0.2 h 颗粒度附近，
        # 使日报条目与接单根次数一一对应（见 _connection_blocks 的口径说明）。
        "connection_minutes": {"day": 9.5, "night": 12.5},
    },
    {
        "well_name": "15/9-F-14",
        "field": "Volve",
        "operator": "Equinor Energy AS",
        "contractor": "Maersk Drilling",
        "rig": "Maersk Inspirer",
        "country": "Norway",
        "state": "North Sea",
        "api_number": "NO-15/9-F-14",
        "latitude": 58.4333,
        "longitude": 1.9,
        "elevation_ground_m": 0.0,
        "elevation_kelly_bushing_m": 32.0,
        "hole_diameter_in": 8.5,
        "mud_type": "SOBM",
        "unit_system": "imperial",  # 英制样本，用于验证单位归一
        "tz": "+01:00",
        "connection_minutes": {"day": 14.0, "night": 18.0},
    },
    {
        "well_name": "苏 48-12-66X",
        "field": "苏里格",
        "operator": "长庆油田分公司",
        "contractor": "川庆钻探工程有限公司",
        "rig": "川庆 5060",
        "country": "中国",
        "state": "内蒙古",
        "api_number": "CN-SLG-S48-12-66X",
        "latitude": 38.9872,
        "longitude": 108.7211,
        "elevation_ground_m": 1298.0,
        "elevation_kelly_bushing_m": 1304.5,
        "hole_diameter_in": 8.5,
        "mud_type": "聚合物钻井液",
        "unit_system": "metric",
        "tz": "+08:00",
        "connection_minutes": {"day": 10.0, "night": 11.5},
    },
]

# 井段工况的时间块模板：(描述模板, 操作码, 时长区间)
PHASE_BLOCK_SPECS: dict[str, list[tuple[str, str, tuple[float, float]]]] = {
    "drilling": [
        ("旋转钻进 {hd}″ 井段，钻压 {wob} kN，转速 {rpm} rpm", "DRILL", (10.0, 14.0)),
        ("__CONNECTIONS__", "DRILL", (0.0, 0.0)),
        ("循环洗井至井底返出，观察返砂", "CIRCULATE", (1.0, 2.0)),
        ("短起下，划眼通井", "TRIP_OUT", (1.0, 2.5)),
        ("下钻到底", "TRIP_IN", (1.0, 2.5)),
        ("MWD 测斜", "LOGGING", (0.3, 0.8)),
    ],
    "trip_out": [
        ("起钻（起立柱）", "TRIP_OUT", (11.0, 15.0)),
        ("更换钻头、检查钻具", "BIT_CHANGE", (1.0, 2.0)),
        ("循环洗井，处理井筒", "CIRCULATE", (1.5, 3.0)),
        ("安全检查与井控装备检查", "TEST", (0.5, 1.5)),
    ],
    "trip_in": [
        ("下钻（下立柱）", "TRIP_IN", (10.0, 14.0)),
        ("循环洗井，恢复泥浆性能", "CIRCULATE", (1.0, 2.0)),
        ("旋转钻进 {hd}″ 井段（恢复钻进）", "DRILL", (2.0, 5.0)),
        ("__CONNECTIONS__", "DRILL", (0.0, 0.0)),
        ("短起下检查井眼", "TRIP_OUT", (0.5, 1.5)),
    ],
    "casing": [
        ("通井划眼，确认井眼畅通", "REAM", (2.0, 3.5)),
        ("下套管（套管串组装）", "CASING", (10.0, 13.0)),
        ("固井作业，注水泥", "CEMENT", (4.0, 6.0)),
        ("候凝，安装井口", "BOP", (1.0, 2.5)),
        ("套管试压", "TEST", (1.0, 2.0)),
    ],
    "surface": [
        ("安装井口与防喷器", "BOP", (4.0, 7.0)),
        ("防喷器功能试验与试压", "TEST", (2.0, 4.0)),
        ("建立循环、配浆", "MUD_COND", (2.0, 4.0)),
        ("表层钻进（444.5 mm 井眼）", "DRILL", (4.0, 8.0)),
        ("__CONNECTIONS__", "DRILL", (0.0, 0.0)),
    ],
}

EQUIPMENT_NPT: list[tuple[str, str, float, str, str]] = [
    ("顶驱 VFD 故障，等待电气师排查", "REPAIR", 2.5, "equipment", "contractor"),
    ("1 号泥浆泵活塞刺漏，更换缸套与活塞", "REPAIR", 3.0, "equipment", "contractor"),
    ("振动筛筛布破损，更换筛布", "REPAIR", 1.0, "equipment", "contractor"),
    ("绞车刹车系统保养，更换刹车块", "REPAIR", 2.0, "equipment", "contractor"),
    ("2 号柴油发电机组水温高，降负荷检修", "REPAIR", 1.5, "equipment", "contractor"),
    ("液压大钳扭矩不足，更换液压马达", "REPAIR", 2.0, "equipment", "contractor"),
]
WELLBORE_NPT: list[tuple[str, str, float, str, str]] = [
    ("井漏，配置堵漏浆堵漏", "STUCK", 4.0, "wellbore", "operator"),
    ("起钻至套管鞋遇卡，活动解卡", "STUCK", 3.5, "wellbore", "operator"),
    ("循环返出掉块，提高粘度带砂", "STUCK", 2.0, "wellbore", "operator"),
    ("气测异常，关井观察后循环排气", "WELL_CONTROL", 2.5, "wellbore", "operator"),
    ("钻杆本体刺漏，起钻更换钻杆", "REPAIR", 5.0, "equipment", "contractor"),
    ("钻具落井，下入打捞筒打捞", "FISH", 6.5, "wellbore", "contractor"),
]
WEATHER_NPT: list[tuple[str, str, float, str, str]] = [
    ("大风，吊装作业暂停", "WEATHER", 3.0, "weather", "force_majeure"),
    ("涌浪超限，隔水管张力调整并暂停作业", "WEATHER", 4.0, "weather", "force_majeure"),
]
LOGISTICS_NPT: list[tuple[str, str, float, str, str]] = [
    ("等待钻头到货，配件在途", "WAIT", 6.0, "logistics", "third_party"),
    ("等待地质指令，甲方未下达中完决策", "WAIT", 2.5, "logistics", "operator"),
    ("等第三方固井队到场", "WAIT", 3.0, "third_party", "third_party"),
]

REMARK_TEMPLATES = [
    "{t} 钻进至 {md:.2f} m，钻压 {wob:.0f} kN，转速 {rpm:.0f} rpm，排量 {flow:.0f} L/s，钻时 {rop:.1f} m/h。",
    "{t} 接单根，耗时 {conn:.0f} 分钟。",
    "{t} 循环调整泥浆性能，密度提至 {dens:.2f} g/cm3，漏斗粘度 {vis:.0f} s。",
    "{t} 起钻至 {md:.2f} m，期间无遇卡显示。",
    "{t} 地质录井汇报：岩性为灰色细砂岩，荧光显示 {fluor} 级。",
    "{t} 短起下检查井眼，返砂正常。",
    "{t} 安全检查：井控装备完好，防喷器控制系统压力正常。",
]

SHIFTS = ("day", "night")

PHASE_TEMPLATE_ORDER = ("cn_vertical", "iadc_classic", "regional_xls")


def template_for_phase(phase: str) -> str:
    """给工况分配日报模板。

    按**工况**而不是按日期分配，是为了保证同一天在不同格式下承载同一批数据：
    若按日期轮换，同一份日报在 PDF 版与 Excel 版里的钻头/泥浆记录会不一致，
    评测时无法判断是"解析错误"还是"数据本来就不同"。
    """
    order = ("surface", "drilling", "trip_out", "trip_in", "casing")
    idx = order.index(phase) if phase in order else len(order)
    return PHASE_TEMPLATE_ORDER[idx % len(PHASE_TEMPLATE_ORDER)]



@dataclass
class SimDay:
    day_index: int
    report_date: date
    payload: dict[str, Any] = field(default_factory=dict)
    truth: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass
class SimWell:
    well: dict[str, Any]
    days: list[SimDay] = field(default_factory=list)


# ------------------------------------------------------------------ 小时工具
def h2clock(h: float) -> str:
    h = min(max(h, 0.0), 24.0)
    hh = int(h)
    mm = int(round((h - hh) * 60))
    if mm == 60:
        hh, mm = hh + 1, 0
    return f"{hh:02d}:{mm:02d}"


def _q10(x: float) -> float:
    """对齐到 0.1 h 颗粒度。"""
    return round(x * 10) / 10.0


# ------------------------------------------------------------------ 时间分解构造
def _materialize(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 (描述, 码, 时长) 块序列铺满 24.00 h，并按 0.1 h 对齐。

    最后一个带 durations 的块吸收全部余量，保证加总严格等于 24.00 h。
    """
    entries: list[dict[str, Any]] = []
    cursor = 0.0
    for blk in blocks:
        h = _q10(float(blk["hours"]))
        if h < 0.1:
            continue
        entries.append({**blk, "hours": h, "start": h2clock(cursor), "end": h2clock(cursor + h)})
        cursor = _q10(cursor + h)
        if cursor >= 24.0:
            break
    slack = _q10(24.0 - cursor)
    if entries and slack >= 0.1:
        entries[-1]["hours"] = _q10(entries[-1]["hours"] + slack)
        entries[-1]["end"] = "24:00"
    elif entries and slack < 0:
        # 超时则从最长的可缩放块里扣减
        over = -slack
        order = sorted(range(len(entries)), key=lambda i: -entries[i]["hours"])
        for i in order:
            if over < 0.1:
                break
            take = min(over, max(0.0, _q10(entries[i]["hours"] - 0.1)))
            entries[i]["hours"] = _q10(entries[i]["hours"] - take)
            over = _q10(over - take)
        cursor = 0.0
        for e in entries:
            e["start"] = h2clock(cursor)
            cursor = _q10(cursor + e["hours"])
            e["end"] = h2clock(cursor)
        entries = [e for e in entries if e["hours"] >= 0.1]
        if entries:
            entries[-1]["end"] = "24:00"
    return entries


def _connection_blocks(
    rng: random.Random, count: int, minutes_by_shift: dict[str, float]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """生成接单根时间块（按接单根耗时分布拆分），并返回已知答案。

    返回 (时间块列表, 单次接单根明细列表)。
    单次耗时围绕班次基准做 ±1.5 min 抖动，白夜班差异刻意保留。

    口径说明（重要）：
    日报按时长整数分钟记录时，接单根这类 6–25 分钟的动作恰好能精确表达；
    但本模拟器把时长对齐到 0.1 h（6 min）颗粒度以便时间分解严格加总，
    因此明细里同时给出 `minutes`（原始分钟，真实值）与 `hours`（日报颗粒度值）。
    评测解析器时用 hours；做 ILT 分布分析时要意识到 6 min 的量化下限。
    """
    blocks: list[dict[str, Any]] = []
    detail: list[dict[str, Any]] = []
    if count <= 0:
        return blocks, detail
    for i in range(count):
        shift = SHIFTS[i % 2]  # 交替倒班，保证白夜班样本量可比
        base = minutes_by_shift.get(shift, 12.0)
        # 下限 9 分钟：低于 8.7 min 时 0.1 h 颗粒度会把两次接单根挤成同一条,
        # 导致"日报条目数"与"接单根次数"不一致（数据集自检抓到的真实缺陷）。
        minutes = round(min(21.0, max(9.0, rng.gauss(base, 1.0))), 1)
        hours = max(0.1, _q10(minutes / 60.0))
        blocks.append(
            {
                "operation": f"接单根（{'白班' if shift == 'day' else '夜班'}）",
                "code": "DRILL",
                "hours": hours,
                "npt_category": None,
                "npt_responsibility": None,
                "ilt_action": "connection",
                "connection_count": 1,
                "shift": shift,
            }
        )
        detail.append({"seq": i + 1, "shift": shift, "minutes": minutes, "hours": hours})
    return blocks, detail


def _build_day_blocks(
    rng: random.Random, well: dict[str, Any], phase: str, footage: float
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """按工况生成一天的完整时间块，并记录已知答案。"""
    hd = 17.5 if phase == "surface" else well["hole_diameter_in"]
    # 只有含 __CONNECTIONS__ 的工况才会真的发生接单根。
    # 起钻/下套管日即使有少量进尺，也不该凭空记出接单根条目（数据集自检抓到的真实缺陷）。
    phase_has_connections = any(desc == "__CONNECTIONS__" for desc, _c, _r in PHASE_BLOCK_SPECS[phase])
    count = max(0, int(round(footage / 9.5))) if (footage > 0 and phase_has_connections) else 0
    conn_blocks, conn_detail = _connection_blocks(rng, count, well["connection_minutes"])
    conn_hours = _q10(sum(b["hours"] for b in conn_blocks))

    blocks: list[dict[str, Any]] = []
    for desc, code, (lo, hi) in PHASE_BLOCK_SPECS[phase]:
        if desc == "__CONNECTIONS__":
            blocks.extend(conn_blocks)
            continue
        hours = rng.uniform(lo, hi) if hi > lo else lo
        blocks.append(
            {
                "operation": desc.format(hd=hd, wob=rng.randint(60, 140), rpm=rng.randint(80, 180)),
                "code": code,
                "hours": _q10(hours),
                "npt_category": None,
                "npt_responsibility": None,
                "ilt_action": "trip_stand" if code in ("TRIP_IN", "TRIP_OUT") else None,
                "connection_count": None,
                "shift": None,
            }
        )

    # 注入 NPT
    npt_events: list[dict[str, Any]] = []
    base_rate = well.get("npt_probability", 0.45)
    for pool, prob in (
        (EQUIPMENT_NPT, base_rate),
        (WELLBORE_NPT, base_rate * 0.5),
        (WEATHER_NPT, base_rate * 0.3),
        (LOGISTICS_NPT, base_rate * 0.3),
    ):
        if rng.random() < prob:
            op_t, code, hours, cat, resp = rng.choice(pool)
            blocks.append(
                {
                    "operation": op_t,
                    "code": code,
                    "hours": _q10(hours),
                    "npt_category": cat,
                    "npt_responsibility": resp,
                    "ilt_action": None,
                    "connection_count": None,
                    "shift": None,
                }
            )
            npt_events.append(
                {"operation": op_t, "hours": _q10(hours), "code": code, "npt_category": cat, "npt_responsibility": resp}
            )

    # 计算固定块占用，余量给最长的钻进块（无钻进块时给最长的可延长块）
    fixed = _q10(sum(b["hours"] for b in blocks))
    if fixed > 24.0:
        # 固定块超时：按比例压缩非 NPT 块（NPT 时长是事实，不应被压缩）
        non_npt = [i for i, b in enumerate(blocks) if not b["npt_category"]]
        excess = _q10(fixed - 24.0)
        pool = _q10(sum(blocks[i]["hours"] for i in non_npt))
        if pool > excess:
            scale = (pool - excess) / pool
            for i in non_npt:
                blocks[i]["hours"] = max(0.1, _q10(blocks[i]["hours"] * scale))
        fixed = _q10(sum(b["hours"] for b in blocks))
    remainder = _q10(24.0 - fixed)
    if remainder >= 0.1:
        drill_pool = [i for i, b in enumerate(blocks) if b["code"] in ("DRILL", "REAM") and not b["npt_category"]]
        target = (
            max(drill_pool, key=lambda i: blocks[i]["hours"])
            if drill_pool
            else max(range(len(blocks)), key=lambda i: (not blocks[i]["npt_category"], blocks[i]["hours"]))
        )
        blocks[target]["hours"] = _q10(blocks[target]["hours"] + remainder)

    entries = _materialize(blocks)
    # 回填类别：真实日报的"时间类别/Class"列按操作码语义填写，这里用同一套
    # code-map 语义标注，使解析器可以评测该列是否正确抽取。
    cm = load_code_map()
    for e in entries:
        e["category"] = "npt" if e.get("npt_category") else cm.category_of(e["code"])

    # 明细与"最终落在日报里的条目"对齐（材料化可能压缩/吸收余量，导致时长微调）。
    # 只用操作描述匹配，不在 truth 里再存一份可能与日报不符的时长。
    materialized_conn = [e for e in entries if e.get("ilt_action") == "connection"]
    for det, ent in zip(conn_detail, materialized_conn):
        det["hours"] = ent["hours"]
        det["start"] = ent.get("start")
        det["end"] = ent.get("end")

    truth = {
        "phase": phase,
        "hole_diameter_in": hd,
        "npt_events": npt_events,
        "connections": conn_detail,
        "connection_count": len(materialized_conn),
        "quantization_note": (
            "日报时长按 0.1 h（6 min）颗粒度记录，分钟级接单根耗时存在量化误差。"
            "原始分钟值见 connections[].minutes，日报颗粒度值见 connections[].hours。"
        ),
    }
    return entries, truth


# ------------------------------------------------------------------ 井级模拟
def simulate_well(
    well_info: dict[str, Any],
    *,
    seed: int = 20261008,
    days: int = 7,
    start_date: date | None = None,
    npt_probability: float = 0.45,
) -> SimWell:
    rng = random.Random(seed)
    well = {**well_info, "npt_probability": npt_probability}
    start_date = start_date or date(2026, 3, 1)
    tz = well.get("tz", "+08:00")
    sw = SimWell(well=well)

    md = round(rng.uniform(1800.0, 2200.0), 2)
    spud_days = rng.uniform(8.0, 22.0)
    bit_no = 1
    bit_in = round(md - rng.uniform(300.0, 700.0), 2)
    bit_hours_acc = 0.0
    bits: list[dict[str, Any]] = []

    # 阶段序列：表层 → 钻进 → 起钻 → 钻进 → 下钻 → 钻进 → 下套管
    phase_plan: list[str] = []
    for i in range(days):
        if i == 0:
            phase_plan.append("surface")
        elif i == days - 1:
            phase_plan.append("casing")
        elif i in (2, 5):
            phase_plan.append("trip_out")
        elif i in (3, 6) and i < days - 1:
            phase_plan.append("trip_in")
        else:
            phase_plan.append("drilling")

    for i, phase in enumerate(phase_plan):
        report_date = start_date + timedelta(days=i)
        md_in_start = round(md, 2)

        footage = {
            "drilling": lambda: round(rng.uniform(120.0, 320.0), 2),
            "surface": lambda: round(rng.uniform(60.0, 160.0), 2),
            "trip_out": lambda: round(rng.uniform(0.0, 30.0), 2),
            "trip_in": lambda: round(rng.uniform(20.0, 80.0), 2),
            "casing": lambda: 0.0,
        }[phase]()
        md = round(md_in_start + footage, 2)

        entries, day_truth = _build_day_blocks(rng, well, phase, footage)
        drill_hours = round(sum(e["hours"] for e in entries if e["code"] in ("DRILL", "REAM")), 2)
        rop = round(footage / drill_hours, 2) if drill_hours > 0 and footage > 0 else None

        # --- 钻头：起钻日披露上一只钻头出井，并换新钻头入井
        if phase == "trip_out":
            out_depth = round(md_in_start - rng.uniform(0.0, 15.0), 2)
            rec = _make_bit_record(rng, well, bit_no, bit_in, out_depth, bit_hours_acc + rng.uniform(20.0, 60.0))
            rec["_attach_day"] = i
            bits.append(rec)
            bit_no += 1
            bit_in = out_depth
            bit_hours_acc = 0.0
        if phase in ("drilling", "surface") and footage > 0:
            bit_hours_acc += drill_hours

        # --- 泥浆
        dens = round(rng.uniform(1.08, 1.32), 2)
        mud = [
            {
                "sample_point": "flowline",
                "depth_m": round(md, 1),
                "density_gcc": dens,
                "funnel_viscosity_s": float(rng.randint(38, 58)),
                "pv_mpas": float(rng.randint(12, 26)),
                "yp_pa": round(rng.uniform(4.0, 12.0), 1),
                "gel_10s_pa": round(rng.uniform(2.0, 5.0), 1),
                "gel_10min_pa": round(rng.uniform(5.0, 14.0), 1),
                "fl_ml": round(rng.uniform(3.0, 6.0), 1),
                "ph": round(rng.uniform(8.0, 9.8), 1),
                "chlorides_mg_l": float(rng.randint(3000, 22000)),
                "sand_pct": round(rng.uniform(0.1, 0.6), 2),
                "solids_pct": round(rng.uniform(8.0, 18.0), 1),
                "mud_type": well["mud_type"],
                "volume_m3": round(rng.uniform(120.0, 260.0), 1),
            },
            {
                "sample_point": "suction",
                "depth_m": round(md, 1),
                "density_gcc": round(dens - rng.uniform(0.0, 0.02), 2),
                "funnel_viscosity_s": float(rng.randint(36, 55)),
                "fl_ml": round(rng.uniform(3.0, 6.0), 1),
                "ph": round(rng.uniform(8.0, 9.8), 1),
                "mud_type": well["mud_type"],
            },
        ]

        # --- 备注
        remarks: list[dict[str, Any]] = []
        clock = 6
        for k in range(rng.randint(5, 8)):
            tpl = REMARK_TEMPLATES[k % 4] if k < 3 else rng.choice(REMARK_TEMPLATES)
            txt = tpl.format(
                t=f"{clock:02d}:{rng.choice(['00', '15', '30', '45'])}",
                md=max(0.0, md - rng.uniform(0.0, max(footage, 1.0))),
                wob=rng.uniform(60, 140),
                rpm=rng.uniform(80, 180),
                flow=rng.uniform(28, 38),
                rop=rng.uniform(8, 20),
                conn=well["connection_minutes"]["night"],
                dens=rng.uniform(1.08, 1.32),
                vis=rng.uniform(38, 58),
                fluor=rng.choice(["1", "2", "3"]),
            )
            remarks.append({"seq": k + 1, "time_hint": f"{clock:02d}:00", "text": txt})
            clock += rng.randint(2, 3)
            if clock >= 24:
                break

        payload = {
            "well": {
                k: well[k]
                for k in (
                    "well_name",
                    "field",
                    "operator",
                    "contractor",
                    "rig",
                    "country",
                    "state",
                    "api_number",
                    "latitude",
                    "longitude",
                    "elevation_ground_m",
                    "elevation_kelly_bushing_m",
                    "mud_type",
                )
                if k in well
            },
            "report": {
                "report_no": i + 1,
                "report_type": "DDR",
                "dtim_start": f"{report_date.isoformat()}T00:00:00{tz}",
                "dtim_end": f"{(report_date + timedelta(days=1)).isoformat()}T00:00:00{tz}",
                "day_number": i + 1,
                "md_m": md,
                "md_in_start_m": md_in_start,
                "progress_m": round(md - md_in_start, 2),
                "tvd_m": round(md * rng.uniform(0.86, 0.99), 2),
                "hole_diameter_in": day_truth["hole_diameter_in"],
                "dtim_spud": f"{(report_date - timedelta(days=int(spud_days))).isoformat()}T08:00:00{tz}",
                "etim_spud_days": round(spud_days + i, 1),
                "rop_av_m_per_h": rop,
                "dist_drill_m": footage if footage > 0 else None,
                "etim_drill_h": drill_hours,
                "etim_ream_h": round(sum(e["hours"] for e in entries if e["code"] == "REAM"), 2),
                "well_status": {
                    "drilling": "钻进",
                    "trip_out": "起钻",
                    "trip_in": "下钻",
                    "casing": "下套管固井",
                    "surface": "表层作业",
                }[phase],
                "sum_24hr": f"{report_date.strftime('%m月%d日')} 00:00–24:00："
                + "；".join(e["operation"] for e in entries[:4])
                + "。",
                "plan_24hr": "继续钻进至设计中完井深，视情况调整泥浆性能。",
                "unit_system": well.get("unit_system", "metric"),
            },
            "time_log": entries,
            "bit_records": [],
            "mud": mud,
            "remarks": remarks,
            "time_verification": {"sum_hours": round(sum(e["hours"] for e in entries), 2), "valid": True},
        }

        truth = {
            "report_date": report_date.isoformat(),
            "report_no": i + 1,
            "md_m": md,
            "md_in_start_m": md_in_start,
            "tvd_m": payload["report"]["tvd_m"],
            "progress_m": payload["report"]["progress_m"],
            "etim_drill_h": drill_hours,
            "rop_av_m_per_h": rop,
            "sum_hours": round(sum(e["hours"] for e in entries), 2),
            "time_log": [
                {
                    "hours": e["hours"],
                    "code": e["code"],
                    "operation": e["operation"],
                    "npt_category": e["npt_category"],
                }
                for e in entries
            ],
            "npt_hours": round(sum(e["hours"] for e in entries if e["npt_category"]), 2),
            "npt_events": day_truth["npt_events"],
            "connections": day_truth["connections"],
            "connection_count": day_truth["connection_count"],
            "mud": {
                "density_gcc": mud[0]["density_gcc"],
                "funnel_viscosity_s": mud[0]["funnel_viscosity_s"],
                "fl_ml": mud[0]["fl_ml"],
            },
            # 以下字段写在日报里，答案也必须带上：
            # 答案不完整会让"字段可用率"统计失真（曾把 21/21 有值的字段报成 0/21）。
            "hole_diameter_in": payload["report"]["hole_diameter_in"],
            "etim_spud_days": payload["report"]["etim_spud_days"],
            "well_status": payload["report"]["well_status"],
            "dtim_spud": payload["report"]["dtim_spud"],
            "remarks": payload["remarks"],
            "phase": phase,
            "template_id": template_for_phase(phase),
            "unit_system": well.get("unit_system", "metric"),
        }
        sw.days.append(
            SimDay(
                day_index=i,
                report_date=report_date,
                payload=payload,
                truth=truth,
                notes=[f"阶段：{phase}", f"合成数据 seed={seed}"],
            )
        )

    # 钻头记录挂到对应日（换钻头那天披露上一只）
    for b in bits:
        idx = min(len(sw.days) - 1, int(b.pop("_attach_day", 0)))
        sw.days[idx].payload["bit_records"].append(b)
        sw.days[idx].truth.setdefault("bit_records", []).append(b)
    return sw


def _make_bit_record(
    rng: random.Random,
    well: dict[str, Any],
    bit_no: int,
    depth_in: float,
    depth_out: float,
    hours: float,
) -> dict[str, Any]:
    footage = round(depth_out - depth_in, 2)
    hours = round(hours, 1)
    wob = float(rng.randint(60, 140))
    rec = {
        "bit_no": bit_no,
        "size_in": well["hole_diameter_in"],
        "make": rng.choice(["Smith", "Halliburton", "Varel", "Kingdream", "SLB"]),
        "model": rng.choice(["MDi516", "HDD505", "FM3941", "MSi616", "SDi713"]),
        "type": "PDC",
        "iadc_code": rng.choice(["M323", "M223", "S423", "M332"]),
        "nozzles_32nds": rng.choice(["3x12+1x13", "4x12", "5x11+1x12", "3x13+1x14"]),
        "depth_in_m": round(depth_in, 2),
        "depth_out_m": round(depth_out, 2),
        "footage_m": footage,
        "hours": hours,
        "rop_m_per_h": round(footage / hours, 2) if hours else None,
        "wob_kgf": wob,
        "rpm": float(rng.randint(80, 180)),
        "flow_lps": round(rng.uniform(28.0, 38.0), 1),
        "dull_grade": rng.choice(
            ["2-3-WT-S-X-I-NO-TD", "1-2-CT-M-X-I-NO-PR", "3-4-BT-S-X-I-HR-TD", "2-2-WT-M-X-I-NO-TD"]
        ),
        "hours_source": "reported",
        "_attach_day": 0,
    }
    return rec


def build_dataset(
    *,
    wells: int = 3,
    days_per_well: int = 7,
    seed: int = 20261008,
    npt_probability: float = 0.45,
) -> dict[str, Any]:
    rng = random.Random(seed)
    dataset: dict[str, Any] = {
        "_disclaimer": (
            "本数据集全部为计算机合成数据，按 IADC 日报标准构造，用于在真实日报到位前"
            "验证解析闭环。井名、公司名、数值均为虚构，不代表任何真实井或真实作业记录。"
            "真实样本到位后请另建目录并在 dataset.json 中登记，评测流程无需改动。"
        ),
        "generated_seed": seed,
        "generated_days_per_well": days_per_well,
        "units_note": "15/9-F-14（Volve 风格）为英制样本，用于验证单位归一；其余为公制。",
        "ilt_note": "接单根耗时按白班/夜班区分并植入班组差异，用于验证 ILT 识别能力。",
        "samples": [],
    }
    for w in WELL_POOL[:wells]:
        sw = simulate_well(w, seed=rng.randint(1, 10**6), days=days_per_well, npt_probability=npt_probability)
        dataset["samples"].append(
            {
                "well": sw.well,
                "days": [
                    {
                        "day_index": d.day_index,
                        "report_date": d.report_date.isoformat(),
                        "payload": d.payload,
                        "truth": d.truth,
                        "notes": d.notes,
                    }
                    for d in sw.days
                ],
            }
        )
    return dataset


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成合成钻井日报数据集（IADC 标准）")
    ap.add_argument("--out", default="data/samples/dataset.json")
    ap.add_argument("--wells", type=int, default=3)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--seed", type=int, default=20261008)
    ap.add_argument("--npt-probability", type=float, default=0.45)
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ds = build_dataset(
        wells=args.wells, days_per_well=args.days, seed=args.seed, npt_probability=args.npt_probability
    )
    out.write_text(json.dumps(ds, ensure_ascii=False, indent=2), encoding="utf-8")
    n = sum(len(w["days"]) for w in ds["samples"])
    print(f"已生成 {len(ds['samples'])} 口井 / {n} 份日报 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

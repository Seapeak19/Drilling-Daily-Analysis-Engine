"""数据集完整性自检。

为什么必须有它：ground truth 是评测的标尺，如果标尺本身与日报内容不一致，
准确率数字就是假的。这里把"答案必须等于日报里印着的东西"变成机器可验证的约束。

检查项：
1. 每天时间条目加总 = 24.00 h；
2. truth.time_log 与 payload.time_log 逐条一致（时长、操作码、操作描述）；
3. truth.connection_count / connections 与 payload 中接单根条目一致；
4. truth.npt_hours / npt_events 与 payload 中 NPT 条目一致；
5. truth.md_m / progress_m 与 payload 表头一致，且井深逐日单调不减；
6. truth.bit_records 与 payload 钻头记录一致，且进尺 = 出井 − 入井；
7. truth.mud 与 payload 泥浆（出口取样点）一致。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONNECTION_KEYS = ("接单根", "接立柱", "接钻杆", "connection")


def is_connection_entry(entry: dict[str, Any]) -> bool:
    op = str(entry.get("operation") or "")
    return any(k in op for k in CONNECTION_KEYS)


def is_npt_entry(entry: dict[str, Any]) -> bool:
    return bool(entry.get("npt_category")) or entry.get("category") == "npt"


@dataclass
class CheckResult:
    problems: list[str] = field(default_factory=list)
    checked_days: int = 0
    checked_wells: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems

    def add(self, where: str, msg: str) -> None:
        self.problems.append(f"{where}: {msg}")


def _approx(a: float | None, b: float | None, tol: float = 0.011) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= tol


def check_dataset(dataset: dict[str, Any]) -> CheckResult:
    res = CheckResult()
    last_md: dict[str, float] = {}

    for w in dataset.get("samples", []):
        res.checked_wells += 1
        wname = w["well"].get("well_name", "?")
        for d in w["days"]:
            res.checked_days += 1
            payload = d["payload"]
            truth = d["truth"]
            where = f"{wname} {d['report_date']}"

            entries = payload.get("time_log") or []
            reps = payload.get("report") or {}

            # 1. 24 小时加总
            total = round(sum(float(e.get("hours") or 0) for e in entries), 2)
            if not _approx(total, 24.0):
                res.add(where, f"时间分解加总 {total} ≠ 24.00（IADC 硬约束）")

            # 2. truth.time_log 与 payload 一致
            tlog = truth.get("time_log") or []
            if len(tlog) != len(entries):
                res.add(where, f"truth.time_log 条数 {len(tlog)} ≠ payload {len(entries)}")
            else:
                for i, (te, pe) in enumerate(zip(tlog, entries)):
                    if not _approx(te.get("hours"), pe.get("hours")):
                        res.add(where, f"time_log[{i}] truth {te.get('hours')} ≠ payload {pe.get('hours')}")
                    if te.get("code") != pe.get("code"):
                        res.add(where, f"time_log[{i}] 操作码 truth {te.get('code')} ≠ payload {pe.get('code')}")

            # 3. 接单根
            conn_entries = [e for e in entries if is_connection_entry(e)]
            if truth.get("connection_count") != len(conn_entries):
                res.add(
                    where,
                    f"connection_count truth {truth.get('connection_count')} ≠ payload {len(conn_entries)}",
                )
            conn_details = truth.get("connections") or []
            if len(conn_details) != len(conn_entries):
                res.add(where, f"connections 明细 {len(conn_details)} ≠ payload 接单根条目 {len(conn_entries)}")
            else:
                th = round(sum(float(c.get("hours") or 0) for c in conn_details), 2)
                ph = round(sum(float(e.get("hours") or 0) for e in conn_entries), 2)
                if not _approx(th, ph, 0.02):
                    res.add(where, f"接单根总时长 truth {th} ≠ payload {ph}")

            # 4. NPT
            npt_entries = [e for e in entries if is_npt_entry(e)]
            npt_h = round(sum(float(e.get("hours") or 0) for e in npt_entries), 2)
            if not _approx(truth.get("npt_hours"), npt_h, 0.02):
                res.add(where, f"npt_hours truth {truth.get('npt_hours')} ≠ payload {npt_h}")
            if len(truth.get("npt_events") or []) != len(npt_entries):
                res.add(
                    where,
                    f"npt_events 条数 {len(truth.get('npt_events') or [])} ≠ payload NPT 条目 {len(npt_entries)}",
                )

            # 5. 井深与进尺
            if not _approx(truth.get("md_m"), reps.get("md_m"), 0.005):
                res.add(where, f"truth.md_m {truth.get('md_m')} ≠ payload {reps.get('md_m')}")
            if not _approx(truth.get("progress_m"), reps.get("progress_m"), 0.005):
                res.add(where, f"truth.progress_m {truth.get('progress_m')} ≠ payload {reps.get('progress_m')}")
            md = reps.get("md_m")
            if md is not None:
                prev = last_md.get(wname)
                if prev is not None and md + 1e-6 < prev:
                    res.add(where, f"井深倒退：{md} < 前一日 {prev}")
                last_md[wname] = md
            # 进尺一致性
            if reps.get("md_in_start_m") is not None and md is not None:
                calc = round(md - reps["md_in_start_m"], 2)
                if not _approx(calc, reps.get("progress_m"), 0.02):
                    res.add(where, f"进尺校验失败：md − md_in_start = {calc} ≠ progress {reps.get('progress_m')}")

            # 6. 钻头记录
            tbits = truth.get("bit_records") or []
            pbits = payload.get("bit_records") or []
            if len(tbits) != len(pbits):
                res.add(where, f"bit_records 条数 truth {len(tbits)} ≠ payload {len(pbits)}")
            for tb, pb in zip(tbits, pbits):
                if not _approx(tb.get("footage_m"), pb.get("footage_m"), 0.02):
                    res.add(where, f"钻头进尺 truth {tb.get('footage_m')} ≠ payload {pb.get('footage_m')}")
                din, dout = pb.get("depth_in_m"), pb.get("depth_out_m")
                if din is not None and dout is not None and pb.get("footage_m") is not None:
                    if not _approx(round(dout - din, 2), pb["footage_m"], 0.02):
                        res.add(where, f"钻头进尺 ≠ 出井 − 入井（{dout} − {din}）")
                if tb.get("dull_grade") != pb.get("dull_grade"):
                    res.add(where, "dull_grade truth 与 payload 不一致")

            # 7. 泥浆（出口）
            tmud = truth.get("mud") or {}
            flow = next((m for m in (payload.get("mud") or []) if m.get("sample_point") == "flowline"), None)
            if flow is None:
                res.add(where, "payload 缺少出口泥浆记录")
            else:
                if not _approx(tmud.get("density_gcc"), flow.get("density_gcc"), 0.005):
                    res.add(where, f"泥浆密度 truth {tmud.get('density_gcc')} ≠ payload {flow.get('density_gcc')}")
                if not _approx(tmud.get("fl_ml"), flow.get("fl_ml"), 0.02):
                    res.add(where, f"泥浆失水 truth {tmud.get('fl_ml')} ≠ payload {flow.get('fl_ml')}")
                if not _approx(tmud.get("funnel_viscosity_s"), flow.get("funnel_viscosity_s"), 0.02):
                    res.add(
                        where,
                        f"泥浆粘度 truth {tmud.get('funnel_viscosity_s')} ≠ payload {flow.get('funnel_viscosity_s')}",
                    )

    return res


def check_dataset_file(path: str | Path) -> CheckResult:
    return check_dataset(json.loads(Path(path).read_text(encoding="utf-8")))


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="检查合成数据集 ground truth 与日报内容是否一致")
    ap.add_argument("--data", default="data/samples/dataset.json")
    args = ap.parse_args(argv)
    res = check_dataset_file(args.data)
    print(
        f"已检查 {res.checked_wells} 口井 / {res.checked_days} 份日报，"
        f"问题 {len(res.problems)} 处"
    )
    for p in res.problems[:40]:
        print("  ✗", p)
    if len(res.problems) > 40:
        print(f"  ... 其余 {len(res.problems) - 40} 处略")
    return 0 if res.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

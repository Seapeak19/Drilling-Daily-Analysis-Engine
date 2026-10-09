"""私有数据闸门测试。

## 为什么这组测试必须存在

`.gitignore` 里那句"真实日报入库前必须先脱敏"原本只是一句注释。注释不会阻止
`git add`，而本仓库绑定公开 GitHub 远端 —— 真实日报一旦推送，井名与公司名
会永久留在 Git 历史里。

`ddr privacy` 把这句话变成可执行检查，但**检查本身也必须被检查**，否则会出现
两种同样致命的结果：

1. **漏报**：闸门对真实数据视而不见 → 数据照样泄露，而且因为"跑过了检查"
   反而更让人放心；
2. **误报**：闸门对合成数据大喊大叫 → 开发者很快学会忽略它，闸门形同废除。

因此这里两个方向都锁：合成集必须零发现，注入的私有数据必须被拦下。
"""

from __future__ import annotations

import json

import pytest

from ddr.privacy import (
    _REDACTION_RE,
    build_allowlist,
    render_text,
    scan_dataset,
)

# 真实样本集（已入库的 21 份合成日报）
SAMPLES = "data/samples"


def _write_dataset(root, items, truths):
    """在 tmp 目录里搭一个最小数据集。"""
    (root / "samples").mkdir(parents=True, exist_ok=True)
    (root / "ground_truth").mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(
        json.dumps({"count": len(items), "items": items}, ensure_ascii=False), encoding="utf-8"
    )
    for name, payload in truths.items():
        (root / "ground_truth" / f"{name}.truth.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )


def _item(name, **kw):
    base = {
        "file": f"samples/{name}.pdf",
        "truth": f"ground_truth/{name}.truth.json",
        "well_name": name,
        "operator": "某合成作业者",
        "contractor": "某合成承包商",
        "synthetic": True,
    }
    base.update(kw)
    return base


def _truth(name, **kw):
    inner = {"remarks": []}
    base = {"well_name": name, "operator": "某合成作业者", "contractor": "某合成承包商", "truth": inner}
    base.update(kw)
    return base


# --------------------------------------------------------------- 白名单
class TestAllowlist:
    def test_allowlist_built_from_synthetic_baseline(self):
        """白名单必须来自合成基线，而不是硬编码在代码里。"""
        al, notes = build_allowlist()
        assert al.baseline_size == 21, "基线应有 21 条合成样本"
        assert al.wells, "应收集到井名白名单"
        assert al.orgs, "应收集到机构名白名单"
        assert not [n for n in notes if "无法" in n], f"基线应可用：{notes}"

    def test_baseline_organisations_are_allowlisted(self):
        """基线里的机构名必须全部进白名单 —— 否则闸门会对自己的合成集误报。"""
        from ddr.privacy import _norm

        al, _ = build_allowlist()
        for org in (
            "中海石油（中国）有限公司天津分公司",
            "中海油田服务股份有限公司",
            "Equinor Energy AS",
            "Maersk Drilling",
        ):
            assert _norm(org) in al.orgs, f"{org} 应入白名单"


# --------------------------------------------------------------- 合成集零发现
class TestNoFalsePositives:
    def test_real_sample_set_is_clean(self):
        """仓库自带的合成集必须零发现，否则闸门会被当成噪音忽略。"""
        res = scan_dataset(SAMPLES)
        assert res.ok, f"合成集不应报问题：{[f.render() for f in res.findings]}"
        assert res.scanned_items == 21
        assert res.scanned_days == 21

    def test_synthetic_dataset_in_tmp_is_clean(self, tmp_path):
        """自造的纯合成数据集也应零发现。"""
        _write_dataset(
            tmp_path,
            [_item("SYN-1"), _item("SYN-2")],
            {"SYN-1": _truth("SYN-1"), "SYN-2": _truth("SYN-2")},
        )
        res = scan_dataset(tmp_path)
        # 注意：tmp 数据集不在合成基线里，所以井名会命中"不在白名单"规则。
        # 这里锁定的行为是：**必须报出来**（默认拒绝），而不是放过。
        assert any("井名" in f.message for f in res.errors), "白名单外的井名必须报出"


# --------------------------------------------------------------- 必须拦下
class TestDetectsPrivateData:
    def test_synthetic_false_is_blocked(self, tmp_path):
        """显式声明 real 的条目必须直接拦下。"""
        _write_dataset(
            tmp_path,
            [_item("SYN-1", synthetic=False)],
            {"SYN-1": _truth("SYN-1")},
        )
        res = scan_dataset(tmp_path)
        assert not res.ok
        assert any("synthetic=false" in f.message for f in res.errors)

    def test_missing_synthetic_flag_warns(self, tmp_path):
        """缺少 synthetic 标记应给出提示，而不是静默通过。"""
        it = _item("SYN-1")
        it.pop("synthetic")
        _write_dataset(tmp_path, [it], {"SYN-1": _truth("SYN-1")})
        res = scan_dataset(tmp_path)
        assert any("synthetic" in f.message for f in res.warnings)

    @pytest.mark.parametrize(
        "org",
        [
            "某石油有限公司",
            "某油田分公司",
            "某钻探工程有限公司",
            "某油服",
            "某勘探局",
            "Some Petroleum Limited",
            "Acme Drilling Inc",
        ],
    )
    def test_real_looking_organisations_blocked(self, tmp_path, org):
        """命中机构后缀且不在白名单 → 必须报 ERROR。"""
        _write_dataset(tmp_path, [_item("SYN-1", operator=org)], {"SYN-1": _truth("SYN-1")})
        res = scan_dataset(tmp_path)
        assert any(f.severity == "error" and "机构名" in f.message for f in res.errors), \
            f"{org} 应被拦下"

    @pytest.mark.parametrize(
        "well",
        ["大庆-12-3", "胜利-1", "长庆-45-6", "塔里木-2H"],
    )
    def test_well_names_outside_allowlist_blocked(self, tmp_path, well):
        """形如「区块-编号」且不在白名单 → 必须报 ERROR。

        注意：这里的井名**不能**用 PL-6-2-A12H / 15/9-F-14 / 苏48-12-66X ——
        那三口是合成基线里的虚构井，本来就在白名单里，应当放行。
        """
        _write_dataset(tmp_path, [_item(well)], {well: _truth(well)})
        res = scan_dataset(tmp_path)
        assert any(f.severity == "error" and "井名" in f.message for f in res.errors), \
            f"{well} 应被拦下"

    def test_baseline_wells_are_allowed(self, tmp_path):
        """合成基线里的三口虚构井必须放行，否则闸门会对自己人开火。"""
        for well in ("PL-6-2-A12H", "15/9-F-14", "苏 48-12-66X"):
            al, _ = build_allowlist()
            assert well.replace(" ", "").lower() in al.wells, f"{well} 应在白名单内"

    def test_ground_truth_org_is_also_checked(self, tmp_path):
        """manifest 干净但 ground truth 里有真实公司名，同样要拦下。

        真实泄露常常发生在 ground_truth 这类"附属文件"里：
        主表脱敏了，附录忘了。
        """
        _write_dataset(
            tmp_path,
            [_item("SYN-1")],
            {"SYN-1": _truth("SYN-1", operator="某真实石油有限公司")},
        )
        res = scan_dataset(tmp_path)
        assert any("ground_truth" in f.location and "机构名" in f.message for f in res.errors)

    def test_real_org_in_remark_text_warns(self, tmp_path):
        """备注里出现真实主体名应给出提示（自由文本泄露点）。"""
        _write_dataset(
            tmp_path,
            [_item("SYN-1")],
            {
                "SYN-1": _truth(
                    "SYN-1",
                    truth={"remarks": [{"seq": 1, "text": "接甲方某石油有限公司通知恢复作业。"}]},
                )
            },
        )
        res = scan_dataset(tmp_path)
        assert any("自由文本" in f.message for f in res.findings)


# --------------------------------------------------------------- 脱敏豁免
class TestRedactionExemption:
    @pytest.mark.parametrize(
        "name",
        ["XX-1", "XX-12", "XX井", "WELL-XX", "Well-XX-3", "井X-1", "井XX", "井A",
         "PL-XX井", "<作业者A>", "[已脱敏]", "已脱敏井"],
    )
    def test_redacted_forms_are_exempt(self, name):
        """已脱敏的写法必须豁免 —— 否则开发者会因误报而放弃闸门。"""
        assert _REDACTION_RE.search(name), f"{name} 应被识别为已脱敏"

    @pytest.mark.parametrize(
        "name",
        ["PL-6-2-A12H", "15/9-F-14", "苏48-12-66X", "大庆-12-3", "West-1", "A12H"],
    )
    def test_realistic_forms_are_not_exempt(self, name):
        """真实井名形态绝不能被误判成"已脱敏"而放行。"""
        assert not _REDACTION_RE.search(name), f"{name} 不应被豁免"

    def test_redacted_well_passes_gate(self, tmp_path):
        """已脱敏的井名可以让整条走通（否则闸门无法在真实场景落地）。"""
        _write_dataset(tmp_path, [_item("XX-1")], {"XX-1": _truth("XX-1")})
        res = scan_dataset(tmp_path)
        assert not [f for f in res.errors if "井名" in f.message]


# --------------------------------------------------------------- 健壮性
class TestRobustness:
    def test_missing_manifest_reports_error(self, tmp_path):
        res = scan_dataset(tmp_path)
        assert not res.ok
        assert any("manifest" in f.message for f in res.errors)

    def test_broken_manifest_reports_error(self, tmp_path):
        (tmp_path / "manifest.json").write_text("{ not json", encoding="utf-8")
        res = scan_dataset(tmp_path)
        assert not res.ok
        assert any("无法解析" in f.message for f in res.errors)

    def test_empty_items_reports_error(self, tmp_path):
        (tmp_path / "manifest.json").write_text(json.dumps({"items": []}), encoding="utf-8")
        res = scan_dataset(tmp_path)
        assert not res.ok

    def test_missing_truth_file_warns(self, tmp_path):
        it = _item("SYN-1")
        (tmp_path / "samples").mkdir(parents=True, exist_ok=True)
        (tmp_path / "manifest.json").write_text(
            json.dumps({"items": [it]}, ensure_ascii=False), encoding="utf-8"
        )
        res = scan_dataset(tmp_path)
        assert any("ground truth 文件缺失" in f.message for f in res.warnings)

    def test_render_text_never_crashes_on_empty(self):
        from ddr.privacy import ScanResult

        assert "数据闸门扫描" in render_text(ScanResult())

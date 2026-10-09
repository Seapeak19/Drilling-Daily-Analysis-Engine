---
name: ddr-parse
description: 解析钻井日报（DDR, Daily Drilling Report）PDF/Excel 为标准化 JSON，校验时间分解是否加总到 24 小时（IADC 硬约束），输出时效分解（有效生产/Flat Time/NPT）结论与图表。当用户提供钻井日报文件并要求"解析/提取/分析/核对/出图"，或询问某份日报的 NPT 率、时效分解、井深进尺、钻头记录、泥浆性能时使用。
---

# 钻井日报解析（DDR Parse Skill）

把钻井日报变成可核对的结构化结论。核心不是"读文档"，而是**按时效口径读懂一份日报**。

## 数据来源性质（必须先向用户确认）

本 Skill 的示例样本 `data/samples/samples/` 是**计算机合成数据**（按 IADC 标准构造，
井名公司名数值均虚构）。用户提供真实日报时，解析结论可用于工程参考，
但**准确率数字来自合成评测集，不能外推到真实日报**——如果有字段抽错，
按 `docs/格式适配指南.md` 登记该格式后重测。

## 快速开始

```bash
# 定位仓库根目录（含 pyproject.toml 与 src/ddr）
cd <仓库根>

# 一次性拿到结论 + 图 + 表
python -m ddr.cli parse <日报文件> --summary \
    --chart out/时效分解.html --csv out/时间分解.csv

# 只要 JSON
python -m ddr.cli parse <日报文件> --out out/report.json

# 多份日报一起看（会跨日报接续井深，反推当日进尺）
python -m ddr.cli batch <目录> --out out/parsed
```

> 环境未就绪时先装：`pip install -e ".[all]"`。
> 若 `python -m ddr.cli` 报模块找不到，说明尚未 `pip install -e .`，
> 此时可临时用 `PYTHONPATH=src python -m ddr.cli ...`。

## 回答用户时的口径（重要）

拿到解析结果后，**优先讲这四件事**，而不是罗列 JSON 字段：

1. **时间分解是否合规**——`time_verification.valid` 是否为 true，
   `sum_hours` 是否等于 24.00。不合规时把 `unaccounted_hours` 和
   `messages` 里的原文说明抄给用户，并明确提示"这是日报本身的问题，
   解析器已按缺口补记，请回查原件"。
2. **时效三类占比**——有效生产 / Flat Time / NPT 各多少小时、占比多少。
   NPT 率要与行业基准 20%–25% 对比说明（高或低都要讲）。
3. **NPT 事件明细**——时段、时长、归因大类（设备/井下/天气/物流/安全）。
   这是索赔与追责的证据链。
4. **异常与不确定性**——`source.warnings` 与校验 messages 里的所有提示都要如实转达，
   不要为了让结论显得干净而省略。

**不要做的事**：
- 不要根据日报之外的常识补全缺失字段（如"应该是 12¼ 井眼吧"）；
- 不要把 `null` 解释成 0（`null` 表示日报里没写）；
- 不要在单位不确定时给出换算后的数值——引擎会保留原始值并标记
  `unit_source: "unknown_raw"`，此时应提示用户人工确认单位；
- **不要把 `npt_responsibility: null` 说成"日报未写责任归属"**。
  该字段的抽取**尚未实现**，解析结果恒为 `null`（即使日报里写了）。
  需要责任归属时，请引导用户人工查阅日报原文；
- **不要把 `events[].hours: null` 说成"事件时长为 0"或"日报没写时长"**。
  `null` 的准确含义是"该备注未以明确时长写法叙述时长"
  （如"钻进至 2066.06 m，钻压 80 kN"这类只含参数、不含时长的句子）。
  只有"耗时 12 分钟""停钻 4.0 小时"这类写法才会给出 `hours`。

## 常用命令

| 目的 | 命令 |
|---|---|
| 解析并给结论 | `python -m ddr.cli parse 文件 --summary` |
| 出时效分解图 | `python -m ddr.cli parse 文件 --chart out/x.html` |
| 导出时间分解表 | `python -m ddr.cli parse 文件 --csv out/x.csv` |
| 批量解析目录 | `python -m ddr.cli batch 目录 --out out/parsed` |
| 看支持的模板与编码 | `python -m ddr.cli info` |
| 强制指定模板 | `python -m ddr.cli parse 文件 --template iadc_classic` |
| 严格 Schema 校验 | `python -m ddr.cli parse 文件 --strict` |
| 检查数据集是否含未脱敏真实信息 | `python -m ddr.cli privacy --data data/samples` |

> ⚠ **不要建议用户把真实日报放进 `data/samples/`**。本仓库绑定公开 GitHub 远端，
> 真实日报一旦提交就永久留在 Git 历史里。真实文件应放 `data/real/`
> （已在 `.gitignore` 中），入库前必须先脱敏并通过 `ddr privacy`。

## 已知模板

| 模板 ID | 版式 | 单位制 |
|---|---|---|
| `cn_vertical` | 中文纵向版（A4，表头单列） | 公制 |
| `iadc_classic` | IADC 英文版（A4 横向） | 混合（表头公制 + 泥浆 ppg/ft） |
| `regional_xls` | 区域公司 Excel 版 | 公制 |

识别为 `unknown` 时：说明这是一种新格式。此时仍会尽力抽取，
但应提醒用户按 `docs/格式适配指南.md` 登记模板，否则字段覆盖率无法保证。

## 操作码与时间类别

时间条目被归一为 24 个操作码（`DRILL`/`TRIP_OUT`/`CASING`/`REPAIR`/`STUCK`…），
每个操作码映射到一个**时间类别**：

- `productive` 有效生产时间——直接推进钻井目标（钻进、扩眼）
- `flat` Flat Time 计划内非钻进——起下钻、下套管、换钻头。**不计入 NPT**
- `npt` 非生产时间——非计划停工（设备故障、井下复杂、天气、等料）

这条区分是回答"这份日报效率如何"的关键：**起下钻慢不是 NPT，但它是 ILT
（隐形损失时间）**——计划书里最有价值的那部分分析留待阶段 2，
当前引擎已识别接单根条目（`ilt_action: "connection"`）作为 ILT 分析的基础。

## 故障排查

| 现象 | 原因与处理 |
|---|---|
| 报"全部 N 页均无文本层" | 扫描件。需先 OCR 成带文本层的 PDF（M1 不含 OCR 功能） |
| 报".xls 不受支持" | 旧版 Excel 二进制格式，请另存为 .xlsx |
| 时间条目数偏少 | 检查 `warnings` 是否有"退化为文本行解析"；若是，表格线可能缺失 |
| 某数值明显偏大/偏小 | 检查该字段的 `unit_source`；`unknown_raw` 表示单位未判定、未换算 |
| `valid: false` 且报缺口 | 日报原文未加总到 24 h，属日报质量问题，需人工回查 |
| 模板识别成 unknown | 新格式，按格式适配指南登记签名与标签表 |

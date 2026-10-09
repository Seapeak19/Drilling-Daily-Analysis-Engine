# 解析引擎准确率报告

评测样本：**21 份**；参与评分的字段断言：**549 项**；总体准确率：**100.00%**

> 样本为计算机合成数据（按 IADC 日报标准构造），不代表真实井数据。

## 〇、数据指纹（本报告的适用范围）

| 项 | 值 |
|---|---|
| 生成时间 | `2026-10-09T17:22:21+08:00` |
| Python | `3.12.14` |
| 提交 | `7be8373` |
| 工作区 | `有未提交改动（本次结果不对应任何提交）` |
| 评测样本 / 断言数 | 21 份 / 549 项 |
| 字段组数 | 7 组 |

> 复现本报告：`ddr eval --data data/samples`（同一提交 + 同一数据集 ⇒ 同一结果）。
> **注意适用范围**：以上准确率是在「样本由本仓库模板渲染器生成」的封闭评测集上取得的，说明抽取逻辑与数据模型自洽，**不等于**在任意真实日报上的准确率。

## 一、分字段组准确率

| 字段组 | 断言数 | 正确 | 准确率 | 说明 |
|---|---|---|---|---|
| 表头/井信息 | 216 | 216 | 100.00% | 井名、公司、井深、钻速等表头数值（含单位归一） |
| 时间分解 | 42 | 42 | 100.00% | 24 小时时间分解的加总与条目数 |
| NPT | 42 | 42 | 100.00% | 非生产时间的总时长与条目识别 |
| 泥浆 | 63 | 63 | 100.00% | 泥浆性能参数（英制模板下需 ppg→g/cm³ 换算） |
| 钻头记录 | 39 | 39 | 100.00% | 钻头进出井深度、进尺、IADC 磨损分级码 |
| ILT/接单根 | 42 | 42 | 100.00% | 隐形损失时间分析的基础：接单根条目识别 |
| 备注事件 | 105 | 105 | 100.00% |  |

## 二、逐字段准确率

| 字段 | 字段组 | 断言数 | 准确率 | 备注 |
|---|---|---|---|---|
| `well_name` | 表头/井信息 | 21 | 100.0% |  |
| `operator` | 表头/井信息 | 21 | 100.0% |  |
| `contractor` | 表头/井信息 | 21 | 100.0% |  |
| `rig` | 表头/井信息 | 21 | 100.0% |  |
| `report_no` | 表头/井信息 | 21 | 100.0% | 日报编号 |
| `report_date` | 表头/井信息 | 21 | 100.0% | 作业日期 |
| `md_m` | 表头/井信息 | 21 | 100.0% | 井深，英制模板下最易因单位换算出错 |
| `tvd_m` | 表头/井信息 | 21 | 100.0% |  |
| `progress_m` | 表头/井信息 | 21 | 100.0% |  |
| `rop_av_m_per_h` | 表头/井信息 | 12 | 100.0% | 平均钻速 |
| `etim_drill_h` | 表头/井信息 | 15 | 100.0% | 钻进时间 |
| `sum_hours` | 时间分解 | 21 | 100.0% | 时间分解加总（IADC 24 小时硬约束） |
| `time_entry_count` | 时间分解 | 21 | 100.0% | 条目数 |
| `npt_hours` | NPT | 21 | 100.0% | NPT 总时长 |
| `npt_entry_count` | NPT | 21 | 100.0% | NPT 条目数 |
| `mud_density_gcc` | 泥浆 | 21 | 100.0% | 出口密度；ppg↔g/cm³ 换算的关键校验点 |
| `mud_fl_ml` | 泥浆 | 21 | 100.0% | API 失水 |
| `mud_viscosity_s` | 泥浆 | 21 | 100.0% | 漏斗粘度 |
| `bit_count` | 钻头记录 | 21 | 100.0% | 钻头条数 |
| `connection_count` | ILT/接单根 | 21 | 100.0% | 接单根识别条数（ILT 的基础） |
| `connection_hours` | ILT/接单根 | 21 | 100.0% | 接单根总时长 |
| `event_count` | 备注事件 | 21 | 100.0% | 备注→事件条数（每条备注应产出一个事件） |
| `event_hours_total` | 备注事件 | 21 | 100.0% | 事件时长合计 |
| `event_hours_none_ratio` | 备注事件 | 21 | 100.0% | 无明确时长的备注条数 |
| `event_duration_exact` | 备注事件 | 21 | 100.0% | 时长与备注不一致的事件条数 |
| `event_evidence_verified` | 备注事件 | 21 | 100.0% | 带时长的 1 个事件中无法定位证据的条数 |
| `bit_depth_out_m` | 钻头记录 | 6 | 100.0% | 出井深度 |
| `bit_footage_m` | 钻头记录 | 6 | 100.0% | 进尺 |
| `bit_dull_grade` | 钻头记录 | 6 | 100.0% | IADC 磨损分级码 |

## 三、模板识别

模板识别正确：**21/21**

| 文件 | 期望模板 | 实际识别 |
|---|---|---|
| `PL-6-2-A12H_D01_2026-03-01.pdf` | cn_vertical | cn_vertical OK |
| `PL-6-2-A12H_D02_2026-03-02.pdf` | iadc_classic | iadc_classic OK |
| `PL-6-2-A12H_D03_2026-03-03.xlsx` | regional_xls | regional_xls OK |
| `PL-6-2-A12H_D04_2026-03-04.pdf` | cn_vertical | cn_vertical OK |
| `PL-6-2-A12H_D05_2026-03-05.pdf` | iadc_classic | iadc_classic OK |
| `PL-6-2-A12H_D06_2026-03-06.xlsx` | regional_xls | regional_xls OK |
| `PL-6-2-A12H_D07_2026-03-07.pdf` | iadc_classic | iadc_classic OK |
| `15-9-F-14_D01_2026-03-01.pdf` | cn_vertical | cn_vertical OK |
| `15-9-F-14_D02_2026-03-02.pdf` | iadc_classic | iadc_classic OK |
| `15-9-F-14_D03_2026-03-03.xlsx` | regional_xls | regional_xls OK |
| `15-9-F-14_D04_2026-03-04.pdf` | cn_vertical | cn_vertical OK |
| `15-9-F-14_D05_2026-03-05.pdf` | iadc_classic | iadc_classic OK |
| `15-9-F-14_D06_2026-03-06.xlsx` | regional_xls | regional_xls OK |
| `15-9-F-14_D07_2026-03-07.pdf` | iadc_classic | iadc_classic OK |
| `苏48-12-66X_D01_2026-03-01.pdf` | cn_vertical | cn_vertical OK |
| `苏48-12-66X_D02_2026-03-02.pdf` | iadc_classic | iadc_classic OK |
| `苏48-12-66X_D03_2026-03-03.xlsx` | regional_xls | regional_xls OK |
| `苏48-12-66X_D04_2026-03-04.pdf` | cn_vertical | cn_vertical OK |
| `苏48-12-66X_D05_2026-03-05.pdf` | iadc_classic | iadc_classic OK |
| `苏48-12-66X_D06_2026-03-06.xlsx` | regional_xls | regional_xls OK |
| `苏48-12-66X_D07_2026-03-07.pdf` | iadc_classic | iadc_classic OK |

## 四、未通过项明细

无。全部断言通过。

## 五、解析失败（异常）

无。

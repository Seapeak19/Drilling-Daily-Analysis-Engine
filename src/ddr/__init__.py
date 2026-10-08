"""钻井日报（DDR）解析与 KPI 看板 —— 解析引擎。

M0/M1 阶段交付：标准化数据模型、区块识别、规则抽取、LLM 抽取接口、校验层。
数据模型字段命名对齐 Energistics WITSML v2.0 DrillReport。
"""

__version__ = "0.1.0"
SCHEMA_VERSION = "1.0.0"

__all__ = ["__version__", "SCHEMA_VERSION"]

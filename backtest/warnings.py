"""回测警告码（``RQ_WARN_*``）**单一来源**。

为什么单独成文件
----------------
警告码同时被以下模块引用：``backtest/engine.py``（产生）、``backtest/persistence.py``
（落库/读回）、``backtest/__init__.py``（包级导出）、``ui/``（红色提示条）。
定义散落各处曾导致 ``backtest/__init__.py`` 从 engine 导入已迁走的码而 ``ImportError``，
进而拖垮 ``import trading`` / ``app_services``（M2 手动交易链路 P0 回归）。
因此统一放这里，任何模块只从本文件导入，**禁止再在别处重复定义**。

新增警告码流程：本文件加常量 + 中文说明 → PM 登记进 ``docs/SPEC.md`` §9.2 →
``ui`` 侧按 ``WARNING_MESSAGES`` 渲染。
"""

from __future__ import annotations

__all__ = [
    "RQ_WARN_NO_ADJ",
    "RQ_WARN_ST_APPROX",
    "RQ_WARN_SHORT_SAMPLE",
    "RQ_WARN_FEW_TRADES",
    "RQ_WARN_HIGH_SUSPEND",
    "RQ_WARN_NO_BENCHMARK",
    "RQ_WARN_MOCK_DATA",
    "RQ_WARN_PRICE_JUMP",
    "RQ_WARN_POSITION_MIN_LOT",
    "RQ_WARN_SYNTHETIC_PRICE",
    "WARNING_MESSAGES",
]

# --- SPEC §9.2 冻结的 5 个基础警告码 ---------------------------------------
RQ_WARN_NO_ADJ = "RQ_WARN_NO_ADJ"
RQ_WARN_ST_APPROX = "RQ_WARN_ST_APPROX"
RQ_WARN_SHORT_SAMPLE = "RQ_WARN_SHORT_SAMPLE"
RQ_WARN_FEW_TRADES = "RQ_WARN_FEW_TRADES"
RQ_WARN_HIGH_SUSPEND = "RQ_WARN_HIGH_SUSPEND"

# --- Dev2 交付补充（已报 Lead 批准，PM 登记 SPEC） -------------------------
#: 基准数据缺失（M1-14；不报错，仅在 warnings 提示“未绘制基准对比”）
RQ_WARN_NO_BENCHMARK = "RQ_WARN_NO_BENCHMARK"
#: 行情全部来自离线 Mock（结果仅供演示，UI 必须显示红色提示条）
RQ_WARN_MOCK_DATA = "RQ_WARN_MOCK_DATA"
#: 价格序列异常跳变（无 source 列时兜底检测：疑似混源/未复权）
RQ_WARN_PRICE_JUMP = "RQ_WARN_PRICE_JUMP"
#: 目标仓位不足 1 手，已按最小 1 手（整手）调整
RQ_WARN_POSITION_MIN_LOT = "RQ_WARN_POSITION_MIN_LOT"
#: 行情为复权合成价格（取不到不复权数据）：手数/资金不代表真实交易，且关闭最小 1 手规则
RQ_WARN_SYNTHETIC_PRICE = "RQ_WARN_SYNTHETIC_PRICE"

#: 警告码 -> 中文说明（UI 回测页“可信度”区直接展示，顺序无关）
WARNING_MESSAGES: dict[str, str] = {
    RQ_WARN_NO_ADJ: "未使用复权因子（缺少 adj_factor），除权除息可能造成虚假盈亏，结果仅供参考",
    RQ_WARN_ST_APPROX: "历史 ST 状态不可得，按非 ST 近似（涨跌停幅度可能偏差）",
    RQ_WARN_SHORT_SAMPLE: "样本期不足 250 个交易日，统计显著性不足",
    RQ_WARN_FEW_TRADES: "交易次数少于 30 笔，指标易受个别交易影响",
    RQ_WARN_HIGH_SUSPEND: "停牌占比偏高（>5%），权益曲线与成交价可能存在偏差",
    RQ_WARN_NO_BENCHMARK: "基准数据缺失，未绘制基准对比曲线（不影响组合自身指标）",
    RQ_WARN_MOCK_DATA: "行情全部来自离线 Mock 示例数据，仅供演示，不代表真实市场表现",
    RQ_WARN_PRICE_JUMP: "行情存在异常价格跳变（疑似数据源混用/未复权），回测结果可能失真",
    RQ_WARN_POSITION_MIN_LOT: "目标仓位不足 1 手，已按最小 1 手买入（单票占比可能高于目标）",
    RQ_WARN_SYNTHETIC_PRICE: (
        "行情为复权合成价格（未取到不复权数据），手数与资金不代表真实交易，已关闭最小 1 手规则"
    ),
}

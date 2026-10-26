"""内置策略包（导入即注册）。

新增内置策略的步骤（BRIEF F2：新增策略 = 新增一个文件）：

1. 在本目录新建 ``xxx.py``，声明 ``Strategy`` 子类并加 ``@register("xxx")``；
2. 在本文件补一行 ``from strategy.builtin import xxx  # noqa: F401``。

``strategy.base.load_builtin_strategies()`` 也会在 ``import strategy`` 时自动扫描本目录
（见 ``strategy/base.py``），两处都生效，不会重复注册（同一类重复注册视为幂等）。

当前内置策略
------------
* ``ma_cross``          双均线（趋势）
* ``rsi_reversal``      RSI 反转（均值回归）
* ``multi_factor``      多因子打分（动量 z − 波动 z）
* ``value_quality``     长期线：质量+动量+低波动+回撤可控（月频，持有 10~20 只）
* ``breakout_abnormal`` 短期线：量比放大+突破 20 日高点+温和涨幅（日频，不追涨停）
* ``ml_ranker``         ML 打分：walk-forward 模型概率选股（逻辑回归/梯度提升）
"""

from __future__ import annotations

from strategy.builtin import (  # noqa: F401
    breakout_abnormal,
    ma_cross,
    ml_ranker,
    multi_factor,
    rsi_reversal,
    value_quality,
)

__all__ = [
    "ma_cross",
    "rsi_reversal",
    "multi_factor",
    "value_quality",
    "breakout_abnormal",
    "ml_ranker",
]

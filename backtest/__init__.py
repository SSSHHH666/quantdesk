"""QuantDesk 回测包（Dev2）。

包含：

* :mod:`backtest.broker_sim` —— A 股撮合规则与费用模型（**唯一实现**，CF-10），
  回测 :class:`~backtest.broker_sim.SimBroker` 与 ``trading/broker.py`` 的
  ``PaperBroker`` 共用，保证“回测 = 模拟盘”口径一致；
* :mod:`backtest.metrics` —— 绩效指标（CF-5 的 ``compute_metrics``，口径见 SPEC §8.3）；
* :mod:`backtest.engine` —— 事件驱动逐日回测引擎（M1-11，输出 ``BacktestResult``）；
* :mod:`backtest.persistence` —— 回测结果落库/读回（M1-13，
  ``backtest_runs`` / ``backtest_equity`` / ``backtest_trades``）。

用法::

    from datetime import date
    from backtest import BacktestEngine, AStockRules, FeeModel, compute_metrics

    limit_up, limit_down = AStockRules.price_limits(10.0, "MAIN", is_st=False)
    fee = FeeModel().calculate("BUY", 10.0, 1000)
    result = BacktestEngine().run("ma_cross", None, ["600519.SH"],
                                  date(2024, 1, 1), date(2025, 12, 31), bars=bars)
    metrics = result.metrics
"""

from __future__ import annotations

from backtest.broker_sim import (  # noqa: F401  统一从包根导出
    BOARD_BSE,
    BOARD_GEM,
    BOARD_LABELS,
    BOARD_MAIN,
    BOARD_STAR,
    DEFAULT_MAX_DEFER_DAYS,
    DEFAULT_SLIPPAGE_BPS,
    MAIN_ST_PRICE_LIMIT_PCT,
    PRICE_LIMIT_PCT,
    PRICE_TICK,
    REASON_MESSAGES,
    SIDE_BUY,
    SIDE_SELL,
    AStockRules,
    FeeBreakdown,
    FeeConfig,
    FeeModel,
    Fill,
    MatchReport,
    SimBroker,
    Unfilled,
    apply_slippage,
    fill_cash_delta,
    guess_board,
    is_buy_fill,
    normalize_board,
    normalize_side,
    reason_message,
    resolve_fee_config,
    round_money,
    round_price,
)
from backtest.engine import (  # noqa: F401
    BacktestEngine,
    BacktestError,
    BacktestResult,
    PendingOrder,
    run_backtest,
)
from backtest.warnings import (  # noqa: F401  警告码单一来源：backtest/warnings.py
    RQ_WARN_FEW_TRADES,
    RQ_WARN_HIGH_SUSPEND,
    RQ_WARN_MOCK_DATA,
    RQ_WARN_NO_ADJ,
    RQ_WARN_NO_BENCHMARK,
    RQ_WARN_POSITION_MIN_LOT,
    RQ_WARN_PRICE_JUMP,
    RQ_WARN_SHORT_SAMPLE,
    RQ_WARN_ST_APPROX,
    RQ_WARN_SYNTHETIC_PRICE,
    WARNING_MESSAGES,
)
from backtest.metrics import (  # noqa: F401
    METRIC_LABELS,
    TRADING_DAYS_PER_YEAR,
    annualized_return,
    closed_trades,
    compute_metrics,
    daily_returns,
    drawdown_series,
    format_metric,
    format_metrics,
    max_drawdown,
    monthly_returns,
    open_positions,
    sharpe_ratio,
    sortino_ratio,
    volatility,
)
from backtest.persistence import (  # noqa: F401
    delete_backtest_result,
    list_backtest_runs,
    load_backtest_result,
    save_backtest_result,
)

__version__ = "0.1.0"

__all__ = [
    # 撮合规则与费用（CF-10）
    "SIDE_BUY",
    "SIDE_SELL",
    "BOARD_MAIN",
    "BOARD_GEM",
    "BOARD_STAR",
    "BOARD_BSE",
    "BOARD_LABELS",
    "PRICE_LIMIT_PCT",
    "MAIN_ST_PRICE_LIMIT_PCT",
    "PRICE_TICK",
    "DEFAULT_SLIPPAGE_BPS",
    "DEFAULT_MAX_DEFER_DAYS",
    "REASON_MESSAGES",
    "AStockRules",
    "FeeModel",
    "SimBroker",
    "FeeConfig",
    "FeeBreakdown",
    "Fill",
    "Unfilled",
    "MatchReport",
    "reason_message",
    "normalize_side",
    "normalize_board",
    "guess_board",
    "round_price",
    "round_money",
    "apply_slippage",
    "resolve_fee_config",
    "is_buy_fill",
    "fill_cash_delta",
    # 回测引擎（CF-5 / M1-11）
    "BacktestEngine",
    "BacktestResult",
    "BacktestError",
    "PendingOrder",
    "run_backtest",
    "RQ_WARN_NO_ADJ",
    "RQ_WARN_ST_APPROX",
    "RQ_WARN_MOCK_DATA",
    "RQ_WARN_NO_BENCHMARK",
    "RQ_WARN_POSITION_MIN_LOT",
    "RQ_WARN_PRICE_JUMP",
    "RQ_WARN_SHORT_SAMPLE",
    "RQ_WARN_FEW_TRADES",
    "RQ_WARN_HIGH_SUSPEND",
    "RQ_WARN_SYNTHETIC_PRICE",
    "WARNING_MESSAGES",
    # 绩效指标（CF-5）
    "compute_metrics",
    "daily_returns",
    "drawdown_series",
    "max_drawdown",
    "annualized_return",
    "sharpe_ratio",
    "sortino_ratio",
    "volatility",
    "monthly_returns",
    "closed_trades",
    "open_positions",
    "format_metric",
    "format_metrics",
    "METRIC_LABELS",
    "TRADING_DAYS_PER_YEAR",
    # 结果持久化（M1-13）
    "save_backtest_result",
    "load_backtest_result",
    "delete_backtest_result",
    "list_backtest_runs",
]


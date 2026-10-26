"""QuantDesk 策略包（Dev2）。

对外只暴露 :mod:`strategy.base` 的内容，并在包导入时自动发现
``strategy/builtin/*.py`` 里的策略文件（“新增策略 = 新增一个文件”）。

用法::

    from strategy import get_strategy, list_strategies, create_strategy

    for item in list_strategies():
        print(item["key"], item["display_name"])

    strategy = create_strategy("ma_cross", {"fast": 5, "slow": 20})
"""

from __future__ import annotations

from strategy.base import (  # noqa: F401  统一从包根导出
    INDICATOR_BACKEND,
    INDICATOR_NAMES,
    SIDE_BUY,
    SIDE_SELL,
    TALIB_AVAILABLE,
    TALIB_VERSION,
    Context,
    IndicatorBackendError,
    Signal,
    Strategy,
    StrategyError,
    StrategyNotFoundError,
    StrategyParamError,
    StrategyRegistrationError,
    StrategySchemaError,
    atr,
    create_strategy,
    ema,
    get_indicator,
    get_strategy,
    indicator_info,
    is_registered,
    iter_strategies,
    list_strategies,
    load_builtin_strategies,
    macd,
    normalize_side,
    open_strategy_registry,
    register,
    rsi,
    sma,
    std,
    temporary_strategy,
    unregister,
    validate_params_schema,
)

__version__ = "0.1.0"

__all__ = [
    "SIDE_BUY",
    "SIDE_SELL",
    "Signal",
    "Context",
    "Strategy",
    "register",
    "unregister",
    "is_registered",
    "get_strategy",
    "create_strategy",
    "list_strategies",
    "iter_strategies",
    "temporary_strategy",
    "open_strategy_registry",
    "load_builtin_strategies",
    "validate_params_schema",
    "StrategyError",
    "StrategyRegistrationError",
    "StrategyNotFoundError",
    "StrategyParamError",
    "StrategySchemaError",
    "IndicatorBackendError",
    "sma",
    "ema",
    "rsi",
    "macd",
    "std",
    "atr",
    "get_indicator",
    "indicator_info",
    "INDICATOR_NAMES",
    "INDICATOR_BACKEND",
    "TALIB_AVAILABLE",
    "TALIB_VERSION",
    "normalize_side",
]

#: 已自动加载的内置策略模块名（M1-07/08/09 交付后自动出现）
LOADED_BUILTIN_STRATEGIES: list[str] = load_builtin_strategies(strict=False)

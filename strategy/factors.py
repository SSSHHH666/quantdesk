"""纯函数因子库（策略 / 推荐 / ML 打分共用，无 IO、无未来数据）。

设计原则
--------
1. **只看历史**：所有因子只用传入 ``history``（截至 ``as_of`` 的行情）计算，
   绝不使用 ``as_of`` 之后的数据；未来数据只允许出现在 ML 训练标签里（见
   :mod:`strategy.ml_ranker`）。
2. **无副作用**：不读写数据库、不联网、不使用随机数；相同输入必得相同输出，
   便于回测复现（AC-F2-03）。
3. **单一来源**：三条策略线与推荐引擎/ML 打分共用本模块，避免同一因子多处实现
   导致口径漂移。指标计算复用 :mod:`strategy.base`（TA-Lib 优先、pandas 兜底）。

因子口径
--------
======================  ==================================================
键                       含义
======================  ==================================================
``momentum_20/60/120``   区间收益率 = 收盘/前 N 日收盘 − 1
``cum_return_20``        20 日累计收益率（用于“未暴涨”过滤）
``max_day_gain_20``      20 日内最大单日涨幅（用于“不追暴涨”过滤）
``volatility_20/60``     日收益率标准差（ddof=0）
``max_drawdown_60/120``  区间最大回撤（负数）
``trend_stability_60``   日收益均值/标准差 × √60（趋势稳定性，越大越稳）
``positive_ratio_60``    60 日内上涨交易日占比（质量/趋势持续性的价格代理）
``rsi_14``               RSI（Wilder 平滑，TA-Lib 口径）
``dist_ma20/60``         收盘价相对 MA20/MA60 的偏离率
``atr_pct_14``           ATR(14)/收盘价（波动率代理）
``volume_ratio_5_20``    5 日均量 / 20 日均量（量比放大）
``volume_ratio_1_20``    当日量 / 20 日均量
``breakout_20``          当日收盘相对“前 20 日最高价”的突破幅度
``last_close``           最新收盘价（展示用）
======================  ==================================================
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from strategy.base import atr, rsi, sma

__all__ = [
    "FactorConfig",
    "DEFAULT_FACTOR_CONFIG",
    "FEATURE_LABELS",
    "ML_FEATURE_NAMES",
    "normalize_bars",
    "compute_features",
    "compute_breakout_features",
    "compute_ml_features",
    "zscore_map",
    "score_value_quality",
    "value_quality_components",
    "breakout_signal",
    "breakout_reason",
    "multi_factor_scores",
    "describe_score",
]

#: 因子中文名（推荐理由 / ML 特征重要性展示）
FEATURE_LABELS: dict[str, str] = {
    "momentum_20": "20日动量",
    "momentum_60": "60日动量",
    "momentum_120": "120日动量",
    "cum_return_20": "20日累计收益",
    "max_day_gain_20": "20日最大单日涨幅",
    "volatility_20": "20日波动率",
    "volatility_60": "60日波动率",
    "max_drawdown_60": "60日最大回撤",
    "max_drawdown_120": "120日最大回撤",
    "trend_stability_60": "60日趋势稳定性",
    "positive_ratio_60": "60日上涨占比",
    "rsi_14": "RSI(14)",
    "dist_ma20": "偏离MA20",
    "dist_ma60": "偏离MA60",
    "atr_pct_14": "ATR占价比",
    "volume_ratio_5_20": "量比(5日/20日)",
    "volume_ratio_1_20": "当日量比(1日/20日)",
    "breakout_20": "突破20日高点",
    "last_close": "最新收盘价",
    # ML 特征（短周期收益率）
    "ret_1": "1日收益",
    "ret_5": "5日收益",
    "ret_10": "10日收益",
    "ret_20": "20日收益",
    "ret_60": "60日收益",
    "drawdown_60": "60日回撤",
}

#: ML 分类器使用的特征列（顺序固定，训练/预测一致）
ML_FEATURE_NAMES: tuple[str, ...] = (
    "ret_1",
    "ret_5",
    "ret_10",
    "ret_20",
    "ret_60",
    "volatility_20",
    "volatility_60",
    "volume_ratio_5_20",
    "volume_ratio_1_20",
    "rsi_14",
    "dist_ma20",
    "dist_ma60",
    "atr_pct_14",
    "drawdown_60",
    "breakout_20",
    "max_day_gain_20",
    "trend_stability_60",
    "positive_ratio_60",
)


# ---------------------------------------------------------------------------
# 行情列规范化（供 ML/推荐直接接收第三方 DataFrame）
# ---------------------------------------------------------------------------

_COLUMN_ALIASES: dict[str, str] = {
    "date": "date",
    "trade_date": "date",
    "tradedate": "date",
    "datetime": "date",
    "ts": "date",
    "symbol": "symbol",
    "code": "symbol",
    "ts_code": "symbol",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
    "vol": "volume",
    "amount": "amount",
}


def normalize_bars(frame: Any) -> pd.DataFrame:
    """把行情 DataFrame 规范成 ``date,symbol,open,high,low,close,volume`` 列（升序）。

    Args:
        frame: 原始行情（列名大小写/中英文别名均可）。

    Returns:
        规范化并去重、按 ``(symbol, date)`` 升序的 DataFrame。

    Raises:
        ValueError: 缺少必需列或为空（中文提示）。
    """

    if not isinstance(frame, pd.DataFrame):
        raise ValueError(f"行情必须是 pandas.DataFrame，收到 {type(frame).__name__}")
    if frame.empty:
        raise ValueError("行情数据为空，无法计算因子")
    renamed: dict[Any, str] = {}
    used: set[str] = set()
    for column in frame.columns:
        alias = _COLUMN_ALIASES.get(str(column).strip().lower())
        if alias and alias not in used:
            renamed[column] = alias
            used.add(alias)
    result = frame.rename(columns=renamed).copy()
    missing = [name for name in ("date", "open", "high", "low", "close") if name not in result.columns]
    if missing:
        raise ValueError(f"行情缺少必需列 {missing}（需要 date/open/high/low/close）")
    if "symbol" not in result.columns:
        result["symbol"] = ""
    if "volume" not in result.columns:
        result["volume"] = 0.0
    result["date"] = pd.to_datetime(result["date"], errors="coerce").dt.date
    result = result.dropna(subset=["date"])
    for column in ("open", "high", "low", "close", "volume"):
        result[column] = pd.to_numeric(result[column], errors="coerce")
    result = result.sort_values(["symbol", "date"], kind="mergesort")
    result = result.drop_duplicates(subset=["symbol", "date"], keep="last").reset_index(drop=True)
    return result


# ---------------------------------------------------------------------------
# 因子配置与计算
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FactorConfig:
    """因子窗口配置（默认值面向 A 股日频；与策略参数解耦，便于 ML/推荐复用）。"""

    momentum_windows: tuple[int, ...] = (20, 60, 120)
    volatility_windows: tuple[int, ...] = (20, 60)
    drawdown_windows: tuple[int, ...] = (60, 120)
    breakout_window: int = 20
    volume_short: int = 5
    volume_long: int = 20
    rsi_period: int = 14
    atr_period: int = 14
    trend_window: int = 60
    positive_window: int = 60
    min_history: int = 60

    @property
    def required_rows(self) -> int:
        """计算全部因子所需的最少历史 bar 数。"""

        windows = (
            max(self.momentum_windows),
            max(self.volatility_windows),
            max(self.drawdown_windows),
            self.breakout_window,
            self.volume_long,
            self.rsi_period,
            self.atr_period,
            self.trend_window,
            self.positive_window,
        )
        return max(int(value) for value in windows) + 2


DEFAULT_FACTOR_CONFIG = FactorConfig()


def _column(frame: pd.DataFrame, name: str) -> pd.Series | None:
    if name in frame.columns:
        return pd.to_numeric(frame[name], errors="coerce")
    return None


def _safe(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _tail(series: pd.Series, size: int) -> np.ndarray:
    return series.iloc[-size:].to_numpy(dtype="float64")


def _return_over(close: np.ndarray, window: int) -> float | None:
    if close.size <= window:
        return None
    base = close[-1 - window]
    if not math.isfinite(base) or base <= 0 or not math.isfinite(close[-1]):
        return None
    return float(close[-1] / base - 1.0)


def _daily_returns(close: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        returns = close[1:] / close[:-1] - 1.0
    return returns[np.isfinite(returns)]


def _volatility(close: np.ndarray, window: int) -> float | None:
    if close.size <= window:
        return None
    returns = _daily_returns(close[-window - 1 :])
    if returns.size < window:
        return None
    value = float(np.std(returns, ddof=0))
    return value if math.isfinite(value) else None


def _max_drawdown(close: np.ndarray, window: int) -> float | None:
    if close.size < 2:
        return None
    values = close[-window:]
    if values.size < 2 or np.any(~np.isfinite(values)):
        return None
    peaks = np.maximum.accumulate(values)
    with np.errstate(divide="ignore", invalid="ignore"):
        drawdown = values / peaks - 1.0
    drawdown = drawdown[np.isfinite(drawdown)]
    if drawdown.size == 0:
        return None
    return float(np.min(drawdown))


def _trend_stability(close: np.ndarray, window: int) -> float | None:
    if close.size <= window:
        return None
    returns = _daily_returns(close[-window - 1 :])
    if returns.size < window:
        return None
    std = float(np.std(returns, ddof=0))
    if std <= 0 or not math.isfinite(std):
        return 0.0
    return float(float(np.mean(returns)) / std * math.sqrt(window))


def _positive_ratio(close: np.ndarray, window: int) -> float | None:
    if close.size <= window:
        return None
    returns = _daily_returns(close[-window - 1 :])
    if returns.size < window:
        return None
    return float(np.mean(returns > 0))


def _volume_ratio(volume: np.ndarray | None, short: int, long: int) -> float | None:
    if volume is None or volume.size < long:
        return None
    long_mean = float(np.nanmean(volume[-long:]))
    if long_mean <= 0 or not math.isfinite(long_mean):
        return None
    short_mean = float(np.nanmean(volume[-short:]))
    if not math.isfinite(short_mean):
        return None
    return float(short_mean / long_mean)


def _breakout(high: np.ndarray, close: np.ndarray, window: int) -> float | None:
    if high.size < window + 1 or close.size < window + 1:
        return None
    prior_high = float(np.nanmax(high[-window - 1 : -1]))
    last_close = float(close[-1])
    if not math.isfinite(prior_high) or prior_high <= 0 or not math.isfinite(last_close):
        return None
    return float(last_close / prior_high - 1.0)


def _max_day_gain(close: np.ndarray, window: int) -> float | None:
    if close.size <= window:
        return None
    returns = _daily_returns(close[-window - 1 :])
    if returns.size == 0:
        return None
    return float(np.max(returns))


def _cum_return(close: np.ndarray, window: int) -> float | None:
    return _return_over(close, window)


def _dist_ma(close: np.ndarray, window: int) -> float | None:
    if close.size < window:
        return None
    mean = float(np.mean(close[-window:]))
    if mean <= 0 or not math.isfinite(mean):
        return None
    return float(close[-1] / mean - 1.0)


def _rsi_last(close_series: pd.Series, period: int) -> float | None:
    if len(close_series) <= period:
        return None
    values = rsi(close_series, period)
    if len(values) == 0:
        return None
    return _safe(values.iloc[-1])


def _atr_pct(history: pd.DataFrame, period: int) -> float | None:
    high = _column(history, "high")
    low = _column(history, "low")
    close = _column(history, "close")
    if high is None or low is None or close is None or len(history) <= period:
        return None
    values = atr(high, low, close, period)
    if len(values) == 0:
        return None
    last_atr = _safe(values.iloc[-1])
    last_close = _safe(close.iloc[-1])
    if last_atr is None or last_close is None or last_close <= 0:
        return None
    return float(last_atr / last_close)


def compute_features(
    history: pd.DataFrame | None,
    config: FactorConfig | None = None,
) -> dict[str, float] | None:
    """计算一只标的截至最后一根 bar 的全量因子（历史不足返回 None）。

    Args:
        history: 该标的的历史行情（升序，至少含 close/high/low；volume 可选）。
        config: 因子窗口配置。

    Returns:
        ``{因子名: 数值}``；历史长度不足 ``config.required_rows`` 时返回 None。
    """

    if history is None or len(history) == 0:
        return None
    config = config or DEFAULT_FACTOR_CONFIG
    if len(history) < config.required_rows:
        return None
    close_series = _column(history, "close")
    high_series = _column(history, "high")
    low_series = _column(history, "low")
    if close_series is None or high_series is None or low_series is None:
        return None
    close = close_series.to_numpy(dtype="float64")
    high = high_series.to_numpy(dtype="float64")
    volume_series = _column(history, "volume")
    volume = volume_series.to_numpy(dtype="float64") if volume_series is not None else None

    features: dict[str, float] = {}
    for window in config.momentum_windows:
        value = _return_over(close, window)
        if value is not None:
            features[f"momentum_{window}"] = value
    for window in config.volatility_windows:
        value = _volatility(close, window)
        if value is not None:
            features[f"volatility_{window}"] = value
    for window in config.drawdown_windows:
        value = _max_drawdown(close, window)
        if value is not None:
            features[f"max_drawdown_{window}"] = value
    for name, value in (
        ("trend_stability_60", _trend_stability(close, config.trend_window)),
        ("positive_ratio_60", _positive_ratio(close, config.positive_window)),
        ("rsi_14", _rsi_last(close_series, config.rsi_period)),
        ("dist_ma20", _dist_ma(close, 20)),
        ("dist_ma60", _dist_ma(close, 60)),
        ("atr_pct_14", _atr_pct(history, config.atr_period)),
        ("volume_ratio_5_20", _volume_ratio(volume, config.volume_short, config.volume_long)),
        ("volume_ratio_1_20", _volume_ratio(volume, 1, config.volume_long)),
        ("breakout_20", _breakout(high, close, config.breakout_window)),
        ("cum_return_20", _cum_return(close, 20)),
        ("max_day_gain_20", _max_day_gain(close, config.breakout_window)),
        ("last_close", float(close[-1])),
    ):
        if value is not None and math.isfinite(float(value)):
            features[name] = float(value)
    return features or None


def compute_breakout_features(
    history: pd.DataFrame | None,
    *,
    breakout_window: int = 20,
    volume_short: int = 5,
    volume_long: int = 20,
) -> dict[str, float] | None:
    """短周期异动因子（突破/量比/涨幅，只依赖最近 `max(窗口)+2` 根 bar）。"""

    if history is None:
        return None
    need = max(breakout_window, volume_long) + 2
    if len(history) < need:
        return None
    close_series = _column(history, "close")
    high_series = _column(history, "high")
    if close_series is None or high_series is None:
        return None
    close = close_series.to_numpy(dtype="float64")
    high = high_series.to_numpy(dtype="float64")
    volume_series = _column(history, "volume")
    volume = volume_series.to_numpy(dtype="float64") if volume_series is not None else None
    features: dict[str, float] = {}
    for name, value in (
        ("breakout_20", _breakout(high, close, breakout_window)),
        ("volume_ratio_5_20", _volume_ratio(volume, volume_short, volume_long)),
        ("volume_ratio_1_20", _volume_ratio(volume, 1, volume_long)),
        ("cum_return_20", _cum_return(close, breakout_window)),
        ("max_day_gain_20", _max_day_gain(close, breakout_window)),
        ("dist_ma20", _dist_ma(close, 20)),
        ("last_close", float(close[-1])),
    ):
        if value is not None and math.isfinite(float(value)):
            features[name] = float(value)
    if close.size >= 2 and math.isfinite(close[-2]) and close[-2] > 0:
        features["gain_today"] = float(close[-1] / close[-2] - 1.0)
    return features or None


def compute_ml_features(history: pd.DataFrame | None) -> dict[str, float] | None:
    """ML 模型特征（与 :data:`ML_FEATURE_NAMES` 一一对应；历史不足返回 None）。"""

    if history is None or len(history) < 62:
        return None
    close_series = _column(history, "close")
    high_series = _column(history, "high")
    if close_series is None or high_series is None:
        return None
    close = close_series.to_numpy(dtype="float64")
    high = high_series.to_numpy(dtype="float64")
    volume_series = _column(history, "volume")
    volume = volume_series.to_numpy(dtype="float64") if volume_series is not None else None
    features: dict[str, float] = {}
    for label, window in (("ret_1", 1), ("ret_5", 5), ("ret_10", 10), ("ret_20", 20), ("ret_60", 60)):
        value = _return_over(close, window)
        if value is None:
            return None
        features[label] = value
    for name, value in (
        ("volatility_20", _volatility(close, 20)),
        ("volatility_60", _volatility(close, 60)),
        ("volume_ratio_5_20", _volume_ratio(volume, 5, 20)),
        ("volume_ratio_1_20", _volume_ratio(volume, 1, 20)),
        ("rsi_14", _rsi_last(close_series, 14)),
        ("dist_ma20", _dist_ma(close, 20)),
        ("dist_ma60", _dist_ma(close, 60)),
        ("atr_pct_14", _atr_pct(history, 14)),
        ("drawdown_60", _max_drawdown(close, 60)),
        ("breakout_20", _breakout(high, close, 20)),
        ("max_day_gain_20", _max_day_gain(close, 20)),
        ("trend_stability_60", _trend_stability(close, 60)),
        ("positive_ratio_60", _positive_ratio(close, 60)),
    ):
        if value is None or not math.isfinite(float(value)):
            return None
        features[name] = float(value)
    return features


# ---------------------------------------------------------------------------
# 截面打分工具
# ---------------------------------------------------------------------------


def zscore_map(values: Mapping[str, float]) -> dict[str, float]:
    """截面 z-score；样本 <2、标准差为 0 或缺失时全部返回 0（除零安全）。"""

    if not values:
        return {}
    numbers = [float(value) for value in values.values() if math.isfinite(float(value))]
    if len(numbers) < 2:
        return {symbol: 0.0 for symbol in values}
    mean = sum(numbers) / len(numbers)
    variance = sum((value - mean) ** 2 for value in numbers) / (len(numbers) - 1)
    std = math.sqrt(variance) if variance > 0 else 0.0
    if std <= 0 or not math.isfinite(std):
        return {symbol: 0.0 for symbol in values}
    result: dict[str, float] = {}
    for symbol, value in values.items():
        number = float(value)
        result[symbol] = (number - mean) / std if math.isfinite(number) else 0.0
    return result


#: 价值/质量组合的因子权重（质量 + 动量 + 低波动 + 回撤可控）
VALUE_QUALITY_WEIGHTS: dict[str, float] = {
    "momentum_120": 0.30,
    "trend_stability_60": 0.20,
    "low_volatility_60": 0.20,
    "low_drawdown_120": 0.15,
    "positive_ratio_60": 0.15,
}


def value_quality_components(
    features_by_symbol: Mapping[str, Mapping[str, float]],
) -> dict[str, dict[str, float]]:
    """价值/质量组合的截面 z-score 分解（供策略打分与推荐理由共用）。

    方向约定：所有分量都是“越大越好”：
    ``momentum_120``（动量）、``trend_stability_60``（趋势稳定）、
    ``low_volatility_60``（低波动 = −z(vol)）、``low_drawdown_120``（回撤小 = −z(|mdd|)）、
    ``positive_ratio_60``（上涨占比 = 质量代理）。
    """

    symbols = list(features_by_symbol)
    momentum = zscore_map(
        {s: features_by_symbol[s].get("momentum_120", 0.0) for s in symbols}
    )
    stability = zscore_map(
        {s: features_by_symbol[s].get("trend_stability_60", 0.0) for s in symbols}
    )
    volatility = zscore_map(
        {s: features_by_symbol[s].get("volatility_60", 0.0) for s in symbols}
    )
    drawdown = zscore_map(
        {s: abs(features_by_symbol[s].get("max_drawdown_120", 0.0)) for s in symbols}
    )
    positive = zscore_map(
        {s: features_by_symbol[s].get("positive_ratio_60", 0.0) for s in symbols}
    )
    components: dict[str, dict[str, float]] = {}
    for symbol in symbols:
        components[symbol] = {
            "momentum_120": momentum.get(symbol, 0.0),
            "trend_stability_60": stability.get(symbol, 0.0),
            "low_volatility_60": -volatility.get(symbol, 0.0),
            "low_drawdown_120": -drawdown.get(symbol, 0.0),
            "positive_ratio_60": positive.get(symbol, 0.0),
        }
    return components


def score_value_quality(
    features_by_symbol: Mapping[str, Mapping[str, float]],
) -> dict[str, float]:
    """价值/质量组合综合得分 = 各 z 分量按 :data:`VALUE_QUALITY_WEIGHTS` 加权。"""

    components = value_quality_components(features_by_symbol)
    scores: dict[str, float] = {}
    for symbol, values in components.items():
        scores[symbol] = float(
            sum(VALUE_QUALITY_WEIGHTS[name] * value for name, value in values.items())
        )
    return scores


def multi_factor_scores(
    features_by_symbol: Mapping[str, Mapping[str, float]],
    *,
    momentum_key: str = "momentum_20",
    volatility_key: str = "volatility_20",
) -> dict[str, float]:
    """多因子打分：动量 z − 波动率 z（与 ``multi_factor`` 策略口径一致）。"""

    symbols = list(features_by_symbol)
    momentum = zscore_map(
        {s: features_by_symbol[s].get(momentum_key, 0.0) for s in symbols}
    )
    volatility = zscore_map(
        {s: features_by_symbol[s].get(volatility_key, 0.0) for s in symbols}
    )
    return {symbol: momentum.get(symbol, 0.0) - volatility.get(symbol, 0.0) for symbol in symbols}


@dataclass(frozen=True)
class BreakoutParams:
    """异动突破策略/推荐共用的阈值（策略参数校验后构造）。"""

    breakout_window: int = 20
    volume_ratio_min: float = 1.5
    min_gain: float = 0.02
    max_gain: float = 0.095
    max_recent_gain: float = 0.20
    min_dist_ma20: float = 0.0


def breakout_signal(
    features: Mapping[str, float] | None,
    params: BreakoutParams | None = None,
) -> tuple[bool, float, dict[str, Any]]:
    """判断是否满足“异动前兆”条件，并给出 0~1 打分与中文原因分解。

    条件（全部满足才算候选）：

    1. 突破：收盘价高于**前 N 日最高价**（``breakout_20 > 0``）；
    2. 量比放大：当日量比 ``volume_ratio_1_20 ≥ volume_ratio_min``（默认 1.5，
       即当日成交量达到 20 日均量的 1.5 倍以上；缺失时退化为 5 日/20 日量比）；
    3. 温和涨幅：当日涨幅在 ``[min_gain, max_gain]``（默认 2%~9.5%，不追涨停）；
    4. 未暴涨：20 日累计涨幅 ≤ ``max_recent_gain``（默认 20%）；
    5. 站上 MA20：``dist_ma20 ≥ min_dist_ma20``。

    Returns:
        ``(是否候选, 得分0~1, 原因分解)``；不满足时为 False 并给出未通过项。
    """

    params = params or BreakoutParams()
    if not features:
        return False, 0.0, {"原因": "历史数据不足"}
    checks: dict[str, Any] = {}
    passed = True

    breakout = features.get("breakout_20")
    check_breakout = breakout is not None and breakout > 0
    checks["breakout"] = check_breakout
    passed = passed and check_breakout

    # 量比优先用“当日量 / 20 日均量”（经典量比口径），缺失时退化为 5 日/20 日量比
    volume_ratio = features.get("volume_ratio_1_20")
    if volume_ratio is None:
        volume_ratio = features.get("volume_ratio_5_20")
    check_volume = volume_ratio is not None and volume_ratio >= params.volume_ratio_min
    checks["volume_ratio"] = check_volume
    passed = passed and check_volume

    gain = features.get("gain_today")
    check_gain = gain is not None and params.min_gain <= gain <= params.max_gain
    checks["gain"] = check_gain
    passed = passed and check_gain

    recent_gain = features.get("cum_return_20")
    check_recent = recent_gain is not None and recent_gain <= params.max_recent_gain
    checks["not_overheated"] = check_recent
    passed = passed and check_recent

    dist_ma20 = features.get("dist_ma20")
    check_trend = dist_ma20 is not None and dist_ma20 >= params.min_dist_ma20
    checks["above_ma20"] = check_trend
    passed = passed and check_trend

    # 得分：即使未通过也给出部分得分（用于“观察名单”排序，便于 UI 展示接近条件的标的）
    def _clamp(value: float) -> float:
        return float(min(max(value, 0.0), 1.0))

    volume_component = (
        _clamp((float(volume_ratio) - 1.0) / 2.0) if volume_ratio is not None else 0.0
    )
    breakout_component = (
        _clamp(max(float(breakout), 0.0) / 0.03) if breakout is not None else 0.0
    )
    if gain is None:
        gain_component = 0.0
    elif params.min_gain <= float(gain) <= params.max_gain:
        gain_component = _clamp(
            (float(gain) - params.min_gain) / max(params.max_gain - params.min_gain, 1e-9)
        )
    else:
        # 距目标区间的远近（越接近越高），但明显偏离时趋近 0
        distance = min(abs(float(gain) - params.min_gain), abs(float(gain) - params.max_gain))
        gain_component = _clamp(1.0 - distance / max(params.max_gain - params.min_gain, 1e-9)) * 0.3
    score = float(_clamp(0.45 * volume_component + 0.35 * breakout_component + 0.20 * gain_component))
    return passed, score, {
        "checks": checks,
        "volume_ratio": volume_ratio,
        "breakout": breakout,
        "gain_today": gain,
        "cum_return_20": recent_gain,
        "dist_ma20": dist_ma20,
    }


def breakout_reason(features: Mapping[str, float]) -> str:
    """把异动因子拼成中文理由（推荐/预览展示）。"""

    parts: list[str] = []
    if (value := features.get("volume_ratio_1_20")) is not None:
        parts.append(f"量比 {value:.2f}（当日/20日均量）")
    if (value := features.get("volume_ratio_5_20")) is not None:
        parts.append(f"5日量比 {value:.2f}")
    if (value := features.get("breakout_20")) is not None:
        parts.append(f"突破20日高点 {value * 100:+.2f}%")
    if (value := features.get("gain_today")) is not None:
        parts.append(f"当日涨幅 {value * 100:+.2f}%")
    if (value := features.get("cum_return_20")) is not None:
        parts.append(f"20日累计 {value * 100:+.2f}%")
    if (value := features.get("dist_ma20")) is not None:
        parts.append(f"距MA20 {value * 100:+.2f}%")
    return "、".join(parts) if parts else "无有效因子"


def describe_score(symbol: str, components: Mapping[str, float], labels: Mapping[str, str] | None = None) -> str:
    """把因子分量拼成中文得分分解（推荐理由）。"""

    labels = labels or {}
    parts = [
        f"{labels.get(name, name)} {value:+.2f}"
        for name, value in components.items()
        if abs(float(value)) >= 0.01
    ]
    return f"{symbol}：" + ("，".join(parts) if parts else "各因子接近截面均值")


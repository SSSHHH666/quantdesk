"""回测绩效指标计算（任务 M1-12，口径冻结于 SPEC §8.3）。

指标口径（**测试按此手算，容差 1e-6**）
---------------------------------------

====================  ==================================================
键                    口径
====================  ==================================================
``total_return``      总收益率 = 期末权益 / 期初权益 − 1；
                      若传入 ``initial_cash`` 则期初取初始资金（含首日收益）。
``annual_return``     年化收益率（几何）：``(1+total_return)^(252/交易日数) − 1``，
                      交易日数 = 日收益样本数（252 天/年）。
``max_drawdown``      **最大回撤（负数）** = ``min(权益/历史峰值 − 1)``，无回撤为 0；
                      展示用绝对值可读 ``max_drawdown_abs``。
``sharpe``            夏普 = ``mean(日收益)/std(日收益, ddof=1) × √252``（rf=0）；
                      ``risk_free_rate`` 按年化无风险利率折算到日。
``win_rate``          胜率（**按平仓交易计**）：盈利平仓笔数 / 平仓笔数；
                      FIFO 配对后仍未平仓的买入**不计入**（另给 ``open_trade_count`` /
                      ``open_quantity`` 并写入 ``notes``，避免误解为亏损）。
``profit_factor``     盈亏比 = 总盈利 / |总亏损|（按平仓交易盈亏，含双边费用）。
``trade_count``       交易次数 = 成交记录条数（买 + 卖）。
``volatility``        年化波动率 = ``std(日收益, ddof=1) × √252``。
``calmar``            卡玛 = ``annual_return / |max_drawdown|``。
``sortino``           索提诺 = ``mean(日收益) / downside_deviation × √252``，
                      下行波动 = ``sqrt(mean(min(日收益,0)^2))``。
====================  ==================================================

除零与无交易安全（SPEC §8.3 异常降级）
--------------------------------------
* 无平仓交易：``win_rate`` / ``profit_factor`` / 盈亏统计 = ``None``，并在 ``notes`` 里说明；
* 无回撤：``max_drawdown = 0.0``，``calmar = None``；
* 收益方差为 0（单日/恒定权益）：``sharpe`` / ``volatility`` = ``None`` 或 0.0；
* 全部指标**不抛异常**，缺失原因写入返回字典的 ``notes``（中文）。

输入约定
--------
* ``equity``：``pd.DataFrame``（需含 ``equity``/``total_asset`` 之一，可选
  ``trade_date`` 日期列）或 ``pd.Series``（索引为日期）。
* ``trades``：``pd.DataFrame`` / ``list[dict]`` / ``list[core.models.Fill]``；
  需要字段 ``symbol/side/price/quantity``，可选 ``fee_total``（或其分项
  ``commission/stamp_tax/transfer_fee/reg_fee``）、``trade_date``。
  若已带 ``pnl``（或 ``profit``）列，则直接按该列统计平仓盈亏。
* ``benchmark``：基准价格序列（``pd.Series``）或含 ``close`` 列的 ``pd.DataFrame``。
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from collections.abc import Mapping
from datetime import date, datetime
from enum import Enum
from typing import Any

import numpy as np
import pandas as pd

__all__ = [
    "TRADING_DAYS_PER_YEAR",
    "METRIC_LABELS",
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
]

#: A 股年化交易日数（SPEC §8.3：annual_return / sharpe 均按 252 天）
TRADING_DAYS_PER_YEAR = 252

#: 权益列候选名（回测引擎/交易域两套叫法都兼容）
_EQUITY_COLUMNS: tuple[str, ...] = ("equity", "total_asset", "net_value", "nav", "equity_curve")
_DATE_COLUMNS: tuple[str, ...] = ("trade_date", "date", "ts", "datetime")

#: 指标中文名（UI KPI 卡、HTML 报告、对比表统一引用）
METRIC_LABELS: dict[str, str] = {
    "total_return": "总收益率",
    "annual_return": "年化收益率",
    "max_drawdown": "最大回撤",
    "max_drawdown_abs": "最大回撤（绝对值）",
    "sharpe": "夏普比率",
    "sortino": "索提诺比率",
    "win_rate": "胜率",
    "profit_factor": "盈亏比",
    "trade_count": "交易次数",
    "closed_trade_count": "平仓交易数",
    "open_trade_count": "未平仓买入笔数",
    "open_quantity": "未平仓股数",
    "winning_trades": "盈利笔数",
    "losing_trades": "亏损笔数",
    "breakeven_trades": "持平笔数",
    "volatility": "年化波动率",
    "calmar": "卡玛比率",
    "avg_win": "平均盈利",
    "avg_loss": "平均亏损",
    "avg_trade_pnl": "平均平仓盈亏",
    "best_trade": "最佳单笔",
    "worst_trade": "最差单笔",
    "gross_profit": "总盈利",
    "gross_loss": "总亏损",
    "total_fees": "总费用",
    "n_days": "交易日数",
    "initial_equity": "期初权益",
    "final_equity": "期末权益",
    "start_date": "开始日期",
    "end_date": "结束日期",
    "benchmark_return": "基准收益率",
    "benchmark_annual_return": "基准年化收益率",
    "benchmark_max_drawdown": "基准最大回撤",
    "excess_return": "超额收益",
    "beta": "Beta",
    "alpha": "Alpha（年化）",
}

#: 百分比类指标（展示时 ×100 并加 %）
_PERCENT_KEYS = frozenset(
    {
        "total_return",
        "annual_return",
        "max_drawdown",
        "max_drawdown_abs",
        "volatility",
        "win_rate",
        "benchmark_return",
        "benchmark_annual_return",
        "benchmark_max_drawdown",
        "excess_return",
        "alpha",
        "return_pct",
    }
)

#: 比率类指标（展示两位小数）
_RATIO_KEYS = frozenset({"sharpe", "sortino", "calmar", "profit_factor", "beta"})

#: 计数类指标（展示整数）
_COUNT_KEYS = frozenset(
    {
        "trade_count",
        "closed_trade_count",
        "open_trade_count",
        "open_quantity",
        "winning_trades",
        "losing_trades",
        "breakeven_trades",
        "n_days",
    }
)


# ---------------------------------------------------------------------------
# 一、输入清洗工具
# ---------------------------------------------------------------------------


def _date_labels(index: Any) -> list[str]:
    """把索引转成 ``YYYY-MM-DD`` 字符串列表（失败时退化为 ``str``）。"""

    try:
        converted = pd.to_datetime(list(index))
        labels: list[str] = []
        for value in converted:
            if pd.isna(value):
                labels.append("")
            else:
                labels.append(str(value)[:10])
        return labels
    except Exception:
        return [str(item) for item in index]


def _extract_equity(equity: Any) -> tuple[np.ndarray, list[str]]:
    """把 equity 统一成 ``(权益数组, 日期标签列表)``，并剔除无效值。"""

    if isinstance(equity, pd.DataFrame):
        column = next((name for name in _EQUITY_COLUMNS if name in equity.columns), None)
        if column is None:
            raise ValueError(
                f"权益曲线缺少权益列；需要以下之一：{'、'.join(_EQUITY_COLUMNS)}；"
                f"当前列：{list(equity.columns)}"
            )
        raw = equity[column]
        date_column = next((name for name in _DATE_COLUMNS if name in equity.columns), None)
        labels = _date_labels(equity[date_column] if date_column is not None else equity.index)
    elif isinstance(equity, pd.Series):
        raw = equity
        labels = _date_labels(equity.index)
    elif isinstance(equity, (list, tuple, np.ndarray)):
        raw = pd.Series(list(equity), dtype="float64")
        labels = [str(i) for i in range(len(raw))]
    else:
        raise ValueError(
            f"权益曲线参数非法：需要 DataFrame/Series 或数值序列，收到 {type(equity).__name__}"
            + ("（None 表示没有权益数据）" if equity is None else "")
        )

    numeric = pd.to_numeric(raw, errors="coerce").astype("float64")
    values = numeric.to_numpy()
    mask = np.isfinite(values)
    if not mask.any():
        raise ValueError("权益曲线为空或全部为无效值（NaN/inf），无法计算绩效指标")
    values = values[mask]
    labels = [label for label, keep in zip(labels, mask) if keep]
    return values, labels


def _clean_returns(values: np.ndarray) -> np.ndarray:
    """去掉 NaN/inf 后的日收益数组（保持原顺序）。"""

    values = np.asarray(values, dtype="float64")
    return values[np.isfinite(values)]


def _finite(value: Any) -> float | None:
    """转成 JSON 友好的 Python float；无效值返回 None。"""

    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _add_note(notes: list[str], message: str) -> None:
    if message not in notes:
        notes.append(message)


# ---------------------------------------------------------------------------
# 二、基础指标函数（可独立调用，便于手算核对）
# ---------------------------------------------------------------------------


def daily_returns(equity: Any, initial_cash: float | None = None) -> pd.Series:
    """日收益率序列（首个元素为 NaN 时会被保留在索引首日）。

    Args:
        equity: 权益曲线。
        initial_cash: 初始资金；提供时首日收益按 ``权益[0]/初始资金 − 1`` 计算，
            不提供时首日无收益（期末/期初均取权益曲线自身）。

    Returns:
        ``pd.Series``（索引为日期标签字符串），含未定义处 NaN。
    """

    values, labels = _extract_equity(equity)
    n = len(values)
    if initial_cash is not None:
        base = float(initial_cash)
        if not math.isfinite(base) or base <= 0:
            raise ValueError(f"初始资金 initial_cash 必须为正数，收到 {initial_cash!r}")
        returns = np.full(n, np.nan, dtype="float64")
        with np.errstate(divide="ignore", invalid="ignore"):
            returns[0] = values[0] / base - 1.0
            if n > 1:
                returns[1:] = values[1:] / values[:-1] - 1.0
        return pd.Series(returns, index=labels, name="daily_return")

    if n < 2:
        return pd.Series(dtype="float64", name="daily_return")
    with np.errstate(divide="ignore", invalid="ignore"):
        returns = values[1:] / values[:-1] - 1.0
    return pd.Series(returns, index=labels[1:], name="daily_return")


def drawdown_series(equity: Any) -> pd.Series:
    """回撤序列（**负值或 0**，= 权益/历史峰值 − 1）。"""

    values, labels = _extract_equity(equity)
    peaks = np.maximum.accumulate(values)
    with np.errstate(divide="ignore", invalid="ignore"):
        drawdown = values / peaks - 1.0
    drawdown = np.where(np.isfinite(drawdown), drawdown, 0.0)
    return pd.Series(np.minimum(drawdown, 0.0), index=labels, name="drawdown")


def max_drawdown(equity: Any) -> float:
    """最大回撤（负数，无回撤返回 0.0）。"""

    series = drawdown_series(equity)
    if len(series) == 0:
        return 0.0
    value = float(series.min())
    return value if math.isfinite(value) else 0.0


def annualized_return(
    total_return: float,
    n_periods: int,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """几何年化收益率：``(1+total)^(periods_per_year/n_periods) − 1``。"""

    total = _finite(total_return)
    if total is None:
        return 0.0
    if n_periods <= 0:
        return 0.0
    if total <= -1.0:
        return -1.0
    growth = 1.0 + total
    try:
        exponent = periods_per_year / float(n_periods)
        value = math.exp(math.log(growth) * exponent) - 1.0
    except (ValueError, OverflowError):
        return -1.0 if growth <= 0 else float("inf")
    if not math.isfinite(value):
        return float("inf") if value > 0 else -1.0
    return float(value)


def sharpe_ratio(
    returns: Any,
    risk_free_rate: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float | None:
    """夏普比率：``mean(超额日收益)/std(日收益,ddof=1)×√periods``；无波动返回 None。"""

    values = _clean_returns(np.asarray(returns, dtype="float64"))
    if values.size < 2:
        return None
    std = float(np.std(values, ddof=1))
    if not math.isfinite(std) or std <= 0:
        return None
    excess = values - float(risk_free_rate) / periods_per_year
    return float(np.mean(excess) / std * math.sqrt(periods_per_year))


def sortino_ratio(
    returns: Any,
    risk_free_rate: float = 0.0,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float | None:
    """索提诺比率：``mean(超额日收益)/下行波动×√periods``；无下行波动返回 None。"""

    values = _clean_returns(np.asarray(returns, dtype="float64"))
    if values.size < 2:
        return None
    downside = np.minimum(values, 0.0)
    downside_dev = float(np.sqrt(np.mean(downside**2)))
    if not math.isfinite(downside_dev) or downside_dev <= 0:
        return None
    excess = values - float(risk_free_rate) / periods_per_year
    return float(np.mean(excess) / downside_dev * math.sqrt(periods_per_year))


def volatility(
    returns: Any,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float | None:
    """年化波动率 = 日收益标准差（ddof=1）× √252；样本不足返回 None。"""

    values = _clean_returns(np.asarray(returns, dtype="float64"))
    if values.size < 2:
        return None
    std = float(np.std(values, ddof=1))
    if not math.isfinite(std):
        return None
    return float(std * math.sqrt(periods_per_year))


def monthly_returns(equity: Any) -> pd.Series:
    """月度收益率序列（末月按最后交易日权益计算；索引为 ``YYYY-MM``）。"""

    values, labels = _extract_equity(equity)
    try:
        index = pd.to_datetime(labels)
    except Exception:
        return pd.Series(dtype="float64", name="monthly_return")
    series = pd.Series(values, index=index, name="equity")
    try:
        month_end = series.resample("ME").last()
    except ValueError:  # 旧版 pandas
        month_end = series.resample("M").last()
    month_end = month_end.dropna()
    if month_end.empty:
        return pd.Series(dtype="float64", name="monthly_return")
    result = month_end.pct_change()
    result.iloc[0] = month_end.iloc[0] / values[0] - 1.0 if values[0] != 0 else np.nan
    result.index = [str(period)[:7] for period in month_end.index]
    result.name = "monthly_return"
    return result


# ---------------------------------------------------------------------------
# 三、平仓交易（FIFO 配对）
# ---------------------------------------------------------------------------

_BUY_SIDE_ALIASES = frozenset({"BUY", "B", "LONG", "买", "买入"})
_SELL_SIDE_ALIASES = frozenset({"SELL", "S", "SHORT", "卖", "卖出"})


def _normalize_trade_side(value: Any) -> str | None:
    raw = value
    if isinstance(raw, Enum):  # 兼容 core.models.Side 等 str 枚举
        raw = raw.value
    elif not isinstance(raw, str) and hasattr(raw, "value"):
        raw = raw.value
    text = str(raw).strip().upper() if raw is not None else ""
    if text in _BUY_SIDE_ALIASES:
        return "BUY"
    if text in _SELL_SIDE_ALIASES:
        return "SELL"
    return None


def _normalize_trades(trades: Any) -> pd.DataFrame:
    """把成交记录统一成 DataFrame（不做列重命名，仅做结构兼容）。"""

    if trades is None:
        return pd.DataFrame()
    if isinstance(trades, pd.DataFrame):
        frame = trades.copy()
    elif isinstance(trades, (list, tuple)):
        if not trades:
            return pd.DataFrame()
        records: list[dict[str, Any]] = []
        for item in trades:
            if isinstance(item, Mapping):
                records.append(dict(item))
            elif hasattr(item, "model_dump"):
                records.append(item.model_dump())
            elif hasattr(item, "__dict__"):
                records.append(dict(vars(item)))
            else:
                raise TypeError(f"trades 元素无法转换为字典：{type(item).__name__}")
        frame = pd.DataFrame(records)
    else:
        raise TypeError(f"trades 必须是 DataFrame 或 list，收到 {type(trades).__name__}")
    return frame


def _trade_field(frame: pd.DataFrame, names: tuple[str, ...]) -> pd.Series | None:
    for name in names:
        if name in frame.columns:
            return frame[name]
    return None


def _trade_fee(frame: pd.DataFrame) -> pd.Series:
    """取单笔总费用：优先 ``fee_total``，否则累加四项费用分项，缺失按 0。"""

    if "fee_total" in frame.columns:
        return pd.to_numeric(frame["fee_total"], errors="coerce").fillna(0.0).astype("float64")
    total = pd.Series(0.0, index=frame.index, dtype="float64")
    for name in ("commission", "stamp_tax", "transfer_fee", "reg_fee"):
        if name in frame.columns:
            total = total + pd.to_numeric(frame[name], errors="coerce").fillna(0.0)
    return total


CLOSED_TRADE_COLUMNS: list[str] = [
    "symbol",
    "entry_date",
    "exit_date",
    "quantity",
    "entry_cost",
    "exit_proceeds",
    "pnl",
    "return_pct",
]

#: 未平仓买入批次列（``open_positions`` 返回）
OPEN_POSITION_COLUMNS: list[str] = ["symbol", "entry_date", "quantity", "unit_cost", "cost"]


def _fifo_match(frame: pd.DataFrame) -> tuple[list[dict[str, Any]], dict[str, deque[list[Any]]]]:
    """FIFO 配对成交：返回（平仓交易行列表, 未平仓买入批次）。

    口径：**未平仓的买入只留在 lots 里，不产生平仓交易、不计入胜率/盈亏比**；
    卖单找不到对应买入（数据缺失/卖空）时跳过。若 ``frame`` 自带 ``pnl`` 列，
    则视为外部已配对（此时无法判断未平仓批次，返回空 lots）。
    """

    if frame.empty:
        return [], {}

    pnl_column = _trade_field(frame, ("pnl", "profit", "realized_pnl"))
    symbol_series = _trade_field(frame, ("symbol", "code"))
    quantity_series = _trade_field(frame, ("quantity", "qty", "size", "volume"))
    if symbol_series is None or quantity_series is None:
        return [], {}

    date_series = _trade_field(frame, ("trade_date", "date", "traded_at"))

    if pnl_column is not None:
        pnl_values = pd.to_numeric(pnl_column, errors="coerce")
        rows: list[dict[str, Any]] = []
        for idx, pnl in pnl_values.items():
            if pd.isna(pnl):
                continue
            rows.append(
                {
                    "symbol": str(symbol_series.loc[idx]),
                    "entry_date": None,
                    "exit_date": (
                        str(date_series.loc[idx])[:10] if date_series is not None else None
                    ),
                    "quantity": int(pd.to_numeric(quantity_series.loc[idx], errors="coerce") or 0),
                    "entry_cost": None,
                    "exit_proceeds": None,
                    "pnl": float(pnl),
                    "return_pct": None,
                }
            )
        return rows, {}

    price_series = _trade_field(frame, ("price", "fill_price", "avg_fill_price", "avg_price", "trade_price"))
    side_series = _trade_field(frame, ("side", "direction"))
    if price_series is None or side_series is None:
        return [], {}

    fee_series = _trade_fee(frame)
    lots: dict[str, deque[list[Any]]] = defaultdict(deque)
    rows = []
    for idx in range(len(frame)):
        symbol = str(symbol_series.iloc[idx] or "").strip()
        if not symbol:
            continue
        side = _normalize_trade_side(side_series.iloc[idx])
        if side is None:
            continue
        try:
            quantity = int(pd.to_numeric(quantity_series.iloc[idx], errors="coerce"))
            price = float(pd.to_numeric(price_series.iloc[idx], errors="coerce"))
        except (TypeError, ValueError):
            continue
        if quantity <= 0 or not math.isfinite(price) or price <= 0:
            continue
        fee = float(fee_series.iloc[idx] or 0.0)
        date_label = str(date_series.iloc[idx])[:10] if date_series is not None else None

        if side == "BUY":
            unit_cost = (price * quantity + fee) / quantity
            lots[symbol].append([quantity, unit_cost, date_label])
            continue

        remaining = quantity
        matched = 0
        cost = 0.0
        entry_dates: list[str] = []
        while remaining > 0 and lots[symbol]:
            lot = lots[symbol][0]
            take = min(int(lot[0]), remaining)
            cost += float(lot[1]) * take
            if lot[2]:
                entry_dates.append(str(lot[2]))
            lot[0] = int(lot[0]) - take
            remaining -= take
            matched += take
            if int(lot[0]) <= 0:
                lots[symbol].popleft()
        if matched <= 0:
            continue
        fee_share = fee * matched / quantity
        proceeds = price * matched - fee_share
        pnl = proceeds - cost
        rows.append(
            {
                "symbol": symbol,
                "entry_date": entry_dates[0] if entry_dates else None,
                "exit_date": date_label,
                "quantity": matched,
                "entry_cost": round(cost, 2),
                "exit_proceeds": round(proceeds, 2),
                "pnl": round(pnl, 2),
                "return_pct": (pnl / cost) if cost > 0 else None,
            }
        )
    return rows, lots


def closed_trades(trades: Any) -> pd.DataFrame:
    """按 FIFO 把成交记录配对成平仓交易，计算每笔已实现盈亏（含双边费用）。

    返回列：``symbol``、``entry_date``、``exit_date``、``quantity``、
    ``entry_cost``、``exit_proceeds``、``pnl``、``return_pct``。
    若 ``trades`` 已带 ``pnl``/``profit`` 列，则直接采用（视为引擎已配对）。
    **未平仓的买入不计入平仓交易**（详见 :func:`open_positions`）；
    卖单找不到对应买入（如数据缺失/卖空）时该笔跳过，不报错。
    """

    frame = _normalize_trades(trades)
    if frame.empty:
        return pd.DataFrame(columns=CLOSED_TRADE_COLUMNS)
    rows, _lots = _fifo_match(frame)
    return pd.DataFrame(rows, columns=CLOSED_TRADE_COLUMNS)


def open_positions(trades: Any) -> pd.DataFrame:
    """FIFO 配对后仍未平仓的买入批次（用于解释胜率口径）。

    返回列：``symbol``、``entry_date``、``quantity``、``unit_cost``（含买入费用的
    每股成本）、``cost``（该批次总成本）。``trades`` 自带 ``pnl`` 列时无法判断
    未平仓批次，返回空表。
    """

    frame = _normalize_trades(trades)
    if frame.empty:
        return pd.DataFrame(columns=OPEN_POSITION_COLUMNS)
    _rows, lots = _fifo_match(frame)
    open_rows: list[dict[str, Any]] = []
    for symbol, queue in lots.items():
        for lot in queue:
            quantity = int(lot[0])
            if quantity <= 0:
                continue
            unit_cost = float(lot[1])
            open_rows.append(
                {
                    "symbol": symbol,
                    "entry_date": lot[2],
                    "quantity": quantity,
                    "unit_cost": round(unit_cost, 6),
                    "cost": round(unit_cost * quantity, 2),
                }
            )
    return pd.DataFrame(open_rows, columns=OPEN_POSITION_COLUMNS)


# ---------------------------------------------------------------------------
# 四、主入口：compute_metrics
# ---------------------------------------------------------------------------


def _extract_benchmark(benchmark: Any) -> pd.Series | None:
    """把基准统一成数值 Series（价格/净值），失败或为空返回 None。"""

    if benchmark is None:
        return None
    if isinstance(benchmark, pd.DataFrame):
        column = next(
            (name for name in ("close", "benchmark_close", "price", "nav", "value") if name in benchmark.columns),
            None,
        )
        if column is None:
            column = benchmark.columns[0] if len(benchmark.columns) else None
        if column is None:
            return None
        series = benchmark[column]
    elif isinstance(benchmark, pd.Series):
        series = benchmark
    else:
        try:
            series = pd.Series(np.asarray(benchmark, dtype="float64"))
        except (TypeError, ValueError):
            return None
    numeric = pd.to_numeric(series, errors="coerce").astype("float64")
    labels = _date_labels(numeric.index)
    numeric.index = labels
    numeric = numeric[np.isfinite(numeric.to_numpy())]
    return numeric if len(numeric) >= 1 else None


def compute_metrics(
    equity: Any,
    trades: Any = None,
    benchmark: Any = None,
    *,
    initial_cash: float | None = None,
    risk_free_rate: float = 0.0,
) -> dict[str, Any]:
    """计算回测绩效指标（CF-5 契约方法，键名冻结）。

    Args:
        equity: 权益曲线（``pd.DataFrame`` 含 ``equity``/``total_asset`` 列，或 Series）。
        trades: 成交明细（DataFrame / list[dict] / list[core.models.Fill]）。
        benchmark: 基准价格序列（可选，缺失时基准相关键为 None 并在 notes 说明）。
        initial_cash: 初始资金（可选；提供时总收益从初始资金起算，含首日）。
        risk_free_rate: 年化无风险利率（默认 0，SPEC §8.3）。

    Returns:
        指标字典（必含：``total_return`` / ``annual_return`` / ``max_drawdown`` /
        ``sharpe`` / ``win_rate`` / ``profit_factor`` / ``trade_count`` /
        ``volatility`` / ``calmar``；另含盈亏统计、基准、``notes`` 等辅助键）。
        所有数值为 Python 原生 float/int/None/str，可直接 ``json.dumps`` 落库。

    Raises:
        ValueError: 权益曲线为空 / 为 ``None`` / 类型或列不合法（属于输入错误，不是“除零”）。
    """

    values, labels = _extract_equity(equity)
    notes: list[str] = []
    n_rows = int(values.size)

    if initial_cash is not None:
        base = float(initial_cash)
        if not math.isfinite(base) or base <= 0:
            raise ValueError(f"初始资金 initial_cash 必须为正数，收到 {initial_cash!r}")
        with np.errstate(divide="ignore", invalid="ignore"):
            total_return = float(values[-1] / base - 1.0)
    else:
        if values[0] <= 0:
            _add_note(notes, "期初权益 ≤ 0，总收益率无意义，按 0 处理")
            total_return = 0.0
        else:
            total_return = float(values[-1] / values[0] - 1.0)

    returns_series = daily_returns(pd.Series(values, index=labels), initial_cash=initial_cash)
    returns = _clean_returns(returns_series.to_numpy())
    n_periods = int(returns.size)
    annual_return = annualized_return(total_return, n_periods)
    drawdowns = drawdown_series(pd.Series(values, index=labels))
    max_dd = float(drawdowns.min()) if len(drawdowns) else 0.0
    if not math.isfinite(max_dd):
        max_dd = 0.0
    volatility_value = volatility(returns)
    sharpe = sharpe_ratio(returns, risk_free_rate)
    sortino = sortino_ratio(returns, risk_free_rate)
    calmar = float(annual_return / abs(max_dd)) if max_dd != 0 else None

    # ---- 交易统计（按平仓交易计；未平仓买入不计入胜率/盈亏比） ----------
    trade_frame = _normalize_trades(trades)
    trade_count = int(len(trade_frame))
    closed_rows, open_lots = _fifo_match(trade_frame) if not trade_frame.empty else ([], {})
    closed = pd.DataFrame(closed_rows, columns=CLOSED_TRADE_COLUMNS)
    closed_count = int(len(closed))
    open_trade_count = 0
    open_quantity = 0
    for queue in open_lots.values():
        for lot in queue:
            quantity = int(lot[0])
            if quantity <= 0:
                continue
            open_trade_count += 1
            open_quantity += quantity
    if open_quantity > 0:
        _add_note(
            notes,
            f"有 {open_trade_count} 笔买入（共 {open_quantity} 股）尚未平仓，"
            "不计入胜率与盈亏比（按平仓交易口径）",
        )
    if closed_count > 0:
        pnl_values = pd.to_numeric(closed["pnl"], errors="coerce").dropna().to_numpy(dtype="float64")
        gross_profit = float(pnl_values[pnl_values > 0].sum())
        gross_loss = float(-pnl_values[pnl_values < 0].sum())
        winning = int((pnl_values > 0).sum())
        losing = int((pnl_values < 0).sum())
        breakeven = int((pnl_values == 0).sum())
        win_rate: float | None = winning / closed_count
        if gross_loss > 0:
            profit_factor: float | None = gross_profit / gross_loss
        else:
            profit_factor = None
            _add_note(notes, "无亏损平仓交易，盈亏比无定义（返回 None）")
        avg_win: float | None = gross_profit / winning if winning > 0 else None
        avg_loss: float | None = -gross_loss / losing if losing > 0 else None
        avg_trade_pnl: float | None = float(pnl_values.mean())
        best_trade: float | None = float(pnl_values.max())
        worst_trade: float | None = float(pnl_values.min())
    else:
        gross_profit = gross_loss = 0.0
        winning = losing = breakeven = 0
        win_rate = None
        profit_factor = None
        avg_win = avg_loss = avg_trade_pnl = best_trade = worst_trade = None
        _add_note(notes, "无平仓交易，胜率/盈亏比等交易类指标无定义（返回 None）")

    total_fees: float | None = None
    if not trade_frame.empty:
        fees = _trade_fee(trade_frame)
        total_fees = float(fees.sum()) if len(fees) else 0.0

    if n_periods < 2:
        _add_note(notes, "日收益样本不足 2 个，夏普/波动率等波动类指标无定义（返回 None）")
    if max_dd == 0:
        _add_note(notes, "区间内无回撤，卡玛比率无定义（返回 None）")
    if trade_count < 30:
        _add_note(notes, "交易次数 < 30，回测结果统计显著性不足（SPEC §8.3 防过拟合提示）")
    if n_rows < 250:
        _add_note(notes, "样本期不足 250 个交易日，回测结果可能不稳健（RQ_WARN_SHORT_SAMPLE）")

    metrics: dict[str, Any] = {
        "total_return": _finite(total_return),
        "annual_return": _finite(annual_return),
        "max_drawdown": _finite(max_dd),
        "max_drawdown_abs": _finite(abs(max_dd)),
        "sharpe": _finite(sharpe),
        "sortino": _finite(sortino),
        "win_rate": _finite(win_rate),
        "profit_factor": _finite(profit_factor),
        "trade_count": trade_count,
        "closed_trade_count": closed_count,
        "open_trade_count": open_trade_count,
        "open_quantity": open_quantity,
        "winning_trades": winning,
        "losing_trades": losing,
        "breakeven_trades": breakeven,
        "volatility": _finite(volatility_value),
        "calmar": _finite(calmar),
        "avg_win": _finite(avg_win),
        "avg_loss": _finite(avg_loss),
        "avg_trade_pnl": _finite(avg_trade_pnl),
        "best_trade": _finite(best_trade),
        "worst_trade": _finite(worst_trade),
        "gross_profit": _finite(gross_profit),
        "gross_loss": _finite(gross_loss),
        "total_fees": _finite(total_fees),
        "n_days": n_rows,
        "initial_equity": _finite(values[0]),
        "final_equity": _finite(values[-1]),
        "start_date": labels[0] if labels else None,
        "end_date": labels[-1] if labels else None,
        "benchmark_return": None,
        "benchmark_annual_return": None,
        "benchmark_max_drawdown": None,
        "excess_return": None,
        "beta": None,
        "alpha": None,
        "notes": notes,
    }

    # ---- 基准（可选） ---------------------------------------------------
    benchmark_series = _extract_benchmark(benchmark)
    if benchmark_series is None:
        if benchmark is not None:
            _add_note(notes, "基准数据为空或无法解析，基准相关指标跳过")
    elif len(benchmark_series) < 2 or float(benchmark_series.iloc[0]) == 0:
        _add_note(notes, "基准数据不足，基准相关指标跳过")
    else:
        benchmark_return = float(benchmark_series.iloc[-1] / benchmark_series.iloc[0] - 1.0)
        benchmark_returns = pd.Series(
            benchmark_series.iloc[1:].to_numpy(dtype="float64")
            / benchmark_series.iloc[:-1].to_numpy(dtype="float64")
            - 1.0,
            index=benchmark_series.index[1:],
        )
        benchmark_annual = annualized_return(benchmark_return, len(benchmark_returns))
        metrics["benchmark_return"] = _finite(benchmark_return)
        metrics["benchmark_annual_return"] = _finite(benchmark_annual)
        metrics["benchmark_max_drawdown"] = _finite(max_drawdown(benchmark_series))
        metrics["excess_return"] = _finite(total_return - benchmark_return)

        # Beta / Alpha：仅当两边日收益索引可对齐时计算
        portfolio_series = returns_series.dropna()
        benchmark_series_returns = benchmark_returns.dropna()
        aligned = pd.concat([portfolio_series, benchmark_series_returns], axis=1, join="inner").dropna()
        if len(aligned) >= 2:
            market = aligned.iloc[:, 1].to_numpy(dtype="float64")
            portfolio = aligned.iloc[:, 0].to_numpy(dtype="float64")
            variance = float(np.var(market, ddof=1))
            if variance > 0:
                beta = float(np.cov(portfolio, market, ddof=1)[0, 1] / variance)
                alpha_daily = (
                    float(np.mean(portfolio))
                    - risk_free_rate / TRADING_DAYS_PER_YEAR
                    - beta * (float(np.mean(market)) - risk_free_rate / TRADING_DAYS_PER_YEAR)
                )
                metrics["beta"] = _finite(beta)
                metrics["alpha"] = _finite(alpha_daily * TRADING_DAYS_PER_YEAR)
        else:
            _add_note(notes, "基准与权益曲线日期无法对齐，Beta/Alpha 跳过")

    return metrics


# ---------------------------------------------------------------------------
# 五、展示辅助（UI KPI 卡 / HTML 报告）
# ---------------------------------------------------------------------------


def format_metric(key: str, value: Any, digits: int = 2) -> str:
    """把指标格式化成中文可读字符串（``None`` → "—"，百分比 ×100 加 %）。"""

    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        if math.isinf(number):
            return "∞" if number > 0 else "-∞"
        return "—"
    if key in _PERCENT_KEYS:
        return f"{number * 100:.{digits}f}%"
    if key in _RATIO_KEYS:
        return f"{number:.{digits}f}"
    if key in _COUNT_KEYS:
        return f"{int(round(number))}"
    if key in {"initial_equity", "final_equity", "avg_win", "avg_loss", "avg_trade_pnl", "best_trade", "worst_trade", "gross_profit", "gross_loss", "total_fees"}:
        return f"{number:,.{digits}f}"
    return f"{number:.{digits}f}"


def format_metrics(metrics: Mapping[str, Any], *, digits: int = 2) -> list[dict[str, Any]]:
    """批量格式化（保持 ``METRIC_LABELS`` 顺序），供 UI 表格/报告直接渲染。"""

    rows: list[dict[str, Any]] = []
    for key, label in METRIC_LABELS.items():
        if key not in metrics:
            continue
        rows.append(
            {
                "key": key,
                "label": label,
                "value": metrics.get(key),
                "display": format_metric(key, metrics.get(key), digits=digits),
            }
        )
    return rows

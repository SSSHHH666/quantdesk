"""绩效指标单元测试（手工构造权益序列逐项核对，容差 1e-6）。

对应验收标准：
- AC-F3-05 给定手工构造的权益序列，``max_drawdown`` / ``annual_return`` / ``sharpe``
  与手算值一致（容差 1e-6）
- TASKS M1-16：① 手工构造权益序列逐项核对；② 无交易 / 零回撤 / 单日数据边界；
  ③ 胜率与盈亏比口径验证

Lead 已裁决口径（写用例时按此，避免误报）：
- ``max_drawdown`` 为**负数**（如 -0.25），其绝对值版本是 ``max_drawdown_abs``；
- ``calmar = annual_return / |max_drawdown|``，最大回撤为 0 时返回 ``None`` 并在 ``notes`` 说明。
"""

from __future__ import annotations

import json
import math

import pandas as pd
import pytest

from backtest.metrics import (
    annualized_return,
    closed_trades,
    compute_metrics,
    daily_returns,
    drawdown_series,
    max_drawdown,
    sharpe_ratio,
    volatility,
)

#: 手工核对容差（AC-F3-05）
TOL = 1e-6
#: 年化交易日数（与 metrics 模块口径一致）
TRADING_DAYS = 252


def _equity_series(values: list[float], start: str = "2026-10-19") -> pd.Series:
    """构造带交易日期索引的权益序列（确定性、可手算）。"""
    dates = pd.bdate_range(start=start, periods=len(values))
    return pd.Series(values, index=dates, name="equity")


# ---------------------------------------------------------------------------
# 一、手工核对：权益 100 → 110 → 99 → 108.9
#     - 总收益 = 108.9/100 - 1 = 8.9%
#     - 日收益 = [+10%, -10%, +10%]
#     - 最大回撤 = 99/110 - 1 = -10%
#     - 夏普 = mean(日收益)/std(ddof=1)(日收益) × √252
# ---------------------------------------------------------------------------

HAND_VALUES = [100.0, 110.0, 99.0, 108.9]
HAND_RETURNS = [0.10, -0.10, 0.10]
HAND_TOTAL_RETURN = 0.089
HAND_MAX_DD = -0.10


def _hand_stats(returns: list[float]) -> tuple[float, float]:
    """手算 mean / 样本标准差（ddof=1），用于独立核对实现。"""
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
    return mean, math.sqrt(variance)


@pytest.mark.unit
def test_hand_computed_metrics_match() -> None:
    equity = _equity_series(HAND_VALUES)

    metrics = compute_metrics(equity)

    mean, std = _hand_stats(HAND_RETURNS)
    expected_sharpe = mean / std * math.sqrt(TRADING_DAYS)
    expected_volatility = std * math.sqrt(TRADING_DAYS)
    expected_annual = (1.0 + HAND_TOTAL_RETURN) ** (TRADING_DAYS / len(HAND_RETURNS)) - 1.0

    assert metrics["n_days"] == 4
    assert metrics["total_return"] == pytest.approx(HAND_TOTAL_RETURN, abs=TOL)
    assert metrics["final_equity"] == pytest.approx(108.9, abs=TOL)
    assert metrics["initial_equity"] == pytest.approx(100.0, abs=TOL)

    # Lead 裁决：max_drawdown 为负数，绝对值版另给 max_drawdown_abs
    assert metrics["max_drawdown"] == pytest.approx(HAND_MAX_DD, abs=TOL)
    assert metrics["max_drawdown_abs"] == pytest.approx(abs(HAND_MAX_DD), abs=TOL)

    assert metrics["sharpe"] == pytest.approx(expected_sharpe, abs=TOL)
    assert metrics["volatility"] == pytest.approx(expected_volatility, abs=TOL)
    assert metrics["annual_return"] == pytest.approx(expected_annual, rel=1e-9)

    # calmar = annual / |mdd|
    assert metrics["calmar"] == pytest.approx(expected_annual / abs(HAND_MAX_DD), rel=1e-9)


@pytest.mark.unit
def test_max_drawdown_is_negative_by_rule() -> None:
    """口径锁定：正数版只能从 max_drawdown_abs 取，防止 UI 混用导致符号错。"""
    metrics = compute_metrics(_equity_series([100.0, 120.0, 90.0, 100.0]))

    assert metrics["max_drawdown"] < 0
    assert metrics["max_drawdown"] == pytest.approx(90.0 / 120.0 - 1.0, abs=TOL)
    assert metrics["max_drawdown_abs"] == pytest.approx(abs(metrics["max_drawdown"]), abs=TOL)


@pytest.mark.unit
def test_daily_returns_hand_values() -> None:
    equity = _equity_series(HAND_VALUES)

    returns = daily_returns(equity)

    assert len(returns) == 3
    assert list(returns.to_numpy()) == pytest.approx(HAND_RETURNS, abs=TOL)


@pytest.mark.unit
def test_daily_returns_with_initial_cash_includes_first_day() -> None:
    """提供 initial_cash 时首日收益 = 权益[0]/初始资金 - 1（共 3 个收益）。"""
    equity = _equity_series([110.0, 99.0, 108.9])

    returns = daily_returns(equity, initial_cash=100.0)
    metrics = compute_metrics(equity, initial_cash=100.0)

    assert len(returns) == 3
    assert list(returns.to_numpy()) == pytest.approx(HAND_RETURNS, abs=TOL)
    assert metrics["total_return"] == pytest.approx(HAND_TOTAL_RETURN, abs=TOL)
    assert metrics["max_drawdown"] == pytest.approx(HAND_MAX_DD, abs=TOL)


@pytest.mark.unit
def test_drawdown_series_is_non_positive() -> None:
    series = drawdown_series(_equity_series([100.0, 120.0, 90.0, 100.0]))

    assert (series <= 1e-12).all(), "回撤序列应为负值或 0"
    assert series.min() == pytest.approx(90.0 / 120.0 - 1.0, abs=TOL)
    assert max_drawdown(_equity_series([100.0, 120.0, 90.0, 100.0])) == pytest.approx(-0.25, abs=TOL)


@pytest.mark.unit
def test_annualized_return_formula() -> None:
    """几何年化：(1+total)^(252/n) - 1；1 年（252 期）时等于总收益。"""
    assert annualized_return(0.089, TRADING_DAYS) == pytest.approx(0.089, abs=1e-12)
    assert annualized_return(0.089, 3) == pytest.approx((1.089 ** (TRADING_DAYS / 3)) - 1, rel=1e-12)
    assert annualized_return(0.0, 0) == 0.0
    assert annualized_return(-1.0, 252) == -1.0


@pytest.mark.unit
def test_sharpe_and_volatility_need_two_samples() -> None:
    assert sharpe_ratio([0.01]) is None
    assert volatility([0.01]) is None
    # 无波动（全 0）→ 夏普无定义
    assert sharpe_ratio([0.0, 0.0, 0.0]) is None


# ---------------------------------------------------------------------------
# 二、边界：无交易 / 零回撤 / 单日数据
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_zero_drawdown_returns_zero_and_calmar_none() -> None:
    """一路新高 → max_drawdown = 0.0、calmar = None，且 notes 中有说明。"""
    metrics = compute_metrics(_equity_series([100.0, 101.0, 102.0, 103.0]))

    assert metrics["max_drawdown"] == 0.0
    assert metrics["max_drawdown_abs"] == 0.0
    assert metrics["calmar"] is None
    assert any("无回撤" in note or "回撤" in note for note in metrics["notes"])


@pytest.mark.unit
def test_no_trades_gives_none_trade_metrics() -> None:
    """无成交：交易类指标一律 None（不是 0），并在 notes 说明。"""
    metrics = compute_metrics(_equity_series(HAND_VALUES), trades=None)

    assert metrics["trade_count"] == 0
    assert metrics["closed_trade_count"] == 0
    assert metrics["win_rate"] is None
    assert metrics["profit_factor"] is None
    assert metrics["avg_win"] is None
    assert any("无平仓交易" in note for note in metrics["notes"])


@pytest.mark.unit
def test_single_day_equity_is_degenerate_but_does_not_crash() -> None:
    """单日数据：收益样本为 0 → 波动类指标 None、总收益 0、不抛异常。"""
    metrics = compute_metrics(_equity_series([100000.0]))

    assert metrics["n_days"] == 1
    assert metrics["total_return"] == pytest.approx(0.0, abs=TOL)
    assert metrics["annual_return"] == pytest.approx(0.0, abs=TOL)
    assert metrics["max_drawdown"] == 0.0
    assert metrics["sharpe"] is None
    assert metrics["volatility"] is None
    assert metrics["calmar"] is None


@pytest.mark.unit
def test_empty_equity_raises_value_error() -> None:
    """空序列 / None → ValueError（“权益曲线为空”属于输入错误，不是除零）。

    口径更新：O-01（None 曾抛 TypeError）已由 Dev2 统一为 ValueError，本用例随之收紧。
    """
    with pytest.raises(ValueError):
        compute_metrics(pd.Series(dtype="float64"))

    with pytest.raises(ValueError):
        compute_metrics(None)  # type: ignore[arg-type]


@pytest.mark.unit
def test_dataframe_without_equity_column_raises() -> None:
    frame = pd.DataFrame({"foo": [1.0, 2.0]})

    with pytest.raises(ValueError) as excinfo:
        compute_metrics(frame)
    assert "权益列" in str(excinfo.value)


@pytest.mark.unit
def test_dataframe_with_equity_and_date_columns() -> None:
    """支持 DataFrame[date, equity] 形态（UI/回测引擎落库后的标准结构）。"""
    frame = pd.DataFrame(
        {
            "date": pd.bdate_range(start="2026-10-19", periods=4),
            "equity": HAND_VALUES,
        }
    )

    metrics = compute_metrics(frame)

    assert metrics["total_return"] == pytest.approx(HAND_TOTAL_RETURN, abs=TOL)
    assert metrics["max_drawdown"] == pytest.approx(HAND_MAX_DD, abs=TOL)


# ---------------------------------------------------------------------------
# 三、胜率 / 盈亏比口径
# ---------------------------------------------------------------------------


def _engine_paired_trades() -> list[dict[str, object]]:
    """引擎已配对好的平仓交易（带 pnl 列；metrics 应直接采用）。"""
    return [
        {"symbol": "600000.SH", "quantity": 1000, "pnl": 100.0, "fee_total": 5.64},
        {"symbol": "600001.SH", "quantity": 1000, "pnl": -50.0, "fee_total": 6.0},
        {"symbol": "600002.SH", "quantity": 1000, "pnl": 200.0, "fee_total": 5.0},
        {"symbol": "600003.SH", "quantity": 1000, "pnl": -30.0, "fee_total": 4.0},
    ]


@pytest.mark.unit
def test_win_rate_and_profit_factor_from_closed_trades() -> None:
    """4 笔平仓：2 盈 2 亏 → 胜率 50%、盈亏比 300/80 = 3.75。"""
    metrics = compute_metrics(_equity_series(HAND_VALUES), _engine_paired_trades())

    assert metrics["trade_count"] == 4
    assert metrics["closed_trade_count"] == 4
    assert metrics["winning_trades"] == 2
    assert metrics["losing_trades"] == 2
    assert metrics["win_rate"] == pytest.approx(0.5, abs=TOL)
    assert metrics["gross_profit"] == pytest.approx(300.0, abs=TOL)
    assert metrics["gross_loss"] == pytest.approx(80.0, abs=TOL)
    assert metrics["profit_factor"] == pytest.approx(300.0 / 80.0, abs=TOL)
    assert metrics["avg_win"] == pytest.approx(150.0, abs=TOL)
    assert metrics["avg_loss"] == pytest.approx(-40.0, abs=TOL)
    assert metrics["best_trade"] == pytest.approx(200.0, abs=TOL)
    assert metrics["worst_trade"] == pytest.approx(-50.0, abs=TOL)
    assert metrics["total_fees"] == pytest.approx(20.64, abs=TOL)


@pytest.mark.unit
def test_all_winners_gives_none_profit_factor() -> None:
    """无亏损交易 → 盈亏比无定义（None），而不是 inf。"""
    trades = [
        {"symbol": "600000.SH", "quantity": 1000, "pnl": 100.0, "fee_total": 5.0},
        {"symbol": "600001.SH", "quantity": 1000, "pnl": 50.0, "fee_total": 5.0},
    ]

    metrics = compute_metrics(_equity_series(HAND_VALUES), trades)

    assert metrics["win_rate"] == pytest.approx(1.0, abs=TOL)
    assert metrics["profit_factor"] is None
    assert any("盈亏比" in note for note in metrics["notes"])


@pytest.mark.unit
def test_fifo_pairing_computes_pnl_with_both_side_fees() -> None:
    """FIFO 配对：买 1000@10.00（费 5.64）→ 卖 1000@11.00（费 11.64），盈亏 = 982.72。"""
    trades = [
        {"symbol": "600000.SH", "side": "BUY", "price": 10.00, "quantity": 1000, "fee_total": 5.64,
         "trade_date": "2026-10-19"},
        {"symbol": "600000.SH", "side": "SELL", "price": 11.00, "quantity": 1000, "fee_total": 11.64,
         "trade_date": "2026-10-20"},
    ]

    closed = closed_trades(trades)

    assert len(closed) == 1
    row = closed.iloc[0]
    expected_cost = 10.00 * 1000 + 5.64
    expected_proceeds = 11.00 * 1000 - 11.64
    assert row["entry_cost"] == pytest.approx(expected_cost, abs=0.01)
    assert row["exit_proceeds"] == pytest.approx(expected_proceeds, abs=0.01)
    assert row["pnl"] == pytest.approx(expected_proceeds - expected_cost, abs=0.01)
    assert row["quantity"] == 1000


@pytest.mark.unit
def test_partial_fifo_sell_matches_fraction_of_lot() -> None:
    """卖 500 股只配掉一半持仓，剩余 500 股仍留在多头队列。"""
    trades = [
        {"symbol": "600000.SH", "side": "BUY", "price": 10.0, "quantity": 1000, "fee_total": 5.0},
        {"symbol": "600000.SH", "side": "SELL", "price": 11.0, "quantity": 500, "fee_total": 3.0},
    ]

    closed = closed_trades(trades)

    assert len(closed) == 1
    assert closed.iloc[0]["quantity"] == 500
    assert closed.iloc[0]["pnl"] == pytest.approx(500 * 11.0 - 3.0 - (500 * 10.0 + 500 * 0.005), abs=0.01)


@pytest.mark.unit
def test_sell_without_buy_is_skipped_not_error() -> None:
    """卖单找不到对应买入（数据缺失/卖空）→ 跳过，不抛异常。"""
    closed = closed_trades([{"symbol": "600000.SH", "side": "SELL", "price": 11.0, "quantity": 100}])

    assert closed.empty


# ---------------------------------------------------------------------------
# 四、输出契约：可 JSON 序列化、键名冻结
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_metrics_keys_are_frozen_and_json_serializable() -> None:
    """AC-F3-06/CF-5：必含键 + 全部值为原生类型，可直接 json.dumps 落库。"""
    metrics = compute_metrics(_equity_series(HAND_VALUES), _engine_paired_trades())

    required = {
        "total_return",
        "annual_return",
        "max_drawdown",
        "sharpe",
        "win_rate",
        "profit_factor",
        "trade_count",
        "volatility",
        "calmar",
    }
    assert required <= set(metrics)

    payload = json.dumps(metrics)  # 不抛异常即通过
    assert '"max_drawdown"' in payload


@pytest.mark.unit
def test_same_input_is_reproducible() -> None:
    """AC-F3-07：同一参数两次回测指标完全一致。"""
    equity = _equity_series(HAND_VALUES)
    trades = _engine_paired_trades()

    first = compute_metrics(equity, trades)
    second = compute_metrics(equity, trades)

    assert first == second

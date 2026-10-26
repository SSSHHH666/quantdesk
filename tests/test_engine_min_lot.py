"""回测引擎最小 1 手规则测试（高价股“买不进”问题）。

规则（fail-safe）：
- 目标仓位金额 < 1 手成本，但 1 手成本 ≤ 单票仓位上限（默认 20%×总资产）→ 按 1 手买入，
  并在 warnings/notes 标注“已按最小 1 手调整”；
- 1 手成本 > 单票仓位上限 → 不交易，notes 给出中文原因与建议；
- 低价股仍按 position_pct 目标仓位正常买入。
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from backtest.engine import RQ_WARN_POSITION_MIN_LOT, BacktestEngine
from strategy.base import Context, Signal, Strategy, temporary_strategy
from core.models import Side

pytestmark = pytest.mark.unit

SYMBOL = "600519.SH"


class _AlwaysBuy(Strategy):
    display_name = "测试_持续买入"

    @classmethod
    def params_schema(cls) -> dict[str, dict[str, object]]:
        return {
            "position_pct": {
                "type": "float",
                "default": 10.0,
                "min": 0.5,
                "max": 100.0,
                "step": 0.5,
                "label": "单票目标仓位",
                "help": "测试用",
            }
        }

    def on_bar(self, ctx: Context) -> list[Signal]:
        signals: list[Signal] = []
        for symbol in ctx.symbols:
            if ctx.quantity(symbol) == 0:
                signals.append(
                    Signal(symbol=symbol, side=Side.BUY, strength=1.0, reason="测试买入")
                )
        return signals


def _flat_bars(price: float, days: int = 40) -> pd.DataFrame:
    """构造单标的平价日线（涨跌幅 0，便于精确核对仓位与股数）。"""

    start = date(2024, 1, 2)
    dates = [start + timedelta(days=offset) for offset in range(days * 2)]
    dates = [day for day in dates if day.weekday() < 5][:days]
    rows = [
        {
            "trade_date": day,
            "symbol": SYMBOL,
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 1_000_000,
            "amount": 0.0,
        }
        for day in dates
    ]
    return pd.DataFrame(rows)


def _run(price: float, *, cash: float = 1_000_000.0, position_pct: float = 10.0):
    bars = _flat_bars(price)
    start = bars["trade_date"].min()
    end = bars["trade_date"].max()
    with temporary_strategy("test_always_buy", _AlwaysBuy):
        result = BacktestEngine().run(
            "test_always_buy",
            {"position_pct": position_pct},
            [SYMBOL],
            start,
            end,
            initial_cash=cash,
            bars=bars,
            benchmark="",
            run_id="min-lot-test",
        )
    return result


def test_low_price_buys_target_position() -> None:
    """低价股（10 元）：按 position_pct 目标仓位买入，不是 1 手。"""

    result = _run(price=10.0, cash=1_000_000.0, position_pct=10.0)

    assert len(result.trades) > 0
    first_buy = result.trades.iloc[0]
    assert int(first_buy["quantity"]) > 100, "10 元低价股应买远超 1 手"
    assert int(first_buy["quantity"]) % 100 == 0
    payload = result.equity.iloc[-1]
    assert float(payload["position_value"]) / float(payload["equity"]) <= 0.20 + 1e-6
    assert RQ_WARN_POSITION_MIN_LOT not in result.warnings


def test_high_price_uses_minimum_one_lot() -> None:
    """高价股（1488 元、100 万本金、10% 目标 < 1 手）：按最小 1 手买入且标注。"""

    result = _run(price=1488.0, cash=1_000_000.0, position_pct=10.0)

    assert len(result.trades) > 0, "1488 元高价股应能按最小 1 手成交（1 手 148,800 ≤ 20%×100 万）"
    first_buy = result.trades.iloc[0]
    assert int(first_buy["quantity"]) == 100
    assert RQ_WARN_POSITION_MIN_LOT in result.warnings
    assert any("最小 1 手" in note for note in result.notes)
    # 单票占比 ≤ 单票仓位上限 20%
    latest = result.equity.iloc[-1]
    ratio = float(latest["position_value"]) / float(latest["equity"])
    assert ratio <= 0.20 + 1e-6, f"单票占比 {ratio:.2%} 超过上限 20%"


def test_one_lot_above_position_limit_skips_trade() -> None:
    """1 手成本超过单票仓位上限（3000 元 × 100 股 = 30 万 > 20%×100 万）→ 不交易并说明。"""

    result = _run(price=3000.0, cash=1_000_000.0, position_pct=10.0)

    assert len(result.trades) == 0, "1 手超单票仓位上限时不应成交"
    assert any("无法满足单票仓位上限" in note for note in result.notes)
    assert any("提高本金" in note or "低价标的" in note for note in result.notes)


def test_single_position_never_exceeds_limit() -> None:
    """任何情况下单票市值占比不得超过 max_position_per_symbol_pct（默认 20%）。"""

    for price in (10.0, 500.0, 1488.0):
        result = _run(price=price, cash=1_000_000.0, position_pct=10.0)
        if len(result.trades) == 0:
            continue
        ratio = (
            result.equity["position_value"] / result.equity["equity"]
        ).max()
        assert float(ratio) <= 0.20 + 1e-6, f"价格 {price} 时单票占比 {ratio:.2%} 超上限"


class _RawCacheSource:
    """模拟数据层：不复权模式返回真实价（1488 元）行情。"""

    def __init__(self, price: float = 1488.0) -> None:
        self.price = price
        self.calls: list[str | None] = []

    def load(self, symbols: list[str], start: date, end: date, adjust: str | None = None) -> pd.DataFrame:
        self.calls.append(adjust)
        if adjust != "none":
            return pd.DataFrame()
        return _flat_bars(self.price)


def test_raw_mode_high_price_uses_minimum_one_lot() -> None:
    """price_adjust=none（数据层不复权）：1488 元高价股走最小 1 手，单票占比 ≤20%。"""

    source = _RawCacheSource(price=1488.0)
    bars = _flat_bars(1488.0)
    start = bars["trade_date"].min()
    end = bars["trade_date"].max()
    with temporary_strategy("test_always_buy", _AlwaysBuy):
        result = BacktestEngine(data_source=source).run(
            "test_always_buy",
            {"position_pct": 10.0},
            [SYMBOL],
            start,
            end,
            initial_cash=1_000_000.0,
            benchmark="",
        )

    assert source.calls and source.calls[0] == "none", "引擎应优先请求不复权行情"
    assert result.metadata["price_mode"] == "raw"
    assert result.metadata["synthetic_prices"] is False
    assert len(result.trades) > 0, "不复权模式下 1488 元应能按最小 1 手成交"
    assert int(result.trades.iloc[0]["quantity"]) == 100
    assert RQ_WARN_POSITION_MIN_LOT in result.warnings
    ratio = (result.equity["position_value"] / result.equity["equity"]).max()
    assert float(ratio) <= 0.20 + 1e-6, f"单票占比 {ratio:.2%} 超过上限 20%"


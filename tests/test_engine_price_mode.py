"""回测引擎价格口径测试（P0）：撮合/手数/资金用不复权价；取不到时回退并告警。

规则（Lead 决策）：
- 缓存类数据源优先 ``adjust="none"``（不复权）用于撮合/手数/资金核算；
- 仅当不复权无数据时回退 ``hfq``（复权合成价）→ ``RQ_WARN_SYNTHETIC_PRICE`` +
  ``metadata.price_mode="adjusted"``，并**关闭最小 1 手规则**（合成价手数不代表真实交易）。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd
import pytest

from backtest.engine import RQ_WARN_POSITION_MIN_LOT, RQ_WARN_SYNTHETIC_PRICE, BacktestEngine
from data.provider import MockProvider
from core.models import Signal, Side
from strategy.base import Context, Strategy, temporary_strategy

pytestmark = pytest.mark.unit

SYMBOL = "600519.SH"
START = date(2024, 1, 2)
END = date(2024, 6, 28)


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
        return [
            Signal(symbol=symbol, side=Side.BUY, strength=1.0, reason="测试买入")
            for symbol in ctx.symbols
            if ctx.quantity(symbol) == 0
        ]


def _bars(scale: float = 1.0) -> pd.DataFrame:
    bars = MockProvider().get_bars([SYMBOL], START, END, "1d").copy()
    for column in ("open", "high", "low", "close"):
        bars[column] = pd.to_numeric(bars[column], errors="coerce") * scale
    return bars


class _CacheSource:
    """模拟 BarCache：记录 adjust 参数；按模式返回不同价格。"""

    def __init__(self, *, raw_scale: float = 1.0, hfq_scale: float = 8.0, raw_empty: bool = False) -> None:
        self.calls: list[str | None] = []
        self.raw_scale = raw_scale
        self.hfq_scale = hfq_scale
        self.raw_empty = raw_empty

    def load(self, symbols: list[str], start: date, end: date, adjust: str | None = None) -> pd.DataFrame:
        self.calls.append(adjust)
        if adjust == "none":
            return pd.DataFrame() if self.raw_empty else _bars(self.raw_scale)
        return _bars(self.hfq_scale)


def _run_with_source(source: Any) -> Any:
    with temporary_strategy("test_always_buy", _AlwaysBuy):
        return BacktestEngine(data_source=source).run(
            "test_always_buy",
            {"position_pct": 10.0},
            [SYMBOL],
            START,
            END,
            initial_cash=1_000_000.0,
            benchmark="",
        )


def test_raw_prices_are_preferred() -> None:
    """优先 request adjust="none"（不复权），metadata.price_mode=raw。"""

    source = _CacheSource(raw_scale=1.0, hfq_scale=8.0)
    result = _run_with_source(source)

    assert source.calls and source.calls[0] == "none", f"应优先请求不复权数据：{source.calls}"
    assert result.metadata["price_mode"] == "raw"
    assert result.metadata["synthetic_prices"] is False
    assert RQ_WARN_SYNTHETIC_PRICE not in result.warnings


def test_hfq_fallback_marks_synthetic_and_disables_min_lot() -> None:
    """不复权无数据 → 回退 hfq：告警 + 关闭最小 1 手 + metadata 标记。"""

    # 合成价 250 元：1 手 25,000 ≤ 20%×100 万（本可走“最小 1 手”），但合成价下应关闭
    source = _CacheSource(raw_scale=1.0, hfq_scale=2.5, raw_empty=True)
    result = _run_with_source(source)

    assert source.calls[:2] == ["none", "hfq"], f"应先 none 后 hfq 回退：{source.calls}"
    assert result.metadata["price_mode"] == "adjusted"
    assert result.metadata["synthetic_prices"] is True
    assert RQ_WARN_SYNTHETIC_PRICE in result.warnings
    assert RQ_WARN_POSITION_MIN_LOT not in result.warnings
    assert len(result.trades) == 0, "合成价模式下不应按最小 1 手买入"
    assert any("合成价格" in note for note in result.notes)


def test_explicit_hfq_mode_warns_and_disables_min_lot() -> None:
    """显式 ``price_adjust="hfq"``：直接用复权合成价，告警并关闭最小 1 手规则。"""

    source = _CacheSource(raw_scale=1.0, hfq_scale=2.5, raw_empty=False)
    with temporary_strategy("test_always_buy", _AlwaysBuy):
        result = BacktestEngine(data_source=source, price_adjust="hfq").run(
            "test_always_buy",
            {"position_pct": 10.0},
            [SYMBOL],
            START,
            END,
            initial_cash=1_000_000.0,
            benchmark="",
        )

    assert source.calls and source.calls[0] == "hfq", f"应直接请求 hfq：{source.calls}"
    assert result.metadata["price_mode"] == "adjusted"
    assert result.metadata["synthetic_prices"] is True
    assert RQ_WARN_SYNTHETIC_PRICE in result.warnings
    assert len(result.trades) == 0


def test_invalid_price_adjust_rejected() -> None:
    """非法价格口径 → 中文 BacktestError。"""

    from backtest.engine import BacktestError

    with pytest.raises(BacktestError) as excinfo:
        BacktestEngine(price_adjust="qfq")
    assert "price_adjust" in str(excinfo.value)


def test_direct_bars_are_treated_as_raw() -> None:
    """外部直接传 bars：按“用户提供价”处理（不标记合成价，不改变最小 1 手规则）。"""

    with temporary_strategy("test_always_buy", _AlwaysBuy):
        result = BacktestEngine().run(
            "test_always_buy",
            {"position_pct": 10.0},
            [SYMBOL],
            START,
            END,
            initial_cash=1_000_000.0,
            bars=_bars(1.0),
            benchmark="",
        )

    assert result.metadata["price_mode"] == "as_provided"
    assert result.metadata["synthetic_prices"] is False
    assert RQ_WARN_SYNTHETIC_PRICE not in result.warnings

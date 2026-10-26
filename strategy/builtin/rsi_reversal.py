"""RSI 反转策略（任务 M1-08）。

规格（SPEC §8.2 规则细节 3）：参数 ``period=14``、``oversold=30``、``overbought=70``、
``position_pct=10``；RSI < oversold 买入、RSI > overbought 卖出。为避免指标在阈值附近
反复抖动导致同方向重复信号，这里采用“**进入超卖区/超买区**”的边沿触发；数据不足时
跳过该标的并记 INFO。

指标统一走 :mod:`strategy.base` 兼容层（TA-Lib 优先，pandas 兜底）；策略只读
:class:`~strategy.base.Context`，不访问数据库/网络/broker，信号可复现。
"""

from __future__ import annotations

import logging
import math

import pandas as pd

from core.models import Side
from strategy.base import (
    Context,
    Signal,
    Strategy,
    StrategyParamError,
    register,
    rsi,
)

logger = logging.getLogger(__name__)

__all__ = ["RsiReversalStrategy"]


@register("rsi_reversal")
class RsiReversalStrategy(Strategy):
    """RSI 反转：超卖买入、超买卖出（Wilder 平滑口径）。"""

    display_name = "RSI 反转"
    description = "RSI 低于超卖阈值买入、高于超买阈值卖出（默认 14/30/70）"
    startup_bars = 17

    @classmethod
    def params_schema(cls) -> dict[str, dict[str, object]]:
        return {
            "period": {
                "type": "int",
                "default": 14,
                "min": 2,
                "max": 120,
                "step": 1,
                "label": "RSI 周期",
                "help": "RSI 计算的交易日数，默认 14（Wilder 平滑）",
            },
            "oversold": {
                "type": "float",
                "default": 30.0,
                "min": 1.0,
                "max": 50.0,
                "step": 0.5,
                "label": "超卖阈值",
                "help": "RSI 低于该值时视为超卖（买入信号），须小于超买阈值",
            },
            "overbought": {
                "type": "float",
                "default": 70.0,
                "min": 50.0,
                "max": 99.0,
                "step": 0.5,
                "label": "超买阈值",
                "help": "RSI 高于该值时视为超买（卖出信号），须大于超卖阈值",
            },
            "position_pct": {
                "type": "float",
                "default": 10.0,
                "min": 0.5,
                "max": 100.0,
                "step": 0.5,
                "unit": "%",
                "label": "单票目标仓位",
                "help": "买入时目标市值占账户总资产的比例（%），由回测引擎换算成股数",
            },
        }

    @classmethod
    def validate_constraints(cls, params: dict[str, object]) -> None:
        oversold = float(params["oversold"])  # type: ignore[arg-type]
        overbought = float(params["overbought"])  # type: ignore[arg-type]
        if oversold >= overbought:
            raise StrategyParamError(
                f"超卖阈值（oversold={oversold:g}）必须小于超买阈值（overbought={overbought:g}）"
            )

    def on_bar(self, ctx: Context) -> list[Signal]:
        period = int(self.param("period", 14))
        oversold = float(self.param("oversold", 30.0))
        overbought = float(self.param("overbought", 70.0))
        need = period + 3
        signals: list[Signal] = []
        for symbol in ctx.symbols:
            history = ctx.history(symbol, need)
            if history is None or len(history) < need or "close" not in history.columns:
                logger.info("%s 历史数据不足（需要 %d 根），跳过", symbol, need)
                continue
            if _is_suspended(history.iloc[-1]):
                continue
            close = pd.to_numeric(history["close"], errors="coerce").astype("float64")
            rsi_line = rsi(close, period)
            if len(rsi_line) < 2:
                continue
            previous = rsi_line.iloc[-2]
            current = rsi_line.iloc[-1]
            if not math.isfinite(float(current)):
                continue
            current_value = float(current)
            previous_value = float(previous) if math.isfinite(float(previous)) else math.nan
            last_close = float(close.iloc[-1])

            entering_oversold = current_value < oversold and (
                not math.isfinite(previous_value) or previous_value >= oversold
            )
            entering_overbought = current_value > overbought and (
                not math.isfinite(previous_value) or previous_value <= overbought
            )
            if entering_oversold and ctx.quantity(symbol) == 0:
                signals.append(
                    Signal(
                        symbol=symbol,
                        side=Side.BUY,
                        strength=min(1.0, (oversold - current_value) / oversold + 0.5),
                        reason=f"RSI({period}) 进入超卖区：{current_value:.2f} < {oversold:g}",
                        price_hint=last_close,
                    )
                )
            elif entering_overbought and ctx.quantity(symbol) > 0:
                signals.append(
                    Signal(
                        symbol=symbol,
                        side=Side.SELL,
                        strength=min(1.0, (current_value - overbought) / (100.0 - overbought) + 0.5),
                        reason=f"RSI({period}) 进入超买区：{current_value:.2f} > {overbought:g}",
                        price_hint=last_close,
                    )
                )
        return signals


def _is_suspended(row: pd.Series) -> bool:
    """当日 K 线是否停牌（数据列缺失时按正常处理）。"""

    try:
        return bool(row.get("is_suspended", False))
    except (TypeError, ValueError):
        return False

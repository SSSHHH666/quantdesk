"""双均线策略（任务 M1-07）。

规格（SPEC §8.2 规则细节 3）：参数 ``fast=5``、``slow=20``、``position_pct=10``；
快线上穿慢线买入、下穿卖出。数据不足（不足 ``slow+2`` 根 bar）时跳过该标的并记录
INFO 日志；信号可复现（不使用随机数、不依赖当前时间）。

实现说明：指标统一走 :mod:`strategy.base` 的兼容层（优先 TA-Lib，缺失时 pandas
兜底）；策略只读 :class:`~strategy.base.Context`，**不访问数据库/网络/broker**。
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
    sma,
)

logger = logging.getLogger(__name__)

__all__ = ["MaCrossStrategy"]


@register("ma_cross")
class MaCrossStrategy(Strategy):
    """双均线：快线上穿慢线买入、下穿卖出。"""

    display_name = "双均线"
    description = "快线上穿慢线买入、下穿卖出（默认 5/20 日均线）"
    startup_bars = 22

    @classmethod
    def params_schema(cls) -> dict[str, dict[str, object]]:
        return {
            "fast": {
                "type": "int",
                "default": 5,
                "min": 2,
                "max": 120,
                "step": 1,
                "label": "快线周期",
                "help": "短周期简单移动平均的交易日数，须小于慢线周期",
            },
            "slow": {
                "type": "int",
                "default": 20,
                "min": 3,
                "max": 250,
                "step": 1,
                "label": "慢线周期",
                "help": "长周期简单移动平均的交易日数，建议为快线的 3~5 倍",
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
        fast = int(params["fast"])  # type: ignore[arg-type]
        slow = int(params["slow"])  # type: ignore[arg-type]
        if fast >= slow:
            raise StrategyParamError(
                f"快线周期（fast={fast}）必须小于慢线周期（slow={slow}）"
            )

    def on_bar(self, ctx: Context) -> list[Signal]:
        fast = int(self.param("fast", 5))
        slow = int(self.param("slow", 20))
        need = slow + 2
        signals: list[Signal] = []
        for symbol in ctx.symbols:
            history = ctx.history(symbol, need)
            if history is None or len(history) < need or "close" not in history.columns:
                logger.info("%s 历史数据不足（需要 %d 根），跳过", symbol, need)
                continue
            if _is_suspended(history.iloc[-1]):
                continue
            close = pd.to_numeric(history["close"], errors="coerce").astype("float64")
            fast_line = sma(close, fast)
            slow_line = sma(close, slow)
            if len(fast_line) < 2 or len(slow_line) < 2:
                continue
            values = (
                fast_line.iloc[-2],
                slow_line.iloc[-2],
                fast_line.iloc[-1],
                slow_line.iloc[-1],
            )
            if any(not math.isfinite(float(value)) for value in values):
                continue
            f_prev, s_prev, f_now, s_now = (float(value) for value in values)
            last_close = float(close.iloc[-1])

            if f_prev <= s_prev and f_now > s_now:
                signals.append(
                    Signal(
                        symbol=symbol,
                        side=Side.BUY,
                        strength=1.0,
                        reason=f"{fast} 日均线上穿 {slow} 日均线（金叉）",
                        price_hint=last_close,
                    )
                )
            elif f_prev >= s_prev and f_now < s_now and ctx.quantity(symbol) > 0:
                signals.append(
                    Signal(
                        symbol=symbol,
                        side=Side.SELL,
                        strength=1.0,
                        reason=f"{fast} 日均线下穿 {slow} 日均线（死叉）",
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

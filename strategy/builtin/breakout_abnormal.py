"""短期异动前兆策略（任务 R4 · 短期线，日频）。

捕捉“异动前兆”：**量比放大 + 突破 20 日高点 + 温和涨幅（2%~9.5%，不追涨停）
+ 近 20 日未暴涨过滤**，次日开盘由回测引擎撮合（与实盘信号时点一致）。

入选条件（全部满足，见 :func:`strategy.factors.breakout_signal`）：

1. 收盘价突破**前 20 日最高价**（``breakout_20 > 0``）；
2. 量比放大：**当日量比 = 当日成交量 / 20 日均量 ≥ ``volume_ratio_min``**（默认 1.5）；
3. 当日涨幅在 ``[min_gain, max_gain]``（默认 2%~9.5%，**不含涨停板追高**）；
4. 未暴涨：20 日累计涨幅 ≤ ``max_recent_gain``（默认 20%）；
5. 站上 MA20（趋势确认）。

每日按异动得分（量比 0.45 + 突破幅度 0.35 + 涨幅位置 0.20）取前 ``max_candidates`` 只买入。
离场条件（先触发先卖）：跌破买入均价 ``stop_loss_pct`` 止损、或收盘跌破 ``exit_ma`` 日均线。
策略只读 :class:`~strategy.base.Context`，不访问数据库/网络/broker，信号可复现。
"""

from __future__ import annotations

import logging
import math

import pandas as pd

from core.models import Side
from strategy.base import Context, Signal, Strategy, StrategyParamError, register, sma
from strategy.factors import (
    BreakoutParams,
    breakout_reason,
    breakout_signal,
    compute_breakout_features,
)

logger = logging.getLogger(__name__)

__all__ = ["BreakoutAbnormalStrategy"]


@register("breakout_abnormal")
class BreakoutAbnormalStrategy(Strategy):
    """异动前兆：量比放大 + 突破 20 日高点 + 温和涨幅（不追涨停）。"""

    display_name = "异动突破"
    description = "量比放大+突破20日高点+温和涨幅(2%~9.5%)+未暴涨过滤，日频短线"
    startup_bars = 22

    @classmethod
    def params_schema(cls) -> dict[str, dict[str, object]]:
        return {
            "breakout_window": {
                "type": "int",
                "default": 20,
                "min": 10,
                "max": 60,
                "step": 1,
                "label": "突破窗口",
                "help": "收盘价突破过去 N 个交易日的最高价视为突破",
            },
            "volume_ratio_min": {
                "type": "float",
                "default": 1.5,
                "min": 1.0,
                "max": 5.0,
                "step": 0.1,
                "label": "最小量比",
                "help": "当日成交量 / 20 日均量的下限（经典量比口径），越大越严格",
            },
            "min_gain": {
                "type": "float",
                "default": 2.0,
                "min": 0.5,
                "max": 5.0,
                "step": 0.1,
                "unit": "%",
                "label": "最小涨幅",
                "help": "当日涨幅下限（%），低于该值视为无异动",
            },
            "max_gain": {
                "type": "float",
                "default": 9.5,
                "min": 5.0,
                "max": 12.0,
                "step": 0.1,
                "unit": "%",
                "label": "最大涨幅",
                "help": "当日涨幅上限（%），默认 9.5% 避免追涨停板",
            },
            "max_recent_gain": {
                "type": "float",
                "default": 20.0,
                "min": 5.0,
                "max": 50.0,
                "step": 1.0,
                "unit": "%",
                "label": "20日累计涨幅上限",
                "help": "近 N 日累计涨幅超过该值视为已暴涨，过滤不追",
            },
            "max_candidates": {
                "type": "int",
                "default": 5,
                "min": 1,
                "max": 20,
                "step": 1,
                "label": "每日最多买入",
                "help": "每日按异动得分排序，最多买入的标的数量",
            },
            "position_pct": {
                "type": "float",
                "default": 10.0,
                "min": 0.5,
                "max": 20.0,
                "step": 0.5,
                "unit": "%",
                "label": "单票目标仓位",
                "help": "每只标的的目标市值占账户总资产比例（%）",
            },
            "stop_loss_pct": {
                "type": "float",
                "default": 7.0,
                "min": 2.0,
                "max": 15.0,
                "step": 0.5,
                "unit": "%",
                "label": "止损比例",
                "help": "收盘价相对买入均价的止损线（%），触发即卖出",
            },
            "exit_ma": {
                "type": "int",
                "default": 10,
                "min": 5,
                "max": 30,
                "step": 1,
                "label": "离场均线",
                "help": "收盘价跌破 N 日均线时卖出离场",
            },
        }

    @classmethod
    def validate_constraints(cls, params: dict[str, object]) -> None:
        min_gain = float(params["min_gain"])  # type: ignore[arg-type]
        max_gain = float(params["max_gain"])  # type: ignore[arg-type]
        if min_gain >= max_gain:
            raise StrategyParamError(f"最小涨幅（{min_gain:g}%）必须小于最大涨幅（{max_gain:g}%）")
        if float(params["volume_ratio_min"]) < 1.0:  # type: ignore[arg-type]
            raise StrategyParamError("最小量比必须 ≥ 1.0（低于 1 表示缩量，不符合“量比放大”）")

    def on_bar(self, ctx: Context) -> list[Signal]:
        breakout_window = int(self.param("breakout_window", 20))
        volume_ratio_min = float(self.param("volume_ratio_min", 1.5))
        params = BreakoutParams(
            breakout_window=breakout_window,
            volume_ratio_min=volume_ratio_min,
            min_gain=float(self.param("min_gain", 2.0)) / 100.0,
            max_gain=float(self.param("max_gain", 9.5)) / 100.0,
            max_recent_gain=float(self.param("max_recent_gain", 20.0)) / 100.0,
        )
        max_candidates = int(self.param("max_candidates", 5))
        stop_loss = float(self.param("stop_loss_pct", 7.0)) / 100.0
        exit_ma = max(2, int(self.param("exit_ma", 10)))
        need = max(breakout_window, 20, exit_ma) + 3

        signals: list[Signal] = []
        for symbol in ctx.symbols:
            history = ctx.history(symbol, need)
            if history is None or len(history) < need or "close" not in history.columns:
                continue
            if _is_suspended(history.iloc[-1]):
                continue
            close = pd.to_numeric(history["close"], errors="coerce").astype("float64")
            last_close = float(close.iloc[-1])
            position = ctx.position(symbol)
            held = ctx.quantity(symbol)
            if position is not None and held > 0:
                avg_cost = _avg_cost(position)
                stop_price = avg_cost * (1.0 - stop_loss) if avg_cost and avg_cost > 0 else None
                exit_reason = None
                if stop_price is not None and last_close <= stop_price:
                    exit_reason = (
                        f"触发止损：收盘 {last_close:.2f} ≤ 成本 {avg_cost:.2f} × (1−{stop_loss * 100:.1f}%)"
                    )
                else:
                    ma_values = sma(close, exit_ma)
                    if len(ma_values) and math.isfinite(float(ma_values.iloc[-1])):
                        ma_value = float(ma_values.iloc[-1])
                        if last_close < ma_value:
                            exit_reason = f"跌破 {exit_ma} 日均线（收盘 {last_close:.2f} < MA{exit_ma} {ma_value:.2f}）"
                if exit_reason:
                    signals.append(
                        Signal(
                            symbol=symbol,
                            side=Side.SELL,
                            strength=1.0,
                            reason=f"异动突破离场：{exit_reason}",
                            price_hint=last_close,
                        )
                    )
                continue

            features = compute_breakout_features(
                history, breakout_window=breakout_window, volume_short=5, volume_long=20
            )
            if features is None:
                continue
            passed, score, detail = breakout_signal(features, params)
            if not passed:
                continue
            signals.append(
                Signal(
                    symbol=symbol,
                    side=Side.BUY,
                    strength=float(max(0.1, min(1.0, score))),
                    reason=(
                        f"异动前兆（得分 {score:.2f}）：{breakout_reason(features)}；"
                        "温和涨幅未追涨停、未暴涨过滤通过"
                    ),
                    price_hint=last_close,
                    meta={"volume_ratio": features.get("volume_ratio_1_20"), "detail": detail},
                )
            )

        # 同一天最多买入 max_candidates 只（按得分排序；卖出信号不受限）
        buys = [signal for signal in signals if signal.side == Side.BUY]
        sells = [signal for signal in signals if signal.side == Side.SELL]
        buys.sort(key=lambda signal: signal.strength, reverse=True)
        return sells + buys[:max_candidates]


def _avg_cost(position: object) -> float | None:
    """读取持仓均价（兼容 core.models.Position 与 dict）。"""

    if position is None:
        return None
    raw = position.get("avg_cost") if isinstance(position, dict) else getattr(position, "avg_cost", None)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _is_suspended(row: pd.Series) -> bool:
    try:
        return bool(row.get("is_suspended", False))
    except (TypeError, ValueError):
        return False

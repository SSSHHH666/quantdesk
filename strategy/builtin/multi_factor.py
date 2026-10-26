"""多因子打分策略（任务 M1-09）。

规格（SPEC §8.2 规则细节 3）：参数 ``momentum_window=20``、``volatility_window=20``、
``top_n=3``、``rebalance_days=5``、``position_pct=10``；对标的池计算
``动量 z-score − 波动率 z-score`` 排序取前 N 名等权，按 ``rebalance_days`` 调仓。

实现要点
--------
* **打分**：动量 = 收盘价相对 ``momentum_window`` 个交易日的收益率；波动率 = 日收益率
  在 ``volatility_window`` 内的标准差；两者在当日截面内做 z-score（标准差为 0 时按 0）。
  ``score = 动量 z − 波动率 z``（动量高、波动低者得分高）。
* **调仓**：每 ``rebalance_days`` 个交易日触发一次（以策略实例内部计数器实现，确定性，
  不依赖当前时间）；对前 N 名发 BUY（由引擎按 ``position_pct`` 换算目标仓位），对
  **持有但不在前 N 名**的标的发 SELL（引擎清仓）。
* 同一根 bar 同一标的同一方向只发一次信号；数据不足的标的跳过并记 INFO。
* 只读 :class:`~strategy.base.Context`，不访问数据库/网络/broker，信号可复现。
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
)

logger = logging.getLogger(__name__)

__all__ = ["MultiFactorStrategy"]


@register("multi_factor")
class MultiFactorStrategy(Strategy):
    """多因子打分：动量 z-score − 波动率 z-score 排序取前 N 名等权调仓。"""

    display_name = "多因子打分"
    description = "动量 z-score 减波动率 z-score 排序，取前 N 名等权，按周期调仓"
    startup_bars = 22

    @classmethod
    def params_schema(cls) -> dict[str, dict[str, object]]:
        return {
            "momentum_window": {
                "type": "int",
                "default": 20,
                "min": 2,
                "max": 250,
                "step": 1,
                "label": "动量窗口",
                "help": "计算动量收益率的交易日数",
            },
            "volatility_window": {
                "type": "int",
                "default": 20,
                "min": 2,
                "max": 250,
                "step": 1,
                "label": "波动率窗口",
                "help": "计算日收益率标准差的交易日数",
            },
            "top_n": {
                "type": "int",
                "default": 3,
                "min": 1,
                "max": 50,
                "step": 1,
                "label": "选股数量",
                "help": "每次调仓取综合得分最高的前 N 只标的等权持有",
            },
            "rebalance_days": {
                "type": "int",
                "default": 5,
                "min": 1,
                "max": 250,
                "step": 1,
                "label": "调仓周期",
                "help": "每隔多少个交易日重新打分调仓（1 表示每日）",
            },
            "position_pct": {
                "type": "float",
                "default": 10.0,
                "min": 0.5,
                "max": 100.0,
                "step": 0.5,
                "unit": "%",
                "label": "单票目标仓位",
                "help": "每只入选标的的目标市值占账户总资产比例（%），由回测引擎换算股数",
            },
        }

    @classmethod
    def validate_constraints(cls, params: dict[str, object]) -> None:
        top_n = int(params["top_n"])  # type: ignore[arg-type]
        if top_n < 1:
            raise StrategyParamError("选股数量（top_n）必须 ≥ 1")

    def __init__(self, params: dict[str, object] | None = None) -> None:
        super().__init__(params)  # type: ignore[arg-type]
        self._bar_count = 0
        self._last_rebalance_bar = -1

    def on_init(self, ctx: Context) -> None:
        """每轮回测开始时重置调仓计数器（保证两次运行结果一致）。"""

        self._bar_count = 0
        self._last_rebalance_bar = -1

    def on_bar(self, ctx: Context) -> list[Signal]:
        self._bar_count += 1
        momentum_window = int(self.param("momentum_window", 20))
        volatility_window = int(self.param("volatility_window", 20))
        top_n = int(self.param("top_n", 3))
        rebalance_days = max(1, int(self.param("rebalance_days", 5)))
        need = max(momentum_window, volatility_window) + 2

        if self._last_rebalance_bar >= 0 and self._bar_count - self._last_rebalance_bar < rebalance_days:
            return []

        scored: dict[str, tuple[float, float]] = {}
        for symbol in ctx.symbols:
            history = ctx.history(symbol, need)
            if history is None or len(history) < need or "close" not in history.columns:
                logger.info("%s 历史数据不足（需要 %d 根），跳过打分", symbol, need)
                continue
            if _is_suspended(history.iloc[-1]):
                continue
            close = pd.to_numeric(history["close"], errors="coerce").astype("float64")
            if len(close) < momentum_window + 1:
                continue
            base_price = float(close.iloc[-(momentum_window + 1)])
            last_price = float(close.iloc[-1])
            if not math.isfinite(base_price) or base_price <= 0 or not math.isfinite(last_price):
                continue
            momentum = last_price / base_price - 1.0
            returns = close.pct_change().dropna()
            if len(returns) < volatility_window:
                continue
            recent_returns = returns.iloc[-volatility_window:]
            if recent_returns.isna().any():
                continue
            volatility_value = float(recent_returns.std(ddof=0))
            if not math.isfinite(volatility_value):
                continue
            scored[symbol] = (momentum, volatility_value)

        if len(scored) < 2:
            logger.info("有效标的不足 2 只（当前 %d 只），本次不调仓", len(scored))
            return []

        momentum_z = _zscore({symbol: value[0] for symbol, value in scored.items()})
        volatility_z = _zscore({symbol: value[1] for symbol, value in scored.items()})
        scores = {
            symbol: momentum_z[symbol] - volatility_z[symbol] for symbol in scored
        }
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        selected = [symbol for symbol, _score in ranked[:top_n]]
        selected_set = set(selected)
        self._last_rebalance_bar = self._bar_count

        signals: list[Signal] = []
        for rank, (symbol, score) in enumerate(ranked[:top_n], start=1):
            momentum, volatility_value = scored[symbol]
            last_close = ctx.close(symbol)
            signals.append(
                Signal(
                    symbol=symbol,
                    side=Side.BUY,
                    strength=max(0.1, min(1.0, 1.0 - (rank - 1) / max(top_n, 1))),
                    reason=(
                        f"多因子排名第 {rank}/{len(scored)}：动量 {momentum * 100:.2f}%、"
                        f"波动率 {volatility_value * 100:.2f}%、综合得分 {score:.3f}"
                    ),
                    price_hint=last_close,
                )
            )
        held_symbols = [symbol for symbol in ctx.symbols if ctx.quantity(symbol) > 0]
        for symbol in held_symbols:
            if symbol in selected_set:
                continue
            signals.append(
                Signal(
                    symbol=symbol,
                    side=Side.SELL,
                    strength=1.0,
                    reason=f"多因子调仓：未进入前 {top_n} 名，卖出离场",
                    price_hint=ctx.close(symbol),
                )
            )
        return signals


def _zscore(values: dict[str, float]) -> dict[str, float]:
    """截面 z-score；样本标准差为 0 或缺失时全部按 0 处理（除零安全）。"""

    if not values:
        return {}
    numbers = list(values.values())
    mean = sum(numbers) / len(numbers)
    if len(numbers) < 2:
        return {symbol: 0.0 for symbol in values}
    variance = sum((value - mean) ** 2 for value in numbers) / (len(numbers) - 1)
    std = math.sqrt(variance) if variance > 0 else 0.0
    if std <= 0 or not math.isfinite(std):
        return {symbol: 0.0 for symbol in values}
    return {symbol: (value - mean) / std for symbol, value in values.items()}


def _is_suspended(row: pd.Series) -> bool:
    """当日 K 线是否停牌（数据列缺失时按正常处理）。"""

    try:
        return bool(row.get("is_suspended", False))
    except (TypeError, ValueError):
        return False

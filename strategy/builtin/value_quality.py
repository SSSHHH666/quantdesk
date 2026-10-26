"""长期价值/质量组合策略（任务 R4 · 长期线）。

目标：质量 + 动量 + 低波动 + 回撤可控的**月频调仓**组合，持有 10~20 只，
追求“年化 10~20%”档的价值型组合（回测用价格类因子；基本面因子待数据源确认后接入，
见 ``strategy/factors.py`` 的因子清单与 ``docs/TASKS.md`` 后续项）。

打分（截面 z-score 加权，全部“越大越好”，见 :data:`strategy.factors.VALUE_QUALITY_WEIGHTS`）：

===============  =====  ==================================================
分量              权重    口径
===============  =====  ==================================================
``momentum_120``  0.30   120 日动量（趋势确认，避免接飞刀）
``trend_stability``0.20   60 日收益均值/标准差 ×√60（趋势稳定性，质量代理）
``low_volatility`` 0.20   −z(60 日波动率)（低波动溢价）
``low_drawdown``   0.15   −z(|120 日最大回撤|)（回撤可控）
``positive_ratio`` 0.15   60 日上涨交易日占比（持续性/质量代理）
===============  =====  ==================================================

调仓规则：每 ``rebalance_days``（默认 21 ≈ 月频）重新打分，买入前 ``top_n`` 名，
卖出已持有但跌出前 ``top_n`` 的标的；由回测引擎按 ``position_pct`` 换算目标仓位。
策略只读 :class:`~strategy.base.Context`，不访问数据库/网络/broker，信号可复现。
"""

from __future__ import annotations

import logging
import math
from typing import Any

import pandas as pd

from core.models import Side
from strategy.base import Context, Signal, Strategy, StrategyParamError, register
from strategy.factors import (
    DEFAULT_FACTOR_CONFIG,
    FEATURE_LABELS,
    compute_features,
    score_value_quality,
    value_quality_components,
)

logger = logging.getLogger(__name__)

__all__ = ["ValueQualityStrategy"]


@register("value_quality")
class ValueQualityStrategy(Strategy):
    """价值/质量组合：动量+趋势稳定+低波动+回撤可控，月频调仓持有 10~20 只。"""

    display_name = "价值质量组合"
    description = "质量+动量+低波动+回撤可控的月频组合，持有 10~20 只（价格类因子）"
    startup_bars = DEFAULT_FACTOR_CONFIG.required_rows

    @classmethod
    def params_schema(cls) -> dict[str, dict[str, object]]:
        return {
            "top_n": {
                "type": "int",
                "default": 15,
                "min": 10,
                "max": 20,
                "step": 1,
                "label": "持有只数",
                "help": "组合持有的标的数量（10~20 只，等权；数据不足时自动减少）",
            },
            "rebalance_days": {
                "type": "int",
                "default": 21,
                "min": 5,
                "max": 60,
                "step": 1,
                "label": "调仓周期（交易日）",
                "help": "每隔多少个交易日重新打分调仓，默认 21 ≈ 月频",
            },
            "position_pct": {
                "type": "float",
                "default": 5.0,
                "min": 0.5,
                "max": 10.0,
                "step": 0.5,
                "unit": "%",
                "label": "单票目标仓位",
                "help": "每只标的的目标市值占账户总资产比例（%）；15 只 × 5% ≈ 75% 总仓位",
            },
            "min_score": {
                "type": "float",
                "default": -9.0,
                "min": -9.0,
                "max": 9.0,
                "step": 0.1,
                "label": "最低入选得分",
                "help": "综合 z 得分低于该值不买入（默认 -9 表示不做额外过滤）",
            },
        }

    @classmethod
    def validate_constraints(cls, params: dict[str, object]) -> None:
        top_n = int(params["top_n"])  # type: ignore[arg-type]
        if not 10 <= top_n <= 20:
            raise StrategyParamError(f"持有只数（top_n）必须在 10~20 之间，当前 {top_n}")

    def __init__(self, params: dict[str, object] | None = None) -> None:
        super().__init__(params)  # type: ignore[arg-type]
        self._bar_count = 0
        self._last_rebalance_bar = -1

    def on_init(self, ctx: Context) -> None:
        """每轮回测开始重置调仓计数器（保证两次运行结果一致）。"""

        self._bar_count = 0
        self._last_rebalance_bar = -1

    def on_bar(self, ctx: Context) -> list[Signal]:
        self._bar_count += 1
        top_n = int(self.param("top_n", 15))
        rebalance_days = max(1, int(self.param("rebalance_days", 21)))
        min_score = float(self.param("min_score", -9.0))
        if self._last_rebalance_bar >= 0 and self._bar_count - self._last_rebalance_bar < rebalance_days:
            return []

        features_by_symbol: dict[str, dict[str, float]] = {}
        for symbol in ctx.symbols:
            history = ctx.history(symbol, DEFAULT_FACTOR_CONFIG.required_rows + 1)
            if history is None or _is_suspended(history.iloc[-1]):
                continue
            features = compute_features(history)
            if features is None:
                logger.info("%s 历史数据不足，跳过价值质量打分", symbol)
                continue
            features_by_symbol[symbol] = features
        if not features_by_symbol:
            logger.info("没有可用标的（历史不足），本次不调仓")
            return []

        scores = score_value_quality(features_by_symbol)
        components = value_quality_components(features_by_symbol)
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        selected = [symbol for symbol, score in ranked[:top_n] if score >= min_score]
        selected_set = set(selected)
        self._last_rebalance_bar = self._bar_count

        signals: list[Signal] = []
        for rank, symbol in enumerate(selected, start=1):
            score = scores[symbol]
            reason = _format_reason(symbol, rank, len(scores), score, components[symbol])
            signals.append(
                Signal(
                    symbol=symbol,
                    side=Side.BUY,
                    strength=max(0.1, min(1.0, 1.0 - (rank - 1) / max(top_n, 1))),
                    reason=reason,
                    price_hint=ctx.close(symbol),
                )
            )
        held = [symbol for symbol in ctx.symbols if ctx.quantity(symbol) > 0]
        for symbol in held:
            if symbol in selected_set:
                continue
            signals.append(
                Signal(
                    symbol=symbol,
                    side=Side.SELL,
                    strength=1.0,
                    reason=f"价值质量调仓：综合得分跌出前 {top_n} 名，卖出离场",
                    price_hint=ctx.close(symbol),
                )
            )
        return signals


def _format_reason(
    symbol: str,
    rank: int,
    total: int,
    score: float,
    components: dict[str, float],
) -> str:
    """生成中文得分分解理由（推荐引擎复用同一套因子口径）。"""

    parts = []
    for name, label in (
        ("momentum_120", "120日动量"),
        ("trend_stability_60", "趋势稳定"),
        ("low_volatility_60", "低波动"),
        ("low_drawdown_120", "回撤可控"),
        ("positive_ratio_60", "上涨占比"),
    ):
        value = components.get(name)
        if value is not None and abs(float(value)) >= 0.01:
            parts.append(f"{label} z={float(value):+.2f}")
    detail = "、".join(parts) if parts else "各因子接近截面均值"
    return f"价值质量第 {rank}/{total} 名（综合 z={score:+.2f}）：{detail}"


def _is_suspended(row: pd.Series) -> bool:
    try:
        return bool(row.get("is_suspended", False))
    except (TypeError, ValueError):
        return False


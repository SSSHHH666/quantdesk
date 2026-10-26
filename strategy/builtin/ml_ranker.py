"""ML 打分策略（R4）：用 walk-forward 机器学习概率做横截面选股。

把 :mod:`strategy.ml_ranker` 的样本外模型接成标准策略插件（``@register("ml_ranker")``），
因此既能被 ``BacktestEngine`` 直接回测，也能被推荐引擎（``strategy/recommend.py``）
调用；策略只读 :class:`~strategy.base.Context`，**不使用当日之后的数据**：

* 每个调仓日，用 ``ctx.bars``（截至当日的历史）拼出面板；
* :meth:`MLRanker.rank` 在“标签日期 ≤ 当日”的样本上训练（避免未来数据），
  再对当日的自选池输出概率；
* 买入概率最高的前 ``top_n`` 只（引擎按 ``position_pct`` 换算仓位），
  已持有但跌出前 ``top_n`` 的标的卖出。

性能：面板按标的向量化计算（滚动窗口一次算完），10 标的 × 2 年回测通常数秒内完成。
样本不足（历史太短/标的太少）时记录 INFO 并跳过本次调仓，不影响回测继续。
"""

from __future__ import annotations

import logging
import math
from typing import Any

import pandas as pd

from core.models import Side
from strategy.base import Context, Signal, Strategy, StrategyParamError, register
from strategy.ml_ranker import MLConfig, MLRanker
from strategy.factors import FEATURE_LABELS

logger = logging.getLogger(__name__)

__all__ = ["MLRankerStrategy"]

#: ML 面板所需行情列
_PANEL_COLUMNS = ("date", "symbol", "open", "high", "low", "close", "volume")


@register("ml_ranker")
class MLRankerStrategy(Strategy):
    """ML 打分选股：walk-forward 模型概率前 N 名，周期调仓。"""

    display_name = "ML 打分"
    description = "walk-forward 机器学习（逻辑回归/梯度提升）预测未来 5 日超额，按概率取前 N 名"
    startup_bars = 66

    @classmethod
    def params_schema(cls) -> dict[str, dict[str, object]]:
        return {
            "top_n": {
                "type": "int",
                "default": 5,
                "min": 1,
                "max": 20,
                "step": 1,
                "label": "选股数量",
                "help": "每次调仓按 ML 概率取前 N 只等权持有",
            },
            "rebalance_days": {
                "type": "int",
                "default": 20,
                "min": 5,
                "max": 60,
                "step": 1,
                "label": "调仓周期（交易日）",
                "help": "每隔多少个交易日重新训练/打分并调仓",
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
            "train_window": {
                "type": "int",
                "default": 250,
                "min": 60,
                "max": 500,
                "step": 10,
                "label": "训练窗口（交易日）",
                "help": "每次训练使用最近多少个交易日的样本（更长更稳但更慢）",
            },
            "min_train_rows": {
                "type": "int",
                "default": 120,
                "min": 30,
                "max": 1000,
                "step": 10,
                "label": "最少训练样本",
                "help": "训练样本少于此值时跳过本次调仓（数据不足保护）",
            },
            "model": {
                "type": "select",
                "default": "logistic",
                "choices": ["logistic", "gbdt"],
                "label": "模型类型",
                "help": "logistic=逻辑回归（稳定快）；gbdt=梯度提升（非线性，较慢）",
            },
        }

    @classmethod
    def validate_constraints(cls, params: dict[str, object]) -> None:
        if int(params["top_n"]) < 1:  # type: ignore[arg-type]
            raise StrategyParamError("选股数量（top_n）必须 ≥ 1")
        if float(params["position_pct"]) * int(params["top_n"]) > 200:  # type: ignore[arg-type]
            raise StrategyParamError("单票仓位 × 选股数量过大（建议总仓位 ≤ 100%）")

    def __init__(self, params: dict[str, object] | None = None) -> None:
        super().__init__(params)  # type: ignore[arg-type]
        self._bar_count = 0
        self._last_rebalance_bar = -1
        self._last_importance: pd.DataFrame = pd.DataFrame()

    def on_init(self, ctx: Context) -> None:
        self._bar_count = 0
        self._last_rebalance_bar = -1
        self._last_importance = pd.DataFrame()

    def on_bar(self, ctx: Context) -> list[Signal]:
        self._bar_count += 1
        rebalance_days = max(1, int(self.param("rebalance_days", 20)))
        if self._last_rebalance_bar >= 0 and self._bar_count - self._last_rebalance_bar < rebalance_days:
            return []

        top_n = int(self.param("top_n", 5))
        bars = _context_bars(ctx)
        if bars is None or len(bars) == 0:
            return []
        config = MLConfig(
            model=str(self.param("model", "logistic")),
            train_window=int(self.param("train_window", 250)),
            test_window=20,
            min_train_rows=int(self.param("min_train_rows", 120)),
            label_horizon=5,
        )
        ranker = MLRanker(config)
        try:
            picks = ranker.rank(bars, ctx.trade_date, top_n=top_n)
            self._last_importance = ranker.feature_importance()
        except ValueError as exc:
            logger.info("ML 策略暂不调仓：%s", exc)
            return []
        except Exception as exc:  # noqa: BLE001 - 模型异常不应中断回测
            logger.warning("ML 策略打分异常，跳过本次调仓：%s", exc)
            return []
        if not picks:
            return []
        self._last_rebalance_bar = self._bar_count
        top_features = list(self._last_importance.head(4)["feature"]) if len(self._last_importance) else []

        selected = [str(pick["symbol"]) for pick in picks]
        selected_set = set(selected)
        proba_by_symbol = {str(pick["symbol"]): float(pick["proba"]) for pick in picks}
        signals: list[Signal] = []
        for rank, symbol in enumerate(selected, start=1):
            features = picks[rank - 1].get("features", {})
            feature_text = "、".join(
                f"{FEATURE_LABELS.get(name, name)} {float(features.get(name, 0.0)):+.3f}"
                for name in top_features
            )
            signals.append(
                Signal(
                    symbol=symbol,
                    side=Side.BUY,
                    strength=float(max(0.1, min(1.0, proba_by_symbol.get(symbol, 0.5)))),
                    reason=(
                        f"ML 概率 {proba_by_symbol.get(symbol, 0.0) * 100:.1f}%（第 {rank}/{len(picks)} 名，"
                        f"模型 {config.model}）：{feature_text or '见特征重要性'}"
                    ),
                    price_hint=ctx.close(symbol),
                )
            )
        for symbol in (item for item in ctx.symbols if ctx.quantity(item) > 0):
            if symbol in selected_set:
                continue
            signals.append(
                Signal(
                    symbol=symbol,
                    side=Side.SELL,
                    strength=1.0,
                    reason=f"ML 调仓：概率跌出前 {top_n} 名，卖出离场",
                    price_hint=ctx.close(symbol),
                )
            )
        return signals


def _context_bars(ctx: Context) -> pd.DataFrame | None:
    """把 Context 里的历史拼成 ML 面板所需 DataFrame（只用 ≤ 当日数据）。"""

    frames: list[pd.DataFrame] = []
    for symbol in ctx.symbols:
        history = ctx.history(symbol)
        if history is None or len(history) == 0:
            continue
        missing = [column for column in _PANEL_COLUMNS if column not in history.columns]
        if missing:
            continue
        frame = history.loc[:, list(_PANEL_COLUMNS)].copy()
        frame["symbol"] = str(symbol)
        frames.append(frame)
    if not frames:
        return None
    result = pd.concat(frames, ignore_index=True)
    result["date"] = pd.to_datetime(result["date"], errors="coerce").dt.date
    return result

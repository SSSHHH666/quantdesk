"""短线选股模型 v1（P0：每日 Top5 推荐，目标“买入后 5 日内较大概率 +1%~3%”）。

设计目标（Lead 定义，可验证代理指标）
------------------------------------
* **标签**：未来 5 个交易日收益率是否 ≥ ``+2%``（主标签）；同时训练 ``+1%``/``+3%``
  两个阈值，输出概率分位。
* **评价**：walk-forward（滚动训练、purge 5 日）样本外**命中率**、**扣双边成本+滑点的
  期望收益**、每日 Top5 组合净值与最大回撤、换手成本。
* **判定线**：样本外命中率 > 55%、Top5 组合扣费后 5 日期望 > 0、最大回撤 < 10%；
  任一不达标 → 报告明确写“不可用”。

特征（仅日频免费数据，全部只用 ≤ 当日信息）
--------------------------------------------
量比（当日/20 日均量）、5/20 日涨幅、突破 20 日高点幅度、距 MA20/MA60 偏离、
ATR 波动率、换手率代理（成交额/20 日均额）、成交额截面分位、上下影线与实体占比、
连续上涨天数、MA20 斜率、RSI、20 日波动率、跳空幅度、市场（自选池均值）相对强度。
所有特征在**截面内**做 rank/z-score（≥5 只标的用分位，少量标的用 z-score），
保证不同日期的量纲可比。

防未来函数
----------
1. 特征只用 ``≤ t`` 的数据；2. 标签使用 ``t+5`` 收益，仅训练/评估用；
3. walk-forward 训练集要求 ``label_date < test_start``（purge：标签窗口在测试开始前
   已走完）；4. 测试期只预测、不拟合。

模型与降级
----------
``sklearn`` 逻辑回归（默认）/ 梯度提升；未安装 sklearn 时降级为**规则打分**
（动量+放量+突破+低波动加权），输出 ``modelless=True`` 并在理由中注明“规则打分”。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from strategy.base import atr, rsi
from strategy.factors import FEATURE_LABELS as _FACTOR_LABELS
from strategy.factors import normalize_bars

logger = logging.getLogger(__name__)

__all__ = [
    "SHORT_FEATURE_NAMES",
    "SHORT_FEATURE_LABELS",
    "ShortTermConfig",
    "ShortTermFold",
    "ShortTermReport",
    "ShortTermModel",
    "rule_score",
]

try:  # pragma: no cover - 依赖环境
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    SKLEARN_AVAILABLE = True
except Exception as _sklearn_exc:  # pragma: no cover
    SKLEARN_AVAILABLE = False
    _SKLEARN_IMPORT_ERROR = f"{type(_sklearn_exc).__name__}: {_sklearn_exc}"
else:
    _SKLEARN_IMPORT_ERROR = None


# ---------------------------------------------------------------------------
# 一、特征定义
# ---------------------------------------------------------------------------

#: 模型输入特征（截面 rank/z-score 后）
SHORT_FEATURE_NAMES: tuple[str, ...] = (
    "rel_ret_5",        # 5 日涨幅（截面相对）
    "rel_ret_20",       # 20 日涨幅（截面相对）
    "vol_ratio_rank",   # 量比（当日/20 日均量）
    "breakout_rank",    # 突破 20 日高点幅度
    "dist_ma20_rank",   # 距 MA20 偏离
    "dist_ma60_rank",   # 距 MA60 偏离
    "atr_pct_rank",     # ATR(14)/收盘价（波动）
    "turnover_rank",    # 换手率代理：成交额/20 日均额
    "amount_rank",      # 成交额截面分位（流动性）
    "upper_shadow_rank",  # 上影线占比
    "lower_shadow_rank",  # 下影线占比
    "body_rank",        # 实体占比
    "consec_up_rank",   # 连续上涨天数
    "ma20_slope_rank",  # MA20 斜率（5 日）
    "rsi_rank",         # RSI(14)
    "volatility_rank",  # 20 日波动率
    "gap_open_rank",    # 跳空幅度
    "market_rel_rank",  # 相对市场（自选池均值）5 日强度
)

SHORT_FEATURE_LABELS: dict[str, str] = {
    "rel_ret_5": "5日相对涨幅",
    "rel_ret_20": "20日相对涨幅",
    "vol_ratio_rank": "量比",
    "breakout_rank": "突破20日高点",
    "dist_ma20_rank": "距MA20",
    "dist_ma60_rank": "距MA60",
    "atr_pct_rank": "ATR波动",
    "turnover_rank": "换手率代理",
    "amount_rank": "成交额分位",
    "upper_shadow_rank": "上影线",
    "lower_shadow_rank": "下影线",
    "body_rank": "实体占比",
    "consec_up_rank": "连续上涨天数",
    "ma20_slope_rank": "MA20斜率",
    "rsi_rank": "RSI(14)",
    "volatility_rank": "20日波动率",
    "gap_open_rank": "跳空幅度",
    "market_rel_rank": "相对市场强度",
}

#: 生成中文理由时使用的原始（未截面化）特征列
_RAW_COLUMNS: tuple[str, ...] = (
    "raw_volume_ratio",
    "raw_breakout_20",
    "raw_ret_5",
    "raw_ret_20",
    "raw_rsi",
    "raw_atr_pct",
    "raw_turnover_ratio",
    "raw_consec_up",
    "raw_amount",
)


@dataclass(frozen=True)
class ShortTermConfig:
    """短线模型配置（默认面向日频、约 2~4 年数据）。"""

    label_horizon: int = 5
    targets: tuple[float, ...] = (0.01, 0.02, 0.03)
    train_window: int = 250
    test_window: int = 20
    min_train_rows: int = 300
    min_history: int = 60
    model: str = "logistic"  # logistic | gbdt
    random_state: int = 42
    top_n: int = 5
    round_trip_cost: float = 0.0023  # 双边费用+滑点≈23bp（佣金5bp*2+印花税5bp+过户/规费+滑点10bp）

    def __post_init__(self) -> None:
        if self.model not in ("logistic", "gbdt"):
            raise ValueError(f"model 仅支持 logistic/gbdt，收到 {self.model!r}")
        if self.label_horizon < 1 or self.train_window < 60 or self.test_window < 5:
            raise ValueError("短线模型参数非法：label_horizon≥1、train_window≥60、test_window≥5")
        if not self.targets:
            raise ValueError("targets 不能为空")


@dataclass
class ShortTermFold:
    """单折 walk-forward 样本外结果。"""

    fold: int
    train_start: Any
    train_end: Any
    test_start: Any
    test_end: Any
    n_train: int
    n_test: int
    hit_rate_target: float | None  # Top-N 中未来收益 ≥ 主标签阈值（2%）比例
    hit_rate_positive: float | None
    mean_return_top_n: float | None
    net_expectancy: float | None  # 扣 round_trip_cost 后
    base_rate: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "fold": self.fold,
            "train_start": str(self.train_start),
            "train_end": str(self.train_end),
            "test_start": str(self.test_start),
            "test_end": str(self.test_end),
            "n_train": self.n_train,
            "n_test": self.n_test,
            "hit_rate_target": self.hit_rate_target,
            "hit_rate_positive": self.hit_rate_positive,
            "mean_return_top_n": self.mean_return_top_n,
            "net_expectancy": self.net_expectancy,
            "base_rate": self.base_rate,
        }


@dataclass
class ShortTermReport:
    """walk-forward 报告（样本外指标 + 判定结论 + 特征重要性 + 预测明细）。"""

    backend: str
    config: ShortTermConfig
    folds: list[ShortTermFold] = field(default_factory=list)
    oos_predictions: pd.DataFrame = field(default_factory=pd.DataFrame)
    metrics: dict[str, Any] = field(default_factory=dict)
    feature_importance: pd.DataFrame = field(default_factory=pd.DataFrame)
    conclusion: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "config": {
                "label_horizon": self.config.label_horizon,
                "targets": list(self.config.targets),
                "train_window": self.config.train_window,
                "test_window": self.config.test_window,
                "model": self.config.model,
                "round_trip_cost": self.config.round_trip_cost,
            },
            "folds": len(self.folds),
            "metrics": dict(self.metrics),
            "conclusion": dict(self.conclusion),
            "top_features": self.feature_importance.head(8).to_dict(orient="records")
            if len(self.feature_importance)
            else [],
            "notes": list(self.notes),
        }

    def folds_frame(self) -> pd.DataFrame:
        return pd.DataFrame([fold.as_dict() for fold in self.folds])


# ---------------------------------------------------------------------------
# 二、模型
# ---------------------------------------------------------------------------


class ShortTermModel:
    """短线 Top-N 推荐模型（walk-forward 训练 + 指定日期打分）。"""

    def __init__(self, config: ShortTermConfig | None = None) -> None:
        self.config = config or ShortTermConfig()
        self.backend = f"sklearn-{self.config.model}" if SKLEARN_AVAILABLE else "rule-fallback"

    # -- 特征面板 ---------------------------------------------------------
    def build_panel(self, bars: pd.DataFrame) -> pd.DataFrame:
        """构建“日期 × 标的”特征面板（含未来 1/2/3% 标签，仅训练/评估用）。"""

        frame = normalize_bars(bars)
        config = self.config
        horizon = config.label_horizon
        panels: list[pd.DataFrame] = []
        for symbol, group in frame.groupby("symbol", sort=True):
            group = group.sort_values("date").reset_index(drop=True)
            if len(group) < config.min_history + horizon + 1:
                continue
            panel = _features_for_symbol(group, symbol)
            if panel is None:
                continue
            close = pd.to_numeric(group["close"], errors="coerce").astype("float64")
            future = close.shift(-horizon)
            panel["future_return"] = future / close - 1.0
            panel["label_date"] = group["date"].shift(-horizon)
            panel["date"] = group["date"]
            panel["symbol"] = symbol
            panels.append(panel)
        if not panels:
            raise ValueError(
                "短线特征面板为空：行情历史不足（每只标的至少需要 "
                f"{config.min_history + horizon + 1} 根 bar）或标的数量不足"
            )
        panel = pd.concat(panels, ignore_index=True)
        # 先做截面化（需要 date 列），再剔除特征不齐的预热期样本
        panel = _cross_sectional_normalize(panel)
        usable = panel[list(SHORT_FEATURE_NAMES)].notna().all(axis=1)
        panel = panel[usable].reset_index(drop=True)
        if panel.empty:
            raise ValueError("短线特征面板没有特征齐全的样本：请扩大行情区间")
        for threshold in config.targets:
            column = f"label_{int(round(threshold * 100))}pct"
            panel[column] = np.where(
                panel["future_return"].notna(), (panel["future_return"] >= threshold).astype(float), np.nan
            )
        return panel

    # -- 模型工厂 ---------------------------------------------------------
    def _build_model(self) -> Any:
        if not SKLEARN_AVAILABLE:
            return _RuleModel()
        if self.config.model == "gbdt":
            return HistGradientBoostingClassifier(
                max_iter=150, learning_rate=0.05, max_depth=3,
                l2_regularization=1.0, random_state=self.config.random_state,
            )
        return Pipeline(
            steps=[
                ("scaler", StandardScaler()),
                (
                    "clf",
                    LogisticRegression(
                        max_iter=500,
                        random_state=self.config.random_state,
                        class_weight="balanced",
                        C=1.0,
                    ),
                ),
            ]
        )

    # -- walk-forward -----------------------------------------------------
    def walk_forward(self, bars: pd.DataFrame) -> ShortTermReport:
        """滚动训练 + 样本外评估（purge 5 日，严格时间顺序）。"""

        panel = self.build_panel(bars)
        config = self.config
        features = list(SHORT_FEATURE_NAMES)
        label_columns = [f"label_{int(round(t * 100))}pct" for t in config.targets]
        main_label = f"label_{int(round(0.02 * 100))}pct"
        labeled = panel[panel[main_label].notna() & panel["label_date"].notna()].copy()
        labeled["label_date"] = pd.to_datetime(labeled["label_date"], errors="coerce").dt.date
        labeled = labeled[labeled["label_date"].notna()].reset_index(drop=True)
        if labeled.empty:
            raise ValueError("短线面板没有可用标签：请提供更长行情（建议 ≥ 2 年）")
        evaluation_dates = sorted(labeled["date"].unique())
        if len(evaluation_dates) < config.train_window + config.test_window:
            raise ValueError(
                f"行情长度不足：样本外评估至少需要 {config.train_window + config.test_window} "
                f"个交易日，当前仅 {len(evaluation_dates)} 个"
            )
        all_dates = sorted(panel["date"].unique())
        date_order = {day: index for index, day in enumerate(all_dates)}
        notes: list[str] = []
        if not SKLEARN_AVAILABLE:
            notes.append(
                f"scikit-learn 不可用（{_SKLEARN_IMPORT_ERROR}），已降级为规则打分（不可作概率使用）"
            )

        folds: list[ShortTermFold] = []
        prediction_rows: list[pd.DataFrame] = []
        importance_rows: list[list[float]] = []
        fold_index = 0
        start = config.train_window
        while start < len(evaluation_dates):
            test_dates = evaluation_dates[start : start + config.test_window]
            if not test_dates:
                break
            test_start, test_end = test_dates[0], test_dates[-1]
            test_order = date_order.get(test_start, 0)
            train_start_date = all_dates[max(0, test_order - config.train_window)]
            # purge：训练标签必须在测试开始前走完（label_date < test_start）
            train_mask = (
                labeled["label_date"].notna()
                & (labeled["label_date"] < test_start)
                & (labeled["date"] >= train_start_date)
            )
            train_part = labeled[train_mask]
            test_part = panel[panel["date"].isin(test_dates)]
            if len(train_part) < config.min_train_rows:
                notes.append(
                    f"第 {fold_index + 1} 折跳过：训练样本 {len(train_part)} < {config.min_train_rows}"
                )
                fold_index += 1
                start += config.test_window
                continue
            X_train = train_part[features].to_numpy(dtype="float64")
            X_test = test_part[features].to_numpy(dtype="float64")
            models: dict[str, Any] = {}
            for threshold, label_column in zip(config.targets, label_columns):
                y_train = train_part[label_column].to_numpy(dtype="float64")
                if len(np.unique(y_train)) < 2:
                    continue
                model = self._build_model()
                model.fit(X_train, y_train)
                models[label_column] = model
            if main_label not in models:
                notes.append(f"第 {fold_index + 1} 折跳过：主标签只有单一类别")
                fold_index += 1
                start += config.test_window
                continue

            test_frame = test_part.copy()
            for label_column, model in models.items():
                test_frame[f"prob_{label_column.replace('label_', '')}"] = model.predict_proba(X_test)[:, 1]
            test_frame["fold"] = fold_index + 1
            prediction_rows.append(test_frame)
            importance_rows.append(
                np.abs(np.asarray(_model_importance(models[main_label]), dtype="float64"))
            )

            # 样本外 Top-N 指标
            top_n = max(1, int(config.top_n))
            picked = (
                test_frame.groupby("date", group_keys=False)
                .apply(lambda group: group.nlargest(top_n, "prob_2pct"))
                .reset_index(drop=True)
            )
            top_returns = pd.to_numeric(picked["future_return"], errors="coerce").dropna()
            base_rate = float((labeled[labeled["date"].isin(test_dates)]["future_return"] >= 0.02).mean())
            hit_rate_target = (
                float((top_returns >= 0.02).mean()) if len(top_returns) else None
            )
            hit_rate_positive = float((top_returns > 0).mean()) if len(top_returns) else None
            mean_return = float(top_returns.mean()) if len(top_returns) else None
            net_expectancy = (
                mean_return - config.round_trip_cost if mean_return is not None else None
            )
            folds.append(
                ShortTermFold(
                    fold=fold_index + 1,
                    train_start=str(train_part["date"].min()),
                    train_end=str(train_part["date"].max()),
                    test_start=str(test_start),
                    test_end=str(test_end),
                    n_train=int(len(train_part)),
                    n_test=int(len(test_part)),
                    hit_rate_target=_safe_metric(hit_rate_target),
                    hit_rate_positive=_safe_metric(hit_rate_positive),
                    mean_return_top_n=_safe_metric(mean_return),
                    net_expectancy=_safe_metric(net_expectancy),
                    base_rate=_safe_metric(base_rate),
                )
            )
            fold_index += 1
            start += config.test_window

        if not folds:
            raise ValueError("短线 walk-forward 未产生有效折：请提供更长行情或降低 min_train_rows")

        predictions = pd.concat(prediction_rows, ignore_index=True)
        metrics = self._aggregate_metrics(predictions)
        importance = pd.DataFrame(
            {
                "feature": features,
                "label": [SHORT_FEATURE_LABELS.get(name, name) for name in features],
                "importance": np.mean(np.vstack(importance_rows), axis=0),
            }
        )
        importance["importance"] = importance["importance"] / max(
            float(importance["importance"].sum()), 1e-12
        )
        importance = importance.sort_values("importance", ascending=False).reset_index(drop=True)
        conclusion = _conclude(metrics)
        notes.append(conclusion["summary"])
        return ShortTermReport(
            backend=self.backend,
            config=config,
            folds=folds,
            oos_predictions=predictions,
            metrics=metrics,
            feature_importance=importance,
            conclusion=conclusion,
            notes=notes,
        )

    def _aggregate_metrics(self, predictions: pd.DataFrame) -> dict[str, Any]:
        """汇总样本外指标（Top-N 命中率/期望/分位概率校准）。"""

        config = self.config
        top_n = max(1, int(config.top_n))
        valid = predictions["future_return"].notna()
        picked = (
            predictions[valid]
            .groupby("date", group_keys=False)
            .apply(lambda group: group.nlargest(top_n, "prob_2pct"))
            .reset_index(drop=True)
        )
        returns = pd.to_numeric(picked["future_return"], errors="coerce").dropna()
        base = pd.to_numeric(predictions.loc[valid, "future_return"], errors="coerce").dropna()
        hit_2 = float((returns >= 0.02).mean()) if len(returns) else None
        hit_1 = float((returns >= 0.01).mean()) if len(returns) else None
        hit_3 = float((returns >= 0.03).mean()) if len(returns) else None
        positive = float((returns > 0).mean()) if len(returns) else None
        mean_return = float(returns.mean()) if len(returns) else None
        median_return = float(returns.median()) if len(returns) else None
        net_expectancy = mean_return - config.round_trip_cost if mean_return is not None else None
        metrics: dict[str, Any] = {
            "backend": self.backend,
            "model": config.model,
            "folds": int(predictions["fold"].nunique()),
            "oos_samples": int(valid.sum()),
            "oos_dates": int(predictions.loc[valid, "date"].nunique()),
            "top_n": top_n,
            "hit_rate_2pct": _safe_metric(hit_2),
            "hit_rate_1pct": _safe_metric(hit_1),
            "hit_rate_3pct": _safe_metric(hit_3),
            "hit_rate_positive": _safe_metric(positive),
            "base_rate_2pct": _safe_metric(float((base >= 0.02).mean())) if len(base) else None,
            "mean_return_top_n": _safe_metric(mean_return),
            "median_return_top_n": _safe_metric(median_return),
            "net_expectancy": _safe_metric(net_expectancy),
            "round_trip_cost": config.round_trip_cost,
            "mean_return_all": _safe_metric(float(base.mean())) if len(base) else None,
        }
        return metrics

    # -- 指定日期打分 -----------------------------------------------------
    def rank_candidates(
        self,
        bars: pd.DataFrame,
        as_of: Any,
        top_n: int = 10,
    ) -> list[dict[str, Any]]:
        """对 ``as_of`` 当日候选池打分（训练只用 ``label_date < as_of`` 的样本，purge 5 日）。

        Returns:
            ``[{symbol, prob_2pct, prob_1pct, prob_3pct, score, reasons, risk_notes,
            factors, backend, fallback}]``，按 ``prob_2pct`` 降序。
        """

        config = self.config
        frame = normalize_bars(bars)
        as_of_date = pd.Timestamp(as_of).date()
        frame = frame.copy()
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
        frame = frame[frame["date"] <= as_of_date].reset_index(drop=True)
        if frame.empty:
            raise ValueError(f"截至 {as_of_date} 没有行情数据")
        # 用完整面板训练（标签需已走完），再用全量 panel 的 as_of 行预测
        panel = self.build_panel(frame)
        features = list(SHORT_FEATURE_NAMES)
        main_label = "label_2pct"
        labeled = panel[panel[main_label].notna() & panel["label_date"].notna()].copy()
        labeled["label_date"] = pd.to_datetime(labeled["label_date"], errors="coerce").dt.date
        labeled = labeled[labeled["label_date"].notna() & (labeled["label_date"] <= as_of_date)]
        today = panel[panel["date"] == as_of_date]
        if today.empty:
            raise ValueError(f"日期 {as_of_date} 没有特征齐全的行情（非交易日或历史不足）")

        models: dict[str, Any] = {}
        fallback = False
        if SKLEARN_AVAILABLE and len(labeled) >= config.min_train_rows:
            X_train = labeled[features].to_numpy(dtype="float64")
            for threshold in config.targets:
                label_column = f"label_{int(round(threshold * 100))}pct"
                y_train = labeled[label_column].to_numpy(dtype="float64")
                if len(np.unique(y_train)) < 2:
                    continue
                model = self._build_model()
                model.fit(X_train, y_train)
                models[label_column] = model
        if "label_2pct" not in models:
            fallback = True
            scores = [_rule_score(row) for _, row in today.iterrows()]
            today = today.copy()
            today["prob_2pct"] = scores
            today["prob_1pct"] = np.clip(np.asarray(scores) + 0.05, 0, 1)
            today["prob_3pct"] = np.clip(np.asarray(scores) - 0.05, 0, 1)
        else:
            X_today = today[features].to_numpy(dtype="float64")
            today = today.copy()
            for label_column, model in models.items():
                column = f"prob_{label_column.replace('label_', '')}"
                today[column] = model.predict_proba(X_today)[:, 1]

        results: list[dict[str, Any]] = []
        for _, row in today.iterrows():
            prob = float(row.get("prob_2pct", 0.0) or 0.0)
            results.append(
                {
                    "symbol": str(row["symbol"]),
                    "prob_2pct": prob,
                    "prob_1pct": float(row.get("prob_1pct", 0.0) or 0.0),
                    "prob_3pct": float(row.get("prob_3pct", 0.0) or 0.0),
                    "score": prob,
                    "reasons": _reasons(row),
                    "risk_notes": _risk_notes(row, prob, fallback),
                    "factors": {name: float(row.get(name, 0.0) or 0.0) for name in features},
                    "backend": self.backend,
                    "fallback": fallback,
                }
            )
        results.sort(key=lambda item: (-item["score"], item["symbol"]))
        return results[: max(1, int(top_n))]


# ---------------------------------------------------------------------------
# 三、特征工程（逐标的向量化，避免 O(n²) 重算）
# ---------------------------------------------------------------------------


def _features_for_symbol(group: pd.DataFrame, symbol: str) -> pd.DataFrame | None:
    """单个标的的原始特征（未截面化）。"""

    close = pd.to_numeric(group["close"], errors="coerce").astype("float64")
    high = pd.to_numeric(group["high"], errors="coerce").astype("float64")
    low = pd.to_numeric(group["low"], errors="coerce").astype("float64")
    open_price = pd.to_numeric(group["open"], errors="coerce").astype("float64")
    volume = pd.to_numeric(group["volume"], errors="coerce").astype("float64")
    amount = pd.to_numeric(group["amount"], errors="coerce").astype("float64")
    if "amount" not in group.columns or amount.fillna(0).sum() <= 0:
        amount = close * volume  # 数据层无成交额时用 收盘×成交量 近似
    returns = close.pct_change()
    frame = pd.DataFrame(index=group.index)
    frame["raw_ret_5"] = close.pct_change(5)
    frame["raw_ret_20"] = close.pct_change(20)
    volume_mean_20 = volume.rolling(20, min_periods=20).mean()
    frame["raw_volume_ratio"] = volume / volume_mean_20
    prior_high = high.shift(1).rolling(20, min_periods=20).max()
    frame["raw_breakout_20"] = close / prior_high - 1.0
    frame["raw_dist_ma20"] = close / close.rolling(20, min_periods=20).mean() - 1.0
    frame["raw_dist_ma60"] = close / close.rolling(60, min_periods=60).mean() - 1.0
    frame["raw_atr_pct"] = atr(high, low, close, 14) / close
    frame["raw_turnover_ratio"] = amount / amount.rolling(20, min_periods=20).mean()
    frame["raw_amount"] = amount
    range_ = (high - low).replace(0, np.nan)
    frame["raw_upper_shadow"] = (high - np.maximum(open_price, close)) / range_
    frame["raw_lower_shadow"] = (np.minimum(open_price, close) - low) / range_
    frame["raw_body"] = (close - open_price).abs() / range_
    up = (returns > 0).astype("float64")
    blocks = (up != up.shift()).cumsum()
    frame["raw_consec_up"] = up.groupby(blocks).cumsum().fillna(0.0)
    ma20 = close.rolling(20, min_periods=20).mean()
    frame["raw_ma20_slope"] = ma20 / ma20.shift(5) - 1.0
    frame["raw_rsi"] = rsi(close, 14)
    frame["raw_volatility"] = returns.rolling(20, min_periods=20).std(ddof=0)
    frame["raw_gap_open"] = open_price / close.shift(1) - 1.0
    frame["raw_market_rel"] = frame["raw_ret_5"]  # 截面后再减市场均值
    frame["raw_close"] = close
    frame["symbol"] = symbol
    return frame


def _cross_sectional_normalize(panel: pd.DataFrame) -> pd.DataFrame:
    """把原始特征转成截面可比特征（≥5 只标的用分位，少量标的用 z-score）。"""

    frame = panel.copy()
    mapping = {
        "rel_ret_5": "raw_ret_5",
        "rel_ret_20": "raw_ret_20",
        "vol_ratio_rank": "raw_volume_ratio",
        "breakout_rank": "raw_breakout_20",
        "dist_ma20_rank": "raw_dist_ma20",
        "dist_ma60_rank": "raw_dist_ma60",
        "atr_pct_rank": "raw_atr_pct",
        "turnover_rank": "raw_turnover_ratio",
        "amount_rank": "raw_amount",
        "upper_shadow_rank": "raw_upper_shadow",
        "lower_shadow_rank": "raw_lower_shadow",
        "body_rank": "raw_body",
        "consec_up_rank": "raw_consec_up",
        "ma20_slope_rank": "raw_ma20_slope",
        "rsi_rank": "raw_rsi",
        "volatility_rank": "raw_volatility",
        "gap_open_rank": "raw_gap_open",
        "market_rel_rank": "raw_market_rel",
    }
    for target, source in mapping.items():
        frame[target] = _per_date_normalize(frame, source)
    return frame


def _per_date_normalize(frame: pd.DataFrame, column: str) -> pd.Series:
    """按日期做截面 rank 分位（≥5 只）或 z-score（少量标的），缺失填 0.5/0。"""

    values = pd.to_numeric(frame[column], errors="coerce")

    def _transform(group: pd.Series) -> pd.Series:
        valid = group.dropna()
        if len(valid) < 2:
            return pd.Series([0.5 if len(valid) <= 1 else np.nan] * len(group), index=group.index)
        if len(valid) >= 5:
            return group.rank(pct=True)
        std = float(valid.std(ddof=0))
        if not math.isfinite(std) or std <= 0:
            return pd.Series([0.5] * len(group), index=group.index)
        mean = float(valid.mean())
        return 0.5 + (group - mean) / std * 0.15  # 压缩到 ~[0,1]，避免极端值

    return values.groupby(frame["date"], group_keys=False).apply(_transform).clip(0.0, 1.0)


# ---------------------------------------------------------------------------
# 四、规则兜底与中文理由
# ---------------------------------------------------------------------------


def rule_score(row: Mapping[str, Any]) -> float:
    """规则打分（sklearn 缺失/样本不足时兜底），输出 0~1 的研究用分数。"""

    def value(name: str, default: float = 0.0) -> float:
        raw = row.get(name, default)
        try:
            number = float(raw)
        except (TypeError, ValueError):
            return default
        return number if math.isfinite(number) else default

    score = (
        0.30 * value("vol_ratio_rank", 0.5)
        + 0.25 * value("breakout_rank", 0.5)
        + 0.20 * value("rel_ret_5", 0.5)
        + 0.10 * value("ma20_slope_rank", 0.5)
        + 0.10 * (1.0 - abs(value("rsi_rank", 0.5) - 0.6))
        + 0.05 * value("lower_shadow_rank", 0.5)
    )
    return float(min(max(score, 0.0), 1.0))


def _rule_score(row: Mapping[str, Any]) -> float:
    return rule_score(row)


def _reasons(row: Mapping[str, Any]) -> list[str]:
    """基于原始特征生成中文理由（供 UI 展示“为什么推荐”）。"""

    reasons: list[str] = []
    volume_ratio = _num(row, "raw_volume_ratio")
    if volume_ratio is not None and volume_ratio >= 1.2:
        reasons.append(f"量比 {volume_ratio:.2f}（当日/20日均量，放量）")
    breakout = _num(row, "raw_breakout_20")
    if breakout is not None and breakout > 0:
        reasons.append(f"突破 20 日高点 {breakout * 100:+.2f}%")
    ret5 = _num(row, "raw_ret_5")
    if ret5 is not None:
        reasons.append(f"5 日涨幅 {ret5 * 100:+.2f}%")
    rsi_value = _num(row, "raw_rsi")
    if rsi_value is not None:
        reasons.append(f"RSI {rsi_value:.1f}")
    atr_pct = _num(row, "raw_atr_pct")
    if atr_pct is not None:
        reasons.append(f"ATR 波动 {atr_pct * 100:.2f}%")
    consec = _num(row, "raw_consec_up")
    if consec is not None and consec >= 2:
        reasons.append(f"连续上涨 {int(consec)} 天")
    turnover = _num(row, "raw_turnover_ratio")
    if turnover is not None and turnover >= 1.3:
        reasons.append(f"换手率代理 {turnover:.2f}（成交额/20日均额）")
    return reasons or ["特征接近截面均值"]


def _risk_notes(row: Mapping[str, Any], prob: float, fallback: bool) -> list[str]:
    notes: list[str] = ["持有周期 5 日，止盈 +3%、止损 -2%（见 backtest_short）"]
    if fallback:
        notes.append("模型样本不足/依赖缺失，当前为规则打分（不可当概率使用）")
    if prob < 0.45:
        notes.append("模型概率偏低，建议观望")
    rsi_value = _num(row, "raw_rsi")
    if rsi_value is not None and rsi_value > 80:
        notes.append("RSI 超买，追高风险大")
    atr_pct = _num(row, "raw_atr_pct")
    if atr_pct is not None and atr_pct > 0.05:
        notes.append("波动率偏高，建议减半仓位")
    consec = _num(row, "raw_consec_up")
    if consec is not None and consec >= 5:
        notes.append("连续上涨天数较多，回撤风险")
    notes.append("模型为研究基线（v1），未达“可用”标准前不得用于实盘")
    return notes


def _num(row: Mapping[str, Any], name: str) -> float | None:
    try:
        value = float(row.get(name))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _model_importance(model: Any) -> np.ndarray:
    if isinstance(model, _RuleModel):
        return np.ones(len(SHORT_FEATURE_NAMES))
    if hasattr(model, "feature_importances_"):
        return np.asarray(model.feature_importances_, dtype="float64")
    if hasattr(model, "coef_"):
        return np.abs(np.asarray(model.coef_, dtype="float64")).ravel()
    try:
        estimator = model.named_steps.get("clf") if hasattr(model, "named_steps") else None
        if estimator is not None and hasattr(estimator, "coef_"):
            return np.abs(np.asarray(estimator.coef_, dtype="float64")).ravel()
    except Exception:  # pragma: no cover
        pass
    return np.zeros(len(SHORT_FEATURE_NAMES), dtype="float64")


class _RuleModel:
    """规则打分模型（仅 sklearn 缺失时使用，``predict_proba`` 接口对齐 sklearn）。"""

    def fit(self, X: np.ndarray, y: np.ndarray) -> "_RuleModel":
        self._weights = np.ones(X.shape[1], dtype="float64")
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        # 特征已截面化到 0~1，取均值作为“综合强度”映射到 0~1 概率
        scores = np.clip(np.mean(X, axis=1), 0.0, 1.0)
        return np.column_stack([1.0 - scores, scores])

    @property
    def feature_importances_(self) -> np.ndarray:
        return np.ones(len(SHORT_FEATURE_NAMES), dtype="float64")


def _safe_metric(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, 6)


def _conclude(metrics: Mapping[str, Any]) -> dict[str, Any]:
    """按 Lead 判定线给出**如实结论**（不美化）。"""

    hit = metrics.get("hit_rate_2pct")
    expectancy = metrics.get("net_expectancy")
    criteria = {
        "hit_rate_2pct_gt_55pct": bool(hit is not None and float(hit) > 0.55),
        "net_expectancy_positive": bool(expectancy is not None and float(expectancy) > 0),
        "max_drawdown_lt_10pct": None,  # 由 backtest_short 结果回填
    }
    usable = (
        criteria["hit_rate_2pct_gt_55pct"]
        and criteria["net_expectancy_positive"]
        and criteria["max_drawdown_lt_10pct"] is not False
    )
    if usable:
        summary = (
            f"样本外命中率 {hit:.2%}（>55%）、扣费期望 {expectancy:.2%}（>0），"
            "初步达到判定线，仍需回测最大回撤 <10% 才可标记“可用”"
        )
    else:
        parts = []
        if hit is None or float(hit) <= 0.55:
            parts.append(f"样本外命中率 {('—' if hit is None else f'{float(hit):.2%}')} ≤ 55%")
        if expectancy is None or float(expectancy) <= 0:
            parts.append(f"扣费期望 {('—' if expectancy is None else f'{float(expectancy):.2%}')} ≤ 0")
        summary = "不可用（研究基线）：" + "；".join(parts)
    return {
        "usable": bool(usable),
        "criteria": criteria,
        "summary": summary,
        "next_steps": [
            "换标签：未来 5 日收益分位（如 Top20%）或绝对 ±2% 三分类，减少类别不平衡",
            "加特征：龙虎榜/资金流（若数据可得）、行业相对强度、分钟级量价（后续版本）",
            "换周期：1 日或 3 日窗口，或改用排序学习（LambdaRank）",
            "严格化样本：剔除 ST/新股/停牌，按流动性过滤、按板块分层评估",
        ],
    }

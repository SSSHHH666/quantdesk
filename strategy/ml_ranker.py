"""ML 打分模块（R4）：walk-forward 分类器，预测“未来 5 日是否跑赢截面中位数”。

定位
----
给推荐引擎/策略提供**样本外可信**的横截面打分，用因子特征（动量/波动/量比/RSI/
回撤/突破等，见 :data:`strategy.factors.ML_FEATURE_NAMES`）训练二分类器：

* 标签：``label = 未来 5 个交易日收益率 > 当日截面中位数``（1/0）；
* 模型：``sklearn`` 逻辑回归（默认，带 StandardScaler）或梯度提升
  （``HistGradientBoostingClassifier``）；**CPU、无深度学习框架**；
  ``sklearn`` 未安装时自动退化为 numpy 逻辑回归（接口不变，报告里标注 backend）。

严格防未来数据
--------------
1. 特征在日期 ``t`` 只用 ``≤ t`` 的行情计算（固定 ``lookback`` 窗口，见 MLConfig）；
2. 标签使用 ``t + label_horizon`` 的收盘价，**只在训练时使用**；
3. walk-forward 每个 fold 的训练集要求 ``label_date < test_start``——即标签在测试期
   开始前已经“走完”，避免标签跨入测试窗口造成泄漏；
4. 测试期只做预测与评估，不参与任何拟合；全程按时间顺序滚动，无随机切分。

输出
----
:meth:`MLRanker.walk_forward` 返回 :class:`MLRankerReport`：每个 fold 的样本外
AUC/准确率/胜率/平均收益、汇总 OOS 预测明细、特征重要性；:meth:`MLRanker.rank`
用于推荐引擎对“指定日期”的自选池打分。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from strategy.base import atr, rsi
from strategy.factors import (
    FEATURE_LABELS,
    ML_FEATURE_NAMES,
    normalize_bars,
)

logger = logging.getLogger(__name__)

__all__ = [
    "SKLEARN_AVAILABLE",
    "ML_CONFIG_DEFAULT",
    "MLConfig",
    "MLFold",
    "MLRankerReport",
    "MLRanker",
]

#: sklearn 是否可用（缺失时用 numpy 逻辑回归兜底，并写入报告 backend/notes）
try:  # pragma: no cover - 依赖环境
    import sklearn  # noqa: F401
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    SKLEARN_AVAILABLE = True
except Exception as _sklearn_exc:  # pragma: no cover - 依赖缺失
    SKLEARN_AVAILABLE = False
    _SKLEARN_IMPORT_ERROR = f"{type(_sklearn_exc).__name__}: {_sklearn_exc}"
    logger.info("scikit-learn 不可用（%s），ML 打分使用 numpy 逻辑回归兜底", _SKLEARN_IMPORT_ERROR)
else:
    _SKLEARN_IMPORT_ERROR = None


# ---------------------------------------------------------------------------
# 配置与结果结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MLConfig:
    """walk-forward 配置（默认值面向 A 股日频、约 3~5 年数据）。"""

    label_horizon: int = 5  # 未来 N 日收益（分类标签）
    train_window: int = 250  # 训练窗口（交易日）
    test_window: int = 20  # 样本外测试窗口（交易日）
    min_train_rows: int = 200  # 单 fold 最少训练样本
    lookback: int = 120  # 特征计算回看窗口（≥ 因子最长窗口，避免全历史重算）
    min_history: int = 62  # 单标的出特征前最少历史 bar
    model: str = "logistic"  # logistic | gbdt
    random_state: int = 42
    feature_names: tuple[str, ...] = ML_FEATURE_NAMES

    def __post_init__(self) -> None:
        if self.label_horizon < 1:
            raise ValueError("label_horizon 必须 ≥ 1")
        if self.train_window < 30:
            raise ValueError("train_window 必须 ≥ 30 个交易日")
        if self.test_window < 5:
            raise ValueError("test_window 必须 ≥ 5 个交易日")
        if self.lookback < self.min_history:
            raise ValueError("lookback 必须 ≥ min_history")
        if self.model not in ("logistic", "gbdt"):
            raise ValueError(f"model 仅支持 logistic/gbdt，收到 {self.model!r}")


ML_CONFIG_DEFAULT = MLConfig()


@dataclass
class MLFold:
    """单折 walk-forward 结果（全部为**样本外**指标）。"""

    fold: int
    train_start: Any
    train_end: Any
    test_start: Any
    test_end: Any
    n_train: int
    n_test: int
    auc: float | None
    accuracy: float | None
    win_rate: float | None
    mean_return: float | None
    positive_rate: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "fold": self.fold,
            "train_start": str(self.train_start),
            "train_end": str(self.train_end),
            "test_start": str(self.test_start),
            "test_end": str(self.test_end),
            "n_train": self.n_train,
            "n_test": self.n_test,
            "auc": self.auc,
            "accuracy": self.accuracy,
            "win_rate": self.win_rate,
            "mean_return": self.mean_return,
            "positive_rate": self.positive_rate,
        }


@dataclass
class MLRankerReport:
    """walk-forward 报告（样本外指标 + 预测明细 + 特征重要性 + 结论）。"""

    backend: str
    config: MLConfig
    folds: list[MLFold] = field(default_factory=list)
    oos_predictions: pd.DataFrame = field(default_factory=pd.DataFrame)
    oos_metrics: dict[str, Any] = field(default_factory=dict)
    feature_importance: pd.DataFrame = field(default_factory=pd.DataFrame)
    notes: list[str] = field(default_factory=list)
    #: 结论与后续动作（显著性与负结果基线；含 M6 改进项）
    conclusion: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        """精简摘要（简报/UI/日志用，不含预测明细大表）。"""

        return {
            "backend": self.backend,
            "config": {
                "label_horizon": self.config.label_horizon,
                "train_window": self.config.train_window,
                "test_window": self.config.test_window,
                "model": self.config.model,
            },
            "folds": [fold.as_dict() for fold in self.folds],
            "oos_metrics": dict(self.oos_metrics),
            "top_features": self.feature_importance.head(8).to_dict(orient="records")
            if len(self.feature_importance)
            else [],
            "notes": list(self.notes),
            "conclusion": dict(self.conclusion),
        }

    def folds_frame(self) -> pd.DataFrame:
        """各折指标表（UI 展示）。"""

        return pd.DataFrame([fold.as_dict() for fold in self.folds])


# ---------------------------------------------------------------------------
# numpy 逻辑回归兜底（sklearn 缺失时使用，接口与 sklearn 对齐）
# ---------------------------------------------------------------------------


class _NumpyLogisticRegression:
    """极简逻辑回归（标准化 + 全批量梯度下降 + L2），仅作 sklearn 缺失时兜底。"""

    def __init__(self, *, epochs: int = 400, learning_rate: float = 0.1, l2: float = 1e-3) -> None:
        self.epochs = epochs
        self.learning_rate = learning_rate
        self.l2 = l2
        self.weights: np.ndarray | None = None
        self.bias = 0.0
        self._mean: np.ndarray | None = None
        self._std: np.ndarray | None = None

    def fit(self, X: np.ndarray, y: np.ndarray) -> "_NumpyLogisticRegression":
        X = np.asarray(X, dtype="float64")
        y = np.asarray(y, dtype="float64")
        self._mean = X.mean(axis=0)
        self._std = X.std(axis=0)
        self._std[self._std <= 1e-12] = 1.0
        Z = (X - self._mean) / self._std
        n_samples, n_features = Z.shape
        self.weights = np.zeros(n_features, dtype="float64")
        self.bias = 0.0
        for _ in range(self.epochs):
            logits = Z @ self.weights + self.bias
            probabilities = 1.0 / (1.0 + np.exp(-np.clip(logits, -30.0, 30.0)))
            error = probabilities - y
            grad_w = Z.T @ error / n_samples + self.l2 * self.weights
            grad_b = float(np.mean(error))
            self.weights -= self.learning_rate * grad_w
            self.bias -= self.learning_rate * grad_b
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        if self.weights is None or self._mean is None or self._std is None:
            raise RuntimeError("模型尚未训练")
        X = np.asarray(X, dtype="float64")
        Z = (X - self._mean) / self._std
        probabilities = 1.0 / (1.0 + np.exp(-np.clip(Z @ self.weights + self.bias, -30.0, 30.0)))
        return np.column_stack([1.0 - probabilities, probabilities])

    @property
    def feature_importances_(self) -> np.ndarray:
        if self.weights is None:
            return np.zeros(len(ML_FEATURE_NAMES))
        return np.abs(self.weights)


def _auc(y_true: np.ndarray, scores: np.ndarray) -> float | None:
    """AUC（Mann-Whitney 秩和口径，含并列处理）；单一类别返回 None。"""

    y_true = np.asarray(y_true, dtype="float64")
    scores = np.asarray(scores, dtype="float64")
    if y_true.size == 0:
        return None
    positives = y_true > 0.5
    n_pos = int(positives.sum())
    n_neg = int(y_true.size - n_pos)
    if n_pos == 0 or n_neg == 0:
        return None
    order = np.argsort(scores, kind="mergesort")
    ranks = np.empty(scores.size, dtype="float64")
    ranks[order] = np.arange(1, scores.size + 1, dtype="float64")
    # 并列分数取平均秩（AUC 标准做法）
    sorted_scores = scores[order]
    start = 0
    while start < sorted_scores.size:
        end = start + 1
        while end < sorted_scores.size and sorted_scores[end] == sorted_scores[start]:
            end += 1
        if end - start > 1:
            ranks[order[start:end]] = float(np.mean(np.arange(start + 1, end + 1)))
        start = end
    rank_sum = float(ranks[positives].sum())
    return float((rank_sum - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------


class MLRanker:
    """横截面 ML 打分器（walk-forward 训练/评估 + 指定日期推荐打分）。

    用法::

        ranker = MLRanker()
        report = ranker.walk_forward(bars)       # bars: DataFrame[symbol, date, OHLCV]
        report.oos_metrics["auc"], report.feature_importance
        picks = ranker.rank(bars, as_of=date(2025, 12, 31), top_n=10)
    """

    def __init__(self, config: MLConfig | None = None) -> None:
        self.config = config or MLConfig()
        self.backend = (
            f"sklearn-{self.config.model}" if SKLEARN_AVAILABLE else "numpy-logistic"
        )
        self._last_feature_importance: pd.DataFrame = pd.DataFrame(
            columns=["feature", "label", "importance"]
        )

    # -- 模型工厂 ---------------------------------------------------------
    def _build_model(self) -> Any:
        if not SKLEARN_AVAILABLE:
            return _NumpyLogisticRegression()
        if self.config.model == "gbdt":
            return HistGradientBoostingClassifier(
                max_iter=200,
                learning_rate=0.05,
                max_depth=3,
                l2_regularization=1.0,
                random_state=self.config.random_state,
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

    # -- 面板构建 ---------------------------------------------------------
    def build_panel(self, bars: pd.DataFrame) -> pd.DataFrame:
        """构建“日期 × 标的”特征面板（含未来收益标签，仅训练/评估用）。

        实现为**逐标的向量化**（滚动窗口一次算完，避免逐日重算指标），特征口径：

        * 收益率/波动/量比/突破/回撤/趋势稳定性/上涨占比：滚动窗口，只用 ≤ 当日数据；
        * RSI/ATR：对**该标的完整历史**用 TA-Lib 口径指标后取当日值（序列起点播种），
          与 :func:`strategy.factors.compute_ml_features` 的窗口版可能有早期差异，
          但训练与预测始终使用同一套 panel 口径，不影响公平性；
        * 标签：``label = 未来 label_horizon 日收益 > 当日截面中位数``（仅训练用）。

        Returns:
            列：``date, symbol, <ML_FEATURE_NAMES...>, future_return, label, label_date``；
            最后 ``label_horizon`` 个交易日无标签（``label`` 为 NaN，预测时仍可用特征）。
        """

        frame = normalize_bars(bars)
        config = self.config
        horizon = config.label_horizon
        feature_names = list(config.feature_names)
        panels: list[pd.DataFrame] = []
        for symbol, group in frame.groupby("symbol", sort=True):
            group = group.sort_values("date").reset_index(drop=True)
            if len(group) < config.min_history + horizon + 1:
                continue
            close = pd.to_numeric(group["close"], errors="coerce").astype("float64")
            high = pd.to_numeric(group["high"], errors="coerce").astype("float64")
            low = pd.to_numeric(group["low"], errors="coerce").astype("float64")
            volume = pd.to_numeric(group["volume"], errors="coerce").astype("float64")
            returns = close.pct_change()
            features = pd.DataFrame({"date": group["date"], "symbol": str(symbol)})
            # 收益率
            features["ret_1"] = returns
            for window in (5, 10, 20, 60):
                features[f"ret_{window}"] = close.pct_change(window)
            # 波动率（ddof=0，与 scalar 因子一致）
            features["volatility_20"] = returns.rolling(20, min_periods=20).std(ddof=0)
            features["volatility_60"] = returns.rolling(60, min_periods=60).std(ddof=0)
            # 量比
            volume_mean_short = volume.rolling(5, min_periods=5).mean()
            volume_mean_long = volume.rolling(20, min_periods=20).mean()
            features["volume_ratio_5_20"] = volume_mean_short / volume_mean_long
            features["volume_ratio_1_20"] = volume / volume_mean_long
            # 指标（复用 strategy.base 兼容层：TA-Lib 优先、pandas 兜底）
            features["rsi_14"] = rsi(close, 14)
            features["atr_pct_14"] = atr(high, low, close, 14) / close
            # 均线偏离
            features["dist_ma20"] = close / close.rolling(20, min_periods=20).mean() - 1.0
            features["dist_ma60"] = close / close.rolling(60, min_periods=60).mean() - 1.0
            # 回撤（窗口内相对峰值的最小值）
            within_peak = close.rolling(60, min_periods=60).max()
            features["drawdown_60"] = (close / within_peak - 1.0).rolling(60, min_periods=60).min()
            # 突破：收盘相对“前 20 日最高价”
            prior_high = high.shift(1).rolling(20, min_periods=20).max()
            features["breakout_20"] = close / prior_high - 1.0
            features["max_day_gain_20"] = returns.rolling(20, min_periods=20).max()
            # 趋势稳定性 / 上涨占比
            mean_return = returns.rolling(60, min_periods=60).mean()
            std_return = returns.rolling(60, min_periods=60).std(ddof=0)
            features["trend_stability_60"] = mean_return / std_return * math.sqrt(60)
            features["positive_ratio_60"] = (returns > 0).astype("float64").rolling(60, min_periods=60).mean()
            # 标签（未来数据仅在训练/评估时使用；预测阶段不依赖）
            base = close
            future = close.shift(-horizon)
            features["future_return"] = future / base - 1.0
            features["label_date"] = group["date"].shift(-horizon)
            panels.append(features)

        if not panels:
            raise ValueError(
                "ML 面板为空：行情历史不足（每只标的至少需要 "
                f"{config.min_history + horizon + 1} 根 bar）或标的数量不足"
            )
        panel = pd.concat(panels, ignore_index=True)
        # 只保留特征齐全的行（前 60 根 bar 的指标预热期自动剔除）
        usable = panel[feature_names].notna().all(axis=1)
        panel = panel[usable].reset_index(drop=True)
        if panel.empty:
            raise ValueError("ML 面板没有特征齐全的样本：请扩大行情区间")
        # 截面标签：未来 N 日收益是否跑赢当日截面中位数
        panel["label"] = np.nan
        valid_future = panel["future_return"].notna() & panel["label_date"].notna()
        for _, group in panel[valid_future].groupby("date"):
            if len(group) < 3:  # 截面样本太少，中位数无意义
                continue
            median = float(group["future_return"].median())
            panel.loc[group.index, "label"] = (group["future_return"] > median).astype(float)
            panel.loc[group.index, "market_median"] = median
        panel["label"] = pd.to_numeric(panel["label"], errors="coerce")
        return panel

    # -- walk-forward -----------------------------------------------------
    def walk_forward(self, bars: pd.DataFrame, symbols: Sequence[str] | None = None) -> MLRankerReport:
        """滚动训练 + 样本外评估（严格时间顺序，无未来数据）。

        Args:
            bars: 行情（``DataFrame``，含 date/symbol/OHLCV）。
            symbols: 可选标的子集（None 表示全部）。

        Returns:
            :class:`MLRankerReport`。
        """

        frame = normalize_bars(bars)
        if symbols:
            frame = frame[frame["symbol"].isin(set(map(str, symbols)))].reset_index(drop=True)
        panel = self.build_panel(frame)
        config = self.config
        feature_names = list(config.feature_names)
        notes: list[str] = []
        if not SKLEARN_AVAILABLE:
            notes.append(f"scikit-learn 不可用（{_SKLEARN_IMPORT_ERROR}），已使用 numpy 逻辑回归兜底")

        labeled = panel[panel["label"].notna()].copy()
        labeled = labeled[labeled["label_date"].notna()]
        # label_date 统一成 date，避免与 test_start 比较时出现 datetime64/date 混比
        labeled["label_date"] = pd.to_datetime(labeled["label_date"], errors="coerce").dt.date
        labeled = labeled[labeled["label_date"].notna()]
        if labeled.empty:
            raise ValueError("ML 面板没有可用标签：请提供更长的行情历史（至少 1 年）")
        evaluation_dates = sorted(labeled["date"].unique())
        if len(evaluation_dates) < config.train_window + config.test_window:
            raise ValueError(
                f"行情长度不足：样本外评估至少需要 {config.train_window + config.test_window} "
                f"个交易日，当前仅 {len(evaluation_dates)} 个"
            )
        all_dates = sorted(panel["date"].unique())
        date_order = {day: index for index, day in enumerate(all_dates)}

        folds: list[MLFold] = []
        prediction_rows: list[dict[str, Any]] = []
        importance_rows: list[list[float]] = []
        fold_index = 0
        start = config.train_window
        while start < len(evaluation_dates):
            test_dates = evaluation_dates[start : start + config.test_window]
            if not test_dates:
                break
            test_start = test_dates[0]
            test_end = test_dates[-1]
            test_start_order = date_order.get(test_start, 0)
            train_start_order = test_start_order - config.train_window
            train_start_date = all_dates[max(0, train_start_order)] if all_dates else test_start
            # 训练集：label_date 必须严格早于测试开始（标签在测试期前已走完，防泄漏）
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
            y_train = train_part["label"].to_numpy(dtype="float64")
            if len(np.unique(y_train)) < 2:
                notes.append(f"第 {fold_index + 1} 折跳过：训练标签只有单一类别")
                fold_index += 1
                start += config.test_window
                continue
            X_train = train_part[feature_names].to_numpy(dtype="float64")
            X_test = test_part[feature_names].to_numpy(dtype="float64")
            model = self._build_model()
            model.fit(X_train, y_train)
            probabilities = model.predict_proba(X_test)[:, 1]
            test_part = test_part.copy()
            test_part["proba"] = probabilities
            test_part["fold"] = fold_index + 1
            prediction_rows.append(test_part)
            importance_rows.append(abs(np.asarray(_model_importance(model), dtype="float64")))

            labels = test_part["label"].to_numpy(dtype="float64")
            valid_mask = test_part["label"].notna().to_numpy()
            auc_value = (
                _auc(labels[valid_mask], probabilities[valid_mask])
                if int(valid_mask.sum()) >= 10
                else None
            )
            predicted_positive = probabilities >= 0.5
            if predicted_positive.sum() > 0:
                win_rate = float(
                    np.mean(labels[predicted_positive][valid_mask[predicted_positive]])
                ) if valid_mask[predicted_positive].any() else None
                positive_mask = predicted_positive & valid_mask
                mean_return = (
                    float(test_part.loc[positive_mask, "future_return"].mean())
                    if positive_mask.any()
                    else None
                )
            else:
                win_rate = None
                mean_return = None
            accuracy = (
                float(np.mean((probabilities[valid_mask] >= 0.5) == (labels[valid_mask] > 0.5)))
                if valid_mask.any()
                else None
            )
            folds.append(
                MLFold(
                    fold=fold_index + 1,
                    train_start=str(train_part["date"].min()),
                    train_end=str(train_part["date"].max()),
                    test_start=str(test_start),
                    test_end=str(test_end),
                    n_train=int(len(train_part)),
                    n_test=int(len(test_part)),
                    auc=_safe_metric(auc_value),
                    accuracy=_safe_metric(accuracy),
                    win_rate=_safe_metric(win_rate),
                    mean_return=_safe_metric(mean_return),
                    positive_rate=_safe_metric(float(np.mean(predicted_positive))),
                )
            )
            fold_index += 1
            start += config.test_window

        if not folds:
            raise ValueError(
                "walk-forward 未产生有效折：请提供更长行情（建议 ≥ 3 年）或降低 min_train_rows"
            )

        oos = pd.concat(prediction_rows, ignore_index=True)
        oos_metrics = self._aggregate_metrics(oos, folds)
        importance = pd.DataFrame(
            {
                "feature": feature_names,
                "label": [FEATURE_LABELS.get(name, name) for name in feature_names],
                "importance": np.mean(np.vstack(importance_rows), axis=0),
            }
        )
        importance["importance"] = importance["importance"] / max(
            float(importance["importance"].sum()), 1e-12
        )
        importance = importance.sort_values("importance", ascending=False).reset_index(drop=True)
        self._last_feature_importance = importance
        conclusion = self._assess_alpha(oos_metrics)
        notes.append(conclusion["summary"])
        report = MLRankerReport(
            backend=self.backend,
            config=config,
            folds=folds,
            oos_predictions=oos,
            oos_metrics=oos_metrics,
            feature_importance=importance,
            notes=notes,
            conclusion=conclusion,
        )
        logger.info(
            "ML walk-forward 完成：%d 折、OOS 样本 %d、AUC=%s、胜率=%s（backend=%s）：%s",
            len(folds),
            len(oos),
            oos_metrics.get("auc"),
            oos_metrics.get("win_rate"),
            self.backend,
            conclusion["summary"],
        )
        return report

    @staticmethod
    def _assess_alpha(oos_metrics: dict[str, Any]) -> dict[str, Any]:
        """给出样本外 alpha 的**如实结论**（含负结果）与 M6 改进项。

        判定口径（保守）：``0.48 ≤ AUC ≤ 0.52`` 视为“接近随机、无显著 alpha”；
        ``AUC < 0.48`` 视为方向性错误（比随机更差），需检查标签/特征/泄漏。
        """

        auc = oos_metrics.get("auc")
        next_steps = [
            "更换标签：未来 5 日（或 10 日）**超额收益分位**（横截面分位/行业中性），替代简单跑赢中位数",
            "特征中性化：行业、市值、波动率暴露中性化后再训练，避免风格 beta 冒充 alpha",
            "检查 purge/embargo：训练/测试之间按交易日留隔离带，标签窗口不得跨入测试期",
            "特征泄漏复核：逐特征回看是否使用了 t 之后数据（含复权、财报 available_at 对齐）",
        ]
        if auc is None:
            summary = "样本外 AUC 无法计算（单一类别/样本不足），暂不能评估 alpha，仅作流程基线"
            significant = False
        elif 0.48 <= float(auc) <= 0.52:
            summary = (
                f"当前特征无显著 alpha（OOS AUC={float(auc):.4f}，接近随机 0.5）；"
                "本结果为**负结果基线**，不可当作可交易信号使用"
            )
            significant = False
        elif float(auc) < 0.48:
            summary = (
                f"当前特征样本外 AUC={float(auc):.4f} < 0.48（弱于随机/方向可能反了），"
                "需先排查标签与特征泄漏，再评估是否反向使用"
            )
            significant = False
        else:
            summary = (
                f"样本外 AUC={float(auc):.4f}，有一定区分度，但需扩大样本/多市场验证，"
                "并通过 OOS 收益与交易成本检验后才可用于推荐"
            )
            significant = True
        return {
            "significant": bool(significant),
            "baseline": True,
            "summary": summary,
            "next_steps": next_steps,
        }

    def _aggregate_metrics(self, oos: pd.DataFrame, folds: list[MLFold]) -> dict[str, Any]:
        """汇总样本外指标（AUC/准确率/胜率/收益/每日 Top3 命中率）。"""

        labels = oos["label"].to_numpy(dtype="float64")
        probabilities = oos["proba"].to_numpy(dtype="float64")
        valid = oos["label"].notna().to_numpy()
        auc_value = _auc(labels[valid], probabilities[valid]) if valid.sum() >= 10 else None
        accuracy = (
            float(np.mean((probabilities[valid] >= 0.5) == (labels[valid] > 0.5)))
            if valid.any()
            else None
        )
        predicted_positive = (probabilities >= 0.5) & valid
        win_rate = (
            float(np.mean(labels[predicted_positive])) if predicted_positive.any() else None
        )
        mean_return_positive = (
            float(oos.loc[predicted_positive, "future_return"].mean())
            if predicted_positive.any()
            else None
        )
        mean_return_all = float(oos.loc[valid, "future_return"].mean()) if valid.any() else None
        # 每日按 proba 取前 3 名，命中率（label=1 的比例）
        top_hits: list[float] = []
        for _, group in oos[valid].groupby("date"):
            if len(group) < 3:
                continue
            top = group.nlargest(3, "proba")
            top_hits.append(float(np.mean(top["label"].to_numpy(dtype="float64") > 0.5)))
        return {
            "backend": self.backend,
            "model": self.config.model,
            "folds": len(folds),
            "oos_samples": int(valid.sum()),
            "oos_dates": int(oos.loc[valid, "date"].nunique()) if valid.any() else 0,
            "auc": _safe_metric(auc_value),
            "accuracy": _safe_metric(accuracy),
            "win_rate": _safe_metric(win_rate),
            "positive_rate": _safe_metric(
                float(np.mean(probabilities[valid] >= 0.5)) if valid.any() else None
            ),
            "mean_return_positive": _safe_metric(mean_return_positive),
            "mean_return_all": _safe_metric(mean_return_all),
            "top3_daily_hit_rate": _safe_metric(float(np.mean(top_hits)) if top_hits else None),
            "label_horizon": self.config.label_horizon,
        }

    # -- 指定日期打分（供推荐引擎） ---------------------------------------
    def rank(
        self,
        bars: pd.DataFrame,
        as_of: Any,
        *,
        top_n: int = 10,
    ) -> list[dict[str, Any]]:
        """对 ``as_of`` 当日的自选池打分（只用 ≤ as_of 的数据训练与预测）。

        Args:
            bars: 行情 DataFrame。
            as_of: 打分日期（date/字符串）。
            top_n: 返回候选数。

        Returns:
            ``[{"symbol","proba","prob_pct","features","reason","backend"}]``，按 proba 降序；
            训练样本不足时抛 ``ValueError``（中文），由推荐引擎降级处理。
        """

        frame = normalize_bars(bars)
        # 统一转成 datetime.date 再比较，避免 datetime64[ns] 与 date 混比（防未来数据过滤）
        as_of_date = pd.Timestamp(as_of).date()
        frame = frame.copy()
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
        frame = frame[frame["date"].notna() & (frame["date"] <= as_of_date)].reset_index(drop=True)
        panel = self.build_panel(frame)
        feature_names = list(self.config.feature_names)
        labeled = panel[(panel["label"].notna()) & (panel["label_date"].notna())]
        train_part = labeled[labeled["label_date"] <= as_of_date]
        if len(train_part) < self.config.min_train_rows:
            raise ValueError(
                f"ML 训练样本不足（{len(train_part)} < {self.config.min_train_rows}），"
                "请扩大回测/行情区间后再用 ML 打分"
            )
        y_train = train_part["label"].to_numpy(dtype="float64")
        if len(np.unique(y_train)) < 2:
            raise ValueError("ML 训练标签只有单一类别，无法训练（请扩大标的池或时间区间）")
        model = self._build_model()
        model.fit(train_part[feature_names].to_numpy(dtype="float64"), y_train)
        self._last_feature_importance = self._importance_frame(model, feature_names)

        today = panel[panel["date"] == as_of_date]
        if today.empty:
            raise ValueError(f"日期 {as_of_date} 没有可用行情（非交易日或数据缺失）")
        probabilities = model.predict_proba(today[feature_names].to_numpy(dtype="float64"))[:, 1]
        results: list[dict[str, Any]] = []
        for (_, row), proba in zip(today.iterrows(), probabilities):
            features = {name: float(row[name]) for name in feature_names}
            results.append(
                {
                    "symbol": str(row["symbol"]),
                    "proba": float(proba),
                    "prob_pct": float(proba * 100.0),
                    "features": features,
                    "backend": self.backend,
                }
            )
        results.sort(key=lambda item: (-item["proba"], item["symbol"]))
        return results[: max(1, int(top_n))]

    # -- 特征重要性 -------------------------------------------------------
    def feature_importance(self) -> pd.DataFrame:
        """最近一次训练的特征重要性（``feature/label/importance``，已归一化）。"""

        return self._last_feature_importance.copy()

    @staticmethod
    def _importance_frame(model: Any, feature_names: Sequence[str]) -> pd.DataFrame:
        values = np.abs(np.asarray(_model_importance(model), dtype="float64"))
        total = float(values.sum()) if values.size else 0.0
        if total > 0:
            values = values / total
        return (
            pd.DataFrame(
                {
                    "feature": list(feature_names),
                    "label": [FEATURE_LABELS.get(name, name) for name in feature_names],
                    "importance": values,
                }
            )
            .sort_values("importance", ascending=False)
            .reset_index(drop=True)
        )


def _model_importance(model: Any) -> np.ndarray:
    """取出模型特征重要性/系数（兼容 sklearn Pipeline 与 numpy 兜底）。"""

    if isinstance(model, _NumpyLogisticRegression):
        return model.feature_importances_
    if hasattr(model, "feature_importances_"):
        return np.asarray(model.feature_importances_, dtype="float64")
    if hasattr(model, "coef_"):
        return np.abs(np.asarray(model.coef_, dtype="float64")).ravel()
    try:  # sklearn Pipeline
        estimator = model.named_steps.get("clf") if hasattr(model, "named_steps") else None
        if estimator is not None:
            if hasattr(estimator, "coef_"):
                return np.abs(np.asarray(estimator.coef_, dtype="float64")).ravel()
            if hasattr(estimator, "feature_importances_"):
                return np.asarray(estimator.feature_importances_, dtype="float64")
    except Exception:  # pragma: no cover - 兜底
        pass
    return np.zeros(len(ML_FEATURE_NAMES), dtype="float64")


def _safe_metric(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, 6)


def feature_importance_table(report: MLRankerReport) -> pd.DataFrame:
    """便捷函数：取报告里的特征重要性表（供 UI 渲染）。"""

    return report.feature_importance.copy()

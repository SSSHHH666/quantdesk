"""短线模型 v2 实验框架（P0）：扩样本、换标签、加特征、换模型、找 OOS 边际。

v1 结论（Lead 实测，已定稿）：3 标的、真实数据、48 折 walk-forward，
OOS 命中率 24.83% = 基础概率（零排序能力），扣费期望 -0.22%、最大回撤 -32.41%，
结论 ``usable=False``。本模块是 v2 的**可复现实验框架**：同一份行情上并行跑
「标签 × 特征集 × 模型」矩阵，严格 walk-forward + purge，输出可比较的 OOS 指标。

实验维度
--------
**标签（5 种）**
- ``ret5_ge2``：未来 5 日收益 ≥ +2%（v1 主标签）
- ``ret3_ge2``：未来 3 日收益 ≥ +2%
- ``cs_top20_5d``：未来 5 日收益的**截面分位 Top20%**（排序问题）
- ``three_class_5d``：未来 5 日三分类（跌 < -2% / 平 / 涨 > +2%），score = P(涨)
- ``gap_up_1d``：次日高开缺口 > +0.5%

**特征集**
- ``v1``：v1 的个股量价特征（18 个）
- ``v2``：v1 + 市场状态（宽基/自选池 20 日动量/波动/宽度/MA20 上方占比）+
  板块相对强度（按代码前缀分组）+ 涨跌停邻近度 + 连续放量 + 量价背离 + ATR/换手分位

**模型**
- ``logistic``：逻辑回归（StandardScaler + balanced）
- ``gbdt``：HistGradientBoosting 分类（三分类标签自动 multiclass）
- ``gbdt_rank``：HistGradientBoosting 回归，目标=截面收益分位（pointwise 排序代理）

防未来函数
----------
特征只用 ``≤ t`` 数据；训练集要求 ``label_date < test_start``（purge=标签期，
``ret3`` 3 日、``ret5`` 5 日、``gap`` 1 日），测试期只预测不拟合。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from backtest.broker_sim import AStockRules, guess_board
from strategy.base import atr, rsi
from strategy.factors import normalize_bars
from strategy.short_term import SHORT_FEATURE_NAMES

logger = logging.getLogger(__name__)

__all__ = [
    "LABEL_SPECS",
    "LabelSpec",
    "V2Config",
    "V2Result",
    "FEATURES_V1",
    "FEATURES_V2",
    "FEATURE_LABELS_V2",
    "build_panel",
    "walk_forward_v2",
    "sector_of",
]

try:  # pragma: no cover
    from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    SKLEARN_AVAILABLE = True
except Exception as _exc:  # pragma: no cover
    SKLEARN_AVAILABLE = False
    _SKLEARN_ERROR = f"{type(_exc).__name__}: {_exc}"
else:
    _SKLEARN_ERROR = None


# ---------------------------------------------------------------------------
# 一、标签与特征定义
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LabelSpec:
    """标签定义。"""

    key: str
    label: str
    kind: str  # binary | cross_section_top | three_class
    horizon: int
    threshold: float = 0.02
    quantile: float = 0.8
    description: str = ""

    @property
    def purge_days(self) -> int:
        return self.horizon


LABEL_SPECS: dict[str, LabelSpec] = {
    "ret5_ge2": LabelSpec("ret5_ge2", "未来5日收益≥+2%", "binary", 5, 0.02),
    "ret3_ge2": LabelSpec("ret3_ge2", "未来3日收益≥+2%", "binary", 3, 0.02),
    "cs_top20_5d": LabelSpec("cs_top20_5d", "未来5日截面Top20%", "cross_section_top", 5, quantile=0.8),
    "three_class_5d": LabelSpec("three_class_5d", "未来5日三分类(跌/平/涨)", "three_class", 5, 0.02),
    "gap_up_1d": LabelSpec("gap_up_1d", "次日高开缺口>+0.5%", "binary", 1, 0.005),
}

#: v1 特征（直接复用 short_term 的 18 个截面特征）
FEATURES_V1: tuple[str, ...] = tuple(SHORT_FEATURE_NAMES)

FEATURES_V2_EXTRA: tuple[str, ...] = (
    "sector_rel_ret_5",
    "sector_rel_ret_20",
    "sector_breadth",
    "limit_up_distance",
    "limit_down_distance",
    "vol_expand_streak",
    "vp_divergence",
    "market_ret_5",
    "market_ret_20",
    "market_vol_20",
    "market_breadth",
    "market_above_ma20",
)

FEATURES_V2: tuple[str, ...] = FEATURES_V1 + FEATURES_V2_EXTRA

FEATURE_LABELS_V2: dict[str, str] = {
    "sector_rel_ret_5": "板块相对5日",
    "sector_rel_ret_20": "板块相对20日",
    "sector_breadth": "板块上涨占比",
    "limit_up_distance": "距涨停",
    "limit_down_distance": "距跌停",
    "vol_expand_streak": "连续放量天数",
    "vp_divergence": "量价背离",
    "market_ret_5": "市场5日动量",
    "market_ret_20": "市场20日动量",
    "market_vol_20": "市场20日波动",
    "market_breadth": "市场上涨占比",
    "market_above_ma20": "市场MA20上方占比",
}

#: 板块分组（按代码前缀近似，无需行业数据即可做相对强度）
SECTOR_PREFIX: tuple[tuple[str, str], ...] = (
    ("300", "GEM"),
    ("301", "GEM"),
    ("688", "STAR"),
    ("689", "STAR"),
    ("600", "MAIN_SH"),
    ("601", "MAIN_SH"),
    ("603", "MAIN_SH"),
    ("605", "MAIN_SH"),
    ("000", "MAIN_SZ"),
    ("001", "MAIN_SZ"),
    ("002", "MAIN_SZ"),
    ("003", "MAIN_SZ"),
    ("920", "BSE"),
)


def sector_of(symbol: str) -> str:
    """按代码前缀近似板块（用于板块相对强度特征）。"""

    code = str(symbol).split(".")[0]
    for prefix, sector in SECTOR_PREFIX:
        if code.startswith(prefix):
            return sector
    return "OTHER"


@dataclass(frozen=True)
class V2Config:
    """单个实验配置。"""

    label: str = "ret5_ge2"
    feature_set: str = "v2"  # v1 | v2
    model: str = "logistic"  # logistic | gbdt | gbdt_rank
    train_window: int = 250
    test_window: int = 20
    min_train_rows: int = 300
    top_n: int = 5
    round_trip_cost: float = 0.0023
    random_state: int = 42

    def __post_init__(self) -> None:
        if self.label not in LABEL_SPECS:
            raise ValueError(f"未知标签 {self.label!r}；可选：{list(LABEL_SPECS)}")
        if self.feature_set not in ("v1", "v2"):
            raise ValueError(f"feature_set 仅支持 v1/v2，收到 {self.feature_set!r}")
        if self.model not in ("logistic", "gbdt", "gbdt_rank"):
            raise ValueError(f"model 仅支持 logistic/gbdt/gbdt_rank，收到 {self.model!r}")
        if not SKLEARN_AVAILABLE:
            raise RuntimeError(f"短线 v2 需要 scikit-learn：{_SKLEARN_ERROR}")

    @property
    def feature_names(self) -> tuple[str, ...]:
        return FEATURES_V1 if self.feature_set == "v1" else FEATURES_V2

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "feature_set": self.feature_set,
            "model": self.model,
            "train_window": self.train_window,
            "test_window": self.test_window,
            "top_n": self.top_n,
            "round_trip_cost": self.round_trip_cost,
        }


@dataclass
class V2Fold:
    fold: int
    train_start: Any
    train_end: Any
    test_start: Any
    test_end: Any
    n_train: int
    n_test: int
    hit_rate: float | None
    hit_positive: float | None
    mean_return_top_n: float | None
    net_expectancy: float | None
    long_short_spread: float | None
    base_rate: float | None
    model_importance: list[float] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {key: value for key, value in self.__dict__.items() if key != "model_importance"}


@dataclass
class V2Result:
    config: V2Config
    folds: list[V2Fold] = field(default_factory=list)
    predictions: pd.DataFrame = field(default_factory=pd.DataFrame)
    metrics: dict[str, Any] = field(default_factory=dict)
    feature_importance: pd.DataFrame = field(default_factory=pd.DataFrame)
    feature_stability: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "config": self.config.as_dict(),
            "metrics": dict(self.metrics),
            "feature_stability": dict(self.feature_stability),
            "top_features": self.feature_importance.head(8).to_dict(orient="records")
            if len(self.feature_importance)
            else [],
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# 二、面板构建（v1 个股量价 + v2 市场/板块/涨跌停/量价背离）
# ---------------------------------------------------------------------------


def _safe_numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").astype("float64")


def _per_date_norm(panel: pd.DataFrame, column: str, *, raw: bool = False) -> pd.Series:
    """按日期做截面分位（≥5 只）或 z-score 压缩（少量标的）；``raw`` 时仅标准化不排序。"""

    values = pd.to_numeric(panel[column], errors="coerce")

    def _transform(group: pd.Series) -> pd.Series:
        valid = group.dropna()
        if len(valid) < 2:
            return pd.Series([0.5] * len(group), index=group.index)
        if raw:
            mean = float(valid.mean())
            std = float(valid.std(ddof=0))
            if not math.isfinite(std) or std <= 0:
                return pd.Series([0.0] * len(group), index=group.index)
            return ((group - mean) / std).clip(-3.0, 3.0)
        if len(valid) >= 5:
            return group.rank(pct=True)
        mean = float(valid.mean())
        std = float(valid.std(ddof=0))
        if not math.isfinite(std) or std <= 0:
            return pd.Series([0.5] * len(group), index=group.index)
        return (0.5 + (group - mean) / std * 0.15).clip(0.0, 1.0)

    return values.groupby(panel["date"], group_keys=False).apply(_transform)


def _symbol_raw_features(group: pd.DataFrame, symbol: str) -> pd.DataFrame | None:
    """单标的原始特征（v1 基础 + 涨跌停距离/连续放量/量价背离）。"""

    close = _safe_numeric(group["close"])
    high = _safe_numeric(group["high"])
    low = _safe_numeric(group["low"])
    open_price = _safe_numeric(group["open"])
    volume = _safe_numeric(group["volume"])
    amount = _safe_numeric(group["amount"]) if "amount" in group.columns else close * volume
    if amount.fillna(0).sum() <= 0:
        amount = close * volume
    returns = close.pct_change()
    frame = pd.DataFrame(index=group.index)
    frame["raw_ret_1"] = returns
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
    frame["raw_consec_up"] = up.groupby((up != up.shift()).cumsum()).cumsum().fillna(0.0)
    ma20 = close.rolling(20, min_periods=20).mean()
    frame["raw_ma20_slope"] = ma20 / ma20.shift(5) - 1.0
    frame["raw_rsi"] = rsi(close, 14)
    frame["raw_volatility"] = returns.rolling(20, min_periods=20).std(ddof=0)
    frame["raw_gap_open"] = open_price / close.shift(1) - 1.0
    frame["raw_market_rel"] = frame["raw_ret_5"]

    # 涨跌停邻近度（按板块幅度；ST 状态不可得时按非 ST 近似）
    prev_close = close.shift(1)
    try:
        board = guess_board(symbol)
    except ValueError:
        board = "MAIN"
    limit_pct = AStockRules.limit_pct(board, False)
    limit_up = (prev_close * (1 + limit_pct)).round(2)
    limit_down = (prev_close * (1 - limit_pct)).round(2)
    frame["raw_limit_up_distance"] = (limit_up - close) / close
    frame["raw_limit_down_distance"] = (close - limit_down) / close
    # 连续放量天数（volume > 1.2 × 20日均量）
    strong_volume = (frame["raw_volume_ratio"] > 1.2).astype("float64")
    frame["raw_vol_expand_streak"] = strong_volume.groupby(
        (strong_volume != strong_volume.shift()).cumsum()
    ).cumsum().fillna(0.0)
    # 量价背离：5 日涨但缩量 / 5 日跌但放量 → 1；否则 0
    ret5 = frame["raw_ret_5"]
    volume_ratio = frame["raw_volume_ratio"]
    frame["raw_vp_divergence"] = (
        ((ret5 > 0.02) & (volume_ratio < 1.0)) | ((ret5 < -0.02) & (volume_ratio > 1.5))
    ).astype("float64")
    frame["raw_close"] = close
    frame["symbol"] = symbol
    frame["sector"] = sector_of(symbol)
    frame["date"] = group["date"]
    return frame


def build_panel(bars: pd.DataFrame, label_key: str, feature_set: str = "v2") -> pd.DataFrame:
    """构建 v2 实验面板（含 5 种标签列与市场/板块特征）。"""

    if label_key not in LABEL_SPECS:
        raise ValueError(f"未知标签 {label_key!r}")
    if feature_set not in ("v1", "v2"):
        raise ValueError("feature_set 仅支持 v1/v2")
    spec = LABEL_SPECS[label_key]
    frame = normalize_bars(bars)
    panels: list[pd.DataFrame] = []
    for symbol, group in frame.groupby("symbol", sort=True):
        group = group.sort_values("date").reset_index(drop=True)
        if len(group) < 70:
            continue
        panel = _symbol_raw_features(group, str(symbol))
        if panel is None:
            continue
        close = _safe_numeric(group["close"])
        open_price = _safe_numeric(group["open"])
        for horizon in (1, 3, 5):
            panel[f"future_return_{horizon}"] = close.shift(-horizon) / close - 1.0
            panel[f"label_date_{horizon}"] = group["date"].shift(-horizon)
        panel["next_gap"] = open_price.shift(-1) / close - 1.0
        panels.append(panel)
    if not panels:
        raise ValueError("v2 面板为空：行情历史不足")
    panel = pd.concat(panels, ignore_index=True)

    # 市场状态（自选池等权代理；若有 000300.SH 则优先用它）
    market_source = panel
    index_rows = panel[panel["symbol"] == "000300.SH"]
    if len(index_rows) > 0:
        market_source = index_rows
    market = market_source.groupby("date").agg(
        market_ret_5=("raw_ret_5", "mean"),
        market_ret_20=("raw_ret_20", "mean"),
        market_vol_20=("raw_volatility", "mean"),
        market_breadth=("raw_ret_5", lambda s: float((s > 0).mean())),
        market_above_ma20=("raw_dist_ma20", lambda s: float((s > 0).mean())),
    ).reset_index()
    panel = panel.merge(market, on="date", how="left")

    # 板块相对强度
    sector = panel.groupby(["date", "sector"]).agg(
        sector_ret_5=("raw_ret_5", "mean"),
        sector_ret_20=("raw_ret_20", "mean"),
        sector_breadth=("raw_ret_5", lambda s: float((s > 0).mean())),
    ).reset_index()
    panel = panel.merge(sector, on=["date", "sector"], how="left")
    panel["raw_sector_rel_ret_5"] = panel["raw_ret_5"] - panel["sector_ret_5"]
    panel["raw_sector_rel_ret_20"] = panel["raw_ret_20"] - panel["sector_ret_20"]
    panel["raw_sector_breadth"] = panel["sector_breadth"]

    # 截面化映射
    mapping: dict[str, str] = {
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
        "sector_rel_ret_5": "raw_sector_rel_ret_5",
        "sector_rel_ret_20": "raw_sector_rel_ret_20",
        "sector_breadth": "raw_sector_breadth",
        "limit_up_distance": "raw_limit_up_distance",
        "limit_down_distance": "raw_limit_down_distance",
        "vol_expand_streak": "raw_vol_expand_streak",
        "vp_divergence": "raw_vp_divergence",
    }
    for target, source in mapping.items():
        if source in panel.columns:
            panel[target] = _per_date_norm(panel, source)
    # 市场状态特征：按日期唯一 → 直接标准化到可比较量纲（不参与截面排序）
    for target, source in (
        ("market_ret_5", "market_ret_5"),
        ("market_ret_20", "market_ret_20"),
        ("market_vol_20", "market_vol_20"),
        ("market_breadth", "market_breadth"),
        ("market_above_ma20", "market_above_ma20"),
    ):
        values = pd.to_numeric(panel[source], errors="coerce")
        if target.startswith("market_ret"):
            panel[target] = (values * 20.0).clip(-3.0, 3.0)
        elif target == "market_vol_20":
            panel[target] = (values * 50.0).clip(0.0, 5.0)
        else:
            panel[target] = values.clip(0.0, 1.0)

    # 标签
    panel["label"] = np.nan
    panel["label_date"] = pd.NaT
    if spec.kind == "binary" and spec.horizon == 1:
        target = panel["next_gap"]
        horizon_col = "next_gap"
        label_date = panel["label_date_1"]
    else:
        horizon_col = f"future_return_{spec.horizon}"
        target = panel[horizon_col]
        label_date = panel[f"label_date_{spec.horizon}"]
    if spec.kind == "binary":
        panel["label"] = np.where(target.notna(), (target >= spec.threshold).astype(float), np.nan)
        panel["label_date"] = label_date
    elif spec.kind == "three_class":
        panel["label"] = np.where(
            target.notna(),
            np.where(target > spec.threshold, 1.0, np.where(target < -spec.threshold, -1.0, 0.0)),
            np.nan,
        )
        panel["label_date"] = label_date
    else:  # cross_section_top
        panel["label_date"] = label_date
        valid = target.notna()
        ranks = panel[valid].groupby("date")[horizon_col].rank(pct=True)
        counts = panel[valid].groupby("date")[horizon_col].transform("count")
        labels = pd.Series(np.nan, index=panel.index)
        top = (ranks >= spec.quantile) & (counts >= 5)
        labels.loc[top.index] = top.astype(float)
        panel["label"] = labels

    # 特征齐全性
    features = FEATURES_V1 if feature_set == "v1" else FEATURES_V2
    usable = panel[list(features)].notna().all(axis=1)
    panel = panel[usable].reset_index(drop=True)
    if panel.empty:
        raise ValueError("v2 面板没有特征齐全的样本")
    panel["label"] = pd.to_numeric(panel["label"], errors="coerce")
    return panel


# ---------------------------------------------------------------------------
# 三、walk-forward 实验
# ---------------------------------------------------------------------------


def _build_model(config: V2Config) -> Any:
    if config.model == "logistic":
        return Pipeline(
            [
                ("scaler", StandardScaler()),
                (
                    "clf",
                    LogisticRegression(
                        max_iter=1000,
                        random_state=config.random_state,
                        class_weight="balanced",
                    ),
                ),
            ]
        )
    if config.model == "gbdt":
        return HistGradientBoostingClassifier(
            max_iter=200,
            learning_rate=0.05,
            max_depth=3,
            l2_regularization=1.0,
            random_state=config.random_state,
        )
    return HistGradientBoostingRegressor(
        max_iter=200,
        learning_rate=0.05,
        max_depth=3,
        l2_regularization=1.0,
        random_state=config.random_state,
    )


def _importance(model: Any, n_features: int) -> np.ndarray:
    if hasattr(model, "feature_importances_"):
        return np.abs(np.asarray(model.feature_importances_, dtype="float64"))
    if hasattr(model, "coef_"):
        coefficients = np.asarray(model.coef_, dtype="float64")
        return np.abs(coefficients).sum(axis=0) if coefficients.ndim > 1 else np.abs(coefficients)
    try:
        estimator = model.named_steps.get("clf")
        coefficients = np.asarray(estimator.coef_, dtype="float64")
        return np.abs(coefficients).sum(axis=0) if coefficients.ndim > 1 else np.abs(coefficients)
    except Exception:  # pragma: no cover
        return np.ones(n_features, dtype="float64")


def _score(model: Any, config: V2Config, X: np.ndarray) -> np.ndarray:
    """统一输出“越大越可能上涨”的排序分数。"""

    spec = LABEL_SPECS[config.label]
    if config.model == "gbdt_rank":
        return np.asarray(model.predict(X), dtype="float64")
    probabilities = model.predict_proba(X)
    if spec.kind == "three_class":
        # 类别顺序：-1(跌)/0(平)/1(涨) → score = P(涨) - P(跌)
        classes = list(model.classes_)
        up = classes.index(1.0) if 1.0 in classes else len(classes) - 1
        down = classes.index(-1.0) if -1.0 in classes else 0
        return probabilities[:, up] - probabilities[:, down]
    return probabilities[:, 1]


def walk_forward_v2(bars: pd.DataFrame, config: V2Config) -> V2Result:
    """跑单个配置的严格 walk-forward 实验。"""

    spec = LABEL_SPECS[config.label]
    panel = build_panel(bars, config.label, config.feature_set)
    features = list(config.feature_names)
    labeled = panel[
        panel["label"].notna()
        & panel["label_date"].notna()
        & pd.to_datetime(panel["label_date"], errors="coerce").notna()
    ].copy()
    labeled["label_date"] = pd.to_datetime(labeled["label_date"], errors="coerce").dt.date
    if labeled.empty:
        raise ValueError(f"标签 {config.label} 没有可用样本")
    evaluation_dates = sorted(labeled["date"].unique())
    if len(evaluation_dates) < config.train_window + config.test_window:
        raise ValueError(
            f"行情长度不足：需要 {config.train_window + config.test_window} 个交易日，"
            f"当前 {len(evaluation_dates)} 个"
        )
    all_dates = sorted(panel["date"].unique())
    date_order = {day: index for index, day in enumerate(all_dates)}
    folds: list[V2Fold] = []
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
        train_mask = (
            labeled["label_date"].notna()
            & (labeled["label_date"] < test_start)
            & (labeled["date"] >= train_start_date)
        )
        train_part = labeled[train_mask]
        test_part = panel[panel["date"].isin(test_dates)]
        if len(train_part) < config.min_train_rows:
            fold_index += 1
            start += config.test_window
            continue
        X_train = train_part[features].to_numpy(dtype="float64")
        if config.model == "gbdt_rank":
            target = train_part.groupby("date")["label"].transform(lambda s: s)  # placeholder
            # 排序代理：用未来 5 日收益的截面分位作为回归目标（与当前标签同日期分组）
            return_col = f"future_return_{spec.horizon}" if spec.horizon > 1 else "next_gap"
            ranks = train_part.groupby("date")[return_col].rank(pct=True)
            y_train = ranks.fillna(0.5).to_numpy(dtype="float64")
        else:
            y_train = train_part["label"].to_numpy(dtype="float64")
            if spec.kind != "three_class" and len(np.unique(y_train)) < 2:
                fold_index += 1
                start += config.test_window
                continue
            if spec.kind == "three_class" and len(np.unique(y_train)) < 2:
                fold_index += 1
                start += config.test_window
                continue
        X_test = test_part[features].to_numpy(dtype="float64")
        model = _build_model(config)
        model.fit(X_train, y_train)
        scores = _score(model, config, X_test)
        test_frame = test_part.copy()
        test_frame["score"] = scores
        test_frame["fold"] = fold_index + 1
        prediction_rows.append(test_frame)
        importance_rows.append(_importance(model, len(features)))

        top_n = max(1, int(config.top_n))
        picked = (
            test_frame.groupby("date", group_keys=False)
            .apply(lambda group: group.nlargest(top_n, "score"))
            .reset_index(drop=True)
        )
        bottom = (
            test_frame.groupby("date", group_keys=False)
            .apply(lambda group: group.nsmallest(top_n, "score"))
            .reset_index(drop=True)
        )
        future_col = f"future_return_{spec.horizon}" if spec.horizon > 1 else "next_gap"
        top_returns = pd.to_numeric(picked[future_col], errors="coerce").dropna()
        bottom_returns = pd.to_numeric(bottom[future_col], errors="coerce").dropna()
        base_series = pd.to_numeric(test_frame[future_col], errors="coerce").dropna()
        picked_labels = picked["label"].to_numpy(dtype="float64")
        valid_picked = picked["label"].notna().to_numpy()
        hit_rate = (
            float((picked_labels[valid_picked] == 1.0).mean()) if valid_picked.any() else None
        )
        base_rate = float((base_series > 0).mean()) if len(base_series) else None
        mean_return = float(top_returns.mean()) if len(top_returns) else None
        long_short = (
            float(top_returns.mean() - bottom_returns.mean())
            if len(top_returns) and len(bottom_returns)
            else None
        )
        folds.append(
            V2Fold(
                fold=fold_index + 1,
                train_start=str(train_part["date"].min()),
                train_end=str(train_part["date"].max()),
                test_start=str(test_start),
                test_end=str(test_end),
                n_train=int(len(train_part)),
                n_test=int(len(test_part)),
                hit_rate=_round(hit_rate),
                hit_positive=_round(float((top_returns > 0).mean())) if len(top_returns) else None,
                mean_return_top_n=_round(mean_return),
                net_expectancy=_round(mean_return - config.round_trip_cost)
                if mean_return is not None
                else None,
                long_short_spread=_round(long_short),
                base_rate=_round(base_rate),
                model_importance=[float(v) for v in importance_rows[-1]],
            )
        )
        fold_index += 1
        start += config.test_window

    if not folds:
        raise ValueError(f"配置 {config.label}/{config.model} 未产生有效折（样本不足）")
    predictions = pd.concat(prediction_rows, ignore_index=True)
    metrics = _aggregate_metrics(predictions, folds, config)
    importance = pd.DataFrame(
        {
            "feature": features,
            "label": [FEATURE_LABELS_V2.get(name, name) for name in features],
            "importance": np.mean(np.vstack(importance_rows), axis=0),
        }
    )
    importance["importance"] = importance["importance"] / max(
        float(importance["importance"].sum()), 1e-12
    )
    importance = importance.sort_values("importance", ascending=False).reset_index(drop=True)
    stability = _feature_stability(importance_rows, features, folds)
    return V2Result(
        config=config,
        folds=folds,
        predictions=predictions,
        metrics=metrics,
        feature_importance=importance,
        feature_stability=stability,
        notes=[
            f"标签：{spec.label}（{spec.kind}，horizon={spec.horizon}，purge={spec.purge_days} 日）",
            "严格 walk-forward：训练标签 label_date < test_start；测试期只预测",
            f"费用口径：round_trip_cost={config.round_trip_cost:.2%}（双边费用+滑点）",
        ],
    )


def _aggregate_metrics(
    predictions: pd.DataFrame, folds: list[V2Fold], config: V2Config
) -> dict[str, Any]:
    valid_folds = [fold for fold in folds if fold.hit_rate is not None]
    hit = [fold.hit_rate for fold in valid_folds]
    expectancy = [fold.net_expectancy for fold in valid_folds if fold.net_expectancy is not None]
    spreads = [fold.long_short_spread for fold in valid_folds if fold.long_short_spread is not None]
    base = [fold.base_rate for fold in valid_folds if fold.base_rate is not None]
    spread_series = np.asarray(spreads, dtype="float64") if spreads else np.asarray([])
    t_stat = (
        float(spread_series.mean() / (spread_series.std(ddof=1) / math.sqrt(len(spread_series))))
        if len(spread_series) >= 3 and spread_series.std(ddof=1) > 0
        else None
    )
    metrics: dict[str, Any] = {
        "label": config.label,
        "feature_set": config.feature_set,
        "model": config.model,
        "folds": len(folds),
        "valid_folds": len(valid_folds),
        "oos_samples": int(len(predictions)),
        "oos_dates": int(predictions["date"].nunique()),
        "hit_rate": _round(float(np.mean(hit))) if hit else None,
        "hit_rate_std": _round(float(np.std(hit, ddof=1))) if len(hit) >= 2 else None,
        "base_rate": _round(float(np.mean(base))) if base else None,
        "hit_rate_lift": _round(float(np.mean(hit) - np.mean(base)))
        if hit and base
        else None,
        "net_expectancy": _round(float(np.mean(expectancy))) if expectancy else None,
        "net_expectancy_std": _round(float(np.std(expectancy, ddof=1)))
        if len(expectancy) >= 2
        else None,
        "long_short_spread": _round(float(np.mean(spreads))) if spreads else None,
        "long_short_t_stat": _round(t_stat),
        "positive_folds_pct": _round(
            float(np.mean([1.0 if value > 0 else 0.0 for value in expectancy]))
        )
        if expectancy
        else None,
        "round_trip_cost": config.round_trip_cost,
    }
    return metrics


def _feature_stability(
    importance_rows: list[list[float]], features: Sequence[str], folds: list[V2Fold]
) -> dict[str, Any]:
    """特征稳定性：跨折重要性均值/标准差、Top5 重合率、首尾半段 Spearman。"""

    matrix = np.asarray(importance_rows, dtype="float64")
    if matrix.ndim != 2 or matrix.shape[0] < 2:
        return {"folds": int(matrix.shape[0]) if matrix.ndim == 2 else 0}
    mean = matrix.mean(axis=0)
    std = matrix.std(axis=0, ddof=1)
    cv = np.divide(std, np.where(mean > 0, mean, np.nan))
    order = np.argsort(-mean)
    top5 = set(order[:5].tolist())
    overlaps: list[float] = []
    for row in matrix:
        row_top5 = set(np.argsort(-row)[:5].tolist())
        overlaps.append(len(row_top5 & top5) / 5.0)
    half = max(1, matrix.shape[0] // 2)
    first = matrix[:half].mean(axis=0)
    second = matrix[half:].mean(axis=0)
    spearman = _spearman(first, second)
    table = [
        {
            "feature": str(features[index]),
            "mean_importance": _round(float(mean[index])),
            "std": _round(float(std[index])),
            "cv": _round(float(cv[index])) if math.isfinite(cv[index]) else None,
        }
        for index in order
    ]
    return {
        "folds": int(matrix.shape[0]),
        "mean_top5_overlap": _round(float(np.mean(overlaps))),
        "first_second_spearman": _round(spearman),
        "top_features": table[:10],
    }


def _spearman(a: np.ndarray, b: np.ndarray) -> float | None:
    if len(a) < 3 or len(b) < 3:
        return None
    ranks_a = pd.Series(a).rank().to_numpy()
    ranks_b = pd.Series(b).rank().to_numpy()
    if ranks_a.std() <= 0 or ranks_b.std() <= 0:
        return None
    return float(np.corrcoef(ranks_a, ranks_b)[0, 1])


def _round(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, 6)

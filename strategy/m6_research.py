"""M6 短线选股研究框架：标签矩阵 × 特征扩充 × 模型对比 × 严格 walk-forward OOS。

M6-R2 加固（Lead task-2）
-------------------------
1. **复权总收益标签**：``future_ret_h = adjclose.shift(-h) / adjclose - 1``，
   其中 ``adjclose = close × adj_factor``（逐日已知的 qfq 复权因子）。不复权价在送转/除权日
   会出现 -20%~-90% 的人为跳空，会直接污染标签。特征同样在复权价上计算（复权价在 t 时刻
   已知，不构成未来函数），不复权价保留在 ``raw_close`` 列供打印/对照。
2. **持有期对齐**：组合模拟的持有期 = 标签 horizon（ret3→3 日、ret10→10 日），
   **非重叠再平衡**（每 h 个交易日一次），成本按实际换手扣：``round_trip_cost × 换手率``
   （换手率 = 被替换持仓占持仓数的比例；首期为全换手）。
3. **门槛严格**（不放宽）：
   - ``RankIC > 0.03``
   - ``Top 组扣费后超额 > 0``（超额 = Top 组合 h 日收益 − 同期全池等权 h 日收益 − 成本，
     按非重叠再平衡窗口计算）
   - 命中率显著高于基础概率：**合并样本**二项检验 p<0.05（用非重叠再平衡日样本，偏保守），
     同时给分折 t 检验
   任一不满足 ``usable=False``，并逐项写清未过项。
4. **统计可信度**：RankIC = 日度截面 Spearman IC 的均值；ICIR、正 IC 比例、多空 t 值、
   分折方向一致性；最佳配置做 5 日块 bootstrap 95% CI；对“36 组取最优”的多重比较偏差
   做 max-statistic 零分布模拟 + Bonferroni 校正。
5. **无未来函数自检**：训练集 ``label_date < test_start`` 且 ``train date <= test 前第
   embargo 个交易日``；``M6Fold`` 暴露 ``train_label_max`` / ``train_end`` / ``embargo_date``
   供 QA 独立断言。

历史基线（必须作为对照）
------------------------
短线 v1：3 只股票 48 折 OOS 命中率 24.83%（= 基础概率）、扣费期望 -0.22%、
MDD -32.41%、usable=False；全市场筛选器 RankIC -0.029/-0.030/-0.037（5/10/20 日）、
Top 组超额不显著。本轮为“扩样本 + 换标签/特征/模型 + 严格 purge”后的再检验。
"""

from __future__ import annotations

import logging
import math
import os as _os

# 本机 DSH Windows 沙箱禁止进程间管道：sklearn HistGradientBoosting 的 joblib
# threading 后端会建 multiprocessing 管道 → PermissionError。固定单线程 OpenMP
# （OpenMP 在首次并行区才读该变量，故在 fit 前设置有效）既规避限制，又保证矩阵
# 结果完全可复现。显式设置的环境变量优先（setdefault）。
_os.environ.setdefault("OMP_NUM_THREADS", "1")

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from backtest.broker_sim import AStockRules, guess_board
from backtest.metrics import compute_metrics
from strategy.factors import normalize_bars
from strategy.short_term_v2 import _symbol_raw_features, sector_of

logger = logging.getLogger(__name__)

__all__ = [
    "M6_LABELS",
    "M6Config",
    "M6Result",
    "M6Fold",
    "FEATURES_V1",
    "FEATURES_MARKET",
    "FEATURES_ALL",
    "PRICE_BASIS",
    "build_m6_panel",
    "label_summary",
    "run_m6_config",
    "load_index_bars",
    "passive_oos_benchmark",
    "gate_check",
    "block_bootstrap_ci",
    "multiple_comparison_analysis",
    "stratified_oos_stats",
    "feature_rank_ic_table",
]

try:
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

try:  # scipy 是 sklearn 的依赖；缺失时退回正态近似（仅影响 p 值精度）
    from scipy.stats import binomtest as _binomtest
    from scipy.stats import ttest_1samp as _ttest_1samp

    SCIPY_AVAILABLE = True
except Exception:  # pragma: no cover
    SCIPY_AVAILABLE = False
    _binomtest = None
    _ttest_1samp = None


# 价格口径：adjclose = close × adj_factor（总收益）。特征与标签都在此口径上计算。
PRICE_BASIS = "adjclose"
# 截面排序/截面标签的最小截面标的数：不足时截面分位与 Top20% 标签不可信
# （例：6 只里取 rank>=0.8 会选出 2 只 = 33%，把正样本率抬高到 >20%）。
MIN_CROSS_SECTION = 40


# ---------------------------------------------------------------------------
# 标签与特征定义
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class M6Label:
    key: str
    label: str
    kind: str  # binary | cross_section_top | three_class
    horizon: int
    threshold: float = 0.02
    quantile: float = 0.8
    description: str = ""

    @property
    def purge_days(self) -> int:
        return max(5, self.horizon)


M6_LABELS: dict[str, M6Label] = {
    "ret3_ge2": M6Label("ret3_ge2", "未来3日收益≥+2%", "binary", 3),
    "ret5_ge2": M6Label("ret5_ge2", "未来5日收益≥+2%", "binary", 5),
    "ret10_ge2": M6Label("ret10_ge2", "未来10日收益≥+2%", "binary", 10),
    "cs_top20_5d": M6Label("cs_top20_5d", "未来5日截面Top20%", "cross_section_top", 5, quantile=0.8),
    "three_class_5d": M6Label("three_class_5d", "未来5日三分类(跌/平/涨)", "three_class", 5),
    "gap_up_1d": M6Label("gap_up_1d", "次日高开缺口>+0.5%", "binary", 1, threshold=0.005),
}

FEATURES_V1: tuple[str, ...] = (
    "rel_ret_5",
    "rel_ret_20",
    "vol_ratio_rank",
    "breakout_rank",
    "dist_ma20_rank",
    "dist_ma60_rank",
    "atr_pct_rank",
    "turnover_rank",
    "amount_rank",
    "upper_shadow_rank",
    "lower_shadow_rank",
    "body_rank",
    "consec_up_rank",
    "ma20_slope_rank",
    "rsi_rank",
    "volatility_rank",
    "gap_open_rank",
    "market_rel_rank",
)

FEATURES_MARKET: tuple[str, ...] = (
    "sector_rel_ret_5",
    "sector_rel_ret_20",
    "sector_breadth",
    "limit_up_distance",
    "limit_down_distance",
    "vol_expand_streak",
    "vp_divergence",
    "near_high_250",
    "market_ret_20",
    "market_ret_60",
    "market_vol_20",
    "market_vol_pct",
    "market_drawdown_60",
    "market_above_ma20",
)

FEATURES_ALL: tuple[str, ...] = FEATURES_V1 + FEATURES_MARKET

FEATURE_LABELS: dict[str, str] = {
    "rel_ret_5": "5日相对涨幅", "rel_ret_20": "20日相对涨幅", "vol_ratio_rank": "量比",
    "breakout_rank": "突破20日高点", "dist_ma20_rank": "距MA20", "dist_ma60_rank": "距MA60",
    "atr_pct_rank": "ATR波动", "turnover_rank": "换手率代理", "amount_rank": "成交额分位",
    "upper_shadow_rank": "上影线", "lower_shadow_rank": "下影线", "body_rank": "实体占比",
    "consec_up_rank": "连续上涨天数", "ma20_slope_rank": "MA20斜率", "rsi_rank": "RSI(14)",
    "volatility_rank": "20日波动率", "gap_open_rank": "跳空幅度", "market_rel_rank": "相对市场强度",
    "sector_rel_ret_5": "板块相对5日", "sector_rel_ret_20": "板块相对20日",
    "sector_breadth": "板块上涨占比", "limit_up_distance": "距涨停", "limit_down_distance": "距跌停",
    "vol_expand_streak": "连续放量天数", "vp_divergence": "量价背离", "near_high_250": "距一年新高",
    "market_ret_20": "市场20日动量", "market_ret_60": "市场60日动量",
    "market_vol_20": "市场20日波动", "market_vol_pct": "市场波动分位",
    "market_drawdown_60": "市场60日回撤", "market_above_ma20": "市场MA20上方",
}


@dataclass(frozen=True)
class M6Config:
    label: str
    feature_set: str = "all"  # v1 | all
    model: str = "logistic"  # logistic | gbdt | gbdt_rank
    top_n: int = 5
    bottom_n: int = 5
    train_window: int = 250
    test_window: int = 20
    min_train_rows: int = 300
    embargo: int = 5
    portfolio_holding: int = 5  # 兼容字段；实际持有期强制 = 标签 horizon
    round_trip_cost: float = 0.0023
    random_state: int = 42

    def __post_init__(self) -> None:
        if self.label not in M6_LABELS:
            raise ValueError(f"未知标签 {self.label!r}")
        if self.feature_set not in ("v1", "all"):
            raise ValueError("feature_set 仅支持 v1/all")
        if self.model not in ("logistic", "gbdt", "gbdt_rank"):
            raise ValueError("model 仅支持 logistic/gbdt/gbdt_rank")
        if not SKLEARN_AVAILABLE:
            raise RuntimeError(f"M6 需要 scikit-learn：{_SKLEARN_ERROR}")

    @property
    def features(self) -> tuple[str, ...]:
        return FEATURES_V1 if self.feature_set == "v1" else FEATURES_ALL

    @property
    def holding_days(self) -> int:
        """组合持有期 = 标签 horizon（强制对齐，非重叠再平衡）。"""
        return M6_LABELS[self.label].horizon

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "feature_set": self.feature_set,
            "model": self.model,
            "top_n": self.top_n,
            "bottom_n": self.bottom_n,
            "train_window": self.train_window,
            "test_window": self.test_window,
            "min_train_rows": self.min_train_rows,
            "embargo": self.embargo,
            "portfolio_holding": self.holding_days,
            "round_trip_cost": self.round_trip_cost,
            "purge_days": M6_LABELS[self.label].purge_days,
            "random_state": self.random_state,
            "price_basis": PRICE_BASIS,
        }


@dataclass
class M6Fold:
    fold: int
    train_start: Any
    train_end: Any
    test_start: Any
    test_end: Any
    n_train: int
    n_test: int
    hit_rate: float | None
    base_rate: float | None
    net_expectancy: float | None
    ic: float | None
    long_short_spread: float | None
    # 无未来函数自检元数据（QA 断言用）
    train_label_max: Any = None
    train_date_max: Any = None
    train_label_min: Any = None
    embargo_date: Any = None
    purge_days: int = 5
    n_features: int = 0
    dropped_features: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class M6Result:
    config: M6Config
    folds: list[M6Fold] = field(default_factory=list)
    predictions: pd.DataFrame = field(default_factory=pd.DataFrame)
    metrics: dict[str, Any] = field(default_factory=dict)
    equity: pd.DataFrame = field(default_factory=pd.DataFrame)
    passive: dict[str, Any] = field(default_factory=dict)
    feature_ic: pd.DataFrame = field(default_factory=pd.DataFrame)
    feature_stability: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "config": self.config.as_dict(),
            "metrics": dict(self.metrics),
            "passive": dict(self.passive),
            "feature_stability": dict(self.feature_stability),
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# 数据与特征
# ---------------------------------------------------------------------------


def load_index_bars(path: str | Path | None = None) -> pd.DataFrame | None:
    """读取沪深300指数日线（scripts/fetch_index.py 产物）；不存在返回 None。"""

    candidates = [
        path,
        Path(__file__).resolve().parents[1] / "runtime" / "m6" / "index_000300.csv",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            frame = pd.read_csv(candidate)
            if "date" not in frame.columns or "close" not in frame.columns:
                continue
            frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
            for column in ("open", "high", "low", "close", "volume"):
                if column in frame.columns:
                    frame[column] = pd.to_numeric(frame[column], errors="coerce")
            return frame.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    return None


def _prepare_bars(bars: pd.DataFrame) -> pd.DataFrame:
    """规范化行情并构造复权价（adjclose = close × adj_factor）。"""

    if bars is None:
        raise ValueError("M6 需要行情数据")
    frame = normalize_bars(bars)
    if "adj_factor" in frame.columns:
        factor = pd.to_numeric(frame["adj_factor"], errors="coerce")
    else:
        factor = pd.Series(1.0, index=frame.index)
    n_filled = int((~np.isfinite(pd.to_numeric(factor, errors="coerce").to_numpy(dtype="float64"))).sum())
    factor = pd.to_numeric(factor, errors="coerce").where(lambda values: values > 0, 1.0).fillna(1.0)
    frame["adj_factor"] = factor.astype("float64")
    frame["raw_close"] = pd.to_numeric(frame["close"], errors="coerce")
    for column in ("open", "high", "low", "close"):
        frame[f"adj_{column}"] = pd.to_numeric(frame[column], errors="coerce") * frame["adj_factor"]
    if "amount" in frame.columns:
        frame["amount"] = pd.to_numeric(frame["amount"], errors="coerce")
    frame.attrs["adj_factor_filled"] = n_filled
    frame.attrs["price_basis"] = PRICE_BASIS
    return frame


def _cross_section_count(panel: pd.DataFrame, values: pd.Series) -> pd.Series:
    """当日有效截面标的数（逐行对齐）。"""

    counts = values.notna().groupby(panel["date"]).transform("sum")
    return pd.to_numeric(counts, errors="coerce").fillna(0.0)


def _rank_normalize(panel: pd.DataFrame, raw: str, target: str) -> None:
    """按日期截面 rank 分位（0~1）；当日截面标的数 < MIN_CROSS_SECTION 时置 NaN。"""

    values = pd.to_numeric(panel[raw], errors="coerce")
    ranks = values.groupby(panel["date"]).rank(pct=True)
    counts = _cross_section_count(panel, values)
    panel[target] = ranks.where(counts >= MIN_CROSS_SECTION)


def build_m6_panel(
    bars: pd.DataFrame,
    *,
    index_bars: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """构建 M6 面板：复权价特征（v1+v2）、6 种复权总收益标签、市场状态（沪深300）。"""

    frame = _prepare_bars(bars)
    panels: list[pd.DataFrame] = []
    for symbol, group in frame.groupby("symbol", sort=True):
        group = group.sort_values("date").reset_index(drop=True)
        if len(group) < 120:
            continue
        feature_input = pd.DataFrame(
            {
                "date": group["date"],
                "open": group["adj_open"],
                "high": group["adj_high"],
                "low": group["adj_low"],
                "close": group["adj_close"],
                "volume": group["volume"],
                "amount": group["amount"] if "amount" in group.columns else np.nan,
            }
        )
        panel = _symbol_raw_features(feature_input, str(symbol))
        if panel is None:
            continue
        adj_close = pd.to_numeric(group["adj_close"], errors="coerce").astype("float64")
        adj_open = pd.to_numeric(group["adj_open"], errors="coerce").astype("float64")
        adj_high = pd.to_numeric(group["adj_high"], errors="coerce").astype("float64")
        raw_close = pd.to_numeric(group["close"], errors="coerce").astype("float64")
        # 涨跌停距离：交易所规则口径，必须用**不复权** close/前收（复权价上 ±10%/±20% 规则无意义）
        try:
            board = guess_board(str(symbol))
        except ValueError:
            board = "MAIN"
        limit_pct = AStockRules.limit_pct(board, False)
        prev_raw_close = raw_close.shift(1)
        limit_up = (prev_raw_close * (1 + limit_pct)).round(2)
        limit_down = (prev_raw_close * (1 - limit_pct)).round(2)
        panel["raw_limit_up_distance"] = (limit_up - raw_close) / raw_close
        panel["raw_limit_down_distance"] = (raw_close - limit_down) / raw_close
        # 复权总收益标签（红线：shift(-h) 只用于标签/标签日期）
        for horizon in (1, 3, 5, 10):
            panel[f"future_ret_{horizon}"] = adj_close.shift(-horizon) / adj_close - 1.0
            panel[f"label_date_{horizon}"] = group["date"].shift(-horizon)
            # 不复权口径（仅供对照报告，不参与标签/训练）
            panel[f"future_ret_raw_{horizon}"] = raw_close.shift(-horizon) / raw_close - 1.0
        panel["next_gap"] = adj_open.shift(-1) / adj_close - 1.0
        panel["next_gap_raw"] = (
            pd.to_numeric(group["open"], errors="coerce").astype("float64").shift(-1) / raw_close - 1.0
        )
        panel["raw_near_high_250"] = adj_close / adj_high.rolling(250, min_periods=120).max() - 1.0
        panel["raw_close"] = raw_close
        panel["adj_close"] = adj_close
        panel["adj_factor"] = group["adj_factor"].to_numpy()
        panels.append(panel)
    if not panels:
        raise ValueError("M6 面板为空：行情历史不足")
    panel = pd.concat(panels, ignore_index=True)

    # 板块相对强弱（按代码前缀 board 分组）；复权收益口径
    sector = panel.groupby(["date", "sector"]).agg(
        sector_ret_5=("raw_ret_5", "mean"),
        sector_ret_20=("raw_ret_20", "mean"),
        sector_breadth=("raw_ret_5", lambda values: float((values > 0).mean())),
    ).reset_index()
    panel = panel.merge(sector, on=["date", "sector"], how="left")
    panel["raw_sector_rel_5"] = panel["raw_ret_5"] - panel["sector_ret_5"]
    panel["raw_sector_rel_20"] = panel["raw_ret_20"] - panel["sector_ret_20"]
    panel["raw_sector_breadth"] = panel["sector_breadth"]

    # 市场状态：优先沪深300，缺失则用自选池均值兜底；只用 <= 当日数据
    index_frame = index_bars if index_bars is not None else load_index_bars()
    if index_frame is not None and len(index_frame) >= 80:
        index = index_frame.copy()
        index_close = pd.to_numeric(index["close"], errors="coerce")
        index_ret = index_close.pct_change()
        market = pd.DataFrame(
            {
                "date": index["date"],
                "market_ret_20": index_close / index_close.shift(20) - 1.0,
                "market_ret_60": index_close / index_close.shift(60) - 1.0,
                "market_vol_20": index_ret.rolling(20, min_periods=20).std(ddof=1) * math.sqrt(252),
                "market_drawdown_60": index_close / index_close.rolling(60, min_periods=20).max() - 1.0,
                "market_above_ma20": (index_close > index_close.rolling(20, min_periods=20).mean()).astype(float),
            }
        )
        market["market_vol_pct"] = (
            market["market_vol_20"].rolling(250, min_periods=60).rank(pct=True).fillna(0.5)
        )
        panel = panel.merge(market, on="date", how="left")
        note_source = "沪深300"
    else:
        note_source = "自选池均值（指数数据缺失兜底）"
        market = panel.groupby("date").agg(
            market_ret_20=("raw_ret_20", "mean"),
            market_ret_60=("raw_ret_60", "mean") if "raw_ret_60" in panel.columns else ("raw_ret_20", "mean"),
            market_vol_20=("raw_volatility", "mean"),
        ).reset_index()
        market["market_vol_pct"] = (
            market["market_vol_20"].rolling(250, min_periods=60).rank(pct=True).fillna(0.5)
        )
        market["market_drawdown_60"] = 0.0
        market["market_above_ma20"] = 0.5
        panel = panel.merge(market, on="date", how="left")
    panel = panel.sort_values(["date", "symbol"]).reset_index(drop=True)
    # 指数缺失日（停牌/节假日）用前值填充；不做 bfill（避免用到未来）
    for column in ("market_ret_20", "market_ret_60", "market_vol_20", "market_vol_pct",
                   "market_drawdown_60", "market_above_ma20"):
        panel[column] = pd.to_numeric(panel[column], errors="coerce").ffill()
    panel["market_vol_pct"] = panel["market_vol_pct"].fillna(0.5)
    panel["market_above_ma20"] = panel["market_above_ma20"].fillna(0.5)
    for column in ("market_ret_20", "market_ret_60", "market_vol_20", "market_drawdown_60"):
        panel[column] = panel[column].fillna(0.0)

    # 截面 rank 化（v1 基础 + v2 扩充）
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
        "sector_rel_ret_5": "raw_sector_rel_5",
        "sector_rel_ret_20": "raw_sector_rel_20",
        "sector_breadth": "raw_sector_breadth",
        "limit_up_distance": "raw_limit_up_distance",
        "limit_down_distance": "raw_limit_down_distance",
        "vol_expand_streak": "raw_vol_expand_streak",
        "vp_divergence": "raw_vp_divergence",
        "near_high_250": "raw_near_high_250",
    }
    for target, source in mapping.items():
        if source in panel.columns:
            _rank_normalize(panel, source, target)
    # 市场状态：同日全市场相同，不做截面 rank，只压缩量纲
    panel["market_ret_20"] = (panel["market_ret_20"] * 20.0).clip(-3.0, 3.0)
    panel["market_ret_60"] = (panel["market_ret_60"] * 20.0).clip(-3.0, 3.0)
    panel["market_vol_20"] = (panel["market_vol_20"] * 50.0).clip(0.0, 5.0)
    panel["market_vol_pct"] = panel["market_vol_pct"].clip(0.0, 1.0)
    panel["market_drawdown_60"] = (panel["market_drawdown_60"] * 10.0).clip(-3.0, 0.0)
    panel["market_above_ma20"] = panel["market_above_ma20"].clip(0.0, 1.0)

    # 标签：全部用复权总收益
    for key, spec in M6_LABELS.items():
        if spec.kind == "binary" and spec.horizon == 1:
            target = panel["next_gap"]
            panel[f"label_{key}"] = np.where(target.notna(), (target >= spec.threshold).astype(float), np.nan)
            panel[f"label_date_{key}"] = panel["label_date_1"]
        else:
            target = panel[f"future_ret_{spec.horizon}"]
            label_date = panel[f"label_date_{spec.horizon}"]
            if spec.kind == "binary":
                panel[f"label_{key}"] = np.where(
                    target.notna(), (target >= spec.threshold).astype(float), np.nan
                )
                panel[f"label_date_{key}"] = label_date
            elif spec.kind == "three_class":
                panel[f"label_{key}"] = np.where(
                    target.notna(),
                    np.where(target > spec.threshold, 1.0, np.where(target < -spec.threshold, -1.0, 0.0)),
                    np.nan,
                )
                panel[f"label_date_{key}"] = label_date
            else:  # cross_section_top
                panel[f"label_date_{key}"] = label_date
                valid = target.notna()
                ranks = target[valid].groupby(panel.loc[valid, "date"]).rank(pct=True)
                counts = target[valid].groupby(panel.loc[valid, "date"]).transform("count")
                labels = pd.Series(np.nan, index=panel.index)
                # 严格大于分位（pct rank = k/N）：N=50 时取 41..50 名 = 恰好 20%；
                # 若用 >=0.8 会取到 11/50=22%，把正样本率抬到 20% 以上。
                top = (ranks > spec.quantile) & (counts >= MIN_CROSS_SECTION)
                labels.loc[top.index] = top.astype(float)
                panel[f"label_{key}"] = labels.to_numpy()

    feature_names = list(FEATURES_ALL)
    usable = panel[feature_names].notna().all(axis=1)
    # 截面规模审计（未过滤前统计）：不足 MIN_CROSS_SECTION 的日期/样本不参与截面排序与标签
    cs_size = panel.groupby("date")["symbol"].transform("nunique")
    below = cs_size < MIN_CROSS_SECTION
    size_by_date = panel.assign(_size=cs_size).groupby("date")["_size"].first()
    cs_audit = {
        "cross_section_min_required": MIN_CROSS_SECTION,
        "dates_below_min": int(panel.loc[below, "date"].nunique()),
        "rows_below_min": int(below.sum()),
        "cross_section_size_min": int(cs_size.min()),
        "cross_section_size_median": float(cs_size.median()),
        "cross_section_size_by_year": {
            int(year): int(round(float(value)))
            for year, value in size_by_date.groupby(
                pd.to_datetime(size_by_date.index).year
            ).median().items()
        },
    }
    panel = panel[usable].reset_index(drop=True)
    if panel.empty:
        raise ValueError("M6 面板没有特征齐全的样本")
    panel.attrs["market_source"] = note_source
    panel.attrs["price_basis"] = PRICE_BASIS
    panel.attrs["adj_factor_filled"] = frame.attrs.get("adj_factor_filled", 0)
    panel.attrs["cross_section_audit"] = cs_audit
    panel.attrs["feature_price_basis"] = {
        "复权价口径（adjclose=close×adj_factor）": [
            "rel_ret_5/20、vol_ratio、breakout_20、dist_ma20/60、atr_pct、turnover 分母、",
            "upper/lower_shadow、body、consec_up、ma20_slope、rsi、volatility、gap_open、",
            "vp_divergence、vol_expand_streak、near_high_250、板块/市场相对强弱",
        ],
        "不复权原始价口径（交易所规则/成交数据）": [
            "limit_up_distance / limit_down_distance（涨跌停价按不复权前收计算）",
            "volume / amount（原始成交量额）",
        ],
    }
    return panel


def label_summary(panel: pd.DataFrame) -> list[dict[str, Any]]:
    """标签矩阵的样本量与不平衡统计；同时给出复权/不复权口径的均值差（除权污染证据）。"""

    rows: list[dict[str, Any]] = []
    for key, spec in M6_LABELS.items():
        column = f"label_{key}"
        values = pd.to_numeric(panel[column], errors="coerce").dropna()
        entry: dict[str, Any] = {
            "label": key,
            "description": spec.label,
            "kind": spec.kind,
            "horizon": spec.horizon,
            "purge_days": spec.purge_days,
            "samples": int(len(values)),
            "dates": int(panel.loc[values.index, "date"].nunique()) if len(values) else 0,
            "Positive": None,
            "positive_rate": None,
        }
        hold = spec.horizon
        for basis in ("adj", "raw"):
            source = "next_gap" if (spec.kind == "binary" and hold == 1) else f"future_ret_{hold}"
            if basis == "raw":
                source = "next_gap_raw" if (spec.kind == "binary" and hold == 1) else f"future_ret_raw_{hold}"
            if source in panel.columns:
                series = pd.to_numeric(panel[source], errors="coerce")
                entry[f"mean_return_{basis}"] = _round(float(series.mean())) if series.notna().any() else None
                entry[f"std_return_{basis}"] = _round(float(series.std(ddof=1))) if series.notna().sum() > 1 else None
        adj_source = "next_gap" if (spec.kind == "binary" and hold == 1) else f"future_ret_{hold}"
        raw_source = "next_gap_raw" if (spec.kind == "binary" and hold == 1) else f"future_ret_raw_{hold}"
        if adj_source in panel.columns and raw_source in panel.columns:
            diff = (
                pd.to_numeric(panel[adj_source], errors="coerce")
                - pd.to_numeric(panel[raw_source], errors="coerce")
            ).abs()
            if diff.notna().any():
                entry["max_abs_diff_adj_vs_raw"] = _round(float(diff.max()))
                entry["p99_abs_diff_adj_vs_raw"] = _round(float(diff.quantile(0.99)))
                entry["n_abs_diff_gt_5pct"] = int((diff > 0.05).sum())
        if spec.kind == "binary" or spec.kind == "cross_section_top":
            entry["positive_rate"] = _round(float((values == 1.0).mean())) if len(values) else None
            entry["Positive"] = int((values == 1.0).sum())
        else:  # three_class
            entry["positive_rate"] = _round(float((values == 1.0).mean())) if len(values) else None
            entry["down_rate"] = _round(float((values == -1.0).mean())) if len(values) else None
            entry["flat_rate"] = _round(float((values == 0.0).mean())) if len(values) else None
            entry["Positive"] = int((values == 1.0).sum())
            entry["Negative"] = int((values == -1.0).sum())
            entry["Flat"] = int((values == 0.0).sum())
        if "label_date_" + key in panel.columns:
            try:
                dates = pd.to_datetime(panel["label_date_" + key], errors="coerce").dropna()
                entry["label_date_min"] = str(dates.min().date()) if len(dates) else None
                entry["label_date_max"] = str(dates.max().date()) if len(dates) else None
            except Exception:  # pragma: no cover
                pass
        if spec.kind == "cross_section_top":
            target = pd.to_numeric(panel[f"future_ret_{spec.horizon}"], errors="coerce")
            valid = target.notna()
            per_date = target[valid].groupby(panel.loc[valid, "date"]).count()
            below = per_date[per_date < MIN_CROSS_SECTION]
            entry["cs_min_required"] = MIN_CROSS_SECTION
            entry["cs_excluded_days"] = int(len(below))
            entry["cs_excluded_samples"] = int(below.sum())
            entry["cs_dates_used"] = int((per_date >= MIN_CROSS_SECTION).sum())
        rows.append(entry)
    return rows


# ---------------------------------------------------------------------------
# 统计工具
# ---------------------------------------------------------------------------


def _spearman(a: np.ndarray, b: np.ndarray) -> float | None:
    if len(a) < 3:
        return None
    ranks_a = pd.Series(a).rank().to_numpy()
    ranks_b = pd.Series(b).rank().to_numpy()
    if ranks_a.std() <= 0 or ranks_b.std() <= 0:
        return None
    return float(np.corrcoef(ranks_a, ranks_b)[0, 1])


def _mean_t_test(values: Sequence[float]) -> dict[str, Any]:
    """单样本 t 检验（H1: 均值 > 0）；返回 mean/t/p_two_sided/p_greater。"""

    array = np.asarray([value for value in values if value is not None], dtype="float64")
    array = array[np.isfinite(array)]
    if len(array) < 3:
        return {"mean": None, "t": None, "p_two_sided": None, "p_greater": None, "n": int(len(array))}
    mean = float(array.mean())
    std = float(array.std(ddof=1))
    if std <= 0:
        return {"mean": round(mean, 8), "t": None, "p_two_sided": None, "p_greater": None, "n": int(len(array))}
    t_stat = mean / (std / math.sqrt(len(array)))
    if SCIPY_AVAILABLE:
        result = _ttest_1samp(array, 0.0, alternative="greater")
        p_greater = float(result.pvalue)
        p_two = float(_ttest_1samp(array, 0.0).pvalue)
    else:  # pragma: no cover
        from math import erf, sqrt

        p_two = float(2 * (1 - 0.5 * (1 + erf(abs(t_stat) / sqrt(2)))))
        p_greater = p_two / 2 if t_stat > 0 else 1 - p_two / 2
    return {
        "mean": _round(mean),
        "t": _round(t_stat),
        "p_two_sided": _round(p_two),
        "p_greater": _round(p_greater),
        "n": int(len(array)),
    }


def _binom_greater_p(successes: int, trials: int, base_rate: float | None) -> dict[str, Any]:
    """合并样本二项检验：P(X >= successes | n, p0)（H1: 命中率 > 基础概率）。"""

    if trials <= 0 or base_rate is None:
        return {"successes": int(successes), "trials": int(trials), "base_rate": None, "p_greater": None}
    p0 = float(base_rate)
    if p0 <= 0.0:
        p_greater = 0.0 if successes > 0 else 1.0
        return {"successes": int(successes), "trials": int(trials), "base_rate": _round(p0), "p_greater": _round(p_greater)}
    if p0 >= 1.0:
        return {"successes": int(successes), "trials": int(trials), "base_rate": _round(p0), "p_greater": 1.0}
    if SCIPY_AVAILABLE:
        p_greater = float(_binomtest(int(successes), int(trials), p0, alternative="greater").pvalue)
    else:  # pragma: no cover - 正态近似 + 连续性校正
        mean = trials * p0
        std = math.sqrt(trials * p0 * (1 - p0))
        z = (successes - 0.5 - mean) / std if std > 0 else 0.0
        from math import erf, sqrt

        p_greater = float(1 - 0.5 * (1 + erf(z / sqrt(2))))
    return {
        "successes": int(successes),
        "trials": int(trials),
        "base_rate": _round(p0),
        "observed_rate": _round(successes / trials),
        "p_greater": _round(p_greater),
    }


def block_bootstrap_ci(
    values: Sequence[float],
    *,
    block: int = 5,
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int = 42,
) -> dict[str, Any]:
    """日度 IC 序列的**移动块 bootstrap** 均值 95% CI（块长 5 日，处理重叠样本自相关）。"""

    array = np.asarray([value for value in values if value is not None], dtype="float64")
    array = array[np.isfinite(array)]
    n = int(len(array))
    if n < block * 2:
        return {"n": n, "block": int(block), "n_boot": 0, "mean": _round(float(array.mean())) if n else None}
    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(n / block))
    max_start = n - block
    means = np.empty(n_boot, dtype="float64")
    for index in range(n_boot):
        starts = rng.integers(0, max_start + 1, size=n_blocks)
        sample = np.concatenate([array[start:start + block] for start in starts])[:n]
        means[index] = float(sample.mean())
    low, high = np.percentile(means, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return {
        "n": n,
        "block": int(block),
        "n_boot": int(n_boot),
        "seed": int(seed),
        "mean": _round(float(array.mean())),
        "boot_mean": _round(float(means.mean())),
        "boot_std": _round(float(means.std(ddof=1))),
        "ci_low": _round(float(low)),
        "ci_high": _round(float(high)),
        "significant_positive": bool(low > 0),
        "method": f"moving-block bootstrap（块长 {block} 日，{n_boot} 次，percentile 95% CI）",
    }


def multiple_comparison_analysis(
    daily_ic: Mapping[str, Sequence[float]],
    observed_best: float | None,
    *,
    block: int = 5,
    n_boot: int = 1000,
    seed: int = 42,
) -> dict[str, Any]:
    """量化“N 组配置取最优”的多重比较偏差。

    零分布：把每个配置的日度 IC 序列 **去均值**（即 H0: 真实 IC=0），对每条序列做 5 日块
    bootstrap 得到 H0 下的均值 IC；每次抽样取全部配置的最大值 → max-statistic 零分布。
    报告观测最优 RankIC 在该零分布下的 p 值，并与 Bonferroni / Sidak 校正对照。
    """

    curves: list[np.ndarray] = []
    for values in daily_ic.values():
        array = np.asarray([value for value in values if value is not None], dtype="float64")
        array = array[np.isfinite(array)]
        if len(array) >= block * 2:
            curves.append(array - float(array.mean()))
    n_configs = int(len(daily_ic))
    result: dict[str, Any] = {
        "n_configs": n_configs,
        "n_effective_curves": int(len(curves)),
        "bonferroni_alpha": _round(0.05 / n_configs) if n_configs else None,
        "sidak_alpha": _round(1 - (1 - 0.05) ** (1.0 / n_configs)) if n_configs else None,
        "block": int(block),
        "n_boot": int(n_boot),
        "statement": (
            "36 组（6 标签 × 2 特征集 × 3 模型）全部在**同一份 OOS 数据**上比较；"
            "“取最大 RankIC 的配置”天然存在选择偏差（winner's curse），"
            "因此除单配置检验外，另给出 max-statistic 零分布与 Bonferroni/Sidak 校正。"
        ),
    }
    if not curves or observed_best is None:
        return result
    rng = np.random.default_rng(seed)
    max_draws = np.empty(n_boot, dtype="float64")
    for index in range(n_boot):
        maxima = []
        for curve in curves:
            n = len(curve)
            n_blocks = int(np.ceil(n / block))
            max_start = n - block
            starts = rng.integers(0, max_start + 1, size=n_blocks)
            sample = np.concatenate([curve[start:start + block] for start in starts])[:n]
            maxima.append(float(sample.mean()))
        max_draws[index] = float(np.max(maxima))
    p_max = float((np.sum(max_draws >= observed_best) + 1) / (n_boot + 1))
    result.update(
        {
            "observed_best_rank_ic": _round(float(observed_best)),
            "null_max_mean": _round(float(max_draws.mean())),
            "null_max_std": _round(float(max_draws.std(ddof=1))),
            "null_max_p95": _round(float(np.percentile(max_draws, 95))),
            "p_max_statistic": _round(p_max),
            "significant_after_max_adjustment": bool(p_max < 0.05),
            "interpretation": (
                f"在 {len(curves)} 条去均值 IC 曲线上做 max-statistic 零分布（{n_boot} 次，5 日块）："
                f"H0 下“最优配置”RankIC 的期望 ≈ {float(max_draws.mean()):.4f}，95% 分位 ≈ "
                f"{float(np.percentile(max_draws, 95)):.4f}；观测值 {float(observed_best):.4f} 的校正 p = "
                f"{p_max:.4f}。"
            ),
        }
    )
    return result


def stratified_oos_stats(daily: pd.DataFrame, config: M6Config) -> dict[str, Any]:
    """分年度 / 分市场状态（沪深300 在 MA20 上/下）的 OOS 分层表现。"""

    if daily is None or daily.empty:
        return {}
    frame = daily.copy()
    frame["year"] = pd.to_datetime(frame["date"]).dt.year

    def _aggregate(subset: pd.DataFrame) -> dict[str, Any]:
        subset = subset.dropna(subset=["ic"]) if "ic" in subset.columns else subset
        hit_successes = float(subset["top_hits"].sum())
        hit_trials = float(subset["top_n_labeled"].sum())
        base_successes = float(subset["base_hits"].sum())
        base_trials = float(subset["base_n"].sum())
        base_rate = base_successes / base_trials if base_trials > 0 else None
        hit_rate = hit_successes / hit_trials if hit_trials > 0 else None
        binom = _binom_greater_p(int(hit_successes), int(hit_trials), base_rate)
        window_excess = subset["excess"].dropna() if "excess" in subset.columns else pd.Series(dtype="float64")
        return {
            "dates": int(subset["date"].nunique()),
            "hit_rate": _round(hit_rate),
            "base_rate": _round(base_rate),
            "hit_rate_lift": _round(hit_rate - base_rate) if hit_rate is not None and base_rate is not None else None,
            "hit_rate_p": binom.get("p_greater"),
            "rank_ic": _round(float(subset["ic"].dropna().mean())) if subset["ic"].notna().any() else None,
            "top_excess_gross": _round(float(window_excess.mean())) if len(window_excess) else None,
            "top_excess_net": _round(float(window_excess.mean()) - config.round_trip_cost) if len(window_excess) else None,
            "windows": int(len(window_excess)),
        }

    by_year = {}
    for year, subset in frame.groupby("year"):
        by_year[int(year)] = _aggregate(subset)
    by_state: dict[str, Any] = {}
    if "market_above_ma20" in frame.columns:
        state = pd.to_numeric(frame["market_above_ma20"], errors="coerce")
        by_state["above_ma20"] = _aggregate(frame[state >= 0.5])
        by_state["below_ma20"] = _aggregate(frame[state < 0.5])
    return {"by_year": by_year, "by_market_state": by_state}


# ---------------------------------------------------------------------------
# 模型与验证
# ---------------------------------------------------------------------------


def _build_model(config: M6Config) -> Any:
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
            class_weight="balanced",  # 显式处理样本不平衡
            early_stopping=False,  # 固定超参、确定性、避免额外验证切分
            random_state=config.random_state,
        )
    return HistGradientBoostingRegressor(
        max_iter=200,
        learning_rate=0.05,
        max_depth=3,
        l2_regularization=1.0,
        early_stopping=False,
        random_state=config.random_state,
    )


def _score(model: Any, config: M6Config, X: np.ndarray) -> np.ndarray:
    spec = M6_LABELS[config.label]
    if config.model == "gbdt_rank":
        return np.asarray(model.predict(X), dtype="float64")
    probabilities = model.predict_proba(X)
    if spec.kind == "three_class":
        classes = list(model.classes_)
        up = classes.index(1.0) if 1.0 in classes else len(classes) - 1
        down = classes.index(-1.0) if -1.0 in classes else 0
        return probabilities[:, up] - probabilities[:, down]
    return probabilities[:, 1]


def _fold_stats(valid: pd.DataFrame, config: M6Config) -> dict[str, Any]:
    """折内指标：**逐日** top_n/bottom_n 再对折内日期聚合（与 _daily_stats 口径一致）。

    旧口径对整折（~1000 stock-day）直接取 top_n，等于只挑 5 个样本，统计噪声极大且与
    日度/组合口径不一致；QA 复现该口径会导致分折 t 符号翻转。
    """

    top_n = max(1, int(config.top_n))
    bottom_n = max(1, int(config.bottom_n))
    hits: list[float] = []
    bases: list[float] = []
    top_rets: list[float] = []
    bot_rets: list[float] = []
    ics: list[float] = []
    for _, group in valid.groupby("date", sort=True):
        if len(group) < 3:
            continue
        ordered = group.sort_values("score", ascending=False)
        top = ordered.head(top_n)
        bottom = ordered.tail(bottom_n)
        top_labels = pd.to_numeric(top["label"], errors="coerce").dropna()
        base_labels = pd.to_numeric(group["label"], errors="coerce").dropna()
        if len(top_labels):
            hits.append(float((top_labels == 1.0).mean()))
        if len(base_labels):
            bases.append(float((base_labels == 1.0).mean()))
        top_rets.append(float(top["target_return"].mean()))
        bot_rets.append(float(bottom["target_return"].mean()))
        value = _spearman(group["score"].to_numpy(), group["target_return"].to_numpy())
        if value is not None:
            ics.append(value)
    return {
        "hit_rate": float(np.mean(hits)) if hits else None,
        "base_rate": float(np.mean(bases)) if bases else None,
        "top_ret": float(np.mean(top_rets)) if top_rets else None,
        "bottom_ret": float(np.mean(bot_rets)) if bot_rets else None,
        "ic": float(np.mean(ics)) if ics else None,
        "dates": len(top_rets),
    }


def _fold_feature_subset(train_part: pd.DataFrame, features: Sequence[str]) -> tuple[list[str], list[str]]:
    """逐折剔除近零方差/常数特征（只使用训练集信息，避免用到未来分布）。"""

    kept: list[str] = []
    dropped: list[str] = []
    for feature in features:
        values = pd.to_numeric(train_part[feature], errors="coerce")
        if values.notna().sum() < 10 or float(values.std(ddof=0) or 0.0) < 1e-12:
            dropped.append(feature)
        else:
            kept.append(feature)
    if not kept:  # 极端兜底：全部被剔除时保留第一个特征
        kept = [features[0]]
        dropped = [item for item in dropped if item != features[0]]
    return kept, dropped


def feature_rank_ic_table(
    panel: pd.DataFrame,
    *,
    horizons: Sequence[int] = (5, 10),
    features: Sequence[str] | None = None,
) -> pd.DataFrame:
    """全样本、模型无关的单因子 OOS 日度 RankIC（回答“有没有任何单因子边际”）。

    对每个特征 × horizon：逐交易日计算截面 Spearman IC（feature_t vs future_ret_h），
    再对日度 IC 序列给均值/标准差/ICIR/t 值/正 IC 比例/方向一致性。
    """

    names = list(features) if features is not None else list(FEATURES_ALL)
    rows: list[dict[str, Any]] = []
    for horizon in horizons:
        target_column = f"future_ret_{horizon}"
        if target_column not in panel.columns:
            continue
        for feature in names:
            if feature not in panel.columns:
                continue
            ic_values: list[float] = []
            for _, group in panel.groupby("date"):
                value = _spearman(
                    pd.to_numeric(group[feature], errors="coerce").to_numpy(dtype="float64"),
                    pd.to_numeric(group[target_column], errors="coerce").to_numpy(dtype="float64"),
                )
                if value is not None:
                    ic_values.append(value)
            if not ic_values:
                continue
            series = pd.Series(ic_values, dtype="float64")
            stats = _mean_t_test(series.tolist())
            rows.append(
                {
                    "feature": feature,
                    "feature_label": FEATURE_LABELS.get(feature, feature),
                    "horizon": int(horizon),
                    "dates": int(len(series)),
                    "rank_ic": _round(float(series.mean())),
                    "ic_std": _round(float(series.std(ddof=1))) if len(series) >= 2 else None,
                    "ic_ir": _round(float(series.mean() / series.std(ddof=1)))
                    if len(series) >= 2 and float(series.std(ddof=1)) > 0
                    else None,
                    "ic_t": stats["t"],
                    "ic_p_greater": stats["p_greater"],
                    "positive_ic_ratio": _round(float((series > 0).mean())),
                    "direction_consistency": _round(float((np.sign(series) == np.sign(series.mean())).mean())),
                }
            )
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame = frame.sort_values(["horizon", "rank_ic"], ascending=[True, False]).reset_index(drop=True)
    return frame


def run_m6_config(
    panel: pd.DataFrame,
    config: M6Config,
    *,
    passive_bars: pd.DataFrame | None = None,
    with_stability: bool = True,
    keep_predictions: bool = True,
) -> M6Result:
    """跑单个（标签 × 特征集 × 模型）配置的严格 walk-forward OOS。"""

    spec = M6_LABELS[config.label]
    features = [feature for feature in config.features if feature in panel.columns]
    label_column = f"label_{config.label}"
    label_date_column = f"label_date_{config.label}"
    target_return_column = "next_gap" if spec.horizon == 1 else f"future_ret_{spec.horizon}"
    holding = max(1, int(spec.horizon))
    labeled = panel[
        panel[label_column].notna()
        & panel[label_date_column].notna()
        & pd.to_datetime(panel[label_date_column], errors="coerce").notna()
    ].copy()
    labeled[label_date_column] = pd.to_datetime(labeled[label_date_column], errors="coerce").dt.date
    evaluation_dates = sorted(labeled["date"].unique())
    all_dates = sorted(panel["date"].unique())
    if len(evaluation_dates) < config.train_window + config.test_window:
        raise ValueError(f"{config.label}/{config.model}: 评估日期不足")
    date_order = {day: index for index, day in enumerate(all_dates)}
    purge = spec.purge_days
    folds: list[M6Fold] = []
    prediction_rows: list[pd.DataFrame] = []
    feature_ic_rows: list[dict[str, Any]] = []
    dropped_counter: dict[str, int] = {}
    fold_index = 0
    start = config.train_window
    while start < len(evaluation_dates):
        test_dates = evaluation_dates[start : start + config.test_window]
        if not test_dates:
            break
        test_start, test_end = test_dates[0], test_dates[-1]
        test_order = date_order.get(test_start, 0)
        train_start_date = all_dates[max(0, test_order - config.train_window)]
        embargo_end_date = all_dates[max(0, test_order - config.embargo)]
        # 无未来函数红线：
        #   (a) purge：训练样本的标签日 label_date 必须早于测试开始日；
        #   (b) embargo：训练特征日 <= 测试开始前第 embargo 个交易日；
        #   (c) 训练窗口长度 = train_window 个交易日。
        train_mask = (
            labeled[label_date_column].notna()
            & (labeled[label_date_column] < test_start)
            & (labeled["date"] >= train_start_date)
            & (labeled["date"] <= embargo_end_date)
        )
        train_part = labeled[train_mask]
        test_part = panel[panel["date"].isin(test_dates)]
        if len(train_part) < config.min_train_rows or len(test_part) < 20:
            fold_index += 1
            start += config.test_window
            continue
        fold_features, fold_dropped = _fold_feature_subset(train_part, features)
        for feature in fold_dropped:
            dropped_counter[feature] = dropped_counter.get(feature, 0) + 1
        X_train = train_part[fold_features].to_numpy(dtype="float64")
        if config.model == "gbdt_rank":
            ranks = train_part.groupby("date")[target_return_column].rank(pct=True)
            y_train = ranks.fillna(0.5).to_numpy(dtype="float64")
        else:
            y_train = train_part[label_column].to_numpy(dtype="float64")
            if len(np.unique(y_train)) < 2:
                fold_index += 1
                start += config.test_window
                continue
        model = _build_model(config)
        model.fit(X_train, y_train)
        X_test = test_part[fold_features].to_numpy(dtype="float64")
        scores = _score(model, config, X_test)
        test_frame = pd.DataFrame(
            {
                "date": test_part["date"].to_numpy(),
                "symbol": test_part["symbol"].to_numpy(),
                "fold": fold_index + 1,
                "score": scores,
                "label": pd.to_numeric(test_part[label_column], errors="coerce").to_numpy(),
                "target_return": pd.to_numeric(test_part[target_return_column], errors="coerce").to_numpy(),
                "future_ret": pd.to_numeric(test_part[target_return_column], errors="coerce").to_numpy(),
                "close_adj": pd.to_numeric(test_part["adj_close"], errors="coerce").to_numpy()
                if "adj_close" in test_part.columns
                else np.nan,
                "close_raw": pd.to_numeric(test_part["raw_close"], errors="coerce").to_numpy()
                if "raw_close" in test_part.columns
                else np.nan,
                "market_above_ma20": pd.to_numeric(test_part["market_above_ma20"], errors="coerce").to_numpy()
                if "market_above_ma20" in test_part.columns
                else np.nan,
            }
        )
        if with_stability:
            for feature in fold_features:
                ic_values: list[float] = []
                for _, group in test_part.groupby("date"):
                    value = _spearman(
                        pd.to_numeric(group[feature], errors="coerce").to_numpy(dtype="float64"),
                        pd.to_numeric(group[target_return_column], errors="coerce").to_numpy(dtype="float64"),
                    )
                    if value is not None:
                        ic_values.append(value)
                if ic_values:
                    feature_ic_rows.append(
                        {"fold": fold_index + 1, "feature": feature, "ic": float(np.mean(ic_values))}
                    )
        prediction_rows.append(test_frame)

        valid = test_frame.dropna(subset=["target_return"])
        stats = _fold_stats(valid, config)
        hit = stats["hit_rate"]
        base = stats["base_rate"]
        mean_return = stats["top_ret"]
        bottom_return = stats["bottom_ret"]
        ic_values = [stats["ic"]] if stats["ic"] is not None else []
        folds.append(
            M6Fold(
                fold=fold_index + 1,
                train_start=str(train_part["date"].min()),
                train_end=str(train_part["date"].max()),
                test_start=str(test_start),
                test_end=str(test_end),
                n_train=int(len(train_part)),
                n_test=int(len(test_part)),
                hit_rate=_round(hit),
                base_rate=_round(base),
                net_expectancy=_round(mean_return - config.round_trip_cost)
                if mean_return is not None
                else None,
                ic=_round(float(np.mean(ic_values))) if ic_values else None,
                long_short_spread=_round(mean_return - bottom_return)
                if mean_return is not None and bottom_return is not None
                else None,
                train_label_max=str(pd.to_datetime(train_part[label_date_column], errors="coerce").max().date()),
                train_date_max=str(max(train_part["date"])),
                train_label_min=str(pd.to_datetime(train_part[label_date_column], errors="coerce").min().date()),
                embargo_date=str(embargo_end_date),
                purge_days=int(purge),
                n_features=int(len(fold_features)),
                dropped_features=list(fold_dropped),
            )
        )
        fold_index += 1
        start += config.test_window

    if not folds:
        raise ValueError(f"{config.label}/{config.model}: 没有有效折（样本不足）")
    predictions = pd.concat(prediction_rows, ignore_index=True)
    daily = _daily_stats(predictions, config)
    metrics = _metrics(predictions, daily, folds, config, target_return_column, label_column, holding)
    equity = _simulate_topk(predictions, config, target_return_column)
    passive = passive_oos_benchmark(passive_bars, predictions, config)
    if with_stability:
        feature_ic = (
            pd.DataFrame(feature_ic_rows) if feature_ic_rows else pd.DataFrame(columns=["fold", "feature", "ic"])
        )
        stability = _feature_stability(feature_ic, dropped_counter, len(features))
    else:
        feature_ic = pd.DataFrame(columns=["fold", "feature", "ic"])
        stability = {"dropped_features": sorted(dropped_counter), "n_features": len(features)}
    metrics["strata"] = stratified_oos_stats(daily, config)
    diagnostics = {
        "daily_ic": [value for value in daily["ic"].tolist() if value is not None],
        "window_excess": [value for value in daily["excess"].dropna().tolist()],
        "window_dates": [str(day) for day in daily.loc[daily["excess"].notna(), "date"].tolist()],
    }
    result_predictions = predictions if keep_predictions else predictions.iloc[0:0].copy()
    return M6Result(
        config=config,
        folds=folds,
        predictions=result_predictions,
        metrics=metrics,
        equity=equity,
        passive=passive,
        feature_ic=feature_ic,
        feature_stability=stability,
        notes=[
            f"标签 {config.label}（{spec.label}，horizon={spec.horizon}，purge={purge} 日，embargo={config.embargo} 日）",
            "严格 OOS：训练标签 label_date < 测试开始；测试前 embargo 个交易日不参与训练",
            f"标签/特征价格口径：{PRICE_BASIS}=close×adj_factor（总收益）；不复权价仅保留对照",
            "价格类特征用复权价；涨跌停距离（交易所规则）与成交量/额为不复权原始口径",
            f"组合持有期 = 标签 horizon = {holding} 日，非重叠再平衡，成本 {config.round_trip_cost:.2%} × 换手率",
            "命中率 p 值使用合并样本二项检验（非重叠再平衡日样本，偏保守）",
        ],
        diagnostics=diagnostics,
    )


def _daily_stats(predictions: pd.DataFrame, config: M6Config) -> pd.DataFrame:
    """逐日 OOS 统计：IC、Top/Bottom/全池收益、命中数、超额（非重叠窗口）。"""

    spec = M6_LABELS[config.label]
    holding = max(1, int(spec.horizon))
    top_n = max(1, int(config.top_n))
    bottom_n = max(1, int(config.bottom_n))
    data = predictions.dropna(subset=["target_return"]).copy()
    data = data.sort_values(["date", "score"], ascending=[True, False])
    rows: list[dict[str, Any]] = []
    for day, group in data.groupby("date", sort=True):
        if len(group) < 3:
            continue
        top = group.head(top_n)
        bottom = group.tail(bottom_n)
        labels = pd.to_numeric(group["label"], errors="coerce")
        base_labels = labels.dropna()
        top_labels = pd.to_numeric(top["label"], errors="coerce")
        rows.append(
            {
                "date": day,
                "fold": int(group["fold"].iloc[0]),
                "ic": _spearman(group["score"].to_numpy(), group["target_return"].to_numpy()),
                "top_ret": float(top["target_return"].mean()),
                "bot_ret": float(bottom["target_return"].mean()),
                "pool_ret": float(group["target_return"].mean()),
                # 命中数 = label==1 的**计数**（三分类 label 为 -1/0/1，求和会被 -1 抵消）
                "top_hits": float((top_labels == 1.0).sum()),
                "top_n_labeled": float(top_labels.notna().sum()),
                "base_hits": float((base_labels == 1.0).sum()),
                "base_n": float(len(base_labels)),
                "n": int(len(group)),
                "market_above_ma20": float(group["market_above_ma20"].iloc[0])
                if "market_above_ma20" in group.columns
                else np.nan,
            }
        )
    daily = pd.DataFrame(rows)
    if daily.empty:
        return daily
    daily["excess"] = np.nan
    rebalance_dates = sorted(daily["date"].unique())[::holding]
    rebalance_set = set(rebalance_dates)
    mask = daily["date"].isin(rebalance_set)
    daily.loc[mask, "excess"] = daily.loc[mask, "top_ret"] - daily.loc[mask, "pool_ret"]
    return daily


def _metrics(
    predictions: pd.DataFrame,
    daily: pd.DataFrame,
    folds: list[M6Fold],
    config: M6Config,
    target_return_column: str,
    label_column: str,
    holding: int,
) -> dict[str, Any]:
    spec = M6_LABELS[config.label]
    ic_series = daily["ic"].dropna() if not daily.empty else pd.Series(dtype="float64")
    ic_stats = _mean_t_test(ic_series.tolist())
    ic_mean = ic_stats["mean"]
    ic_std = _round(float(ic_series.std(ddof=1))) if len(ic_series) >= 2 else None
    ic_ir = _round(ic_mean / ic_std) if ic_mean is not None and ic_std else None
    ic_ir_annual = _round(ic_ir * math.sqrt(252)) if ic_ir is not None else None

    # 合并样本命中率（全部 OOS 日期；含重叠，仅用于报告）
    all_successes = int(daily["top_hits"].sum()) if not daily.empty else 0
    all_trials = int(daily["top_n_labeled"].sum()) if not daily.empty else 0
    base_successes = int(daily["base_hits"].sum()) if not daily.empty else 0
    base_trials = int(daily["base_n"].sum()) if not daily.empty else 0
    base_rate = base_successes / base_trials if base_trials else None
    hit_rate = all_successes / all_trials if all_trials else None
    binom_all = _binom_greater_p(all_successes, all_trials, base_rate)
    # 非重叠再平衡日样本（gate 用，偏保守）
    window = daily[daily["excess"].notna()] if not daily.empty else daily
    win_successes = int(window["top_hits"].sum()) if not window.empty else 0
    win_trials = int(window["top_n_labeled"].sum()) if not window.empty else 0
    win_base_successes = int(window["base_hits"].sum()) if not window.empty else 0
    win_base_trials = int(window["base_n"].sum()) if not window.empty else 0
    win_base_rate = win_base_successes / win_base_trials if win_base_trials else None
    binom_window = _binom_greater_p(win_successes, win_trials, win_base_rate)

    fold_hit = np.asarray([fold.hit_rate for fold in folds if fold.hit_rate is not None], dtype="float64")
    fold_base = np.asarray([fold.base_rate for fold in folds if fold.base_rate is not None], dtype="float64")
    fold_ics = np.asarray([fold.ic for fold in folds if fold.ic is not None], dtype="float64")
    if len(fold_hit) and len(fold_base):
        diff = fold_hit - fold_base
        fold_test = _mean_t_test(diff.tolist())
    else:
        fold_test = {"mean": None, "t": None, "p_two_sided": None, "p_greater": None, "n": 0}

    excess_stats = _mean_t_test(window["excess"].tolist()) if not window.empty else {"mean": None, "t": None, "p_greater": None, "n": 0}
    top_ret_stats = _mean_t_test(window["top_ret"].tolist()) if not window.empty else {"mean": None, "t": None}
    pool_ret_stats = _mean_t_test(window["pool_ret"].tolist()) if not window.empty else {"mean": None, "t": None}
    spread_series = daily["top_ret"] - daily["bot_ret"] if not daily.empty else pd.Series(dtype="float64")
    spread_stats = _mean_t_test(spread_series.tolist())

    expectancy = [
        fold.net_expectancy for fold in folds if fold.net_expectancy is not None
    ]
    metrics = {
        "label": config.label,
        "feature_set": config.feature_set,
        "model": config.model,
        "holding_days": int(holding),
        "purge_days": int(spec.purge_days),
        "embargo_days": int(config.embargo),
        "folds": len(folds),
        "oos_samples": int(len(predictions)),
        "oos_dates": int(predictions["date"].nunique()),
        "oos_start": str(min(predictions["date"])) if len(predictions) else None,
        "oos_end": str(max(predictions["date"])) if len(predictions) else None,
        # 命中率（合并样本）
        "hit_rate": _round(hit_rate),
        "base_rate": _round(base_rate),
        "hit_rate_lift": _round(hit_rate - base_rate) if hit_rate is not None and base_rate is not None else None,
        "hit_rate_p": binom_window["p_greater"],  # gate 口径：非重叠再平衡日 + 合并样本二项检验
        "hit_rate_binom_p": binom_window["p_greater"],  # 契约别名（合并样本二项检验）
        "hit_rate_p_overlap": binom_all["p_greater"],  # 全部 OOS 日期（含重叠，仅供参考）
        "hit_rate_binom_p_overlap": binom_all["p_greater"],
        "hit_rate_overlap_rate": binom_all.get("observed_rate"),
        "hit_rate_window_rate": binom_window.get("observed_rate"),
        "hit_rate_window_base_rate": binom_window.get("base_rate"),
        "hit_rate_window_pairs": [binom_window["successes"], binom_window["trials"]],
        "hit_rate_pairs": [binom_all["successes"], binom_all["trials"]],
        "hit_rate_t": fold_test["t"],
        "hit_rate_fold_t_test_p": fold_test["p_two_sided"],
        "hit_rate_fold_p_greater": fold_test["p_greater"],
        "hit_rate_folds": int(fold_test["n"]),
        # 收益/超额（按标签 horizon 的非重叠窗口；与 gate 口径一致）
        "net_expectancy": _round(top_ret_stats["mean"] - config.round_trip_cost)
        if top_ret_stats["mean"] is not None
        else None,
        "top_return_mean": top_ret_stats["mean"],
        "pool_return_mean": pool_ret_stats["mean"],
        "top_excess_gross": excess_stats["mean"],
        "top_excess_net": _round(excess_stats["mean"] - config.round_trip_cost)
        if excess_stats["mean"] is not None
        else None,
        "top_excess_vs_passive": _round(excess_stats["mean"] - config.round_trip_cost)
        if excess_stats["mean"] is not None
        else None,  # 契约别名：Top 组扣费后相对全池等权基准的超额
        "top_excess_vs_passive_gross": excess_stats["mean"],
        "passive_basis": "同期全池等权 h 日收益（非重叠窗口，未计成本）",
        "top_excess_t": excess_stats["t"],
        "top_excess_p_greater": excess_stats["p_greater"],
        "excess_windows": int(excess_stats["n"]),
        # IC / 多空
        "rank_ic": ic_mean,
        "ic_mean": ic_mean,
        "ic_std": ic_std,
        "ic_ir": ic_ir,
        "ic_ir_annual": ic_ir_annual,
        "ic_t": ic_stats["t"],
        "ic_p_greater": ic_stats["p_greater"],
        "positive_ic_ratio": _round(float((ic_series > 0).mean())) if len(ic_series) else None,
        "ic_direction_consistency": _round(
            float((np.sign(fold_ics) == np.sign(fold_ics.mean())).mean())
        ) if len(fold_ics) else None,
        "fold_ic_mean": _round(float(fold_ics.mean())) if len(fold_ics) else None,
        "fold_ic_std": _round(float(fold_ics.std(ddof=1))) if len(fold_ics) >= 2 else None,
        "fold_ic_positive_ratio": _round(float((fold_ics > 0).mean())) if len(fold_ics) else None,
        "long_short_spread": spread_stats["mean"],
        "long_short_t": spread_stats["t"],
        "positive_folds_pct": _round(float((np.asarray(expectancy) > 0).mean())) if expectancy else None,
        "round_trip_cost": config.round_trip_cost,
        "cost_model": "round_trip_cost × 换手率（首期全换手；非重叠再平衡）",
        "target_return_column": target_return_column,
    }
    metrics["rank_ic_icir_verdict"] = _ic_verdict(metrics)
    return metrics


def _ic_verdict(metrics: Mapping[str, Any]) -> str:
    rank_ic = metrics.get("rank_ic")
    if rank_ic is None:
        return "无 IC 数据"
    if rank_ic > 0.03:
        return "RankIC > 0.03（达到门槛线）"
    if rank_ic > 0:
        return "RankIC 为正但 < 0.03（未达门槛线）"
    return "RankIC ≤ 0（方向错误）"


def _simulate_topk(predictions: pd.DataFrame, config: M6Config, target_return_column: str) -> pd.DataFrame:
    """Top-k 组合：持有期 = 标签 horizon，**非重叠再平衡**，按实际换手扣双边成本。"""

    spec = M6_LABELS[config.label]
    holding = max(1, int(spec.horizon))
    top_n = max(1, int(config.top_n))
    dates = sorted(predictions["date"].unique())
    rebalance_dates = dates[::holding]
    rows: list[dict[str, Any]] = []
    equity = 1.0
    equity_gross = 1.0
    previous_names: set[str] = set()
    previous_turnover: float | None = None
    for day in rebalance_dates:
        day_frame = predictions[predictions["date"] == day].dropna(subset=["target_return"])
        if len(day_frame) < top_n:
            continue
        top = day_frame.nlargest(top_n, "score")
        names = set(top["symbol"].astype(str))
        turnover = 1.0 if not previous_names else len(names - previous_names) / float(len(names))
        cost = config.round_trip_cost * turnover
        gross = float(pd.to_numeric(top["target_return"], errors="coerce").mean())
        net = gross - cost
        equity_gross *= 1.0 + gross
        equity *= max(1.0 + net, 0.0)
        rows.append(
            {
                "date": day,
                "top_return_gross": gross,
                "cost": cost,
                "turnover": turnover,
                "net_return": net,
                "equity_gross": equity_gross,
                "equity": equity,
                "n_top": int(len(top)),
                "top_symbols": ",".join(sorted(names)),
            }
        )
        previous_names = names
        previous_turnover = turnover
    if not rows:
        return pd.DataFrame(columns=["trade_date", "equity", "equity_gross", "drawdown", "cash", "position_value"])
    frame = pd.DataFrame(rows)
    equity_frame = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(frame["date"]).dt.date,
            "equity": frame["equity"] * 100_000.0,
            "equity_gross": frame["equity_gross"] * 100_000.0,
            "net_return": frame["net_return"],
            "top_return_gross": frame["top_return_gross"],
            "cost": frame["cost"],
            "turnover": frame["turnover"],
            "cash": 0.0,
            "position_value": frame["equity"] * 100_000.0,
        }
    )
    equity_frame["drawdown"] = equity_frame["equity"] / equity_frame["equity"].cummax() - 1.0
    equity_frame["drawdown_gross"] = equity_frame["equity_gross"] / equity_frame["equity_gross"].cummax() - 1.0
    if len(equity_frame) >= 2:
        metrics = compute_metrics(equity_frame, pd.DataFrame(), initial_cash=100_000.0)
        gross_metrics = compute_metrics(
            equity_frame.rename(columns={"equity": "equity_gross"}).assign(
                equity=equity_frame["equity_gross"].to_numpy()
            ),
            pd.DataFrame(),
            initial_cash=100_000.0,
        )
    else:
        metrics = {}
        gross_metrics = {}
    equity_frame.attrs["metrics"] = metrics
    equity_frame.attrs["gross_metrics"] = gross_metrics
    equity_frame.attrs["total_cost"] = float(frame["cost"].sum())
    equity_frame.attrs["mean_turnover"] = float(frame["turnover"].mean())
    return equity_frame


def _feature_stability(
    feature_ic: pd.DataFrame,
    dropped_counter: Mapping[str, int] | None = None,
    n_features: int | None = None,
) -> dict[str, Any]:
    if feature_ic is None or feature_ic.empty:
        return {
            "dropped_features": sorted(dropped_counter or {}),
            "n_features": n_features,
            "top_features": [],
        }
    table: list[dict[str, Any]] = []
    for feature, group in feature_ic.groupby("feature"):
        values = group["ic"].dropna().to_numpy(dtype="float64")
        if len(values) == 0:
            continue
        mean = float(values.mean())
        consistency = float((np.sign(values) == np.sign(mean)).mean()) if mean != 0 else 1.0
        table.append(
            {
                "feature": feature,
                "feature_label": FEATURE_LABELS.get(feature, feature),
                "mean_ic": _round(mean),
                "ic_std": _round(float(values.std(ddof=1))) if len(values) >= 2 else None,
                "direction_consistency": _round(consistency),
                "folds": int(len(values)),
                "abs_mean_ic": _round(abs(mean)),
            }
        )
    table = sorted(table, key=lambda item: -abs(item["mean_ic"] or 0.0))
    return {
        "dropped_features": sorted(dropped_counter or {}),
        "n_features": n_features,
        "n_kept_features": len(table),
        "top_features": table[:12],
        "mean_direction_consistency": _round(
            float(
                np.mean(
                    [item["direction_consistency"] for item in table if item["direction_consistency"] is not None]
                )
            )
        )
        if table
        else None,
    }


def passive_oos_benchmark(
    bars: pd.DataFrame | None,
    predictions: pd.DataFrame,
    config: M6Config,
) -> dict[str, Any]:
    """同仓位被动基准（OOS 区间）：等权买入持有 + 与策略同节奏的非重叠窗口对照。"""

    if bars is None or predictions is None or predictions.empty:
        return {}
    frame = _prepare_bars(bars)
    start = pd.Timestamp(predictions["date"].min())
    end = pd.Timestamp(predictions["date"].max())
    frame = frame[(frame["date"] >= start.date()) & (frame["date"] <= end.date())]
    if frame.empty:
        return {}
    closes = frame.pivot_table(index="date", columns="symbol", values="adj_close", aggfunc="last")
    closes = closes.sort_index().ffill()
    # 被动基准标的集合 = OOS 预测里出现过的标的集合（不得含未来才进入面板的标的）
    prediction_symbols = {str(symbol) for symbol in predictions["symbol"].unique()}
    keep_columns = [column for column in closes.columns if str(column) in prediction_symbols]
    closes = closes[keep_columns]
    if closes.shape[1] == 0:
        return {}
    if len(closes) < 2:
        return {}
    returns = closes.pct_change().fillna(0.0)
    equal_weight = returns.mean(axis=1)
    equity = (1.0 + equal_weight).cumprod() * 100_000.0
    equity_frame = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(equity.index).date,
            "equity": equity.to_numpy(),
            "cash": 0.0,
            "position_value": equity.to_numpy(),
        }
    )
    equity_frame["drawdown"] = equity_frame["equity"] / equity_frame["equity"].cummax() - 1.0
    metrics = compute_metrics(equity_frame, pd.DataFrame(), initial_cash=100_000.0)

    # 与策略同节奏（非重叠、持有期=horizon）的被动窗口收益
    spec = M6_LABELS[config.label]
    holding = max(1, int(spec.horizon))
    index_positions = {day: position for position, day in enumerate(closes.index)}
    window_returns: list[float] = []
    window_dates: list[Any] = []
    for day in sorted(predictions["date"].unique()):
        position = index_positions.get(day)
        if position is None or position + holding >= len(closes):
            continue
        start_prices = closes.iloc[position]
        end_prices = closes.iloc[position + holding]
        valid = start_prices.notna() & end_prices.notna() & (start_prices > 0)
        if valid.sum() == 0:
            continue
        window_returns.append(
            float((end_prices[valid] / start_prices[valid] - 1.0).mean())
        )
        window_dates.append(day)
    window_frame = pd.DataFrame({"date": window_dates, "pool_return": window_returns})
    window_equity = (1.0 + window_frame["pool_return"]).cumprod() * 100_000.0 if len(window_frame) else pd.Series(dtype="float64")
    passive_window: dict[str, Any] = {
        "windows": int(len(window_frame)),
        "mean_window_return": _round(float(window_frame["pool_return"].mean())) if len(window_frame) else None,
        "total_return": _round(float(window_equity.iloc[-1] / 100_000.0 - 1.0)) if len(window_equity) else None,
        "max_drawdown": _round(
            float((window_equity / window_equity.cummax() - 1.0).min())
        )
        if len(window_equity)
        else None,
        "holding_days": int(holding),
        "method": f"等权全池买入持有 {holding} 日（与策略非重叠窗口同节奏，未计成本）",
    }
    return {
        "method": "同仓位被动：OOS 区间内全部标的每日等权持有（复权总收益口径，未计交易成本）",
        "start": str(start.date()),
        "end": str(end.date()),
        "total_return": _round(metrics.get("total_return")),
        "annual_return": _round(metrics.get("annual_return")),
        "max_drawdown": _round(metrics.get("max_drawdown")),
        "sharpe": _round(metrics.get("sharpe")),
        "symbols": int(closes.shape[1]),
        "price_basis": PRICE_BASIS,
        "window_matched": passive_window,
    }


def gate_check(result: M6Result) -> dict[str, Any]:
    """M6 通过门槛（严格，不放宽）：三项全过才 usable=True，并逐项写清未过项。"""

    metrics = result.metrics
    rank_ic = metrics.get("rank_ic")
    excess = metrics.get("top_excess_vs_passive")
    if excess is None:
        excess = metrics.get("top_excess_net")
    if excess is None:
        excess = metrics.get("net_expectancy")  # 兼容旧版/假结果构造
    hit_p = metrics.get("hit_rate_p")
    if hit_p is None:
        hit_p = metrics.get("hit_rate_binom_p")
    if hit_p is None:
        hit_p = metrics.get("hit_rate_fold_t_test_p")
    if hit_p is None:
        hit_p = metrics.get("hit_p")
    hit_rate = metrics.get("hit_rate")
    base_rate = metrics.get("base_rate")
    lift = metrics.get("hit_rate_lift")
    if lift is None and hit_rate is not None and base_rate is not None:
        lift = float(hit_rate) - float(base_rate)
    criteria = {
        "rank_ic_gt_0.03": bool(rank_ic is not None and float(rank_ic) > 0.03),
        "top_excess_gt_0": bool(excess is not None and float(excess) > 0),
        "hit_rate_significant": bool(
            hit_p is not None and float(hit_p) < 0.05 and lift is not None and float(lift) > 0
        ),
    }
    failed = [name for name, passed in criteria.items() if not passed]
    detail: dict[str, Any] = {
        "rank_ic": rank_ic,
        "threshold_rank_ic": 0.03,
        "top_excess_vs_passive": _round(excess) if excess is not None else None,
        "top_excess_net": _round(excess) if excess is not None else None,
        "threshold_top_excess": 0.0,
        "hit_rate": hit_rate,
        "base_rate": base_rate,
        "hit_rate_lift": _round(lift) if lift is not None else None,
        "hit_rate_p": hit_p,
        "hit_rate_binom_p": hit_p,
        "threshold_hit_p": 0.05,
        "hit_rate_p_overlap": metrics.get("hit_rate_p_overlap"),
        "hit_rate_fold_t_test_p": metrics.get("hit_rate_fold_t_test_p"),
        "excess_windows": metrics.get("excess_windows"),
        "holding_days": metrics.get("holding_days"),
        "excess_basis": metrics.get("passive_basis"),
    }
    return {
        "usable": bool(all(criteria.values())),
        "criteria": criteria,
        "failed": failed,
        "detail": detail,
        "note": (
            "通过 M6 门槛（仍需扩大样本/多市场复核）"
            if all(criteria.values())
            else "未通过 M6 门槛：维持研究阶段，不得接入推荐/UI 宣称有效；未过项：" + "、".join(failed)
        ),
    }


def _round(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, 6)

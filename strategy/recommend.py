"""候选推荐引擎（R4）：输入策略 + 自选池 + 日期，输出带**得分分解理由**的 top-N。

供 UI（策略页/回测页/自动化页）展示“今天该关注哪些标的、为什么”。设计：

* **只读历史**：只使用 ``as_of`` 当日及以前的数据（不含未来数据）；
* **单一因子来源**：打分复用 :mod:`strategy.factors`（与策略线/ML 打分同口径），
  避免“推荐说一套、回测跑另一套”；
* **策略感知**：不同策略用各自的打分逻辑与理由文案：:

      value_quality     质量+动量+低波动+回撤可控（月频组合，输出完整排名）
      breakout_abnormal 量比/突破/温和涨幅/未暴涨（日频，输出通过项 + 观察名单）
      multi_factor      动量 z − 波动 z
      ml_ranker         walk-forward ML 概率（自动训练，样本不足时降级为动量排序）
      其它策略          按 20 日动量排序（标注“非排序型策略，仅供参考”）

* **可选 ML 混合**：``use_ml=True`` 时把 ML 概率按 ``ml_weight`` 混入基础得分。

用法::

    from datetime import date
    from strategy.recommend import recommend

    picks = recommend("value_quality", universe, date(2025, 12, 31), top_n=10)
    for pick in picks:
        print(pick.rank, pick.symbol, round(pick.score, 3), pick.reason)

    # 直接给行情（离线/测试）
    picks = recommend("breakout_abnormal", universe, "2025-12-31", bars=bars)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from core.models import Side
from strategy.base import StrategyError, get_strategy
from strategy.factors import (
    BreakoutParams,
    DEFAULT_FACTOR_CONFIG,
    FEATURE_LABELS,
    breakout_reason,
    breakout_signal,
    compute_breakout_features,
    compute_features,
    multi_factor_scores,
    score_value_quality,
    value_quality_components,
)

logger = logging.getLogger(__name__)

__all__ = [
    "Recommendation",
    "recommend",
    "recommend_dataframe",
    "recent_recommend",
    "resolve_market_universe",
    "screen_market",
    "screen_market_result",
    "SUPPORTED_STRATEGIES",
    "SHORT_TERM_KEYS",
]

#: 支持专属打分逻辑的策略（其余策略走通用动量排序）
SUPPORTED_STRATEGIES: tuple[str, ...] = (
    "value_quality",
    "breakout_abnormal",
    "multi_factor",
    "ml_ranker",
    "short_term_v1",
)

#: 短线推荐别名（recommend() 的 strategy_key 可用这些）
SHORT_TERM_KEYS: tuple[str, ...] = ("short_term", "short_term_v1", "recent", "short")

#: 研究阶段统一声明（未通过样本外验证的模型/规则，UI 必须原样展示）
RESEARCH_STAGE_NOTE = (
    "研究阶段：短线模型 v1 未通过样本外验证（OOS 命中率≈基础概率、扣费期望为负），"
    "结果仅供研究，不构成买入建议，禁止用于实盘；详见 docs/SHORT_TERM_MODEL_V1.md"
)

#: 单标的出因子所需最少历史 bar（与 factors 默认窗口一致）
_MIN_HISTORY = DEFAULT_FACTOR_CONFIG.required_rows


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Recommendation:
    """一条候选（UI 直接渲染；``as_dict`` 可 JSON 序列化）。

    ``name`` 为中文名称，可为 ``None``（UI 需显示中文占位 ``—``）；``as_dict`` 会过滤
    空值/字符串 ``"None"``，保证不会把 ``"None"`` 渲染成假名称。
    """

    symbol: str
    strategy_key: str
    as_of: date
    rank: int
    score: float
    reason: str
    qualified: bool = True
    factors: Mapping[str, float] = field(default_factory=dict)
    components: Mapping[str, float] = field(default_factory=dict)
    ml_proba: float | None = None
    note: str = ""
    name: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": _clean_name(self.name),
            "strategy_key": self.strategy_key,
            "as_of": self.as_of.isoformat() if isinstance(self.as_of, date) else str(self.as_of),
            "rank": self.rank,
            "score": round(float(self.score), 6),
            "qualified": bool(self.qualified),
            "reason": self.reason,
            "factors": {key: round(float(value), 6) for key, value in self.factors.items()},
            "components": {key: round(float(value), 6) for key, value in self.components.items()},
            "ml_proba": None if self.ml_proba is None else round(float(self.ml_proba), 6),
            "note": self.note,
        }


def _clean_name(value: Any) -> str | None:
    """名称清洗：None/空/字符串 "None"/"nan" → None；其余原样返回。"""

    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("none", "nan", "null"):
        return None
    return text


def _lookup_names(symbols: Sequence[str]) -> dict[str, str]:
    """从数据层取中文名称（失败/无名称时返回空 dict，不影响推荐主流程）。"""

    cleaned = [str(item) for item in symbols if str(item).strip()]
    if not cleaned:
        return {}
    try:
        from data.symbols import names_of  # type: ignore

        mapping = names_of(cleaned) or {}
        return {
            str(symbol): str(name).strip()
            for symbol, name in mapping.items()
            if _clean_name(name) is not None
        }
    except Exception:  # noqa: BLE001 - 名称只是展示字段，取不到不影响推荐
        return {}


def attach_names(recommendations: list[Recommendation]) -> list[Recommendation]:
    """给推荐列表补中文名称（已有名称保持不变；无名称查询数据层，失败保持 None）。"""

    if not recommendations:
        return recommendations
    missing = [item.symbol for item in recommendations if _clean_name(item.name) is None]
    names = _lookup_names(missing) if missing else {}
    if not names:
        return recommendations
    return [
        item if _clean_name(item.name) is not None or item.symbol not in names else replace(item, name=names[item.symbol])
        for item in recommendations
    ]


# ---------------------------------------------------------------------------
# 数据准备
# ---------------------------------------------------------------------------


def _parse_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return pd.Timestamp(value).date()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"as_of 日期非法：{value!r}（应为 YYYY-MM-DD）") from exc


def _prepare(
    universe: Sequence[str],
    as_of: date,
    *,
    bars: Any = None,
    data_source: Any = None,
    lookback_days: int = 550,
):
    """用回测引擎同一套清洗逻辑准备行情（保证推荐与回测口径一致）。"""

    from backtest.engine import BacktestEngine

    symbols = [str(item).strip() for item in universe if str(item).strip()]
    if not symbols:
        raise ValueError("自选池为空：请至少提供 1 个标的")
    symbols = list(dict.fromkeys(symbols))
    start = as_of - timedelta(days=max(180, int(lookback_days)))
    engine = BacktestEngine(data_source=data_source)
    prepared = engine.prepare_market_data(symbols, start, as_of, bars=bars)
    if not prepared.frames:
        raise ValueError(
            f"未获取到 {start} ~ {as_of} 的行情数据（标的：{'、'.join(symbols)}）；"
            "请先拉取数据或传入 bars"
        )
    return prepared


def _history_until(prepared: Any, symbol: str, as_of: date) -> tuple[pd.DataFrame | None, date | None]:
    """取某标的截至 ``as_of`` 的历史（as_of 非交易日时取之前最近交易日）。

    日期比较统一转成 ``datetime.date``，避免 ``datetime64[ns]`` 与 ``date`` 直接比较
    触发 ``pandas.errors.InvalidComparison``（同时保证“只用当日及以前数据”语义）。
    """

    frame = prepared.frames.get(symbol)
    if frame is None or len(frame) == 0:
        return None, None
    day_values = pd.to_datetime(frame["date"], errors="coerce").dt.date.tolist()
    cutoff = None
    for index, day in enumerate(day_values):
        if day is not None and not pd.isna(day) and day <= as_of:
            cutoff = index
        else:
            break
    if cutoff is None:
        return None, None
    return frame.iloc[: cutoff + 1], day_values[cutoff]


def _params_for(strategy_key: str, params: Mapping[str, Any] | None) -> dict[str, Any]:
    """用策略注册表校验参数（补默认值），保证推荐与回测参数口径一致。"""

    if strategy_key == "ml_ranker":
        return dict(params or {})
    try:
        strategy_cls = get_strategy(strategy_key)
        return strategy_cls.validate_params(params)
    except StrategyError as exc:
        raise ValueError(f"策略参数非法：{exc}") from exc


# ---------------------------------------------------------------------------
# 各策略打分
# ---------------------------------------------------------------------------


def _score_value_quality(
    histories: Mapping[str, pd.DataFrame],
    params: Mapping[str, Any],
    as_of: date,
    top_n: int,
) -> list[Recommendation]:
    features_by_symbol: dict[str, dict[str, float]] = {}
    for symbol, history in histories.items():
        features = compute_features(history)
        if features is not None:
            features_by_symbol[symbol] = features
    if not features_by_symbol:
        return []
    scores = score_value_quality(features_by_symbol)
    components = value_quality_components(features_by_symbol)
    preferred_top_n = int(params.get("top_n", 15))
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    recommendations: list[Recommendation] = []
    for rank, (symbol, score) in enumerate(ranked[: max(top_n, 1)], start=1):
        component = components[symbol]
        parts = [
            f"{FEATURE_LABELS.get('momentum_120', '120日动量')} z={component['momentum_120']:+.2f}",
            f"趋势稳定 z={component['trend_stability_60']:+.2f}",
            f"低波动 z={component['low_volatility_60']:+.2f}",
            f"回撤可控 z={component['low_drawdown_120']:+.2f}",
            f"上涨占比 z={component['positive_ratio_60']:+.2f}",
        ]
        recommendations.append(
            Recommendation(
                symbol=symbol,
                strategy_key="value_quality",
                as_of=as_of,
                rank=rank,
                score=float(score),
                qualified=rank <= preferred_top_n,
                reason=(
                    f"价值质量综合得分 {score:+.2f}（截面第 {rank}/{len(ranked)}）："
                    + "、".join(parts)
                ),
                factors=features_by_symbol[symbol],
                components=component,
                note=(
                    f"该策略默认持有前 {preferred_top_n} 只，等权月频调仓"
                    if rank > preferred_top_n
                    else "入选默认持仓名单（前 top_n 名）"
                ),
            )
        )
    return recommendations


def _score_breakout_abnormal(
    histories: Mapping[str, pd.DataFrame],
    params: Mapping[str, Any],
    as_of: date,
    top_n: int,
) -> list[Recommendation]:
    breakout_params = BreakoutParams(
        breakout_window=int(params.get("breakout_window", 20)),
        volume_ratio_min=float(params.get("volume_ratio_min", 1.5)),
        min_gain=float(params.get("min_gain", 2.0)) / 100.0,
        max_gain=float(params.get("max_gain", 9.5)) / 100.0,
        max_recent_gain=float(params.get("max_recent_gain", 20.0)) / 100.0,
    )
    candidates: list[dict[str, Any]] = []
    for symbol, history in histories.items():
        features = compute_breakout_features(
            history,
            breakout_window=breakout_params.breakout_window,
            volume_short=5,
            volume_long=20,
        )
        if features is None:
            continue
        passed, score, detail = breakout_signal(features, breakout_params)
        checks = detail.get("checks", {}) if isinstance(detail, dict) else {}
        missing = [name for name, value in checks.items() if not value]
        candidates.append(
            {
                "symbol": symbol,
                "features": features,
                "passed": passed,
                "score": float(score),
                "missing": missing,
            }
        )
    if not candidates:
        return []
    # 通过条件的按得分降序；未通过条件的作为“观察名单”补足 top_n（并注明未通过项）
    qualified = sorted(
        [item for item in candidates if item["passed"]],
        key=lambda item: (-item["score"], item["symbol"]),
    )
    watch = sorted(
        [item for item in candidates if not item["passed"]],
        key=lambda item: (-item["score"], item["symbol"]),
    )
    ordered = qualified + watch
    recommendations: list[Recommendation] = []
    reason_labels = {
        "breakout": "未突破20日高点",
        "volume_ratio": f"量比不足 {breakout_params.volume_ratio_min:g}",
        "gain": "当日涨幅不在 2%~9.5% 区间",
        "not_overheated": "20日累计涨幅过大（已暴涨）",
        "above_ma20": "未站上MA20",
    }
    for rank, item in enumerate(ordered[: max(top_n, 1)], start=1):
        features = item["features"]
        if item["passed"]:
            reason = (
                f"异动前兆通过全部条件（得分 {item['score']:.2f}）：{breakout_reason(features)}；"
                "未追涨停、未暴涨过滤通过"
            )
            note = "符合异动前兆买入条件"
        else:
            missing_text = "、".join(reason_labels.get(name, name) for name in item["missing"])
            reason = f"观察名单（未满足：{missing_text}）：{breakout_reason(features)}"
            note = "仅观察，不建议立即买入"
        recommendations.append(
            Recommendation(
                symbol=item["symbol"],
                strategy_key="breakout_abnormal",
                as_of=as_of,
                rank=rank,
                score=item["score"],
                qualified=bool(item["passed"]),
                reason=reason,
                factors=features,
                note=note,
            )
        )
    return recommendations


def _score_multi_factor(
    histories: Mapping[str, pd.DataFrame],
    params: Mapping[str, Any],
    as_of: date,
    top_n: int,
) -> list[Recommendation]:
    features_by_symbol: dict[str, dict[str, float]] = {}
    for symbol, history in histories.items():
        features = compute_features(history)
        if features is not None:
            features_by_symbol[symbol] = features
    if not features_by_symbol:
        return []
    scores = multi_factor_scores(features_by_symbol)
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    recommendations: list[Recommendation] = []
    for rank, (symbol, score) in enumerate(ranked[: max(top_n, 1)], start=1):
        features = features_by_symbol[symbol]
        recommendations.append(
            Recommendation(
                symbol=symbol,
                strategy_key="multi_factor",
                as_of=as_of,
                rank=rank,
                score=float(score),
                reason=(
                    f"多因子得分 {score:+.2f}：20日动量 {features.get('momentum_20', 0.0) * 100:+.2f}%、"
                    f"20日波动率 {features.get('volatility_20', 0.0) * 100:.2f}%（动量强、波动低者优先）"
                ),
                factors=features,
                note="多因子策略按该得分排序取前 top_n 名",
            )
        )
    return recommendations


def _score_ml(
    histories: Mapping[str, pd.DataFrame],
    prepared: Any,
    as_of: date,
    top_n: int,
    params: Mapping[str, Any],
    bars: Any = None,
) -> list[Recommendation]:
    from strategy.ml_ranker import MLConfig, MLRanker

    config = MLConfig(
        model=str(params.get("model", "logistic")),
        train_window=int(params.get("train_window", 250)),
        test_window=int(params.get("test_window", 20)),
        min_train_rows=int(params.get("min_train_rows", 120)),
    )
    ranker = MLRanker(config)
    if bars is None:
        # 用 prepared 的 frames 重新拼行情（避免二次取数）
        frames = [
            frame.assign(symbol=symbol) for symbol, frame in prepared.frames.items()
        ]
        bars = pd.concat(frames, ignore_index=True) if frames else None
    picks = ranker.rank(bars, as_of, top_n=max(top_n, 1))
    importance = ranker.feature_importance()
    top_features = list(importance.head(5)["feature"])
    recommendations: list[Recommendation] = []
    for rank, pick in enumerate(picks, start=1):
        features = pick["features"]
        feature_text = "、".join(
            f"{FEATURE_LABELS.get(name, name)} {features.get(name, 0.0):+.3f}" for name in top_features
        )
        recommendations.append(
            Recommendation(
                symbol=pick["symbol"],
                strategy_key="ml_ranker",
                as_of=as_of,
                rank=rank,
                score=float(pick["proba"]),
                ml_proba=float(pick["proba"]),
                reason=(
                    f"ML 概率 {pick['proba'] * 100:.1f}%（{pick['backend']}，"
                    f"预测未来 {config.label_horizon} 日跑赢截面中位数）：主要特征 {feature_text}"
                ),
                factors=features,
                note="ML 打分为样本外 walk-forward 模型输出，不构成投资建议",
            )
        )
    return recommendations


def _score_generic(
    strategy_key: str,
    histories: Mapping[str, pd.DataFrame],
    as_of: date,
    top_n: int,
) -> list[Recommendation]:
    features_by_symbol: dict[str, dict[str, float]] = {}
    for symbol, history in histories.items():
        features = compute_features(history)
        if features is not None:
            features_by_symbol[symbol] = features
    if not features_by_symbol:
        return []
    momentum = {s: f.get("momentum_20", 0.0) for s, f in features_by_symbol.items()}
    from strategy.factors import zscore_map

    scores = zscore_map(momentum)
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    recommendations: list[Recommendation] = []
    for rank, (symbol, score) in enumerate(ranked[: max(top_n, 1)], start=1):
        features = features_by_symbol[symbol]
        recommendations.append(
            Recommendation(
                symbol=symbol,
                strategy_key=strategy_key,
                as_of=as_of,
                rank=rank,
                score=float(score),
                reason=(
                    f"非排序型策略（{strategy_key}），按 20 日动量排序："
                    f"动量 {features.get('momentum_20', 0.0) * 100:+.2f}%、"
                    f"RSI {features.get('rsi_14', 0.0):.1f}"
                ),
                factors=features,
                note="该策略不产生排序分数，此列表仅供参考",
            )
        )
    return recommendations


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------


def resolve_market_universe(
    universe: Sequence[str] | None = None,
    data_source: Any = None,
) -> tuple[list[str], str]:
    """解析“全市场扫描”标的池（近期组改造，P0）。

    优先级：显式 ``universe``（自选池）→ 数据源 ``get_symbols("STOCK")``（Dev1 全市场
    快照就绪后自动生效）→ 离线 ``MockProvider`` 自选池（并明确标注“快照未就绪”）。

    Returns:
        ``(symbols, source_note)``；``source_note`` 用于推荐卡片注明扫描范围。
    """

    if universe:
        symbols = [str(item).strip() for item in universe if str(item).strip()]
        if symbols:
            return list(dict.fromkeys(symbols)), "自选池扫描"
    source = data_source
    if source is None:
        try:
            from data.cache import BarCache  # type: ignore

            cache = BarCache()
            source = getattr(cache, "provider", None) or cache
        except Exception:  # noqa: BLE001 - 数据层不可用则降级 mock
            source = None
    if source is not None and hasattr(source, "get_symbols"):
        try:
            frame = source.get_symbols("STOCK")
            if frame is not None and len(frame) > 0 and "symbol" in frame.columns:
                symbols = [str(item) for item in frame["symbol"].tolist() if str(item).strip()]
                if symbols:
                    return list(dict.fromkeys(symbols)), f"全市场快照扫描（{type(source).__name__}）"
        except Exception as exc:  # noqa: BLE001
            logger.info("全市场标的池获取失败，降级离线自选池：%s", exc)
    try:
        from data.provider import MockProvider  # type: ignore

        frame = MockProvider().get_symbols("STOCK")
        symbols = [str(item) for item in frame["symbol"].tolist() if str(item).strip()]
        if symbols:
            return (
                list(dict.fromkeys(symbols)),
                "离线自选池扫描（全市场快照未就绪，接口已预留 get_symbols）",
            )
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"无法解析全市场标的池，请显式传入 universe 或检查数据层：{exc}") from exc
    raise ValueError("无法解析全市场标的池：请显式传入 universe")


def recent_recommend(
    as_of: date | str,
    *,
    top_n: int = 10,
    universe: Sequence[str] | None = None,
    params: Mapping[str, Any] | None = None,
    bars: Any = None,
    data_source: Any = None,
    lookback_days: int = 900,
) -> list[Recommendation]:
    """短线 v1 近期推荐（全市场扫描优先，输出带理由/风险提示的 Top-N）。

    Args:
        as_of: 打分日期（非交易日取之前最近交易日）。
        top_n: 返回条数。
        universe: 自选池；None/空 → 尝试全市场快照（数据层未就绪时降级离线示例池）。
        params: :class:`strategy.short_term.ShortTermConfig` 覆盖项（train_window/model 等）。
        bars: 行情 DataFrame（测试/离线用，优先）。
        data_source: 数据源（默认本地缓存）。
        lookback_days: 取数回看自然日数（默认 900，约 2.5 年，保证 walk-forward 训练样本）。

    Returns:
        :class:`Recommendation` 列表；``score`` 为“未来 5 日 ≥+2%”的样本外概率。
    """

    from dataclasses import fields as dataclass_fields

    from backtest.engine import BacktestEngine
    from strategy.short_term import ShortTermConfig, ShortTermModel

    as_of_date = _parse_date(as_of)
    symbols, universe_note = resolve_market_universe(universe, data_source)
    start = as_of_date - timedelta(days=max(400, int(lookback_days)))
    try:
        engine = BacktestEngine(data_source=data_source)
        prepared = engine.prepare_market_data(symbols, start, as_of_date, bars=bars)
    except Exception as exc:  # noqa: BLE001 - 统一为中文 ValueError 供 UI 展示
        raise ValueError(f"短线推荐取数失败：{exc}") from exc
    frames: list[pd.DataFrame] = []
    for symbol, frame in prepared.frames.items():
        part = frame.loc[
            :, [column for column in ("date", "symbol", "open", "high", "low", "close", "volume", "amount")
                if column in frame.columns]
        ].copy()
        part["symbol"] = symbol
        frames.append(part)
    if not frames:
        raise ValueError(f"截至 {as_of_date} 没有可用行情，无法生成短线推荐")
    all_bars = pd.concat(frames, ignore_index=True)

    allowed = {field.name for field in dataclass_fields(ShortTermConfig)}
    config_kwargs = {key: value for key, value in (params or {}).items() if key in allowed}
    if top_n:
        config_kwargs.setdefault("top_n", int(top_n))
    config = ShortTermConfig(**config_kwargs)  # type: ignore[arg-type]
    try:
        picks = ShortTermModel(config).rank_candidates(all_bars, as_of_date, top_n=max(1, int(top_n)))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"短线模型打分失败（可能历史/样本不足）：{exc}") from exc

    recommendations: list[Recommendation] = []
    for rank, pick in enumerate(picks, start=1):
        prob_2 = float(pick.get("prob_2pct", 0.0) or 0.0)
        reason = (
            f"短线 v1：未来 5 日 ≥+2% 概率 {prob_2 * 100:.1f}%"
            f"（≥+1% {float(pick.get('prob_1pct', 0.0)) * 100:.1f}% / "
            f"≥+3% {float(pick.get('prob_3pct', 0.0)) * 100:.1f}%）；"
            + "；".join(pick.get("reasons", []))
        )
        risk = "；".join(pick.get("risk_notes", [])[:3])
        factors = {
            "prob_2pct": prob_2,
            "prob_1pct": float(pick.get("prob_1pct", 0.0) or 0.0),
            "prob_3pct": float(pick.get("prob_3pct", 0.0) or 0.0),
        }
        factors.update(
            {key: float(value) for key, value in list(pick.get("factors", {}).items())[:6]}
        )
        recommendations.append(
            Recommendation(
                symbol=str(pick.get("symbol", "")),
                strategy_key="short_term_v1",
                as_of=as_of_date,
                rank=rank,
                score=prob_2,
                # 研究阶段：一律不标记 qualified，避免 UI/下游把未验证模型当买入信号
                qualified=False,
                reason=reason,
                factors=factors,
                components={},
                ml_proba=prob_2,
                note=f"{universe_note}；风险提示：{risk}；{RESEARCH_STAGE_NOTE}",
                name=str(pick.get("name") or "") or None,
            )
        )
    return attach_names(recommendations[: max(1, int(top_n))])


def recommend(
    strategy_key: str,
    universe: Sequence[str] | None,
    as_of: date | str,
    *,
    top_n: int = 10,
    params: Mapping[str, Any] | None = None,
    bars: Any = None,
    data_source: Any = None,
    lookback_days: int = 550,
    use_ml: bool = False,
    ml_weight: float = 0.3,
) -> list[Recommendation]:
    """输出带理由的 top-N 候选。

    Args:
        strategy_key: 策略 key（``value_quality`` / ``breakout_abnormal`` /
            ``multi_factor`` / ``ml_ranker`` / ``short_term_v1``；其它策略走通用动量排序）。
            ``short_term`` / ``short_term_v1`` / ``recent`` 走**短线 v1 全市场扫描**。
        universe: 自选池（标的代码列表）；短线 v1 允许 None/空 → 自动解析全市场快照。
        as_of: 打分日期（date 或 ``YYYY-MM-DD``；非交易日取之前最近交易日）。
        top_n: 返回条数（默认 10）。
        params: 策略参数（会经注册表校验；``ml_ranker``/``short_term_v1`` 见各自配置）。
        bars: 行情 DataFrame（提供时优先使用，便于离线/测试）；否则用 data_source
            或本地缓存。
        data_source: 数据源（``DataProvider.get_bars`` / ``BarCache.load`` / callable）。
        lookback_days: 取数回看自然日数（默认 550，覆盖 120 日因子窗口）。
        use_ml: True 时把 ML 概率按 ``ml_weight`` 混入基础得分（策略非 ml_ranker 时）。
        ml_weight: ML 混合权重（0~1）。

    Returns:
        :class:`Recommendation` 列表（按 rank 升序，``qualified`` 表示是否满足买入条件）。

    Raises:
        ValueError: 自选池为空 / 无行情 / 参数非法（中文消息）。
    """

    as_of_date = _parse_date(as_of)
    strategy_key = str(strategy_key or "").strip()
    if not strategy_key:
        raise ValueError("strategy_key 不能为空")
    top_n = max(1, int(top_n))
    # 短线 v1：全市场扫描（universe 省略时自动解析；数据层快照未就绪则降级自选池）
    if strategy_key.lower() in SHORT_TERM_KEYS:
        return recent_recommend(
            as_of_date,
            top_n=top_n,
            universe=universe,
            params=params,
            bars=bars,
            data_source=data_source,
            lookback_days=max(900, int(lookback_days)),
        )
    if not universe:
        raise ValueError("自选池为空：请至少提供 1 个标的（短线推荐可用 strategy_key=short_term_v1）")
    normalized_params = _params_for(strategy_key, params)
    prepared = _prepare(universe, as_of_date, bars=bars, data_source=data_source, lookback_days=lookback_days)
    histories: dict[str, pd.DataFrame] = {}
    actual_day = as_of_date
    for symbol in prepared.frames:
        history, day = _history_until(prepared, symbol, as_of_date)
        if history is None or len(history) < 22:
            continue
        histories[symbol] = history
        if day is not None:
            actual_day = max(actual_day, day) if actual_day != as_of_date else day
    if not histories:
        raise ValueError(f"截至 {as_of_date} 没有足够历史行情，无法推荐（每只至少 22 根 bar）")

    if strategy_key == "value_quality":
        recommendations = _score_value_quality(histories, normalized_params, as_of_date, top_n)
    elif strategy_key == "breakout_abnormal":
        recommendations = _score_breakout_abnormal(histories, normalized_params, as_of_date, top_n)
    elif strategy_key == "multi_factor":
        recommendations = _score_multi_factor(histories, normalized_params, as_of_date, top_n)
    elif strategy_key == "ml_ranker":
        recommendations = _score_ml(
            histories, prepared, as_of_date, top_n, normalized_params, bars=bars
        )
    else:
        recommendations = _score_generic(strategy_key, histories, as_of_date, top_n)

    if use_ml and recommendations and strategy_key != "ml_ranker":
        recommendations = _blend_ml(recommendations, histories, prepared, as_of_date, bars, ml_weight)
    return attach_names(recommendations[:top_n])


def _blend_ml(
    recommendations: list[Recommendation],
    histories: Mapping[str, pd.DataFrame],
    prepared: Any,
    as_of: date,
    bars: Any,
    ml_weight: float,
) -> list[Recommendation]:
    """把 ML 概率混入基础得分（可选能力；失败时原样返回并记录 note）。"""

    from strategy.ml_ranker import MLConfig, MLRanker

    weight = min(max(float(ml_weight), 0.0), 1.0)
    if weight <= 0:
        return recommendations
    try:
        if bars is None:
            frames = [frame.assign(symbol=symbol) for symbol, frame in prepared.frames.items()]
            bars = pd.concat(frames, ignore_index=True) if frames else None
        picks = MLRanker(MLConfig()).rank(bars, as_of, top_n=max(len(recommendations), 10))
        proba_by_symbol = {pick["symbol"]: float(pick["proba"]) for pick in picks}
    except Exception as exc:  # noqa: BLE001 - ML 失败降级为纯因子排序
        logger.info("ML 混合失败，使用纯因子排序：%s", exc)
        return recommendations
    base_scores = {item.symbol: item.score for item in recommendations}
    if not base_scores:
        return recommendations
    values = np.array(list(base_scores.values()), dtype="float64")
    low, high = float(values.min()), float(values.max())
    span = high - low if high > low else 1.0
    blended: list[Recommendation] = []
    for item in recommendations:
        normalized = (item.score - low) / span
        proba = proba_by_symbol.get(item.symbol)
        if proba is None:
            combined = item.score
            ml_note = "ML 未覆盖该标的，保持因子得分"
        else:
            combined = (1.0 - weight) * normalized + weight * proba
            ml_note = f"ML 概率 {proba * 100:.1f}% 按 {weight:.0%} 权重混合"
        blended.append(
            Recommendation(
                symbol=item.symbol,
                strategy_key=item.strategy_key,
                as_of=item.as_of,
                rank=item.rank,
                score=float(combined),
                qualified=item.qualified,
                reason=item.reason + f"；{ml_note}",
                factors=item.factors,
                components=item.components,
                ml_proba=proba,
                note=item.note,
                name=item.name,
            )
        )
    blended.sort(key=lambda item: (-item.score, item.symbol))
    return [
        Recommendation(
            symbol=item.symbol,
            strategy_key=item.strategy_key,
            as_of=item.as_of,
            rank=index,
            score=item.score,
            qualified=item.qualified,
            reason=item.reason,
            factors=item.factors,
            components=item.components,
            ml_proba=item.ml_proba,
            note=item.note,
            name=item.name,
        )
        for index, item in enumerate(blended, start=1)
    ]


def screen_market_result(
    as_of: date | str | None = None,
    *,
    top_n: int = 25,
    config: Any = None,
    snapshot: Any = None,
    bars: Any = None,
    data_source: Any = None,
    mode: str | None = None,
) -> Any:
    """全市场筛选完整结果（含统计/警告，供 app_services/UI 展示状态条）。"""

    from strategy.screener import MarketScreener, ScreenerConfig

    screener_config = config or ScreenerConfig()
    if mode is not None or top_n != screener_config.top_n:
        screener_config = ScreenerConfig(
            **{
                **screener_config.__dict__,
                "top_n": int(top_n),
                "mode": str(mode or screener_config.mode),
            }
        )
    return MarketScreener(screener_config, data_source=data_source).screen(
        as_of=as_of, snapshot=snapshot, bars=bars
    )


def screen_market(
    as_of: date | str | None = None,
    *,
    top_n: int = 25,
    config: Any = None,
    snapshot: Any = None,
    bars: Any = None,
    data_source: Any = None,
    mode: str | None = None,
) -> list[Recommendation]:
    """全市场短线雷达（研究阶段）：调用 :class:`strategy.screener.MarketScreener`。

    返回结构与其他策略一致（``Recommendation``），但 **``qualified`` 恒为 False**：
    规则筛选尚未通过统计验证，UI 必须展示“研究阶段”标注，不得当作买入建议。

    Args:
        as_of: 快照日期；None 用最新快照。
        top_n: 返回条数。
        config: ``ScreenerConfig``（权重/过滤阈值/数据模式）。
        snapshot: 注入全市场快照（测试/回放）。
        bars: 注入历史行情（优先）。
        data_source: 数据源（默认本地缓存）。
        mode: ``cache``（只用缓存，默认）或 ``incremental``（增量拉取）。

    Returns:
        :class:`Recommendation` 列表。
    """

    result = screen_market_result(
        as_of,
        top_n=top_n,
        config=config,
        snapshot=snapshot,
        bars=bars,
        data_source=data_source,
        mode=mode,
    )
    recommendations: list[Recommendation] = []
    for rank, pick in enumerate(result.picks[: max(1, int(top_n))], start=1):
        reasons = "；".join(pick.get("reasons", []))
        risk_flags = "、".join(pick.get("risk_flags", [])) or "无过热标记"
        recommendations.append(
            Recommendation(
                symbol=str(pick.get("symbol", "")),
                strategy_key="screen_market",
                as_of=result.as_of,
                rank=rank,
                score=float(pick.get("score", 0.0)),
                qualified=False,
                name=str(pick.get("name") or "") or None,
                reason=f"全市场筛选得分 {float(pick.get('score', 0.0)):+.2f}：{reasons}",
                factors={
                    "mom20": pick.get("mom20"),
                    "mom60": pick.get("mom60"),
                    "vol_ratio": pick.get("vol_ratio"),
                    "breakout20": pick.get("breakout20"),
                    "dist_ma20": pick.get("dist_ma20"),
                    "atr_pct": pick.get("atr_pct"),
                    "near_high_250": pick.get("near_high_250"),
                    "amount_yi": pick.get("amount_yi"),
                },
                components={},
                note=(
                    f"研究阶段：规则筛选未经统计验证（见 docs/SCREENER_VALIDATION.md）；"
                    f"风险标记：{risk_flags}"
                ),
            )
        )
    return attach_names(recommendations)


def recommend_dataframe(
    strategy_key: str,
    universe: Sequence[str],
    as_of: date | str,
    **kwargs: Any,
) -> pd.DataFrame:
    """把 :func:`recommend` 结果转成 DataFrame（UI 表格直接渲染/导出 CSV）。"""

    picks = recommend(strategy_key, universe, as_of, **kwargs)
    if not picks:
        return pd.DataFrame(columns=["rank", "symbol", "score", "qualified", "reason", "note"])
    return pd.DataFrame([pick.as_dict() for pick in picks])[
        ["rank", "symbol", "score", "qualified", "reason", "note"]
    ]


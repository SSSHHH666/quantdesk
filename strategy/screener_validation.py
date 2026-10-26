"""全市场筛选器（规则打分）的历史验证：分组回测 + IC/RankIC + 成本后净值。

目的
----
回答一个核心问题：**Lead 原型里的“全市场 → 流动性 → 260 日量价信号 → 截面 z-score
→ 过热剔除 → Top N”到底有没有 OOS 边际？** 本模块严格按时间滑动：

* 每个交易日 ``t`` 只用 ``≤ t`` 的行情计算信号与截面打分；
* 用 ``t`` 之后 5/10/20 个交易日的收益评估（未来数据只用于评估，不参与打分）；
* 按打分分 5 组，统计各组未来收益、胜率、Top 组相对全市场超额、Top-Bottom 多空差；
* 计算每日 Spearman(score, 未来收益) 的 IC/RankIC（含 ICIR/正 IC 比例）；
* Top 组每 ``持有期`` 天再平衡一次，扣双边成本后的净值/年化/最大回撤。

结论口径（写入 ``docs/SCREENER_VALIDATION.md``）
------------------------------------------------
* ``有效``：|均值 RankIC| ≥ 0.03、Top 组超额 t 值 ≥ 2、Top 组扣费后净值为正；
* ``边缘``：0.01 ≤ |RankIC| < 0.03，或超额 t 值 1~2，或扣费后接近 0；
* ``无效``：其余（含 RankIC 接近 0、Top 组无超额、扣费后为负）。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from backtest.broker_sim import round_money
from backtest.metrics import compute_metrics
from strategy.factors import normalize_bars
from strategy.screener import DEFAULT_SCREEN_WEIGHTS, ScreenerConfig, _weighted_score

logger = logging.getLogger(__name__)

__all__ = [
    "ValidationConfig",
    "ValidationResult",
    "validate_screener",
    "render_markdown",
]


@dataclass(frozen=True)
class ValidationConfig:
    """验证参数。"""

    groups: int = 5
    horizons: tuple[int, ...] = (5, 10, 20)
    holding_days: int = 5
    round_trip_cost: float = 0.0023
    min_symbols_per_date: int = 20
    min_history: int = 120
    weights: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_SCREEN_WEIGHTS))

    def __post_init__(self) -> None:
        if self.groups < 3:
            raise ValueError("groups 至少 3")
        if not self.horizons:
            raise ValueError("horizons 不能为空")
        if self.holding_days < 1:
            raise ValueError("holding_days 必须 ≥ 1")
        if float(sum(self.weights.values())) <= 0:
            raise ValueError("权重之和必须 > 0")

    def as_dict(self) -> dict[str, Any]:
        return {
            "groups": self.groups,
            "horizons": list(self.horizons),
            "holding_days": self.holding_days,
            "round_trip_cost": self.round_trip_cost,
            "min_symbols_per_date": self.min_symbols_per_date,
            "min_history": self.min_history,
            "weights": dict(self.weights),
        }


@dataclass
class ValidationResult:
    """验证结果（指标 + 分组表 + IC 序列 + 净值 + 结论）。"""

    config: ValidationConfig
    metrics: dict[str, Any]
    group_table: pd.DataFrame
    ic_series: pd.DataFrame
    equity: pd.DataFrame
    conclusion: dict[str, Any]
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": self.config.as_dict(),
            "metrics": dict(self.metrics),
            "group_table": self.group_table.to_dict(orient="records"),
            "conclusion": dict(self.conclusion),
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# 向量化信号（与 strategy/screener.py 同口径）
# ---------------------------------------------------------------------------


def _vectorized_signals(group: pd.DataFrame) -> pd.DataFrame | None:
    close = pd.to_numeric(group["close"], errors="coerce").astype("float64")
    high = pd.to_numeric(group["high"], errors="coerce").astype("float64")
    low = pd.to_numeric(group["low"], errors="coerce").astype("float64")
    volume = pd.to_numeric(group["volume"], errors="coerce").astype("float64")
    amount = pd.to_numeric(group["amount"], errors="coerce").astype("float64")
    if amount.fillna(0).sum() <= 0:
        amount = close * volume
    returns = close.pct_change()
    frame = pd.DataFrame(
        {
            "date": group["date"],
            "symbol": str(group["symbol"].iloc[0]),
            "close": close,
            "mom20": close / close.shift(20) - 1.0,
            "mom60": close / close.shift(60) - 1.0,
            "vol20": returns.rolling(20, min_periods=20).std(ddof=1) * math.sqrt(252),
            "vol_ratio": volume / volume.shift(1).rolling(20, min_periods=20).mean(),
            "breakout20": close / high.shift(1).rolling(20, min_periods=20).max() - 1.0,
            "dist_ma20": close / close.rolling(20, min_periods=20).mean() - 1.0,
            "dist_ma60": close / close.rolling(60, min_periods=20).mean() - 1.0,
            "ret5": close / close.shift(5) - 1.0,
            "near_high_250": close / high.rolling(250, min_periods=120).max() - 1.0,
            "amount_yi": amount / 1e8,
            "amount_pct": amount.rank(pct=True),
        }
    )
    true_range = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1
    ).max(axis=1)
    frame["atr_pct"] = true_range.rolling(14, min_periods=14).mean() / close
    up = (returns > 0).astype("float64")
    frame["consec_up"] = up.groupby((up != up.shift()).cumsum()).cumsum().fillna(0.0)
    for horizon in (5, 10, 20):
        frame[f"forward_{horizon}"] = close.shift(-horizon) / close - 1.0
    return frame


def validate_screener(
    bars: pd.DataFrame,
    config: ValidationConfig | None = None,
    *,
    screener_config: ScreenerConfig | None = None,
) -> ValidationResult:
    """对筛选规则做严格历史验证。

    Args:
        bars: 长表行情（date/symbol/OHLCV），建议 ≥ 30 只、2019~2026。
        config: 验证参数。
        screener_config: 打分权重/参数（默认与 MarketScreener 一致）。

    Returns:
        :class:`ValidationResult`。
    """

    config = config or ValidationConfig()
    screener_config = screener_config or ScreenerConfig()
    weights = dict(config.weights or screener_config.weights)
    frame = normalize_bars(bars)
    signals = []
    for symbol, group in frame.groupby("symbol", sort=True):
        group = group.sort_values("date").reset_index(drop=True)
        if len(group) < config.min_history:
            continue
        value = _vectorized_signals(group)
        if value is not None:
            signals.append(value)
    if not signals:
        raise ValueError("验证失败：没有标的满足最少历史长度")
    panel = pd.concat(signals, ignore_index=True)
    panel = panel[panel["date"].notna()].copy()
    panel["score"] = np.nan
    # 每个交易日独立截面打分（只用当日及以前数据；未来收益仅用于评估）
    records: list[dict[str, Any]] = []
    group_returns: dict[int, dict[int, list[float]]] = {
        group: {horizon: [] for horizon in config.horizons} for group in range(1, config.groups + 1)
    }
    ic_rows: list[dict[str, Any]] = []
    excess_series: dict[int, list[float]] = {horizon: [] for horizon in config.horizons}
    group_win: dict[int, dict[int, list[float]]] = {
        group: {horizon: [] for horizon in config.horizons} for group in range(1, config.groups + 1)
    }
    dates = sorted(panel["date"].unique())
    for day in dates:
        day_frame = panel[panel["date"] == day].copy()
        if len(day_frame) < max(config.min_symbols_per_date, config.groups * 2):
            continue
        required = ["mom20", "mom60", "vol20", "vol_ratio", "breakout20", "dist_ma20", "near_high_250"]
        day_frame = day_frame.dropna(subset=required)
        if len(day_frame) < max(config.min_symbols_per_date, config.groups * 2):
            continue
        day_frame["score"] = _weighted_score(day_frame, weights)
        day_frame = day_frame.dropna(subset=["score"])
        if len(day_frame) < config.groups * 2:
            continue
        try:
            day_frame["group"] = pd.qcut(
                day_frame["score"].rank(method="first"), config.groups, labels=False
            ) + 1
        except ValueError:
            continue
        for horizon in config.horizons:
            valid = day_frame.dropna(subset=[f"forward_{horizon}"])
            if len(valid) < config.groups * 2:
                continue
            market_mean = float(valid[f"forward_{horizon}"].mean())
            for group in range(1, config.groups + 1):
                group_data = valid[valid["group"] == group]
                if len(group_data) == 0:
                    continue
                mean_return = float(group_data[f"forward_{horizon}"].mean())
                group_returns[group][horizon].append(mean_return)
                group_win[group][horizon].append(float((group_data[f"forward_{horizon}"] > 0).mean()))
            top_data = valid[valid["group"] == config.groups]
            if len(top_data) > 0:
                excess_series[horizon].append(float(top_data[f"forward_{horizon}"].mean()) - market_mean)
            # IC（Spearman）与 RankIC（同一次秩相关；score 是 z 值，秩相关即 RankIC）
            ic = _spearman(valid["score"].to_numpy(), valid[f"forward_{horizon}"].to_numpy())
            ic_rows.append({"date": day, "horizon": horizon, "ic": ic, "rank_ic": ic, "n": len(valid)})
        records.append({"date": day, "n": len(day_frame), "top_group": config.groups})
    if not ic_rows:
        raise ValueError("验证失败：没有足够的交易日/标的形成截面样本")

    ic_frame = pd.DataFrame(ic_rows)
    ic_summary: dict[str, Any] = {}
    for horizon in config.horizons:
        subset = ic_frame[ic_frame["horizon"] == horizon]["ic"].dropna()
        if len(subset) == 0:
            continue
        mean_ic = float(subset.mean())
        std_ic = float(subset.std(ddof=1)) if len(subset) >= 2 else None
        ic_summary[horizon] = {
            "mean_ic": _round(mean_ic),
            "mean_rank_ic": _round(mean_ic),
            "ic_std": _round(std_ic),
            "ic_ir": _round(mean_ic / std_ic) if std_ic and std_ic > 0 else None,
            "positive_ic_ratio": _round(float((subset > 0).mean())),
            "n_dates": int(len(subset)),
        }

    # 分组表
    group_rows: list[dict[str, Any]] = []
    for group in range(1, config.groups + 1):
        row: dict[str, Any] = {"group": group, "label": "Top" if group == config.groups else ("Bottom" if group == 1 else f"G{group}")}
        for horizon in config.horizons:
            returns = group_returns[group][horizon]
            wins = group_win[group][horizon]
            row[f"return_{horizon}d"] = _round(float(np.mean(returns))) if returns else None
            row[f"win_rate_{horizon}d"] = _round(float(np.mean(wins))) if wins else None
        group_rows.append(row)
    group_table = pd.DataFrame(group_rows)

    # 多空/超额统计
    long_short: dict[str, Any] = {}
    for horizon in config.horizons:
        top = group_returns[config.groups][horizon]
        bottom = group_returns[1][horizon]
        excess = excess_series[horizon]
        spread = (
            float(np.mean(top) - np.mean(bottom)) if top and bottom and len(top) == len(bottom) else None
        )
        long_short[horizon] = {
            "top_excess": _round(float(np.mean(excess))) if excess else None,
            "top_excess_t": _round(_t_stat(excess)),
            "top_excess_positive_ratio": _round(float(np.mean([1.0 if value > 0 else 0.0 for value in excess])))
            if excess
            else None,
            "top_minus_bottom": _round(spread),
        }

    # Top 组组合净值（每 holding_days 再平衡，非重叠，扣双边成本）
    equity, portfolio = _simulate_portfolio(
        panel, config, weights
    )

    metrics: dict[str, Any] = {
        "symbols": int(panel["symbol"].nunique()),
        "rows": int(len(panel)),
        "dates": int(panel["date"].nunique()),
        "evaluated_dates": int(len({row["date"] for row in ic_rows})),
        "horizons": list(config.horizons),
        "ic": ic_summary,
        "long_short": long_short,
        "portfolio": portfolio,
        "round_trip_cost": config.round_trip_cost,
        "groups": config.groups,
    }
    conclusion = _conclude(metrics)
    notes = [
        "严格 OOS：每个交易日只用 ≤ 当日数据打分，未来收益仅用于评估",
        f"成本口径：每次再平衡按 round_trip_cost={config.round_trip_cost:.2%} 扣费（保守全额换手）",
        "样本：本地可得行情；若标的数 < 30，结论仅作管线验证，不具统计意义",
    ]
    return ValidationResult(
        config=config,
        metrics=metrics,
        group_table=group_table,
        ic_series=ic_frame,
        equity=equity,
        conclusion=conclusion,
        notes=notes,
    )


def _simulate_portfolio(
    panel: pd.DataFrame, config: ValidationConfig, weights: Mapping[str, float]
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Top 组每 holding_days 个交易日再平衡（非重叠），扣成本后净值。"""

    horizon = config.holding_days
    dates = sorted(panel["date"].unique())
    # 先计算每个再平衡日的 Top 组 forward_holding 收益
    rebalance_dates = dates[:: max(1, horizon)]
    rows: list[dict[str, Any]] = []
    forward_col = f"forward_{horizon}"
    for day in rebalance_dates:
        day_frame = panel[panel["date"] == day].dropna(
            subset=["mom20", "mom60", "vol20", "vol_ratio", "breakout20", "dist_ma20", "near_high_250", forward_col]
        )
        if len(day_frame) < max(config.min_symbols_per_date, config.groups * 2):
            continue
        day_frame = day_frame.copy()
        day_frame["score"] = _weighted_score(day_frame, weights)
        day_frame = day_frame.dropna(subset=["score"])
        if len(day_frame) < config.groups * 2:
            continue
        threshold = day_frame["score"].quantile(1 - 1 / config.groups)
        top = day_frame[day_frame["score"] >= threshold]
        if top.empty:
            continue
        rows.append(
            {
                "date": day,
                "top_return": float(top[forward_col].mean()),
                "n_top": int(len(top)),
            }
        )
    if not rows:
        return pd.DataFrame(columns=["trade_date", "equity", "cash", "position_value", "drawdown"]), {}
    frame = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    equity_values: list[float] = []
    equity = 1.0
    for value in frame["top_return"]:
        net = 1.0 + float(value) - config.round_trip_cost
        equity *= max(net, 0.0)
        equity_values.append(equity)
    frame["equity"] = equity_values
    frame["trade_date"] = pd.to_datetime(frame["date"]).dt.date
    equity_frame = pd.DataFrame(
        {
            "trade_date": frame["trade_date"],
            "equity": frame["equity"] * 100_000.0,
            "cash": 0.0,
            "position_value": frame["equity"] * 100_000.0,
        }
    )
    equity_frame["drawdown"] = equity_frame["equity"] / equity_frame["equity"].cummax() - 1.0
    metrics = compute_metrics(
        equity_frame,
        pd.DataFrame(),
        initial_cash=100_000.0,
    )
    portfolio = {
        "rebalances": int(len(frame)),
        "total_return": _round(float(equity - 1.0)),
        "annual_return": metrics.get("annual_return"),
        "max_drawdown": metrics.get("max_drawdown"),
        "sharpe": metrics.get("sharpe"),
        "final_equity": _round(float(equity_frame["equity"].iloc[-1])),
        "turnover_cost_total": _round(config.round_trip_cost * len(frame)),
    }
    return equity_frame, portfolio


def _conclude(metrics: Mapping[str, Any]) -> dict[str, Any]:
    """按预设阈值给出 有效/边缘/无效 与改进方向（不美化）。"""

    ic = metrics.get("ic", {})
    horizons = metrics.get("horizons", [])
    primary = 5 if 5 in horizons else (horizons[0] if horizons else None)
    primary_ic = ic.get(primary, {}) if primary is not None else {}
    rank_ic = primary_ic.get("mean_rank_ic")
    ic_ir = primary_ic.get("ic_ir")
    long_short = metrics.get("long_short", {}).get(primary, {}) if primary is not None else {}
    top_excess = long_short.get("top_excess")
    top_t = long_short.get("top_excess_t")
    portfolio = metrics.get("portfolio", {})
    net_return = portfolio.get("total_return")
    rank_ic_value = float(rank_ic) if rank_ic is not None else None
    if rank_ic_value is None:
        verdict = "无法判定（IC 样本不足）"
        usable = False
    elif rank_ic_value >= 0.03 and (top_t or 0) >= 2 and (net_return or 0) > 0:
        verdict = "有效（达到预设阈值，但仍需更大样本与实盘检验）"
        usable = True
    elif rank_ic_value <= -0.01 and (top_t or 0) < 2:
        verdict = (
            "无效（RankIC 为负：打分与未来收益方向相反、Top 组无超额；"
            "低分组反而略优，不可直接用于正向选股）"
        )
        usable = False
    elif 0.01 <= rank_ic_value < 0.03 or 1.0 <= (top_t or 0) < 2:
        verdict = "边缘（有微弱正向信号，统计上不稳健，不足以支撑实盘）"
        usable = False
    else:
        verdict = "无效（RankIC 接近 0 / Top 组无显著超额）"
        usable = False
    return {
        "verdict": verdict,
        "usable": bool(usable),
        "primary_horizon": primary,
        "rank_ic": rank_ic,
        "ic_ir": ic_ir,
        "top_excess": top_excess,
        "top_excess_t": top_t,
        "portfolio_total_return": net_return,
        "next_steps": [
            "换因子：加入资金流/龙虎榜/行业相对强度（当前只有量价）",
            "换持有期：5/10/20 日中寻找 IC 衰减最慢的周期",
            "加市场状态过滤：牛/熊/震荡分层后再筛（当前对所有行情一视同仁）",
            "做多空组合：若 Bottom 组显著跑输，可考虑对冲或反向剔除",
            "严格化样本：剔除 ST/新股/停牌、按流动性分层验证",
        ],
    }


def _spearman(a: np.ndarray, b: np.ndarray) -> float | None:
    if len(a) < 3 or len(b) < 3:
        return None
    ranks_a = pd.Series(a).rank().to_numpy()
    ranks_b = pd.Series(b).rank().to_numpy()
    if ranks_a.std() <= 0 or ranks_b.std() <= 0:
        return None
    return float(np.corrcoef(ranks_a, ranks_b)[0, 1])


def _t_stat(values: Sequence[float]) -> float | None:
    array = np.asarray(list(values), dtype="float64")
    if len(array) < 3:
        return None
    std = float(array.std(ddof=1))
    if std <= 0:
        return None
    return float(array.mean() / (std / math.sqrt(len(array))))


def _round(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, 6)


def render_markdown(result: ValidationResult, *, title: str = "全市场筛选器验证") -> str:
    """把验证结果渲染为 Markdown（写入 docs/SCREENER_VALIDATION.md 的核心段落）。"""

    metrics = result.metrics
    conclusion = result.conclusion
    lines: list[str] = [f"## {title}", ""]
    lines.append(f"- 标的数：**{metrics.get('symbols')}**；样本行数：{metrics.get('rows')}；"
                 f"交易日：{metrics.get('dates')}；评估截面色：{metrics.get('evaluated_dates')}")
    lines.append(f"- 分组数：{metrics.get('groups')}；持有期：{metrics.get('horizons')} 个交易日")
    lines.append(f"- 结论：**{conclusion.get('verdict')}**")
    lines.append("")
    lines.append("### IC / RankIC（主持有期 5 日）")
    lines.append("")
    lines.append("| 持有期 | 均值 IC | 均值 RankIC | ICIR | 正 IC 比例 | 截面天数 |")
    lines.append("|---|---|---|---|---|---|")
    for horizon, values in metrics.get("ic", {}).items():
        lines.append(
            f"| {horizon}d | {values.get('mean_ic')} | {values.get('mean_rank_ic')} | "
            f"{values.get('ic_ir')} | {values.get('positive_ic_ratio')} | {values.get('n_dates')} |"
        )
    lines.append("")
    lines.append("### 分组收益（未来持有期均值）")
    lines.append("")
    header = "| 分组 | " + " | ".join(
        f"{horizon}d 收益 | {horizon}d 胜率" for horizon in metrics.get("horizons", [])
    ) + " |"
    lines.append(header)
    lines.append("|---" * (1 + 2 * len(metrics.get("horizons", []))) + "|")
    for _, row in result.group_table.iterrows():
        cells = [str(row["label"])]
        for horizon in metrics.get("horizons", []):
            cells.append(str(row.get(f"return_{horizon}d")))
            cells.append(str(row.get(f"win_rate_{horizon}d")))
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("### Top 组超额 / 多空")
    lines.append("")
    lines.append("| 持有期 | Top 组超额 | 超额 t 值 | 超额为正比例 | Top-Bottom |")
    lines.append("|---|---|---|---|---|")
    for horizon, values in metrics.get("long_short", {}).items():
        lines.append(
            f"| {horizon}d | {values.get('top_excess')} | {values.get('top_excess_t')} | "
            f"{values.get('top_excess_positive_ratio')} | {values.get('top_minus_bottom')} |"
        )
    lines.append("")
    portfolio = metrics.get("portfolio", {})
    lines.append("### Top 组扣费后净值")
    lines.append("")
    lines.append(
        f"- 再平衡次数：{portfolio.get('rebalances')}；总收益：{portfolio.get('total_return')}；"
        f"年化：{portfolio.get('annual_return')}；最大回撤：{portfolio.get('max_drawdown')}；"
        f"夏普：{portfolio.get('sharpe')}"
    )
    lines.append("")
    lines.append("### 改进方向")
    lines.append("")
    for step in conclusion.get("next_steps", []):
        lines.append(f"- {step}")
    lines.append("")
    if result.notes:
        lines.append("> 备注：")
        for note in result.notes:
            lines.append(f"> - {note}")
    return "\n".join(lines)

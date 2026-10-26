"""短线 v1 组合回测：每日 Top5 等权买入，持有 5 日或止盈(+3%)/止损(-2%)。

规则（Lead 定义）
-----------------
* 每个交易日收盘后用 :class:`strategy.short_term.ShortTermModel` 的 walk-forward 样本外
  概率，取 Top5 候选；**次日开盘**买入（T+1，避免未来函数），等权（各约 ``1/top_n`` 权益）。
* 持仓退出（先触发先成交，按当日 OHLC 保守判定）：

  - 止盈：当日最高价 ≥ 买入价 × (1+``take_profit``) → 以止盈价卖出；
  - 止损：当日最低价 ≤ 买入价 × (1−``stop_loss``) → 以止损价卖出；
  - 到期：持有满 ``max_hold`` 个交易日 → 当日收盘价卖出；
  - T+1：买入当日不得卖出（A 股规则）。
* 费用与滑点复用 :mod:`backtest.broker_sim`（唯一实现）；卖出同样加不利滑点。
* 输出：净值曲线、命中率（扣费后净收益 ≥ +2% 的占比）、期望收益（扣双边成本+滑点）、
  最大回撤、换手成本，并按 Lead 判定线（命中率>55%、期望>0、回撤<10%）给出
  **明确可用/不可用**结论（不美化）。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from backtest.broker_sim import FeeModel, resolve_fee_config, round_money
from backtest.metrics import compute_metrics
from strategy.factors import normalize_bars
from strategy.short_term import ShortTermConfig, ShortTermModel, ShortTermReport

logger = logging.getLogger(__name__)

__all__ = ["ShortBacktestConfig", "ShortBacktestResult", "ShortTermBacktester"]


@dataclass(frozen=True)
class ShortBacktestConfig:
    """短线组合回测配置。"""

    initial_cash: float = 100_000.0
    top_n: int = 5
    take_profit: float = 0.03
    stop_loss: float = 0.02
    max_hold: int = 5
    slippage_bps: float = 5.0
    min_prob: float = 0.0
    model: ShortTermConfig = field(default_factory=ShortTermConfig)

    def __post_init__(self) -> None:
        if self.initial_cash <= 0:
            raise ValueError("initial_cash 必须为正数")
        if self.top_n < 1:
            raise ValueError("top_n 必须 ≥ 1")
        if not 0 < self.take_profit < 1 or not 0 < self.stop_loss < 1:
            raise ValueError("止盈/止损比例必须在 (0, 1) 之间")
        if self.max_hold < 1:
            raise ValueError("max_hold 必须 ≥ 1")


@dataclass
class ShortBacktestResult:
    """短线组合回测结果。"""

    equity: pd.DataFrame
    trades: pd.DataFrame
    metrics: dict[str, Any]
    conclusion: dict[str, Any]
    report: ShortTermReport
    config: ShortBacktestConfig
    notes: list[str] = field(default_factory=list)

    @property
    def final_equity(self) -> float:
        return float(self.equity.iloc[-1]["equity"]) if len(self.equity) else self.config.initial_cash


class ShortTermBacktester:
    """每日 Top5 等权 + 5 日持有/止盈止损的短线组合回测器。"""

    def __init__(self, config: ShortBacktestConfig | None = None, *, fee_config: Any = None) -> None:
        self.config = config or ShortBacktestConfig()
        self.fee_config = resolve_fee_config(fee_config)
        self.fee_model = FeeModel(self.fee_config)

    # ------------------------------------------------------------------
    def run(self, bars: pd.DataFrame) -> ShortBacktestResult:
        """跑短线组合回测（OOS 预测 + 每日选股 + 持仓管理）。

        Args:
            bars: 长表行情（date/symbol/OHLCV），建议 ≥ 2 年多标的。

        Returns:
            :class:`ShortBacktestResult`（净值/成交/指标/结论/模型报告）。
        """

        config = self.config
        model = ShortTermModel(config.model)
        report = model.walk_forward(bars)
        predictions = report.oos_predictions
        if predictions is None or predictions.empty:
            raise ValueError("短线模型没有样本外预测，无法回测")
        frame = normalize_bars(bars)
        dates = sorted(pd.to_datetime(predictions["date"], errors="coerce").dropna().dt.date.unique())
        if not dates:
            raise ValueError("短线模型预测日期为空")

        # 行情索引：symbol -> DataFrame（date 排序）+ 快速 OHLC 查询
        bars_by_symbol: dict[str, pd.DataFrame] = {}
        for symbol, group in frame.groupby("symbol", sort=True):
            group = group.sort_values("date").reset_index(drop=True)
            bars_by_symbol[str(symbol)] = group
        ohlc: dict[tuple[str, date], dict[str, float]] = {}
        for symbol, group in bars_by_symbol.items():
            for _, row in group.iterrows():
                ohlc[(symbol, row["date"])] = {
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "is_suspended": bool(row.get("is_suspended", False)),
                }
        # 预测透视图：date -> [(symbol, prob_1pct, prob_2pct, prob_3pct)]
        predictions_by_date: dict[date, list[tuple[str, float, float, float]]] = {}
        for day, group in predictions.groupby("date"):
            records: list[tuple[str, float, float, float]] = []
            for _, row in group.iterrows():
                records.append(
                    (
                        str(row["symbol"]),
                        float(row.get("prob_1pct", 0.0) or 0.0),
                        float(row.get("prob_2pct", 0.0) or 0.0),
                        float(row.get("prob_3pct", 0.0) or 0.0),
                    )
                )
            predictions_by_date[day] = sorted(records, key=lambda item: item[2], reverse=True)

        cash = float(config.initial_cash)
        positions: dict[str, dict[str, Any]] = {}
        pending_entries: list[str] = []
        trade_rows: list[dict[str, Any]] = []
        equity_rows: list[dict[str, Any]] = []
        total_fees = 0.0
        slippage = config.slippage_bps / 10_000.0

        for index, day in enumerate(dates):
            # 1) 昨日收盘选股 → 今日开盘买入（T+1）
            for symbol in pending_entries:
                if len(positions) >= config.top_n or symbol in positions:
                    continue
                quote = ohlc.get((symbol, day))
                if quote is None or quote["is_suspended"] or quote["open"] <= 0:
                    continue
                price = round_money(quote["open"] * (1 + slippage))
                if price <= 0:
                    continue
                equity_now = cash + self._position_value(positions, ohlc, day)
                target_cash = min(equity_now / max(config.top_n, 1), cash)
                lot = 200 if symbol.startswith(("688", "689")) else 100
                quantity = int(target_cash // (price * lot)) * lot
                if quantity <= 0:
                    continue
                amount = round_money(price * quantity)
                fee = self.fee_model.calculate("BUY", price, quantity)
                cost = round_money(amount + fee.total)
                if cost > cash + 1e-6:
                    continue
                cash = round_money(cash - cost)
                total_fees += fee.total
                positions[symbol] = {
                    "quantity": quantity,
                    "entry_price": price,
                    "entry_date": day,
                    "entry_index": index,
                    "buy_cost": cost,
                    "buy_fee": fee.total,
                    "take_profit_price": round_money(price * (1 + config.take_profit)),
                    "stop_loss_price": round_money(price * (1 - config.stop_loss)),
                }
            pending_entries = []

            # 2) 持仓退出（止盈/止损/到期；T+1：entry_index < index 才可卖）
            for symbol in list(positions):
                position = positions[symbol]
                if index <= int(position["entry_index"]):
                    continue
                quote = ohlc.get((symbol, day))
                if quote is None:
                    continue
                exit_price: float | None = None
                reason = ""
                if quote["high"] >= position["take_profit_price"] > 0:
                    exit_price = float(position["take_profit_price"])
                    reason = "止盈"
                elif quote["low"] <= position["stop_loss_price"] > 0:
                    exit_price = float(position["stop_loss_price"])
                    reason = "止损"
                elif index - int(position["entry_index"]) >= config.max_hold:
                    exit_price = float(quote["close"])
                    reason = "到期"
                if exit_price is None or exit_price <= 0:
                    continue
                sell_price = round_money(exit_price * (1 - slippage))
                quantity = int(position["quantity"])
                amount = round_money(sell_price * quantity)
                fee = self.fee_model.calculate("SELL", sell_price, quantity)
                proceeds = round_money(amount - fee.total)
                cash = round_money(cash + proceeds)
                total_fees += fee.total
                net_cost = float(position["buy_cost"])
                net_return = (proceeds - net_cost) / net_cost if net_cost > 0 else 0.0
                trade_rows.append(
                    {
                        "symbol": symbol,
                        "entry_date": position["entry_date"],
                        "exit_date": day,
                        "holding_days": index - int(position["entry_index"]),
                        "entry_price": position["entry_price"],
                        "exit_price": sell_price,
                        "quantity": quantity,
                        "buy_fee": round_money(position["buy_fee"]),
                        "sell_fee": round_money(fee.total),
                        "net_return": round(net_return, 6),
                        "exit_reason": reason,
                    }
                )
                positions.pop(symbol, None)

            # 3) 收盘估值
            position_value = self._position_value(positions, ohlc, day)
            equity = round_money(cash + position_value)
            equity_rows.append(
                {
                    "trade_date": day,
                    "equity": equity,
                    "cash": round_money(cash),
                    "position_value": position_value,
                }
            )

            # 4) 今日收盘选股 → 明日开盘买入（仅尚未持有的标的）
            candidates = predictions_by_date.get(day, [])
            held = set(positions)
            pending_entries = [
                symbol
                for symbol, _p1, p2, _p3 in candidates
                if p2 >= config.min_prob and symbol not in held
            ][: max(0, config.top_n - len(positions))]

        # 5) 指标与结论
        equity_frame = pd.DataFrame(equity_rows)
        equity_frame["drawdown"] = equity_frame["equity"] / equity_frame["equity"].cummax() - 1.0
        trades_frame = pd.DataFrame(trade_rows)
        metrics = self._compute_metrics(equity_frame, trades_frame, total_fees)
        conclusion = self._conclude(report, metrics)
        report.conclusion["criteria"]["max_drawdown_lt_10pct"] = bool(
            metrics["max_drawdown"] is not None and abs(float(metrics["max_drawdown"])) < 0.10
        )
        report.conclusion["usable"] = bool(
            report.conclusion["criteria"]["hit_rate_2pct_gt_55pct"]
            and report.conclusion["criteria"]["net_expectancy_positive"]
            and report.conclusion["criteria"]["max_drawdown_lt_10pct"]
        )
        if report.conclusion["usable"]:
            report.conclusion["summary"] = (
                f"达到判定线：OOS 命中率 {metrics['hit_rate_2pct']:.2%}、"
                f"扣费期望 {metrics['net_expectancy']:.2%}、最大回撤 {metrics['max_drawdown']:.2%}"
            )
        else:
            report.conclusion["summary"] = "不可用（研究基线）：" + "；".join(
                [
                    f"OOS 命中率 {self._pct(metrics['hit_rate_2pct'])}（需>55%）",
                    f"扣费期望 {self._pct(metrics['net_expectancy'])}（需>0）",
                    f"最大回撤 {self._pct(metrics['max_drawdown'])}（需<10%）",
                ]
            )
        notes = [
            "短线 v1 为研究基线：walk-forward OOS 预测 + T+1 开盘买入，费用/滑点复用 broker_sim",
            "未达判定线前不得用于实盘；结论见 docs/SHORT_TERM_MODEL_V1.md",
        ]
        return ShortBacktestResult(
            equity=equity_frame,
            trades=trades_frame,
            metrics=metrics,
            conclusion=report.conclusion,
            report=report,
            config=config,
            notes=notes,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _position_value(
        positions: dict[str, dict[str, Any]],
        ohlc: dict[tuple[str, date], dict[str, float]],
        day: date,
    ) -> float:
        total = 0.0
        for symbol, position in positions.items():
            quote = ohlc.get((symbol, day))
            price = float(quote["close"]) if quote is not None else float(position["entry_price"])
            total += price * int(position["quantity"])
        return round_money(total)

    @staticmethod
    def _pct(value: Any) -> str:
        try:
            return f"{float(value) * 100:.2f}%"
        except (TypeError, ValueError):
            return "—"

    def _compute_metrics(
        self, equity: pd.DataFrame, trades: pd.DataFrame, total_fees: float
    ) -> dict[str, Any]:
        base = compute_metrics(equity, trades, initial_cash=self.config.initial_cash)
        closed = trades
        if closed is not None and len(closed) > 0:
            returns = pd.to_numeric(closed["net_return"], errors="coerce").dropna()
            hit_rate = float((returns >= 0.02).mean())
            positive_rate = float((returns > 0).mean())
            expectancy = float(returns.mean())
            avg_hold = float(pd.to_numeric(closed["holding_days"], errors="coerce").mean())
        else:
            hit_rate = positive_rate = expectancy = avg_hold = None
        avg_equity = float(equity["equity"].mean()) if len(equity) else float(self.config.initial_cash)
        metrics = dict(base)
        metrics.update(
            {
                "hit_rate_2pct": _safe(hit_rate),
                "positive_rate": _safe(positive_rate),
                "net_expectancy": _safe(expectancy),
                "avg_holding_days": _safe(avg_hold),
                "trade_count": int(len(trades)) if trades is not None else 0,
                "total_fees": round_money(total_fees),
                "turnover_cost_pct": _safe(total_fees / avg_equity) if avg_equity > 0 else None,
                "final_equity": float(equity["equity"].iloc[-1]) if len(equity) else self.config.initial_cash,
            }
        )
        return metrics

    def _conclude(self, report: ShortTermReport, metrics: dict[str, Any]) -> dict[str, Any]:
        hit = report.metrics.get("hit_rate_2pct")
        expectancy = report.metrics.get("net_expectancy")
        criteria = {
            "hit_rate_2pct_gt_55pct": bool(hit is not None and float(hit) > 0.55),
            "net_expectancy_positive": bool(expectancy is not None and float(expectancy) > 0),
            "max_drawdown_lt_10pct": bool(
                metrics.get("max_drawdown") is not None
                and abs(float(metrics["max_drawdown"])) < 0.10
            ),
        }
        usable = all(criteria.values())
        summary = (
            "达到判定线（但需更多样本外验证后才可实盘）"
            if usable
            else "不可用（研究基线）"
        )
        return {
            "usable": bool(usable),
            "criteria": criteria,
            "summary": summary,
            "next_steps": list(report.conclusion.get("next_steps", [])),
        }


def _safe(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, 6)

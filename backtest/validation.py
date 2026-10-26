"""回测报告标准输出（M6 P1）：同仓位被动基准 + 样本外标记。

口径（本项目合规红线，见 ``config.yaml`` 的 ``backtest.price_adjust``）：

* 回测撮合/手数/资金**一律用不复权真实价**（``price_adjust=none``）；
* 本模块的被动基准也全部基于同一份不复权行情计算，不引入复权合成价；
* ``scripts/run_backtest.py`` 是**单区间回测**（非 walk-forward），因此报告中的
  ``oos`` 必须为 ``False``，控制台打印 ``样本外：否（…）``，禁止把单区间结果
  说成样本外。

模块职责
--------
1. :func:`compute_passive_benchmark` —— 同区间 / 同初始资金 / 同标的列表 /
   单票同目标仓位（默认 10%，与 ``BacktestEngine.default_position_pct`` 一致）的
   等权买入并持有被动基准，期间不调仓，剩余现金保留；买入费用与撮合同源
   （:class:`backtest.broker_sim.FeeModel`：佣金/印花/过户/规费 + 滑点）。
2. :func:`build_backtest_report` —— 把策略结果与被动基准合并成标准报告字典
   （JSON 用英文字段名，CSV 用中文字段名，字段见 :data:`REPORT_JSON_FIELDS` /
   :data:`REPORT_CSV_COLUMNS`）。
3. :func:`write_backtest_report` —— 落盘 ``runtime/exports/backtest_report_<run_id>.json``
   与 ``.csv``（UTF-8-SIG，Excel 可直接打开）。

样本外（OOS）说明
-----------------
仓库当前**没有**实现 walk-forward 切分（``backtest/validation.py`` 的 WF 部分属
P8 规划，见 ``docs/ARCHITECTURE.md``）；现有 walk-forward 实现在
``strategy/backtest_short.py``（短线）与 ``strategy/m6_research.py``（研究矩阵）。
``scripts/run_backtest.py`` 只做单区间回测，报告固定输出
:data:`OOS_MARKER_SINGLE_INTERVAL`，不得据此宣称“样本外有效”。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from backtest.broker_sim import (
    BOARD_STAR,
    FeeModel,
    apply_slippage,
    guess_board,
    resolve_fee_config,
    round_money,
)
from backtest.metrics import (
    annualized_return,
    daily_returns,
    max_drawdown,
    sharpe_ratio,
)

__all__ = [
    "PRICE_ADJUST_NONE",
    "PRICE_CALIBER_RAW",
    "PRICE_CALIBER_MOCK",
    "OOS_MARKER_SINGLE_INTERVAL",
    "OOS_NOTE_SINGLE_INTERVAL",
    "PASSIVE_BENCHMARK_METHOD",
    "REPORT_JSON_FIELDS",
    "REPORT_CSV_COLUMNS",
    "PassiveBenchmark",
    "compute_passive_benchmark",
    "build_backtest_report",
    "report_csv_row",
    "write_backtest_report",
]

# ---------------------------------------------------------------------------
# 一、口径常量（集中在此，脚本/测试/文档都引用，避免各处文案漂移）
# ---------------------------------------------------------------------------

#: 回测价格口径：``none`` = 不复权真实价（撮合/手数/资金/被动基准统一口径）
PRICE_ADJUST_NONE = "none"
#: 控制台/报告中的价格口径中文说明（真实行情）
PRICE_CALIBER_RAW = "不复权真实价（price_adjust=none）"
#: Mock 演示数据的价格口径说明（绝不可当作真实行情）
PRICE_CALIBER_MOCK = "Mock 离线示例价（price_adjust=none 语义；非真实行情）"

#: 单区间回测的样本外标记（必须原样出现在报告与控制台）
OOS_MARKER_SINGLE_INTERVAL = "否（单区间回测；walk-forward 样本外请用 backtest/validation.py）"
#: 单区间回测的样本外说明（JSON 字段 ``oos_note``）
OOS_NOTE_SINGLE_INTERVAL = (
    "单区间回测（非 walk-forward），结果不构成样本外证据；"
    "walk-forward 样本外实现见 strategy/backtest_short.py 与 strategy/m6_research.py"
    "（backtest/validation.py 的 WF 切分为 P8 规划）。"
)

#: 被动基准定义文案（原样打印/写入报告，避免“同仓位”口径被误读）
PASSIVE_BENCHMARK_METHOD = (
    "同区间、同初始资金、同标的列表、单票目标仓位 = 初始资金 × position_pct%"
    "（默认 10%，与 BacktestEngine.default_position_pct 一致）；"
    "期初（首个可用交易日收盘）按不复权收盘价 + 买入滑点等权买入并持有，期间不调仓；"
    "买入按整手取整，目标不足 1 手时沿用引擎最小 1 手规则"
    "（1 手成本 ≤ 单票仓位上限 20% 才按 1 手买入）；"
    "费用与撮合同源（broker_sim.FeeModel：佣金/印花/过户/规费 + 滑点）；剩余现金保留。"
)

#: 报告 JSON 必含字段（顺序即文档顺序，测试按此断言齐全）
REPORT_JSON_FIELDS: tuple[str, ...] = (
    "report_version",
    "run_id",
    "strategy",
    "params",
    "symbols",
    "start_date",
    "end_date",
    "price_adjust",
    "price_caliber",
    "strategy_total_return",
    "strategy_total_return_pct",
    "passive_total_return",
    "passive_total_return_pct",
    "excess_return_pp",
    "strategy_annual_return",
    "passive_annual_return",
    "strategy_max_drawdown",
    "passive_max_drawdown",
    "strategy_sharpe",
    "passive_sharpe",
    "initial_cash",
    "final_equity",
    "passive_final_equity",
    "strategy_total_fees",
    "passive_total_fees",
    "n_days",
    "oos",
    "oos_marker",
    "oos_note",
    "passive_method",
    "engine_price_mode",
    "is_mock",
    "generated_at",
)

#: CSV 列名（中文，顺序即输出顺序；数值字段带单位，便于 Excel 直接阅读）
REPORT_CSV_COLUMNS: tuple[str, ...] = (
    "策略",
    "标的",
    "区间",
    "价格口径",
    "策略收益",
    "被动收益",
    "超额pp",
    "策略MDD",
    "被动MDD",
    "初始资金",
    "期末权益",
    "被动期末权益",
    "样本外",
    "生成时间",
    "run_id",
)

#: 报告格式版本（字段变更时递增）
REPORT_VERSION = "m6-p1.1"

#: 被动基准允许的最少权益行数（更少无法给出有意义的回撤/夏普）
_MIN_EQUITY_ROWS = 2


# ---------------------------------------------------------------------------
# 二、被动基准
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PassiveBenchmark:
    """同仓位被动基准的计算结果。

    Attributes:
        symbols: 标的（保持入参顺序，去重）。
        start_date / end_date: 被动基准的权益曲线首/末交易日（与策略权益曲线对齐）。
        initial_cash: 初始资金（元，与策略一致）。
        position_pct: 单票目标仓位（%，与引擎 ``position_pct`` 一致）。
        slippage_bps: 买入滑点（基点，与回测撮合一致）。
        buy_date: 实际执行期初买入的交易日（通常为 ``start_date``）。
        final_equity: 期末权益（元，按最后交易日收盘价估值）。
        total_return: 总收益率（小数，``final_equity / initial_cash - 1``）。
        annual_return: 年化收益率（小数，几何年化，252 交易日）。
        max_drawdown: 最大回撤（小数，负数）。
        sharpe: 夏普比率（无波动/样本不足为 ``None``）。
        total_fees: 期初买入支付的总费用（元；买入无印花税）。
        cash_remaining: 期末现金（元）。
        position_value: 期末持仓市值（元）。
        holdings: ``{symbol: 股数}``。
        equity: 权益曲线（``trade_date``/``equity``/``cash``/``position_value``）。
        n_days: 权益曲线交易日数（与策略 ``metrics.n_days`` 同口径）。
        notes: 计算过程中的说明（缺 bar、按最小 1 手调整、资金不足等）。
    """

    symbols: tuple[str, ...]
    start_date: date
    end_date: date
    initial_cash: float
    position_pct: float
    slippage_bps: float
    buy_date: date
    final_equity: float
    total_return: float
    annual_return: float
    max_drawdown: float
    sharpe: float | None
    total_fees: float
    cash_remaining: float
    position_value: float
    holdings: dict[str, int] = field(default_factory=dict)
    equity: pd.DataFrame = field(default_factory=pd.DataFrame, repr=False)
    n_days: int = 0
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """JSON 友好字典（不含权益曲线）。"""

        return {
            "symbols": list(self.symbols),
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "initial_cash": self.initial_cash,
            "position_pct": self.position_pct,
            "slippage_bps": self.slippage_bps,
            "buy_date": self.buy_date.isoformat(),
            "final_equity": self.final_equity,
            "total_return": self.total_return,
            "annual_return": self.annual_return,
            "max_drawdown": self.max_drawdown,
            "sharpe": self.sharpe,
            "total_fees": self.total_fees,
            "cash_remaining": self.cash_remaining,
            "position_value": self.position_value,
            "holdings": dict(self.holdings),
            "n_days": self.n_days,
            "notes": list(self.notes),
        }


def _as_date(value: Any, *, name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"被动基准：{name} 必须是 YYYY-MM-DD 日期，收到 {value!r}") from exc


def _board_lot_size(board: str) -> int:
    """板块最小买入单位（股）：科创板 200 股起，其余 100 股（与引擎一致）。"""

    return 200 if board == BOARD_STAR else 100


def _normalize_bars(bars: Any) -> pd.DataFrame:
    """把行情整理成 ``date/symbol/open/high/low/close/volume/amount`` 规范表。"""

    if not isinstance(bars, pd.DataFrame) or bars.empty:
        raise ValueError("被动基准：行情数据为空或不是 DataFrame")
    frame = bars.copy()
    date_column = next((name for name in ("date", "trade_date") if name in frame.columns), None)
    if date_column is None:
        raise ValueError("被动基准：行情缺少 date/trade_date 列")
    if "symbol" not in frame.columns:
        raise ValueError("被动基准：行情缺少 symbol 列")
    if "close" not in frame.columns:
        raise ValueError("被动基准：行情缺少 close 列")
    frame["date"] = pd.to_datetime(frame[date_column], errors="coerce").dt.date
    frame["symbol"] = frame["symbol"].astype(str).str.strip()
    frame = frame.dropna(subset=["date", "symbol"])
    for column in ("open", "high", "low", "close"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        else:
            frame[column] = float("nan")
    frame = frame.dropna(subset=["close"])
    frame = frame[frame["close"] > 0]
    if "is_suspended" in frame.columns:
        frame = frame[~frame["is_suspended"].fillna(False).astype(bool)]
    frame = frame.sort_values(["symbol", "date"]).reset_index(drop=True)
    if frame.empty:
        raise ValueError("被动基准：行情清洗后没有可用 bar")
    return frame


def _resolve_calendar(
    frame: pd.DataFrame,
    start: date,
    end: date,
    calendar: Sequence[Any] | None,
) -> list[date]:
    if calendar is None:
        days = [
            day
            for day in sorted(set(frame["date"].tolist()))
            if start <= day <= end
        ]
    else:
        days = []
        for raw in calendar:
            day = _as_date(raw, name="calendar")
            if start <= day <= end and day not in days:
                days.append(day)
        days.sort()
    if len(days) < _MIN_EQUITY_ROWS:
        raise ValueError(
            f"被动基准：{start} ~ {end} 内权益曲线不足 {_MIN_EQUITY_ROWS} 个交易日，无法计算"
        )
    return days


def _position_pct(value: Any) -> float:
    try:
        pct = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"被动基准：position_pct 必须是数值，收到 {value!r}") from exc
    if not math.isfinite(pct) or pct <= 0 or pct > 100:
        raise ValueError(f"被动基准：position_pct 必须在 (0, 100] 之间，收到 {value!r}")
    return pct


def compute_passive_benchmark(
    bars: Any,
    symbols: Sequence[str],
    start: Any,
    end: Any,
    *,
    initial_cash: float,
    position_pct: float = 10.0,
    fee_config: Any = None,
    slippage_bps: float = 5.0,
    calendar: Sequence[Any] | None = None,
) -> PassiveBenchmark:
    """计算「同仓位被动基准」：等权买入并持有，期间不调仓，剩余现金保留。

    Args:
        bars: 与策略回测**同一份**不复权行情（DataFrame，含 date/symbol/open/close 等）。
        symbols: 标的列表（与策略一致；顺序保留）。
        start / end: 回测区间（含首尾）。
        initial_cash: 初始资金（元，与策略一致）。
        position_pct: 单票目标仓位（%，默认 10，与 ``BacktestEngine`` 默认一致）。
        fee_config: 费率配置（``None`` 用 ``core.models.FeeConfig`` 默认；建议传
            策略结果的 ``result.fee_config`` 保证同源）。
        slippage_bps: 买入滑点（基点，默认 5bp，与回测撮合一致）。
        calendar: 可选的交易日序列（建议传策略权益曲线的 ``trade_date``），
            保证被动与策略使用同一交易日历；``None`` 时取行情并集。

    Returns:
        :class:`PassiveBenchmark`。

    Raises:
        ValueError: 行情为空/缺列、区间交易日不足、初始资金/仓位非法（中文消息）。

    期初买入口径：
        * 用**首个可用交易日收盘价**（不复权）+ 买入滑点；
        * 单票目标市值 = ``initial_cash × position_pct%``，按板块整手向下取整；
        * 目标不足 1 手且 1 手成本 ≤ ``20% × initial_cash``（引擎单票上限默认值）
          时按最小 1 手买入（与引擎 ``RQ_WARN_POSITION_MIN_LOT`` 规则一致）；
        * 买入费用用 :class:`backtest.broker_sim.FeeModel`，与撮合同源；
        * 期间不调仓、不卖出（因此不产生印花税），期末按最后收盘价估值。
    """

    begin = _as_date(start, name="start")
    finish = _as_date(end, name="end")
    if begin > finish:
        raise ValueError(f"被动基准：开始日期 {begin} 不能晚于结束日期 {finish}")
    try:
        cash0 = float(initial_cash)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"被动基准：initial_cash 必须是数值，收到 {initial_cash!r}") from exc
    if not math.isfinite(cash0) or cash0 <= 0:
        raise ValueError(f"被动基准：initial_cash 必须为正数，收到 {initial_cash!r}")
    pct = _position_pct(position_pct)
    try:
        slippage = float(slippage_bps)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"被动基准：slippage_bps 必须是数值，收到 {slippage_bps!r}") from exc
    if not math.isfinite(slippage) or slippage < 0:
        raise ValueError(f"被动基准：slippage_bps 必须是非负有限数值，收到 {slippage_bps!r}")

    symbol_list = [str(item).strip() for item in symbols if str(item).strip()]
    symbol_list = list(dict.fromkeys(symbol_list))
    if not symbol_list:
        raise ValueError("被动基准：标的列表不能为空")

    frame = _normalize_bars(bars)
    frame = frame[frame["symbol"].isin(set(symbol_list))].copy()
    if frame.empty:
        raise ValueError(f"被动基准：行情中没有标的 {'、'.join(symbol_list)} 的数据")
    days = _resolve_calendar(frame, begin, finish, calendar)

    fees = resolve_fee_config(fee_config)
    fee_model = FeeModel(fees)
    cash = round_money(cash0)
    notes: list[str] = []
    holdings: dict[str, int] = {}
    total_fees = 0.0
    buy_date: date | None = None

    for symbol in symbol_list:
        group = frame[frame["symbol"] == symbol]
        if group.empty:
            notes.append(f"{symbol} 在 {begin}~{finish} 内无行情，被动基准未持有")
            continue
        by_date = {row["date"]: row for _, row in group.iterrows()}
        chosen: date | None = None
        for day in days:
            if day in by_date:
                chosen = day
                break
        if chosen is None:
            notes.append(f"{symbol} 在权益曲线交易日历内无可用 bar，被动基准未持有")
            continue
        row = by_date[chosen]
        price = apply_slippage(float(row["close"]), "BUY", slippage)
        board = guess_board(symbol)
        lot = _board_lot_size(board)
        target_value = cash0 * pct / 100.0
        quantity = int(target_value // price) // lot * lot
        if quantity <= 0 and price * lot <= cash0 * 0.20 + 1e-9:
            quantity = lot
            notes.append(
                f"{symbol} 目标仓位约 {target_value:,.0f} 元不足 1 手"
                f"（{lot} 股约 {price * lot:,.0f} 元），按引擎最小 1 手规则买入"
            )
        while quantity > 0:
            amount = round_money(price * quantity)
            fee = fee_model.calculate("BUY", price, quantity).total
            if amount + fee <= cash + 1e-9:
                break
            quantity -= lot
        if quantity <= 0:
            notes.append(
                f"{symbol} 目标仓位不足 1 手且 1 手成本超过单票上限（20% × 初始资金），"
                "被动基准未持有"
            )
            continue
        amount = round_money(price * quantity)
        fee = fee_model.calculate("BUY", price, quantity).total
        cash = round_money(cash - amount - fee)
        total_fees = round_money(total_fees + fee)
        holdings[symbol] = quantity
        buy_date = chosen if buy_date is None else min(buy_date, chosen)

    if buy_date is None:
        buy_date = days[0]
        notes.append("所有标的都未买入，被动基准为全现金（收益 0%）")

    # 逐交易日估值：持仓按最近收盘价 mark-to-market（停牌/缺 bar 沿用最近收盘）
    closes = {
        symbol: group.set_index("date")["close"].sort_index()
        for symbol, group in frame.groupby("symbol")
    }
    rows: list[dict[str, Any]] = []
    for day in days:
        position_value = 0.0
        for symbol, quantity in holdings.items():
            series = closes.get(symbol)
            if series is None or len(series) == 0:
                continue
            history = series[series.index <= day]
            if len(history) == 0:
                continue
            position_value += quantity * float(history.iloc[-1])
        position_value = round_money(position_value)
        rows.append(
            {
                "trade_date": day,
                "equity": round_money(cash + position_value),
                "cash": cash,
                "position_value": position_value,
            }
        )

    equity = pd.DataFrame(rows)
    values = equity["equity"].astype("float64")
    final_equity = float(values.iloc[-1])
    total_return = float(final_equity / cash0 - 1.0)
    returns = daily_returns(
        pd.Series(values.to_numpy(), index=pd.to_datetime(equity["trade_date"])),
        initial_cash=cash0,
    ).dropna()
    annual = annualized_return(total_return, int(len(values)))
    mdd = max_drawdown(pd.Series(values.to_numpy()))
    sharpe = sharpe_ratio(returns.to_numpy())
    position_value = float(equity["position_value"].iloc[-1])

    return PassiveBenchmark(
        symbols=tuple(symbol_list),
        start_date=days[0],
        end_date=days[-1],
        initial_cash=cash0,
        position_pct=pct,
        slippage_bps=slippage,
        buy_date=buy_date,
        final_equity=round_money(final_equity),
        total_return=total_return,
        annual_return=annual,
        max_drawdown=mdd,
        sharpe=sharpe,
        total_fees=total_fees,
        cash_remaining=cash,
        position_value=position_value,
        holdings=dict(holdings),
        equity=equity,
        n_days=int(len(equity)),
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# 三、标准报告（策略 vs 被动 + 样本外标记）
# ---------------------------------------------------------------------------


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    return _as_date(value, name="date").isoformat()


def build_backtest_report(
    result: Any,
    passive: PassiveBenchmark,
    *,
    price_caliber: str = PRICE_CALIBER_RAW,
    oos: bool = False,
    oos_marker: str | None = None,
    oos_note: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """把策略回测结果与被动基准合并成标准报告字典。

    Args:
        result: :class:`backtest.engine.BacktestResult`（或同字段对象）。
        passive: :func:`compute_passive_benchmark` 的结果。
        price_caliber: 价格口径说明（真实行情用 :data:`PRICE_CALIBER_RAW`）。
        oos: 是否样本外（单区间回测必须为 ``False``）。
        oos_marker: 样本外标记文案；``None`` 时按 ``oos`` 自动选择。
        oos_note: 样本外说明；``None`` 时按 ``oos`` 自动选择。
        generated_at: 生成时间（ISO 8601）；``None`` 时取当前本地时间。

    Returns:
        可直接 ``json.dumps`` 的报告字典（字段见 :data:`REPORT_JSON_FIELDS`）。
    """

    metrics = dict(getattr(result, "metrics", {}) or {})
    params = dict(getattr(result, "params", {}) or {})
    strategy_total = float(metrics.get("total_return") or 0.0)
    passive_total = float(passive.total_return)
    excess_pp = (strategy_total - passive_total) * 100.0
    strategy_mdd = float(metrics.get("max_drawdown") or 0.0)
    metadata = dict(getattr(result, "metadata", {}) or {})
    run_id = str(getattr(result, "run_id", "") or "")
    if not run_id:
        raise ValueError("报告生成失败：策略结果缺少 run_id")
    if oos_marker is None:
        oos_marker = "是（walk-forward 样本外）" if oos else OOS_MARKER_SINGLE_INTERVAL
    if oos_note is None:
        oos_note = (
            "walk-forward 样本外结果"
            if oos
            else OOS_NOTE_SINGLE_INTERVAL
        )

    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "run_id": run_id,
        "strategy": str(getattr(result, "strategy_key", "") or ""),
        "params": params,
        "symbols": [str(item) for item in (getattr(result, "symbols", []) or [])],
        "start_date": _iso(getattr(result, "start_date", passive.start_date)),
        "end_date": _iso(getattr(result, "end_date", passive.end_date)),
        "price_adjust": PRICE_ADJUST_NONE,
        "price_caliber": str(price_caliber),
        "strategy_total_return": strategy_total,
        "strategy_total_return_pct": round(strategy_total * 100.0, 6),
        "passive_total_return": passive_total,
        "passive_total_return_pct": round(passive_total * 100.0, 6),
        "excess_return_pp": round(excess_pp, 6),
        "strategy_annual_return": metrics.get("annual_return"),
        "passive_annual_return": passive.annual_return,
        "strategy_max_drawdown": strategy_mdd,
        "passive_max_drawdown": float(passive.max_drawdown),
        "strategy_sharpe": metrics.get("sharpe"),
        "passive_sharpe": passive.sharpe,
        "initial_cash": float(getattr(result, "initial_cash", passive.initial_cash)),
        "final_equity": metrics.get(
            "final_equity", float(getattr(result, "final_equity", 0.0) or 0.0)
        ),
        "passive_final_equity": passive.final_equity,
        "strategy_total_fees": metrics.get("total_fees"),
        "passive_total_fees": passive.total_fees,
        "n_days": int(metrics.get("n_days") or passive.n_days),
        "oos": bool(oos),
        "oos_marker": str(oos_marker),
        "oos_note": str(oos_note),
        "passive_method": PASSIVE_BENCHMARK_METHOD,
        "engine_price_mode": metadata.get("price_mode"),
        "is_mock": bool(metadata.get("is_mock", getattr(result, "is_mock", False))),
        "generated_at": generated_at or datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    missing = [key for key in REPORT_JSON_FIELDS if key not in report]
    if missing:
        raise ValueError(f"报告生成失败：缺少字段 {'、'.join(missing)}")
    return report


def _fmt_pct(value: Any, digits: int = 4) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(number):
        return ""
    return f"{number * 100.0:.{digits}f}%"


def _fmt_money(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(number):
        return ""
    return f"{number:.2f}"


def report_csv_row(report: dict[str, Any]) -> dict[str, str]:
    """把报告字典转成 CSV 行（中文列名，数值为带单位的字符串）。"""

    return {
        "策略": str(report.get("strategy", "")),
        "标的": "、".join(str(item) for item in report.get("symbols", []) or []),
        "区间": f"{report.get('start_date', '')} ~ {report.get('end_date', '')}",
        "价格口径": str(report.get("price_caliber", "")),
        "策略收益": _fmt_pct(report.get("strategy_total_return")),
        "被动收益": _fmt_pct(report.get("passive_total_return")),
        "超额pp": f"{float(report.get('excess_return_pp') or 0.0):+.4f}",
        "策略MDD": _fmt_pct(report.get("strategy_max_drawdown")),
        "被动MDD": _fmt_pct(report.get("passive_max_drawdown")),
        "初始资金": _fmt_money(report.get("initial_cash")),
        "期末权益": _fmt_money(report.get("final_equity")),
        "被动期末权益": _fmt_money(report.get("passive_final_equity")),
        "样本外": "是" if report.get("oos") else "否",
        "生成时间": str(report.get("generated_at", "")),
        "run_id": str(report.get("run_id", "")),
    }


def write_backtest_report(
    report: dict[str, Any],
    out_dir: str | Path,
) -> tuple[Path, Path]:
    """把标准报告落盘为 JSON + CSV，返回 ``(json_path, csv_path)``。

    * JSON：UTF-8，``ensure_ascii=False``，字段为英文字段名；
    * CSV：一行、中文列（:data:`REPORT_CSV_COLUMNS`），UTF-8-SIG（Excel 友好）。

    文件名：``backtest_report_<run_id>.json`` / ``.csv``。
    """

    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    run_id = str(report.get("run_id", "")).strip()
    if not run_id:
        raise ValueError("报告落盘失败：报告缺少 run_id")
    json_path = directory / f"backtest_report_{run_id}.json"
    csv_path = directory / f"backtest_report_{run_id}.csv"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    row = report_csv_row(report)
    pd.DataFrame([row], columns=list(REPORT_CSV_COLUMNS)).to_csv(
        csv_path, index=False, encoding="utf-8-sig"
    )
    return json_path, csv_path

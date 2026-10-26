"""QuantDesk 自动化任务（M3-02 / M3-03）—— 盘前准备 / 盘中扫描 / 盘后总结。

三个 job（SPEC §6.5 / §8.5）
---------------------------
- ``job_prepare``（默认 08:50）：刷新交易日历/自选池行情、数据校验、T+1 日初结算、记录熔断基线；
- ``job_scan``（默认 10:00）：跑启用策略 → 信号留痕 → **若自动交易已显式开启**才经
  ``OrderService.place``（唯一入口，内部必过 RiskGate）下单；否则只生成信号、不下单；
- ``job_summarize``（默认 15:30）：估值、资金曲线、当日盈亏/绩效、通知、日报。

安全口径（R3 要求）
------------------
- 自动交易开关 = ``config.automation.enabled``（默认 False）。**关闭时 scan 只生成信号，
  stats["auto_trading"]=False 并写审计**，绝不产生订单；
- 开关开启时仍受 ``RiskGate``（AUTO 单要求 ``allow_auto_orders``）与账户/风控约束；
- 非交易日 job 返回 ``SKIPPED``（不报错、不产生订单，AC-F5-04）；
- 单个策略/单只标的发生异常不影响其它策略，失败数记录在 stats。

依赖注入
--------
生产环境由 :func:`build_job_context` 装配真实依赖（DB 由 ``core/db.py`` 提供）；
单测可构造 :class:`JobContext` 注入假 provider / 假 order_service / 假通知，不触网。
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Callable

from core.logging import AuditAction, write_audit
from core.models import (
    JobResult,
    JobRunStatus,
    OrderRequest,
    ProviderStatus,
    SignalStatus,
    StrategyMode,
    build_client_order_id,
    now_local,
)

logger = logging.getLogger(__name__)

JOB_PREPARE = "prepare"
JOB_SCAN = "scan"
JOB_SUMMARIZE = "summarize"
JOB_KEYS: tuple[str, ...] = (JOB_PREPARE, JOB_SCAN, JOB_SUMMARIZE)
JOB_NAMES: dict[str, str] = {
    JOB_PREPARE: "盘前准备",
    JOB_SCAN: "盘中扫描",
    JOB_SUMMARIZE: "盘后总结",
}
AUDIT_ACTION_SCAN = "automation.scan"  # 自动化扫描审计动作（记录自动交易开关状态）

_SUMMARY_EVENT = "job.summarize"


# ---------------------------------------------------------------------------
# 依赖上下文
# ---------------------------------------------------------------------------


@dataclass
class JobContext:
    """三个 job 的依赖集合（生产装配 / 测试注入共用）。"""

    config: Any
    account_id: str = "ACC-PAPER"
    clock: Callable[[], datetime] = now_local
    bar_cache: Any = None
    provider: Any = None
    order_service: Any = None
    portfolio: Any = None
    notifier: Any = None
    strategy_reader: Any = None
    signal_writer: Any = None
    job_run_writer: Any = None
    market_provider: Callable[[], Any] | None = None
    is_trading_day: Callable[[date], bool] | None = None
    auto_trading_enabled: Callable[[], bool] | None = None
    event_publisher: Callable[[str, dict, str | None], None] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


_DEFAULT_CONTEXT: JobContext | None = None


def init_job_context(context: JobContext | None) -> JobContext | None:
    """注入全局 job 上下文（启动期/测试用）。"""
    global _DEFAULT_CONTEXT
    _DEFAULT_CONTEXT = context
    return _DEFAULT_CONTEXT


def get_job_context() -> JobContext:
    """获取全局 job 上下文；未初始化时延迟装配生产依赖。"""
    global _DEFAULT_CONTEXT
    if _DEFAULT_CONTEXT is None:
        _DEFAULT_CONTEXT = build_job_context()
    return _DEFAULT_CONTEXT


def reset_job_context() -> None:
    """清空全局上下文（测试隔离）。"""
    global _DEFAULT_CONTEXT
    _DEFAULT_CONTEXT = None


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------


def job_prepare(trace_id: str | None = None) -> JobResult:
    """盘前准备：刷新数据、数据校验、T+1 日初结算、熔断基线。"""
    return _run_job(JOB_PREPARE, _do_prepare, trace_id)


def job_scan(trace_id: str | None = None) -> JobResult:
    """盘中扫描：策略信号 → （自动交易开启时）风控 → 订单；关闭时只留信号。"""
    return _run_job(JOB_SCAN, _do_scan, trace_id)


def job_summarize(trace_id: str | None = None) -> JobResult:
    """盘后总结：估值/资金曲线/盈亏/通知/日报。"""
    return _run_job(JOB_SUMMARIZE, _do_summarize, trace_id)


def run_job(job_key: str, trace_id: str | None = None) -> JobResult:
    """按 job key 分发执行（调度器/手动触发共用）。"""
    key = str(job_key or "").strip().lower()
    mapping: dict[str, Callable[..., JobResult]] = {
        JOB_PREPARE: job_prepare,
        JOB_SCAN: job_scan,
        JOB_SUMMARIZE: job_summarize,
    }
    if key not in mapping:
        return JobResult(
            job_key=key or "unknown",
            trace_id=trace_id,
            status=JobRunStatus.FAILED,
            message=f"未知任务：{job_key!r}（只支持 {', '.join(JOB_KEYS)}）",
            error="任务 key 非法",
        )
    return mapping[key](trace_id)


def _run_job(job_key: str, func: Callable[[JobContext, str], JobResult], trace_id: str | None) -> JobResult:
    """统一执行外壳：计时、异常兜底、写 job_runs、发 job.* 事件。"""
    trace = (trace_id or "").strip() or uuid.uuid4().hex
    started = now_local()
    _publish_event(None, "job.started", {"job_key": job_key, "trace_id": trace}, trace)
    try:
        ctx = get_job_context()
        result = func(ctx, trace)
    except Exception as exc:  # noqa: BLE001 - 任何 job 失败都不得影响下一次调度
        logger.error("自动化任务失败：job=%s trace=%s", job_key, trace, exc_info=True)
        result = JobResult(
            job_key=job_key,
            trace_id=trace,
            status=JobRunStatus.FAILED,
            message=f"任务执行失败：{type(exc).__name__}: {exc}",
            error=f"{type(exc).__name__}: {exc}",
        )
        _publish_event(None, "job.failed", {"job_key": job_key, "trace_id": trace, "error": result.error}, trace)
    finished = now_local()
    result.job_key = job_key
    result.trace_id = trace
    result.started_at = started
    result.finished_at = finished
    result.duration_ms = round((finished - started).total_seconds() * 1000.0, 2)
    try:
        ctx = get_job_context()
        _record_job_run(ctx, result)
        _publish_event(
            ctx,
            "job.finished",
            {"job_key": job_key, "trace_id": trace, "stats": result.stats, "status": str(result.status)},
            trace,
        )
    except Exception:  # noqa: BLE001 - 记录失败不影响 job 结果
        logger.debug("记录 job_runs/事件失败：job=%s", job_key, exc_info=True)
    return result


# ---------------------------------------------------------------------------
# 三个 job 的具体实现
# ---------------------------------------------------------------------------


def _do_prepare(ctx: JobContext, trace: str) -> JobResult:
    cfg = ctx.config
    today = ctx.clock().date()
    if not _is_trading_day(ctx, today):
        return _skipped(JOB_PREPARE, trace, f"{today} 非交易日，跳过盘前准备")

    stats: dict[str, Any] = {"trade_date": today.isoformat(), "errors": []}
    universe = list(getattr(getattr(cfg, "data", None), "universe", []) or [])
    stats["universe"] = len(universe)

    if ctx.bar_cache is not None and universe:
        try:
            start = today - timedelta(days=max(30, int(getattr(cfg.data, "history_years", 5) or 5) * 365))
            report = ctx.bar_cache.update(universe, start, today)
            stats["bars_added"] = int(getattr(report, "added", 0) or 0)
            stats["bars_updated"] = int(getattr(report, "updated", 0) or 0)
            if hasattr(ctx.bar_cache, "validate"):
                validation = ctx.bar_cache.validate(universe, start, today)
                stats["data_issues"] = int(getattr(validation, "total_issues", 0) or 0)
        except Exception as exc:  # noqa: BLE001 - 单步失败不中断
            stats["errors"].append(f"行情刷新失败：{type(exc).__name__}: {exc}")

    # T+1 日初结算（可用数量 = 持仓 - 冻结；今日买入清零）
    if ctx.portfolio is not None:
        try:
            ctx.portfolio.settle_new_day(ctx.account_id, today)
            stats["settled"] = True
        except Exception as exc:  # noqa: BLE001
            stats["errors"].append(f"日初结算失败：{type(exc).__name__}: {exc}")

    # 估值 + 熔断基线（当日开盘总资产）
    prices = _latest_prices(ctx, universe)
    if ctx.portfolio is not None and prices:
        try:
            account = ctx.portfolio.mark_prices(ctx.account_id, prices)
            point = ctx.portfolio.snapshot_equity(ctx.account_id, today, prices)
            stats["daily_open_asset"] = float(getattr(account, "total_asset", 0.0))
            stats["equity"] = point
        except Exception as exc:  # noqa: BLE001
            stats["errors"].append(f"估值/基线失败：{type(exc).__name__}: {exc}")

    _audit(ctx, "automation.prepare", trace, after=stats)
    return _ok(JOB_PREPARE, trace, f"盘前准备完成（自选池 {len(universe)} 只）", stats)


def _do_scan(ctx: JobContext, trace: str) -> JobResult:
    cfg = ctx.config
    today = ctx.clock().date()
    if not _is_trading_day(ctx, today):
        return _skipped(JOB_SCAN, trace, f"{today} 非交易日，跳过盘中扫描")

    auto_trading = _auto_trading_enabled(ctx)
    stats: dict[str, Any] = {
        "trade_date": today.isoformat(),
        "auto_trading": auto_trading,
        "signals": 0,
        "orders": 0,
        "blocked": 0,
        "failed_instances": 0,
        "skipped_instances": 0,
        "details": [],
    }
    instances = _reader_call(ctx.strategy_reader, "list_enabled")
    if not instances:
        _audit(
            ctx,
            AUDIT_ACTION_SCAN,
            trace,
            after={"auto_trading": auto_trading, "signals": 0, "reason": "无启用的策略实例"},
        )
        return _ok(
            JOB_SCAN,
            trace,
            "盘中扫描完成：无启用的策略实例（自动交易%s）" % ("已开启" if auto_trading else "未开启，仅信号"),
            stats,
        )

    positions = _safe_positions(ctx)
    account = _safe_account(ctx)
    for instance in instances:
        try:
            _scan_instance(ctx, instance, trace, today, positions, account, auto_trading, stats)
        except Exception as exc:  # noqa: BLE001 - 单策略失败不影响其它策略
            stats["failed_instances"] += 1
            stats["details"].append(
                {"instance": _instance_field(instance, "name", ""), "error": f"{type(exc).__name__}: {exc}"}
            )
            logger.error("策略实例扫描失败：%s", _instance_field(instance, "name", ""), exc_info=True)

    if not auto_trading:
        _audit(
            ctx,
            AUDIT_ACTION_SCAN,
            trace,
            after={
                "auto_trading": False,
                "signals": stats["signals"],
                "orders": 0,
                "reason": "automation.enabled=false：只生成信号，不自动下单",
            },
        )
    message = (
        f"盘中扫描完成：信号 {stats['signals']} 条，"
        + (f"下单 {stats['orders']} 笔，风控拦截 {stats['blocked']} 笔" if auto_trading else "自动交易未开启（仅信号，未下单）")
    )
    return _ok(JOB_SCAN, trace, message, stats)


def _do_summarize(ctx: JobContext, trace: str) -> JobResult:
    cfg = ctx.config
    today = ctx.clock().date()
    if not _is_trading_day(ctx, today):
        return _skipped(JOB_SUMMARIZE, trace, f"{today} 非交易日，跳过盘后总结")

    universe = list(getattr(getattr(cfg, "data", None), "universe", []) or [])
    prices = _latest_prices(ctx, universe)
    stats: dict[str, Any] = {"trade_date": today.isoformat(), "errors": []}
    equity_point: dict[str, Any] = {}
    if ctx.portfolio is not None:
        try:
            if prices:
                ctx.portfolio.mark_prices(ctx.account_id, prices)
            equity_point = ctx.portfolio.snapshot_equity(ctx.account_id, today, prices or None)
            account = ctx.portfolio.get_account(ctx.account_id)
            stats.update(
                {
                    "total_asset": float(account.total_asset),
                    "cash": float(account.cash),
                    "realized_pnl": float(ctx.portfolio.realized_pnl(ctx.account_id)),
                    "unrealized_pnl": float(ctx.portfolio.unrealized_pnl(ctx.account_id, prices or None)),
                    "daily_pnl": equity_point.get("daily_pnl", 0.0),
                    "daily_return_pct": equity_point.get("daily_return", 0.0),
                }
            )
        except Exception as exc:  # noqa: BLE001
            stats["errors"].append(f"盘后结算失败：{type(exc).__name__}: {exc}")

    report_title = f"QuantDesk 盘后总结 {today.isoformat()}"
    report_body = (
        f"总资产 {stats.get('total_asset', 0.0):,.2f} 元；"
        f"当日盈亏 {stats.get('daily_pnl', 0.0):+,.2f} 元"
        f"（{stats.get('daily_return_pct', 0.0):+.2f}%）；"
        f"已实现 {stats.get('realized_pnl', 0.0):+,.2f} 元；"
        f"未实现 {stats.get('unrealized_pnl', 0.0):+,.2f} 元"
    )
    stats["report"] = report_body
    if ctx.notifier is not None:
        try:
            report = ctx.notifier.send(_SUMMARY_EVENT, report_title, report_body)
            stats["notify_status"] = str(getattr(report, "status", "UNKNOWN"))
        except Exception as exc:  # noqa: BLE001 - 通知失败不影响总结
            stats["errors"].append(f"通知发送失败：{type(exc).__name__}: {exc}")
    _audit(ctx, "automation.summarize", trace, after=stats)
    return _ok(JOB_SUMMARIZE, trace, "盘后总结完成：" + report_body, stats)


# ---------------------------------------------------------------------------
# 扫描单个策略实例（信号 → 可选下单）
# ---------------------------------------------------------------------------


def _scan_instance(
    ctx: JobContext,
    instance: Any,
    trace: str,
    today: date,
    positions: dict[str, Any],
    account: Any,
    auto_trading: bool,
    stats: dict[str, Any],
) -> None:
    strategy_key = str(_instance_field(instance, "strategy_key", "") or "").strip()
    instance_id = str(_instance_field(instance, "id", "") or "")
    params = _instance_field(instance, "params", {}) or {}
    symbols = list(_instance_field(instance, "universe", []) or [])
    mode = str(_instance_field(instance, "mode", StrategyMode.OFF.value)).upper()
    if not strategy_key or not symbols:
        stats["skipped_instances"] += 1
        return
    # 仅“模拟盘/自动”模式参与交易；“仅信号”模式即使 automation.enabled 也不下单
    trade_mode = mode in (StrategyMode.PAPER.value, StrategyMode.AUTO.value)

    bars = _load_bars(ctx, symbols, today)
    if not bars:
        stats["skipped_instances"] += 1
        return

    from strategy.base import Context, create_strategy  # 延迟导入：避免与策略层循环依赖

    strategy = create_strategy(strategy_key, params)
    context = Context(
        now=ctx.clock(),
        bars={symbol: frame for symbol, frame in bars.items() if frame is not None and len(frame) > 0},
        positions=positions,
        account=account,
        params=dict(params),
        is_backtest=False,
    )
    signals = list(strategy.on_bar(context) or [])
    position_pct = float(params.get("position_pct", 10.0) or 10.0)
    for signal in signals:
        stats["signals"] += 1
        _handle_signal(
            ctx, instance_id, symbols, signal, trace, today,
            auto_trading and trade_mode, bars, stats, position_pct=position_pct,
        )


def _handle_signal(
    ctx: JobContext,
    instance_id: str,
    universe: list[str],
    signal: Any,
    trace: str,
    today: date,
    auto_trading: bool,
    bars: dict[str, Any],
    stats: dict[str, Any],
    *,
    position_pct: float = 10.0,
) -> None:
    symbol = str(getattr(signal, "symbol", "") or "").strip().upper()
    side = str(getattr(getattr(signal, "side", None), "value", getattr(signal, "side", ""))).upper()
    price = _signal_price(ctx, signal, symbol, bars)
    record = {
        "trace_id": trace,
        "strategy_instance_id": instance_id or None,
        "symbol": symbol,
        "side": side,
        "strength": float(getattr(signal, "strength", 1.0) or 1.0),
        "price_hint": price,
        "reason": str(getattr(signal, "reason", "") or ""),
        "signal_time": ctx.clock(),
        "status": SignalStatus.GENERATED.value,
    }
    signal_id = _writer_call(ctx.signal_writer, "record", record)

    if not auto_trading:
        stats["details"].append({"symbol": symbol, "side": side, "action": "signal_only", "signal_id": signal_id})
        return
    if ctx.order_service is None:
        stats["details"].append({"symbol": symbol, "side": side, "action": "no_order_service", "signal_id": signal_id})
        return
    if price is None or price <= 0:
        stats["details"].append({"symbol": symbol, "side": side, "action": "skipped_no_price", "signal_id": signal_id})
        return

    quantity = _size_quantity(ctx, symbol, side, price, position_pct=position_pct)
    if quantity <= 0:
        stats["details"].append({"symbol": symbol, "side": side, "action": "skipped_zero_qty", "signal_id": signal_id})
        return
    request = OrderRequest(
        account_id=ctx.account_id,
        symbol=symbol,
        side=side,
        order_type="LIMIT",
        price=round(float(price), 2),
        quantity=int(quantity),
        source="AUTO",
        strategy_instance_id=instance_id or None,
        client_order_id=build_client_order_id(instance_id or "auto", symbol, today, side),
        trace_id=trace,
        note=f"自动化扫描下单（策略实例 {instance_id or '-'}）",
    )
    try:
        result = ctx.order_service.place(request)
    except Exception as exc:  # noqa: BLE001 - 单笔失败不影响其它信号
        stats["details"].append({"symbol": symbol, "side": side, "action": "order_error", "error": str(exc)})
        _writer_call(ctx.signal_writer, "update", signal_id, SignalStatus.IGNORED.value, None)
        return
    if bool(getattr(result, "success", False)):
        stats["orders"] += 1
        _writer_call(ctx.signal_writer, "update", signal_id, SignalStatus.ORDERED.value, getattr(result, "order_id", None))
        stats["details"].append(
            {"symbol": symbol, "side": side, "action": "ordered", "order_id": getattr(result, "order_id", None)}
        )
    else:
        stats["blocked"] += 1
        _writer_call(
            ctx.signal_writer, "update", signal_id, SignalStatus.RISK_BLOCKED.value, getattr(result, "order_id", None)
        )
        stats["details"].append(
            {
                "symbol": symbol,
                "side": side,
                "action": "risk_blocked",
                "reason_code": getattr(result, "reason_code", None),
                "message": str(getattr(result, "message", ""))[:120],
            }
        )


# ---------------------------------------------------------------------------
# 数据/数量辅助
# ---------------------------------------------------------------------------


def _latest_prices(ctx: JobContext, symbols: list[str]) -> dict[str, float]:
    """取最新价：优先快照 provider，其次缓存日线最后收盘。"""
    prices: dict[str, float] = {}
    if ctx.provider is not None and symbols:
        try:
            snapshot = ctx.provider.get_snapshot(list(symbols))
            if snapshot is not None and len(snapshot) > 0:
                for _, row in snapshot.iterrows():
                    symbol = str(row.get("symbol") or "").upper()
                    price = row.get("last")
                    if symbol and price is not None:
                        prices[symbol] = float(price)
        except Exception:  # noqa: BLE001
            logger.debug("快照取价失败，回退日线收盘价", exc_info=True)
    if len(prices) < len(symbols) and ctx.bar_cache is not None:
        try:
            frame = ctx.bar_cache.load(symbols, ctx.clock().date() - timedelta(days=20), ctx.clock().date())
            if frame is not None and len(frame) > 0:
                for symbol, group in frame.groupby("symbol"):
                    last = group.sort_values("date").iloc[-1]
                    if float(last["close"]) > 0:
                        prices[str(symbol).upper()] = float(last["close"])
        except Exception:  # noqa: BLE001
            logger.debug("缓存取价失败", exc_info=True)
    return prices


def _load_bars(ctx: JobContext, symbols: list[str], today: date) -> dict[str, Any]:
    """从缓存加载历史日线（引擎侧负责“截至今日”切片）。"""
    if ctx.bar_cache is None:
        return {}
    try:
        start = today - timedelta(days=365 * 2)
        frame = ctx.bar_cache.load(list(symbols), start, today)
    except Exception:  # noqa: BLE001
        logger.error("加载历史行情失败", exc_info=True)
        return {}
    if frame is None or len(frame) == 0:
        return {}
    result: dict[str, Any] = {}
    for symbol, group in frame.groupby("symbol"):
        result[str(symbol).upper()] = group.sort_values("date").reset_index(drop=True)
    return result


def _signal_price(ctx: JobContext, signal: Any, symbol: str, bars: dict[str, Any]) -> float | None:
    hint = getattr(signal, "price_hint", None)
    try:
        if hint is not None and float(hint) > 0:
            return float(hint)
    except (TypeError, ValueError):
        pass
    frame = bars.get(symbol)
    if frame is not None and len(frame) > 0:
        try:
            value = float(frame.iloc[-1]["close"])
            if value > 0:
                return value
        except (KeyError, TypeError, ValueError):
            pass
    prices = _latest_prices(ctx, [symbol])
    return prices.get(symbol)


def _size_quantity(ctx: JobContext, symbol: str, side: str, price: float, *, position_pct: float = 10.0) -> int:
    """数量估算：买入按策略目标仓位/可买资金整手；卖出用可卖数量（全平）。"""
    if side.upper() == "SELL":
        position = _position_of(ctx, symbol)
        if position is None:
            return 0
        available = int(getattr(position, "available_quantity", 0) or 0)
        return available
    # 买入：目标单票仓位（取策略实例参数 position_pct，默认 10%）
    try:
        account = _safe_account(ctx)
        total_asset = float(getattr(account, "total_asset", 0.0) or 0.0)
        cash = float(getattr(account, "cash", 0.0) or 0.0)
        target_value = total_asset * position_pct / 100.0
        position = _position_of(ctx, symbol)
        held_value = float(getattr(position, "market_value", 0.0) or 0.0) if position else 0.0
        budget = max(min(target_value - held_value, cash * 0.95), 0.0)
        lots = int(budget / (price * 100.0)) * 100
        return max(lots, 0)
    except Exception:  # noqa: BLE001
        return 0


def _position_of(ctx: JobContext, symbol: str) -> Any | None:
    if ctx.portfolio is None:
        return None
    try:
        return ctx.portfolio.get_position(ctx.account_id, symbol)
    except Exception:  # noqa: BLE001
        return None


def _safe_positions(ctx: JobContext) -> dict[str, Any]:
    if ctx.portfolio is None:
        return {}
    try:
        return {position.symbol: position for position in ctx.portfolio.get_positions(ctx.account_id)}
    except Exception:  # noqa: BLE001
        return {}


def _safe_account(ctx: JobContext) -> Any:
    if ctx.portfolio is None:
        return None
    try:
        return ctx.portfolio.get_account(ctx.account_id)
    except Exception:  # noqa: BLE001
        return None


def _is_trading_day(ctx: JobContext, day: date) -> bool:
    """交易日判定：注入函数优先，其次交易日历表，最后按工作日近似。"""
    if ctx.is_trading_day is not None:
        return bool(ctx.is_trading_day(day))
    db = (ctx.extra or {}).get("database")
    if db is not None:
        try:
            from sqlalchemy import select  # noqa: PLC0415

            from core.models import TradingCalendarORM  # noqa: PLC0415

            with db.session() as session:
                row = session.get(TradingCalendarORM, day)
                if row is not None:
                    return bool(row.is_open)
        except Exception:  # noqa: BLE001
            logger.debug("读取交易日历失败，按工作日近似", exc_info=True)
    return day.weekday() < 5


def _auto_trading_enabled(ctx: JobContext) -> bool:
    """自动交易开关：注入函数优先，否则读 config.automation.enabled（默认 False）。"""
    if ctx.auto_trading_enabled is not None:
        return bool(ctx.auto_trading_enabled())
    return bool(getattr(getattr(ctx.config, "automation", None), "enabled", False))


# ---------------------------------------------------------------------------
# 记录 / 事件 / 审计 / 结果辅助
# ---------------------------------------------------------------------------


def _record_job_run(ctx: JobContext, result: JobResult) -> None:
    writer = ctx.job_run_writer
    if writer is None:
        return
    payload = {
        "job_key": result.job_key,
        "job_name": JOB_NAMES.get(result.job_key, result.job_key),
        "trace_id": result.trace_id,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "status": str(getattr(result.status, "value", result.status)),
        "message": result.message,
        "stats": result.stats,
    }
    handler = getattr(writer, "record", None)
    if callable(handler):
        handler(payload)
    elif callable(writer):
        writer(payload)


def _publish_event(ctx: JobContext | None, topic: str, payload: dict[str, Any], trace: str) -> None:
    try:
        if ctx is not None and ctx.event_publisher is not None:
            ctx.event_publisher(topic, payload, trace)
            return
        from core.events import publish  # noqa: PLC0415

        publish(topic, payload, trace)
    except Exception:  # noqa: BLE001 - 事件失败不影响任务
        logger.debug("发布 job 事件失败：topic=%s", topic, exc_info=True)


def _audit(ctx: JobContext, action: str, trace: str, *, after: Any = None) -> None:
    actor = "scheduler"
    try:
        ctx.extra.setdefault("audit_writer", None)
        writer = (ctx.extra or {}).get("audit_writer")
        if callable(writer):
            writer(action, ("automation", action), None, after, actor, trace, "SUCCESS")
        else:
            write_audit(action, ("automation", action), None, after, actor=actor, trace_id=trace)
    except Exception:  # noqa: BLE001 - 审计失败不阻塞 job
        logger.debug("写自动化审计失败：action=%s", action, exc_info=True)


def _reader_call(reader: Any, method: str) -> list[Any]:
    if reader is None:
        return []
    handler = getattr(reader, method, None)
    if callable(handler):
        return list(handler() or [])
    if callable(reader):
        return list(reader() or [])
    return []


def _writer_call(writer: Any, method: str, *args: Any) -> Any:
    if writer is None:
        return None
    handler = getattr(writer, method, None)
    return handler(*args) if callable(handler) else None


def _instance_field(instance: Any, name: str, default: Any = None) -> Any:
    if isinstance(instance, dict):
        return instance.get(name, default)
    raw = getattr(instance, name, default)
    return getattr(raw, "value", raw)


def _ok(job_key: str, trace: str, message: str, stats: dict[str, Any]) -> JobResult:
    return JobResult(job_key=job_key, trace_id=trace, status=JobRunStatus.SUCCESS, message=message, stats=stats)


def _skipped(job_key: str, trace: str, message: str) -> JobResult:
    return JobResult(
        job_key=job_key,
        trace_id=trace,
        status=JobRunStatus.SKIPPED,
        message=message,
        stats={"skipped": True, "reason": message},
    )


# ---------------------------------------------------------------------------
# SQL 小仓储（策略实例读取 / 信号留痕 / job_runs 记录）
# ---------------------------------------------------------------------------


class SqlStrategyReader:
    """读取启用的策略实例（只读 ``strategy_instances``，供 scan 使用）。"""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self._session_factory = session_factory

    def list_enabled(self) -> list[dict[str, Any]]:
        from sqlalchemy import select  # noqa: PLC0415

        from core.models import StrategyInstanceORM  # noqa: PLC0415

        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(StrategyInstanceORM)
                    .where(StrategyInstanceORM.enabled.is_(True))
                    .order_by(StrategyInstanceORM.name)
                )
                .scalars()
                .all()
            )
            result: list[dict[str, Any]] = []
            for row in rows:
                try:
                    params = json.loads(row.params_json or "{}")
                except ValueError:
                    params = {}
                try:
                    universe = json.loads(row.universe_json or "[]")
                except ValueError:
                    universe = []
                result.append(
                    {
                        "id": row.id,
                        "name": row.name,
                        "strategy_key": row.strategy_key,
                        "params": params if isinstance(params, dict) else {},
                        "universe": universe if isinstance(universe, list) else [],
                        "mode": row.mode,
                        "enabled": bool(row.enabled),
                    }
                )
            return result


class SqlSignalStore:
    """策略信号留痕（只写 ``strategy_signals``；自动化信号目前无独立服务层）。"""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self._session_factory = session_factory

    def record(self, payload: dict[str, Any]) -> int:
        from core.models import StrategySignalORM  # noqa: PLC0415

        with self._session_factory() as session:
            row = StrategySignalORM(
                trace_id=payload.get("trace_id"),
                strategy_instance_id=payload.get("strategy_instance_id"),
                symbol=payload["symbol"],
                side=payload["side"],
                strength=float(payload.get("strength", 1.0)),
                price_hint=payload.get("price_hint"),
                reason=payload.get("reason"),
                signal_time=payload.get("signal_time") or now_local(),
                status=payload.get("status") or SignalStatus.GENERATED.value,
            )
            session.add(row)
            session.commit()
            return int(row.id)

    def update(self, signal_id: int | None, status: str, order_id: str | None = None) -> None:
        if signal_id is None:
            return
        from core.models import StrategySignalORM  # noqa: PLC0415

        with self._session_factory() as session:
            row = session.get(StrategySignalORM, int(signal_id))
            if row is None:
                return
            row.status = str(status)
            row.order_id = None if order_id is None else str(order_id)
            session.commit()


class SqlJobRunRecorder:
    """把 job 执行结果写入 ``job_runs``（自动化任务的执行留痕）。"""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self._session_factory = session_factory

    def record(self, payload: dict[str, Any]) -> None:
        from core.models import JobRunORM  # noqa: PLC0415

        with self._session_factory() as session:
            row = JobRunORM(
                job_key=str(payload.get("job_key")),
                job_name=str(payload.get("job_name") or ""),
                trace_id=payload.get("trace_id"),
                started_at=payload.get("started_at"),
                finished_at=payload.get("finished_at"),
                status=str(payload.get("status") or JobRunStatus.SUCCESS.value),
                message=str(payload.get("message") or ""),
                stats_json=json.dumps(payload.get("stats") or {}, ensure_ascii=False, default=str),
            )
            session.add(row)
            session.commit()


# ---------------------------------------------------------------------------
# 生产装配（首次触发时延迟构建；DB 由 core/db.py 提供，不在此建引擎）
# ---------------------------------------------------------------------------


def build_job_context(config: Any = None, *, account_id: str = "ACC-PAPER") -> JobContext:
    """装配真实依赖（DB/数据缓存/模拟盘/风控/订单服务/通知/仓储）。

    说明：本函数只在需要真实运行时被调用（scheduler.start / 手动触发），
    单测应直接构造 :class:`JobContext` 注入假实现。
    """
    from core.config import get_config  # noqa: PLC0415

    cfg = config or get_config()
    from core.db import get_database, init_db  # noqa: PLC0415

    init_db(cfg)
    database = get_database(cfg)
    from data.cache import BarCache  # noqa: PLC0415
    from trading.paper import PaperBroker, build_order_service  # noqa: PLC0415
    from trading.portfolio import PortfolioService  # noqa: PLC0415
    from trading.repository_sql import SqlTradingRepository  # noqa: PLC0415
    from trading.risk import RiskGate  # noqa: PLC0415

    repository = SqlTradingRepository(database=database)
    portfolio = PortfolioService(repository)
    portfolio.ensure_account(
        account_id,
        initial_cash=float(cfg.trading.initial_cash),
        name=str(cfg.trading.account_name),
        broker=str(cfg.trading.broker),
    )
    bar_cache = BarCache(config=cfg, database=database)
    broker = PaperBroker(
        portfolio,
        account_id=account_id,
        price_provider=lambda symbol: _latest_prices(
            _static_ctx(cfg, bar_cache, database), [symbol]
        ).get(symbol),
        slippage_bps=float(getattr(cfg.fees, "slippage_bps", 5.0) or 5.0),
        fee_config=cfg.fee_config(),
    )
    market_provider = lambda: _build_market_snapshot(cfg, database, bar_cache, account_id)  # noqa: E731
    order_service = build_order_service(
        account_id=account_id,
        broker=broker,
        risk_gate=RiskGate(cfg),
        repository=repository,
        portfolio=portfolio,
        market_provider=market_provider,
        fee_config=cfg.fee_config(),
    )
    from automation.notify import Notifier  # noqa: PLC0415

    notifier = Notifier(cfg, session_factory=database.session_factory)
    return JobContext(
        config=cfg,
        account_id=account_id,
        bar_cache=bar_cache,
        provider=bar_cache.provider,
        order_service=order_service,
        portfolio=portfolio,
        notifier=notifier,
        strategy_reader=SqlStrategyReader(database.session_factory),
        signal_writer=SqlSignalStore(database.session_factory),
        job_run_writer=SqlJobRunRecorder(database.session_factory),
        extra={"database": database},
    )


def _static_ctx(cfg: Any, bar_cache: Any, database: Any) -> JobContext:
    """轻量上下文（仅用于取价回调，避免递归装配）。"""
    return JobContext(config=cfg, bar_cache=bar_cache, extra={"database": database})


def _build_market_snapshot(cfg: Any, database: Any, bar_cache: Any, account_id: str) -> Any:
    """组装带风控扩展字段的 ``MarketSnapshot``（universe/涨跌停价/上市天数/as_of 等）。"""
    from datetime import date as _date  # noqa: PLC0415

    from sqlalchemy import select  # noqa: PLC0415

    from backtest.broker_sim import AStockRules  # noqa: PLC0415
    from core.models import DailyBarORM, SymbolORM, TradeStatus  # noqa: PLC0415
    from trading.order_manager import assemble_market_snapshot  # noqa: PLC0415

    today = now_local().date()
    universe = list(getattr(getattr(cfg, "data", None), "universe", []) or [])
    latest: dict[str, float] = {}
    price_times: dict[str, Any] = {}
    limits: dict[str, tuple[float, float]] = {}
    listed_days: dict[str, int] = {}
    suspended: set[str] = set()
    delisting: set[str] = set()
    with database.session() as session:
        symbols = {
            row.symbol: row
            for row in session.execute(
                select(SymbolORM).where(SymbolORM.symbol.in_(list(universe) or [""]))
            ).scalars().all()
        } if universe else {}
        last_bars: dict[str, Any] = {}
        if universe:
            rows = session.execute(
                select(DailyBarORM).where(DailyBarORM.symbol.in_(universe)).order_by(DailyBarORM.symbol, DailyBarORM.trade_date)
            ).scalars().all()
            for row in rows:
                last_bars[row.symbol] = row
    for symbol in universe:
        row = last_bars.get(symbol)
        meta = symbols.get(symbol)
        if row is None or float(row.close) <= 0:
            continue
        latest[symbol] = float(row.close)
        price_times[symbol] = datetime.combine(row.trade_date, time(15, 0))
        prev_close = float(getattr(row, "prev_close", 0.0) or 0.0) or float(row.close)
        board = str(getattr(meta, "board", "MAIN") or "MAIN")
        is_st = bool(getattr(meta, "is_st", False))
        try:
            limits[symbol] = AStockRules.price_limits(prev_close, board, is_st)
        except Exception:  # noqa: BLE001
            pass
        list_date = getattr(meta, "list_date", None)
        if list_date is not None:
            listed_days[symbol] = max((today - list_date).days, 0)
        trade_status = str(getattr(meta, "trade_status", TradeStatus.NORMAL.value) or "")
        if trade_status == TradeStatus.SUSPENDED.value:
            suspended.add(symbol)
        if trade_status == TradeStatus.DELISTING.value:
            delisting.add(symbol)
    return assemble_market_snapshot(
        trade_date=today,
        is_trading_time=_is_trading_time(cfg),
        latest_prices=latest,
        price_times=price_times,
        suspended=suspended,
        delisting=delisting,
        universe=universe,
        limit_up_prices={symbol: value[0] for symbol, value in limits.items()},
        limit_down_prices={symbol: value[1] for symbol, value in limits.items()},
        listed_days=listed_days,
        as_of=now_local(),
        automation_enabled=bool(getattr(getattr(cfg, "automation", None), "enabled", False)),
        daily_open_asset=0.0,
    )


def _is_trading_time(cfg: Any) -> bool:
    """A 股连续竞价时段判定（09:30–11:30 / 13:00–15:00，工作日）。"""
    now = now_local()
    if now.weekday() >= 5:
        return False
    morning = time(9, 30) <= now.time() <= time(11, 30)
    afternoon = time(13, 0) <= now.time() <= time(15, 0)
    return morning or afternoon


__all__ = [
    "JOB_KEYS",
    "JOB_NAMES",
    "JOB_PREPARE",
    "JOB_SCAN",
    "JOB_SUMMARIZE",
    "AUDIT_ACTION_SCAN",
    "JobContext",
    "init_job_context",
    "get_job_context",
    "reset_job_context",
    "build_job_context",
    "job_prepare",
    "job_scan",
    "job_summarize",
    "run_job",
    "SqlStrategyReader",
    "SqlSignalStore",
    "SqlJobRunRecorder",
]

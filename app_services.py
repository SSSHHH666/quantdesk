"""QuantDesk 应用门面层（app_services.py）—— UI 唯一推荐调用入口。

为什么需要这一层
----------------
UI 页面（Streamlit）不应该关心 Provider/BarCache/BacktestEngine/RiskGate/OrderService/
Scheduler 的装配细节与异常类型。本模块提供一组**同步、UI 友好**的函数：

    ok, data, message = app_services.函数名(...)

- ``ok``：是否成功（业务失败/被风控拒绝也返回 ``False``，不抛异常）；
- ``data``：结果对象或数据结构（失败时尽量为 ``None`` / 空列表）；
- ``message``：**中文**可读说明（含原因码与建议动作，可直接 ``st.error/st.warning``）。

统一约定
--------
1. **任何失败都不抛异常**：内部统一捕获并转成 ``(False, ..., "中文原因 + 建议")``；
   未预期的异常会写日志，UI 不会白屏。
2. **降级优先**：``data.provider: akshare`` 不可用/受限时由 Provider 链自动降级
   ``mock``（离线示例数据）；回测/推荐在缓存为空时会先尝试增量拉取，仍为空则返回中文提示。
3. **懒加载单例**：配置/数据库/仓储/订单服务/调度器等在首次调用时装配，进程内复用；
   测试或守卫脚本可调用 :func:`reset_services` 注入隔离实例（不污染 ``runtime/quantdesk.db``）。
4. **下单唯一入口**：:func:`place_order` 只调用 ``OrderService.place``（内部必过 RiskGate），
   UI 禁止直接调用 ``BrokerAdapter.submit_order``。
5. 参数/返回/失败语义见各函数 docstring；本文件末尾自带 ``python app_services.py`` 自检。

函数签名清单（供 UI 开发对照）
------------------------------
::

    get_provider() -> (bool, DataProvider, str)
    refresh_bars(symbols=None, years=5) -> (bool, dict, str)
    run_backtest(strategy_key, params=None, symbols=None, start=None, end=None, cash=None)
        -> (bool, dict, str)          # dict: result/metrics/summary/warnings/equity/trades
    preview_order(req) -> (bool, RiskPreview, str)     # 只读，不落库不下单
    place_order(req) -> (bool, OrderResult, str)       # 唯一下单入口，必过风控
    get_account() -> (bool, Account, str)
    get_positions() -> (bool, list[Position], str)
    get_equity_curve(days=120) -> (bool, list[dict], str)
    get_recommendations(group="远期精选", top_n=10) -> (bool, list[dict], str)
    scheduler_status() -> (bool, list[JobStatus], str)
    scheduler_start() -> (bool, bool, str)
    scheduler_stop() -> (bool, bool, str)
    trigger_job(name) -> (bool, dict, str)             # dict: trace_id/job_key/status/message
    tail_logs(lines=200) -> (bool, list[str], str)
    get_watchlist(group=None) -> (bool, list[dict], str)
    add_watchlist(symbol, group="default", name=None, note=None) -> (bool, dict, str)
    remove_watchlist(symbol) -> (bool, bool, str)

暂不可用/降级说明
------------------
- **akshare**：本机未安装或被限流时，Provider 链自动降级 ``mock``（离线示例数据，覆盖
  2021-01-04 ~ 2026-12-31）；此时 ``refresh_bars`` 的 ``message`` 会提示“离线 Mock 数据”；
- **邮件通知**：MVP 未实现（桌面/Webhook 可用），不影响其它功能；
- **真实券商**：MVP 只接模拟盘 ``paper``（QMT/CCXT 为骨架），下单均落模拟账户；
- **推荐/回测**：缓存无数据时需先 ``refresh_bars``（或本函数内部自动尝试一次）。
"""

from __future__ import annotations

import functools
import logging
import threading
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

logger = logging.getLogger("quantdesk.app_services")

#: 默认模拟账户 ID（MVP 单账户；UI 展示名取 config.trading.account_name）
DEFAULT_ACCOUNT_ID = "ACC-PAPER"

#: 自选池分组中文别名 → 存储分组（long_term/short_term/default）
_GROUP_ALIASES: dict[str, str] = {
    "default": "default",
    "默认": "default",
    "全部": "default",
    "long_term": "long_term",
    "远期": "long_term",
    "远期精选": "long_term",
    "short_term": "short_term",
    "近期": "short_term",
    "近期异动": "short_term",
}

#: 推荐分组 → (自选池分组, strategy.recommend 的 strategy_key)
_RECOMMEND_GROUPS: dict[str, tuple[str, str]] = {
    "远期精选": ("long_term", "value_quality"),
    "远期": ("long_term", "value_quality"),
    "long_term": ("long_term", "value_quality"),
    "value_quality": ("long_term", "value_quality"),
    "近期异动": ("short_term", "breakout_abnormal"),
    "近期": ("short_term", "breakout_abnormal"),
    "short_term": ("short_term", "breakout_abnormal"),
    "breakout_abnormal": ("short_term", "breakout_abnormal"),
    "多因子": ("default", "multi_factor"),
    "multi_factor": ("default", "multi_factor"),
    "default": ("default", "value_quality"),
}

_LOCK = threading.RLock()
_SERVICES: dict[str, Any] = {}
_OVERRIDES: dict[str, Any] = {}


# ===========================================================================
# 结果与守卫
# ===========================================================================


def _ok(data: Any, message: str) -> tuple[bool, Any, str]:
    """成功结果（统一三元组）。"""
    return True, data, message


def _fail(message: str, data: Any = None) -> tuple[bool, Any, str]:
    """失败结果（统一三元组；message 必为中文可操作提示）。"""
    return False, data, message


def _format_error(exc: BaseException) -> str:
    """把异常转成 UI 友好的中文一句说明（避免把整段 traceback/类名直接给用户）。"""
    try:  # pydantic 校验错误：只保留字段 + 中文/英文原因，压成单行
        from pydantic import ValidationError

        if isinstance(exc, ValidationError):
            parts = []
            for error in exc.errors()[:4]:
                field = ".".join(str(item) for item in error.get("loc", ()) if item != "body")
                parts.append(f"{field or '参数'}：{error.get('msg', '非法')}")
            return "参数校验失败（" + "；".join(parts) + "）"
    except Exception:  # noqa: BLE001 - pydantic 不可用/结构异常时退回通用文案
        pass
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else "未知错误"
    if isinstance(exc, (ValueError, LookupError, RuntimeError, OSError)):
        return text  # 业务层已给中文原因（如分组非法/代码格式非法）
    return f"{type(exc).__name__}: {text}"


def _guard(feature: str, default_data: Any = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """统一异常兜底装饰器：任何异常 → ``(False, default_data, 中文提示)``，绝不外抛。"""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> tuple[bool, Any, str]:
            try:
                return func(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - 门面层最终兜底：UI 不允许白屏
                logger.error("app_services.%s 执行失败", func.__name__, exc_info=True)
                return _fail(
                    f"{feature}失败：{_format_error(exc)}。"
                    "请检查数据源/数据库后重试；若持续失败请查看日志页（带 trace_id）。",
                    default_data,
                )

        return wrapper

    return decorator


# ===========================================================================
# 懒加载服务装配（不在此创建新引擎：DB 统一由 core/db.py 提供）
# ===========================================================================


def reset_services(**overrides: Any) -> None:
    """重置门面层缓存的服务实例（测试/守卫脚本隔离用）。

    :param overrides: 可注入 ``config`` / ``database`` 等实例；未注入的按默认规则懒加载。
    说明：生产 UI 不需要调用；自检脚本用内存 SQLite 注入，避免污染 ``runtime/quantdesk.db``。
    """
    global _OVERRIDES
    with _LOCK:
        _SERVICES.clear()
        _OVERRIDES = dict(overrides)


def _get(name: str, factory: Callable[[], Any]) -> Any:
    """线程安全的懒加载单例。"""
    with _LOCK:
        if name not in _SERVICES:
            _SERVICES[name] = factory()
        return _SERVICES[name]


def _config() -> Any:
    """全局配置（``AppConfig``）。"""
    def build() -> Any:
        if "config" in _OVERRIDES:
            return _OVERRIDES["config"]
        from core.config import get_config

        return get_config()

    return _get("config", build)


def _database() -> Any:
    """数据库管理器（核心 ``core/db.py``；首次调用建表并注入审计会话工厂）。"""
    def build() -> Any:
        if "database" in _OVERRIDES:
            database = _OVERRIDES["database"]
            if hasattr(database, "initialize"):
                database.initialize()
        else:
            from core.db import get_database, init_db

            init_db(_config())
            database = get_database()
        return database

    return _get("database", build)


def _repository() -> Any:
    """交易域 SQL 仓储（账户/持仓/订单/成交/资金曲线）。"""
    def build() -> Any:
        from trading.repository_sql import SqlTradingRepository

        return SqlTradingRepository(database=_database())

    return _get("repository", build)


def _portfolio() -> Any:
    """账户/持仓服务（首次调用确保模拟账户存在）。"""
    def build() -> Any:
        from trading.portfolio import PortfolioService

        portfolio = PortfolioService(_repository())
        account_id = DEFAULT_ACCOUNT_ID
        if portfolio.repository.get_account(account_id) is None:
            config = _config()
            portfolio.ensure_account(
                account_id,
                initial_cash=float(config.trading.initial_cash),
                name=str(config.trading.account_name),
                broker=str(config.trading.broker),
            )
        return portfolio

    return _get("portfolio", build)


def _bar_cache() -> Any:
    """行情缓存（Provider + SQLite 增量落库）。"""
    def build() -> Any:
        from data.cache import BarCache

        return BarCache(config=_config(), database=_database())

    return _get("bar_cache", build)


def _provider() -> Any:
    """数据源（主备降级链已由 ``build_provider`` 处理：akshare 失败自动 mock）。"""
    return _get("provider", lambda: _bar_cache().provider)


def _scheduler() -> Any:
    """自动化调度器单例。"""
    def build() -> Any:
        from automation.scheduler import get_scheduler

        return get_scheduler()

    return _get("scheduler", build)


def _notifier() -> Any:
    """通知服务（默认渠道配置来自 config.notify）。"""
    def build() -> Any:
        from automation.notify import Notifier

        return Notifier(_config(), session_factory=_database().session_factory)

    return _get("notifier", build)


def _watchlist() -> Any:
    """自选池服务（独立扩展表 ``watchlist_items``，惰性建表）。"""
    def build() -> Any:
        from data.watchlist import WatchlistService

        return WatchlistService(config=_config(), database=_database())

    return _get("watchlist", build)


def _latest_price(symbol: str) -> float | None:
    """取单个标的最新价（失败返回 None，由调用方决定是否拒单）。"""
    try:
        provider = _provider()
        today = date.today()
        frame = provider.get_bars([symbol], today - timedelta(days=10), today, "1d")
        if frame is None or len(frame) == 0:
            snapshot = provider.get_snapshot([symbol])
            if snapshot is not None and len(snapshot) > 0:
                return float(snapshot.iloc[-1]["last"])
            return None
        frame = frame.sort_values("date")
        close = float(frame.iloc[-1]["close"])
        return close if close > 0 else None
    except Exception:  # noqa: BLE001 - 取价失败按缺失处理
        return None


def _market_snapshot(symbols: Iterable[str] | None = None) -> Any:
    """组装风控用 ``MarketSnapshot``（含 UI 需要的扩展字段，自动容错）。"""
    from backtest.broker_sim import AStockRules, guess_board
    from core.models import now_local
    from trading.order_manager import assemble_market_snapshot

    config = _config()
    universe = list(getattr(config.data, "universe", []) or [])
    wanted = list(dict.fromkeys([*universe, *(symbols or [])]))
    prices: dict[str, float] = {}
    price_times: dict[str, datetime] = {}
    limit_up: dict[str, float] = {}
    limit_down: dict[str, float] = {}
    try:
        provider = _provider()
        snapshot = provider.get_snapshot(wanted)
        if snapshot is not None and len(snapshot) > 0:
            for _, row in snapshot.iterrows():
                symbol = str(row.get("symbol") or "").strip().upper()
                last = row.get("last")
                prev_close = row.get("prev_close")
                if not symbol or last is None:
                    continue
                prices[symbol] = float(last)
                ts = row.get("ts")
                price_times[symbol] = ts if isinstance(ts, datetime) else now_local()
                try:
                    base = float(prev_close) if prev_close is not None else float(last)
                    board = guess_board(symbol)
                    upper, lower = AStockRules.price_limits(base, board, False)
                    limit_up[symbol], limit_down[symbol] = upper, lower
                except Exception:  # noqa: BLE001 - 涨跌停拿不到则跳过区间校验
                    continue
    except Exception:  # noqa: BLE001 - 快照失败：空快照交给风控按 fail-closed 处理
        logger.warning("组装行情快照失败（风控将按数据缺失处理）", exc_info=True)

    now = now_local()
    return assemble_market_snapshot(
        trade_date=now.date(),
        is_trading_time=_is_trading_time(now),
        latest_prices=prices,
        price_times=price_times,
        universe=universe,
        limit_up_prices=limit_up,
        limit_down_prices=limit_down,
        as_of=now,
        automation_enabled=bool(getattr(getattr(config, "automation", None), "enabled", False)),
    )


def _is_trading_time(now: datetime | None = None) -> bool:
    """A 股连续竞价时段近似判断（工作日 09:30–11:30 / 13:00–15:00）。"""
    moment = now or datetime.now()
    if moment.weekday() >= 5:
        return False
    return dt_time(9, 30) <= moment.time() <= dt_time(11, 30) or dt_time(13, 0) <= moment.time() <= dt_time(15, 0)


def _coerce_order_request(req: Any) -> Any:
    """把 UI 的 dict/OrderRequest 统一成 ``OrderRequest``（缺字段给中文错误）。"""
    from core.models import OrderRequest

    if isinstance(req, OrderRequest):
        return req
    if isinstance(req, Mapping):
        allowed = set(OrderRequest.model_fields)
        payload = {key: value for key, value in dict(req).items() if key in allowed}
        payload.setdefault("account_id", DEFAULT_ACCOUNT_ID)
        payload.setdefault("source", "MANUAL")
        payload.setdefault("order_type", "LIMIT")
        return OrderRequest(**payload)
    raise ValueError(
        f"委托参数类型非法：{type(req).__name__}；请传入 OrderRequest 或包含 "
        "symbol/side/price/quantity 的 dict"
    )


# ===========================================================================
# 1) 数据源 / 行情刷新
# ===========================================================================


@_guard("获取数据源", default_data=None)
def get_provider() -> tuple[bool, Any, str]:
    """获取数据源实例（主备降级链入口）。

    参数：无。
    返回：``(True, DataProvider, "数据源：<name>（状态：OK/降级说明）")``。
    失败语义：Provider 装配失败返回 ``(False, None, 中文原因+建议)``；
    若 akshare 不可用会自动降级 mock（``data.fallbacks``），此时 message 提示离线 Mock 数据。
    """
    provider = _provider()
    name = str(getattr(provider, "name", "unknown"))
    status_text = ""
    try:
        health = provider.health_check()
        status = getattr(health, "status", "")
        status = getattr(status, "value", status)
        status_text = f"（状态：{status}）"
        if str(status).upper() not in {"OK", "NORMAL"}:
            status_text += " —— 数据源异常，已按配置降级/使用缓存"
    except Exception:  # noqa: BLE001 - 健康检查失败不影响返回 provider
        status_text = "（健康检查不可用）"
    if name.lower() == "mock":
        status_text += "；当前为离线 Mock 示例数据（akshare 未安装/受限时自动降级）"
    return _ok(provider, f"数据源：{name}{status_text}")


@_guard("刷新行情", default_data=None)
def refresh_bars(symbols: Iterable[str] | None = None, years: int = 5) -> tuple[bool, Any, str]:
    """增量刷新日线行情到本地 SQLite 缓存（Provider → BarCache）。

    参数：
        symbols: 标的列表；``None`` 时用 ``config.data.universe`` 自选池。
        years: 回看年数（默认 5，用于首次全量拉取；已缓存区间自动跳过）。
    返回：``(True, {"provider","symbols","added","updated","skipped","rows","issues","report","validation"}, 中文摘要)``。
    失败语义：数据源/数据库异常返回 ``(False, None, 中文原因+建议)``；
    单只标的失败不中断整批，失败明细在 ``data["report"].errors`` 中。
    """
    config = _config()
    symbol_list = [str(item).strip().upper() for item in (symbols or config.data.universe or []) if str(item).strip()]
    symbol_list = list(dict.fromkeys(symbol_list))
    if not symbol_list:
        return _fail("刷新行情失败：未指定标的（请在设置页配置自选池，或传入 symbols）", None)
    years = max(1, int(years or 1))
    today = date.today()
    start = today - timedelta(days=365 * years)

    cache = _bar_cache()
    report = cache.update(symbol_list, start, today)
    validation = None
    try:
        validation = cache.validate(symbol_list, start, today)
    except Exception:  # noqa: BLE001 - 校验失败不影响已刷新的行情
        logger.warning("行情质量校验失败（不影响刷新结果）", exc_info=True)

    provider_name = str(getattr(cache.provider, "name", "unknown"))
    issues = int(getattr(validation, "total_issues", 0) or 0) if validation is not None else 0
    data = {
        "provider": provider_name,
        "symbols": symbol_list,
        "added": int(getattr(report, "added", 0) or 0),
        "updated": int(getattr(report, "updated", 0) or 0),
        "skipped": int(getattr(report, "skipped", 0) or 0),
        "rows": (int(getattr(report, "added", 0) or 0) + int(getattr(report, "updated", 0) or 0)),
        "issues": issues,
        "report": report,
        "validation": validation,
    }
    message = str(getattr(report, "message", "") or "").strip() or (
        f"行情刷新完成（{provider_name}）：新增 {data['added']} 行、更新 {data['updated']} 行"
    )
    if issues:
        message += f"；数据质量提示 {issues} 条（可在日志页「数据校验」查看）"
    if provider_name.lower() == "mock":
        message += "；当前为离线 Mock 示例数据（akshare 未安装/受限时自动降级）"
    if getattr(report, "failed_symbols", None):
        message += f"；失败标的 {len(report.failed_symbols)} 只：" + "、".join(report.failed_symbols[:5])
    return _ok(data, message)


# ===========================================================================
# 2) 回测
# ===========================================================================


@_guard("运行回测", default_data=None)
def run_backtest(
    strategy_key: str,
    params: Mapping[str, Any] | None = None,
    symbols: Iterable[str] | None = None,
    start: Any = None,
    end: Any = None,
    cash: float | None = None,
) -> tuple[bool, Any, str]:
    """运行一次回测（BacktestEngine + 本地缓存，缺数据时自动尝试拉取一次）。

    参数：
        strategy_key: 策略注册表 key（如 ``ma_cross`` / ``rsi_reversal`` / ``multi_factor``）。
        params: 策略参数 dict（None 用策略默认值）。
        symbols: 标的池；None 用 ``config.data.universe``。
        start / end: 区间（date/``YYYY-MM-DD``）；None 默认近 3 年 / 今天。
        cash: 初始资金；None 用 ``config.backtest.default_initial_cash``。
    返回：``(True, {"result","metrics","summary","warnings","warning_messages","equity","trades"}, 中文摘要)``。
    失败语义：策略不存在/区间无数据/数据源异常返回 ``(False, None, 中文原因+建议)``；
    区间无缓存时会自动 ``refresh_bars`` 一次（akshare 受限则用 Mock 数据）。
    """
    from backtest.engine import BacktestEngine

    config = _config()
    end_date = _to_date(end) if end is not None else date.today()
    start_date = _to_date(start) if start is not None else end_date - timedelta(days=365 * 3)
    if start_date > end_date:
        return _fail(f"回测失败：开始日期 {start_date} 晚于结束日期 {end_date}", None)
    symbol_list = [str(item).strip().upper() for item in (symbols or config.data.universe or []) if str(item).strip()]
    symbol_list = list(dict.fromkeys(symbol_list))
    if not symbol_list:
        return _fail("回测失败：标的池为空（请选择至少 1 个标的）", None)
    initial_cash = float(cash if cash is not None else config.backtest.default_initial_cash)
    if initial_cash <= 0:
        return _fail(f"回测失败：初始资金必须为正数（当前 {initial_cash}）", None)

    cache = _bar_cache()
    # 缓存为空时自动尝试拉取一次（Provider 链降级 mock，保证离线也能演示）
    try:
        cached = cache.load(symbol_list, start_date, end_date)
    except Exception:  # noqa: BLE001 - 加载失败按空缓存处理
        cached = None
    if cached is None or len(cached) == 0:
        refresh_result = refresh_bars(symbol_list, years=max(1, (date.today() - start_date).days // 365 + 1))
        if not refresh_result[0]:
            return _fail(
                f"回测失败：区间内无行情且自动拉取失败（{refresh_result[2]}）",
                None,
            )

    engine = BacktestEngine(data_source=cache)
    result = engine.run(
        strategy_key,
        dict(params or {}),
        symbol_list,
        start_date,
        end_date,
        initial_cash=initial_cash,
        fee_config=config.fee_config(),
        slippage_bps=float(getattr(config.fees, "slippage_bps", 5.0) or 5.0),
        benchmark=str(getattr(config.backtest, "benchmark", "000300.SH") or "000300.SH"),
    )
    metrics = dict(result.metrics or {})
    data = {
        "result": result,
        "metrics": metrics,
        "summary": result.summary(),
        "warnings": list(result.warnings or []),
        "warning_messages": result.warning_messages(),
        "equity": result.equity,
        "trades": result.trades,
    }
    message = (
        f"回测完成（{strategy_key}，{start_date} ~ {end_date}）："
        f"总收益 {_fmt_pct(metrics.get('total_return'))}、年化 {_fmt_pct(metrics.get('annual_return'))}、"
        f"最大回撤 {_fmt_pct(metrics.get('max_drawdown'))}、夏普 {_fmt_num(metrics.get('sharpe'))}、"
        f"交易 {int(metrics.get('trade_count') or 0)} 笔"
    )
    if data["warnings"]:
        message += f"；⚠ 警告 {len(data['warnings'])} 项（样本短/交易少/未复权等，详见 warning_messages）"
    return _ok(data, message)


def _to_date(value: Any) -> date:
    """把 date/datetime/字符串统一成 date（非法值给中文错误）。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        import pandas as pd

        return pd.Timestamp(str(value)).date()
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"日期格式非法：{value!r}（应为 YYYY-MM-DD）") from exc


def _fmt_pct(value: Any) -> str:
    """百分比格式化（None/非法显示 ``-``）。"""
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "-"


def _fmt_num(value: Any, digits: int = 2) -> str:
    """数值格式化（None/非法显示 ``-``）。"""
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "-"


# ===========================================================================
# 3) 下单：预检 / 提交（唯一入口，必过风控）
# ===========================================================================


def _order_service() -> Any:
    """订单服务（唯一下单入口，内部必调 RiskGate；broker 由本层统一装配）。"""
    def build() -> Any:
        from trading.order_manager import build_order_service
        from trading.paper import PaperBroker
        from trading.risk import RiskGate

        config = _config()
        portfolio = _portfolio()
        broker = PaperBroker(
            portfolio,
            account_id=DEFAULT_ACCOUNT_ID,
            price_provider=_latest_price,
            slippage_bps=float(getattr(config.fees, "slippage_bps", 5.0) or 5.0),
            fee_config=config.fee_config(),
        )
        return build_order_service(
            account_id=DEFAULT_ACCOUNT_ID,
            broker=broker,
            risk_gate=RiskGate(config),
            repository=_repository(),
            portfolio=portfolio,
            market_provider=_market_snapshot,
            fee_config=config.fee_config(),
        )

    return _get("order_service", build)


@_guard("下单预检", default_data=None)
def preview_order(req: Any) -> tuple[bool, Any, str]:
    """只读风控预检（不落库、不下单）——手动交易页使用。

    参数：
        req: ``OrderRequest`` 或 dict（``symbol``/``side``/``price``/``quantity`` 等；
             缺 ``account_id`` 时默认当前模拟账户）。
    返回：``(ok, RiskPreview, 中文说明)``；``ok=preview.decision.allow``。
    失败语义：参数非法/账户异常返回 ``(False, None, 中文原因+建议)``；
    风控拒绝时 ``ok=False`` 且 ``data`` 仍为 ``RiskPreview``（含 RK 原因码与建议）。
    """
    request = _coerce_order_request(req)
    preview = _order_service().preview(request)
    decision = preview.decision
    fee_total = float(preview.fee.total) if preview.fee is not None else 0.0
    message = decision.message or "风控预检完成"
    if decision.allow:
        message = (
            f"风控预检通过：预计金额 {preview.estimated_amount:,.2f} 元，费用 {fee_total:,.2f} 元，"
            f"成交后单票仓位 {preview.post_trade_position_pct:.2f}% / 总仓位 "
            f"{preview.post_trade_total_position_pct:.2f}%"
        )
    return bool(decision.allow), preview, message


@_guard("提交订单", default_data=None)
def place_order(req: Any) -> tuple[bool, Any, str]:
    """提交订单（**唯一下单入口**：``OrderService.place`` 内部必过 RiskGate）。

    参数：
        req: ``OrderRequest`` 或 dict（同 :func:`preview_order`）。
    返回：``(ok, OrderResult, 中文说明)``；``ok=result.success``。
    失败语义：风控拒绝/券商拒单/参数非法均返回 ``(False, OrderResult, 含 RK/BR 原因码的中文说明)``，
    订单仍会落库为 ``REJECTED``（便于审计与“今日委托”表展示）。
    """
    request = _coerce_order_request(req)
    result = _order_service().place(request)
    message = str(getattr(result, "message", "") or "")
    if not getattr(result, "success", False) and getattr(result, "reason_code", None):
        message = f"{message}〔{result.reason_code}〕"
    return bool(getattr(result, "success", False)), result, message or "订单提交完成"


# ===========================================================================
# 4) 账户 / 持仓 / 资金曲线
# ===========================================================================


@_guard("获取账户", default_data=None)
def get_account() -> tuple[bool, Any, str]:
    """获取当前模拟账户（不存在时自动创建）。

    返回：``(True, Account, "账户：<name>，总资产 … 元，可用 … 元")``。
    失败语义：数据库不可用返回 ``(False, None, 中文原因+建议)``。
    """
    account = _portfolio().get_account(DEFAULT_ACCOUNT_ID)
    message = (
        f"账户：{account.name}，总资产 {account.total_asset:,.2f} 元，"
        f"可用资金 {account.available_cash:,.2f} 元，持仓市值 {account.position_value:,.2f} 元"
    )
    return _ok(account, message)


@_guard("获取持仓", default_data=None)
def get_positions() -> tuple[bool, Any, str]:
    """获取当前账户持仓列表（含 T+1 可用数量、成本、市值、浮盈）。

    返回：``(True, list[Position], "共 N 只持仓")``。
    失败语义：数据库不可用返回 ``(False, None, 中文原因+建议)``。
    """
    positions = _portfolio().get_positions(DEFAULT_ACCOUNT_ID)
    message = f"共 {len(positions)} 只持仓" if positions else "当前无持仓（可先去手动交易页买入）"
    return _ok(positions, message)


@_guard("获取资金曲线", default_data=None)
def get_equity_curve(days: int = 120) -> tuple[bool, Any, str]:
    """获取资金曲线（最近 N 个交易日，按日期升序）。

    参数：``days`` 最近条数（默认 120，范围 1~2000）。
    返回：``(True, list[dict], "共 N 条资金曲线")``；dict 字段：
    ``trade_date/total_asset/cash/position_value/daily_pnl/daily_return``。
    失败语义：数据库不可用返回 ``(False, None, 中文原因+建议)``。
    """
    limit = max(1, min(int(days or 120), 2000))
    rows = _repository().list_equity(DEFAULT_ACCOUNT_ID, limit=limit)
    message = f"共 {len(rows)} 条资金曲线" if rows else "暂无资金曲线（产生成交或运行盘后总结后生成）"
    return _ok(rows, message)


# ===========================================================================
# 5) 今日推荐
# ===========================================================================


@_guard("获取推荐", default_data=None)
def get_recommendations(group: str = "远期精选", top_n: int = 10) -> tuple[bool, Any, str]:
    """获取“带理由”的 Top-N 推荐（strategy.recommend）。

    参数：
        group: 推荐分组（本层映射到自选池分组 + 打分策略）：
            - ``远期精选/远期/long_term/value_quality`` → ``value_quality``（默认）
            - ``近期异动/近期/short_term/breakout_abnormal`` → ``breakout_abnormal``
            - ``多因子/multi_factor`` → ``multi_factor``
            也接受自选池分组名（default/long_term/short_term）。
        top_n: 返回条数（默认 10）。
    返回：``(True, [Recommendation.as_dict()...], "共 N 条推荐（策略 key=…）")``。
    失败语义：**降级为空列表**并返回 ``(False, [], 中文原因+建议)**（页面显示占位即可，不抛异常）；
    自选池为空时回退 ``config.data.universe``；缓存无数据时自动尝试刷新一次。
    """
    from strategy.recommend import recommend

    config = _config()
    key = str(group or "远期精选").strip()
    watch_group, strategy_key = _RECOMMEND_GROUPS.get(
        key, _RECOMMEND_GROUPS.get(key.lower(), ("default", "value_quality"))
    )
    top = max(1, min(int(top_n or 10), 50))

    # 自选池分组标的；为空时回退配置自选池
    universe: list[str] = []
    try:
        items = _watchlist().list_items(watch_group)
        universe = [item.symbol for item in items]
    except Exception:  # noqa: BLE001 - 自选池不可用时回退配置
        logger.warning("读取自选池失败，回退 config.data.universe", exc_info=True)
    if not universe:
        universe = [str(item).strip().upper() for item in getattr(config.data, "universe", []) or []]
    universe = list(dict.fromkeys([item for item in universe if item]))
    if not universe:
        return _fail("推荐失败：自选池为空（请先在自选池页添加标的，或在 config.yaml 配置 data.universe）", [])

    as_of = date.today()
    cache = _bar_cache()
    lookback_start = as_of - timedelta(days=550)
    try:
        cached = cache.load(universe, lookback_start, as_of)
    except Exception:  # noqa: BLE001
        cached = None
    if cached is None or len(cached) == 0:
        refresh_result = refresh_bars(universe, years=2)
        if not refresh_result[0]:
            return _fail(f"推荐失败：无可用行情，且自动拉取失败（{refresh_result[2]}）", [])
    try:
        recommendations = recommend(strategy_key, universe, as_of, top_n=top, data_source=cache)
    except Exception as exc:  # noqa: BLE001 - 推荐失败降级为空列表（页面显示占位与建议）
        logger.warning("推荐计算失败，已降级为空列表：%s", exc)
        return _fail(
            f"推荐暂不可用（已降级为空列表）：{exc}；建议先在行情页刷新数据后重试",
            [],
        )
    data = [item.as_dict() for item in recommendations]
    message = f"共 {len(data)} 条推荐（策略 {strategy_key}，分组 {watch_group}，截至 {as_of}）"
    if not data:
        message = f"暂无符合条件的推荐（策略 {strategy_key}）；可放宽条件或稍后重试"
    return _ok(data, message)


# ===========================================================================
# 6) 自动化调度器
# ===========================================================================


@_guard("获取调度器状态", default_data=None)
def scheduler_status() -> tuple[bool, Any, str]:
    """获取调度器与三个 job 的状态（是否运行 / 下次运行时间 / 上次结果）。

    返回：``(True, list[JobStatus], "调度器运行中/未运行；任务：…")``。
    失败语义：APScheduler 未安装等返回 ``(False, [], 中文原因+安装建议)``。
    """
    scheduler = _scheduler()
    statuses = scheduler.status()
    running = scheduler.is_running()
    parts = [f"{item.name}({item.job_key}) 下次 {item.next_run_time or '未排期'}" for item in statuses]
    message = ("调度器运行中" if running else "调度器未运行") + "；" + "；".join(parts)
    return _ok(statuses, message)


@_guard("启动调度器", default_data=False)
def scheduler_start() -> tuple[bool, Any, str]:
    """启动自动化调度器（幂等；三个 job 按 config.automation.jobs 时间执行）。

    返回：``(True, True, 中文说明)``。
    失败语义：APScheduler 缺失/单实例锁被占用/配置非法返回 ``(False, False, 中文原因+建议)``。
    注意：``automation.enabled`` 默认 false —— 调度器可启动，但 **scan 只生成信号不下单**。
    """
    scheduler = _scheduler()
    scheduler.start()
    message = "调度器已启动（注意：自动交易开关 automation.enabled 默认关闭，scan 仅生成信号）"
    return _ok(True, message)


@_guard("停止调度器", default_data=False)
def scheduler_stop() -> tuple[bool, Any, str]:
    """停止自动化调度器（幂等；不影响已提交订单）。

    返回：``(True, True/False, "调度器已停止/本来未运行")``。
    失败语义：关闭异常返回 ``(False, False, 中文原因+建议)``。
    """
    stopped = _scheduler().stop()
    return _ok(bool(stopped), "调度器已停止（已提交订单不受影响）" if stopped else "调度器本来未运行")


@_guard("手动触发任务", default_data=None)
def trigger_job(name: str) -> tuple[bool, Any, str]:
    """手动立即执行一个自动化任务（不改变定时计划）。

    参数：``name`` 取 ``prepare`` / ``scan`` / ``summarize``。
    返回：``(True, {"trace_id","job_key","status","message"}, 中文结果摘要)``。
    失败语义：未知任务/执行失败返回 ``(False, None, 中文原因+建议)``。
    注意：同步执行，``prepare`` 可能拉取行情、``scan`` 可能下单，耗时数秒到数十秒。
    """
    key = str(name or "").strip().lower()
    if key not in {"prepare", "scan", "summarize"}:
        return _fail(f"手动触发失败：未知任务 {name!r}（仅支持 prepare/scan/summarize）", None)
    scheduler = _scheduler()
    trace_id = scheduler.trigger_now(key)
    row = next((item for item in scheduler.status() if item.job_key == key), None)
    data = {
        "trace_id": trace_id,
        "job_key": key,
        "status": getattr(row, "last_status", None) if row else None,
        "message": getattr(row, "last_message", "") if row else "",
    }
    message = f"任务 {key} 已执行：{data['status'] or 'UNKNOWN'}；{data['message']}（trace_id={trace_id}）"
    ok = str(data["status"]).upper() in {"SUCCESS", "SKIPPED"}
    return ok, data, message


# ===========================================================================
# 7) 日志尾读（脱敏）
# ===========================================================================


@_guard("读取日志", default_data=None)
def tail_logs(lines: int = 200) -> tuple[bool, Any, str]:
    """读取运行日志最近 N 行（JSON Lines，已脱敏）。

    参数：``lines`` 条数（默认 200，范围 1~2000）。
    返回：``(True, list[str], "日志文件：<路径>，最近 N 行")``；每行为脱敏后的原始文本。
    失败语义：日志文件不存在/读取失败返回 ``(False, [], 中文说明：日志路径与建议)``。
    """
    from core.logging import get_log_dir, mask_text

    limit = max(1, min(int(lines or 200), 2000))
    log_dir = Path(get_log_dir())
    if not log_dir.exists():
        return _fail(f"暂无日志：日志目录不存在（{log_dir}），可先运行一次回测/自动化任务", [])
    candidates = sorted(
        (path for path in log_dir.glob("*.log") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return _fail(f"暂无日志：目录 {log_dir} 下没有 .log 文件", [])
    latest = candidates[0]
    text = latest.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = [mask_text(line) for line in text[-limit:]]
    return _ok(tail, f"日志文件：{latest}，最近 {len(tail)} 行")


# ===========================================================================
# 8) 自选池
# ===========================================================================


def _normalize_watch_group(group: Any) -> str:
    """把中文/别名分组统一成存储分组名（非法分组给中文错误）。"""
    text = str(group or "default").strip()
    mapped = _GROUP_ALIASES.get(text) or _GROUP_ALIASES.get(text.lower())
    if mapped is None:
        raise ValueError(
            f"分组 {group!r} 非法：支持 默认(default)/远期(long_term)/近期(short_term)"
        )
    return mapped


@_guard("获取自选池", default_data=None)
def get_watchlist(group: Any = None) -> tuple[bool, Any, str]:
    """获取自选池列表（可按分组过滤）。

    参数：``group`` 为 ``None``（全部）/ ``default`` / ``long_term`` / ``short_term``，
    也接受中文别名「默认/远期/近期」。
    返回：``(True, [{"symbol","name","group","note","added_at","updated_at"}...], "共 N 只")``。
    失败语义：数据库/分组非法返回 ``(False, None, 中文原因+建议)``。
    """
    service = _watchlist()
    stored_group = _normalize_watch_group(group) if group is not None else None
    items = service.list_items(stored_group)
    data = [item.to_dict() for item in items]
    scope = "全部" if stored_group is None else stored_group
    return _ok(data, f"自选池（{scope}）共 {len(data)} 只")


@_guard("加入自选池", default_data=None)
def add_watchlist(
    symbol: str,
    group: Any = "default",
    name: str | None = None,
    note: str | None = None,
) -> tuple[bool, Any, str]:
    """把标的加入自选池（已存在则更新名称/分组/备注，不重复插入）。

    参数：``symbol``（如 ``600519.SH``）、``group``（默认分组/中文别名）、``name``、``note``。
    返回：``(True, WatchlistItem.to_dict(), "已加入自选池：…")``。
    失败语义：代码格式非法/分组非法/数据库异常返回 ``(False, None, 中文原因+建议)``。
    """
    item = _watchlist().add(
        symbol,
        group=_normalize_watch_group(group),
        name=name,
        note=note,
    )
    return _ok(item.to_dict(), f"已加入自选池：{item.symbol}（{item.group_name}）")


@_guard("移出自选池", default_data=False)
def remove_watchlist(symbol: str) -> tuple[bool, Any, str]:
    """把标的移出自选池。

    参数：``symbol``（如 ``600519.SH``）。
    返回：``(True, True, "已移出自选池")``；不在池中时 ``(False, False, "该标的不在自选池")``。
    失败语义：代码格式非法/数据库异常返回 ``(False, False, 中文原因+建议)``。
    """
    removed = bool(_watchlist().remove(symbol))
    if not removed:
        return _fail(f"{symbol} 不在自选池（可能已移除或代码格式不一致）", False)
    return _ok(True, f"已移出自选池：{symbol}")


__all__ = [
    "DEFAULT_ACCOUNT_ID",
    "reset_services",
    # 数据
    "get_provider",
    "refresh_bars",
    # 回测
    "run_backtest",
    # 交易
    "preview_order",
    "place_order",
    # 账户
    "get_account",
    "get_positions",
    "get_equity_curve",
    # 推荐
    "get_recommendations",
    # 自动化
    "scheduler_status",
    "scheduler_start",
    "scheduler_stop",
    "trigger_job",
    # 日志
    "tail_logs",
    # 自选池
    "get_watchlist",
    "add_watchlist",
    "remove_watchlist",
]


# ===========================================================================
# 自检（quantdesk 目录：``python app_services.py``；用内存 SQLite + Mock 数据）
# ===========================================================================
if __name__ == "__main__":  # pragma: no cover - 守卫脚本
    import sys

    print("=" * 78)
    print("QuantDesk app_services.py 自检（内存 SQLite + Mock 数据，不污染 runtime/quantdesk.db）")
    print("=" * 78)

    from core.config import get_config
    from core.db import Database

    config = get_config()
    database = Database(url="sqlite://", config=config)
    reset_services(config=config, database=database)

    passed = 0
    failed = 0

    def _report(name: str, result: tuple[bool, Any, str], *, optional: bool = False) -> Any:
        global passed, failed
        ok, data, message = result
        flag = "PASS" if ok else ("SKIP" if optional else "FAIL")
        if ok:
            passed += 1
        elif not optional:
            failed += 1
        print(f"  [{flag}] {name}: {message}")
        return data

    try:
        provider = _report("get_provider", get_provider())
        _report("refresh_bars", refresh_bars(["600519.SH", "000001.SZ"], years=2))

        today = date.today()
        _report(
            "run_backtest",
            run_backtest(
                "ma_cross",
                {"fast": 5, "slow": 20, "position_pct": 10},
                ["600519.SH"],
                today - timedelta(days=365),
                today,
            ),
        )
        _report("get_account", get_account())
        _report("get_positions", get_positions())
        _report("get_equity_curve", get_equity_curve(60))

        # 自检环境当前可能不在交易时段：仅为本自检放行时段判定（生产代码不生效）
        globals()["_is_trading_time"] = lambda now=None: True
        price = _latest_price("000001.SZ")
        if price:
            _report(
                "preview_order",
                preview_order(
                    {"symbol": "000001.SZ", "side": "BUY", "price": round(price, 2), "quantity": 100}
                ),
            )
            _report(
                "place_order",
                place_order(
                    {"symbol": "000001.SZ", "side": "BUY", "price": round(price, 2), "quantity": 100}
                ),
            )
            _report("get_positions(after)", get_positions())
            _report("get_equity_curve(after)", get_equity_curve(5))
        else:
            print("  [SKIP] preview_order/place_order：未取到 000001.SZ 最新价")

        _report("get_recommendations", get_recommendations("远期精选", 3))
        _report("scheduler_status", scheduler_status())
        _report("scheduler_start", scheduler_start())
        _report("scheduler_stop", scheduler_stop())
        _report("tail_logs", tail_logs(5), optional=True)
        _report("add_watchlist", add_watchlist("600519.SH", group="远期", name="贵州茅台"))
        _report("get_watchlist", get_watchlist("远期"))
        _report("remove_watchlist", remove_watchlist("600519.SH"))
    finally:
        reset_services()
        try:
            database.dispose()
        except Exception:  # noqa: BLE001
            pass

    print("-" * 78)
    print(f"自检结果：PASS {passed} / FAIL {failed}")
    sys.exit(0 if failed == 0 else 1)

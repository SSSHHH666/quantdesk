"""QuantDesk 行情增量缓存、复权加载与数据质量校验（任务 M1-03 + M1-04 / 契约 CF-2）。

能力清单
--------
1. **增量更新** ``BarCache.update``：按每只标的已有最大交易日续拉，**重复拉取不重复写**
   （``daily_bars`` (symbol, trade_date) 唯一 + ``ON CONFLICT DO UPDATE`` upsert），
   返回 ``UpdateReport(added/updated/skipped)``（AC-F1-01）。
2. **复权加载** ``BarCache.load``：支持 ``hfq``（后复权）/``qfq``（前复权）/``none``，
   缺失复权因子时退化为不复权并记 WARN（SPEC §4.6 / DQ005）。
3. **数据校验** ``BarCache.validate``：检测 DQ001–DQ007 全部七类问题，写入
   ``data_quality_issues``，返回 ``ValidationReport``（含严重度、中文说明与建议）。
4. **辅助**：``latest_date``（各标的/整体最新交易日）、``sync_trading_calendar``（交易日历落库）、
   ``sync_adjust_events``（除权除息事件落库）。

约定
----
- CF-2 方法签名冻结：``update`` / ``load`` / ``validate`` / ``latest_date``；
- 数据库通过 :class:`core.db.Database` 或会话工厂注入（默认取全局数据库单例），
  便于 QA 用临时库做隔离测试；
- 数据源通过 :class:`core.provider.DataProvider` 注入（默认按 ``config.data`` 组装主备链）；
- 本模块只依赖 core/* 与 data/provider.py，不 import 策略/交易/UI 模块。

自检（quantdesk 目录）：``python -m data.cache``
"""

from __future__ import annotations

import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Iterator, Sequence

import pandas as pd
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from core.config import AppConfig, get_config
from core.db import Database, get_database
from core.logging import get_logger
from core.models import (
    AdjustEventORM,
    DailyBarORM,
    DataQualityIssueORM,
    IssueSeverity,
    QDModel,
    TradingCalendarORM,
    now_local,
    now_local_naive,
)
from data.provider import (
    ALL_BAR_COLUMNS,
    DataProvider,
    ProviderRegistry,
    build_provider,
    get_registry,
    normalize_symbols,
)

__all__ = [
    "UpdateReport",
    "ValidationIssue",
    "ValidationReport",
    "BarCache",
    "DataIntegrityError",
    "DQ_SEVERITY",
    "DQ_SUGGESTIONS",
]

logger = get_logger("data.cache")


class DataIntegrityError(ValueError):
    """数据完整性错误（中文可操作提示）。

    典型场景：同一标的、同一请求区间内 ``daily_bars`` 混存多个数据源（如 mock 与 akshare），
    若继续拼接会得到假收益（P0 缺陷）。此时 ``load()`` **fail-closed 拒绝返回**。
    """

#: DQ001–DQ007 严重度与中文建议动作（SPEC §9.2）
DQ_SEVERITY: dict[str, IssueSeverity] = {
    "DQ001": IssueSeverity.ERROR,  # 缺失 K 线
    "DQ002": IssueSeverity.WARN,   # 重复 K 线
    "DQ003": IssueSeverity.ERROR,  # 价格异常跳变
    "DQ004": IssueSeverity.WARN,   # 零成交量
    "DQ005": IssueSeverity.WARN,   # 复权因子缺失
    "DQ006": IssueSeverity.WARN,   # 数据过期
    "DQ007": IssueSeverity.ERROR,  # 停牌未标记
}

DQ_SUGGESTIONS: dict[str, str] = {
    "DQ001": "重新拉取该区间数据（或检查交易日历/数据源）",
    "DQ002": "对重复行去重（保留最新一条）",
    "DQ003": "检查除权除息事件；若无事件则为数据源异常，建议重拉该标的",
    "DQ004": "确认是否停牌/节假日前；若为停牌请补写 is_suspended 标记",
    "DQ005": "补齐复权因子；缺失期间回测/展示会退化为不复权",
    "DQ006": "刷新行情并检查数据源健康状态",
    "DQ007": "按停牌处理并补写 is_suspended=1，避免用 0 价格参与撮合",
}

#: upsert 时允许覆盖的行情列（不覆盖 created_at）
_BAR_UPDATE_COLUMNS: tuple[str, ...] = (
    "open", "high", "low", "close", "volume", "amount",
    "adj_factor", "turnover_rate", "is_suspended", "source",
)

#: 单次 SQL 批大小（避免 SQLite 变量上限）
_BATCH_SIZE = 300


def _chunks(items: Sequence[Any], size: int = _BATCH_SIZE) -> Iterator[Sequence[Any]]:
    """把序列切分为固定大小批次。"""
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _as_date(value: Any) -> date:
    """把 date/datetime/字符串统一成 ``datetime.date``。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def _is_missing(value: Any) -> bool:
    """判断标量是否缺失（None/NaN/NaT）。"""
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _same_value(left: Any, right: Any) -> bool:
    """比较两个字段值是否等价（数值按容差，缺失值互相等价）。"""
    if _is_missing(left) and _is_missing(right):
        return True
    if _is_missing(left) or _is_missing(right):
        return False
    if isinstance(left, bool) or isinstance(right, bool):
        return bool(left) == bool(right)
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right)) < 1e-9
    return left == right


# ---------------------------------------------------------------------------
# 报告模型（CF-2 返回值字段契约）
# ---------------------------------------------------------------------------


class UpdateReport(QDModel):
    """增量更新报告（CF-2：``added/updated/skipped`` 行数 + 数据源 + 耗时）。"""

    provider: str = ""
    source: str = ""
    symbols: list[str] = Field(default_factory=list)
    start: date
    end: date
    freq: str = "1d"
    added: int = 0          # 新增行数
    updated: int = 0        # 更新（值发生变化）行数
    skipped: int = 0        # 已存在且未变化（跳过）行数
    mock_blocked: int = 0   # 因"mock 数据禁止入库"被拦截的行数（P0 数据完整性约束，扩展字段）
    failed_symbols: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    adjust_events_added: int = 0
    elapsed_ms: float = 0.0
    message: str = ""

    @property
    def total_rows(self) -> int:
        """本次处理涉及的总行数（新增 + 更新 + 跳过）。"""
        return self.added + self.updated + self.skipped


class ValidationIssue(QDModel):
    """单条数据质量问题（DQ001–DQ007）。"""

    run_id: str
    symbol: str = ""
    trade_date: date | None = None
    issue_code: str
    severity: IssueSeverity = IssueSeverity.ERROR
    detail: str = ""          # 中文问题说明
    suggestion: str = ""      # 中文建议动作


class ValidationReport(QDModel):
    """数据校验报告（CF-2 返回值；已写入 ``data_quality_issues``）。"""

    run_id: str
    symbols: list[str] = Field(default_factory=list)
    start: date
    end: date
    issues: list[ValidationIssue] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)           # DQ 码 → 数量
    severity_counts: dict[str, int] = Field(default_factory=dict)  # ERROR/WARN → 数量
    checked_at: datetime = Field(default_factory=now_local)
    message: str = ""

    @property
    def total_issues(self) -> int:
        """问题总数。"""
        return len(self.issues)

    @property
    def has_errors(self) -> bool:
        """是否存在 ERROR 级问题（阻断式问题）。"""
        return any(issue.severity is IssueSeverity.ERROR for issue in self.issues)


# ---------------------------------------------------------------------------
# BarCache
# ---------------------------------------------------------------------------


class BarCache:
    """日线行情缓存服务（CF-2：``update`` / ``load`` / ``validate`` / ``latest_date``）。

    :param provider: 数据源；为空时按配置组装主备链（akshare 未接入时自动降级 mock）
    :param config: 全局配置；为空时用 ``core.config.get_config()``
    :param database: 数据库管理器；为空时用全局数据库单例
    :param session_factory: 直接指定会话工厂（优先于 ``database``；QA 临时库常用）
    :param registry: 数据源注册表（测试可注入）
    """

    def __init__(
        self,
        provider: DataProvider | None = None,
        *,
        config: AppConfig | None = None,
        database: Database | None = None,
        session_factory: sessionmaker[Session] | None = None,
        registry: ProviderRegistry | None = None,
    ) -> None:
        self.config = config or get_config()
        self.registry = registry or get_registry()
        self.provider = provider or build_provider(self.config, self.registry)
        if session_factory is not None:
            self._session_factory = session_factory
        elif database is not None:
            self._session_factory = database.session_factory
        else:
            self._session_factory = get_database().session_factory
        self._lock = threading.RLock()
        #: 当前校验批次 ID（validate 内部使用；单实例串行调用）
        self._current_run_id: str = ""

    # -- 基础设施 ---------------------------------------------------------
    @contextmanager
    def _session(self) -> Iterator[Session]:
        """事务上下文：正常提交、异常回滚、最终关闭。"""
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def __repr__(self) -> str:  # pragma: no cover - 调试展示
        return f"<BarCache provider={self.provider.name}>"

    # -- 查询辅助 ---------------------------------------------------------
    def latest_date(self, symbols: list[str] | str | None = None) -> date | None:
        """返回指定标的（为空则全部）的最新交易日；无数据返回 ``None``。"""
        symbol_list = normalize_symbols(symbols) if symbols is not None else []
        with self._session() as session:
            statement = select(func.max(DailyBarORM.trade_date))
            if symbol_list:
                statement = statement.where(DailyBarORM.symbol.in_(symbol_list))
            return session.execute(statement).scalar()

    def coverage_report(
        self, symbols: list[str], start: date, end: date
    ) -> list[dict[str, Any]]:
        """数据覆盖报告（每标的首日/末日/行数/应有交易日/缺口天数/来源）。

        用于回填后核对「各标的起始日一致、无缺口、无 mock 混源」；只读，不修改数据。
        """
        symbol_list = normalize_symbols(symbols)
        start_date, end_date = _as_date(start), _as_date(end)
        if not symbol_list:
            return []
        result: list[dict[str, Any]] = []
        with self._session() as session:
            expected_days = self._expected_open_days(session, start_date, end_date)
            expected_set = set(expected_days)
            for symbol in symbol_list:
                rows = session.execute(
                    select(DailyBarORM.trade_date, DailyBarORM.source)
                    .where(
                        DailyBarORM.symbol == symbol,
                        DailyBarORM.trade_date >= start_date,
                        DailyBarORM.trade_date <= end_date,
                    )
                    .order_by(DailyBarORM.trade_date)
                ).all()
                dates = [row[0] for row in rows]
                sources = sorted({str(row[1] or "").strip() for row in rows})
                present = set(dates)
                missing = sorted(expected_set - present)
                result.append(
                    {
                        "symbol": symbol,
                        "first_date": min(dates) if dates else None,
                        "last_date": max(dates) if dates else None,
                        "rows": len(dates),
                        "expected_days": len(expected_days),
                        "missing_days": len(missing),
                        "missing_dates": [str(day) for day in missing[:20]],
                        "sources": sources,
                    }
                )
        return result

    def _bounds(self, session: Session, symbols: Iterable[str]) -> dict[str, tuple[date, date]]:
        """查询每只标的已缓存的（最早交易日, 最新交易日）。"""
        result: dict[str, tuple[date, date]] = {}
        rows = session.execute(
            select(
                DailyBarORM.symbol,
                func.min(DailyBarORM.trade_date),
                func.max(DailyBarORM.trade_date),
            )
            .where(DailyBarORM.symbol.in_(list(symbols)))
            .group_by(DailyBarORM.symbol)
        ).all()
        for symbol, min_date, max_date in rows:
            result[symbol] = (min_date, max_date)
        return result

    def _count_rows(self, session: Session, symbol: str, start: date, end: date) -> int:
        """统计某标的在区间内的缓存行数。"""
        return int(
            session.execute(
                select(func.count()).select_from(DailyBarORM).where(
                    DailyBarORM.symbol == symbol,
                    DailyBarORM.trade_date >= start,
                    DailyBarORM.trade_date <= end,
                )
            ).scalar()
            or 0
        )

    # -- 更新 -------------------------------------------------------------
    @staticmethod
    def _prepare_record(row: dict[str, Any], fallback_symbol: str, source: str) -> dict[str, Any]:
        """把数据源返回的一行行情转成 DB 写入字典（类型规整、缺失值转 None）。"""
        raw_date = row.get("date")
        if isinstance(raw_date, datetime):
            trade_date = raw_date.date()
        elif isinstance(raw_date, date):
            trade_date = raw_date
        else:
            trade_date = pd.Timestamp(raw_date).date()
        symbol = str(row.get("symbol") or fallback_symbol).strip().upper()
        adj_factor = row.get("adj_factor")
        turnover_rate = row.get("turnover_rate")
        is_suspended = row.get("is_suspended", False)
        return {
            "symbol": symbol,
            "trade_date": trade_date,
            "open": float(row.get("open") or 0.0),
            "high": float(row.get("high") or 0.0),
            "low": float(row.get("low") or 0.0),
            "close": float(row.get("close") or 0.0),
            "volume": int(float(row.get("volume") or 0)),
            "amount": float(row.get("amount") or 0.0),
            "adj_factor": None if _is_missing(adj_factor) else float(adj_factor),
            "turnover_rate": None if _is_missing(turnover_rate) else float(turnover_rate),
            "is_suspended": bool(is_suspended) if not _is_missing(is_suspended) else False,
            "source": str(row.get("source") or source or ""),
        }

    def _upsert_bars(self, session: Session, records: Sequence[dict[str, Any]]) -> tuple[int, int, int]:
        """行情 upsert；返回（新增, 变化更新, 未变化跳过）行数。"""
        if not records:
            return 0, 0, 0
        now = now_local_naive()
        dedup: dict[tuple[str, date], dict[str, Any]] = {}
        for record in records:
            dedup[(record["symbol"], record["trade_date"])] = record

        added: list[dict[str, Any]] = []
        changed: list[dict[str, Any]] = []
        skipped = 0
        by_symbol: dict[str, list[tuple[date, dict[str, Any]]]] = {}
        for (symbol, trade_date), record in dedup.items():
            by_symbol.setdefault(symbol, []).append((trade_date, record))

        for symbol, entries in by_symbol.items():
            dates = [trade_date for trade_date, _ in entries]
            existing_rows = session.execute(
                select(DailyBarORM).where(
                    DailyBarORM.symbol == symbol,
                    DailyBarORM.trade_date >= min(dates),
                    DailyBarORM.trade_date <= max(dates),
                )
            ).scalars().all()
            existing = {row.trade_date: row for row in existing_rows}
            for trade_date, record in entries:
                current = existing.get(trade_date)
                if current is None:
                    added.append({**record, "created_at": now, "updated_at": now})
                elif any(
                    not _same_value(getattr(current, column), record[column])
                    for column in _BAR_UPDATE_COLUMNS
                ):
                    changed.append({**record, "created_at": now, "updated_at": now})
                else:
                    skipped += 1

        rows_to_write = added + changed
        for chunk in _chunks(rows_to_write):
            statement = sqlite_insert(DailyBarORM)
            statement = statement.on_conflict_do_update(
                index_elements=["symbol", "trade_date"],
                set_={column: getattr(statement.excluded, column) for column in (*_BAR_UPDATE_COLUMNS, "updated_at")},
            )
            session.execute(statement, list(chunk))
        return len(added), len(changed), skipped

    def _sync_adjust_events(self, session: Session, symbols: Sequence[str]) -> int:
        """把数据源的除权除息事件写入 ``adjust_events``（幂等）；返回新增行数。"""
        getter = getattr(self.provider, "get_adjust_events", None)
        if not callable(getter):
            return 0
        try:
            events = getter(list(symbols))
        except Exception as exc:  # 数据源实现差异不影响主流程
            logger.warning("除权除息事件获取失败（不影响行情更新）：%s", exc)
            return 0
        if events is None or (isinstance(events, pd.DataFrame) and events.empty):
            return 0
        if isinstance(events, pd.DataFrame):
            records = events.to_dict("records")
        else:
            records = list(events)
        now = now_local_naive()
        rows: list[dict[str, Any]] = []
        for record in records:
            symbol = str(record.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            ex_date = _as_date(record["ex_date"])
            rows.append(
                {
                    "symbol": symbol,
                    "ex_date": ex_date,
                    "event_type": str(record.get("event_type") or "DIVIDEND"),
                    "cash_per_share": float(record.get("cash_per_share") or 0.0),
                    "stock_ratio": None if _is_missing(record.get("stock_ratio")) else float(record["stock_ratio"]),
                    "record_date": None if _is_missing(record.get("record_date")) else _as_date(record["record_date"]),
                    "source": str(record.get("source") or self.provider.name),
                    "created_at": now,
                }
            )
        if not rows:
            return 0
        # 复用调用方事务（update 内已在写事务中；另开会话会自锁）
        existing = set(
            session.execute(
                select(AdjustEventORM.symbol, AdjustEventORM.ex_date, AdjustEventORM.event_type)
                .where(
                    AdjustEventORM.symbol.in_({row["symbol"] for row in rows}),
                    AdjustEventORM.ex_date >= min(row["ex_date"] for row in rows),
                    AdjustEventORM.ex_date <= max(row["ex_date"] for row in rows),
                )
            ).all()
        )
        new_rows = [
            row for row in rows
            if (row["symbol"], row["ex_date"], row["event_type"]) not in existing
        ]
        for chunk in _chunks(new_rows):
            statement = sqlite_insert(AdjustEventORM).on_conflict_do_nothing(
                index_elements=["symbol", "ex_date", "event_type"]
            )
            session.execute(statement, list(chunk))
        return len(new_rows)

    def update(
        self,
        symbols: list[str],
        start: date,
        end: date,
        freq: str = "1d",
    ) -> UpdateReport:
        """增量更新日线行情（CF-2 冻结方法）。

        增量口径：按每只标的已缓存的最新交易日续拉，缺失区间才请求数据源；
        写入使用 SQLite ``ON CONFLICT DO UPDATE``，重复拉取不重复写、内容不变不更新时间戳。

        :param symbols: 标的列表（如 ``["600519.SH"]``）
        :param start: 开始日期（含）
        :param end: 结束日期（含）
        :param freq: 频率，MVP 仅支持 ``"1d"``
        :return: :class:`UpdateReport`
        """
        started = time.perf_counter()
        if freq != "1d":
            raise ValueError(f"BarCache.update 仅支持日频 freq='1d'（收到 {freq!r}）；分钟级不在 MVP 范围")
        symbol_list = normalize_symbols(symbols)
        start_date, end_date = _as_date(start), _as_date(end)
        report = UpdateReport(
            provider=self.provider.name,
            source=self.provider.name,
            symbols=symbol_list,
            start=start_date,
            end=end_date,
            freq=freq,
        )
        if not symbol_list:
            report.message = "未指定标的，未执行更新"
            report.elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            return report

        # P0 数据完整性约束：MockProvider 数据只允许内存使用，绝不写入 daily_bars。
        # 原因：mock 假价格与真实价格混存会让回测拼接近似收益（曾出现 246% 假收益）。
        provider_name = str(getattr(self.provider, "name", "") or "").lower()
        if provider_name == "mock":
            report.mock_blocked = len(symbol_list)
            report.elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            report.message = (
                "当前为 MockProvider 离线演示数据：按数据完整性约束 mock 行情不会写入 daily_bars"
                "（禁止用于真实回测）；如需缓存真实数据请把 config.yaml 的 data.provider 设为 akshare"
            )
            logger.warning(
                "已拦截 MockProvider 数据入库（数据完整性约束，禁止 mock 与真实行情混存）：symbols=%s",
                ",".join(symbol_list),
            )
            return report

        blocked_rows = 0
        fetched_records: list[dict[str, Any]] = []
        with self._session() as session:
            bounds = self._bounds(session, symbol_list)
            # 请求区间内已缓存行数（用于 skipped 口径：已存在且未变化）
            skipped_rows = sum(
                self._count_rows(session, symbol, start_date, end_date) for symbol in symbol_list
            )
            for symbol in symbol_list:
                cached = bounds.get(symbol)
                intervals: list[tuple[date, date]] = []
                if cached is None:
                    intervals.append((start_date, end_date))
                else:
                    min_date, max_date = cached
                    # 向前补历史
                    if start_date < min_date:
                        intervals.append((start_date, min_date - timedelta(days=1)))
                    # 向后增量
                    if end_date > max_date:
                        intervals.append((max_date + timedelta(days=1), end_date))
                for interval_start, interval_end in intervals:
                    # 区间内若无交易日（周末/节假日），直接跳过，避免无意义网络请求（周末增量可 <0.1s）
                    if not self._expected_open_days(session, interval_start, interval_end):
                        logger.debug("区间 %s ~ %s 无交易日，跳过拉取", interval_start, interval_end)
                        continue
                    try:
                        frame = self.provider.get_bars([symbol], interval_start, interval_end, freq)
                    except Exception as exc:
                        message = f"标的 {symbol} 拉取失败（{type(exc).__name__}: {exc}）"
                        report.failed_symbols.append(symbol)
                        report.errors.append(message)
                        logger.warning("行情拉取失败：%s", message)
                        continue
                    if frame is None or frame.empty:
                        logger.debug("数据源无数据：%s %s~%s", symbol, interval_start, interval_end)
                        continue
                    for row in frame.to_dict("records"):
                        record = self._prepare_record(row, symbol, self.provider.name)
                        if not (interval_start <= record["trade_date"] <= interval_end):
                            continue
                        # 兜底路径可能返回 mock 行（数据源失败→MockProvider），一律拦截不落库
                        if "mock" in str(record.get("source", "")).lower():
                            blocked_rows += 1
                            continue
                        fetched_records.append(record)

            added, updated, unchanged = self._upsert_bars(session, fetched_records)
            report.added = added
            report.updated = updated
            # 已存在且未变化 = 区间内已缓存行（扣除本次被更新覆盖的）+ 拉取后确认未变化的行
            report.skipped = max(skipped_rows - updated, 0) + unchanged
            report.adjust_events_added = self._sync_adjust_events(session, symbol_list)

        report.elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        report.message = (
            f"行情更新完成（{self.provider.name}）：新增 {report.added} 行、更新 {report.updated} 行、"
            f"跳过 {report.skipped} 行、除权事件 +{report.adjust_events_added}，耗时 {report.elapsed_ms:.0f} ms"
        )
        if report.failed_symbols:
            report.message += f"；失败标的 {len(report.failed_symbols)} 只"
        if blocked_rows:
            report.mock_blocked = blocked_rows
            report.message += (
                f"；已拦截 {blocked_rows} 行 mock 数据（离线 Mock 不写入缓存，不可用于真实回测）"
            )
            logger.warning(
                "已拦截 %s 行 mock 兜底数据入库（数据完整性约束；请检查数据源可用性）", blocked_rows
            )
        logger.info(report.message)
        return report

    # -- 加载 -------------------------------------------------------------
    def load(
        self,
        symbols: list[str],
        start: date,
        end: date,
        adjust: str = "hfq",
    ) -> pd.DataFrame:
        """加载日线行情（CF-2 冻结方法），支持后复权/前复权/不复权。

        :param adjust: ``hfq`` 后复权（回测默认）/``qfq`` 前复权（行情页默认）/``none`` 不复权
        :return: DataFrame，列顺序 ``date, symbol, open, high, low, close, volume, amount, ...``
        """
        mode = str(adjust).strip().lower()
        if mode not in ("hfq", "qfq", "none"):
            raise ValueError(f"复权方式 {adjust!r} 非法：仅支持 hfq（后复权）/ qfq（前复权）/ none（不复权）")
        symbol_list = normalize_symbols(symbols)
        if not symbol_list:
            return pd.DataFrame(columns=list(ALL_BAR_COLUMNS))
        start_date, end_date = _as_date(start), _as_date(end)
        with self._session() as session:
            # 注意：这里只取标量列（不是 ORM 实例）。会话关闭后若再访问 ORM 行属性，
            # 在调用方注入默认 expire_on_commit=True 的 session factory 时会触发英文
            # DetachedInstanceError，从而掩盖我们想抛出的中文 DataIntegrityError。
            rows = session.execute(
                select(
                    DailyBarORM.symbol,
                    DailyBarORM.trade_date,
                    DailyBarORM.open,
                    DailyBarORM.high,
                    DailyBarORM.low,
                    DailyBarORM.close,
                    DailyBarORM.volume,
                    DailyBarORM.amount,
                    DailyBarORM.adj_factor,
                    DailyBarORM.turnover_rate,
                    DailyBarORM.is_suspended,
                    DailyBarORM.source,
                )
                .where(
                    DailyBarORM.symbol.in_(symbol_list),
                    DailyBarORM.trade_date >= start_date,
                    DailyBarORM.trade_date <= end_date,
                )
                .order_by(DailyBarORM.symbol, DailyBarORM.trade_date)
            ).all()
        if not rows:
            return pd.DataFrame(columns=list(ALL_BAR_COLUMNS))
        # P0 数据完整性（fail-closed）：同一标的在请求区间内混存多个数据源时拒绝返回。
        # 混拼 mock 假价格（约 100 元）与真实价格（约 1400 元）会算出假收益，绝不允许。
        # 此时 rows 已是纯值元组，session 关闭后读取安全（不依赖 expire_on_commit 配置）。
        sources_by_symbol: dict[str, set[str]] = {}
        for row in rows:
            sources_by_symbol.setdefault(str(row[0]), set()).add(str(row[11] or "").strip())
        mixed = {
            symbol: sorted(source_set)
            for symbol, source_set in sources_by_symbol.items()
            if len(source_set) > 1
        }
        if mixed:
            detail = "；".join(f"{symbol}: {', '.join(source_set)}" for symbol, source_set in sorted(mixed.items()))
            raise DataIntegrityError(
                "检测到行情数据来源混存（mock 假数据与真实行情不可拼接，已拒绝返回）："
                f"{detail}。请运行 scripts/purge_mock_cache.py 清理 mock 数据后重试"
            )
        frame = pd.DataFrame(
            {
                "date": pd.to_datetime([row[1] for row in rows]),
                "symbol": [row[0] for row in rows],
                "open": [row[2] for row in rows],
                "high": [row[3] for row in rows],
                "low": [row[4] for row in rows],
                "close": [row[5] for row in rows],
                "volume": [row[6] for row in rows],
                "amount": [row[7] for row in rows],
                "adj_factor": [row[8] for row in rows],
                "turnover_rate": [row[9] for row in rows],
                "is_suspended": [bool(row[10]) for row in rows],
            }
        )
        if mode != "none":
            frame = self._apply_adjust(frame, mode)
        return frame.sort_values(["date", "symbol"], ignore_index=True)

    @staticmethod
    def _apply_adjust(frame: pd.DataFrame, mode: str) -> pd.DataFrame:
        """按后复权/前复权口径调整 OHLC；缺失因子退化为不复权并告警。"""
        parts: list[pd.DataFrame] = []
        for symbol, group in frame.groupby("symbol", sort=False):
            data = group.copy()
            factors = pd.to_numeric(data["adj_factor"], errors="coerce")
            if factors.isna().all() or (factors.fillna(0.0) <= 0).all():
                logger.warning("标的 %s 缺少有效复权因子，回测/展示退化为不复权（DQ005）", symbol)
                parts.append(data)
                continue
            if factors.isna().any():
                logger.warning("标的 %s 部分 K 线缺少复权因子，缺失行按不复权处理（DQ005）", symbol)
            factors = factors.fillna(1.0).where(lambda series: series > 0, 1.0)
            if mode == "hfq":
                price_multiplier = factors
            else:  # qfq：按区间内最后一个复权因子归一
                last_factor = float(factors.iloc[-1]) or 1.0
                price_multiplier = factors / last_factor
            for column in ("open", "high", "low", "close"):
                data[column] = (data[column].astype(float) * price_multiplier).round(4)
            data["adj_factor"] = factors
            parts.append(data)
        return pd.concat(parts, ignore_index=True)

    # -- 校验 -------------------------------------------------------------
    @staticmethod
    def _fallback_calendar(provider: DataProvider, start: date, end: date) -> list[date]:
        """数据源日历兜底（DB 中无交易日历表数据时使用）。"""
        try:
            days = provider.get_trading_calendar(start, end)
            if days:
                return sorted(_as_date(day) for day in days)
        except Exception as exc:  # pragma: no cover - 数据源异常兜底
            logger.warning("交易日历获取失败，退化为工作日日历：%s", exc)
        return [timestamp.date() for timestamp in pd.bdate_range(start, end)]

    def _expected_open_days(self, session: Session, start: date, end: date) -> list[date]:
        """交易日历中的开市日；DB 无数据时用数据源日历兜底。"""
        rows = session.execute(
            select(TradingCalendarORM.trade_date)
            .where(
                TradingCalendarORM.is_open.is_(True),
                TradingCalendarORM.trade_date >= start,
                TradingCalendarORM.trade_date <= end,
            )
            .order_by(TradingCalendarORM.trade_date)
        ).scalars().all()
        if rows:
            return list(rows)
        return self._fallback_calendar(self.provider, start, end)

    def _add_issue(
        self,
        issues: list[ValidationIssue],
        cap_tracker: dict[tuple[str, str], int],
        *,
        symbol: str,
        code: str,
        detail: str,
        trade_date: date | None = None,
        cap: int = 20,
    ) -> None:
        """追加一条校验问题（同标的同码最多记录 ``cap`` 条，避免刷屏）。"""
        key = (symbol, code)
        if cap_tracker.get(key, 0) >= cap:
            return
        cap_tracker[key] = cap_tracker.get(key, 0) + 1
        issues.append(
            ValidationIssue(
                run_id=self._current_run_id,
                symbol=symbol,
                trade_date=trade_date,
                issue_code=code,
                severity=DQ_SEVERITY[code],
                detail=detail,
                suggestion=DQ_SUGGESTIONS[code],
            )
        )

    def validate(
        self,
        symbols: list[str],
        start: date,
        end: date,
    ) -> ValidationReport:
        """执行数据质量校验并写入 ``data_quality_issues``（CF-2 冻结方法）。

        检测项（SPEC §9.2）：

        - ``DQ001`` 缺失 K 线（对比交易日历）
        - ``DQ002`` 重复 K 线（防御性检查，正常被唯一约束拦截）
        - ``DQ003`` 价格异常跳变（|涨跌幅| > 20% 且当日无除权除息事件）
        - ``DQ004`` 零成交量
        - ``DQ005`` 复权因子缺失
        - ``DQ006`` 数据过期（最新数据早于应有交易日）
        - ``DQ007`` 停牌未标记（OHLC 与成交量全为 0 但未标记停牌）
        """
        symbol_list = normalize_symbols(symbols)
        start_date, end_date = _as_date(start), _as_date(end)
        run_id = f"val-{uuid.uuid4().hex[:10]}"
        self._current_run_id = run_id
        issues: list[ValidationIssue] = []
        cap_tracker: dict[tuple[str, str], int] = {}
        today = now_local().date()
        expected_end = min(end_date, today)

        with self._session() as session:
            expected_days = self._expected_open_days(session, start_date, expected_end)
            expected_set = set(expected_days)
            expected_last = expected_days[-1] if expected_days else None

            for symbol in symbol_list:
                rows = session.execute(
                    select(DailyBarORM)
                    .where(
                        DailyBarORM.symbol == symbol,
                        DailyBarORM.trade_date >= start_date,
                        DailyBarORM.trade_date <= end_date,
                    )
                    .order_by(DailyBarORM.trade_date)
                ).scalars().all()

                # DQ001 缺失 K 线
                if not rows:
                    self._add_issue(
                        issues, cap_tracker, symbol=symbol, code="DQ001",
                        detail=(
                            f"区间 {start_date} ~ {end_date} 无任何 K 线"
                            f"（按交易日历应有 {len(expected_days)} 个交易日）"
                        ),
                    )
                else:
                    present = {row.trade_date for row in rows}
                    for missing_day in sorted(expected_set - present):
                        self._add_issue(
                            issues, cap_tracker, symbol=symbol, code="DQ001", trade_date=missing_day,
                            detail=f"缺失 {missing_day} 的日线数据（该日为交易日）",
                        )

                # DQ002 重复 K 线（唯一约束保证正常不会出现，作防御性检查）
                duplicates = session.execute(
                    select(DailyBarORM.trade_date, func.count().label("count"))
                    .where(
                        DailyBarORM.symbol == symbol,
                        DailyBarORM.trade_date >= start_date,
                        DailyBarORM.trade_date <= end_date,
                    )
                    .group_by(DailyBarORM.trade_date)
                    .having(func.count() > 1)
                ).all()
                for trade_date, _count in duplicates:
                    self._add_issue(
                        issues, cap_tracker, symbol=symbol, code="DQ002", trade_date=trade_date,
                        detail=f"{trade_date} 存在重复 K 线记录",
                    )

                if not rows:
                    continue

                # 除权除息事件日期（用于跳变豁免）
                event_dates = set(
                    session.execute(
                        select(AdjustEventORM.ex_date).where(
                            AdjustEventORM.symbol == symbol,
                            AdjustEventORM.ex_date >= start_date,
                            AdjustEventORM.ex_date <= end_date,
                        )
                    ).scalars().all()
                )

                # DQ003 价格异常跳变 / DQ004 零成交量 / DQ005 复权因子缺失 / DQ007 停牌未标记
                previous_close: float | None = None
                for row in rows:
                    if previous_close and previous_close > 0:
                        change_pct = (float(row.close) / previous_close - 1.0) * 100.0
                        if abs(change_pct) > 20.0 and row.trade_date not in event_dates:
                            self._add_issue(
                                issues, cap_tracker, symbol=symbol, code="DQ003", trade_date=row.trade_date,
                                detail=(
                                    f"{row.trade_date} 收盘价相对前一交易日变动 {change_pct:.2f}%，"
                                    "超过 20% 且当日无除权除息事件"
                                ),
                            )
                    previous_close = float(row.close) if row.close else previous_close

                    ohlc_zero = all(
                        float(value or 0.0) == 0.0 for value in (row.open, row.high, row.low, row.close)
                    )
                    if ohlc_zero and int(row.volume or 0) == 0 and not row.is_suspended:
                        self._add_issue(
                            issues, cap_tracker, symbol=symbol, code="DQ007", trade_date=row.trade_date,
                            detail=f"{row.trade_date} OHLC 与成交量全为 0 但未标记停牌（is_suspended=0）",
                        )
                    elif int(row.volume or 0) == 0 and not row.is_suspended and float(row.close or 0.0) > 0:
                        self._add_issue(
                            issues, cap_tracker, symbol=symbol, code="DQ004", trade_date=row.trade_date,
                            detail=f"{row.trade_date} 成交量为 0（非停牌）",
                        )
                    if row.adj_factor is None or float(row.adj_factor or 0.0) <= 0:
                        self._add_issue(
                            issues, cap_tracker, symbol=symbol, code="DQ005", trade_date=row.trade_date,
                            detail=f"{row.trade_date} 复权因子缺失或非法（adj_factor={row.adj_factor!r}）",
                        )

                # DQ006 数据过期
                latest = rows[-1].trade_date
                if expected_last is not None and latest < expected_last:
                    self._add_issue(
                        issues, cap_tracker, symbol=symbol, code="DQ006", trade_date=latest,
                        detail=(
                            f"最新数据为 {latest}，早于应有交易日 {expected_last}"
                            f"（过期阈值 {self.config.data.stale_threshold_hours:g} 小时）"
                        ),
                    )

            # 写入 data_quality_issues（一次校验一个 run_id）
            if issues:
                now = now_local_naive()
                session.add_all(
                    [
                        DataQualityIssueORM(
                            run_id=issue.run_id,
                            symbol=issue.symbol,
                            trade_date=issue.trade_date,
                            issue_code=issue.issue_code,
                            severity=issue.severity.value,
                            detail=f"{issue.detail}；建议：{issue.suggestion}",
                            created_at=now,
                        )
                        for issue in issues
                    ]
                )

        counts: dict[str, int] = {}
        severity_counts: dict[str, int] = {}
        for issue in issues:
            counts[issue.issue_code] = counts.get(issue.issue_code, 0) + 1
            severity_counts[issue.severity.value] = severity_counts.get(issue.severity.value, 0) + 1

        if issues:
            message = (
                f"数据校验完成：共 {len(issues)} 个问题（"
                + "、".join(f"{code}×{count}" for code, count in sorted(counts.items()))
                + f"），涉及 {len({issue.symbol for issue in issues})} 只标的，已写入 data_quality_issues（run_id={run_id}）"
            )
        else:
            message = f"数据校验通过：{start_date} ~ {end_date} 未发现 DQ001–DQ007 问题"
        logger.info(message)

        return ValidationReport(
            run_id=run_id,
            symbols=symbol_list,
            start=start_date,
            end=end_date,
            issues=issues,
            counts=counts,
            severity_counts=severity_counts,
            checked_at=now_local(),
            message=message,
        )

    # -- 日历 / 除权事件同步 ---------------------------------------------
    def sync_trading_calendar(self, start: date, end: date) -> int:
        """把数据源交易日历写入 ``trading_calendar``（幂等），返回新增行数。"""
        start_date, end_date = _as_date(start), _as_date(end)
        days = sorted(set(self._fallback_calendar(self.provider, start_date, end_date)))
        if not days:
            return 0
        now = now_local_naive()
        rows: list[dict[str, Any]] = []
        for index, trade_date in enumerate(days):
            rows.append(
                {
                    "trade_date": trade_date,
                    "is_open": True,
                    "prev_trade_date": days[index - 1] if index > 0 else None,
                    "next_trade_date": days[index + 1] if index < len(days) - 1 else None,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        added = 0
        with self._session() as session:
            existing = set(
                session.execute(
                    select(TradingCalendarORM.trade_date).where(
                        TradingCalendarORM.trade_date >= start_date,
                        TradingCalendarORM.trade_date <= end_date,
                    )
                ).scalars().all()
            )
            new_rows = [row for row in rows if row["trade_date"] not in existing]
            for chunk in _chunks(new_rows):
                statement = sqlite_insert(TradingCalendarORM).on_conflict_do_nothing(
                    index_elements=["trade_date"]
                )
                session.execute(statement, list(chunk))
            added = len(new_rows)
        logger.info("交易日历同步完成：%s ~ %s 新增 %s 天", start_date, end_date, added)
        return added

    def sync_adjust_events(self, symbols: list[str] | None = None) -> int:
        """把数据源除权除息事件写入 ``adjust_events``（幂等），返回新增行数。"""
        symbol_list = normalize_symbols(symbols) if symbols is not None else []
        if not symbol_list:
            # 未指定标的时，从 symbols 表取（若为空则用配置自选池）
            try:
                from core.models import SymbolORM

                with self._session() as session:
                    symbol_list = list(session.execute(select(SymbolORM.symbol)).scalars().all())
            except Exception:  # pragma: no cover - 数据库不可用时兜底
                symbol_list = []
            if not symbol_list:
                symbol_list = normalize_symbols(self.config.data.universe)
        with self._session() as session:
            return self._sync_adjust_events(session, symbol_list)


# ---------------------------------------------------------------------------
# 自检（quantdesk 目录：``python -m data.cache``）
# ---------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    import shutil
    import tempfile
    from pathlib import Path

    import numpy as np
    import yaml
    from sqlalchemy import delete, func

    from core.config import DEFAULT_CONFIG_PATH, load_config, reset_config, set_config
    from core.db import Database
    from core.models import AdjustEventORM, DailyBarORM, DataQualityIssueORM, now_local_naive
    from data.provider import BAR_COLUMNS, MockProvider

    class _CacheTestProvider(MockProvider):
        """自检专用：非 mock 名称 + ``source='test'``，避免触发 P0「mock 禁止入库」拦截。"""

        name = "test"

        def get_bars(self, symbols, start, end, freq="1d"):  # type: ignore[override]
            frame = super().get_bars(symbols, start, end, freq)
            if not frame.empty:
                frame = frame.copy()
                frame["source"] = "test"
            return frame

        def get_adjust_events(self, symbols=None):  # type: ignore[override]
            frame = super().get_adjust_events(symbols)
            if not frame.empty:
                frame = frame.copy()
                frame["source"] = "test"
            return frame

    tmp_dir = Path(tempfile.mkdtemp(prefix="qd_cache_"))
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    raw["app"]["data_dir"] = str(tmp_dir / "runtime")
    raw["database"]["url"] = f"sqlite:///{(tmp_dir / 'runtime' / 'cache.db').as_posix()}"
    raw["data"]["provider"] = "mock"
    config_path = tmp_dir / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    cfg = load_config(config_path, environ={})
    set_config(cfg)
    database = Database(config=cfg)
    try:
        health = database.initialize()
        assert health.ok, health.message
        provider = _CacheTestProvider(cfg)
        cache = BarCache(provider=provider, config=cfg, database=database)

        symbols = ["600519.SH", "000001.SZ", "300750.SZ"]
        begin, first_end, july_end = date(2023, 1, 1), date(2023, 6, 30), date(2023, 7, 31)
        first_days = len(pd.bdate_range(begin, first_end))
        first_rows = first_days * len(symbols)

        # 1) 交易日历同步（幂等）
        calendar_added = cache.sync_trading_calendar(date(2023, 1, 1), date(2023, 12, 31))
        assert calendar_added == len(pd.bdate_range(date(2023, 1, 1), date(2023, 12, 31))), calendar_added
        assert cache.sync_trading_calendar(date(2023, 1, 1), date(2023, 12, 31)) == 0

        # 2) 首次全量更新（AC-F1-01 前置：写入行数正确、除权事件落库）
        report_first = cache.update(symbols, begin, first_end)
        assert report_first.added == first_rows, (report_first.added, first_rows)
        assert report_first.updated == 0 and report_first.skipped == 0
        assert report_first.provider == "test" and report_first.adjust_events_added > 0
        assert "行情更新完成" in report_first.message
        with database.session() as session:
            assert session.execute(select(func.count()).select_from(DailyBarORM)).scalar() == first_rows
            assert session.execute(select(func.count()).select_from(AdjustEventORM)).scalar() == 5 * len(symbols)

        # 3) 重复更新幂等（AC-F1-01：重复拉取不重复写）
        report_second = cache.update(symbols, begin, first_end)
        assert report_second.added == 0 and report_second.updated == 0
        assert report_second.skipped == first_rows, report_second.skipped
        with database.session() as session:
            assert session.execute(select(func.count()).select_from(DailyBarORM)).scalar() == first_rows

        # 4) 增量更新（只补新区间）+ latest_date
        july_rows = len(pd.bdate_range(date(2023, 7, 1), july_end)) * len(symbols)
        report_increment = cache.update(symbols, date(2023, 7, 1), july_end)
        assert report_increment.added == july_rows, (report_increment.added, july_rows)
        assert cache.latest_date(symbols) == july_end
        # 向前补历史（2021–2022），用于验证前复权
        cache.update(["600519.SH"], date(2021, 1, 1), date(2022, 12, 31))

        # 5) 加载与复权口径（CF-2 load）
        total_rows = first_rows + july_rows
        hfq = cache.load(symbols, begin, july_end, adjust="hfq")
        qfq = cache.load(symbols, begin, july_end, adjust="qfq")
        raw_close = cache.load(symbols, begin, july_end, adjust="none")
        assert len(raw_close) == total_rows, (len(raw_close), total_rows)
        assert list(raw_close.columns)[: len(BAR_COLUMNS)] == list(BAR_COLUMNS)
        assert hfq["close"].sum() > raw_close["close"].sum()  # 后复权价 ≥ 不复权价
        # 前复权：区间内 2023-01-04 有除权因子跃升，跃升前价格被下调、之后等于原价
        qfq_ratio = qfq["close"] / raw_close["close"]
        assert qfq_ratio.max() <= 1.0 + 1e-6 and qfq_ratio.min() < 0.99, (qfq_ratio.min(), qfq_ratio.max())
        qfq_long = cache.load(["600519.SH"], date(2021, 1, 1), date(2022, 12, 31), adjust="qfq")
        raw_long = cache.load(["600519.SH"], date(2021, 1, 1), date(2022, 12, 31), adjust="none")
        assert len(qfq_long) == len(raw_long) > 0
        assert qfq_long["close"].iloc[0] < raw_long["close"].iloc[0]  # 前复权：历史价格被下调

        # 6) 干净数据校验通过
        clean_report = cache.validate(symbols, begin, july_end)
        assert clean_report.total_issues == 0, clean_report.model_dump()
        assert "通过" in clean_report.message and clean_report.run_id.startswith("val-")

        # 7) 构造缺陷：缺 2 根 K 线 / 价格跳变 / 零成交量 / 复权因子缺失 / 停牌未标记
        with database.session() as session:
            session.execute(
                delete(DailyBarORM).where(
                    DailyBarORM.symbol == "600519.SH",
                    DailyBarORM.trade_date.in_([date(2023, 2, 1), date(2023, 2, 2)]),
                )
            )
            rows_000001 = session.execute(
                select(DailyBarORM)
                .where(DailyBarORM.symbol == "000001.SZ", DailyBarORM.trade_date >= begin)
                .order_by(DailyBarORM.trade_date)
            ).scalars().all()
            jump = rows_000001[10]
            previous = rows_000001[9]
            jump.close = round(float(previous.close) * 1.5, 2)
            jump.high = max(float(jump.high), jump.close)
            rows_300750 = session.execute(
                select(DailyBarORM)
                .where(DailyBarORM.symbol == "300750.SZ", DailyBarORM.trade_date >= begin)
                .order_by(DailyBarORM.trade_date)
            ).scalars().all()
            rows_300750[20].volume = 0                                   # DQ004 零成交量
            rows_300750[21].adj_factor = None                            # DQ005 复权因子缺失
            suspended_like = rows_300750[22]                             # DQ007 停牌未标记
            suspended_like.open = suspended_like.high = suspended_like.low = suspended_like.close = 0.0
            suspended_like.volume = 0
            suspended_like.is_suspended = False

        defect_report = cache.validate(symbols, begin, july_end)
        assert defect_report.total_issues > 0 and defect_report.has_errors
        assert defect_report.counts.get("DQ001", 0) >= 2, defect_report.counts
        assert defect_report.counts.get("DQ003", 0) >= 1, defect_report.counts
        assert defect_report.counts.get("DQ004", 0) >= 1, defect_report.counts
        assert defect_report.counts.get("DQ005", 0) >= 1, defect_report.counts
        assert defect_report.counts.get("DQ007", 0) >= 1, defect_report.counts
        assert sum(defect_report.counts.values()) == defect_report.total_issues
        with database.session() as session:
            stored = session.execute(
                select(func.count()).select_from(DataQualityIssueORM)
                .where(DataQualityIssueORM.run_id == defect_report.run_id)
            ).scalar()
        assert stored == defect_report.total_issues, (stored, defect_report.total_issues)
        assert defect_report.issues[0].suggestion  # 中文建议动作非空

        # 8) DQ006 数据过期：请求区间超出已有数据
        stale_report = cache.validate(symbols, begin, date(2023, 12, 31))
        assert stale_report.counts.get("DQ006", 0) == len(symbols), stale_report.counts

        # 9) 除权事件同步幂等 / 空输入保护 / 非法参数中文异常
        assert cache.sync_adjust_events(symbols) == 0  # 更新时已落库
        assert cache.load([], begin, july_end).empty
        assert cache.load(["NOT-EXIST.SH"], begin, july_end).empty
        for bad_call in (
            lambda: cache.update(symbols, begin, july_end, freq="1m"),
            lambda: cache.load(symbols, begin, july_end, adjust="bad"),
        ):
            try:
                bad_call()
            except ValueError as exc:
                assert any(ch >= "\u4e00" for ch in str(exc)), str(exc)
            else:
                raise AssertionError("非法参数未被拦截")

        # 10) 数据覆盖报告（首日/末日/行数/缺口/来源；此时 600519 因缺陷注入缺 2 天）
        coverage = cache.coverage_report(symbols, begin, july_end)
        assert len(coverage) == len(symbols)
        assert all(row["sources"] == ["test"] for row in coverage), coverage
        assert len({row["first_date"] for row in coverage}) == 1, coverage  # 各标的起始日一致
        missing_map = {row["symbol"]: row["missing_days"] for row in coverage}
        assert missing_map["000001.SZ"] == 0 and missing_map["300750.SZ"] == 0, coverage
        assert missing_map["600519.SH"] == 2, coverage  # 缺陷注入删除的 2 个交易日

        # 11) P0：MockProvider 数据绝不写入 daily_bars（行数不变）
        with database.session() as session:
            rows_before = session.execute(select(func.count()).select_from(DailyBarORM)).scalar()
        mock_cache = BarCache(provider=MockProvider(cfg), config=cfg, database=database)
        mock_report = mock_cache.update(["600519.SH"], date(2024, 1, 1), date(2024, 3, 31))
        assert mock_report.added == 0 and mock_report.updated == 0, mock_report.model_dump()
        assert mock_report.mock_blocked >= 1, mock_report.model_dump()
        assert "Mock" in mock_report.message or "mock" in mock_report.message
        with database.session() as session:
            rows_after = session.execute(select(func.count()).select_from(DailyBarORM)).scalar()
            assert rows_after == rows_before, (rows_before, rows_after)
            assert session.execute(
                select(func.count()).select_from(DailyBarORM).where(DailyBarORM.source == "mock")
            ).scalar() == 0

        # 12) P0：同一标的混存多来源 → load() fail-closed（拒绝拼接假收益）
        #     并验证调用方注入默认 expire_on_commit=True 的 session factory 时仍抛中文 DataIntegrityError
        with database.session() as session:
            session.add(
                DailyBarORM(
                    symbol="600519.SH", trade_date=date(2023, 1, 7),  # 周六，不影响既有工作日数据
                    open=100.0, high=101.0, low=99.0, close=100.0, volume=1000, amount=100000.0,
                    adj_factor=1.0, turnover_rate=1.0, is_suspended=False, source="mock",
                    created_at=now_local_naive(), updated_at=now_local_naive(),
                )
            )
        from sqlalchemy.orm import sessionmaker as _sessionmaker

        strict_cache = BarCache(
            provider=provider,
            config=cfg,
            session_factory=_sessionmaker(bind=database.engine, expire_on_commit=True),
        )
        for loader in (cache, strict_cache):
            try:
                loader.load(["600519.SH"], begin, july_end, adjust="none")
            except DataIntegrityError as exc:
                assert "混存" in str(exc) and "purge_mock_cache" in str(exc), str(exc)
            except Exception as exc:  # DetachedInstanceError 等：必须是中文 DataIntegrityError
                raise AssertionError(
                    f"混源检测抛出非中文 DataIntegrityError（{type(exc).__name__}: {exc}）"
                ) from exc
            else:
                raise AssertionError("混源数据未被 fail-closed 拦截")
        with database.session() as session:
            session.execute(delete(DailyBarORM).where(DailyBarORM.source == "mock"))
        assert not cache.load(["600519.SH"], begin, july_end, adjust="none").empty
        assert not strict_cache.load(["600519.SH"], begin, july_end, adjust="none").empty

        print(
            "data/cache.py 自检通过：交易日历同步幂等/首次全量/重复更新幂等(AC-F1-01)/增量补齐/"
            "hfq·qfq·none 复权/干净数据校验/DQ001·003·004·005·007 检出并落库(AC-F1-02)/DQ006 过期/"
            "空输入与非法参数/覆盖报告/P0 mock 禁止入库/混源 fail-closed"
        )
    finally:
        database.dispose()
        reset_config()
        # 清理临时目录（本机 tempfile 根目录可能指向工作目录，避免污染项目）
        shutil.rmtree(tmp_dir, ignore_errors=True)

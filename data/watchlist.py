"""QuantDesk 自选池存储（F12 / SPEC §5.4 canonical 结构）。

数据模型（扩展表，不修改 22 张核心表）
--------------------------------------
- ``watchlist_groups``：分组（默认「全部/远期/近期/手动」，可增删改）；
- ``watchlist_items``：自选池条目（``UNIQUE(group_id, symbol)``，一个标的可属多个分组），
  字段 ``id/group_id/symbol/note/tags_json/source/is_active/sort_order/created_at/updated_at``。

说明：早期 Dev1 版本使用过 ``symbol`` 主键的独立 ``watchlist_items`` 表；``core/db.py`` schema v2
迁移会自动转换为本规范结构（旧数据保留）。

能力：CRUD + 分组 + JSON 导入导出（兼容列表/包装键/分组映射）+ 代码格式与去重校验 +
ST 风险提示；名称通过 ``symbols`` 表/全市场快照解析（``data.symbols.name_of``）。

常用入口::

    from data.watchlist import get_watchlist, add, remove, set_group, WatchlistService

自检（quantdesk 目录）：``python -m data.watchlist``
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from core.config import AppConfig, get_config
from core.db import Database, get_database
from core.logging import get_logger
from core.models import (
    MarketSnapshotORM,
    SymbolORM,
    WatchlistGroupORM,
    WatchlistItemORM,
    now_local_naive,
)

__all__ = [
    "GROUP_DEFAULT",
    "GROUP_LONG_TERM",
    "GROUP_SHORT_TERM",
    "GROUP_MANUAL",
    "VALID_GROUPS",
    "GROUP_NAMES",
    "WatchlistError",
    "WatchlistItem",
    "WatchlistService",
    "validate_symbol",
    "normalize_group",
    "get_watchlist",
    "add",
    "remove",
    "set_group",
    "get_service",
    "reset_service",
]

logger = get_logger("data.watchlist")

#: 分组常量（SPEC §5.4：默认「全部/远期/近期/手动」）
GROUP_DEFAULT = "default"
GROUP_LONG_TERM = "long_term"
GROUP_SHORT_TERM = "short_term"
GROUP_MANUAL = "manual"
VALID_GROUPS: tuple[str, ...] = (GROUP_DEFAULT, GROUP_LONG_TERM, GROUP_SHORT_TERM, GROUP_MANUAL)

#: 分组 ID → (中文名, 排序)
GROUP_NAMES: dict[str, tuple[str, int]] = {
    GROUP_DEFAULT: ("全部", 0),
    GROUP_LONG_TERM: ("远期", 1),
    GROUP_SHORT_TERM: ("近期", 2),
    GROUP_MANUAL: ("手动", 3),
}

_SYMBOL_PATTERN = r"^[0-9]{6}\.(SH|SZ|BJ)$"


class WatchlistError(ValueError):
    """自选池操作错误（中文可操作提示）。"""


def validate_symbol(symbol: Any) -> str:
    """校验并规范化标的代码（``600519.SH``）；非法抛中文异常。"""
    text = str(symbol or "").strip().upper()
    if not re.match(_SYMBOL_PATTERN, text):
        raise WatchlistError(
            f"标的代码 {symbol!r} 格式非法：应为 6 位数字 + 交易所后缀（如 600519.SH / 000001.SZ / 899050.BJ）"
        )
    return text


def normalize_group(group: Any) -> str:
    """校验并规范化分组 ID（default/long_term/short_term/manual，也接受中文名；**大小写敏感**）。"""
    raw = str(group or GROUP_DEFAULT).strip()
    for group_id, (name, _sort) in GROUP_NAMES.items():
        if raw == group_id or raw == name:
            return group_id
    raise WatchlistError(
        f"分组 {group!r} 非法：仅支持 {' / '.join(VALID_GROUPS)}（远期=long_term，近期=short_term，手动=manual）"
    )


@dataclass
class WatchlistItem:
    """自选池条目（对外展示对象；名称由 symbols/快照解析）。"""

    symbol: str
    name: str = ""
    group_id: str = GROUP_DEFAULT
    group_name: str = GROUP_DEFAULT   # 契约：存中文显示名（远期/近期/全部/手动）
    group_label: str = ""
    note: str = ""
    source: str = "MANUAL"
    added_at: datetime | None = None
    updated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 友好字典（契约键：symbol/name/group/group_name/group_label/note/source/...）。"""
        return {
            "symbol": self.symbol,
            "name": self.name,
            "group": self.group_id,            # 分组 ID（long_term/short_term/...）
            "group_id": self.group_id,
            "group_name": self.group_name,     # 中文显示名（远期/近期/...）
            "group_label": self.group_label or self.group_name,
            "note": self.note,
            "source": self.source,
            "added_at": self.added_at.isoformat() if self.added_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class WatchlistService:
    """自选池服务（SQLite 扩展表 CRUD + 分组 + JSON 导入导出）。"""

    def __init__(
        self,
        *,
        config: AppConfig | None = None,
        database: Database | None = None,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        self.config = config or get_config()
        self.database: Database | None = database
        if session_factory is not None:
            # 完全走注入会话工厂：不触碰全局 database 单例（避免测试环境误建默认 runtime 库）
            self._session_factory = session_factory
        else:
            self.database = database or get_database()
            self._session_factory = self.database.session_factory
        self._schema_ready = False

    # -- 基础设施 ---------------------------------------------------------
    def _engine(self) -> Any:
        """获取用于建表的 Engine：优先 database，否则从会话工厂的 bind 解析。"""
        if self.database is not None:
            return self.database.engine
        bind = getattr(self._session_factory, "kw", {}).get("bind")
        if bind is not None:
            return bind
        with self._session() as session:
            return session.get_bind()

    @contextmanager
    def _session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def ensure_schema(self) -> None:
        """确保分组/条目扩展表与名称映射表存在并补齐默认分组（幂等）。"""
        if self._schema_ready:
            return
        engine = self._engine()
        WatchlistGroupORM.__table__.create(engine, checkfirst=True)
        WatchlistItemORM.__table__.create(engine, checkfirst=True)
        SymbolORM.__table__.create(engine, checkfirst=True)  # 名称映射（name_of 数据源）
        now = now_local_naive()
        with self._session() as session:
            for group_id, (name, sort_order) in GROUP_NAMES.items():
                statement = sqlite_insert(WatchlistGroupORM).values(
                    id=group_id, name=name, description="", sort_order=sort_order,
                    created_at=now, updated_at=now,
                )
                session.execute(statement.on_conflict_do_nothing(index_elements=["id"]))
        self._schema_ready = True
        logger.debug("自选池扩展表已就绪：watchlist_groups / watchlist_items / symbols(名称)")

    def _set_symbol_name(self, symbol: str, name: str) -> None:
        """把名称写入 ``symbols`` 表（供 get/list 展示与策略/UI 名称映射）。"""
        clean_name = str(name or "").strip()
        if not clean_name:
            return
        now = now_local_naive()
        with self._session() as session:
            statement = sqlite_insert(SymbolORM).values(
                symbol=symbol, name=clean_name, updated_at=now
            )
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=["symbol"],
                    set_={"name": clean_name, "updated_at": now},
                )
            )

    def _group_info(self, session: Session) -> dict[str, tuple[str, int]]:
        """读取分组 ID → (中文名, 排序)。"""
        rows = session.execute(
            select(WatchlistGroupORM.id, WatchlistGroupORM.name, WatchlistGroupORM.sort_order)
        ).all()
        return {str(row[0]): (str(row[1]), int(row[2])) for row in rows}

    def _resolve_names(self, symbols: list[str], session: Session | None = None) -> dict[str, str]:
        """从 ``symbols`` 表（其次全市场快照）解析名称；失败返回空映射。"""
        if not symbols:
            return {}
        try:
            if session is not None:
                return self._query_names(session, symbols)
            with self._session() as own_session:
                return self._query_names(own_session, symbols)
        except Exception:  # noqa: BLE001 - 名称是展示增强，失败不阻断
            return {}

    @staticmethod
    def _query_names(session: Session, symbols: list[str]) -> dict[str, str]:
        """在当前会话内查询名称（symbols 优先，market_snapshot 兜底）。"""
        rows = session.execute(
            select(SymbolORM.symbol, SymbolORM.name).where(SymbolORM.symbol.in_(symbols))
        ).all()
        result = {str(symbol): str(name) for symbol, name in rows if name}
        missing = [symbol for symbol in symbols if symbol not in result]
        if missing:
            try:
                snapshot_rows = session.execute(
                    select(MarketSnapshotORM.symbol, MarketSnapshotORM.name)
                    .where(MarketSnapshotORM.symbol.in_(missing), MarketSnapshotORM.name != "")
                    .order_by(MarketSnapshotORM.trade_date.desc())
                ).all()
                for symbol, name in snapshot_rows:
                    result.setdefault(str(symbol), str(name))
            except Exception:  # noqa: BLE001 - 快照表可能尚未创建
                pass
        return result

    def _rows_to_items(self, session: Session, rows: list[Any]) -> list[WatchlistItem]:
        """查询行 → 对外 WatchlistItem 列表（group_name=ID，group_label=中文名）。"""
        group_info = self._group_info(session)
        symbols = [str(row[0]) for row in rows]
        names = self._resolve_names(symbols, session=session)
        items: list[WatchlistItem] = []
        for row in rows:
            symbol = str(row[0])
            group_id = str(row[1] or GROUP_DEFAULT)
            group_label = group_info.get(group_id, (group_id, 99))[0]
            items.append(
                WatchlistItem(
                    symbol=symbol,
                    name=names.get(symbol, ""),
                    group_id=group_id,
                    group_name=group_label,   # 契约：group_name 存中文显示名
                    group_label=group_label,
                    note=str(row[2] or ""),
                    source=str(row[3] or "MANUAL"),
                    added_at=row[4],
                    updated_at=row[5],
                )
            )
        return items

    # -- 查询 -------------------------------------------------------------
    def count(self, group: str | None = None) -> int:
        """统计自选池条目数（默认仅启用项；可按分组过滤）。"""
        self.ensure_schema()
        statement = select(func.count()).select_from(WatchlistItemORM).where(
            WatchlistItemORM.is_active.is_(True)
        )
        if group is not None:
            statement = statement.where(WatchlistItemORM.group_id == normalize_group(group))
        with self._session() as session:
            return int(session.execute(statement).scalar() or 0)

    def list_items(self, group: str | None = None) -> list[WatchlistItem]:
        """列出自选池（可按分组过滤；按分组排序 + 代码排序）。"""
        self.ensure_schema()
        with self._session() as session:
            group_info = self._group_info(session)
            statement = select(
                WatchlistItemORM.symbol,
                WatchlistItemORM.group_id,
                WatchlistItemORM.note,
                WatchlistItemORM.source,
                WatchlistItemORM.created_at,
                WatchlistItemORM.updated_at,
            ).where(WatchlistItemORM.is_active.is_(True))
            if group is not None:
                statement = statement.where(WatchlistItemORM.group_id == normalize_group(group))
            rows = session.execute(statement).all()
            items = self._rows_to_items(session, rows)
        order = {group_id: info[1] for group_id, info in group_info.items()}
        items.sort(
            key=lambda item: (
                order.get(item.group_id, 99),
                item.symbol,
            )
        )
        return items

    @staticmethod
    def _group_id(group_name_or_id: str) -> str:
        """把中文分组名或 ID 统一为 ID（用于排序/导入）。"""
        for group_id, (name, _sort) in GROUP_NAMES.items():
            if group_name_or_id in (group_id, name):
                return group_id
        return group_name_or_id

    def get(self, symbol: Any, group: str | None = None) -> WatchlistItem | None:
        """按代码查询自选池条目（可限定分组）；不存在返回 ``None``。"""
        code = validate_symbol(symbol)
        self.ensure_schema()
        with self._session() as session:
            statement = select(
                WatchlistItemORM.symbol,
                WatchlistItemORM.group_id,
                WatchlistItemORM.note,
                WatchlistItemORM.source,
                WatchlistItemORM.created_at,
                WatchlistItemORM.updated_at,
            ).where(WatchlistItemORM.symbol == code)
            if group is not None:
                statement = statement.where(WatchlistItemORM.group_id == normalize_group(group))
            row = session.execute(statement).first()
            if row is None:
                return None
            return self._rows_to_items(session, [row])[0]

    # -- 增删改 -----------------------------------------------------------
    def add(
        self,
        symbol: Any,
        *,
        group: str = GROUP_DEFAULT,
        name: str | None = None,   # 契约：add 的 name 不落库（名称由 symbols/快照解析）
        note: str | None = None,
        source: str = "MANUAL",
    ) -> WatchlistItem:
        """加入自选池（**按 (symbol, group) 唯一**；同组重复只更新备注/来源）。

        说明（2026-10-26 契约）：同一标的可同时存在于多个分组；``name`` 参数仅为兼容保留，
        不落库；名称统一由 ``symbols``/全市场快照解析。ST/*ST 只记 WARNING，不阻断。
        """
        code = validate_symbol(symbol)
        group_id = normalize_group(group)
        clean_name = str(name or "").strip()
        if "ST" in clean_name.upper():
            logger.warning("自选池新增 ST 风险警示标的：%s（%s），请确认风险", code, clean_name)
        self.ensure_schema()
        now = now_local_naive()
        with self._session() as session:
            existing = session.execute(
                select(WatchlistItemORM.id).where(
                    WatchlistItemORM.symbol == code,
                    WatchlistItemORM.group_id == group_id,
                )
            ).first()
            if existing is None:
                session.execute(
                    sqlite_insert(WatchlistItemORM).values(
                        group_id=group_id,
                        symbol=code,
                        note=str(note or "").strip(),
                        tags_json="[]",
                        source=str(source or "MANUAL").upper(),
                        is_active=True,
                        sort_order=0,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                values: dict[str, Any] = {
                    "is_active": True,
                    "source": str(source or "MANUAL").upper(),
                    "updated_at": now,   # created_at 保持不变（幂等 upsert）
                }
                if note is not None:
                    values["note"] = str(note).strip()
                session.execute(
                    update(WatchlistItemORM)
                    .where(WatchlistItemORM.id == existing[0])
                    .values(**values)
                )
        logger.info("自选池已保存：%s（分组=%s）", code, group_id)
        item = self.get(code, group_id)
        assert item is not None
        return item

    def add_many(
        self,
        items: Iterable[str | Mapping[str, Any]],
        *,
        group: str = GROUP_DEFAULT,
        replace: bool = False,
        source: str = "IMPORT",
    ) -> dict[str, Any]:
        """批量加入，返回 ``{added, updated, skipped, errors, st_warnings}``。"""
        report: dict[str, Any] = {
            "added": 0, "updated": 0, "skipped": 0, "errors": [], "st_warnings": [],
        }
        if replace:
            self.clear(group=group)
        for item in items:
            try:
                parsed = self._parse_item(item, default_group=group)
                existed = self.get(parsed["symbol"], parsed["group"]) is not None
                self.add(
                    parsed["symbol"], group=parsed["group"], name=parsed.get("name"),
                    note=parsed.get("note"), source=source,
                )
                report["updated" if existed else "added"] += 1
                if "ST" in str(parsed.get("name") or "").upper():
                    report["st_warnings"].append(parsed["symbol"])
            except (WatchlistError, TypeError, ValueError) as exc:
                report["errors"].append({"item": item, "error": str(exc)})
        return report

    @staticmethod
    def _parse_item(item: Any, *, default_group: str) -> dict[str, Any]:
        if isinstance(item, str):
            return {
                "symbol": validate_symbol(item),
                "group": normalize_group(default_group),
                "name": None,
                "note": None,
            }
        if isinstance(item, Mapping):
            raw_symbol = item.get("symbol") or item.get("code") or item.get("ticker")
            if raw_symbol is None:
                raise WatchlistError(f"导入项缺少 symbol/code 字段：{dict(item)!r}")
            return {
                "symbol": validate_symbol(raw_symbol),
                "group": normalize_group(
                    item.get("group") or item.get("group_name") or default_group
                ),
                "name": item.get("name"),
                "note": item.get("note"),
            }
        raise WatchlistError(f"导入项类型不支持：{type(item).__name__}（应为字符串或字典）")

    def remove(self, symbol: Any, *, group: str | None = None) -> bool:
        """移出自选池（可限定分组）；任一条目被删除返回 True。"""
        code = validate_symbol(symbol)
        self.ensure_schema()
        with self._session() as session:
            statement = delete(WatchlistItemORM).where(WatchlistItemORM.symbol == code)
            if group is not None:
                statement = statement.where(
                    WatchlistItemORM.group_id == normalize_group(group)
                )
            result = session.execute(statement)
        removed = max(int(result.rowcount or 0), 0) > 0
        if removed:
            logger.info("自选池已移除：%s", code)
        return removed

    def set_group(self, symbol: Any, group: str) -> bool:
        """把标的所有分组的条目移到指定分组（同组已存在则去重合并）；成功返回 True。"""
        code = validate_symbol(symbol)
        target_group = normalize_group(group)
        self.ensure_schema()
        moved = False
        with self._session() as session:
            rows = session.execute(
                select(WatchlistItemORM.id, WatchlistItemORM.group_id).where(
                    WatchlistItemORM.symbol == code
                )
            ).all()
            if not rows:
                return False
            target_existing = session.execute(
                select(WatchlistItemORM.id).where(
                    WatchlistItemORM.symbol == code,
                    WatchlistItemORM.group_id == target_group,
                )
            ).first()
            now = now_local_naive()
            for row_id, current_group in rows:
                if str(current_group) == target_group:
                    continue
                if target_existing is not None:
                    session.execute(delete(WatchlistItemORM).where(WatchlistItemORM.id == row_id))
                else:
                    session.execute(
                        update(WatchlistItemORM)
                        .where(WatchlistItemORM.id == row_id)
                        .values(group_id=target_group, updated_at=now)
                    )
                    target_existing = (row_id,)
                moved = True
        if moved:
            logger.info("自选池分组已更新：%s → %s", code, target_group)
        return moved

    def clear(self, group: str | None = None) -> int:
        """清空自选池（可限定分组），返回删除条数。"""
        self.ensure_schema()
        with self._session() as session:
            statement = delete(WatchlistItemORM)
            if group is not None:
                statement = statement.where(
                    WatchlistItemORM.group_id == normalize_group(group)
                )
            result = session.execute(statement)
        removed = max(int(result.rowcount or 0), 0)
        logger.info("自选池已清空：%s 条（分组=%s）", removed, group or "全部")
        return removed

    # -- JSON 导入 / 导出 -------------------------------------------------
    @staticmethod
    def _extract_items(payload: Any) -> list[Any]:
        if isinstance(payload, list):
            return list(payload)
        if isinstance(payload, Mapping):
            if "symbol" in payload or "code" in payload:
                return [payload]
            for key in ("symbols", "stocks", "picks", "items", "universe", "watchlist", "data"):
                value = payload.get(key)
                if isinstance(value, list):
                    return list(value)
            group_items: list[Any] = []
            for group, value in payload.items():
                group_text = str(group).strip()
                group_id = WatchlistService._group_id(group_text)
                if group_id in VALID_GROUPS and isinstance(value, list):
                    for item in value:
                        if isinstance(item, str):
                            group_items.append({"symbol": item, "group": group_id})
                        elif isinstance(item, Mapping):
                            group_items.append({**dict(item), "group": item.get("group") or group_id})
                        else:
                            group_items.append(item)
            if group_items:
                return group_items
        raise WatchlistError(
            "JSON 结构无法识别：应为代码列表、含 symbols/stocks/picks 键的对象，或 {分组: [代码]} 映射"
        )

    def import_json(
        self,
        source: str | Path | Mapping[str, Any] | list[Any],
        *,
        group: str = GROUP_DEFAULT,
        replace: bool = False,
    ) -> dict[str, Any]:
        """从 JSON 文件/对象导入自选池，返回统计报告（单条失败不中断）。"""
        if isinstance(source, (str, Path)):
            path = Path(source)
            if not path.exists():
                raise WatchlistError(f"自选池 JSON 文件不存在：{path}")
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except json.JSONDecodeError as exc:
                raise WatchlistError(f"自选池 JSON 解析失败：{path}（{exc}）") from exc
        else:
            payload = source
        items = self._extract_items(payload)
        report = self.add_many(items, group=group, replace=replace, source="IMPORT")
        report["total"] = len(items)
        logger.info(
            "自选池导入完成：新增 %s、更新 %s、失败 %s、ST 提示 %s",
            report["added"], report["updated"], len(report["errors"]), len(report["st_warnings"]),
        )
        return report

    def export_json(self, path: str | Path | None = None) -> list[dict[str, Any]]:
        """导出自选池为 JSON（默认 ``<data_dir>/exports/watchlist.json``）。"""
        items = [item.to_dict() for item in self.list_items()]
        target = Path(path) if path is not None else self.config.export_dir / "watchlist.json"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info("自选池已导出：%s（%s 条）", target, len(items))
        except OSError as exc:
            raise WatchlistError(f"自选池导出失败：{target}（{exc}）") from exc
        return items

    # -- 初始化 -----------------------------------------------------------
    def init_from_universe(self, config: AppConfig | None = None) -> int:
        """自选池为空时用 ``config.data.universe`` 初始化（返回新增条数）。"""
        cfg = config or self.config
        if self.count() > 0:
            return 0
        universe = list(cfg.data.universe)
        if not universe:
            return 0
        names = self._resolve_names(universe)
        report = self.add_many(
            [{"symbol": symbol, "name": names.get(symbol)} for symbol in universe],
            group=GROUP_DEFAULT,
            source="MANUAL",
        )
        logger.info("自选池已用默认自选池初始化：%s 条", report["added"])
        return int(report["added"])

    def __repr__(self) -> str:  # pragma: no cover
        return "<WatchlistService tables=watchlist_groups/watchlist_items>"


# ---------------------------------------------------------------------------
# 模块级便捷函数（默认服务单例）
# ---------------------------------------------------------------------------

_service: WatchlistService | None = None


def get_service() -> WatchlistService:
    """获取自选池服务默认单例。"""
    global _service
    if _service is None:
        _service = WatchlistService()
    return _service


def reset_service() -> None:
    """重置默认服务单例（测试隔离用）。"""
    global _service
    _service = None


def get_watchlist(group: str | None = None) -> list[WatchlistItem]:
    """获取自选池列表（可按分组过滤；分组支持 ID 或中文名）。"""
    return get_service().list_items(group)


def add(
    symbol: Any,
    *,
    group: str = GROUP_DEFAULT,
    name: str | None = None,
    note: str | None = None,
) -> WatchlistItem:
    """加入自选池（便捷函数）。"""
    return get_service().add(symbol, group=group, name=name, note=note)


def remove(symbol: Any) -> bool:
    """移出自选池（便捷函数）。"""
    return get_service().remove(symbol)


def set_group(symbol: Any, group: str) -> bool:
    """设置分组（便捷函数）。"""
    return get_service().set_group(symbol, group)


# ---------------------------------------------------------------------------
# 自检（quantdesk 目录：``python -m data.watchlist``）
# ---------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    import shutil
    import tempfile

    from core.config import load_config, reset_config, set_config

    cfg = load_config(environ={})
    tmp_dir = Path(tempfile.mkdtemp(prefix="qd_watchlist_"))
    try:
        set_config(cfg)
        database = Database(url="sqlite://", config=cfg)
        database.initialize()
        service = WatchlistService(config=cfg, database=database)
        S1, S2, S3 = "601899.SH", "002594.SZ", "600036.SH"

        # 1) 默认分组 + 多分组 CRUD + 中文 group_name + source
        service.ensure_schema()
        with database.session() as session:
            group_ids = {row[0] for row in session.execute(select(WatchlistGroupORM.id)).all()}
        assert set(GROUP_NAMES) <= group_ids, group_ids
        first = service.add(S1, group=GROUP_SHORT_TERM, name="紫金矿业")
        assert first.symbol == S1 and first.group_name == "近期" and first.source == "MANUAL"
        assert isinstance(first.name, str)  # 名称由 symbols/快照解析，未导入时为空串
        assert service.count() == 1
        service.add(S1, group=GROUP_LONG_TERM)
        assert service.count() == 2, "多分组：同一标的可同时在近期/远期"
        assert service.get(S1, GROUP_SHORT_TERM) is not None and service.get(S1, GROUP_LONG_TERM) is not None
        again = service.add(S1, group=GROUP_SHORT_TERM, note="备注更新")
        assert service.count() == 2 and again.note == "备注更新"
        assert again.added_at == first.added_at, "同组 upsert 不应改变 created_at"
        assert [i.group_name for i in service.list_items(GROUP_LONG_TERM)] == ["远期"]

        # 2) set_group 移动 + remove 单组/全部
        service.set_group(S1, GROUP_LONG_TERM)  # 近期行合并到远期（去重）
        assert service.count() == 1 and service.get(S1, GROUP_SHORT_TERM) is None
        assert service.set_group("000001.SZ", GROUP_LONG_TERM) is False
        service.add(S1, group=GROUP_SHORT_TERM)
        service.add(S2, group=GROUP_SHORT_TERM)
        assert service.remove(S1, group=GROUP_LONG_TERM) is True and service.count() == 2
        assert service.get(S1, GROUP_LONG_TERM) is None
        assert service.remove(S1) is True and service.remove(S1) is False and service.count() == 1

        # 3) 校验：格式/分组大小写敏感/非法分组
        for bad_symbol in ("600519", "60051.SH", "600519.XX", ""):
            try:
                service.add(bad_symbol)
            except WatchlistError as exc:
                assert "格式非法" in str(exc)
            else:
                raise AssertionError(f"非法代码未被拦截：{bad_symbol!r}")
        try:
            service.add(S1, group="LONG_TERM")  # 契约：大小写敏感
        except WatchlistError as exc:
            assert "分组" in str(exc)
        else:
            raise AssertionError("大小写错误的分组未被拦截")
        for group_id, (label, _sort) in GROUP_NAMES.items():
            assert normalize_group(group_id) == group_id and normalize_group(label) == group_id

        # 4) 批量导入：去重 / 错误清单 / ST 提示 / source
        service.clear()
        report = service.add_many(
            [{"symbol": S1}, {"symbol": S1}, {"symbol": "abc"}, S2], group=GROUP_SHORT_TERM
        )
        assert report["added"] == 2 and report["updated"] == 1 and len(report["errors"]) == 1, report
        assert {i.source for i in service.list_items()} == {"IMPORT"}
        st_report = service.add_many([{"symbol": S3, "name": "*ST测试"}], group=GROUP_LONG_TERM)
        assert st_report["st_warnings"] == [S3]
        service.add(S3, group=GROUP_SHORT_TERM)
        replaced = service.add_many([{"symbol": "600519.SH"}], group=GROUP_SHORT_TERM, replace=True)
        assert replaced["added"] == 1
        assert [i.symbol for i in service.list_items(GROUP_SHORT_TERM)] == ["600519.SH"]

        # 5) JSON 导入/导出（契约键集合）
        payload_file = tmp_dir / "picks.json"
        payload_file.write_text(
            json.dumps(
                {
                    "long_term": ["600519.SH", {"symbol": "000858.SZ"}],
                    "short_term": [{"code": "300750.SZ", "note": "波段"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        imported = service.import_json(payload_file)
        assert imported["added"] == 3 and not imported["errors"], imported
        export_path = tmp_dir / "export" / "watchlist.json"
        exported = service.export_json(export_path)
        payload = json.loads(export_path.read_text(encoding="utf-8"))
        assert payload == exported and payload
        keys = set(payload[0])
        assert {"symbol", "name", "group", "group_name", "note", "source"} <= keys, keys
        group_row = next(row for row in payload if row["symbol"] == "300750.SZ")
        assert group_row["group"] == GROUP_SHORT_TERM and group_row["group_name"] == "近期"
        for bad_source in (tmp_dir / "missing.json",):
            try:
                service.import_json(bad_source)
            except WatchlistError as exc:
                assert "不存在" in str(exc)
            else:
                raise AssertionError("缺失 JSON 文件未报中文错误")

        # 6) 配置自选池初始化（幂等）
        service.clear()
        cfg.data.universe = ["600519.SH", "000001.SZ", "300750.SZ"]
        initialized = service.init_from_universe(cfg)
        assert initialized == 3 and service.count() == 3
        assert service.init_from_universe(cfg) == 0

        # 7) 注入 session_factory 时不触碰全局 database（惰性化）
        import core.db as core_db

        assert core_db._database is None
        memory_db = Database(url="sqlite://", config=cfg)
        memory_db.initialize()
        injected = WatchlistService(config=cfg, session_factory=memory_db.session_factory)
        assert injected.database is None
        assert injected.count() == 0
        injected.add("600519.SH")
        assert injected.count() == 1
        assert core_db._database is None
        memory_db.dispose()

        database.dispose()
        print(
            "data/watchlist.py 自检通过：SPEC §5.4 规范表/多分组(同标的跨组)/中文 group_name/"
            "upsert 幂等与 created_at 保持/set_group·remove(单组·全部)/格式与大小写校验/"
            "批量导入去重·错误·ST 提示·source/JSON 导入导出契约键/配置初始化/惰性化"
        )
    finally:
        reset_config()
        shutil.rmtree(tmp_dir, ignore_errors=True)

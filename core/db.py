"""QuantDesk 数据库引擎、会话、建表迁移、备份与自检（任务 M0-04 / 契约 CF-7）。

能力清单
--------
1. **引擎与会话工厂**：SQLite（WAL 模式、外键开启、busy_timeout），``check_same_thread=False``
   以适配 Streamlit 多线程；支持 ``sqlite://`` 内存库（测试用 StaticPool）。
2. **幂等建表 + 版本表**：``Base.metadata.create_all`` 幂等；``schema_version`` 表记录结构版本，
   提供轻量迁移位（后续结构变更只需在 :data:`MIGRATIONS` 登记迁移函数）。
3. **在线备份与保留策略**：使用 SQLite 在线备份 API 生成 ``runtime/backups/quantdesk-YYYYMMDD-HHMMSS.db``，
   按 ``database.backup_keep``（默认 7）自动清理旧备份（SPEC §11.4）。
4. **启动自检**：数据库可读写、22 张表齐全、``PRAGMA integrity_check`` 完整性、结构版本；
   损坏时返回中文可操作提示（指引从最近备份恢复），**不抛出未处理异常**（SPEC §11.5）。
5. **与日志审计接线**：:func:`init_db` 会把会话工厂注入 ``core/logging.write_audit``，
   使审计写入无需业务模块传 session（``set_audit_session_factory``）。

约定
----
- 数据库文件路径按 ``config.database.url`` 解析，相对路径以**配置文件所在目录**为基准
  （例如默认 ``sqlite:///./runtime/quantdesk.db`` → ``quantdesk/runtime/quantdesk.db``）。
- 模块级提供默认数据库单例（``get_engine`` / ``get_session_factory`` / ``SessionLocal`` /
  ``session_scope`` / ``init_db``）；测试可用 :class:`Database` 独立实例（互不影响）。
- 本模块只依赖标准库 + SQLAlchemy + ``core/models.py`` / ``core/config.py`` / ``core/logging.py``。

自检（quantdesk 目录）：``python -m core.db``
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import AppConfig, get_config
from .logging import get_logger, set_audit_session_factory
from .models import ALL_TABLE_NAMES, EXTENSION_TABLE_NAMES, TABLE_NAMES, Base, now_local_naive

__all__ = [
    "SCHEMA_VERSION",
    "SCHEMA_VERSION_TABLE",
    "MIGRATIONS",
    "DatabaseError",
    "DBHealth",
    "Database",
    "get_database",
    "reset_database",
    "get_engine",
    "get_session_factory",
    "SessionLocal",
    "get_session",
    "session_scope",
    "init_db",
    "create_all",
    "startup_check",
    "backup_database",
    "dispose",
]

logger = get_logger("core.db")

#: 当前数据库结构版本（结构变更时 +1，并在 MIGRATIONS 登记迁移函数）
#: v2：Phase 2 扩展表（SPEC §5.4 5 张）+ 全市场快照表 + 自选池结构迁移
SCHEMA_VERSION = 2

#: 结构版本表名（不属于业务表，由本模块维护）
SCHEMA_VERSION_TABLE = "schema_version"


def _migrate_v2_phase2_tables(conn: Any) -> None:
    """schema v1 → v2 迁移：新增扩展表并迁移早期独立版 ``watchlist_items`` 结构。

    步骤：
    1. 若 ``watchlist_items`` 是早期 Dev1 独立表（``symbol`` 主键、含 ``group_name``、
       无 ``id``/``group_id``），先重命名为 ``watchlist_items_legacy_v1`` 占位；
    2. ``Base.metadata.create_all`` 建出全部缺失扩展表（含 canonical 版 ``watchlist_items``、
       ``watchlist_groups``、``recommend_*``、``research_runs``、``market_snapshot``）；
    3. 补齐默认分组（全部/远期/近期/手动）；
    4. 把 legacy 自选池数据迁移到新结构并删除旧表。
    """
    inspector = inspect(conn)
    tables = set(inspector.get_table_names())
    legacy_renamed = False
    if "watchlist_items" in tables:
        columns = {column["name"] for column in inspector.get_columns("watchlist_items")}
        if "group_id" not in columns or "id" not in columns:
            conn.execute(text("ALTER TABLE watchlist_items RENAME TO watchlist_items_legacy_v1"))
            legacy_renamed = True
            logger.info("检测到早期独立自选池表，已重命名为 watchlist_items_legacy_v1 并准备迁移")

    Base.metadata.create_all(bind=conn)  # 创建全部缺失表（含扩展表）
    now = now_local_naive().isoformat(sep=" ")
    for group_id, group_name, sort_order in (
        ("default", "全部", 0),
        ("long_term", "远期", 1),
        ("short_term", "近期", 2),
        ("manual", "手动", 3),
    ):
        conn.execute(
            text(
                "INSERT OR IGNORE INTO watchlist_groups "
                "(id, name, description, sort_order, created_at, updated_at) "
                "VALUES (:id, :name, '', :sort_order, :now, :now)"
            ),
            {"id": group_id, "name": group_name, "sort_order": sort_order, "now": now},
        )

    if legacy_renamed and "watchlist_items_legacy_v1" in set(inspect(conn).get_table_names()):
        conn.execute(
            text(
                "INSERT OR IGNORE INTO watchlist_items "
                "(group_id, symbol, note, tags_json, source, is_active, sort_order, created_at, updated_at) "
                "SELECT COALESCE(NULLIF(group_name, ''), 'default'), symbol, COALESCE(note, ''), "
                "'[]', 'IMPORT', 1, 0, COALESCE(added_at, :now), COALESCE(updated_at, :now) "
                "FROM watchlist_items_legacy_v1 WHERE symbol IS NOT NULL"
            ),
            {"now": now},
        )
        conn.execute(text("DROP TABLE watchlist_items_legacy_v1"))
        logger.info("自选池历史数据已迁移到 Phase 2 结构（watchlist_groups / watchlist_items）")


#: 轻量迁移登记表：目标版本 → (中文说明, 迁移函数(conn) -> None)
MIGRATIONS: dict[int, tuple[str, Callable[[Any], None]]] = {
    2: (
        "Phase 2 扩展表：SPEC §5.4 五张表 + 全市场快照表 + 自选池结构迁移",
        _migrate_v2_phase2_tables,
    )
}

#: SQLite 默认连接参数（check_same_thread 适配 Streamlit 多线程）
_SQLITE_CONNECT_ARGS: dict[str, Any] = {"check_same_thread": False, "timeout": 30.0}


class DatabaseError(RuntimeError):
    """数据库操作错误（中文可操作提示）。"""


# ---------------------------------------------------------------------------
# SQLite PRAGMA（WAL / 外键 / 超时）
# ---------------------------------------------------------------------------


@event.listens_for(Engine, "connect")
def _configure_sqlite_connection(dbapi_connection: Any, connection_record: Any) -> None:
    """每个 SQLite 连接建立时设置 PRAGMA（WAL、外键、忙等待、同步级别）。"""
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")       # 读写并发（SPEC §5.1）
        cursor.execute("PRAGMA foreign_keys=ON")        # 外键约束（SPEC §5.1）
        cursor.execute("PRAGMA synchronous=NORMAL")     # WAL 下兼顾安全与性能
        cursor.execute("PRAGMA busy_timeout=15000")     # 并发写等待 15 秒
    except sqlite3.Error:  # pragma: no cover - 极端只读环境
        pass
    finally:
        cursor.close()


# ---------------------------------------------------------------------------
# 自检报告
# ---------------------------------------------------------------------------


@dataclass
class DBHealth:
    """数据库启动自检结果（供启动脚本/设置页展示）。"""

    ok: bool
    url: str
    db_path: str
    integrity_ok: bool
    writable: bool
    schema_ok: bool
    schema_version: int | None
    tables: list[str] = field(default_factory=list)
    missing_tables: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    message: str = ""
    checked_at: datetime = field(default_factory=now_local_naive)

    def to_dict(self) -> dict[str, Any]:
        """转为可 JSON 序列化的字典（UI/日志用）。"""
        return {
            "ok": self.ok,
            "url": self.url,
            "db_path": self.db_path,
            "integrity_ok": self.integrity_ok,
            "writable": self.writable,
            "schema_ok": self.schema_ok,
            "schema_version": self.schema_version,
            "tables": list(self.tables),
            "missing_tables": list(self.missing_tables),
            "errors": list(self.errors),
            "message": self.message,
            "checked_at": self.checked_at.isoformat(),
        }


# ---------------------------------------------------------------------------
# URL / 路径工具
# ---------------------------------------------------------------------------


def _is_sqlite_url(url: str) -> bool:
    return url.startswith("sqlite://")


def _is_memory_url(url: str) -> bool:
    """判断是否为内存 SQLite（``sqlite://`` 或 ``sqlite:///:memory:``）。"""
    return url in ("sqlite://", "sqlite:///:memory:", "sqlite://") or url.endswith(":memory:")


def resolve_sqlite_path(url: str, base_dir: Path | None = None) -> Path | None:
    """把 ``sqlite:///相对路径`` 解析为绝对文件路径；内存库返回 ``None``。"""
    if not url.startswith("sqlite:///"):
        return None
    tail = url[len("sqlite:///"):]
    if not tail or tail == ":memory:" or tail.startswith("file:"):
        return None
    path = Path(tail)
    if path.is_absolute():
        return path
    return ((base_dir or Path.cwd()) / path).resolve()


def _normalize_url(url: str, base_dir: Path) -> str:
    """把相对 SQLite 路径转成绝对路径 URL，避免依赖进程工作目录。"""
    if not url.startswith("sqlite:///"):
        return url
    tail = url[len("sqlite:///"):]
    if not tail or tail == ":memory:" or tail.startswith("file:"):
        return url
    path = Path(tail)
    if path.is_absolute():
        return url
    absolute = (base_dir / path).resolve()
    return f"sqlite:///{absolute.as_posix()}"


# ---------------------------------------------------------------------------
# 数据库管理器
# ---------------------------------------------------------------------------


class Database:
    """单个 SQLite 数据库的引擎/会话/建表/备份/自检封装。

    默认使用全局配置（``core.config.get_config()``）；测试可传入独立 url/config，互不干扰。
    """

    def __init__(
        self,
        url: str | None = None,
        echo: bool | None = None,
        *,
        config: AppConfig | None = None,
    ) -> None:
        self.config: AppConfig = config or get_config()
        self.url: str = url or self.config.database.url
        self.echo: bool = self.config.database.echo if echo is None else bool(echo)
        self._lock = threading.RLock()
        self._engine: Engine | None = None
        self._session_factory: sessionmaker[Session] | None = None
        self._normalized_url = _normalize_url(self.url, self.config.base_dir)
        self._sqlite_path = resolve_sqlite_path(self._normalized_url)
        self._memory = _is_memory_url(self._normalized_url)

        if not _is_sqlite_url(self._normalized_url):
            raise DatabaseError(
                f"数据库连接串不受支持：{self.url}。MVP 仅支持 SQLite（SPEC §5.1）"
            )

    # -- 引擎 / 会话 ------------------------------------------------------
    def _ensure_parent_dir(self) -> None:
        """确保 SQLite 文件所在目录存在（runtime/ 首次启动时创建）。"""
        if self._sqlite_path is not None:
            self._sqlite_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def engine(self) -> Engine:
        """SQLAlchemy 引擎（懒加载，首次访问时创建）。"""
        with self._lock:
            if self._engine is None:
                self._ensure_parent_dir()
                kwargs: dict[str, Any] = {"echo": self.echo, "future": True, "connect_args": dict(_SQLITE_CONNECT_ARGS)}
                if self._memory:
                    # 内存库：同一进程内共享连接，供多会话/测试使用
                    kwargs["poolclass"] = StaticPool
                self._engine = create_engine(self._normalized_url, **kwargs)
                logger.debug("数据库引擎已创建：%s", self._normalized_url)
            return self._engine

    @property
    def session_factory(self) -> sessionmaker[Session]:
        """会话工厂（``sessionmaker``，懒加载）。"""
        with self._lock:
            if self._session_factory is None:
                self._session_factory = sessionmaker(
                    bind=self.engine,
                    expire_on_commit=False,
                    autoflush=False,
                )
            return self._session_factory

    #: 兼容别名：``from core.db import SessionLocal`` 风格
    @property
    def SessionLocal(self) -> sessionmaker[Session]:
        """会话工厂兼容别名（与模块级 ``SessionLocal`` 用法一致）。"""
        return self.session_factory

    @contextmanager
    def session(self) -> Iterator[Session]:
        """事务上下文：正常结束提交，异常回滚，最终关闭会话。"""
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- 建表 / 版本 ------------------------------------------------------
    def create_all(self) -> None:
        """幂等建表（22 张核心表 + 扩展表，当前共 28 张；已存在则跳过）。"""
        Base.metadata.create_all(self.engine)
        logger.debug("业务表已确保存在：%d 张（核心 %d + 扩展 %d）", len(ALL_TABLE_NAMES), len(TABLE_NAMES), len(EXTENSION_TABLE_NAMES))

    def schema_version(self) -> int | None:
        """读取当前数据库结构版本；无版本表时返回 ``None``。"""
        inspector = inspect(self.engine)
        if SCHEMA_VERSION_TABLE not in inspector.get_table_names():
            return None
        with self.engine.connect() as conn:
            return conn.execute(text(f"SELECT MAX(version) FROM {SCHEMA_VERSION_TABLE}")).scalar()

    def _ensure_schema_version(self) -> None:
        """创建/升级结构版本表（轻量迁移位）。"""
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {SCHEMA_VERSION_TABLE} ("
                    "version INTEGER PRIMARY KEY, "
                    "applied_at DATETIME NOT NULL, "
                    "description TEXT NOT NULL DEFAULT ''"
                    ")"
                )
            )
            current = conn.execute(text(f"SELECT MAX(version) FROM {SCHEMA_VERSION_TABLE}")).scalar()
            if current is None:
                conn.execute(
                    text(
                        f"INSERT INTO {SCHEMA_VERSION_TABLE} (version, applied_at, description) "
                        "VALUES (:version, :applied_at, :description)"
                    ),
                    {
                        "version": SCHEMA_VERSION,
                        "applied_at": now_local_naive().isoformat(sep=" "),
                        "description": (
                            f"v{SCHEMA_VERSION} 初始化：{len(TABLE_NAMES)} 张核心表 + "
                            f"{len(EXTENSION_TABLE_NAMES)} 张扩展表（SPEC §5.2 / §5.4）"
                        ),
                    },
                )
                logger.info("数据库结构初始化完成：版本 v%s", SCHEMA_VERSION)
            elif current < SCHEMA_VERSION:
                for version in range(int(current) + 1, SCHEMA_VERSION + 1):
                    if version not in MIGRATIONS:
                        raise DatabaseError(
                            f"数据库结构版本 v{current} 需要升级到 v{SCHEMA_VERSION}，"
                            f"但缺少 v{version} 的迁移函数（请在 core/db.py 的 MIGRATIONS 登记）"
                        )
                    description, migrate = MIGRATIONS[version]
                    logger.info("执行数据库迁移 v%s：%s", version, description)
                    migrate(conn)
                    conn.execute(
                        text(
                            f"INSERT INTO {SCHEMA_VERSION_TABLE} (version, applied_at, description) "
                            "VALUES (:version, :applied_at, :description)"
                        ),
                        {
                            "version": version,
                            "applied_at": now_local_naive().isoformat(sep=" "),
                            "description": description,
                        },
                    )
            elif current > SCHEMA_VERSION:
                logger.warning(
                    "数据库结构版本 v%s 高于当前代码版本 v%s：请升级程序，避免结构不兼容",
                    current,
                    SCHEMA_VERSION,
                )

    def initialize(self, *, create_dirs: bool = True) -> DBHealth:
        """启动初始化：创建运行目录、建表、结构版本，然后返回自检报告。"""
        if create_dirs:
            self.ensure_data_dirs()
        self.create_all()
        self._ensure_schema_version()
        return self.startup_check()

    def ensure_data_dirs(self) -> None:
        """创建 runtime/ 下的数据、日志、备份、导出目录（幂等）。"""
        for path in (
            self.config.data_dir_path,
            self.config.log_dir,
            self.config.backup_dir,
            self.config.export_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    # -- 自检 -------------------------------------------------------------
    def table_names(self) -> list[str]:
        """返回数据库内已有表名（按名称排序）。"""
        return sorted(inspect(self.engine).get_table_names())

    def integrity_check(self) -> tuple[bool, str]:
        """执行 ``PRAGMA integrity_check``；返回（是否完好, 原始结果）。"""
        try:
            with self.engine.connect() as conn:
                result = conn.execute(text("PRAGMA integrity_check")).scalar()
            return str(result).lower() == "ok", str(result)
        except Exception as exc:  # 文件损坏时连接/查询可能直接抛错
            return False, f"完整性检查失败：{exc}"

    def _writable_probe(self) -> tuple[bool, str]:
        """用 ``BEGIN IMMEDIATE`` 尝试获取写锁后回滚，验证数据库可写（不改变任何数据）。"""
        raw = None
        cursor = None
        try:
            raw = self.engine.raw_connection()
            cursor = raw.cursor()
            cursor.execute("BEGIN IMMEDIATE")  # 只读库/文件被占用会立即失败
            cursor.execute("ROLLBACK")
            return True, ""
        except Exception as exc:
            return False, f"数据库不可写（请检查文件权限/磁盘空间/是否被占用）：{exc}"
        finally:
            try:
                if cursor is not None:
                    cursor.close()
                if raw is not None:
                    raw.close()
            except Exception:  # pragma: no cover
                pass

    def startup_check(self) -> DBHealth:
        """启动自检：完整性、可读写、全部表齐全（22 核心 + 扩展）、结构版本（不抛异常）。"""
        errors: list[str] = []
        integrity_ok, integrity_detail = self.integrity_check()
        if not integrity_ok:
            errors.append(integrity_detail)
        writable, writable_detail = self._writable_probe()
        if not writable:
            errors.append(writable_detail)

        tables: list[str] = []
        try:
            tables = self.table_names()
        except Exception as exc:
            errors.append(f"读取表清单失败（数据库可能损坏）：{exc}")
        missing = [name for name in ALL_TABLE_NAMES if name not in tables]
        version: int | None = None
        if SCHEMA_VERSION_TABLE in tables:
            try:
                version = self.schema_version()
            except Exception as exc:
                errors.append(f"读取结构版本失败：{exc}")

        schema_ok = not missing and version == SCHEMA_VERSION
        ok = integrity_ok and writable and schema_ok

        if ok:
            extension_tables = [name for name in EXTENSION_TABLE_NAMES if name in tables]
            message = (
                f"数据库自检通过：核心业务表 {len(TABLE_NAMES)} 张 + 扩展表 "
                f"{len(extension_tables)} 张齐全，结构版本 v{version}，"
                f"WAL 模式，读写正常（{self._sqlite_path or '内存数据库'}）"
            )
        else:
            hints: list[str] = []
            if not integrity_ok:
                hints.append(
                    f"数据库文件可能已损坏：请从最近备份恢复（目录 {self.config.backup_dir}，"
                    "设置页可执行恢复；恢复前会自动备份当前库）"
                )
            if not writable:
                hints.append("数据库不可写：请检查文件是否被其它程序占用、磁盘是否已满")
            if missing:
                hints.append(f"缺少 {len(missing)} 张表：请运行 scripts/init_db.py 或 core.db.init_db() 建表")
            if version is not None and version != SCHEMA_VERSION:
                hints.append(f"结构版本不一致（数据库 v{version} / 代码 v{SCHEMA_VERSION}）")
            message = "数据库自检未通过：" + "；".join(hints or errors or ["未知错误"])
            logger.error("数据库自检未通过：%s", message)

        return DBHealth(
            ok=ok,
            url=self._normalized_url,
            db_path=str(self._sqlite_path) if self._sqlite_path else ":memory:",
            integrity_ok=integrity_ok,
            writable=writable,
            schema_ok=schema_ok,
            schema_version=version,
            tables=tables,
            missing_tables=missing,
            errors=errors,
            message=message,
        )

    # -- 备份 -------------------------------------------------------------
    def backup(self, dest_dir: str | Path | None = None, keep: int | None = None) -> Path:
        """使用 SQLite 在线备份 API 备份数据库，并清理超出保留份数的旧备份。

        :param dest_dir: 备份目录；默认 ``<data_dir>/backups``
        :param keep: 保留份数；默认 ``database.backup_keep``
        :return: 新备份文件路径
        :raises DatabaseError: 内存库/源文件不存在/备份失败
        """
        if self._sqlite_path is None:
            raise DatabaseError("当前为内存数据库，无法备份（请配置 sqlite:/// 文件数据库）")
        source = self._sqlite_path
        if not source.exists():
            raise DatabaseError(f"数据库文件不存在，无法备份：{source}（请先运行 init_db() 建库）")
        target_dir = Path(dest_dir) if dest_dir is not None else self.config.backup_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        keep_count = int(keep if keep is not None else self.config.database.backup_keep)
        stamp = now_local_naive().strftime("%Y%m%d-%H%M%S")
        target = target_dir / f"quantdesk-{stamp}.db"
        counter = 1
        while target.exists():
            target = target_dir / f"quantdesk-{stamp}-{counter}.db"
            counter += 1
        try:
            # SQLite 在线备份 API：WAL 模式下也能得到一致快照
            src_conn = sqlite3.connect(str(source))
            dst_conn = sqlite3.connect(str(target))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
                src_conn.close()
        except sqlite3.Error as exc:
            raise DatabaseError(f"数据库备份失败：{source} → {target}（{exc}）") from exc
        removed = self.cleanup_backups(keep_count, dest_dir=target_dir)
        logger.info("数据库备份完成：%s（清理旧备份 %s 份）", target, removed)
        return target

    def list_backups(self, dest_dir: str | Path | None = None) -> list[Path]:
        """列出备份目录中的备份文件（按时间从旧到新）。"""
        target_dir = Path(dest_dir) if dest_dir is not None else self.config.backup_dir
        if not target_dir.exists():
            return []
        return sorted(target_dir.glob("quantdesk-*.db"), key=lambda p: (p.stat().st_mtime, p.name))

    def cleanup_backups(self, keep: int | None = None, dest_dir: str | Path | None = None) -> int:
        """清理超出保留份数的旧备份，返回删除数量（保留最近 ``keep`` 份）。"""
        keep_count = max(int(keep if keep is not None else self.config.database.backup_keep), 1)
        backups = self.list_backups(dest_dir)
        removed = 0
        for path in backups[:-keep_count] if len(backups) > keep_count else []:
            try:
                path.unlink()
                removed += 1
            except OSError as exc:  # pragma: no cover - 文件被占用
                logger.warning("旧备份清理失败：%s（%s）", path, exc)
        return removed

    # -- 生命周期 ---------------------------------------------------------
    def dispose(self) -> None:
        """释放引擎连接池（测试/退出时调用）。"""
        with self._lock:
            if self._engine is not None:
                self._engine.dispose()
                self._engine = None
                self._session_factory = None


# ---------------------------------------------------------------------------
# 模块级默认数据库（单例）
# ---------------------------------------------------------------------------

_database: Database | None = None
_database_lock = threading.RLock()


def get_database(config: AppConfig | None = None, *, refresh: bool = False) -> Database:
    """获取默认数据库单例（首次调用时按配置创建）。"""
    global _database
    with _database_lock:
        if _database is None or refresh:
            _database = Database(config=config)
        return _database


def reset_database() -> None:
    """关闭并清空默认数据库单例（测试隔离用，不删除数据库文件）。"""
    global _database
    with _database_lock:
        if _database is not None:
            _database.dispose()
        _database = None
    set_audit_session_factory(None)


def get_engine() -> Engine:
    """默认数据库引擎。"""
    return get_database().engine


def get_session_factory() -> sessionmaker[Session]:
    """默认数据库会话工厂（``core/logging.write_audit`` 依赖此接口）。"""
    return get_database().session_factory


class _LazySessionLocal:
    """延迟绑定默认会话工厂的代理，兼容 ``SessionLocal()`` 与 ``sessionmaker`` 属性访问。"""

    def __call__(self, **kwargs: Any) -> Session:
        return get_session_factory()(**kwargs)

    def __getattr__(self, item: str) -> Any:
        return getattr(get_session_factory(), item)

    def __repr__(self) -> str:  # pragma: no cover - 调试展示
        return "<SessionLocal proxy: core.db.get_session_factory()>"


#: 兼容别名：``from core.db import SessionLocal`` 后可直接调用 ``SessionLocal()``
SessionLocal = _LazySessionLocal()


def get_session() -> Session:
    """创建默认数据库会话（调用方负责 close，或用 :func:`session_scope`）。"""
    return get_session_factory()()


@contextmanager
def session_scope() -> Iterator[Session]:
    """默认数据库事务上下文：正常提交、异常回滚、最终关闭。"""
    with get_database().session() as session:
        yield session


def init_db(config: AppConfig | None = None) -> DBHealth:
    """初始化默认数据库：建运行目录、建表、结构版本、注入审计会话工厂，并返回自检报告。"""
    database = get_database(config=config, refresh=config is not None)
    health = database.initialize()
    set_audit_session_factory(database.session_factory)
    if health.ok:
        logger.info("数据库初始化完成：%s", health.message)
    else:
        logger.error("数据库初始化后自检未通过：%s", health.message)
    return health


def create_all() -> None:
    """对默认数据库执行幂等建表。"""
    get_database().create_all()


def startup_check() -> DBHealth:
    """对默认数据库执行启动自检。"""
    return get_database().startup_check()


def backup_database(dest_dir: str | Path | None = None, keep: int | None = None) -> Path:
    """备份默认数据库（目录/份数默认取配置）。"""
    return get_database().backup(dest_dir=dest_dir, keep=keep)


def dispose() -> None:
    """释放默认数据库引擎并解绑审计会话工厂。"""
    reset_database()


# ---------------------------------------------------------------------------
# 自检（quantdesk 目录：``python -m core.db``）
# ---------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    import shutil
    import tempfile

    from sqlalchemy import select

    from .config import DEFAULT_CONFIG_PATH, load_config, reset_config, set_config
    from .logging import AuditAction, write_audit
    from .models import ALL_TABLE_NAMES, EXTENSION_TABLE_NAMES, TABLE_NAMES, AccountORM, AuditLogORM, OrderORM

    tmp_dir = Path(tempfile.mkdtemp(prefix="qd_db_"))
    stmt = Path(tempfile.mkdtemp(prefix="qd_cfg_")) / "config.yaml"
    # 用项目 config.yaml 生成临时配置：数据目录/数据库都指向临时目录
    import yaml

    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    raw["app"]["data_dir"] = str(tmp_dir / "runtime")
    raw["database"]["url"] = f"sqlite:///{(tmp_dir / 'runtime' / 'quantdesk.db').as_posix()}"
    raw["database"]["backup_keep"] = 3
    stmt.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    cfg = load_config(stmt, environ={})
    # 覆盖单例，避免污染项目 runtime/
    set_config(cfg)
    database: Database | None = None
    try:
        # 1) 初始化 + 自检
        database = Database(config=cfg)
        health = database.initialize()
        assert health.ok, health.message
        assert health.schema_version == SCHEMA_VERSION
        assert len(health.missing_tables) == 0
        assert len(TABLE_NAMES) == 22, "核心表必须保持 22 张（CF-7 冻结）"
        assert len(EXTENSION_TABLE_NAMES) == 6
        assert len(ALL_TABLE_NAMES) == 28
        assert set(ALL_TABLE_NAMES) <= set(database.table_names())
        assert cfg.log_dir.exists() and cfg.backup_dir.exists() and cfg.export_dir.exists()

        # 2) WAL / 外键 PRAGMA 生效
        with database.engine.connect() as conn:
            assert str(conn.execute(text("PRAGMA journal_mode")).scalar()).lower() == "wal"
            assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1

        # 3) 幂等初始化 / 结构版本表
        assert database.initialize().ok is True
        assert database.schema_version() == SCHEMA_VERSION
        with database.engine.connect() as conn:
            rows = conn.execute(text(f"SELECT version, description FROM {SCHEMA_VERSION_TABLE}")).all()
            assert len(rows) == 1 and rows[0][0] == SCHEMA_VERSION and "初始化" in rows[0][1]

        # 4) 会话读写 + 事务回滚
        with database.session() as session:
            session.add(AccountORM(id="acc-1", name="模拟账户"))
            session.add(OrderORM(id="ord-1", account_id="acc-1", symbol="600519.SH", side="BUY", quantity=100))
        with database.session() as session:
            acc = session.scalars(select(AccountORM).where(AccountORM.id == "acc-1")).one()
            assert acc.name == "模拟账户"
            assert session.scalars(select(OrderORM)).one().quantity == 100
        try:
            with database.session() as session:
                session.add(AccountORM(id="acc-rollback", name="回滚测试"))
                raise RuntimeError("模拟业务异常")
        except RuntimeError:
            pass
        with database.session() as session:
            assert session.scalars(select(AccountORM).where(AccountORM.id == "acc-rollback")).first() is None

        # 5) 会话工厂别名 / 内存库
        session = database.SessionLocal()
        session.close()
        mem_db = Database(url="sqlite://", config=cfg)
        assert mem_db.initialize().ok is True
        assert mem_db.startup_check().db_path == ":memory:"
        mem_db.dispose()

        # 5.5) schema v1 → v2 迁移：早期独立 watchlist_items 结构 → Phase 2 结构
        import sqlite3 as _sqlite3

        from .models import WatchlistGroupORM, WatchlistItemORM

        legacy_path = tmp_dir / "legacy_v1.db"
        legacy_conn = _sqlite3.connect(str(legacy_path))
        legacy_conn.executescript(
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, "
            "applied_at DATETIME NOT NULL, description TEXT NOT NULL DEFAULT '');"
            "INSERT INTO schema_version VALUES (1, '2026-10-01 00:00:00', 'v1 初始化');"
            "CREATE TABLE watchlist_items (symbol TEXT PRIMARY KEY, name TEXT, group_name TEXT, "
            "note TEXT, added_at DATETIME, updated_at DATETIME);"
            "INSERT INTO watchlist_items VALUES "
            "('600519.SH', '贵州茅台', 'long_term', '核心资产', "
            "'2026-10-01 00:00:00', '2026-10-01 00:00:00');"
        )
        legacy_conn.commit()
        legacy_conn.close()
        legacy_db = Database(url=f"sqlite:///{legacy_path.as_posix()}", config=cfg)
        legacy_health = legacy_db.initialize()
        assert legacy_health.ok, legacy_health.message
        assert legacy_health.schema_version == SCHEMA_VERSION, legacy_health.schema_version
        with legacy_db.session() as session:
            migrated = session.scalars(select(WatchlistItemORM)).all()
            assert len(migrated) == 1 and migrated[0].symbol == "600519.SH", migrated
            assert migrated[0].group_id == "long_term", migrated[0].group_id
            group_ids = set(session.scalars(select(WatchlistGroupORM.id)).all())
            assert {"default", "long_term", "short_term", "manual"} <= group_ids, group_ids
        legacy_db.dispose()

        # 6) 在线备份 + 保留策略
        backup1 = database.backup(keep=2)
        assert backup1.exists() and backup1.suffix == ".db" and backup1.stat().st_size > 0
        for index in range(3):
            (cfg.backup_dir / f"quantdesk-20200101-00000{index}.db").write_bytes(b"dummy")
        removed = database.cleanup_backups(keep=2, dest_dir=cfg.backup_dir)
        assert removed == 2, removed  # 4 份 → 保留最近 2 份 → 删除 2 份
        assert len(database.list_backups(cfg.backup_dir)) == 2
        assert database.backup(keep=2).exists()

        # 7) 损坏库检测：不抛异常，给出中文提示
        corrupt_path = tmp_dir / "corrupt.db"
        corrupt_path.write_bytes(b"this is not a sqlite database")
        corrupt_db = Database(url=f"sqlite:///{corrupt_path.as_posix()}", config=cfg)
        corrupt_health = corrupt_db.startup_check()
        assert corrupt_health.ok is False and corrupt_health.integrity_ok is False
        assert "损坏" in corrupt_health.message or "自检未通过" in corrupt_health.message
        corrupt_db.dispose()

        # 8) 模块级默认库 + 审计接线（write_audit 自动使用注入的会话工厂）
        reset_database()
        module_health = init_db()
        assert module_health.ok, module_health.message
        with session_scope() as session:
            session.add(AccountORM(id="acc-auto", name="默认库账户"))
        assert write_audit(AuditAction.SETTINGS_UPDATE, ("app_settings", "theme"), {}, {"theme": "dark"}) is True
        with session_scope() as session:
            audit_row = session.scalars(select(AuditLogORM)).one()
            assert audit_row.action == AuditAction.SETTINGS_UPDATE and audit_row.target_id == "theme"
            assert session.scalars(select(AccountORM).where(AccountORM.id == "acc-auto")).one() is not None
        assert isinstance(get_engine(), Engine)
        probe_session = get_session_factory()()
        assert probe_session is not None
        probe_session.close()
        lazy_session = SessionLocal()
        assert lazy_session is not None
        lazy_session.close()
        dispose()

        print(
            "core/db.py 自检通过：WAL/外键/幂等建表(22 核心+6 扩展=28)+schema v2 迁移/"
            "会话提交回滚/内存库/在线备份与保留策略/损坏检测中文提示/默认单例+审计接线"
        )
    finally:
        reset_database()
        reset_config()
        if database is not None:
            database.dispose()
        # 清理临时目录（本机 tempfile 根目录可能指向工作目录，避免污染项目）
        shutil.rmtree(tmp_dir, ignore_errors=True)
        shutil.rmtree(stmt.parent, ignore_errors=True)

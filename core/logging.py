"""QuantDesk 结构化日志与审计流水（契约冻结项 CF-11，SPEC §8.8 / §11.3）。

能力清单
--------
1. **结构化日志**：JSON Lines 落到 ``runtime/logs/app-YYYYMMDD.log``（按日切分，默认保留 30 天），
   每条固定字段 ``ts / level / logger / trace_id / module / message / extra``（SPEC §8.8-1 冻结）。
2. **控制台人读格式**：``时间 | 级别 | logger | trace=xxx | 消息``，便于用户直接看懂。
3. **trace_id 贯穿**：用 ``contextvars`` 保存当前调用链 ID（context manager / set / get），
   日志、事件总线（``core/events.py``）与审计都读取同一 trace_id。
4. **审计落库**：``write_audit(action, target, before, after, actor, trace_id)`` 写 ``audit_logs`` 表
   （不可删改；保留 1 年）。数据库会话通过 ``set_audit_session_factory`` 注入，或延迟使用 ``core/db.py``
   提供的会话工厂（接口约定见 :func:`write_audit`）。
5. **敏感信息过滤**：日志消息、``extra``、审计 before/after 中的密码/token/密钥等一律替换为 ``***``，
   绝不落盘（SPEC §7 规则 1 / §11.6-2）。

设计说明
--------
- 采用「配置 root logger」策略：应用启动调用一次 :func:`setup_logging`，此后所有模块
  （含第三方库，如 streamlit/APScheduler）的日志都会按统一格式落盘，避免日志遗漏。
- 按日切分用自定义 Handler（而不是 ``TimedRotatingFileHandler``），以满足 SPEC 要求的
  ``app-YYYYMMDD.log`` 文件名，并在切日时自动清理超过保留天数的旧文件。
- 本模块只依赖标准库 + ``core/models.py``（枚举/时区），不 import 任何业务模块。
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import date, datetime, timedelta
from logging import LogRecord
from pathlib import Path
from typing import Any

from .models import (
    LOCAL_TZ,
    AuditActor,
    AuditLogORM,
    AuditResult,
    now_local_naive,
)

__all__ = [
    # 初始化与获取
    "setup_logging",
    "reset_logging",
    "get_logger",
    "get_log_dir",
    # trace_id
    "new_trace_id",
    "get_trace_id",
    "set_trace_id",
    "clear_trace_id",
    "trace_context",
    # 格式化与脱敏
    "JsonLineFormatter",
    "ConsoleFormatter",
    "DailyJsonFileHandler",
    "mask_sensitive",
    "mask_text",
    # 审计
    "AuditAction",
    "write_audit",
    "set_audit_session_factory",
    "get_audit_session_factory",
    # 常量
    "LOG_FIELDS",
    "DEFAULT_LOG_DIR",
    "DEFAULT_RETENTION_DAYS",
]

logger = logging.getLogger(__name__)

#: SPEC §8.8-1 冻结的日志字段
LOG_FIELDS: tuple[str, ...] = ("ts", "level", "logger", "trace_id", "module", "message", "extra")

#: 默认日志目录（相对工作目录；实际以 ``app.data_dir`` 为准）
DEFAULT_LOG_DIR = Path("./runtime/logs")

#: 默认日志保留天数（SPEC：日志 30 天滚动）
DEFAULT_RETENTION_DAYS = 30

#: 日志文件前缀（app-YYYYMMDD.log）
LOG_FILE_PREFIX = "app"

#: 由本模块管理的 handler 标记（用于重复初始化时先移除旧 handler）
_HANDLER_MARK = "_quantdesk_managed"

#: 受管理的日志器名称（应用统一日志器前缀）
_APP_LOGGER_NAME = "quantdesk"


# ---------------------------------------------------------------------------
# trace_id（contextvars，一次调用链贯穿日志/事件/审计）
# ---------------------------------------------------------------------------

_trace_id_var: ContextVar[str | None] = ContextVar("quantdesk_trace_id", default=None)


def new_trace_id() -> str:
    """生成新的 trace_id（16 位十六进制）。"""
    return uuid.uuid4().hex[:16]


def get_trace_id() -> str | None:
    """读取当前上下文的 trace_id；未设置返回 ``None``。"""
    return _trace_id_var.get()


def set_trace_id(trace_id: str | None) -> Token[str | None]:
    """设置当前上下文 trace_id，返回可用于恢复的 token。"""
    return _trace_id_var.set(trace_id)


def clear_trace_id() -> None:
    """清空当前上下文 trace_id（测试/线程结束时调用）。"""
    _trace_id_var.set(None)


@contextmanager
def trace_context(trace_id: str | None = None) -> Iterator[str]:
    """trace_id 上下文管理器：进入时设置，退出时自动恢复。

    :param trace_id: 指定 trace_id；为空则自动生成
    :return: 本次上下文的 trace_id

    用法::

        with trace_context() as tid:
            logger.info("开始处理", extra={"job": "scan"})
            bus.publish(Topic.JOB_STARTED, {"job_key": "scan"}, trace_id=tid)
    """
    tid = trace_id or new_trace_id()
    token = _trace_id_var.set(tid)
    try:
        yield tid
    finally:
        _trace_id_var.reset(token)


# ---------------------------------------------------------------------------
# 敏感信息过滤（密码/token/密钥绝不落盘）
# ---------------------------------------------------------------------------

#: 敏感字段名关键字（小写包含匹配；命中即整体替换为 ***）
_SENSITIVE_KEYWORDS: tuple[str, ...] = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "api-key",
    "access_key",
    "access-key",
    "private_key",
    "private-key",
    "authorization",
    "credential",
    "cookie",
    "smtp_password",
)

#: 明文中的敏感键值对（password=xxx、token: xxx、"password": "xxx"）
_SECRET_TEXT_RE = re.compile(
    r"(?i)([\"']?(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key"
    r"|private[_-]?key|authorization|credential|cookie)[\"']?\s*[=:]\s*)"
    r"([\"']?)([^\"'\s,;&}]+)",
)

#: 认证头（含 Authorization 标签 + Bearer/Basic/Digest 凭据；必须整段抹掉）
_AUTHORIZATION_HEADER_RE = re.compile(
    r"(?i)(\b(?:authorization|proxy-authorization)['\"]?\s*[=:]\s*)"
    r"(?:(?:bearer|basic|digest|token)\s+)?[A-Za-z0-9\-._~+/=]{4,}"
)

#: 裸 Bearer 头（无 Authorization 标签时也要掩掉）
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9\-._~+/=]+")

#: 掩码占位符
MASK = "***"


def _is_sensitive_key(key: str) -> bool:
    """字段名是否命中敏感关键字（``smtp_password``、``access_token`` 均命中）。"""
    if not isinstance(key, str) or not key:
        return False
    lowered = key.strip().lower()
    return any(keyword in lowered for keyword in _SENSITIVE_KEYWORDS)


def mask_text(text: str) -> str:
    """对字符串中的敏感内容做掩码（``password=xxx``、``Authorization: Bearer xxx`` 等）。

    注意执行顺序（缺陷 D-01 修复）：
    1. 先整段掩掉 ``Authorization: Bearer/Basic <凭据>`` 认证头；
    2. 再掩裸 ``Bearer <凭据>``；
    3. 最后处理 ``key=value`` / ``"key": "value"`` 形式。
    若先跑第 3 步，``Authorization:`` 的值 ``Bearer`` 会先被替换成 ``***``，
    导致第 1/2 步失配、JWT 尾部落盘（QA D-01）。
    """
    if not text or not isinstance(text, str):
        return text
    masked = _AUTHORIZATION_HEADER_RE.sub(lambda match: f"{match.group(1)}{MASK}", text)
    masked = _BEARER_RE.sub(lambda match: f"{match.group(1)} {MASK}", masked)
    masked = _SECRET_TEXT_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}{MASK}", masked)
    return masked


def mask_sensitive(value: Any) -> Any:
    """递归脱敏任意可序列化对象（dict/list/tuple/set/pydantic 模型/其它原样返回）。

    - 字段名命中 :data:`_SENSITIVE_KEYWORDS` → 值替换为 ``***``；
    - 字符串值 → :func:`mask_text` 掩码；
    - 容器 → 递归处理；其余类型原样返回（由 JSON 序列化的 ``default=str`` 兜底）。
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, Mapping):
        return {
            key: (MASK if _is_sensitive_key(str(key)) else mask_sensitive(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [mask_sensitive(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [mask_sensitive(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    # pydantic v2 模型
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return mask_sensitive(model_dump())
        except Exception:  # pragma: no cover - 防御性兜底
            return str(value)
    return value


def _safe_json(value: Any) -> str | None:
    """把任意对象转成脱敏后的 JSON 字符串（用于审计 before/after）。"""
    if value is None:
        return None
    try:
        return json.dumps(mask_sensitive(value), ensure_ascii=False, default=str)
    except Exception:  # pragma: no cover - 极端不可序列化对象
        return json.dumps({"__unserializable__": str(value)}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 日志格式化器
# ---------------------------------------------------------------------------

#: LogRecord 自带的保留字段（其余字段归入 extra）
_RESERVED_RECORD_KEYS: frozenset[str] = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
        "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
        "relativeCreated", "thread", "threadName", "processName", "process", "taskName",
        "message", "asctime", "trace_id",
    }
)


def _record_trace_id(record: LogRecord) -> str | None:
    """取日志记录的 trace_id（优先记录自带 extra，其次当前上下文）。"""
    return getattr(record, "trace_id", None) or get_trace_id()


def _extra_of(record: LogRecord) -> dict[str, Any]:
    """提取并脱敏日志记录的额外字段（顶层字段名同样做敏感判定）。"""
    extra: dict[str, Any] = {}
    for key, value in record.__dict__.items():
        if key in _RESERVED_RECORD_KEYS or key.startswith("_"):
            continue
        extra[key] = MASK if _is_sensitive_key(key) else mask_sensitive(value)
    return extra


class JsonLineFormatter(logging.Formatter):
    """JSON Lines 格式化器（每行一条 JSON，字段见 :data:`LOG_FIELDS`）。"""

    def format(self, record: LogRecord) -> str:
        """把日志记录格式化为单行 JSON（含 ts/level/logger/trace_id/module/message/extra）。"""
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=LOCAL_TZ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "trace_id": _record_trace_id(record),
            "module": record.module,
            "message": mask_text(record.getMessage()),
            "extra": _extra_of(record),
        }
        if record.exc_info:
            payload["exception"] = mask_text(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack"] = mask_text(self.formatStack(record.stack_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    """控制台人读格式：``时间 | 级别 | logger | trace=xxx | 消息 | extra``。"""

    def format(self, record: LogRecord) -> str:
        """格式化为一行中文友好文本（附 extra 与异常堆栈）。"""
        ts = datetime.fromtimestamp(record.created, tz=LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
        trace_id = _record_trace_id(record) or "-"
        line = (
            f"{ts} | {record.levelname:<5} | {record.name} | trace={trace_id} | "
            f"{mask_text(record.getMessage())}"
        )
        extra = _extra_of(record)
        if extra:
            line += f" | extra={json.dumps(extra, ensure_ascii=False, default=str)}"
        if record.exc_info:
            line += "\n" + mask_text(self.formatException(record.exc_info))
        return line


# ---------------------------------------------------------------------------
# 按日切分文件 Handler（app-YYYYMMDD.log）
# ---------------------------------------------------------------------------

_LOG_FILE_RE = re.compile(r"^app-(\d{8})\.log$")


class DailyJsonFileHandler(logging.Handler):
    """按日期切分的 JSON Lines 文件 Handler，并负责清理过期日志。

    - 文件名：``<log_dir>/app-YYYYMMDD.log``（SPEC §8.8-1）；
    - 切日时自动关闭旧文件、打开新文件、清理超过 ``retention_days`` 的旧日志；
    - 写入失败不抛异常（``handleError``），避免日志问题拖垮主流程。
    """

    def __init__(
        self,
        log_dir: str | Path = DEFAULT_LOG_DIR,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        encoding: str = "utf-8",
        level: int = logging.NOTSET,
    ) -> None:
        super().__init__(level=level)
        self.log_dir = Path(log_dir)
        self.retention_days = max(int(retention_days), 1)
        self.encoding = encoding
        self._stream: Any = None
        self._current_date: date | None = None
        self._file_lock = threading.RLock()
        self.setFormatter(JsonLineFormatter())
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, day: date) -> Path:
        return self.log_dir / f"{LOG_FILE_PREFIX}-{day.strftime('%Y%m%d')}.log"

    def _rotate(self, day: date) -> None:
        """切换到指定日期的日志文件并清理过期文件。"""
        if self._stream is not None:
            try:
                self._stream.close()
            finally:
                self._stream = None
        path = self._path_for(day)
        # 追加写入：同日多次启动/重开文件不会覆盖历史日志
        self._stream = open(path, "a", encoding=self.encoding, buffering=1)
        self._current_date = day
        self._cleanup_old_files(day)

    def _cleanup_old_files(self, today: date) -> None:
        """删除保留期之外的 ``app-YYYYMMDD.log``（失败只忽略，不影响写日志）。"""
        cutoff = today - timedelta(days=self.retention_days)
        try:
            for path in self.log_dir.glob(f"{LOG_FILE_PREFIX}-*.log"):
                match = _LOG_FILE_RE.match(path.name)
                if not match:
                    continue
                try:
                    file_day = datetime.strptime(match.group(1), "%Y%m%d").date()
                except ValueError:
                    continue
                if file_day < cutoff:
                    try:
                        path.unlink()
                    except OSError:  # pragma: no cover - 文件被占用等
                        pass
        except OSError:  # pragma: no cover
            pass

    def emit(self, record: LogRecord) -> None:
        """写入一条日志；跨日时自动切分文件。"""
        try:
            day = datetime.fromtimestamp(record.created, tz=LOCAL_TZ).date()
            with self._file_lock:
                if self._stream is None or day != self._current_date:
                    self._rotate(day)
                assert self._stream is not None
                self._stream.write(self.format(record) + "\n")
                self._stream.flush()
        except Exception:  # pragma: no cover - 磁盘满/权限等
            self.handleError(record)

    def close(self) -> None:
        """关闭文件句柄（进程退出/重置日志时调用）。"""
        with self._file_lock:
            if self._stream is not None:
                try:
                    self._stream.close()
                finally:
                    self._stream = None
                self._current_date = None
        super().close()


# ---------------------------------------------------------------------------
# 初始化 / 获取 / 重置
# ---------------------------------------------------------------------------

_setup_lock = threading.RLock()
_log_dir: Path = DEFAULT_LOG_DIR


def _cfg_get(config: Any, dotted_key: str, default: Any = None) -> Any:
    """从 dict 或对象配置中按 ``a.b.c`` 路径取值（找不到返回默认值）。"""
    if config is None:
        return default
    current: Any = config
    for part in dotted_key.split("."):
        if isinstance(current, Mapping):
            if part not in current:
                return default
            current = current[part]
        else:
            current = getattr(current, part, None)
            if current is None:
                return default
    return current


def _remove_managed_handlers(target_logger: logging.Logger) -> None:
    """移除本模块此前安装的 handler（重复初始化/测试隔离用）。"""
    for handler in list(target_logger.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            target_logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # pragma: no cover
                pass


def _resolve_log_dir(config: Any, log_dir: str | Path | None) -> Path:
    """解析日志目录：显式参数 > 配置 ``app.data_dir`` > 默认 ``./runtime``。"""
    if log_dir is not None:
        return Path(log_dir)
    data_dir = _cfg_get(config, "app.data_dir", None)
    if data_dir:
        return Path(str(data_dir)) / "logs"
    return DEFAULT_LOG_DIR


def setup_logging(
    config: Any = None,
    *,
    log_dir: str | Path | None = None,
    level: str | int | None = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    console: bool = True,
    force: bool = False,
) -> logging.Logger:
    """初始化应用日志系统（进程内幂等）。

    :param config: 配置对象/字典（读取 ``app.data_dir`` 与 ``app.log_level``）
    :param log_dir: 日志目录；为空则用 ``<app.data_dir>/logs``（默认 ``./runtime/logs``）
    :param level: 日志级别（名字或数字）；为空则用 ``app.log_level``，默认 ``INFO``
    :param retention_days: 日志保留天数（默认 30）
    :param console: 是否输出控制台人读格式（测试可关）
    :param force: 是否强制移除旧 handler 后重建
    :return: 应用根日志器 ``quantdesk``
    """
    global _log_dir
    with _setup_lock:
        app_logger = logging.getLogger(_APP_LOGGER_NAME)
        if app_logger.handlers and not force:
            # 已初始化：只按需更新级别，避免重复叠加 handler（幂等）
            if level is not None:
                app_logger.setLevel(level)
            return app_logger

        resolved_level: int | str = level or _cfg_get(config, "app.log_level", "INFO") or "INFO"
        if isinstance(resolved_level, str):
            resolved_level = resolved_level.upper()
            if resolved_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
                resolved_level = "INFO"

        target = _resolve_log_dir(config, log_dir)
        _log_dir = target

        # 清空旧 handler（含旧的 root handler，避免重复输出）
        for old in list(app_logger.handlers):
            app_logger.removeHandler(old)
            try:
                old.close()
            except Exception:  # pragma: no cover
                pass
        app_logger.setLevel(resolved_level)
        app_logger.propagate = False
        app_logger.handlers.clear()

        file_handler: logging.Handler | None = None
        try:
            file_handler = DailyJsonFileHandler(target, retention_days=retention_days)
            file_handler.setLevel(resolved_level)
            setattr(file_handler, _HANDLER_MARK, True)
            app_logger.addHandler(file_handler)
        except Exception as exc:  # pragma: no cover - 磁盘/权限异常
            # 日志目录不可写时不崩溃：退化为仅控制台，并在控制台提示中文原因
            print(f"[QuantDesk] 警告：日志文件初始化失败（{exc}），本次仅输出到控制台", file=sys.stderr)
            file_handler = None

        if console:
            console_handler = logging.StreamHandler(stream=sys.stdout)
            console_handler.setLevel(resolved_level)
            console_handler.setFormatter(ConsoleFormatter())
            setattr(console_handler, _HANDLER_MARK, True)
            app_logger.addHandler(console_handler)

        # 让第三方库日志也进入同一套 handler（统一落盘），但级别不低于 WARNING 以减少噪音
        if isinstance(resolved_level, str):
            numeric_level = int(logging.getLevelName(resolved_level))
        else:
            numeric_level = int(resolved_level)
        third_party_level = max(logging.WARNING, numeric_level)
        for noisy in ("streamlit", "apscheduler", "urllib3", "matplotlib", "akshare"):
            logging.getLogger(noisy).setLevel(third_party_level)

        app_logger.debug("日志系统初始化完成：dir=%s level=%s retention=%s天", target, resolved_level, retention_days)
        return app_logger


def reset_logging() -> None:
    """关闭并移除应用日志 handler（测试/重启用；不删除日志文件）。"""
    app_logger = logging.getLogger(_APP_LOGGER_NAME)
    _remove_managed_handlers(app_logger)
    for noisy in ("streamlit", "apscheduler", "urllib3", "matplotlib", "akshare"):
        logging.getLogger(noisy).setLevel(logging.NOTSET)


def get_logger(name: str = _APP_LOGGER_NAME) -> logging.Logger:
    """获取应用日志器：``get_logger("order_manager")`` → ``quantdesk.order_manager``。"""
    if not name:
        return logging.getLogger(_APP_LOGGER_NAME)
    if name == _APP_LOGGER_NAME or name.startswith(f"{_APP_LOGGER_NAME}."):
        return logging.getLogger(name)
    return logging.getLogger(f"{_APP_LOGGER_NAME}.{name}")


def get_log_dir() -> Path:
    """返回当前日志目录（未初始化时为默认目录）。"""
    return _log_dir


# ---------------------------------------------------------------------------
# 审计流水（audit_logs；只增不改不删）
# ---------------------------------------------------------------------------


class AuditAction:
    """审计动作名常量（CF-11 冻结：UI/日志/测试统一引用，避免散落硬编码字符串）。"""

    ORDER_PLACE = "order.place"
    ORDER_CANCEL = "order.cancel"
    RISK_CONFIG_UPDATE = "risk.config.update"
    RISK_UNLOCK = "risk.unlock"
    STRATEGY_CREATE = "strategy.create"
    STRATEGY_UPDATE = "strategy.update"
    STRATEGY_DELETE = "strategy.delete"
    STRATEGY_START = "strategy.start"
    STRATEGY_STOP = "strategy.stop"
    AUTOMATION_START = "automation.start"
    AUTOMATION_STOP = "automation.stop"
    JOB_MANUAL_TRIGGER = "job.manual_trigger"
    ACCOUNT_CREATE = "account.create"
    ACCOUNT_RESET = "account.reset"
    DATA_SOURCE_SWITCH = "data.source.switch"
    DATA_REFRESH = "data.refresh"
    BACKUP_CREATE = "backup.create"
    BACKUP_RESTORE = "backup.restore"
    NOTIFY_TEST = "notify.test"
    SETTINGS_UPDATE = "settings.update"
    DATA_QUALITY_PURGE = "data.quality.purge"


#: 审计会话工厂：返回 SQLAlchemy Session 的可调用对象（如 sessionmaker）
SessionFactory = Callable[[], Any]

_audit_session_factory: SessionFactory | None = None
_audit_lock = threading.RLock()


def set_audit_session_factory(factory: SessionFactory | None) -> None:
    """注入审计写库用的 Session 工厂（由 ``core/db.py`` 启动时调用）。

    :param factory: 返回 ``sqlalchemy.orm.Session`` 的可调用对象，如 ``sessionmaker(bind=engine)``；
                    传 ``None`` 表示恢复为「延迟使用 core/db.py」模式。
    """
    global _audit_session_factory
    with _audit_lock:
        _audit_session_factory = factory


def get_audit_session_factory() -> SessionFactory | None:
    """返回当前审计会话工厂（未注入时为 ``None``）。"""
    return _audit_session_factory


def _resolve_audit_session() -> Any:
    """获取审计写库 Session：优先注入的工厂，其次延迟导入 ``core/db.py``。

    ``core/db.py``（任务 M0-04）需提供以下任一接口：
    - ``get_session_factory() -> sessionmaker``（推荐）
    - ``SessionLocal``（sessionmaker 实例）
    """
    factory = _audit_session_factory
    if factory is not None:
        return factory()
    try:
        from . import db as _db  # 延迟导入：避免与 core/db.py 形成启动期循环依赖
    except Exception as exc:  # pragma: no cover - M0-04 未完成时
        raise RuntimeError(
            "审计写库失败：数据库模块 core/db.py 尚未就绪，请先完成 M0-04 或调用 set_audit_session_factory()"
        ) from exc
    getter = getattr(_db, "get_session_factory", None)
    if callable(getter):
        factory = getter()
        if factory is not None:
            return factory()
    factory = getattr(_db, "SessionLocal", None)
    if factory is not None:
        return factory()
    raise RuntimeError("审计写库失败：core/db.py 未提供 get_session_factory() 或 SessionLocal")


def _split_target(
    target: Any, target_type: str | None, target_id: str | None
) -> tuple[str | None, str | None]:
    """解析审计对象：支持 ``(type, id)`` 元组或 ``target_id`` 字符串。"""
    if isinstance(target, (tuple, list)) and len(target) == 2:
        target_type = target_type or str(target[0])
        target_id = target_id or str(target[1])
    elif target is not None and target_id is None:
        target_id = str(target)
    return target_type, target_id


def write_audit(
    action: str,
    target: str | tuple[str, str] | None = None,
    before: Any = None,
    after: Any = None,
    actor: str | AuditActor = AuditActor.SYSTEM,
    trace_id: str | None = None,
    *,
    result: str | AuditResult = AuditResult.SUCCESS,
    target_type: str | None = None,
    target_id: str | None = None,
    session: Any = None,
) -> bool:
    """写一条审计记录到 ``audit_logs`` 表（契约 CF-11 冻结签名）。

    :param action: 动作名，使用 :class:`AuditAction` 常量（如 ``order.place``）
    :param target: 审计对象：``(target_type, target_id)`` 元组或仅 target_id 字符串
    :param before: 变更前快照（dict/list，自动脱敏后存 JSON 字符串）
    :param after: 变更后快照（同上）
    :param actor: 操作者：``manual`` / ``scheduler`` / ``system`` / ``auto``
    :param trace_id: 追踪 ID；为空时自动取当前上下文
    :param result: 结果：``SUCCESS`` / ``FAILED`` / ``DENIED``
    :param target_type: 显式指定对象类型（覆盖 target 元组解析）
    :param target_id: 显式指定对象 ID（覆盖 target 参数）
    :param session: 可选的 SQLAlchemy Session；传入时只 ``add+flush``，由调用方提交事务；
                    不传时自动创建会话、提交并在结束后关闭
    :return: 是否写入成功（写审计失败**不阻塞主流程**，只记 ERROR 日志）

    示例::

        write_audit(
            AuditAction.RISK_CONFIG_UPDATE, ("risk_config", "1"),
            before={"max_total_position_pct": 80}, after={"max_total_position_pct": 60},
            actor=AuditActor.MANUAL,
        )
    """
    resolved_trace_id = trace_id or get_trace_id()
    resolved_target_type, resolved_target_id = _split_target(target, target_type, target_id)
    actor_value = getattr(actor, "value", actor) or AuditActor.SYSTEM.value
    result_value = getattr(result, "value", result) or AuditResult.SUCCESS.value

    row = AuditLogORM(
        ts=now_local_naive(),
        actor=str(actor_value),
        action=str(action),
        target_type=resolved_target_type,
        target_id=resolved_target_id,
        before_json=_safe_json(before),
        after_json=_safe_json(after),
        result=str(result_value),
        trace_id=resolved_trace_id,
    )

    own_session = session is None
    db_session: Any = None
    try:
        db_session = _resolve_audit_session() if own_session else session
        db_session.add(row)
        if own_session:
            db_session.commit()
        else:
            db_session.flush()
        return True
    except Exception:
        if own_session and db_session is not None:
            try:
                db_session.rollback()
            except Exception:  # pragma: no cover
                pass
        logger.error(
            "审计落库失败（不影响主流程）：action=%s target=%s/%s trace_id=%s",
            action,
            resolved_target_type,
            resolved_target_id,
            resolved_trace_id,
            exc_info=True,
        )
        return False
    finally:
        if own_session and db_session is not None:
            try:
                db_session.close()
            except Exception:  # pragma: no cover
                pass


# ---------------------------------------------------------------------------
# 自检（``python -m core.logging`` 在 quantdesk 目录下运行）
# ---------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    import tempfile

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    with tempfile.TemporaryDirectory() as tmp:
        # 1) 初始化日志（临时目录 + 关闭控制台，便于断言文件内容）
        setup_logging(
            {"app": {"data_dir": tmp, "log_level": "DEBUG"}},
            console=False,
            retention_days=30,
            force=True,
        )
        try:
            test_logger = get_logger("selftest")

            # 2) trace_id 上下文贯穿 + 敏感信息过滤
            with trace_context("trace-abc") as tid:
                assert tid == "trace-abc" and get_trace_id() == "trace-abc"
                test_logger.info(
                    "下单成功 password=hunter2 token=abc123",
                    extra={"user": "u1", "api_key": "sk-secret-value", "nested": {"password": "p@ss"}},
                )
            test_logger.warning("无 trace 的日志")

            log_dir = Path(tmp) / "logs"
            files = list(log_dir.glob("app-*.log"))
            assert len(files) == 1, files
            lines = files[0].read_text(encoding="utf-8").strip().splitlines()
            records = [json.loads(line) for line in lines]
            assert all(set(LOG_FIELDS) <= set(rec) for rec in records), records

            traced = [rec for rec in records if rec["trace_id"] == "trace-abc"]
            assert len(traced) == 1, records
            first = traced[0]
            assert first["level"] == "INFO" and first["logger"].endswith("selftest")
            assert first["extra"]["user"] == "u1"
            assert first["extra"]["api_key"] == MASK
            assert first["extra"]["nested"]["password"] == MASK
            dumped = json.dumps(records, ensure_ascii=False)
            assert "hunter2" not in dumped and "abc123" not in dumped and "p@ss" not in dumped, dumped
            assert "无 trace 的日志" in dumped

            # 3) 控制台格式含人读时间/级别/trace
            console_text = ConsoleFormatter().format(
                logging.LogRecord("quantdesk.demo", logging.INFO, __file__, 1, "演示消息 token=xyz", None, None)
            )
            assert "quantdesk.demo" in console_text and "trace=" in console_text
            assert "xyz" not in console_text, console_text

            # 3.1) 缺陷 D-01 回归：Authorization: Bearer <jwt> 必须整段掩码（QA test_authorization_bearer_header_is_fully_masked）
            bearer_masked = mask_text("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc")
            assert "eyJhbGciOiJIUzI1NiJ9.abc" not in bearer_masked, bearer_masked
            assert MASK in bearer_masked and bearer_masked.startswith("Authorization: "), bearer_masked
            assert "abc.def" not in mask_text("Bearer abc.def")
            assert "sk-live-9999" not in mask_text('{"api_key": "sk-live-9999"}')
            assert mask_text("普通日志：下单成功 100 股") == "普通日志：下单成功 100 股"
            assert mask_text("") == ""
        finally:
            # 必须关闭文件句柄，否则 Windows 上临时目录清理会报“文件被占用”
            reset_logging()

    # 4) 审计落库（内存库建 audit_logs 表）+ before/after 脱敏
    engine = create_engine("sqlite://")
    AuditLogORM.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    set_audit_session_factory(factory)

    with trace_context("trace-audit"):
        ok = write_audit(
            AuditAction.ORDER_PLACE,
            ("order", "ord-1"),
            before={"password": "hunter2", "price": 10.0},
            after={"status": "FILLED", "token": "abc123"},
            actor=AuditActor.MANUAL,
        )
    assert ok is True
    assert get_audit_session_factory() is factory
    with factory() as session:
        row = session.scalars(select(AuditLogORM)).one()
        assert row.action == AuditAction.ORDER_PLACE
        assert row.target_type == "order" and row.target_id == "ord-1"
        assert row.actor == "manual" and row.result == "SUCCESS"
        assert row.trace_id == "trace-audit" and row.ts is not None
        assert "hunter2" not in (row.before_json or "") and "abc123" not in (row.after_json or "")
        assert row.before_json is not None and "***" in row.before_json

    # 5) 显式 session + 调用方提交的事务路径
    with factory() as session:
        ok2 = write_audit(
            AuditAction.RISK_CONFIG_UPDATE, ("risk_config", "1"),
            before={"max_total_position_pct": 80}, after={"max_total_position_pct": 60},
            actor="manual", session=session,
        )
        assert ok2 is True
        session.commit()
    with factory() as session:
        assert len(session.scalars(select(AuditLogORM)).all()) == 2

    set_audit_session_factory(None)

    print(
        "core/logging.py 自检通过：JSON Lines 字段/按日文件/trace_id 贯穿/敏感信息脱敏/"
        "控制台格式/审计落库（含 before-after 脱敏与显式事务）"
    )

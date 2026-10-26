"""QuantDesk 同步事件总线（契约冻结项 CF-8，SPEC §3.3 / §6.6）。

设计要点
--------
1. **进程内、同步分发**：``publish`` 立即依次调用订阅者，不引入线程/队列。
   原因：MVP 为单用户本地应用，同步分发让 ``trace_id`` 与异常堆栈最容易追踪，
   也避免“UI 订阅后异步刷新”带来的时序不确定（借鉴 vnpy EventEngine 的 handler 隔离思想，自行实现）。
2. **异常隔离**：单个订阅者抛异常只记日志，绝不向 ``publish`` 调用方或其他订阅者传播
   （BORROW_MAP P1 验收点）。
3. **主题常量集中定义**：全部主题见 :class:`Topic`，与 SPEC §3.3 一致；新增主题须走契约变更流程。
4. **通配符订阅**：支持 ``order.*``、``job.*``、``*`` 等模式，便于日志/审计模块一处监听。
   精确主题订阅者先执行，通配符订阅者随后按注册顺序执行。
5. **trace_id 贯穿**：``publish`` 未显式传 ``trace_id`` 时，自动取 ``core/logging.py``
   的上下文（contextvars），保证一次调用链共享同一 trace_id。

用法示例::

    from core.events import Topic, get_event_bus

    bus = get_event_bus()
    bus.subscribe(Topic.ORDER_FILLED, on_filled)
    bus.subscribe("order.*", audit_handler)      # 通配符
    bus.publish(Topic.ORDER_FILLED, {"order_id": "..."}, trace_id="abc123")
"""

from __future__ import annotations

import fnmatch
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .models import now_local

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 主题常量（SPEC §3.3 冻结，CF-8）
# ---------------------------------------------------------------------------


class Topic:
    """事件主题常量（SPEC §3.3 冻结）。

    ``payload`` 关键字段（发布者按此组装，订阅者按此读取）：

    ====================  ==========================================================
    主题                   payload 关键字段
    ====================  ==========================================================
    ``market.bar``        symbol, trade_date, open/high/low/close/volume, adj_factor
    ``signal``            trace_id, strategy_instance_id, symbol, side, strength, reason, ts
    ``order.submitted``   trace_id, order_id, symbol, side, price, qty, source
    ``order.filled``      trace_id, order_id, price, qty, fee_breakdown
    ``order.rejected``    trace_id, order_id, reason_code, message
    ``order.cancelled``   trace_id, order_id, cancelled_qty
    ``risk.blocked``      trace_id, symbol, side, reason_code, details
    ``job.started``       job_key, trace_id
    ``job.finished``      job_key, trace_id, stats
    ``job.failed``        job_key, trace_id, error
    ``notify.sent``       channel, event_type, status
    ====================  ==========================================================
    """

    MARKET_BAR = "market.bar"
    SIGNAL = "signal"
    ORDER_SUBMITTED = "order.submitted"
    ORDER_FILLED = "order.filled"
    ORDER_REJECTED = "order.rejected"
    ORDER_CANCELLED = "order.cancelled"
    RISK_BLOCKED = "risk.blocked"
    JOB_STARTED = "job.started"
    JOB_FINISHED = "job.finished"
    JOB_FAILED = "job.failed"
    NOTIFY_SENT = "notify.sent"


# 模块级别名（两种引用方式都可用，降低各模块的 import 心智负担）
MARKET_BAR = Topic.MARKET_BAR
SIGNAL = Topic.SIGNAL
ORDER_SUBMITTED = Topic.ORDER_SUBMITTED
ORDER_FILLED = Topic.ORDER_FILLED
ORDER_REJECTED = Topic.ORDER_REJECTED
ORDER_CANCELLED = Topic.ORDER_CANCELLED
RISK_BLOCKED = Topic.RISK_BLOCKED
JOB_STARTED = Topic.JOB_STARTED
JOB_FINISHED = Topic.JOB_FINISHED
JOB_FAILED = Topic.JOB_FAILED
NOTIFY_SENT = Topic.NOTIFY_SENT

#: 全部冻结主题（顺序同 SPEC §3.3）
ALL_TOPICS: tuple[str, ...] = (
    Topic.MARKET_BAR,
    Topic.SIGNAL,
    Topic.ORDER_SUBMITTED,
    Topic.ORDER_FILLED,
    Topic.ORDER_REJECTED,
    Topic.ORDER_CANCELLED,
    Topic.RISK_BLOCKED,
    Topic.JOB_STARTED,
    Topic.JOB_FINISHED,
    Topic.JOB_FAILED,
    Topic.NOTIFY_SENT,
)

#: 各主题 payload 必填字段（供 :func:`validate_payload` 与 QA 断言使用）
TOPIC_PAYLOAD_FIELDS: dict[str, tuple[str, ...]] = {
    Topic.MARKET_BAR: ("symbol", "trade_date", "close"),
    Topic.SIGNAL: ("symbol", "side"),
    Topic.ORDER_SUBMITTED: ("order_id", "symbol", "side"),
    Topic.ORDER_FILLED: ("order_id", "price", "qty"),
    Topic.ORDER_REJECTED: ("order_id", "reason_code"),
    Topic.ORDER_CANCELLED: ("order_id",),
    Topic.RISK_BLOCKED: ("symbol", "side", "reason_code"),
    Topic.JOB_STARTED: ("job_key",),
    Topic.JOB_FINISHED: ("job_key",),
    Topic.JOB_FAILED: ("job_key", "error"),
    Topic.NOTIFY_SENT: ("channel", "event_type", "status"),
}


# ---------------------------------------------------------------------------
# 事件对象
# ---------------------------------------------------------------------------


@dataclass
class Event:
    """事件对象（订阅者入参）。

    :param topic: 事件主题，见 :class:`Topic`
    :param payload: 事件数据字典
    :param trace_id: 追踪 ID（贯穿一次调用链）
    :param ts: 事件产生时间（本地时区 ``Asia/Shanghai``）
    """

    topic: str
    payload: dict[str, Any] = field(default_factory=dict)
    trace_id: str | None = None
    ts: datetime = field(default_factory=now_local)

    @property
    def type(self) -> str:
        """兼容 vnpy 风格字段名（``event.type`` 等价 ``event.topic``）。"""
        return self.topic

    @property
    def data(self) -> dict[str, Any]:
        """兼容 vnpy/nautilus 风格字段名（``event.data`` 等价 ``event.payload``）。"""
        return self.payload

    def get(self, key: str, default: Any = None) -> Any:
        """便捷读取 payload 字段。"""
        return self.payload.get(key, default)


Handler = Callable[[Event], None]


def validate_payload(topic: str, payload: dict[str, Any]) -> list[str]:
    """校验 payload 是否包含该主题的必填字段（SPEC §3.3）。

    :return: 缺失字段名列表；空列表表示通过。未知主题返回空列表（不强行约束自定义主题）。
    """
    required = TOPIC_PAYLOAD_FIELDS.get(topic)
    if not required:
        return []
    return [key for key in required if key not in payload]


def topic_matches(pattern: str, topic: str) -> bool:
    """判断订阅模式是否匹配主题。

    - 完全相同 → ``True``；
    - 含 ``*`` / ``?`` 的通配模式用 ``fnmatch`` 匹配（如 ``order.*``、``job.*``、``*``）；
    - 其它情况 → ``False``（不做前缀模糊匹配，避免误订阅）。
    """
    if pattern == topic:
        return True
    if "*" in pattern or "?" in pattern:
        return fnmatch.fnmatchcase(topic, pattern)
    return False


def _current_trace_id() -> str | None:
    """从 ``core/logging.py`` 的上下文取当前 trace_id（延迟导入，避免循环依赖）。"""
    try:
        from .logging import get_trace_id  # 延迟导入：core.logging 不反向依赖 events
    except Exception:  # pragma: no cover - 极端情况下日志模块不可用
        return None
    try:
        return get_trace_id()
    except Exception:  # pragma: no cover
        return None


# ---------------------------------------------------------------------------
# 事件总线
# ---------------------------------------------------------------------------


class EventBus:
    """进程内同步事件总线（SPEC §6.6 冻结接口：subscribe / publish / unsubscribe）。

    - 线程安全：订阅表读写用 ``RLock`` 保护；``publish`` 在锁内拷贝订阅者快照后**锁外**分发，
      避免 handler 内再次 subscribe/publish 造成死锁；
    - 幂等注册：同一 handler 重复订阅同一主题只保留一次（避免重复处理资金类事件）；
    - 异常隔离：单个 handler 异常只记 ERROR 日志（含 traceback），不中断其它 handler。
    """

    def __init__(self, name: str = "quantdesk", event_logger: logging.Logger | None = None) -> None:
        self.name = name
        self._logger = event_logger or logging.getLogger(f"quantdesk.events.{name}")
        self._lock = threading.RLock()
        # 精确主题订阅：topic -> [handler, ...]
        self._handlers: dict[str, list[Handler]] = {}
        # 通配符订阅：[(pattern, handler), ...]（保持注册顺序）
        self._patterns: list[tuple[str, Handler]] = []

    # -- 订阅管理 ---------------------------------------------------------
    def subscribe(self, topic: str, handler: Handler) -> None:
        """订阅主题（支持通配符）。

        :param topic: 主题字符串（见 :class:`Topic`），可含 ``*``/``?``
        :param handler: 回调函数，签名 ``handler(event: Event) -> None``
        :raises ValueError: topic 为空或 handler 不可调用（中文错误提示）
        """
        if not isinstance(topic, str) or not topic.strip():
            raise ValueError("事件订阅失败：主题 topic 不能为空字符串")
        if not callable(handler):
            raise ValueError(f"事件订阅失败：handler 必须可调用（当前类型 {type(handler).__name__}）")
        topic = topic.strip()
        with self._lock:
            if "*" in topic or "?" in topic:
                if any(pat == topic and h is handler for pat, h in self._patterns):
                    return  # 幂等：重复注册同一 pattern + handler 忽略
                self._patterns.append((topic, handler))
            else:
                handlers = self._handlers.setdefault(topic, [])
                if any(h is handler for h in handlers):
                    return
                handlers.append(handler)
        self._logger.debug("事件订阅成功：topic=%s handler=%s", topic, _handler_name(handler))

    def unsubscribe(self, topic: str, handler: Handler) -> None:
        """取消订阅（SPEC §6.6 冻结签名，返回 ``None``）。

        未注册时不抛异常（只记 DEBUG），便于模块卸载/测试清理幂等。
        """
        topic = (topic or "").strip()
        with self._lock:
            if "*" in topic or "?" in topic:
                self._patterns = [(pat, h) for pat, h in self._patterns if not (pat == topic and h is handler)]
            else:
                handlers = self._handlers.get(topic)
                if handlers:
                    self._handlers[topic] = [h for h in handlers if h is not handler]
                    if not self._handlers[topic]:
                        self._handlers.pop(topic, None)
        self._logger.debug("事件取消订阅：topic=%s handler=%s", topic, _handler_name(handler))

    def clear(self) -> None:
        """清空全部订阅（仅测试/重启场景使用）。"""
        with self._lock:
            self._handlers.clear()
            self._patterns.clear()

    def subscriber_count(self, topic: str | None = None) -> int:
        """返回订阅者数量；``topic`` 为空时返回精确主题订阅者总数（不含通配符）。"""
        with self._lock:
            if topic is None:
                return sum(len(v) for v in self._handlers.values())
            return len(self._handlers.get(topic, []))

    def has_subscribers(self, topic: str) -> bool:
        """判断某主题是否有订阅者（含通配符匹配）。"""
        with self._lock:
            if self._handlers.get(topic):
                return True
            return any(topic_matches(pattern, topic) for pattern, _ in self._patterns)

    # -- 发布 -------------------------------------------------------------
    def publish(self, topic: str, payload: dict[str, Any], trace_id: str | None = None) -> None:
        """同步发布事件（SPEC §6.6 冻结签名）。

        :param topic: 事件主题（见 :class:`Topic`）
        :param payload: 事件数据字典（会被原样放入 ``Event.payload``，bus 不改写）
        :param trace_id: 追踪 ID；为空时自动取日志上下文中的 trace_id
        :raises ValueError: topic 为空（配置错误必须尽早暴露）
        """
        if not isinstance(topic, str) or not topic.strip():
            raise ValueError("事件发布失败：主题 topic 不能为空字符串")
        topic = topic.strip()
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise ValueError(f"事件发布失败：payload 必须是 dict（当前类型 {type(payload).__name__}）")
        if trace_id is None:
            trace_id = _current_trace_id()

        # 补全 trace_id（部分发布者可能把 trace_id 放在 payload 里）
        if trace_id is None:
            trace_id = payload.get("trace_id")

        event = Event(topic=topic, payload=payload, trace_id=trace_id)

        # 锁内只做快照，锁外分发（handler 内可安全再次订阅/发布）
        with self._lock:
            exact = list(self._handlers.get(topic, ()))
            matched = [(pattern, handler) for pattern, handler in self._patterns if topic_matches(pattern, topic)]

        for handler in exact:
            self._dispatch(handler, event)
        for pattern, handler in matched:
            self._dispatch(handler, event, pattern=pattern)

    def _dispatch(self, handler: Handler, event: Event, *, pattern: str | None = None) -> None:
        """调用单个订阅者；任何异常都被隔离（记录 ERROR + traceback，不向外抛）。"""
        try:
            handler(event)
        except Exception:
            self._logger.error(
                "事件订阅者处理异常已隔离：topic=%s handler=%s%s trace_id=%s",
                event.topic,
                _handler_name(handler),
                f" pattern={pattern}" if pattern else "",
                event.trace_id,
                exc_info=True,
            )


def _handler_name(handler: Handler) -> str:
    """返回 handler 的可读名称（日志用）。"""
    return getattr(handler, "__qualname__", None) or getattr(handler, "__name__", None) or repr(handler)


# ---------------------------------------------------------------------------
# 全局默认总线（进程内单例，供各模块直接 import 使用）
# ---------------------------------------------------------------------------

_DEFAULT_BUS: EventBus | None = None
_DEFAULT_BUS_LOCK = threading.Lock()


def get_event_bus() -> EventBus:
    """获取进程内全局默认事件总线（线程安全懒加载单例）。"""
    global _DEFAULT_BUS
    if _DEFAULT_BUS is None:
        with _DEFAULT_BUS_LOCK:
            if _DEFAULT_BUS is None:
                _DEFAULT_BUS = EventBus()
    return _DEFAULT_BUS


def init_event_bus(bus: EventBus | None = None) -> EventBus:
    """初始化/替换全局事件总线（测试隔离或启动期注入用）。"""
    global _DEFAULT_BUS
    with _DEFAULT_BUS_LOCK:
        _DEFAULT_BUS = bus or EventBus()
    return _DEFAULT_BUS


def publish(topic: str, payload: dict[str, Any], trace_id: str | None = None) -> None:
    """快捷发布：等价 ``get_event_bus().publish(...)``。"""
    get_event_bus().publish(topic, payload, trace_id)


def subscribe(topic: str, handler: Handler) -> None:
    """快捷订阅：等价 ``get_event_bus().subscribe(...)``。"""
    get_event_bus().subscribe(topic, handler)


def unsubscribe(topic: str, handler: Handler) -> None:
    """快捷取消订阅：等价 ``get_event_bus().unsubscribe(...)``。"""
    get_event_bus().unsubscribe(topic, handler)


__all__ = [
    "Topic",
    "Event",
    "Handler",
    "EventBus",
    "ALL_TOPICS",
    "TOPIC_PAYLOAD_FIELDS",
    "validate_payload",
    "topic_matches",
    "get_event_bus",
    "init_event_bus",
    "publish",
    "subscribe",
    "unsubscribe",
    "MARKET_BAR",
    "SIGNAL",
    "ORDER_SUBMITTED",
    "ORDER_FILLED",
    "ORDER_REJECTED",
    "ORDER_CANCELLED",
    "RISK_BLOCKED",
    "JOB_STARTED",
    "JOB_FINISHED",
    "JOB_FAILED",
    "NOTIFY_SENT",
]


# ---------------------------------------------------------------------------
# 自检（不依赖 tests/ 目录；``python core/events.py`` 可直接运行）
# ---------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    received: list[tuple[str, str | None]] = []
    wildcard_hits: list[str] = []
    errors: list[str] = []

    bus = EventBus(name="selftest")

    def handler_a(event: Event) -> None:
        received.append(("a", event.trace_id))

    def handler_b(event: Event) -> None:
        received.append(("b", event.trace_id))

    def handler_boom(event: Event) -> None:
        errors.append(event.topic)
        raise RuntimeError("模拟订阅者异常（必须被隔离）")

    def handler_order_wildcard(event: Event) -> None:
        wildcard_hits.append(event.topic)

    # 1) 主题常量与 SPEC §3.3 一致
    assert ALL_TOPICS == (
        "market.bar", "signal", "order.submitted", "order.filled", "order.rejected",
        "order.cancelled", "risk.blocked", "job.started", "job.finished", "job.failed", "notify.sent",
    )
    assert validate_payload(Topic.RISK_BLOCKED, {"symbol": "600519.SH"}) == ["side", "reason_code"]

    # 2) 多订阅者 + 异常隔离
    bus.subscribe(Topic.ORDER_FILLED, handler_a)
    bus.subscribe(Topic.ORDER_FILLED, handler_b)
    bus.subscribe(Topic.ORDER_FILLED, handler_boom)
    bus.publish(Topic.ORDER_FILLED, {"order_id": "o1", "price": 10.0, "qty": 100}, trace_id="t-1")
    assert received == [("a", "t-1"), ("b", "t-1")], received  # 异常者不影响其它订阅者
    assert errors == [Topic.ORDER_FILLED]

    # 3) 通配符订阅（order.* 只收订单主题）
    bus.subscribe("order.*", handler_order_wildcard)
    bus.publish(Topic.ORDER_SUBMITTED, {"order_id": "o2", "symbol": "600519.SH", "side": "BUY"})
    bus.publish(Topic.SIGNAL, {"symbol": "600519.SH", "side": "BUY"})
    assert wildcard_hits == [Topic.ORDER_SUBMITTED], wildcard_hits
    assert bus.has_subscribers(Topic.ORDER_CANCELLED) is True
    assert bus.has_subscribers(Topic.MARKET_BAR) is False

    # 4) 取消订阅
    bus.unsubscribe(Topic.ORDER_FILLED, handler_a)
    received.clear()
    bus.publish(Topic.ORDER_FILLED, {"order_id": "o3", "price": 10.0, "qty": 100})
    assert received == [("b", None)], received

    # 5) 空主题/非法参数给中文错误
    for bad_call in (
        lambda: bus.publish("", {}),
        lambda: bus.subscribe("", handler_a),
        lambda: bus.subscribe(Topic.SIGNAL, "not-callable"),  # type: ignore[arg-type]
        lambda: bus.publish(Topic.SIGNAL, "not-a-dict"),  # type: ignore[arg-type]
    ):
        try:
            bad_call()
        except ValueError as exc:
            assert "不能为空" in str(exc) or "可调用" in str(exc) or "必须是 dict" in str(exc), str(exc)
        else:
            raise AssertionError("非法参数未被拦截")

    # 6) 全局单例
    assert get_event_bus() is get_event_bus()
    print("core/events.py 自检通过：主题常量/多订阅者/异常隔离/通配符/取消订阅/trace_id 贯穿")

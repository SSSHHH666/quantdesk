"""事件总线与结构化日志测试（多订阅者、异常隔离、取消订阅、trace_id、敏感信息过滤）。

对应验收标准：
- SPEC §6.6 事件总线冻结接口（subscribe / publish / unsubscribe）
- AC-F8-01 日志文件为有效 JSON Lines，每条含 ``ts/level/trace_id/message``；
  一次手动下单能按 trace_id 追到全部步骤
- AC-F8-02 审计：参数修改产生审计记录，含 before/after
- SPEC §8.8 敏感信息（密码/token/密钥）绝不落盘

Lead 口径：事件主题与日志字段以 ``core/events.py``、``core/logging.py`` 实际实现
+ SPEC §3.3 为准；若发现与 SPEC 不一致，报告 lead，不在用例里改代码。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest

from core.events import (
    ALL_TOPICS,
    TOPIC_PAYLOAD_FIELDS,
    Event,
    EventBus,
    Topic,
    get_event_bus,
    init_event_bus,
    topic_matches,
    validate_payload,
)
from core.logging import (
    MASK,
    AuditAction,
    AuditActor,
    AuditResult,
    DailyJsonFileHandler,
    JsonLineFormatter,
    get_logger,
    get_trace_id,
    mask_sensitive,
    mask_text,
    new_trace_id,
    reset_logging,
    set_audit_session_factory,
    setup_logging,
    trace_context,
    write_audit,
)

#: 16 位十六进制 trace_id（core/logging.py 的格式契约）
TRACE_ID_RE = re.compile(r"^[0-9a-f]{16}$")


# ---------------------------------------------------------------------------
# 一、事件总线：多订阅者 / 通配符 / 幂等 / 取消订阅
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEventBusSubscription:
    def test_multiple_subscribers_all_receive_in_registration_order(self) -> None:
        bus = EventBus("test-multi")
        received: list[str] = []

        def first(event: Event) -> None:
            received.append(f"first:{event.topic}")

        def second(event: Event) -> None:
            received.append(f"second:{event.topic}")

        bus.subscribe(Topic.ORDER_FILLED, first)
        bus.subscribe(Topic.ORDER_FILLED, second)

        assert bus.subscriber_count(Topic.ORDER_FILLED) == 2
        bus.publish(Topic.ORDER_FILLED, {"order_id": "O1", "price": 10.0, "qty": 100})

        assert received == [f"first:{Topic.ORDER_FILLED}", f"second:{Topic.ORDER_FILLED}"]

    def test_wildcard_subscriber_receives_matching_topics(self) -> None:
        bus = EventBus("test-wildcard")
        received: list[str] = []
        bus.subscribe("order.*", lambda event: received.append(event.topic))

        bus.publish(Topic.ORDER_SUBMITTED, {"order_id": "O1", "symbol": "600000.SH", "side": "BUY"})
        bus.publish(Topic.ORDER_FILLED, {"order_id": "O1", "price": 10.0, "qty": 100})
        bus.publish(Topic.SIGNAL, {"symbol": "600000.SH", "side": "BUY"})  # 不匹配

        assert received == [Topic.ORDER_SUBMITTED, Topic.ORDER_FILLED]

    def test_duplicate_subscribe_is_idempotent(self) -> None:
        """同一 handler 重复订阅只保留一次（避免资金类事件被重复处理）。"""
        bus = EventBus("test-idempotent")
        calls: list[int] = []

        def handler(event: Event) -> None:
            calls.append(1)

        bus.subscribe(Topic.SIGNAL, handler)
        bus.subscribe(Topic.SIGNAL, handler)
        bus.publish(Topic.SIGNAL, {"symbol": "600000.SH", "side": "BUY"})

        assert calls == [1]
        assert bus.subscriber_count(Topic.SIGNAL) == 1

    def test_unsubscribe_stops_delivery_and_is_idempotent(self) -> None:
        bus = EventBus("test-unsubscribe")
        calls: list[str] = []

        def handler(event: Event) -> None:
            calls.append(event.topic)

        bus.subscribe(Topic.RISK_BLOCKED, handler)
        bus.publish(Topic.RISK_BLOCKED, {"symbol": "600000.SH", "side": "BUY", "reason_code": "RK007"})
        assert len(calls) == 1

        bus.unsubscribe(Topic.RISK_BLOCKED, handler)
        bus.publish(Topic.RISK_BLOCKED, {"symbol": "600000.SH", "side": "BUY", "reason_code": "RK007"})
        assert len(calls) == 1, "取消订阅后不应再收到事件"

        # 再次取消订阅（未注册）不得抛异常
        bus.unsubscribe(Topic.RISK_BLOCKED, handler)
        bus.unsubscribe("never.subscribed", handler)

    def test_clear_removes_all_subscribers(self) -> None:
        bus = EventBus("test-clear")
        bus.subscribe(Topic.SIGNAL, lambda event: None)
        bus.subscribe("order.*", lambda event: None)

        bus.clear()

        assert bus.subscriber_count() == 0
        assert bus.has_subscribers(Topic.ORDER_FILLED) is False

    def test_has_subscribers_accounts_for_wildcards(self) -> None:
        bus = EventBus("test-has-subscribers")
        bus.subscribe("job.*", lambda event: None)

        assert bus.has_subscribers(Topic.JOB_STARTED) is True
        assert bus.has_subscribers(Topic.SIGNAL) is False

    @pytest.mark.parametrize("topic", ["", "   ", None])
    def test_empty_topic_is_rejected(self, topic: object) -> None:
        bus = EventBus("test-invalid-topic")
        with pytest.raises(ValueError):
            bus.subscribe(topic, lambda event: None)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            bus.publish(topic, {})  # type: ignore[arg-type]

    def test_non_callable_handler_is_rejected(self) -> None:
        bus = EventBus("test-bad-handler")
        with pytest.raises(ValueError):
            bus.subscribe(Topic.SIGNAL, "not-callable")  # type: ignore[arg-type]

    def test_non_dict_payload_is_rejected(self) -> None:
        bus = EventBus("test-bad-payload")
        with pytest.raises(ValueError):
            bus.publish(Topic.SIGNAL, ["symbol", "600000.SH"])  # type: ignore[arg-type]


@pytest.mark.unit
class TestEventBusIsolation:
    def test_handler_exception_is_isolated(self) -> None:
        """单个订阅者异常只记日志（ERROR + traceback），不中断其它订阅者、不向外抛。"""
        bus = EventBus("test-isolation")
        received: list[str] = []

        def boom(event: Event) -> None:
            raise RuntimeError("订阅者内部故障")

        bus.subscribe(Topic.ORDER_FILLED, boom)
        bus.subscribe(Topic.ORDER_FILLED, lambda event: received.append("ok"))

        bus.publish(Topic.ORDER_FILLED, {"order_id": "O1", "price": 10.0, "qty": 100})  # 不抛异常

        assert received == ["ok"], "异常订阅者不应阻断后续订阅者"
        assert bus.subscriber_count(Topic.ORDER_FILLED) == 2

    def test_publish_inside_handler_does_not_deadlock(self) -> None:
        """handler 内再次 publish（锁外分发）——不得死锁，且新事件被投递。"""
        bus = EventBus("test-reentrancy")
        seen: list[str] = []

        def on_signal(event: Event) -> None:
            seen.append("signal")
            bus.publish(Topic.RISK_BLOCKED, {"symbol": "600000.SH", "side": "BUY", "reason_code": "RK007"})

        bus.subscribe(Topic.SIGNAL, on_signal)
        bus.subscribe(Topic.RISK_BLOCKED, lambda event: seen.append("blocked"))

        bus.publish(Topic.SIGNAL, {"symbol": "600000.SH", "side": "BUY"})

        assert seen == ["signal", "blocked"]


# ---------------------------------------------------------------------------
# 二、事件对象与 payload 契约（SPEC §3.3）
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEventContract:
    def test_event_object_fields_and_aliases(self) -> None:
        event = Event(topic=Topic.ORDER_FILLED, payload={"price": 10.0}, trace_id="abc123")

        assert event.topic == Topic.ORDER_FILLED
        assert event.type == event.topic  # vnpy 风格别名
        assert event.data == event.payload  # nautilus 风格别名
        assert event.get("price") == 10.0
        assert event.get("missing", "default") == "default"
        assert event.trace_id == "abc123"

    def test_frozen_topics_match_spec(self) -> None:
        """BRIEF §5 冻结的主题名必须在 ALL_TOPICS 里（多出来的属于增强，不算错）。"""
        required = {
            "market.bar",
            "signal",
            "order.submitted",
            "order.filled",
            "order.rejected",
            "risk.blocked",
            "job.started",
            "job.finished",
            "job.failed",
        }
        assert required <= set(ALL_TOPICS)
        assert all(isinstance(topic, str) and topic for topic in ALL_TOPICS)

    def test_every_topic_has_payload_fields(self) -> None:
        assert set(ALL_TOPICS) <= set(TOPIC_PAYLOAD_FIELDS)
        for topic, fields in TOPIC_PAYLOAD_FIELDS.items():
            assert fields, f"{topic} 未声明必填 payload 字段"

    def test_validate_payload_reports_missing_fields(self) -> None:
        assert validate_payload(Topic.ORDER_FILLED, {"order_id": "O1", "price": 10.0, "qty": 100}) == []
        assert validate_payload(Topic.ORDER_FILLED, {"order_id": "O1"}) == ["price", "qty"]
        assert validate_payload("custom.topic", {}) == []  # 自定义主题不强行约束

    @pytest.mark.parametrize(
        ("pattern", "topic", "expected"),
        [
            ("order.*", "order.filled", True),
            ("order.*", "risk.blocked", False),
            ("job.?", "job.x", True),
            ("*", "anything", True),
            ("signal", "signal", True),
            ("signal", "signal.extra", False),  # 不做前缀模糊匹配
        ],
    )
    def test_topic_matches(self, pattern: str, topic: str, expected: bool) -> None:
        assert topic_matches(pattern, topic) is expected


# ---------------------------------------------------------------------------
# 三、trace_id 贯穿（AC-F5-02 / AC-F8-01）
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTraceId:
    def test_trace_id_format(self) -> None:
        tid = new_trace_id()
        assert TRACE_ID_RE.match(tid)
        assert new_trace_id() != new_trace_id()

    def test_trace_context_sets_and_restores(self) -> None:
        assert get_trace_id() is None

        with trace_context() as tid:
            assert TRACE_ID_RE.match(tid)
            assert get_trace_id() == tid

        assert get_trace_id() is None

    def test_nested_trace_context_restores_outer(self) -> None:
        with trace_context("outer-0001") as outer:
            with trace_context("inner-0002") as inner:
                assert get_trace_id() == inner
            assert get_trace_id() == outer
        assert get_trace_id() is None

    def test_bus_fills_trace_id_from_context(self) -> None:
        bus = EventBus("test-trace")
        captured: list[Event] = []
        bus.subscribe(Topic.ORDER_SUBMITTED, captured.append)

        with trace_context("trace-abc") as tid:
            bus.publish(Topic.ORDER_SUBMITTED, {"order_id": "O1", "symbol": "600000.SH", "side": "BUY"})

        assert captured[0].trace_id == tid == "trace-abc"

    def test_explicit_trace_id_wins_over_context(self) -> None:
        bus = EventBus("test-trace-explicit")
        captured: list[Event] = []
        bus.subscribe(Topic.ORDER_SUBMITTED, captured.append)

        with trace_context("ctx-trace"):
            bus.publish(
                Topic.ORDER_SUBMITTED,
                {"order_id": "O1", "symbol": "600000.SH", "side": "BUY"},
                trace_id="explicit-trace",
            )

        assert captured[0].trace_id == "explicit-trace"

    def test_payload_trace_id_is_used_when_context_empty(self) -> None:
        bus = EventBus("test-trace-payload")
        captured: list[Event] = []
        bus.subscribe(Topic.SIGNAL, captured.append)

        bus.publish(Topic.SIGNAL, {"symbol": "600000.SH", "side": "BUY", "trace_id": "payload-trace"})

        assert captured[0].trace_id == "payload-trace"

    def test_global_bus_singleton_and_override(self) -> None:
        original = get_event_bus()
        custom = EventBus("custom-global")
        try:
            assert init_event_bus(custom) is custom
            assert get_event_bus() is custom
        finally:
            init_event_bus(original)
        assert get_event_bus() is original


# ---------------------------------------------------------------------------
# 四、敏感信息过滤（SPEC §8.8：密码/token/密钥绝不落盘）
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMasking:
    @pytest.mark.parametrize(
        ("text", "leaked"),
        [
            ("password=SuperSecret123", "SuperSecret123"),
            ("token: abcdef123456", "abcdef123456"),
            ("api_key=sk-live-9999", "sk-live-9999"),
            ('{"secret": "topsecret"}', "topsecret"),
        ],
    )
    def test_mask_text_removes_secrets(self, text: str, leaked: str) -> None:
        masked = mask_text(text)

        assert leaked not in masked, f"敏感信息未被掩码：{masked!r}"
        assert MASK in masked

    @pytest.mark.unit
    def test_authorization_bearer_header_is_fully_masked(self) -> None:
        """缺陷候选（待 Lead 定夺）：``Authorization: Bearer <jwt>`` 只掩掉 ``Bearer``，JWT 尾部落盘。

        定位：``core/logging.py`` 的 ``_SECRET_TEXT_RE`` 先匹配 ``Authorization:`` 并把
        ``Bearer`` 当成“值”替换成 ``***``；随后 ``_AUTH_HEADER_RE`` 再也找不到 ``Bearer``，
        于是 ``eyJ...`` 原样保留。

        影响：SPEC §8.8 要求 token 绝不落盘；当密钥出现在**自由文本**（而非 extra 字段）时
        会部分泄露（extra 字段因 key 命中会被整体掩码，不受影响）。
        建议修复：先跑 ``_AUTH_HEADER_RE`` 再跑 ``_SECRET_TEXT_RE``，或让前者允许
        ``*** `` 前缀；属 Dev1 的 ``core/logging.py``，QA 不自行改业务代码。
        """
        masked = mask_text("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc")

        assert "eyJhbGciOiJIUzI1NiJ9.abc" not in masked, f"Bearer token 未完全掩码：{masked!r}"

    def test_mask_text_keeps_normal_text(self) -> None:
        assert mask_text("普通日志：下单成功 100 股") == "普通日志：下单成功 100 股"
        assert mask_text("") == ""

    def test_mask_sensitive_recurses_containers(self) -> None:
        payload = {
            "symbol": "600000.SH",
            "password": "p@ss",
            "nested": {"api_key": "sk-1", "items": [{"token": "t-1"}, "ok"]},
            "count": 3,
        }

        masked = mask_sensitive(payload)

        assert masked["symbol"] == "600000.SH"
        assert masked["password"] == MASK
        assert masked["nested"]["api_key"] == MASK
        assert masked["nested"]["items"][0]["token"] == MASK
        assert masked["nested"]["items"][1] == "ok"
        assert masked["count"] == 3
        assert "p@ss" not in json.dumps(masked, ensure_ascii=False)

    def test_mask_sensitive_handles_pydantic_models(self, account_factory) -> None:
        account = account_factory()

        masked = mask_sensitive(account)

        assert isinstance(masked, dict)
        assert masked["id"] == account.id


# ---------------------------------------------------------------------------
# 五、JSON Lines 格式化与文件落盘（AC-F8-01）
# ---------------------------------------------------------------------------


def _make_record(message: str, level: int = logging.INFO, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="quantdesk.test",
        level=level,
        pathname=__file__,
        lineno=42,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


@pytest.mark.unit
class TestJsonLineFormatter:
    def test_required_fields_present(self) -> None:
        record = _make_record("下单成功")
        payload = json.loads(JsonLineFormatter().format(record))

        # AC-F8-01：每条必须含 ts/level/trace_id/message
        assert {"ts", "level", "trace_id", "message"} <= set(payload)
        assert payload["level"] == "INFO"
        assert payload["message"] == "下单成功"
        assert payload["logger"] == "quantdesk.test"
        assert payload["extra"] == {}

    def test_trace_id_from_context_or_extra(self) -> None:
        with trace_context("ctx-999"):
            from_context = json.loads(JsonLineFormatter().format(_make_record("x")))
        from_extra = json.loads(JsonLineFormatter().format(_make_record("x", trace_id="explicit-888")))

        assert from_context["trace_id"] == "ctx-999"
        assert from_extra["trace_id"] == "explicit-888"

    def test_message_and_extra_are_masked(self) -> None:
        record = _make_record("登录 password=hunter2", api_key="sk-secret")

        payload = json.loads(JsonLineFormatter().format(record))

        assert "hunter2" not in payload["message"]
        assert payload["extra"]["api_key"] == MASK

    def test_extra_is_plain_dict_for_non_sensitive_fields(self) -> None:
        record = _make_record("成交", order_id="O1", quantity=100)
        payload = json.loads(JsonLineFormatter().format(record))

        assert payload["extra"] == {"order_id": "O1", "quantity": 100}


@pytest.fixture()
def app_logger(tmp_path: Path):
    """隔离的应用日志器：日志目录指向 tmp_path，用例结束 reset_logging。"""
    log_dir = tmp_path / "logs"
    logger = setup_logging(log_dir=log_dir, level="DEBUG", console=False, force=True)
    try:
        yield logger, log_dir
    finally:
        reset_logging()


@pytest.mark.integration
class TestLoggingToFile:
    def test_json_lines_file_is_valid(self, app_logger) -> None:
        logger, log_dir = app_logger

        with trace_context("trace-log-1"):
            get_logger("order_manager").info(
                "手动下单：600000.SH 买 100 股", extra={"order_id": "ORD-1"}
            )

        files = sorted(log_dir.glob("app-*.log"))
        assert files, f"未生成日志文件：{list(log_dir.iterdir())}"

        lines = [line for line in files[-1].read_text(encoding="utf-8").splitlines() if line.strip()]
        assert lines, "日志文件为空"
        records = [json.loads(line) for line in lines]  # 每行必须是合法 JSON

        target = next(record for record in records if "手动下单" in record["message"])
        assert target["level"] == "INFO"
        assert target["trace_id"] == "trace-log-1"  # trace_id 贯穿
        assert target["extra"]["order_id"] == "ORD-1"
        assert "ts" in target

    def test_sensitive_extra_never_hits_disk(self, app_logger) -> None:
        logger, log_dir = app_logger
        get_logger("settings").info("保存配置", extra={"smtp_password": "plain-text-pwd"})

        content = "\n".join(path.read_text(encoding="utf-8") for path in log_dir.glob("app-*.log"))

        assert "plain-text-pwd" not in content, "敏感字段泄露到日志文件"
        assert MASK in content

    def test_handler_file_name_contract(self, tmp_path: Path) -> None:
        handler = DailyJsonFileHandler(tmp_path, retention_days=7)
        try:
            record = _make_record("hello")
            handler.emit(record)
            files = list(tmp_path.glob("app-*.log"))
            assert len(files) == 1
            assert re.match(r"^app-\d{8}\.log$", files[0].name)
        finally:
            handler.close()

    def test_retention_deletes_old_logs(self, tmp_path: Path) -> None:
        import datetime as dt

        old = tmp_path / f"app-{(dt.date.today() - dt.timedelta(days=30)).strftime('%Y%m%d')}.log"
        old.write_text('{"message": "old"}\n', encoding="utf-8")

        handler = DailyJsonFileHandler(tmp_path, retention_days=7)
        try:
            handler.emit(_make_record("new"))
        finally:
            handler.close()

        assert not old.exists(), "超过保留期的日志应被清理"


# ---------------------------------------------------------------------------
# 六、审计流水（AC-F8-02 参数修改 before/after + 敏感字段脱敏）
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestAuditLog:
    def test_write_audit_persists_before_after_and_masks_secrets(self, test_db) -> None:
        from core.models import AuditLogORM

        set_audit_session_factory(test_db.session_factory)
        try:
            with trace_context() as tid:
                ok = write_audit(
                    AuditAction.RISK_CONFIG_UPDATE,
                    ("risk_config", "1"),
                    before={"max_total_position_pct": 80, "api_key": "sk-leak"},
                    after={"max_total_position_pct": 60},
                    actor=AuditActor.MANUAL,
                )
            assert ok is True
        finally:
            set_audit_session_factory(None)

        session = test_db.session()
        try:
            row = session.query(AuditLogORM).one()
        finally:
            session.close()

        assert row.action == AuditAction.RISK_CONFIG_UPDATE
        assert row.actor == AuditActor.MANUAL.value
        assert row.target_type == "risk_config" and row.target_id == "1"
        assert row.result == AuditResult.SUCCESS.value
        assert row.trace_id == tid

        before = json.loads(row.before_json or "{}")
        after = json.loads(row.after_json or "{}")
        assert before["max_total_position_pct"] == 80
        assert after["max_total_position_pct"] == 60
        assert before["api_key"] == MASK
        assert "sk-leak" not in (row.before_json or "")

    def test_write_audit_returns_false_when_db_unavailable(self, test_db) -> None:
        """审计写库失败不得阻断主流程：返回 False 而不是抛异常。"""
        set_audit_session_factory(test_db.session_factory)
        try:
            ok = write_audit(
                AuditAction.ORDER_PLACE,
                ("order", "O-1"),
                actor=AuditActor.MANUAL,
                session=object(),  # 故意传入非法 session → add/flush 失败
            )
        finally:
            set_audit_session_factory(None)

        assert ok is False

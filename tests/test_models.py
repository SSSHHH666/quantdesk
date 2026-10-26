"""模型层单元测试（枚举冻结值、必填校验、订单状态机、RK001–RK020 文案）。

对应验收标准：
- SPEC §5.2 枚举取值冻结（orders.status / side / source 等）
- SPEC §9.2 原因码冻结：RK001–RK020、BR001–BR005、DQ001–DQ007
- TASKS M0-10（测试骨架）+ AC-F6-02 的“中文消息非空”部分
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re

import pytest
from pydantic import ValidationError

from core.models import (
    ALL_REASON_CODE_MESSAGES,
    ORDER_ACTIVE_STATUSES,
    ORDER_CANCELLABLE_STATUSES,
    Account,
    AuditActor,
    AuditResult,
    Bar,
    Board,
    BrokerErrorCode,
    BrokerType,
    CashFlowType,
    DataQualityCode,
    Exchange,
    FeeBreakdown,
    FeeConfig,
    JobRunStatus,
    NotificationChannel,
    NotificationStatus,
    Order,
    OrderRequest,
    OrderResult,
    OrderSource,
    OrderStatus,
    OrderType,
    Position,
    ProviderStatus,
    RiskConfig,
    RiskDecision,
    RiskReasonCode,
    Side,
    Signal,
    SignalStatus,
    StrategyMode,
    SymbolKind,
    TradeStatus,
    build_client_order_id,
    describe_reason,
    normalize_side,
    suggest_reason,
)

#: 中文断言：至少包含一个汉字（防止“文案是英文/空串”蒙混过关）
HAN_RE = re.compile(r"[\u4e00-\u9fff]")


def _has_han(text: str) -> bool:
    return bool(text) and HAN_RE.search(text) is not None


# ---------------------------------------------------------------------------
# 一、枚举取值冻结（SPEC §5.2）
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFrozenEnums:
    """枚举是落库/契约字段，取值不允许悄悄改。"""

    def test_side(self) -> None:
        assert Side.BUY.value == "BUY"
        assert Side.SELL.value == "SELL"
        assert {item.value for item in Side} == {"BUY", "SELL"}

    def test_order_type(self) -> None:
        assert {item.value for item in OrderType} == {"LIMIT", "MARKET"}

    def test_order_status_values_and_helpers(self) -> None:
        assert {item.value for item in OrderStatus} == {
            "PENDING",
            "SUBMITTED",
            "PARTIALLY_FILLED",
            "FILLED",
            "CANCELLED",
            "REJECTED",
            "EXPIRED",
        }
        assert {s for s in OrderStatus if s.is_terminal} == {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }
        assert {s for s in OrderStatus if s.can_cancel} == {
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIALLY_FILLED,
        }

    def test_terminal_states_are_never_cancellable(self) -> None:
        """状态机不变式：终态 ∩ 可撤状态 = ∅；活跃状态 = 非终态。"""
        terminal = {s for s in OrderStatus if s.is_terminal}
        cancellable = {s for s in OrderStatus if s.can_cancel}
        assert terminal & cancellable == set()
        assert ORDER_CANCELLABLE_STATUSES == frozenset(cancellable)
        assert ORDER_ACTIVE_STATUSES == frozenset(set(OrderStatus) - terminal)
        for status in ORDER_CANCELLABLE_STATUSES:
            assert status in ORDER_ACTIVE_STATUSES

    def test_domain_enums(self) -> None:
        assert {item.value for item in OrderSource} == {"MANUAL", "AUTO"}
        assert {item.value for item in BrokerType} == {"PAPER", "CCXT", "QMT"}
        assert {item.value for item in SymbolKind} == {"STOCK", "INDEX"}
        assert {item.value for item in Exchange} == {"SH", "SZ", "BJ"}
        assert {item.value for item in Board} == {"MAIN", "GEM", "STAR", "BSE"}
        assert {item.value for item in TradeStatus} == {"NORMAL", "SUSPENDED", "DELISTING", "DELISTED"}
        assert {item.value for item in StrategyMode} == {"OFF", "PAPER", "AUTO"}
        assert {item.value for item in SignalStatus} == {"GENERATED", "RISK_BLOCKED", "ORDERED", "IGNORED"}
        assert {item.value for item in CashFlowType} == {
            "DEPOSIT",
            "WITHDRAW",
            "BUY",
            "SELL",
            "DIVIDEND",
            "FEE",
            "OTHER",
        }
        assert {item.value for item in AuditActor} == {"manual", "scheduler", "system", "auto"}
        assert {item.value for item in AuditResult} == {"SUCCESS", "FAILED", "DENIED"}
        assert {item.value for item in JobRunStatus} == {"SUCCESS", "FAILED", "SKIPPED"}
        assert {item.value for item in NotificationChannel} == {"DESKTOP", "EMAIL", "WEBHOOK"}
        assert {item.value for item in NotificationStatus} == {"SENT", "FAILED", "DISABLED"}
        assert {item.value for item in ProviderStatus} == {"OK", "DEGRADED", "DOWN"}


# ---------------------------------------------------------------------------
# 二、原因码冻结与中文文案（SPEC §9.2 / AC-F6-02）
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReasonCodes:
    def test_risk_codes_are_exactly_rk001_to_rk020(self) -> None:
        codes = [item.value for item in RiskReasonCode]
        assert codes == [f"RK{index:03d}" for index in range(1, 21)]
        assert len(codes) == len(set(codes)) == 20

    def test_broker_and_data_quality_codes(self) -> None:
        assert [item.value for item in BrokerErrorCode] == [f"BR{index:03d}" for index in range(1, 6)]
        assert [item.value for item in DataQualityCode] == [f"DQ{index:03d}" for index in range(1, 8)]

    @pytest.mark.parametrize("code", [f"RK{index:03d}" for index in range(1, 21)])
    def test_every_risk_code_has_chinese_message_and_suggestion(self, code: str) -> None:
        """AC-F6-02 的文案部分：每个 RK 码都有非空中文含义 + 建议动作。"""
        message = describe_reason(code)
        suggestion = suggest_reason(code)

        assert code in message, f"{code} 的中文描述必须回显原因码，实际：{message!r}"
        assert _has_han(message), f"{code} 的中文描述缺失汉字，实际：{message!r}"
        assert _has_han(suggestion), f"{code} 的建议动作缺失汉字，实际：{suggestion!r}"
        assert message not in ("", code)

    def test_message_table_covers_all_families(self) -> None:
        for code in (item.value for item in RiskReasonCode):
            assert code in ALL_REASON_CODE_MESSAGES
        for code in (item.value for item in BrokerErrorCode):
            assert code in ALL_REASON_CODE_MESSAGES
        for code in (item.value for item in DataQualityCode):
            assert code in ALL_REASON_CODE_MESSAGES

    def test_unknown_code_is_reported_not_silently_empty(self) -> None:
        assert "RK999" in describe_reason("RK999")
        assert "未知" in describe_reason("RK999")
        assert suggest_reason("RK999") == ""
        assert describe_reason(None) == ""

    def test_enum_and_string_inputs_are_interchangeable(self) -> None:
        assert describe_reason(RiskReasonCode.RK014) == describe_reason("RK014")

    def test_risk_decision_deny_carries_code_message_suggestion(self) -> None:
        decision = RiskDecision.deny("RK007", details={"actual": 23.4, "limit": 20.0})

        assert decision.allow is False
        assert decision.reason_code == "RK007"
        assert "RK007" in decision.message
        assert _has_han(decision.message)
        assert _has_han(decision.suggestion)
        # 兼容别名（BORROW_MAP P3 用 reason）
        assert decision.reason == decision.reason_code

    def test_risk_decision_pass_has_no_reason_code(self) -> None:
        decision = RiskDecision.pass_()

        assert decision.allow is True
        assert decision.reason_code is None
        assert decision.reason is None


# ---------------------------------------------------------------------------
# 三、必填校验与字段归一化
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestValidation:
    def test_order_request_requires_core_fields(self) -> None:
        base = {"account_id": "ACC-1", "symbol": "600000.SH", "side": "BUY", "quantity": 100}
        OrderRequest(**base)  # 不抛错即通过

        for missing in ("account_id", "symbol", "side", "quantity"):
            payload = dict(base)
            payload.pop(missing)
            with pytest.raises(ValidationError):
                OrderRequest(**payload)

    def test_order_request_has_no_business_validation_by_design(self) -> None:
        """设计说明：OrderRequest 只做类型校验，数量/价格合法性交给 RiskGate（避免两处口径冲突）。"""
        order = OrderRequest(account_id="ACC-1", symbol="600000.SH", side="BUY", price=-1.0, quantity=0)

        assert order.price == -1.0
        assert order.quantity == 0

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("BUY", Side.BUY),
            ("buy", Side.BUY),
            ("B", Side.BUY),
            ("LONG", Side.BUY),
            ("买", Side.BUY),
            ("买入", Side.BUY),
            ("SELL", Side.SELL),
            ("s", Side.SELL),
            ("SHORT", Side.SELL),
            ("卖", Side.SELL),
            ("卖出", Side.SELL),
            (Side.BUY, Side.BUY),
        ],
    )
    def test_side_aliases_are_normalized(self, raw: object, expected: Side) -> None:
        assert normalize_side(raw) is expected
        order = OrderRequest(account_id="ACC-1", symbol="600000.SH", side=raw, quantity=100)  # type: ignore[arg-type]
        assert order.side is expected

    @pytest.mark.parametrize("raw", ["HOLD", "", None, "ALL", 123])
    def test_invalid_side_is_rejected_with_chinese_error(self, raw: object) -> None:
        with pytest.raises(ValueError):
            normalize_side(raw)
        with pytest.raises(ValidationError) as excinfo:
            OrderRequest(account_id="ACC-1", symbol="600000.SH", side=raw, quantity=100)  # type: ignore[arg-type]
        assert "交易方向" in str(excinfo.value)

    def test_order_defaults(self) -> None:
        order = OrderRequest(account_id="ACC-1", symbol="600000.SH", side="BUY", quantity=100)

        assert order.order_type is OrderType.LIMIT
        assert order.source is OrderSource.MANUAL
        assert order.price is None
        assert order.strategy_instance_id is None

    def test_order_request_allows_extension_fields(self) -> None:
        """QDModel extra="allow"：UI/插件透传字段不应被拒绝。"""
        order = OrderRequest(
            account_id="ACC-1",
            symbol="600000.SH",
            side="BUY",
            quantity=100,
            ui_hint="手动交易页",
        )

        assert order.model_dump()["ui_hint"] == "手动交易页"

    def test_bar_rejects_negative_price(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            Bar(symbol="600000.SH", trade_date=dt.date(2026, 10, 26), open=-1, high=1, low=1, close=1)
        assert "K 线价格" in str(excinfo.value)

    def test_signal_strength_must_be_in_range(self) -> None:
        Signal(symbol="600000.SH", side="BUY", strength=0.0)
        Signal(symbol="600000.SH", side="BUY", strength=1.0)
        with pytest.raises(ValidationError) as excinfo:
            Signal(symbol="600000.SH", side="BUY", strength=1.5)
        assert "strength" in str(excinfo.value)

    def test_risk_config_percent_bounds(self) -> None:
        assert RiskConfig().max_position_per_symbol_pct == 20.0
        assert RiskConfig().max_total_position_pct == 80.0
        for field in (
            "max_position_per_symbol_pct",
            "max_total_position_pct",
            "max_daily_loss_pct",
            "max_price_deviation_pct",
            "max_single_order_value_pct",
        ):
            with pytest.raises(ValidationError):
                RiskConfig(**{field: 101.0})
            with pytest.raises(ValidationError):
                RiskConfig(**{field: -1.0})

    def test_fee_config_defaults_match_spec(self) -> None:
        fees = FeeConfig()

        assert fees.commission_rate == pytest.approx(0.00025)
        assert fees.min_commission == pytest.approx(5.0)
        assert fees.stamp_tax_rate == pytest.approx(0.0005)
        assert fees.transfer_fee_rate == pytest.approx(0.00001)
        assert fees.reg_fee_rate == pytest.approx(0.0000541)
        assert fees.slippage_bps == pytest.approx(5.0)

    @pytest.mark.parametrize("field", ["commission_rate", "stamp_tax_rate", "transfer_fee_rate", "reg_fee_rate"])
    def test_fee_rates_must_be_in_range(self, field: str) -> None:
        with pytest.raises(ValidationError):
            FeeConfig(**{field: 1.0})
        with pytest.raises(ValidationError):
            FeeConfig(**{field: -0.01})


# ---------------------------------------------------------------------------
# 四、订单状态机与派生属性
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOrderStateMachine:
    def _order(self, **overrides: object) -> Order:
        payload: dict[str, object] = {
            "id": "ORD-1",
            "account_id": "ACC-1",
            "symbol": "600000.SH",
            "side": "BUY",
            "quantity": 1000,
        }
        payload.update(overrides)
        return Order(**payload)  # type: ignore[arg-type]

    def test_default_status_is_pending(self) -> None:
        order = self._order()

        assert order.status is OrderStatus.PENDING
        assert order.filled_quantity == 0
        assert order.is_active is True

    @pytest.mark.parametrize(
        ("status", "active", "cancellable"),
        [
            (OrderStatus.PENDING, True, False),
            (OrderStatus.SUBMITTED, True, True),
            (OrderStatus.PARTIALLY_FILLED, True, True),
            (OrderStatus.FILLED, False, False),
            (OrderStatus.CANCELLED, False, False),
            (OrderStatus.REJECTED, False, False),
            (OrderStatus.EXPIRED, False, False),
        ],
    )
    def test_status_flags(self, status: OrderStatus, active: bool, cancellable: bool) -> None:
        order = self._order(status=status)

        assert order.is_active is active
        assert order.status.can_cancel is cancellable
        assert order.status.is_terminal is (not active)

    def test_remaining_quantity_never_negative(self) -> None:
        assert self._order(filled_quantity=0).remaining_quantity == 1000
        assert self._order(filled_quantity=400).remaining_quantity == 600
        assert self._order(filled_quantity=1000).remaining_quantity == 0
        assert self._order(filled_quantity=1200).remaining_quantity == 0

    def test_reject_fields_are_separate_from_message(self) -> None:
        order = self._order(status=OrderStatus.REJECTED, reject_reason="RK014", reject_message="资金不足")

        assert order.reject_reason == "RK014"
        assert _has_han(order.reject_message or "")

    def test_order_result_from_order(self) -> None:
        filled = self._order(status=OrderStatus.FILLED, filled_quantity=1000, avg_fill_price=10.01)
        result = OrderResult.from_order(filled)

        assert result.success is True
        assert result.status is OrderStatus.FILLED
        assert result.order_id == filled.id
        assert result.message

        pending = self._order(status=OrderStatus.SUBMITTED)
        assert OrderResult.from_order(pending).success is True
        assert OrderResult.from_order(pending).message == "委托已提交"


# ---------------------------------------------------------------------------
# 五、幂等键与费用明细
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestClientOrderId:
    def test_matches_documented_sha1_formula(self) -> None:
        """SPEC §8.5：sha1("strategy_instance_id|symbol|trade_date|side")。"""
        expected = hashlib.sha1("STRAT-1|600000.SH|2026-10-26|BUY".encode("utf-8")).hexdigest()

        actual = build_client_order_id("STRAT-1", "600000.SH", dt.date(2026, 10, 26), Side.BUY)

        assert actual == expected
        assert len(actual) == 40

    def test_same_inputs_same_key(self) -> None:
        args = ("STRAT-1", "600000.SH", dt.date(2026, 10, 26), Side.BUY)
        assert build_client_order_id(*args) == build_client_order_id(*args)

    def test_date_datetime_and_string_are_equivalent(self) -> None:
        day = dt.date(2026, 10, 26)
        as_date = build_client_order_id("STRAT-1", "600000.SH", day, Side.BUY)
        as_datetime = build_client_order_id("STRAT-1", "600000.SH", dt.datetime(2026, 10, 26, 14, 30), Side.BUY)
        as_string = build_client_order_id("STRAT-1", "600000.SH", "2026-10-26", Side.BUY)

        assert as_date == as_datetime == as_string

    def test_key_differs_by_side_symbol_strategy_and_day(self) -> None:
        base = build_client_order_id("STRAT-1", "600000.SH", dt.date(2026, 10, 26), Side.BUY)

        assert build_client_order_id("STRAT-1", "600000.SH", dt.date(2026, 10, 26), Side.SELL) != base
        assert build_client_order_id("STRAT-1", "000001.SZ", dt.date(2026, 10, 26), Side.BUY) != base
        assert build_client_order_id("STRAT-2", "600000.SH", dt.date(2026, 10, 26), Side.BUY) != base
        assert build_client_order_id("STRAT-1", "600000.SH", dt.date(2026, 10, 27), Side.BUY) != base

    def test_accepts_chinese_side_alias(self) -> None:
        assert build_client_order_id("STRAT-1", "600000.SH", dt.date(2026, 10, 26), "买入") == build_client_order_id(
            "STRAT-1", "600000.SH", dt.date(2026, 10, 26), Side.BUY
        )


@pytest.mark.unit
class TestAccountPositionDerived:
    def test_account_available_cash_and_position_value(self, account_factory, position_factory) -> None:
        account = account_factory(cash=60000.0, frozen_cash=1000.0, total_asset=100000.0)
        position = position_factory(quantity=1500, available_quantity=500, avg_cost=10.0, last_price=10.0)

        assert account.available_cash == pytest.approx(59000.0)
        # 持仓市值 = 总资产 - 现金 - 冻结 = 100000 - 60000 - 1000
        assert account.position_value == pytest.approx(39000.0)
        assert account.total_position_pct == pytest.approx(39.0)

        assert position.t1_locked_quantity == 1000
        assert position.unrealized_pnl == pytest.approx(0.0)
        assert position.is_empty is False

    def test_zero_division_is_guarded(self) -> None:
        account = Account(id="ACC-0", cash=0.0, total_asset=0.0)
        position = Position(account_id="ACC-0", symbol="600000.SH", quantity=0, avg_cost=0.0, last_price=0.0)

        assert account.total_position_pct == 0.0
        assert position.unrealized_pnl_pct == 0.0
        assert position.is_empty is True

    def test_fee_breakdown_total_with_slippage(self) -> None:
        fee = FeeBreakdown(commission=5.0, stamp_tax=0.0, transfer_fee=0.1, reg_fee=0.54, total=5.64, slippage_cost=10.0)

        assert fee.total_with_slippage == pytest.approx(15.64)


# ---------------------------------------------------------------------------
# 六、ORM 建表（集成：临时 SQLite，CF-13 ①）
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestOrmSchema:
    EXPECTED_TABLES = {
        "symbols",
        "trading_calendar",
        "daily_bars",
        "fundamentals",
        "adjust_events",
        "data_quality_issues",
        "strategy_instances",
        "strategy_signals",
        "backtest_runs",
        "backtest_equity",
        "backtest_trades",
        "accounts",
        "positions",
        "orders",
        "trades",
        "cash_flows",
        "equity_curve",
        "risk_config",
        "app_settings",
        "audit_logs",
        "job_runs",
        "notifications",
    }

    def test_temp_sqlite_has_all_22_tables(self, test_db) -> None:
        tables = test_db.table_names()

        assert len(self.EXPECTED_TABLES) == 22
        missing = self.EXPECTED_TABLES - tables
        assert not missing, f"临时 SQLite 建表缺失：{sorted(missing)}"

    def test_temp_sqlite_is_a_real_file_on_disk(self, test_db) -> None:
        assert test_db.path.exists()
        assert test_db.path.suffix == ".db"

    def test_cases_get_isolated_databases(self, test_db) -> None:
        """每个用例独占库文件：本用例从 0 行开始，写入后只在本库可见。"""
        from core.models import AuditLogORM

        assert test_db.count("audit_logs") == 0

        session = test_db.session()
        try:
            session.add(AuditLogORM(actor="system", action="test.isolation", result="SUCCESS"))
            session.commit()
        finally:
            session.close()

        assert test_db.count("audit_logs") == 1

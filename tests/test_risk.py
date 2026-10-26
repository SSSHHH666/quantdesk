"""风控闸门单元测试（RK001–RK020 全覆盖、fail-closed、10 步固定顺序）。

对应验收标准：
- AC-F6-01 唯一入口/调用顺序（本文件覆盖“闸门自身”部分）
- AC-F6-02 RK001–RK020 每个原因码至少 1 条用例（reason_code + 中文消息非空）
- AC-F6-03 单票上限 10 万/20%/已持 1.5 万 → 再买 1 万被 RK007
- AC-F6-04 总仓位 78% + 5% → RK008
- AC-F6-05 熔断 -3.2% → 买入 RK009、卖出放行、次日重置恢复
- AC-F6-06 价格偏离 10.50（+5%）→ RK010；10.10（+1%）→ 通过
- AC-F6-07 风控纯函数：同输入两次结果一致、不写库
- SPEC §8.6 fail-closed：风控内部异常必须拒单（RK018），绝不放行
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from core.models import (
    RISK_REASON_MESSAGES,
    Account,
    MarketSnapshot,
    OrderRequest,
    Position,
    RiskConfig,
    RiskReasonCode,
    Side,
    describe_reason as describe_reason_core,
    suggest_reason as suggest_reason_core,
)
from trading.risk import (
    CHECK_STEP_NAMES,
    DEFAULT_RISK_PARAMS,
    RiskGate,
)

# ---------------------------------------------------------------------------
# 确定性“默认世界”：全部通过风控的一张单
#   - 标的 600000.SH（沪市主板，100 股整手）
#   - 买 1000 股 @10.00 → 金额 10,000 = 总资产 10 万 × 10%（正好卡在上限内）
#   - 账户 10 万现金、无持仓、交易时段内、最新价 10.00（偏离 0%）
# ---------------------------------------------------------------------------

SYMBOL = "600000.SH"
OTHER_SYMBOL = "000001.SZ"
ACCOUNT_ID = "ACC-TEST-001"
TRADE_DATE = dt.date(2026, 10, 26)
AS_OF = dt.datetime(2026, 10, 26, 14, 30, 0)
PRICE = 10.0
QUANTITY = 1000


def _gate(**overrides: Any) -> RiskGate:
    """默认风控闸门（SPEC §9.1 安全默认值），可关键字覆盖。"""
    return RiskGate(RiskConfig(**overrides))


def _order(**overrides: Any) -> OrderRequest:
    payload: dict[str, Any] = {
        "account_id": ACCOUNT_ID,
        "symbol": SYMBOL,
        "side": Side.BUY,
        "price": PRICE,
        "quantity": QUANTITY,
    }
    payload.update(overrides)
    return OrderRequest(**payload)


def _account(**overrides: Any) -> Account:
    payload: dict[str, Any] = {"id": ACCOUNT_ID, "cash": 100000.0, "total_asset": 100000.0}
    payload.update(overrides)
    return Account(**payload)


def _position(**overrides: Any) -> Position:
    payload: dict[str, Any] = {
        "account_id": ACCOUNT_ID,
        "symbol": SYMBOL,
        "quantity": 1000,
        "available_quantity": 1000,
        "avg_cost": PRICE,
        "last_price": PRICE,
    }
    payload.update(overrides)
    return Position(**payload)


def _market(*, latest_price: float | None = PRICE, **overrides: Any) -> MarketSnapshot:
    """行情快照；``latest_price=None`` 表示“拿不到最新价”（用于 RK006）。

    默认带**新鲜**的 ``price_times`` + ``as_of``：风控第 4 步已改为 fail-closed
    （缺 price_times 即 RK006 拒单），因此“合法世界”必须提供快照时间。
    """
    payload: dict[str, Any] = {
        "trade_date": TRADE_DATE,
        "is_trading_time": True,
        "latest_prices": {} if latest_price is None else {SYMBOL: float(latest_price)},
        "price_times": {SYMBOL: AS_OF},
        "as_of": AS_OF,
    }
    payload.update(overrides)
    return MarketSnapshot(**payload)


def _case(
    *,
    gate: RiskGate | None = None,
    order: OrderRequest | None = None,
    account: Account | None = None,
    positions: list[Position] | None = None,
    market: MarketSnapshot | None = None,
) -> dict[str, Any]:
    return {
        "gate": gate or _gate(),
        "order": order if order is not None else _order(),
        "account": account if account is not None else _account(),
        "positions": positions if positions is not None else [],
        "market": market if market is not None else _market(),
    }


def _check(case: dict[str, Any]):
    return case["gate"].check(case["order"], case["account"], case["positions"], case["market"])


def _has_han(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in (text or ""))


# ---------------------------------------------------------------------------
# 一、基线：默认世界必须通过
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_default_world_passes() -> None:
    decision = _check(_case())

    assert decision.allow is True, f"基线用例应通过风控，实际被拒：{decision.reason_code} {decision.message}"
    assert decision.reason_code is None
    assert decision.message == "风控通过"


# ---------------------------------------------------------------------------
# 二、RK001–RK020 逐个覆盖（AC-F6-02）
# ---------------------------------------------------------------------------

#: (原因码, 构造器)。每个构造器返回 _case(...) 的参数字典，且**只**触发对应那一条规则。
RK_CASES: list[tuple[str, Any]] = [
    # 1 参数合法性 ---------------------------------------------------------
    (
        "RK017",
        lambda: _case(order=_order(quantity=150)),  # 主板 150 股非整手
    ),
    (
        "RK018",
        lambda: _case(order=_order(quantity=0)),  # 数量必须为正
    ),
    (
        "RK011",
        lambda: _case(
            market=_market(price_limits={SYMBOL: (11.0, 9.0)}),
            order=_order(price=11.5, quantity=500),  # 高于涨停价 11.00
        ),
    ),
    # 2 交易时段 -----------------------------------------------------------
    (
        "RK005",
        lambda: _case(market=_market(is_trading_time=False)),
    ),
    # 3 标的可交易性 -------------------------------------------------------
    (
        "RK001",
        lambda: _case(market=_market(universe=[OTHER_SYMBOL])),
    ),
    (
        "RK002",
        lambda: _case(market=_market(blacklist=[SYMBOL])),  # ST/退市整理/用户禁用
    ),
    (
        "RK003",
        lambda: _case(market=_market(suspended=[SYMBOL])),
    ),
    (
        "RK004",
        lambda: _case(market=_market(listed_days={SYMBOL: 30})),  # 上市 30 < 阈值 60
    ),
    # 4 行情新鲜度 / 6 价格可用性 -----------------------------------------
    (
        "RK006",
        lambda: _case(market=_market(latest_price=None)),  # 拿不到最新价 → 拒单
    ),
    # 5 重复订单 -----------------------------------------------------------
    (
        "RK019",
        lambda: _case(
            order=_order(client_order_id="COID-DUP-001"),
            market=_market(existing_client_order_ids={"COID-DUP-001"}),
        ),
    ),
    # 6 价格偏离 -----------------------------------------------------------
    (
        "RK010",
        lambda: _case(order=_order(price=10.5, quantity=500)),  # 偏离 +5% > 2%
    ),
    # 7 单笔金额与频率 -----------------------------------------------------
    (
        "RK012",
        lambda: _case(order=_order(quantity=1500)),  # 15,000 = 总资产 15% > 单笔 10%
    ),
    (
        "RK013",
        lambda: _case(
            market=_market(
                as_of=AS_OF,
                recent_order_times=[AS_OF - dt.timedelta(seconds=5 * index) for index in range(10)],
            )
        ),
    ),
    # 8 账户状态 -----------------------------------------------------------
    (
        "RK009",
        lambda: _case(
            account=_account(cash=96800.0, total_asset=96800.0),
            order=_order(quantity=900),  # 9000 ≤ 9680（单笔 10%），先过第 7 步
            market=_market(daily_open_asset=100000.0),  # 当日 -3.2% ≤ -3.0%
        ),
    ),
    (
        "RK020",
        lambda: _case(account=_account(enabled=False)),
    ),
    # 9 资金与持仓 ---------------------------------------------------------
    (
        "RK014",
        lambda: _case(account=_account(cash=1000.0)),  # 买 1 万只够 1000
    ),
    (
        "RK015",
        lambda: _case(order=_order(side=Side.SELL)),  # 无持仓却卖出
    ),
    (
        "RK016",
        lambda: _case(
            order=_order(side=Side.SELL),
            positions=[_position(quantity=1000, available_quantity=0)],  # 今日买入不可卖
        ),
    ),
    # 10 仓位额度 ----------------------------------------------------------
    (
        "RK007",
        lambda: _case(
            account=_account(cash=85000.0, total_asset=100000.0),
            positions=[_position(quantity=1500, available_quantity=1500)],
        ),
    ),
    (
        "RK008",
        lambda: _case(
            account=_account(cash=22000.0, total_asset=100000.0),
            positions=[
                _position(symbol="600001.SH", quantity=1950, available_quantity=1950),
                _position(symbol="600002.SH", quantity=1950, available_quantity=1950),
                _position(symbol="600003.SH", quantity=1950, available_quantity=1950),
                _position(symbol="600004.SH", quantity=1950, available_quantity=1950),
            ],
            order=_order(quantity=500),  # 再买 5,000 → 总仓位 83%
        ),
    ),
]


@pytest.mark.unit
def test_rk_case_table_covers_every_reason_code() -> None:
    """用例表必须覆盖 RK001–RK020 全集（防止后续只增不改导致漏测）。"""
    all_codes = [code.value for code in RiskReasonCode]
    covered = [code for code, _ in RK_CASES]

    assert sorted(covered) == sorted(all_codes), (
        f"覆盖不全：缺 {sorted(set(all_codes) - set(covered))}，"
        f"多 {sorted(set(covered) - set(all_codes))}"
    )
    assert len(covered) == len(set(covered)) == 20


@pytest.mark.unit
@pytest.mark.parametrize(("expected_code", "builder"), RK_CASES, ids=[code for code, _ in RK_CASES])
def test_each_reason_code_is_triggered(expected_code: str, builder: Any) -> None:
    """AC-F6-02：每个原因码都有可复现的拒绝场景 + 中文消息非空。"""
    decision = _check(builder())

    assert decision.allow is False, f"{expected_code} 场景竟被放行"
    assert decision.reason_code == expected_code
    # 中文消息必须包含原因码与建议动作（UI 直接展示给用户）
    assert expected_code in decision.message
    assert _has_han(decision.message)
    assert decision.details.get("suggestion")
    # checks 明细最后一条即本次拒绝点
    last_check = decision.checks[-1]
    assert last_check["reason_code"] == expected_code
    assert last_check["passed"] is False
    assert last_check["step"] in range(1, 11)


@pytest.mark.unit
def test_reason_code_names_match_spec() -> None:
    """原因码常量名与 SPEC §9.2 一致（唯一来源：``core.models.RISK_REASON_MESSAGES``）。"""
    expected = {
        "RK001": "SYMBOL_NOT_IN_UNIVERSE",
        "RK002": "SYMBOL_BLACKLISTED",
        "RK003": "SYMBOL_SUSPENDED",
        "RK004": "LISTED_TOO_SHORT",
        "RK005": "MARKET_CLOSED",
        "RK006": "DATA_STALE",
        "RK007": "SINGLE_POSITION_LIMIT",
        "RK008": "TOTAL_POSITION_LIMIT",
        "RK009": "DAILY_LOSS_LIMIT",
        "RK010": "PRICE_DEVIATION",
        "RK011": "PRICE_OUT_OF_LIMIT",
        "RK012": "ORDER_VALUE_LIMIT",
        "RK013": "ORDER_RATE_LIMIT",
        "RK014": "INSUFFICIENT_CASH",
        "RK015": "INSUFFICIENT_POSITION",
        "RK016": "T1_UNAVAILABLE",
        "RK017": "LOT_SIZE_INVALID",
        "RK018": "PARAM_INVALID",
        "RK019": "DUPLICATE_ORDER",
        "RK020": "ACCOUNT_DISABLED",
    }
    for code, name in expected.items():
        assert RISK_REASON_MESSAGES[code]["name"] == name, f"{code} 常量名与 SPEC 不一致"


# ---------------------------------------------------------------------------
# 三、10 步固定顺序（SPEC §8.6）
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_there_are_exactly_ten_steps_with_chinese_names() -> None:
    assert len(CHECK_STEP_NAMES) == 10
    assert all(_has_han(name) for name in CHECK_STEP_NAMES)


@pytest.mark.unit
def test_earlier_step_wins_when_multiple_rules_violated() -> None:
    """同时违反多条规则时，必须由序号更小的步骤拒绝（顺序不可乱）。"""
    # 违反：RK017（第 1 步整手）+ RK010（第 6 步偏离）+ RK014（第 9 步资金）
    case = _case(
        account=_account(cash=100.0),
        order=_order(quantity=150, price=11.0),
    )
    decision = _check(case)

    assert decision.reason_code == "RK017"
    assert decision.details["step"] == 1
    # 第 1 步先记一条“参数校验通过”，再由整手规则拒绝——全部落在第 1 步
    assert {check["step"] for check in decision.checks} == {1}
    assert decision.checks[-1]["rule"] == "lot_size"

    # 整手合法后，同一张单会在第 9 步（资金）被拒，而不是第 6 步
    case2 = _case(account=_account(cash=100.0), order=_order(quantity=100, price=PRICE))
    decision2 = _check(case2)

    assert decision2.reason_code == "RK014"
    assert decision2.details["step"] == 9


@pytest.mark.unit
def test_steps_are_monotonic_and_named_consistently() -> None:
    """checks 明细必须按步骤升序排列，且 step_name 与 CHECK_STEP_NAMES 一致。"""
    decision = _check(_case())

    assert decision.allow is True
    steps = [check["step"] for check in decision.checks]
    assert steps == sorted(steps), f"checks 步骤未按升序排列：{steps}"
    assert set(steps) == set(range(1, 11)), f"未覆盖全部 10 步：{sorted(set(steps))}"
    for check in decision.checks:
        assert check["step_name"] == CHECK_STEP_NAMES[check["step"] - 1]
    assert decision.checks[-1]["rule"] == "final"


# ---------------------------------------------------------------------------
# 四、fail-closed（SPEC §8.6：宁可拒单，绝不放行）
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_internal_error_is_rejected_not_raised() -> None:
    """风控内部异常（这里用不可迭代的持仓对象制造）必须转成 RK018 拒单。"""
    gate = _gate()

    decision = gate.check(_order(), _account(), object(), _market())  # type: ignore[arg-type]

    assert decision.allow is False
    assert decision.reason_code == "RK018"
    assert "fail-closed" in decision.message
    assert "内部异常" in decision.message
    assert decision.checks[0]["rule"] == "internal_error"


@pytest.mark.unit
@pytest.mark.parametrize(
    "kwargs",
    [
        {"order": None},
        {"account": None},
        {"positions": None},
        {"market": None},
    ],
    ids=["order=None", "account=None", "positions=None", "market=None"],
)
def test_missing_inputs_fail_closed(kwargs: dict[str, Any]) -> None:
    gate = _gate()
    base = {"order": _order(), "account": _account(), "positions": [], "market": _market()}
    base.update(kwargs)

    decision = gate.check(base["order"], base["account"], base["positions"], base["market"])

    assert decision.allow is False
    assert decision.reason_code == "RK018"


# ---------------------------------------------------------------------------
# 五、AC-F6-03 / F6-04：仓位上限的精确数值
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ac_f6_03_single_position_limit_numbers() -> None:
    """总资产 10 万、单票上限 20%、已持 1.5 万 → 再买 1 万被 RK007（允许额度 5,000）。"""
    decision = _check(
        _case(
            account=_account(cash=85000.0, total_asset=100000.0),
            positions=[_position(quantity=1500, available_quantity=1500)],
        )
    )

    assert decision.reason_code == "RK007"
    assert decision.details["rule"] == "single_position_limit"
    assert decision.details["actual"] == pytest.approx(25.0)  # 买入后 25%
    assert decision.details["limit"] == pytest.approx(20.0)
    assert "500 股" in decision.message  # 超出 5,000 元 ≈ 500 股


@pytest.mark.unit
def test_ac_f6_04_total_position_limit_numbers() -> None:
    """已有持仓 78%，再买 5% → RK008（83% > 80%）。"""
    decision = _check(
        _case(
            account=_account(cash=22000.0, total_asset=100000.0),
            positions=[
                _position(symbol="600001.SH", quantity=1950, available_quantity=1950),
                _position(symbol="600002.SH", quantity=1950, available_quantity=1950),
                _position(symbol="600003.SH", quantity=1950, available_quantity=1950),
                _position(symbol="600004.SH", quantity=1950, available_quantity=1950),
            ],
            order=_order(quantity=500),
        )
    )

    assert decision.reason_code == "RK008"
    assert decision.details["rule"] == "total_position_limit"
    assert decision.details["actual"] == pytest.approx(83.0)
    assert decision.details["limit"] == pytest.approx(80.0)


# ---------------------------------------------------------------------------
# 六、AC-F6-05：熔断只挡买入、卖出放行、次日重置恢复
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ac_f6_05_daily_loss_fuse_blocks_buy_but_not_sell() -> None:
    market = _market(daily_open_asset=100000.0)
    account = _account(cash=96800.0, total_asset=96800.0)

    buy = _check(_case(account=account, market=market, order=_order(quantity=900)))
    assert buy.reason_code == "RK009"
    assert buy.details["actual"] == pytest.approx(-3.2, abs=1e-6)

    sell = _check(
        _case(
            account=account,
            market=market,
            order=_order(side=Side.SELL, quantity=100),
            positions=[_position(quantity=1000, available_quantity=1000)],
        )
    )
    assert sell.allow is True, f"熔断期间卖出应放行，实际：{sell.reason_code} {sell.message}"

    # 次日：盘前重新记录基线（开盘总资产 = 当日总资产）→ 熔断解除
    next_day_market = _market(daily_open_asset=96800.0)
    buy_next_day = _check(_case(account=account, market=next_day_market, order=_order(quantity=900)))
    assert buy_next_day.allow is True, f"次日买入应恢复，实际：{buy_next_day.reason_code} {buy_next_day.message}"


@pytest.mark.unit
def test_fused_flag_short_circuits_buy_only() -> None:
    fused_market = _market(fused=True)

    buy = _check(_case(market=fused_market))
    assert buy.reason_code == "RK009"

    sell = _check(
        _case(
            market=fused_market,
            order=_order(side=Side.SELL, quantity=100),
            positions=[_position(quantity=1000, available_quantity=1000)],
        )
    )
    assert sell.allow is True


# ---------------------------------------------------------------------------
# 七、AC-F6-06：价格偏离
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ac_f6_06_price_deviation_boundaries() -> None:
    """最新价 10.00：限价 10.50（+5%）被拒 RK010；限价 10.10（+1%）通过。"""
    rejected = _check(_case(order=_order(price=10.50, quantity=500)))
    assert rejected.reason_code == "RK010"
    assert rejected.details["limit"] == pytest.approx(2.0)

    accepted = _check(_case(order=_order(price=10.10, quantity=500)))
    assert accepted.allow is True, f"偏离 1% 应通过，实际：{accepted.reason_code} {accepted.message}"

    # 恰好 2.00% 的边界：不超过上限（含等号）
    boundary = _check(_case(order=_order(price=10.20, quantity=500)))
    assert boundary.allow is True


# ---------------------------------------------------------------------------
# 八、AC-F6-07：纯函数 + 不写库
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ac_f6_07_same_input_same_output_and_no_db_writes(test_db) -> None:
    """同一输入调用两次结果完全一致；且不触碰数据库（用临时 SQLite 断言）。"""
    gate = _gate()
    case = _case(order=_order(quantity=1500))  # RK012

    before = test_db.count("audit_logs") + test_db.count("orders") + test_db.count("risk_config")
    first = _check(case)
    second = _check(case)
    after = test_db.count("audit_logs") + test_db.count("orders") + test_db.count("risk_config")

    assert first.model_dump() == second.model_dump()
    assert before == after == 0, "风控 check() 不应产生任何数据库写入"
    assert first.reason_code == second.reason_code == "RK012"


@pytest.mark.unit
def test_gate_params_are_frozen_after_construction() -> None:
    gate = _gate()

    with pytest.raises(TypeError):
        gate.params["max_total_position_pct"] = 100.0  # type: ignore[index]

    assert gate.params["max_total_position_pct"] == DEFAULT_RISK_PARAMS["max_total_position_pct"]


# ---------------------------------------------------------------------------
# 九、开关与降级行为
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_risk_disabled_passes_with_warning_message() -> None:
    decision = _check(_case(gate=_gate(enabled=False), order=_order(quantity=150)))

    assert decision.allow is True
    assert "关闭" in decision.message
    assert decision.checks[0]["skipped"] is True


@pytest.mark.unit
def test_market_order_is_denied_by_default_and_allowed_when_configured() -> None:
    market_order = _order(order_type="MARKET", price=None, quantity=1000)

    denied = _check(_case(order=market_order))
    assert denied.reason_code == "RK018"
    assert "市价单" in denied.message

    allowed_case = _case(gate=_gate(allow_market_order=True), order=market_order)
    assert _check(allowed_case).allow is True


@pytest.mark.unit
def test_auto_order_needs_explicit_authorization() -> None:
    """AUTO 单需显式授权：``allow_auto_orders`` 默认 False（config.yaml automation.enabled=false）。

    口径（P0-2 修复后）：授权开关是 ``allow_auto_orders``（由 ``automation.enabled`` 映射），
    行情快照 ``automation_enabled=False`` 可做运行期临时关闭（优先级更高）。
    """
    auto_order = _order(source="AUTO")

    denied = _check(_case(gate=_gate(), order=auto_order))
    assert denied.reason_code == "RK020"
    assert "自动化" in denied.message

    # 手动单不受自动化开关影响
    manual = _check(_case(gate=_gate()))
    assert manual.allow is True

    # 显式授权后放行
    authorized = _check(_case(gate=_gate(allow_auto_orders=True), order=auto_order))
    assert authorized.allow is True, f"显式授权后自动单应放行：{authorized.reason_code} {authorized.message}"

    # 授权但运行期关闭 → 仍拒绝
    runtime_off = _check(
        _case(
            gate=_gate(allow_auto_orders=True),
            order=auto_order,
            market=_market(automation_enabled=False),
        )
    )
    assert runtime_off.reason_code == "RK020"


@pytest.mark.unit
def test_stale_snapshot_is_rejected_at_step_four() -> None:
    """行情超过 stale_threshold_hours → RK006（第 4 步，早于价格偏离）。"""
    stale_time = AS_OF - dt.timedelta(hours=30)
    decision = _check(
        _case(
            market=_market(as_of=AS_OF, price_times={SYMBOL: stale_time}, stale_threshold_hours=24.0),
            order=_order(quantity=1000, price=10.5),  # 同时有偏离，但第 4 步先拒
        )
    )

    assert decision.reason_code == "RK006"
    assert decision.details["step"] == 4


@pytest.mark.unit
def test_delisting_symbol_is_blacklisted() -> None:
    """退市整理期同样按 RK002 处理（SPEC §4.7）。"""
    decision = _check(_case(market=_market(delisting=[SYMBOL])))

    assert decision.reason_code == "RK002"
    assert "退市" in decision.message


@pytest.mark.unit
def test_min_order_amount_blocks_tiny_buy() -> None:
    """低于 trading.min_order_amount（默认 1,000 元）→ RK012，防止最低佣金侵蚀。"""
    decision = _check(_case(market=_market(latest_price=5.0), order=_order(price=5.0, quantity=100)))

    assert decision.reason_code == "RK012"
    assert "最小下单金额" in decision.message


@pytest.mark.unit
def test_sell_side_is_not_limited_by_position_caps() -> None:
    """卖出允许减仓：仓位上限只对买入生效。"""
    decision = _check(
        _case(
            account=_account(cash=1000.0, total_asset=100000.0),
            positions=[_position(quantity=1000, available_quantity=1000)],
            order=_order(side=Side.SELL, quantity=100),
        )
    )

    assert decision.allow is True, f"卖出应放行，实际：{decision.reason_code} {decision.message}"


@pytest.mark.unit
def test_sell_is_exempt_from_single_order_value_cap() -> None:
    """Lead 裁决口径：``max_single_order_value_pct`` 只约束买入，卖出不受单笔金额上限限制。

    场景：总资产 10 万、已持有 2,000 股，卖出 1,500 股（金额 15,000 = 15%）——
    卖出是减仓、不增加风险敞口，不应被 RK012 拦截。
    """
    decision = _check(
        _case(
            account=_account(cash=20000.0, total_asset=100000.0),
            positions=[_position(quantity=2000, available_quantity=2000)],
            order=_order(side=Side.SELL, quantity=1500),
        )
    )

    assert decision.allow is True, (
        "卖出不应受 max_single_order_value_pct 约束（Lead 裁决），"
        f"实际被拒：{decision.reason_code} {decision.message}"
    )


# ---------------------------------------------------------------------------
# 十、文案与原因码表一致性
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_reason_table_covers_all_codes_with_chinese_text() -> None:
    """文案唯一来源：``core.models.RISK_REASON_MESSAGES``（20 条齐全、中文齐全）。"""
    assert len(RISK_REASON_MESSAGES) == 20
    for code in (item.value for item in RiskReasonCode):
        info = RISK_REASON_MESSAGES[code]
        assert info["name"], f"{code} 缺常量名"
        assert _has_han(info["meaning"]), f"{code} 缺中文含义"
        assert _has_han(info["suggestion"]), f"{code} 缺中文建议"

        # core.models 的翻译函数必须回显原因码与建议
        text = describe_reason_core(code)
        assert code in text and _has_han(text)
        assert _has_han(suggest_reason_core(code))


# ---------------------------------------------------------------------------
# 十一、P0-2 回归：默认构造的 RiskGate() 必须对自动单 fail-closed
#
# 缺陷现场（2026-10-25，pm-quant 报出）：``RiskGate()``（不传 config）用
# ``DEFAULT_RISK_PARAMS["automation_enabled"] = None``，第 8 步把 None 当“未提供”跳过，
# 于是 ``OrderSource.AUTO`` 订单被放行（fail-open）。
# 而 ``RiskGate(RiskConfig())`` 因 RiskConfig 默认 False 却能正确拒绝 —— 两条构造路径
# 口径不一致，属安全默认值缺失。
# 正确行为：**默认拒绝**，只有显式开启（构造配置 True 或行情快照 True）才允许自动单。
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAutoOrderFailClosedRegression:
    """P0-2 回归：默认构造 + AUTO 订单必须 RK020；显式开启后才允许。"""

    def _auto_order(self) -> OrderRequest:
        return _order(source="AUTO")

    def test_default_gate_rejects_auto_order(self) -> None:
        """``RiskGate()`` 不给任何配置时，AUTO 订单必须被 RK020 拒绝（不得 fail-open）。"""
        decision = RiskGate().check(self._auto_order(), _account(), [], _market())  # 注意：默认构造

        assert decision.allow is False, (
            "P0-2：默认 RiskGate() 放行了自动单（fail-open）；"
            f"checks 第 8 步={[c for c in decision.checks if c['step'] == 8]}"
        )
        assert decision.reason_code == "RK020"
        assert "自动化" in decision.message

    def test_default_gate_still_allows_manual_order(self) -> None:
        """修 P0-2 不能让手动单误伤：默认构造下 MANUAL 订单照常通过。"""
        decision = RiskGate().check(_order(), _account(), [], _market())

        assert decision.allow is True, f"手动单不应被自动化开关拦截：{decision.reason_code} {decision.message}"

    def test_explicit_authorization_allows_auto_order(self) -> None:
        """显式授权（``allow_auto_orders=True``，通常由 config.yaml automation.enabled=true 映射）后放行。"""
        decision = RiskGate(RiskConfig(allow_auto_orders=True)).check(
            self._auto_order(), _account(), [], _market()
        )

        assert decision.allow is True, f"显式授权后自动单应放行：{decision.reason_code} {decision.message}"

    def test_runtime_switch_off_beats_authorization(self) -> None:
        """运行期临时关闭优先级更高：已授权 + 行情快照 automation_enabled=False → RK020。"""
        authorized_gate = RiskGate(RiskConfig(allow_auto_orders=True))
        decision = authorized_gate.check(
            self._auto_order(), _account(), [], _market(automation_enabled=False)
        )

        assert decision.allow is False
        assert decision.reason_code == "RK020"
        assert "自动化" in decision.message

    def test_explicit_disabled_blocks_even_with_default_gate(self) -> None:
        """对照：默认构造 + market 显式关闭 → 必须拒绝，确保修复不引入反例。"""
        for gate in (RiskGate(), RiskGate(RiskConfig(allow_auto_orders=False))):
            decision = gate.check(self._auto_order(), _account(), [], _market(automation_enabled=False))
            assert decision.allow is False
            assert decision.reason_code == "RK020"

    def test_risk_gate_disabled_still_blocks_unauthorized_auto_order(self) -> None:
        """风控总开关关闭也不能顺手放行自动单（否则“一关风控就自动交易”）。"""
        decision = RiskGate(RiskConfig(enabled=False)).check(
            self._auto_order(), _account(), [], _market(automation_enabled=False)
        )

        assert decision.allow is False
        assert decision.reason_code == "RK020"

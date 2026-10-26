"""A 股交易规则与撮合单元测试（费用精确值、涨跌停、整手、T+1、停牌）。

对应验收标准：
- AC-F3-01 涨停日买入不可成交 / 跌停日卖出不可成交，并记录原因
- AC-F3-02 T 日买入 T 日卖出被拒（T+1），T+1 日卖出成功
- AC-F3-03 费用精确性：买 1000 股 @10.00 → 佣金 5.00 / 过户费 0.10 / 规费 0.54 / 合计 5.64；
             卖 1000 股 @10.00 → 合计 10.64（容差 0.01）
- AC-F3-04 整手校验：买 150 股主板被拒；买 200 股科创板通过；卖 0 股被拒
- SPEC §4.3 涨跌停幅度：主板 10% / 主板 ST 5% / 创业板 20% / 科创板 20% / 北交所 30%
"""

from __future__ import annotations

import copy
import datetime as dt

import pytest

from backtest.broker_sim import (
    BOARD_BSE,
    BOARD_GEM,
    BOARD_MAIN,
    BOARD_STAR,
    DEFAULT_SLIPPAGE_BPS,
    FeeModel,
    SimBroker,
    apply_slippage,
    reason_message,
)
from core.models import Bar, FeeBreakdown, Position, Side

TRADE_DATE = dt.date(2026, 10, 26)
MAIN_SYMBOL = "600000.SH"
STAR_SYMBOL = "688001.SH"
GEM_SYMBOL = "300001.SZ"
BSE_SYMBOL = "830799.BJ"

#: 费用容差（AC-F3-03 规定 0.01 元）
FEE_TOL = 0.01


def _bar(
    symbol: str = MAIN_SYMBOL,
    *,
    open: float = 10.0,
    high: float = 10.5,
    low: float = 9.5,
    close: float = 10.2,
    prev_close: float | None = 10.0,
    is_suspended: bool = False,
    board: str | None = None,
) -> Bar:
    payload: dict[str, object] = {
        "symbol": symbol,
        "trade_date": TRADE_DATE,
        "open": open,
        "high": high,
        "low": low,
        "close": close,
        "prev_close": prev_close,
        "is_suspended": is_suspended,
        "volume": 1_000_000.0,
    }
    if board is not None:
        payload["board"] = board
    return Bar(**payload)  # type: ignore[arg-type]


def _order(
    *,
    order_id: str = "ORD-1",
    symbol: str = MAIN_SYMBOL,
    side: str = "BUY",
    price: float | None = 10.0,
    quantity: int = 1000,
    order_type: str = "LIMIT",
    **extra: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": order_id,
        "symbol": symbol,
        "side": side,
        "price": price,
        "quantity": quantity,
        "order_type": order_type,
    }
    payload.update(extra)
    return payload


def _position(symbol: str = MAIN_SYMBOL, quantity: int = 1000, available: int = 1000) -> Position:
    return Position(account_id="ACC-1", symbol=symbol, quantity=quantity, available_quantity=available)


# ---------------------------------------------------------------------------
# 一、费用模型（AC-F3-03：数值精确且以单测锁定）
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFeeModel:
    def test_buy_1000_at_10_fee_breakdown(self) -> None:
        """买 1,000 股 @10.00：佣金 5.00、过户费 0.10、规费 0.54、合计 5.64。"""
        fee = FeeModel().calculate("BUY", 10.00, 1000)

        assert isinstance(fee, FeeBreakdown)
        assert fee.commission == pytest.approx(5.00, abs=FEE_TOL)
        assert fee.stamp_tax == pytest.approx(0.0, abs=FEE_TOL)
        assert fee.transfer_fee == pytest.approx(0.10, abs=FEE_TOL)
        assert fee.reg_fee == pytest.approx(0.54, abs=FEE_TOL)
        assert fee.total == pytest.approx(5.64, abs=FEE_TOL)

    def test_sell_1000_at_10_fee_breakdown(self) -> None:
        """卖 1,000 股 @10.00：佣金 5.00、印花税 5.00、过户费 0.10、规费 0.54、合计 10.64。"""
        fee = FeeModel().calculate("SELL", 10.00, 1000)

        assert fee.commission == pytest.approx(5.00, abs=FEE_TOL)
        assert fee.stamp_tax == pytest.approx(5.00, abs=FEE_TOL)
        assert fee.transfer_fee == pytest.approx(0.10, abs=FEE_TOL)
        assert fee.reg_fee == pytest.approx(0.54, abs=FEE_TOL)
        assert fee.total == pytest.approx(10.64, abs=FEE_TOL)

    def test_stamp_tax_only_on_sell(self) -> None:
        assert FeeModel().calculate("BUY", 10.0, 1000).stamp_tax == 0.0
        assert FeeModel().calculate("SELL", 10.0, 1000).stamp_tax > 0.0

    def test_commission_floor_of_five_yuan(self) -> None:
        """小额成交佣金按最低 5 元收（而不是按万 2.5 算出的 0.5 元）。"""
        small = FeeModel().calculate("BUY", 10.0, 200)  # 成交额 2,000 → 万 2.5 = 0.5 元
        large = FeeModel().calculate("BUY", 100.0, 1000)  # 成交额 100,000 → 万 2.5 = 25 元

        assert small.commission == pytest.approx(5.00, abs=FEE_TOL)
        assert large.commission == pytest.approx(25.00, abs=FEE_TOL)

    def test_large_amount_full_breakdown(self) -> None:
        """100,000 元买入：佣金 25、过户费 1.00、规费 5.41 → 31.41；卖出再加印花税 50 → 81.41。"""
        buy = FeeModel().calculate("BUY", 100.0, 1000)
        sell = FeeModel().calculate("SELL", 100.0, 1000)

        assert buy.commission == pytest.approx(25.00, abs=FEE_TOL)
        assert buy.transfer_fee == pytest.approx(1.00, abs=FEE_TOL)
        assert buy.reg_fee == pytest.approx(5.41, abs=FEE_TOL)
        assert buy.total == pytest.approx(31.41, abs=FEE_TOL)
        assert sell.stamp_tax == pytest.approx(50.00, abs=FEE_TOL)
        assert sell.total == pytest.approx(81.41, abs=FEE_TOL)

    def test_each_fee_rounded_independently(self) -> None:
        """SPEC §4.5：每项独立四舍五入到 0.01 再累加（规费 0.541 → 0.54，不是 0.541）。"""
        fee = FeeModel().calculate("BUY", 10.0, 1000)

        assert fee.reg_fee == 0.54  # 精确到分
        assert round(fee.commission + fee.transfer_fee + fee.reg_fee, 2) == fee.total

    def test_include_reg_fee_in_commission(self, fee_config) -> None:
        from core.models import FeeConfig

        merged = FeeModel(FeeConfig(include_reg_fee_in_commission=True)).calculate("BUY", 100.0, 1000)
        separate = FeeModel(fee_config).calculate("BUY", 100.0, 1000)

        assert merged.reg_fee == 0.0
        assert merged.commission == pytest.approx(separate.commission + separate.reg_fee, abs=FEE_TOL)
        assert merged.total == pytest.approx(separate.total, abs=FEE_TOL)

    def test_amount(self) -> None:
        assert FeeModel.amount(10.0, 1000) == pytest.approx(10000.0)
        assert FeeModel.amount(3.33, 300) == pytest.approx(999.0)

    @pytest.mark.parametrize(
        ("side", "price", "quantity"),
        [("BUY", 0.0, 100), ("BUY", -1.0, 100), ("BUY", 10.0, 0), ("BUY", 10.0, -100), ("HOLD", 10.0, 100)],
    )
    def test_invalid_inputs_raise_value_error(self, side: str, price: float, quantity: int) -> None:
        with pytest.raises(ValueError):
            FeeModel().calculate(side, price, quantity)


# ---------------------------------------------------------------------------
# 二、涨跌停幅度与涨跌停价（SPEC §4.3）
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPriceLimits:
    @pytest.mark.parametrize(
        ("board", "is_st", "expected_pct"),
        [
            (BOARD_MAIN, False, 0.10),
            (BOARD_MAIN, True, 0.05),
            (BOARD_GEM, False, 0.20),
            (BOARD_GEM, True, 0.20),  # 创业板 ST 仍 20%（全面注册制口径）
            (BOARD_STAR, False, 0.20),
            (BOARD_STAR, True, 0.20),
            (BOARD_BSE, False, 0.30),
            (BOARD_BSE, True, 0.30),
        ],
    )
    def test_limit_pct_by_board(self, board: str, is_st: bool, expected_pct: float) -> None:
        from backtest.broker_sim import AStockRules

        assert AStockRules.limit_pct(board, is_st) == pytest.approx(expected_pct)

    @pytest.mark.parametrize(
        ("board", "is_st", "expected"),
        [
            (BOARD_MAIN, False, (11.00, 9.00)),
            (BOARD_MAIN, True, (10.50, 9.50)),
            (BOARD_GEM, False, (12.00, 8.00)),
            (BOARD_STAR, False, (12.00, 8.00)),
            (BOARD_BSE, False, (13.00, 7.00)),
        ],
    )
    def test_price_limits_rounding(self, board: str, is_st: bool, expected: tuple[float, float]) -> None:
        from backtest.broker_sim import AStockRules

        assert AStockRules.price_limits(10.0, board, is_st) == pytest.approx(expected)

    def test_price_limits_round_to_cent(self) -> None:
        """3.33 元：涨停 3.663 → 3.66；跌停 2.997 → 3.00（ROUND_HALF_UP 到分）。"""
        from backtest.broker_sim import AStockRules

        limit_up, limit_down = AStockRules.price_limits(3.33, BOARD_MAIN)

        assert limit_up == pytest.approx(3.66)
        assert limit_down == pytest.approx(3.00)

    @pytest.mark.parametrize("prev_close", [0.0, -1.0, None])
    def test_invalid_prev_close_raises(self, prev_close: object) -> None:
        from backtest.broker_sim import AStockRules

        with pytest.raises(ValueError):
            AStockRules.price_limits(prev_close, BOARD_MAIN)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("side", "price", "limits", "expected"),
        [
            ("BUY", 10.5, (11.0, 9.0), (True, None)),
            ("BUY", 11.0, (11.0, 9.0), (True, None)),       # 等于涨停价允许申报
            ("BUY", 11.5, (11.0, 9.0), (False, "RK011")),   # 超过涨停价 RK011
            ("SELL", 9.5, (11.0, 9.0), (True, None)),
            ("SELL", 8.5, (11.0, 9.0), (False, "RK011")),   # 低于跌停价 RK011
            ("BUY", None, (11.0, 9.0), (True, None)),       # 市价单不校验
            ("BUY", 10.0, None, (True, None)),              # 无涨跌停数据不校验
        ],
    )
    def test_validate_price_in_limits(
        self, side: str, price: float | None, limits: tuple[float, float] | None, expected: tuple[bool, str | None]
    ) -> None:
        from backtest.broker_sim import AStockRules

        assert AStockRules.validate_price_in_limits(side, price, limits) == expected


# ---------------------------------------------------------------------------
# 三、整手规则（AC-F3-04）
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLotSize:
    @pytest.mark.parametrize(
        ("side", "quantity", "available", "board", "expected_ok"),
        [
            # 买入
            ("BUY", 100, None, BOARD_MAIN, True),
            ("BUY", 150, None, BOARD_MAIN, False),   # AC-F3-04：买 150 股主板被拒
            ("BUY", 1000, None, BOARD_MAIN, True),
            ("BUY", 50, None, BOARD_MAIN, False),
            ("BUY", 200, None, BOARD_STAR, True),    # AC-F3-04：买 200 股科创板通过
            ("BUY", 199, None, BOARD_STAR, False),
            ("BUY", 201, None, BOARD_STAR, True),    # 科创板 200 股后可 1 股递增
            ("BUY", 100, None, BOARD_BSE, True),
            ("BUY", 99, None, BOARD_BSE, False),
            ("BUY", 100, None, BOARD_GEM, True),
            ("BUY", 150, None, BOARD_GEM, False),
            # 卖出
            ("SELL", 0, 0, BOARD_MAIN, False),       # AC-F3-04：卖 0 股被拒
            ("SELL", 100, 100, BOARD_MAIN, True),
            ("SELL", 50, 50, BOARD_MAIN, True),      # 零股一次性清仓允许
            ("SELL", 50, 100, BOARD_MAIN, False),    # 非清仓须整手
            ("SELL", 100, 50, BOARD_STAR, False),    # 超过可用 → 由 T+1 校验报 RK016
            ("SELL", 50, 50, BOARD_STAR, True),      # 科创板按 1 股递增
        ],
    )
    def test_validate_lot(
        self,
        side: str,
        quantity: int,
        available: int | None,
        board: str,
        expected_ok: bool,
    ) -> None:
        from backtest.broker_sim import AStockRules

        ok, code = AStockRules.validate_lot(side, quantity, available, board)

        assert ok is expected_ok
        if expected_ok:
            assert code is None
        else:
            assert code in {"RK016", "RK017"}

    def test_non_integer_and_bool_quantity_rejected(self) -> None:
        from backtest.broker_sim import AStockRules

        assert AStockRules.validate_lot("BUY", 100.5, None, BOARD_MAIN) == (False, "RK017")
        assert AStockRules.validate_lot("BUY", True, None, BOARD_MAIN) == (False, "RK017")


# ---------------------------------------------------------------------------
# 四、T+1 可卖校验（AC-F3-02 的规则层）
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestT1:
    def test_today_buy_cannot_sell(self) -> None:
        from backtest.broker_sim import AStockRules

        position = _position(quantity=1000, available=0)  # 当日买入，available 未解锁

        assert AStockRules.validate_t1(1000, position) == (False, "RK016")
        assert AStockRules.validate_t1(100, position) == (False, "RK016")

    def test_next_day_can_sell(self) -> None:
        from backtest.broker_sim import AStockRules

        settled = _position(quantity=1000, available=1000)

        assert AStockRules.validate_t1(1000, settled) == (True, None)
        assert AStockRules.validate_t1(500, settled) == (True, None)

    def test_insufficient_total_position(self) -> None:
        from backtest.broker_sim import AStockRules

        position = _position(quantity=100, available=100)

        assert AStockRules.validate_t1(200, position) == (False, "RK015")

    def test_zero_quantity_rejected(self) -> None:
        from backtest.broker_sim import AStockRules

        assert AStockRules.validate_t1(0, _position()) == (False, "RK017")


# ---------------------------------------------------------------------------
# 五、能否成交的判定（AC-F3-01：涨停买不到、跌停卖不掉、停牌不成交）
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCanFill:
    def test_normal_bar_fills(self) -> None:
        from backtest.broker_sim import AStockRules

        assert AStockRules.can_fill("BUY", 10.0, _bar(open=10.0, high=10.5, low=9.5), (11.0, 9.0)) is True
        assert AStockRules.can_fill("SELL", 10.0, _bar(open=10.0, high=10.5, low=9.5), (11.0, 9.0)) is True

    def test_one_word_limit_up_blocks_buy(self) -> None:
        """一字涨停（open=high=low=涨停价）→ 买入不可成交（AC-F3-01）。"""
        from backtest.broker_sim import AStockRules

        bar = _bar(open=11.0, high=11.0, low=11.0, close=11.0, prev_close=10.0)

        assert AStockRules.can_fill("BUY", 11.0, bar, (11.0, 9.0)) is False
        assert AStockRules.can_fill("BUY", 10.9, bar, (11.0, 9.0)) is False

    def test_one_word_limit_down_blocks_sell(self) -> None:
        """一字跌停（open=high=low=跌停价）→ 卖出不可成交（AC-F3-01）。"""
        from backtest.broker_sim import AStockRules

        bar = _bar(open=9.0, high=9.0, low=9.0, close=9.0, prev_close=10.0)

        assert AStockRules.can_fill("SELL", 9.0, bar, (11.0, 9.0)) is False
        assert AStockRules.can_fill("SELL", 9.1, bar, (11.0, 9.0)) is False

    def test_order_at_limit_up_price_is_not_filled(self) -> None:
        """委托价=涨停价按“不成交”处理（D-5，避免排板必成的乐观偏差）。"""
        from backtest.broker_sim import AStockRules

        bar = _bar(open=10.5, high=11.0, low=10.0, close=11.0, prev_close=10.0)

        assert AStockRules.can_fill("BUY", 11.0, bar, (11.0, 9.0)) is False
        assert AStockRules.can_fill("BUY", 10.6, bar, (11.0, 9.0)) is True

    def test_limit_price_not_touched_is_not_filled(self) -> None:
        """限价远低于最低价 → 当日不可能成交。"""
        from backtest.broker_sim import AStockRules

        bar = _bar(open=10.0, high=10.5, low=9.8, prev_close=10.0)

        assert AStockRules.can_fill("BUY", 9.0, bar, (11.0, 9.0)) is False
        assert AStockRules.can_fill("BUY", 9.9, bar, (11.0, 9.0)) is True  # low 9.8 ≤ 9.9

    def test_suspended_bar_never_fills(self) -> None:
        from backtest.broker_sim import AStockRules

        assert AStockRules.can_fill("BUY", 10.0, _bar(is_suspended=True), (11.0, 9.0)) is False
        assert AStockRules.can_fill("BUY", 10.0, None, (11.0, 9.0)) is False

    def test_market_order_at_limit_up_open_not_filled(self) -> None:
        from backtest.broker_sim import AStockRules

        bar = _bar(open=11.0, high=11.0, low=11.0, prev_close=10.0)

        assert AStockRules.can_fill("BUY", None, bar, (11.0, 9.0)) is False


# ---------------------------------------------------------------------------
# 六、滑点
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSlippage:
    def test_buy_slippage_is_unfavorable(self) -> None:
        """买入 10 元 + 5bp = 10.005 → 四舍五入 10.01（对买方不利）。"""
        assert apply_slippage(10.0, "BUY", DEFAULT_SLIPPAGE_BPS) == pytest.approx(10.01)

    def test_sell_slippage_is_unfavorable(self) -> None:
        """卖出 20 元 - 5bp = 19.99（对卖方不利）。"""
        assert apply_slippage(20.0, "SELL", DEFAULT_SLIPPAGE_BPS) == pytest.approx(19.99)

    def test_zero_slippage_keeps_price(self) -> None:
        assert apply_slippage(20.0, "BUY", 0.0) == pytest.approx(20.00)
        assert apply_slippage(20.0, "SELL", 0.0) == pytest.approx(20.00)

    def test_invalid_inputs(self) -> None:
        with pytest.raises(ValueError):
            apply_slippage(0.0, "BUY")
        with pytest.raises(ValueError):
            apply_slippage(10.0, "BUY", -1.0)


# ---------------------------------------------------------------------------
# 七、撮合器 SimBroker（回测=模拟盘同一套口径）
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSimBrokerFills:
    def test_buy_fill_fee_and_price(self) -> None:
        report = SimBroker().match_with_report([_order()], {MAIN_SYMBOL: _bar()}, TRADE_DATE)

        assert not report.rejected and not report.deferred
        assert len(report.fills) == 1
        fill = report.fills[0]
        assert fill.side is Side.BUY
        assert fill.quantity == 1000
        assert fill.price == pytest.approx(10.00)  # 限价 10.00 封顶，滑点不可越过限价
        assert fill.amount == pytest.approx(10000.00)
        assert fill.fee_total == pytest.approx(5.64, abs=FEE_TOL)
        assert fill.trade_date == TRADE_DATE

    def test_market_order_pays_slippage(self) -> None:
        report = SimBroker().match_with_report(
            [_order(price=None, order_type="MARKET")], {MAIN_SYMBOL: _bar()}, TRADE_DATE
        )

        assert len(report.fills) == 1
        assert report.fills[0].price == pytest.approx(10.01)  # 10.00 × (1+5bp) 四舍五入
        assert report.fills[0].slippage_cost > 0

    def test_sell_fill_includes_stamp_tax(self) -> None:
        report = SimBroker().match_with_report(
            [_order(side="SELL")], {MAIN_SYMBOL: _bar()}, TRADE_DATE, positions={MAIN_SYMBOL: _position()}
        )

        assert len(report.fills) == 1
        fill = report.fills[0]
        assert fill.side is Side.SELL
        assert fill.stamp_tax == pytest.approx(5.00, abs=FEE_TOL)
        assert fill.fee_total == pytest.approx(10.64, abs=FEE_TOL)

    def test_match_returns_fills_only(self) -> None:
        fills = SimBroker().match([_order()], {MAIN_SYMBOL: _bar()}, TRADE_DATE)

        assert len(fills) == 1

    def test_sim_broker_does_not_mutate_order(self) -> None:
        order = _order()
        snapshot = copy.deepcopy(order)

        SimBroker().match_with_report([order], {MAIN_SYMBOL: _bar()}, TRADE_DATE)

        assert order == snapshot

    def test_missing_prev_close_is_rejected_fail_closed(self) -> None:
        """口径变更（2026-10-25，随 P0-1 修复引入）：缺前收盘价 → fail-closed 拒单，不再“警告后成交”。

        原实现为“警告 + 跳过涨跌停校验直接成交”，存在涨跌停判定被静默绕过的风险；
        现按 fail-closed 拒绝，并给出中文提示 + warnings，属安全口径收紧。
        """
        report = SimBroker().match_with_report([_order()], {MAIN_SYMBOL: _bar(prev_close=None)}, TRADE_DATE)

        assert report.fills == [], "缺前收盘价时不应成交（fail-closed）"
        assert report.rejected[0].reason_code == "NO_PREV_CLOSE"
        assert any("前收盘价" in warning for warning in report.warnings)


@pytest.mark.unit
class TestSimBrokerRejects:
    def test_lot_size_rejected(self) -> None:
        report = SimBroker().match_with_report([_order(quantity=150)], {MAIN_SYMBOL: _bar()}, TRADE_DATE)

        assert not report.fills
        assert report.rejected[0].reason_code == "RK017"
        assert "整手" in report.rejected[0].message

    def test_star_board_requires_200_shares(self) -> None:
        rejected = SimBroker().match_with_report(
            [_order(symbol=STAR_SYMBOL, quantity=100)], {STAR_SYMBOL: _bar(symbol=STAR_SYMBOL)}, TRADE_DATE
        )
        assert rejected.rejected[0].reason_code == "RK017"

        filled = SimBroker().match_with_report(
            [_order(symbol=STAR_SYMBOL, quantity=200)], {STAR_SYMBOL: _bar(symbol=STAR_SYMBOL)}, TRADE_DATE
        )
        assert len(filled.fills) == 1

    def test_t1_sell_rejected(self) -> None:
        """AC-F3-02：当日买入（available=0）当日卖出 → RK016 拒绝。"""
        report = SimBroker().match_with_report(
            [_order(side="SELL")],
            {MAIN_SYMBOL: _bar()},
            TRADE_DATE,
            positions={MAIN_SYMBOL: _position(quantity=1000, available=0)},
        )

        assert not report.fills
        assert report.rejected[0].reason_code == "RK016"
        assert "T+1" in report.rejected[0].message

    def test_sell_with_next_day_availability_fills(self) -> None:
        """AC-F3-02：T+1 日（available 已解锁）卖出成功。"""
        report = SimBroker().match_with_report(
            [_order(side="SELL")],
            {MAIN_SYMBOL: _bar()},
            TRADE_DATE,
            positions={MAIN_SYMBOL: _position(quantity=1000, available=1000)},
        )

        assert len(report.fills) == 1

    def test_price_out_of_limit_rejected(self) -> None:
        report = SimBroker().match_with_report([_order(price=11.5, quantity=500)], {MAIN_SYMBOL: _bar()}, TRADE_DATE)

        assert report.rejected[0].reason_code == "RK011"


@pytest.mark.unit
class TestSimBrokerDeferral:
    def test_suspended_is_deferred_then_expired(self) -> None:
        """停牌：首日顺延（deferred），顺延次数用尽后作废（EXPIRED）。"""
        suspended_bar = _bar(is_suspended=True)

        first = SimBroker(max_defer_days=1).match_with_report([_order()], {MAIN_SYMBOL: suspended_bar}, TRADE_DATE)
        assert not first.fills
        assert first.deferred[0].reason_code == "SUSPENDED"
        assert first.deferred[0].deferred is True

        # 已经在上一交易日顺延过一次（defer_count=1）
        second = SimBroker(max_defer_days=1).match_with_report(
            [_order(defer_count=1)], {MAIN_SYMBOL: suspended_bar}, TRADE_DATE
        )
        assert not second.fills
        assert second.expired[0].reason_code == "EXPIRED"
        assert second.expired[0].expired is True

    def test_limit_up_buy_is_deferred(self) -> None:
        """AC-F3-01：涨停日买入无成交，并记录不可成交原因。"""
        limit_up_bar = _bar(open=11.0, high=11.0, low=11.0, close=11.0, prev_close=10.0)

        report = SimBroker().match_with_report([_order(price=11.0)], {MAIN_SYMBOL: limit_up_bar}, TRADE_DATE)

        assert not report.fills
        assert report.deferred[0].reason_code == "PRICE_LIMIT_UNMATCHED"
        assert "涨跌停" in report.deferred[0].message

    def test_missing_bar_is_deferred(self) -> None:
        report = SimBroker().match_with_report([_order()], {}, TRADE_DATE)

        assert not report.fills
        assert report.deferred[0].reason_code == "NO_BAR"

    def test_reason_messages_are_chinese(self) -> None:
        for code in ("SUSPENDED", "PRICE_LIMIT_UNMATCHED", "NO_BAR", "NO_PREV_CLOSE", "RK016", "RK017", "RK011"):
            message = reason_message(code)
            assert any("\u4e00" <= char <= "\u9fff" for char in message), f"{code} 缺少中文说明"


# ---------------------------------------------------------------------------
# 八、北交所涨跌停价（30%）——用规则层单测锁定
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bse_30_percent_band() -> None:
    from backtest.broker_sim import AStockRules

    limit_up, limit_down = AStockRules.price_limits(10.0, BOARD_BSE)

    assert limit_up == pytest.approx(13.00)
    assert limit_down == pytest.approx(7.00)
    # 北交所 100 股起、按 1 股递增
    assert AStockRules.validate_lot("BUY", 100, None, BOARD_BSE)[0] is True
    assert AStockRules.validate_lot("BUY", 101, None, BOARD_BSE)[0] is True


# ---------------------------------------------------------------------------
# 九、P0-1 回归：卖出必须校验持仓（fail-closed）
#
# 缺陷现场（2026-10-25，pm-quant 报出）：``SimBroker.match_with_report()`` 未传
# ``positions``（或传空 dict）时，卖单跳过 ``validate_t1`` / 可用数量校验，直接成交
# （实测：卖 100 股 @10.00 竟成交，fee=5.56）。
# 本组用例在修复前为红、修复后必须转绿；修复方式不得是“整体跳过卖单撮合”。
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSellRequiresPositionRegression:
    """P0-1 回归：无持仓/空持仓/超量卖出/当日买入当日卖出，一律不得成交。"""

    def test_sell_without_positions_argument_is_rejected(self) -> None:
        """未传 ``positions``（默认 None）时卖 100 股必须被拒，不得成交。"""
        report = SimBroker().match_with_report([_order(side="SELL", quantity=100)], {MAIN_SYMBOL: _bar()}, TRADE_DATE)

        assert report.fills == [], (
            "P0-1：未传 positions 时卖单竟然成交（"
            f"price={report.fills[0].price if report.fills else None},"
            f"fee={report.fills[0].fee_total if report.fills else None}）"
        )
        assert len(report.rejected) == 1
        assert report.rejected[0].reason_code in {"RK015", "RK016"}
        assert any("\u4e00" <= char <= "\u9fff" for char in report.rejected[0].message)

    def test_sell_with_empty_positions_mapping_is_rejected(self) -> None:
        """``positions={}``（显式空持仓）同样必须拒单。"""
        report = SimBroker().match_with_report(
            [_order(side="SELL", quantity=100)], {MAIN_SYMBOL: _bar()}, TRADE_DATE, positions={}
        )

        assert report.fills == [], "P0-1：空持仓映射下卖单竟然成交"
        assert report.rejected[0].reason_code in {"RK015", "RK016"}

    def test_sell_more_than_held_is_rejected(self) -> None:
        """持仓 100 股卖 200 股 → RK015（持仓不足），不得超额成交。"""
        report = SimBroker().match_with_report(
            [_order(side="SELL", quantity=200)],
            {MAIN_SYMBOL: _bar()},
            TRADE_DATE,
            positions={MAIN_SYMBOL: _position(quantity=100, available=100)},
        )

        assert report.fills == []
        assert report.rejected[0].reason_code == "RK015"

    def test_same_day_buy_then_sell_is_rejected(self) -> None:
        """T+1：当日买入（available=0）当日卖出 → RK016。"""
        report = SimBroker().match_with_report(
            [_order(side="SELL", quantity=100)],
            {MAIN_SYMBOL: _bar()},
            TRADE_DATE,
            positions={MAIN_SYMBOL: _position(quantity=1000, available=0)},
        )

        assert report.fills == []
        assert report.rejected[0].reason_code == "RK016"

    def test_normal_sell_fills_with_correct_fee(self) -> None:
        """正常路径不能被回归修坏：持仓足额卖出成交且卖出费用 10.64 正确。"""
        report = SimBroker().match_with_report(
            [_order(side="SELL", quantity=1000)],
            {MAIN_SYMBOL: _bar()},
            TRADE_DATE,
            positions={MAIN_SYMBOL: _position(quantity=1000, available=1000)},
        )

        assert len(report.fills) == 1
        fill = report.fills[0]
        assert fill.side is Side.SELL
        assert fill.quantity == 1000
        assert fill.stamp_tax == pytest.approx(5.00, abs=FEE_TOL)
        assert fill.fee_total == pytest.approx(10.64, abs=FEE_TOL)

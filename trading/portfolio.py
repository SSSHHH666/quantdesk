"""QuantDesk 账户/持仓/盈亏/资金曲线服务（M2-03）。

职责（SPEC §6.3 ``PortfolioService``）
------------------------------------
- 唯一负责维护 ``accounts`` / ``positions`` / ``trades`` / ``cash_flows`` / ``equity_curve``
  这 5 张表对应的领域状态；
- ``apply_fill``：按成交回报精确更新持仓数量、T+1 可用数量、加权平均成本、现金、
  已实现盈亏与资金流水；
- ``settle_new_day``：盘前 T+1 日初结算（``available_quantity = quantity - frozen``）；
- ``snapshot_equity``：按交易日幂等写入资金曲线（含当日盈亏/收益率）；
- ``reconcile``：启动对账（持仓 = 成交净额、现金 = 期初 + 流水），不一致给中文报告。

依赖注入（重要）
----------------
本模块**不创建数据库引擎/会话**。持久化通过 :class:`TradingRepository` 协议注入：

- 生产：由 Dev1 的 ``core/db.py`` 提供 SQLAlchemy 实现（会话由调用方注入）；
- 自测/模拟盘：使用本模块内置的 :class:`InMemoryTradingRepository`（零 IO、可重复）。

金额口径：所有金额四舍五入到 0.01 元（复用 CF-10 ``backtest.broker_sim.round_money``，
保证与费用模型/回测口径一致）；平均成本保留 4 位小数。

T+1 口径（SPEC §4.2）
--------------------
- 买入成交：``quantity += qty``、``today_buy_quantity += qty``，**不增加** ``available_quantity``；
- 卖出成交：仅能卖出 ``available_quantity``（风控/订单服务已校验，本模块再做一次防御校验）；
- 日初结算：``available_quantity = quantity - frozen_quantity``、``today_buy_quantity = 0``。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Iterable, Protocol, runtime_checkable

from backtest.broker_sim import normalize_side, round_money, SIDE_BUY, SIDE_SELL
from core.models import Account, BrokerType, Fill, Order, Position, now_local

#: 资金流水类型（SPEC §5.2 cash_flows.flow_type）
FLOW_BUY = "BUY"
FLOW_SELL = "SELL"
FLOW_FEE = "FEE"
FLOW_DEPOSIT = "DEPOSIT"

_EPS = 1e-6


# ---------------------------------------------------------------------------
# 仓储协议（生产用 SQLAlchemy 实现，测试用内存实现；接口保持简单、可幂等）
# ---------------------------------------------------------------------------


@runtime_checkable
class TradingRepository(Protocol):
    """交易域仓储协议（账户/持仓/成交/流水/资金曲线/订单）。

    生产实现由 ``core/db.py`` 的会话注入构造（本模块不建引擎）；
    所有方法都应是幂等/可重复调用的（``save_*`` 覆盖写、``append_*`` 只追加）。
    """

    # -- 账户与持仓 -------------------------------------------------------
    def get_account(self, account_id: str) -> Account | None: ...

    def save_account(self, account: Account) -> None: ...

    def list_accounts(self) -> list[Account]: ...

    def list_positions(self, account_id: str) -> list[Position]: ...

    def get_position(self, account_id: str, symbol: str) -> Position | None: ...

    def save_position(self, position: Position) -> None: ...

    def delete_position(self, account_id: str, symbol: str) -> None: ...

    # -- 成交与资金流水 ---------------------------------------------------
    def append_trade(self, fill: Fill) -> None: ...

    def list_trades(self, account_id: str, limit: int = 1000) -> list[Fill]: ...

    def append_cash_flow(self, flow: dict[str, Any]) -> None: ...

    def list_cash_flows(self, account_id: str, limit: int = 5000) -> list[dict[str, Any]]: ...

    # -- 已实现盈亏与资金曲线 --------------------------------------------
    def get_realized_pnl(self, account_id: str) -> float: ...

    def add_realized_pnl(self, account_id: str, delta: float) -> None: ...

    def upsert_equity(self, point: dict[str, Any]) -> None: ...

    def list_equity(self, account_id: str, limit: int = 2000) -> list[dict[str, Any]]: ...

    # -- 订单（供 trading/order_manager.py 使用） ------------------------
    def get_order(self, order_id: str) -> Order | None: ...

    def get_order_by_client_id(self, client_order_id: str) -> Order | None: ...

    def save_order(self, order: Order) -> None: ...

    def list_orders(
        self, account_id: str, statuses: Iterable[str] | None = None, limit: int = 200
    ) -> list[Order]: ...


# ---------------------------------------------------------------------------
# 内存仓储（自测 / PaperBroker 演示；零 IO）
# ---------------------------------------------------------------------------


class InMemoryTradingRepository:
    """内存版交易域仓储：进程内 dict/list，供单测与“无 DB 也能跑通全流程”的演示使用。

    线程安全说明：MVP 单用户、单进程；如需多线程使用请在外层加锁（与真实 DB 事务语义对齐）。
    """

    def __init__(self) -> None:
        self._accounts: dict[str, Account] = {}
        self._positions: dict[tuple[str, str], Position] = {}
        self._trades: dict[str, list[Fill]] = {}
        self._cash_flows: dict[str, list[dict[str, Any]]] = {}
        self._equity: dict[tuple[str, date], dict[str, Any]] = {}
        self._equity_order: dict[str, list[date]] = {}
        self._realized: dict[str, float] = {}
        self._orders: dict[str, Order] = {}
        self._client_ids: dict[str, str] = {}

    # -- 账户与持仓 -------------------------------------------------------
    def get_account(self, account_id: str) -> Account | None:
        account = self._accounts.get(account_id)
        return account.model_copy() if account is not None else None

    def save_account(self, account: Account) -> None:
        self._accounts[account.id] = account.model_copy()

    def list_accounts(self) -> list[Account]:
        return [account.model_copy() for account in self._accounts.values()]

    def list_positions(self, account_id: str) -> list[Position]:
        return [
            position.model_copy()
            for (holder, _symbol), position in sorted(self._positions.items())
            if holder == account_id
        ]

    def get_position(self, account_id: str, symbol: str) -> Position | None:
        position = self._positions.get((account_id, symbol.strip().upper()))
        return position.model_copy() if position is not None else None

    def save_position(self, position: Position) -> None:
        self._positions[(position.account_id, position.symbol)] = position.model_copy()

    def delete_position(self, account_id: str, symbol: str) -> None:
        self._positions.pop((account_id, symbol.strip().upper()), None)

    # -- 成交与资金流水 ---------------------------------------------------
    def append_trade(self, fill: Fill) -> None:
        self._trades.setdefault(fill.account_id or "", []).append(fill.model_copy())

    def list_trades(self, account_id: str, limit: int = 1000) -> list[Fill]:
        rows = self._trades.get(account_id, [])
        return [row.model_copy() for row in rows[-limit:]]

    def append_cash_flow(self, flow: dict[str, Any]) -> None:
        self._cash_flows.setdefault(str(flow.get("account_id")), []).append(dict(flow))

    def list_cash_flows(self, account_id: str, limit: int = 5000) -> list[dict[str, Any]]:
        return [dict(row) for row in self._cash_flows.get(account_id, [])[-limit:]]

    # -- 已实现盈亏与资金曲线 --------------------------------------------
    def get_realized_pnl(self, account_id: str) -> float:
        return float(self._realized.get(account_id, 0.0))

    def add_realized_pnl(self, account_id: str, delta: float) -> None:
        self._realized[account_id] = round_money(self._realized.get(account_id, 0.0) + delta)

    def upsert_equity(self, point: dict[str, Any]) -> None:
        key = (str(point["account_id"]), point["trade_date"])
        is_new = key not in self._equity
        self._equity[key] = dict(point)
        if is_new:
            self._equity_order.setdefault(key[0], []).append(key[1])

    def list_equity(self, account_id: str, limit: int = 2000) -> list[dict[str, Any]]:
        days = sorted(self._equity_order.get(account_id, []))[-limit:]
        return [dict(self._equity[(account_id, day)]) for day in days]

    # -- 订单 -------------------------------------------------------------
    def get_order(self, order_id: str) -> Order | None:
        order = self._orders.get(order_id)
        return order.model_copy() if order is not None else None

    def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        order_id = self._client_ids.get(str(client_order_id))
        return self.get_order(order_id) if order_id else None

    def save_order(self, order: Order) -> None:
        self._orders[order.id] = order.model_copy()
        if order.client_order_id:
            self._client_ids[str(order.client_order_id)] = order.id

    def list_orders(
        self, account_id: str, statuses: Iterable[str] | None = None, limit: int = 200
    ) -> list[Order]:
        allowed = {str(status) for status in statuses} if statuses is not None else None
        rows = [
            order.model_copy()
            for order in self._orders.values()
            if order.account_id == account_id
            and (allowed is None or str(getattr(order.status, "value", order.status)) in allowed)
        ]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows[:limit]


# ---------------------------------------------------------------------------
# PortfolioService
# ---------------------------------------------------------------------------


class PortfolioService:
    """账户/持仓/盈亏/资金曲线服务（SPEC §6.3，M2-03）。

    :param repository: 交易域仓储（生产为 SQLAlchemy 实现，测试为内存实现）。
    :param clock: 可注入时钟（返回 ``datetime``），默认 ``core.models.now_local``。
    """

    def __init__(self, repository: TradingRepository, clock: Any = None) -> None:
        if not isinstance(repository, TradingRepository):
            raise TypeError(
                "PortfolioService 初始化失败：repository 必须实现 TradingRepository 协议"
                "（内存实现见 InMemoryTradingRepository）"
            )
        self.repository = repository
        self._clock = clock or now_local

    # -- 查询 -------------------------------------------------------------

    def get_account(self, account_id: str) -> Account:
        """取账户；不存在时抛中文 ``ValueError``（不静默造账户，避免脏数据）。"""
        account = self.repository.get_account(account_id)
        if account is None:
            raise ValueError(f"账户不存在：{account_id}（请先创建/初始化模拟账户）")
        return account

    def get_positions(self, account_id: str) -> list[Position]:
        """取账户全部持仓（按标的代码排序）。"""
        return self.repository.list_positions(account_id)

    def get_position(self, account_id: str, symbol: str) -> Position | None:
        """取单标的持仓；不存在返回 ``None``。"""
        return self.repository.get_position(account_id, symbol)

    def ensure_account(
        self,
        account_id: str,
        initial_cash: float = 100000.0,
        name: str = "模拟账户",
        broker: str = "paper",
    ) -> Account:
        """确保账户存在（不存在则按初始资金创建）；返回账户快照。

        幂等：已存在时不做任何修改（避免覆盖真实资金状态）。
        """
        existing = self.repository.get_account(account_id)
        if existing is not None:
            return existing
        if initial_cash <= 0:
            raise ValueError(f"初始资金必须大于 0（当前 {initial_cash}）")
        cash = round_money(initial_cash)
        account = Account(
            id=account_id,
            name=name,
            broker=BrokerType(broker.upper()),
            cash=cash,
            frozen_cash=0.0,
            total_asset=cash,
            initial_cash=cash,
            enabled=True,
            updated_at=self._clock(),
        )
        self.repository.save_account(account)
        self.repository.append_cash_flow(
            {
                "account_id": account_id,
                "flow_type": FLOW_DEPOSIT,
                "amount": cash,
                "balance_after": cash,
                "ref_type": "account",
                "ref_id": account_id,
                "occurred_at": self._clock(),
                "note": "模拟账户初始资金",
            }
        )
        return account

    # -- 成交入账（核心） -------------------------------------------------

    def apply_fill(self, fill: Fill) -> Position:
        """按成交回报更新持仓/可用/成本/现金/流水/已实现盈亏（幂等键由调用方保证）。

        :raises ValueError: 账户不存在、卖出超可用、成交参数非法（中文提示）。
        """
        account = self.get_account(fill.account_id or "")
        side = normalize_side(fill.side)
        quantity = int(fill.quantity)
        price = round_price_safe(fill.price)
        if quantity <= 0:
            raise ValueError(f"成交数量必须大于 0（当前 {fill.quantity}）")
        if price <= 0:
            raise ValueError(f"成交价格必须大于 0（当前 {fill.price}）")

        amount = round_money(fill.amount if fill.amount else price * quantity)
        fee_total = round_money(fill.fee_total)
        symbol = str(fill.symbol or "").strip().upper()
        if not symbol:
            raise ValueError("成交回报缺少标的代码 symbol")

        position = self.repository.get_position(account.id, symbol)
        if position is None:
            position = Position(account_id=account.id, symbol=symbol)
        # 兼容旧持仓的零值字段
        position.last_price = price

        if side == SIDE_BUY:
            old_qty = int(position.quantity)
            old_cost = float(position.avg_cost)
            new_qty = old_qty + quantity
            # 成本口径：买入金额 + 买入费用均计入成本（A 股常见口径，UI 展示更接近实际盈亏）
            new_cost = (old_qty * old_cost + amount + fee_total) / new_qty if new_qty else 0.0
            position.quantity = new_qty
            position.today_buy_quantity = int(position.today_buy_quantity) + quantity
            position.avg_cost = round(float(new_cost), 4)
            position.last_price = price
            position.market_value = round_money(price * position.quantity)
            cash_delta = -(amount + fee_total)
        elif side == SIDE_SELL:
            available = int(position.available_quantity)
            if quantity > available:
                raise ValueError(
                    f"卖出超过可用数量（T+1）：可卖 {available} 股，成交 {quantity} 股"
                )
            if quantity > int(position.quantity):
                raise ValueError(
                    f"卖出超过持仓数量：持有 {int(position.quantity)} 股，成交 {quantity} 股"
                )
            cost_basis = round(float(position.avg_cost) * quantity, 2)
            realized = round_money(amount - fee_total - cost_basis)
            self.repository.add_realized_pnl(account.id, realized)
            position.quantity = int(position.quantity) - quantity
            position.available_quantity = max(available - quantity, 0)
            position.today_buy_quantity = max(
                int(position.today_buy_quantity) - quantity, 0
            )
            position.last_price = price
            position.market_value = round_money(price * position.quantity)
            cash_delta = amount - fee_total
        else:
            raise ValueError(f"不支持的成交方向：{fill.side!r}（只支持 BUY/SELL）")

        cash = round_money(account.cash + cash_delta)
        if cash < -_EPS:
            raise ValueError(
                f"账户现金不足以完成该笔成交：需要 {round_money(-cash_delta)} 元，"
                f"可用 {round_money(account.cash)} 元（请检查资金/风控校验是否被绕过）"
            )
        account.cash = max(cash, 0.0)
        account.updated_at = self._clock()

        if position.quantity <= 0:
            self.repository.delete_position(account.id, symbol)
        else:
            self.repository.save_position(position)
        self.repository.save_account(account)
        self.repository.append_trade(fill.model_copy(update={"amount": amount, "fee_total": fee_total}))
        self.repository.append_cash_flow(
            {
                "account_id": account.id,
                "flow_type": FLOW_BUY if side == SIDE_BUY else FLOW_SELL,
                "amount": round_money(cash_delta),
                "balance_after": account.cash,
                "ref_type": "trade",
                "ref_id": fill.id or fill.order_id,
                "occurred_at": fill.traded_at or self._clock(),
                "note": f"{side} {symbol} {quantity} 股 @ {price:.2f} 元（费用 {fee_total:.2f} 元）",
            }
        )
        self._refresh_total_asset(account)
        return position if position.quantity > 0 else Position(account_id=account.id, symbol=symbol)

    # -- 估值 / 日初结算 / 资金曲线 ---------------------------------------

    def mark_prices(self, account_id: str, prices: dict[str, float]) -> Account:
        """按最新价更新持仓市值与账户总资产（只改估值，不产生成交）。"""
        account = self.get_account(account_id)
        for position in self.repository.list_positions(account_id):
            price = _lookup_price(prices, position.symbol)
            if price is None or price <= 0:
                continue
            position.last_price = round_price_safe(price)
            position.market_value = round_money(position.last_price * int(position.quantity))
            self.repository.save_position(position)
        self._refresh_total_asset(account)
        updated = self.repository.get_account(account_id)
        assert updated is not None
        return updated

    def settle_new_day(self, account_id: str, trade_date: date | None = None) -> None:
        """盘前 T+1 日初结算：全部持仓 ``available_quantity = quantity - frozen_quantity``。"""
        self.get_account(account_id)  # 账户不存在时抛中文错误
        for position in self.repository.list_positions(account_id):
            position.available_quantity = max(
                int(position.quantity) - int(position.frozen_quantity), 0
            )
            position.today_buy_quantity = 0
            self.repository.save_position(position)

    def snapshot_equity(
        self,
        account_id: str,
        trade_date: date | None = None,
        prices: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """写当日资金曲线（幂等 upsert），返回该点。

        当日盈亏 = 当日总资产 − 上一交易日总资产（无历史时按“总资产 − 期初资金”）。
        """
        if prices:
            self.mark_prices(account_id, prices)
        account = self.get_account(account_id)
        day = trade_date or (self._clock().date() if hasattr(self._clock(), "date") else now_local().date())
        position_value = round_money(sum(position.market_value for position in self.repository.list_positions(account_id)))
        total_asset = round_money(account.cash + position_value)

        history = self.repository.list_equity(account_id)
        previous_total = None
        for point in reversed(history):
            if point["trade_date"] < day:
                previous_total = float(point["total_asset"])
                break
        base = previous_total if previous_total is not None else account.initial_cash
        daily_pnl = round_money(total_asset - base)
        daily_return = round((daily_pnl / base * 100.0) if base else 0.0, 4)
        point = {
            "account_id": account_id,
            "trade_date": day,
            "total_asset": total_asset,
            "cash": account.cash,
            "position_value": position_value,
            "daily_pnl": daily_pnl,
            "daily_return": daily_return,
        }
        self.repository.upsert_equity(point)
        return point

    # -- 盈亏 -------------------------------------------------------------

    def realized_pnl(self, account_id: str) -> float:
        """累计已实现盈亏（卖出净收入 − 对应成本，含卖出费用）。"""
        return round_money(self.repository.get_realized_pnl(account_id))

    def unrealized_pnl(self, account_id: str, prices: dict[str, float] | None = None) -> float:
        """未实现盈亏（按传入价格或持仓最新价估值）。"""
        total = 0.0
        for position in self.repository.list_positions(account_id):
            price = _lookup_price(prices or {}, position.symbol)
            if price is None or price <= 0:
                price = float(position.last_price)
            total += (price - float(position.avg_cost)) * int(position.quantity)
        return round_money(total)

    def total_asset(self, account_id: str, prices: dict[str, float] | None = None) -> float:
        """总资产 = 现金 + 持仓市值；传入 prices 时先更新估值。"""
        if prices:
            self.mark_prices(account_id, prices)
        account = self.get_account(account_id)
        return round_money(
            account.cash
            + sum(position.market_value for position in self.repository.list_positions(account_id))
        )

    # -- 对账 -------------------------------------------------------------

    def reconcile(self, account_id: str) -> dict[str, Any]:
        """启动对账：持仓数量 = 成交净额、现金 = 期初资金 + 流水累计。

        返回 ``{"ok": bool, "cash_diff": float, "position_diffs": {...}, "messages": [...]}``；
        金额容差 0.01 元（券商对账单口径）。
        """
        account = self.get_account(account_id)
        trades = self.repository.list_trades(account_id, limit=10**6)
        flows = self.repository.list_cash_flows(account_id, limit=10**6)
        positions = {position.symbol: position for position in self.repository.list_positions(account_id)}

        messages: list[str] = []
        net_qty: dict[str, int] = {}
        for trade in trades:
            symbol = str(trade.symbol or "").upper()
            side = normalize_side(trade.side)
            delta = int(trade.quantity) if side == SIDE_BUY else -int(trade.quantity)
            net_qty[symbol] = net_qty.get(symbol, 0) + delta

        position_diffs: dict[str, int] = {}
        for symbol in set(net_qty) | set(positions):
            diff = int(positions[symbol].quantity if symbol in positions else 0) - net_qty.get(symbol, 0)
            if diff != 0:
                position_diffs[symbol] = diff
        if position_diffs:
            messages.append(
                "持仓与成交净额不一致（差异股数：" + "、".join(
                    f"{symbol} {diff:+d}" for symbol, diff in sorted(position_diffs.items())
                ) + "），请检查是否有未落库成交或人工改库"
            )

        # 现金口径：账户创建时已写入一笔 DEPOSIT 流水，故“期初资金 + 全部流水”会重复计算；
        # 有流水时以流水累计为准，无流水时退回 initial_cash（兼容外部导入账户）。
        expected_cash = (
            round_money(sum(float(flow.get("amount", 0.0)) for flow in flows))
            if flows
            else round_money(float(account.initial_cash))
        )
        cash_diff = round_money(expected_cash - float(account.cash))
        if abs(cash_diff) > 0.01:
            messages.append(
                f"现金与流水累计不一致：应为 {expected_cash:.2f} 元，实际 {float(account.cash):.2f} 元，"
                f"差异 {cash_diff:+.2f} 元"
            )

        ok = not position_diffs and abs(cash_diff) <= 0.01
        # 总资产口径校验：total_asset 应等于 现金 + 持仓市值（派生字段被人工改坏时能发现）
        expected_total = round_money(
            float(account.cash) + sum(float(position.market_value) for position in positions.values())
        )
        total_diff = round_money(float(account.total_asset) - expected_total)
        if abs(total_diff) > 0.01:
            ok = False
            messages.append(
                f"总资产与“现金+持仓市值”不一致：账户记录 {float(account.total_asset):.2f} 元，"
                f"折算 {expected_total:.2f} 元，差异 {total_diff:+.2f} 元（请重新估值后保存账户）"
            )
        if ok:
            messages.append("账户对账通过：持仓与现金一致")
        return {
            "ok": ok,
            "cash_diff": cash_diff,
            "total_asset_diff": total_diff,
            "position_diffs": position_diffs,
            "messages": messages,
        }

    # -- 内部 -------------------------------------------------------------

    def _refresh_total_asset(self, account: Account) -> None:
        """重算并保存账户总资产（现金 + 持仓市值）。"""
        position_value = sum(
            position.market_value for position in self.repository.list_positions(account.id)
        )
        account.total_asset = round_money(account.cash + position_value)
        account.updated_at = self._clock()
        self.repository.save_account(account)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def round_price_safe(value: Any) -> float:
    """价格四舍五入到 0.01 元；非法值抛中文 ``ValueError``。"""
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"价格必须是数值（当前 {value!r}）") from exc
    if number <= 0:
        raise ValueError(f"价格必须大于 0（当前 {value!r}）")
    return round_money(number)


def _lookup_price(prices: dict[str, float], symbol: str) -> float | None:
    """在 {symbol: price} 中查价（大小写不敏感）。"""
    if not prices:
        return None
    symbol = str(symbol or "").strip().upper()
    if symbol in prices:
        return float(prices[symbol])
    for key, value in prices.items():
        if str(key).strip().upper() == symbol:
            return float(value)
    return None


def make_fill_id() -> str:
    """生成成交 ID（UUID4 十六进制）。"""
    return uuid.uuid4().hex


__all__ = [
    "TradingRepository",
    "InMemoryTradingRepository",
    "PortfolioService",
    "FLOW_BUY",
    "FLOW_SELL",
    "FLOW_FEE",
    "FLOW_DEPOSIT",
    "round_price_safe",
    "make_fill_id",
]

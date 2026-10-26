"""QuantDesk 交易域 SQL 仓储（M3 持久化，`TradingRepository` 的 SQLAlchemy 实现）。

设计
----
- **不自己建引擎/会话**：通过注入 ``session_factory``（``core.db.get_session_factory()``
  或 ``Database.session_factory``）获得会话；每个方法一个短事务，异常回滚。
- 覆盖账户、持仓、订单、成交、资金流水、资金曲线、已实现盈亏（由成交回放推算）。
- 与 :class:`trading.portfolio.InMemoryTradingRepository` 实现同一
  :class:`trading.portfolio.TradingRepository` 协议，可直接替换：
  内存仓储用于单测/离线演示，本类用于生产（重启后数据可恢复）。
- 金额/时间字段按 ``core/models.py`` ORM 定义写库；订单风控明细存 ``risk_checks_json``。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Iterable, Iterator

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backtest.broker_sim import normalize_side, round_money, SIDE_BUY
from core.models import (
    Account,
    AccountORM,
    AppSettingORM,
    CashFlowORM,
    EquityCurveORM,
    Fill,
    Order,
    OrderORM,
    Position,
    PositionORM,
    TradeORM,
    now_local_naive,
)


def _enum_text(value: Any) -> str:
    """枚举/字符串统一成大写文本。"""
    raw = getattr(value, "value", value)
    return str(raw).strip().upper() if raw is not None else ""


def _dt(value: Any, default: datetime | None = None) -> datetime:
    """转 datetime（SQLite 存 naive 本地时间）。"""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo is not None else value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    parsed = default or now_local_naive()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return parsed
    return parsed


def _date(value: Any) -> date:
    """转 date。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value[:10]).date()
    return now_local_naive().date()


class SqlTradingRepository:
    """交易域 SQL 仓储（生产实现；构造不建引擎，会话由 ``core/db.py`` 提供）。"""

    def __init__(self, session_factory: Any = None, *, database: Any = None) -> None:
        if session_factory is not None:
            self._session_factory = session_factory
        elif database is not None:
            self._session_factory = database.session_factory
        else:  # 延迟解析默认数据库，避免导入期副作用
            self._session_factory = None

    # -- 会话 -------------------------------------------------------------

    def _factory(self) -> Any:
        if self._session_factory is None:
            from core.db import get_session_factory  # 延迟导入

            self._session_factory = get_session_factory()
        return self._session_factory

    @contextmanager
    def _session(self) -> Iterator[Session]:
        session: Session = self._factory()()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # =====================================================================
    # 账户与持仓
    # =====================================================================

    def get_account(self, account_id: str) -> Account | None:
        with self._session() as session:
            row = session.get(AccountORM, account_id)
            return self._account_to_model(row) if row is not None else None

    def save_account(self, account: Account) -> None:
        with self._session() as session:
            row = session.get(AccountORM, account.id)
            if row is None:
                row = AccountORM(id=account.id)
                session.add(row)
            row.name = account.name
            row.broker = _enum_text(account.broker) or "PAPER"
            row.base_currency = account.base_currency or "CNY"
            row.initial_cash = float(account.initial_cash)
            row.cash = round_money(account.cash)
            row.frozen_cash = round_money(account.frozen_cash)
            row.total_asset = round_money(account.total_asset)
            row.enabled = bool(account.enabled)
            row.updated_at = _dt(account.updated_at)

    def list_accounts(self) -> list[Account]:
        with self._session() as session:
            rows = session.execute(select(AccountORM).order_by(AccountORM.id)).scalars().all()
            return [self._account_to_model(row) for row in rows]

    def list_positions(self, account_id: str) -> list[Position]:
        with self._session() as session:
            rows = (
                session.execute(
                    select(PositionORM)
                    .where(PositionORM.account_id == account_id)
                    .order_by(PositionORM.symbol)
                )
                .scalars()
                .all()
            )
            return [self._position_to_model(row) for row in rows]

    def get_position(self, account_id: str, symbol: str) -> Position | None:
        with self._session() as session:
            row = session.get(PositionORM, (account_id, str(symbol).strip().upper()))
            return self._position_to_model(row) if row is not None else None

    def save_position(self, position: Position) -> None:
        with self._session() as session:
            key = (position.account_id, str(position.symbol).strip().upper())
            row = session.get(PositionORM, key)
            if row is None:
                row = PositionORM(account_id=key[0], symbol=key[1])
                session.add(row)
            row.quantity = int(position.quantity)
            row.available_quantity = int(position.available_quantity)
            row.frozen_quantity = int(position.frozen_quantity)
            row.today_buy_quantity = int(position.today_buy_quantity)
            row.avg_cost = float(position.avg_cost)
            row.last_price = float(position.last_price)
            row.market_value = round_money(position.market_value)
            row.updated_at = _dt(position.updated_at)

    def delete_position(self, account_id: str, symbol: str) -> None:
        with self._session() as session:
            session.execute(
                delete(PositionORM).where(
                    PositionORM.account_id == account_id,
                    PositionORM.symbol == str(symbol).strip().upper(),
                )
            )

    # =====================================================================
    # 成交、资金流水、资金曲线、已实现盈亏
    # =====================================================================

    def append_trade(self, fill: Fill) -> None:
        with self._session() as session:
            session.add(
                TradeORM(
                    id=fill.id or fill.order_id,
                    order_id=fill.order_id,
                    account_id=fill.account_id or "",
                    symbol=str(fill.symbol or "").strip().upper(),
                    side=normalize_side(fill.side),
                    price=round_money(fill.price),
                    quantity=int(fill.quantity),
                    amount=round_money(fill.amount),
                    commission=float(fill.commission),
                    stamp_tax=float(fill.stamp_tax),
                    transfer_fee=float(fill.transfer_fee),
                    reg_fee=float(fill.reg_fee),
                    fee_total=float(fill.fee_total),
                    traded_at=_dt(fill.traded_at),
                    source=_enum_text(fill.source) or "MANUAL",
                )
            )

    def list_trades(self, account_id: str, limit: int = 1000) -> list[Fill]:
        with self._session() as session:
            rows = (
                session.execute(
                    select(TradeORM)
                    .where(TradeORM.account_id == account_id)
                    .order_by(TradeORM.traded_at, TradeORM.id)
                )
                .scalars()
                .all()
            )
            models = [self._trade_to_model(row) for row in rows]
            return models[-limit:] if limit else models

    def append_cash_flow(self, flow: dict[str, Any]) -> None:
        with self._session() as session:
            session.add(
                CashFlowORM(
                    account_id=str(flow.get("account_id") or ""),
                    flow_type=str(flow.get("flow_type") or "OTHER"),
                    amount=round_money(float(flow.get("amount", 0.0))),
                    balance_after=round_money(float(flow.get("balance_after", 0.0))),
                    ref_type=flow.get("ref_type"),
                    ref_id=None if flow.get("ref_id") is None else str(flow.get("ref_id")),
                    occurred_at=_dt(flow.get("occurred_at")),
                    note=(str(flow.get("note")) if flow.get("note") else None),
                )
            )

    def list_cash_flows(self, account_id: str, limit: int = 5000) -> list[dict[str, Any]]:
        with self._session() as session:
            rows = (
                session.execute(
                    select(CashFlowORM)
                    .where(CashFlowORM.account_id == account_id)
                    .order_by(CashFlowORM.id)
                )
                .scalars()
                .all()
            )
            result = [
                {
                    "account_id": row.account_id,
                    "flow_type": row.flow_type,
                    "amount": float(row.amount),
                    "balance_after": float(row.balance_after),
                    "ref_type": row.ref_type,
                    "ref_id": row.ref_id,
                    "occurred_at": row.occurred_at,
                    "note": row.note,
                }
                for row in rows
            ]
            return result[-limit:] if limit else result

    def upsert_equity(self, point: dict[str, Any]) -> None:
        key = (str(point["account_id"]), _date(point["trade_date"]))
        with self._session() as session:
            row = session.get(EquityCurveORM, key)
            if row is None:
                row = EquityCurveORM(account_id=key[0], trade_date=key[1])
                session.add(row)
            row.total_asset = round_money(float(point.get("total_asset", 0.0)))
            row.cash = round_money(float(point.get("cash", 0.0)))
            row.position_value = round_money(float(point.get("position_value", 0.0)))
            row.daily_pnl = round_money(float(point.get("daily_pnl", 0.0)))
            row.daily_return = float(point.get("daily_return", 0.0))

    def list_equity(self, account_id: str, limit: int = 2000) -> list[dict[str, Any]]:
        with self._session() as session:
            rows = (
                session.execute(
                    select(EquityCurveORM)
                    .where(EquityCurveORM.account_id == account_id)
                    .order_by(EquityCurveORM.trade_date)
                )
                .scalars()
                .all()
            )
            result = [
                {
                    "account_id": row.account_id,
                    "trade_date": row.trade_date,
                    "total_asset": float(row.total_asset),
                    "cash": float(row.cash),
                    "position_value": float(row.position_value),
                    "daily_pnl": float(row.daily_pnl),
                    "daily_return": float(row.daily_return),
                }
                for row in rows
            ]
            return result[-limit:] if limit else result

    def get_realized_pnl(self, account_id: str) -> float:
        """累计已实现盈亏：优先读 ``app_settings`` 增量记录（PortfolioService 每次卖出累加）。"""
        key = f"realized_pnl:{account_id}"
        with self._session() as session:
            row = session.get(AppSettingORM, key)
            if row is not None and row.value_json:
                try:
                    value = json.loads(row.value_json)
                    if isinstance(value, dict):
                        value = value.get("value", 0.0)
                    return round_money(float(value))
                except (TypeError, ValueError):
                    pass
        # 兜底：按成交回放（按时间排序；同一时间按买卖先后无法区分时以先买后卖假设）
        trades = self.list_trades(account_id, limit=0)
        quantity: dict[str, int] = {}
        cost: dict[str, float] = {}
        realized = 0.0
        for fill in trades:
            symbol = str(fill.symbol or "").upper()
            qty = int(fill.quantity)
            side = normalize_side(fill.side)
            fee = float(fill.fee_total)
            amount = round_money(fill.amount or fill.price * qty)
            if side == SIDE_BUY:
                new_qty = quantity.get(symbol, 0) + qty
                cost[symbol] = (
                    (quantity.get(symbol, 0) * cost.get(symbol, 0.0) + amount + fee) / new_qty
                    if new_qty
                    else 0.0
                )
                quantity[symbol] = new_qty
            else:
                held = quantity.get(symbol, 0)
                avg = cost.get(symbol, 0.0)
                realized += amount - fee - avg * min(qty, held)
                quantity[symbol] = max(held - qty, 0)
                if quantity[symbol] == 0:
                    cost[symbol] = 0.0
        return round_money(realized)

    def add_realized_pnl(self, account_id: str, delta: float) -> None:
        """累加已实现盈亏到 ``app_settings``（键 ``realized_pnl:{account_id}``，JSON 数值）。"""
        key = f"realized_pnl:{account_id}"
        with self._session() as session:
            row = session.get(AppSettingORM, key)
            current = 0.0
            if row is not None and row.value_json:
                try:
                    value = json.loads(row.value_json)
                    current = float(value.get("value", 0.0) if isinstance(value, dict) else value)
                except (TypeError, ValueError):
                    current = 0.0
            total = round_money(current + float(delta))
            if row is None:
                row = AppSettingORM(key=key)
                session.add(row)
            row.value_json = json.dumps({"value": total}, ensure_ascii=False)
            row.updated_at = now_local_naive()

    # =====================================================================
    # 订单
    # =====================================================================

    def get_order(self, order_id: str) -> Order | None:
        with self._session() as session:
            row = session.get(OrderORM, str(order_id))
            return self._order_to_model(row) if row is not None else None

    def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        with self._session() as session:
            row = session.execute(
                select(OrderORM).where(OrderORM.client_order_id == str(client_order_id))
            ).scalars().first()
            return self._order_to_model(row) if row is not None else None

    def save_order(self, order: Order) -> None:
        with self._session() as session:
            row = session.get(OrderORM, order.id)
            if row is None:
                row = OrderORM(id=order.id)
                session.add(row)
            row.client_order_id = order.client_order_id
            row.account_id = order.account_id
            row.symbol = str(order.symbol).strip().upper()
            row.side = _enum_text(order.side)
            row.order_type = _enum_text(order.order_type) or "LIMIT"
            row.price = None if order.price is None else float(order.price)
            row.quantity = int(order.quantity)
            row.filled_quantity = int(order.filled_quantity)
            row.avg_fill_price = None if order.avg_fill_price is None else float(order.avg_fill_price)
            row.status = _enum_text(order.status) or "PENDING"
            row.source = _enum_text(order.source) or "MANUAL"
            row.strategy_instance_id = order.strategy_instance_id
            row.reject_reason = order.reject_reason
            row.reject_message = order.reject_message
            row.risk_checks_json = (
                json.dumps(order.risk_checks, ensure_ascii=False, default=str)
                if order.risk_checks
                else None
            )
            row.trace_id = order.trace_id
            row.created_at = _dt(order.created_at)
            row.submitted_at = None if order.submitted_at is None else _dt(order.submitted_at)
            row.finished_at = None if order.finished_at is None else _dt(order.finished_at)
            row.updated_at = _dt(order.updated_at)

    def list_orders(
        self, account_id: str, statuses: Iterable[str] | None = None, limit: int = 200
    ) -> list[Order]:
        with self._session() as session:
            statement = select(OrderORM).where(OrderORM.account_id == account_id)
            if statuses is not None:
                allowed = [_enum_text(status) for status in statuses]
                if allowed:
                    statement = statement.where(OrderORM.status.in_(allowed))
            statement = statement.order_by(OrderORM.created_at.desc()).limit(int(limit))
            rows = session.execute(statement).scalars().all()
            return [self._order_to_model(row) for row in rows]

    # =====================================================================
    # ORM → 领域模型
    # =====================================================================

    @staticmethod
    def _account_to_model(row: AccountORM) -> Account:
        return Account(
            id=row.id,
            name=row.name,
            broker=row.broker,
            base_currency=row.base_currency,
            initial_cash=float(row.initial_cash),
            cash=float(row.cash),
            frozen_cash=float(row.frozen_cash),
            total_asset=float(row.total_asset),
            enabled=bool(row.enabled),
            updated_at=row.updated_at,
        )

    @staticmethod
    def _position_to_model(row: PositionORM) -> Position:
        return Position(
            account_id=row.account_id,
            symbol=row.symbol,
            quantity=int(row.quantity),
            available_quantity=int(row.available_quantity),
            frozen_quantity=int(row.frozen_quantity),
            today_buy_quantity=int(row.today_buy_quantity),
            avg_cost=float(row.avg_cost),
            last_price=float(row.last_price),
            market_value=float(row.market_value),
            updated_at=row.updated_at,
        )

    @staticmethod
    def _trade_to_model(row: TradeORM) -> Fill:
        return Fill(
            id=row.id,
            order_id=row.order_id,
            account_id=row.account_id,
            symbol=row.symbol,
            side=row.side,
            price=float(row.price),
            quantity=int(row.quantity),
            amount=float(row.amount),
            commission=float(row.commission),
            stamp_tax=float(row.stamp_tax),
            transfer_fee=float(row.transfer_fee),
            reg_fee=float(row.reg_fee),
            fee_total=float(row.fee_total),
            traded_at=row.traded_at,
            source=row.source,
        )

    @staticmethod
    def _order_to_model(row: OrderORM) -> Order:
        checks: list[dict[str, Any]] = []
        if row.risk_checks_json:
            try:
                parsed = json.loads(row.risk_checks_json)
                checks = parsed if isinstance(parsed, list) else []
            except (TypeError, ValueError):
                checks = []
        return Order(
            id=row.id,
            client_order_id=row.client_order_id,
            account_id=row.account_id,
            symbol=row.symbol,
            side=row.side,
            order_type=row.order_type,
            price=None if row.price is None else float(row.price),
            quantity=int(row.quantity),
            filled_quantity=int(row.filled_quantity),
            avg_fill_price=None if row.avg_fill_price is None else float(row.avg_fill_price),
            status=row.status,
            source=row.source,
            strategy_instance_id=row.strategy_instance_id,
            reject_reason=row.reject_reason,
            reject_message=row.reject_message,
            risk_checks=checks,
            trace_id=row.trace_id,
            created_at=row.created_at,
            submitted_at=row.submitted_at,
            finished_at=row.finished_at,
            updated_at=row.updated_at,
        )


__all__ = ["SqlTradingRepository"]

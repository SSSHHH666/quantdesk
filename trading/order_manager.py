"""QuantDesk 订单服务（M2-05 / M2-06）—— **全系统唯一下单入口**。

契约
----
- CF-6：``OrderService.place/preview/cancel`` 为唯一入口；
  ``place`` 内部**必须先调用** :meth:`trading.risk.RiskGate.check`，通过后才允许提交 broker。
- CF-4：broker 只通过 :class:`trading.broker.BrokerAdapter` 交互。

唯一入口纪律（M2-06，代码评审 + 单测双重保证）
----------------------------------------------
1. UI、自动化 job、脚本一律只能调用本模块的 ``OrderService`` / 模块级 ``place_order``；
   **禁止**直接调用 ``BrokerAdapter.submit_order``（AC-F6-01）。
2. ``broker_registry`` 只允许本模块（订单服务装配）使用；PaperBroker 由本模块的
   ``build_order_service`` 装配，其它模块不要自行创建 broker 实例。
3. ``place`` 中顺序固定：**幂等检查 → 建单 → RiskGate.check → 风控拒绝则落库 REJECTED →
   通过才 broker.submit_order → 成交入账 → 事件/审计**。任何路径都不会跳过风控。

依赖注入（不自己建引擎/会话）
------------------------------
::

    repo = InMemoryTradingRepository()                 # 或 core/db.py 注入的 SQL 实现
    portfolio = PortfolioService(repo)
    broker = PaperBroker(portfolio, prices=...)
    service = build_order_service(
        account_id="ACC-1", broker=broker, risk_gate=RiskGate(config.risk),
        repository=repo, portfolio=portfolio, market_provider=lambda: snapshot,
    )

事件与审计（SPEC §3.3 / CF-11）
--------------------------------
- 发布 ``order.submitted`` / ``order.filled`` / ``order.rejected`` / ``order.cancelled`` /
  ``risk.blocked``；payload 必填字段见 ``core.events.TOPIC_PAYLOAD_FIELDS``。
- 审计写 ``order.place``（成功/拒绝/失败）与 ``order.cancel``；``trace_id`` 贯穿全程。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Callable, Iterable

from backtest.broker_sim import FeeModel, normalize_side, round_money, SIDE_BUY, SIDE_SELL
from core.events import EventBus, Topic, get_event_bus, validate_payload
from core.logging import AuditAction, AuditResult, get_trace_id, new_trace_id, write_audit
from core.models import (
    AuditActor,
    FeeBreakdown,
    FeeConfig,
    Fill,
    MarketSnapshot,
    Order,
    OrderRequest,
    OrderResult,
    OrderSource,
    OrderStatus,
    RiskDecision,
    RiskPreview,
    now_local,
)
from trading.broker import BrokerAdapter, BrokerError
from trading.portfolio import PortfolioService, TradingRepository, make_fill_id
from trading.risk import RiskGate

logger = logging.getLogger(__name__)

_EPS = 1e-6

#: 订单状态机合法迁移（SPEC §5.2 冻结；终态不可再变）
ORDER_TRANSITIONS: dict[str, frozenset[str]] = {
    OrderStatus.PENDING.value: frozenset(
        {
            OrderStatus.SUBMITTED.value,
            OrderStatus.REJECTED.value,
            OrderStatus.CANCELLED.value,
        }
    ),
    OrderStatus.SUBMITTED.value: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED.value,
            OrderStatus.FILLED.value,
            OrderStatus.CANCELLED.value,
            OrderStatus.REJECTED.value,
            OrderStatus.EXPIRED.value,
        }
    ),
    OrderStatus.PARTIALLY_FILLED.value: frozenset(
        {
            OrderStatus.FILLED.value,
            OrderStatus.CANCELLED.value,
            OrderStatus.EXPIRED.value,
        }
    ),
    OrderStatus.FILLED.value: frozenset(),
    OrderStatus.CANCELLED.value: frozenset(),
    OrderStatus.REJECTED.value: frozenset(),
    OrderStatus.EXPIRED.value: frozenset(),
}


def _status_value(status: Any) -> str:
    """把 ``OrderStatus``/字符串统一成 ``"FILLED"`` 形式。"""
    return str(getattr(status, "value", status) or "").strip().upper()


def _enum_value(value: Any, default: str = "") -> str:
    """把枚举/字符串统一成大写字符串。"""
    raw = getattr(value, "value", value)
    return str(raw).strip().upper() if raw is not None else default


def _default_audit_writer(
    action: str,
    target: tuple[str, str] | None,
    before: Any,
    after: Any,
    actor: str,
    trace_id: str | None,
    result: str,
) -> bool:
    """默认审计写入器：调用 ``core.logging.write_audit``；失败只记日志、不影响交易主流程。"""
    try:
        return write_audit(
            action,
            target,
            before=before,
            after=after,
            actor=actor,
            trace_id=trace_id,
            result=result,
        )
    except Exception:  # noqa: BLE001 - 审计失败不得阻塞下单（SPEC §11.3）
        logger.error("写审计失败（不阻塞主流程）：action=%s trace_id=%s", action, trace_id, exc_info=True)
        return False


class OrderService:
    """订单生命周期服务（唯一入口，CF-6）。

    :param account_id: 本服务绑定的账户 ID（MVP 单账户；多账户时每账户一个服务实例）。
    :param broker: 已装配的 broker 适配器（PaperBroker 等）。
    :param risk_gate: 风控闸门（**必须**传入；``place`` 每次都会调用）。
    :param portfolio: 账户/持仓服务。
    :param repository: 交易域仓储（订单落库）。
    :param market_provider: 返回 :class:`MarketSnapshot` 的可调用对象（风控/成交报价用）。
    :param fee_config: 费率配置（用于 ``preview`` 费用明细与成交兜底费用）。
    :param event_bus: 事件总线（默认全局总线；测试可注入独立总线）。
    :param audit_writer: 审计写入器（默认 ``core.logging.write_audit``；测试可注入假函数）。
    :param clock: 时钟（返回 ``datetime``），默认 ``core.models.now_local``。
    """

    def __init__(
        self,
        account_id: str,
        broker: BrokerAdapter,
        risk_gate: RiskGate,
        portfolio: PortfolioService,
        repository: TradingRepository,
        market_provider: Callable[[], MarketSnapshot],
        *,
        fee_config: FeeConfig | None = None,
        event_bus: EventBus | None = None,
        audit_writer: Callable[..., bool] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(account_id, str) or not account_id.strip():
            raise ValueError("OrderService 初始化失败：account_id 不能为空")
        if not isinstance(broker, BrokerAdapter):
            raise TypeError("OrderService 初始化失败：broker 必须是 BrokerAdapter 子类实例")
        if not isinstance(risk_gate, RiskGate):
            raise TypeError("OrderService 初始化失败：risk_gate 必须是 RiskGate（禁止绕过风控）")
        if not callable(market_provider):
            raise TypeError("OrderService 初始化失败：market_provider 必须是可调用对象")
        self.account_id = account_id.strip()
        self.broker = broker
        self.risk_gate = risk_gate
        self.portfolio = portfolio
        self.repository = repository
        self._market_provider = market_provider
        self.fee_model = FeeModel(fee_config)
        self.fee_config = fee_config or FeeConfig()
        self.event_bus = event_bus or get_event_bus()
        self._audit_writer = audit_writer or _default_audit_writer
        self._clock = clock or now_local

    # =====================================================================
    # 唯一入口 1：下单
    # =====================================================================

    def place(self, req: OrderRequest) -> OrderResult:
        """提交一笔订单（唯一入口）。

        流程（AC-F6-01 可验证顺序）：幂等检查 → 在内存构建订单 →
        组装行情快照 → **``RiskGate.check`` 先判定** → 拒绝则订单落库 REJECTED 并发
        ``risk.blocked``/``order.rejected``；通过才落库 SUBMITTED 并调用 ``broker.submit_order``
        → 成交入账（PortfolioService）→ 更新状态 → 事件 + 审计。
        即：**风控判定发生在任何订单落库/券商提交之前**（风控为纯函数、无副作用）。

        :return: :class:`OrderResult`（成功/失败均带中文说明；失败带 RK/BR 原因码）。
        """
        trace_id = (req.trace_id or get_trace_id() or new_trace_id()).strip()
        client_order_id = (req.client_order_id or "").strip() or uuid.uuid4().hex

        # 1) 幂等：同一 client_order_id 重复提交只产生 1 条订单
        existing = self.repository.get_order_by_client_id(client_order_id)
        if existing is not None:
            logger.info("重复提交被幂等拦截：client_order_id=%s order_id=%s", client_order_id, existing.id)
            return OrderResult.from_order(existing, message="重复提交：返回已有委托结果（未重复下单）")

        # 2) 参数/账户一致性校验（缺失属于调用方 bug，返回 RK018 且不落库）
        if str(req.account_id or "").strip() and str(req.account_id).strip() != self.account_id:
            return OrderResult.failure(
                "RK018",
                f"委托账户（{req.account_id}）与订单服务账户（{self.account_id}）不一致，已拒绝",
                trace_id=trace_id,
            )

        now = self._clock()
        try:
            order = self._build_order(req, client_order_id, trace_id, now)
        except Exception as exc:  # noqa: BLE001 - 参数非法不落库，直接返回 RK018（fail-closed）
            return OrderResult.failure("RK018", f"下单请求参数非法，已拒绝：{exc}", trace_id=trace_id)

        try:
            # 行情快照原样传给 RiskGate：其扩展字段（universe / price_limits / listed_days /
            # existing_client_order_ids / recent_order_times / as_of / automation_enabled 等）
            # 通过 core.models.QDModel(extra="allow") 透传，缺失时风控对应检查标记 skipped、
            # 不会误拒绝（Lead 裁决 2026-10-25；组装入口见 assemble_market_snapshot）。
            market = self._market_provider()
        except Exception as exc:  # noqa: BLE001 - fail-closed：拿不到行情快照即拒单
            return self._finish_rejected(
                order, "RK006", f"获取行情快照失败，已按 fail-closed 拒绝：{exc}", trace_id,
                actor=self._actor(order),
            )

        account = self.portfolio.get_account(self.account_id)
        positions = self.portfolio.get_positions(self.account_id)

        # 3) 风控（唯一风控入口，必须先于落库/提交 broker；AC-F6-01 断言点：
        #    RiskGate.check 必须在 repository.save_order 与 broker.submit_order 之前调用）
        decision = self.risk_gate.check(req, account, positions, market)
        order.risk_checks = list(decision.checks)
        if not decision.allow:
            return self._reject_from_risk(order, decision, trace_id)

        # 4) 通过风控 → 落库 SUBMITTED → 提交 broker（全系统唯一调用点）
        order.status = OrderStatus.SUBMITTED
        order.submitted_at = now
        order.updated_at = now
        self.repository.save_order(order)
        self._publish(Topic.ORDER_SUBMITTED, {
            "trace_id": trace_id,
            "order_id": order.id,
            "symbol": order.symbol,
            "side": _enum_value(order.side),
            "price": order.price,
            "qty": order.quantity,
            "source": _enum_value(order.source),
        }, trace_id)

        try:
            broker_req = self._attach_order_id(req, order.id)
            result = self.broker.submit_order(broker_req)
        except BrokerError as exc:
            return self._finish_rejected(order, exc.code, str(exc), trace_id, actor=self._actor(order))
        except Exception as exc:  # noqa: BLE001 - 适配器异常统一转 BR001，本地数据不被破坏
            return self._finish_rejected(
                order, "BR001", f"券商适配器异常：{type(exc).__name__}: {exc}", trace_id,
                actor=self._actor(order),
            )

        return self._apply_broker_result(order, result, trace_id)

    # =====================================================================
    # 唯一入口 2：只读预检（UI 手动交易页）
    # =====================================================================

    def preview(self, req: OrderRequest) -> RiskPreview:
        """只读预检：风控结论 + 费用明细 + 成交后仓位占比（不落库、不下单）。

        :raises ValueError: 账户不存在等基础数据缺失（中文提示）。
        """
        market = self._market_provider()
        account = self.portfolio.get_account(self.account_id)
        positions = self.portfolio.get_positions(self.account_id)
        decision = self.risk_gate.check(req, account, positions, market)

        side = normalize_side(req.side)
        symbol = str(req.symbol or "").strip().upper()
        quantity = int(req.quantity or 0)
        price = req.price if req.price is not None else self._latest_price(market, symbol)
        total_asset = float(account.total_asset or 0.0)
        position = next((item for item in positions if item.symbol == symbol), None)
        position_value = float(position.market_value) if position else 0.0
        total_position_value = sum(float(item.market_value) for item in positions)

        fee: FeeBreakdown | None = None
        estimated_amount = 0.0
        if price is not None and quantity > 0:
            estimated_amount = round_money(float(price) * quantity)
            fee = self.fee_model.calculate(side, float(price), quantity)
        fee_total = float(fee.total) if fee else 0.0

        if side == SIDE_BUY:
            cash_after = round_money(account.cash - estimated_amount - fee_total)
            post_single = ((position_value + estimated_amount) / total_asset * 100.0) if total_asset else 0.0
            post_total = ((total_position_value + estimated_amount) / total_asset * 100.0) if total_asset else 0.0
        else:
            cash_after = round_money(account.cash + estimated_amount - fee_total)
            post_single = ((position_value - estimated_amount) / total_asset * 100.0) if total_asset else 0.0
            post_total = ((total_position_value - estimated_amount) / total_asset * 100.0) if total_asset else 0.0
        post_single = round(max(post_single, 0.0), 4)
        post_total = round(max(post_total, 0.0), 4)

        available_to_buy = 0
        if price is not None and float(price) > 0:
            available_to_buy = int(float(account.cash) / (float(price) * 100.0)) * 100
        available_to_sell = int(position.available_quantity) if position else 0

        return RiskPreview(
            decision=decision,
            price=float(price) if price is not None else None,
            quantity=quantity,
            estimated_amount=estimated_amount,
            fee=fee,
            cash_after=cash_after,
            post_trade_position_pct=post_single,
            post_trade_total_position_pct=post_total,
            available_to_buy=available_to_buy,
            available_to_sell=available_to_sell,
            message=decision.message,
        )

    # =====================================================================
    # 唯一入口 3：撤单
    # =====================================================================

    def cancel(self, order_id: str) -> OrderResult:
        """撤销订单；仅 ``SUBMITTED`` / ``PARTIALLY_FILLED`` 可撤（SPEC §8.4）。"""
        trace_id = get_trace_id() or new_trace_id()
        order = self.repository.get_order(str(order_id))
        if order is None:
            return OrderResult.failure("BR002", f"撤单失败：订单不存在（{order_id}）", order_id=str(order_id), trace_id=trace_id)
        current = _status_value(order.status)
        if current not in {OrderStatus.SUBMITTED.value, OrderStatus.PARTIALLY_FILLED.value}:
            if current == OrderStatus.FILLED.value:
                message = "撤单失败：订单已成交（FILLED），不可撤单"
            elif current == OrderStatus.CANCELLED.value:
                message = "撤单失败：订单已撤销（CANCELLED），不可重复撤单"
            elif current in {OrderStatus.REJECTED.value, OrderStatus.EXPIRED.value}:
                message = f"撤单失败：订单已终结（{current}），不可撤单"
            else:
                message = f"撤单失败：订单当前状态为 {current}，只有 SUBMITTED/PARTIALLY_FILLED 可撤"
            return OrderResult.failure(
                "BR002",
                message,
                order_id=order.id,
                status=order.status,
                trace_id=trace_id,
            )
        try:
            result = self.broker.cancel_order(order.id)
        except BrokerError as exc:
            return OrderResult.failure(exc.code, str(exc), order_id=order.id, status=order.status, trace_id=trace_id)
        except Exception as exc:  # noqa: BLE001
            return OrderResult.failure(
                "BR001", f"撤单异常：{type(exc).__name__}: {exc}", order_id=order.id,
                status=order.status, trace_id=trace_id,
            )

        if not bool(getattr(result, "success", False)):
            code = str(getattr(result, "reason_code", None) or "BR002")
            message = str(getattr(result, "message", "") or "券商拒绝撤单")
            return OrderResult.failure(code, message, order_id=order.id, status=order.status, trace_id=trace_id)

        now = self._clock()
        cancelled_qty = max(int(order.quantity) - int(order.filled_quantity), 0)
        order.status = OrderStatus.CANCELLED
        order.finished_at = now
        order.updated_at = now
        self.repository.save_order(order)
        self._publish(Topic.ORDER_CANCELLED, {
            "trace_id": trace_id,
            "order_id": order.id,
            "cancelled_qty": cancelled_qty,
        }, trace_id)
        self._audit(
            AuditAction.ORDER_CANCEL, ("order", order.id), None,
            self._order_snapshot(order), self._actor(order), trace_id, AuditResult.SUCCESS.value,
        )
        return OrderResult(
            success=True,
            order_id=order.id,
            client_order_id=order.client_order_id,
            status=OrderStatus.CANCELLED,
            filled_quantity=order.filled_quantity,
            avg_fill_price=order.avg_fill_price,
            message=f"撤单成功，撤销 {cancelled_qty} 股",
            order=order,
            trace_id=trace_id,
        )

    # =====================================================================
    # 查询辅助（UI 只读）
    # =====================================================================

    def get_order(self, order_id: str) -> Order | None:
        """按订单号查询（不存在返回 None）。"""
        return self.repository.get_order(str(order_id))

    def list_orders(self, statuses: Iterable[str] | None = None, limit: int = 200) -> list[Order]:
        """查询本账户订单（默认最新在前）。"""
        return self.repository.list_orders(self.account_id, statuses=statuses, limit=limit)

    def list_today_orders(self, limit: int = 200) -> list[Order]:
        """查询当日订单（手动交易页“今日委托”）。"""
        today = self._clock().date()
        return [
            order for order in self.list_orders(limit=limit)
            if order.created_at.date() == today
        ]

    # =====================================================================
    # 内部：订单构建 / 状态流转 / 结果处理
    # =====================================================================

    def _build_order(
        self,
        req: OrderRequest,
        client_order_id: str,
        trace_id: str,
        now: datetime,
    ) -> Order:
        """由请求构建 PENDING 订单（字段做安全归一化）。"""
        source = _enum_value(req.source, OrderSource.MANUAL.value) or OrderSource.MANUAL.value
        side = _enum_value(req.side)
        order_type = _enum_value(req.order_type, "LIMIT") or "LIMIT"
        try:
            quantity = int(req.quantity)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"委托数量必须是整数（当前 {req.quantity!r}）") from exc
        return Order(
            id=uuid.uuid4().hex,
            client_order_id=client_order_id,
            account_id=self.account_id,
            symbol=str(req.symbol or "").strip().upper(),
            side=side,
            order_type=order_type,
            price=float(req.price) if req.price is not None else None,
            quantity=quantity,
            filled_quantity=0,
            status=OrderStatus.PENDING,
            source=source,
            strategy_instance_id=req.strategy_instance_id,
            trace_id=trace_id,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _attach_order_id(req: OrderRequest, order_id: str) -> OrderRequest:
        """把内部订单号附加到请求上（``QDModel(extra="allow")`` 透传，不修改原请求）。

        BrokerAdapter 契约只接收 ``OrderRequest``；订单号由 OrderService 生成，
        通过扩展字段透传给适配器，适配器回填到 ``OrderResult.order_id``。
        """
        if hasattr(req, "model_copy"):
            copy = req.model_copy()
            copy.order_id = order_id  # type: ignore[attr-defined]
            return copy
        return req  # pragma: no cover - 非 pydantic 请求（测试鸭子类型）

    def _apply_broker_result(self, order: Order, result: OrderResult, trace_id: str) -> OrderResult:
        """把 broker 返回结果落到订单/持仓/事件/审计。"""
        now = self._clock()
        status = _status_value(getattr(result, "status", OrderStatus.PENDING))
        success = bool(getattr(result, "success", False))
        order_id_from_broker = str(getattr(result, "order_id", None) or order.id)
        if order_id_from_broker != order.id:
            logger.warning(
                "broker 返回的 order_id 与本地不一致：本地=%s 券商=%s（以本地为准）",
                order.id, order_id_from_broker,
            )
        filled_qty = int(getattr(result, "filled_quantity", 0) or 0)
        avg_price = getattr(result, "avg_fill_price", None)

        # 券商拒单（未成交）
        if not success and filled_qty <= 0 and status != OrderStatus.CANCELLED.value:
            code = str(getattr(result, "reason_code", None) or "BR002")
            message = str(getattr(result, "message", "") or "券商拒绝该委托")
            return self._finish_rejected(order, code, message, trace_id, actor=self._actor(order))

        # 成交入账（FILLED / PARTIALLY_FILLED）
        if filled_qty > 0:
            fill_price = float(avg_price if avg_price is not None else (order.price or 0.0))
            fee = getattr(result, "fee", None) or self.fee_model.calculate(
                _enum_value(order.side), fill_price, filled_qty
            )
            fill = self._build_fill(order, fill_price, filled_qty, fee, now)
            try:
                self.portfolio.apply_fill(fill)
                # AC-F4-01：成交后更新当日资金曲线（同交易日幂等 upsert；失败不阻塞订单）
                try:
                    self.portfolio.snapshot_equity(self.account_id, now.date())
                except Exception:  # noqa: BLE001
                    logger.error(
                        "成交后写资金曲线失败（不阻塞订单）：account_id=%s", self.account_id, exc_info=True
                    )
            except Exception as exc:  # noqa: BLE001 - 本地入账失败：订单拒绝并保留原因
                return self._finish_rejected(
                    order, "BR001",
                    f"成交入账失败（本地账户未变更）：{type(exc).__name__}: {exc}",
                    trace_id, actor=self._actor(order),
                )
            order.filled_quantity = filled_qty
            order.avg_fill_price = round_money(fill_price)
            self._publish(Topic.ORDER_FILLED, {
                "trace_id": trace_id,
                "order_id": order.id,
                "price": order.avg_fill_price,
                "qty": filled_qty,
                "fee_breakdown": {
                    "commission": fee.commission,
                    "stamp_tax": fee.stamp_tax,
                    "transfer_fee": fee.transfer_fee,
                    "reg_fee": fee.reg_fee,
                    "total": fee.total,
                },
            }, trace_id)

        order.status = status if status in {item.value for item in OrderStatus} else OrderStatus.SUBMITTED.value
        if order.status in {OrderStatus.FILLED.value, OrderStatus.CANCELLED.value, OrderStatus.EXPIRED.value}:
            order.finished_at = now
        order.updated_at = now
        self.repository.save_order(order)

        self._audit(
            AuditAction.ORDER_PLACE, ("order", order.id), None,
            self._order_snapshot(order), self._actor(order), trace_id, AuditResult.SUCCESS.value,
        )
        return OrderResult(
            success=success or filled_qty > 0,
            order_id=order.id,
            client_order_id=order.client_order_id,
            status=order.status,
            filled_quantity=order.filled_quantity,
            avg_fill_price=order.avg_fill_price,
            fee=getattr(result, "fee", None),
            message=str(getattr(result, "message", "") or "委托已提交"),
            order=order,
            trace_id=trace_id,
        )

    def _build_fill(
        self,
        order: Order,
        price: float,
        quantity: int,
        fee: FeeBreakdown,
        now: datetime,
    ) -> Fill:
        """由订单 + 成交结果构建 ``Fill``（供 PortfolioService 入账）。"""
        return Fill(
            id=make_fill_id(),
            order_id=order.id,
            account_id=self.account_id,
            symbol=order.symbol,
            side=_enum_value(order.side),
            price=round_money(price),
            quantity=int(quantity),
            amount=round_money(price * quantity),
            commission=float(fee.commission),
            stamp_tax=float(fee.stamp_tax),
            transfer_fee=float(fee.transfer_fee),
            reg_fee=float(fee.reg_fee),
            fee_total=float(fee.total),
            slippage_cost=float(fee.slippage_cost),
            trade_date=now.date(),
            traded_at=now,
            source=_enum_value(order.source),
            trace_id=order.trace_id,
        )

    def _reject_from_risk(self, order: Order, decision: RiskDecision, trace_id: str) -> OrderResult:
        """风控拒绝：订单落库 REJECTED，发 ``risk.blocked`` + ``order.rejected``，写审计。"""
        now = self._clock()
        order.status = OrderStatus.REJECTED
        order.reject_reason = decision.reason_code
        order.reject_message = decision.message
        order.finished_at = now
        order.updated_at = now
        self.repository.save_order(order)
        self._publish(Topic.RISK_BLOCKED, {
            "trace_id": trace_id,
            "symbol": order.symbol,
            "side": _enum_value(order.side),
            "reason_code": decision.reason_code,
            "details": decision.details,
        }, trace_id)
        self._publish(Topic.ORDER_REJECTED, {
            "trace_id": trace_id,
            "order_id": order.id,
            "reason_code": decision.reason_code,
            "message": decision.message,
        }, trace_id)
        self._audit(
            AuditAction.ORDER_PLACE, ("order", order.id), None,
            self._order_snapshot(order), self._actor(order), trace_id, AuditResult.DENIED.value,
        )
        return OrderResult(
            success=False,
            order_id=order.id,
            client_order_id=order.client_order_id,
            status=OrderStatus.REJECTED,
            reason_code=decision.reason_code,
            message=decision.message,
            order=order,
            trace_id=trace_id,
        )

    def _finish_rejected(
        self,
        order: Order,
        reason_code: str,
        message: str,
        trace_id: str,
        actor: str,
        *,
        status: OrderStatus = OrderStatus.REJECTED,
    ) -> OrderResult:
        """通用拒单收尾（券商/适配器/入账异常）：落库 + 事件 + 审计。"""
        now = self._clock()
        order.status = status
        order.reject_reason = str(reason_code)
        order.reject_message = message
        order.finished_at = now
        order.updated_at = now
        self.repository.save_order(order)
        self._publish(Topic.ORDER_REJECTED, {
            "trace_id": trace_id,
            "order_id": order.id,
            "reason_code": str(reason_code),
            "message": message,
        }, trace_id)
        self._audit(
            AuditAction.ORDER_PLACE, ("order", order.id), None,
            self._order_snapshot(order), actor, trace_id, AuditResult.FAILED.value,
        )
        return OrderResult(
            success=False,
            order_id=order.id,
            client_order_id=order.client_order_id,
            status=status,
            reason_code=str(reason_code),
            message=message,
            order=order,
            trace_id=trace_id,
        )

    # =====================================================================
    # 内部：事件 / 审计 / 小工具
    # =====================================================================

    def _publish(self, topic: str, payload: dict[str, Any], trace_id: str) -> None:
        """发布事件；payload 必填字段自检，异常隔离（不阻塞交易主流程）。"""
        missing = validate_payload(topic, payload)
        if missing:
            logger.error("事件 payload 缺少必填字段：topic=%s missing=%s", topic, missing)
        try:
            self.event_bus.publish(topic, payload, trace_id)
        except Exception:  # noqa: BLE001 - 事件失败不影响订单结果
            logger.error("发布事件失败：topic=%s trace_id=%s", topic, trace_id, exc_info=True)

    def _audit(
        self,
        action: str,
        target: tuple[str, str] | None,
        before: Any,
        after: Any,
        actor: str,
        trace_id: str,
        result: str,
    ) -> None:
        """写审计（失败只记日志）。"""
        try:
            self._audit_writer(action, target, before, after, actor, trace_id, result)
        except Exception:  # noqa: BLE001 - 审计失败不得阻塞交易主流程
            logger.error("审计写入异常：action=%s trace_id=%s", action, trace_id, exc_info=True)

    def _actor(self, order: Order) -> str:
        """审计 actor：手动单 ``AuditActor.MANUAL``，自动单 ``AuditActor.AUTO``。

        口径（Lead 裁决 2026-10-25）：``AuditActor.AUTO`` 保留，用于自动化下单审计，
        与手动单在 ``audit_logs.actor`` 上可区分（CF-11）。
        """
        if _enum_value(order.source) == OrderSource.AUTO.value:
            return AuditActor.AUTO.value
        return AuditActor.MANUAL.value

    @staticmethod
    def _order_snapshot(order: Order) -> dict[str, Any]:
        """订单快照（审计 after_json 用，避免把 ORM/大对象写库）。"""
        return {
            "id": order.id,
            "client_order_id": order.client_order_id,
            "account_id": order.account_id,
            "symbol": order.symbol,
            "side": _enum_value(order.side),
            "order_type": _enum_value(order.order_type),
            "price": order.price,
            "quantity": order.quantity,
            "filled_quantity": order.filled_quantity,
            "avg_fill_price": order.avg_fill_price,
            "status": _status_value(order.status),
            "source": _enum_value(order.source),
            "reject_reason": order.reject_reason,
            "reject_message": order.reject_message,
            "trace_id": order.trace_id,
        }

    @staticmethod
    def _latest_price(market: MarketSnapshot, symbol: str) -> float | None:
        """从行情快照取最新价（大小写不敏感）。"""
        prices = getattr(market, "latest_prices", {}) or {}
        if symbol in prices:
            return float(prices[symbol])
        for key, value in prices.items():
            if str(key).strip().upper() == symbol:
                return float(value)
        return None

    def _transition_allowed(self, current: str, target: str) -> bool:
        """状态机校验（非法流转拒绝）。"""
        return target in ORDER_TRANSITIONS.get(current, frozenset())


# ``_audit`` 方法内部需要一个稳定的可调用引用，构造时绑定（避免实例属性与方法的命名冲突）


# =====================================================================
# 工厂 / 单例（UI 与自动化只通过这里拿 OrderService）
# =====================================================================

# =====================================================================
# 行情快照组装（风控扩展字段透传）
# =====================================================================


def assemble_market_snapshot(
    *,
    trade_date: Any,
    is_trading_time: bool,
    latest_prices: dict[str, float],
    price_times: dict[str, Any] | None = None,
    suspended: Iterable[str] | None = None,
    delisting: Iterable[str] | None = None,
    daily_open_asset: float = 0.0,
    realized_pnl_today: float = 0.0,
    fused: bool = False,
    stale_threshold_hours: float = 24.0,
    **extensions: Any,
) -> MarketSnapshot:
    """组装 ``MarketSnapshot``，并透传风控所需的**可选扩展字段**。

    Lead 裁决（2026-10-25）：风控扩展信息（``universe`` / ``price_limits`` /
    ``listed_days`` / ``existing_client_order_ids`` / ``recent_order_times`` /
    ``as_of`` / ``automation_enabled`` / ``is_fused`` 等）走
    ``core.models.QDModel(extra="allow")`` 透传，**不修改冻结契约**；
    缺失时 ``RiskGate`` 对应检查标记 skipped，不会误拒绝。

    用法::

        snapshot = assemble_market_snapshot(
            trade_date=date.today(), is_trading_time=True,
            latest_prices={"600519.SH": 1680.0},
            price_times={"600519.SH": datetime.now()},
            universe=config.data.universe,
            price_limits={"600519.SH": (1848.0, 1512.0)},
            listed_days={"600519.SH": 5000},
            as_of=datetime.now(),
        )

    :param trade_date: 交易日（``date`` 或 ``YYYY-MM-DD``）。
    :param is_trading_time: 是否处于可下单时段（风控 RK005 用）。
    :param latest_prices: ``{symbol: 最新价}``（风控 RK006/RK010 与金额计算用）。
    :param price_times: ``{symbol: 行情时间}``（风控 RK006 新鲜度用）。
    :param suspended: 停牌标的集合（风控 RK003）。
    :param delisting: 退市整理标的集合（风控 RK002）。
    :param daily_open_asset: 当日开盘总资产（熔断基线，风控 RK009）。
    :param realized_pnl_today: 当日已实现盈亏（展示用）。
    :param fused: 是否已熔断（``True`` 时直接 RK009）。
    :param stale_threshold_hours: 行情新鲜度阈值（小时，对应 ``data.stale_threshold_hours``）。
    :param extensions: 其余字段原样透传（见上方扩展字段列表）。
    """
    return MarketSnapshot(
        trade_date=trade_date,
        is_trading_time=bool(is_trading_time),
        latest_prices=dict(latest_prices or {}),
        price_times=dict(price_times or {}),
        suspended=set(suspended or ()),
        delisting=set(delisting or ()),
        daily_open_asset=float(daily_open_asset),
        realized_pnl_today=float(realized_pnl_today),
        fused=bool(fused),
        stale_threshold_hours=float(stale_threshold_hours),
        **extensions,
    )


_DEFAULT_SERVICE: OrderService | None = None


def build_order_service(
    *,
    account_id: str,
    broker: BrokerAdapter,
    risk_gate: RiskGate,
    repository: TradingRepository,
    portfolio: PortfolioService | None = None,
    market_provider: Callable[[], MarketSnapshot],
    fee_config: FeeConfig | None = None,
    event_bus: EventBus | None = None,
    audit_writer: Callable[..., bool] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> OrderService:
    """装配一个订单服务（组合根；UI/自动化不要自行 new OrderService）。"""
    portfolio = portfolio or PortfolioService(repository, clock=clock)
    return OrderService(
        account_id=account_id,
        broker=broker,
        risk_gate=risk_gate,
        portfolio=portfolio,
        repository=repository,
        market_provider=market_provider,
        fee_config=fee_config,
        event_bus=event_bus,
        audit_writer=audit_writer,
        clock=clock,
    )


def init_order_service(service: OrderService | None = None, **kwargs: Any) -> OrderService:
    """初始化全局订单服务单例（启动期调用；传入 ``service`` 或装配参数）。"""
    global _DEFAULT_SERVICE
    _DEFAULT_SERVICE = service or build_order_service(**kwargs)
    return _DEFAULT_SERVICE


def get_order_service() -> OrderService:
    """获取全局订单服务单例；未初始化时给中文可操作提示。"""
    if _DEFAULT_SERVICE is None:
        raise RuntimeError(
            "订单服务尚未初始化：请先调用 init_order_service()（UI/自动化禁止绕过 OrderService 直接下单）"
        )
    return _DEFAULT_SERVICE


def reset_order_service() -> None:
    """清空全局单例（测试隔离用）。"""
    global _DEFAULT_SERVICE
    _DEFAULT_SERVICE = None


def place_order(req: OrderRequest) -> OrderResult:
    """模块级便捷下单：等价 ``get_order_service().place(req)``（UI/自动化推荐入口）。"""
    return get_order_service().place(req)


def preview_order(req: OrderRequest) -> RiskPreview:
    """模块级便捷预检：等价 ``get_order_service().preview(req)``。"""
    return get_order_service().preview(req)


def cancel_order(order_id: str) -> OrderResult:
    """模块级便捷撤单：等价 ``get_order_service().cancel(order_id)``。"""
    return get_order_service().cancel(order_id)


__all__ = [
    "assemble_market_snapshot",
    "OrderService",
    "ORDER_TRANSITIONS",
    "build_order_service",
    "init_order_service",
    "get_order_service",
    "reset_order_service",
    "place_order",
    "preview_order",
    "cancel_order",
]

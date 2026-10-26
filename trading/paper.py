"""QuantDesk 模拟盘券商适配器（M2-02，PaperBroker）。

职责
----
- 实现 :class:`trading.broker.BrokerAdapter` 的 4 个必须方法 + 可选能力，注册名 ``paper``；
- **即时全额成交**（SPEC §4.8：模拟盘默认即时成交，不模拟委托排队）；
- 成交价 = 基准价（限价单用委托价 / 市价单用最新价）± 滑点（默认 5bp）；
- 费用**复用** CF-10 的 :class:`backtest.broker_sim.FeeModel`（不重复写费率）；
- 规则**复用** CF-10 的 :class:`backtest.broker_sim.AStockRules`（滑点/价格量化）；
- 成交后通过 :class:`trading.portfolio.PortfolioService` 入账（持仓/资金/流水/资金曲线），
  与回测共用同一套 A 股口径（SPEC §4.10）。

安全边界
--------
- 本模块**不调用** ``RiskGate``：风控唯一入口是 ``OrderService.place``（CF-6）；
  PaperBroker 只做“防御性二次校验”（现金/可用数量），防止绕过订单服务直接调用 adapter；
- 不访问网络、不写数据库（持久化由注入的 repository 完成）。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Callable

from backtest.broker_sim import (
    AStockRules,
    FeeModel,
    normalize_side,
    round_money,
    SIDE_BUY,
    SIDE_SELL,
)
from core.models import (
    Account,
    OrderRequest,
    OrderResult,
    OrderStatus,
    Position,
    now_local,
)
from trading.broker import BrokerAdapter, BrokerError, register_broker
from trading.order_manager import OrderService, build_order_service
from trading.portfolio import (
    InMemoryTradingRepository,
    PortfolioService,
    TradingRepository,
)
from trading.risk import RiskGate

logger = logging.getLogger(__name__)


@register_broker("paper", display_name="模拟盘")
class PaperBroker(BrokerAdapter):
    """模拟盘券商适配器（注册名 ``paper``，MVP 默认实现）。

    :param portfolio: 账户/持仓服务（内存或 DB 仓储均可）。
    :param account_id: 绑定的账户 ID（``get_account``/``get_positions`` 无参时使用）。
    :param prices: ``{symbol: 最新价}`` 可变字典（UI/自动化可通过 ``update_prices`` 刷新）。
    :param price_provider: 可选取价函数 ``(symbol) -> float | None``，优先级高于 ``prices``。
    :param slippage_bps: 滑点（基点，默认 5bp = 0.05%，读 ``fees.slippage_bps``）。
    :param fee_config: 费率配置（``core.models.FeeConfig``；默认用标准 A 股费率）。
    :param clock: 时钟（返回 ``datetime``），默认 ``core.models.now_local``。
    """

    name = "paper"
    display_name = "模拟盘"

    def __init__(
        self,
        portfolio: PortfolioService,
        account_id: str | None = None,
        *,
        prices: dict[str, float] | None = None,
        price_provider: Callable[[str], float | None] | None = None,
        slippage_bps: float = 5.0,
        fee_config: Any = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.portfolio = portfolio
        self.account_id = account_id
        self.prices: dict[str, float] = {str(k).upper(): float(v) for k, v in (prices or {}).items()}
        self.price_provider = price_provider
        self.slippage_bps = float(slippage_bps)
        self.fee_model = FeeModel(fee_config)
        self._clock = clock or now_local
        #: 本适配器提交过的订单状态（撤单校验用；模拟盘默认即时成交）
        self._orders: dict[str, str] = {}

    # =====================================================================
    # 行情辅助（UI/自动化可直接调用）
    # =====================================================================

    def update_prices(self, prices: dict[str, float]) -> None:
        """批量刷新最新价（供 UI 行情页/自动化 job 调用）。"""
        for symbol, price in (prices or {}).items():
            self.set_price(symbol, price)

    def set_price(self, symbol: str, price: float) -> None:
        """设置单个标的最新价；非法价格抛中文 ``ValueError``。"""
        try:
            value = float(price)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"最新价必须是数值（symbol={symbol!r} price={price!r}）") from exc
        if value <= 0:
            raise ValueError(f"最新价必须大于 0（symbol={symbol!r} price={price!r}）")
        self.prices[str(symbol).strip().upper()] = value

    def latest_price(self, symbol: str) -> float | None:
        """取最新价：``price_provider`` 优先，其次 ``prices``；缺失返回 ``None``。"""
        key = str(symbol or "").strip().upper()
        if self.price_provider is not None:
            try:
                value = self.price_provider(key)
            except Exception:  # noqa: BLE001 - 取价失败按缺失处理，由调用方拒单
                logger.error("price_provider 取价异常：symbol=%s", key, exc_info=True)
                value = None
            if value is not None:
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    number = 0.0
                if number > 0:
                    return number
        return self.prices.get(key)

    # =====================================================================
    # CF-4：4 个必须实现的方法
    # =====================================================================

    def submit_order(self, order: OrderRequest) -> OrderResult:
        """模拟成交：确定成交价（±滑点）→ 计算费用 → PortfolioService 入账 → 返回 FILLED。"""
        try:
            side = normalize_side(getattr(order, "side", None))
        except ValueError as exc:
            return OrderResult.failure("RK018", str(exc), trace_id=getattr(order, "trace_id", None))
        symbol = str(getattr(order, "symbol", "") or "").strip().upper()
        if not symbol:
            return OrderResult.failure("RK018", "委托缺少标的代码，无法成交", trace_id=getattr(order, "trace_id", None))
        try:
            quantity = int(getattr(order, "quantity", 0) or 0)
        except (TypeError, ValueError):
            quantity = 0
        if quantity <= 0:
            return OrderResult.failure("RK018", f"委托数量非法（{getattr(order, 'quantity', None)!r}）", trace_id=getattr(order, "trace_id", None))

        account_id = str(getattr(order, "account_id", "") or self.account_id or "").strip()
        if not account_id:
            return OrderResult.failure("RK020", "模拟盘未绑定账户，无法成交", trace_id=getattr(order, "trace_id", None))
        try:
            account = self.portfolio.get_account(account_id)
        except ValueError as exc:
            return OrderResult.failure("RK020", str(exc), trace_id=getattr(order, "trace_id", None))

        order_id = str(getattr(order, "order_id", "") or uuid.uuid4().hex)
        trace_id = getattr(order, "trace_id", None)

        # 基准价：限价单 = 委托价；市价单 = 最新价
        order_type = str(getattr(getattr(order, "order_type", None), "value", getattr(order, "order_type", "LIMIT"))).upper()
        base_price: float | None
        if order_type == "MARKET":
            base_price = self.latest_price(symbol)
            if base_price is None:
                return OrderResult.failure(
                    "RK006", f"市价单缺少标的 {symbol} 最新价，无法模拟成交（请刷新行情）",
                    order_id=order_id, trace_id=trace_id,
                )
        else:
            raw_price = getattr(order, "price", None)
            try:
                base_price = float(raw_price) if raw_price is not None else None
            except (TypeError, ValueError):
                base_price = None
            if base_price is None or base_price <= 0:
                return OrderResult.failure(
                    "RK018", f"限价单价格非法（{raw_price!r}）", order_id=order_id, trace_id=trace_id
                )

        # 成交价 = 基准价 ± 滑点（复用 AStockRules，四舍五入到 0.01 元）
        try:
            fill_price = AStockRules.apply_slippage(float(base_price), side, self.slippage_bps)
        except Exception as exc:  # noqa: BLE001
            return OrderResult.failure(
                "RK018", f"计算模拟成交价失败：{exc}", order_id=order_id, trace_id=trace_id
            )
        amount = round_money(fill_price * quantity)

        # 费用（CF-10 FeeModel 唯一实现）
        try:
            fee = self.fee_model.calculate(side, fill_price, quantity)
        except Exception as exc:  # noqa: BLE001
            return OrderResult.failure(
                "RK018", f"计算模拟盘费用失败：{exc}", order_id=order_id, trace_id=trace_id
            )
        slippage_cost = round_money(abs(fill_price - float(base_price)) * quantity)
        fee = fee.model_copy(update={"slippage_cost": slippage_cost})

        # 防御性校验：即使有人绕过 OrderService 直接调用 adapter，也不会产生透支/卖空
        positions = self.portfolio.get_positions(account_id)
        if side == SIDE_SELL:
            position = next((item for item in positions if item.symbol == symbol), None)
            held = int(position.quantity) if position else 0
            available = int(position.available_quantity) if position else 0
            if position is None or quantity > held:
                return OrderResult.failure(
                    "RK015", f"模拟盘拒单：持仓不足（持有 {held} 股，卖出 {quantity} 股）",
                    order_id=order_id, trace_id=trace_id,
                )
            if quantity > available:
                return OrderResult.failure(
                    "RK016",
                    f"模拟盘拒单：T+1 可用数量不足（可卖 {available} 股，卖出 {quantity} 股）",
                    order_id=order_id, trace_id=trace_id,
                )
        else:
            required_cash = round_money(amount + float(fee.total))
            if required_cash > round_money(account.available_cash) + 1e-6:
                return OrderResult.failure(
                    "RK014",
                    f"模拟盘拒单：可用资金不足（需 {required_cash:.2f} 元，"
                    f"可用 {round_money(account.available_cash):.2f} 元）",
                    order_id=order_id, trace_id=trace_id,
                )

        # 注意：成交入账（持仓/资金/流水/资金曲线）由 OrderService 调用
        # PortfolioService.apply_fill 完成（单一事实来源）；适配器只负责“模拟成交回报”，
        self._orders[order_id] = OrderStatus.FILLED.value
        return OrderResult(
            success=True,
            order_id=order_id,
            client_order_id=getattr(order, "client_order_id", None),
            status=OrderStatus.FILLED,
            filled_quantity=quantity,
            avg_fill_price=fill_price,
            fee=fee,
            message=(
                f"模拟盘成交：{symbol} {side} {quantity} 股 @ {fill_price:.2f} 元，"
                f"费用 {float(fee.total):.2f} 元"
            ),
            trace_id=trace_id,
            raw={"broker": self.name, "slippage_cost": slippage_cost},
        )

    def cancel_order(self, order_id: str) -> OrderResult:
        """撤单：模拟盘默认即时成交，仅对尚未成交/未知订单给出明确结果。"""
        status = self._orders.get(str(order_id))
        if status is None:
            return OrderResult.failure(
                "BR002", f"撤单失败：订单不存在或未由模拟盘受理（{order_id}）",
                order_id=str(order_id),
            )
        if status == OrderStatus.FILLED.value:
            return OrderResult.failure(
                "BR002", "撤单失败：该订单已成交，不可撤销（模拟盘默认即时全额成交）",
                order_id=str(order_id), status=OrderStatus.FILLED,
            )
        self._orders[str(order_id)] = OrderStatus.CANCELLED.value
        return OrderResult(
            success=True,
            order_id=str(order_id),
            status=OrderStatus.CANCELLED,
            message="撤单成功",
        )

    def get_positions(self) -> list[Position]:
        """查询绑定账户持仓。"""
        account_id = self._require_account_id()
        return self.portfolio.get_positions(account_id)

    def get_account(self) -> Account:
        """查询绑定账户。"""
        account_id = self._require_account_id()
        return self.portfolio.get_account(account_id)

    # -- 可选能力 ---------------------------------------------------------

    def get_order(self, order_id: str):  # type: ignore[override]
        """模拟盘不支持单笔查询（订单事实来源是本地 ``orders`` 表）。"""
        return None

    def is_available(self) -> bool:
        """模拟盘永远可用（不依赖网络/凭证）。"""
        return True

    def describe(self) -> dict[str, Any]:
        """自描述信息（设置页/环境自检展示）。"""
        info = super().describe()
        info.update(
            {
                "account_id": self.account_id,
                "slippage_bps": self.slippage_bps,
                "priced_symbols": sorted(self.prices),
            }
        )
        return info

    # -- 内部 -------------------------------------------------------------

    def _require_account_id(self) -> str:
        """确定绑定账户：显式指定优先，否则取仓储里的唯一账户。"""
        if self.account_id:
            return self.account_id
        accounts = self.portfolio.repository.list_accounts()
        if len(accounts) == 1:
            self.account_id = accounts[0].id
            return self.account_id
        raise BrokerError(
            "模拟盘尚未绑定账户：请先创建账户并构造 PaperBroker(account_id=...)", code="BR001"
        )


# =====================================================================
# 组合根：一条语句得到可用的模拟盘 OrderService（UI/演示/自测均可复用）
# =====================================================================


def build_paper_service(
    *,
    account_id: str = "ACC-PAPER",
    initial_cash: float = 100000.0,
    account_name: str = "模拟账户",
    risk_config: Any = None,
    fee_config: Any = None,
    prices: dict[str, float] | None = None,
    price_provider: Callable[[str], float | None] | None = None,
    repository: TradingRepository | None = None,
    market_provider: Callable[[], Any] | None = None,
    event_bus: Any = None,
    audit_writer: Callable[..., bool] | None = None,
    clock: Callable[[], datetime] | None = None,
    slippage_bps: float | None = None,
) -> OrderService:
    """装配“内存仓储 + 模拟盘 PaperBroker + 风控 + OrderService”的完整交易闭环。

    :param market_provider: 必填，返回 ``MarketSnapshot`` 的可调用对象
        （由 UI 行情页/自动化 job 组装；风控依赖其中的最新价/交易时段/停牌等）。
    :return: 已初始化账户的 :class:`OrderService`（唯一下单入口）。
    """
    if market_provider is None:
        raise ValueError(
            "build_paper_service 需要 market_provider：请传入返回 MarketSnapshot 的可调用对象"
            "（行情页/自动化 job 负责组装最新价与交易状态）"
        )
    repository = repository or InMemoryTradingRepository()
    portfolio = PortfolioService(repository, clock=clock)
    portfolio.ensure_account(account_id, initial_cash=initial_cash, name=account_name)

    if slippage_bps is None:
        slippage_bps = float(getattr(fee_config, "slippage_bps", 5.0) or 5.0)
    broker = PaperBroker(
        portfolio,
        account_id=account_id,
        prices=prices,
        price_provider=price_provider,
        slippage_bps=float(slippage_bps),
        fee_config=fee_config,
        clock=clock,
    )
    return build_order_service(
        account_id=account_id,
        broker=broker,
        risk_gate=RiskGate(risk_config),
        repository=repository,
        portfolio=portfolio,
        market_provider=market_provider,
        fee_config=fee_config,
        event_bus=event_bus,
        audit_writer=audit_writer,
        clock=clock,
    )


__all__ = ["PaperBroker", "build_paper_service"]

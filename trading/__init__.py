"""QuantDesk 交易执行层（trading）。

本包对外提供两类冻结契约（变更须 Lead 审批）：

- ``trading.broker``（CF-4）
    - :class:`~trading.broker.BrokerAdapter`：券商适配器抽象（submit_order / cancel_order /
      get_positions / get_account，可选 get_order / is_available）。
    - :data:`~trading.broker.broker_registry`：适配器注册表，按 ``trading.broker`` 配置选择实现。

- ``trading.risk``（CF-5）
    - :class:`~trading.risk.RiskGate`：纯函数风控闸门，所有订单（手动 + 自动）的唯一风控入口；
      10 步固定判定顺序、RK001–RK020 原因码、fail-closed。
    - :data:`~trading.risk.RISK_REASON_CODES` / :func:`~trading.risk.describe_reason`：
      原因码 → 中文含义 + 建议动作，供 UI 直接展示。

后续模块（M2-05 起）：``order_manager.py``（OrderService，唯一下单入口）、
``portfolio.py``（账户/持仓/T+1）、``broker.py`` 中的 PaperBroker/CcxtBroker/QmtBroker 实现。
"""

from trading.broker import (
    BROKER_ERROR_TEXT,
    BrokerAdapter,
    BrokerError,
    BrokerErrorCode,
    BrokerNotConfiguredError,
    BrokerNotImplementedError,
    BrokerRejectedError,
    BrokerRegistrationError,
    BrokerRegistry,
    BrokerSymbolUnsupportedError,
    BrokerTimeoutError,
    BrokerUnavailableError,
    broker_registry,
    create_broker,
    get_broker_class,
    list_brokers,
    register_broker,
)
from trading.order_manager import (
    assemble_market_snapshot,
    ORDER_TRANSITIONS,
    OrderService,
    build_order_service,
    cancel_order,
    get_order_service,
    init_order_service,
    place_order,
    preview_order,
    reset_order_service,
)
from trading.paper import PaperBroker, build_paper_service
from trading.portfolio import (
    InMemoryTradingRepository,
    PortfolioService,
    TradingRepository,
    make_fill_id,
)
from trading.repository_sql import SqlTradingRepository
from trading.risk import (
    ALL_RISK_CODES,
    CHECK_STEP_NAMES,
    DEFAULT_RISK_PARAMS,
    RISK_REASON_CODES,
    RiskCode,
    RiskGate,
    RiskReason,
    describe_reason,
)

__all__ = [
    # broker（CF-4）
    "BrokerAdapter",
    "BrokerRegistry",
    "broker_registry",
    "register_broker",
    "get_broker_class",
    "create_broker",
    "list_brokers",
    "BrokerErrorCode",
    "BROKER_ERROR_TEXT",
    "BrokerError",
    "BrokerUnavailableError",
    "BrokerRejectedError",
    "BrokerSymbolUnsupportedError",
    "BrokerNotImplementedError",
    "BrokerTimeoutError",
    "BrokerNotConfiguredError",
    "BrokerRegistrationError",
    # risk（CF-5）
    "RiskGate",
    "RiskCode",
    "RiskReason",
    "RISK_REASON_CODES",
    "ALL_RISK_CODES",
    "CHECK_STEP_NAMES",
    "DEFAULT_RISK_PARAMS",
    "describe_reason",
    # 账户/持仓（M2-03）
    "PortfolioService",
    "TradingRepository",
    "InMemoryTradingRepository",
    "SqlTradingRepository",
    "make_fill_id",
    # 订单服务（M2-05/M2-06，CF-6 唯一入口）
    "OrderService",
    "ORDER_TRANSITIONS",
    "assemble_market_snapshot",
    "build_order_service",
    "init_order_service",
    "get_order_service",
    "reset_order_service",
    "place_order",
    "preview_order",
    "cancel_order",
    # 模拟盘（M2-02）
    "PaperBroker",
    "build_paper_service",
]

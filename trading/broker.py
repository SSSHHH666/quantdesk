"""QuantDesk 券商适配层：BrokerAdapter 抽象 + 适配器注册表。

契约：CF-4（冻结，变更须 Lead 审批）
    ``BrokerAdapter`` 必须实现 4 个方法：
        submit_order(order: OrderRequest) -> OrderResult
        cancel_order(order_id: str) -> OrderResult
        get_positions() -> list[Position]
        get_account() -> Account
    可选能力（非 MVP 必需，基类已给出默认实现）：
        get_order(order_id: str) -> Order | None   # 默认返回 None（表示该适配器不提供单笔查询）
        is_available() -> bool                    # 默认 True（已注册即视为可用；真实适配器应覆盖）

安全纪律（BRIEF F6 / SPEC §8.6）：
    UI、策略、自动化任务**禁止**直接调用 ``submit_order``；唯一合法调用点是
    ``OrderService.place()``，且它必须在提交前调用 ``RiskGate.check()``。
    本模块只提供“执行通道”，不承担风控职责，也不允许被任何模块用来绕过风控。

注册表：
    ``broker_registry`` 是模块级单例；配置项 ``trading.broker`` 选择运行时适配器。
    切换券商只改配置，不改订单/风控/UI 代码（SPEC AC-F9-04）。
    未注册、未配置或配置了未知名称时，抛出中文可读的 ``BrokerNotConfiguredError``，
    UI 可直接把 ``str(exc)`` 展示给用户（含原因码与建议动作）。

借鉴说明（docs/BORROW_MAP.md A2/A9，均已按“只重写思路、不复制代码”处理）：
    - vnpy ``BaseGateway``：网关最小契约 + 方法名稳定的思想（MIT）；
    - lumibot ``Broker``：13 个抽象方法里取最小可用集的思想（GPL-3.0，仅思路）。
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:  # 仅类型检查时导入：本模块在 core.models 交付前也能独立加载
    from core.models import Account, Order, OrderRequest, OrderResult, Position


# ---------------------------------------------------------------------------
# 券商/适配层错误码（SPEC §9.2 冻结：BR001–BR005）
# ---------------------------------------------------------------------------


class BrokerErrorCode(str, Enum):
    """券商/适配层错误码（值即对外展示的 BR 码，冻结）。"""

    BROKER_UNAVAILABLE = "BR001"  # 券商不可用 / 网络异常
    ORDER_REJECTED = "BR002"      # 券商拒单
    SYMBOL_UNSUPPORTED = "BR003"  # 标的不支持
    NOT_IMPLEMENTED = "BR004"     # 适配器未实现
    TIMEOUT = "BR005"             # 请求超时


#: BR 码 → (中文含义, 建议动作)
BROKER_ERROR_TEXT: dict[str, tuple[str, str]] = {
    BrokerErrorCode.BROKER_UNAVAILABLE.value: (
        "券商不可用（网络异常或适配器未就绪）",
        "检查网络与券商客户端是否在线，或改用模拟盘 paper",
    ),
    BrokerErrorCode.ORDER_REJECTED.value: (
        "券商拒单",
        "查看券商返回的具体原因后调整委托价格/数量",
    ),
    BrokerErrorCode.SYMBOL_UNSUPPORTED.value: (
        "券商不支持该标的",
        "更换标的，或使用支持该市场的适配器",
    ),
    BrokerErrorCode.NOT_IMPLEMENTED.value: (
        "适配器功能未实现",
        "MVP 阶段请使用模拟盘 paper；实盘适配将在后续版本接入",
    ),
    BrokerErrorCode.TIMEOUT.value: (
        "券商请求超时",
        "稍后重试；若持续超时请检查网络或券商状态",
    ),
}


class BrokerError(RuntimeError):
    """适配层统一异常。

    ``str(exc)`` 为完整中文提示：``含义〔BRxxx〕：细节 → 建议：...``，
    便于 UI 直接展示、日志检索与测试断言。
    """

    default_code: ClassVar[BrokerErrorCode] = BrokerErrorCode.BROKER_UNAVAILABLE

    def __init__(
        self,
        message: str,
        code: BrokerErrorCode | str | None = None,
        *,
        detail: Any = None,
    ) -> None:
        raw_code = code if code is not None else self.default_code
        self.code: str = raw_code.value if isinstance(raw_code, BrokerErrorCode) else str(raw_code)
        meaning, suggestion = BROKER_ERROR_TEXT.get(
            self.code, ("券商适配层异常", "检查配置与网络后重试")
        )
        self.meaning = meaning
        self.suggestion = suggestion
        self.detail = detail
        self.message = message
        super().__init__(f"{meaning}〔{self.code}〕：{message} → 建议：{suggestion}")

    def to_dict(self) -> dict[str, Any]:
        """转为可落库/可展示的结构（错误码 + 中文 + 建议）。"""
        return {
            "code": self.code,
            "message": self.message,
            "meaning": self.meaning,
            "suggestion": self.suggestion,
            "detail": self.detail,
        }


class BrokerUnavailableError(BrokerError):
    """BR001：券商不可用 / 网络异常。"""

    default_code = BrokerErrorCode.BROKER_UNAVAILABLE


class BrokerRejectedError(BrokerError):
    """BR002：券商拒单。"""

    default_code = BrokerErrorCode.ORDER_REJECTED


class BrokerSymbolUnsupportedError(BrokerError):
    """BR003：标的不支持。"""

    default_code = BrokerErrorCode.SYMBOL_UNSUPPORTED


class BrokerNotImplementedError(BrokerError):
    """BR004：适配器未实现（如 MVP 的 QmtBroker 骨架）。"""

    default_code = BrokerErrorCode.NOT_IMPLEMENTED


class BrokerTimeoutError(BrokerError):
    """BR005：请求超时。"""

    default_code = BrokerErrorCode.TIMEOUT


class BrokerNotConfiguredError(BrokerError):
    """未注册 / 未配置 / 配置了未知的 broker。

    归类为 BR001（券商不可用），提示文案里会列出当前可选适配器并给出配置位置。
    """

    default_code = BrokerErrorCode.BROKER_UNAVAILABLE


class BrokerRegistrationError(BrokerError):
    """适配器注册失败（编码错误，属于开发期问题）。"""

    default_code = BrokerErrorCode.NOT_IMPLEMENTED


# ---------------------------------------------------------------------------
# BrokerAdapter 抽象（CF-4：只允许这 4 个抽象方法 + 2 个可选能力）
# ---------------------------------------------------------------------------


class BrokerAdapter(ABC):
    """券商/交易所适配层抽象基类（CF-4 冻结）。

    子类实现约定：
    1. 类属性 ``name`` 为注册表唯一键（小写，如 ``paper`` / ``ccxt`` / ``qmt``）；
       ``display_name`` 为 UI 展示用的中文名。
    2. ``submit_order`` 只负责“把已通过风控的委托送到执行端并回报结果”，
       **不得**在本方法内再次放行未经风控的订单。
    3. 任何执行端异常都应转换为 :class:`BrokerError` 子类（BR001–BR005），
       便于 OrderService 统一把订单置为 ``REJECTED`` 并发 ``order.rejected`` 事件。
    4. 适配器不得直接写订单/持仓业务表——落库与对账由 OrderService / PortfolioService 负责。
    """

    #: 注册表唯一键（小写）。子类必须覆盖。
    name: str = ""
    #: UI 展示名（中文）。子类可覆盖。
    display_name: str = ""

    # -- 4 个必须实现的方法（CF-4，签名不得改动） -------------------------

    @abstractmethod
    def submit_order(self, order: "OrderRequest") -> "OrderResult":
        """提交委托并返回执行结果。

        参数:
            order: 已通过风控校验的委托请求（``OrderRequest``）。

        返回:
            ``OrderResult``：至少包含订单状态；失败时带 BR/RK 原因码与中文说明。
        """

    @abstractmethod
    def cancel_order(self, order_id: str) -> "OrderResult":
        """撤销委托（BRIEF 约定为传入 order_id）。

        参数:
            order_id: 订单唯一标识。

        返回:
            ``OrderResult``：撤单结果；不可撤时返回明确的中文错误（如 BR002）。
        """

    @abstractmethod
    def get_positions(self) -> "list[Position]":
        """查询当前持仓列表（含数量、可用数量、成本价等）。"""

    @abstractmethod
    def get_account(self) -> "Account":
        """查询账户信息（现金、总资产、冻结资金等）。"""

    # -- 2 个可选能力（默认实现；不强制子类覆盖） -------------------------

    def get_order(self, order_id: str) -> "Order | None":
        """可选：按订单号查询单笔订单。

        默认返回 ``None``，表示“该适配器不提供单笔查询能力”，
        调用方必须容忍 ``None``（例如改用本地 ``orders`` 表作为事实来源）。
        """
        return None

    def is_available(self) -> bool:
        """可选：适配器当前是否可用（依赖已安装、凭证已配置、网络正常）。

        默认 ``True``：只要被注册且被配置选中，就视为可用。
        真实适配器（Ccxt/Qmt）应覆盖本方法：缺依赖/缺凭证时返回 ``False``，
        由 UI 显示“未配置”，并用 PaperBroker 跑通全流程（SPEC §8.9）。
        """
        return True

    # -- 辅助（非契约，不影响子类实现） -----------------------------------

    def describe(self) -> dict[str, Any]:
        """返回适配器自描述信息，供设置页/环境自检展示。"""
        return {
            "name": self.name,
            "display_name": self.display_name or self.name,
            "class": type(self).__name__,
            "available": bool(self.is_available()),
        }

    def __repr__(self) -> str:  # pragma: no cover - 仅调试展示
        return f"<{type(self).__name__} name={self.name!r}>"


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


class BrokerRegistry:
    """适配器注册表：name → 适配器类。

    设计要点（借鉴 vnpy MainEngine.add_gateway 的装配模式，自行重写）：
    - 注册与获取都做中文错误提示，不抛裸 ``KeyError``；
    - 重复注册同一类幂等；覆盖为不同类必须显式 ``override=True``，防止并行开发误覆盖；
    - 只维护内存字典，不做任何网络/DB 操作（便于单测与离线运行）。
    """

    def __init__(self) -> None:
        self._adapters: dict[str, type[BrokerAdapter]] = {}
        self._display_names: dict[str, str] = {}

    # -- 注册 -------------------------------------------------------------

    def register(
        self,
        name: str,
        adapter_cls: type[BrokerAdapter],
        *,
        display_name: str | None = None,
        override: bool = False,
    ) -> type[BrokerAdapter]:
        """注册适配器类。

        参数:
            name: 唯一键（大小写不敏感，内部统一小写）。
            adapter_cls: 必须是 :class:`BrokerAdapter` 的子类。
            display_name: UI 展示名；默认取类的 ``display_name`` 属性。
            override: 已存在且为不同类时是否允许覆盖（默认不允许）。
        """
        if not isinstance(name, str) or not name.strip():
            raise BrokerRegistrationError("注册适配器失败：name 不能为空")
        key = name.strip().lower()
        if not isinstance(adapter_cls, type) or not issubclass(adapter_cls, BrokerAdapter):
            raise BrokerRegistrationError(
                f"注册适配器「{key}」失败：{adapter_cls!r} 必须继承 BrokerAdapter"
            )
        if inspect.isabstract(adapter_cls):
            raise BrokerRegistrationError(
                f"注册适配器「{key}」失败：{adapter_cls.__name__} 仍是抽象类，"
                "请先实现全部 4 个抽象方法（submit_order/cancel_order/get_positions/get_account）"
            )
        existing = self._adapters.get(key)
        if existing is not None and existing is not adapter_cls and not override:
            raise BrokerRegistrationError(
                f"适配器「{key}」已被注册为 {existing.__name__}，拒绝用 {adapter_cls.__name__} 覆盖"
                "（如确需覆盖请显式传 override=True）"
            )
        self._adapters[key] = adapter_cls
        self._display_names[key] = (
            display_name
            or getattr(adapter_cls, "display_name", "")
            or key
        )
        return adapter_cls

    # -- 查询 -------------------------------------------------------------

    def get(self, name: str | None) -> type[BrokerAdapter]:
        """取适配器类；未配置/未注册时抛出中文可读的 ``BrokerNotConfiguredError``。"""
        key = (name or "").strip().lower()
        if not key:
            raise BrokerNotConfiguredError(self._not_configured_message(""))
        adapter_cls = self._adapters.get(key)
        if adapter_cls is None:
            raise BrokerNotConfiguredError(self._not_configured_message(key))
        return adapter_cls

    def create(self, name: str | None, **kwargs: Any) -> BrokerAdapter:
        """创建适配器实例（参数透传给适配器构造函数）。"""
        adapter_cls = self.get(name)
        try:
            return adapter_cls(**kwargs)
        except BrokerError:
            raise
        except TypeError as exc:
            raise BrokerUnavailableError(
                f"创建适配器「{adapter_cls.__name__}」失败：构造参数不匹配（{exc}）",
                detail={"kwargs": sorted(kwargs)},
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 统一转换为中文 BR001
            raise BrokerUnavailableError(
                f"创建适配器「{adapter_cls.__name__}」失败：{type(exc).__name__}: {exc}"
            ) from exc

    def has(self, name: str | None) -> bool:
        """是否已注册该适配器名。"""
        return (name or "").strip().lower() in self._adapters

    def names(self) -> list[str]:
        """已注册适配器名列表（排序后返回，保证可复现）。"""
        return sorted(self._adapters)

    def describe(self) -> dict[str, str]:
        """注册表快照：{name: 中文展示名}，供 UI 下拉框使用。"""
        return {name: self._display_names.get(name, name) for name in self.names()}

    def unregister(self, name: str) -> None:
        """注销适配器（主要供测试/热插拔使用；不存在时静默）。"""
        key = (name or "").strip().lower()
        self._adapters.pop(key, None)
        self._display_names.pop(key, None)

    # -- 内部 -------------------------------------------------------------

    def _not_configured_message(self, name: str) -> str:
        known = "、".join(self.names()) if self._adapters else "（尚未注册任何适配器）"
        if not name:
            return (
                "尚未配置券商适配器（config.yaml 的 trading.broker 为空）。"
                f"当前可选：{known}。MVP 推荐配置 paper（模拟盘），无需真实账户即可跑通全流程。"
            )
        return (
            f"未找到券商适配器「{name}」。请检查 config.yaml 的 trading.broker 配置"
            f"（当前可选：{known}）。MVP 默认使用 paper（模拟盘）；"
            "如需接入真实券商，请先安装/启用对应适配器并在设置页选择。"
        )


#: 模块级注册表单例（全仓唯一，供各模块 import 使用）
broker_registry = BrokerRegistry()


def register_broker(
    name: str,
    *,
    display_name: str | None = None,
    override: bool = False,
):
    """适配器注册装饰器。

    用法::

        @register_broker("paper", display_name="模拟盘")
        class PaperBroker(BrokerAdapter):
            ...
    """

    def decorator(adapter_cls: type[BrokerAdapter]) -> type[BrokerAdapter]:
        broker_registry.register(
            name,
            adapter_cls,
            display_name=display_name,
            override=override,
        )
        return adapter_cls

    return decorator


def get_broker_class(name: str | None) -> type[BrokerAdapter]:
    """按配置名取适配器类（等价 ``broker_registry.get``）。"""
    return broker_registry.get(name)


def create_broker(name: str | None, **kwargs: Any) -> BrokerAdapter:
    """按配置名创建适配器实例（等价 ``broker_registry.create``）。"""
    return broker_registry.create(name, **kwargs)


def list_brokers() -> list[str]:
    """返回已注册适配器名列表。"""
    return broker_registry.names()


__all__ = [
    # 契约主体
    "BrokerAdapter",
    "broker_registry",
    "BrokerRegistry",
    "register_broker",
    "get_broker_class",
    "create_broker",
    "list_brokers",
    # 错误码与异常
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
]

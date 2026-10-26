"""QuantDesk 风控闸门（RiskGate）—— 所有订单的唯一风控入口。

契约：CF-5（冻结，变更须 Lead 审批）
    ``RiskGate.check(order, account, positions, market) -> RiskDecision``

设计要点
1. **纯函数**：不读写数据库、不访问网络、不依赖系统时间/随机数；相同输入必得相同输出。
   为保证确定性，行情新鲜度与下单频率使用入参中的显式参照时间
   （``market.as_of`` / ``market.now``，其次 ``order.created_at``），
   都没有时退化为 ``market.trade_date`` 当日 15:00（收盘）作为参照，或把该项标记为 skipped。
2. **fail-closed**：任何内部异常都会被捕获并返回 ``RK018`` 拒绝，绝不“放行”。
3. **判定顺序固定**（SPEC §8.6，返回第一个失败原因码）：
      1 参数合法性        → RK018 / RK017 / RK011
      2 交易时段/交易日    → RK005
      3 标的可交易性      → RK001 / RK002 / RK003 / RK004
      4 行情新鲜度        → RK006
      5 重复订单          → RK019
      6 价格偏离          → RK010
      7 单笔金额/下单频率  → RK012（仅买入）/ RK013
      8 账户状态          → RK009 / RK020
      9 资金/持仓         → RK014 / RK015 / RK016
     10 仓位额度          → RK007 / RK008
4. **原因码齐全**：RK001–RK020（SPEC §9.2 冻结）。码表**唯一来源是 core/models.py**
   （``RiskReasonCode`` + ``RISK_REASON_MESSAGES``，CF-7）；本文件只保留判定逻辑，
   不重复维护中文文案，避免两处口径漂移。
5. **参数来源**：``config.yaml`` 的 ``risk`` 段是风控阈值的**唯一运行时来源**
   （经 ``core.config.get_config()`` 注入；``core.models.RiskConfig`` 只作 DTO）。
   ``RiskGate(config)`` 可传入 DTO / dict / 整份 config.yaml；传 ``None`` 时自动从配置层读取，
   配置层不可用才退回 DTO 兜底。运行期不再读 DB/网络。
6. **自动交易 fail-closed**：AUTO 来源订单默认拒绝（``allow_auto_orders`` 默认 False）；
   只有配置显式开启（如 ``automation.enabled=true``）才放行，删除“缺数据即跳过”的 fail-open 路径。

借鉴说明（docs/BORROW_MAP.md A10 / P3）：
    lumibot 把“确定性下单校验”挂在唯一 submit 路径内、失败即 return False（GPL-3.0）。
    本文件只重写其思想——把校验抽成纯函数并返回带原因码的 ``RiskDecision``，
    未复制任何代码、注释、变量名或文件结构。

调用方（OrderService.place）约定：
    - 本模块不落库、不发事件；``risk.blocked`` 事件与审计由 OrderService 负责。
    - ``RiskDecision.checks`` 是逐条规则的通过/跳过记录（实际值 + 阈值），供 UI 展示“为什么被拒”。

MarketSnapshot 的扩展信息（可选；缺失时对应检查标记 skipped，不会误判拒绝）：
    universe / tradeable_symbols        可交易池 set[str]                       → RK001
    blacklist                           系统黑名单补充 set[str]                 → RK002
    delisting                           退市整理 set[str]                       → RK002
    suspended                           停牌 set[str]                           → RK003
    listed_days                         {symbol: 已上市交易日数}                 → RK004
    price_times                         {symbol: 快照时间}  （SPEC 字段）        → RK006
    as_of / now / current_time          参照时间 datetime                        → RK006/RK013
    existing_client_order_ids           已存在委托幂等键 set[str]                → RK019
    recent_order_times                  最近下单时间 list[datetime]               → RK013
    price_limits / limit_up_prices / limit_down_prices
                                        {symbol: 涨跌停价}                       → RK011
    is_fused                            熔断状态覆盖 bool                        → RK009
    automation_enabled                  自动化总开关 bool                        → RK020
    daily_open_asset                    当日开盘总资产（SPEC 字段）              → RK009
    （``order`` 上也可携带 ``limit_up`` / ``limit_down`` / ``created_at``。）

    说明：上述扩展项是对 SPEC §6.7 ``MarketSnapshot`` 的**向后兼容补充**，不改变 check 签名。
    ``core/models.py`` 的 ``MarketSnapshot`` 继承 ``QDModel(extra="allow")``，
    因此 OrderService/QA 可直接以关键字参数传入这些扩展字段（如
    ``MarketSnapshot(..., universe=[...], price_limits={...})``），无需修改冻结契约；
    不传时对应检查标记 skipped，不会误判拒绝。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Iterable, Mapping

try:  # 契约类型与 RK 码表统一来自 core.models（CF-7 唯一来源，避免两处维护）
    from core.models import (
        RISK_REASON_MESSAGES,
        RiskDecision,
        RiskReasonCode,
    )
except ImportError as _exc:  # pragma: no cover - 仅在 core/models.py 未交付时触发
    raise ImportError(
        "风控闸门无法导入 core.models（RiskDecision/RiskReasonCode/RISK_REASON_MESSAGES）："
        f"请确认 Dev1 的 M0-03（core/models.py）已交付。原始错误：{_exc}"
    ) from _exc

if TYPE_CHECKING:  # 仅类型检查，避免运行期强耦合
    from core.models import Account, MarketSnapshot, OrderRequest, Position, RiskConfig

try:  # 可选复用 CF-10 的唯一实现（backtest/broker_sim.py::AStockRules）；未交付时用内置兜底
    from backtest.broker_sim import AStockRules as _AStockRules
except Exception:  # noqa: BLE001 - 导入失败不得影响风控可用性
    _AStockRules = None


# ===========================================================================
# 原因码（SPEC §9.2 冻结）
# ===========================================================================


#: 风控原因码枚举：直接复用 ``core.models.RiskReasonCode``（CF-7 唯一来源，禁止复制一份）
RiskCode = RiskReasonCode


@dataclass(frozen=True)
class RiskReason:
    """一条原因码的中文描述视图（数据来自 ``core.models.RISK_REASON_MESSAGES``）。"""

    code: str
    name: str
    meaning: str
    suggestion: str


#: RK 码 → 中文含义 + 建议动作（由 core.models 唯一来源派生，不在此重复维护文案）
RISK_REASON_CODES: Mapping[str, RiskReason] = MappingProxyType(
    {
        code: RiskReason(
            code=code,
            name=str(info.get("name", "")),
            meaning=str(info.get("meaning", "")),
            suggestion=str(info.get("suggestion", "")),
        )
        for code, info in RISK_REASON_MESSAGES.items()
    }
)

#: 全部原因码（顺序即 core.models.RiskReasonCode 定义顺序），QA 可断言 RK001–RK020 齐全
ALL_RISK_CODES: tuple[str, ...] = tuple(code.value for code in RiskReasonCode)


def describe_reason(reason_code: str) -> str:
    """把原因码转成中文提示：``含义〔RKxxx〕→ 建议：...``。未知码返回原样。"""
    reason = RISK_REASON_CODES.get(str(reason_code))
    if reason is None:
        return f"未知原因码：{reason_code}"
    return f"{reason.meaning}〔{reason.code}〕→ 建议：{reason.suggestion}"


# ===========================================================================
# 默认参数（SPEC §9.1 / §7 配置键；调用方可整体覆盖）
# ===========================================================================

def _build_dto_defaults() -> dict[str, Any]:
    """用 ``core.models`` 的 DTO 组装兜底默认值（DTO 只作类型/默认值载体）。

    Lead 裁决（2026-10-25）：**config.yaml 的 ``risk`` 段是风控阈值的唯一运行时来源**，
    ``core.models.RiskConfig`` 只作 DTO；``trading/risk.py`` 不得另设一套业务阈值。
    本函数仅在核心配置层不可用时兜底（单测/早期启动），运行期由 :func:`_load_runtime_config`
    从 ``core.config.get_config()``（config.yaml + 环境变量）注入。
    """
    from core.models import FeeConfig, RiskConfig  # 延迟导入：模块导入期避免依赖配置层

    params: dict[str, Any] = dict(RiskConfig().model_dump())
    params.update(FeeConfig().model_dump())
    params.update(
        {
            # 行情新鲜度阈值不在 RiskConfig DTO 中（属于 data.stale_threshold_hours），
            # 运行期由 config.yaml 注入；此处仅保留与 config.yaml 默认一致的安全兜底。
            "max_price_age_hours": 24.0,
            "min_order_amount_enforce_sell": False,
            # 自动交易显式授权开关：默认 False（fail-closed，AUTO 单默认拒绝）
            "allow_auto_orders": False,
        }
    )
    return params


#: 兜底参数（DTO 默认值；运行期以 config.yaml 为准，见 :func:`_load_runtime_config`）
DEFAULT_RISK_PARAMS: dict[str, Any] = _build_dto_defaults()


def _load_runtime_config() -> Any:
    """默认配置来源：``core.config.get_config()``（config.yaml + ``QD_*`` 环境变量）。

    核心配置层尚未就绪（早期单测）时返回 ``None``，由 DTO 兜底值接管。
    """
    try:
        from core.config import get_config  # 延迟导入，避免与核心配置层形成导入环

        return get_config()
    except Exception:  # noqa: BLE001 - 配置层未就绪/配置非法时不阻塞风控构造（使用 DTO 兜底）
        return None


#: 第 1–10 步的中文名称（checks 明细里使用，UI 可直接展示）
CHECK_STEP_NAMES: tuple[str, ...] = (
    "参数合法性",
    "交易时段",
    "标的可交易性",
    "行情新鲜度",
    "重复订单",
    "价格偏离",
    "单笔金额与下单频率",
    "账户状态",
    "资金与持仓",
    "仓位额度",
)

_SIDE_BUY = "BUY"
_SIDE_SELL = "SELL"
_ORDER_TYPE_LIMIT = "LIMIT"
_ORDER_TYPE_MARKET = "MARKET"
_SOURCE_AUTO = "AUTO"

_MISSING = object()


# ===========================================================================
# 通用取值工具（全部为纯函数，支持 pydantic 模型 / dict / 普通对象）
# ===========================================================================


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """兼容对象属性与字典的只读取值。"""
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _get_first(obj: Any, names: Iterable[str], default: Any = None) -> Any:
    """按顺序取第一个非 None 的字段。"""
    for name in names:
        value = _get(obj, name, _MISSING)
        if value is not _MISSING and value is not None:
            return value
    return default


def _as_symbol(value: Any) -> str:
    """统一标的代码格式：去空白、转大写（内部统一 ``600519.SH`` 风格）。"""
    if value is None:
        return ""
    raw = getattr(value, "value", value)
    return str(raw).strip().upper()


def _as_enum_text(value: Any) -> str:
    """枚举/字符串统一转大写文本（``Side.BUY`` → ``BUY``）。"""
    if value is None:
        return ""
    raw = getattr(value, "value", value)
    return str(raw).strip().upper()


def _to_float(value: Any, default: float | None = None) -> float | None:
    """宽容地转 float；非法值返回 default。"""
    if value is None or isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int | None = None) -> int | None:
    """宽容地转 int；仅接受整数（100.0 可接受，100.5 拒绝）。"""
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number) or not float(number).is_integer():
        return default
    return int(number)


def _to_datetime(value: Any) -> datetime | None:
    """转 datetime；支持 datetime / date / ISO 字符串 / 时间戳。失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time(15, 0))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value))
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        if not text:
            return None
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
                try:
                    return datetime.strptime(text, fmt)
                except ValueError:
                    continue
    return None


def _to_date(value: Any) -> date | None:
    """转 date（用于 trade_date）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        parsed = _to_datetime(value)
        return parsed.date() if parsed else None
    return None


def _lookup(mapping: Any, symbol: str) -> Any:
    """在 {symbol: value} 映射中查值，兼容大小写不一致的 key。"""
    if not isinstance(mapping, Mapping):
        return None
    if symbol in mapping:
        return mapping[symbol]
    for key, value in mapping.items():
        if _as_symbol(key) == symbol:
            return value
    return None


def _symbol_set(values: Any) -> frozenset[str]:
    """把可交易池/黑名单等转成大写代码集合。"""
    if not values:
        return frozenset()
    if isinstance(values, str):
        values = [values]
    return frozenset(_as_symbol(item) for item in values if _as_symbol(item))


def _seconds_between(later: datetime, earlier: datetime) -> float:
    """两个时间的秒差；时区感知/无感知混用时不抛异常（统一去时区比较）。"""
    if (later.tzinfo is None) != (earlier.tzinfo is None):
        later = later.replace(tzinfo=None)
        earlier = earlier.replace(tzinfo=None)
    return (later - earlier).total_seconds()


def _money(value: float) -> str:
    """金额展示：千分位 + 两位小数。"""
    return f"{value:,.2f}"


def _pct(value: float) -> str:
    """百分比展示：两位小数。"""
    return f"{value:.2f}%"


def _jsonable(value: Any) -> Any:
    """把值转成可 JSON 序列化的形式（datetime → ISO 字符串）。"""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        return sorted(_as_symbol(item) for item in value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _config_to_mapping(config: Any) -> dict[str, Any]:
    """把 ``RiskConfig`` / 整个 config.yaml 字典 / 普通对象统一转成扁平 dict。"""
    if config is None:
        return {}

    if isinstance(config, Mapping):
        data = dict(config)
    else:
        dump = getattr(config, "model_dump", None)  # pydantic v2
        if callable(dump):
            data = dict(dump())
        elif hasattr(config, "__dict__"):
            data = {key: val for key, val in vars(config).items() if not key.startswith("_")}
        else:  # 极端情况：无法解析，退回默认值（不抛错，配置问题由 core/config 报）
            return {}

    result: dict[str, Any] = {}
    # 支持传入“整份 config.yaml”：risk / trading / fees / data 四段映射到风控参数
    risk_section = data.get("risk")
    if isinstance(risk_section, Mapping):
        result.update({key: val for key, val in risk_section.items()})
        trading_section = data.get("trading")
        if isinstance(trading_section, Mapping):
            if "min_order_amount" in trading_section:
                result["min_order_amount"] = trading_section["min_order_amount"]
            if "allow_market_order" in trading_section:
                result["allow_market_order"] = trading_section["allow_market_order"]
        fees_section = data.get("fees")
        if isinstance(fees_section, Mapping):
            for key in (
                "commission_rate",
                "min_commission",
                "stamp_tax_rate",
                "transfer_fee_rate",
                "reg_fee_rate",
            ):
                if key in fees_section:
                    result[key] = fees_section[key]
        data_section = data.get("data")
        if isinstance(data_section, Mapping) and "stale_threshold_hours" in data_section:
            result["max_price_age_hours"] = data_section["stale_threshold_hours"]
        automation_section = data.get("automation")
        if isinstance(automation_section, Mapping) and "enabled" in automation_section:
            result["automation_enabled"] = automation_section["enabled"]
            # 用户显式打开 automation.enabled = 显式授权自动交易（risk 段可单独覆盖该开关）
            result.setdefault("allow_auto_orders", automation_section["enabled"])
        # 兼容“扁平 RiskConfig 模型”放在 config.risk 里之外的情况
        if "max_position_per_symbol_pct" in data and "max_position_per_symbol_pct" not in result:
            result.update(
                {
                    key: data[key]
                    for key in DEFAULT_RISK_PARAMS
                    if key in data
                }
            )
    else:
        result.update({key: val for key, val in data.items() if key in DEFAULT_RISK_PARAMS})
    return result


# ===========================================================================
# RiskGate
# ===========================================================================


class RiskGate:
    """风控闸门：纯函数、fail-closed、判定顺序固定的唯一风控入口。

    用法::

        gate = RiskGate(config.risk)            # 传入 RiskConfig 或 dict
        decision = gate.check(order, account, positions, market)
        if not decision.allow:
            # 用 decision.reason_code / decision.message 展示，并禁止提交
            ...
    """

    def __init__(self, config: "RiskConfig | Mapping[str, Any] | None" = None) -> None:
        """构造风控闸门。

        参数:
            config: ``RiskConfig``（DTO，仅当类型载体）、扁平 dict、或整份 config.yaml 字典，均可。
                    传 ``None`` 时自动从 ``core.config.get_config()``（config.yaml 唯一运行时来源）
                    读取；配置层不可用时退回 ``RiskConfig``/``FeeConfig`` DTO 兜底。
        说明:
            - **config.yaml 的 risk 段是阈值唯一来源**，本类不另设一套业务默认值（Lead 裁决）；
            - ``allow_auto_orders`` 默认 False：自动（AUTO）来源订单默认 fail-closed，
              只有调用方显式传入 ``allow_auto_orders=True``（通常映射 ``automation.enabled=true``）
              才允许自动交易；
            - 构造完成后参数即冻结（``MappingProxyType``），运行期不再读取外部配置/DB，
              保证 ``check`` 是同一输入同一输出的纯函数。
        """
        params = dict(DEFAULT_RISK_PARAMS)
        overrides = _config_to_mapping(config if config is not None else _load_runtime_config())
        for key, value in overrides.items():
            if key in params and value is not None:
                params[key] = value

        # 数值字段做宽容转换（配置层应已用 pydantic 校验；这里失败则退回默认，不崩溃）
        for key in (
            "max_position_per_symbol_pct",
            "max_total_position_pct",
            "max_daily_loss_pct",
            "max_price_deviation_pct",
            "max_single_order_value_pct",
            "min_order_amount",
            "max_price_age_hours",
            "commission_rate",
            "min_commission",
            "stamp_tax_rate",
            "transfer_fee_rate",
            "reg_fee_rate",
        ):
            params[key] = _to_float(params[key], _to_float(DEFAULT_RISK_PARAMS[key], 0.0))
        for key in ("max_orders_per_minute", "min_listed_days"):
            params[key] = _to_int(params[key], _to_int(DEFAULT_RISK_PARAMS[key], 0))
        params["enabled"] = bool(params["enabled"])
        params["allow_market_order"] = bool(params["allow_market_order"])
        params["min_order_amount_enforce_sell"] = bool(params["min_order_amount_enforce_sell"])
        # 自动交易显式授权：默认 False（fail-closed）；只有配置显式 true 才放行 AUTO 单
        params["allow_auto_orders"] = bool(params.get("allow_auto_orders", False))
        if params.get("automation_enabled") is not None:
            params["automation_enabled"] = bool(params["automation_enabled"])
        params["blacklist"] = _symbol_set(params.get("blacklist"))

        self._params: Mapping[str, Any] = MappingProxyType(params)

    # -- 只读属性 ---------------------------------------------------------

    @property
    def params(self) -> Mapping[str, Any]:
        """冻结后的参数快照（只读，供 UI 展示与日志记录）。"""
        return self._params

    def __repr__(self) -> str:  # pragma: no cover - 调试展示
        p = self._params
        return (
            f"<RiskGate enabled={p['enabled']} 单票上限={p['max_position_per_symbol_pct']}% "
            f"总仓位上限={p['max_total_position_pct']}% 熔断={p['max_daily_loss_pct']}%>"
        )

    # =====================================================================
    # 公共入口（CF-5 冻结签名）
    # =====================================================================

    def check(
        self,
        order: "OrderRequest",
        account: "Account",
        positions: "list[Position]",
        market: "MarketSnapshot",
    ) -> RiskDecision:
        """对一笔订单做完整风控判定。

        参数:
            order: 待提交的委托（``OrderRequest``；支持 dict/鸭子类型对象）。
            account: 账户快照（现金、总资产、是否启用等）。
            positions: 当前持仓列表（卖出校验 T+1 可用数量、仓位占比计算）。
            market: 行情/市场快照（交易时段、最新价、停牌、黑名单等）。

        返回:
            ``RiskDecision``：``allow`` 为 True 表示放行；False 时 ``reason_code`` 为 RK 码、
            ``message`` 为中文可读原因 + 建议，``checks`` 为逐条规则明细。

        异常:
            本方法**不会向外抛异常**：任何内部错误都会按 fail-closed 原则返回 ``RK018`` 拒绝。
        """
        checks: list[dict[str, Any]] = []
        try:
            return self._run_checks(order, account, positions, market, checks)
        except Exception as exc:  # noqa: BLE001 - fail-closed：宁可拒单，绝不放行
            code = RiskCode.RK018.value
            reason = RISK_REASON_CODES[code]
            detail = f"风控内部异常，已按 fail-closed 原则拒绝本单（{type(exc).__name__}: {exc}）"
            checks.append(
                {
                    "step": 0,
                    "step_name": "内部异常",
                    "rule": "internal_error",
                    "passed": False,
                    "skipped": False,
                    "reason_code": code,
                    "message": detail,
                    "actual": _jsonable(repr(exc)),
                    "limit": None,
                }
            )
            return RiskDecision(
                allow=False,
                reason_code=code,
                message=f"{reason.meaning}：{detail}〔{code}〕→ 建议：{reason.suggestion}",
                details={
                    "step": 0,
                    "rule": "internal_error",
                    "reason_code": code,
                    "error": repr(exc),
                    "suggestion": reason.suggestion,
                },
                checks=list(checks),
            )

    # =====================================================================
    # 内部：10 步判定
    # =====================================================================

    def _run_checks(
        self,
        order: Any,
        account: Any,
        positions: Any,
        market: Any,
        checks: list[dict[str, Any]],
    ) -> RiskDecision:
        p = self._params

        # ----- 前置：入参存在性（缺失属调用方 bug，fail-closed） -----
        if order is None:
            raise ValueError("委托对象为空（order=None）")
        if account is None:
            raise ValueError("账户信息为空（account=None）")
        if positions is None:
            raise ValueError("持仓列表为空（positions=None）")
        if market is None:
            raise ValueError("行情快照为空（market=None）")

        # 委托来源枚举：只允许 MANUAL/AUTO；未知/缺失来源按不可信处理（fail-closed RK018）
        source = _as_enum_text(_get(order, "source", "MANUAL")) or "MANUAL"
        if source not in ("MANUAL", _SOURCE_AUTO):
            return self._deny(
                checks, 1, "source_valid", RiskCode.RK018,
                f"委托来源非法：{_get(order, 'source')!r}，只支持 MANUAL（手动）/AUTO（自动）",
                actual=_get(order, "source"),
            )

        # 风控总开关关闭：按配置放行（SPEC §9.1 “不建议关闭，关闭时审计告警”由 OrderService 负责）。
        # 但 AUTO（自动化）订单仍要求显式授权，避免“一关风控就自动放行自动交易”（fail-closed）。
        if not p["enabled"]:
            if source == _SOURCE_AUTO and not bool(p.get("allow_auto_orders", False)):
                return self._deny(
                    checks, 8, "automation_enabled", RiskCode.RK020,
                    "风控总开关已关闭且未显式授权自动交易（risk.allow_auto_orders=false），"
                    "默认 fail-closed 拒绝自动单",
                    actual=False,
                )
            self._pass(
                checks,
                0,
                "risk_enabled",
                "风控总开关已关闭（risk.enabled=false），本次未做风控校验",
                skipped=True,
            )
            return RiskDecision(
                allow=True,
                reason_code=None,
                message="风控总开关已关闭（risk.enabled=false），本次订单未做风控校验，建议尽快开启",
                details={"risk_enabled": False, "checks_total": len(checks)},
                checks=list(checks),
            )

        # 常用字段解析（宽容解析，非法值交给第 1 步报 RK018/RK017）
        symbol = _as_symbol(_get(order, "symbol"))
        side = _as_enum_text(_get(order, "side"))
        order_type = _as_enum_text(_get(order, "order_type", _ORDER_TYPE_LIMIT)) or _ORDER_TYPE_LIMIT
        quantity = _to_int(_get(order, "quantity"))
        price = _to_float(_get(order, "price"))

        latest_prices = _get(market, "latest_prices", {}) or {}
        latest_price = _to_float(_lookup(latest_prices, symbol))

        position_map = self._position_map(positions)
        position = position_map.get(symbol)
        board = self._board_of(symbol)

        # =================================================================
        # 第 1 步：参数合法性（价格>0、数量>0、方向合法、整手、涨跌停区间）
        # =================================================================
        if not symbol:
            return self._deny(checks, 1, "symbol_required", RiskCode.RK018,
                              "委托标的代码为空，无法识别交易标的")
        if side not in (_SIDE_BUY, _SIDE_SELL):
            return self._deny(checks, 1, "side_valid", RiskCode.RK018,
                              f"委托方向非法：「{_get(order, 'side')!r}」，只支持 BUY（买入）/ SELL（卖出）",
                              actual=_get(order, "side"))
        if quantity is None or quantity <= 0:
            return self._deny(checks, 1, "quantity_positive", RiskCode.RK018,
                              f"委托数量必须为正整数（当前：{_get(order, 'quantity')!r}）",
                              actual=_get(order, "quantity"))
        if order_type not in (_ORDER_TYPE_LIMIT, _ORDER_TYPE_MARKET):
            return self._deny(checks, 1, "order_type_valid", RiskCode.RK018,
                              f"委托类型非法：「{order_type}」，只支持 LIMIT（限价）/ MARKET（市价）",
                              actual=order_type)
        if order_type == _ORDER_TYPE_MARKET and not p["allow_market_order"]:
            return self._deny(
                checks, 1, "market_order_allowed", RiskCode.RK018,
                "MVP 默认禁止市价单（A 股实盘无真正市价单）；如确需使用，请在 config.yaml 将 "
                "trading.allow_market_order 设为 true",
                actual="MARKET",
            )
        if order_type == _ORDER_TYPE_LIMIT and (price is None or price <= 0):
            return self._deny(checks, 1, "price_positive", RiskCode.RK018,
                              f"限价单价格必须大于 0（当前：{_get(order, 'price')!r}）",
                              actual=_get(order, "price"))
        self._pass(checks, 1, "price_quantity_side_valid", "价格/数量/方向/类型校验通过")

        # 整手校验（主板/创业板 100 股整数倍；科创板买入 ≥200 股；卖出零股需一次性清仓）
        available_qty = 0
        if position is not None:
            available_qty = _to_int(_get(position, "available_quantity"), 0) or 0
        lot_ok, lot_reason = self._validate_lot(side, quantity, available_qty, board)
        if not lot_ok:
            return self._deny(
                checks, 1, "lot_size", RiskCode.RK017,
                lot_reason or f"{board} 板块数量不符合整手规则（数量={quantity}）",
                actual=quantity,
                limit=("科创板 ≥200 股" if board == "STAR" else "100 股整数倍"),
            )
        self._pass(checks, 1, "lot_size", "整手规则校验通过", actual=quantity)

        # 涨跌停区间（仅在能拿到涨跌停价时校验；拿不到则跳过，避免误拒）
        limit_up, limit_down = self._get_price_limits(market, order, symbol)
        check_price = price if order_type == _ORDER_TYPE_LIMIT else latest_price
        if limit_up is not None and limit_down is not None and check_price is not None:
            if side == _SIDE_BUY and check_price > limit_up + 1e-9:
                return self._deny(
                    checks, 1, "price_limit_range", RiskCode.RK011,
                    f"买入限价 {check_price:.2f} 元高于涨停价 {limit_up:.2f} 元",
                    actual=check_price, limit=limit_up,
                )
            if side == _SIDE_SELL and check_price < limit_down - 1e-9:
                return self._deny(
                    checks, 1, "price_limit_range", RiskCode.RK011,
                    f"卖出限价 {check_price:.2f} 元低于跌停价 {limit_down:.2f} 元",
                    actual=check_price, limit=limit_down,
                )
            self._pass(checks, 1, "price_limit_range",
                       f"价格在涨跌停区间内（{limit_down:.2f} ~ {limit_up:.2f}）",
                       actual=check_price, limit=(limit_down, limit_up))
        else:
            self._pass(checks, 1, "price_limit_range",
                       "缺少涨跌停价数据，跳过区间校验（建议调用方在行情快照中提供）",
                       skipped=True)

        # =================================================================
        # 第 2 步：交易时段 / 交易日
        # =================================================================
        market_open = _get_first(market, ("is_trading_time", "is_market_open"))
        is_trading_day = _get(market, "is_trading_day")
        if market_open is False or is_trading_day is False:
            return self._deny(
                checks, 2, "market_open", RiskCode.RK005,
                "当前处于休市状态，禁止委托（A 股交易时间 09:30–11:30、13:00–15:00）",
                actual={"is_trading_time": market_open, "is_trading_day": is_trading_day},
                limit=True,
            )
        if market_open is None and is_trading_day is None:
            return self._deny(
                checks, 2, "market_open", RiskCode.RK005,
                "行情快照缺少交易时段标记（is_trading_time/is_trading_day），无法确认是否可交易，"
                "已按 fail-closed 拒绝",
                actual=None, limit=True,
            )
        else:
            self._pass(checks, 2, "market_open", "当前处于可交易时段", actual=market_open)

        # =================================================================
        # 第 3 步：标的可交易性（在池 / 黑名单 / 停牌 / 上市天数）
        # =================================================================
        universe = _get_first(market, ("universe", "tradeable_symbols", "symbol_universe"))
        if universe is None:
            self._pass(checks, 3, "symbol_in_universe", "未提供可交易池，跳过标的池校验", skipped=True)
        else:
            universe_set = _symbol_set(universe)
            if symbol not in universe_set:
                return self._deny(
                    checks, 3, "symbol_in_universe", RiskCode.RK001,
                    f"{symbol} 不在可交易池（共 {len(universe_set)} 只）",
                    actual=symbol,
                )
            self._pass(checks, 3, "symbol_in_universe", f"标的 {symbol} 在可交易池内")

        blacklist = set(p["blacklist"]) | set(
            _symbol_set(_get_first(market, ("blacklist", "system_blacklist"), ()))
        )
        if symbol in blacklist:
            return self._deny(
                checks, 3, "symbol_blacklist", RiskCode.RK002,
                f"{symbol} 在黑名单（ST/退市整理/用户禁用等）",
                actual=symbol,
            )
        delisting = _symbol_set(_get(market, "delisting", ()))
        if symbol in delisting:
            return self._deny(
                checks, 3, "symbol_blacklist", RiskCode.RK002,
                f"{symbol} 处于退市整理期，禁止交易",
                actual=symbol,
            )
        self._pass(checks, 3, "symbol_blacklist", "标的不在黑名单/退市整理名单")

        suspended = _symbol_set(_get(market, "suspended", ()))
        if symbol in suspended:
            return self._deny(
                checks, 3, "symbol_suspended", RiskCode.RK003,
                f"{symbol} 当前停牌，无法成交（复牌后再操作）",
                actual=symbol,
            )
        self._pass(checks, 3, "symbol_suspended", "标的未停牌")

        listed_days_map = _get_first(market, ("listed_days", "listed_days_map"))
        listed_days = _to_int(_lookup(listed_days_map, symbol))
        if listed_days is None:
            self._pass(checks, 3, "listed_days",
                       f"未提供上市天数，跳过上市时长校验（阈值 {p['min_listed_days']} 个交易日）",
                       skipped=True)
        elif listed_days < int(p["min_listed_days"]):
            return self._deny(
                checks, 3, "listed_days", RiskCode.RK004,
                f"{symbol} 已上市 {listed_days} 个交易日，低于阈值 {p['min_listed_days']} 个交易日",
                actual=listed_days, limit=p["min_listed_days"],
            )
        else:
            self._pass(checks, 3, "listed_days", f"上市 {listed_days} 个交易日，满足要求",
                       actual=listed_days, limit=p["min_listed_days"])

        # =================================================================
        # 第 4 步：行情新鲜度
        # =================================================================
        # 行情新鲜度阈值：优先用 MarketSnapshot.stale_threshold_hours（对应 data.stale_threshold_hours）
        market_threshold = _to_float(_get(market, "stale_threshold_hours"))
        max_price_age_hours = (
            market_threshold
            if market_threshold is not None and market_threshold > 0
            else float(p["max_price_age_hours"])
        )
        price_times = _get(market, "price_times")
        snapshot_time = _to_datetime(_lookup(price_times, symbol)) if isinstance(price_times, Mapping) else None
        reference_time = self._reference_time(market, order)
        if not isinstance(price_times, Mapping) or not price_times:
            return self._deny(
                checks, 4, "price_freshness", RiskCode.RK006,
                "行情快照未提供 price_times，无法校验行情新鲜度，已按 fail-closed 拒绝"
                "（请调用方填充快照时间后再下单）",
                actual=None, limit=f"{max_price_age_hours:.1f} 小时",
            )
        elif snapshot_time is None:
            return self._deny(
                checks, 4, "price_freshness", RiskCode.RK006,
                f"未获取到标的 {symbol} 的行情快照时间，无法确认行情新鲜度，已拒绝下单",
                actual=None, limit=f"{max_price_age_hours:.1f} 小时",
            )
        elif reference_time is None:
            self._pass(checks, 4, "price_freshness",
                       "缺少参照时间，跳过新鲜度校验（建议传入 market.as_of 或 order.created_at）",
                       skipped=True)
        else:
            age_hours = _seconds_between(reference_time, snapshot_time) / 3600.0
            if age_hours > max_price_age_hours:
                return self._deny(
                    checks, 4, "price_freshness", RiskCode.RK006,
                    f"最新快照时间 {snapshot_time.isoformat(sep=' ')}，"
                    f"距参照时间 {age_hours:.1f} 小时，超过阈值 {max_price_age_hours:.1f} 小时",
                    actual=round(age_hours, 2), limit=max_price_age_hours,
                )
            self._pass(checks, 4, "price_freshness",
                       f"行情新鲜度正常（{max(age_hours, 0.0):.2f} 小时内）",
                       actual=round(age_hours, 2), limit=max_price_age_hours)

        # =================================================================
        # 第 5 步：重复订单（幂等键）
        # =================================================================
        client_order_id = _get(order, "client_order_id")
        if not client_order_id:
            self._pass(checks, 5, "duplicate_order",
                       "委托未携带 client_order_id，跳过重复校验（建议 OrderService 生成幂等键）",
                       skipped=True)
        else:
            existing = _get_first(
                market,
                ("existing_client_order_ids", "duplicate_client_order_ids", "open_client_order_ids"),
            )
            if existing is None:
                open_orders = _get(market, "open_orders")
                if open_orders:
                    existing = {
                        _get(item, "client_order_id") or _get(item, "id")
                        for item in open_orders
                    }
            if existing is None:
                self._pass(checks, 5, "duplicate_order", "未提供既有委托幂等键，跳过重复校验",
                           skipped=True)
            elif str(client_order_id) in {str(item) for item in existing if item is not None}:
                return self._deny(
                    checks, 5, "duplicate_order", RiskCode.RK019,
                    f"幂等键 {client_order_id} 已存在，已拒绝重复提交（请查看今日委托）",
                    actual=str(client_order_id),
                )
            else:
                self._pass(checks, 5, "duplicate_order", "非重复订单", actual=str(client_order_id))

        # =================================================================
        # 第 6 步：价格偏离（限价 vs 最新价）+ 最新价可用性
        # =================================================================
        if latest_price is None or latest_price <= 0:
            return self._deny(
                checks, 6, "price_deviation", RiskCode.RK006,
                f"未获取到标的 {symbol} 的最新价，无法校验价格偏离与委托金额，已拒绝下单",
                actual=None,
            )
        if order_type == _ORDER_TYPE_LIMIT:
            deviation_pct = abs(price - latest_price) / latest_price * 100.0
            if deviation_pct > float(p["max_price_deviation_pct"]) + 1e-9:
                return self._deny(
                    checks, 6, "price_deviation", RiskCode.RK010,
                    f"限价 {price:.2f} 元 vs 最新价 {latest_price:.2f} 元，"
                    f"偏离 {deviation_pct:.2f}%，超过上限 {p['max_price_deviation_pct']:.2f}%"
                    "（近似价格笼子/防手误）",
                    actual=round(deviation_pct, 4), limit=p["max_price_deviation_pct"],
                )
            self._pass(checks, 6, "price_deviation",
                       f"价格偏离 {deviation_pct:.2f}%，在允许范围内",
                       actual=round(deviation_pct, 4), limit=p["max_price_deviation_pct"])
        else:
            self._pass(checks, 6, "price_deviation", "市价单不适用价格偏离校验", skipped=True)

        effective_price = price if order_type == _ORDER_TYPE_LIMIT else latest_price
        amount = effective_price * quantity
        total_asset = self._total_asset(account, positions, market, latest_prices)

        # =================================================================
        # 第 7 步：单笔金额上限 / 最小金额 / 下单频率
        # =================================================================
        enforce_min_amount = side == _SIDE_BUY or bool(p["min_order_amount_enforce_sell"])
        if enforce_min_amount and amount < float(p["min_order_amount"]) - 1e-9:
            return self._deny(
                checks, 7, "min_order_amount", RiskCode.RK012,
                f"单笔委托金额 {_money(amount)} 元低于最小下单金额 {_money(float(p['min_order_amount']))} 元"
                "（防止最低佣金侵蚀）",
                actual=round(amount, 2), limit=p["min_order_amount"],
            )
        # 口径（Lead 裁决 2026-10-25）：max_single_order_value_pct **仅约束买入**；
        # 卖出只受整手/持仓/T+1/停牌/涨跌停约束，不受单笔金额上限，避免大额清仓被 RK012 误拦。
        if side != _SIDE_BUY:
            self._pass(checks, 7, "max_single_order_amount",
                       "卖出不受单笔金额上限约束（该上限仅约束买入，允许大额清仓）",
                       skipped=True)
        elif total_asset > 0:
            max_single_value = total_asset * float(p["max_single_order_value_pct"]) / 100.0
            if amount > max_single_value + 1e-6:
                return self._deny(
                    checks, 7, "max_single_order_amount", RiskCode.RK012,
                    f"单笔委托金额 {_money(amount)} 元（占总资产 "
                    f"{_pct(amount / total_asset * 100.0)}）超过单笔上限 "
                    f"{p['max_single_order_value_pct']:.2f}%（{_money(max_single_value)} 元）",
                    actual=round(amount, 2), limit=round(max_single_value, 2),
                )
            self._pass(checks, 7, "max_single_order_amount",
                       f"单笔金额 {_money(amount)} 元未超过上限 {_money(max_single_value)} 元",
                       actual=round(amount, 2), limit=round(max_single_value, 2))
        else:
            self._pass(checks, 7, "max_single_order_amount",
                       "账户总资产未知（≤0），跳过单笔上限校验", skipped=True)

        recent_order_times = _get_first(market, ("recent_order_times", "order_times"))
        if recent_order_times is None or reference_time is None:
            self._pass(checks, 7, "order_rate", "未提供最近下单时间，跳过频率校验", skipped=True)
        else:
            window_seconds = 60.0
            recent_count = 0
            for item in recent_order_times or ():
                moment = _to_datetime(item)
                if moment is None:
                    continue
                elapsed = _seconds_between(reference_time, moment)
                if 0.0 <= elapsed <= window_seconds:
                    recent_count += 1
            if recent_count >= int(p["max_orders_per_minute"]):
                return self._deny(
                    checks, 7, "order_rate", RiskCode.RK013,
                    f"最近 1 分钟已提交 {recent_count} 笔，达到上限 "
                    f"{p['max_orders_per_minute']} 笔/分钟",
                    actual=recent_count, limit=p["max_orders_per_minute"],
                )
            self._pass(checks, 7, "order_rate",
                       f"最近 1 分钟已提交 {recent_count} 笔，未超过上限",
                       actual=recent_count, limit=p["max_orders_per_minute"])

        # =================================================================
        # 第 8 步：账户状态（熔断 / 账户停用 / 自动化关闭时自动单）
        # =================================================================
        if side == _SIDE_BUY:
            # 熔断状态优先级：扩展字段 is_fused（可显式解锁） > fused=True > 按基线实时计算
            explicit_fused = _get(market, "is_fused", _MISSING)
            fused_field = _get(market, "fused", _MISSING)
            daily_open_asset = _to_float(_get(market, "daily_open_asset"))
            if explicit_fused is not _MISSING and explicit_fused is not None:
                if bool(explicit_fused):
                    return self._deny(
                        checks, 8, "daily_loss_circuit", RiskCode.RK009,
                        "当日已触发亏损熔断并进入冻结状态，今日禁止买入（卖出不受限制，次日盘前自动恢复）",
                        actual=True, limit=f"-{p['max_daily_loss_pct']:.2f}%",
                    )
                self._pass(checks, 8, "daily_loss_circuit", "熔断状态正常（显式标记未熔断）")
            elif fused_field is True:
                return self._deny(
                    checks, 8, "daily_loss_circuit", RiskCode.RK009,
                    "当日已触发亏损熔断（fused=true），今日禁止买入（卖出不受限制，次日盘前自动恢复）",
                    actual=True, limit=f"-{p['max_daily_loss_pct']:.2f}%",
                )
            elif daily_open_asset is None or daily_open_asset <= 0 or total_asset <= 0:
                self._pass(checks, 8, "daily_loss_circuit",
                           "缺少当日开盘总资产，跳过熔断判定（建议盘前记录基线）",
                           skipped=True)
            else:
                loss_pct = (total_asset - daily_open_asset) / daily_open_asset * 100.0
                if loss_pct <= -float(p["max_daily_loss_pct"]) + 1e-9:
                    return self._deny(
                        checks, 8, "daily_loss_circuit", RiskCode.RK009,
                        f"当日亏损 {loss_pct:.2f}% 已触及熔断线 -{p['max_daily_loss_pct']:.2f}%，"
                        "今日禁止买入（卖出不受限制，次日盘前自动恢复）",
                        actual=round(loss_pct, 4), limit=-float(p["max_daily_loss_pct"]),
                    )
                self._pass(checks, 8, "daily_loss_circuit",
                           f"当日盈亏 {loss_pct:+.2f}%，未触及熔断线",
                           actual=round(loss_pct, 4), limit=-float(p["max_daily_loss_pct"]))
        else:
            self._pass(checks, 8, "daily_loss_circuit", "卖出不受熔断限制", skipped=True)

        account_enabled = _get(account, "enabled")
        if account_enabled is False:
            return self._deny(
                checks, 8, "account_enabled", RiskCode.RK020,
                "账户状态为停用，禁止下单（请在设置页启用账户）",
                actual=False,
            )
        self._pass(checks, 8, "account_enabled", "账户状态正常")

        if source == _SOURCE_AUTO:
            # 自动化订单双重 fail-closed（Lead P0 修复）：
            #   ① 配置必须显式授权（allow_auto_orders=True，通常来自 automation.enabled=true）；
            #   ② 行情快照若显式标明 automation_enabled=False，则拒绝（运行期临时关闭）。
            automation_enabled = _get(market, "automation_enabled", _MISSING)
            if automation_enabled is False:
                return self._deny(
                    checks, 8, "automation_enabled", RiskCode.RK020,
                    "自动化总开关已关闭，拒绝自动单（手动单不受影响）",
                    actual=False,
                )
            if not bool(p.get("allow_auto_orders", False)):
                return self._deny(
                    checks, 8, "automation_enabled", RiskCode.RK020,
                    "未显式授权自动交易（risk.allow_auto_orders=false，默认 fail-closed）；"
                    "如需自动下单，请由 automation 层在配置中显式开启（automation.enabled=true）",
                    actual=False,
                )
            if automation_enabled is _MISSING or automation_enabled is None:
                self._pass(checks, 8, "automation_enabled",
                           "已显式授权自动交易（allow_auto_orders=true），未提供运行期自动化状态")
            else:
                self._pass(checks, 8, "automation_enabled", "自动化总开关已开启且已授权")
        else:
            self._pass(checks, 8, "automation_enabled", "手动单不受自动化开关限制", skipped=True)

        # =================================================================
        # 第 9 步：资金 / 持仓
        # =================================================================
        estimated_fees = self._estimate_fees(side, effective_price, quantity)
        if side == _SIDE_BUY:
            required_cash = amount + estimated_fees
            available_cash = self._available_cash(account)
            if required_cash > available_cash + 1e-6:
                gap = required_cash - available_cash
                affordable_lots = 0
                if effective_price > 0:
                    affordable_lots = int(available_cash / (effective_price * 100.0)) * 100
                return self._deny(
                    checks, 9, "cash_sufficiency", RiskCode.RK014,
                    f"买入需 {_money(required_cash)} 元"
                    f"（成交金额 {_money(amount)} + 预估费用 {_money(estimated_fees)}），"
                    f"可用 {_money(available_cash)} 元，还差 {_money(gap)} 元"
                    + (f"；按当前价最多可买约 {affordable_lots} 股" if effective_price > 0 else ""),
                    actual=round(required_cash, 2), limit=round(available_cash, 2),
                )
            self._pass(checks, 9, "cash_sufficiency",
                       f"资金充足：需 {_money(required_cash)} 元（含预估费用），可用 {_money(available_cash)} 元",
                       actual=round(required_cash, 2), limit=round(available_cash, 2))
        else:
            held_qty = (_to_int(_get(position, "quantity"), 0) or 0) if position else 0
            if position is None or quantity > held_qty:
                return self._deny(
                    checks, 9, "position_sufficiency", RiskCode.RK015,
                    f"当前持有 {held_qty} 股，委托卖出 {quantity} 股",
                    actual=quantity, limit=held_qty,
                )
            if quantity > available_qty:
                return self._deny(
                    checks, 9, "t1_availability", RiskCode.RK016,
                    f"可卖 {available_qty} 股，委托卖出 {quantity} 股"
                    f"（T+1：今日买入不可卖，明日可卖）",
                    actual=quantity, limit=available_qty,
                )
            self._pass(checks, 9, "t1_availability",
                       f"持仓充足：持有 {held_qty} 股，可卖 {available_qty} 股",
                       actual=quantity, limit=available_qty)

        # =================================================================
        # 第 10 步：仓位额度（单票上限 / 总仓位上限；卖出不受限）
        # =================================================================
        if side == _SIDE_SELL:
            self._pass(checks, 10, "position_limit", "卖出不受仓位上限限制（允许减仓）", skipped=True)
        else:
            if total_asset <= 0:
                return self._deny(
                    checks, 10, "position_limit", RiskCode.RK018,
                    "账户总资产异常（≤0），无法计算仓位占比，已按 fail-closed 拒绝",
                    actual=total_asset,
                )
            single_position_value = self._position_value(position, latest_prices) if position else 0.0
            single_after_value = single_position_value + amount
            single_after_pct = single_after_value / total_asset * 100.0
            if single_after_pct > float(p["max_position_per_symbol_pct"]) + 1e-9:
                reduce_qty = self._reduce_quantity(
                    single_after_value - total_asset * float(p["max_position_per_symbol_pct"]) / 100.0,
                    effective_price,
                )
                return self._deny(
                    checks, 10, "single_position_limit", RiskCode.RK007,
                    f"买入后 {symbol} 仓位将达 {_pct(single_after_pct)}，"
                    f"超过单票上限 {p['max_position_per_symbol_pct']:.2f}%"
                    + (f"，建议减少约 {reduce_qty} 股" if reduce_qty else "")
                    + "（或调高上限 trading.risk.max_position_per_symbol_pct）",
                    actual=round(single_after_pct, 4), limit=p["max_position_per_symbol_pct"],
                )
            self._pass(checks, 10, "single_position_limit",
                       f"买入后 {symbol} 仓位 {_pct(single_after_pct)}，未超过单票上限 "
                       f"{p['max_position_per_symbol_pct']:.2f}%",
                       actual=round(single_after_pct, 4), limit=p["max_position_per_symbol_pct"])

            total_position_value = sum(
                self._position_value(pos, latest_prices) for pos in position_map.values()
            )
            total_after_value = total_position_value + amount
            total_after_pct = total_after_value / total_asset * 100.0
            if total_after_pct > float(p["max_total_position_pct"]) + 1e-9:
                reduce_qty = self._reduce_quantity(
                    total_after_value - total_asset * float(p["max_total_position_pct"]) / 100.0,
                    effective_price,
                )
                return self._deny(
                    checks, 10, "total_position_limit", RiskCode.RK008,
                    f"买入后总仓位将达 {_pct(total_after_pct)}，"
                    f"超过总仓位上限 {p['max_total_position_pct']:.2f}%"
                    + (f"，建议减少约 {reduce_qty} 股" if reduce_qty else "")
                    + "（或调高上限 trading.risk.max_total_position_pct）",
                    actual=round(total_after_pct, 4), limit=p["max_total_position_pct"],
                )
            self._pass(checks, 10, "total_position_limit",
                       f"买入后总仓位 {_pct(total_after_pct)}，未超过上限 "
                       f"{p['max_total_position_pct']:.2f}%",
                       actual=round(total_after_pct, 4), limit=p["max_total_position_pct"])

        # =================================================================
        # 全部通过
        # =================================================================
        self._pass(checks, 10, "final", "风控全部检查通过，允许下单")
        return RiskDecision(
            allow=True,
            reason_code=None,
            message="风控通过",
            details={
                "checks_total": len(checks),
                "steps": len(CHECK_STEP_NAMES),
                "estimated_fees": round(estimated_fees, 2),
                "order_amount": round(amount, 2),
            },
            checks=list(checks),
        )

    # =====================================================================
    # 内部工具（纯函数）
    # =====================================================================

    def _pass(
        self,
        checks: list[dict[str, Any]],
        step: int,
        rule: str,
        message: str,
        *,
        actual: Any = None,
        limit: Any = None,
        skipped: bool = False,
    ) -> None:
        """记录一条通过的检查（skipped=True 表示缺少数据、跳过而非判定通过）。"""
        checks.append(
            {
                "step": step,
                "step_name": CHECK_STEP_NAMES[step - 1] if 1 <= step <= len(CHECK_STEP_NAMES) else "风控总开关",
                "rule": rule,
                "passed": True,
                "skipped": skipped,
                "reason_code": None,
                "message": message,
                "actual": _jsonable(actual),
                "limit": _jsonable(limit),
            }
        )

    def _deny(
        self,
        checks: list[dict[str, Any]],
        step: int,
        rule: str,
        code: RiskCode,
        detail: str,
        *,
        actual: Any = None,
        limit: Any = None,
    ) -> RiskDecision:
        """构造“拒绝”决策：记录未通过明细，返回带 RK 码与建议的中文结果。"""
        code_value = code.value
        reason = RISK_REASON_CODES[code_value]
        checks.append(
            {
                "step": step,
                "step_name": CHECK_STEP_NAMES[step - 1] if 1 <= step <= len(CHECK_STEP_NAMES) else "内部异常",
                "rule": rule,
                "passed": False,
                "skipped": False,
                "reason_code": code_value,
                "message": detail,
                "actual": _jsonable(actual),
                "limit": _jsonable(limit),
            }
        )
        return RiskDecision(
            allow=False,
            reason_code=code_value,
            message=f"{reason.meaning}：{detail}〔{code_value}〕→ 建议：{reason.suggestion}",
            details={
                "step": step,
                "step_name": CHECK_STEP_NAMES[step - 1] if 1 <= step <= len(CHECK_STEP_NAMES) else "内部异常",
                "rule": rule,
                "reason_code": code_value,
                "actual": _jsonable(actual),
                "limit": _jsonable(limit),
                "suggestion": reason.suggestion,
            },
            checks=list(checks),
        )

    # -- 标的与持仓工具 ---------------------------------------------------

    @staticmethod
    def _board_of(symbol: str) -> str:
        """按代码前缀推断板块（MAIN/GEM/STAR/BSE），用于整手规则与提示文案。"""
        code = (symbol or "").split(".")[0]
        if code.startswith(("688", "689")):
            return "STAR"
        if code.startswith(("300", "301")):
            return "GEM"
        if code.startswith(("8", "920")):
            return "BSE"
        return "MAIN"

    @staticmethod
    def _position_map(positions: Iterable[Any]) -> dict[str, Any]:
        """持仓列表 → {symbol: position}（symbol 统一大写）。"""
        result: dict[str, Any] = {}
        for pos in positions or ():
            symbol = _as_symbol(_get(pos, "symbol"))
            if symbol:
                result[symbol] = pos
        return result

    def _validate_lot(
        self,
        side: str,
        quantity: int,
        available: int,
        board: str,
    ) -> tuple[bool, str | None]:
        """整手校验：优先复用 CF-10 的 ``AStockRules.validate_lot``（唯一实现），否则内置兜底。

        注意：``AStockRules.validate_lot`` 在卖出数量超过可用数量时会返回 ``RK016``，
        而 CF-5 的判定顺序要求 T+1/持仓不足在第 9 步（RK015/RK016）判定，
        因此这里把 ``RK016`` 视为“整手本身合法、交给第 9 步处理”，避免原因码错位。
        """
        if _AStockRules is not None:
            try:
                result = _AStockRules.validate_lot(side, quantity, available, board)
                if isinstance(result, tuple) and len(result) >= 2:
                    ok = bool(result[0])
                    reason_code = str(result[1]) if result[1] else None
                    if ok or reason_code == RiskCode.RK016.value:
                        return True, None
                    return False, self._lot_message(side, quantity, board)
            except Exception:  # noqa: BLE001 - 复用失败时退回内置规则，不影响风控可用性
                pass

        # 内置兜底（SPEC §4.4 口径）
        if side == _SIDE_BUY:
            if board == "STAR":
                return (True, None) if quantity >= 200 else (
                    False, self._lot_message(side, quantity, board)
                )
            if board == "BSE":
                return (True, None) if quantity >= 100 else (
                    False, self._lot_message(side, quantity, board)
                )
            if quantity % 100 != 0:
                return False, self._lot_message(side, quantity, board)
            return True, None
        # 卖出：可为零股，但零股必须一次性全部卖出；非全部卖出时须为 100 股整数倍
        if available > 0 and quantity >= available:
            return True, None
        if quantity % 100 != 0:
            return False, self._lot_message(side, quantity, board)
        return True, None

    @staticmethod
    def _lot_message(side: str, quantity: int, board: str) -> str:
        """整手规则失败的中文可读说明（含板块与建议）。"""
        if side == _SIDE_BUY:
            if board == "STAR":
                return f"科创板买入数量不得少于 200 股（当前 {quantity} 股），此后可按 1 股递增"
            if board == "BSE":
                return f"北交所买入数量不得少于 100 股（当前 {quantity} 股）"
            return f"买入数量必须为 100 股的整数倍（当前 {quantity} 股）"
        return f"卖出数量必须为 100 股的整数倍（当前 {quantity} 股）；零股请一次性全部卖出"

    # -- 行情与账户工具 ---------------------------------------------------

    @staticmethod
    def _get_price_limits(market: Any, order: Any, symbol: str) -> tuple[float | None, float | None]:
        """取涨停/跌停价（支持 price_limits / limit_up_prices / limit_down_prices / order 字段）。"""
        price_limits = _get(market, "price_limits")
        if isinstance(price_limits, Mapping):
            pair = _lookup(price_limits, symbol)
            if isinstance(pair, (tuple, list)) and len(pair) >= 2:
                return _to_float(pair[0]), _to_float(pair[1])
        limit_up = _to_float(_lookup(_get(market, "limit_up_prices"), symbol))
        limit_down = _to_float(_lookup(_get(market, "limit_down_prices"), symbol))
        if limit_up is None:
            limit_up = _to_float(_get(order, "limit_up"))
        if limit_down is None:
            limit_down = _to_float(_get(order, "limit_down"))
        return limit_up, limit_down

    @staticmethod
    def _reference_time(market: Any, order: Any) -> datetime | None:
        """确定性参照时间：market 显式时间 → order 时间 → trade_date 当日 15:00。"""
        explicit = _get_first(market, ("as_of", "now", "current_time", "snapshot_time", "server_time"))
        moment = _to_datetime(explicit)
        if moment is not None:
            return moment
        order_time = _get_first(order, ("created_at", "submitted_at", "order_time", "timestamp", "ts"))
        moment = _to_datetime(order_time)
        if moment is not None:
            return moment
        trade_date = _to_date(_get(market, "trade_date"))
        if trade_date is not None:
            return datetime.combine(trade_date, time(15, 0))
        return None

    @staticmethod
    def _position_value(position: Any, latest_prices: Any) -> float:
        """单个持仓市值：最新价 × 数量；无最新价时用 last_price / avg_cost / market_value。"""
        if position is None:
            return 0.0
        symbol = _as_symbol(_get(position, "symbol"))
        quantity = _to_int(_get(position, "quantity"), 0) or 0
        if quantity <= 0:
            return 0.0
        price = _to_float(_lookup(latest_prices, symbol))
        if price is None or price <= 0:
            price = _to_float(_get(position, "last_price"))
        if price is None or price <= 0:
            price = _to_float(_get(position, "avg_cost"))
        if price is None or price <= 0:
            market_value = _to_float(_get(position, "market_value"), 0.0) or 0.0
            return max(market_value, 0.0)
        return quantity * price

    @staticmethod
    def _total_asset(
        account: Any,
        positions: Iterable[Any],
        market: Any,
        latest_prices: Mapping[str, Any] | Any,
    ) -> float:
        """账户总资产：优先 ``account.total_asset``；缺失/非正时用“现金 + 持仓市值”兜底。"""
        total = _to_float(_get(account, "total_asset"))
        if total is not None and total > 0:
            return total
        cash = _to_float(_get(account, "cash"), 0.0) or 0.0
        position_value = sum(
            RiskGate._position_value(pos, latest_prices) for pos in (positions or ())
        )
        return max(cash + position_value, 0.0)

    @staticmethod
    def _available_cash(account: Any) -> float:
        """可用资金：优先 available_cash；否则 cash − frozen_cash（宁可低估，不可高估）。"""
        explicit = _to_float(_get(account, "available_cash"))
        if explicit is not None:
            return max(explicit, 0.0)
        cash = _to_float(_get(account, "cash"), 0.0) or 0.0
        frozen = _to_float(_get(account, "frozen_cash"), 0.0) or 0.0
        return max(cash - frozen, 0.0)

    def _estimate_fees(self, side: str, price: float, quantity: int) -> float:
        """风控侧费用估算（仅用于判断资金是否足够）。

        注意：成交费用以 CF-10 ``FeeModel`` 为准；这里按 SPEC §4.5 同口径独立估算，
        每项四舍五入到 0.01 元后累加，宁可略高估（避免资金不足）。
        """
        p = self._params
        amount = price * quantity
        commission = round(max(amount * float(p["commission_rate"]), float(p["min_commission"])), 2)
        transfer_fee = round(amount * float(p["transfer_fee_rate"]), 2)
        reg_fee = round(amount * float(p["reg_fee_rate"]), 2)
        stamp_tax = round(amount * float(p["stamp_tax_rate"]), 2) if side == _SIDE_SELL else 0.0
        return commission + transfer_fee + reg_fee + stamp_tax

    @staticmethod
    def _reduce_quantity(excess_value: float, price: float) -> int:
        """按超出金额估算建议减少的股数（向上取整到 100 股）。"""
        if excess_value <= 0 or price <= 0:
            return 0
        lots = math.ceil(excess_value / price / 100.0)
        return max(lots, 0) * 100


__all__ = [
    "RiskGate",
    "RiskCode",
    "RiskReason",
    "RISK_REASON_CODES",
    "ALL_RISK_CODES",
    "CHECK_STEP_NAMES",
    "DEFAULT_RISK_PARAMS",
    "describe_reason",
]

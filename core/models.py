"""QuantDesk 核心领域模型与 ORM 表定义（契约冻结项 CF-7）。

本文件是团队公共契约 ``core/models.py`` 的唯一实现，包含两部分：

1. **领域模型（pydantic v2）**：供 ``strategy/``、``trading/``、``backtest/``、``automation/``、
   ``ui/`` 与 ``data/`` 共享的枚举、值对象与 DTO。字段与 SPEC §6.7 冻结一致。
2. **ORM 表（SQLAlchemy 2.0）**：SPEC §5.2 规定的 22 张表，供 ``core/db.py`` 建表、
   各模块通过 ``core/db.py`` 的会话读写。

口径与设计决定（Dev1 冻结，供 Lead 评审）
------------------------------------------------
- **时间**：业务时间戳统一使用本地时区 ``Asia/Shanghai``（SPEC §5.1 允许二选一，本文件冻结为本地时间）。
  DB 列用 ``DATETIME`` 保存**无时区信息的本地时间**（``now_local_naive()``），
  pydantic 模型的时间戳默认带 ``Asia/Shanghai`` 时区（``now_local()``），便于日志与跨进程追踪。
- **外键**：表之间使用**逻辑外键**（只建索引，不建物理 FOREIGN KEY 约束）。
  原因：SQLite 物理外键在批量 upsert / 回测临时数据写入时容易误伤，且契约只要求 PK/唯一/索引齐全；
  数据一致性由 ``core/db.py`` 启动对账与各服务层保证（SPEC §5.3）。
- **命名**：pydantic 模型使用契约名（``Order``/``Account``/``Position``…）；
  ORM 类统一加 ``ORM`` 后缀（``OrderORM``/``AccountORM``…），避免与 pydantic 模型重名。
  表名严格按 SPEC §5.2，见 ``TABLE_NAMES``。
- **pydantic 配置**：业务模型允许额外字段（``extra="allow"``），方便插件/UI 透传扩展字段；
  但不放宽必填校验。``OrderRequest`` **故意不做业务合法性校验**（价格>0、整手、涨跌停等由
  ``RiskGate`` 统一判定 RK011/RK017/RK018），避免两处校验口径冲突。
- 时间戳字段：所有业务表带 ``created_at``/``updated_at``（本地时间，不带时区）。

严禁在本文件中 import 任何业务模块（SPEC §3.1 依赖方向硬约束）。
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# ---------------------------------------------------------------------------
# 时区与时间工具
# ---------------------------------------------------------------------------

#: 系统统一时区（SPEC §7 app.timezone，冻结）。
#: Windows 上若未安装 ``tzdata``（Python 自带时区库缺失），退化为固定 UTC+8；
#: 中国无夏令时，两者等价，保证系统在最小依赖下也能启动。
try:
    LOCAL_TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - 仅 Windows 缺 tzdata 时触发
    LOCAL_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def now_local() -> datetime:
    """返回带 ``Asia/Shanghai`` 时区的当前时间。

    用途：pydantic 模型时间戳默认值、日志/事件时间戳。
    """
    return datetime.now(tz=LOCAL_TZ)


def now_local_naive() -> datetime:
    """返回不带时区信息的本地当前时间（用于 DB 的 DATETIME 列默认值）。"""
    return datetime.now(tz=LOCAL_TZ).replace(tzinfo=None)


def today_local() -> date:
    """返回本地时区的今天日期（交易日字段默认值）。"""
    return datetime.now(tz=LOCAL_TZ).date()


# ---------------------------------------------------------------------------
# 枚举（SPEC §5 / §4 / §8 / §9.2 冻结值）
# ---------------------------------------------------------------------------


class Side(str, Enum):
    """买卖方向（SPEC §5.2 orders.side 冻结）。"""

    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    """委托类型（SPEC §5.2 orders.order_type 冻结）。"""

    LIMIT = "LIMIT"
    MARKET = "MARKET"


class OrderStatus(str, Enum):
    """订单生命周期状态机（SPEC §5.2 orders.status 冻结）。

    合法迁移（唯一事实来源，OrderService 维护）::

        PENDING → SUBMITTED → PARTIALLY_FILLED → FILLED
                            ↘ CANCELLED / REJECTED / EXPIRED
    """

    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"

    @property
    def is_terminal(self) -> bool:
        """是否为终态（终态不可撤单、不可再变更）。"""
        return self in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        )

    @property
    def can_cancel(self) -> bool:
        """是否允许撤单（SPEC §8.4：仅 SUBMITTED / PARTIALLY_FILLED 可撤）。"""
        return self in (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED)


#: 订单可撤状态集合（供 Dev3 直接使用，避免各处硬编码状态名）
ORDER_CANCELLABLE_STATUSES = frozenset({OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED})
#: 订单活跃状态集合（未终结）
ORDER_ACTIVE_STATUSES = frozenset(
    {OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED}
)


class OrderSource(str, Enum):
    """订单来源（SPEC §5.2 orders.source：MANUAL/AUTO）。"""

    MANUAL = "MANUAL"
    AUTO = "AUTO"


class BrokerType(str, Enum):
    """账户所属券商/适配器类型（SPEC §5.2 accounts.broker：PAPER/CCXT/QMT）。"""

    PAPER = "PAPER"
    CCXT = "CCXT"
    QMT = "QMT"


class SymbolKind(str, Enum):
    """标的类型（SPEC §5.2 symbols.kind：STOCK/INDEX）。"""

    STOCK = "STOCK"
    INDEX = "INDEX"


class Exchange(str, Enum):
    """交易所（SPEC §5.2 symbols.exchange：SH/SZ/BJ）。"""

    SH = "SH"
    SZ = "SZ"
    BJ = "BJ"


class Board(str, Enum):
    """板块（SPEC §5.2 symbols.board：MAIN/GEM/STAR/BSE），决定涨跌停幅度。"""

    MAIN = "MAIN"  # 沪深主板 ±10%（ST ±5%）
    GEM = "GEM"    # 创业板 ±20%
    STAR = "STAR"  # 科创板 ±20%
    BSE = "BSE"    # 北交所 ±30%（MVP 默认不交易，见 OQ-2）


class TradeStatus(str, Enum):
    """标的交易状态（SPEC §4.7）。"""

    NORMAL = "NORMAL"      # 正常交易
    SUSPENDED = "SUSPENDED"  # 停牌
    DELISTING = "DELISTING"  # 退市整理（黑名单）
    DELISTED = "DELISTED"    # 已退市（不可交易）


class AdjustEventType(str, Enum):
    """除权除息事件类型（SPEC §5.2 adjust_events.event_type）。"""

    DIVIDEND = "DIVIDEND"  # 现金分红
    SPLIT = "SPLIT"        # 送股/转增/拆股
    RIGHTS = "RIGHTS"      # 配股


class IssueSeverity(str, Enum):
    """数据质量问题严重度（SPEC §5.2 data_quality_issues.severity）。"""

    ERROR = "ERROR"
    WARN = "WARN"


class StrategyMode(str, Enum):
    """策略实例运行模式（SPEC §5.2 strategy_instances.mode：OFF/PAPER/AUTO）。"""

    OFF = "OFF"
    PAPER = "PAPER"
    AUTO = "AUTO"


class SignalStatus(str, Enum):
    """信号处置状态（SPEC §5.2 strategy_signals.status 冻结）。"""

    GENERATED = "GENERATED"
    RISK_BLOCKED = "RISK_BLOCKED"
    ORDERED = "ORDERED"
    IGNORED = "IGNORED"


class BacktestStatus(str, Enum):
    """回测运行状态（SPEC §5.2 backtest_runs.status）。"""

    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


class CashFlowType(str, Enum):
    """资金流水类型（SPEC §5.2 cash_flows.flow_type 冻结）。"""

    DEPOSIT = "DEPOSIT"
    WITHDRAW = "WITHDRAW"
    BUY = "BUY"
    SELL = "SELL"
    DIVIDEND = "DIVIDEND"
    FEE = "FEE"
    OTHER = "OTHER"


class AuditActor(str, Enum):
    """审计操作者（SPEC §5.2 audit_logs.actor）。

    取值说明：``manual``/``scheduler``/``system`` 为 SPEC §5.2 冻结口径；
    ``auto`` 为 SPEC §8.4「手工/自动订单同一张表，审计区分 actor」补充的自动化下单口径，
    与 ``scheduler`` 的区别：``scheduler`` 指 job 调度本身，``auto`` 指 job 内部的自动下单动作。
    """

    MANUAL = "manual"
    SCHEDULER = "scheduler"
    SYSTEM = "system"
    AUTO = "auto"


class AuditResult(str, Enum):
    """审计结果（SPEC §5.2 audit_logs.result，未冻结具体值，本文件定义为以下三种）。"""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    DENIED = "DENIED"


class JobRunStatus(str, Enum):
    """定时任务执行状态（SPEC §5.2 job_runs.status：SUCCESS/FAILED/SKIPPED）。"""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class NotificationChannel(str, Enum):
    """通知渠道（SPEC §5.2 notifications.channel：DESKTOP/EMAIL/WEBHOOK）。"""

    DESKTOP = "DESKTOP"
    EMAIL = "EMAIL"
    WEBHOOK = "WEBHOOK"


class NotificationStatus(str, Enum):
    """通知发送状态（SPEC §5.2 notifications.status：SENT/FAILED/DISABLED）。"""

    SENT = "SENT"
    FAILED = "FAILED"
    DISABLED = "DISABLED"


class ProviderStatus(str, Enum):
    """数据源健康状态（SPEC §6.1 health_check：OK/DEGRADED/DOWN）。"""

    OK = "OK"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"


# ---------------------------------------------------------------------------
# 原因码（SPEC §9.2 冻结）
# ---------------------------------------------------------------------------


class RiskReasonCode(str, Enum):
    """风控拒绝原因码 RK001–RK020（SPEC §9.2 冻结，禁止增删改）。"""

    RK001 = "RK001"  # SYMBOL_NOT_IN_UNIVERSE    标的不在可交易池
    RK002 = "RK002"  # SYMBOL_BLACKLISTED        标的在黑名单
    RK003 = "RK003"  # SYMBOL_SUSPENDED          标的停牌
    RK004 = "RK004"  # LISTED_TOO_SHORT          上市不足 N 个交易日
    RK005 = "RK005"  # MARKET_CLOSED             非交易时段/非交易日
    RK006 = "RK006"  # DATA_STALE                行情数据过期
    RK007 = "RK007"  # SINGLE_POSITION_LIMIT     单票仓位超限
    RK008 = "RK008"  # TOTAL_POSITION_LIMIT      总仓位超限
    RK009 = "RK009"  # DAILY_LOSS_LIMIT          单日亏损熔断
    RK010 = "RK010"  # PRICE_DEVIATION           价格偏离过大
    RK011 = "RK011"  # PRICE_OUT_OF_LIMIT        价格超出涨跌停区间
    RK012 = "RK012"  # ORDER_VALUE_LIMIT         单笔金额超限
    RK013 = "RK013"  # ORDER_RATE_LIMIT          下单频率超限
    RK014 = "RK014"  # INSUFFICIENT_CASH         可用资金不足
    RK015 = "RK015"  # INSUFFICIENT_POSITION     持仓不足
    RK016 = "RK016"  # T1_UNAVAILABLE            T+1 可用数量不足
    RK017 = "RK017"  # LOT_SIZE_INVALID          数量不符合整手规则
    RK018 = "RK018"  # PARAM_INVALID             参数非法（含风控内部异常 fail-closed）
    RK019 = "RK019"  # DUPLICATE_ORDER           重复订单（幂等键冲突）
    RK020 = "RK020"  # ACCOUNT_DISABLED          账户停用/自动化关闭时提交自动单


class BrokerErrorCode(str, Enum):
    """券商/适配层错误码 BR001–BR005（SPEC §9.2 冻结）。"""

    BR001 = "BR001"  # BROKER_UNAVAILABLE         券商不可用/网络异常
    BR002 = "BR002"  # ORDER_REJECTED             券商拒单（含不可撤单）
    BR003 = "BR003"  # SYMBOL_UNSUPPORTED         标的不支持
    BR004 = "BR004"  # ADAPTER_NOT_IMPLEMENTED    适配器未实现（如 QmtBroker）
    BR005 = "BR005"  # REQUEST_TIMEOUT            请求超时


class DataQualityCode(str, Enum):
    """数据质量码 DQ001–DQ007（SPEC §9.2 冻结）。"""

    DQ001 = "DQ001"  # 缺失 K 线
    DQ002 = "DQ002"  # 重复 K 线
    DQ003 = "DQ003"  # 价格异常跳变
    DQ004 = "DQ004"  # 零成交量
    DQ005 = "DQ005"  # 复权因子缺失
    DQ006 = "DQ006"  # 数据过期
    DQ007 = "DQ007"  # 停牌未标记


#: 回测警告码（SPEC §9.2 冻结）
RQ_WARN_NO_ADJ = "RQ_WARN_NO_ADJ"
RQ_WARN_ST_APPROX = "RQ_WARN_ST_APPROX"
RQ_WARN_SHORT_SAMPLE = "RQ_WARN_SHORT_SAMPLE"
RQ_WARN_FEW_TRADES = "RQ_WARN_FEW_TRADES"
RQ_WARN_HIGH_SUSPEND = "RQ_WARN_HIGH_SUSPEND"

#: 风控原因码 → {常量名, 中文含义, 建议动作}（SPEC §9.2 全表，供 UI/日志/测试统一引用）
RISK_REASON_MESSAGES: dict[str, dict[str, str]] = {
    RiskReasonCode.RK001.value: {
        "name": "SYMBOL_NOT_IN_UNIVERSE", "meaning": "标的不在可交易池", "suggestion": "先加入自选池",
    },
    RiskReasonCode.RK002.value: {
        "name": "SYMBOL_BLACKLISTED", "meaning": "标的在黑名单（ST/退市整理/用户禁用）", "suggestion": "更换标的",
    },
    RiskReasonCode.RK003.value: {
        "name": "SYMBOL_SUSPENDED", "meaning": "标的停牌", "suggestion": "复牌后再操作",
    },
    RiskReasonCode.RK004.value: {
        "name": "LISTED_TOO_SHORT", "meaning": "上市不足 60 个交易日", "suggestion": "更换标的",
    },
    RiskReasonCode.RK005.value: {
        "name": "MARKET_CLOSED", "meaning": "非交易时段/非交易日", "suggestion": "交易时段再下单",
    },
    RiskReasonCode.RK006.value: {
        "name": "DATA_STALE", "meaning": "行情数据过期", "suggestion": "刷新行情/检查数据源",
    },
    RiskReasonCode.RK007.value: {
        "name": "SINGLE_POSITION_LIMIT", "meaning": "单票仓位超限", "suggestion": "减少数量或调高上限",
    },
    RiskReasonCode.RK008.value: {
        "name": "TOTAL_POSITION_LIMIT", "meaning": "总仓位超限", "suggestion": "减仓或调高上限",
    },
    RiskReasonCode.RK009.value: {
        "name": "DAILY_LOSS_LIMIT", "meaning": "单日亏损熔断", "suggestion": "今日停止买入，明日自动恢复",
    },
    RiskReasonCode.RK010.value: {
        "name": "PRICE_DEVIATION", "meaning": "价格偏离过大", "suggestion": "检查价格（接近涨跌停/手误）",
    },
    RiskReasonCode.RK011.value: {
        "name": "PRICE_OUT_OF_LIMIT", "meaning": "价格超出涨跌停区间", "suggestion": "改到有效区间",
    },
    RiskReasonCode.RK012.value: {
        "name": "ORDER_VALUE_LIMIT", "meaning": "单笔金额超限", "suggestion": "拆单或调高上限",
    },
    RiskReasonCode.RK013.value: {
        "name": "ORDER_RATE_LIMIT", "meaning": "下单频率超限", "suggestion": "稍后再试",
    },
    RiskReasonCode.RK014.value: {
        "name": "INSUFFICIENT_CASH", "meaning": "可用资金不足", "suggestion": "减少数量/卖出腾资金",
    },
    RiskReasonCode.RK015.value: {
        "name": "INSUFFICIENT_POSITION", "meaning": "持仓不足", "suggestion": "检查持仓数量",
    },
    RiskReasonCode.RK016.value: {
        "name": "T1_UNAVAILABLE", "meaning": "T+1 可用数量不足（今日买入不可卖）", "suggestion": "明日再卖",
    },
    RiskReasonCode.RK017.value: {
        "name": "LOT_SIZE_INVALID", "meaning": "数量不符合整手规则", "suggestion": "按 100 股整数倍",
    },
    RiskReasonCode.RK018.value: {
        "name": "PARAM_INVALID", "meaning": "参数非法（含风控内部异常 fail-closed）", "suggestion": "检查输入",
    },
    RiskReasonCode.RK019.value: {
        "name": "DUPLICATE_ORDER", "meaning": "重复订单（幂等键冲突）", "suggestion": "查今日委托",
    },
    RiskReasonCode.RK020.value: {
        "name": "ACCOUNT_DISABLED", "meaning": "账户停用/自动化关闭时提交自动单", "suggestion": "开启账户或自动化",
    },
}

#: 券商适配层错误码中文说明
BROKER_ERROR_MESSAGES: dict[str, dict[str, str]] = {
    BrokerErrorCode.BR001.value: {
        "name": "BROKER_UNAVAILABLE", "meaning": "券商不可用/网络异常", "suggestion": "检查网络或稍后重试",
    },
    BrokerErrorCode.BR002.value: {
        "name": "ORDER_REJECTED", "meaning": "券商拒单", "suggestion": "查看券商返回信息",
    },
    BrokerErrorCode.BR003.value: {
        "name": "SYMBOL_UNSUPPORTED", "meaning": "标的不支持", "suggestion": "更换标的",
    },
    BrokerErrorCode.BR004.value: {
        "name": "ADAPTER_NOT_IMPLEMENTED", "meaning": "适配器未实现（如 QMT 未接入）", "suggestion": "使用模拟盘",
    },
    BrokerErrorCode.BR005.value: {
        "name": "REQUEST_TIMEOUT", "meaning": "请求超时", "suggestion": "稍后重试",
    },
}

#: 数据质量码中文说明
DATA_QUALITY_MESSAGES: dict[str, dict[str, str]] = {
    DataQualityCode.DQ001.value: {"meaning": "缺失 K 线", "suggestion": "重新拉取该区间数据"},
    DataQualityCode.DQ002.value: {"meaning": "重复 K 线", "suggestion": "执行去重（保留最新）"},
    DataQualityCode.DQ003.value: {"meaning": "价格异常跳变", "suggestion": "检查除权除息事件或数据源"},
    DataQualityCode.DQ004.value: {"meaning": "零成交量", "suggestion": "确认是否停牌/节假日前"},
    DataQualityCode.DQ005.value: {"meaning": "复权因子缺失", "suggestion": "补齐复权因子，否则回测降级为不复权"},
    DataQualityCode.DQ006.value: {"meaning": "数据过期", "suggestion": "刷新行情/检查数据源"},
    DataQualityCode.DQ007.value: {"meaning": "停牌未标记", "suggestion": "按停牌处理并补充标记"},
}

#: 回测警告码中文说明（SPEC §9.2）
BACKTEST_WARNING_MESSAGES: dict[str, str] = {
    RQ_WARN_NO_ADJ: "未复权：结果可能因除权除息失真",
    RQ_WARN_ST_APPROX: "ST 状态为近似口径：历史 ST 标记不可得",
    RQ_WARN_SHORT_SAMPLE: "样本期过短（<250 交易日）：结果可信度低",
    RQ_WARN_FEW_TRADES: "交易次数过少（<30 笔）：统计意义不足",
    RQ_WARN_HIGH_SUSPEND: "停牌占比过高：成交假设可能偏离实际",
}

#: 全部原因码字典的合并视图（码 → 中文说明信息）
ALL_REASON_CODE_MESSAGES: dict[str, dict[str, str]] = {
    **RISK_REASON_MESSAGES,
    **BROKER_ERROR_MESSAGES,
    **DATA_QUALITY_MESSAGES,
}


def _code_value(code: Any) -> str:
    """把原因码枚举/字符串统一成字符串码值（``RiskReasonCode.RK014`` → ``"RK014"``）。

    注意：``str(StrEnum)`` 在 Python 3.12 会得到 ``"RiskReasonCode.RK014"``，
    因此凡是要落库/日志/比较的原因码都必须先取 ``.value``。
    """
    return str(getattr(code, "value", code))


def describe_reason(code: str | RiskReasonCode | None) -> str:
    """把原因码翻译为中文描述（含原因码本身），用于 ``RiskDecision.message`` 与 UI 提示。"""
    if not code:
        return ""
    code_value = _code_value(code)
    info = ALL_REASON_CODE_MESSAGES.get(code_value)
    if info is None:
        return f"未知原因码：{code_value}"
    return f"{info['meaning']}（{code_value}）"


def suggest_reason(code: str | RiskReasonCode | None) -> str:
    """返回原因码对应的中文建议动作；未知码返回空字符串。"""
    if not code:
        return ""
    info = ALL_REASON_CODE_MESSAGES.get(_code_value(code))
    return info.get("suggestion", "") if info else ""


def build_client_order_id(
    strategy_instance_id: str,
    symbol: str,
    trade_date: date | datetime | str,
    side: Side | str,
) -> str:
    """按 SPEC §8.5 生成自动化订单幂等键 ``client_order_id``。

    口径：``sha1("strategy_instance_id|symbol|trade_date|side")`` 的十六进制摘要。
    同一交易日、同一策略实例、同一标的、同一方向重复触发生成的键一致，
    由 ``orders.client_order_id`` 唯一约束保证不重复下单。

    :param strategy_instance_id: 策略实例 ID
    :param symbol: 标的代码（如 ``600519.SH``）
    :param trade_date: 交易日（``date``/``datetime`` 或 ``YYYY-MM-DD`` 字符串）
    :param side: 买卖方向（``Side`` 或 ``"BUY"/"SELL"``，含中文别名）
    :return: 40 位十六进制幂等键
    """
    if isinstance(trade_date, datetime):
        day = trade_date.date().isoformat()
    elif isinstance(trade_date, date):
        day = trade_date.isoformat()
    else:
        day = str(trade_date).strip()
    raw = f"{strategy_instance_id}|{symbol}|{day}|{normalize_side(side).value}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# pydantic 模型基类
# ---------------------------------------------------------------------------


class QDModel(BaseModel):
    """所有领域模型的基类。

    - ``extra="allow"``：允许透传扩展字段（UI/插件/日志），不破坏冻结字段；
    - ``from_attributes=True``：支持 ``Account.model_validate(account_orm)`` 这类 ORM → DTO 转换。
    """

    model_config = ConfigDict(
        extra="allow",
        from_attributes=True,
        populate_by_name=True,
        validate_assignment=False,
        str_strip_whitespace=True,
    )

    def to_dict(self) -> dict[str, Any]:
        """转为 JSON 安全的字典（枚举/日期时间转为字符串），供事件 payload / 日志 / 落库使用。"""
        return self.model_dump(mode="json")

    def as_dict(self) -> dict[str, Any]:
        """``to_dict`` 的兼容别名（不同模块命名习惯）。"""
        return self.to_dict()


#: 交易方向别名（兼容策略/UI/手工输入的中文与简写；统一归一化为 :class:`Side`）
_SIDE_ALIASES: dict[str, Side] = {
    "BUY": Side.BUY,
    "B": Side.BUY,
    "LONG": Side.BUY,
    "买": Side.BUY,
    "买入": Side.BUY,
    "SELL": Side.SELL,
    "S": Side.SELL,
    "SHORT": Side.SELL,
    "卖": Side.SELL,
    "卖出": Side.SELL,
}


def normalize_side(value: Any) -> Side:
    """把各种写法的交易方向统一成 :class:`Side`。

    兼容：``Side`` 枚举、``"BUY"/"SELL"``（大小写不敏感）、简写 ``B/S/LONG/SHORT``、中文 ``买/卖/买入/卖出``。
    非法值抛出带中文提示的 ``ValueError``（由 pydantic 包装为校验错误）。
    """
    if isinstance(value, Side):
        return value
    raw = getattr(value, "value", value)
    text = str(raw).strip().upper() if raw is not None else ""
    side = _SIDE_ALIASES.get(text)
    if side is None:
        raise ValueError(f"非法的交易方向：{value!r}（仅支持 BUY / SELL）")
    return side


class FeeConfig(QDModel):
    """A 股费用与滑点配置（SPEC §7 fees，冻结默认值）。"""

    commission_rate: float = 0.00025      # 佣金费率（万 2.5），买卖双向
    min_commission: float = 5.0           # 佣金最低收费（元），仅对股票生效
    stamp_tax_rate: float = 0.0005        # 印花税率，仅卖出
    transfer_fee_rate: float = 0.00001    # 过户费率，买卖双向（沪深均收）
    reg_fee_rate: float = 0.0000541       # 规费（证管 + 经手）费率，买卖双向
    include_reg_fee_in_commission: bool = False  # 是否把规费合并进佣金展示
    slippage_bps: float = 5.0             # 滑点（基点，5bp = 0.05%），买 + / 卖 -

    @field_validator("commission_rate", "stamp_tax_rate", "transfer_fee_rate", "reg_fee_rate")
    @classmethod
    def _check_rate(cls, value: float) -> float:
        """费率必须在 [0, 1) 区间，否则给出中文错误提示。"""
        if not 0.0 <= value < 1.0:
            raise ValueError(f"费率必须在 0~1 之间（当前 {value}），请检查 config.yaml 的 fees 配置")
        return float(value)

    @field_validator("min_commission")
    @classmethod
    def _check_min_commission(cls, value: float) -> float:
        if value < 0:
            raise ValueError(f"最低佣金不能为负数（当前 {value}）")
        return float(value)

    @field_validator("slippage_bps")
    @classmethod
    def _check_slippage(cls, value: float) -> float:
        if not 0.0 <= value <= 1000.0:
            raise ValueError(f"滑点必须在 0~1000 基点之间（当前 {value}）")
        return float(value)


class FeeBreakdown(QDModel):
    """单笔成交费用明细（SPEC §6.4；每项独立四舍五入到 0.01 元后累加）。"""

    commission: float = 0.0       # 佣金
    stamp_tax: float = 0.0        # 印花税（仅卖出）
    transfer_fee: float = 0.0     # 过户费
    reg_fee: float = 0.0          # 规费
    total: float = 0.0            # 总费用
    slippage_cost: float = 0.0    # 滑点成本（非费用，回测明细用，可选）

    @property
    def total_with_slippage(self) -> float:
        """费用 + 滑点成本（回测绩效核算用，非券商对账单口径）。"""
        return round(self.total + self.slippage_cost, 2)


class Signal(QDModel):
    """策略信号（SPEC §3.3 ``signal`` 主题、§5.2 strategy_signals）。

    **信号不是订单**：只表达交易意图，尚未经过风控。
    """

    trace_id: str | None = None
    strategy_instance_id: str | None = None
    strategy_key: str | None = None       # 策略注册表 key（可选，便于日志/展示）
    symbol: str
    side: Side
    strength: float = 1.0                 # 信号强度 0~1
    price_hint: float | None = None       # 建议价格（可选）
    reason: str = ""                      # 中文信号原因（策略自述）
    ts: datetime = Field(default_factory=now_local)  # 信号生成时间（本地时区）
    meta: dict[str, Any] = Field(default_factory=dict)  # 附加信息（策略参数/指标值，审计调试用）

    @field_validator("side", mode="before")
    @classmethod
    def _normalize_side(cls, value: Any) -> Side:
        """兼容中文/简写方向写法（Dev2 策略与手工输入均可用）。"""
        return normalize_side(value)

    @field_validator("strength")
    @classmethod
    def _check_strength(cls, value: float) -> float:
        """强度归一化到 0~1；超出范围给中文提示（策略作者常见错误）。"""
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"信号强度 strength 必须在 0~1 之间（当前 {value}）")
        return float(value)

    @property
    def signal_time(self) -> datetime:
        """兼容 DB 字段名 ``signal_time``（写入 strategy_signals 时使用）。"""
        return self.ts


class OrderRequest(QDModel):
    """下单请求（OrderService.place / BrokerAdapter.submit_order 的入参）。

    设计说明：**本模型不做业务合法性校验**（数量>0、价格>0、整手、涨跌停区间、交易时段等），
    这些由 ``RiskGate.check`` 按 SPEC §8.6 固定顺序统一判定（RK011/RK017/RK018 等），
    避免同一规则在两处校验产生口径冲突。
    """

    account_id: str
    symbol: str
    side: Side
    order_type: OrderType = OrderType.LIMIT
    price: float | None = None            # 限价单必填；市价单可为 None
    quantity: int
    source: OrderSource = OrderSource.MANUAL
    strategy_instance_id: str | None = None
    client_order_id: str | None = None    # 幂等键；为空时由 OrderService 生成
    trace_id: str | None = None
    note: str | None = None               # 备注（审计用，可为空）

    @field_validator("side", mode="before")
    @classmethod
    def _normalize_side(cls, value: Any) -> Side:
        """兼容中文/简写方向写法（手工输入与策略调用均可用）。"""
        return normalize_side(value)


class Order(QDModel):
    """订单（SPEC §5.2 orders 表对应的领域对象，状态机唯一事实来源）。"""

    id: str
    client_order_id: str | None = None
    account_id: str
    symbol: str
    side: Side
    order_type: OrderType = OrderType.LIMIT
    price: float | None = None
    quantity: int
    filled_quantity: int = 0
    avg_fill_price: float | None = None
    status: OrderStatus = OrderStatus.PENDING
    source: OrderSource = OrderSource.MANUAL
    strategy_instance_id: str | None = None
    reject_reason: str | None = None      # RK/BR 码
    reject_message: str | None = None     # 中文拒单原因
    risk_checks: list[dict[str, Any]] = Field(default_factory=list)
    trace_id: str | None = None
    created_at: datetime = Field(default_factory=now_local)
    submitted_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime = Field(default_factory=now_local)

    @property
    def remaining_quantity(self) -> int:
        """未成交数量（quantity - filled_quantity，不小于 0）。"""
        return max(int(self.quantity) - int(self.filled_quantity), 0)

    @property
    def is_active(self) -> bool:
        """订单是否仍活跃（未进入终态）。"""
        return self.status in ORDER_ACTIVE_STATUSES

    @field_validator("side", mode="before")
    @classmethod
    def _normalize_side(cls, value: Any) -> Side:
        """兼容中文/简写方向写法。"""
        return normalize_side(value)


class OrderResult(QDModel):
    """下单/撤单结果（BrokerAdapter 与 OrderService 统一返回，契约 CF-4）。"""

    success: bool = False
    order_id: str | None = None
    client_order_id: str | None = None
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: int = 0
    avg_fill_price: float | None = None
    fee: FeeBreakdown | None = None
    reason_code: str | None = None        # RK/BR 码
    message: str = ""                     # 中文结果说明（UI 直接展示）
    order: Order | None = None            # 完整订单快照（可选）
    trace_id: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)  # 券商原始返回（调试用）

    @classmethod
    def from_order(cls, order: Order, *, success: bool | None = None, message: str = "") -> OrderResult:
        """由订单对象快速构造结果（便于 OrderService 复用）。"""
        if not message:
            message = "委托已成交" if order.status is OrderStatus.FILLED else "委托已提交"
        return cls(
            success=(order.status is not OrderStatus.REJECTED) if success is None else success,
            order_id=order.id,
            client_order_id=order.client_order_id,
            status=order.status,
            filled_quantity=order.filled_quantity,
            avg_fill_price=order.avg_fill_price,
            reason_code=order.reject_reason,
            message=order.reject_message or message,
            order=order,
            trace_id=order.trace_id,
        )

    @classmethod
    def failure(
        cls,
        reason_code: str,
        message: str = "",
        *,
        order_id: str | None = None,
        status: OrderStatus = OrderStatus.REJECTED,
        trace_id: str | None = None,
    ) -> OrderResult:
        """构造失败结果；``message`` 为空时自动填中文原因说明。"""
        return cls(
            success=False,
            order_id=order_id,
            status=status,
            reason_code=reason_code,
            message=message or describe_reason(reason_code),
            trace_id=trace_id,
        )


class Fill(QDModel):
    """成交回报（SPEC §5.2 trades 表对应的领域对象，PortfolioService.apply_fill 入参）。"""

    id: str | None = None
    order_id: str
    account_id: str | None = None
    symbol: str | None = None
    side: Side | None = None
    price: float
    quantity: int
    amount: float = 0.0                   # 成交金额 = 成交价 × 数量
    commission: float = 0.0
    stamp_tax: float = 0.0
    transfer_fee: float = 0.0
    reg_fee: float = 0.0
    fee_total: float = 0.0
    slippage_cost: float = 0.0
    trade_date: date | None = None        # 交易日（T+1 结算用）
    traded_at: datetime = Field(default_factory=now_local)
    source: OrderSource | None = None
    trace_id: str | None = None

    @field_validator("side", mode="before")
    @classmethod
    def _normalize_side(cls, value: Any) -> Side | None:
        """兼容中文/简写方向写法；``None`` 原样返回（成交回报可缺方向）。"""
        if value is None:
            return None
        return normalize_side(value)


class RiskCheck(QDModel):
    """单条风控规则检查记录（``RiskDecision.checks`` 列表元素，供 UI 展示“为什么被拒”）。"""

    rule: str = ""        # 规则标识，如 "single_position_pct"
    label: str = ""       # 中文规则名，如 "单票仓位上限"
    passed: bool = True
    actual: Any = None    # 实际值
    limit: Any = None     # 阈值
    message: str = ""     # 中文说明（未通过时写原因）


class RiskConfig(QDModel):
    """风控参数（SPEC §7 risk / §9.1 默认值；百分比参数以“百分数”存储，如 20 表示 20%）。"""

    enabled: bool = True
    max_position_per_symbol_pct: float = 20.0   # 单票市值占总资产上限（%）
    max_total_position_pct: float = 80.0        # 全部持仓市值占总资产上限（%）
    max_daily_loss_pct: float = 3.0             # 单日亏损熔断线（%）
    max_price_deviation_pct: float = 2.0        # 限价与最新价最大偏离（%）
    max_single_order_value_pct: float = 10.0    # 单笔委托金额占总资产上限（%）
    max_orders_per_minute: int = 10             # 每分钟最多提交订单数
    min_listed_days: int = 60                   # 上市不足 N 个交易日不交易
    min_order_amount: float = 1000.0            # 单笔最小金额（元），来源 trading.min_order_amount
    blacklist: list[str] = Field(default_factory=list)  # 用户自定义黑名单
    # 以下两项来自 trading/automation 配置，合并进风控入参，供 RK020 / 市价单判定使用
    allow_market_order: bool = False            # 是否允许市价单（trading.allow_market_order）
    automation_enabled: bool = False            # 自动化总开关（automation.enabled），自动单校验用

    @field_validator(
        "max_position_per_symbol_pct",
        "max_total_position_pct",
        "max_daily_loss_pct",
        "max_price_deviation_pct",
        "max_single_order_value_pct",
    )
    @classmethod
    def _check_percent(cls, value: float) -> float:
        """百分比参数必须在 0~100 之间。"""
        if not 0.0 <= value <= 100.0:
            raise ValueError(f"风控百分比参数必须在 0~100 之间（当前 {value}）")
        return float(value)

    @field_validator("max_orders_per_minute")
    @classmethod
    def _check_order_rate(cls, value: int) -> int:
        if value < 1:
            raise ValueError(f"每分钟下单上限必须 ≥ 1（当前 {value}）")
        return int(value)

    @field_validator("min_listed_days")
    @classmethod
    def _check_listed_days(cls, value: int) -> int:
        if value < 0:
            raise ValueError(f"最小上市天数不能为负数（当前 {value}）")
        return int(value)

    @field_validator("min_order_amount")
    @classmethod
    def _check_min_amount(cls, value: float) -> float:
        if value < 0:
            raise ValueError(f"单笔最小金额不能为负数（当前 {value}）")
        return float(value)


class RiskDecision(QDModel):
    """风控裁决结果（SPEC §6.3/§6.7 冻结字段：allow/reason_code/message/details/checks）。"""

    allow: bool
    reason_code: str | None = None
    message: str = ""                                  # 中文结论（UI 直接展示）
    details: dict[str, Any] = Field(default_factory=dict)
    checks: list[dict[str, Any]] = Field(default_factory=list)  # 每条规则的通过/未通过记录

    @property
    def suggestion(self) -> str:
        """中文建议动作（由原因码推导，可被 details["suggestion"] 覆盖）。"""
        return str(self.details.get("suggestion") or suggest_reason(self.reason_code))

    @property
    def reason(self) -> str | None:
        """兼容旧口径别名（BORROW_MAP P3 使用 ``reason``，值同 ``reason_code``）。"""
        return self.reason_code

    @classmethod
    def pass_(
        cls,
        checks: list[dict[str, Any]] | None = None,
        *,
        message: str = "风控通过",
        details: dict[str, Any] | None = None,
    ) -> RiskDecision:
        """构造“通过”裁决（``pass`` 是 Python 关键字，故命名为 ``pass_``）。"""
        return cls(allow=True, reason_code=None, message=message, details=details or {}, checks=checks or [])

    @classmethod
    def deny(
        cls,
        reason_code: str,
        message: str = "",
        *,
        details: dict[str, Any] | None = None,
        checks: list[dict[str, Any]] | None = None,
    ) -> RiskDecision:
        """构造“拒绝”裁决；``message`` 为空时自动填中文原因，``details`` 自动补建议动作。"""
        detail = dict(details or {})
        detail.setdefault("suggestion", suggest_reason(reason_code))
        return cls(
            allow=False,
            reason_code=_code_value(reason_code),
            message=message or describe_reason(reason_code),
            details=detail,
            checks=checks or [],
        )


class RiskPreview(QDModel):
    """只读风控预检结果（SPEC §6.3 ``OrderService.preview``，UI 手动交易页使用）。

    预检**不落库、不下单**；仅返回风控结论 + 费用明细 + 成交后仓位占比。
    """

    decision: RiskDecision
    price: float | None = None
    quantity: int = 0
    estimated_amount: float = 0.0              # 预计成交金额（元）
    fee: FeeBreakdown | None = None            # 费用明细
    cash_after: float = 0.0                    # 成交后可用资金
    post_trade_position_pct: float = 0.0       # 成交后该标的仓位占比（%）
    post_trade_total_position_pct: float = 0.0  # 成交后总仓位占比（%）
    available_to_buy: int = 0                  # 当前可买数量（整手口径）
    available_to_sell: int = 0                 # 当前可卖数量（T+1 口径）
    message: str = ""                          # 预检中文摘要


class MarketSnapshot(QDModel):
    """市场快照（SPEC §6.7 冻结字段；风控入参，由 OrderService/自动化组装）。"""

    trade_date: date
    is_trading_time: bool = False
    latest_prices: dict[str, float] = Field(default_factory=dict)   # symbol -> 最新价
    price_times: dict[str, datetime] = Field(default_factory=dict)  # symbol -> 行情时间（新鲜度判定）
    suspended: set[str] = Field(default_factory=set)                # 停牌标的集合
    delisting: set[str] = Field(default_factory=set)                # 退市整理标的集合
    daily_open_asset: float = 0.0           # 当日开盘总资产（熔断基线）
    realized_pnl_today: float = 0.0         # 当日已实现盈亏
    # 以下为便于风控判定的派生信息（可选，未提供时风控按默认规则计算）
    fused: bool = False                      # 是否已触发单日亏损熔断
    stale_threshold_hours: float = 24.0      # 行情新鲜度阈值（小时），对应 data.stale_threshold_hours

    def latest_price(self, symbol: str) -> float | None:
        """取标的最新价；缺失返回 ``None``（风控据此判 RK006）。"""
        return self.latest_prices.get(symbol)


class Account(QDModel):
    """账户（SPEC §5.2 accounts；MVP 单账户，结构支持多账户）。"""

    id: str
    name: str = "模拟账户"
    broker: BrokerType = BrokerType.PAPER
    base_currency: str = "CNY"
    initial_cash: float = 100000.0
    cash: float = 100000.0
    frozen_cash: float = 0.0
    total_asset: float = 100000.0
    enabled: bool = True
    updated_at: datetime = Field(default_factory=now_local)

    @property
    def available_cash(self) -> float:
        """可用资金 = 现金 - 冻结资金。"""
        return round(float(self.cash) - float(self.frozen_cash), 2)

    @property
    def position_value(self) -> float:
        """持仓市值 = 总资产 - 现金 - 冻结资金。"""
        return round(float(self.total_asset) - float(self.cash) - float(self.frozen_cash), 2)

    @property
    def total_position_pct(self) -> float:
        """总仓位占比（%）；总资产为 0 时返回 0，避免除零。"""
        if not self.total_asset:
            return 0.0
        return round(self.position_value / float(self.total_asset) * 100.0, 4)


class Position(QDModel):
    """持仓（SPEC §5.2 positions；``available_quantity`` 体现 T+1 语义）。"""

    account_id: str
    symbol: str
    name: str | None = None
    quantity: int = 0                 # 总持仓（股）
    available_quantity: int = 0       # 可卖数量（T+1）
    frozen_quantity: int = 0          # 冻结数量（挂单占用）
    today_buy_quantity: int = 0       # 当日买入数量（不可卖）
    avg_cost: float = 0.0             # 平均成本（元/股，复权口径）
    last_price: float = 0.0           # 最新价（估值用）
    market_value: float = 0.0         # 市值 = 数量 × 最新价
    updated_at: datetime = Field(default_factory=now_local)

    @property
    def is_empty(self) -> bool:
        """是否空仓。"""
        return int(self.quantity) == 0

    @property
    def t1_locked_quantity(self) -> int:
        """T+1 锁定数量 = 总持仓 - 可卖数量。"""
        return max(int(self.quantity) - int(self.available_quantity), 0)

    @property
    def unrealized_pnl(self) -> float:
        """浮动盈亏 = (最新价 - 成本价) × 数量。"""
        return round((float(self.last_price) - float(self.avg_cost)) * int(self.quantity), 2)

    @property
    def unrealized_pnl_pct(self) -> float:
        """浮动盈亏率（%）；成本价或数量为 0 时返回 0。"""
        cost = float(self.avg_cost) * int(self.quantity)
        if not cost:
            return 0.0
        return round(self.unrealized_pnl / cost * 100.0, 4)


class Bar(QDModel):
    """单根 K 线（SPEC §5.2 daily_bars；``trade_date`` 与 DB 字段一致）。

    说明：``volume`` 允许浮点以兼容各数据源（DB 落库时由数据层转为整数股）。
    """

    symbol: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0            # 成交量（股）
    amount: float = 0.0            # 成交额（元）
    adj_factor: float | None = None       # 后复权因子
    turnover_rate: float | None = None    # 换手率（%）
    is_suspended: bool = False
    prev_close: float | None = None       # 前收盘价（不复权，算涨跌停用；可选）
    source: str | None = None

    @field_validator("high", "low", "open", "close")
    @classmethod
    def _check_price(cls, value: float) -> float:
        """价格不得为负（允许 0 用于停牌占位；异常数据由数据层校验报告记录）。"""
        if value < 0:
            raise ValueError(f"K 线价格不能为负数（当前 {value}）")
        return float(value)


#: 说明：策略上下文 ``Context`` 按 Lead 裁定归 ``strategy/base.py``（SPEC §6.2 的 @dataclass），
#: 本文件不定义同名模型，避免两套口径；策略层从 ``strategy.base`` import ``Context``。
#: ``Signal`` 按 Lead 裁定保留在本文件（唯一来源），Dev2 从 ``core.models`` import。


class ProviderHealth(QDModel):
    """数据源健康检查结果（SPEC §6.1 ``DataProvider.health_check``）。"""

    name: str = ""
    status: ProviderStatus = ProviderStatus.OK
    last_error: str | None = None
    latency_ms: float | None = None
    checked_at: datetime = Field(default_factory=now_local)
    message: str = ""  # 中文状态说明（UI 状态条展示）


class JobStatus(QDModel):
    """调度任务状态（SPEC §6.5 ``SchedulerService.status()`` 列表元素）。"""

    job_key: str
    name: str = ""
    next_run_time: datetime | None = None
    last_run: datetime | None = None
    last_status: str | None = None       # SUCCESS/FAILED/SKIPPED（或 None 表示未运行过）
    last_message: str = ""
    enabled: bool = True


class JobResult(QDModel):
    """定时任务执行结果（SPEC §6.5 ``job_*`` 返回值）。"""

    job_key: str
    trace_id: str | None = None
    status: JobRunStatus = JobRunStatus.SUCCESS
    message: str = ""
    stats: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: float | None = None
    error: str | None = None             # 失败时的中文错误/堆栈摘要


# ---------------------------------------------------------------------------
# ORM 表（SQLAlchemy 2.0；SPEC §5.2 共 22 张，表名与关键字段冻结）
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    """ORM 声明基类。``core/db.py`` 使用 ``Base.metadata.create_all(engine)`` 建表。"""


class TimestampMixin:
    """通用「创建/更新时间」列（本地时间 ``Asia/Shanghai``，SPEC §5.1）。

    注意：DB 中保存的是**不带时区信息**的本地时间（``now_local_naive``），
    避免 SQLite 存储时区偏移导致跨时区/字符串比较异常。
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local_naive, comment="创建时间（本地时间 Asia/Shanghai）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=now_local_naive,
        onupdate=now_local_naive,
        comment="更新时间（本地时间 Asia/Shanghai）",
    )


# ------------------------------ A. 行情与研究域 ------------------------------


class SymbolORM(TimestampMixin, Base):
    """标的主数据（表 ``symbols``）；``board`` + ``is_st`` 决定涨跌停幅度。"""

    __tablename__ = "symbols"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True, comment="标的代码，如 600519.SH")
    code: Mapped[str] = mapped_column(String(16), nullable=False, default="", comment="6 位代码")
    name: Mapped[str] = mapped_column(String(64), nullable=False, default="", comment="中文简称")
    exchange: Mapped[str] = mapped_column(String(8), nullable=False, default=Exchange.SH.value, comment="SH/SZ/BJ")
    board: Mapped[str] = mapped_column(String(8), nullable=False, default=Board.MAIN.value, comment="MAIN/GEM/STAR/BSE")
    kind: Mapped[str] = mapped_column(String(8), nullable=False, default=SymbolKind.STOCK.value, comment="STOCK/INDEX")
    lot_size: Mapped[int] = mapped_column(Integer, nullable=False, default=100, comment="买入最小数量（股）")
    tick_size: Mapped[float] = mapped_column(Float, nullable=False, default=0.01, comment="最小报价单位（元）")
    price_limit_pct: Mapped[float | None] = mapped_column(Float, nullable=True, comment="涨跌幅限制（%，可空）")
    is_st: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否 ST/*ST")
    list_date: Mapped[date | None] = mapped_column(Date, nullable=True, comment="上市日期")
    delist_date: Mapped[date | None] = mapped_column(Date, nullable=True, comment="退市日期")
    trade_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TradeStatus.NORMAL.value, comment="NORMAL/SUSPENDED/DELISTING/DELISTED"
    )

    __table_args__ = (
        Index("ix_symbols_board", "board"),
        Index("ix_symbols_kind_trade_status", "kind", "trade_status"),
    )


class TradingCalendarORM(TimestampMixin, Base):
    """交易日历（表 ``trading_calendar``）；``prev/next`` 用于 T+1 与顺延。"""

    __tablename__ = "trading_calendar"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True, comment="交易日")
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否开市")
    prev_trade_date: Mapped[date | None] = mapped_column(Date, nullable=True, comment="上一交易日")
    next_trade_date: Mapped[date | None] = mapped_column(Date, nullable=True, comment="下一交易日")


class DailyBarORM(TimestampMixin, Base):
    """日线行情（表 ``daily_bars``）；``(symbol, trade_date)`` 唯一保证重复拉取不重复写。"""

    __tablename__ = "daily_bars"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True, comment="标的代码")
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True, comment="交易日")
    open: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="开盘价（不复权）")
    high: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="最高价")
    low: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="最低价")
    close: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="收盘价")
    volume: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="成交量（股）")
    amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="成交额（元）")
    adj_factor: Mapped[float | None] = mapped_column(Float, nullable=True, comment="后复权因子")
    turnover_rate: Mapped[float | None] = mapped_column(Float, nullable=True, comment="换手率（%）")
    is_suspended: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否停牌")
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="", comment="数据来源")

    __table_args__ = (Index("ix_daily_bars_trade_date", "trade_date"),)


class FundamentalORM(TimestampMixin, Base):
    """基本面快照（表 ``fundamentals``），多因子策略使用。"""

    __tablename__ = "fundamentals"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True, comment="标的代码")
    report_date: Mapped[date] = mapped_column(Date, primary_key=True, comment="报告期")
    ann_date: Mapped[date | None] = mapped_column(Date, nullable=True, comment="公告日期")
    pe_ttm: Mapped[float | None] = mapped_column(Float, nullable=True, comment="市盈率 TTM")
    pb: Mapped[float | None] = mapped_column(Float, nullable=True, comment="市净率")
    ps_ttm: Mapped[float | None] = mapped_column(Float, nullable=True, comment="市销率 TTM")
    roe: Mapped[float | None] = mapped_column(Float, nullable=True, comment="净资产收益率（%）")
    total_mv: Mapped[float | None] = mapped_column(Float, nullable=True, comment="总市值（元）")
    revenue_yoy: Mapped[float | None] = mapped_column(Float, nullable=True, comment="营收同比（%）")
    profit_yoy: Mapped[float | None] = mapped_column(Float, nullable=True, comment="净利润同比（%）")
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="", comment="数据来源")


class AdjustEventORM(Base):
    """除权除息事件（表 ``adjust_events``）；``(symbol, ex_date, event_type)`` 唯一。"""

    __tablename__ = "adjust_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="自增主键")
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True, comment="标的代码")
    ex_date: Mapped[date] = mapped_column(Date, nullable=False, comment="除权除息日")
    event_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AdjustEventType.DIVIDEND.value, comment="DIVIDEND/SPLIT/RIGHTS"
    )
    cash_per_share: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="每股现金分红（元）")
    stock_ratio: Mapped[float | None] = mapped_column(Float, nullable=True, comment="每股送转比例")
    record_date: Mapped[date | None] = mapped_column(Date, nullable=True, comment="股权登记日")
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="", comment="数据来源")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local_naive, comment="创建时间（本地时间）"
    )

    __table_args__ = (
        UniqueConstraint("symbol", "ex_date", "event_type", name="uq_adjust_events_symbol_ex_date_type"),
        Index("ix_adjust_events_ex_date", "ex_date"),
    )


class DataQualityIssueORM(Base):
    """数据质量校验问题（表 ``data_quality_issues``，F1 验收用）。"""

    __tablename__ = "data_quality_issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="自增主键")
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True, comment="校验批次 ID")
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, default="", comment="标的代码")
    trade_date: Mapped[date | None] = mapped_column(Date, nullable=True, comment="问题交易日（可空）")
    issue_code: Mapped[str] = mapped_column(String(16), nullable=False, comment="DQ001–DQ007")
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default=IssueSeverity.ERROR.value, comment="ERROR/WARN")
    detail: Mapped[str | None] = mapped_column(Text, nullable=True, comment="中文问题说明与建议")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local_naive, comment="创建时间（本地时间）"
    )

    __table_args__ = (Index("ix_data_quality_issues_symbol_trade_date", "symbol", "trade_date"),)


# ------------------------------ B. 策略与回测域 ------------------------------


class StrategyInstanceORM(TimestampMixin, Base):
    """策略实例（表 ``strategy_instances``，UI 策略页管理、自动化读取）。"""

    __tablename__ = "strategy_instances"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="实例 ID（UUID）")
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, comment="实例名称（唯一）")
    strategy_key: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True, comment="策略注册表 key，如 ma_cross"
    )
    params_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}", comment="策略参数 JSON")
    universe_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]", comment="标的池 JSON 数组")
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default=StrategyMode.OFF.value, comment="OFF/PAPER/AUTO")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否启用")
    last_signal_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="最近信号时间")


class StrategySignalORM(Base):
    """策略信号留痕（表 ``strategy_signals``，审计 + 策略页预览）。"""

    __tablename__ = "strategy_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="自增主键")
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="追踪 ID")
    strategy_instance_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="策略实例 ID")
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, comment="标的代码")
    side: Mapped[str] = mapped_column(String(8), nullable=False, comment="BUY/SELL")
    strength: Mapped[float] = mapped_column(Float, nullable=False, default=1.0, comment="信号强度 0~1")
    price_hint: Mapped[float | None] = mapped_column(Float, nullable=True, comment="建议价格")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True, comment="中文信号原因")
    signal_time: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local_naive, comment="信号生成时间（本地时间）"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SignalStatus.GENERATED.value, comment="GENERATED/RISK_BLOCKED/ORDERED/IGNORED"
    )
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="关联订单 ID")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local_naive, comment="创建时间（本地时间）"
    )

    __table_args__ = (
        Index("ix_strategy_signals_instance_time", "strategy_instance_id", "signal_time"),
        Index("ix_strategy_signals_trace_id", "trace_id"),
        Index("ix_strategy_signals_symbol_time", "symbol", "signal_time"),
    )


class BacktestRunORM(Base):
    """回测记录（表 ``backtest_runs``，结果可对比的数据基础）。"""

    __tablename__ = "backtest_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="回测 ID（UUID）")
    strategy_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True, comment="策略 key")
    params_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}", comment="策略参数 JSON")
    universe_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]", comment="标的池 JSON")
    start_date: Mapped[date] = mapped_column(Date, nullable=False, comment="回测开始日")
    end_date: Mapped[date] = mapped_column(Date, nullable=False, comment="回测结束日")
    initial_cash: Mapped[float] = mapped_column(Float, nullable=False, default=100000.0, comment="初始资金（元）")
    fee_config_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="费用配置 JSON")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=BacktestStatus.RUNNING.value, comment="RUNNING/DONE/FAILED"
    )
    metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="绩效指标 JSON")
    warnings_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="RQ_WARN_* 警告 JSON 数组")
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True, comment="失败原因（中文）")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local_naive, comment="创建时间（本地时间）"
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="完成时间")

    __table_args__ = (Index("ix_backtest_runs_status_created", "status", "created_at"),)


class BacktestEquityORM(Base):
    """回测权益曲线（表 ``backtest_equity``）；``(run_id, trade_date)`` 主键。"""

    __tablename__ = "backtest_equity"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="回测 ID")
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True, comment="交易日")
    equity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="总权益（元）")
    cash: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="现金（元）")
    position_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="持仓市值（元）")
    benchmark_close: Mapped[float | None] = mapped_column(Float, nullable=True, comment="基准收盘价")
    drawdown: Mapped[float | None] = mapped_column(Float, nullable=True, comment="回撤（%）")


class BacktestTradeORM(Base):
    """回测成交明细（表 ``backtest_trades``）。"""

    __tablename__ = "backtest_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="自增主键")
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="回测 ID")
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, comment="成交交易日")
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, comment="标的代码")
    side: Mapped[str] = mapped_column(String(8), nullable=False, comment="BUY/SELL")
    price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="成交价")
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="成交数量（股）")
    amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="成交金额（元）")
    commission: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="佣金")
    stamp_tax: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="印花税")
    transfer_fee: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="过户费")
    reg_fee: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="规费")
    slippage_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="滑点成本")
    fee_total: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="费用合计")

    __table_args__ = (Index("ix_backtest_trades_run_id", "run_id", "trade_date"),)


# ------------------------------ C. 交易与执行域 ------------------------------


class AccountORM(TimestampMixin, Base):
    """模拟账户（表 ``accounts``）；MVP 单账户，结构支持多账户。"""

    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="账户 ID（UUID）")
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, comment="账户名称（唯一）")
    broker: Mapped[str] = mapped_column(String(16), nullable=False, default=BrokerType.PAPER.value, comment="PAPER/CCXT/QMT")
    base_currency: Mapped[str] = mapped_column(String(8), nullable=False, default="CNY", comment="基础币种")
    initial_cash: Mapped[float] = mapped_column(Float, nullable=False, default=100000.0, comment="初始资金（元）")
    cash: Mapped[float] = mapped_column(Float, nullable=False, default=100000.0, comment="现金（元）")
    frozen_cash: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="冻结资金（元）")
    total_asset: Mapped[float] = mapped_column(Float, nullable=False, default=100000.0, comment="总资产（元）")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否启用")


class PositionORM(TimestampMixin, Base):
    """持仓（表 ``positions``）；``available_quantity`` 体现 T+1。"""

    __tablename__ = "positions"

    account_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="账户 ID")
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True, comment="标的代码")
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="总持仓（股）")
    available_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="可卖数量（T+1）")
    frozen_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="冻结数量（挂单占用）")
    today_buy_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="当日买入数量")
    avg_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="平均成本（元/股）")
    last_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="最新价（估值用）")
    market_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="市值（元）")


class OrderORM(Base):
    """订单主表（表 ``orders``）；状态机唯一事实来源。"""

    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="订单 ID（UUID）")
    client_order_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, comment="幂等键（唯一；手动单可由 UI 生成）"
    )
    account_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="账户 ID")
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, comment="标的代码")
    side: Mapped[str] = mapped_column(String(8), nullable=False, comment="BUY/SELL")
    order_type: Mapped[str] = mapped_column(String(8), nullable=False, default=OrderType.LIMIT.value, comment="LIMIT/MARKET")
    price: Mapped[float | None] = mapped_column(Float, nullable=True, comment="委托价格（市价单可空）")
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="委托数量（股）")
    filled_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="已成交数量（股）")
    avg_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True, comment="成交均价")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=OrderStatus.PENDING.value,
        comment="PENDING/SUBMITTED/PARTIALLY_FILLED/FILLED/CANCELLED/REJECTED/EXPIRED",
    )
    source: Mapped[str] = mapped_column(String(8), nullable=False, default=OrderSource.MANUAL.value, comment="MANUAL/AUTO")
    strategy_instance_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="来源策略实例 ID")
    reject_reason: Mapped[str | None] = mapped_column(String(16), nullable=True, comment="拒绝原因码（RK/BR）")
    reject_message: Mapped[str | None] = mapped_column(Text, nullable=True, comment="中文拒单原因")
    risk_checks_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="风控 checks 明细 JSON")
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="追踪 ID")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local_naive, comment="创建时间（本地时间）"
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="提交时间")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="终态时间")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local_naive, onupdate=now_local_naive, comment="更新时间（本地时间）"
    )

    __table_args__ = (
        Index("ix_orders_account_status_created", "account_id", "status", "created_at"),
        Index("ix_orders_trace_id", "trace_id"),
        Index("ix_orders_symbol_created", "symbol", "created_at"),
    )


class TradeORM(Base):
    """成交回报（表 ``trades``）。"""

    __tablename__ = "trades"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="成交 ID（UUID）")
    order_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="订单 ID")
    account_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="账户 ID")
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, comment="标的代码")
    side: Mapped[str] = mapped_column(String(8), nullable=False, comment="BUY/SELL")
    price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="成交价")
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="成交数量（股）")
    amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="成交金额（元）")
    commission: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="佣金")
    stamp_tax: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="印花税")
    transfer_fee: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="过户费")
    reg_fee: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="规费")
    fee_total: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="费用合计")
    traded_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local_naive, comment="成交时间（本地时间）"
    )
    source: Mapped[str] = mapped_column(String(8), nullable=False, default=OrderSource.MANUAL.value, comment="MANUAL/AUTO")

    __table_args__ = (
        Index("ix_trades_order_id", "order_id"),
        Index("ix_trades_account_traded_at", "account_id", "traded_at"),
    )


class CashFlowORM(Base):
    """资金流水（表 ``cash_flows``，对账基础）。"""

    __tablename__ = "cash_flows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="自增主键")
    account_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="账户 ID")
    flow_type: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="DEPOSIT/WITHDRAW/BUY/SELL/DIVIDEND/FEE/OTHER"
    )
    amount: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="金额（元，正负表示增减）")
    balance_after: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="变动后现金余额（元）")
    ref_type: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="关联单据类型，如 order/trade")
    ref_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="关联单据 ID")
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local_naive, comment="发生时间（本地时间）"
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True, comment="备注（中文）")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local_naive, comment="创建时间（本地时间）"
    )

    __table_args__ = (Index("ix_cash_flows_account_occurred", "account_id", "occurred_at"),)


class EquityCurveORM(TimestampMixin, Base):
    """实盘/模拟资金曲线（表 ``equity_curve``）；每账户每日一条，重复写覆盖（幂等）。"""

    __tablename__ = "equity_curve"

    account_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="账户 ID")
    trade_date: Mapped[date] = mapped_column(Date, primary_key=True, comment="交易日")
    total_asset: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="总资产（元）")
    cash: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="现金（元）")
    position_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="持仓市值（元）")
    daily_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="当日盈亏（元）")
    daily_return: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="当日收益率（%）")


# ------------------------------ D. 运维与配置域 ------------------------------


class RiskConfigORM(Base):
    """风控参数 DB 覆盖（表 ``risk_config``，单行 id=1，优先级高于 config.yaml）。"""

    __tablename__ = "risk_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1, comment="固定为 1（单行）")
    params_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}", comment="风控参数 JSON")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local_naive, onupdate=now_local_naive, comment="更新时间（本地时间）"
    )
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system", comment="修改人/来源")

    __table_args__ = (CheckConstraint("id = 1", name="ck_risk_config_single_row"),)


class AppSettingORM(Base):
    """其它 UI 可改设置（表 ``app_settings``，键值对）。"""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True, comment="设置键")
    value_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="设置值 JSON")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local_naive, onupdate=now_local_naive, comment="更新时间（本地时间）"
    )


class AuditLogORM(Base):
    """审计流水（表 ``audit_logs``，只增不改不删；保留 1 年）。"""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="自增主键")
    ts: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local_naive, index=True, comment="发生时间（本地时间）"
    )
    actor: Mapped[str] = mapped_column(String(16), nullable=False, default=AuditActor.SYSTEM.value, comment="manual/scheduler/system/auto")
    action: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="动作名，如 order.place / risk.config.update / job.manual_trigger"
    )
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="对象类型")
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="对象 ID")
    before_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="变更前快照 JSON")
    after_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="变更后快照 JSON")
    result: Mapped[str] = mapped_column(String(16), nullable=False, default=AuditResult.SUCCESS.value, comment="SUCCESS/FAILED/DENIED")
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="追踪 ID")

    __table_args__ = (
        Index("ix_audit_logs_target", "target_type", "target_id"),
        Index("ix_audit_logs_trace_id", "trace_id"),
    )


class JobRunORM(Base):
    """调度执行记录（表 ``job_runs``）。"""

    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="自增主键")
    job_key: Mapped[str] = mapped_column(String(64), nullable=False, comment="任务 key，如 prepare/scan/summarize")
    job_name: Mapped[str] = mapped_column(String(128), nullable=False, default="", comment="任务中文名")
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="追踪 ID")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="计划触发时间")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="实际开始时间")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="结束时间")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=JobRunStatus.SUCCESS.value, comment="SUCCESS/FAILED/SKIPPED"
    )
    message: Mapped[str | None] = mapped_column(Text, nullable=True, comment="中文结果/失败原因")
    stats_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="统计信息 JSON")

    __table_args__ = (
        Index("ix_job_runs_job_key_started", "job_key", "started_at"),
        Index("ix_job_runs_status", "status"),
    )


class NotificationORM(Base):
    """通知发送记录（表 ``notifications``）。"""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="自增主键")
    channel: Mapped[str] = mapped_column(String(16), nullable=False, comment="DESKTOP/EMAIL/WEBHOOK")
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, default="", comment="通知事件类型")
    title: Mapped[str] = mapped_column(String(256), nullable=False, default="", comment="标题")
    body: Mapped[str | None] = mapped_column(Text, nullable=True, comment="正文")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=NotificationStatus.SENT.value, comment="SENT/FAILED/DISABLED"
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True, comment="失败原因（中文）")
    sent_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local_naive, comment="发送时间（本地时间）"
    )

    __table_args__ = (
        Index("ix_notifications_channel_sent", "channel", "sent_at"),
        Index("ix_notifications_event_type", "event_type"),
    )


# ---------------------------------------------------------------------------
# Phase 2 扩展表（SPEC §5.4，CF-7 扩展：扩展新增、不改已有 22 表；schema v2）
# ---------------------------------------------------------------------------


class RecommendKind(str, Enum):
    """推荐类型（SPEC §5.4 recommend_runs.kind）：远期/近期/异动雷达。"""

    FAR = "FAR"
    NEAR = "NEAR"
    RADAR = "RADAR"


class RecommendStatus(str, Enum):
    """推荐批次状态（SPEC §5.4 recommend_runs.status）。"""

    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


class ResearchKind(str, Enum):
    """策略研究类型（SPEC §5.4 research_runs.kind）。"""

    ML_RANKING = "ML_RANKING"
    WALK_FORWARD = "WALK_FORWARD"


class WatchlistSource(str, Enum):
    """自选池条目来源（SPEC §5.4 watchlist_items.source）。"""

    MANUAL = "MANUAL"
    IMPORT = "IMPORT"
    RECOMMEND = "RECOMMEND"


class WatchlistGroupORM(TimestampMixin, Base):
    """自选池分组（表 ``watchlist_groups``，SPEC §5.4；默认：全部/远期/近期/手动）。"""

    __tablename__ = "watchlist_groups"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, comment="分组 ID（slug，如 long_term）")
    name: Mapped[str] = mapped_column(String(20), nullable=False, unique=True, comment="分组名（≤20 字，唯一）")
    description: Mapped[str] = mapped_column(String(256), nullable=False, default="", comment="分组说明")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="排序（升序）")


class WatchlistItemORM(TimestampMixin, Base):
    """自选池条目（表 ``watchlist_items``，SPEC §5.4）。

    一个标的可属多个分组（``UNIQUE(group_id, symbol)`` 允许多行）。
    注意：本表为 Phase 2 正式结构；早期 Dev1 独立表（symbol 主键）由 ``core/db.py``
    schema v2 迁移自动转换为本结构。
    """

    __tablename__ = "watchlist_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="自增主键")
    group_id: Mapped[str] = mapped_column(String(32), nullable=False, default="default", comment="所属分组 ID")
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, comment="标的代码（6 位 + 市场后缀）")
    note: Mapped[str] = mapped_column(String(256), nullable=False, default="", comment="备注")
    tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]", comment="标签 JSON 数组")
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default=WatchlistSource.MANUAL.value, comment="MANUAL/IMPORT/RECOMMEND"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否启用")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="组内排序")

    __table_args__ = (
        UniqueConstraint("group_id", "symbol", name="uq_watchlist_items_group_symbol"),
        Index("ix_watchlist_items_symbol", "symbol"),
        Index("ix_watchlist_items_group_order", "group_id", "sort_order"),
    )


class RecommendRunORM(Base):
    """推荐/雷达生成批次（表 ``recommend_runs``，SPEC §5.4；可复现：方法/权重/数据截止）。"""

    __tablename__ = "recommend_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="批次 ID（UUID）")
    kind: Mapped[str] = mapped_column(String(8), nullable=False, default=RecommendKind.FAR.value, comment="FAR/NEAR/RADAR")
    as_of: Mapped[date] = mapped_column(Date, nullable=False, comment="生成基准日")
    method: Mapped[str] = mapped_column(String(64), nullable=False, default="", comment="方法/版本标识")
    params_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}", comment="参数 JSON")
    universe_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]", comment="标的池 JSON")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=RecommendStatus.RUNNING.value, comment="RUNNING/DONE/FAILED"
    )
    metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="策略级回测指标 JSON")
    oos_metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="样本外指标 JSON")
    warnings_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]", comment="警告 JSON（RQ_WARN_*）")
    data_cutoff: Mapped[date | None] = mapped_column(Date, nullable=True, comment="数据截止日（禁止用之后数据）")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local_naive, comment="创建时间（本地时间）"
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="完成时间")

    __table_args__ = (
        Index("ix_recommend_runs_kind_as_of", "kind", "as_of"),
        Index("ix_recommend_runs_status_created", "status", "created_at"),
    )


class RecommendItemORM(Base):
    """单条推荐/异动事件（表 ``recommend_items``，SPEC §5.4；含可解释性与样本外字段）。"""

    __tablename__ = "recommend_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment="自增主键")
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="所属批次 ID")
    rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="排名（升序）")
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, comment="标的代码")
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="综合得分")
    reason_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]", comment="可解释理由 JSON（≥2 条）")
    factor_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}", comment="因子拆解 JSON")
    suggested_hold_days: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="建议持有天数")
    suggested_position_pct: Mapped[float | None] = mapped_column(Float, nullable=True, comment="建议仓位（%）")
    risk_flags: Mapped[str] = mapped_column(Text, nullable=False, default="[]", comment="风险提示 JSON")
    backtest_metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="策略级回测指标 JSON")
    oos_metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="样本外指标 JSON")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local_naive, comment="创建时间（本地时间）"
    )

    __table_args__ = (Index("ix_recommend_items_run_rank", "run_id", "rank"),)


class ResearchRunORM(Base):
    """策略研究记录（表 ``research_runs``，SPEC §5.4；样本内/外、折数、种子、依赖版本可复现）。"""

    __tablename__ = "research_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="研究 ID（UUID）")
    strategy_key: Mapped[str] = mapped_column(String(64), nullable=False, comment="策略注册表 key")
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ResearchKind.WALK_FORWARD.value, comment="ML_RANKING/WALK_FORWARD"
    )
    data_start: Mapped[date | None] = mapped_column(Date, nullable=True, comment="数据开始日")
    data_end: Mapped[date | None] = mapped_column(Date, nullable=True, comment="数据结束日")
    folds_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]", comment="折配置 JSON")
    params_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}", comment="参数 JSON")
    seed: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="随机种子")
    deps_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}", comment="依赖版本 JSON")
    is_metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="样本内指标 JSON")
    oos_metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="样本外指标 JSON")
    robustness_json: Mapped[str | None] = mapped_column(Text, nullable=True, comment="参数稳健性 JSON")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="RUNNING", comment="RUNNING/DONE/FAILED"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=now_local_naive, comment="创建时间（本地时间）"
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="完成时间")

    __table_args__ = (Index("ix_research_runs_strategy_created", "strategy_key", "created_at"),)


class MarketSnapshotORM(TimestampMixin, Base):
    """全市场日快照（表 ``market_snapshot``，R6-P0 新增扩展表；供异动雷达/名称映射/推荐使用）。

    唯一键 ``(trade_date, symbol)``（同日重复采集幂等覆盖）；``name`` 用于推荐结果/UI 名称映射。
    """

    __tablename__ = "market_snapshot"

    trade_date: Mapped[date] = mapped_column(Date, primary_key=True, comment="快照交易日")
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True, comment="标的代码（600519.SH）")
    name: Mapped[str] = mapped_column(String(64), nullable=False, default="", comment="中文名称")
    price: Mapped[float | None] = mapped_column(Float, nullable=True, comment="最新价")
    pct_change: Mapped[float | None] = mapped_column(Float, nullable=True, comment="涨跌幅（%）")
    volume: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="成交量（股）")
    amount: Mapped[float | None] = mapped_column(Float, nullable=True, comment="成交额（元）")
    open: Mapped[float | None] = mapped_column(Float, nullable=True, comment="今开")
    high: Mapped[float | None] = mapped_column(Float, nullable=True, comment="最高")
    low: Mapped[float | None] = mapped_column(Float, nullable=True, comment="最低")
    prev_close: Mapped[float | None] = mapped_column(Float, nullable=True, comment="昨收")
    snapshot_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="快照时间（本地时间）")
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="sina", comment="数据源（sina/akshare）")

    __table_args__ = (
        Index("ix_market_snapshot_symbol", "symbol"),
        Index("ix_market_snapshot_trade_date", "trade_date"),
    )


#: SPEC §5.2 冻结的 22 张表名（建表/自检/迁移按此清单核对）
TABLE_NAMES: tuple[str, ...] = (
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
)

#: 22 张表对应的 ORM 类（顺序同 ``TABLE_NAMES``）
ALL_ORM_MODELS: tuple[type[Base], ...] = (
    SymbolORM,
    TradingCalendarORM,
    DailyBarORM,
    FundamentalORM,
    AdjustEventORM,
    DataQualityIssueORM,
    StrategyInstanceORM,
    StrategySignalORM,
    BacktestRunORM,
    BacktestEquityORM,
    BacktestTradeORM,
    AccountORM,
    PositionORM,
    OrderORM,
    TradeORM,
    CashFlowORM,
    EquityCurveORM,
    RiskConfigORM,
    AppSettingORM,
    AuditLogORM,
    JobRunORM,
    NotificationORM,
)


#: Phase 2 扩展表（SPEC §5.4，5 张）——CF-7 按“扩展新增、不改已有 22 表”处理
PHASE2_TABLE_NAMES: tuple[str, ...] = (
    "watchlist_groups",
    "watchlist_items",
    "recommend_runs",
    "recommend_items",
    "research_runs",
)

#: 全市场快照表名（R6-P0 新增第 6 张扩展表，供异动雷达/名称映射）
MARKET_SNAPSHOT_TABLE_NAME = "market_snapshot"

#: 全部扩展表（Phase 2 5 张 + 全市场快照 1 张）
EXTENSION_TABLE_NAMES: tuple[str, ...] = PHASE2_TABLE_NAMES + (MARKET_SNAPSHOT_TABLE_NAME,)

#: 全部表名 = 冻结 22 张核心表 + 扩展表（当前 28 张）
ALL_TABLE_NAMES: tuple[str, ...] = TABLE_NAMES + EXTENSION_TABLE_NAMES

#: 扩展表对应 ORM 类（顺序同 ``EXTENSION_TABLE_NAMES``）
EXTENSION_ORM_MODELS: tuple[type[Base], ...] = (
    WatchlistGroupORM,
    WatchlistItemORM,
    RecommendRunORM,
    RecommendItemORM,
    ResearchRunORM,
    MarketSnapshotORM,
)

#: 全部 ORM 类（核心 22 + 扩展）
ALL_TABLE_MODELS: tuple[type[Base], ...] = ALL_ORM_MODELS + EXTENSION_ORM_MODELS


def orm_model_for_table(table_name: str) -> type[Base] | None:
    """按表名返回对应 ORM 类（核心 + 扩展）；未知表名返回 ``None``（供 db.py 自检/调试）。"""
    for model in ALL_TABLE_MODELS:
        if model.__tablename__ == table_name:
            return model
    return None


__all__: list[str] = [
    # 时区工具
    "LOCAL_TZ",
    "now_local",
    "now_local_naive",
    "today_local",
    # 枚举
    "Side",
    "OrderType",
    "OrderStatus",
    "OrderSource",
    "BrokerType",
    "SymbolKind",
    "Exchange",
    "Board",
    "TradeStatus",
    "AdjustEventType",
    "IssueSeverity",
    "StrategyMode",
    "SignalStatus",
    "BacktestStatus",
    "CashFlowType",
    "AuditActor",
    "AuditResult",
    "JobRunStatus",
    "NotificationChannel",
    "NotificationStatus",
    "ProviderStatus",
    "RiskReasonCode",
    "BrokerErrorCode",
    "DataQualityCode",
    # Phase 2 枚举（SPEC §5.4）
    "RecommendKind",
    "RecommendStatus",
    "ResearchKind",
    "WatchlistSource",
    # 常量与工具
    "ORDER_ACTIVE_STATUSES",
    "ORDER_CANCELLABLE_STATUSES",
    "RQ_WARN_NO_ADJ",
    "RQ_WARN_ST_APPROX",
    "RQ_WARN_SHORT_SAMPLE",
    "RQ_WARN_FEW_TRADES",
    "RQ_WARN_HIGH_SUSPEND",
    "RISK_REASON_MESSAGES",
    "BROKER_ERROR_MESSAGES",
    "DATA_QUALITY_MESSAGES",
    "BACKTEST_WARNING_MESSAGES",
    "ALL_REASON_CODE_MESSAGES",
    "describe_reason",
    "suggest_reason",
    "build_client_order_id",
    "normalize_side",
    # pydantic 领域模型
    "QDModel",
    "FeeConfig",
    "FeeBreakdown",
    "Signal",
    "OrderRequest",
    "Order",
    "OrderResult",
    "Fill",
    "RiskCheck",
    "RiskConfig",
    "RiskDecision",
    "RiskPreview",
    "MarketSnapshot",
    "Account",
    "Position",
    "Bar",
    "ProviderHealth",
    "JobStatus",
    "JobResult",
    # ORM
    "Base",
    "TimestampMixin",
    "SymbolORM",
    "TradingCalendarORM",
    "DailyBarORM",
    "FundamentalORM",
    "AdjustEventORM",
    "DataQualityIssueORM",
    "StrategyInstanceORM",
    "StrategySignalORM",
    "BacktestRunORM",
    "BacktestEquityORM",
    "BacktestTradeORM",
    "AccountORM",
    "PositionORM",
    "OrderORM",
    "TradeORM",
    "CashFlowORM",
    "EquityCurveORM",
    "RiskConfigORM",
    "AppSettingORM",
    "AuditLogORM",
    "JobRunORM",
    "NotificationORM",
    # Phase 2 扩展 ORM（SPEC §5.4 + 全市场快照）
    "WatchlistGroupORM",
    "WatchlistItemORM",
    "RecommendRunORM",
    "RecommendItemORM",
    "ResearchRunORM",
    "MarketSnapshotORM",
    "TABLE_NAMES",
    "PHASE2_TABLE_NAMES",
    "MARKET_SNAPSHOT_TABLE_NAME",
    "EXTENSION_TABLE_NAMES",
    "ALL_TABLE_NAMES",
    "ALL_ORM_MODELS",
    "EXTENSION_ORM_MODELS",
    "ALL_TABLE_MODELS",
    "orm_model_for_table",
]

# ---------------------------------------------------------------------------
# 自检（不依赖 tests/ 目录；可直接 python core/models.py 运行）
# ---------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    # 1) 枚举值核对（SPEC §5/§9.2）
    assert [s.value for s in Side] == ["BUY", "SELL"]
    assert [s.value for s in OrderStatus] == [
        "PENDING", "SUBMITTED", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "REJECTED", "EXPIRED",
    ]
    assert [s.value for s in BrokerType] == ["PAPER", "CCXT", "QMT"]
    assert [s.value for s in StrategyMode] == ["OFF", "PAPER", "AUTO"]
    assert [s.value for s in SignalStatus] == ["GENERATED", "RISK_BLOCKED", "ORDERED", "IGNORED"]
    assert [s.value for s in CashFlowType] == [
        "DEPOSIT", "WITHDRAW", "BUY", "SELL", "DIVIDEND", "FEE", "OTHER",
    ]
    assert [s.value for s in JobRunStatus] == ["SUCCESS", "FAILED", "SKIPPED"]
    assert len(list(RiskReasonCode)) == 20, "RK001–RK020 必须恰好 20 个"
    assert set(RISK_REASON_MESSAGES) == {c.value for c in RiskReasonCode}

    # 2) 必填校验 / 默认值
    sig = Signal(symbol="600519.SH", side="BUY")
    assert sig.side is Side.BUY and sig.ts.tzinfo is not None
    sig_cn = Signal(symbol="600519.SH", side="买")  # 中文方向别名归一化
    assert sig_cn.side is Side.BUY
    assert normalize_side("b") is Side.BUY and normalize_side(Side.SELL) is Side.SELL
    sig_json = sig.to_dict()  # JSON 安全（枚举/时间转字符串）
    assert sig_json["side"] == "BUY" and isinstance(sig_json["ts"], str), sig_json
    assert "meta" in sig_json and sig_json["meta"] == {}
    req = OrderRequest(account_id="acc-1", symbol="600519.SH", side="BUY", price=10.0, quantity=100)
    assert req.order_type is OrderType.LIMIT and req.source is OrderSource.MANUAL
    order = Order(id="o1", account_id="acc-1", symbol="600519.SH", side=Side.BUY, quantity=100)
    assert order.remaining_quantity == 100 and order.is_active
    order.status = OrderStatus.FILLED
    assert order.status.is_terminal and not order.status.can_cancel

    # 3) 风控裁决辅助方法（枚举/字符串原因码都应生成规范中文消息）
    for code in (RiskReasonCode.RK014.value, RiskReasonCode.RK014):
        deny = RiskDecision.deny(code)
        assert not deny.allow and "可用资金不足" in deny.message and deny.suggestion
        assert deny.reason_code == "RK014", deny.reason_code
    assert "单票仓位超限（RK007）" == describe_reason(RiskReasonCode.RK007)
    assert suggest_reason("RK999") == "" and "未知原因码" in describe_reason("RK999")
    ok = RiskDecision.pass_()
    assert ok.allow and ok.reason_code is None

    # 4) 非法输入给中文错误
    try:
        RiskConfig(max_total_position_pct=150)
    except Exception as exc:  # ValidationError
        assert "0~100" in str(exc), str(exc)
    else:
        raise AssertionError("非法百分比未被拦截")
    try:
        Signal(symbol="600519.SH", side="BUY", strength=1.5)
    except Exception as exc:
        assert "0~1" in str(exc), str(exc)
    else:
        raise AssertionError("非法信号强度未被拦截")
    try:
        OrderRequest(symbol="600519.SH", side="BUY", quantity=100)  # 缺 account_id
    except Exception:
        pass
    else:
        raise AssertionError("缺少必填字段未被拦截")

    # 5) 账户/持仓派生属性与幂等键
    acc = Account(id="acc-1", cash=60000.0, total_asset=100000.0)
    assert acc.available_cash == 60000.0 and acc.position_value == 40000.0
    assert acc.total_position_pct == 40.0
    pos = Position(account_id="acc-1", symbol="600519.SH", quantity=100, available_quantity=0,
                   avg_cost=10.0, last_price=11.0)
    assert pos.unrealized_pnl == 100.0 and pos.t1_locked_quantity == 100
    cid = build_client_order_id("s1", "600519.SH", "2026-10-26", "BUY")
    assert cid == build_client_order_id("s1", "600519.SH", date(2026, 10, 26), Side.BUY)
    assert len(cid) == 40

    # 6) ORM 建表自检（内存 SQLite，不触碰 runtime 数据库）
    from sqlalchemy import create_engine, inspect, select
    from sqlalchemy.orm import Session

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert tables == set(ALL_TABLE_NAMES), f"表数量/名称不符：{sorted(tables)}"
    assert len(tables) == 28, f"22 张核心表 + 6 张扩展表 = 28，实际 {len(tables)}"
    assert len(TABLE_NAMES) == 22 and set(TABLE_NAMES) <= tables, "核心 22 表必须完整（CF-7 冻结）"
    assert len(EXTENSION_TABLE_NAMES) == 6 and set(EXTENSION_TABLE_NAMES) <= tables
    assert len(ALL_ORM_MODELS) == 22 and len(EXTENSION_ORM_MODELS) == 6
    assert len(ALL_TABLE_MODELS) == 28

    # 主键 / 唯一约束 / 索引抽查（CF-7）
    assert inspector.get_pk_constraint("orders")["constrained_columns"] == ["id"]
    assert inspector.get_pk_constraint("daily_bars")["constrained_columns"] == ["symbol", "trade_date"]
    assert inspector.get_pk_constraint("positions")["constrained_columns"] == ["account_id", "symbol"]
    assert inspector.get_pk_constraint("equity_curve")["constrained_columns"] == ["account_id", "trade_date"]
    assert any(
        u["name"] == "uq_adjust_events_symbol_ex_date_type"
        for u in inspector.get_unique_constraints("adjust_events")
    ), "adjust_events 唯一约束缺失"
    order_indexes = {ix["name"] for ix in inspector.get_indexes("orders")}
    assert {"ix_orders_account_status_created", "ix_orders_trace_id"} <= order_indexes
    assert "ix_daily_bars_trade_date" in {ix["name"] for ix in inspector.get_indexes("daily_bars")}
    assert "ix_audit_logs_ts" in {ix["name"] for ix in inspector.get_indexes("audit_logs")}
    # Phase 2 扩展表 / 全市场快照（SPEC §5.2 冻结结构不变，扩展按 §5.4 + R6-P0）
    assert inspector.get_pk_constraint("watchlist_items")["constrained_columns"] == ["id"]
    assert any(
        u["name"] == "uq_watchlist_items_group_symbol"
        for u in inspector.get_unique_constraints("watchlist_items")
    ), "watchlist_items 唯一约束 (group_id, symbol) 缺失"
    assert "ix_watchlist_items_symbol" in {ix["name"] for ix in inspector.get_indexes("watchlist_items")}
    assert inspector.get_pk_constraint("recommend_runs")["constrained_columns"] == ["id"]
    assert "ix_recommend_items_run_rank" in {ix["name"] for ix in inspector.get_indexes("recommend_items")}
    assert inspector.get_pk_constraint("research_runs")["constrained_columns"] == ["id"]
    assert inspector.get_pk_constraint("market_snapshot")["constrained_columns"] == ["trade_date", "symbol"]
    assert "ix_market_snapshot_symbol" in {ix["name"] for ix in inspector.get_indexes("market_snapshot")}

    # 7) 交易域读写往返 + ORM → pydantic DTO + 幂等唯一约束
    with Session(engine) as session:
        session.add(AccountORM(id="acc-1", name="模拟账户", initial_cash=100000.0, cash=60000.0, total_asset=100000.0))
        session.add(
            OrderORM(
                id="ord-1", client_order_id="cid-1", account_id="acc-1", symbol="600519.SH",
                side="BUY", order_type="LIMIT", price=10.0, quantity=100, status="FILLED",
            )
        )
        session.add(
            PositionORM(
                account_id="acc-1", symbol="600519.SH", quantity=100, available_quantity=0,
                today_buy_quantity=100, avg_cost=10.0, last_price=11.0, market_value=1100.0,
            )
        )
        session.commit()
        acc_row = session.scalars(select(AccountORM).where(AccountORM.id == "acc-1")).one()
        assert acc_row.name == "模拟账户" and acc_row.created_at is not None
        pos_row = session.scalars(select(PositionORM).where(PositionORM.symbol == "600519.SH")).one()
        assert pos_row.available_quantity == 0 and pos_row.today_buy_quantity == 100
        dto = Position.model_validate(pos_row)  # ORM → pydantic（from_attributes）
        assert dto.symbol == "600519.SH" and dto.t1_locked_quantity == 100
        # client_order_id 唯一约束（幂等键）
        session.add(
            OrderORM(id="ord-2", client_order_id="cid-1", account_id="acc-1",
                     symbol="600519.SH", side="BUY", quantity=100)
        )
        try:
            session.commit()
        except Exception:
            session.rollback()
        else:
            raise AssertionError("client_order_id 唯一约束未生效")

    # 8) Phase 2 扩展表读写（watchlist/recommend/research/market_snapshot）
    with Session(engine) as session:
        session.add(WatchlistGroupORM(id="long_term", name="远期", sort_order=1))
        session.add(
            WatchlistItemORM(group_id="long_term", symbol="600519.SH", note="核心", source="MANUAL")
        )
        session.add(
            RecommendRunORM(
                id="run-1", kind="FAR", as_of=date(2026, 10, 23),
                method="test", data_cutoff=date(2026, 10, 23),
            )
        )
        session.add(
            RecommendItemORM(
                run_id="run-1", rank=1, symbol="600519.SH", score=88.5,
                reason_json='["ROE 高", "动量强"]',
            )
        )
        session.add(
            ResearchRunORM(
                id="res-1", strategy_key="ma_cross", kind="WALK_FORWARD",
                data_start=date(2020, 1, 1), data_end=date(2025, 12, 31), seed=42,
            )
        )
        session.add(
            MarketSnapshotORM(
                trade_date=date(2026, 10, 23), symbol="600519.SH",
                name="贵州茅台", price=1400.0, pct_change=1.2, volume=1000,
            )
        )
        session.commit()
        assert session.scalars(select(WatchlistGroupORM)).one().name == "远期"
        item = session.scalars(select(WatchlistItemORM)).one()
        assert item.symbol == "600519.SH" and item.group_id == "long_term"
        assert session.scalars(select(RecommendItemORM)).one().run_id == "run-1"
        assert session.scalars(select(ResearchRunORM)).one().seed == 42
        snapshot_row = session.scalars(select(MarketSnapshotORM)).one()
        assert snapshot_row.name == "贵州茅台" and snapshot_row.price == 1400.0

    print("core/models.py 自检通过：领域模型（枚举/必填/原因码/派生属性）+ 28 张 ORM 表（22 核心 + 6 扩展；建表/PK/唯一/索引/读写往返）")


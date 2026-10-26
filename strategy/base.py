"""QuantDesk 策略层基础模块：策略基类 / 注册表 / 参数 schema / 指标兼容层。

契约冻结：**CF-3**（见 ``docs/TASKS.md`` 第 2 节）
任务编号：M1-05（策略基类与注册表）、M1-06（指标兼容层）
口径来源：``docs/SPEC.md`` §6.2、§8.2；``docs/BORROW_MAP.md`` P2 / B10

设计要点
--------
1. **新增策略 = 新增一个文件**：在 ``strategy/builtin/xxx.py`` 里写一个
   ``Strategy`` 子类 + ``@register("xxx")``，不需要改任何其它文件
   （BRIEF F2 验收、BORROW_MAP P2）。
2. **策略只产出 Signal**：策略不得访问数据库、不得联网、不得直接下单；
   指标数据全部由引擎通过 :class:`Context` 注入（防未来函数：``ctx.bars``
   只包含 ``<= 当前交易日`` 的历史）。
3. **参数 schema** 是 UI 动态表单的唯一依据（AC-F2-01/05），字段规范见
   :meth:`Strategy.params_schema`；非法参数抛 :class:`StrategyParamError`
   （中文消息，含字段名、合法区间）。
4. **指标优先 TA-Lib，缺失时 pandas 兜底**（M1-06 / AC-F2-04）：
   ``sma/ema/rsi/macd/std/atr`` 六个同名函数接口一致、口径对齐 TA-Lib
   （容差 ≤ 1e-6）；:func:`indicator_info` 暴露当前实现来源，供设置页展示。

借鉴说明（合规）
----------------
* 注册表“一个文件一个插件 + 元数据”的思路来自 Vibe-Trading（MIT）B7，按
  QuantDesk 自身的字段重写；
* ``Strategy`` 生命周期钩子（``on_init`` / ``confirm_signal``）的思路来自
  freqtrade（GPL-3.0）A11 与 lumibot（GPL-3.0）A8 —— **只重写设计思想，
  未复制任何代码、注释或变量名**；
* 指标门面“同名函数整体替换”的思路来自 TA-Lib Python 封装（BSD-2）B10，
  允许复用接口形态，实现为本项目自行编写。

典型用法
--------
::

    from strategy.base import Strategy, Context, Signal, register, get_strategy

    @register("ma_cross")
    class MaCross(Strategy):
        display_name = "双均线"
        description = "快线上穿慢线买入、下穿卖出"

        @classmethod
        def params_schema(cls):
            return {
                "fast": {"type": "int", "default": 5, "min": 2, "max": 120,
                         "step": 1, "label": "快线周期", "help": "短周期均线长度"},
                "slow": {"type": "int", "default": 20, "min": 3, "max": 250,
                         "step": 1, "label": "慢线周期", "help": "长周期均线长度"},
            }

        def on_bar(self, ctx: Context) -> list[Signal]:
            ...

    cls = get_strategy("ma_cross")          # 取策略类
    inst = cls({"fast": 5, "slow": 30})     # 参数已校验并补全默认值
"""

from __future__ import annotations

import copy
import importlib
import inspect
import logging
import math
import pkgutil
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np
import pandas as pd

# 接口裁定（Lead）：Signal 的唯一来源是 ``core/models.py``（Dev1 已交付），
# 策略层只导入与再导出，**不重复定义**；若依赖尚未安装会给出明确中文提示。
try:  # pragma: no cover - 依赖环境
    from core.models import Side, Signal
except ImportError as _core_exc:  # pragma: no cover
    raise ImportError(
        "策略层导入失败：无法从 core.models 导入 Signal / Side（唯一来源）。"
        f"请先安装依赖（pydantic、sqlalchemy）：{_core_exc}"
    ) from _core_exc

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查用，运行时不导入
    from core.models import Account, Position

logger = logging.getLogger(__name__)

__all__ = [
    # 数据契约
    "SIDE_BUY",
    "SIDE_SELL",
    "VALID_SIDES",
    "Signal",
    "Context",
    # 策略基类与注册表
    "Strategy",
    "register",
    "unregister",
    "is_registered",
    "get_strategy",
    "create_strategy",
    "list_strategies",
    "iter_strategies",
    "temporary_strategy",
    "open_strategy_registry",
    "load_builtin_strategies",
    "validate_params_schema",
    # 异常
    "StrategyError",
    "StrategyRegistrationError",
    "StrategyNotFoundError",
    "StrategyParamError",
    "StrategySchemaError",
    # 指标兼容层
    "sma",
    "ema",
    "rsi",
    "macd",
    "std",
    "atr",
    "get_indicator",
    "indicator_info",
    "INDICATOR_NAMES",
    "INDICATOR_BACKEND",
    "TALIB_AVAILABLE",
    "TALIB_VERSION",
    "normalize_side",
]


# ---------------------------------------------------------------------------
# 一、信号与上下文（CF-3 冻结字段，不得改名/改顺序语义）
# ---------------------------------------------------------------------------

#: 买卖方向常量（值取自 ``core.models.Side``，保证全项目字符串一致）
SIDE_BUY = Side.BUY.value
SIDE_SELL = Side.SELL.value
VALID_SIDES: tuple[str, ...] = (SIDE_BUY, SIDE_SELL)

_SIDE_ALIASES: dict[str, str] = {
    "BUY": SIDE_BUY,
    "B": SIDE_BUY,
    "LONG": SIDE_BUY,
    "买": SIDE_BUY,
    "买入": SIDE_BUY,
    "SELL": SIDE_SELL,
    "S": SIDE_SELL,
    "SHORT": SIDE_SELL,
    "卖": SIDE_SELL,
    "卖出": SIDE_SELL,
}


def normalize_side(side: Any) -> Side:
    """把各种写法统一成 ``core.models.Side`` 枚举（非法值抛中文 ``ValueError``）。

    兼容：``Side``/其它 ``Enum``（取 ``.value``）、字符串大小写、中文“买/卖”、
    简写 B/S。``Side`` 是 ``str`` 枚举，因此 ``normalize_side("buy") == "BUY"`` 为 True。

    典型用法：``Signal(symbol="600519.SH", side=normalize_side("买"), ...)``。
    """

    raw = side
    if isinstance(raw, Enum):  # 兼容 core.models.Side 等 str 枚举
        raw = raw.value
    elif not isinstance(raw, str) and hasattr(raw, "value"):
        raw = raw.value
    text = str(raw).strip().upper() if raw is not None else ""
    normalized = _SIDE_ALIASES.get(text)
    if normalized is None:
        raise ValueError(f"非法的交易方向：{side!r}（仅支持 BUY / SELL）")
    return Side(normalized)


@dataclass(frozen=True)
class Context:
    """策略运行上下文（**唯一来源**：本文件；SPEC §6.2 冻结，由引擎构造，策略只读）。

    防未来函数：``bars[symbol]`` 只包含 **截至当前交易日（含当日）** 的历史，
    引擎负责切片；策略不得假设自己能拿到更晚的数据。

    注意：``Signal`` 的唯一来源是 ``core/models.py``（Lead 接口裁定），本文件只
    再导出；``Signal`` 字段为 ``symbol/side(Side)/strength/reason/price_hint/ts``
    （另可选 ``trace_id/strategy_instance_id/strategy_key``）。

    Attributes:
        now: 当前时间（回测中为当日收盘时间，模拟盘为任务执行时间）。
        bars: ``symbol -> DataFrame(date, open, high, low, close, volume, ...)``，
            按时间升序。
        positions: ``symbol -> Position``（``core.models.Position``，字段含
            ``quantity`` / ``available_quantity`` / ``today_buy_quantity`` /
            ``avg_cost`` / ``last_price`` 等）。
        account: 账户快照（``core.models.Account``，字段含 ``cash`` / ``total_asset``）。
        params: 策略参数（已通过 :meth:`Strategy.validate_params` 校验）。
        is_backtest: True 表示回测环境，False 表示模拟盘/实盘。
    """

    now: datetime
    bars: Mapping[str, pd.DataFrame]
    positions: Mapping[str, "Position"]
    account: "Account"
    params: Mapping[str, Any]
    is_backtest: bool

    def __post_init__(self) -> None:
        if isinstance(self.now, datetime):
            pass
        elif isinstance(self.now, date):
            object.__setattr__(self, "now", datetime(self.now.year, self.now.month, self.now.day))
        else:
            raise ValueError(f"Context.now 必须是 datetime，收到 {type(self.now).__name__}")
        if not isinstance(self.bars, Mapping):
            raise ValueError("Context.bars 必须是 dict[symbol, DataFrame]")
        if not isinstance(self.params, Mapping):
            raise ValueError("Context.params 必须是 dict")
        object.__setattr__(self, "is_backtest", bool(self.is_backtest))

    # -- 便捷只读访问器（策略代码优先用这些，避免重复判空） ----------------
    @property
    def trade_date(self) -> date:
        """当前交易日（``datetime.date``）。"""

        return self.now.date()

    @property
    def symbols(self) -> list[str]:
        """本根 bar 有数据的标的列表。"""

        return list(self.bars.keys())

    def history(self, symbol: str, n: int | None = None) -> pd.DataFrame | None:
        """取某标的截至当前的历史 K 线；``n`` 为最近 n 根（None 表示全部）。"""

        df = self.bars.get(symbol)
        if df is None or len(df) == 0:
            return None
        if n is not None and n > 0:
            return df.iloc[-int(n) :]
        return df

    def last_bar(self, symbol: str) -> pd.Series | None:
        """取某标的当前交易日这根 bar；无数据返回 None。"""

        df = self.bars.get(symbol)
        if df is None or len(df) == 0:
            return None
        return df.iloc[-1]

    def close(self, symbol: str) -> float | None:
        """取某标的当前交易日收盘价；无数据/非法值返回 None。"""

        row = self.last_bar(symbol)
        if row is None:
            return None
        for key in ("close", "Close", "CLOSE"):
            try:
                value = row[key] if hasattr(row, "__getitem__") else None
            except (KeyError, IndexError, TypeError):
                value = None
            if value is not None:
                try:
                    price = float(value)
                except (TypeError, ValueError):
                    return None
                if math.isfinite(price) and price > 0:
                    return price
        return None

    def has_bars(self, symbol: str, min_bars: int = 1) -> bool:
        """历史长度是否达到 ``min_bars``（用于“数据不足则跳过”）。"""

        df = self.bars.get(symbol)
        return df is not None and len(df) >= int(min_bars)

    def position(self, symbol: str) -> Any | None:
        """取某标的持仓对象（无持仓返回 None）。"""

        return self.positions.get(symbol)

    def quantity(self, symbol: str) -> int:
        """当前持仓数量（股；无持仓返回 0）。"""

        return int(_position_field(self.positions.get(symbol), "quantity", 0) or 0)

    def available_quantity(self, symbol: str) -> int:
        """当前可卖数量（受 T+1 限制；无持仓返回 0）。"""

        pos = self.positions.get(symbol)
        available = _position_field(pos, "available_quantity", None)
        if available is None:
            return self.quantity(symbol)
        return int(available or 0)

    def market_value(self, symbol: str) -> float:
        """持仓市值（有 last_price 用最后价，否则用当前收盘价；无持仓返回 0）。"""

        qty = self.quantity(symbol)
        if qty == 0:
            return 0.0
        pos = self.positions.get(symbol)
        for key in ("last_price", "market_price", "price"):
            value = _position_field(pos, key, None)
            if value is not None:
                try:
                    price = float(value)
                except (TypeError, ValueError):
                    price = 0.0
                if price > 0:
                    return qty * price
        price = self.close(symbol) or 0.0
        return qty * price


def _position_field(position: Any, name: str, default: Any = None) -> Any:
    """从持仓对象/字典里读字段（兼容 pydantic 模型、dataclass、dict）。"""

    if position is None:
        return default
    if isinstance(position, Mapping):
        return position.get(name, default)
    return getattr(position, name, default)


# ---------------------------------------------------------------------------
# 二、策略参数 schema 规范与校验
# ---------------------------------------------------------------------------


class StrategyError(Exception):
    """策略层异常基类（所有错误消息均为中文、可直接展示给用户）。"""


class StrategyRegistrationError(StrategyError, ValueError):
    """注册表错误：key 非法/重复、类不是 Strategy 子类、参数 schema 非法等。"""


class StrategyNotFoundError(StrategyError, LookupError):
    """策略未注册（``get_strategy`` / ``create_strategy`` 抛出）。"""


class StrategySchemaError(StrategyError, ValueError):
    """``params_schema()`` 返回值不符合规范。"""


class StrategyParamError(StrategyError, ValueError):
    """策略参数非法（UI 用它做字段级红色提示）。"""

    def __init__(self, message: str, field_name: str | None = None, value: Any = None) -> None:
        super().__init__(message)
        self.field_name = field_name
        self.value = value

    def to_dict(self) -> dict[str, Any]:
        """UI 展示用结构：``{"field": ..., "message": ...}``。"""

        return {"field": self.field_name, "message": str(self)}


PARAM_TYPES: tuple[str, ...] = ("int", "float", "str", "bool", "select")

#: 每个参数字段必须提供的键（``min/max/step/unit/choices/advanced`` 为可选）。
PARAM_SCHEMA_REQUIRED_KEYS: tuple[str, ...] = ("type", "default", "label", "help")


def validate_params_schema(schema: Mapping[str, Any], owner: str = "") -> None:
    """校验 ``params_schema()`` 的返回值是否符合规范（注册时 fail-fast）。

    规范（每个参数字段）::

        {
            "type": "int" | "float" | "str" | "bool" | "select",   # 必填
            "default": <默认值>,                                    # 必填
            "label": "中文标签",                                    # 必填
            "help": "中文帮助文本",                                 # 必填
            "min": 数值, "max": 数值, "step": 数值,                 # 数值型可选
            "unit": "单位", "choices": [...],                       # 可选
            "advanced": False,                                      # 可选（UI 折叠）
        }

    Raises:
        StrategySchemaError: 任一字段不合规，消息中给出具体字段与原因。
    """

    who = f"策略 {owner} 的 " if owner else "策略 "
    if not isinstance(schema, Mapping):
        raise StrategySchemaError(f"{who}params_schema() 必须返回 dict，收到 {type(schema).__name__}")
    for name, spec in schema.items():
        if not isinstance(name, str) or not name.strip():
            raise StrategySchemaError(f"{who}参数名必须是非空字符串，收到 {name!r}")
        if not name.replace("_", "").isalnum() or name[0].isdigit():
            raise StrategySchemaError(f"{who}参数名 {name!r} 只能包含字母/数字/下划线，且不能以数字开头")
        if not isinstance(spec, Mapping):
            raise StrategySchemaError(f"{who}参数 {name!r} 的 schema 必须是 dict，收到 {type(spec).__name__}")
        missing = [k for k in PARAM_SCHEMA_REQUIRED_KEYS if k not in spec]
        if missing:
            raise StrategySchemaError(
                f"{who}参数 {name!r} 的 schema 缺少必填键 {missing}；"
                f"规范为 {list(PARAM_SCHEMA_REQUIRED_KEYS)} + 可选 min/max/step/unit/choices"
            )
        ptype = spec["type"]
        if ptype not in PARAM_TYPES:
            raise StrategySchemaError(
                f"{who}参数 {name!r} 的 type={ptype!r} 不支持，可选值：{'/'.join(PARAM_TYPES)}"
            )
        if not isinstance(spec["label"], str) or not spec["label"].strip():
            raise StrategySchemaError(f"{who}参数 {name!r} 的 label 必须是非空中文标签")
        if not isinstance(spec["help"], str) or not spec["help"].strip():
            raise StrategySchemaError(f"{who}参数 {name!r} 的 help 必须是非空中文帮助文本")

        default = spec["default"]
        if ptype == "int" and (isinstance(default, bool) or not isinstance(default, int)):
            raise StrategySchemaError(f"{who}参数 {name!r} 的 default 必须是 int，收到 {default!r}")
        if ptype == "float" and (isinstance(default, bool) or not isinstance(default, (int, float))):
            raise StrategySchemaError(f"{who}参数 {name!r} 的 default 必须是 float，收到 {default!r}")
        if ptype == "bool" and not isinstance(default, bool):
            raise StrategySchemaError(f"{who}参数 {name!r} 的 default 必须是 bool，收到 {default!r}")
        if ptype == "str" and not isinstance(default, str):
            raise StrategySchemaError(f"{who}参数 {name!r} 的 default 必须是 str，收到 {default!r}")

        for bound in ("min", "max", "step"):
            if bound in spec and spec[bound] is not None:
                try:
                    float(spec[bound])
                except (TypeError, ValueError) as exc:
                    raise StrategySchemaError(f"{who}参数 {name!r} 的 {bound} 必须是数值") from exc
        if "min" in spec and "max" in spec and spec["min"] is not None and spec["max"] is not None:
            if float(spec["min"]) > float(spec["max"]):
                raise StrategySchemaError(f"{who}参数 {name!r} 的 min 不能大于 max")
        if "step" in spec and spec["step"] is not None and float(spec["step"]) <= 0:
            raise StrategySchemaError(f"{who}参数 {name!r} 的 step 必须大于 0")

        choices = spec.get("choices")
        if ptype == "select":
            if not isinstance(choices, (list, tuple)) or len(choices) == 0:
                raise StrategySchemaError(f"{who}参数 {name!r} 的 type=select 时必须提供非空 choices 列表")
            if default not in choices:
                raise StrategySchemaError(f"{who}参数 {name!r} 的 default={default!r} 不在 choices={list(choices)} 中")
        elif choices is not None:
            if not isinstance(choices, (list, tuple)) or len(choices) == 0:
                raise StrategySchemaError(f"{who}参数 {name!r} 的 choices 必须是非空列表")


def _coerce_bool(name: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if float(value) in (0.0, 1.0):
            return bool(value)
        raise StrategyParamError(f"参数 {name} 必须是布尔值（true/false），收到 {value!r}", name, value)
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "y", "是", "开"):
        return True
    if text in ("false", "0", "no", "n", "否", "关"):
        return False
    raise StrategyParamError(f"参数 {name} 必须是布尔值（true/false），收到 {value!r}", name, value)


def _coerce_typed_param(name: str, label: str, ptype: str, value: Any, spec: Mapping[str, Any]) -> Any:
    """按 schema 把单个参数值做类型转换 + 区间校验。"""

    if ptype == "int":
        if isinstance(value, bool):
            raise StrategyParamError(f"参数 {label}（{name}）必须是整数，收到布尔值", name, value)
        if isinstance(value, float) and not value.is_integer():
            raise StrategyParamError(f"参数 {label}（{name}）必须是整数，收到 {value!r}", name, value)
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise StrategyParamError(f"参数 {label}（{name}）必须是整数，收到 {value!r}", name, value) from exc
        return number
    if ptype == "float":
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise StrategyParamError(f"参数 {label}（{name}）必须是数值，收到 {value!r}", name, value) from exc
        if not math.isfinite(number):
            raise StrategyParamError(f"参数 {label}（{name}）必须是有限数值，收到 {value!r}", name, value)
        return number
    if ptype == "bool":
        return _coerce_bool(name, value)
    if ptype == "str":
        return str(value)
    # select：保持 choices 中的原始类型
    choices = list(spec.get("choices") or [])
    for choice in choices:
        if choice == value or str(choice) == str(value):
            return choice
    raise StrategyParamError(
        f"参数 {label}（{name}）必须是以下之一：{'、'.join(str(c) for c in choices)}，收到 {value!r}",
        name,
        value,
    )


# ---------------------------------------------------------------------------
# 三、策略基类
# ---------------------------------------------------------------------------


class Strategy(ABC):
    """策略插件基类（契约 CF-3）。

    子类最少要实现：``display_name``、``params_schema()``、``on_bar()``；
    可覆写：``description``、``startup_bars``、``on_init()``、``confirm_signal()``、
    ``validate_constraints()``。

    生命周期（由回测/自动化引擎统一调用）::

        inst = cls(params)     # 参数校验 + 补默认值（在 __init__ 内完成）
        inst.on_init(ctx)      # 每轮开始一次（预热/指标缓存）
        inst.on_bar(ctx)       # 每个交易日一次，返回信号列表
        inst.confirm_signal(s) # 可选确认钩子（返回 False 则丢弃该信号）

    纪律：策略内**禁止**访问数据库/网络/broker，不得写全局状态；同样的输入
    必须产出同样的信号（可复现，AC-F2-03）。
    """

    #: 注册表唯一键（由 ``@register("...")`` 自动写入）
    key: ClassVar[str] = ""
    #: UI 展示名（中文，必填）
    display_name: ClassVar[str] = ""
    #: 一句话说明（UI 策略列表展示）
    description: ClassVar[str] = ""
    #: 预热所需 bar 数量：历史不足时引擎会跳过该标的（避免前 N 根出假信号）
    startup_bars: ClassVar[int] = 0

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        self.params: dict[str, Any] = self.validate_params(params)
        self.logger = logging.getLogger(f"{__name__}.{type(self).key or type(self).__name__}")

    # -- 子类必须实现 -----------------------------------------------------
    @classmethod
    @abstractmethod
    def params_schema(cls) -> dict[str, dict[str, Any]]:
        """声明可配置参数（UI 依据此动态生成表单并做字段校验）。

        Returns:
            ``{参数名: {"type", "default", "label", "help", ...}}``，规范见
            :func:`validate_params_schema`。无参数策略返回 ``{}``。
        """

        raise NotImplementedError

    @abstractmethod
    def on_bar(self, ctx: Context) -> list[Signal]:
        """每个交易日调用一次，返回本根 bar 产生的信号列表（可为空）。

        要求：只用 ``ctx.bars`` 中 ``<= ctx.trade_date`` 的数据；不得重复发
        同标的同方向信号（引擎也会去重，但策略侧应自行避免）。
        """

        raise NotImplementedError

    # -- 可选钩子 ---------------------------------------------------------
    def on_init(self, ctx: Context) -> None:
        """每轮回测/每次任务启动时调用一次；默认空实现。"""

        return None

    def confirm_signal(self, signal: Signal, ctx: Context | None = None) -> bool:
        """可选信号确认钩子：返回 False 时引擎丢弃该信号；默认全部通过。"""

        return True

    @classmethod
    def validate_constraints(cls, params: Mapping[str, Any]) -> None:
        """跨字段约束校验钩子（如“快线周期 < 慢线周期”）。

        默认不校验；子类发现非法组合时抛 :class:`StrategyParamError`（中文消息）。
        """

        return None

    # -- 参数处理 ---------------------------------------------------------
    @classmethod
    def params_schema_safe(cls) -> dict[str, dict[str, Any]]:
        """返回经过规范校验的 schema 深拷贝（UI/引擎使用，防止调用方误改）。"""

        schema = cls.params_schema()
        validate_params_schema(schema, owner=cls.key or cls.__name__)
        return copy.deepcopy(dict(schema))

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        """只取 schema 里的默认值。"""

        schema = cls.params_schema_safe()
        return {name: spec["default"] for name, spec in schema.items()}

    @classmethod
    def validate_params(cls, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """补默认值 + 类型转换 + 区间校验 + 跨字段约束校验。

        Args:
            params: 用户参数（None 表示全部用默认值）。

        Returns:
            规范化后的参数字典（只含 schema 中声明的字段）。

        Raises:
            StrategyParamError: 类型错误、超区间、缺必填、未知参数、跨字段冲突。
                ``message`` 为中文，可直接展示给用户；``field_name`` 供 UI 定位。
        """

        schema = cls.params_schema_safe()
        if params is None:
            raw: dict[str, Any] = {}
        elif isinstance(params, Mapping):
            raw = dict(params)
        else:
            raise StrategyParamError(
                f"策略参数必须是 dict，收到 {type(params).__name__}", None, params
            )

        unknown = [str(k) for k in raw if k not in schema]
        if unknown:
            available = "、".join(schema) if schema else "（该策略无可配置参数）"
            raise StrategyParamError(
                f"策略 {cls.key or cls.__name__} 不支持参数 {('、'.join(unknown))}；"
                f"可用参数：{available}"
            )

        normalized: dict[str, Any] = {}
        for name, spec in schema.items():
            label = str(spec.get("label") or name)
            if name in raw and raw[name] is not None:
                value = _coerce_typed_param(name, label, str(spec["type"]), raw[name], spec)
            else:
                value = copy.deepcopy(spec["default"])

            if spec["type"] in ("int", "float"):
                low = spec.get("min")
                high = spec.get("max")
                if low is not None and value < float(low):
                    raise StrategyParamError(
                        f"参数 {label}（{name}）不能小于 {low}，当前 {value}", name, value
                    )
                if high is not None and value > float(high):
                    raise StrategyParamError(
                        f"参数 {label}（{name}）不能大于 {high}，当前 {value}", name, value
                    )
            normalized[name] = value

        try:
            cls.validate_constraints(normalized)
        except StrategyParamError:
            raise
        except ValueError as exc:  # 允许子类直接 raise ValueError
            raise StrategyParamError(f"策略参数组合非法：{exc}") from exc
        return normalized

    # -- 便捷访问 ---------------------------------------------------------
    def param(self, name: str, default: Any = None) -> Any:
        """取参数值（不存在时返回 default，便于策略代码写 ``self.param("x", 5)``）。"""

        return self.params.get(name, default)

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"<{type(self).__name__} key={type(self).key!r} params={self.params!r}>"


# ---------------------------------------------------------------------------
# 四、策略注册表
# ---------------------------------------------------------------------------

#: 全局注册表：``key -> Strategy 子类``（新增策略只写文件 + 装饰器，不在此登记）
STRATEGY_REGISTRY: dict[str, type[Strategy]] = {}

_KEY_PATTERN = r"^[a-z][a-z0-9_]*$"


def _validate_key(key: str) -> str:
    import re

    if not isinstance(key, str) or not key.strip():
        raise StrategyRegistrationError("策略注册 key 必须是非空字符串")
    normalized = key.strip()
    if re.match(_KEY_PATTERN, normalized) is None:
        raise StrategyRegistrationError(
            f"策略注册 key {normalized!r} 非法：只能用小写字母、数字、下划线，且以字母开头"
            "（示例：ma_cross）"
        )
    return normalized


def register(key: str) -> Callable[[type[Strategy]], type[Strategy]]:
    """注册策略的类装饰器：``@register("ma_cross")``。

    注册时立即校验（fail-fast，避免 UI 拿到坏策略）：

    * 必须是 :class:`Strategy` 子类且已实现全部抽象方法；
    * ``key`` 合法且未被其它类占用（同一类重复注册视为幂等，兼容模块重载）；
    * ``display_name`` 非空、``params_schema()`` 符合规范。
    """

    def decorator(cls: type[Strategy]) -> type[Strategy]:
        if not isinstance(cls, type) or not issubclass(cls, Strategy):
            raise StrategyRegistrationError(
                f"@register 只能装饰 Strategy 子类，收到 {cls!r}"
            )
        normalized = _validate_key(key)
        if inspect.isabstract(cls):
            abstract_methods = sorted(getattr(cls, "__abstractmethods__", ()) or ())
            raise StrategyRegistrationError(
                f"策略 {normalized!r}（{cls.__name__}）仍包含未实现的抽象方法："
                f"{'、'.join(abstract_methods)}；请实现后再注册"
            )
        existing = STRATEGY_REGISTRY.get(normalized)
        if existing is not None and existing is not cls:
            raise StrategyRegistrationError(
                f"策略 key {normalized!r} 已被 {existing.__name__} 注册，不能重复使用；"
                "如需替换请先调用 unregister()"
            )
        declared = str(getattr(cls, "key", "") or "").strip()
        if declared and declared != normalized:
            raise StrategyRegistrationError(
                f"策略类 {cls.__name__} 的 key 属性为 {declared!r}，与注册 key {normalized!r} 不一致"
            )
        display_name = str(getattr(cls, "display_name", "") or "").strip()
        if not display_name:
            raise StrategyRegistrationError(
                f"策略 {normalized!r}（{cls.__name__}）必须设置 display_name（中文展示名），"
                "供 UI 策略列表与回测页使用"
            )
        validate_params_schema(cls.params_schema(), owner=normalized)

        cls.key = normalized
        STRATEGY_REGISTRY[normalized] = cls
        logger.debug("策略已注册：%s（%s）", normalized, cls.__name__)
        return cls

    return decorator


def unregister(key: str) -> type[Strategy] | None:
    """从注册表移除策略（QA 临时注册/测试清理用）；不存在返回 None。"""

    return STRATEGY_REGISTRY.pop(str(key), None)


def is_registered(key: str) -> bool:
    """策略 key 是否已注册。"""

    return str(key) in STRATEGY_REGISTRY


def get_strategy(key: str) -> type[Strategy]:
    """按 key 取策略类。

    Raises:
        StrategyNotFoundError: 未注册，消息中列出当前可用 key（中文）。
    """

    normalized = str(key or "").strip()
    try:
        return STRATEGY_REGISTRY[normalized]
    except KeyError as exc:
        available = "、".join(sorted(STRATEGY_REGISTRY)) or "（当前没有已注册策略）"
        raise StrategyNotFoundError(
            f"策略 {normalized!r} 未注册；可用策略：{available}"
        ) from exc


def create_strategy(key: str, params: Mapping[str, Any] | None = None) -> Strategy:
    """按 key + 参数创建策略实例（参数已校验）。"""

    return get_strategy(key)(params)


def iter_strategies() -> Iterable[tuple[str, type[Strategy]]]:
    """按 key 排序遍历 ``(key, 策略类)``。"""

    for key in sorted(STRATEGY_REGISTRY):
        yield key, STRATEGY_REGISTRY[key]


def list_strategies() -> list[dict[str, Any]]:
    """列出全部已注册策略（UI 动态表单/回测页下拉用）。

    Returns:
        ``[{"key", "display_name", "description", "params_schema", "startup_bars"}]``
        （按 key 排序；``params_schema`` 为深拷贝，调用方可放心修改）。
    """

    result: list[dict[str, Any]] = []
    for key, cls in iter_strategies():
        try:
            schema = cls.params_schema_safe()
        except StrategySchemaError:  # 理论上注册时已校验，防御性兜底
            schema = {}
        result.append(
            {
                "key": key,
                "display_name": getattr(cls, "display_name", key),
                "description": getattr(cls, "description", ""),
                "params_schema": schema,
                "startup_bars": int(getattr(cls, "startup_bars", 0) or 0),
            }
        )
    return result


@contextmanager
def temporary_strategy(key: str, cls: type[Strategy]):
    """临时注册一个策略并在退出时还原（QA 验收 AC-F2-02 用）。

    例::

        with temporary_strategy("qa_demo", DemoStrategy):
            assert is_registered("qa_demo")
    """

    normalized = _validate_key(key)
    existed = normalized in STRATEGY_REGISTRY
    previous = STRATEGY_REGISTRY.get(normalized)
    register(normalized)(cls)
    try:
        yield cls
    finally:
        if existed and previous is not None:
            STRATEGY_REGISTRY[normalized] = previous
        else:
            STRATEGY_REGISTRY.pop(normalized, None)


def open_strategy_registry() -> dict[str, type[Strategy]]:
    """返回注册表本体（只读用途；测试断言用，勿直接改动业务代码里的注册表）。"""

    return STRATEGY_REGISTRY


def load_builtin_strategies(*, strict: bool = False) -> list[str]:
    """导入 ``strategy/builtin/*.py`` 触发 ``@register`` 自动注册。

    “新增策略 = 新增一个文件”，因此内置策略由本函数在包导入时自动发现；
    ``builtin`` 目录尚不存在时返回空列表（不报错，便于并行开发）。

    Args:
        strict: True 时任一模块导入失败直接抛异常（QA/集成冒烟用）；
            False 时记录 ERROR 日志并跳过，不影响其它策略加载。

    Returns:
        成功导入的模块名列表（如 ``["ma_cross", "rsi_reversal"]``）。
    """

    package = f"{__name__.rsplit('.', 1)[0]}.builtin"
    try:
        pkg = importlib.import_module(package)
    except ModuleNotFoundError:
        logger.info("未发现内置策略包 %s（M1-07/08/09 尚未交付），跳过自动加载", package)
        return []
    except Exception as exc:  # pragma: no cover - 环境异常
        logger.error("导入内置策略包 %s 失败：%s", package, exc)
        if strict:
            raise
        return []

    loaded: list[str] = []
    for module_info in sorted(pkgutil.iter_modules(getattr(pkg, "__path__", [])), key=lambda m: m.name):
        if module_info.name.startswith("_"):
            continue
        full_name = f"{package}.{module_info.name}"
        try:
            importlib.import_module(full_name)
            loaded.append(module_info.name)
        except Exception as exc:
            if strict:
                raise
            logger.exception("加载内置策略模块 %s 失败（已跳过）：%s", full_name, exc)
    return loaded


# ---------------------------------------------------------------------------
# 五、指标兼容层（M1-06）：优先 TA-Lib，导入失败时 pandas 兜底
# ---------------------------------------------------------------------------
#
# 口径说明（两套实现必须一致，容差 ≤ 1e-6）：
#   * SMA   —— 简单移动平均，前 period-1 个值为 NaN。
#   * EMA   —— TA-Lib 口径：以“前 period 个值的 SMA”作为种子，再按
#              alpha = 2/(period+1) 递推（与 pandas ewm(adjust=False) 从首值
#              起算的常见写法不同，故这里显式实现以保持一致）。
#   * RSI   —— Wilder 平滑：先取前 period 个涨跌幅的均值作为种子，再按
#              alpha = 1/period 递推；RSI = 100 × 平均涨幅 /（平均涨幅 + 平均跌幅），
#              分母为 0 时取 0（与 TA-Lib 完全一致，避免除零）。
#   * MACD  —— 快/慢 EMA 均从 slow-1 起算（TA-Lib 内部口径），macd = 快线 − 慢线，
#              signal = EMA(macd, signal)，hist = macd − signal；三条输出统一从
#              slow+signal-2 起有效（更早的值 TA-Lib 不返回，兜底实现同样置 NaN）。
#   * STD   —— 总体标准差（ddof=0，对应 TA-Lib STDDEV 默认口径）；ddof=1 仅 pandas 可用。
#   * ATR   —— 真实波幅 TR = max(high, prev_close) - min(low, prev_close)，
#              再用 Wilder 平滑（alpha = 1/period），首个有效值在索引 period。
#
# 数据不足时返回全 NaN（不抛异常），由策略按 startup_bars 自行跳过。

#: 指标名（顺序固定，供设置页/文档展示）
INDICATOR_NAMES: tuple[str, ...] = ("sma", "ema", "rsi", "macd", "std", "atr")

#: 两套实现的结果容差（AC-F2-04）
INDICATOR_TOLERANCE = 1e-6

#: 当前默认实现来源：``"talib"`` 或 ``"pandas"``（M1-06 ⑤ 暴露给设置页）
INDICATOR_BACKEND = "pandas"
TALIB_AVAILABLE = False
TALIB_VERSION: str | None = None
TALIB_IMPORT_ERROR: str | None = None

try:  # pragma: no cover - 依赖本机环境
    import talib as _talib  # type: ignore

    TALIB_AVAILABLE = True
    TALIB_VERSION = str(getattr(_talib, "__version__", "unknown"))
    INDICATOR_BACKEND = "talib"
except Exception as _talib_exc:  # ImportError / Windows DLL 加载失败等
    _talib = None  # type: ignore[assignment]
    TALIB_IMPORT_ERROR = f"{type(_talib_exc).__name__}: {_talib_exc}"
    logger.info("TA-Lib 不可用（%s），指标层使用 pandas 兜底实现", TALIB_IMPORT_ERROR)


class IndicatorBackendError(StrategyError, RuntimeError):
    """显式要求 TA-Lib 实现但当前不可用时抛出（中文消息提示改用兜底）。"""


def _as_float_series(values: Any, *, name: str = "输入") -> pd.Series:
    """把 list/ndarray/Series 统一成 float64 的 ``pd.Series``（保留索引）。"""

    if isinstance(values, pd.DataFrame):
        raise TypeError(f"指标{name}应为价格序列（pd.Series），收到 DataFrame；请先取一列，如 df['close']")
    if isinstance(values, pd.Series):
        series = values.astype("float64")
    else:
        try:
            series = pd.Series(np.asarray(values, dtype="float64"))
        except (TypeError, ValueError) as exc:
            raise TypeError(f"指标{name}无法转换为数值序列：{exc}") from exc
    if len(series) == 0:
        raise ValueError(f"指标{name}为空序列，无法计算")
    return series


def _check_period(period: Any, *, name: str = "period") -> int:
    """校验窗口长度为正整数。"""

    if isinstance(period, bool):
        raise ValueError(f"指标 {name} 必须是正整数，收到 {period!r}")
    try:
        value = int(period)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"指标 {name} 必须是正整数，收到 {period!r}") from exc
    if isinstance(period, float) and not float(period).is_integer():
        raise ValueError(f"指标 {name} 必须是整数，收到 {period!r}")
    if value < 1:
        raise ValueError(f"指标 {name} 必须 ≥ 1，收到 {period!r}")
    return value


def _use_talib(backend: str, clean: bool) -> bool:
    """决定这一次调用是否走 TA-Lib。

    ``backend="auto"``：TA-Lib 已安装且输入无 NaN 时走 TA-Lib，否则走 pandas
    （TA-Lib 遇 NaN 会停止计算，兜底实现可跳过前导 NaN，正确性优先）。
    """

    if backend not in ("auto", "talib", "pandas"):
        raise ValueError(f"指标 backend 参数非法：{backend!r}（可选 auto/talib/pandas）")
    if backend == "pandas":
        return False
    if not TALIB_AVAILABLE:
        if backend == "talib":
            raise IndicatorBackendError(
                f"TA-Lib 不可用（{TALIB_IMPORT_ERROR}），无法使用 talib 实现；"
                "请改用 backend='pandas' 的兜底实现"
            )
        return False
    if not clean:
        if backend == "talib":
            raise IndicatorBackendError(
                "TA-Lib 实现不支持含 NaN 的输入序列；请先清洗数据，或使用 backend='pandas'"
            )
        return False
    return True


def _recursive_mean_at(values: np.ndarray, period: int, alpha: float, seed_pos: int) -> np.ndarray:
    """在指定位置播种的递推均值（TA-Lib 内部 EMA 口径，MACD 用）。

    种子 = ``values[seed_pos-period+1 : seed_pos+1]`` 的算术平均，之后按
    ``y[i] = alpha*x[i] + (1-alpha)*y[i-1]`` 递推；种子窗口不完整则全 NaN。
    """

    arr = np.asarray(values, dtype="float64")
    out = np.full(arr.shape, np.nan, dtype="float64")
    if seed_pos < period - 1 or seed_pos >= arr.size:
        return out
    window = arr[seed_pos - period + 1 : seed_pos + 1]
    if not np.all(np.isfinite(window)):
        return out
    tail = arr[seed_pos:].copy()
    tail[0] = float(window.mean())
    out[seed_pos:] = pd.Series(tail).ewm(alpha=alpha, adjust=False, min_periods=1).mean().to_numpy()
    return out


def _recursive_mean(values: np.ndarray, period: int, alpha: float) -> np.ndarray:
    """TA-Lib 口径的递推均值（EMA / Wilder 平滑共用）。

    以“首个有效值起的前 period 个观测的算术平均”为种子，之后按
    ``y[i] = alpha*x[i] + (1-alpha)*y[i-1]`` 递推；数据不足时返回全 NaN。
    """

    arr = np.asarray(values, dtype="float64")
    finite = np.isfinite(arr)
    if not finite.any():
        return np.full(arr.shape, np.nan, dtype="float64")
    first = int(np.argmax(finite))
    seed_pos = first + period - 1
    return _recursive_mean_at(arr, period, alpha, seed_pos)


def _talib_array(function_name: str, *arrays: np.ndarray, **kwargs: Any) -> np.ndarray:
    """调用 TA-Lib 函数并转成 float64 ndarray（TA-Lib 未安装时不会被调用）。"""

    function = getattr(_talib, function_name)
    result = function(*(np.ascontiguousarray(a, dtype="float64") for a in arrays), **kwargs)
    if isinstance(result, tuple):
        raise TypeError("多输出指标请使用专门的多返回值处理")
    return np.asarray(result, dtype="float64")


def _series_like(arr: np.ndarray, index: pd.Index, name: str) -> pd.Series:
    return pd.Series(np.asarray(arr, dtype="float64"), index=index, name=name)


def sma(values: Any, period: int = 20, *, backend: str = "auto") -> pd.Series:
    """简单移动平均（SMA），前 ``period-1`` 个值为 NaN。"""

    series = _as_float_series(values)
    window = _check_period(period)
    if _use_talib(backend, not series.isna().any()):
        arr = _talib_array("SMA", series.to_numpy(), timeperiod=window)
    else:
        arr = series.rolling(window=window, min_periods=window).mean().to_numpy()
    return _series_like(arr, series.index, f"sma_{window}")


def ema(values: Any, period: int = 20, *, backend: str = "auto") -> pd.Series:
    """指数移动平均（EMA），TA-Lib 口径（SMA 种子 + alpha=2/(period+1)）。"""

    series = _as_float_series(values)
    window = _check_period(period)
    if _use_talib(backend, not series.isna().any()):
        arr = _talib_array("EMA", series.to_numpy(), timeperiod=window)
    else:
        arr = _recursive_mean(series.to_numpy(), window, 2.0 / (window + 1.0))
    return _series_like(arr, series.index, f"ema_{window}")


def rsi(values: Any, period: int = 14, *, backend: str = "auto") -> pd.Series:
    """相对强弱指标（RSI，Wilder 平滑）；首个有效值在索引 ``period``。"""

    series = _as_float_series(values)
    window = _check_period(period)
    if _use_talib(backend, not series.isna().any()):
        arr = _talib_array("RSI", series.to_numpy(), timeperiod=window)
        return _series_like(arr, series.index, f"rsi_{window}")

    delta = series.diff().to_numpy()
    gain = np.where(np.isfinite(delta), np.maximum(delta, 0.0), np.nan)
    loss = np.where(np.isfinite(delta), np.maximum(-delta, 0.0), np.nan)
    avg_gain = _recursive_mean(gain, window, 1.0 / window)
    avg_loss = _recursive_mean(loss, window, 1.0 / window)
    total = avg_gain + avg_loss
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(total > 0, 100.0 * avg_gain / np.where(total > 0, total, 1.0), 0.0)
    arr = np.where(np.isfinite(total), ratio, np.nan)
    return _series_like(arr, series.index, f"rsi_{window}")


def macd(
    values: Any,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
    *,
    backend: str = "auto",
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD：返回 ``(macd 线, signal 线, 柱状图)`` 三个 Series（TA-Lib 口径）。

    口径（与 TA-Lib ``MACD`` 完全一致，容差 ≤ 1e-6）：

    * 快/慢 EMA 都**从索引 ``slow-1`` 开始**推算：慢线种子 = ``close[:slow]`` 的
      SMA，快线种子 = ``close[slow-fast:slow]`` 的 SMA，之后按 TA-Lib 的
      alpha 递推（这是 TA-Lib MACD 与单独 EMA 的差异点）；
    * ``signal`` = 以 ``macd 线`` 前 ``signal`` 个值为种子的 EMA；
    * 三条输出**统一起点为 ``slow+signal-2``**（更早的值 TA-Lib 不返回，这里同样
      置 NaN），与 TA-Lib 接口对齐。
    """

    fast = _check_period(fast_period, name="fast_period")
    slow = _check_period(slow_period, name="slow_period")
    signal_window = _check_period(signal_period, name="signal_period")
    if fast >= slow:
        raise ValueError(f"MACD 快线周期必须小于慢线周期（fast={fast}，slow={slow}）")

    series = _as_float_series(values)
    clean = not series.isna().any()
    if _use_talib(backend, clean):
        raw_macd, raw_signal, raw_hist = _talib.MACD(  # type: ignore[union-attr]
            np.ascontiguousarray(series.to_numpy(), dtype="float64"),
            fastperiod=fast,
            slowperiod=slow,
            signalperiod=signal_window,
        )
        arrays = (
            np.asarray(raw_macd, dtype="float64"),
            np.asarray(raw_signal, dtype="float64"),
            np.asarray(raw_hist, dtype="float64"),
        )
    else:
        close = series.to_numpy()
        macd_start = slow - 1
        # TA-Lib 的 MACD 内部：两条 EMA 都从 slow-1 起算（不是各自的自然起点）
        fast_line = _recursive_mean_at(close, fast, 2.0 / (fast + 1.0), macd_start)
        slow_line = _recursive_mean_at(close, slow, 2.0 / (slow + 1.0), macd_start)
        macd_line = fast_line - slow_line
        signal_start = macd_start + signal_window - 1
        signal_line = _recursive_mean_at(
            macd_line, signal_window, 2.0 / (signal_window + 1.0), signal_start
        )
        # TA-Lib 三条输出统一起点为 signal_start，更早的值不返回
        if signal_start > 0:
            macd_line[: min(signal_start, macd_line.size)] = np.nan
        hist_line = macd_line - signal_line
        arrays = (macd_line, signal_line, hist_line)

    names = ("macd", "macd_signal", "macd_hist")
    return tuple(_series_like(arr, series.index, name) for arr, name in zip(arrays, names))  # type: ignore[return-value]


def std(values: Any, period: int = 20, *, ddof: int = 0, backend: str = "auto") -> pd.Series:
    """滚动标准差；默认总体标准差（ddof=0，对应 TA-Lib STDDEV 口径）。

    ``ddof=1``（样本标准差）仅 pandas 实现可用。
    """

    window = _check_period(period)
    if ddof not in (0, 1):
        raise ValueError(f"指标 std 的 ddof 仅支持 0 或 1，收到 {ddof!r}")
    series = _as_float_series(values)
    if ddof == 0 and _use_talib(backend, not series.isna().any()):
        arr = _talib_array("STDDEV", series.to_numpy(), timeperiod=window, nbdev=1.0)
    else:
        arr = series.rolling(window=window, min_periods=window).std(ddof=ddof).to_numpy()
    return _series_like(arr, series.index, f"std_{window}")


def atr(high: Any, low: Any, close: Any, period: int = 14, *, backend: str = "auto") -> pd.Series:
    """平均真实波幅（ATR，Wilder 平滑）；首个有效值在索引 ``period``。"""

    window = _check_period(period)
    high_series = _as_float_series(high, name=" high ")
    low_series = _as_float_series(low, name=" low ")
    close_series = _as_float_series(close, name=" close ")
    if not (len(high_series) == len(low_series) == len(close_series)):
        raise ValueError("指标 atr 的 high/low/close 长度必须一致")

    clean = not (
        high_series.isna().any() or low_series.isna().any() or close_series.isna().any()
    )
    if _use_talib(backend, clean):
        arr = _talib_array(
            "ATR", high_series.to_numpy(), low_series.to_numpy(), close_series.to_numpy(), timeperiod=window
        )
        return _series_like(arr, close_series.index, f"atr_{window}")

    prev_close = close_series.shift(1).to_numpy()
    # 真实波幅 = max(high, prev_close) - min(low, prev_close)（等价于三值取最大口径）
    tr = np.maximum(high_series.to_numpy(), prev_close) - np.minimum(
        low_series.to_numpy(), prev_close
    )
    arr = _recursive_mean(tr, window, 1.0 / window)
    return _series_like(arr, close_series.index, f"atr_{window}")


#: 指标名 -> 函数（``get_indicator`` 用；新增指标只需在此登记并补 pandas 实现）
INDICATORS: dict[str, Callable[..., Any]] = {
    "sma": sma,
    "ema": ema,
    "rsi": rsi,
    "macd": macd,
    "std": std,
    "atr": atr,
}


def get_indicator(name: str, *, backend: str = "auto") -> Callable[..., Any]:
    """按名字取指标函数（可绑定实现来源），供策略/UI/测试统一调用。

    Args:
        name: ``sma/ema/rsi/macd/std/atr``。
        backend: ``auto/talib/pandas``，强制某一实现（AC-F2-04 对比两套结果用）。

    Example::

        rsi_pandas = get_indicator("rsi", backend="pandas")
        rsi_talib = get_indicator("rsi", backend="talib")   # 未安装时抛 IndicatorBackendError
    """

    normalized = str(name or "").strip().lower()
    try:
        function = INDICATORS[normalized]
    except KeyError as exc:
        raise KeyError(
            f"未知指标 {name!r}；可用指标：{'、'.join(INDICATOR_NAMES)}"
        ) from exc
    return partial(function, backend=backend)


def indicator_info() -> dict[str, Any]:
    """暴露指标层当前实现来源（M1-06 ⑤：设置页“TA-Lib 已安装/已使用兜底实现”）。

    Returns:
        ``{"backend", "talib_available", "talib_version", "import_error",
        "functions", "tolerance", "display"}``
    """

    if TALIB_AVAILABLE:
        display = f"TA-Lib {TALIB_VERSION}（优先使用 TA-Lib 实现）"
    else:
        display = "pandas 兜底实现（TA-Lib 未安装，接口与 TA-Lib 一致，误差 ≤ 1e-6）"
    return {
        "backend": INDICATOR_BACKEND,
        "talib_available": TALIB_AVAILABLE,
        "talib_version": TALIB_VERSION,
        "import_error": TALIB_IMPORT_ERROR,
        "functions": list(INDICATOR_NAMES),
        "tolerance": INDICATOR_TOLERANCE,
        "display": display,
    }

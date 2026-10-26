"""QuantDesk 数据源抽象、注册表与 MockProvider（任务 M1-01 / 契约冻结项 CF-1）。

职责
----
1. **DataProvider 抽象基类**：冻结 6 个方法（SPEC §6.1），返回列名与语义固定；
2. **ProviderRegistry**：注册/获取/主备降级 ``resolve``（借鉴 OpenBB provider 注册表 + a-stock-data 主备降级思想，重写实现）；
3. **MockProvider**：**完全离线、确定性**（固定随机种子）的示例数据，满足 ≥10 标的 × 3 年，
   保证无网络、未安装 akshare 时 F1–F9 全流程可演示（SPEC §11.2）。
4. **``build_provider``**：按 ``config.data.provider`` + ``fallbacks`` 组装主备链；akshare 尚未接入时自动降级 mock。

CF-1 返回列口径
---------------
``get_bars`` 必含列（顺序固定）::

    date, symbol, open, high, low, close, volume, amount

可选列：``adj_factor``、``turnover_rate``、``is_suspended``。
对不存在的标的 / 空区间返回**空 DataFrame（列齐全）**，不抛异常（AC-F1-04）。

Mock 数据口径
-------------
- 覆盖固定区间 ``2021-01-04 ~ 2026-12-31``（工作日日历），**与系统当前日期无关**，保证测试可复现；
- 13 只标的（10 只股票含 1 只 ST + 2 个指数 + 1 只创业板示例），价格按每标的固定种子随机游走；
- 生成结果按 symbol 缓存，重复调用完全一致（确定性）。

自检（quantdesk 目录）：``python -m data.provider``
"""

from __future__ import annotations

import hashlib
import inspect
import threading
from abc import ABC, abstractmethod
from datetime import date, datetime, time, timedelta
from typing import Any, ClassVar, Iterable, Literal

import numpy as np
import pandas as pd

from core.config import AppConfig, get_config
from core.logging import get_logger
from core.models import ProviderHealth, ProviderStatus, now_local

__all__ = [
    "BAR_COLUMNS",
    "OPTIONAL_BAR_COLUMNS",
    "ALL_BAR_COLUMNS",
    "DataProvider",
    "ProviderError",
    "ProviderNotFoundError",
    "ProviderUnavailableError",
    "ProviderRegistry",
    "MOCK_SYMBOLS",
    "MOCK_START",
    "MOCK_END",
    "MockProvider",
    "normalize_bars",
    "normalize_symbols",
    "get_registry",
    "register_provider",
    "reset_registry",
    "register_builtin_providers",
    "build_provider",
]

logger = get_logger("data.provider")

#: get_bars 必含列（CF-1 冻结顺序）
BAR_COLUMNS: tuple[str, ...] = (
    "date", "symbol", "open", "high", "low", "close", "volume", "amount",
)
#: get_bars 可选列（按此顺序追加）
OPTIONAL_BAR_COLUMNS: tuple[str, ...] = ("adj_factor", "turnover_rate", "is_suspended")
#: 全部行情列
ALL_BAR_COLUMNS: tuple[str, ...] = BAR_COLUMNS + OPTIONAL_BAR_COLUMNS


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class ProviderError(RuntimeError):
    """数据源相关错误（中文可操作提示）。"""


class ProviderNotFoundError(ProviderError):
    """数据源未注册。"""


class ProviderUnavailableError(ProviderError):
    """数据源不可用（主源 + 降级链全部失败）。"""


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def normalize_symbols(symbols: Iterable[str] | str | None) -> list[str]:
    """把标的参数统一成去重、大写的列表（``"600519.SH"`` / ``["..."]`` / ``None``）。"""
    if symbols is None:
        return []
    if isinstance(symbols, str):
        raw: list[str] = [symbols]
    else:
        raw = [str(item) for item in symbols]
    cleaned: list[str] = []
    for item in raw:
        text = str(item).strip().upper()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def _to_date(value: Any) -> date:
    """把 date/datetime/字符串统一成 ``datetime.date``。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def normalize_bars(data: pd.DataFrame | list[dict[str, Any]] | None) -> pd.DataFrame:
    """把行情数据规范化为 CF-1 口径（补列/排序/类型转换）。

    :param data: DataFrame 或记录列表；``None`` 视为空
    :return: 列顺序固定、``date`` 为 datetime 的 DataFrame；空数据也返回列齐全的空表
    :raises ProviderError: 缺少必含列（中文提示）
    """
    columns = list(BAR_COLUMNS) + list(OPTIONAL_BAR_COLUMNS)
    if data is None:
        return pd.DataFrame(columns=columns)
    frame = data.copy() if isinstance(data, pd.DataFrame) else pd.DataFrame(data)
    if frame.empty:
        return pd.DataFrame(columns=columns)
    missing = [name for name in BAR_COLUMNS if name not in frame.columns]
    if missing:
        raise ProviderError(f"行情数据缺列：{', '.join(missing)}（CF-1 要求至少包含 {list(BAR_COLUMNS)}）")
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"])
    frame["symbol"] = frame["symbol"].astype(str)
    for name in ("open", "high", "low", "close", "volume", "amount"):
        frame[name] = pd.to_numeric(frame[name], errors="coerce").fillna(0.0)
    for name in OPTIONAL_BAR_COLUMNS:
        if name not in frame.columns:
            frame[name] = False if name == "is_suspended" else np.nan
    frame["is_suspended"] = frame["is_suspended"].fillna(False).astype(bool)
    ordered = [name for name in ALL_BAR_COLUMNS if name in frame.columns]
    extras = [name for name in frame.columns if name not in ordered]
    return frame[ordered + extras].sort_values(["date", "symbol"], ignore_index=True)


# ---------------------------------------------------------------------------
# 数据源抽象（SPEC §6.1 冻结 6 方法）
# ---------------------------------------------------------------------------


class DataProvider(ABC):
    """数据源抽象基类（CF-1：6 个方法签名冻结，新增数据源只实现本类并注册）。

    实现约定：
    - **不抛未处理异常给调用方**（除参数非法）：网络/数据源异常应返回空 DataFrame 或空列表；
    - ``get_bars`` 必须返回 CF-1 列（用 :func:`normalize_bars` 兜底）；
    - ``health_check`` 返回 :class:`ProviderHealth`，用于主备降级。
    """

    #: 数据源名称（注册表键，如 "akshare"/"mock"）
    name: ClassVar[str] = "base"

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config

    # -- 冻结方法 ---------------------------------------------------------
    @abstractmethod
    def get_bars(
        self, symbols: list[str], start: date, end: date, freq: str = "1d"
    ) -> pd.DataFrame:
        """获取日线行情（必含 CF-1 列；无数据返回空表，不抛异常）。"""

    @abstractmethod
    def get_trading_calendar(self, start: date, end: date) -> list[date]:
        """获取交易日列表（升序）。"""

    @abstractmethod
    def get_symbols(self, kind: str = "STOCK") -> pd.DataFrame:
        """获取标的主数据（STOCK/INDEX）。"""

    @abstractmethod
    def get_snapshot(self, symbols: list[str]) -> pd.DataFrame:
        """获取最新快照（最新价/涨跌幅/快照时间）。"""

    @abstractmethod
    def get_fundamentals(
        self, symbols: list[str], report_date: date | None = None
    ) -> pd.DataFrame:
        """获取基本面快照（多因子策略用）。"""

    @abstractmethod
    def health_check(self) -> ProviderHealth:
        """健康检查（OK/DEGRADED/DOWN + 中文说明）。"""

    # -- 通用辅助 ---------------------------------------------------------
    def __repr__(self) -> str:  # pragma: no cover - 调试展示
        return f"<{type(self).__name__} name={self.name}>"


# ---------------------------------------------------------------------------
# 注册表（注册 / 获取 / 主备降级）
# ---------------------------------------------------------------------------


class ProviderRegistry:
    """数据源注册表（CF-1：``register`` / ``get`` / ``resolve``）。

    - ``register(name, cls)``：注册数据源类；重复注册同一类幂等，不同类需 ``replace=True``；
    - ``get(name)``：取实例（懒创建并缓存；未知名称抛中文异常）；
    - ``resolve(primary, fallbacks)``：按主源 → 降级链顺序做健康检查，返回首个可用实例；
      全部不可用抛 :class:`ProviderUnavailableError`（消息含每个数据源的失败原因）。
    """

    def __init__(self) -> None:
        self._classes: dict[str, type[DataProvider]] = {}
        self._instances: dict[str, DataProvider] = {}
        self._lock = threading.RLock()

    # -- 注册 -------------------------------------------------------------
    def register(self, name: str, cls: type[DataProvider], *, replace: bool = False) -> None:
        """注册数据源类（``name`` 为小写注册键）。"""
        key = str(name).strip().lower()
        if not key:
            raise ProviderError("数据源注册失败：名称不能为空")
        if not (inspect.isclass(cls) and issubclass(cls, DataProvider)):
            raise ProviderError(
                f"数据源注册失败：{cls!r} 必须是 DataProvider 的子类（实现 6 个冻结方法）"
            )
        with self._lock:
            existing = self._classes.get(key)
            if existing is not None and existing is not cls and not replace:
                raise ProviderError(
                    f"数据源注册失败：{key!r} 已由 {existing.__name__} 注册；"
                    f"如需替换请传 replace=True"
                )
            self._classes[key] = cls
            self._instances.pop(key, None)
        logger.debug("数据源注册：%s → %s", key, cls.__name__)

    def unregister(self, name: str) -> None:
        """注销数据源（不存在时忽略）。"""
        key = str(name).strip().lower()
        with self._lock:
            self._classes.pop(key, None)
            self._instances.pop(key, None)

    def is_registered(self, name: str) -> bool:
        """判断数据源是否已注册。"""
        return str(name).strip().lower() in self._classes

    def list_providers(self) -> list[str]:
        """返回已注册的数据源名称（排序）。"""
        with self._lock:
            return sorted(self._classes)

    def get_class(self, name: str) -> type[DataProvider]:
        """取数据源类（未注册抛中文异常）。"""
        key = str(name).strip().lower()
        with self._lock:
            cls = self._classes.get(key)
        if cls is None:
            raise ProviderNotFoundError(
                f"数据源 {name!r} 未注册；已注册：{self.list_providers()}（新增数据源请继承 DataProvider 并注册）"
            )
        return cls

    # -- 实例 -------------------------------------------------------------
    def get(self, name: str, config: AppConfig | None = None) -> DataProvider:
        """获取数据源实例（懒创建 + 缓存；``config`` 仅首次创建时生效）。"""
        key = str(name).strip().lower()
        cls = self.get_class(key)
        with self._lock:
            instance = self._instances.get(key)
            if instance is not None:
                return instance
        try:
            instance = cls(config=config) if config is not None else cls()
        except TypeError:
            # 兼容不接收 config 的第三方/测试实现
            instance = cls()  # type: ignore[call-arg]
        with self._lock:
            self._instances.setdefault(key, instance)
            return self._instances[key]

    def clear_instances(self) -> None:
        """清空实例缓存（注册表类不变；测试用）。"""
        with self._lock:
            self._instances.clear()

    # -- 主备降级 ---------------------------------------------------------
    def resolve(
        self,
        primary: str,
        fallbacks: list[str] | None = None,
        config: AppConfig | None = None,
    ) -> DataProvider:
        """按主源 → 降级链顺序解析可用数据源（CF-1 冻结语义）。

        :param primary: 主数据源名称
        :param fallbacks: 降级链（按顺序尝试）
        :param config: 传给数据源的配置
        :raises ProviderUnavailableError: 全部不可用
        """
        candidates: list[str] = []
        for name in [primary, *(fallbacks or [])]:
            key = str(name).strip().lower()
            if key and key not in candidates:
                candidates.append(key)
        errors: list[str] = []
        for index, name in enumerate(candidates):
            if not self.is_registered(name):
                errors.append(f"{name}（未注册）")
                logger.warning("数据源 %s 未注册，跳过", name)
                continue
            try:
                provider = self.get(name, config=config)
                health = provider.health_check()
            except Exception as exc:  # 数据源实现异常不阻断降级
                errors.append(f"{name}（{type(exc).__name__}: {exc}）")
                logger.warning("数据源 %s 健康检查异常：%s", name, exc)
                continue
            if health.status is not ProviderStatus.DOWN:
                if index > 0:
                    logger.warning("数据源降级：主源 %s 不可用，已切换到 %s（%s）", primary, name, health.message)
                return provider
            errors.append(f"{name}（{health.status.value}：{health.last_error or health.message}）")
            logger.warning("数据源 %s 不可用：%s", name, health.message)
        raise ProviderUnavailableError(
            "数据源不可用：主源与降级链均失败（" + "；".join(errors) + "）。"
            "请检查网络/数据源配置，或把 config.yaml 的 data.provider 改为 mock 使用离线演示数据"
        )


# ---------------------------------------------------------------------------
# MockProvider（离线确定性示例数据）
# ---------------------------------------------------------------------------

#: Mock 行情固定覆盖区间（与系统日期无关，保证测试可复现）
MOCK_START: date = date(2021, 1, 4)
MOCK_END: date = date(2026, 12, 31)

#: Mock 标的主数据（10 只股票 + 1 只 ST + 2 个指数；覆盖主板/创业板/科创板/北交所）
MOCK_SYMBOLS: tuple[dict[str, Any], ...] = (
    {"symbol": "600519.SH", "code": "600519", "name": "贵州茅台", "exchange": "SH", "board": "MAIN", "kind": "STOCK", "is_st": False, "list_date": date(2001, 8, 27)},
    {"symbol": "000001.SZ", "code": "000001", "name": "平安银行", "exchange": "SZ", "board": "MAIN", "kind": "STOCK", "is_st": False, "list_date": date(1991, 4, 3)},
    {"symbol": "300750.SZ", "code": "300750", "name": "宁德时代", "exchange": "SZ", "board": "GEM", "kind": "STOCK", "is_st": False, "list_date": date(2018, 6, 11)},
    {"symbol": "688981.SH", "code": "688981", "name": "中芯国际", "exchange": "SH", "board": "STAR", "kind": "STOCK", "is_st": False, "list_date": date(2020, 7, 16)},
    {"symbol": "601318.SH", "code": "601318", "name": "中国平安", "exchange": "SH", "board": "MAIN", "kind": "STOCK", "is_st": False, "list_date": date(2007, 3, 1)},
    {"symbol": "000858.SZ", "code": "000858", "name": "五粮液", "exchange": "SZ", "board": "MAIN", "kind": "STOCK", "is_st": False, "list_date": date(1998, 4, 27)},
    {"symbol": "002594.SZ", "code": "002594", "name": "比亚迪", "exchange": "SZ", "board": "MAIN", "kind": "STOCK", "is_st": False, "list_date": date(2011, 6, 30)},
    {"symbol": "600036.SH", "code": "600036", "name": "招商银行", "exchange": "SH", "board": "MAIN", "kind": "STOCK", "is_st": False, "list_date": date(2002, 4, 9)},
    {"symbol": "000004.SZ", "code": "000004", "name": "*ST国华", "exchange": "SZ", "board": "MAIN", "kind": "STOCK", "is_st": True, "list_date": date(1991, 1, 14)},
    {"symbol": "601899.SH", "code": "601899", "name": "紫金矿业", "exchange": "SH", "board": "MAIN", "kind": "STOCK", "is_st": False, "list_date": date(2008, 4, 25)},
    {"symbol": "000300.SH", "code": "000300", "name": "沪深300", "exchange": "SH", "board": "MAIN", "kind": "INDEX", "is_st": False, "list_date": date(2005, 4, 8)},
    {"symbol": "000001.SH", "code": "000001", "name": "上证指数", "exchange": "SH", "board": "MAIN", "kind": "INDEX", "is_st": False, "list_date": date(1991, 7, 15)},
    {"symbol": "899050.BJ", "code": "899050", "name": "北证50", "exchange": "BJ", "board": "BSE", "kind": "INDEX", "is_st": False, "list_date": date(2022, 11, 21)},
)
_MOCK_BY_SYMBOL: dict[str, dict[str, Any]] = {item["symbol"]: item for item in MOCK_SYMBOLS}
#: Mock 最近一期基本面报告期（固定，保证可复现）
_MOCK_REPORT_DATE: date = date(2025, 12, 31)


class MockProvider(DataProvider):
    """离线确定性示例数据源（≥10 标的 × 3 年；固定随机种子，复现结果一致）。

    - 无网络、无 akshare 依赖，用于演示与测试兜底（SPEC §11.2）；
    - 数据区间固定为 ``2021-01-04 ~ 2026-12-31``，**不随系统日期变化**；
    - 额外提供 ``get_adjust_events``（除权除息事件）供数据层落库（非 CF-1 冻结方法）。
    """

    name: ClassVar[str] = "mock"

    def __init__(self, config: AppConfig | None = None) -> None:
        super().__init__(config)
        self._cache: dict[str, pd.DataFrame] = {}
        self._lock = threading.RLock()
        self._forced_status: ProviderStatus | None = None
        self._forced_error: str | None = None

    # -- 生成 -------------------------------------------------------------
    @staticmethod
    def _seed_for(symbol: str) -> int:
        """按标的名生成固定种子（与调用顺序无关，保证确定性）。"""
        digest = hashlib.sha1(symbol.encode("utf-8")).hexdigest()
        return int(digest[:8], 16)

    def _series(self, symbol: str) -> pd.DataFrame:
        """生成/缓存单只标的的完整 Mock 日线（固定区间）。"""
        with self._lock:
            cached = self._cache.get(symbol)
            if cached is not None:
                return cached
            meta = _MOCK_BY_SYMBOL.get(symbol)
            if meta is None:
                empty = pd.DataFrame(columns=list(ALL_BAR_COLUMNS))
                return empty  # 未知标的返回空表，由调用方跳过
            rng = np.random.default_rng(self._seed_for(symbol))
            dates = pd.bdate_range(MOCK_START, MOCK_END)
            count = len(dates)
            is_index = meta["kind"] == "INDEX"
            # 基础价格与波动：指数点位高、波动小；个股价格低、波动大
            if is_index:
                base_price = 3000.0 + (self._seed_for(symbol) % 700)
                drift, vol = 0.00015, 0.008
                limit = 0.02
            else:
                base_price = 8.0 + (self._seed_for(symbol) % 90)
                drift, vol = 0.0002, 0.018
                limit = 0.095
            returns = np.clip(rng.normal(drift, vol, count), -limit, limit)
            close = base_price * np.exp(np.cumsum(np.log1p(returns)))
            open_price = close * (1.0 + rng.normal(0.0, 0.004, count))
            high = np.maximum(open_price, close) * (1.0 + np.abs(rng.normal(0.0, 0.006, count)))
            low = np.minimum(open_price, close) * (1.0 - np.abs(rng.normal(0.0, 0.006, count)))
            round_price = lambda arr: np.round(np.maximum(arr, 0.01), 2)  # noqa: E731
            open_price, high, low, close = (
                round_price(open_price), round_price(high), round_price(low), round_price(close),
            )
            # 成交量/成交额（指数成交额为 0）
            base_volume = 5_000_000 + (self._seed_for(symbol) % 20_000_000)
            volume = np.maximum((base_volume * rng.lognormal(0.0, 0.35, count)).astype(np.int64), 1)
            amount = np.round(volume * ((open_price + close) / 2.0), 2) if not is_index else np.zeros(count)
            # 后复权因子：个股每年首个交易日除权（×1.02），指数恒为 1
            adj = np.ones(count)
            if not is_index:
                for year in range(2022, 2027):
                    first_day = np.searchsorted(dates.values, np.datetime64(f"{year}-01-04"))
                    adj[first_day:] *= 1.02
            # 换手率（%）
            shares = 3e8 + (self._seed_for(symbol) % 15) * 1e8
            turnover = np.round(volume / shares * 100.0, 4) if not is_index else np.zeros(count)

            frame = pd.DataFrame(
                {
                    "trade_date": dates,
                    "symbol": symbol,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                    "amount": amount,
                    "adj_factor": np.round(adj, 6),
                    "turnover_rate": turnover,
                    "is_suspended": False,
                }
            )
            frame["prev_close"] = frame["close"].shift(1)
            frame["source"] = "mock"
            self._cache[symbol] = frame
            return frame

    def _slice(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        """截取单只标的在区间内的行情（未知标的返回空表）。"""
        frame = self._series(symbol)
        if frame.empty:
            return pd.DataFrame(columns=list(ALL_BAR_COLUMNS))
        mask = (frame["trade_date"] >= pd.Timestamp(start)) & (frame["trade_date"] <= pd.Timestamp(end))
        return frame.loc[mask].copy()

    # -- CF-1 方法实现 ----------------------------------------------------
    def get_bars(
        self, symbols: list[str], start: date, end: date, freq: str = "1d"
    ) -> pd.DataFrame:
        """获取 Mock 日线（CF-1 列；未知标的/空区间返回空表）。"""
        if freq != "1d":
            raise ValueError(f"MockProvider 仅支持日频 freq='1d'（收到 {freq!r}）；分钟级数据不在 MVP 范围")
        symbol_list = normalize_symbols(symbols)
        start_date, end_date = _to_date(start), _to_date(end)
        if start_date > end_date:
            return pd.DataFrame(columns=list(ALL_BAR_COLUMNS))
        frames = [self._slice(symbol, start_date, end_date) for symbol in symbol_list]
        frames = [frame for frame in frames if not frame.empty]
        if not frames:
            return pd.DataFrame(columns=list(ALL_BAR_COLUMNS))
        merged = pd.concat(frames, ignore_index=True)
        merged = merged.rename(columns={"trade_date": "date"})
        return normalize_bars(merged)

    def get_trading_calendar(self, start: date, end: date) -> list[date]:
        """获取 Mock 交易日历（工作日近似；真实节假日由 akshare 交易日历补充）。"""
        start_date, end_date = _to_date(start), _to_date(end)
        if start_date > end_date:
            return []
        days = pd.bdate_range(start_date, end_date)
        return [timestamp.date() for timestamp in days]

    def get_symbols(self, kind: str = "STOCK") -> pd.DataFrame:
        """获取 Mock 标的主数据（kind=STOCK/INDEX）。"""
        target = (kind or "STOCK").strip().upper()
        rows = [
            {
                "symbol": item["symbol"],
                "code": item["code"],
                "name": item["name"],
                "exchange": item["exchange"],
                "board": item["board"],
                "kind": item["kind"],
                "lot_size": 100,
                "tick_size": 0.01,
                "price_limit_pct": (20.0 if item["board"] in ("GEM", "STAR") else 30.0 if item["board"] == "BSE" else 5.0 if item["is_st"] else 10.0),
                "is_st": item["is_st"],
                "list_date": item["list_date"],
                "delist_date": None,
                "trade_status": "NORMAL",
                "updated_at": now_local(),
            }
            for item in MOCK_SYMBOLS
            if target in ("ALL", "*") or item["kind"] == target
        ]
        columns = [
            "symbol", "code", "name", "exchange", "board", "kind", "lot_size", "tick_size",
            "price_limit_pct", "is_st", "list_date", "delist_date", "trade_status", "updated_at",
        ]
        return pd.DataFrame(rows, columns=columns)

    def get_snapshot(self, symbols: list[str]) -> pd.DataFrame:
        """获取 Mock 最新快照（取固定区间内 ≤ 今日的最后一个交易日）。"""
        symbol_list = normalize_symbols(symbols)
        trade_day = min(now_local().date(), MOCK_END)
        rows: list[dict[str, Any]] = []
        for symbol in symbol_list:
            frame = self._series(symbol)
            if frame.empty:
                continue
            available = frame[frame["trade_date"] <= pd.Timestamp(trade_day)]
            if available.empty:
                continue
            last = available.iloc[-1]
            prev_close = float(last["prev_close"]) if pd.notna(last["prev_close"]) else float(last["close"])
            last_price = float(last["close"])
            change = round(last_price - prev_close, 2)
            change_pct = round(change / prev_close * 100.0, 4) if prev_close else 0.0
            rows.append(
                {
                    "symbol": symbol,
                    "name": _MOCK_BY_SYMBOL[symbol]["name"],
                    "last": last_price,
                    "prev_close": prev_close,
                    "change": change,
                    "change_pct": change_pct,
                    "volume": int(last["volume"]),
                    "amount": float(last["amount"]),
                    "ts": datetime.combine(last["trade_date"].date(), time(15, 0), tzinfo=now_local().tzinfo),
                }
            )
        columns = ["symbol", "name", "last", "prev_close", "change", "change_pct", "volume", "amount", "ts"]
        return pd.DataFrame(rows, columns=columns)

    def get_fundamentals(
        self, symbols: list[str], report_date: date | None = None
    ) -> pd.DataFrame:
        """获取 Mock 基本面快照（确定性；默认报告期 2025-12-31）。"""
        symbol_list = normalize_symbols(symbols)
        target_report = _to_date(report_date) if report_date is not None else _MOCK_REPORT_DATE
        rows: list[dict[str, Any]] = []
        for symbol in symbol_list:
            meta = _MOCK_BY_SYMBOL.get(symbol)
            if meta is None:
                continue
            rng = np.random.default_rng(self._seed_for(symbol) + 7)
            rows.append(
                {
                    "symbol": symbol,
                    "report_date": target_report,
                    "ann_date": target_report + timedelta(days=90),
                    "pe_ttm": round(float(rng.uniform(5.0, 60.0)), 2),
                    "pb": round(float(rng.uniform(0.6, 12.0)), 2),
                    "ps_ttm": round(float(rng.uniform(0.5, 20.0)), 2),
                    "roe": round(float(rng.uniform(-5.0, 35.0)), 2),
                    "total_mv": round(float(rng.uniform(5e9, 2.5e12)), 2),
                    "revenue_yoy": round(float(rng.normal(8.0, 18.0)), 2),
                    "profit_yoy": round(float(rng.normal(10.0, 25.0)), 2),
                    "source": "mock",
                }
            )
        columns = [
            "symbol", "report_date", "ann_date", "pe_ttm", "pb", "ps_ttm", "roe",
            "total_mv", "revenue_yoy", "profit_yoy", "source",
        ]
        return pd.DataFrame(rows, columns=columns)

    def get_adjust_events(self, symbols: list[str] | None = None) -> pd.DataFrame:
        """获取 Mock 除权除息事件（非 CF-1 冻结方法，供数据层落 ``adjust_events``）。

        口径：每只股票每年 6 月首个工作日现金分红，金额按固定种子确定性生成；指数无事件。
        """
        symbol_list = normalize_symbols(symbols) if symbols is not None else [
            item["symbol"] for item in MOCK_SYMBOLS if item["kind"] == "STOCK"
        ]
        rows: list[dict[str, Any]] = []
        for symbol in symbol_list:
            meta = _MOCK_BY_SYMBOL.get(symbol)
            if meta is None or meta["kind"] != "STOCK":
                continue
            rng = np.random.default_rng(self._seed_for(symbol) + 13)
            for year in range(2022, 2027):
                ex_date = pd.bdate_range(start=f"{year}-06-01", periods=1)[0].date()
                rows.append(
                    {
                        "symbol": symbol,
                        "ex_date": ex_date,
                        "event_type": "DIVIDEND",
                        "cash_per_share": round(float(rng.uniform(0.1, 2.5)), 3),
                        "stock_ratio": None,
                        "record_date": ex_date - timedelta(days=1),
                        "source": "mock",
                    }
                )
        columns = ["symbol", "ex_date", "event_type", "cash_per_share", "stock_ratio", "record_date", "source"]
        return pd.DataFrame(rows, columns=columns)

    def health_check(self) -> ProviderHealth:
        """Mock 数据源健康检查（默认 OK；测试可 ``set_health`` 模拟降级/不可用）。"""
        started = now_local()
        if self._forced_status is not None:
            return ProviderHealth(
                name=self.name,
                status=self._forced_status,
                last_error=self._forced_error,
                latency_ms=round((now_local() - started).total_seconds() * 1000, 2),
                checked_at=started,
                message=f"Mock 数据源状态被显式设置为 {self._forced_status.value}",
            )
        return ProviderHealth(
            name=self.name,
            status=ProviderStatus.OK,
            last_error=None,
            latency_ms=round((now_local() - started).total_seconds() * 1000, 2),
            checked_at=started,
            message="Mock 数据源正常（离线演示模式，确定性示例数据）",
        )

    # -- 测试辅助 ---------------------------------------------------------
    def set_health(self, status: ProviderStatus | str | None, error: str | None = None) -> None:
        """测试辅助：强制健康状态（``None`` 恢复正常）。"""
        if status is None:
            self._forced_status = None
            self._forced_error = None
            return
        self._forced_status = status if isinstance(status, ProviderStatus) else ProviderStatus(str(status).upper())
        self._forced_error = error


# ---------------------------------------------------------------------------
# 全局注册表与组装
# ---------------------------------------------------------------------------

_registry: ProviderRegistry | None = None
_registry_lock = threading.RLock()


def register_builtin_providers(registry: ProviderRegistry) -> None:
    """注册内置数据源。

    - ``mock``：始终可用（离线演示 / 兜底）；
    - ``akshare``：延迟导入（可选依赖），导入失败或未安装时只跳过注册，不影响 mock 可用。
    """
    if not registry.is_registered(MockProvider.name):
        registry.register(MockProvider.name, MockProvider)
    if not registry.is_registered("akshare"):
        try:
            from data.akshare_provider import AkshareProvider

            registry.register(AkshareProvider.name, AkshareProvider)
            logger.debug("数据源注册：akshare（真实 A 股数据）")
        except Exception as exc:  # 可选依赖：akshare 未安装/导入失败时静默跳过
            logger.debug("akshare 数据源未注册（可选依赖不可用）：%s", exc)


def get_registry() -> ProviderRegistry:
    """获取全局数据源注册表单例（含内置 mock）。"""
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = ProviderRegistry()
            register_builtin_providers(_registry)
        return _registry


def reset_registry() -> None:
    """重置全局注册表（测试隔离用）。"""
    global _registry
    with _registry_lock:
        _registry = None


def register_provider(name: str, cls: type[DataProvider], *, replace: bool = False) -> None:
    """向全局注册表注册数据源（便捷函数）。"""
    get_registry().register(name, cls, replace=replace)


def build_provider(config: AppConfig | None = None, registry: ProviderRegistry | None = None) -> DataProvider:
    """按配置组装主备数据源（``data.provider`` + ``data.fallbacks``）。

    - 主源与降级链全部不可用时抛 :class:`ProviderUnavailableError`；
    - 若主源 ``akshare`` 尚未实现（M1-02 前），自动降级到 ``mock`` 并记 WARN（离线演示模式）。
    """
    cfg = config or get_config()
    target_registry = registry or get_registry()
    primary = str(cfg.data.provider).strip().lower()
    fallbacks = [str(item).strip().lower() for item in cfg.data.fallbacks]
    if not target_registry.is_registered(primary):
        logger.warning(
            "数据源 %s 尚未注册/实现，自动降级为 mock 离线演示数据（仅演示，不可用于真实回测）", primary
        )
        if target_registry.is_registered(MockProvider.name):
            return target_registry.get(MockProvider.name, config=cfg)
    try:
        return target_registry.resolve(primary, fallbacks, config=cfg)
    except ProviderUnavailableError:
        if target_registry.is_registered(MockProvider.name):
            logger.warning("所有数据源不可用，最终降级到 mock 离线演示数据（仅演示，不可用于真实回测）")
            return target_registry.get(MockProvider.name, config=cfg)
        raise


# ---------------------------------------------------------------------------
# 自检（quantdesk 目录：``python -m data.provider``）
# ---------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    from core.config import load_config, reset_config, set_config

    cfg = load_config()
    cfg.data.provider = "mock"
    cfg.data.universe = ["600519.SH", "000001.SZ", "300750.SZ", "000300.SH"]
    set_config(cfg)
    try:
        registry = ProviderRegistry()
        register_builtin_providers(registry)

        # 1) 注册表：注册/去重/获取/未知名称中文异常
        assert registry.list_providers() == ["mock"]
        registry.register("mock", MockProvider)  # 幂等
        assert registry.is_registered("mock")
        try:
            registry.register("mock", type("Other", (DataProvider,), {  # type: ignore[abstract]
                "get_bars": lambda self, *a, **k: pd.DataFrame(),
                "get_trading_calendar": lambda self, s, e: [],
                "get_symbols": lambda self, kind="STOCK": pd.DataFrame(),
                "get_snapshot": lambda self, symbols: pd.DataFrame(),
                "get_fundamentals": lambda self, symbols, report_date=None: pd.DataFrame(),
                "health_check": lambda self: ProviderHealth(name="x", status=ProviderStatus.OK),
            }))
        except ProviderError as exc:
            assert "replace=True" in str(exc)
        else:
            raise AssertionError("重复注册未拦截")
        try:
            registry.get("not-exists")
        except ProviderNotFoundError as exc:
            assert "未注册" in str(exc)
        else:
            raise AssertionError("未知数据源未拦截")

        # 2) MockProvider：确定性 / 列口径 / ≥10 标的
        provider = registry.get("mock", config=cfg)
        symbols = ["600519.SH", "000001.SZ", "300750.SZ", "688981.SH", "601318.SH", "000858.SZ",
                   "002594.SZ", "600036.SH", "000004.SZ", "601899.SH", "000300.SH", "000001.SH", "899050.BJ"]
        bars_first = provider.get_bars(symbols, date(2023, 1, 1), date(2023, 12, 31))
        bars_second = provider.get_bars(symbols, date(2023, 1, 1), date(2023, 12, 31))
        assert not bars_first.empty
        assert list(bars_first.columns[: len(BAR_COLUMNS)]) == list(BAR_COLUMNS)
        assert set(symbols) <= set(bars_first["symbol"].unique())
        pd.testing.assert_frame_equal(bars_first, bars_second, check_like=False)  # 确定性
        assert len(provider.get_symbols("STOCK")) == 10 and len(provider.get_symbols("INDEX")) == 3

        # 3) 空/未知标的：空 DataFrame（不抛异常），列齐全（AC-F1-04）
        empty = provider.get_bars(["000000.XX"], date(2023, 1, 1), date(2023, 12, 31))
        assert empty.empty and list(empty.columns[: len(BAR_COLUMNS)]) == list(BAR_COLUMNS)
        reversed_range = provider.get_bars(symbols, date(2023, 12, 31), date(2023, 1, 1))
        assert reversed_range.empty

        # 4) 交易日历 / 快照 / 基本面
        calendar = provider.get_trading_calendar(date(2023, 1, 1), date(2023, 1, 31))
        assert calendar and all(isinstance(day, date) for day in calendar)
        assert calendar[0] == date(2023, 1, 2) and date(2023, 1, 1) not in calendar  # 元旦周一?（工作日近似）
        snapshot = provider.get_snapshot(["600519.SH", "000300.SH", "000000.XX"])
        assert len(snapshot) == 2 and {"symbol", "last", "change_pct", "ts"} <= set(snapshot.columns)
        fundamentals = provider.get_fundamentals(["600519.SH", "000001.SZ"])
        assert len(fundamentals) == 2 and {"pe_ttm", "pb", "roe", "total_mv"} <= set(fundamentals.columns)
        assert fundamentals["report_date"].iloc[0] == _MOCK_REPORT_DATE
        adjust_events = provider.get_adjust_events(["600519.SH"])
        assert not adjust_events.empty and set(adjust_events["event_type"]) == {"DIVIDEND"}

        # 5) 健康检查 + 主备降级
        assert provider.health_check().status is ProviderStatus.OK
        provider.set_health(ProviderStatus.DOWN, "模拟数据源故障")
        assert provider.health_check().status is ProviderStatus.DOWN
        try:
            registry.resolve("mock", [], config=cfg)  # 唯一可用源 DOWN → 应抛异常
        except ProviderUnavailableError as exc:
            assert "故障" in str(exc)
        else:
            raise AssertionError("数据源 DOWN 时 resolve 未抛异常")
        provider.set_health(None)
        assert registry.resolve("mock", [], config=cfg).name == "mock"  # 恢复后可用
        # 主源未注册（akshare 未接入）→ 自动降级 mock
        assert build_provider(cfg, registry).name == "mock"
        # 全部不可用 → 中文异常
        dead_registry = ProviderRegistry()
        dead_registry.register("mock", MockProvider)
        dead_registry.get("mock", config=cfg).set_health(ProviderStatus.DOWN, "故障")
        try:
            dead_registry.resolve("mock", ["not-exists"], config=cfg)
        except ProviderUnavailableError as exc:
            assert "数据源不可用" in str(exc) and "故障" in str(exc)
        else:
            raise AssertionError("全部数据源不可用未抛异常")

        print(
            "data/provider.py 自检通过：6 方法抽象/注册表(注册·去重·未知异常)/Mock 确定性(13 标的×6 年)/"
            "CF-1 列口径/空表兜底/交易日历·快照·基本面/健康检查与主备降级"
        )
    finally:
        reset_registry()
        reset_config()

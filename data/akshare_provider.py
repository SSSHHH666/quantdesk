"""QuantDesk 真实 A 股数据源：akshare Provider（任务 M1-02 / 契约 CF-1）。

能力
----
实现 :class:`data.provider.DataProvider` 的 6 个冻结方法（SPEC §6.1）：

1. ``get_bars``：日线（**不复权价 + ``adj_factor``**；复权由 ``BarCache.load`` 统一计算，
   另提供 ``get_bars_adjusted`` 直接取 hfq/qfq/none 序列）。主备链：
   **新浪 ``stock_zh_a_daily``（主）→ 腾讯 ``stock_zh_a_hist_tx``（备）→ Mock（兜底）**；
   东财 ``push2his.eastmoney.com`` 接口在当前网络全部 ProxyError（R4 Lead 实测），不参与主备链；
   指数用新浪 ``stock_zh_index_daily``（主）→ 腾讯 ``stock_zh_index_daily_tx``（备）。
2. ``get_trading_calendar``：新浪交易日历 ``tool_trade_date_hist_sina``（缓存 1 小时）。
3. ``get_symbols``：沪深交易所股票列表（含上市日期/板块/ST 标记）+ 常用指数。
4. ``get_snapshot``：东财实时快照为主、新浪全市场快照为备、最近收盘价为最后兜底。
5. ``get_fundamentals``：能取多少算多少（东财快照的 PE/PB/总市值；缺失字段为 NaN）。
6. ``get_adjust_events``：东财分红送配详情，映射为 ``adjust_events`` 结构。

工程约束（Lead R3 要求）
------------------------
- **限流**：每次网络请求前按 ``config.data.request_interval_ms``（默认 300ms）节流；
- **重试**：``config.data.max_retries``（默认 3）次，指数退避（上限 5s）；
- **降级**：重试后仍失败 → 记 WARNING 并返回 :class:`MockProvider` 离线演示数据，**不抛给 UI**；
- **健康检查**：真实探测（带 10 秒超时，避免网络挂起阻塞 UI），失败返回 ``DOWN``；
- **中文错误**：所有异常/日志/健康说明均为中文；
- 网络请求一律通过 :meth:`AkshareProvider._call`，便于统一限流/重试/错误处理。

说明
----
- 本模块不直接依赖 ``BarCache``（避免 data 包内部循环依赖）；「缓存优先、缺区间才拉网络、
  拉回写缓存」由 ``data/cache.py::BarCache.update`` + ``scripts/fetch_data.py`` 编排。
- 数据源注册见 ``data/provider.py::register_builtin_providers``（名称为 ``akshare``）；
  akshare 未安装时注册被跳过，系统仍可用 mock 运行。

自检（quantdesk 目录，需要网络；断网时自动验证降级路径）：``python -m data.akshare_provider``
"""

from __future__ import annotations

import os

# 关闭 akshare 内部的 tqdm 进度条，避免污染脚本/日志输出（必须在 import akshare 之前设置）
os.environ.setdefault("TQDM_DISABLE", "1")

import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, ClassVar

import pandas as pd

from core.config import AppConfig, get_config
from core.logging import get_logger
from core.models import ProviderHealth, ProviderStatus, now_local
from data.provider import (
    ALL_BAR_COLUMNS,
    DataProvider,
    MockProvider,
    ProviderError,
    normalize_bars,
    normalize_symbols,
)

# 可选依赖：akshare 未安装时本模块仍可 import（provider 不注册，系统用 mock 兜底）
try:  # pragma: no cover - 取决于环境
    import akshare as ak

    AKSHARE_AVAILABLE = True
    AKSHARE_IMPORT_ERROR: str | None = None
except Exception as _exc:  # pragma: no cover
    ak = None  # type: ignore[assignment]
    AKSHARE_AVAILABLE = False
    AKSHARE_IMPORT_ERROR = str(_exc)

__all__ = ["AkshareProvider", "AKSHARE_AVAILABLE", "AKSHARE_IMPORT_ERROR", "KNOWN_INDEX_SYMBOLS"]

logger = get_logger("data.akshare_provider")

#: 常用指数（行情/回测基准）；指数无复权概念，``adj_factor`` 恒为 1
KNOWN_INDEX_SYMBOLS: frozenset[str] = frozenset(
    {
        "000001.SH",  # 上证指数
        "000300.SH",  # 沪深 300
        "000905.SH",  # 中证 500
        "000852.SH",  # 中证 1000
        "000016.SH",  # 上证 50
        "399001.SZ",  # 深证成指
        "399005.SZ",  # 中小板指
        "399006.SZ",  # 创业板指
        "899050.BJ",  # 北证 50
    }
)

#: get_symbols 返回列（与 MockProvider 一致）
_SYMBOL_COLUMNS: list[str] = [
    "symbol", "code", "name", "exchange", "board", "kind", "lot_size", "tick_size",
    "price_limit_pct", "is_st", "list_date", "delist_date", "trade_status", "updated_at",
]

#: 基本面返回列（与 MockProvider 一致）
_FUNDAMENTAL_COLUMNS: list[str] = [
    "symbol", "report_date", "ann_date", "pe_ttm", "pb", "ps_ttm", "roe",
    "total_mv", "revenue_yoy", "profit_yoy", "source",
]

#: 快照返回列
_SNAPSHOT_COLUMNS: list[str] = [
    "symbol", "name", "last", "prev_close", "change", "change_pct", "volume", "amount", "ts",
]

#: 除权除息返回列
_ADJUST_EVENT_COLUMNS: list[str] = [
    "symbol", "ex_date", "event_type", "cash_per_share", "stock_ratio", "record_date", "source",
]


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def _as_date(value: Any) -> date:
    """把 date/datetime/字符串统一成 ``datetime.date``。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def _to_float(value: Any) -> float | None:
    """把 akshare 返回值转成 float（处理空串/NaN/带千分位/百分号），失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return None if pd.isna(value) else float(value)
    text = str(value).strip().replace(",", "").replace("%", "")
    if not text or text in ("-", "--", "None", "nan", "NaN"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    return None if number is None else int(round(number))


def _split_symbol(symbol: str) -> tuple[str, str]:
    """拆分 ``600519.SH`` → ``("600519", "SH")``；格式非法抛中文异常。"""
    text = str(symbol).strip().upper()
    if "." not in text:
        raise ProviderError(f"标的代码 {symbol!r} 格式非法：应为「6 位代码.交易所」，如 600519.SH")
    code, exchange = text.split(".", 1)
    if len(code) != 6 or not code.isdigit() or exchange not in ("SH", "SZ", "BJ"):
        raise ProviderError(f"标的代码 {symbol!r} 格式非法：应为 6 位数字 + SH/SZ/BJ（如 600519.SH）")
    return code, exchange


def _is_index_symbol(symbol: str) -> bool:
    """判断是否为指数（常用指数名单 + 板块编码规则）。"""
    code, exchange = _split_symbol(symbol)
    if f"{code}.{exchange}" in KNOWN_INDEX_SYMBOLS:
        return True
    # 沪市 000xxx 为指数（上证指数等）；深市 399xxx 为指数；北交所 899xxx 为指数
    return (exchange == "SH" and code.startswith("000")) or (
        exchange == "SZ" and code.startswith("399")
    ) or (exchange == "BJ" and code.startswith("899"))


def _sina_symbol(symbol: str) -> str:
    """``600519.SH`` → 新浪格式 ``sh600519``。"""
    code, exchange = _split_symbol(symbol)
    return f"{exchange.lower()}{code}"


def _yyyymmdd(value: date) -> str:
    """日期 → akshare 需要的 ``YYYYMMDD`` 字符串。"""
    return value.strftime("%Y%m%d")


def _board_for(code: str, exchange: str, name: str = "") -> tuple[str, float]:
    """按代码前缀推断板块与涨跌幅限制（%，含 ST 调整；SPEC §4.3）。"""
    is_st = "ST" in (name or "").upper()
    if exchange == "BJ" or code.startswith(("4", "8", "920")):
        return "BSE", 30.0
    if exchange == "SH" and code.startswith(("688", "689")):
        return "STAR", 20.0
    if exchange == "SZ" and code.startswith(("300", "301")):
        return "GEM", 20.0
    return "MAIN", (5.0 if is_st else 10.0)


def _symbol_from_code(code: str) -> str | None:
    """6 位代码 → 带交易所后缀的标的（如 ``600519`` → ``600519.SH``）；无法识别返回 ``None``。"""
    digits = "".join(ch for ch in str(code) if ch.isdigit())
    if len(digits) != 6:
        return None
    if digits.startswith(("4", "8", "920")):
        exchange = "BJ"
    elif digits.startswith(("6", "9")):
        exchange = "SH"
    else:
        exchange = "SZ"
    return f"{digits}.{exchange}"


def _lot_size_for(code: str, exchange: str) -> int:
    """买入最小数量：科创板 200 股起，其余 100 股（SPEC §4.4）。"""
    if exchange == "SH" and code.startswith(("688", "689")):
        return 200
    return 100


# ---------------------------------------------------------------------------
# AkshareProvider
# ---------------------------------------------------------------------------


class AkshareProvider(DataProvider):
    """akshare 真实 A 股数据源（实现 CF-1 六方法，失败自动降级 MockProvider）。"""

    name: ClassVar[str] = "akshare"

    #: 健康探测超时（秒）——避免网络挂起导致 UI 卡死
    HEALTH_TIMEOUT_SECONDS: float = 10.0
    #: 健康检查结果缓存（秒）
    HEALTH_CACHE_SECONDS: float = 60.0
    #: 股票列表缓存（秒）
    SYMBOLS_CACHE_SECONDS: float = 24 * 3600
    #: 交易日历缓存（秒）
    CALENDAR_CACHE_SECONDS: float = 3600.0
    #: 实时快照缓存（秒）
    SNAPSHOT_CACHE_SECONDS: float = 60.0
    #: 复权因子缓存（秒）
    FACTOR_CACHE_SECONDS: float = 6 * 3600.0
    #: 除权除息事件缓存（秒）
    ADJUST_EVENT_CACHE_SECONDS: float = 24 * 3600.0

    def __init__(self, config: AppConfig | None = None) -> None:
        super().__init__(config)
        if self.config is None:
            try:
                self.config = get_config()
            except Exception:  # pragma: no cover - 配置不可用时用默认参数
                self.config = None
        interval_ms = self.config.data.request_interval_ms if self.config else 300
        self._interval_seconds = max(float(interval_ms), 0.0) / 1000.0
        self._max_retries = int(self.config.data.max_retries) if self.config else 3
        self._lock = threading.RLock()
        self._last_request_ts = 0.0
        self._last_error: str | None = None
        self._degraded_count = 0
        self._last_degraded_at: datetime | None = None
        self._health_override: ProviderStatus | None = None
        self._health_override_error: str | None = None
        self._health_cache: tuple[float, ProviderHealth] | None = None
        self._cache: dict[str, tuple[float, Any]] = {}
        #: 降级兜底数据源（离线演示模式）
        self._fallback = MockProvider(self.config)

    # -- 缓存 -------------------------------------------------------------
    def _cache_get(self, key: str, ttl: float) -> Any | None:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            timestamp, value = entry
            if time.monotonic() - timestamp > ttl:
                self._cache.pop(key, None)
                return None
            return value

    def _cache_set(self, key: str, value: Any) -> None:
        with self._lock:
            self._cache[key] = (time.monotonic(), value)

    # -- 限流 / 重试 ------------------------------------------------------
    def _throttle(self) -> None:
        """按 ``request_interval_ms`` 节流（全局串行，避免触发数据源限流）。"""
        with self._lock:
            now = time.monotonic()
            wait = self._interval_seconds - (now - self._last_request_ts)
            if wait > 0:
                time.sleep(wait)
            self._last_request_ts = time.monotonic()

    def _call(self, operation: str, func: Any, *args: Any, retries: int | None = None, **kwargs: Any) -> Any:
        """统一网络调用：限流 + 重试（指数退避）+ 中文错误。

        :param operation: 操作名（日志/错误提示用，如 ``stock_zh_a_daily``）
        :param retries: 覆盖默认重试次数（0 表示只尝试一次）
        :raises ProviderError: 全部尝试失败
        """
        if not AKSHARE_AVAILABLE:
            raise ProviderError(
                f"akshare 未安装，无法调用 {operation}（请执行 pip install akshare；"
                "也可把 config.yaml 的 data.provider 改为 mock 使用离线数据）"
            )
        attempts = max(1, int(self._max_retries if retries is None else retries) + 1)
        last_exc: Exception | None = None
        for attempt in range(attempts):
            self._throttle()
            try:
                result = func(*args, **kwargs)
                self._last_error = None
                return result
            except Exception as exc:  # noqa: BLE001 - 网络/解析异常统一重试
                last_exc = exc
                self._last_error = f"{type(exc).__name__}: {exc}"
                if attempt < attempts - 1:
                    backoff = min(self._interval_seconds * (2 ** attempt), 5.0)
                    logger.warning(
                        "akshare %s 第 %s/%s 次请求失败：%s；%.1f 秒后重试",
                        operation, attempt + 1, attempts, self._last_error, backoff,
                    )
                    time.sleep(backoff)
        raise ProviderError(
            f"akshare 请求失败（{operation}，已尝试 {attempts} 次）：{self._last_error}"
        ) from last_exc

    def _mark_degraded(self, operation: str, reason: Any) -> None:
        """记录一次降级（健康检查据此返回 DEGRADED）。"""
        with self._lock:
            self._degraded_count += 1
            self._last_degraded_at = now_local()
        logger.warning("akshare %s 失败，降级到 MockProvider 离线演示数据：%s", operation, reason)

    # -- 健康检查 ---------------------------------------------------------
    def set_health_override(self, status: ProviderStatus | str | None, error: str | None = None) -> None:
        """测试辅助：强制健康状态（``None`` 恢复正常探测）。"""
        if status is None:
            self._health_override = None
            self._health_override_error = None
        else:
            self._health_override = (
                status if isinstance(status, ProviderStatus) else ProviderStatus(str(status).upper())
            )
            self._health_override_error = error
        with self._lock:
            self._health_cache = None

    def health_check(self) -> ProviderHealth:
        """真实探测数据源（带超时；失败返回 ``DOWN``，不抛异常）。"""
        started = now_local()
        # 1) 测试/降级模拟覆盖
        if self._health_override is not None:
            return ProviderHealth(
                name=self.name,
                status=self._health_override,
                last_error=self._health_override_error,
                latency_ms=0.0,
                checked_at=started,
                message=f"akshare 健康状态被显式设置为 {self._health_override.value}",
            )
        if not AKSHARE_AVAILABLE:
            return ProviderHealth(
                name=self.name, status=ProviderStatus.DOWN,
                last_error=AKSHARE_IMPORT_ERROR, latency_ms=0.0, checked_at=started,
                message=f"akshare 未安装或导入失败：{AKSHARE_IMPORT_ERROR}（可改用 data.provider: mock）",
            )
        # 2) 缓存命中
        with self._lock:
            cached = self._health_cache
        if cached is not None and time.monotonic() - cached[0] <= self.HEALTH_CACHE_SECONDS:
            return cached[1]

        # 3) 真实探测：交易日历接口轻量且稳定（daemon 线程 + 超时，避免 URL 挂起）
        probe_box: dict[str, Any] = {}

        def _probe() -> None:
            try:
                probe_box["value"] = ak.tool_trade_date_hist_sina()
            except Exception as exc:  # noqa: BLE001
                probe_box["error"] = exc

        thread = threading.Thread(target=_probe, daemon=True, name="akshare-health-probe")
        thread.start()
        thread.join(timeout=self.HEALTH_TIMEOUT_SECONDS)
        elapsed_ms = round((now_local() - started).total_seconds() * 1000, 2)

        if thread.is_alive() or "value" not in probe_box and "error" not in probe_box:
            health = ProviderHealth(
                name=self.name, status=ProviderStatus.DOWN,
                last_error=f"探测超时（>{self.HEALTH_TIMEOUT_SECONDS:g}s）",
                latency_ms=elapsed_ms, checked_at=started,
                message="akshare 探测超时：网络可能不可用（系统将自动降级到 mock 离线数据）",
            )
        elif "error" in probe_box:
            health = ProviderHealth(
                name=self.name, status=ProviderStatus.DOWN,
                last_error=str(probe_box["error"]), latency_ms=elapsed_ms, checked_at=started,
                message=f"akshare 探测失败：{probe_box['error']}（系统将自动降级到 mock 离线数据）",
            )
        else:
            value = probe_box.get("value")
            if value is None or (hasattr(value, "empty") and value.empty):
                health = ProviderHealth(
                    name=self.name, status=ProviderStatus.DOWN, last_error="交易日历探测返回空数据",
                    latency_ms=elapsed_ms, checked_at=started,
                    message="akshare 探测失败：返回空数据（可能被限流）",
                )
            else:
                with self._lock:
                    degraded_recent = self._degraded_count > 0 and self._last_degraded_at is not None
                status = ProviderStatus.DEGRADED if degraded_recent else ProviderStatus.OK
                message = "akshare 数据源正常（真实 A 股行情）"
                if degraded_recent:
                    message = (
                        f"akshare 可用，但最近有 {self._degraded_count} 次请求降级到 mock；"
                        "请检查网络/数据源限流"
                    )
                health = ProviderHealth(
                    name=self.name, status=status, last_error=None,
                    latency_ms=elapsed_ms, checked_at=started, message=message,
                )
        with self._lock:
            self._health_cache = (time.monotonic(), health)
        return health

    # -- 内部：品种识别 / 行情 -------------------------------------------
    def _fallback_bars(self, symbols: list[str], start: date, end: date, freq: str) -> pd.DataFrame:
        """离线兜底：MockProvider 行情（未知标的为空表）。"""
        try:
            return self._fallback.get_bars(symbols, start, end, freq)
        except Exception as exc:  # pragma: no cover - mock 自身不应失败
            logger.error("mock 兜底数据生成失败：%s", exc)
            return pd.DataFrame(columns=list(ALL_BAR_COLUMNS))

    @staticmethod
    def _normalize_sina_stock(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """新浪 ``stock_zh_a_daily`` 原始表 → CF-1 行情结构（不复权）。"""
        frame = pd.DataFrame(
            {
                "date": pd.to_datetime(raw["date"], errors="coerce"),
                "symbol": symbol,
                "open": pd.to_numeric(raw["open"], errors="coerce"),
                "high": pd.to_numeric(raw["high"], errors="coerce"),
                "low": pd.to_numeric(raw["low"], errors="coerce"),
                "close": pd.to_numeric(raw["close"], errors="coerce"),
                "volume": pd.to_numeric(raw["volume"], errors="coerce").fillna(0).astype("int64"),  # 股
                "amount": pd.to_numeric(raw.get("amount"), errors="coerce").fillna(0.0),
                "turnover_rate": (pd.to_numeric(raw.get("turnover"), errors="coerce") * 100.0),  # 小数→%
                "is_suspended": False,
                "source": "akshare",
            }
        )
        frame = frame.dropna(subset=["date", "open", "close"])
        frame["adj_factor"] = 1.0  # 由因子接口合并时覆盖
        return frame

    @staticmethod
    def _normalize_em_stock(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """东财 ``stock_zh_a_hist`` 原始表（中文列）→ CF-1 行情结构（不复权）。"""
        frame = pd.DataFrame(
            {
                "date": pd.to_datetime(raw["日期"], errors="coerce"),
                "symbol": symbol,
                "open": pd.to_numeric(raw["开盘"], errors="coerce"),
                "high": pd.to_numeric(raw["最高"], errors="coerce"),
                "low": pd.to_numeric(raw["最低"], errors="coerce"),
                "close": pd.to_numeric(raw["收盘"], errors="coerce"),
                # 东财成交量为「手」，换算为「股」
                "volume": (pd.to_numeric(raw["成交量"], errors="coerce").fillna(0) * 100).astype("int64"),
                "amount": pd.to_numeric(raw.get("成交额"), errors="coerce").fillna(0.0),
                "turnover_rate": pd.to_numeric(raw.get("换手率"), errors="coerce"),
                "is_suspended": False,
                "source": "akshare",
            }
        )
        frame = frame.dropna(subset=["date", "open", "close"])
        frame["adj_factor"] = 1.0
        return frame

    def _get_factor_series(self, symbol: str) -> pd.DataFrame:
        """获取后复权因子序列（新浪 ``hfq-factor``，缓存 6 小时；缺失抛 ProviderError）。"""
        key = f"factor:{symbol}"
        cached = self._cache_get(key, self.FACTOR_CACHE_SECONDS)
        if cached is not None:
            return cached
        raw = self._call(
            "stock_zh_a_daily(hfq-factor)", ak.stock_zh_a_daily,
            symbol=_sina_symbol(symbol), adjust="hfq-factor",
        )
        if raw is None or raw.empty:
            raise ProviderError(f"无法获取 {symbol} 的复权因子（返回空）")
        factor = pd.DataFrame(
            {
                "date": pd.to_datetime(raw["date"], errors="coerce"),
                "adj_factor": pd.to_numeric(raw["hfq_factor"], errors="coerce"),
            }
        ).dropna()
        if factor.empty:
            raise ProviderError(f"{symbol} 复权因子解析为空")
        factor = factor.sort_values("date").reset_index(drop=True)
        self._cache_set(key, factor)
        return factor

    def _merge_factor(self, frame: pd.DataFrame, factor: pd.DataFrame) -> pd.DataFrame:
        """按日期向后取最近一次复权因子（因子按除权日离散变化）。

        注意：先移除左表的占位 ``adj_factor`` 列，否则 ``merge_asof`` 会因列名重叠报错，
        导致真实行情被静默降级为 mock（D-02 回归点）。
        """
        left = (
            frame.sort_values("date")
            .reset_index(drop=True)
            .drop(columns=["adj_factor"], errors="ignore")
        )
        right = factor.sort_values("date").reset_index(drop=True)
        merged = pd.merge_asof(left, right, on="date", direction="backward")
        merged["adj_factor"] = pd.to_numeric(merged["adj_factor"], errors="coerce").fillna(1.0)
        return merged

    def _fetch_stock_bars(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        """拉取股票日线（**新浪主源 → 腾讯备源**），返回不复权价 + adj_factor。

        东财 ``stock_zh_a_hist``（push2his.eastmoney.com）在当前网络全部 ProxyError，
        **不参与主备链**（R4 Lead 实测结论，详见 docs/DATA_SOURCES.md）。
        若数据源正常但区间内无数据（非交易日/停牌/越界），返回空表；仅请求异常才抛错。
        """
        errors: list[str] = []
        had_exception = False
        # 主源：新浪（稳定，带独立复权因子接口）
        try:
            raw = self._call(
                "stock_zh_a_daily", ak.stock_zh_a_daily,
                symbol=_sina_symbol(symbol), start_date=_yyyymmdd(start), end_date=_yyyymmdd(end),
                adjust="",
            )
            if raw is not None and not raw.empty:
                frame = self._normalize_sina_stock(raw, symbol)
                if not frame.empty:
                    try:
                        factor = self._get_factor_series(symbol)
                        frame = self._merge_factor(frame, factor)
                    except ProviderError as exc:
                        logger.warning("%s 复权因子获取失败，将退化为不复权（DQ005）：%s", symbol, exc)
                        frame["adj_factor"] = 1.0
                    return normalize_bars(frame)
            errors.append("新浪返回空数据")
        except ProviderError as exc:
            had_exception = True
            errors.append(f"新浪：{exc}")
            logger.warning("新浪行情获取失败，尝试腾讯备用源：%s", exc)

        # 备源：腾讯（成交量已为股；复权因子由 hfq/raw 比值推导）
        try:
            frame = self._fetch_stock_bars_tencent(symbol, start, end)
            if frame is not None and not frame.empty:
                return frame
            errors.append("腾讯返回空数据")
        except ProviderError as exc:
            had_exception = True
            errors.append(f"腾讯：{exc}")
            logger.warning("腾讯备用源行情获取失败：%s", exc)

        if not had_exception:
            # 两个数据源都正常但区间内无数据（周末/停牌/越界）：返回空表，不触发 mock 兜底
            return pd.DataFrame(columns=list(ALL_BAR_COLUMNS))
        raise ProviderError(
            f"{symbol} 行情获取失败（新浪/腾讯均失败；东财接口在当前网络不可用、不参与主备链）："
            f"{'；'.join(errors)}"
        )

    @staticmethod
    def _normalize_tencent_stock(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """腾讯 ``stock_zh_a_hist_tx`` 原始表 → CF-1 行情结构（不复权）。"""
        frame = pd.DataFrame(
            {
                "date": pd.to_datetime(raw["date"], errors="coerce"),
                "symbol": symbol,
                "open": pd.to_numeric(raw["open"], errors="coerce"),
                "high": pd.to_numeric(raw["high"], errors="coerce"),
                "low": pd.to_numeric(raw["low"], errors="coerce"),
                "close": pd.to_numeric(raw["close"], errors="coerce"),
                "volume": pd.to_numeric(raw["volume"], errors="coerce").fillna(0).astype("int64"),  # 腾讯已为「股」
                "amount": pd.to_numeric(raw.get("amount"), errors="coerce").fillna(0.0),
                "turnover_rate": pd.to_numeric(raw.get("turnover"), errors="coerce") * 100.0,  # 小数→%
                "is_suspended": False,
                "source": "akshare/tencent",
                "adj_factor": 1.0,
            }
        )
        return frame.dropna(subset=["date", "open", "close"])

    def _fetch_stock_bars_tencent(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        """腾讯备用日线（复权因子用 hfq/raw 收盘价比值推导；失败退化不复权）。"""
        code_ex = _sina_symbol(symbol)  # 腾讯同样使用 sh600519 形式
        raw = self._call(
            "stock_zh_a_hist_tx", ak.stock_zh_a_hist_tx,
            symbol=code_ex, start_date=_yyyymmdd(start), end_date=_yyyymmdd(end),
            adjust="", retries=1,
        )
        if raw is None or raw.empty:
            raise ProviderError("腾讯返回空数据")
        frame = self._normalize_tencent_stock(raw, symbol)
        if frame.empty:
            raise ProviderError("腾讯返回数据无法解析")
        try:
            hfq = self._call(
                "stock_zh_a_hist_tx(hfq)", ak.stock_zh_a_hist_tx,
                symbol=code_ex, start_date=_yyyymmdd(start), end_date=_yyyymmdd(end),
                adjust="hfq", retries=1,
            )
            if hfq is not None and not hfq.empty:
                hfq_frame = pd.DataFrame(
                    {
                        "date": pd.to_datetime(hfq["date"], errors="coerce"),
                        "hfq_close": pd.to_numeric(hfq["close"], errors="coerce"),
                    }
                ).dropna()
                merged = frame.drop(columns=["adj_factor"], errors="ignore").merge(
                    hfq_frame, on="date", how="left"
                )
                ratio = merged["hfq_close"] / merged["close"].replace(0, pd.NA)
                merged["adj_factor"] = pd.to_numeric(ratio, errors="coerce").fillna(1.0).round(6)
                frame = merged.drop(columns=["hfq_close"])
            else:
                logger.warning("%s 腾讯复权价为空，退化为不复权（DQ005）", symbol)
        except ProviderError as exc:
            logger.warning("%s 腾讯复权因子推导失败，退化为不复权（DQ005）：%s", symbol, exc)
        return normalize_bars(frame)

    def _fetch_index_bars(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        """拉取指数日线（**新浪主源 → 腾讯备源**；东财指数接口当前网络不可用）。"""
        code, exchange = _split_symbol(symbol)
        errors: list[str] = []
        had_exception = False
        # 主源：新浪指数日线（无日期参数，返回全量后按区间切片）
        try:
            raw = self._call(
                "stock_zh_index_daily", ak.stock_zh_index_daily,
                symbol=f"{exchange.lower()}{code}", retries=1,
            )
            if raw is not None and not raw.empty:
                frame = pd.DataFrame(
                    {
                        "date": pd.to_datetime(raw["date"], errors="coerce"),
                        "symbol": symbol,
                        "open": pd.to_numeric(raw["open"], errors="coerce"),
                        "high": pd.to_numeric(raw["high"], errors="coerce"),
                        "low": pd.to_numeric(raw["low"], errors="coerce"),
                        "close": pd.to_numeric(raw["close"], errors="coerce"),
                        "volume": pd.to_numeric(raw["volume"], errors="coerce").fillna(0).astype("int64"),
                        "amount": 0.0,
                        "turnover_rate": float("nan"),
                        "is_suspended": False,
                        "adj_factor": 1.0,
                        "source": "akshare",
                    }
                ).dropna(subset=["date", "open", "close"])
                mask = (frame["date"] >= pd.Timestamp(start)) & (frame["date"] <= pd.Timestamp(end))
                frame = frame.loc[mask]
                if not frame.empty:
                    return normalize_bars(frame)
            errors.append("新浪指数返回空数据")
        except ProviderError as exc:
            had_exception = True
            errors.append(f"新浪：{exc}")
            logger.warning("新浪指数获取失败，尝试腾讯备用源：%s", exc)
        # 备源：腾讯指数（接口较慢，仅新浪失败时使用）
        try:
            raw = self._call(
                "stock_zh_index_daily_tx", ak.stock_zh_index_daily_tx,
                symbol=f"{exchange.lower()}{code}", retries=0,
            )
            if raw is not None and not raw.empty:
                frame = pd.DataFrame(
                    {
                        "date": pd.to_datetime(raw["date"], errors="coerce"),
                        "symbol": symbol,
                        "open": pd.to_numeric(raw["open"], errors="coerce"),
                        "high": pd.to_numeric(raw["high"], errors="coerce"),
                        "low": pd.to_numeric(raw["low"], errors="coerce"),
                        "close": pd.to_numeric(raw["close"], errors="coerce"),
                        "volume": 0,
                        "amount": pd.to_numeric(raw.get("amount"), errors="coerce").fillna(0.0),
                        "turnover_rate": float("nan"),
                        "is_suspended": False,
                        "adj_factor": 1.0,
                        "source": "akshare/tencent",
                    }
                ).dropna(subset=["date", "open", "close"])
                mask = (frame["date"] >= pd.Timestamp(start)) & (frame["date"] <= pd.Timestamp(end))
                frame = frame.loc[mask]
                if not frame.empty:
                    return normalize_bars(frame)
            errors.append("腾讯指数返回空数据")
        except ProviderError as exc:
            had_exception = True
            errors.append(f"腾讯：{exc}")
        if not had_exception:
            return pd.DataFrame(columns=list(ALL_BAR_COLUMNS))
        raise ProviderError(f"{symbol} 指数行情获取失败（新浪/腾讯均失败）：{'；'.join(errors)}")

    # -- CF-1 方法：get_bars ---------------------------------------------
    def get_bars(
        self, symbols: list[str], start: date, end: date, freq: str = "1d"
    ) -> pd.DataFrame:
        """获取日线（不复权 + ``adj_factor``）；失败降级 MockProvider，不抛给 UI。

        返回列顺序遵循 CF-1：``date, symbol, open, high, low, close, volume, amount,
        adj_factor, turnover_rate, is_suspended``。
        """
        if freq != "1d":
            raise ValueError(f"AkshareProvider 仅支持日频 freq='1d'（收到 {freq!r}）；分钟级不在 MVP 范围")
        symbol_list = normalize_symbols(symbols)
        start_date, end_date = _as_date(start), _as_date(end)
        if not symbol_list or start_date > end_date:
            return pd.DataFrame(columns=list(ALL_BAR_COLUMNS))
        frames: list[pd.DataFrame] = []
        degraded_symbols: list[str] = []
        for symbol in symbol_list:
            try:
                frame = (
                    self._fetch_index_bars(symbol, start_date, end_date)
                    if _is_index_symbol(symbol)
                    else self._fetch_stock_bars(symbol, start_date, end_date)
                )
            except Exception as exc:  # noqa: BLE001 - 任何异常都降级，绝不抛给 UI
                logger.warning(
                    "%s 行情获取失败，使用 mock 兜底（离线 Mock，不写入缓存、不可用于真实回测）：%s",
                    symbol, exc,
                )
                frame = self._fallback_bars([symbol], start_date, end_date, freq)
                degraded_symbols.append(symbol)
            if frame is not None and not frame.empty:
                frames.append(frame)
        if degraded_symbols:
            self._mark_degraded(
                "get_bars",
                f"以下标的降级为离线 Mock（不可用于真实回测）：{', '.join(degraded_symbols)}",
            )
        if not frames:
            return pd.DataFrame(columns=list(ALL_BAR_COLUMNS))
        return normalize_bars(pd.concat(frames, ignore_index=True))

    def get_bars_adjusted(
        self, symbols: list[str], start: date, end: date, adjust: str = "hfq"
    ) -> pd.DataFrame:
        """直接返回复权后的日线（非 CF-1 冻结方法，便于行情页/脚本直接使用）。

        :param adjust: ``hfq`` 后复权 / ``qfq`` 前复权 / ``none`` 不复权
        """
        mode = str(adjust).strip().lower()
        if mode not in ("hfq", "qfq", "none"):
            raise ValueError(f"复权方式 {adjust!r} 非法：仅支持 hfq / qfq / none")
        frame = self.get_bars(symbols, start, end, freq="1d")
        if frame.empty or mode == "none":
            return frame
        parts: list[pd.DataFrame] = []
        for symbol, group in frame.groupby("symbol", sort=False):
            data = group.copy()
            factors = pd.to_numeric(data["adj_factor"], errors="coerce").fillna(1.0)
            factors = factors.where(factors > 0, 1.0)
            multiplier = factors if mode == "hfq" else factors / float(factors.iloc[-1] or 1.0)
            for column in ("open", "high", "low", "close"):
                data[column] = (data[column].astype(float) * multiplier).round(4)
            parts.append(data)
        return pd.concat(parts, ignore_index=True).sort_values(["date", "symbol"], ignore_index=True)

    # -- CF-1 方法：交易日历 ---------------------------------------------
    def get_trading_calendar(self, start: date, end: date) -> list[date]:
        """获取交易日历（新浪，缓存 1 小时）；失败降级 mock 工作日日历。"""
        start_date, end_date = _as_date(start), _as_date(end)
        if start_date > end_date:
            return []
        try:
            cached = self._cache_get("calendar", self.CALENDAR_CACHE_SECONDS)
            if cached is None:
                raw = self._call("tool_trade_date_hist_sina", ak.tool_trade_date_hist_sina, retries=1)
                if raw is None or raw.empty:
                    raise ProviderError("交易日历返回空数据")
                column = "trade_date" if "trade_date" in raw.columns else raw.columns[0]
                days = pd.to_datetime(raw[column], errors="coerce").dropna()
                cached = [timestamp.date() for timestamp in days]
                self._cache_set("calendar", cached)
            return [day for day in cached if start_date <= day <= end_date]
        except Exception as exc:  # noqa: BLE001 - 日历失败降级 mock，不抛给 UI
            self._mark_degraded("get_trading_calendar", exc)
            logger.warning("交易日历不可用，降级为 mock 工作日日历：%s", exc)
            return self._fallback.get_trading_calendar(start_date, end_date)

    # -- CF-1 方法：标的列表 ---------------------------------------------
    def _load_symbols(self) -> pd.DataFrame:
        """构建标的主数据（沪深股票列表 + 常用指数），缓存 24 小时。"""
        cached = self._cache_get("symbols", self.SYMBOLS_CACHE_SECONDS)
        if cached is not None:
            return cached
        rows: list[dict[str, Any]] = []
        updated_at = now_local()
        # 上交所（主板/科创板）
        for sh_symbol, board_filter in (("主板A股", "MAIN"), ("科创板", "STAR")):
            try:
                raw = self._call(
                    "stock_info_sh_name_code", ak.stock_info_sh_name_code,
                    symbol=sh_symbol, retries=1,
                )
            except ProviderError as exc:
                logger.warning("上交所股票列表（%s）获取失败：%s", sh_symbol, exc)
                continue
            if raw is None or raw.empty:
                continue
            for record in raw.to_dict("records"):
                code = str(record.get("证券代码") or "").strip()
                name = str(record.get("证券简称") or "").strip()
                if len(code) != 6 or not code.isdigit():
                    continue
                board, limit_pct = _board_for(code, "SH", name)
                rows.append(
                    {
                        "symbol": f"{code}.SH",
                        "code": code,
                        "name": name,
                        "exchange": "SH",
                        "board": board,
                        "kind": "STOCK",
                        "lot_size": _lot_size_for(code, "SH"),
                        "tick_size": 0.01,
                        "price_limit_pct": limit_pct,
                        "is_st": "ST" in name.upper(),
                        "list_date": _as_date(record["上市日期"]) if record.get("上市日期") else None,
                        "delist_date": None,
                        "trade_status": "NORMAL",
                        "updated_at": updated_at,
                    }
                )
            logger.debug("上交所 %s 列表：%d 只", sh_symbol, len(rows))
        # 深交所（A 股列表，含主板/创业板）
        try:
            raw = self._call(
                "stock_info_sz_name_code", ak.stock_info_sz_name_code,
                symbol="A股列表", retries=1,
            )
            if raw is not None and not raw.empty:
                for record in raw.to_dict("records"):
                    code = str(record.get("A股代码") or "").strip()
                    name = str(record.get("A股简称") or "").strip()
                    if len(code) != 6 or not code.isdigit():
                        continue
                    board, limit_pct = _board_for(code, "SZ", name)
                    board_hint = str(record.get("板块") or "")
                    if "创业" in board_hint:
                        board, limit_pct = "GEM", 20.0
                    rows.append(
                        {
                            "symbol": f"{code}.SZ",
                            "code": code,
                            "name": name,
                            "exchange": "SZ",
                            "board": board,
                            "kind": "STOCK",
                            "lot_size": _lot_size_for(code, "SZ"),
                            "tick_size": 0.01,
                            "price_limit_pct": limit_pct,
                            "is_st": "ST" in name.upper(),
                            "list_date": _as_date(record["A股上市日期"]) if record.get("A股上市日期") else None,
                            "delist_date": None,
                            "trade_status": "NORMAL",
                            "updated_at": updated_at,
                        }
                    )
        except ProviderError as exc:
            logger.warning("深交所股票列表获取失败：%s", exc)
        # 常用指数
        for symbol in sorted(KNOWN_INDEX_SYMBOLS):
            code, exchange = _split_symbol(symbol)
            rows.append(
                {
                    "symbol": symbol,
                    "code": code,
                    "name": symbol,
                    "exchange": exchange,
                    "board": "MAIN",
                    "kind": "INDEX",
                    "lot_size": 100,
                    "tick_size": 0.01,
                    "price_limit_pct": None,
                    "is_st": False,
                    "list_date": None,
                    "delist_date": None,
                    "trade_status": "NORMAL",
                    "updated_at": updated_at,
                }
            )
        frame = pd.DataFrame(rows, columns=_SYMBOL_COLUMNS)
        if frame.empty:
            raise ProviderError("股票列表为空（上交所/深交所接口均失败）")
        self._cache_set("symbols", frame)
        return frame

    def get_symbols(self, kind: str = "STOCK") -> pd.DataFrame:
        """获取标的主数据（STOCK/INDEX/ALL）；失败降级 mock 列表。"""
        target = (kind or "STOCK").strip().upper()
        try:
            frame = self._load_symbols()
        except Exception as exc:  # noqa: BLE001 - 列表失败降级 mock，不抛给 UI
            self._mark_degraded("get_symbols", exc)
            logger.warning("股票列表不可用，降级为 mock 列表：%s", exc)
            return self._fallback.get_symbols(kind)
        if target in ("ALL", "*"):
            return frame.reset_index(drop=True)
        return frame[frame["kind"] == target].reset_index(drop=True)

    # -- CF-1 方法：实时快照 ---------------------------------------------
    def _spot_eastmoney(self) -> pd.DataFrame:
        """东财全市场快照（缓存 60s；失败抛 ProviderError）。"""
        cached = self._cache_get("spot_em", self.SNAPSHOT_CACHE_SECONDS)
        if cached is not None:
            return cached
        raw = self._call("stock_zh_a_spot_em", ak.stock_zh_a_spot_em, retries=0)
        if raw is None or raw.empty:
            raise ProviderError("东财快照返回空数据")
        self._cache_set("spot_em", raw)
        return raw

    def _spot_sina(self) -> pd.DataFrame:
        """新浪全市场快照（较慢，缓存 120s；失败抛 ProviderError）。"""
        cached = self._cache_get("spot_sina", 120.0)
        if cached is not None:
            return cached
        raw = self._call("stock_zh_a_spot", ak.stock_zh_a_spot, retries=0)
        if raw is None or raw.empty:
            raise ProviderError("新浪快照返回空数据")
        self._cache_set("spot_sina", raw)
        return raw

    def get_snapshot(self, symbols: list[str]) -> pd.DataFrame:
        """获取实时快照（东财 → 新浪 → 最近收盘价 → mock 四级兜底，不抛给 UI）。

        多源合并：某个源缺少的标的（如指数不在个股快照里）会自动用下一级源补齐，
        保证请求的标的尽量都有快照。
        """
        symbol_list = normalize_symbols(symbols)
        if not symbol_list:
            return pd.DataFrame(columns=_SNAPSHOT_COLUMNS)
        ts = now_local()
        collected: dict[str, dict[str, Any]] = {}

        # 1) 东财快照（字段最全、最快）
        try:
            raw = self._spot_eastmoney()
            for record in raw.to_dict("records"):
                symbol = _symbol_from_code(str(record.get("代码") or ""))
                if symbol is None or symbol not in symbol_list or symbol in collected:
                    continue
                volume_lots = _to_int(record.get("成交量"))
                collected[symbol] = {
                    "symbol": symbol,
                    "name": record.get("名称"),
                    "last": _to_float(record.get("最新价")),
                    "prev_close": _to_float(record.get("昨收")),
                    "change": _to_float(record.get("涨跌额")),
                    "change_pct": _to_float(record.get("涨跌幅")),
                    # 东财快照成交量为「手」，换算为「股」
                    "volume": None if volume_lots is None else volume_lots * 100,
                    "amount": _to_float(record.get("成交额")),
                    "ts": ts,
                }
        except Exception as exc:  # noqa: BLE001 - 东财快照失败继续降级
            logger.warning("东财快照不可用，尝试新浪快照：%s", exc)

        # 2) 新浪快照（补齐东财缺失的标的）
        missing = [symbol for symbol in symbol_list if symbol not in collected]
        if missing:
            try:
                raw = self._spot_sina()
                for record in raw.to_dict("records"):
                    sina_code = str(record.get("代码") or "").strip().lower()
                    if len(sina_code) < 3:
                        continue
                    prefix, digits = sina_code[:2], sina_code[2:]
                    if prefix not in ("sh", "sz", "bj") or not digits.isdigit():
                        continue
                    symbol = f"{digits.zfill(6)}.{prefix.upper()}"
                    if symbol not in missing or symbol in collected:
                        continue
                    collected[symbol] = {
                        "symbol": symbol,
                        "name": record.get("名称"),
                        "last": _to_float(record.get("最新价")),
                        "prev_close": _to_float(record.get("昨收")),
                        "change": _to_float(record.get("涨跌额")),
                        "change_pct": _to_float(record.get("涨跌幅")),
                        "volume": _to_int(record.get("成交量")),
                        "amount": _to_float(record.get("成交额")),
                        "ts": ts,
                    }
            except Exception as exc:  # noqa: BLE001 - 新浪快照失败继续降级
                logger.warning("新浪快照不可用，尝试最近收盘价兜底：%s", exc)

        # 3) 最近收盘价兜底（指数等不在全市场快照里的标的；非实时但避免 UI 空白）
        missing = [symbol for symbol in symbol_list if symbol not in collected]
        for symbol in missing:
            try:
                bars = self.get_bars([symbol], ts.date() - timedelta(days=20), ts.date())
            except Exception:  # noqa: BLE001 - 兜底路径不抛
                bars = pd.DataFrame()
            if bars.empty:
                continue
            last = bars.iloc[-1]
            close_price = float(last["close"])
            collected[symbol] = {
                "symbol": symbol,
                "name": symbol,
                "last": close_price,
                "prev_close": close_price,
                "change": 0.0,
                "change_pct": 0.0,
                "volume": int(last["volume"] or 0),
                "amount": float(last["amount"] or 0.0),
                "ts": ts,
            }
        if collected:
            if any(symbol not in collected for symbol in symbol_list):
                logger.warning("部分标的无实时快照，已用最近收盘价/降级数据补齐")
            return pd.DataFrame(
                [collected[symbol] for symbol in symbol_list if symbol in collected],
                columns=_SNAPSHOT_COLUMNS,
            ).reset_index(drop=True)
        # 4) mock 兜底
        self._mark_degraded("get_snapshot", "东财/新浪快照与收盘价兜底均失败")
        return self._fallback.get_snapshot(symbol_list)

    # -- CF-1 方法：基本面 -----------------------------------------------
    def get_fundamentals(
        self, symbols: list[str], report_date: date | None = None
    ) -> pd.DataFrame:
        """获取基本面快照（能取多少算多少：PE/PB/总市值来自东财快照，其余为 NaN）。"""
        symbol_list = normalize_symbols(symbols)
        if not symbol_list:
            return pd.DataFrame(columns=_FUNDAMENTAL_COLUMNS)
        target_report = _as_date(report_date) if report_date is not None else date(now_local().year - 1, 12, 31)
        spot: pd.DataFrame | None = None
        try:
            spot = self._spot_eastmoney()
        except Exception as exc:  # noqa: BLE001 - 基本面允许缺失，降级为空指标
            logger.warning("东财快照不可用，基本面仅返回空指标（可后续补齐）：%s", exc)
        spot_map: dict[str, dict[str, Any]] = {}
        if spot is not None and not spot.empty:
            for record in spot.to_dict("records"):
                code = str(record.get("代码") or "").zfill(6)
                exchange = "SH" if code.startswith(("6", "9")) else "SZ"
                spot_map[f"{code}.{exchange}"] = record
        rows: list[dict[str, Any]] = []
        for symbol in symbol_list:
            record = spot_map.get(symbol, {})
            rows.append(
                {
                    "symbol": symbol,
                    "report_date": target_report,
                    "ann_date": None,
                    "pe_ttm": _to_float(record.get("市盈率-动态")),
                    "pb": _to_float(record.get("市净率")),
                    "ps_ttm": None,
                    "roe": None,
                    "total_mv": _to_float(record.get("总市值")),
                    "revenue_yoy": None,
                    "profit_yoy": None,
                    "source": "akshare",
                }
            )
        return pd.DataFrame(rows, columns=_FUNDAMENTAL_COLUMNS)

    # -- 除权除息事件 -----------------------------------------------------
    def _fetch_dividend_raw(self, symbol: str) -> tuple[str, pd.DataFrame | None]:
        """获取分红送配原始表：东财 datacenter（主）→ 新浪 history_dividend_detail（备）。

        说明：东财 **行情接口**（push2/push2his）在当前网络不可用，但 ``stock_fhps_detail_em``
        走 ``datacenter-web.eastmoney.com``，实测**可用**；仍保留新浪备用以增强鲁棒性。
        """
        code, _exchange = _split_symbol(symbol)
        try:
            raw = self._call(
                "stock_fhps_detail_em", ak.stock_fhps_detail_em, symbol=code, retries=0
            )
            if raw is not None and not raw.empty:
                return "eastmoney", raw
        except ProviderError as exc:
            logger.warning("%s 东财分红接口不可用，尝试新浪分红接口：%s", symbol, exc)
        try:
            raw = self._call(
                "stock_history_dividend_detail", ak.stock_history_dividend_detail,
                symbol=code, indicator="分红", retries=0,
            )
            if raw is not None and not raw.empty:
                return "sina", raw
        except ProviderError as exc:
            logger.warning("%s 分红接口均不可用（跳过，不影响行情）：%s", symbol, exc)
        return "", None

    def _parse_dividend_events(
        self, symbol: str, source: str, raw: pd.DataFrame
    ) -> list[dict[str, Any]]:
        """把分红送配原始表解析为 ``adjust_events`` 行（每 10 股口径 → 每股）。"""
        records: list[dict[str, Any]] = []
        for record in raw.to_dict("records"):
            ex_raw = record.get("除权除息日")
            if ex_raw is None or pd.isna(ex_raw):
                continue
            ex_date = _as_date(ex_raw)
            record_date_raw = record.get("股权登记日")
            record_date = (
                _as_date(record_date_raw)
                if record_date_raw is not None and not pd.isna(record_date_raw)
                else None
            )
            if source == "eastmoney":
                # 东财：现金分红比例 / 送转总比例 均为「每 10 股」
                cash = _to_float(record.get("现金分红-现金分红比例")) or 0.0
                stock_total = _to_float(record.get("送转股份-送转总比例")) or 0.0
                cash_per_share = round(cash / 10.0, 6)
                stock_ratio = round(stock_total / 10.0, 6)
            else:
                # 新浪：派息 / 送股 / 转增 均为「每 10 股」
                cash = _to_float(record.get("派息")) or 0.0
                stock_total = (_to_float(record.get("送股")) or 0.0) + (
                    _to_float(record.get("转增")) or 0.0
                )
                cash_per_share = round(cash / 10.0, 6)
                stock_ratio = round(stock_total / 10.0, 6)
            if cash_per_share > 0:
                records.append(
                    {
                        "symbol": symbol,
                        "ex_date": ex_date,
                        "event_type": "DIVIDEND",
                        "cash_per_share": cash_per_share,
                        "stock_ratio": None,
                        "record_date": record_date,
                        "source": f"akshare/{source}",
                    }
                )
            if stock_ratio > 0:
                records.append(
                    {
                        "symbol": symbol,
                        "ex_date": ex_date,
                        "event_type": "SPLIT",
                        "cash_per_share": 0.0,
                        "stock_ratio": stock_ratio,
                        "record_date": record_date,
                        "source": f"akshare/{source}",
                    }
                )
        return records

    def get_adjust_events(self, symbols: list[str] | None = None) -> pd.DataFrame:
        """获取除权除息事件（东财 datacenter 分红送配为主、新浪分红为备）。

        返回列：``symbol, ex_date, event_type, cash_per_share, stock_ratio, record_date, source``。
        同一除权日若同时有现金分红与送转，会拆成 ``DIVIDEND`` / ``SPLIT`` 两行。
        分红接口失败只记 WARNING 并跳过（不影响行情写入）。
        """
        symbol_list = normalize_symbols(symbols) if symbols is not None else []
        if not symbol_list:
            return pd.DataFrame(columns=_ADJUST_EVENT_COLUMNS)
        rows: list[dict[str, Any]] = []
        for symbol in symbol_list:
            if _is_index_symbol(symbol):
                continue
            cache_key = f"adjust_events:{symbol}"
            cached = self._cache_get(cache_key, self.ADJUST_EVENT_CACHE_SECONDS)
            if cached is None:
                source, raw = self._fetch_dividend_raw(symbol)
                records = (
                    self._parse_dividend_events(symbol, source, raw)
                    if raw is not None and not raw.empty and source
                    else []
                )
                cached = pd.DataFrame(records, columns=_ADJUST_EVENT_COLUMNS)
                self._cache_set(cache_key, cached)
            if not cached.empty:
                rows.extend(cached.to_dict("records"))
        return pd.DataFrame(rows, columns=_ADJUST_EVENT_COLUMNS)

    def __repr__(self) -> str:  # pragma: no cover - 调试展示
        return f"<AkshareProvider available={AKSHARE_AVAILABLE} fallback=mock>"


# ---------------------------------------------------------------------------
# 自检（quantdesk 目录：``python -m data.akshare_provider``）
# ---------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    from core.config import load_config, reset_config, set_config
    from data.provider import build_provider, get_registry, reset_registry

    cfg = load_config(environ={})
    cfg.data.provider = "akshare"
    cfg.data.universe = ["600519.SH", "000001.SZ", "300750.SZ"]
    set_config(cfg)
    try:
        provider = AkshareProvider(cfg)
        print(f"akshare 可用：{AKSHARE_AVAILABLE}；导入错误：{AKSHARE_IMPORT_ERROR}")
        health = provider.health_check()
        print(f"健康检查：{health.status.value} | {health.message} | {health.latency_ms} ms")

        if health.status is not ProviderStatus.DOWN:
            # 1) 日线（复权因子）
            start, end = date(2024, 1, 2), date(2024, 2, 29)
            bars = provider.get_bars(["600519.SH"], start, end)
            assert not bars.empty, "akshare 日线为空"
            assert list(bars.columns)[:8] == [
                "date", "symbol", "open", "high", "low", "close", "volume", "amount",
            ], list(bars.columns)
            assert (bars["adj_factor"] > 0).all()
            assert (bars["volume"] > 0).any()
            # 防止真实接口失败后静默降级 mock（D-02 回归断言：茅台真实价格量级 > 1000 元）
            assert set(bars["source"]) == {"akshare"}, f"疑似降级 mock：{set(bars['source'])}"
            assert float(bars["close"].iloc[-1]) > 1000.0, f"疑似 mock 数据：收盘 {bars['close'].iloc[-1]}"
            print(f"600519 日线 {len(bars)} 行；最新 {bars.iloc[-1]['date'].date()} 收盘 {bars.iloc[-1]['close']}；因子 {bars['adj_factor'].iloc[-1]:.4f}")

            # 2) 复权序列（hfq ≥ 不复权）
            adjusted = provider.get_bars_adjusted(["600519.SH"], start, end, adjust="hfq")
            assert not adjusted.empty and adjusted["close"].sum() >= bars["close"].sum()
            qfq = provider.get_bars_adjusted(["600519.SH"], start, end, adjust="qfq")
            assert not qfq.empty

            # 3) 指数（无复权）
            index_bars = provider.get_bars(["000300.SH"], start, end)
            assert not index_bars.empty and (index_bars["adj_factor"] == 1.0).all()
            assert set(index_bars["source"]) == {"akshare"}, set(index_bars["source"])
            print(f"000300 指数 {len(index_bars)} 行")

            # 4) 交易日历 / 标的列表（含上市日期）
            calendar = provider.get_trading_calendar(start, end)
            assert len(calendar) > 10 and all(isinstance(day, date) for day in calendar)
            symbols_frame = provider.get_symbols("STOCK")
            assert "600519.SH" in set(symbols_frame["symbol"])
            row = symbols_frame[symbols_frame["symbol"] == "600519.SH"].iloc[0]
            print(f"股票列表 {len(symbols_frame)} 只；600519 上市日期={row['list_date']} 板块={row['board']}")

            # 5) 快照 / 基本面 / 除权除息
            snapshot = provider.get_snapshot(["600519.SH", "000300.SH"])
            assert len(snapshot) == 2, snapshot  # 指数不在个股快照里，应由收盘价兜底补齐
            print(f"快照 {len(snapshot)} 行；600519 最近价={snapshot.iloc[0]['last']}")
            fundamentals = provider.get_fundamentals(["600519.SH"])
            assert len(fundamentals) == 1 and "pe_ttm" in fundamentals.columns
            events = provider.get_adjust_events(["600519.SH"])
            assert not events.empty and set(events["event_type"]) <= {"DIVIDEND", "SPLIT"}
            print(f"除权除息事件 {len(events)} 条（最近除权日 {events['ex_date'].max()}）")

            # 6) 注册表集成：build_provider 应返回 akshare
            reset_registry()
            registry = get_registry()
            assert registry.is_registered("akshare"), "akshare 未注册到 ProviderRegistry"
            assert build_provider(cfg).name == "akshare"
            print("注册表集成 OK：build_provider -> akshare")

            # 7) 断网模拟：akshare DOWN → build_provider 降级 mock；请求失败 → get_bars 逐标的降级 mock
            reset_registry()
            registry = get_registry()
            registry.get("akshare", config=cfg).set_health_override(ProviderStatus.DOWN, "模拟断网")
            degraded = build_provider(cfg)
            assert degraded.name == "mock", degraded.name
            original_call = provider._call
            provider._call = lambda *a, **k: (_ for _ in ()).throw(ProviderError("模拟网络故障"))  # type: ignore[assignment]
            try:
                mock_bars = provider.get_bars(["600519.SH"], start, date(2024, 1, 10))
            finally:
                provider._call = original_call  # type: ignore[assignment]
            assert not mock_bars.empty, "断网时未降级到 mock 数据"
            assert set(mock_bars["source"]) == {"mock"}, set(mock_bars["source"])
            registry.get("akshare", config=cfg).set_health_override(None)
            print("断网降级 OK：build_provider -> mock；get_bars 请求失败自动走离线 mock（不写入缓存）")
        else:
            # 离线环境：验证健康检查 DOWN、build_provider 降级 mock、get_bars 走 mock 兜底
            reset_registry()
            registry = get_registry()
            registry.get("akshare", config=cfg).set_health_override(ProviderStatus.DOWN, "离线环境模拟")
            assert build_provider(cfg).name == "mock"
            bars = provider.get_bars(["600519.SH"], date(2024, 1, 2), date(2024, 1, 10))
            assert not bars.empty and set(bars["source"]) <= {"mock", "akshare"}, set(bars["source"])
            registry.get("akshare", config=cfg).set_health_override(None)
            print("离线模式自检通过：健康检查 DOWN、build_provider 降级 mock、get_bars 走离线 mock 兜底")

        print(
            "data/akshare_provider.py 自检通过：日线+复权因子/指数/日历/股票列表/快照/基本面/"
            "除权事件/注册表集成/断网降级 mock"
        )
    finally:
        reset_registry()
        reset_config()

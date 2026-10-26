"""全市场快照采集与名称映射（R6-P0，短线异动雷达前置）。

能力
----
1. **全市场实时快照**：新浪 ``stock_zh_a_spot()``（实测 5000+ 只；东财 push2 当前网络不可用，不再尝试）；
2. **字段标准化**：``symbol``（统一 ``600519.SH``）、``name``、``price``、``pct_change``、``volume``、
   ``amount``、``open/high/low/prev_close``、``timestamp``、``source``；
3. **落库**：``market_snapshot`` 表（``trade_date + symbol`` 唯一，含 name，重复采集幂等覆盖）；
4. **名称映射**：``get_names(symbols) -> dict``（``symbols`` 表优先、快照兜底；另见 ``data.symbols.name_of``）；
5. **查询**：``latest_snapshot(date)`` 返回最近一个交易日快照；
6. **限流/重试/降级**：按 ``config.data.request_interval_ms`` 限流、``max_retries`` 重试；
   失败时返回**上次快照**并附中文提示（不抛给 UI），由调用方决定是否提示“离线/过期”。

用法::

    from data.market_snapshot import collect_daily, fetch_spot, get_names, latest_snapshot

    report = collect_daily()                     # 采集 + 落库（失败自动降级上次快照）
    get_names(["600519.SH", "000001.SZ"])        # {"600519.SH": "贵州茅台", ...}
    df = latest_snapshot()                       # 最近一个交易日快照 DataFrame

自检（quantdesk 目录，需要网络）：``python -m data.market_snapshot``
"""

from __future__ import annotations

import os

os.environ.setdefault("TQDM_DISABLE", "1")  # 关闭 akshare 内部进度条（必须在 import akshare 前）

import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from core.config import AppConfig, get_config
from core.db import Database, get_database
from core.logging import get_logger
from core.models import MarketSnapshotORM, now_local, now_local_naive
from data.provider import normalize_symbols

try:  # pragma: no cover - 取决于环境
    import akshare as ak

    AKSHARE_AVAILABLE = True
    AKSHARE_IMPORT_ERROR: str | None = None
except Exception as _exc:  # pragma: no cover
    ak = None  # type: ignore[assignment]
    AKSHARE_AVAILABLE = False
    AKSHARE_IMPORT_ERROR = str(_exc)

__all__ = [
    "SNAPSHOT_COLUMNS",
    "SnapshotError",
    "SnapshotReport",
    "MarketSnapshotService",
    "fetch_spot",
    "collect_daily",
    "latest_snapshot",
    "get_names",
    "get_service",
    "reset_service",
]

logger = get_logger("data.market_snapshot")

#: 标准化后的快照列（顺序固定）
SNAPSHOT_COLUMNS: tuple[str, ...] = (
    "symbol", "name", "price", "pct_change", "volume", "amount",
    "open", "high", "low", "prev_close", "timestamp", "source",
)

#: 单批 upsert 行数
_BATCH_SIZE = 300


class SnapshotError(RuntimeError):
    """全市场快照采集失败（中文可操作提示）。"""


def _to_float(value: Any) -> float | None:
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


def _chunks(items: list[Any], size: int = _BATCH_SIZE):
    for start in range(0, len(items), size):
        yield items[start:start + size]


@dataclass
class SnapshotReport:
    """快照采集报告（中文 message 可直接展示给用户）。"""

    ok: bool
    degraded: bool
    trade_date: date | None
    rows: int = 0
    saved: int = 0
    source: str = ""
    message: str = ""
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "degraded": self.degraded,
            "trade_date": self.trade_date.isoformat() if self.trade_date else None,
            "rows": self.rows,
            "saved": self.saved,
            "source": self.source,
            "message": self.message,
            "elapsed_ms": self.elapsed_ms,
        }


class MarketSnapshotService:
    """全市场快照服务：采集 / 落库 / 查询 / 名称映射 / 降级。"""

    #: 内存快照缓存（秒）
    CACHE_SECONDS = 120.0

    def __init__(self, *, config: AppConfig | None = None, database: Database | None = None) -> None:
        self.config = config or get_config()
        self.database = database or get_database()
        self._lock = threading.RLock()
        self._cache: tuple[float, pd.DataFrame] | None = None
        self._interval_seconds = max(float(self.config.data.request_interval_ms), 0.0) / 1000.0
        self._max_retries = int(self.config.data.max_retries)
        self._last_request_ts = 0.0
        self.last_error: str | None = None

    # -- 限流 / 重试 ------------------------------------------------------
    def _throttle(self) -> None:
        with self._lock:
            wait = self._interval_seconds - (time.monotonic() - self._last_request_ts)
            if wait > 0:
                time.sleep(wait)
            self._last_request_ts = time.monotonic()

    def _fetch_spot_raw(self) -> pd.DataFrame:
        """调用新浪全市场快照（限流 + 重试）；失败抛中文 :class:`SnapshotError`。"""
        if not AKSHARE_AVAILABLE:
            raise SnapshotError(
                f"akshare 未安装或导入失败（{AKSHARE_IMPORT_ERROR}），无法采集全市场快照；"
                "请执行 pip install akshare 后重试（系统会先使用上次快照）"
            )
        attempts = max(1, self._max_retries + 1)
        last_exc: Exception | None = None
        for attempt in range(attempts):
            self._throttle()
            try:
                frame = ak.stock_zh_a_spot()  # 新浪全市场（约 5000+ 只，耗时 ~20s）
                if frame is None or frame.empty:
                    raise SnapshotError("新浪全市场快照返回空数据（可能被限流）")
                self.last_error = None
                return frame
            except Exception as exc:  # noqa: BLE001 - 网络/解析异常统一重试
                last_exc = exc
                self.last_error = f"{type(exc).__name__}: {exc}"
                if attempt < attempts - 1:
                    backoff = min(self._interval_seconds * (2 ** attempt), 5.0)
                    logger.warning(
                        "全市场快照第 %s/%s 次采集失败：%s；%.1f 秒后重试",
                        attempt + 1, attempts, self.last_error, backoff,
                    )
                    time.sleep(backoff)
        raise SnapshotError(f"全市场快照采集失败（已尝试 {attempts} 次）：{self.last_error}") from last_exc

    # -- 标准化 -----------------------------------------------------------
    @staticmethod
    def _standardize(raw: pd.DataFrame) -> pd.DataFrame:
        """新浪原始快照 → 标准列（symbol/name/price/.../timestamp/source）。"""
        now = now_local()
        rows: list[dict[str, Any]] = []
        for record in raw.to_dict("records"):
            raw_code = str(record.get("代码") or "").strip().lower()
            if len(raw_code) < 3:
                continue
            prefix, digits = raw_code[:2], raw_code[2:]
            if prefix not in ("sh", "sz", "bj") or not digits.isdigit():
                continue
            rows.append(
                {
                    "symbol": f"{digits.zfill(6)}.{prefix.upper()}",
                    "name": str(record.get("名称") or "").strip(),
                    "price": _to_float(record.get("最新价")),
                    "pct_change": _to_float(record.get("涨跌幅")),
                    "volume": _to_int(record.get("成交量")),
                    "amount": _to_float(record.get("成交额")),
                    "open": _to_float(record.get("今开")),
                    "high": _to_float(record.get("最高")),
                    "low": _to_float(record.get("最低")),
                    "prev_close": _to_float(record.get("昨收")),
                    "timestamp": now,
                    "source": "sina",
                }
            )
        frame = pd.DataFrame(rows, columns=list(SNAPSHOT_COLUMNS))
        # 去重（同 symbol 保留最后一条）
        return frame.drop_duplicates(subset=["symbol"], keep="last").reset_index(drop=True)

    def fetch_spot(self, *, force: bool = False) -> pd.DataFrame:
        """获取全市场快照（内存缓存 120 秒）；失败抛 :class:`SnapshotError`。"""
        with self._lock:
            cached = self._cache
        if not force and cached is not None and time.monotonic() - cached[0] <= self.CACHE_SECONDS:
            return cached[1].copy()
        raw = self._fetch_spot_raw()
        frame = self._standardize(raw)
        if frame.empty:
            raise SnapshotError("全市场快照标准化后为空（数据源字段可能变化）")
        with self._lock:
            self._cache = (time.monotonic(), frame)
        return frame.copy()

    # -- 落库 -------------------------------------------------------------
    def save_snapshot(self, frame: pd.DataFrame, *, trade_date: date | None = None) -> int:
        """把标准化快照写入 ``market_snapshot``（``(trade_date,symbol)`` 唯一，幂等覆盖）。"""
        if frame is None or frame.empty:
            return 0
        snapshot_frame = frame.copy()
        if trade_date is None:
            timestamp = snapshot_frame["timestamp"].iloc[0] if "timestamp" in snapshot_frame.columns else None
            trade_date = timestamp.date() if isinstance(timestamp, datetime) else now_local().date()
        now = now_local_naive()
        rows: list[dict[str, Any]] = []
        for record in snapshot_frame.to_dict("records"):
            rows.append(
                {
                    "trade_date": trade_date,
                    "symbol": str(record.get("symbol") or "").strip().upper(),
                    "name": str(record.get("name") or ""),
                    "price": record.get("price"),
                    "pct_change": record.get("pct_change"),
                    "volume": record.get("volume"),
                    "amount": record.get("amount"),
                    "open": record.get("open"),
                    "high": record.get("high"),
                    "low": record.get("low"),
                    "prev_close": record.get("prev_close"),
                    "snapshot_at": record.get("timestamp") or now,
                    "source": str(record.get("source") or "sina"),
                    "created_at": now,
                    "updated_at": now,
                }
            )
        saved = 0
        with self.database.session() as session:
            for chunk in _chunks(rows):
                statement = sqlite_insert(MarketSnapshotORM)
                statement = statement.on_conflict_do_update(
                    index_elements=["trade_date", "symbol"],
                    set_={
                        "name": statement.excluded.name,
                        "price": statement.excluded.price,
                        "pct_change": statement.excluded.pct_change,
                        "volume": statement.excluded.volume,
                        "amount": statement.excluded.amount,
                        "open": statement.excluded.open,
                        "high": statement.excluded.high,
                        "low": statement.excluded.low,
                        "prev_close": statement.excluded.prev_close,
                        "snapshot_at": statement.excluded.snapshot_at,
                        "source": statement.excluded.source,
                        "updated_at": statement.excluded.updated_at,
                    },
                )
                session.execute(statement, list(chunk))
                saved += len(chunk)
        logger.info("全市场快照已落库：%s 行（trade_date=%s）", saved, trade_date)
        return saved

    # -- 查询 / 名称 ------------------------------------------------------
    def latest_date(self) -> date | None:
        """最近一个已采集快照交易日；无数据返回 ``None``。"""
        with self.database.session() as session:
            return session.execute(select(func.max(MarketSnapshotORM.trade_date))).scalar()

    def latest_snapshot(self, trade_date: date | None = None) -> pd.DataFrame:
        """返回最近（或指定）交易日快照 DataFrame；无数据返回空表（列齐全）。"""
        with self.database.session() as session:
            target = trade_date or session.execute(
                select(func.max(MarketSnapshotORM.trade_date))
            ).scalar()
            if target is None:
                return pd.DataFrame(columns=list(SNAPSHOT_COLUMNS))
            rows = session.execute(
                select(
                    MarketSnapshotORM.symbol,
                    MarketSnapshotORM.name,
                    MarketSnapshotORM.price,
                    MarketSnapshotORM.pct_change,
                    MarketSnapshotORM.volume,
                    MarketSnapshotORM.amount,
                    MarketSnapshotORM.open,
                    MarketSnapshotORM.high,
                    MarketSnapshotORM.low,
                    MarketSnapshotORM.prev_close,
                    MarketSnapshotORM.snapshot_at,
                    MarketSnapshotORM.source,
                )
                .where(MarketSnapshotORM.trade_date == target)
                .order_by(MarketSnapshotORM.symbol)
            ).all()
        frame = pd.DataFrame(
            {
                "symbol": [row[0] for row in rows],
                "name": [row[1] for row in rows],
                "price": [row[2] for row in rows],
                "pct_change": [row[3] for row in rows],
                "volume": [row[4] for row in rows],
                "amount": [row[5] for row in rows],
                "open": [row[6] for row in rows],
                "high": [row[7] for row in rows],
                "low": [row[8] for row in rows],
                "prev_close": [row[9] for row in rows],
                "timestamp": [row[10] for row in rows],
                "source": [row[11] for row in rows],
            },
            columns=list(SNAPSHOT_COLUMNS),
        )
        return frame

    def get_names(self, symbols: list[str] | str) -> dict[str, str]:
        """批量获取中文名称（``symbols`` 表优先、快照兜底；未知返回空字符串）。"""
        from data.symbols import names_of

        return names_of(normalize_symbols(symbols), database=self.database)

    # -- 采集编排（含降级） ----------------------------------------------
    def collect_daily(self, *, trade_date: date | None = None, force: bool = False) -> SnapshotReport:
        """采集当日全市场快照并落库；失败时**返回上次快照** + 中文提示（不抛给 UI）。"""
        started = time.perf_counter()
        try:
            frame = self.fetch_spot(force=force)
            saved = self.save_snapshot(frame, trade_date=trade_date)
            elapsed = round((time.perf_counter() - started) * 1000, 2)
            return SnapshotReport(
                ok=True, degraded=False, trade_date=trade_date or self.latest_date(),
                rows=len(frame), saved=saved, source="sina", elapsed_ms=elapsed,
                message=f"全市场快照采集完成：{len(frame)} 只，已落库 {saved} 行",
            )
        except Exception as exc:  # noqa: BLE001 - 降级不抛给 UI
            self.last_error = str(exc)
            logger.warning("全市场快照采集失败，降级返回上次快照：%s", exc)
            fallback = self.latest_snapshot(trade_date)
            elapsed = round((time.perf_counter() - started) * 1000, 2)
            fallback_date = trade_date or (fallback["timestamp"].iloc[0].date() if not fallback.empty else None)
            message = (
                f"全市场快照采集失败（{exc}）。"
                + (
                    f"已使用上次快照（{fallback_date}，{len(fallback)} 只）应急，异动雷达结果可能滞后。"
                    if not fallback.empty
                    else "当前无历史快照可用，请检查网络后重试。"
                )
            )
            return SnapshotReport(
                ok=False, degraded=True, trade_date=fallback_date, rows=len(fallback), saved=0,
                source="cache", elapsed_ms=elapsed, message=message,
            )

    def __repr__(self) -> str:  # pragma: no cover - 调试展示
        return f"<MarketSnapshotService db={self.database.url}>"


# ---------------------------------------------------------------------------
# 模块级默认服务
# ---------------------------------------------------------------------------

_service: MarketSnapshotService | None = None


def get_service() -> MarketSnapshotService:
    """获取全市场快照服务默认单例。"""
    global _service
    if _service is None:
        _service = MarketSnapshotService()
    return _service


def reset_service() -> None:
    """重置默认服务单例（测试隔离用）。"""
    global _service
    _service = None


def fetch_spot(*, force: bool = False) -> pd.DataFrame:
    """模块级便捷函数：获取全市场快照。"""
    return get_service().fetch_spot(force=force)


def collect_daily(*, trade_date: date | None = None, force: bool = False) -> SnapshotReport:
    """模块级便捷函数：采集当日快照并落库（失败降级上次快照）。"""
    return get_service().collect_daily(trade_date=trade_date, force=force)


def latest_snapshot(trade_date: date | None = None) -> pd.DataFrame:
    """模块级便捷函数：查询最近（或指定）交易日快照。"""
    return get_service().latest_snapshot(trade_date)


def get_names(symbols: list[str] | str) -> dict[str, str]:
    """模块级便捷函数：批量获取中文名称。"""
    return get_service().get_names(symbols)


# ---------------------------------------------------------------------------
# 自检（quantdesk 目录：``python -m data.market_snapshot``）
# ---------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    import shutil
    import tempfile
    from pathlib import Path

    import yaml

    from core.config import DEFAULT_CONFIG_PATH, load_config, reset_config, set_config

    tmp_dir = Path(tempfile.mkdtemp(prefix="qd_snapshot_"))
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    raw["app"]["data_dir"] = str(tmp_dir / "runtime")
    raw["database"]["url"] = f"sqlite:///{(tmp_dir / 'runtime' / 'snapshot.db').as_posix()}"
    config_path = tmp_dir / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    cfg = load_config(config_path, environ={})
    set_config(cfg)
    database = Database(config=cfg)
    try:
        from core.db import init_db

        assert init_db(cfg).ok
        service = MarketSnapshotService(config=cfg, database=database)
        print(f"akshare 可用：{AKSHARE_AVAILABLE}；导入错误：{AKSHARE_IMPORT_ERROR}")

        report = service.collect_daily(force=True)
        print("采集报告：", report.message)
        if report.ok:
            assert report.rows >= 4000, f"全市场快照应 ≥4000 只，实际 {report.rows}"
            assert report.saved >= 4000
            latest = service.latest_snapshot()
            assert len(latest) >= 4000 and list(latest.columns) == list(SNAPSHOT_COLUMNS)
            assert latest["symbol"].str.match(r"^\d{6}\.(SH|SZ|BJ)$").all()
            names = service.get_names(["600519.SH", "000001.SZ"])
            assert names.get("600519.SH") == "贵州茅台", names
            assert names.get("000001.SZ") == "平安银行", names
            print(f"落库 {report.saved} 行；名称映射 OK：{names}")

            # 降级路径：模拟采集失败 → 返回上次快照 + 中文提示
            original = service._fetch_spot_raw

            def _boom() -> pd.DataFrame:
                raise SnapshotError("模拟网络故障（自检）")

            service._fetch_spot_raw = _boom  # type: ignore[assignment]
            try:
                degraded = service.collect_daily(force=True)
            finally:
                service._fetch_spot_raw = original  # type: ignore[assignment]
            assert degraded.degraded is True and degraded.ok is False
            assert degraded.rows >= 4000 and "上次快照" in degraded.message
            print("降级路径 OK：返回上次快照 + 中文提示")
        else:
            # 离线环境：仅验证降级语义（不抛异常）
            assert report.degraded is True and "快照" in report.message
            print("离线模式自检通过：采集失败仅降级提示，不抛异常")

        print(
            "data/market_snapshot.py 自检通过：全市场快照采集/标准化(symbol.SH 格式)/落库幂等/"
            "latest_snapshot/名称映射/限流重试/失败降级上次快照"
        )
    finally:
        database.dispose()
        reset_service()
        reset_config()
        shutil.rmtree(tmp_dir, ignore_errors=True)

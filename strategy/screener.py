"""全市场筛选服务（P0）：把 Lead 的“全 A 股 → 流动性过滤 → 量价信号 → 截面打分 → Top N”
原型产品化。

分层流程
--------
1. **全市场快照**：优先 ``data.market_snapshot``（Dev1 已落库），其次本地
   ``data/a_spot.csv``，最后尝试实时快照；失败给出中文提示。
2. **基础过滤**：非北交所、非 ST/*ST/退市、价格 ≥ ``min_price``（默认 3 元）、
   成交额 ≥ ``min_amount``（默认 1 亿）、停牌/无成交剔除。
3. **流动性前 N**：按成交额取前 ``liquidity_top_n``（默认 300），避免对全市场逐一拉历史。
4. **历史信号**：60/20 日动量、量比（当日/20 日均量）、20 日突破、距 MA20/MA60、
   ATR%、接近一年新高、连续上涨天数、成交额分位。
5. **截面 z-score 打分**：权重可配置（默认与 Lead 原型一致）。
6. **过热剔除**：5 日涨幅 > 20%、距 MA20 > 15%、ATR% > 6%、连续上涨 > 4 天 → 剔除并记
   ``risk_flags``。
7. **Top N**：输出 ``[{symbol,name,score,reasons,risk_flags,...}]``。

数据模式
--------
* ``cache``：**只用本地缓存**（不联网），适合 UI/离线演示；缺失标的多时在 warnings 提示。
* ``incremental``：先读缓存，再调用 ``BarCache.update`` 增量拉取缺失区间（会联网，受数据层
  限流控制）；失败标的跳过并记录，不影响整体。
* 也支持注入 ``snapshot``/``bars``（测试与历史验证用）。

合规声明
--------
筛选为**研究阶段**规则，未经统计验证前不构成买入建议；验证结论见
``docs/SCREENER_VALIDATION.md``。
"""

from __future__ import annotations

import json
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from strategy.factors import normalize_bars

logger = logging.getLogger(__name__)

__all__ = [
    "ScreenerConfig",
    "ScreenerResult",
    "MarketScreener",
    "DEFAULT_SCREEN_WEIGHTS",
    "normalize_snapshot",
]

#: 默认打分权重（与 Lead 原型一致；可在 config 覆盖）
DEFAULT_SCREEN_WEIGHTS: dict[str, float] = {
    "mom60": 0.22,
    "mom20": 0.18,
    "vol_ratio": 0.14,
    "breakout20": 0.12,
    "low_vol": 0.10,
    "amount": 0.08,
    "ma20_closeness": 0.08,
    "near_high": 0.08,
}


@dataclass(frozen=True)
class ScreenerConfig:
    """筛选参数（可配置权重、过滤阈值与数据模式）。"""

    min_price: float = 3.0
    min_amount: float = 1e8
    liquidity_top_n: int = 300
    top_n: int = 25
    history_days: int = 260
    min_history: int = 120
    mode: str = "cache"  # cache | incremental
    weights: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_SCREEN_WEIGHTS))
    # 过热剔除阈值
    max_ret5: float = 0.20
    max_dist_ma20: float = 0.15
    max_atr_pct: float = 0.06
    max_consec_up: int = 4
    # 并发/限流（incremental 兜底逐只拉取时使用）
    max_workers: int = 4
    requests_per_second: float = 4.0
    snapshot_path: str | None = None
    bars_path: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in ("cache", "incremental"):
            raise ValueError(f"mode 仅支持 cache/incremental，收到 {self.mode!r}")
        if self.liquidity_top_n < 10 or self.top_n < 1:
            raise ValueError("liquidity_top_n ≥ 10 且 top_n ≥ 1")
        total = float(sum(self.weights.values()))
        if total <= 0:
            raise ValueError("打分权重之和必须 > 0")
        unknown = set(self.weights) - set(DEFAULT_SCREEN_WEIGHTS)
        if unknown:
            raise ValueError(f"未知打分权重键：{sorted(unknown)}；可选 {sorted(DEFAULT_SCREEN_WEIGHTS)}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "min_price": self.min_price,
            "min_amount": self.min_amount,
            "liquidity_top_n": self.liquidity_top_n,
            "top_n": self.top_n,
            "history_days": self.history_days,
            "mode": self.mode,
            "weights": dict(self.weights),
            "max_ret5": self.max_ret5,
            "max_dist_ma20": self.max_dist_ma20,
            "max_atr_pct": self.max_atr_pct,
            "max_consec_up": self.max_consec_up,
        }


@dataclass
class ScreenerResult:
    """一次筛选的结果与统计。"""

    as_of: date
    picks: list[dict[str, Any]]
    scored: pd.DataFrame
    stats: dict[str, Any]
    config: ScreenerConfig
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "method": (
                "全市场快照 → 基础过滤（非ST/非北交/价≥阈值/额≥阈值）→ 流动性前N → "
                "260日量价信号 → 截面z-score打分 → 过热剔除 → TopN"
            ),
            "stats": dict(self.stats),
            "warnings": list(self.warnings),
            "picks": list(self.picks),
        }

    def save_json(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        return target


# ---------------------------------------------------------------------------
# 快照标准化
# ---------------------------------------------------------------------------

_SNAPSHOT_ALIASES: dict[str, tuple[str, ...]] = {
    "symbol": ("symbol", "代码", "code", "ts_code"),
    "name": ("name", "名称", "股票名称"),
    "price": ("price", "最新价", "最新", "close"),
    "pct_change": ("pct_change", "涨跌幅", "涨跌幅%"),
    "amount": ("amount", "成交额", "成交额(元)"),
    "volume": ("volume", "成交量"),
    "open": ("open", "今开"),
    "high": ("high", "最高"),
    "low": ("low", "最低"),
    "prev_close": ("prev_close", "昨收"),
}


def _normalize_symbol(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text.startswith(("sh", "sz", "bj")):
        market = text[:2].upper()
        code = text[2:]
        return f"{code}.{market}"
    if "." in text:
        code, market = text.split(".", 1)
        return f"{code}.{market.upper()}"
    if text.isdigit() and len(text) == 6:
        if text.startswith(("6",)):
            return f"{text}.SH"
        if text.startswith(("0", "3")):
            return f"{text}.SZ"
        if text.startswith(("8", "9", "4")):
            return f"{text}.BJ"
    return None


def normalize_snapshot(frame: pd.DataFrame) -> pd.DataFrame:
    """把中文/英文快照统一成 ``symbol,name,price,pct_change,amount,volume,...``。"""

    if frame is None or len(frame) == 0:
        raise ValueError("全市场快照为空")
    renamed: dict[Any, str] = {}
    used: set[str] = set()
    for target, aliases in _SNAPSHOT_ALIASES.items():
        for alias in aliases:
            if alias in frame.columns and target not in used:
                renamed[alias] = target
                used.add(target)
                break
    result = frame.rename(columns=renamed).copy()
    if "symbol" not in result.columns:
        raise ValueError("快照缺少代码列（symbol/代码）")
    result["symbol"] = result["symbol"].map(_normalize_symbol)
    result = result[result["symbol"].notna()].copy()
    for column in ("price", "pct_change", "amount", "volume", "open", "high", "low", "prev_close"):
        if column in result.columns:
            result[column] = pd.to_numeric(result[column], errors="coerce")
    if "name" not in result.columns:
        result["name"] = ""
    result["name"] = result["name"].astype(str)
    return result.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 主服务
# ---------------------------------------------------------------------------


class MarketScreener:
    """全市场筛选器（研究阶段）。"""

    def __init__(
        self,
        config: ScreenerConfig | None = None,
        *,
        data_source: Any = None,
        snapshot_provider: Any = None,
    ) -> None:
        self.config = config or ScreenerConfig()
        self.data_source = data_source
        self.snapshot_provider = snapshot_provider
        self._rate_lock = None
        self._last_request_at = 0.0

    # ------------------------------------------------------------------
    def screen(
        self,
        *,
        as_of: date | str | None = None,
        snapshot: pd.DataFrame | None = None,
        bars: pd.DataFrame | None = None,
        save_path: str | Path | None = None,
    ) -> ScreenerResult:
        """执行一次全市场筛选。

        Args:
            as_of: 快照日期（默认今天/最近交易日）。
            snapshot: 注入快照（测试/回放用）；None 时读取数据层。
            bars: 注入历史行情（长表）；None 时按 ``mode`` 读缓存/增量拉取。
            save_path: 可选，保存 JSON 快照。

        Returns:
            :class:`ScreenerResult`。
        """

        started = time.perf_counter()
        as_of_date = _as_date(as_of)
        warnings: list[str] = []
        snapshot_frame = self._load_snapshot(snapshot)
        stats: dict[str, Any] = {"snapshot_total": int(len(snapshot_frame))}

        filtered = self._basic_filter(snapshot_frame, warnings)
        stats["after_filter"] = int(len(filtered))
        liquidity = filtered.sort_values("amount", ascending=False).head(self.config.liquidity_top_n)
        stats["liquidity_selected"] = int(len(liquidity))
        symbols = [str(item) for item in liquidity["symbol"].tolist()]

        history = self._load_history(symbols, as_of_date, bars, warnings, stats)
        scored = self._compute_signals(history, liquidity, as_of_date, warnings, stats)
        if scored.empty:
            raise ValueError(
                "筛选未产生可用信号：历史数据不足或全部标的被过滤；"
                "可切换 mode='incremental' 补齐历史行情后重试"
            )
        cleaned = self._remove_overheated(scored, warnings)
        picks_frame = cleaned.sort_values("score", ascending=False).head(self.config.top_n).copy()
        picks_frame["reasons"] = picks_frame.apply(_reasons, axis=1)
        picks = _to_records(picks_frame)

        stats.update(
            {
                "scored": int(len(scored)),
                "after_overheat": int(len(cleaned)),
                "picks": int(len(picks)),
                "elapsed_sec": round(time.perf_counter() - started, 2),
                "mode": self.config.mode,
            }
        )
        result = ScreenerResult(
            as_of=as_of_date,
            picks=picks,
            scored=scored,
            stats=stats,
            config=self.config,
            warnings=warnings,
        )
        if save_path is not None:
            result.save_json(save_path)
        return result

    # ------------------------------------------------------------------
    # 数据装载
    # ------------------------------------------------------------------
    def _load_snapshot(self, snapshot: pd.DataFrame | None) -> pd.DataFrame:
        if snapshot is not None:
            return normalize_snapshot(snapshot)
        if self.snapshot_provider is not None:
            provider = self.snapshot_provider
            try:
                raw = provider() if callable(provider) else provider
                return normalize_snapshot(raw)
            except Exception as exc:  # noqa: BLE001
                logger.info("注入的快照 provider 失败，改用数据层：%s", exc)
        try:
            from data.market_snapshot import latest_snapshot  # type: ignore

            frame = latest_snapshot()
            if frame is not None and len(frame) > 0:
                return normalize_snapshot(frame)
        except Exception as exc:  # noqa: BLE001
            logger.info("读取快照库失败：%s", exc)
        # 本地快照 CSV（Lead 原型使用的工作区快照）作为离线兜底
        candidates = [
            self.config.snapshot_path,
            str(Path(__file__).resolve().parents[2] / "data" / "a_spot.csv"),
        ]
        for path in candidates:
            if not path:
                continue
            try:
                if Path(path).exists():
                    return normalize_snapshot(pd.read_csv(path))
            except Exception as exc:  # noqa: BLE001
                logger.info("读取本地快照 %s 失败：%s", path, exc)
        raise ValueError(
            "无法获取全市场快照：请先运行 data.market_snapshot.collect_daily()，"
            "或通过 snapshot= 注入快照 DataFrame"
        )

    def _basic_filter(self, frame: pd.DataFrame, warnings: list[str]) -> pd.DataFrame:
        config = self.config
        data = frame.copy()
        before = len(data)
        data = data[~data["symbol"].astype(str).str.endswith(".BJ")]
        dropped_bj = before - len(data)
        mask_st = data["name"].astype(str).str.contains("ST|退", na=False) if "name" in data.columns else False
        data = data[~mask_st] if not isinstance(mask_st, bool) else data
        dropped_st = before - dropped_bj - len(data)
        if "price" in data.columns:
            data = data[data["price"].notna() & (data["price"] >= config.min_price)]
        if "amount" in data.columns:
            data = data[data["amount"].notna() & (data["amount"] >= config.min_amount)]
        if "volume" in data.columns:
            data = data[data["volume"].fillna(0) > 0]
        dropped_other = before - dropped_bj - dropped_st - len(data)
        if dropped_bj or dropped_st or dropped_other:
            warnings.append(
                f"基础过滤剔除：北交所 {dropped_bj}、ST/退市 {max(dropped_st, 0)}、"
                f"低价/低成交额/停牌 {max(dropped_other, 0)}"
            )
        if len(data) < 10:
            warnings.append(f"基础过滤后仅 {len(data)} 只标的，样本偏少")
        return data.reset_index(drop=True)

    def _load_history(
        self,
        symbols: list[str],
        as_of: date,
        bars: pd.DataFrame | None,
        warnings: list[str],
        stats: dict[str, Any],
    ) -> pd.DataFrame:
        if bars is not None:
            history = normalize_bars(bars)
            stats["history_source"] = "injected"
        else:
            history = self._load_history_from_layer(symbols, as_of, warnings, stats)
        if history is None or len(history) == 0:
            raise ValueError("历史行情为空：请切换 mode='incremental' 或注入 bars")
        history = history[history["symbol"].isin(set(symbols))]
        available = set(history["symbol"].astype(str).unique())
        missing = [symbol for symbol in symbols if symbol not in available]
        stats["history_symbols"] = int(history["symbol"].nunique())
        stats["history_rows"] = int(len(history))
        stats["history_coverage"] = round(len(available) / max(len(symbols), 1), 4)
        if missing:
            warnings.append(
                f"流动性前 {len(symbols)} 只中仅 {len(available)} 只有历史行情（覆盖率 "
                f"{stats['history_coverage']:.0%}），{len(missing)} 只已跳过，示例：{missing[:5]}；"
                "可切换 mode='incremental' 补齐缓存"
            )
        return history

    def _load_history_from_layer(
        self, symbols: list[str], as_of: date, warnings: list[str], stats: dict[str, Any]
    ) -> pd.DataFrame:
        config = self.config
        start = as_of - timedelta(days=max(400, int(config.history_days * 2)))
        history: pd.DataFrame | None = None
        if self.data_source is not None:
            source = self.data_source
            if hasattr(source, "load"):
                try:
                    history = source.load(symbols, start, as_of, adjust="none")
                except TypeError:
                    history = source.load(symbols, start, as_of)
            elif hasattr(source, "get_bars"):
                history = source.get_bars(symbols, start, as_of, "1d")
            stats["history_source"] = f"data_source:{type(source).__name__}"
        if history is None or len(history) == 0:
            try:
                from data.cache import BarCache  # type: ignore

                cache = BarCache()
                if config.mode == "incremental":
                    try:
                        report = cache.update(symbols, start, as_of)
                        stats["update"] = _update_report_dict(report)
                    except Exception as exc:  # noqa: BLE001
                        warnings.append(f"增量更新失败（继续使用缓存）：{type(exc).__name__}: {exc}")
                try:
                    history = cache.load(symbols, start, as_of, adjust="none")
                except TypeError:  # 兼容旧签名
                    history = cache.load(symbols, start, as_of)
                stats["history_source"] = f"BarCache({config.mode})"
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"本地缓存不可用：{type(exc).__name__}: {exc}")
        if history is None or len(history) == 0:
            try:  # 最后兜底：Provider 直接取数（可能联网）
                from data.provider import build_provider  # type: ignore

                provider = build_provider()
                history = provider.get_bars(symbols, start, as_of, "1d")
                stats["history_source"] = "provider"
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"数据源取历史失败：{type(exc).__name__}: {exc}")
        if history is None:
            return pd.DataFrame()
        usable = set(history["symbol"].astype(str).unique())
        missing = [symbol for symbol in symbols if symbol not in usable]
        if missing:
            warnings.append(f"{len(missing)} 只标的缺少历史行情（已跳过），示例：{missing[:5]}")
        return normalize_bars(history) if len(history) else history

    # ------------------------------------------------------------------
    # 信号与打分
    # ------------------------------------------------------------------
    def _compute_signals(
        self,
        history: pd.DataFrame,
        liquidity: pd.DataFrame,
        as_of: date,
        warnings: list[str],
        stats: dict[str, Any],
    ) -> pd.DataFrame:
        config = self.config
        snapshot_info = liquidity.set_index("symbol")
        rows: list[dict[str, Any]] = []
        for symbol, group in history.groupby("symbol", sort=False):
            group = group.sort_values("date").reset_index(drop=True)
            if len(group) < config.min_history:
                continue
            signals = _compute_single_signals(group)
            if signals is None:
                continue
            record = {"symbol": str(symbol)}
            if symbol in snapshot_info.index:
                info = snapshot_info.loc[symbol]
                record.update(
                    {
                        "name": str(info.get("name", "") or ""),
                        "price": _float(info.get("price")),
                        "pct_change": _float(info.get("pct_change")),
                        "amount": _float(info.get("amount")),
                    }
                )
            else:
                record.update({"name": "", "price": None, "pct_change": None, "amount": None})
            record.update(signals)
            rows.append(record)
        if not rows:
            return pd.DataFrame()
        frame = pd.DataFrame(rows)
        frame["amount_yi"] = frame["amount"].fillna(0) / 1e8
        # 成交额分位（截面）
        frame["amount_pct"] = frame["amount_yi"].rank(pct=True) if len(frame) > 1 else 0.5
        frame["score"] = _weighted_score(frame, config.weights)
        stats["signal_symbols"] = int(len(frame))
        return frame

    def _remove_overheated(self, scored: pd.DataFrame, warnings: list[str]) -> pd.DataFrame:
        config = self.config
        data = scored.copy()
        flags: list[list[str]] = [[] for _ in range(len(data))]
        for index, (_, row) in enumerate(data.iterrows()):
            if _float(row.get("ret5")) is not None and float(row["ret5"]) > config.max_ret5:
                flags[index].append("5日涨幅过热")
            if _float(row.get("dist_ma20")) is not None and float(row["dist_ma20"]) > config.max_dist_ma20:
                flags[index].append("远离MA20")
            if _float(row.get("atr_pct")) is not None and float(row["atr_pct"]) > config.max_atr_pct:
                flags[index].append("波动过大")
            if _float(row.get("consec_up")) is not None and float(row["consec_up"]) > config.max_consec_up:
                flags[index].append("连涨过长")
        data["risk_flags"] = ["、".join(items) for items in flags]
        overheat = data["risk_flags"].str.len() > 0
        removed = int(overheat.sum())
        if removed:
            warnings.append(f"过热剔除 {removed} 只（5日涨幅/远离MA20/波动/连涨超阈值）")
        return data[~overheat].reset_index(drop=True)


# ---------------------------------------------------------------------------
# 单标的信号 / 打分 / 理由
# ---------------------------------------------------------------------------


def _compute_single_signals(group: pd.DataFrame) -> dict[str, float] | None:
    close = pd.to_numeric(group["close"], errors="coerce").astype("float64")
    high = pd.to_numeric(group["high"], errors="coerce").astype("float64")
    low = pd.to_numeric(group["low"], errors="coerce").astype("float64")
    volume = pd.to_numeric(group["volume"], errors="coerce").astype("float64")
    if len(close) < 120 or close.iloc[-1] <= 0:
        return None
    returns = close.pct_change().dropna()
    last = float(close.iloc[-1])
    mom20 = last / float(close.iloc[-21]) - 1 if len(close) > 21 else None
    mom60 = last / float(close.iloc[-61]) - 1 if len(close) > 61 else None
    vol20 = float(returns.iloc[-20:].std(ddof=1)) * math.sqrt(252) if len(returns) >= 20 else None
    volume_ratio = (
        float(volume.iloc[-1]) / float(volume.iloc[-21:-1].mean()) if len(volume) > 21 else 1.0
    )
    hh20 = float(high.iloc[-21:-1].max()) if len(high) > 21 else float(high.max())
    breakout20 = last / hh20 - 1 if hh20 > 0 else None
    ma20 = float(close.iloc[-20:].mean())
    ma60 = float(close.iloc[-60:].mean())
    dist_ma20 = last / ma20 - 1 if ma20 > 0 else None
    dist_ma60 = last / ma60 - 1 if ma60 > 0 else None
    true_range = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1
    ).max(axis=1)
    atr_pct = float(true_range.iloc[-14:].mean()) / last if last > 0 else None
    consec_up = 0
    for value in returns.iloc[::-1]:
        if value > 0:
            consec_up += 1
        else:
            break
    ret5 = last / float(close.iloc[-6]) - 1 if len(close) > 6 else 0.0
    high_250 = float(high.iloc[-250:].max()) if len(high) >= 250 else float(high.max())
    near_high = last / high_250 - 1 if high_250 > 0 else None
    return {
        "mom20": mom20,
        "mom60": mom60,
        "vol20": vol20,
        "vol_ratio": volume_ratio,
        "breakout20": breakout20,
        "dist_ma20": dist_ma20,
        "dist_ma60": dist_ma60,
        "atr_pct": atr_pct,
        "consec_up": float(consec_up),
        "ret5": ret5,
        "near_high_250": near_high,
    }


def _zs(series: pd.Series) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce")
    if len(values) < 2:
        return pd.Series([0.0] * len(values), index=values.index)
    std = float(values.std(ddof=1))
    mean = float(values.mean())
    if not math.isfinite(std) or std <= 0:
        return pd.Series([0.0] * len(values), index=values.index)
    return (values - mean) / std


def _weighted_score(frame: pd.DataFrame, weights: Mapping[str, float]) -> pd.Series:
    components = {
        "mom60": _zs(frame["mom60"].fillna(0.0)),
        "mom20": _zs(frame["mom20"].fillna(0.0)),
        "vol_ratio": _zs(frame["vol_ratio"].clip(upper=6).fillna(1.0)),
        "breakout20": _zs(frame["breakout20"].clip(-0.3, 0.3).fillna(0.0)),
        "low_vol": _zs((-frame["vol20"]).fillna(0.0)),
        "amount": _zs(frame["amount_yi"].fillna(0.0)),
        "ma20_closeness": _zs((-frame["dist_ma20"].abs()).fillna(1.0)),
        "near_high": _zs(frame["near_high_250"].fillna(-1.0)),
    }
    score = pd.Series(0.0, index=frame.index, dtype="float64")
    for name, weight in weights.items():
        if name in components:
            score = score + float(weight) * components[name]
    return score


def _reasons(row: pd.Series) -> list[str]:
    reasons: list[str] = []
    if (value := _float(row.get("mom60"))) is not None and value > 0.10:
        reasons.append(f"60日动量 +{value * 100:.0f}%")
    if (value := _float(row.get("mom20"))) is not None and value > 0.05:
        reasons.append(f"20日动量 +{value * 100:.0f}%")
    if (value := _float(row.get("vol_ratio"))) is not None and value > 1.5:
        reasons.append(f"量比 {value:.2f}（放量）")
    if (value := _float(row.get("breakout20"))) is not None and value > 0:
        reasons.append("突破20日高点")
    if (value := _float(row.get("near_high_250"))) is not None and -0.05 < value < 0.02:
        reasons.append("接近一年新高")
    if (value := _float(row.get("dist_ma20"))) is not None and value < 0.05:
        reasons.append("贴近MA20（不追高）")
    return reasons or ["综合动量占优"]


def _to_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        records.append(
            {
                "symbol": str(row["symbol"]),
                "name": str(row.get("name", "") or ""),
                "score": round(float(row["score"]), 4),
                "price": _float(row.get("price")),
                "pct_change": _float(row.get("pct_change")),
                "amount_yi": _float(row.get("amount_yi")),
                "mom20": _float(row.get("mom20")),
                "mom60": _float(row.get("mom60")),
                "vol_ratio": _float(row.get("vol_ratio")),
                "breakout20": _float(row.get("breakout20")),
                "dist_ma20": _float(row.get("dist_ma20")),
                "atr_pct": _float(row.get("atr_pct")),
                "near_high_250": _float(row.get("near_high_250")),
                "consec_up": _float(row.get("consec_up")),
                "reasons": list(row.get("reasons", [])),
                "risk_flags": [],
                "research_stage": True,
                "disclaimer": "研究阶段规则筛选，未经统计验证，不构成买入建议",
            }
        )
    return records


def _update_report_dict(report: Any) -> dict[str, Any]:
    if report is None:
        return {}
    if hasattr(report, "model_dump"):
        try:
            return dict(report.model_dump())
        except Exception:  # noqa: BLE001
            pass
    return {key: value for key, value in getattr(report, "__dict__", {}).items() if not key.startswith("_")}


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return date.today()
    try:
        return pd.Timestamp(value).date()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"as_of 日期非法：{value!r}") from exc

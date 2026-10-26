#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M6 扩样本构建：短线研究股票池 50 → 200（默认），并产出覆盖审计。

本脚本是 ``scripts/build_universe_legacy50.py`` 的**参数化副本**（原脚本不改），
用于 M6 阶段把可回测股票池从 50 只扩到 200 只（验收下限 180 只），产出：

- ``data/universe_200.json``                       股票池（顶层数组，含 symbol/name/board…）
- ``runtime/short_term/bars_200.csv``              长表日线（不复权 OHLCV + 后复权因子）
- ``runtime/short_term/build_report_200.json``     构建过程统计 + 审计结论（机器可读）
- ``runtime/exports/universe_coverage_200.md``     数据覆盖审计报告（人读，含 PASS/FAIL）

数据口径（fail-closed）
------------------------
- 行情：akshare **Sina** ``stock_zh_a_daily(symbol, adjust="")``，**不复权**（撮合用）；
- 因子：akshare **Sina** ``stock_zh_a_daily(symbol, adjust="hfq-factor")``，**后复权因子**，
  按除权日 asof 向后合并（``后复权价 = 不复权价 × adj_factor``）。口径与
  ``core/models.py``（``adj_factor: 后复权因子``）、``data/akshare_provider.py``
  （``hfq_factor``）以及 legacy ``bars_50.csv`` 完全一致；
- ``source`` 列恒为 ``akshare``；任何 mock/合成数据都不会写入（本脚本没有 mock 路径）；
- 不写 ``runtime/quantdesk.db``（本脚本只产出 JSON/CSV/MD）。

筛选规则
--------
1) 全市场快照（工作区 ``data/a_spot.csv``，Sina，2026-10-25）初筛：四板块
   （沪主板 600/601/603/605、深主板 000/001/002/003、创业板 300/301、科创板 688/689）；
   剔除 ST/*ST/退市、非沪深四板块（北交所等）、价格<3 元、停牌/无成交；
2) 候选池按板块目标配额**等比例放大**（``--candidates``），保证各板块都有足够候选；
3) 逐只拉 2019-01-02 ~ 2026-10-23 不复权日线并核验：上市满 2 年（首根 bar ≤ 2024-10-23）、
   末根 bar ≥ 2026-10-16（近期未停牌）、**近 250 个交易日（全市场参考日历）至少 240 根**、
   最新收盘 ≥ 3 元；
4) 通过者按「近 20 日平均成交额」在板块配额内择优，配额不足时从剩余池按成交额补齐；
5) 取后复权因子（失败重试 1 次后跳过并记录原因，缺口由后备标的补齐），写长表。

用法
----
    .venv\\Scripts\\python.exe scripts\\build_universe_m6.py                    # 全量构建（约 15 分钟）
    .venv\\Scripts\\python.exe scripts\\build_universe_m6.py --limit 200 --candidates 280 --sleep 0.25
    .venv\\Scripts\\python.exe scripts\\build_universe_m6.py --audit-only       # 离线重算审计（不联网）
    .venv\\Scripts\\python.exe scripts\\build_universe_m6.py --refresh-factors  # 只重取 adj_factor
    .venv\\Scripts\\python\\python.exe scripts\\build_universe_m6.py --fix-names # 官方名称校正

退出码：0 = 股票池 ≥180 只且审计 PASS；1 = 未达标或审计 FAIL。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import socket
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd

# 项目根目录（scripts/build_universe_m6.py -> quantdesk/）
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "runtime" / "short_term"

DEFAULT_OUT_JSON = DATA_DIR / "universe_200.json"
DEFAULT_OUT_CSV = OUT_DIR / "bars_200.csv"
DEFAULT_OUT_REPORT = OUT_DIR / "build_report_200.json"
DEFAULT_COVERAGE = ROOT / "runtime" / "exports" / "universe_coverage_200.md"
DEFAULT_CROSSSECTION = ROOT / "runtime" / "exports" / "universe_200_crosssection.md"

# 工作区级全市场快照（default-workspace/data/a_spot.csv，Sina，2026-10-25，5233 行）
SPOT_CSV = ROOT.parent / "data" / "a_spot.csv"
# 参考交易日历来源（legacy 池 50 只 × 1892 个交易日，只读，不修改）
LEGACY_CSV = OUT_DIR / "bars_50.csv"

# 日线区间（与短线模型 v2 / legacy50 约定一致）
BAR_START = date(2019, 1, 1)
BAR_END = date(2026, 10, 23)
SPOT_DATE = "2026-10-25"

# 筛选门槛（与任务书一致）
MIN_LISTED_BEFORE = date(2024, 10, 23)   # 上市满 2 年：首根 bar 必须 ≤ 此日
FRESH_AFTER = date(2026, 10, 16)         # 近期未长期停牌：末根 bar 必须 ≥ 此日
RECENT_WINDOW = 250                      # 近 250 个交易日…
RECENT_MIN_BARS = 240                    # …至少 240 根
MIN_PRICE = 3.0
MIN_POOL = 180                           # 验收下限（目标 200）
REQUEST_TIMEOUT = 15                     # 单请求 socket 超时（秒），否则可能挂死
RETRY_ATTEMPTS = 2                       # 首次 + 重试 1 次

# 板块识别：代码前缀 → (board, board_cn)
BOARD_RULES: list[tuple[tuple[str, ...], str, str]] = [
    (("600", "601", "603", "605"), "MAIN", "沪主板"),
    (("000", "001", "002", "003"), "MAIN", "深主板"),
    (("300", "301"), "GEM", "创业板"),
    (("688", "689"), "STAR", "科创板"),
]
# legacy50 的板块配额（合计 50）：主板 28（沪 14 + 深 14）、创业板 12、科创板 10。
# M6 按 --limit 等比例放大（默认 200 → 沪 56 / 深 56 / 创业板 48 / 科创板 40）。
BASE_QUOTA: dict[str, int] = {"MAIN_SH": 14, "MAIN_SZ": 14, "GEM": 12, "STAR": 10}

BAR_COLUMNS = ["date", "symbol", "open", "high", "low", "close",
               "volume", "amount", "adj_factor", "source"]

# 抓取过程中发现的「零值/无成交占位 bar」（新浪对停牌日有时返回 OHLC=0、量额=0 的行）。
# 语义上停牌日不应有 bar，因此剔除且不补造；构建报告会记录明细。
DROPPED_INVALID_BARS: list[dict] = []

# 口径偏差正式说明（task 文字 qfq-factor vs 实现 hfq-factor），同时写入 build_report 与覆盖审计报告。
FACTOR_ADJUST_NOTE = (
    "task 文字要求 adj_factor 用 adjust='qfq-factor'，实际实现采用 adjust='hfq-factor'（后复权因子）："
    "本项目 adj_factor 语义已固定为后复权因子（core/models.py 字段注释「后复权因子」、"
    "data/akshare_provider.py 合并 hfq_factor、legacy bars_50.csv 均为 hfq 口径），"
    "必须与 DB/回测引擎保持一致。对同一标的 hfq_factor(t)×qfq_factor(t)=常数 K，"
    "故 close×adj_factor 的复权序列在两种口径下只差常数倍，"
    "相邻日收益率/future_ret_h/累计收益/RankIC/命中率完全等价；QA 可用 close×adj_factor 断言 future_ret。"
    "本次采用 hfq 口径。"
)


@dataclass(frozen=True)
class Paths:
    """本脚本的四个产出路径（构建 / 审计共用）。"""

    json: Path
    csv: Path
    report: Path
    coverage: Path


# --------------------------------------------------------------------------- #
# 基础设施
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def prepare_env() -> None:
    """绕过系统代理（本机系统代理对 akshare 不可用，直连才通）+ 设置 socket 超时。"""
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    socket.setdefaulttimeout(REQUEST_TIMEOUT)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def rel(path: Path) -> str:
    """相对项目根的展示路径（失败时回退绝对路径）。"""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def rel_md(path: Path) -> str:
    """Markdown 内展示用的相对路径（统一正斜杠）。"""
    return rel(path).replace("\\", "/")


def _is_finite(value: object) -> bool:
    try:
        return math.isfinite(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _to_float(value: object) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------- #
# 快照 / 初筛（复制 legacy，语义不变）
# --------------------------------------------------------------------------- #
def clean_name(name: str) -> str:
    """去掉瞬时除权/新股前缀（XD/XR/DR/N/C），保证名称干净可复用。"""
    text = str(name or "").strip()
    for prefix in ("XD", "XR", "DR", "N", "C"):
        if text.startswith(prefix) and len(text) > len(prefix):
            text = text[len(prefix):]
            break
    return text


def board_of(code6: str) -> tuple[str, str] | None:
    """6 位代码 → (board, board_cn)；不属于 A 股四板块（如北交所 8/920/4）返回 None。"""
    for prefixes, board, board_cn in BOARD_RULES:
        if code6.startswith(prefixes):
            return board, board_cn
    return None


def spot_code_to_symbol(raw: str) -> tuple[str, str] | None:
    """``sh600519`` / ``sz000001`` → (``600519.SH``, ``600519``)；非沪深返回 None。"""
    text = str(raw or "").strip().lower()
    if len(text) != 8 or text[:2] not in ("sh", "sz"):
        return None
    code6 = text[2:]
    if not code6.isdigit():
        return None
    suffix = "SH" if text[:2] == "sh" else "SZ"
    return f"{code6}.{suffix}", code6


def load_spot_rows(refresh: bool) -> tuple[list[dict], str, int]:
    """读取全市场快照，返回 (rows, 来源描述, 解析跳过行数)。"""
    skipped = 0
    if refresh or not SPOT_CSV.exists():
        log("快照文件缺失或要求刷新 → 调用 akshare.stock_zh_a_spot()（Sina，约 15s）")
        import akshare as ak
        df = ak.stock_zh_a_spot()
        rows = []
        for rec in df.to_dict("records"):
            parsed = spot_code_to_symbol(rec.get("代码", ""))
            if not parsed:
                skipped += 1
                continue
            symbol, code6 = parsed
            rows.append({"code": symbol, "code6": code6, "name": str(rec.get("名称", "")),
                         "price": _to_float(rec.get("最新价")), "amount": _to_float(rec.get("成交额"))})
        return rows, "akshare.stock_zh_a_spot (Sina)", skipped

    rows = []
    with SPOT_CSV.open("r", encoding="utf-8-sig", newline="") as fh:
        for rec in csv.DictReader(fh):
            parsed = spot_code_to_symbol(rec.get("代码", ""))
            if not parsed:
                skipped += 1
                continue
            symbol, code6 = parsed
            rows.append({"code": symbol, "code6": code6, "name": str(rec.get("名称", "")),
                         "price": _to_float(rec.get("最新价")), "amount": _to_float(rec.get("成交额"))})
    return rows, str(SPOT_CSV), skipped


def screen_spot(rows: list[dict], parse_skipped: int = 0) -> tuple[list[dict], dict]:
    """规则初筛：四板块 + 非 ST/退 + 价格≥3 + 有成交（非停牌）+ 按成交额降序。"""
    stats: dict = {"total": len(rows) + parse_skipped}
    kept: list[dict] = []
    dropped: Counter = Counter()
    if parse_skipped:
        dropped["非沪深四板块(北交所/格式异常)"] += parse_skipped
    for row in rows:
        board_info = board_of(row["code6"])
        name = row["name"]
        if board_info is None:
            dropped["非沪深四板块(北交所/格式异常)"] += 1
            continue
        if "ST" in name.upper() or "退" in name:
            dropped["ST/退市"] += 1
            continue
        if row["price"] < MIN_PRICE:
            dropped["价格<3元"] += 1
            continue
        if row["amount"] <= 0:
            dropped["停牌/无成交"] += 1
            continue
        board, board_cn = board_info
        # 注意：配额字典的 key 是 MAIN_SH / MAIN_SZ / GEM / STAR（legacy 脚本此处误用中文名
        # 作为 GEM/STAR 的 board_key，导致这两块配额空转，M6 已修正）。
        board_key = "MAIN_SH" if board_cn == "沪主板" else \
                    "MAIN_SZ" if board_cn == "深主板" else board
        row.update({"board": board, "board_cn": board_cn,
                    "name": clean_name(name), "board_key": board_key})
        kept.append(row)
    kept.sort(key=lambda r: r["amount"], reverse=True)
    stats["kept"] = len(kept)
    stats["dropped"] = dict(dropped)
    return kept, stats


# --------------------------------------------------------------------------- #
# akshare 拉取（Sina；失败重试 1 次后跳过）
# --------------------------------------------------------------------------- #
def to_sina(symbol: str) -> str:
    """``600519.SH`` → ``sh600519``。"""
    return ("sh" if symbol.endswith(".SH") else "sz") + symbol[:6]


def fetch_bars(symbol: str) -> list[dict]:
    """Sina 不复权日线（全历史），只保留 BAR_START ~ BAR_END。失败抛异常。

    新浪对**停牌日**有时返回「OHLC=0、volume=0、amount=0」的占位行。此类行不是真实成交，
    按「无 bar = 不可交易」剔除（不补造），明细记入 ``DROPPED_INVALID_BARS``。
    """
    import akshare as ak
    df = ak.stock_zh_a_daily(symbol=to_sina(symbol), adjust="")
    out: list[dict] = []
    dropped_local: list[dict] = []
    for rec in df.to_dict("records"):
        raw = rec["date"]
        d = raw.date() if hasattr(raw, "date") else date.fromisoformat(str(raw)[:10])
        if d < BAR_START or d > BAR_END:
            continue
        ohlc = [float(rec[k]) for k in ("open", "high", "low", "close")]
        volume = float(rec["volume"]) if _is_finite(rec.get("volume")) else float("nan")
        amount = float(rec["amount"]) if _is_finite(rec.get("amount")) else float("nan")
        if (not all(math.isfinite(v) for v in ohlc) or any(v <= 0 for v in ohlc)
                or not math.isfinite(volume) or volume <= 0
                or not math.isfinite(amount) or amount <= 0):
            dropped_local.append({"symbol": symbol, "date": d.isoformat(),
                                  "open": ohlc[0], "high": ohlc[1], "low": ohlc[2], "close": ohlc[3],
                                  "volume": None if not math.isfinite(volume) else volume,
                                  "amount": None if not math.isfinite(amount) else amount})
            continue
        out.append({"date": d.isoformat(), "open": ohlc[0], "high": ohlc[1],
                    "low": ohlc[2], "close": ohlc[3], "volume": volume, "amount": amount})
    out.sort(key=lambda b: b["date"])
    if dropped_local:
        log(f"    {symbol} 剔除 {len(dropped_local)} 行零值/无成交占位 bar（按停牌处理，不补造）："
            f"{', '.join(item['date'] for item in dropped_local[:5])}")
        DROPPED_INVALID_BARS.extend(dropped_local)
    return out


def fetch_hfq_factor(symbol: str) -> list[tuple[date, float]]:
    """Sina **后复权因子**（date, hfq_factor），按日期升序；失败抛异常。

    口径与 ``data/akshare_provider.py`` / legacy ``bars_50.csv`` 一致：
    ``后复权价 = 不复权价 × hfq_factor``。因子按除权日离散变化，使用时需 asof 向后合并。
    """
    import akshare as ak
    df = ak.stock_zh_a_daily(symbol=to_sina(symbol), adjust="hfq-factor")
    pairs: list[tuple[date, float]] = []
    for rec in df.to_dict("records"):
        raw = rec["date"]
        d = raw.date() if hasattr(raw, "date") else date.fromisoformat(str(raw)[:10])
        for key in ("hfq_factor", "qfq_factor"):
            if key in rec:
                try:
                    pairs.append((d, float(rec[key])))
                except (TypeError, ValueError):
                    pass
                break
    pairs.sort()
    return pairs


def factor_asof(pairs: list[tuple[date, float]], target: date) -> float | None:
    """取 <= target 的最近一次因子值（除权日生效）。"""
    value = None
    for d, f in pairs:
        if d <= target:
            value = f
        else:
            break
    return value


def fetch_with_retry(fetch_fn, symbol: str, what: str) -> tuple[object | None, str | None]:
    """单标的拉取：失败重试 1 次（共 RETRY_ATTEMPTS 次），仍失败返回 (None, 错误摘要)。"""
    last_error = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return fetch_fn(symbol), None
        except Exception as exc:  # noqa: BLE001 - 单只失败不影响整体
            last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
            log(f"    {symbol} {what} 第 {attempt}/{RETRY_ATTEMPTS} 次失败：{last_error}")
            if attempt < RETRY_ATTEMPTS:
                time.sleep(0.3)
    return None, last_error


# --------------------------------------------------------------------------- #
# 交易日历 / 核验窗口 / 选股
# --------------------------------------------------------------------------- #
def load_reference_calendar() -> tuple[list[date], str]:
    """加载全市场参考交易日历（用于「近 250 个交易日」窗口）。

    优先读取 legacy ``bars_50.csv``（50 只 × 2019-01-02~2026-10-23，1892 个交易日，只读），
    缺失时回退 akshare 交易日历 ``tool_trade_date_hist_sina()``。返回 (dates, 来源说明)。
    """
    if LEGACY_CSV.exists():
        frame = pd.read_csv(LEGACY_CSV, usecols=["date"], dtype={"date": str})
        dates = sorted({date.fromisoformat(str(d)[:10]) for d in frame["date"]})
        dates = [d for d in dates if BAR_START <= d <= BAR_END]
        return dates, f"runtime/short_term/bars_50.csv 交易日并集（{len(dates)} 个交易日，只读）"
    import akshare as ak
    frame = ak.tool_trade_date_hist_sina()
    dates = sorted({(d.date() if hasattr(d, "date") else date.fromisoformat(str(d)[:10]))
                    for d in frame["trade_date"]})
    dates = [d for d in dates if BAR_START <= d <= BAR_END]
    return dates, f"akshare.tool_trade_date_hist_sina()（{len(dates)} 个交易日）"


def window_stats(bars: list[dict], recent_iso: set[str], ref_dates: list[date]) -> dict:
    """近 20 日平均成交额、**近 250 个参考交易日**内 bar 数、首末日、总行数。"""
    if not bars:
        return {"first": None, "last": None, "rows": 0, "avg_amount_20d": 0.0,
                "recent_bars": 0, "last_close": None}
    tail = bars[-20:]
    amounts = [b["amount"] for b in tail if _is_finite(b.get("amount"))]
    avg20 = sum(amounts) / len(amounts) if amounts else 0.0
    return {
        "first": bars[0]["date"], "last": bars[-1]["date"], "rows": len(bars),
        "avg_amount_20d": round(avg20, 2),
        "recent_bars": sum(1 for b in bars if b["date"] in recent_iso),
        "last_close": bars[-1]["close"],
    }


def _proportional(total: int, weights: dict[str, float]) -> dict[str, int]:
    """按权重把 total 个整数名额分给各 key（最大余数法），保证合计 = total。"""
    keys = list(weights)
    if not keys or total <= 0:
        return {k: 0 for k in keys}
    denom = sum(weights.values())
    if denom <= 0:
        return {k: 0 for k in keys}
    raw = {k: total * weights[k] / denom for k in keys}
    out = {k: int(math.floor(raw[k])) for k in keys}
    remainder = total - sum(out.values())
    for k in sorted(keys, key=lambda key: raw[key] - out[key], reverse=True)[:remainder]:
        out[k] += 1
    return out


def scaled_quota(limit: int) -> dict[str, int]:
    """把 legacy50 的板块配额等比例放大到 --limit（默认 200 → 56/56/48/40）。"""
    return _proportional(limit, {k: float(v) for k, v in BASE_QUOTA.items()})


def allocate_candidates(kept: list[dict], candidates: int, quota: dict[str, int]) -> tuple[list[dict], dict]:
    """候选池按板块目标配额等比例放大（1.4×），保证各板块候选充足。"""
    by_key: dict[str, list[dict]] = {}
    for row in kept:
        by_key.setdefault(row["board_key"], []).append(row)
    for key in by_key:
        by_key[key].sort(key=lambda r: r["amount"], reverse=True)

    alloc = {k: 0 for k in quota}
    weights = {k: float(v) for k, v in quota.items()}
    active = set(quota)
    remaining = candidates
    while remaining > 0 and active:
        share = _proportional(remaining, {k: weights[k] for k in active})
        moved = 0
        for key in list(active):
            room = len(by_key.get(key, [])) - alloc[key]
            want = max(0, min(share[key], room))
            alloc[key] += want
            remaining -= want
            moved += want
            if alloc[key] >= len(by_key.get(key, [])):
                active.discard(key)
        if moved == 0:
            break

    picked: list[dict] = []
    used: set[str] = set()
    for key in quota:
        for row in by_key.get(key, [])[:alloc[key]]:
            picked.append(row)
            used.add(row["code"])
    # 极端情况下四板块供给不足 → 用其余快照标的按成交额补齐
    if len(picked) < candidates:
        for row in kept:
            if len(picked) >= candidates:
                break
            if row["code"] not in used:
                picked.append(row)
                used.add(row["code"])
    picked.sort(key=lambda r: r["amount"], reverse=True)
    return picked[:candidates], alloc


def pick_universe(verified: list[dict], limit: int, quota: dict[str, int]) -> list[dict]:
    """按板块配额择优：配额内按近 20 日平均成交额降序；不足则从剩余池按成交额补齐。"""
    by_key: dict[str, list[dict]] = {}
    for item in verified:
        by_key.setdefault(item["board_key"], []).append(item)
    for key in by_key:
        by_key[key].sort(key=lambda r: r["avg_amount_20d"], reverse=True)

    picked: list[dict] = []
    for key, q in quota.items():
        picked.extend(by_key.get(key, [])[:q])
    picked_codes = {item["code"] for item in picked}
    rest = sorted([v for v in verified if v["code"] not in picked_codes],
                  key=lambda r: r["avg_amount_20d"], reverse=True)
    shortfall = limit - len(picked)
    if shortfall > 0:
        picked.extend(rest[:shortfall])
    picked.sort(key=lambda r: r["avg_amount_20d"], reverse=True)
    return picked[:limit]


# --------------------------------------------------------------------------- #
# 文件读写
# --------------------------------------------------------------------------- #
def load_universe_items(path: Path) -> list[dict]:
    """读取股票池（兼容两种格式：顶层数组 / {meta, universe}）；统一返回 list。"""
    if not path.exists():
        raise SystemExit(f"股票池文件不存在：{path}（先运行完整构建）")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return raw
    return raw.get("universe", [])


def load_report_or_empty(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def load_universe_meta(paths: Paths) -> dict:
    """读取股票池元信息：优先 build_report 的 universe_meta，其次 JSON 内嵌 meta。"""
    meta = load_report_or_empty(paths.report).get("universe_meta", {}) or {}
    if not meta and paths.json.exists():
        raw = json.loads(paths.json.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            meta = raw.get("meta", {}) or {}
    return meta


def save_universe(paths: Paths, items: list[dict], meta: dict) -> None:
    """写股票池：**顶层数组**（便于消费方直接遍历）；meta 落到 build_report 的 universe_meta。"""
    paths.json.parent.mkdir(parents=True, exist_ok=True)
    paths.json.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    report = load_report_or_empty(paths.report)
    report["universe_meta"] = meta
    paths.report.parent.mkdir(parents=True, exist_ok=True)
    paths.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def read_bars_frame(path: Path) -> pd.DataFrame:
    """读取已生成的日线长表（pandas）。"""
    if not path.exists():
        raise SystemExit(f"日线文件不存在：{path}（先运行完整构建）")
    return pd.read_csv(path, dtype={"symbol": str, "date": str})


# --------------------------------------------------------------------------- #
# 审计（离线，纯函数：不联网、不写文件，返回 dict + Markdown 文本）
# --------------------------------------------------------------------------- #
def _categorize_failure(reason: str) -> str:
    """把自由文本的失败原因归类，便于计数。"""
    text = str(reason or "")
    if text.startswith("拉取失败"):
        return "行情拉取失败（重试 1 次后跳过）"
    if text.startswith("无 2019"):
        return "无 2019 以来日线"
    if text.startswith("上市不足"):
        return "上市不足 2 年"
    if text.startswith("近期停牌"):
        return "近期长期停牌（末根 bar 过旧）"
    if text.startswith("近 250"):
        return "近 250 交易日 bar 不足 240 根"
    if text.startswith("历史 bar"):
        return "历史 bar 不足 800 根"
    if text.startswith("最新收盘"):
        return "最新收盘 <3 元"
    if text.startswith("后复权因子"):
        return "后复权因子获取失败/覆盖不足"
    return "其他"


def render_audit(paths: Paths) -> tuple[dict, str]:
    """对已落盘的 universe/bars/report 做离线覆盖审计。

    返回 (audit_dict, markdown_text)。**不写任何文件、不联网**。
    """
    universe = load_universe_items(paths.json)
    report = load_report_or_empty(paths.report)
    frame = read_bars_frame(paths.csv)

    # ---- 规范化 ----
    if "date" in frame.columns:
        frame["date"] = frame["date"].astype(str)
    if "symbol" in frame.columns:
        frame["symbol"] = frame["symbol"].astype(str)
    total_rows = int(len(frame))
    all_dates = sorted(frame["date"].dropna().unique().tolist()) if total_rows else []
    symbols_in_csv = list(dict.fromkeys(frame["symbol"].tolist())) if total_rows else []
    n_symbols = len(symbols_in_csv)

    # 全池并集交易日（以 CSV 中实际出现的日期为参照；真实交易日才会出现 bar）
    union_dates = all_dates
    union_set = set(union_dates)

    # ---- 全表级质量 ----
    dup_rows = int(frame.duplicated(subset=["date", "symbol"]).sum()) if total_rows else 0
    source_values = sorted(str(v) for v in frame["source"].dropna().unique()) if total_rows else []
    source_null = int(frame["source"].isna().sum()) if total_rows else 0
    mixed_source_symbols = (int(frame.groupby("symbol")["source"].nunique().gt(1).sum())
                            if total_rows else 0)

    factor_raw = pd.to_numeric(frame["adj_factor"], errors="coerce") if "adj_factor" in frame else pd.Series(dtype=float)
    factor_missing = int(frame["adj_factor"].isna().sum()) if "adj_factor" in frame else total_rows
    factor_nonnumeric = int(factor_raw.isna().sum())
    factor_nonpositive = int((factor_raw <= 0).sum())

    price_bad = int(((frame[["open", "high", "low", "close"]] <= 0).any(axis=1)).sum()) if total_rows else 0
    range_bad = int((frame["high"] < frame["low"]).sum()) if total_rows else 0
    ohlc_bad = 0
    if total_rows:
        ohlc_bad += int((frame["high"] < frame[["open", "close"]].max(axis=1) - 1e-9).sum())
        ohlc_bad += int((frame["low"] > frame[["open", "close"]].min(axis=1) + 1e-9).sum())
    amount_na = int(frame["amount"].isna().sum()) if "amount" in frame else total_rows
    amount_nonpos = int((frame["amount"].fillna(0) <= 0).sum()) if total_rows else 0
    volume_na = int(frame["volume"].isna().sum()) if "volume" in frame else total_rows
    volume_nonpos = int((frame["volume"].fillna(0) <= 0).sum()) if total_rows else 0
    union_days = len(union_dates)

    # ---- 逐只统计（缺口以全池并集交易日为参照，且限定在该标的自身首末日区间内）----
    subset = frame[["date", "symbol"]].copy() if total_rows else pd.DataFrame(columns=["date", "symbol"])
    grouped = {sym: grp["date"].tolist() for sym, grp in subset.groupby("symbol")} if total_rows else {}
    by_symbol_rows = {sym: len(dates) for sym, dates in grouped.items()}

    json_by_symbol = {str(u.get("symbol")): u for u in universe}
    per_symbol: list[dict] = []
    for sym in symbols_in_csv:
        dates = grouped.get(sym, [])
        first, last = (dates[0], dates[-1]) if dates else ("", "")
        ref_in_range = [d for d in union_dates if first <= d <= last]
        missing = sorted(set(ref_in_range) - set(dates))
        info = json_by_symbol.get(sym, {})
        per_symbol.append({
            "symbol": sym,
            "name": info.get("name", ""),
            "board_cn": info.get("board_cn", ""),
            "rows": len(dates),
            "first": first,
            "last": last,
            "gap": len(missing),
            "missing_head": missing[:5],
            "avg_amount_20d": info.get("avg_amount_20d"),
        })
    per_symbol.sort(key=lambda s: (-(s["gap"]), s["symbol"]))

    rows_per_symbol = [s["rows"] for s in per_symbol]
    min_rows = min(rows_per_symbol) if rows_per_symbol else 0
    max_rows = max(rows_per_symbol) if rows_per_symbol else 0
    min_last = min((s["last"] for s in per_symbol), default="")
    min_first = min((s["first"] for s in per_symbol if s["first"]), default="")
    min_date = all_dates[0] if all_dates else ""
    max_date = all_dates[-1] if all_dates else ""

    gap_symbols = [s for s in per_symbol if s["gap"] > 0]
    gap_days_total = int(sum(s["gap"] for s in per_symbol))
    max_gap = max((s["gap"] for s in per_symbol), default=0)
    gap_buckets = Counter()
    for s in per_symbol:
        g = s["gap"]
        gap_buckets["0（无缺口）" if g == 0 else "1-5 日" if g <= 5 else
                    "6-20 日" if g <= 20 else "21-60 日" if g <= 60 else ">60 日"] += 1

    # ---- 板块分布（来自股票池 JSON）----
    board_dist = Counter(str(u.get("board_cn") or u.get("board") or "未知") for u in universe)
    board_rows: Counter = Counter()
    for item in universe:
        board_rows[str(item.get("board_cn") or item.get("board") or "未知")] += by_symbol_rows.get(
            str(item.get("symbol")), 0)

    # ---- 剔除原因计数（核验失败 + 因子失败 + 快照初筛）----
    failures = list(report.get("failures", []) or []) + list(report.get("factor_failures", []) or [])
    failure_counts = Counter(_categorize_failure(f.get("reason", "")) for f in failures)
    failure_by_stage = Counter(str(f.get("stage", "verify")) for f in failures)
    screen_stats = report.get("screen_stats", {}) or {}

    # ---- 自动断言 ----
    json_symbols = [str(u.get("symbol")) for u in universe]
    json_rows_match = all(int(u.get("bars_2019_2026", -1)) == by_symbol_rows.get(str(u.get("symbol")), -2)
                          for u in universe)
    report_bar_rows = report.get("bar_rows")
    report_picked = report.get("picked")
    checks: list[dict] = []

    def add_check(cid: str, item: str, ok: bool, detail: str) -> None:
        checks.append({"id": cid, "item": item, "ok": bool(ok), "detail": detail})

    add_check("C1", "无重复 (date,symbol)", dup_rows == 0, f"重复 {dup_rows} 行")
    add_check("C2", "source 全为 akshare（无 mock/混源）",
              source_values == ["akshare"] and source_null == 0 and mixed_source_symbols == 0,
              f"取值 {source_values}，空值 {source_null}，混源标的 {mixed_source_symbols} 只")
    add_check("C3", "adj_factor 无缺失/非正/非数值",
              factor_missing == 0 and factor_nonnumeric == 0 and factor_nonpositive == 0,
              f"缺失 {factor_missing} 行，非数值 {factor_nonnumeric} 行，≤0 {factor_nonpositive} 行")
    add_check("C4", f"每标的 ≥800 行", min_rows >= 800,
              f"最少 {min_rows} 行（{max_rows} 行为最多），共 {n_symbols} 只")
    add_check("C5", "每标的末根 bar ≥ 2026-10-16", bool(min_last) and min_last >= FRESH_AFTER.isoformat(),
              f"最早末根 bar = {min_last}")
    add_check("C6", "行数自洽（CSV = 逐只求和 = 构建报告 = JSON）",
              total_rows == sum(rows_per_symbol)
              and report_bar_rows is not None and total_rows == int(report_bar_rows)
              and report_picked is not None and n_symbols == int(report_picked)
              and json_rows_match,
              f"CSV {total_rows} 行 / 逐只求和 {sum(rows_per_symbol)} / 报告 {report_bar_rows} / "
              f"标的 {n_symbols} vs 报告 {report_picked} / JSON 行数一致={json_rows_match}")
    add_check("C7", f"标的数 ≥180（目标 200）", n_symbols >= MIN_POOL,
              f"实际 {n_symbols} 只")
    add_check("C8", "区间覆盖 2019-01-02 ~ 2026-10-23",
              min_date == "2019-01-02" and max_date == BAR_END.isoformat(),
              f"实际 {min_date} ~ {max_date}")
    add_check("C9", "价格有效性（OHLC>0、high≥low、high/low 包住 open/close）",
              price_bad == 0 and range_bad == 0 and ohlc_bad == 0,
              f"非正价格 {price_bad} 行，high<low {range_bad} 行，OHLC 越界 {ohlc_bad} 行")
    add_check("C10", "股票池 JSON 与 CSV 标的集合/行数一致",
              set(json_symbols) == set(symbols_in_csv) and json_rows_match,
              f"JSON {len(json_symbols)} 只 / CSV {n_symbols} 只 / 行数一致={json_rows_match}")
    add_check("C11", "零 mock：数据与元信息均无 mock 标识",
              "mock" not in " ".join(source_values).lower()
              and all("mock" not in str(u.get("source", "")).lower() for u in universe),
              f"来源 {source_values}；股票池 source 字段均为 akshare")
    add_check("C12", "CSV 表头与列顺序",
              list(frame.columns) == BAR_COLUMNS,
              f"实际 {list(frame.columns)}")
    add_check("C13", "成交额/成交量为空或非正的行数为 0",
              amount_na == 0 and amount_nonpos == 0 and volume_na == 0 and volume_nonpos == 0,
              f"amount 空 {amount_na} / ≤0 {amount_nonpos} 行；volume 空 {volume_na} / ≤0 {volume_nonpos} 行")
    passed = all(c["ok"] for c in checks)
    passed_count = sum(1 for c in checks if c["ok"])

    invalid_dropped = list(report.get("invalid_bars_dropped") or [])
    summary = {
        "symbols": n_symbols,
        "invalid_bars_dropped": len(invalid_dropped),
        "rows": total_rows,
        "rows_min": min_rows,
        "rows_max": max_rows,
        "date_min": min_date,
        "date_max": max_date,
        "union_days": union_days,
        "duplicate_rows": dup_rows,
        "factor_missing": factor_missing,
        "factor_nonnumeric": factor_nonnumeric,
        "factor_nonpositive": factor_nonpositive,
        "source_values": source_values,
        "source_null": source_null,
        "mixed_source_symbols": mixed_source_symbols,
        "price_bad": price_bad,
        "range_bad": range_bad,
        "ohlc_bad": ohlc_bad,
        "amount_na": amount_na,
        "amount_nonpos": amount_nonpos,
        "volume_na": volume_na,
        "volume_nonpos": volume_nonpos,
        "gap_symbols": len(gap_symbols),
        "gap_days_total": gap_days_total,
        "max_gap": max_gap,
        "board_dist": dict(board_dist),
        "failure_counts": dict(failure_counts),
        "checks_total": len(checks),
        "checks_passed": passed_count,
        "pass": passed,
    }
    audit = {
        "generated_at": now_iso(),
        "pass": passed,
        "checks": checks,
        "summary": summary,
        "gap_symbols": [{"symbol": s["symbol"], "name": s["name"], "gap": s["gap"],
                         "missing_head": s["missing_head"]} for s in gap_symbols],
        "board_dist": dict(board_dist),
        "failure_counts": dict(failure_counts),
        "failure_by_stage": dict(failure_by_stage),
        "report_generated_at": report.get("generated_at"),
        "command": report.get("command"),
        "elapsed_sec": report.get("elapsed_sec"),
    }

    md = _render_audit_markdown(
        paths=paths, report=report, universe=universe, per_symbol=per_symbol,
        checks=checks, summary=summary, gap_symbols=gap_symbols, gap_buckets=gap_buckets,
        board_dist=board_dist, board_rows=board_rows, failure_counts=failure_counts,
        failure_by_stage=failure_by_stage, screen_stats=screen_stats,
        min_date=min_date, max_date=max_date, union_days=union_days,
    )
    return audit, md


def _render_audit_markdown(
    *,
    paths: Paths,
    report: dict,
    universe: list[dict],
    per_symbol: list[dict],
    checks: list[dict],
    summary: dict,
    gap_symbols: list[dict],
    gap_buckets: Counter,
    board_dist: Counter,
    board_rows: Counter,
    failure_counts: Counter,
    failure_by_stage: Counter,
    screen_stats: dict,
    min_date: str,
    max_date: str,
    union_days: int,
) -> str:
    """渲染覆盖审计 Markdown（人读版）。"""
    lines: list[str] = []
    add = lines.append
    status = "PASS" if summary["pass"] else "FAIL"
    add("# M6 短线研究股票池数据覆盖审计（universe_coverage_200）")
    add("")
    add(f"> 生成时间：{now_iso()} · 生成脚本：`scripts/build_universe_m6.py --audit-only`（离线重算，不联网）")
    add(f"> 构建命令：`{report.get('command') or '（build_report_200.json 缺失，未记录）'}`")
    add(f"> 构建耗时：**{report.get('elapsed_sec', '—')} 秒** · 股票池 "
        f"`{rel_md(paths.json)}`（**{len(universe)} 只**）· 日线 `{rel_md(paths.csv)}`"
        f"（**{summary['rows']} 行**，{min_date} ~ {max_date}）")
    add("> 数据口径：akshare **Sina** `stock_zh_a_daily(adjust='')` 不复权 OHLCV + "
        "`stock_zh_a_daily(adjust='hfq-factor')` 后复权因子（按除权日 asof 合并）；"
        "`source` 列恒为 `akshare`，**零 mock / 零合成数据**；未写 `runtime/quantdesk.db`。")
    add("> 口径偏差说明：task 文字写的是 `adjust='qfq-factor'`，**实现采用 `hfq-factor`（后复权因子）**，"
        "原因与等价性见下方「## 0.1 数据口径偏差说明」。")
    add(f"> 参考交易日历：本 CSV 全池并集交易日（「{summary['symbols']} 只标的中至少 1 只有 bar」"
        f"的日期并集，共 **{union_days}** 个交易日）；逐只缺口只在其自身首末日区间内比对。")
    add("")
    add(f"## 0. 结论：**{status}**（{summary['checks_passed']}/{summary['checks_total']} 项校验通过）")
    add("")
    if not summary["pass"]:
        failed = [c for c in checks if not c["ok"]]
        add("**未通过项：**")
        add("")
        for c in failed:
            add(f"- {c['id']} {c['item']}：{c['detail']}")
        add("")
    add("## 0.1 数据口径偏差说明（task 文字 qfq-factor vs 实际 hfq-factor）")
    add("")
    add("| 项 | 说明 |")
    add("|---|---|")
    add("| task 文字 | `adj_factor` 用 `stock_zh_a_daily(symbol, adjust='qfq-factor')` 按除权日 asof 合并 |")
    add("| 实际实现 | `stock_zh_a_daily(symbol, adjust='hfq-factor')`（**后复权因子**） |")
    add("| 偏差原因 | 本项目 `adj_factor` 的语义在代码里已固定为**后复权因子**："
        "`core/models.py` 的字段注释「后复权因子」、`data/akshare_provider.py` 合并 `hfq_factor`、"
        "legacy `bars_50.csv` 亦为 hfq 口径；`adj_factor` 必须与 DB/回测引擎/legacy 数据一致。 |")
    add("| 收益等价性 | 对同一标的 `hfq_factor(t) × qfq_factor(t) = 常数 K`（两者互为倒数乘常数）；"
        "`close × adj_factor` 的复权序列在两种口径下只差同一个常数倍，所以**相邻日收益率、"
        "future_ret_h、累计收益、RankIC/命中率完全等价**；QA 用 `close × adj_factor` 断言 future_ret 不受影响。 |")
    add("| 采用口径 | **hfq-factor**（与 legacy `bars_50.csv` / DB / 回测引擎一致）；"
        "跨标的「以最新价为基准」比较可另算 qfq，但不改变本文件 `close × adj_factor` 的收益语义。 |")
    add("")
    add("## 1. 结论速览")
    add("")
    add("| 项 | 数值 |")
    add("|---|---|")
    add(f"| 股票池标的数 | **{summary['symbols']}**（目标 200，验收下限 180） |")
    add(f"| 日线长表行数 | **{summary['rows']}** |")
    add(f"| 区间 | {summary['date_min']} ~ {summary['date_max']} |")
    add(f"| 每标的行数（最小 / 最大） | {summary['rows_min']} / {summary['rows_max']} |")
    add(f"| 重复 (date,symbol) 行 | {summary['duplicate_rows']} |")
    add(f"| `adj_factor` 缺失行 | {summary['factor_missing']} |")
    add(f"| `source` 取值 / 混源标的 | {', '.join(summary['source_values']) or '—'} / "
        f"{summary['mixed_source_symbols']} |")
    add(f"| 有缺口（停牌）标的数 | {summary['gap_symbols']} |")
    add(f"| 缺口（停牌）交易日合计 | {summary['gap_days_total']} 天（单只最大 {summary['max_gap']} 天） |")
    add(f"| 板块分布 | {', '.join(f'{k} {v}' for k, v in board_dist.items())} |")
    add("")
    add("## 2. 校验清单（自动断言）")
    add("")
    add("| # | 检查项 | 结果 | 实测 |")
    add("|---|---|---|---|")
    for c in checks:
        add(f"| {c['id']} | {c['item']} | {'✅ PASS' if c['ok'] else '❌ FAIL'} | {c['detail']} |")
    add("")
    add("## 3. 数据质量明细")
    add("")
    add("| 检查项 | 结果 |")
    add("|---|---|")
    add(f"| 主键（date,symbol）重复 | {summary['duplicate_rows']} 行 |")
    add(f"| `source` 空值 / 取值集合 | {summary['source_null']} / {summary['source_values']} |")
    add(f"| 同一标的多 source（混源） | {summary['mixed_source_symbols']} 只 |")
    add(f"| `adj_factor` 为空 / 非数值 / ≤0 | {summary['factor_missing']} / "
        f"{summary['factor_nonnumeric']} / {summary['factor_nonpositive']} 行 |")
    add(f"| 非正价格（open/high/low/close ≤ 0） | {summary['price_bad']} 行 |")
    add(f"| high < low | {summary['range_bad']} 行 |")
    add(f"| OHLC 越界（high<max(open,close) 或 low>min(open,close)） | {summary['ohlc_bad']} 行 |")
    add(f"| 成交额缺失 / ≤0 | {summary['amount_na']} / {summary['amount_nonpos']} 行 |")
    add(f"| 成交量缺失 / ≤0 | {summary['volume_na']} / {summary['volume_nonpos']} 行 |")
    add(f"| 零值/无成交占位 bar（疑似停牌，已剔除、不补造） | {summary['invalid_bars_dropped']} 行 |")
    add("")
    dropped_invalid = report.get("invalid_bars_dropped") or []
    if dropped_invalid:
        add("### 3.1 已剔除的零值/无成交占位 bar（按「无 bar = 不可交易」处理）")
        add("")
        add("| 标的 | 日期 | open/high/low/close | volume / amount | 处理 |")
        add("|---|---|---|---|---|")
        for item in dropped_invalid:
            add(f"| {item.get('symbol')} | {item.get('date')} | "
                f"{item.get('open')}/{item.get('high')}/{item.get('low')}/{item.get('close')} | "
                f"{item.get('volume')} / {item.get('amount')} | 剔除（不补造） |")
        add("")
        add("> 说明：新浪在个别停牌日返回「OHLC=0、volume=0、amount=0」的占位行，"
            "并非真实成交。按项目约定「无 bar = 不可交易」剔除，不做插值/补造；"
            "剔除后该标的的缺口统计会把这些日期计为停牌日。")
        add("")
    add("## 4. 板块分布")
    add("")
    add("| 板块 | 标的数 | 行数 |")
    add("|---|---:|---:|")
    for board, count in board_dist.most_common():
        add(f"| {board} | {count} | {board_rows.get(board, 0)} |")
    add("")
    add("## 5. 停牌 / 缺口统计")
    add("")
    add(f"**参考交易日历**：本 CSV 中「{summary['symbols']} 只标的中至少 1 只有 bar」的日期并集"
        f"（{summary['date_min']} ~ {summary['date_max']}，共 {union_days} 个交易日）；"
        "每个标的只在其自身首末日区间内做缺口比对。")
    add("")
    add("| 缺口分布 | 标的数 |")
    add("|---|---:|")
    for bucket in ("0（无缺口）", "1-5 日", "6-20 日", "21-60 日", ">60 日"):
        add(f"| {bucket} | {gap_buckets.get(bucket, 0)} |")
    add("")
    if gap_symbols:
        add(f"共 **{len(gap_symbols)} 只**标的存在缺口（缺口日无 bar = 不可交易），"
            f"合计 **{summary['gap_days_total']}** 个交易日：")
        add("")
        add("| 标的 | 名称 | 缺口天数 | 缺口样例（前 5 个） |")
        add("|---|---|---:|---|")
        for s in gap_symbols:
            add(f"| {s['symbol']} | {s['name']} | {s['gap']} | {', '.join(s['missing_head']) or '—'} |")
        add("")
        add("> 说明：A 股停牌日**不存在 bar** 属正常现象（重大事项/重组/长期停牌），"
            "**不是新浪数据缺失**，也不应补造。上一轮 legacy50 已抽 3 例用腾讯备源交叉验证确认为真实停牌；"
            "本轮扩样未逐只做备源交叉验证（成本原因），若某标的缺口 >60 日建议人工复核。"
            "模型需按「无 bar = 不可交易」处理，撮合必须跳过缺口日。")
    else:
        add("- 无缺口：全部标的在其自身区间内与全池交易日历完全一致。")
    add("")
    add("## 6. 筛选与剔除原因计数")
    add("")
    total_scan = screen_stats.get("total")
    add(f"全市场快照：**{total_scan if total_scan is not None else '—'}** 行（Sina，{SPOT_DATE}）"
        f" → 初筛保留 **{screen_stats.get('kept', '—')}** 只；"
        f"候选池 **{report.get('candidates', '—')}** 只 → 核验通过 **{report.get('verified', '—')}** 只 "
        f"→ 入池 **{report.get('picked', '—')}** 只。")
    add("")
    add("### 6.1 初筛剔除（快照规则）")
    add("")
    add("| 剔除原因 | 数量 |")
    add("|---|---:|")
    for reason, count in sorted((screen_stats.get("dropped") or {}).items(), key=lambda kv: -kv[1]):
        add(f"| {reason} | {count} |")
    add("")
    add("### 6.2 逐只核验阶段剔除 / 失败")
    add("")
    if failure_counts:
        add("| 原因 | 数量 |")
        add("|---|---:|")
        for reason, count in failure_counts.most_common():
            add(f"| {reason} | {count} |")
        add("")
        add("| 标的 | 名称 | 阶段 | 原因 |")
        add("|---|---|---|---|")
        for f in report.get("failures", []):
            add(f"| {f.get('symbol', '')} | {f.get('name', '')} | "
                f"{f.get('stage', 'verify')} | {f.get('reason', '')} |")
    else:
        add("- 无：候选池全部通过核验（无失败/剔除）。")
    add("")
    add("## 7. 逐只覆盖明细")
    add("")
    add("| 标的 | 名称 | 板块 | 行数 | 首日 | 末日 | 缺口 |")
    add("|---|---|---|---:|---|---|---:|")
    for s in per_symbol:
        add(f"| {s['symbol']} | {s['name']} | {s['board_cn']} | {s['rows']} | "
            f"{s['first']} | {s['last']} | {s['gap']} |")
    add("")
    add("## 8. 存活者偏差声明（重要）")
    add("")
    add("1. **样本来自 2026-10-25 当日仍存续、仍有成交的全市场快照**：已退市/长期停牌摘牌的股票"
        "不在快照中，因此本股票池**天然存在存活者偏差（survivorship bias）**，"
        "回测收益/胜率可能被系统性高估。")
    add("2. **股票池是「事后（ex-post）」按当前流动性选出的**：若直接用于 2019–2026 全区间回测，"
        "等于使用了未来信息（选样偏差 + 前视偏差）。正确用法是 walk-forward："
        "每个再平衡点只用当时可得的数据选池；或在报告中明确声明该局限。")
    add("3. **上市未满 2 年 / 近期停牌标的已被剔除**（首根 bar ≤ 2024-10-23、末根 bar ≥ 2026-10-16），"
        "这进一步偏向「已上市且持续交易」的存续样本。")
    add("4. 本审计只保证**数据覆盖与口径**（真实 akshare、不复权 OHLCV、后复权因子、无重复/无缺失），"
        "不保证样本代表全市场，也不构成收益预测。")
    add("")
    add("## 9. 复现命令")
    add("")
    add("```powershell")
    add("# 全量构建（后台，日志 runtime/short_term/build_log_200.txt）")
    add("cd quantdesk")
    add(f".venv\\Scripts\\python.exe scripts\\build_universe_m6.py --limit 200 --candidates 280 "
        f"--sleep 0.25")
    add("")
    add("# 离线重算审计（不联网，重写本报告与 build_report_200.json 的 audit 段）")
    add(".venv\\Scripts\\python.exe scripts\\build_universe_m6.py --audit-only")
    add("")
    add("# 读入校验（normalize_bars 兼容性 + 关键断言）")
    add(".venv\\Scripts\\python.exe -c \"import pandas as pd; from strategy.factors import "
        "normalize_bars; df=pd.read_csv('runtime/short_term/bars_200.csv'); "
        "print(len(df), df.symbol.nunique(), normalize_bars(df).shape)\"")
    add("```")
    add("")
    return "\n".join(lines)


def write_audit(paths: Paths, update_report: bool = True) -> dict:
    """计算并落盘审计报告；可选把 audit 段写回 build_report。"""
    audit, md = render_audit(paths)
    paths.coverage.parent.mkdir(parents=True, exist_ok=True)
    paths.coverage.write_text(md, encoding="utf-8")
    log(f"已写审计报告 {rel(paths.coverage)}（{'PASS' if audit['pass'] else 'FAIL'}，"
        f"{audit['summary']['checks_passed']}/{audit['summary']['checks_total']}）")
    if update_report and paths.report.exists():
        report = load_report_or_empty(paths.report)
        report["audit"] = audit
        report["audit_command"] = f"{sys.executable} {Path(__file__)} --audit-only"
        # 兜底补全口径偏差说明（老报告可能缺该字段）
        report.setdefault("factor_adjust_note", FACTOR_ADJUST_NOTE)
        meta = report.get("universe_meta")
        if isinstance(meta, dict):
            note = str(meta.get("notes", "") or "")
            if "hfq-factor" not in note:
                meta["notes"] = note + ("；" if note else "") + (
                    "adj_factor 采用 hfq-factor（后复权因子，与 legacy bars_50/DB/回测引擎一致）；"
                    "task 文字 qfq-factor 与实现存在口径偏差，收益等价性见 build_report 的 factor_adjust_note")
        paths.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"已把审计结论写回 {rel(paths.report)} 的 audit 段")
    return audit


# --------------------------------------------------------------------------- #
# 分年截面规模统计（存活者偏差量化，离线可复现）
# --------------------------------------------------------------------------- #
def compute_crosssection(paths: Paths) -> dict:
    """按年统计「当日至少有 bar 的标的数」的 min/median/max、≥40 只交易日数，
    以及 200 只标的首次出现（上市）年份分布。"""
    frame = pd.read_csv(paths.csv, usecols=["date", "symbol"],
                        dtype={"date": str, "symbol": str})
    by_date = frame.groupby("date")["symbol"].nunique().sort_index()
    dates = pd.to_datetime(by_date.index)
    values = by_date.to_numpy(dtype="int64")

    def _median(arr) -> int:
        ordered = sorted(int(v) for v in arr)
        n = len(ordered)
        if n == 0:
            return 0
        mid = n // 2
        return ordered[mid] if n % 2 else int(round((ordered[mid - 1] + ordered[mid]) / 2))

    year_stats: dict[str, dict] = {}
    for year in sorted(set(dates.year.tolist())):
        mask = dates.year == year
        vals = values[mask]
        year_stats[str(year)] = {
            "trading_days": int(len(vals)),
            "min_symbols": int(vals.min()),
            "median_symbols": _median(vals),
            "max_symbols": int(vals.max()),
            "days_ge_40": int((vals >= 40).sum()),
            "days_ge_10": int((vals >= 10).sum()),
        }

    first_bar = frame.groupby("symbol")["date"].min()
    first_years = pd.to_datetime(first_bar).dt.year
    additions = {str(y): int((first_years == y).sum()) for y in range(2019, 2027)}
    existed_2019 = int((first_bar <= "2019-12-31").sum())
    started_20190102 = int((first_bar == "2019-01-02").sum())

    total_days = int(len(values))
    summary = {
        "total_symbols": int(frame["symbol"].nunique()),
        "total_rows": int(len(frame)),
        "total_trading_days": total_days,
        "days_ge_40": int((values >= 40).sum()),
        "min_symbols": int(values.min()),
        "median_symbols": _median(values),
        "max_symbols": int(values.max()),
        "first_day": by_date.index[0],
        "first_day_symbols": int(values[0]),
        "year_stats": year_stats,
        "first_bar_year_additions": additions,
        "first_bar_year_additions_sum": int(sum(additions.values())),
        "symbols_existed_in_2019": existed_2019,
        "symbols_first_bar_20190102": started_20190102,
        "last_day": by_date.index[-1],
        "last_day_symbols": int(values[-1]),
    }
    return summary


def write_crosssection(paths: Paths, out_path: Path, update_report: bool = True) -> dict:
    """生成分年截面规模统计 Markdown（并写回 build_report 的 crosssection 段）。"""
    stats = compute_crosssection(paths)
    year_stats = stats["year_stats"]
    lines: list[str] = []
    add = lines.append
    add("# universe_200 分年截面规模统计（存活者偏差量化）")
    add("")
    add(f"> 生成时间：{now_iso()} · 脚本：`scripts/build_universe_m6.py --audit-only`（离线，不联网）")
    add(f"> 数据：`{rel_md(paths.csv)}`（{stats['total_symbols']} 只 × {stats['total_rows']} 行，"
        f"{stats['total_trading_days']} 个交易日）")
    add(f"> 定义：「当日截面规模」= 该交易日 CSV 中有 bar 的标的数（无 bar = 停牌/未上市/已退市）。")
    add("")
    add("## 0. 结论摘要")
    add("")
    add(f"- 全区间 {stats['total_trading_days']} 个交易日，截面规模 **{stats['min_symbols']} ~ "
        f"{stats['max_symbols']}** 只，中位数 **{stats['median_symbols']}** 只。")
    add(f"- 截面 ≥40 只（可做跨标的横向比较的粗阈值）的交易日：**{stats['days_ge_40']} 天**"
        f"（占 {stats['days_ge_40'] / max(1, stats['total_trading_days']) * 100:.1f}%；"
        "本池 2019 年最小截面也有 "
        f"{stats['min_symbols']} 只，故该阈值从不触发，见第 1 节说明）。")
    early = year_stats.get("2019", {})
    max_year = max(int(y) for y in year_stats)
    late = year_stats.get(str(max_year), {})
    add(f"- 早期（2019 年）截面中位数仅 **{early.get('median_symbols', '—')} 只**，"
        f"最新年份（{max_year} 年）为 **{late.get('median_symbols', '—')} 只**："
        f"**2019 年截面 = 当前 200 只池的 {early.get('median_symbols', 0) / max(1, stats['total_symbols']) * 100:.1f}%**，"
        "且本池是 2026 年事后按流动性选出的（存活者偏差 + 选样偏差），"
        "全区间等权回测在 2019–2021 的有效标的数明显少于后期。")
    add(f"- 200 只中 **{stats['symbols_existed_in_2019']} 只**在 2019 年已有 bar"
        f"（其中 {stats['symbols_first_bar_20190102']} 只首根 bar 就是 2019-01-02，即 2019 年前已上市）；"
        f"其余为 2020 年后陆续上市（首根 bar 年份分布见第 2 节，合计 {stats['first_bar_year_additions_sum']}/200）。")
    add("")
    add("## 1. 分年截面规模（当日至少有 bar 的标的数）")
    add("")
    add("| 年份 | 交易日数 | 最小 | 中位数 | 最大 | ≥40 只交易日数 | ≥10 只交易日数 |")
    add("|---|---:|---:|---:|---:|---:|---:|")
    for year in sorted(year_stats):
        s = year_stats[year]
        add(f"| {year} | {s['trading_days']} | {s['min_symbols']} | {s['median_symbols']} | "
            f"{s['max_symbols']} | {s['days_ge_40']} | {s['days_ge_10']} |")
    add("")
    add("> 说明：早期截面小是**结构性的**——本池 200 只中只有 "
        f"{stats['symbols_existed_in_2019']} 只在 2019 年交易，其余尚未上市。"
        "因此 2019–2021 的截面天然偏小，不代表当年全市场只有这么多股票。"
        f"另：本池最小截面 {stats['min_symbols']} 只，**≥40 只阈值在 {stats['total_trading_days']} 个交易日里全部满足、"
        "对本题不具区分度**；真正有区分度的是分年中位数与「2019 年已存在只数」，保留该指标仅为满足审计口径。")
    add("")
    add("## 2. 200 只标的的首次出现（上市）年份分布")
    add("")
    add("| 首根 bar 年份 | 新增只数 | 累计 |")
    add("|---|---:|---:|")
    cumulative = 0
    for year in sorted(stats["first_bar_year_additions"]):
        count = stats["first_bar_year_additions"][year]
        cumulative += count
        add(f"| {year} | {count} | {cumulative} |")
    add(f"| **合计** | **{stats['first_bar_year_additions_sum']}** | — |")
    add("")
    add("## 3. 2019 年已存在标的")
    add("")
    add(f"- 2019 年内已有 bar（首根 bar ≤ 2019-12-31）的标的：**{stats['symbols_existed_in_2019']} 只**")
    add(f"- 首根 bar = 2019-01-02（2019 年前已上市，可覆盖完整区间）：**{stats['symbols_first_bar_20190102']} 只**")
    add(f"- 首个交易日 **{stats['first_day']}** 实际截面：**{stats['first_day_symbols']} 只**"
        f"（全区间最小截面 {stats['min_symbols']} 只出现在早期停牌日）")
    add("")
    add("## 4. 存活者偏差量化与使用声明")
    add("")
    add(f"1. **截面规模变化**：2019 年中位 **{early.get('median_symbols', '—')} 只** → "
        f"{max_year} 年中位 **{late.get('median_symbols', '—')} 只**，"
        f"约 {late.get('median_symbols', 1) / max(1, early.get('median_symbols', 1)):.2f} 倍；"
        f"2019 年截面仅为当前 200 只池的 "
        f"{early.get('median_symbols', 0) / max(1, stats['total_symbols']) * 100:.1f}%，"
        f"且 200 只中有 **{stats['total_symbols'] - stats['symbols_existed_in_2019']} 只**"
        "在 2019 年尚未上市（首根 bar 晚于 2019）。")
    add(f"2. **选样偏差（look-ahead selection）**：股票池来自 2026-10-25 快照（当日仍存续、仍有成交），"
        "再按 2026 年的近 20 日成交额择优。用它回测 2019 年，相当于**提前知道哪些股票活到了 2026 年且流动性好**，"
        "会系统性剔除后来退市/长期停牌/流动性衰落的标的，收益与胜率可能被高估。"
        "注意：本池 2019 年存在标的的「截面收缩」主要来自**上市时间差**（40 只 2019 年后才上市），"
        "而**存活者偏差无法从本数据内部量化**（需要历史全市场快照/退市名单），只能声明方向。")
    add("3. **早期截面偏小的复合影响**：2019–2021 截面中位 147–177 只，虽不是极小样本，"
        "但相对 200 只的全池仍少 12%–27%，且经停牌剔除后单日可用标的更少；"
        "等权组合/横截面 RankIC 的统计噪声更大，分年度结论需结合截面规模解读。")
    add("4. **正确用法**：M6 结论应至少做到 walk-forward + 每期按当时可得数据选池（as-of universe）；"
        "若受数据限制无法重构历史全市场快照，必须在研究报告「数据限制」章节明确声明上述偏差及其方向。")
    add("")
    add("## 5. 复现命令")
    add("")
    add("```powershell")
    add("cd quantdesk")
    add(".venv\\Scripts\\python.exe scripts\\build_universe_m6.py --audit-only")
    add("```")
    add("")
    add("> 说明：`--audit-only` 会离线重算覆盖审计，并同时重写本截面统计文件；"
        "完整重算命令与耗时见 `runtime/short_term/build_report_200.json`。")
    add("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    log(f"已写分年截面统计 {rel(out_path)}（{stats['total_trading_days']} 个交易日，"
        f"≥40 只截面 {stats['days_ge_40']} 天）")
    if update_report and paths.report.exists():
        report = load_report_or_empty(paths.report)
        report["crosssection"] = stats
        report["crosssection_command"] = f"{sys.executable} {Path(__file__)} --audit-only"
        paths.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return stats


# --------------------------------------------------------------------------- #
# 构建流程
# --------------------------------------------------------------------------- #
def write_bars_csv(path: Path, items: list[dict]) -> int:
    """写长表日线 CSV；任何一行缺少 asof 因子都 fail-closed 抛错（不留空、不编造）。"""
    total = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(BAR_COLUMNS)
        for item in items:
            pairs = item["_factors"]
            for bar in item["bars"]:
                d = date.fromisoformat(bar["date"])
                factor = factor_asof(pairs, d)
                if factor is None:
                    raise ValueError(f"{item['code']} {bar['date']} 无 asof 后复权因子（fail-closed）")
                volume = "" if not _is_finite(bar.get("volume")) else int(round(float(bar["volume"])))
                amount = "" if not _is_finite(bar.get("amount")) else float(bar["amount"])
                writer.writerow([bar["date"], item["code"], bar["open"], bar["high"],
                                 bar["low"], bar["close"], volume, amount,
                                 f"{factor:.10f}", "akshare"])
                total += 1
    return total


def run_build(args: argparse.Namespace, paths: Paths, cross_path: Path) -> int:
    """全量构建主流程。"""
    started = time.time()
    DROPPED_INVALID_BARS.clear()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    command = f"{sys.executable} {Path(__file__)} " + " ".join(sys.argv[1:])
    import akshare as ak
    ak_version = getattr(ak, "__version__", "unknown")

    ref_dates, ref_source = load_reference_calendar()
    recent_iso = {d.isoformat() for d in ref_dates[-RECENT_WINDOW:]}
    log(f"参考交易日历：{ref_source}；近 {RECENT_WINDOW} 日窗口 "
        f"{sorted(recent_iso)[0] if recent_iso else '—'} ~ {sorted(recent_iso)[-1] if recent_iso else '—'}")
    quota = scaled_quota(args.limit)
    log(f"目标股票池 {args.limit} 只，板块配额（按 legacy50 等比例放大）：{quota}")

    # ---------- 1) 快照 + 初筛 ----------
    t0 = time.time()
    rows, spot_source, parse_skipped = load_spot_rows(args.refresh_spot)
    log(f"全市场快照：{len(rows)} 行（解析跳过 {parse_skipped}），来源 {spot_source}")
    kept, screen_stats = screen_spot(rows, parse_skipped)
    screen_sec = round(time.time() - t0, 1)
    log(f"初筛后：{screen_stats['kept']} 只（剔除明细 {screen_stats['dropped']}，{screen_sec}s）")

    # ---------- 2) 候选池（按板块配额放大） ----------
    candidates, candidate_alloc = allocate_candidates(kept, args.candidates, quota)
    if candidates:
        log(f"候选池 {len(candidates)} 只（板块候选分配 {candidate_alloc}，"
            f"门槛快照成交额 {candidates[-1]['amount'] / 1e8:.2f} 亿元）")
    else:
        log("!! 无候选")
        return 1

    # ---------- 3) 逐只核验 ----------
    t0 = time.time()
    verified: list[dict] = []
    failures: list[dict] = []
    for idx, row in enumerate(candidates, 1):
        symbol = row["code"]
        if idx > 1:
            time.sleep(args.sleep)
        bars, error = fetch_with_retry(fetch_bars, symbol, "日线")
        if bars is None:
            failures.append({"symbol": symbol, "name": row["name"], "stage": "bars",
                             "reason": f"拉取失败（重试 {RETRY_ATTEMPTS - 1} 次后跳过）：{error}"})
            log(f"  [{idx}/{len(candidates)}] {symbol} {row['name']} 拉取失败，跳过（{error}）")
            continue

        stats = window_stats(bars, recent_iso, ref_dates)
        row.update(stats)
        reason = None
        if not bars:
            reason = "无 2019 以来日线"
        elif date.fromisoformat(stats["first"]) > MIN_LISTED_BEFORE:
            reason = f"上市不足 2 年（首根 {stats['first']}）"
        elif date.fromisoformat(stats["last"]) < FRESH_AFTER:
            reason = f"近期停牌（末根 {stats['last']}）"
        elif stats["recent_bars"] < RECENT_MIN_BARS:
            reason = (f"近 {RECENT_WINDOW} 个交易日仅 {stats['recent_bars']} 根 bar"
                      f"（<{RECENT_MIN_BARS}）")
        elif stats["rows"] < 800:
            reason = f"历史 bar 不足 800 根（首根 {stats['first']}，{stats['rows']} 根）"
        elif stats["last_close"] is not None and stats["last_close"] < MIN_PRICE:
            reason = f"最新收盘 <3 元（{stats['last_close']:.2f}）"
        if reason:
            failures.append({"symbol": symbol, "name": row["name"], "stage": "verify", "reason": reason})
            log(f"  [{idx}/{len(candidates)}] {symbol} {row['name']} 剔除：{reason}")
            continue

        row["bars"] = bars
        row["data_source"] = "akshare"
        verified.append(row)
        log(f"  [{idx}/{len(candidates)}] {symbol} {row['name']:<8s} "
            f"{stats['rows']:5d} 根 {stats['first']}~{stats['last']} "
            f"近250日 {stats['recent_bars']:3d} 根 "
            f"20日均额 {stats['avg_amount_20d'] / 1e8:6.2f} 亿")
    bars_sec = round(time.time() - t0, 1)
    log(f"核验通过 {len(verified)} 只，剔除/失败 {len(failures)} 只（{bars_sec}s）")

    if len(verified) < MIN_POOL:
        log(f"!! 核验通过仅 {len(verified)} 只，低于验收下限 {MIN_POOL} 只")

    # ---------- 4) 选股 + 后复权因子（失败重试 1 次，缺口由后备补齐） ----------
    t0 = time.time()
    picked = pick_universe(verified, args.limit, quota)
    leftover = sorted([v for v in verified if v["code"] not in {p["code"] for p in picked}],
                      key=lambda r: r["avg_amount_20d"], reverse=True)
    log(f"配额择优选中 {len(picked)} 只（通过 {len(verified)}，后备 {len(leftover)}）")

    final: list[dict] = []
    factor_failures: list[dict] = []
    queue: list[dict] = list(picked)
    while queue and len(final) < args.limit:
        item = queue.pop(0)
        time.sleep(args.sleep)
        pairs, error = fetch_with_retry(fetch_hfq_factor, item["code"], "后复权因子")
        if pairs is None or len(pairs) == 0:
            factor_failures.append({"symbol": item["code"], "name": item["name"], "stage": "factor",
                                    "reason": f"后复权因子获取失败（重试后跳过）：{error or '返回空表'}"})
            log(f"  {item['code']} {item['name']} 因子失败，尝试后备标的")
            if leftover:
                queue.append(leftover.pop(0))
            continue
        missing = [b["date"] for b in item["bars"]
                   if factor_asof(pairs, date.fromisoformat(b["date"])) is None]
        if missing:
            factor_failures.append({
                "symbol": item["code"], "name": item["name"], "stage": "factor",
                "reason": f"后复权因子覆盖不足：{len(missing)} 根 bar 无 asof 因子"
                          f"（示例 {', '.join(missing[:3])}）"})
            log(f"  {item['code']} {item['name']} 因子覆盖不足（{len(missing)} 根），尝试后备标的")
            if leftover:
                queue.append(leftover.pop(0))
            continue
        item["_factors"] = pairs
        final.append(item)
        log(f"  [{len(final)}/{args.limit}] {item['code']} {item['name']} 因子 Ok"
            f"（{len(pairs)} 个除权日因子点）")
    final.sort(key=lambda r: r["avg_amount_20d"], reverse=True)
    factors_sec = round(time.time() - t0, 1)
    log(f"最终入池 {len(final)} 只；因子失败 {len(factor_failures)} 只（{factors_sec}s）")

    if not final:
        log("!! 最终股票池为空，终止")
        return 1

    # fail-closed：写盘前确认来源全是 akshare、无 mock
    for item in final:
        if item.get("data_source") != "akshare":
            raise SystemExit(f"fail-closed：{item['code']} 数据源异常 {item.get('data_source')!r}")
        if "mock" in json.dumps(item.get("bars", [])[:1]).lower():
            raise SystemExit(f"fail-closed：{item['code']} 行情含 mock 标识")

    # ---------- 5) 写 CSV ----------
    t0 = time.time()
    total_rows = write_bars_csv(paths.csv, final)
    write_sec = round(time.time() - t0, 1)
    log(f"已写 {rel(paths.csv)}（{total_rows} 行，{write_sec}s）")

    # ---------- 6) 写股票池 JSON ----------
    universe: list[dict] = []
    for item in final:
        universe.append({
            "symbol": item["code"],
            "name": item["name"],
            "board": item["board"],
            "board_lower": item["board"].lower(),
            "board_cn": item["board_cn"],
            "avg_amount_20d": item["avg_amount_20d"],
            "spot_amount": round(item["amount"], 2),
            "last_price": round(item["price"], 2),
            "last_close": round(float(item["last_close"]), 2) if item["last_close"] is not None else None,
            "first_bar": item["first"],
            "last_bar": item["last"],
            "bars_2019_2026": item["rows"],
            "recent_bars_250d": item["recent_bars"],
            "snapshot_date": SPOT_DATE,
            "source": "akshare.stock_zh_a_daily(adjust='')",
        })
    meta = {
        "name": "universe_200（M6 短线研究可回测股票池）",
        "generated_at": now_iso(),
        "generator": "scripts/build_universe_m6.py",
        "command": command,
        "spot_source": spot_source,
        "spot_date": SPOT_DATE,
        "bar_range": [BAR_START.isoformat(), BAR_END.isoformat()],
        "price_adjust": "none（不复权，撮合用）",
        "adj_factor": "hfq_factor（后复权因子，adjust='hfq-factor'）：后复权价 = 不复权价 × adj_factor",
        "recent_window": {"reference_calendar": ref_source,
                          "window_trading_days": RECENT_WINDOW,
                          "min_bars": RECENT_MIN_BARS},
        "screen": {"board": "MAIN(600/601/603/605,000/001/002/003)+GEM(300/301)+STAR(688/689)",
                   "exclude": ["ST/*ST/退", "北交所(8/920/4)", "上市<2年",
                               "近期长期停牌", "近250交易日<240根", "价格<3元", "成交额<=0"],
                   "sort": "快照成交额降序 → 板块配额候选 → 近20日平均成交额择优"},
        "board_quota": quota,
        "count": len(universe),
        "notes": "本文件为顶层数组；board 大写（MAIN/GEM/STAR），board_lower 为小写别名；"
                 "单位 avg_amount_20d/spot_amount = 元；meta 见 runtime/short_term/build_report_200.json 的 universe_meta；"
                 "adj_factor 采用 hfq-factor（后复权因子，与 legacy bars_50/DB/回测引擎一致），"
                 "task 文字 qfq-factor 与实现存在口径偏差，收益等价性说明见 build_report 的 factor_adjust_note",
    }
    save_universe(paths, universe, meta)
    log(f"已写 {rel(paths.json)}（{len(universe)} 只，顶层数组）")

    # ---------- 7) 构建报告 ----------
    board_mix = Counter(u["board_cn"] for u in universe)
    report = {
        "generated_at": meta["generated_at"],
        "command": command,
        "elapsed_sec": round(time.time() - started, 1),
        "timing": {"screen_sec": screen_sec, "bars_sec": bars_sec,
                   "factors_sec": factors_sec, "write_sec": write_sec},
        "python": sys.version.split()[0],
        "akshare_version": ak_version,
        "spot_source": spot_source,
        "spot_date": SPOT_DATE,
        "bar_range": [BAR_START.isoformat(), BAR_END.isoformat()],
        "price_adjust": "none（不复权）",
        "adj_factor": "hfq_factor（后复权因子，adjust='hfq-factor'）",
        "factor_adjust_note": FACTOR_ADJUST_NOTE,
        "recent_window": {"reference_calendar": ref_source,
                          "window_trading_days": RECENT_WINDOW,
                          "min_bars": RECENT_MIN_BARS},
        "board_quota": quota,
        "board_candidate_alloc": candidate_alloc,
        "screen_stats": screen_stats,
        "candidates": len(candidates),
        "verified": len(verified),
        "picked": len(final),
        "board_mix": dict(board_mix),
        "bar_rows": total_rows,
        "invalid_bars_dropped": list(DROPPED_INVALID_BARS),
        "invalid_bars_dropped_count": len(DROPPED_INVALID_BARS),
        "factor_failures": factor_failures,
        "failures": failures,
        "per_symbol": [
            {"symbol": u["symbol"], "name": u["name"], "board_cn": u["board_cn"],
             "rows": u["bars_2019_2026"], "first": u["first_bar"], "last": u["last_bar"],
             "recent_bars_250d": u["recent_bars_250d"],
             "avg_amount_20d": u["avg_amount_20d"]}
            for u in universe
        ],
        "universe_meta": meta,
    }
    paths.report.parent.mkdir(parents=True, exist_ok=True)
    paths.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"已写 {rel(paths.report)}；构建总耗时 {report['elapsed_sec']}s")

    # ---------- 8) 覆盖审计 + 分年截面统计 ----------
    audit = write_audit(paths, update_report=True)
    write_crosssection(paths, cross_path, update_report=True)
    ok = audit["pass"] and len(final) >= MIN_POOL
    log(f"审计结论：{'PASS' if audit['pass'] else 'FAIL'}；入池 {len(final)} 只 "
        f"（验收下限 {MIN_POOL}）→ {'构建成功' if ok else '构建未完全达标'}")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# 维护命令
# --------------------------------------------------------------------------- #
def refresh_factors(args: argparse.Namespace, paths: Paths) -> int:
    """仅重取 ``adj_factor``（后复权因子）并回写 CSV，不动已抓好的不复权 OHLC。"""
    frame = read_bars_frame(paths.csv).copy()
    frame["adj_factor"] = frame["adj_factor"].astype(object)
    symbols = sorted(frame["symbol"].unique())
    log(f"刷新后复权因子：{len(symbols)} 只")
    updated = 0
    failures: list[dict] = []
    for idx, symbol in enumerate(symbols, 1):
        time.sleep(args.sleep)
        pairs, error = fetch_with_retry(fetch_hfq_factor, symbol, "后复权因子")
        if not pairs:
            failures.append({"symbol": symbol, "reason": error or "返回空表"})
            log(f"  [{idx}/{len(symbols)}] {symbol} 因子获取失败")
            continue
        dates = pd.to_datetime(frame.loc[frame["symbol"] == symbol, "date"])
        factors = [factor_asof(pairs, d.date()) for d in dates]
        if any(f is None for f in factors):
            failures.append({"symbol": symbol, "reason": "部分日期无 asof 因子"})
            log(f"  [{idx}/{len(symbols)}] {symbol} 因子覆盖不足")
            continue
        frame.loc[frame["symbol"] == symbol, "adj_factor"] = [f"{f:.10f}" for f in factors]
        updated += 1
    frame = frame[BAR_COLUMNS]
    frame.to_csv(paths.csv, index=False, encoding="utf-8")
    log(f"因子回写完成：{updated}/{len(symbols)} 只 → {rel(paths.csv)}")

    meta = load_universe_meta(paths)
    meta["adj_factor"] = "hfq_factor（后复权因子）：后复权价 = 不复权价 × adj_factor"
    meta["factors_refreshed_at"] = now_iso()
    save_universe(paths, load_universe_items(paths.json), meta)
    report = load_report_or_empty(paths.report)
    report["factor_failures"] = failures
    report["factors_refreshed_at"] = meta["factors_refreshed_at"]
    paths.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if updated == len(symbols) else 1


def fetch_official_names() -> dict[str, str]:
    """交易所官方 代码→名称（上交所主板/科创板 + 深交所 A 股列表）；失败返回空 dict。"""
    import akshare as ak
    names: dict[str, str] = {}

    def _collect(df, code_key: str, name_key: str) -> None:
        for rec in df.to_dict("records"):
            code = str(rec.get(code_key, "")).strip()
            name = clean_name(rec.get(name_key, ""))
            if code.isdigit() and len(code) == 6 and name:
                names[code] = name

    for symbol, code_key, name_key in (
        ("主板A股", "证券代码", "证券简称"),
        ("科创板", "证券代码", "证券简称"),
    ):
        try:
            _collect(ak.stock_info_sh_name_code(symbol=symbol), code_key, name_key)
        except Exception:  # noqa: BLE001
            pass
    try:
        _collect(ak.stock_info_sz_name_code(symbol="A股列表"), "A股代码", "A股简称")
    except Exception:  # noqa: BLE001
        pass
    return names


def fix_names(paths: Paths) -> int:
    """用交易所官方名称修正股票池（快照名可能被截断）。"""
    items = load_universe_items(paths.json)
    official = fetch_official_names()
    log(f"交易所官方名称拉取：{len(official)} 条")
    fixed = 0
    for item in items:
        name = official.get(str(item.get("symbol", ""))[:6])
        if name and name != item.get("name"):
            log(f"  {item['symbol']} 名称修正：{item.get('name')} → {name}")
            item["name"] = name
            fixed += 1
    meta = load_universe_meta(paths)
    meta["names_fixed_at"] = now_iso()
    save_universe(paths, items, meta)
    log(f"名称修正完成：{fixed}/{len(items)} 只 → {rel(paths.json)}")
    return fixed


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="构建 M6 短线研究股票池（200 只，不复权日线 + 后复权因子）")
    parser.add_argument("--limit", type=int, default=200, help="股票池数量（默认 200）")
    parser.add_argument("--candidates", type=int, default=280, help="进入日线核验的候选数（默认 280）")
    parser.add_argument("--sleep", type=float, default=0.25, help="逐只请求间隔秒数（默认 0.25）")
    parser.add_argument("--out-json", default=str(DEFAULT_OUT_JSON), help="股票池输出 JSON 路径")
    parser.add_argument("--out-csv", default=str(DEFAULT_OUT_CSV), help="日线长表输出 CSV 路径")
    parser.add_argument("--out-report", default=str(DEFAULT_OUT_REPORT), help="构建报告输出 JSON 路径")
    parser.add_argument("--coverage", default=str(DEFAULT_COVERAGE), help="覆盖审计报告输出 MD 路径")
    parser.add_argument("--crosssection-out", default=str(DEFAULT_CROSSSECTION),
                        help="分年截面规模统计输出 MD 路径（默认 runtime/exports/universe_200_crosssection.md）")
    parser.add_argument("--refresh-spot", action="store_true", help="重新拉取全市场快照（默认用工作区 a_spot.csv）")
    parser.add_argument("--refresh-factors", action="store_true",
                        help="只重取 adj_factor（后复权因子）并回写已有 CSV，不重抓行情")
    parser.add_argument("--audit-only", action="store_true",
                        help="只生成离线覆盖审计（不联网，重算 PASS/FAIL 并回写报告 audit 段）")
    parser.add_argument("--fix-names", action="store_true",
                        help="用交易所官方名称修正股票池里的名称（快照名可能被截断）")
    return parser


def resolve_paths(args: argparse.Namespace) -> Paths:
    return Paths(
        json=Path(args.out_json).expanduser().resolve(),
        csv=Path(args.out_csv).expanduser().resolve(),
        report=Path(args.out_report).expanduser().resolve(),
        coverage=Path(args.coverage).expanduser().resolve(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = resolve_paths(args)
    cross_path = Path(args.crosssection_out).expanduser().resolve()
    prepare_env()

    if args.fix_names:
        fix_names(paths)
        return 0
    if args.refresh_factors:
        return refresh_factors(args, paths)
    if args.audit_only:
        audit = write_audit(paths, update_report=True)
        write_crosssection(paths, cross_path, update_report=True)
        return 0 if audit["pass"] else 1
    return run_build(args, paths, cross_path)


if __name__ == "__main__":
    sys.exit(main())

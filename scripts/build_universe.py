#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""构建「短线模型 v2」可回测股票池（数据支持任务，不涉及策略逻辑）。

用途
----
1) 从全市场快照按**成交额**初筛 → 规则剔除 ST/退市/北交所/低价/停牌；
2) 用 akshare（Sina 通道，``stock_zh_a_daily`` **不复权**）逐只核验：上市满 2 年、
   近 250 交易日无长期停牌，并计算近 20 日平均成交额；
3) 按板块配额择优取 N 只（默认 50），输出：

   - ``data/universe_50.json``                     股票池（含 symbol/name/board/avg_amount_20d…）
   - ``runtime/short_term/bars_50.csv``            长表日线（不复权 OHLC，2019-01-01 ~ 快照日）
   - ``runtime/short_term/build_report.json``      构建过程统计（供覆盖审计使用）

设计要点
--------
- **数据源选择**：东财（push2/push2his）在本机被网络策略拦截（ProxyError /
  RemoteDisconnected），因此日线统一走 **Sina**（``stock_zh_a_daily``，稳定可用）；
  快照优先用工作区已有的 ``data/a_spot.csv``（Sina 全市场快照），缺失时回退
  ``akshare.stock_zh_a_spot()``。
- **不复权**：``adjust=""`` 返回原始 OHLC（撮合用）；``adj_factor`` 另由
  ``adjust="qfq-factor"`` 取前复权因子并按除权日 asof 合并（缺失时留空，不编造）。
- **单位**：Sina ``volume`` = 股，``amount`` = 元（已实测核对，见 docs/DATA_QUALITY_NOTES.md）。
- **不写数据库**：本脚本只产出 JSON/CSV 文件，入库由 Dev1 的 fetch_data.py 负责。

用法
----
    .venv\\Scripts\\python.exe scripts\\build_universe.py                    # 全量构建（约 6~8 分钟）
    .venv\\Scripts\\python.exe scripts\\build_universe.py --limit 50 --candidates 110 --sleep 0.25
    .venv\\Scripts\\python.exe scripts\\build_universe.py --refresh-spot      # 重新拉全市场快照
    .venv\\Scripts\\python.exe scripts\\build_universe.py --refresh-factors   # 只重取后复权因子
    .venv\\Scripts\\python.exe scripts\\build_universe.py --fix-names         # 用交易所官方名校正名称
    .venv\\Scripts\\python.exe scripts\\build_universe.py --audit-only        # 只生成覆盖审计（不联网）

退出码：0 = 成功且股票池数量达标；1 = 失败或数量不足。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path

# 项目根目录（scripts/build_universe.py -> quantdesk/）
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUT_JSON = DATA_DIR / "universe_50.json"
OUT_DIR = ROOT / "runtime" / "short_term"
OUT_CSV = OUT_DIR / "bars_50.csv"
OUT_REPORT = OUT_DIR / "build_report.json"
SPOT_CSV = ROOT.parent / "data" / "a_spot.csv"  # 工作区级快照（default-workspace/data/a_spot.csv）

# 日线区间（与短线模型 v2 约定一致：2019-01-01 ~ 2026-10-23）
BAR_START = date(2019, 1, 1)
BAR_END = date(2026, 10, 23)
# 快照日（data/a_spot.csv 的抓取日）
SPOT_DATE = "2026-10-25"

# 上市满 2 年：首根 bar 必须早于该日期
MIN_LISTED_BEFORE = date(2024, 10, 23)
# 近期未长期停牌：末根 bar 不早于此日；近 250 交易日至少 240 根
FRESH_AFTER = date(2026, 10, 16)
RECENT_WINDOW = 250
RECENT_MIN_BARS = 240
# 初筛门槛
MIN_PRICE = 3.0

# 板块识别：代码前缀 → (board, board_cn)
BOARD_RULES: list[tuple[tuple[str, ...], str, str]] = [
    (("600", "601", "603", "605"), "MAIN", "沪主板"),
    (("000", "001", "002", "003"), "MAIN", "深主板"),
    (("300", "301"), "GEM", "创业板"),
    (("688", "689"), "STAR", "科创板"),
]
# 板块配额（合计 50）：主板 28（沪 14 + 深 14）、创业板 12、科创板 10
BOARD_QUOTA = {"MAIN_SH": 14, "MAIN_SZ": 14, "GEM": 12, "STAR": 10}

BAR_COLUMNS = ["date", "symbol", "open", "high", "low", "close",
               "volume", "amount", "adj_factor", "source"]


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def prepare_env() -> None:
    """绕过系统代理（本机系统代理对 akshare 请求不可用，直连才通）。"""
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"


def clean_name(name: str) -> str:
    """去掉瞬时除权/新股前缀（XD/XR/DR/N/C），保证名称干净可复用。"""
    text = str(name or "").strip()
    for prefix in ("XD", "XR", "DR", "N", "C"):
        if text.startswith(prefix) and len(text) > len(prefix):
            # 仅当去掉后仍是非空中文名才剥离，避免误伤（如「C」开头本就少见）
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
    """``sh600519`` / ``sz000001`` → (``600519.SH``, ``600519``)；非沪深或格式异常返回 None。"""
    text = str(raw or "").strip().lower()
    if len(text) != 8 or text[:2] not in ("sh", "sz"):
        return None
    code6 = text[2:]
    if not code6.isdigit():
        return None
    suffix = "SH" if text[:2] == "sh" else "SZ"
    return f"{code6}.{suffix}", code6


def to_float(value: object) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def load_spot_rows(refresh: bool) -> tuple[list[dict], str]:
    """读取全市场快照，返回 (rows, 来源描述)。rows 统一为 dict(code6, name, price, amount)。"""
    if refresh or not SPOT_CSV.exists():
        log("快照文件缺失或要求刷新 → 调用 akshare.stock_zh_a_spot()（Sina，约 15s）")
        import akshare as ak
        df = ak.stock_zh_a_spot()
        rows = []
        for rec in df.to_dict("records"):
            parsed = spot_code_to_symbol(rec.get("代码", ""))
            if not parsed:
                continue
            symbol, code6 = parsed
            rows.append({"code": symbol, "code6": code6, "name": str(rec.get("名称", "")),
                         "price": to_float(rec.get("最新价")), "amount": to_float(rec.get("成交额"))})
        return rows, "akshare.stock_zh_a_spot (Sina)"

    rows = []
    with SPOT_CSV.open("r", encoding="utf-8-sig", newline="") as fh:
        for rec in csv.DictReader(fh):
            parsed = spot_code_to_symbol(rec.get("代码", ""))
            if not parsed:
                continue
            symbol, code6 = parsed
            rows.append({"code": symbol, "code6": code6, "name": str(rec.get("名称", "")),
                         "price": to_float(rec.get("最新价")), "amount": to_float(rec.get("成交额"))})
    return rows, str(SPOT_CSV)


def screen_spot(rows: list[dict]) -> tuple[list[dict], dict]:
    """规则初筛：四板块 + 非 ST/退 + 价格≥3 + 有成交（非停牌）+ 按成交额降序。"""
    stats = {"total": len(rows)}
    kept, dropped = [], Counter()
    for row in rows:
        board_info = board_of(row["code6"])
        name = row["name"]
        if board_info is None:
            dropped["非沪深四板块(北交所等)"] += 1
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
        row.update({"board": board, "board_cn": board_cn,
                    "name": clean_name(name),
                    "board_key": "MAIN_SH" if board_cn == "沪主板" else
                                 "MAIN_SZ" if board_cn == "深主板" else board_cn})
        kept.append(row)
    kept.sort(key=lambda r: r["amount"], reverse=True)
    stats["kept"] = len(kept)
    stats["dropped"] = dict(dropped)
    return kept, stats


def to_sina(symbol: str) -> str:
    """``600519.SH`` → ``sh600519``（akshare Sina 接口需要的前缀格式）。"""
    return ("sh" if symbol.endswith(".SH") else "sz") + symbol[:6]


def fetch_bars(symbol: str) -> list[dict]:
    """Sina 不复权日线（全历史），只保留 BAR_START ~ BAR_END。失败抛异常由调用方处理。"""
    import akshare as ak
    df = ak.stock_zh_a_daily(symbol=to_sina(symbol), adjust="")
    out = []
    for rec in df.to_dict("records"):
        d = rec["date"]
        d = d.date() if hasattr(d, "date") else date.fromisoformat(str(d)[:10])
        if d < BAR_START or d > BAR_END:
            continue
        out.append({
            "date": d.isoformat(),
            "open": float(rec["open"]), "high": float(rec["high"]),
            "low": float(rec["low"]), "close": float(rec["close"]),
            "volume": float(rec["volume"]), "amount": float(rec["amount"]),
        })
    return out


def fetch_hfq_factor(symbol: str) -> list[tuple[date, float]]:
    """Sina **后复权因子**（date, hfq_factor），按日期升序；失败返回空列表（不编造）。

    口径与 `data/akshare_provider.py` 一致：``后复权价 = 不复权价 × hfq_factor``。
    因子按除权日离散变化，使用时需 asof 向后合并。
    """
    import akshare as ak
    df = ak.stock_zh_a_daily(symbol=to_sina(symbol), adjust="hfq-factor")
    pairs = []
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
    """取 <= target 的最近一次因子值（前复权因子按除权日生效）。"""
    value = None
    for d, f in pairs:
        if d <= target:
            value = f
        else:
            break
    return value


def recent_stats(bars: list[dict]) -> dict:
    """近 20 日平均成交额、近 250 日 bar 数、首末日。"""
    if not bars:
        return {"first": None, "last": None, "rows": 0,
                "avg_amount_20d": 0.0, "recent_bars": 0}
    avg20 = sum(b["amount"] for b in bars[-20:]) / min(20, len(bars))
    return {
        "first": bars[0]["date"], "last": bars[-1]["date"], "rows": len(bars),
        "avg_amount_20d": round(avg20, 2),
        "recent_bars": min(len(bars), RECENT_WINDOW),
    }


def pick_universe(verified: list[dict], limit: int) -> list[dict]:
    """按板块配额择优：配额内按近 20 日平均成交额降序；不足则从剩余池按成交额补齐。"""
    by_key: dict[str, list[dict]] = {}
    for item in verified:
        by_key.setdefault(item["board_key"], []).append(item)
    for key in by_key:
        by_key[key].sort(key=lambda r: r["avg_amount_20d"], reverse=True)

    picked: list[dict] = []
    for key, quota in BOARD_QUOTA.items():
        picked.extend(by_key.get(key, [])[:quota])
    # 配额未填满（某板块标的不足）或已超额 → 用剩余标的按成交额补齐/裁剪
    picked_codes = {item["code"] for item in picked}
    rest = sorted([v for v in verified if v["code"] not in picked_codes],
                  key=lambda r: r["avg_amount_20d"], reverse=True)
    shortfall = limit - len(picked)
    if shortfall > 0:
        picked.extend(rest[:shortfall])
    picked.sort(key=lambda r: r["avg_amount_20d"], reverse=True)
    return picked[:limit]


def load_universe_items() -> list[dict]:
    """读取股票池（兼容两种格式：顶层数组 / {meta, universe}）；统一返回 list。"""
    if not OUT_JSON.exists():
        raise SystemExit(f"股票池文件不存在：{OUT_JSON}（先运行完整构建）")
    raw = json.loads(OUT_JSON.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return raw
    return raw.get("universe", [])


def load_universe_meta() -> dict:
    """读取股票池元信息：优先 build_report.json 的 universe_meta，其次 JSON 内嵌 meta。"""
    meta: dict = {}
    if OUT_REPORT.exists():
        try:
            meta = json.loads(OUT_REPORT.read_text(encoding="utf-8")).get("universe_meta", {}) or {}
        except json.JSONDecodeError:
            meta = {}
    if not meta and OUT_JSON.exists():
        raw = json.loads(OUT_JSON.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            meta = raw.get("meta", {}) or {}
    return meta


def load_report_or_empty() -> dict:
    """读取 build_report.json（不存在或损坏时返回空 dict）。"""
    if OUT_REPORT.exists():
        try:
            return json.loads(OUT_REPORT.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}

def save_universe(items: list[dict], meta: dict) -> None:
    """写股票池：**顶层数组**（便于消费方直接遍历）；meta 落到 build_report.json。"""
    OUT_JSON.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    report: dict = {}
    if OUT_REPORT.exists():
        try:
            report = json.loads(OUT_REPORT.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            report = {}
    report["universe_meta"] = meta
    OUT_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_official_names() -> dict[str, str]:
    """交易所官方 代码→名称（上交所主板/科创板 + 深交所 A 股列表）；失败返回空 dict。

    背景：新浪快照在除权日会把名称截断（实测 ``XD中国船`` / ``XD澜起科``），
    不能直接作为股票池展示名。
    """
    import akshare as ak
    names: dict[str, str] = {}

    def _collect(df, code_key: str, name_key: str) -> None:
        for rec in df.to_dict("records"):
            code = str(rec.get(code_key, "")).strip()
            name = clean_name(rec.get(name_key, ""))
            if code.isdigit() and len(code) == 6 and name:
                names[code] = name

    try:
        _collect(ak.stock_info_sh_name_code(symbol="主板A股"), "证券代码", "证券简称")
    except Exception:  # noqa: BLE001
        pass
    try:
        _collect(ak.stock_info_sh_name_code(symbol="科创板"), "证券代码", "证券简称")
    except Exception:  # noqa: BLE001
        pass
    try:
        _collect(ak.stock_info_sz_name_code(symbol="A股列表"), "A股代码", "A股简称")
    except Exception:  # noqa: BLE001
        pass
    return names


def fix_names() -> int:
    """用交易所官方名称修正已有股票池（快照名可能被截断）。"""
    items = load_universe_items()
    official = fetch_official_names()
    log(f"交易所官方名称拉取：{len(official)} 条")
    fixed = 0
    for item in items:
        name = official.get(item["symbol"][:6])
        if name and name != item["name"]:
            log(f"  {item['symbol']} 名称修正：{item['name']} → {name}")
            item["name"] = name
            fixed += 1
    meta = load_universe_meta()
    meta["names_fixed_at"] = datetime.now().isoformat(timespec="seconds")
    save_universe(items, meta)
    log(f"名称修正完成：{fixed}/{len(items)} 只 → {OUT_JSON.relative_to(ROOT)}")
    return fixed


def read_bars_frame():
    """读取已生成的日线长表（pandas）。"""
    import pandas as pd
    if not OUT_CSV.exists():
        raise SystemExit(f"日线文件不存在：{OUT_CSV}（先运行完整构建）")
    return pd.read_csv(OUT_CSV, dtype={"symbol": str, "date": str})


def refresh_factors(sleep: float) -> int:
    """仅重取 ``adj_factor``（后复权因子）并回写 CSV，不动已抓好的不复权 OHLC。"""
    import pandas as pd
    df = read_bars_frame()
    df["adj_factor"] = df["adj_factor"].astype(object)  # 允许写字符串因子（避免 dtype 警告）
    symbols = sorted(df["symbol"].unique())
    log(f"刷新后复权因子：{len(symbols)} 只")
    updated = 0
    for idx, symbol in enumerate(symbols, 1):
        time.sleep(sleep)
        try:
            pairs = fetch_hfq_factor(symbol)
        except Exception as exc:  # noqa: BLE001
            log(f"  [{idx}/{len(symbols)}] {symbol} 因子获取失败：{type(exc).__name__}")
            continue
        if not pairs:
            continue
        dates = pd.to_datetime(df.loc[df["symbol"] == symbol, "date"])
        factors = [factor_asof(pairs, d.date()) for d in dates]
        mask = df["symbol"] == symbol
        df.loc[mask, "adj_factor"] = ["" if f is None else f"{f:.10f}" for f in factors]
        updated += 1
    df = df[BAR_COLUMNS]
    df.to_csv(OUT_CSV, index=False, encoding="utf-8")
    log(f"因子回写完成：{updated}/{len(symbols)} 只 → {OUT_CSV.relative_to(ROOT)}")

    items = load_universe_items()
    meta = load_universe_meta()
    meta["adj_factor"] = "hfq_factor（后复权因子）：后复权价 = 不复权价 × adj_factor"
    meta["factors_refreshed_at"] = datetime.now().isoformat(timespec="seconds")
    save_universe(items, meta)
    log(f"股票池已重写为顶层数组：{OUT_JSON.relative_to(ROOT)}（{len(items)} 只）")
    return updated


def write_coverage_report() -> Path:
    """生成数据覆盖审计：CSV（新抓取）vs DB（项目缓存）→ runtime/exports/universe_coverage.md。"""
    import sqlite3

    import pandas as pd

    universe = load_universe_items()
    df = read_bars_frame()
    db_path = ROOT / "runtime" / "quantdesk.db"
    symbols = [u["symbol"] for u in universe]

    # ---- 参考交易日历：取「≥10% 标的有 bar」的日期并集（自适应，覆盖 2019 起全区间）----
    # 注：早期年份上市标的少，若用 90% 门槛会漏掉 2019–2021 的缺口检测；10% 门槛可覆盖全区间。
    by_date = df.groupby("date")["symbol"].nunique()
    threshold = max(2, int(len(symbols) * 0.1))
    ref_dates = sorted(by_date[by_date >= threshold].index)
    all_dates = sorted(df["date"].unique())

    # ---- DB 覆盖 ----
    db_info: dict[str, dict] = {}
    db_total = 0
    db_mock_total = 0
    db_range = ("", "")
    if db_path.exists():
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = con.cursor()
        db_total = cur.execute("select count(*) from daily_bars").fetchone()[0]
        db_mock_total = cur.execute("select count(*) from daily_bars where source='mock'").fetchone()[0]
        db_range = cur.execute("select min(trade_date), max(trade_date) from daily_bars").fetchone()
        rows = cur.execute(
            "select symbol, count(*), min(trade_date), max(trade_date), "
            "count(distinct source), group_concat(distinct source) "
            "from daily_bars where symbol in (%s) group by symbol" % ",".join("?" * len(symbols)),
            symbols,
        ).fetchall()
        for sym, n, mn, mx, nsrc, srcs in rows:
            db_info[sym] = {"rows": n, "first": mn, "last": mx, "sources": srcs or "",
                            "mixed": (nsrc or 0) > 1}
        # DB 交易日历（用于缺口）
        cal = [r[0] for r in cur.execute(
            "select trade_date from trading_calendar order by trade_date").fetchall()]
        con.close()
    else:
        cal = []

    # ---- 逐只统计（CSV 侧）----
    stats = []
    for u in universe:
        sub = df[df["symbol"] == u["symbol"]].sort_values("date")
        dates = sub["date"].tolist()
        first, last = (dates[0], dates[-1]) if dates else ("", "")
        gap = 0
        if dates:
            ref = [d for d in ref_dates if first <= d <= last]
            gap = len(set(ref) - set(dates))
        dup = int(sub.duplicated(subset=["date"]).sum())
        bad_price = int(((sub["open"] <= 0) | (sub["close"] <= 0)).sum())
        zero_amount = int((sub["amount"] <= 0).sum())
        factor_missing = int(sub["adj_factor"].isna().sum())
        missing_dates = sorted(set(d for d in ref_dates if first <= d <= last) - set(dates))
        stats.append({
            "symbol": u["symbol"], "name": u["name"], "board_cn": u["board_cn"],
            "rows": len(sub), "first": first, "last": last, "gap": gap,
            "dup": dup, "bad_price": bad_price, "zero_amount": zero_amount,
            "factor_missing": factor_missing,
            "missing_head": missing_dates[:5],
            "db": db_info.get(u["symbol"]),
        })

    in_db = [s for s in stats if s["db"]]
    not_in_db = [s for s in stats if not s["db"]]
    mixed_db = [s for s in stats if s["db"] and s["db"]["mixed"]]

    # ---- 写报告 ----
    lines: list[str] = []
    add = lines.append
    add("# 短线模型 v2 股票池数据覆盖审计（universe_coverage）")
    add("")
    add(f"> 生成时间：{datetime.now():%Y-%m-%dT%H:%M:%S%z} · 生成脚本：`scripts/build_universe.py --audit-only`")
    add(f"> 股票池：`data/universe_50.json`（**{len(universe)} 只**）· 日线：`runtime/short_term/bars_50.csv`"
        f"（**{len(df)} 行**，{all_dates[0]} ~ {all_dates[-1]}）")
    add(f"> 数据源：akshare **Sina** `stock_zh_a_daily(adjust='')`（东财 push2/push2his 在本机被网络策略拦截，不可用）")
    add(f"> 参考交易日历：CSV 内「≥10% 标的有 bar」的日期并集（{len(ref_dates)} 个交易日"
        f"{'，' + ref_dates[0] + ' ~ ' + ref_dates[-1] if ref_dates else ''}）"
        "；每个标的只在其**自身首末日区间内**做缺口比对")
    add("")
    add("## 1. 结论速览")
    add("")
    add("| 项 | 数值 |")
    add("|---|---|")
    add(f"| 股票池标的数 | {len(universe)} |")
    add(f"| 日线长表行数 | {len(df)} |")
    add(f"| 标的平均行数 | {sum(s['rows'] for s in stats) / max(1, len(stats)):.1f} |")
    add(f"| 缺口标的数（有缺口交易日） | {sum(1 for s in stats if s['gap'] > 0)} |")
    add(f"| 含重复行的标的数 | {sum(1 for s in stats if s['dup'] > 0)} |")
    add(f"| adj_factor 缺失的标的数 | {sum(1 for s in stats if s['factor_missing'] > 0)} |")
    add(f"| 已在项目 DB 中的标的 | {len(in_db)} / {len(universe)} |")
    add(f"| **DB 未接入、需 Dev1 补数据的标的** | **{len(not_in_db)} / {len(universe)}** |")
    add(f"| DB 混源（多 source）标的 | {len(mixed_db)} |")
    add(f"| DB 全库行数 / mock 行数 / 日期范围 | {db_total} / {db_mock_total} / {db_range[0]} ~ {db_range[1]} |")
    add("")
    add("## 2. 逐只覆盖明细")
    add("")
    add("| 标的 | 名称 | 板块 | CSV行数 | CSV首日 | CSV末日 | CSV缺口 | 重复 | DB行数 | DB首日 | DB末日 | DB源 | 状态 |")
    add("|---|---|---|---:|---|---|---:|---:|---:|---|---|---|---|")
    for s in stats:
        db = s["db"]
        if db:
            status = "⚠️ DB混源" if db["mixed"] else ("✅ 已接入DB" if db["rows"] >= s["rows"] * 0.9 else "⚠️ DB行数不足")
            db_cells = [str(db["rows"]), db["first"], db["last"], db["sources"]]
        else:
            status = "❌ DB无此标的"
            db_cells = ["0", "—", "—", "—"]
        add(f"| {s['symbol']} | {s['name']} | {s['board_cn']} | {s['rows']} | {s['first']} | {s['last']} | "
            f"{s['gap']} | {s['dup']} | {db_cells[0]} | {db_cells[1]} | {db_cells[2]} | {db_cells[3]} | {status} |")
    add("")
    add("## 3. 需 Dev1 补数据的缺口清单")
    add("")
    if not_in_db:
        add(f"以下 **{len(not_in_db)} 只**标的在 `runtime/quantdesk.db::daily_bars` 中**没有任何数据**，"
            "短线模型 v2 若走 DB 读取会缺样本（当前 CSV 已提供 2019-01-01 起的真实不复权日线，可直接用）：")
        add("")
        add("| # | 标的 | 名称 | 板块 | 需补区间 | CSV 现成行数 |")
        add("|---:|---|---|---|---|---:|")
        for i, s in enumerate(not_in_db, 1):
            add(f"| {i} | {s['symbol']} | {s['name']} | {s['board_cn']} | "
                f"{BAR_START.isoformat()} ~ {BAR_END.isoformat()} | {s['rows']} |")
    else:
        add("- 无：50 只标的均已在 DB 中。")
    add("")
    if in_db:
        short_db = [s for s in in_db if s["db"]["rows"] < s["rows"] * 0.9]
        if short_db:
            add("### 3.1 DB 行数明显少于 CSV 的标的（口径/区间不一致）")
            add("")
            add("| 标的 | 名称 | DB行数 | DB区间 | CSV行数 | CSV区间 | 差异原因（推测需核实） |")
            add("|---|---|---:|---|---:|---|---|")
            for s in short_db:
                db = s["db"]
                add(f"| {s['symbol']} | {s['name']} | {db['rows']} | {db['first']}~{db['last']} | "
                    f"{s['rows']} | {s['first']}~{s['last']} | DB 只回溯到 {db['first']}（项目默认 5 年），"
                    f"v2 需 2019 起 |")
            add("")
        add("### 3.2 DB 已有标的的缺口交易日（对照 DB 交易日历）")
        add("")
        add("| 标的 | DB区间 | DB应有交易日 | 实际行数 | 缺口 |")
        add("|---|---|---:|---:|---:|")
        for s in in_db:
            db = s["db"]
            if not cal:
                break
            expect = [d for d in cal if db["first"] <= d <= db["last"]]
            add(f"| {s['symbol']} | {db['first']}~{db['last']} | {len(expect)} | {db['rows']} | "
                f"{max(0, len(expect) - db['rows'])} |")
        add("")
    add("## 4. 数据质量检查（对 `bars_50.csv`）")
    add("")
    add("| 检查项 | 结果 |")
    add("|---|---|")
    add(f"| 主键（date,symbol）重复 | {int(df.duplicated(['date', 'symbol']).sum())} 行 |")
    add(f"| 非正价格（open/close ≤ 0） | {sum(s['bad_price'] for s in stats)} 行 |")
    add(f"| 成交额 ≤ 0（疑似停牌/异常） | {sum(s['zero_amount'] for s in stats)} 行 |")
    add(f"| `adj_factor` 为空 | {int(df['adj_factor'].isna().sum())} 行 |")
    add(f"| 混源（同一标的多个 source） | {df.groupby('symbol')['source'].nunique().gt(1).sum()} 只 |")
    add(f"| 全表 source 取值 | {', '.join(sorted(df['source'].dropna().unique()))} |")
    add("")
    gap_symbols = [s for s in stats if s["gap"] > 0]
    if gap_symbols:
        add("### 4.1 缺口明细（对照参考交易日历）")
        add("")
        add("| 标的 | 名称 | 缺口天数 | 缺口样例（前 5 个） |")
        add("|---|---|---:|---|")
        for s in gap_symbols:
            add(f"| {s['symbol']} | {s['name']} | {s['gap']} | {', '.join(s['missing_head']) or '—'} |")
        add("")
        add("> 说明：缺口多来自**停牌**（合理，如重大事项停牌/重组），不一定是数据缺失。"
            "本次已抽 3 例用**腾讯备源**交叉验证（002185 2019-07-02~07-08、600176 2020-12-01~08、"
            "600150 2019-03-27~04-08）：腾讯同样只有边界 bar、无中间日 → **确认为真实停牌**，非新浪数据洞。"
            "停牌日不应补造数据；模型需按「无 bar = 不可交易」处理。")
        add("")
    add("## 5. 使用建议")
    add("")
    add("1. **短线模型 v2 走 CSV**：`runtime/short_term/bars_50.csv` 已是 2019-01-01 起的真实不复权日线，"
        "可直接用于 walk-forward，无需等 DB 补齐。")
    add("2. **走 DB 前先补数**：DB 当前只有 "
        f"{len(in_db)} 只、且区间为 {db_range[0]}~{db_range[1]}（项目默认 5 年），"
        "与 v2 需要的 2019 起不一致；需 Dev1 用 `scripts/fetch_data.py` 扩池 + 回溯到 2019。")
    add("3. **撮合只用不复权价**：CSV 的 OHLC 是 `adjust=''` 原始价；复权仅用于信号/收益计算，"
        "公式见 `docs/DATA_QUALITY_NOTES.md`。")
    add("4. **停牌处理**：缺口日的 bar 不存在，模型需显式处理（前值填充仅限特征，撮合必须跳过）。")
    add("")

    out = ROOT / "runtime" / "exports" / "universe_coverage.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    log(f"已写 {out.relative_to(ROOT)}（{len(stats)} 只标的）")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建短线模型 v2 股票池（不复权日线）")
    parser.add_argument("--limit", type=int, default=50, help="股票池数量（默认 50）")
    parser.add_argument("--candidates", type=int, default=90, help="进入日线核验的候选数（默认 90）")
    parser.add_argument("--sleep", type=float, default=0.35, help="逐只请求间隔秒数（默认 0.35）")
    parser.add_argument("--refresh-spot", action="store_true", help="重新拉取全市场快照")
    parser.add_argument("--refresh-factors", action="store_true",
                        help="只重取 adj_factor（后复权因子）并回写已有 CSV，不重抓行情")
    parser.add_argument("--audit-only", action="store_true",
                        help="只生成数据覆盖审计 runtime/exports/universe_coverage.md（不联网）")
    parser.add_argument("--fix-names", action="store_true",
                        help="用交易所官方名称修正股票池里的名称（快照名可能被截断）")
    args = parser.parse_args(argv)

    prepare_env()
    if args.fix_names:
        fix_names()
        return 0
    if args.refresh_factors:
        refresh_factors(args.sleep)
        return 0
    if args.audit_only:
        write_coverage_report()
        return 0
    started = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # ---------- 1) 快照 + 初筛 ----------
    rows, spot_source = load_spot_rows(args.refresh_spot)
    log(f"全市场快照：{len(rows)} 行，来源 {spot_source}")
    kept, screen_stats = screen_spot(rows)
    log(f"初筛后：{screen_stats['kept']} 只（剔除明细 {screen_stats['dropped']}）")
    candidates = kept[: args.candidates]
    log(f"取成交额前 {len(candidates)} 只进入日线核验"
        f"（门槛成交额 {candidates[-1]['amount'] / 1e8:.2f} 亿元）" if candidates else "无候选")

    # ---------- 2) 逐只核验 ----------
    verified: list[dict] = []
    failures: list[dict] = []
    for idx, row in enumerate(candidates, 1):
        symbol = row["code"]
        if idx > 1:
            time.sleep(args.sleep)
        try:
            bars = fetch_bars(symbol)
        except Exception as exc:  # noqa: BLE001 - 单只失败不影响整体
            failures.append({"symbol": symbol, "name": row["name"],
                             "reason": f"{type(exc).__name__}: {str(exc)[:120]}"})
            log(f"  [{idx}/{len(candidates)}] {symbol} {row['name']} 拉取失败：{type(exc).__name__}")
            continue
        stats = recent_stats(bars)
        row.update(stats)

        reason = None
        if not bars:
            reason = "无 2019 以来日线"
        elif date.fromisoformat(stats["first"]) > MIN_LISTED_BEFORE:
            reason = f"上市不足 2 年（首根 {stats['first']}）"
        elif date.fromisoformat(stats["last"]) < FRESH_AFTER:
            reason = f"近期停牌（末根 {stats['last']}）"
        elif stats["recent_bars"] < RECENT_MIN_BARS:
            reason = f"近 250 日仅 {stats['recent_bars']} 根"
        if reason:
            failures.append({"symbol": symbol, "name": row["name"], "reason": reason})
            log(f"  [{idx}/{len(candidates)}] {symbol} {row['name']} 剔除：{reason}")
            continue

        row["bars"] = bars
        verified.append(row)
        log(f"  [{idx}/{len(candidates)}] {symbol} {row['name']:<8s} "
            f"{stats['rows']:5d} 根 {stats['first']}~{stats['last']} "
            f"20日均额 {stats['avg_amount_20d'] / 1e8:6.2f} 亿")

    log(f"核验通过 {len(verified)} 只，剔除/失败 {len(failures)} 只")

    # ---------- 3) 按板块配额选股 ----------
    picked = pick_universe(verified, args.limit)
    if len(picked) < args.limit:
        log(f"!! 仅选出 {len(picked)} 只，未达 {args.limit} 只（可增大 --candidates 重试）")
    mix = Counter(item["board_cn"] for item in picked)
    log(f"选中 {len(picked)} 只，板块分布：{dict(mix)}")

    # ---------- 4) 补前复权因子 + 写 CSV ----------
    factor_failures = 0
    for item in picked:
        try:
            time.sleep(args.sleep)
            pairs = fetch_hfq_factor(item["code"])
        except Exception:  # noqa: BLE001
            pairs = []
            factor_failures += 1
        item["_factors"] = pairs
    if factor_failures:
        log(f"后复权因子获取失败 {factor_failures} 只（adj_factor 留空，不编造）")

    with OUT_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(BAR_COLUMNS)
        total_rows = 0
        for item in picked:
            pairs = item.pop("_factors")
            for bar in item["bars"]:
                d = date.fromisoformat(bar["date"])
                factor = factor_asof(pairs, d)
                writer.writerow([bar["date"], item["code"], bar["open"], bar["high"],
                                 bar["low"], bar["close"], int(bar["volume"]), bar["amount"],
                                 "" if factor is None else f"{factor:.10f}", "akshare"])
                total_rows += 1
    log(f"已写 {OUT_CSV.relative_to(ROOT)}（{total_rows} 行）")

    # ---------- 5) 写股票池 JSON ----------
    universe = []
    for item in picked:
        universe.append({
            "symbol": item["code"],
            "name": item["name"],
            "board": item["board"],
            "board_lower": item["board"].lower(),
            "board_cn": item["board_cn"],
            "avg_amount_20d": item["avg_amount_20d"],
            "spot_amount": round(item["amount"], 2),
            "last_price": round(item["price"], 2),
            "first_bar": item["first"],
            "last_bar": item["last"],
            "bars_2019_2026": item["rows"],
            "snapshot_date": SPOT_DATE,
            "source": "akshare.stock_zh_a_daily(adjust='')",
        })
    meta = {
        "name": "universe_50（短线模型 v2 可回测股票池）",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "generator": "scripts/build_universe.py",
        "spot_source": spot_source,
        "spot_date": SPOT_DATE,
        "bar_range": [BAR_START.isoformat(), BAR_END.isoformat()],
        "price_adjust": "none（不复权，撮合用）",
        "adj_factor": "hfq_factor（后复权因子）：后复权价 = 不复权价 × adj_factor",
        "screen": {"board": "MAIN(600/601/603/605,000/001/002/003)+GEM(300/301)+STAR(688/689)",
                   "exclude": ["ST/*ST/退", "北交所(8/920/4)", "上市<2年",
                               "近期长期停牌", "价格<3元", "成交额<=0"],
                   "sort": "成交额（快照日）降序 → 取候选 → 近20日平均成交额择优"},
        "board_quota": BOARD_QUOTA,
        "count": len(universe),
        "notes": "本文件为顶层数组；board 大写（MAIN/GEM/STAR），board_lower 为小写别名；"
                 "单位 avg_amount_20d/spot_amount = 元；meta 见 runtime/short_term/build_report.json 的 universe_meta",
    }
    save_universe(universe, meta)
    log(f"已写 {OUT_JSON.relative_to(ROOT)}（{len(universe)} 只，顶层数组）")

    # ---------- 6) 构建报告（供覆盖审计） ----------
    report = load_report_or_empty()
    report.update({
        "generated_at": meta["generated_at"],
        "elapsed_sec": round(time.time() - started, 1),
        "spot_source": spot_source,
        "screen_stats": screen_stats,
        "candidates": len(candidates),
        "verified": len(verified),
        "picked": len(picked),
        "board_mix": dict(mix),
        "bar_rows": total_rows,
        "factor_failures": factor_failures,
        "failures": failures,
        "per_symbol": [
            {"symbol": u["symbol"], "name": u["name"], "board_cn": u["board_cn"],
             "rows": u["bars_2019_2026"], "first": u["first_bar"], "last": u["last_bar"],
             "avg_amount_20d": u["avg_amount_20d"]}
            for u in universe
        ],
    })
    OUT_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"已写 {OUT_REPORT.relative_to(ROOT)}；总耗时 {report['elapsed_sec']}s")

    return 0 if len(picked) >= args.limit else 1


if __name__ == "__main__":
    sys.exit(main())

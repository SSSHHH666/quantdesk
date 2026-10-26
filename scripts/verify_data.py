#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""QuantDesk 数据体检（一键、只读、可被 release_check 引用）。

用途
----
对 ``runtime/quantdesk.db`` 做一次性数据完整性检查，覆盖：

| 编号 | 检查项 | 触发级别 |
|---|---|---|
| C1 | 数据库文件存在 + ``PRAGMA integrity_check`` | ERROR |
| C2 | ``daily_bars`` 规模（行数/标的数/日期范围） | ERROR（空库） |
| C3 | 来源唯一性：同一标的多 ``source`` 混存 / 存在 ``mock`` 行 | ERROR |
| C4 | 缺口：对照 ``trading_calendar`` 的缺失交易日 | WARN |
| C5 | 复权因子：``adj_factor`` 为空 | WARN |
| C6 | 停牌标记：零成交量未标记停牌 / 标记停牌却有成交 | WARN |
| C7 | ``data_quality_issues``：统计 **并复核是否已过期（stale）** | ERROR（active）/ WARN（stale） |
| C8 | 标的元数据一致性：``symbols.exchange`` / ``board`` 与代码前缀是否相符 | WARN |
| C9 | 交易日历覆盖 ``daily_bars`` 的首末日 | WARN |

**只读**：全程 ``mode=ro`` 连接，不建表、不写库、不改数据。

用法
----
    .venv\\Scripts\\python.exe scripts\\verify_data.py                  # 体检默认 DB
    .venv\\Scripts\\python.exe scripts\\verify_data.py --db runtime/quantdesk.db
    .venv\\Scripts\\python.exe scripts\\verify_data.py --symbols 600519.SH,000001.SZ
    .venv\\Scripts\\python.exe scripts\\verify_data.py --json           # 机器可读（供 release_check）
    .venv\\Scripts\\python.exe scripts\\verify_data.py --strict         # WARN 也算失败

退出码
------
    0 —— PASS（没有 ERROR；--strict 时也没有 WARN）
    1 —— FAIL（存在 ERROR，或 --strict 下存在 WARN）
    2 —— 参数/环境错误（找不到数据库、无法解析、库损坏读不出）

给 ``release_check.py`` 的接入建议（**不改 release_check**）：以子进程调用本脚本并读退出码，
或 `--json` 解析 ``summary.error`` / ``summary.warn``；本脚本不 import 项目模块，无副作用。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "runtime" / "quantdesk.db"

PREFIX_BOARD = [
    (("600", "601", "603", "605"), "SH", "MAIN"),
    (("000", "001", "002", "003"), "SZ", "MAIN"),
    (("300", "301"), "SZ", "GEM"),
    (("688", "689"), "SH", "STAR"),
]
SYMBOL_RE = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$")

LEVEL_ERROR = "ERROR"
LEVEL_WARN = "WARN"
LEVEL_INFO = "INFO"


class Report:
    """收集检查结果。"""

    def __init__(self) -> None:
        self.checks: list[dict] = []

    def add(self, code: str, title: str, level: str, detail: str, ok: bool = True) -> None:
        self.checks.append({"code": code, "title": title, "level": level,
                            "ok": ok, "detail": detail})

    # -- 统计 --
    def count(self, level: str, only_bad: bool = False) -> int:
        return sum(1 for c in self.checks
                   if c["level"] == level and (not only_bad or not c["ok"]))

    def bad(self, level: str) -> list[dict]:
        return [c for c in self.checks if c["level"] == level and not c["ok"]]


def resolve_db(explicit: str | None) -> Path:
    """解析数据库路径：--db > config.yaml 的 database.url > runtime/quantdesk.db。"""
    if explicit:
        return Path(explicit).resolve()
    cfg = ROOT / "config.yaml"
    if cfg.exists():
        try:
            import yaml  # 项目依赖，已安装
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            url = str((data.get("database") or {}).get("url", ""))
            if url.startswith("sqlite:///"):
                rel = url.replace("sqlite:///", "", 1)
                return (ROOT / rel).resolve()
        except Exception:  # noqa: BLE001 - 解析失败退回默认
            pass
    return DEFAULT_DB


def board_prefix(code6: str) -> tuple[str, str] | None:
    for prefixes, exchange, board in PREFIX_BOARD:
        if code6.startswith(prefixes):
            return exchange, board
    return None


def connect_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)


def check_db_file(rep: Report, path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        rep.add("C1", "数据库文件", LEVEL_ERROR, f"找不到数据库：{path}（先运行 scripts/init_db.py）", ok=False)
        return None
    size = path.stat().st_size
    try:
        con = connect_ro(path)
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        rep.add("C1", "数据库文件", LEVEL_ERROR, f"打开/读取失败：{exc}", ok=False)
        return None
    if integrity != "ok":
        rep.add("C1", "数据库完整性", LEVEL_ERROR, f"integrity_check={integrity}", ok=False)
    else:
        wal = Path(str(path) + "-wal")
        extra = f" + WAL {wal.stat().st_size:,}B" if wal.exists() else ""
        rep.add("C1", "数据库文件", LEVEL_INFO,
                f"{path.name}：{size:,} 字节{extra}，integrity_check=ok，journal_mode="
                f"{con.execute('PRAGMA journal_mode').fetchone()[0]}")
    return con


def check_scale(rep: Report, con: sqlite3.Connection, symbols: list[str] | None) -> list[str]:
    where, params = "", []
    if symbols:
        where = " where symbol in (%s)" % ",".join("?" * len(symbols))
        params = symbols
    total = con.execute(f"select count(*) from daily_bars{where}", params).fetchone()[0]
    if total == 0:
        rep.add("C2", "daily_bars 规模", LEVEL_ERROR,
                "daily_bars 为空（先运行 scripts/fetch_data.py）", ok=False)
        return []
    rows = con.execute(
        f"select symbol, count(*), min(trade_date), max(trade_date) from daily_bars{where} "
        "group by symbol order by symbol", params).fetchall()
    found = [r[0] for r in rows]
    span = f"{min(r[2] for r in rows)} ~ {max(r[3] for r in rows)}"
    rep.add("C2", "daily_bars 规模", LEVEL_INFO,
            f"{total:,} 行 / {len(found)} 只标的 / {span}")
    if symbols:
        missing = [s for s in symbols if s not in found]
        if missing:
            rep.add("C2", "标的覆盖", LEVEL_WARN,
                    f"以下标的在 daily_bars 中无数据：{'、'.join(missing[:10])}", ok=False)
    return found


def check_sources(rep: Report, con: sqlite3.Connection) -> None:
    mixed = con.execute(
        "select symbol, count(distinct source), group_concat(distinct source) "
        "from daily_bars group by symbol having count(distinct source) > 1").fetchall()
    if mixed:
        rep.add("C3", "来源唯一性", LEVEL_ERROR,
                "同一标的存在多 source（禁止拼接，UB 混合会算出假收益）："
                + "；".join(f"{s}({n}:{src})" for s, n, src in mixed[:8]), ok=False)
    else:
        srcs = [r[0] for r in con.execute(
            "select distinct source from daily_bars order by source").fetchall()]
        rep.add("C3", "来源唯一性", LEVEL_INFO,
                f"每个标的仅一个 source，全库来源={srcs}")
    mock_rows = con.execute(
        "select count(*) from daily_bars where source='mock'").fetchone()[0]
    if mock_rows:
        rep.add("C3", "mock 污染", LEVEL_ERROR,
                f"daily_bars 含 {mock_rows} 行 source='mock'（运行 scripts/purge_mock_cache.py 清理）",
                ok=False)


def check_gaps(rep: Report, con: sqlite3.Connection, symbols: list[str]) -> None:
    try:
        cal = [r[0] for r in con.execute(
            "select trade_date from trading_calendar order by trade_date").fetchall()]
    except sqlite3.Error:
        rep.add("C4", "缺口检查", LEVEL_WARN, "trading_calendar 不可用，跳过缺口检查", ok=False)
        return
    if not cal:
        rep.add("C4", "缺口检查", LEVEL_WARN, "trading_calendar 为空，跳过缺口检查", ok=False)
        return
    gap_symbols = []
    for sym in symbols:
        mn, mx, n = con.execute(
            "select min(trade_date), max(trade_date), count(*) from daily_bars where symbol=?",
            (sym,)).fetchone()
        if not mn:
            continue
        expect = [d for d in cal if mn <= d <= mx]
        have = {r[0] for r in con.execute(
            "select trade_date from daily_bars where symbol=?", (sym,))}
        miss = [d for d in expect if d not in have]
        if miss:
            gap_symbols.append((sym, len(miss), miss[:5]))
    if gap_symbols:
        detail = "；".join(f"{s} 缺 {n} 天（样例 {','.join(m) if m else '—'}）"
                          for s, n, m in gap_symbols[:8])
        rep.add("C4", "缺口检查", LEVEL_WARN,
                f"{len(gap_symbols)} 只标的缺交易日（多为停牌，需按多源交叉验证确认）：{detail}",
                ok=False)
    else:
        rep.add("C4", "缺口检查", LEVEL_INFO,
                f"{len(symbols)} 只标的在各自区间内与交易日历完全对齐（0 缺口）")


def check_adj_factor(rep: Report, con: sqlite3.Connection) -> None:
    total = con.execute("select count(*) from daily_bars").fetchone()[0]
    nulls = con.execute("select count(*) from daily_bars where adj_factor is null").fetchone()[0]
    if nulls:
        rep.add("C5", "复权因子", LEVEL_WARN,
                f"adj_factor 为空 {nulls}/{total} 行（DQ005：该期间退化为不复权）", ok=False)
    else:
        rep.add("C5", "复权因子", LEVEL_INFO, f"adj_factor 无空值（{total} 行）")


def check_suspension(rep: Report, con: sqlite3.Connection) -> None:
    zero_unmarked = con.execute(
        "select count(*) from daily_bars where volume<=0 and coalesce(is_suspended,0)=0"
    ).fetchone()[0]
    marked_with_vol = con.execute(
        "select count(*) from daily_bars where is_suspended=1 and volume>0").fetchone()[0]
    if zero_unmarked:
        rep.add("C6", "停牌标记", LEVEL_WARN,
                f"{zero_unmarked} 行为零成交量但未标记停牌（DQ007：勿用 0 价格撮合）", ok=False)
    if marked_with_vol:
        rep.add("C6", "停牌标记", LEVEL_WARN,
                f"{marked_with_vol} 行标记停牌但有成交量（标记可能过期）", ok=False)
    if not zero_unmarked and not marked_with_vol:
        suspended = con.execute("select count(*) from daily_bars where is_suspended=1").fetchone()[0]
        rep.add("C6", "停牌标记", LEVEL_INFO,
                f"零成交量/停牌标记一致（偶发停牌行 {suspended} 行）")


def check_dq_issues(rep: Report, con: sqlite3.Connection) -> None:
    """统计 DQ 记录，并**复算**判断哪条已经过期（stale）。"""
    try:
        rows = con.execute(
            "select id, symbol, trade_date, issue_code, severity, created_at, detail "
            "from data_quality_issues order by id").fetchall()
    except sqlite3.Error:
        rep.add("C7", "DQ 记录", LEVEL_INFO, "data_quality_issues 表不可用，跳过")
        return
    if not rows:
        rep.add("C7", "DQ 记录", LEVEL_INFO, "data_quality_issues 为空（无历史问题）")
        return

    stale, active = [], []
    for _id, sym, td, code, sev, created, detail in rows:
        if code == "DQ003":
            pair = con.execute(
                "select trade_date, close from daily_bars where symbol=? and trade_date<=? "
                "order by trade_date desc limit 2", (sym, td)).fetchall()
            if len(pair) == 2 and pair[0][0] == td and pair[1][1]:
                chg = abs(pair[0][1] / pair[1][1] - 1) * 100
                (stale if chg <= 20 else active).append(
                    (sym, td, code, f"当前复算 {chg:.2f}%（阈值 20%）"))
                continue
        if code == "DQ001":  # 缺失 K 线：现在有 bar 说明已补齐
            has = con.execute(
                "select count(*) from daily_bars where symbol=? and trade_date=?",
                (sym, td)).fetchone()[0]
            (stale if has else active).append((sym, td, code, "该日 bar 现存在" if has else "仍缺失"))
            continue
        active.append((sym, td, code, "未做自动复算（非 DQ001/DQ003）"))

    if active:
        rep.add("C7", "DQ 记录", LEVEL_ERROR,
                f"{len(active)} 条 DQ 仍成立："
                + "；".join(f"#{i} {s}@{d} {c}（{why}）" for i, (s, d, c, why) in enumerate(active[:6])),
                ok=False)
    if stale:
        rep.add("C7", "DQ 记录", LEVEL_WARN,
                f"{len(stale)} 条 DQ **已过期（stale）**：数据现无该问题，属历史遗留记录，可清理："
                + "；".join(f"{s}@{d} {c}（{why}）" for s, d, c, why in stale[:6]), ok=False)


def check_symbol_meta(rep: Report, con: sqlite3.Connection) -> None:
    """标的元数据一致性：exchange/board 是否与代码前缀相符（错值会导致涨跌停幅度算错）。"""
    try:
        rows = con.execute(
            "select symbol, exchange, board from symbols").fetchall()
    except sqlite3.Error:
        rep.add("C8", "标的元数据", LEVEL_INFO, "symbols 表不可用，跳过")
        return
    if not rows:
        rep.add("C8", "标的元数据", LEVEL_INFO, "symbols 表为空，跳过")
        return
    bad_exchange, bad_board = [], []
    for sym, exchange, board in rows:
        m = SYMBOL_RE.match(str(sym or ""))
        if not m:
            continue
        code6, suffix = m.group(1), m.group(2)
        expected = board_prefix(code6)
        if not expected:
            continue
        exp_exchange, exp_board = expected
        if exchange and str(exchange).upper() != exp_exchange:
            bad_exchange.append((sym, exchange, exp_exchange))
        if board and str(board).upper() != exp_board:
            bad_board.append((sym, board, exp_board))
    if bad_exchange:
        rep.add("C8", "标的元数据 exchange", LEVEL_WARN,
                f"{len(bad_exchange)}/{len(rows)} 只的 exchange 与代码前缀不符"
                f"（样例 {bad_exchange[:3]}；会导致涨跌停幅度/规费口径判断错误）", ok=False)
    else:
        rep.add("C8", "标的元数据 exchange", LEVEL_INFO, "exchange 与代码前缀一致")
    if bad_board:
        rep.add("C8", "标的元数据 board", LEVEL_WARN,
                f"{len(bad_board)}/{len(rows)} 只的 board 与代码前缀不符"
                f"（样例 {bad_board[:3]}；创业板/科创板应为 GEM/STAR）", ok=False)
    else:
        rep.add("C8", "标的元数据 board", LEVEL_INFO, "board 与代码前缀一致")


def check_calendar_cover(rep: Report, con: sqlite3.Connection) -> None:
    try:
        cmin, cmax = con.execute(
            "select min(trade_date), max(trade_date) from trading_calendar").fetchone()
        bmin, bmax = con.execute(
            "select min(trade_date), max(trade_date) from daily_bars").fetchone()
    except sqlite3.Error:
        return
    if not (cmin and bmin):
        return
    if bmin < cmin or bmax > cmax:
        rep.add("C9", "交易日历覆盖", LEVEL_WARN,
                f"daily_bars 区间 {bmin}~{bmax} 超出交易日历 {cmin}~{cmax}（缺口检查会不准）",
                ok=False)
    else:
        rep.add("C9", "交易日历覆盖", LEVEL_INFO,
                f"交易日历 {cmin}~{cmax} 覆盖 daily_bars 区间 {bmin}~{bmax}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QuantDesk 数据体检（只读，可被 release_check 引用）")
    parser.add_argument("--db", default=None, help="数据库路径（默认按 config.yaml 解析，回退 runtime/quantdesk.db）")
    parser.add_argument("--symbols", default=None, help="只检查指定标的（逗号分隔，如 600519.SH,000001.SZ）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON（供 release_check 解析）")
    parser.add_argument("--strict", action="store_true", help="WARN 也视为失败（退出码 1）")
    args = parser.parse_args(argv)

    symbols = None
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    db_path = resolve_db(args.db)
    rep = Report()
    con = check_db_file(rep, db_path)
    if con is None:
        # 环境错误（库缺失/打不开/损坏）→ 退出码 2，与"数据有 ERROR"（1）区分开
        return finish(rep, args, db_path, force_code=2)

    try:
        found = check_scale(rep, con, symbols)
        if found:
            check_sources(rep, con)
            check_gaps(rep, con, found)
            check_adj_factor(rep, con)
            check_suspension(rep, con)
            check_dq_issues(rep, con)
            check_symbol_meta(rep, con)
            check_calendar_cover(rep, con)
    finally:
        con.close()
    return finish(rep, args, db_path)


def finish(rep: Report, args: argparse.Namespace, db_path: Path,
           force_code: int | None = None) -> int:
    """输出结果并计算退出码（force_code 用于环境错误，优先返回）。"""
    errors = rep.bad(LEVEL_ERROR)
    warns = rep.bad(LEVEL_WARN)
    exit_code = 0
    if force_code is not None:
        exit_code = force_code
    elif errors or (args.strict and warns):
        exit_code = 1

    if args.json:
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "db": str(db_path),
            "result": "FAIL" if exit_code else "PASS",
            "summary": {"error": len(errors), "warn": len(warns),
                        "info": rep.count(LEVEL_INFO), "strict": args.strict},
            "checks": rep.checks,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return exit_code

    print("=" * 72)
    print(f"QuantDesk 数据体检  ·  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"数据库：{db_path}")
    print("=" * 72)
    for item in rep.checks:
        if item["level"] == LEVEL_ERROR:
            mark = "FAIL" if not item["ok"] else " OK "
        elif item["level"] == LEVEL_WARN:
            mark = "WARN"
        else:
            mark = "INFO"
        print(f"[{mark}] {item['code']} {item['title']}：{item['detail']}")
    print("-" * 72)
    verdict = "FAIL" if exit_code else "PASS"
    print(f"结论：{verdict}  ·  ERROR {len(errors)} 个 / WARN {len(warns)} 个 "
          f"/ INFO {rep.count(LEVEL_INFO)} 个")
    if exit_code:
        print("修复建议：")
        if errors:
            print("  · 有 ERROR：按上面 FAIL 项处理；mock 混存先跑 scripts/purge_mock_cache.py")
        if args.strict and warns:
            print("  · strict 模式下 WARN 也算失败：逐条确认上表 WARN 项")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

"""标的名称映射（`data.symbols`）：供策略/推荐/UI 统一查询中文名称。

名称来源优先级：``symbols.name``（标的主数据）→ ``market_snapshot.name``（最近全市场快照）。

常用入口::

    from data.symbols import name_of, names_of, set_name, sync_names_from_snapshot
    name_of("600519.SH")                     # → "贵州茅台"
    names_of(["600519.SH", "000001.SZ"])     # → {"600519.SH": "贵州茅台", ...}
"""

from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from core.db import Database, get_database
from core.logging import get_logger
from core.models import MarketSnapshotORM, SymbolORM, now_local_naive

__all__ = [
    "normalize_symbol",
    "name_of",
    "names_of",
    "set_name",
    "infer_exchange",
    "infer_board",
    "infer_price_limit_pct",
    "infer_lot_size",
    "expected_metadata",
    "audit_symbols_metadata",
    "repair_symbols_metadata",
    "sync_names_from_snapshot",
]

logger = get_logger("data.symbols")

_SYMBOL_RE = re.compile(r"^[0-9]{6}\.(SH|SZ|BJ)$")

#: 板块常量（与 ``symbols.board`` / AStockRules 一致）
BOARD_MAIN = "MAIN"
BOARD_GEM = "GEM"
BOARD_STAR = "STAR"
BOARD_BSE = "BSE"
ALL_BOARDS: tuple[str, ...] = (BOARD_MAIN, BOARD_GEM, BOARD_STAR, BOARD_BSE)

#: 价格限制（%，SPEC §4.3）；ST 仅影响主板
_PRICE_LIMIT_MAIN_ST = 5.0
_PRICE_LIMIT_MAIN = 10.0
_PRICE_LIMIT_GEM_STAR = 20.0
_PRICE_LIMIT_BSE = 30.0


def normalize_symbol(symbol: object) -> str:
    """规范化标的代码为 ``600519.SH``；格式非法抛中文 ``ValueError``。"""
    text = str(symbol or "").strip().upper()
    if not _SYMBOL_RE.match(text):
        raise ValueError(f"标的代码 {symbol!r} 格式非法：应为 6 位数字 + 交易所后缀（如 600519.SH）")
    return text


def names_of(symbols: list[str] | tuple[str, ...], *, database: Database | None = None) -> dict[str, str]:
    """批量查询中文名称：先查 ``symbols``，缺失的用最近 ``market_snapshot`` 补齐。"""
    normalized: list[str] = []
    for item in symbols or []:
        try:
            symbol = normalize_symbol(item)
        except ValueError:
            continue
        if symbol not in normalized:
            normalized.append(symbol)
    if not normalized:
        return {}
    db = database or get_database()
    result: dict[str, str] = {}
    with db.session() as session:
        rows = session.execute(
            select(SymbolORM.symbol, SymbolORM.name).where(SymbolORM.symbol.in_(normalized))
        ).all()
        for symbol, name in rows:
            if name:
                result[symbol] = str(name)
        missing = [symbol for symbol in normalized if symbol not in result]
        if missing:
            snapshot_rows = session.execute(
                select(MarketSnapshotORM.symbol, MarketSnapshotORM.name)
                .where(
                    MarketSnapshotORM.symbol.in_(missing),
                    MarketSnapshotORM.name != "",
                )
                .order_by(MarketSnapshotORM.trade_date.desc())
            ).all()
            for symbol, name in snapshot_rows:
                result.setdefault(symbol, str(name or ""))
    return result


def name_of(symbol: str, *, database: Database | None = None) -> str:
    """查询单个标的中文名称；未知返回空字符串（不抛异常）。"""
    try:
        return names_of([symbol], database=database).get(normalize_symbol(symbol), "")
    except ValueError:
        return ""


def set_name(symbol: str, name: str, *, database: Database | None = None) -> None:
    """写入/更新标的名称到 ``symbols`` 表（不存在则插入最小记录；用于自选池手动补名）。"""
    symbol_text = normalize_symbol(symbol)
    clean_name = str(name or "").strip()
    if not clean_name:
        return
    db = database or get_database()
    now = now_local_naive()
    with db.session() as session:
        statement = sqlite_insert(SymbolORM).values(
            symbol=symbol_text, name=clean_name, updated_at=now
        )
        statement = statement.on_conflict_do_update(
            index_elements=[SymbolORM.symbol],
            set_={"name": clean_name, "updated_at": now},
        )
        session.execute(statement)


def infer_exchange(symbol: str) -> str:
    """**按代码前缀**推断交易所（``SH``/``SZ``/``BJ``）。

    与 ``backtest/broker_sim.py::AStockRules.guess_board`` 同源口径（仅看代码前缀，不读 DB）：
    600/601/603/605/688/689 → SH；000/001/002/003/300/301/399/200 → SZ；
    430/83x/87x/88x/920/4/8 → BJ；900 → SH。
    特例：``000001.SH``/``000300.SH`` 等沪市指数代码以 000 开头但后缀为 SH → 以指数后缀为准。
    """
    text = normalize_symbol(symbol)
    code, suffix = text.split(".", 1)
    if code.startswith("000") and suffix == "SH":
        return "SH"  # 沪市指数（上证指数/沪深300 等）
    if code.startswith("899") and suffix == "BJ":
        return "BJ"  # 北证 50 等北交所指数
    if code.startswith(("600", "601", "603", "605", "688", "689")):
        return "SH"
    if code.startswith(("000", "001", "002", "003", "300", "301", "399", "200")):
        return "SZ"
    if code.startswith(("4", "8", "920")):
        return "BJ"
    if code.startswith("900"):
        return "SH"
    return suffix  # 兜底：以代码后缀为准


def infer_board(symbol: str) -> str:
    """**按代码前缀**推断板块（``MAIN/GEM/STAR/BSE``）。"""
    code, _suffix = normalize_symbol(symbol).split(".", 1)
    exchange = infer_exchange(symbol)
    if exchange == "BJ" or code.startswith(("4", "8", "920")):
        return BOARD_BSE
    if code.startswith(("300", "301")):
        return BOARD_GEM
    if code.startswith(("688", "689")):
        return BOARD_STAR
    return BOARD_MAIN


def infer_price_limit_pct(board: str, is_st: bool) -> float:
    """按板块 + ST 计算涨跌幅限制（%，SPEC §4.3）。"""
    if board == BOARD_BSE:
        return _PRICE_LIMIT_BSE
    if board in (BOARD_GEM, BOARD_STAR):
        return _PRICE_LIMIT_GEM_STAR
    return _PRICE_LIMIT_MAIN_ST if is_st else _PRICE_LIMIT_MAIN


def infer_lot_size(board: str) -> int:
    """按板块计算买入最小数量（科创板 200 股，其余 100 股；SPEC §4.4）。"""
    return 200 if board == BOARD_STAR else 100


def expected_metadata(symbol: str, *, is_st: bool = False) -> dict[str, object]:
    """返回某标的应有的 ``{exchange, board, price_limit_pct, lot_size}``（按代码前缀）。"""
    exchange = infer_exchange(symbol)
    board = infer_board(symbol)
    return {
        "exchange": exchange,
        "board": board,
        "price_limit_pct": infer_price_limit_pct(board, is_st),
        "lot_size": infer_lot_size(board),
    }


def _symbols_need_repair(database: Database | None = None) -> list[dict[str, object]]:
    """全表扫描，返回 exchange/board/price_limit_pct/lot_size 与代码前缀不一致的行。"""
    db = database or get_database()
    mismatches: list[dict[str, object]] = []
    with db.session() as session:
        rows = session.execute(
            select(
                SymbolORM.symbol,
                SymbolORM.exchange,
                SymbolORM.board,
                SymbolORM.price_limit_pct,
                SymbolORM.lot_size,
                SymbolORM.is_st,
            )
        ).all()
        for symbol, exchange, board, price_limit_pct, lot_size, is_st in rows:
            expected = expected_metadata(str(symbol), is_st=bool(is_st))
            actual_limit = None if price_limit_pct is None else round(float(price_limit_pct), 4)
            expected_limit = round(float(expected["price_limit_pct"]), 4)
            wrong_fields = []
            if str(exchange or "").upper() != expected["exchange"]:
                wrong_fields.append("exchange")
            if str(board or "").upper() != expected["board"]:
                wrong_fields.append("board")
            if actual_limit is None or abs(actual_limit - expected_limit) > 1e-6:
                wrong_fields.append("price_limit_pct")
            if int(lot_size or 0) != int(expected["lot_size"]):
                wrong_fields.append("lot_size")
            if wrong_fields:
                mismatches.append(
                    {
                        "symbol": str(symbol),
                        "wrong_fields": wrong_fields,
                        "actual": {
                            "exchange": exchange, "board": board,
                            "price_limit_pct": price_limit_pct, "lot_size": lot_size,
                        },
                        "expected": expected,
                    }
                )
    return mismatches


def audit_symbols_metadata(database: Database | None = None) -> list[dict[str, object]]:
    """审计 ``symbols`` 元数据，返回与代码前缀不一致的标的清单（空列表=全表一致）。"""
    return _symbols_need_repair(database)


def repair_symbols_metadata(database: Database | None = None) -> dict[str, object]:
    """按**代码前缀**批量修正 ``symbols`` 的 exchange/board/price_limit_pct/lot_size（幂等）。

    :return: 修正报告（修正条数、按字段统计、抽样、剩余不一致数），可直接打印/落审计。
    """
    from sqlalchemy import bindparam, update

    db = database or get_database()
    mismatches = _symbols_need_repair(db)
    if not mismatches:
        return {
            "checked": 0, "mismatched": 0, "updated": 0,
            "fixed": {"exchange": 0, "board": 0, "price_limit_pct": 0, "lot_size": 0},
            "samples": [], "remaining_mismatch": 0,
        }
    fixed = {"exchange": 0, "board": 0, "price_limit_pct": 0, "lot_size": 0}
    payloads: list[dict[str, object]] = []
    samples: list[dict[str, object]] = []
    now = now_local_naive()
    for item in mismatches:
        for field_name in item["wrong_fields"]:
            fixed[field_name] += 1
        expected = item["expected"]
        payloads.append(
            {
                "b_symbol": item["symbol"],
                "b_exchange": expected["exchange"],
                "b_board": expected["board"],
                "b_limit": expected["price_limit_pct"],
                "b_lot": expected["lot_size"],
                "b_now": now,
            }
        )
        if len(samples) < 5:
            samples.append(
                {"symbol": item["symbol"], "actual": item["actual"], "expected": expected}
            )
    table = SymbolORM.__table__
    statement = (
        update(table)
        .where(table.c.symbol == bindparam("b_symbol"))
        .values(
            exchange=bindparam("b_exchange"),
            board=bindparam("b_board"),
            price_limit_pct=bindparam("b_limit"),
            lot_size=bindparam("b_lot"),
            updated_at=bindparam("b_now"),
        )
    )
    with db.session() as session:
        session.execute(statement, payloads)
    remaining = len(_symbols_need_repair(db))
    report = {
        "checked": len(mismatches),
        "mismatched": len(mismatches),
        "updated": len(payloads),
        "fixed": fixed,
        "samples": samples,
        "remaining_mismatch": remaining,
    }
    logger.info(
        "symbols 元数据修复完成：修正 %s 条（exchange=%s / board=%s / price_limit=%s / lot_size=%s），剩余不一致=%s",
        report["updated"], fixed["exchange"], fixed["board"], fixed["price_limit_pct"],
        fixed["lot_size"], remaining,
    )
    return report


def sync_names_from_snapshot(
    *, database: Database | None = None, trade_date: object | None = None,
    repair_metadata: bool = True,
) -> int:
    """把 ``market_snapshot`` 的名称同步到 ``symbols``（幂等），并顺带修正元数据。

    :param repair_metadata: 同步后是否调用 :func:`repair_symbols_metadata`（默认 True；
                            exchange/board 以代码前缀为准，快照不参与推断）
    :return: 同步的名称条数
    """
    db = database or get_database()
    with db.session() as session:
        statement = select(MarketSnapshotORM.symbol, MarketSnapshotORM.name).where(
            MarketSnapshotORM.name != ""
        )
        if trade_date is not None:
            statement = statement.where(MarketSnapshotORM.trade_date == trade_date)
        rows = session.execute(statement).all()
        now = now_local_naive()
        synced = 0
        for symbol, name in rows:
            try:
                symbol_text = normalize_symbol(symbol)
            except ValueError:
                continue
            insert_stmt = sqlite_insert(SymbolORM).values(
                symbol=symbol_text, name=str(name), updated_at=now
            )
            session.execute(
                insert_stmt.on_conflict_do_update(
                    index_elements=[SymbolORM.symbol],
                    set_={"name": str(name), "updated_at": now},
                )
            )
            synced += 1
    logger.info("名称映射同步完成：%s 条（来自 market_snapshot）", synced)
    if repair_metadata:
        try:
            repair_symbols_metadata(db)
        except Exception as exc:  # noqa: BLE001 - 元数据修正失败不影响名称同步
            logger.warning("名称同步后的 symbols 元数据修正失败：%s", exc)
    return synced


# ---------------------------------------------------------------------------
# 自检（quantdesk 目录：``python -m data.symbols``）
# ---------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    from datetime import date

    from sqlalchemy import update

    from core.config import load_config, reset_config, set_config
    from core.db import Database

    cfg = load_config(environ={})
    set_config(cfg)
    database = Database(url="sqlite://", config=cfg)
    try:
        database.initialize()
        # 1) 构造一批错值（历史数据：全 SH/MAIN）+ 一条正确记录
        with database.session() as session:
            session.add_all(
                [
                    SymbolORM(symbol="600519.SH", name="贵州茅台", exchange="SZ", board="GEM",
                              price_limit_pct=20.0, lot_size=100),
                    SymbolORM(symbol="000001.SZ", name="平安银行", exchange="SH", board="MAIN",
                              price_limit_pct=None, lot_size=100),
                    SymbolORM(symbol="300750.SZ", name="宁德时代", exchange="SH", board="MAIN",
                              price_limit_pct=10.0, lot_size=100),
                    SymbolORM(symbol="688111.SH", name="金山办公", exchange="SZ", board="MAIN",
                              price_limit_pct=10.0, lot_size=100),
                    SymbolORM(symbol="899050.BJ", name="北证50", exchange="SH", board="MAIN",
                              price_limit_pct=10.0, lot_size=100),
                    SymbolORM(symbol="600036.SH", name="招商银行", exchange="SH", board="MAIN",
                              price_limit_pct=10.0, lot_size=100),
                ]
            )
        mismatches = audit_symbols_metadata(database)
        assert len(mismatches) == 5, mismatches
        report = repair_symbols_metadata(database)
        assert report["updated"] == 5 and report["remaining_mismatch"] == 0, report
        # 2) 抽样校验
        expected_samples = {
            "600519.SH": ("SH", "MAIN", 10.0, 100),
            "000001.SZ": ("SZ", "MAIN", 10.0, 100),
            "300750.SZ": ("SZ", "GEM", 20.0, 100),
            "688111.SH": ("SH", "STAR", 20.0, 200),
            "899050.BJ": ("BJ", "BSE", 30.0, 100),
            "600036.SH": ("SH", "MAIN", 10.0, 100),
        }
        with database.session() as session:
            for symbol, (exchange, board, limit_pct, lot_size) in expected_samples.items():
                row = session.execute(
                    select(
                        SymbolORM.exchange, SymbolORM.board,
                        SymbolORM.price_limit_pct, SymbolORM.lot_size,
                    ).where(SymbolORM.symbol == symbol)
                ).first()
                assert row is not None
                assert (row[0], row[1], round(float(row[2]), 2), int(row[3])) == (
                    exchange, board, limit_pct, lot_size
                ), (symbol, row)
        # 3) 全表扫描 100% 一致 + 幂等（再次修复 0 条）
        assert audit_symbols_metadata(database) == []
        second = repair_symbols_metadata(database)
        assert second["updated"] == 0 and second["remaining_mismatch"] == 0
        # 4) 名称同步顺带修正元数据
        with database.session() as session:
            session.add(
                MarketSnapshotORM(
                    trade_date=date(2026, 10, 26), symbol="600519.SH",
                    name="贵州茅台", price=1400.0, source="sina",
                )
            )
            session.execute(update(SymbolORM).where(SymbolORM.symbol == "600519.SH").values(
                exchange="SZ", board="GEM"))
        assert len(audit_symbols_metadata(database)) == 1
        synced = sync_names_from_snapshot(database=database)
        assert synced >= 1
        with database.session() as session:
            row = session.execute(
                select(SymbolORM.name, SymbolORM.exchange, SymbolORM.board)
                .where(SymbolORM.symbol == "600519.SH")
            ).first()
        assert row[0] == "贵州茅台" and row[1] == "SH" and row[2] == "MAIN", row
        assert audit_symbols_metadata(database) == []
        print(
            "data/symbols.py 自检通过：按代码前缀修正 exchange/board/price_limit/lot_size（"
            f"修正 {report['updated']} 条：{report['fixed']}）/"
            "抽样 6 只全部正确/全表扫描一致率 100%/二次修复幂等/名称同步顺带修复"
        )
    finally:
        database.dispose()
        reset_config()

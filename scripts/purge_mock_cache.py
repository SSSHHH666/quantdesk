r"""QuantDesk 一键清理 mock 污染行情数据（P0 数据完整性修复）。

背景
----
历史版本在 akshare 失败时会用 ``MockProvider`` 假数据兜底并写入 ``daily_bars``，
导致 mock 假价格（约 100 元）与真实价格（约 1400 元）混存；回测跨区间时会拼出
**虚假收益**（曾出现 246% 假收益）。现在 ``BarCache.update`` 已从代码上拒绝写入 mock 数据
（``UpdateReport.mock_blocked``），``BarCache.load`` 对混源数据 fail-closed 报错；
本脚本用于**一键清理历史污染**，可重复执行（幂等）。

清理范围
--------
- ``daily_bars``：``source`` 含 mock 的全部行；
- ``adjust_events``：``source`` 含 mock 的全部行（mock 分红事件同样会污染复权/结算）。
- 不删除 ``data_quality_issues`` / 审计等其它表。

用法
----
::

    .venv\Scripts\python.exe scripts\purge_mock_cache.py            # 清理（幂等）
    .venv\Scripts\python.exe scripts\purge_mock_cache.py --dry-run  # 只统计不删除
    .venv\Scripts\python.exe scripts\purge_mock_cache.py --config other.yaml

退出码：0 成功/无需清理，1 数据库错误，2 配置错误。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import delete, func, select  # noqa: E402

from core.config import ConfigError, load_config  # noqa: E402
from core.db import Database  # noqa: E402
from core.logging import get_logger, setup_logging  # noqa: E402
from core.models import AdjustEventORM, DailyBarORM  # noqa: E402

logger = get_logger("scripts.purge_mock_cache")


def _mock_condition(column):
    """``source`` 含 mock（大小写不敏感）的过滤条件。"""
    return func.lower(func.coalesce(column, "")).contains("mock")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="清理 daily_bars / adjust_events 中的 mock 污染数据（幂等）"
    )
    parser.add_argument("--dry-run", action="store_true", help="只统计将删除的行数，不修改数据库")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 quantdesk/config.yaml）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"[错误] 配置加载失败：\n{exc}")
        return 2

    db_path = config.sqlite_path
    if db_path is None:
        print("[错误] 当前配置为内存数据库/非 SQLite，无可清理文件")
        return 1
    if not Path(db_path).exists():
        print(f"数据库不存在，无需清理：{db_path}")
        return 0

    setup_logging(config)
    database = Database(config=config)
    try:
        tables = set(database.table_names())
        if DailyBarORM.__tablename__ not in tables:
            print(f"数据库尚未建表（缺少 {DailyBarORM.__tablename__}），无需清理：{db_path}")
            return 0

        with database.session() as session:
            daily_count = int(
                session.execute(
                    select(func.count()).select_from(DailyBarORM).where(_mock_condition(DailyBarORM.source))
                ).scalar()
                or 0
            )
            adjust_count = 0
            if "adjust_events" in tables:
                adjust_count = int(
                    session.execute(
                        select(func.count()).select_from(AdjustEventORM).where(_mock_condition(AdjustEventORM.source))
                    ).scalar()
                    or 0
                )

        print("=" * 68)
        print("QuantDesk mock 污染数据清理")
        print("=" * 68)
        print(f"数据库        : {db_path}")
        print(f"daily_bars    : 待清理 {daily_count} 行（source 含 mock）")
        print(f"adjust_events : 待清理 {adjust_count} 行（source 含 mock）")
        if args.dry_run:
            print("--dry-run：未修改数据库")
            print("=" * 68)
            return 0
        if daily_count == 0 and adjust_count == 0:
            print("未发现 mock 污染数据（幂等，无需处理）")
            print("=" * 68)
            return 0

        deleted_daily = 0
        deleted_adjust = 0
        with database.session() as session:
            result = session.execute(delete(DailyBarORM).where(_mock_condition(DailyBarORM.source)))
            deleted_daily = max(int(result.rowcount or 0), 0)
            if "adjust_events" in tables:
                result = session.execute(
                    delete(AdjustEventORM).where(_mock_condition(AdjustEventORM.source))
                )
                deleted_adjust = max(int(result.rowcount or 0), 0)

        with database.session() as session:
            source_rows = session.execute(
                select(DailyBarORM.source, func.count()).group_by(DailyBarORM.source).order_by(DailyBarORM.source)
            ).all()
        print(f"已删除 daily_bars {deleted_daily} 行、adjust_events {deleted_adjust} 行")
        print("清理后 daily_bars 数据源分布：")
        if source_rows:
            for source, count in source_rows:
                print(f"  - {source or '(空)'}: {count} 行")
        else:
            print("  - （无数据）")
        print("提示：清理后请重新执行 scripts/fetch_data.py 回填真实数据；")
        print("      BarCache.load() 已对混源数据 fail-closed，回测不会再用到 mock 假数据。")
        print("=" * 68)
        logger.info("mock 污染清理完成：daily_bars %s 行、adjust_events %s 行", deleted_daily, deleted_adjust)
        return 0
    except Exception as exc:  # noqa: BLE001 - 数据库损坏/锁定等给出中文提示
        print(f"[错误] 清理失败：{type(exc).__name__}: {exc}")
        logger.error("mock 污染清理失败：%s", exc, exc_info=True)
        return 1
    finally:
        database.dispose()


if __name__ == "__main__":  # pragma: no cover - 脚本入口
    sys.exit(main())

r"""QuantDesk 数据初始化脚本（任务 M1-04）。

作用
----
按 ``config.yaml`` 的自选池（``data.universe``）或数据库自选池表（``watchlist_items``）
与历史年数拉取 A 股日线并落库到 SQLite，输出**进度**与**数据质量报告**（DQ001–DQ007）。

用法（在 quantdesk 目录或任意目录均可执行）::

    .venv\Scripts\python.exe scripts\fetch_data.py                    # 按配置默认自选池拉取
    .venv\Scripts\python.exe scripts\fetch_data.py --watchlist        # 按数据库自选池表拉取
    .venv\Scripts\python.exe scripts\fetch_data.py --watchlist --group long_term
    .venv\Scripts\python.exe scripts\fetch_data.py --symbols 600519.SH --years 1
    .venv\Scripts\python.exe scripts\fetch_data.py --dry-run          # 只看计划，不联网、不写库

参数
----
- ``--symbols 600519.SH,000001.SZ``：覆盖标的列表（逗号分隔，优先级最高）
- ``--watchlist``：按数据库自选池（``watchlist_items`` 表）拉取；表为空时自动用配置自选池初始化
- ``--group long_term|short_term|default``：配合 ``--watchlist`` 只拉某个分组
- ``--years N``：覆盖历史年数（默认取配置）
- ``--dry-run``：只打印将要拉取的标的/区间/数据源/数据库路径，不联网、不写库
- ``--config PATH``：指定配置文件路径（默认 ``quantdesk/config.yaml``）

行为说明
--------
- **数据源主备链**：新浪（主）→ 腾讯（备）→ Mock（兜底）；东财行情接口在当前网络不可用，
  详见 ``docs/DATA_SOURCES.md``；
- **断网自动降级**：akshare 不可用时自动使用 MockProvider 离线演示数据并给出警告，不崩溃；
- **缓存优先/增量**：复用 :class:`data.cache.BarCache`，已缓存区间不重复拉取，重复运行幂等
  （第二次运行 ``新增 0``，只跳过已存在数据）；
- **数据质量报告**：拉取后用 ``BarCache.validate`` 检测 DQ001–DQ007 并写入 ``data_quality_issues``，
  脚本打印中文汇总与建议动作；
- 退出码：0 成功（含幂等无新增）、1 数据库/全部标的失败、2 参数或配置错误。

只读脚本：仅写 ``runtime/quantdesk.db`` 等运行期数据，不修改任何源码文件。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

# 支持从任意工作目录直接运行：把项目根目录加入 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import delete, select  # noqa: E402

from core.config import ConfigError, load_config  # noqa: E402
from core.db import Database, init_db  # noqa: E402
from core.logging import AuditAction, get_logger, setup_logging, write_audit  # noqa: E402
from core.models import TABLE_NAMES, DataQualityIssueORM, now_local  # noqa: E402
from data.cache import BarCache  # noqa: E402
from data.provider import (  # noqa: E402
    ProviderError,
    build_provider,
    get_registry,
    normalize_symbols,
)

logger = get_logger("scripts.fetch_data")

#: 数据质量码中文名称（报告用）
_DQ_NAMES = {
    "DQ001": "缺失 K 线",
    "DQ002": "重复 K 线",
    "DQ003": "价格异常跳变",
    "DQ004": "零成交量",
    "DQ005": "复权因子缺失",
    "DQ006": "数据过期",
    "DQ007": "停牌未标记",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="QuantDesk 数据初始化：按自选池拉取日线并落库，输出进度与数据质量报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python scripts/fetch_data.py\n"
            "  python scripts/fetch_data.py --symbols 600519.SH --years 1\n"
            "  python scripts/fetch_data.py --dry-run\n"
        ),
    )
    parser.add_argument("--symbols", default=None, help="覆盖标的列表，逗号分隔（如 600519.SH,000001.SZ）")
    parser.add_argument("--watchlist", action="store_true", help="按数据库自选池表（watchlist_items）拉取")
    parser.add_argument("--group", default=None, help="配合 --watchlist：只拉指定分组（default/long_term/short_term）")
    parser.add_argument("--years", type=int, default=None, help="覆盖历史年数（默认取 config.data.history_years）")
    parser.add_argument("--dry-run", action="store_true", help="只展示计划，不联网、不写库")
    parser.add_argument(
        "--market-snapshot", action="store_true",
        help="采集全市场快照并落库（market_snapshot 表，供异动雷达/名称映射；失败降级上次快照）",
    )
    parser.add_argument(
        "--repair-symbols", action="store_true",
        help="按代码前缀批量修正 symbols 的 exchange/board/price_limit_pct/lot_size（幂等）",
    )
    parser.add_argument(
        "--purge-stale-dq", action="store_true",
        help="清理 mock 事故历史残留的 stale DQ003（600519.SH 2025-01-02），并写审计",
    )
    parser.add_argument("--config", default=None, help="配置文件路径（默认 quantdesk/config.yaml）")
    return parser


def _watchlist_symbols(
    config, group: str | None, *, allow_init: bool, read_only: bool = False
) -> tuple[list[str], str]:
    """从数据库自选池读取标的（可选分组）；为空时可选择用配置自选池初始化。

    :param read_only: ``--dry-run`` 用：只读检查，不自选池表不存在时不建表、不写库
    :return: （标的列表, 来源说明）
    """
    from data.watchlist import WatchlistService

    if read_only:
        db_path = getattr(config, "sqlite_path", None)
        if db_path is None or not Path(db_path).exists():
            return [], "数据库自选池（数据库尚未初始化）"
        try:
            from sqlalchemy import inspect

            from core.db import Database
            from core.models import WatchlistItemORM

            table_name = WatchlistItemORM.__tablename__
            if table_name not in inspect(Database(config=config).engine).get_table_names():
                return [], "数据库自选池（自选池表尚未创建）"
        except Exception as exc:  # noqa: BLE001 - dry-run 下读取失败不阻塞计划展示
            logger.debug("dry-run 读取自选池表失败：%s", exc)
            return [], "数据库自选池（读取失败）"

    service = WatchlistService(config=config)
    if allow_init and service.count() == 0:
        initialized = service.init_from_universe(config)
        if initialized:
            print(f"提示：自选池为空，已用配置默认自选池 data.universe 初始化 {initialized} 只标的")
    items = service.list_items(group) if group else service.list_items()
    source = f"数据库自选池（watchlist_items{', 分组=' + group if group else ''}）"
    return [item.symbol for item in items], source


def _resolve_symbols(
    config,
    symbols_arg: str | None,
    *,
    use_watchlist: bool,
    group: str | None,
    allow_init: bool,
    read_only: bool = False,
) -> tuple[list[str], str]:
    """确定要拉取的标的列表与来源说明（优先级：--symbols > --watchlist/--group > 配置自选池）。"""
    if symbols_arg:
        raw = [item for item in str(symbols_arg).replace("，", ",").split(",") if item.strip()]
        symbols = normalize_symbols(raw)
        source = "命令行 --symbols"
    elif use_watchlist or group:
        symbols, source = _watchlist_symbols(
            config, group, allow_init=allow_init, read_only=read_only
        )
        if not symbols:
            print(f"提示：{source}，回退使用配置默认自选池 data.universe")
            symbols, source = normalize_symbols(config.data.universe), "配置默认自选池 data.universe（自选池为空回退）"
    else:
        symbols, source = normalize_symbols(config.data.universe), "配置默认自选池 data.universe"
    if not symbols:
        raise ValueError(
            "标的列表为空：请在 config.yaml 配置 data.universe、维护数据库自选池，或用 --symbols 指定（如 600519.SH）"
        )
    return symbols, source


def _print_plan(
    config,
    symbols: list[str],
    start: date,
    end: date,
    database_path: str,
    symbols_source: str,
    years: int,
) -> None:
    """打印拉取计划（--dry-run）。"""
    registry = get_registry()
    providers = registry.list_providers()
    print("=" * 72)
    print("QuantDesk 数据初始化计划（--dry-run：不联网、不写库）")
    print("=" * 72)
    print(f"配置文件      : {config.source_path or '(默认)'}")
    print(f"主数据源      : {config.data.provider}（降级链：{', '.join(config.data.fallbacks) or '无'}）")
    print(f"已注册数据源  : {', '.join(providers) or '无'}")
    print(f"akshare 可用  : {'是' if registry.is_registered('akshare') else '否（将降级 mock 离线数据）'}")
    print(f"数据库路径    : {database_path}")
    print(f"标的来源      : {symbols_source}")
    print(f"标的数量      : {len(symbols)} 只")
    print(f"标的列表      : {', '.join(symbols)}")
    print(f"拉取区间      : {start} ~ {end}（历史 {years} 年）")
    print(f"复权口径      : 落库为不复权价 + adj_factor（{config.data.adjust} 用于展示/回测换算）")
    print(f"请求节流      : {config.data.request_interval_ms} ms/次，最多重试 {config.data.max_retries} 次")
    print("-" * 72)
    print("执行后将：1) 建库建表；2) 增量拉取日线并 upsert（幂等）；3) 输出数据质量报告。")
    print("=" * 72)


def _print_validation_report(validation) -> None:
    """打印数据质量报告（中文）。"""
    print("-" * 72)
    if not validation.issues:
        print(f"数据质量报告：未发现 DQ001–DQ007 问题（run_id={validation.run_id}）")
        print("-" * 72)
        return
    print(
        f"数据质量报告：共 {validation.total_issues} 个问题"
        f"（run_id={validation.run_id}，已写入 data_quality_issues 表）"
    )
    for code in sorted(validation.counts):
        count = validation.counts[code]
        severity = "ERROR" if any(
            issue.issue_code == code and issue.severity.value == "ERROR" for issue in validation.issues
        ) else "WARN"
        suggestion = next(
            (issue.suggestion for issue in validation.issues if issue.issue_code == code), ""
        )
        print(f"  {code} {_DQ_NAMES.get(code, '')} [{severity}] × {count}：{suggestion}")
    print("  问题明细（最多显示 10 条）：")
    for issue in validation.issues[:10]:
        trade_date = issue.trade_date or "-"
        print(f"    {trade_date} {issue.symbol} {issue.issue_code}：{issue.detail}")
    if validation.total_issues > 10:
        print(f"    …… 其余 {validation.total_issues - 10} 条请在「日志页 → 数据校验报告」查看")
    print("-" * 72)


def _purge_stale_dq(config) -> int:
    """清理 mock 事故历史残留的 stale DQ003（600519.SH 2025-01-02），写审计并返回删除条数。"""
    database = Database(config=config)
    try:
        with database.session() as session:
            rows = session.execute(
                select(DataQualityIssueORM).where(
                    DataQualityIssueORM.issue_code == "DQ003",
                    DataQualityIssueORM.symbol == "600519.SH",
                    DataQualityIssueORM.trade_date == date(2025, 1, 2),
                )
            ).scalars().all()
            if not rows:
                return 0
            snapshots = [
                {
                    "id": row.id,
                    "run_id": row.run_id,
                    "symbol": row.symbol,
                    "trade_date": str(row.trade_date),
                    "issue_code": row.issue_code,
                    "severity": row.severity,
                    "detail": row.detail,
                }
                for row in rows
            ]
            ids = [row.id for row in rows]
            session.execute(delete(DataQualityIssueORM).where(DataQualityIssueORM.id.in_(ids)))
        for snapshot in snapshots:
            write_audit(
                AuditAction.DATA_QUALITY_PURGE,
                ("data_quality_issues", str(snapshot["id"])),
                before=snapshot,
                after={
                    "deleted": True,
                    "reason": "mock 事故历史残留；按当前真实数据复算 2025-01-02 涨幅 2.36% < 20% 阈值",
                },
                actor="manual",
            )
        logger.info("stale DQ003 清理完成：删除 %s 条（已写审计）", len(snapshots))
        return len(snapshots)
    finally:
        database.dispose()


def _write_coverage_report(
    config, coverage_rows: list[dict], start: date, end: date, provider_name: str
) -> Path:
    """生成数据覆盖报告并写入 ``<data_dir>/exports/data_coverage.md``（返回路径）。"""
    lines = [
        "# QuantDesk 数据覆盖报告",
        "",
        f"- 生成时间：{now_local().isoformat(timespec='seconds')}",
        f"- 请求区间：{start} ~ {end}",
        f"- 数据源：{provider_name}",
        "",
        "| 标的 | 首日 | 末日 | 行数 | 应有交易日 | 缺口天数 | 数据源 |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in coverage_rows:
        lines.append(
            f"| {row['symbol']} | {row['first_date'] or '-'} | {row['last_date'] or '-'} | "
            f"{row['rows']} | {row['expected_days']} | {row['missing_days']} | "
            f"{', '.join(row['sources']) or '-'} |"
        )
    # 一致性检查：起始日是否一致 / 是否混源 / 是否有缺口
    starts = {row["first_date"] for row in coverage_rows if row["first_date"]}
    warnings: list[str] = []
    if len(starts) > 1:
        warnings.append(f"各标的起始日不一致：{sorted(str(day) for day in starts)}")
    mixed = [row["symbol"] for row in coverage_rows if len(row["sources"]) > 1]
    if mixed:
        warnings.append(f"检测到混源标的：{', '.join(mixed)}（请运行 scripts/purge_mock_cache.py 清理并重拉）")
    gaps = [f"{row['symbol']}({row['missing_days']} 天)" for row in coverage_rows if row["missing_days"] > 0]
    if gaps:
        warnings.append(f"存在缺口：{', '.join(gaps)}")
    mock_sources = [row["symbol"] for row in coverage_rows if any("mock" in source.lower() for source in row["sources"])]
    if mock_sources:
        warnings.append(f"检测到 mock 来源：{', '.join(mock_sources)}（mock 禁止进入回测/缓存）")
    lines.append("")
    if warnings:
        lines.append("## 一致性警告")
        lines.append("")
        for warning in warnings:
            lines.append(f"- ⚠️ {warning}")
    else:
        lines.append("## 一致性检查")
        lines.append("")
        lines.append("- ✅ 各标的起始日一致、无缺口、无 mock 混源")
    lines.append("")
    target = config.export_dir / "data_coverage.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    """脚本入口；返回退出码（0 成功 / 1 失败 / 2 参数或配置错误）。"""
    args = _build_parser().parse_args(argv)

    # 1) 加载配置
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"[错误] 配置加载失败：\n{exc}")
        return 2

    # 2) 解析参数与日期区间
    try:
        years = int(args.years if args.years is not None else config.data.history_years)
        if not 1 <= years <= 30:
            raise ValueError(f"--years 必须在 1~30 之间（当前 {years}）")
    except (ValueError, TypeError) as exc:
        print(f"[错误] 参数非法：{exc}")
        return 2
    end_date = now_local().date()
    start_date = end_date - timedelta(days=int(years * 365.25))
    database_path = str(config.sqlite_path or ":memory:")

    # 3) 计划预览（--dry-run：不联网、不写库；自选池只读检查）
    if args.dry_run:
        try:
            symbols, symbols_source = _resolve_symbols(
                config, args.symbols, use_watchlist=args.watchlist, group=args.group,
                allow_init=False, read_only=True,
            )
        except ValueError as exc:
            print(f"[错误] 参数非法：{exc}")
            return 2
        _print_plan(config, symbols, start_date, end_date, database_path, symbols_source, years)
        return 0

    # 4) 初始化日志与数据库
    setup_logging(config)
    print("=" * 72)
    print("QuantDesk 数据初始化")
    print("=" * 72)
    print(f"数据库路径：{database_path}")
    print("-" * 72)
    health = init_db(config)
    if not health.ok:
        print(f"[错误] 数据库初始化/自检失败：{health.message}")
        return 1
    business_tables = len([name for name in health.tables if name in TABLE_NAMES])
    print(f"数据库自检通过：结构版本 v{health.schema_version}，核心业务表 {business_tables} 张")

    # 4.1) 管理类操作：symbols 元数据修复 / stale DQ 清理（可单独执行）
    if args.repair_symbols:
        print("-" * 72)
        print("正在按代码前缀修复 symbols 元数据（exchange/board/price_limit_pct/lot_size）……")
        from data.symbols import repair_symbols_metadata

        repair = repair_symbols_metadata()
        print(
            f"symbols 元数据修复：修正 {repair['updated']} 条（{repair['fixed']}），"
            f"剩余不一致 {repair['remaining_mismatch']}"
        )
        for sample in repair["samples"][:3]:
            print(f"  示例 {sample['symbol']}：{sample['actual']} → {sample['expected']}")
    if args.purge_stale_dq:
        deleted = _purge_stale_dq(config)
        print(f"stale DQ003 清理：删除 {deleted} 条（已写审计 data.quality.purge）")
    if (args.repair_symbols or args.purge_stale_dq) and not (
        args.watchlist or args.group or args.symbols or args.market_snapshot
    ):
        print("=" * 72)
        return 0

    # 4.2) 全市场快照采集（可选：异动雷达/名称映射前置；失败降级上次快照，不阻塞日线）
    snapshot_report = None
    if args.market_snapshot:
        print("-" * 72)
        print("正在采集全市场快照（新浪 stock_zh_a_spot，约 20 秒）……")
        try:
            from data.market_snapshot import MarketSnapshotService

            snapshot_report = MarketSnapshotService(config=config).collect_daily()
            print(f"全市场快照：{snapshot_report.message}")
        except Exception as exc:  # noqa: BLE001 - 快照失败不阻塞日线拉取
            logger.warning("全市场快照采集失败（不影响日线拉取）：%s", exc)
            print(f"[警告] 全市场快照采集失败（不影响日线拉取）：{exc}")
        print("-" * 72)

    # 4.5) 解析标的（--watchlist 需要数据库，故放在建库之后；自选池为空时用配置自选池初始化）
    try:
        symbols, symbols_source = _resolve_symbols(
            config, args.symbols, use_watchlist=args.watchlist, group=args.group, allow_init=True,
        )
    except ValueError as exc:
        print(f"[错误] 标的解析失败：{exc}")
        return 2
    print(f"标的来源：{symbols_source}")
    print(f"标的 {len(symbols)} 只，区间 {start_date} ~ {end_date}（{years} 年）")
    print("-" * 72)

    # 5) 组装数据源（主备 + 降级）
    try:
        provider = build_provider(config)
    except ProviderError as exc:
        print(f"[错误] 数据源初始化失败：{exc}")
        return 1
    if provider.name == "mock":
        print("")
        print("*" * 72)
        print("警告：当前使用 MockProvider「离线演示模式」数据（akshare 不可用或未配置）。")
        print("      写入的是示例行情，仅用于演示/测试，不是真实 A 股数据。")
        print("*" * 72)
    else:
        print(f"数据源：{provider.name}（真实 A 股行情）")
    print("-" * 72)

    # 6) 逐标的拉取（复用 BarCache：缓存优先 + 增量 + upsert 幂等）
    cache = BarCache(provider=provider, config=config)
    # 先同步交易日历：用于增量判断「区间内是否有交易日」及后续 T+1/校验（失败不阻塞）
    try:
        calendar_added = cache.sync_trading_calendar(start_date, end_date)
        print(f"交易日历：新增 {calendar_added} 天（{start_date} ~ {end_date}）")
    except Exception as exc:  # noqa: BLE001 - 日历失败不影响行情
        logger.warning("交易日历同步失败（不影响行情拉取）：%s", exc)
    print("-" * 72)
    totals = {"added": 0, "updated": 0, "skipped": 0, "adjust_events": 0}
    failures: list[tuple[str, str]] = []
    started = time.perf_counter()
    for index, symbol in enumerate(symbols, start=1):
        percent = int(index / len(symbols) * 100)
        symbol_started = time.perf_counter()
        try:
            report = cache.update([symbol], start_date, end_date)
        except Exception as exc:  # noqa: BLE001 - 单标的失败不影响其它标的
            failures.append((symbol, str(exc)))
            logger.warning("标的 %s 拉取失败：%s", symbol, exc)
            print(f"[{index}/{len(symbols)} {percent:3d}%] {symbol} 失败：{exc}")
            continue
        totals["added"] += report.added
        totals["updated"] += report.updated
        totals["skipped"] += report.skipped
        totals["adjust_events"] += report.adjust_events_added
        elapsed_symbol = time.perf_counter() - symbol_started
        print(
            f"[{index}/{len(symbols)} {percent:3d}%] {symbol}："
            f"新增 {report.added}、更新 {report.updated}、跳过 {report.skipped}、"
            f"除权事件 +{report.adjust_events_added}（{elapsed_symbol:.1f}s）"
        )
    elapsed = time.perf_counter() - started

    # 7) 数据质量校验报告
    print("-" * 72)
    print("正在进行数据质量校验（DQ001–DQ007）……")
    try:
        validation = cache.validate(symbols, start_date, end_date)
        _print_validation_report(validation)
    except Exception as exc:  # noqa: BLE001 - 校验失败不阻塞主流程
        logger.error("数据质量校验失败：%s", exc, exc_info=True)
        print(f"[警告] 数据质量校验失败（不影响已落库数据）：{exc}")

    # 7.5) 数据覆盖报告（每标的首日/末日/行数/缺口/来源；写入 <data_dir>/exports/data_coverage.md）
    try:
        coverage_rows = cache.coverage_report(symbols, start_date, end_date)
        coverage_path = _write_coverage_report(
            config, coverage_rows, start_date, end_date, provider.name
        )
        print(f"数据覆盖报告：{coverage_path}")
        for row in coverage_rows:
            first = row["first_date"] or "-"
            last = row["last_date"] or "-"
            sources = ",".join(row["sources"]) or "(空)"
            print(
                f"  {row['symbol']}：{first} ~ {last}，{row['rows']} 行，"
                f"缺口 {row['missing_days']} 天，来源 {sources}"
            )
    except Exception as exc:  # noqa: BLE001 - 覆盖报告失败不阻塞主流程
        logger.warning("数据覆盖报告生成失败：%s", exc)
        print(f"[警告] 数据覆盖报告生成失败：{exc}")

    # 8) 汇总
    print("=" * 72)
    print("完成汇总")
    print("-" * 72)
    print(f"数据源      : {provider.name}")
    print(f"数据库      : {database_path}")
    print(f"标的来源    : {symbols_source}（{len(symbols)} 只）")
    if snapshot_report is not None:
        print(
            f"全市场快照  : {snapshot_report.rows} 只（落库 {snapshot_report.saved} 行，"
            f"{'正常' if snapshot_report.ok else '降级：' + snapshot_report.message}）"
        )
    print(
        f"行情行数    : 新增 {totals['added']}、更新 {totals['updated']}、跳过 {totals['skipped']}"
    )
    print(f"除权事件    : 新增 {totals['adjust_events']}")
    if failures:
        print(f"失败标的    : {len(failures)} 只 → {', '.join(symbol for symbol, _ in failures)}")
    else:
        print("失败标的    : 无")
    print(f"耗时        : {elapsed:.1f} 秒")
    if totals["added"] == 0 and totals["updated"] == 0 and not failures:
        print("提示        : 本次无新增数据（重复运行幂等；如需重拉请先清理对应区间）")
    if failures and totals["added"] == 0 and totals["updated"] == 0 and totals["skipped"] == 0:
        print("[错误] 所有标的均拉取失败，请检查网络或数据源配置（config.yaml → data.provider）")
        return 1
    print("=" * 72)
    return 0


if __name__ == "__main__":  # pragma: no cover - 脚本入口
    sys.exit(main())

"""QuantDesk 数据库恢复脚本（M4-05）。

安全策略（重要）
----------------
1. **恢复前自动备份当前库**：目标库存在时先做一次在线备份（``quantdesk-before-restore-*.db``），
   自动备份失败则**中止恢复**（避免"恢复失败又丢了现有数据"）；
2. 备份文件先做 ``PRAGMA integrity_check`` + **22 张核心表存在性校验**，损坏/缺表的备份
   **绝不覆盖**当前库；
3. 恢复需要显式二次确认：命令行加 ``--yes``，或在交互终端输入 ``RESTORE``；
4. 恢复后再次执行 ``integrity_check`` + 22 表校验，并给出关键表行数摘要。

用法（项目根目录）::

    .venv\\Scripts\\python.exe scripts\\restore_db.py --list
    .venv\\Scripts\\python.exe scripts\\restore_db.py runtime\\backups\\quantdesk-20261025-2130.db --yes
    .venv\\Scripts\\python.exe scripts\\restore_db.py 备份文件 --db 测试库.db --yes   # 演练/临时库

退出码：0=成功，1=失败（中文原因）。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
for path in (str(ROOT), str(SCRIPT_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from backup_db import integrity_check, list_backups  # noqa: E402  同目录脚本复用


def required_core_tables() -> tuple[str, ...]:
    """返回 SPEC §5.2 冻结的 22 张核心表名（core.models 不可用时退回内置清单）。"""
    try:
        from core.models import TABLE_NAMES

        return tuple(TABLE_NAMES)
    except Exception:  # noqa: BLE001 - 极端情况下仍可给出核心校验清单
        return (
            "symbols", "trading_calendar", "daily_bars", "fundamentals", "adjust_events",
            "data_quality_issues", "strategy_instances", "strategy_signals", "backtest_runs",
            "backtest_equity", "backtest_trades", "accounts", "positions", "orders", "trades",
            "cash_flows", "equity_curve", "risk_config", "app_settings", "audit_logs",
            "job_runs", "notifications",
        )


def inspect_sqlite(path: Path) -> tuple[int, str]:
    """返回（表数量, 表名逗号串）；无法读取时抛中文 RuntimeError。"""
    try:
        conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=10)
        try:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise RuntimeError(f"读取数据库失败（文件可能损坏）：{exc}") from exc
    names = [row[0] for row in rows]
    return len(names), ",".join(names)


def validate_backup(backup_path: Path) -> dict[str, object]:
    """校验备份文件：存在、integrity_check、22 张核心表齐全。

    :returns: ``{"tables": [...], "size": int, "detail": str}``
    :raises RuntimeError: 备份不可用（中文原因；调用方不得覆盖当前库）
    """
    backup_path = Path(backup_path)
    if not backup_path.exists():
        raise RuntimeError(f"恢复失败：备份文件不存在（{backup_path}）")
    if backup_path.stat().st_size <= 0:
        raise RuntimeError(f"恢复失败：备份文件为空（{backup_path}）")
    ok, detail = integrity_check(backup_path)
    if not ok:
        raise RuntimeError(
            f"恢复失败：备份文件完整性检查未通过（{detail}），已中止（当前库未被覆盖）。"
            "请改用其它备份或手工处理。"
        )
    count, names_csv = inspect_sqlite(backup_path)
    tables = [name for name in names_csv.split(",") if name]
    required = required_core_tables()
    missing = [name for name in required if name not in tables]
    if missing:
        raise RuntimeError(
            f"恢复失败：备份缺少 {len(missing)} 张核心表（{'、'.join(missing[:6])}…），"
            "不像 QuantDesk 数据库；已中止（当前库未被覆盖）。"
        )
    return {"tables": tables, "table_count": count, "size": backup_path.stat().st_size, "detail": detail}


def _auto_backup_current(target: Path, backup_dir: Path, keep: int) -> Path | None:
    """恢复前自动备份当前库；目标不存在/为空时跳过。"""
    if not target.exists() or target.stat().st_size <= 0:
        return None
    from backup_db import backup_database

    print("[1/4] 恢复前自动备份当前库（防止恢复失败丢失现有数据）…")
    result = backup_database(target, backup_dir, keep, prefix="quantdesk-before-restore")
    return Path(result["path"])


def _confirm(assume_yes: bool) -> None:
    """二次确认：--yes 或交互输入 RESTORE；非交互环境拒绝静默覆盖。"""
    if assume_yes:
        return
    if not sys.stdin.isatty():
        raise RuntimeError(
            "恢复需要二次确认：非交互环境请显式加 --yes（命令行）以避免误覆盖现有数据库。"
        )
    print("即将用备份覆盖当前数据库，现有数据将被替换（已自动备份）。")
    answer = input("请输入 RESTORE 确认（其它输入取消）：").strip()
    if answer != "RESTORE":
        raise RuntimeError("已取消恢复（未输入 RESTORE），当前数据库保持不变。")


def restore_database(
    backup_path: Path,
    target_path: Path,
    backup_dir: Path,
    keep: int = 7,
    *,
    assume_yes: bool = False,
) -> dict[str, object]:
    """把备份恢复到目标数据库（内部函数，CLI 与自测复用）。

    :raises RuntimeError: 备份不可用/自动备份失败/用户取消/恢复失败（中文原因）
    """
    backup_path = Path(backup_path).resolve()
    target_path = Path(target_path)
    backup_dir = Path(backup_dir)

    print("[0/4] 校验备份文件 …")
    info = validate_backup(backup_path)
    print(
        f"      备份可用：{backup_path.name}（{info['size']:,} 字节，"
        f"{info['table_count']} 张表，完整性 OK）"
    )

    auto_backup = _auto_backup_current(target_path, backup_dir, keep)
    if auto_backup is not None:
        print(f"      当前库已自动备份：{auto_backup}")

    print("[2/4] 二次确认 …")
    _confirm(assume_yes)

    # 清理目标库残留 WAL/SHM（恢复写入后旧 WAL 帧不得回放；请先停止应用/自动化）
    for suffix in ("-wal", "-shm"):
        stale = Path(str(target_path) + suffix)
        if stale.exists():
            try:
                stale.unlink()
            except OSError as exc:  # pragma: no cover
                print(f"[警告] 无法删除 {stale.name}（{exc}），请确认应用已停止")
    target_path.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    print("[3/4] 正在恢复（SQLite backup API）…")
    try:
        src = sqlite3.connect(f"file:{backup_path.as_posix()}?mode=ro", uri=True, timeout=30)
        dst = sqlite3.connect(str(target_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"恢复失败：写入目标库 {target_path} 时出错（{exc}）。"
            "若应用/自动化仍在运行请先停止后重试；当前库可能未变更。"
        ) from exc
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)

    print("[4/4] 恢复后校验 …")
    ok, detail = integrity_check(target_path)
    if not ok:
        raise RuntimeError(
            f"恢复失败：恢复后的数据库完整性检查未通过（{detail}）。"
            f"已自动备份原库：{auto_backup or '（原库不存在，无需备份）'}"
        )
    count, names_csv = inspect_sqlite(target_path)
    tables = [name for name in names_csv.split(",") if name]
    missing = [name for name in required_core_tables() if name not in tables]
    if missing:
        raise RuntimeError(f"恢复失败：恢复后缺少核心表（{'、'.join(missing[:6])}…）")
    # 关键表行数摘要（便于人工确认数据一致）
    summary: dict[str, int] = {}
    try:
        conn = sqlite3.connect(f"file:{target_path.as_posix()}?mode=ro", uri=True, timeout=10)
        try:
            for table in ("accounts", "positions", "orders", "trades", "daily_bars",
                          "equity_curve", "backtest_runs", "audit_logs"):
                try:
                    summary[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                except sqlite3.Error:
                    continue
        finally:
            conn.close()
    except sqlite3.Error:  # pragma: no cover - 摘要失败不影响恢复结果
        pass

    size = target_path.stat().st_size
    print(f"[成功] 恢复完成：{target_path}")
    print(f"       大小：{size:,} 字节；{count} 张表（22 张核心表齐全）；完整性 OK；耗时 {elapsed_ms:.0f} ms")
    if summary:
        print("       关键表行数：" + "，".join(f"{key}={value}" for key, value in summary.items()))
    if auto_backup is not None:
        print(f"       如需回退：scripts\\restore_db.py \"{auto_backup}\" --yes")
    return {
        "backup": backup_path,
        "target": target_path,
        "auto_backup": auto_backup,
        "table_count": count,
        "summary": summary,
        "elapsed_ms": elapsed_ms,
        "integrity": detail,
    }


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    from backup_db import resolve_default_paths

    default_db, default_backup_dir, _default_keep = resolve_default_paths()
    parser = argparse.ArgumentParser(
        description="QuantDesk 数据库恢复（恢复前自动备份当前库 + 完整性校验）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("backup", nargs="?", type=Path, help="备份文件路径（相对路径会在备份目录中查找）")
    parser.add_argument("--list", action="store_true", help="列出备份目录中的备份文件后退出")
    parser.add_argument("--db", type=Path, default=default_db, help="目标数据库文件路径")
    parser.add_argument("--backup-dir", type=Path, default=default_backup_dir, help="备份目录（自动备份/查找用）")
    parser.add_argument("--keep", type=int, default=7, help="自动备份保留份数")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认（自动化/演练必填）")
    return parser


def main(argv: list[str] | None = None) -> int:
    """脚本入口：返回进程退出码（0 成功 / 1 失败）。"""
    args = build_parser().parse_args(argv)
    print("=" * 66)
    print("QuantDesk 数据库恢复")
    print(f"  目标数据库：{args.db}")
    print(f"  备份目录  ：{args.backup_dir}")
    print("=" * 66)

    if args.list:
        backups = list_backups(args.backup_dir)
        if not backups:
            print(f"[提示] 暂无备份：{args.backup_dir}（先运行 scripts\\backup_db.py）")
            return 0
        print(f"共 {len(backups)} 份备份（从旧到新）：")
        for index, path in enumerate(backups, 1):
            stat = path.stat()
            print(f"  {index:>2}. {path.name}  {stat.st_size:,} 字节  {datetime_str(stat.st_mtime)}")
        return 0

    if args.backup is None:
        print("[失败] 未指定备份文件：scripts\\restore_db.py <备份文件> --yes（或 --list 查看）")
        return 1
    backup_path = args.backup
    if not backup_path.is_absolute() and not backup_path.exists():
        candidate = args.backup_dir / backup_path
        if candidate.exists():
            backup_path = candidate

    try:
        restore_database(backup_path, args.db, args.backup_dir, args.keep, assume_yes=args.yes)
    except RuntimeError as exc:
        print(f"[失败] {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI 兜底
        print(f"[失败] 恢复异常：{type(exc).__name__}: {exc}")
        return 1
    print("[完成] 请启动应用验证账户/持仓/订单数据；如异常可用“自动备份”回退。")
    return 0


def datetime_str(timestamp: float) -> str:
    """把 mtime 格式化为本地时间字符串。"""
    from datetime import datetime

    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    raise SystemExit(main())

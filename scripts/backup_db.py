"""QuantDesk 数据库在线备份脚本（M4-05）。

功能
----
- 用 SQLite **在线备份 API**（``sqlite3.Connection.backup``）把 ``runtime/quantdesk.db``
  一致地复制到 ``runtime/backups/``（WAL 模式下也安全）；
- 备份前后都执行 ``PRAGMA integrity_check``，损坏库**拒绝生成备份**并给中文提示；
- 按 ``database.backup_keep``（默认 7）保留最近 N 份，自动清理旧备份；
- ``--dry-run`` 只打印将执行的动作，不写文件；
- 打印备份路径、文件大小、耗时，适合设置页"立即备份"/发版前手工执行。

用法（项目根目录）::

    .venv\\Scripts\\python.exe scripts\\backup_db.py                # 备份默认数据库
    .venv\\Scripts\\python.exe scripts\\backup_db.py --dry-run      # 预演，不写盘
    .venv\\Scripts\\python.exe scripts\\backup_db.py --keep 3       # 只保留最近 3 份
    .venv\\Scripts\\python.exe scripts\\backup_db.py --db path.db --dest-dir backups

退出码：0=成功，1=失败（中文原因）。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BACKUP_GLOB = "quantdesk-*.db"


# ---------------------------------------------------------------------------
# 通用工具（restore_db.py 也会复用其中的检查函数）
# ---------------------------------------------------------------------------


def resolve_default_paths() -> tuple[Path, Path, int]:
    """从配置读取默认（数据库路径, 备份目录, 保留份数）；配置不可用时退回项目默认。"""
    db_path = ROOT / "runtime" / "quantdesk.db"
    backup_dir = ROOT / "runtime" / "backups"
    keep = 7
    try:
        from core.config import get_config

        config = get_config()
        sqlite_path = config.sqlite_path
        if sqlite_path is not None:
            db_path = Path(sqlite_path)
        backup_dir = Path(config.backup_dir)
        keep = int(config.database.backup_keep)
    except Exception as exc:  # noqa: BLE001 - 配置层不可用不阻塞脚本（使用默认路径）
        print(f"[警告] 读取配置失败，使用默认路径（{type(exc).__name__}: {exc}）")
    return db_path, backup_dir, keep


def integrity_check(db_path: Path) -> tuple[bool, str]:
    """对 SQLite 文件执行 ``PRAGMA integrity_check``（只读方式，失败返回 False+中文原因）。"""
    if not db_path.exists():
        return False, f"数据库文件不存在：{db_path}"
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=10)
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return False, f"完整性检查失败（文件可能已损坏/被占用）：{exc}"
    detail = str(result[0]) if result else "unknown"
    return detail.lower() == "ok", detail


def list_backups(backup_dir: Path) -> list[Path]:
    """列出备份目录中的备份文件（按修改时间从旧到新）。"""
    if not backup_dir.exists():
        return []
    return sorted(
        (path for path in backup_dir.glob(BACKUP_GLOB) if path.is_file()),
        key=lambda path: (path.stat().st_mtime, path.name),
    )


def cleanup_backups(backup_dir: Path, keep: int) -> list[Path]:
    """保留最近 ``keep`` 份备份，返回被删除的路径列表。"""
    keep = max(int(keep), 1)
    backups = list_backups(backup_dir)
    removed: list[Path] = []
    for path in backups[:-keep] if len(backups) > keep else []:
        try:
            path.unlink()
            removed.append(path)
        except OSError as exc:  # pragma: no cover - 文件被占用
            print(f"[警告] 旧备份清理失败：{path}（{exc}）")
    return removed


def backup_database(
    db_path: Path,
    backup_dir: Path,
    keep: int = 7,
    *,
    dry_run: bool = False,
    prefix: str = "quantdesk",
) -> dict[str, object]:
    """执行一次在线备份（内部函数，供 CLI 与恢复脚本复用）。

    :returns: ``{"path": Path, "size": int, "removed": list[Path], "elapsed_ms": float}``
    :raises RuntimeError: 源库不存在/损坏/备份失败（中文消息）
    """
    db_path = Path(db_path)
    backup_dir = Path(backup_dir)
    if not db_path.exists():
        raise RuntimeError(f"备份失败：数据库文件不存在（{db_path}）。请先运行 scripts/init_db.py 建库。")
    if db_path.stat().st_size <= 0:
        raise RuntimeError(f"备份失败：数据库文件为空（{db_path}），无法备份。")

    ok, detail = integrity_check(db_path)
    if not ok:
        raise RuntimeError(
            f"备份失败：源数据库完整性检查未通过（{detail}）。"
            "为避免备份损坏数据，已中止；请先按 RUNBOOK §6 从最近可用备份恢复。"
        )

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"{prefix}-{stamp}.db"
    counter = 1
    while target.exists():
        target = backup_dir / f"{prefix}-{stamp}-{counter}.db"
        counter += 1
    size_hint = db_path.stat().st_size
    if dry_run:
        print(f"[预演] 将备份：{db_path}")
        print(f"[预演] 目标文件：{target}（源大小 {size_hint:,} 字节）")
        print(f"[预演] 保留策略：最近 {max(int(keep), 1)} 份（当前已有 {len(list_backups(backup_dir))} 份）")
        return {"path": target, "size": 0, "removed": [], "elapsed_ms": 0.0, "dry_run": True}

    backup_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    try:
        src = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=30)
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)  # SQLite 在线备份 API：WAL 模式下也能得到一致快照
        finally:
            dst.close()
            src.close()
    except sqlite3.Error as exc:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"备份失败：{db_path} → {target}（{exc}）") from exc

    ok, detail = integrity_check(target)
    if not ok:
        target.unlink(missing_ok=True)
        raise RuntimeError(
            f"备份失败：新备份文件完整性检查未通过（{detail}），已删除该文件（原库未被修改）。"
        )
    removed = cleanup_backups(backup_dir, keep)
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
    size = target.stat().st_size
    print(f"[成功] 备份完成：{target}")
    print(f"       大小：{size:,} 字节（{size / 1024:.1f} KB）；耗时 {elapsed_ms:.0f} ms")
    print(f"       完整性检查：OK；保留最近 {max(int(keep), 1)} 份，清理旧备份 {len(removed)} 份")
    if removed:
        for path in removed:
            print(f"       已删除：{path.name}")
    return {"path": target, "size": size, "removed": removed, "elapsed_ms": elapsed_ms, "dry_run": False}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    default_db, default_backup_dir, default_keep = resolve_default_paths()
    parser = argparse.ArgumentParser(
        description="QuantDesk 数据库在线备份（SQLite backup API，WAL 安全）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--db", type=Path, default=default_db, help="源数据库文件路径")
    parser.add_argument("--dest-dir", type=Path, default=default_backup_dir, help="备份目录")
    parser.add_argument("--keep", type=int, default=default_keep, help="保留最近 N 份备份")
    parser.add_argument("--dry-run", action="store_true", help="只预演，不写文件")
    return parser


def main(argv: list[str] | None = None) -> int:
    """脚本入口：返回进程退出码（0 成功 / 1 失败）。"""
    args = build_parser().parse_args(argv)
    print("=" * 66)
    print("QuantDesk 数据库备份")
    print(f"  源数据库：{args.db}")
    print(f"  备份目录：{args.dest_dir}")
    print(f"  保留份数：{args.keep}{'（dry-run 不写盘）' if args.dry_run else ''}")
    print("=" * 66)
    try:
        backup_database(args.db, args.dest_dir, args.keep, dry_run=args.dry_run)
    except RuntimeError as exc:
        print(f"[失败] {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI 兜底：给中文提示而非 traceback
        print(f"[失败] 备份异常：{type(exc).__name__}: {exc}")
        return 1
    print("[完成] 可用 scripts\\restore_db.py --list 查看备份列表")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

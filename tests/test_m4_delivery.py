"""M4 交付验收测试：数据库备份/恢复脚本 e2e + 发版自检脚本冒烟。

覆盖 Lead R6 要求：
1. 备份 → 恢复 → 数据一致（行数/完整性校验）、幂等（重复备份/重复恢复）；
2. 损坏库处理：坏 SQLite 文件要被**检出并给出中文可操作提示**，不得崩溃；
3. ``scripts/backup_db.py`` / ``scripts/restore_db.py`` / ``scripts/release_check.py`` 冒烟；
   正常状态下 ``release_check.py`` 必须 PASS（退出码 0，无 [FAIL]）。

注意（P0 修复）：``release_check.py`` 内部会跑 ``pytest -q`` 全量——从测试里调用它会
“套件内套套件”递归卡死，因此这里**必须传 ``--skip-tests``**（发版时人工跑全量）。
本文件全部是子进程/落盘型用例，统一标 ``slow``，默认快速套件不执行。
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "scripts"
SYMBOL = "600519.SH"

pytestmark = [pytest.mark.integration, pytest.mark.slow]


# ---------------------------------------------------------------------------
# 环境与脚本运行辅助
# ---------------------------------------------------------------------------


def _require_script(name: str) -> Path:
    path = SCRIPTS / name
    if not path.exists():
        pytest.xfail(f"scripts/{name} 尚未交付（R6 进行中）；落地后本用例自动生效")
    return path


def _base_env(tmp_path: Path, db_path: Path) -> dict[str, str]:
    env = os.environ.copy()  # 必须继承系统环境（Windows 缺 SystemRoot 时子进程无法启动）
    env.update(
        {
            "QD_CONFIG_FILE": str(PROJECT_ROOT / "config.yaml"),
            "QD_APP_DATA_DIR": str(tmp_path / "runtime"),
            "QD_DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
            "QD_DATA_PROVIDER": "mock",
            "QD_OFFLINE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
        }
    )
    return env


def _run_script(
    script: Path, *args: str, env: dict[str, str], expect_ok: bool | None = None
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [sys.executable, str(script), *args],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdin=subprocess.DEVNULL,  # 非交互：验证二次确认缺省拒绝
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=240,
    )
    if expect_ok is True:
        assert result.returncode == 0, f"脚本失败：stdout={result.stdout}\nstderr={result.stderr}"
    elif expect_ok is False:
        assert result.returncode != 0, f"脚本本应失败但成功：stdout={result.stdout}"
    return result


@pytest.fixture()
def seeded_db(tmp_path: Path):
    """初始化一个含数据/表的临时库，返回 (db_path, backups_dir, env)。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from core.models import Base, DailyBarORM

    db_path = tmp_path / "runtime" / "quantdesk.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    backups_dir = tmp_path / "backups"
    env = _base_env(tmp_path, db_path)

    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        for offset in range(30):
            session.add(
                DailyBarORM(
                    symbol=SYMBOL,
                    trade_date=dt.date(2024, 1, 2) + dt.timedelta(days=offset),
                    open=10.0 + offset,
                    high=11.0 + offset,
                    low=9.0 + offset,
                    close=10.5 + offset,
                    volume=1_000_000,
                    amount=10_500_000.0,
                    adj_factor=1.0,
                    turnover_rate=1.0,
                    is_suspended=False,
                    source="akshare",
                )
            )
        session.commit()
    finally:
        session.close()
    engine.dispose()
    return db_path, backups_dir, env


def _count(db_path: Path, table: str = "daily_bars") -> int:
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _integrity_ok(db_path: Path) -> bool:
    with sqlite3.connect(db_path) as conn:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0]).lower() == "ok"


# ---------------------------------------------------------------------------
# 一、备份脚本
# ---------------------------------------------------------------------------


def test_backup_script_creates_valid_snapshot(seeded_db) -> None:
    db_path, backups_dir, env = seeded_db
    script = _require_script("backup_db.py")

    result = _run_script(
        script, "--db", str(db_path), "--dest-dir", str(backups_dir), "--keep", "3", env=env, expect_ok=True
    )

    assert "[成功] 备份完成" in result.stdout, f"缺少成功提示：{result.stdout}"
    backups = sorted(backups_dir.glob("*.db"))
    assert backups, f"未生成备份文件：{list(backups_dir.glob('*'))}"
    latest = backups[-1]
    assert latest.stat().st_size > 0
    assert _count(latest) == _count(db_path) == 30
    assert _integrity_ok(latest), "备份文件 PRAGMA integrity_check 未通过"


def test_backup_script_dry_run_writes_nothing(seeded_db) -> None:
    db_path, backups_dir, env = seeded_db
    script = _require_script("backup_db.py")

    result = _run_script(
        script, "--db", str(db_path), "--dest-dir", str(backups_dir), "--dry-run", env=env, expect_ok=True
    )

    assert "[预演]" in result.stdout
    assert not list(backups_dir.glob("*.db")), "dry-run 不应写入备份文件"


def test_backup_is_idempotent_and_keeps_limit(seeded_db) -> None:
    db_path, backups_dir, env = seeded_db
    script = _require_script("backup_db.py")

    for _ in range(3):
        _run_script(
            script, "--db", str(db_path), "--dest-dir", str(backups_dir), "--keep", "2", env=env, expect_ok=True
        )

    backups = sorted(backups_dir.glob("*.db"))
    assert len(backups) <= 2, f"保留策略未生效（keep=2，实际 {len(backups)} 份）"
    for backup in backups:
        assert _count(backup) == 30
        assert _integrity_ok(backup)


# ---------------------------------------------------------------------------
# 二、恢复脚本：备份 → 破坏 → 恢复 → 数据一致 + 幂等
# ---------------------------------------------------------------------------


def test_restore_script_roundtrip_restores_data(seeded_db) -> None:
    db_path, backups_dir, env = seeded_db
    backup_script = _require_script("backup_db.py")
    restore_script = _require_script("restore_db.py")

    _run_script(backup_script, "--db", str(db_path), "--dest-dir", str(backups_dir), env=env, expect_ok=True)
    backup_file = sorted(backups_dir.glob("*.db"))[-1]
    before = _count(db_path)

    with sqlite3.connect(db_path) as conn:  # 模拟误删
        conn.execute("DELETE FROM daily_bars")
        conn.commit()
    assert _count(db_path) == 0

    result = _run_script(restore_script, str(backup_file), "--db", str(db_path), "--yes", env=env, expect_ok=True)

    assert "[成功] 恢复完成" in result.stdout, f"缺少恢复成功提示：{result.stdout}"
    assert _count(db_path) == before, "恢复后数据与备份不一致"
    assert _integrity_ok(db_path)

    again = _run_script(restore_script, str(backup_file), "--db", str(db_path), "--yes", env=env, expect_ok=True)
    assert "[成功]" in again.stdout
    assert _count(db_path) == before


def test_restore_requires_explicit_confirmation(seeded_db) -> None:
    """非交互环境未加 ``--yes`` 必须拒绝（防止误覆盖生产库）。"""
    db_path, backups_dir, env = seeded_db
    backup_script = _require_script("backup_db.py")
    restore_script = _require_script("restore_db.py")

    _run_script(backup_script, "--db", str(db_path), "--dest-dir", str(backups_dir), env=env, expect_ok=True)
    backup_file = sorted(backups_dir.glob("*.db"))[-1]
    before = _count(db_path)

    result = _run_script(restore_script, str(backup_file), "--db", str(db_path), env=env, expect_ok=False)

    assert any(word in result.stdout for word in ("RESTORE", "二次确认", "确认"))
    assert _count(db_path) == before, "未确认时不得修改数据库"


# ---------------------------------------------------------------------------
# 三、损坏库处理（检出 + 中文提示，不崩溃）
# ---------------------------------------------------------------------------


def test_corrupt_database_is_detected_gracefully(tmp_path: Path) -> None:
    from core.config import load_config
    from core.db import Database

    corrupt = tmp_path / "runtime" / "corrupt.db"
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_bytes(b"this is definitely not a sqlite database" * 100)

    base = load_config(environ={})
    database_cfg = base.database.model_copy(update={"url": f"sqlite:///{corrupt.as_posix()}"})
    app_cfg = base.app.model_copy(update={"data_dir": str(tmp_path / "runtime")})
    cfg = base.model_copy(update={"database": database_cfg, "app": app_cfg})

    database = Database(config=cfg)
    try:
        health = database.startup_check()
    finally:
        database.dispose()

    assert health.ok is False, "损坏库未被检出"
    message = health.message or ""
    assert any("\u4e00" <= ch <= "\u9fff" for ch in message), "缺少中文可操作提示"
    assert any(word in message for word in ("备份", "损坏", "恢复"))


def test_release_check_fails_on_corrupt_database(tmp_path: Path) -> None:
    script = _require_script("release_check.py")
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"garbage" * 500)
    env = _base_env(tmp_path, corrupt)

    result = _run_script(script, "--skip-tests", env=env, expect_ok=False)

    assert "[FAIL]" in result.stdout, f"损坏库应判 FAIL：stdout={result.stdout}\nstderr={result.stderr}"


# ---------------------------------------------------------------------------
# 四、发版自检脚本：正常状态必须 PASS（--skip-tests 防止递归跑全量）
# ---------------------------------------------------------------------------


def test_release_check_passes_on_healthy_state(seeded_db) -> None:
    _db_path, _backups_dir, env = seeded_db
    script = _require_script("release_check.py")

    result = _run_script(script, "--skip-tests", env=env, expect_ok=True)

    assert "[FAIL]" not in result.stdout, f"正常状态不应有 FAIL 项：{result.stdout}"
    assert "[PASS]" in result.stdout
    assert "发版自检通过" in result.stdout or "PASS" in result.stdout
    assert "数据库" in result.stdout
    # --skip-tests 时测试项应显式标注跳过，避免“假装跑过”
    assert "skip-tests" in result.stdout or "跳过" in result.stdout

"""数据层「mock 混源」隔离测试（P0 数据完整性约束）。

背景：``runtime/quantdesk.db`` 曾混入 524 行 mock 行情，导致 246% 假收益。
修复后的契约：
1. ``MockProvider`` 的数据**只允许内存使用**，``BarCache.update()`` 绝不写入 ``daily_bars``；
2. ``BarCache.load()`` 在区间内检测到同一标的多 source → **fail-closed 抛中文错误**
   （提示运行 ``scripts/purge_mock_cache.py`` 清理）；
3. ``scripts/purge_mock_cache.py`` 幂等：连续跑两次结果一致（第二次无副作用）。

隔离：全部用 ``test_db`` 临时 SQLite（用例级），不触碰项目 ``runtime/`` 库；
配置用 ``load_config(environ={})`` 显式禁用环境变量覆盖，保证可复现。
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from core.config import load_config
from core.models import DailyBarORM
from data.cache import BarCache, DataIntegrityError
from data.provider import MockProvider

SYMBOL = "600519.SH"
START = dt.date(2024, 1, 2)
END = dt.date(2024, 3, 29)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PURGE_SCRIPT = PROJECT_ROOT / "scripts" / "purge_mock_cache.py"

pytestmark = pytest.mark.integration


def _cache(test_db, provider=None) -> BarCache:
    return BarCache(
        provider=provider or MockProvider(),
        config=load_config(environ={}),
        session_factory=test_db.session_factory,
    )


def _insert_bars(test_db, rows: list[tuple[str, dt.date, str]]) -> None:
    """按 ``(symbol, trade_date, source)`` 插入日线（价格用确定值）。"""
    session = test_db.session()
    try:
        for index, (symbol, day, source) in enumerate(rows):
            session.add(
                DailyBarORM(
                    symbol=symbol,
                    trade_date=day,
                    open=10.0 + index,
                    high=10.5 + index,
                    low=9.5 + index,
                    close=10.2 + index,
                    volume=1_000_000,
                    amount=10_000_000.0,
                    adj_factor=1.0,
                    turnover_rate=1.0,
                    is_suspended=False,
                    source=source,
                )
            )
        session.commit()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 一、MockProvider 绝不落库
# ---------------------------------------------------------------------------


def test_mock_provider_does_not_write_daily_bars(test_db) -> None:
    cache = _cache(test_db)
    before = test_db.count("daily_bars")

    report = cache.update([SYMBOL], START, END)

    after = test_db.count("daily_bars")
    assert after == before == 0, f"mock 行情被写入了 daily_bars（{before} → {after} 行）"
    assert report.added == 0
    # 拦截以扩展字段如实上报，且消息明确说明「不写入缓存」
    assert getattr(report, "mock_blocked", 0) == 1
    assert "mock" in report.message.lower() or "Mock" in report.message
    assert "不写入" in report.message or "不会写入" in report.message


def test_mock_update_is_idempotent_on_empty_db(test_db) -> None:
    cache = _cache(test_db)

    first = cache.update([SYMBOL], START, END)
    second = cache.update([SYMBOL], START, END)

    assert test_db.count("daily_bars") == 0
    assert first.mock_blocked == second.mock_blocked == 1


# ---------------------------------------------------------------------------
# 二、load() 混源 fail-closed
# ---------------------------------------------------------------------------


def test_load_raises_when_multiple_sources_in_range(test_db) -> None:
    _insert_bars(
        test_db,
        [
            (SYMBOL, dt.date(2024, 1, 2), "akshare"),
            (SYMBOL, dt.date(2024, 1, 3), "mock"),  # 混入 mock
            (SYMBOL, dt.date(2024, 1, 4), "akshare"),
        ],
    )
    cache = _cache(test_db)

    with pytest.raises(DataIntegrityError) as excinfo:
        cache.load([SYMBOL], START, END)

    message = str(excinfo.value)
    # 契约：fail-closed + 中文可操作错误（说明混存不可拼接、给出清理脚本）
    assert any(keyword in message for keyword in ("混存", "混用", "数据来源")), (
        f"混源未按契约报错，实际：{message}"
    )
    assert "mock" in message.lower() and SYMBOL in message
    assert "拒绝" in message
    assert "purge_mock_cache.py" in message, "错误信息应提示清理脚本（可操作）"


def test_load_accepts_single_source(test_db) -> None:
    """纯单源数据必须正常加载（避免守卫过宽误伤）。"""
    _insert_bars(
        test_db,
        [
            (SYMBOL, dt.date(2024, 1, 2), "akshare"),
            (SYMBOL, dt.date(2024, 1, 3), "akshare"),
        ],
    )
    cache = _cache(test_db)

    frame = cache.load([SYMBOL], START, END, adjust="none")

    assert len(frame) == 2
    assert set(frame["symbol"]) == {SYMBOL}
    # load() 输出契约列（date/symbol/open/high/low/close/volume...）
    assert {"date", "symbol", "open", "high", "low", "close", "volume"} <= set(frame.columns)


def test_load_empty_range_returns_empty_frame(test_db) -> None:
    cache = _cache(test_db)

    frame = cache.load([SYMBOL], START, END)

    assert frame.empty


# ---------------------------------------------------------------------------
# 三、purge_mock_cache.py 幂等
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_purge_mock_cache_script_is_idempotent(test_db, tmp_path: Path) -> None:
    if not PURGE_SCRIPT.exists():
        pytest.xfail(
            "scripts/purge_mock_cache.py 尚未交付（Lead 已派 Dev1）；"
            "脚本落地后本用例自动生效，无需改测试"
        )

    # 准备：真实 + mock 混合的库
    _insert_bars(
        test_db,
        [
            (SYMBOL, dt.date(2024, 1, 2), "akshare"),
            (SYMBOL, dt.date(2024, 1, 3), "mock"),
            ("000001.SZ", dt.date(2024, 1, 2), "mock"),
        ],
    )
    assert test_db.count("daily_bars") == 3

    env = os.environ.copy()  # 必须继承系统环境（Windows 缺少 SystemRoot 时子进程无法启动）
    env.update(
        {
            "QD_CONFIG_FILE": str(PROJECT_ROOT / "config.yaml"),
            "QD_APP_DATA_DIR": str(tmp_path / "runtime"),
            "QD_DATABASE_URL": f"sqlite:///{test_db.path.as_posix()}",
        }
    )

    def _run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(PURGE_SCRIPT), *args],
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )

    # dry-run 不得删除任何行
    dry = _run("--dry-run")
    assert dry.returncode == 0, f"--dry-run 失败：stdout={dry.stdout}\nstderr={dry.stderr}"
    assert test_db.count("daily_bars") == 3, "--dry-run 不应修改数据库"

    first = _run()
    assert first.returncode == 0, f"首次清理失败：stdout={first.stdout}\nstderr={first.stderr}"
    rows_after_first = test_db.count("daily_bars")
    with test_db.engine.connect() as conn:
        sources_after_first = {
            str(row[0]) for row in conn.execute(text("SELECT DISTINCT source FROM daily_bars"))
        }
    assert sources_after_first <= {"akshare"}, f"mock 行未被清理：{sources_after_first}"
    assert rows_after_first == 1

    # 幂等：第二次运行结果一致、无副作用
    second = _run()
    assert second.returncode == 0, f"二次清理失败：stdout={second.stdout}\nstderr={second.stderr}"
    assert test_db.count("daily_bars") == rows_after_first

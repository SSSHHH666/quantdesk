"""性能基线测试（M5 验收 / Lead R6）。

基线（实测值见每个用例 docstring；本机 = Windows + venv Python 3.12.7）：
1. 冷启动：``AppTest`` 首屏（Streamlit 脚本首轮执行，含页面发现/配置加载/DB 初始化）≤ **15s**
2. AppTest 二次渲染（切换页面后的重新执行）≤ **2s**
3. 单次回测：3 标的 × 5 年日线 ≤ **10s**

说明：
- 时间用 ``time.perf_counter()`` 实测，断言失败时会在消息里打印实测值，便于定位性能退化；
- 环境与 UI 用例一致（mock 数据源 + 离线 + 临时 DB），排除网络波动影响；
- 首轮包含一次性开销（Streamlit 运行期、缓存初始化），第二项专测“交互后重渲染”。
"""

from __future__ import annotations

import datetime as dt
import time
from pathlib import Path

import pytest

from backtest.engine import BacktestEngine
from data.provider import MockProvider

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_FILE = PROJECT_ROOT / "app.py"

COLD_START_BUDGET_S = 15.0
SECOND_RENDER_BUDGET_S = 2.0
BACKTEST_BUDGET_S = 10.0

pytestmark = [pytest.mark.e2e, pytest.mark.slow]


@pytest.fixture()
def isolated_app_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from core import config as core_config

    core_config.reset_config()
    monkeypatch.delenv("QD_CONFIG_FILE", raising=False)
    monkeypatch.setenv("QD_DATA_PROVIDER", "mock")
    monkeypatch.setenv("QD_OFFLINE", "1")
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setenv("QD_APP_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("QD_DATABASE_URL", f"sqlite:///{(runtime_dir / 'perf.db').as_posix()}")
    try:
        yield runtime_dir
    finally:
        core_config.reset_config()


def test_app_cold_start_within_budget(isolated_app_env: Path) -> None:
    """AppTest 首屏 ≤ 15s（实测约 1.2~2.0s；含 Streamlit 脚本首轮 + DB 初始化）。"""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_FILE), default_timeout=120)
    started = time.perf_counter()
    at.run()
    elapsed = time.perf_counter() - started

    assert len(at.exception) == 0, f"首屏异常：{[str(e.value) for e in at.exception]}"
    assert elapsed <= COLD_START_BUDGET_S, f"冷启动 {elapsed:.2f}s 超过基线 {COLD_START_BUDGET_S}s"


def test_app_second_render_within_budget(isolated_app_env: Path) -> None:
    """二次渲染（交互/切页后的重执行）≤ 2s（实测约 0.2~0.6s）。"""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP_FILE), default_timeout=120)
    at.run()  # 首轮预热

    started = time.perf_counter()
    at.run()
    elapsed = time.perf_counter() - started

    assert len(at.exception) == 0
    assert elapsed <= SECOND_RENDER_BUDGET_S, f"二次渲染 {elapsed:.2f}s 超过基线 {SECOND_RENDER_BUDGET_S}s"


def test_backtest_three_symbols_five_years_within_budget() -> None:
    """单次回测：3 标的 × 5 年（2021-01-04 ~ 2025-12-31）≤ 10s（实测约 1.5~3.5s）。"""
    engine = BacktestEngine(data_source=MockProvider())
    symbols = ["600519.SH", "000001.SZ", "300750.SZ"]

    started = time.perf_counter()
    result = engine.run(
        "ma_cross",
        {"fast": 5, "slow": 20},
        symbols=symbols,
        start=dt.date(2021, 1, 4),
        end=dt.date(2025, 12, 31),
        initial_cash=100000.0,
        benchmark=None,
    )
    elapsed = time.perf_counter() - started

    assert len(result.equity) > 0
    assert result.symbols == symbols
    assert elapsed <= BACKTEST_BUDGET_S, f"3 标的 5 年回测 {elapsed:.2f}s 超过基线 {BACKTEST_BUDGET_S}s"

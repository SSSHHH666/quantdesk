"""回测引擎数据源守卫测试（P0 数据混源修复）。

契约：
1. 同一 symbol 在回测区间内**混用多个 source** → ``BacktestError``，中文消息含「数据源混用」；
2. 纯 mock bars 允许回测（演示用），但 ``result.is_mock == True`` 且 warnings 含
   ``RQ_WARN_MOCK_DATA``（中文说明含「离线 Mock」）；
3. 相邻交易日价格异常跳变（超过涨跌停 3 倍阈值）→ ``RQ_WARN_PRICE_JUMP`` 警告可见；
4. 负向对照：纯真实来源（akshare）→ ``is_mock == False`` 且无 mock 警告。

数据全部来自 ``MockProvider``（确定性、无网络），仅在内存里改 ``source``/价格构造用例。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from backtest.engine import (
    RQ_WARN_MOCK_DATA,
    RQ_WARN_PRICE_JUMP,
    WARNING_MESSAGES,
    BacktestEngine,
    BacktestError,
    BacktestResult,
)
from core.models import FeeConfig
from data.provider import MockProvider

SYMBOL = "600519.SH"
START = dt.date(2023, 1, 3)
END = dt.date(2024, 3, 29)
STRATEGY = ("ma_cross", {"fast": 5, "slow": 20})

pytestmark = pytest.mark.unit


def _mock_bars() -> pd.DataFrame:
    """确定性 mock 日线（含 source="mock" 与 prev_close 列）。"""
    frame = MockProvider().get_bars([SYMBOL], START, END)
    assert not frame.empty
    return frame


def _run(bars: pd.DataFrame) -> BacktestResult:
    engine = BacktestEngine()
    return engine.run(
        STRATEGY[0],
        STRATEGY[1],
        symbols=[SYMBOL],
        start=START,
        end=END,
        initial_cash=100000.0,
        fee_config=FeeConfig(),
        benchmark=None,
        bars=bars,
    )


# ---------------------------------------------------------------------------
# 一、混源 → fail-closed 中文错误
# ---------------------------------------------------------------------------


def test_mixed_source_bars_raise_chinese_error() -> None:
    bars = _mock_bars().copy()
    bars["source"] = "mock"
    half = len(bars) // 2
    bars.loc[bars.index[half:], "source"] = "akshare"

    with pytest.raises(BacktestError) as excinfo:
        _run(bars)

    message = str(excinfo.value)
    assert "数据源混用" in message, f"混源未按契约拒绝，实际：{message}"
    assert SYMBOL in message
    assert "禁止回测" in message


def test_mixed_source_message_lists_sources() -> None:
    bars = _mock_bars().copy()
    bars["source"] = "mock"
    bars.loc[bars.index[-1], "source"] = "akshare"

    with pytest.raises(BacktestError) as excinfo:
        _run(bars)

    message = str(excinfo.value)
    assert "akshare" in message and "mock" in message


def test_single_source_bars_are_not_rejected() -> None:
    """负向对照：单一来源（akshare）不应触发混源守卫，且不应被标记为 mock。"""
    bars = _mock_bars().copy()
    bars["source"] = "akshare"

    result = _run(bars)

    assert result.is_mock is False
    assert RQ_WARN_MOCK_DATA not in result.warnings
    assert result.metadata.get("sources", {}).get(SYMBOL) == "akshare"


# ---------------------------------------------------------------------------
# 二、纯 mock → 可回测但显式标注
# ---------------------------------------------------------------------------


def test_pure_mock_bars_are_flagged_is_mock_with_warning() -> None:
    result = _run(_mock_bars())

    assert result.is_mock is True, "纯 mock 行情必须标记 result.is_mock=True（UI 红色提示条）"
    assert RQ_WARN_MOCK_DATA in result.warnings, f"缺少 mock 数据警告：{result.warnings}"
    assert "离线 Mock" in WARNING_MESSAGES[RQ_WARN_MOCK_DATA]
    assert result.metadata.get("sources", {}).get(SYMBOL) == "mock"

    # 中文警告列表可直接给 UI 展示
    messages = result.warning_messages()
    assert any("离线 Mock" in text for text in messages)


def test_mock_result_still_has_metrics_for_demo() -> None:
    """演示用途：纯 mock 仍产出完整指标（只是被显式标记不可当真）。"""
    result = _run(_mock_bars())

    required = {"total_return", "annual_return", "max_drawdown", "sharpe", "trade_count"}
    assert required <= set(result.metrics)
    assert len(result.equity) > 0


# ---------------------------------------------------------------------------
# 三、异常价格跳变 → 警告可见
# ---------------------------------------------------------------------------


def test_abnormal_price_jump_emits_warning() -> None:
    bars = _mock_bars().copy()
    bars["source"] = "akshare"  # 单源，确保警告只来自“跳变”而非混源
    # 选中段（避开年度复权因子变化日），把整根 K 线放大 5 倍 → 日收益约 +400%
    jump_row = len(bars) // 2
    for column in ("open", "high", "low", "close"):
        bars.loc[bars.index[jump_row], column] = float(bars.loc[bars.index[jump_row], column]) * 5.0

    result = _run(bars)

    assert RQ_WARN_PRICE_JUMP in result.warnings, f"价格跳变未告警：{result.warnings}"
    assert "价格跳变" in WARNING_MESSAGES[RQ_WARN_PRICE_JUMP]
    assert any("跳变" in note for note in result.notes), f"缺少跳变说明：{result.notes}"


def test_normal_bars_do_not_emit_jump_warning() -> None:
    bars = _mock_bars().copy()
    bars["source"] = "akshare"

    result = _run(bars)

    assert RQ_WARN_PRICE_JUMP not in result.warnings

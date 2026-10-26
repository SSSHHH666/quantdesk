"""回测引擎数据防御测试（P0）：混源必拒、Mock 必标注、异常跳变告警。

背景：数据层可能出现同一 symbol 混用 mock 与真实行情（或区间内 source 不一致），
直接回测会因价格序列不可比产生虚假收益（实测曾出现 246% 假收益）。引擎必须
fail-closed：混源 → 抛 ``BacktestError``；全 Mock → 允许但必须显式标注（UI 红色提示条）。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from backtest.engine import (
    RQ_WARN_MOCK_DATA,
    RQ_WARN_PRICE_JUMP,
    BacktestEngine,
    BacktestError,
)
from data.provider import MockProvider

pytestmark = pytest.mark.unit

SYMBOL = "600519.SH"
START = date(2024, 1, 2)
END = date(2024, 6, 28)


def _mock_bars(source: str | None = "mock") -> pd.DataFrame:
    """MockProvider 确定性日线（离线、无网络）；可指定/移除 source 列。"""

    bars = MockProvider().get_bars([SYMBOL], START, END, "1d").copy()
    if source is None:
        bars = bars.drop(columns=["source"], errors="ignore")
    else:
        bars["source"] = source
    return bars


def test_engine_rejects_mixed_sources() -> None:
    """同一 symbol 区间内出现多个 source → 必须抛 BacktestError（禁止回测）。"""

    bars = _mock_bars("mock")
    midpoint = len(bars) // 2
    bars.loc[bars.index[midpoint:], "source"] = "akshare"

    with pytest.raises(BacktestError) as excinfo:
        BacktestEngine().run(
            "ma_cross", None, [SYMBOL], START, END, initial_cash=100000.0, bars=bars
        )

    message = str(excinfo.value)
    assert "数据源混用" in message, f"错误信息应说明数据源混用：{message}"
    assert SYMBOL in message, "错误信息应指明具体标的"


def test_engine_marks_mock_data() -> None:
    """全部来源为 mock → 允许回测，但 result.is_mock=True 且 warnings/metadata 标注演示数据。"""

    bars = _mock_bars("mock")
    result = BacktestEngine().run(
        "ma_cross", None, [SYMBOL], START, END, initial_cash=100000.0, bars=bars
    )

    assert result.is_mock is True
    assert RQ_WARN_MOCK_DATA in result.warnings
    assert result.metadata.get("is_mock") is True
    assert "演示" in str(result.metadata.get("data_note", ""))
    # 红色提示条所需的语义：summary 也能透出 is_mock
    assert result.summary()["is_mock"] is True


def test_engine_warns_price_jump() -> None:
    """无 source 列时兜底检测异常跳变（阈值 = 涨跌停幅度 ×3）→ 记 warning 供 UI 展示。"""

    bars = _mock_bars(source=None)
    jump_index = bars.index[len(bars) // 2]
    for column in ("open", "high", "low", "close"):
        bars.loc[jump_index:, column] = bars.loc[jump_index:, column] * 1.5

    result = BacktestEngine().run(
        "ma_cross", None, [SYMBOL], START, END, initial_cash=100000.0, bars=bars
    )

    assert RQ_WARN_PRICE_JUMP in result.warnings
    assert any("异常价格跳变" in note for note in result.notes), "notes 应说明跳变标的与示例日期"
    assert result.metadata.get("price_jumps", 0) >= 1


def test_engine_clean_real_source_has_no_mock_flag() -> None:
    """单一真实来源（source=akshare）→ 不标记 mock、不误报混源。"""

    bars = _mock_bars("akshare")
    result = BacktestEngine().run(
        "ma_cross", None, [SYMBOL], START, END, initial_cash=100000.0, bars=bars
    )

    assert result.is_mock is False
    assert RQ_WARN_MOCK_DATA not in result.warnings

"""全市场筛选器（strategy/screener.py）与验证框架的单元测试。

覆盖：
* 快照标准化（中文列 → 标准列、代码格式）；
* 分层筛选（ST/北交所/低价/低成交额剔除）、TopN、理由/风险标记、研究阶段标注；
* 打分排序与权重校验；
* 历史验证（分组/IC/成本后净值）能跑通且指标完整；
* 不联网：全部使用注入的合成快照与行情。
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from strategy.screener import (
    DEFAULT_SCREEN_WEIGHTS,
    MarketScreener,
    ScreenerConfig,
    normalize_snapshot,
)
from strategy.screener_validation import ValidationConfig, validate_screener

pytestmark = pytest.mark.unit

SYMBOLS = [
    "600000.SH",
    "600036.SH",
    "601318.SH",
    "601899.SH",
    "600487.SH",
    "000001.SZ",
    "000858.SZ",
    "002415.SZ",
    "002594.SZ",
    "300059.SZ",
    "300750.SZ",
    "688981.SH",
    "688111.SH",
    "600519.SH",
    "603259.SH",
]


def _make_bars(symbols: list[str], days: int = 320) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    start = date(2023, 1, 2)
    rows: list[pd.DataFrame] = []
    for offset, symbol in enumerate(symbols):
        dates = [start + timedelta(days=index) for index in range(days)]
        drift = 0.0004 * ((offset % 5) - 2)
        returns = rng.normal(drift, 0.02, days)
        close = 20.0 * np.cumprod(1.0 + returns)
        open_price = close * (1.0 + rng.normal(0, 0.004, days))
        high = np.maximum(open_price, close) * (1.0 + rng.uniform(0, 0.015, days))
        low = np.minimum(open_price, close) * (1.0 - rng.uniform(0, 0.015, days))
        volume = rng.integers(2_000_000, 20_000_000, days).astype("float64")
        rows.append(
            pd.DataFrame(
                {
                    "date": dates,
                    "symbol": symbol,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                    "amount": close * volume,
                }
            )
        )
    return pd.concat(rows, ignore_index=True)


def _make_snapshot(symbols: list[str]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for index, symbol in enumerate(symbols):
        rows.append(
            {
                "代码": symbol.split(".")[0],
                "名称": "测试股" + str(index),
                "最新价": 10.0 + index,
                "涨跌幅": 1.0,
                "成交额": 5e8 + index * 1e8,
                "成交量": 1e7,
                "今开": 10.0,
                "最高": 11.0,
                "最低": 9.8,
                "昨收": 10.0,
            }
        )
    frame = pd.DataFrame(rows)
    # 额外加：北交所、ST、低价、低成交额（应被过滤）
    extra = pd.DataFrame(
        [
            {"代码": "bj920000", "名称": "北交股", "最新价": 10.0, "涨跌幅": 1.0, "成交额": 5e8, "成交量": 1e6},
            {"代码": "600123", "名称": "ST测试", "最新价": 8.0, "涨跌幅": 1.0, "成交额": 5e8, "成交量": 1e6},
            {"代码": "600124", "名称": "低价股", "最新价": 1.5, "涨跌幅": 1.0, "成交额": 5e8, "成交量": 1e6},
            {"代码": "600125", "名称": "低成交", "最新价": 12.0, "涨跌幅": 1.0, "成交额": 1e6, "成交量": 1e5},
        ]
    )
    return pd.concat([frame, extra], ignore_index=True)


def test_normalize_snapshot_maps_chinese_columns() -> None:
    frame = normalize_snapshot(_make_snapshot(SYMBOLS[:3]).iloc[:3])
    assert {"symbol", "name", "price", "amount", "volume", "pct_change"} <= set(frame.columns)
    assert frame["symbol"].iloc[0] == "600000.SH"
    assert all(frame["symbol"].str.endswith((".SH", ".SZ")))


def test_screener_filters_and_picks() -> None:
    snapshot = _make_snapshot(SYMBOLS)
    bars = _make_bars(SYMBOLS)
    config = ScreenerConfig(liquidity_top_n=20, top_n=5, mode="cache")
    result = MarketScreener(config).screen(
        as_of=date(2023, 11, 30), snapshot=snapshot, bars=bars
    )

    assert 0 < len(result.picks) <= 5
    scores = [pick["score"] for pick in result.picks]
    assert scores == sorted(scores, reverse=True), "结果应按得分降序"
    for pick in result.picks:
        assert pick["symbol"].endswith((".SH", ".SZ"))
        assert not pick["symbol"].endswith(".BJ")
        assert "ST" not in pick["name"]
        assert isinstance(pick["reasons"], list) and pick["reasons"]
        assert isinstance(pick["risk_flags"], list)
        assert pick["research_stage"] is True
    assert result.stats["snapshot_total"] >= len(SYMBOLS)
    assert result.stats["after_filter"] >= len(SYMBOLS)
    assert result.stats["scored"] > 0


def test_screener_weight_validation() -> None:
    with pytest.raises(ValueError):
        ScreenerConfig(weights={**DEFAULT_SCREEN_WEIGHTS, "unknown_factor": 1.0})
    with pytest.raises(ValueError):
        ScreenerConfig(mode="realtime")


def test_validation_runs_and_reports_groups() -> None:
    bars = _make_bars(SYMBOLS)
    config = ValidationConfig(
        groups=3, horizons=(5, 10), holding_days=5, min_symbols_per_date=10, min_history=60
    )
    result = validate_screener(bars, config)

    assert result.metrics["symbols"] == len(SYMBOLS)
    assert result.metrics["groups"] == 3
    assert len(result.group_table) == 3
    assert {"mean_ic", "mean_rank_ic", "ic_ir"} <= set(result.metrics["ic"][5])
    assert 5 in result.metrics["long_short"]
    assert "verdict" in result.conclusion and "usable" in result.conclusion
    assert len(result.equity) > 0
    assert result.conclusion["usable"] is False or result.conclusion["verdict"]

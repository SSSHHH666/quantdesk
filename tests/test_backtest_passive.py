"""M6 P1 回归测试：回测报告标准输出（同仓位被动基准 + 样本外标记）。

覆盖验收点（零 mock，全部用真实 akshare 历史数据）：

1. 被动基准与策略**同起点 / 同初始资金 / 同标的列表 / 同单票目标仓位**；
2. 超额（pp）严格等于「策略总收益 − 被动总收益」（数值一致）；
3. JSON / CSV 字段齐全、可回读（中英文字段映射一致）；
4. 同参数两次运行结果**完全可复现**（指标/权益/被动基准/报告逐字段一致）；
5. 单区间回测的样本外字段必须为 ``False`` /“否”，不得冒充 walk-forward OOS；
6. 回归守卫：``scripts/run_backtest.py`` 取行情必须请求不复权真实价
   （``BarCache.load(adjust="none")``），防止再次退回 hfq 复权合成价。

数据源：``runtime/short_term/bars_50.csv``（50 只、2019-01-02~2026-10-23、
source 全 ``akshare``、不复权 OHLCV + adj_factor），测试内注入
``BacktestEngine.run(..., bars=...)``，不联网、不依赖运行时 SQLite。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # 与 tests/conftest.py 同口径
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest.engine import BacktestEngine  # noqa: E402
from backtest.validation import (  # noqa: E402
    OOS_MARKER_SINGLE_INTERVAL,
    PASSIVE_BENCHMARK_METHOD,
    REPORT_CSV_COLUMNS,
    REPORT_JSON_FIELDS,
    build_backtest_report,
    compute_passive_benchmark,
    write_backtest_report,
)

pytestmark = pytest.mark.integration

BARS_CSV = PROJECT_ROOT / "runtime" / "short_term" / "bars_50.csv"
#: 3 只真实标的（主板高价股 + 创业板 + 主板低价股，覆盖最小 1 手/整手两种情形）
SYMBOLS: tuple[str, ...] = ("600519.SH", "300750.SZ", "000725.SZ")
START = date(2025, 1, 2)
END = date(2025, 12, 31)
INITIAL_CASH = 1_000_000.0
SLIPPAGE_BPS = 5.0
FIXED_GENERATED_AT = "2026-10-26T00:00:00+08:00"


# ---------------------------------------------------------------------------
# 夹具（module 级：真实数据只读一次，回测只跑两次）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_bars() -> pd.DataFrame:
    """真实 akshare 不复权行情切片（3 标的 × 2025 年）。"""

    assert BARS_CSV.exists(), f"缺少真实行情文件：{BARS_CSV}"
    frame = pd.read_csv(BARS_CSV, dtype={"symbol": str})
    assert set(frame["source"].dropna().unique()) == {"akshare"}, "行情 source 必须全为 akshare"
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    subset = frame[
        frame["symbol"].isin(SYMBOLS) & (frame["date"] >= START) & (frame["date"] <= END)
    ].copy()
    assert not subset.empty, "切片后没有行情数据"
    missing = set(SYMBOLS) - set(subset["symbol"].unique())
    assert not missing, f"行情缺少标的：{sorted(missing)}"
    return subset


def _run_ma_cross(bars: pd.DataFrame, run_id: str) -> object:
    return BacktestEngine().run(
        "ma_cross",
        {},
        list(SYMBOLS),
        START,
        END,
        initial_cash=INITIAL_CASH,
        slippage_bps=SLIPPAGE_BPS,
        benchmark="",
        bars=bars,
        run_id=run_id,
    )


def _passive_for(result: object, bars: pd.DataFrame) -> object:
    return compute_passive_benchmark(
        bars,
        list(SYMBOLS),
        result.start_date,
        result.end_date,
        initial_cash=INITIAL_CASH,
        position_pct=float((result.params or {}).get("position_pct") or 10.0),
        fee_config=result.fee_config,
        slippage_bps=SLIPPAGE_BPS,
        calendar=result.equity["trade_date"].tolist(),
    )


@pytest.fixture(scope="module")
def strategy_result(real_bars: pd.DataFrame) -> object:
    return _run_ma_cross(real_bars, "m6p1-passive-test")


@pytest.fixture(scope="module")
def passive(strategy_result: object, real_bars: pd.DataFrame) -> object:
    return _passive_for(strategy_result, real_bars)


@pytest.fixture(scope="module")
def report(strategy_result: object, passive: object) -> dict:
    return build_backtest_report(
        strategy_result,
        passive,
        oos=False,
        generated_at=FIXED_GENERATED_AT,
    )


# ---------------------------------------------------------------------------
# ① 同起点 / 同初始资金 / 同标的 / 同仓位
# ---------------------------------------------------------------------------


def test_passive_uses_same_window_cash_symbols_and_position(
    strategy_result: object, passive: object
) -> None:
    assert passive.start_date == strategy_result.start_date
    assert passive.end_date == strategy_result.end_date
    assert passive.initial_cash == pytest.approx(INITIAL_CASH)
    assert strategy_result.initial_cash == pytest.approx(INITIAL_CASH)

    assert set(passive.symbols) == set(SYMBOLS)
    assert set(strategy_result.symbols) == set(SYMBOLS)

    # 单票目标仓位与引擎 default_position_pct（策略参数）一致
    assert passive.position_pct == pytest.approx(
        float(strategy_result.params["position_pct"])
    )
    assert passive.position_pct == pytest.approx(10.0)

    # 同一交易日历：交易日数、首末权益日期与策略完全对齐
    assert passive.n_days == int(strategy_result.metrics["n_days"])
    assert len(passive.equity) == passive.n_days
    assert passive.equity["trade_date"].iloc[0] == strategy_result.start_date
    assert passive.equity["trade_date"].iloc[-1] == strategy_result.end_date
    equity = passive.equity["equity"].astype("float64")
    assert (equity > 0).all()

    # 买入是真实可成交的：有持仓、现金非负、买入成本不超过初始资金
    assert passive.holdings, "被动基准应至少持有 1 只标的"
    assert passive.cash_remaining >= 0.0
    assert passive.position_value + passive.cash_remaining == pytest.approx(
        passive.final_equity, abs=0.01
    )
    assert passive.total_fees > 0.0


# ---------------------------------------------------------------------------
# ② 超额 = 策略 − 被动（数值一致）
# ---------------------------------------------------------------------------


def test_excess_return_equals_strategy_minus_passive(
    strategy_result: object, passive: object, report: dict
) -> None:
    expected_pp = (report["strategy_total_return"] - report["passive_total_return"]) * 100.0
    assert report["excess_return_pp"] == pytest.approx(expected_pp, abs=1e-6)
    # 交叉校验：百分比字段同样自洽
    assert report["strategy_total_return_pct"] - report["passive_total_return_pct"] == pytest.approx(
        report["excess_return_pp"], abs=1e-5
    )
    # 与引擎/被动对象的原始数值一致（不是另算一套）
    assert report["strategy_total_return"] == pytest.approx(
        float(strategy_result.metrics["total_return"])
    )
    assert report["passive_total_return"] == pytest.approx(passive.total_return)
    assert report["strategy_max_drawdown"] == pytest.approx(
        float(strategy_result.metrics["max_drawdown"])
    )
    assert report["passive_max_drawdown"] == pytest.approx(passive.max_drawdown)
    assert report["price_adjust"] == "none"
    assert "price_adjust=none" in report["price_caliber"]


# ---------------------------------------------------------------------------
# ③ JSON / CSV 字段齐全且可回读
# ---------------------------------------------------------------------------


def test_json_and_csv_fields_are_complete_and_reloadable(
    report: dict, tmp_path: Path
) -> None:
    json_path, csv_path = write_backtest_report(report, tmp_path)
    assert json_path.name == f"backtest_report_{report['run_id']}.json"
    assert csv_path.name == f"backtest_report_{report['run_id']}.csv"
    assert json_path.exists() and csv_path.exists()
    assert json_path.stat().st_size > 0 and csv_path.stat().st_size > 0

    # ---- JSON 回读：字段齐全、关键值一致 ----
    loaded = json.loads(json_path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    missing = [key for key in REPORT_JSON_FIELDS if key not in loaded]
    assert not missing, f"JSON 报告缺字段：{missing}"
    assert loaded["run_id"] == report["run_id"]
    assert loaded["strategy"] == "ma_cross"
    assert loaded["symbols"] == list(SYMBOLS)
    assert loaded["start_date"] == START.isoformat()
    assert loaded["end_date"] == END.isoformat()
    assert loaded["initial_cash"] == pytest.approx(INITIAL_CASH)
    assert loaded["excess_return_pp"] == pytest.approx(report["excess_return_pp"])
    assert loaded["oos"] is False
    assert loaded["oos_marker"] == OOS_MARKER_SINGLE_INTERVAL
    assert loaded["passive_method"] == PASSIVE_BENCHMARK_METHOD
    assert "不调仓" in loaded["passive_method"]
    assert "10%" in loaded["passive_method"]
    assert loaded["generated_at"] == FIXED_GENERATED_AT

    # ---- CSV 回读：中文列齐全、单行、值与 JSON 自洽 ----
    csv_frame = pd.read_csv(csv_path, encoding="utf-8-sig", dtype=str)
    assert list(csv_frame.columns) == list(REPORT_CSV_COLUMNS)
    assert len(csv_frame) == 1
    row = csv_frame.iloc[0]
    assert row["策略"] == "ma_cross"
    assert set(row["标的"].split("、")) == set(SYMBOLS)
    assert row["区间"] == f"{START.isoformat()} ~ {END.isoformat()}"
    assert "price_adjust=none" in row["价格口径"]
    assert row["样本外"] == "否"
    assert row["run_id"] == report["run_id"]
    assert float(row["超额pp"]) == pytest.approx(report["excess_return_pp"], abs=1e-3)
    assert row["策略收益"].endswith("%") and row["被动收益"].endswith("%")
    assert row["策略MDD"].endswith("%") and row["被动MDD"].endswith("%")
    assert float(row["初始资金"]) == pytest.approx(INITIAL_CASH)
    assert float(row["期末权益"]) == pytest.approx(report["final_equity"])
    assert float(row["被动期末权益"]) == pytest.approx(report["passive_final_equity"])


# ---------------------------------------------------------------------------
# ④ 同参数两次运行完全可复现
# ---------------------------------------------------------------------------


def test_same_params_are_reproducible(real_bars: pd.DataFrame) -> None:
    first = _run_ma_cross(real_bars, "m6p1-repro")
    second = _run_ma_cross(real_bars, "m6p1-repro")

    for key in ("total_return", "annual_return", "max_drawdown", "sharpe", "final_equity", "n_days"):
        assert first.metrics[key] == second.metrics[key], f"指标 {key} 两次运行不一致"
    assert first.equity.equals(second.equity), "权益曲线两次运行不一致"
    assert (first.trades is None and second.trades is None) or first.trades.equals(
        second.trades
    ), "成交明细两次运行不一致"

    passive_first = _passive_for(first, real_bars)
    passive_second = _passive_for(second, real_bars)
    assert passive_first.total_return == passive_second.total_return
    assert passive_first.max_drawdown == passive_second.max_drawdown
    assert passive_first.final_equity == passive_second.final_equity
    assert passive_first.holdings == passive_second.holdings
    assert passive_first.total_fees == passive_second.total_fees
    assert passive_first.equity.equals(passive_second.equity)

    report_first = build_backtest_report(
        first, passive_first, oos=False, generated_at=FIXED_GENERATED_AT
    )
    report_second = build_backtest_report(
        second, passive_second, oos=False, generated_at=FIXED_GENERATED_AT
    )
    assert report_first == report_second, "同参数两次报告不一致（不可复现）"


# ---------------------------------------------------------------------------
# ⑤ 单区间回测的样本外字段必须为 False/“否”
# ---------------------------------------------------------------------------


def test_single_interval_report_is_not_oos(report: dict) -> None:
    assert report["oos"] is False
    assert report["oos_marker"] == OOS_MARKER_SINGLE_INTERVAL
    assert report["oos_marker"].startswith("否")
    assert "walk-forward" in report["oos_marker"]
    assert "单区间回测" in report["oos_marker"]
    assert "不构成样本外证据" in report["oos_note"]


# ---------------------------------------------------------------------------
# ⑥ 回归守卫：CLI 必须用不复权真实价取数（原 bug：adjust="hfq"）
# ---------------------------------------------------------------------------


def _load_run_backtest_module():
    path = PROJECT_ROOT / "scripts" / "run_backtest.py"
    spec = importlib.util.spec_from_file_location("run_backtest_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_loads_unadjusted_bars(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_run_backtest_module()
    assert module._BACKTEST_ADJUST == "none", "回测撮合口径必须是不复权真实价（price_adjust=none）"

    recorded: dict[str, object] = {}

    class FakeBarCache:
        def load(self, symbols, start, end, adjust="hfq"):
            recorded["adjust"] = adjust
            recorded["symbols"] = list(symbols)
            return pd.DataFrame(
                {
                    "date": [START] * len(symbols),
                    "symbol": list(symbols),
                    "open": [10.0] * len(symbols),
                    "high": [10.0] * len(symbols),
                    "low": [10.0] * len(symbols),
                    "close": [10.0] * len(symbols),
                }
            )

    monkeypatch.setattr("data.cache.BarCache", FakeBarCache)
    frame, source = module._load_bars(list(SYMBOLS), START, END, "cache")

    assert recorded["adjust"] == "none", "run_backtest.py 不得再以 hfq 取回测行情"
    assert len(frame) == len(SYMBOLS)
    assert "不复权" in source
    assert "adjust=none" in source

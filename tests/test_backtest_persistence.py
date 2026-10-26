"""回测结果落库与读回测试（backtest/persistence.py）。

覆盖：
1. ``save_backtest_result`` → ``load_backtest_result`` 往返一致（run_id/策略/参数/标的/区间/
   指标/权益曲线/成交明细/警告）；
2. 幂等：同一 ``run_id`` 重复保存不产生重复行（三表都是替换语义）；
3. 故障容错：``session=None``、损坏 session 时**返回 ok=False + 中文 warnings，不抛异常**
   （写库失败绝不影响本次回测结果）；
4. ``delete_backtest_result`` 三表级联删除；``list_backtest_runs`` 能列出已存结果；
5. 全部在临时 SQLite（``test_db`` 夹具）上跑，不触碰项目 runtime 库。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from backtest.engine import BacktestEngine
from backtest.persistence import (
    delete_backtest_result,
    list_backtest_runs,
    load_backtest_result,
    save_backtest_result,
)
from data.provider import MockProvider

SYMBOL = "600519.SH"
START = dt.date(2024, 1, 2)
END = dt.date(2024, 3, 29)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def backtest_result() -> Any:
    """真实跑一次小回测（MockProvider 确定性数据，无网络）。"""
    engine = BacktestEngine(data_source=MockProvider())
    return engine.run(
        "ma_cross",
        {"fast": 5, "slow": 20},
        symbols=[SYMBOL],
        start=START,
        end=END,
        initial_cash=100000.0,
        benchmark=None,
    )


@pytest.fixture()
def session(test_db):
    """临时 SQLite 会话（用完关闭）。"""
    session = test_db.session()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


# ---------------------------------------------------------------------------
# 一、保存 / 读回往返
# ---------------------------------------------------------------------------


def test_backtest_fixture_has_expected_shape(backtest_result) -> None:
    """前置：回测结果本身可用（后续用例比较的基准）。"""
    assert backtest_result.run_id
    assert len(backtest_result.equity) > 0
    assert "total_return" in backtest_result.metrics
    assert backtest_result.strategy_key == "ma_cross"


def test_save_then_load_roundtrip(session, backtest_result) -> None:
    report = save_backtest_result(session, backtest_result)

    assert report["ok"] is True, report["warnings"]
    assert report["run_id"] == backtest_result.run_id
    assert report["rows"]["runs"] == 1
    assert report["rows"]["equity"] == len(backtest_result.equity)
    assert report["rows"]["trades"] == len(backtest_result.trades)

    loaded = load_backtest_result(session, backtest_result.run_id)
    assert loaded is not None

    # 元数据往返
    assert loaded.run_id == backtest_result.run_id
    assert loaded.strategy_key == backtest_result.strategy_key
    assert loaded.params == backtest_result.params
    assert loaded.symbols == backtest_result.symbols
    assert loaded.start_date == backtest_result.start_date
    assert loaded.end_date == backtest_result.end_date
    assert loaded.initial_cash == pytest.approx(backtest_result.initial_cash)
    assert loaded.warnings == backtest_result.warnings

    # 指标往返（JSON 化后按浮点比较）
    for key in ("total_return", "annual_return", "max_drawdown", "max_drawdown_abs", "sharpe", "trade_count"):
        assert loaded.metrics.get(key) == pytest.approx(backtest_result.metrics.get(key), rel=1e-9, abs=1e-12), (
            f"指标 {key} 往返不一致：{loaded.metrics.get(key)} vs {backtest_result.metrics.get(key)}"
        )

    # 权益曲线 / 成交明细往返
    assert len(loaded.equity) == len(backtest_result.equity)
    assert loaded.equity["equity"].iloc[-1] == pytest.approx(backtest_result.equity["equity"].iloc[-1], abs=1e-6)
    assert len(loaded.trades) == len(backtest_result.trades)
    if len(backtest_result.trades):
        assert loaded.trades["fee_total"].sum() == pytest.approx(
            backtest_result.trades["fee_total"].sum(), abs=1e-6
        )


def test_load_unknown_run_returns_none(session) -> None:
    assert load_backtest_result(session, "no-such-run-id") is None


# ---------------------------------------------------------------------------
# 二、幂等保存
# ---------------------------------------------------------------------------


def test_save_twice_is_idempotent(session, backtest_result, test_db) -> None:
    first = save_backtest_result(session, backtest_result)
    second = save_backtest_result(session, backtest_result)

    assert first["ok"] is True and second["ok"] is True
    # 三表行数不翻倍
    assert test_db.count("backtest_runs") == 1
    assert test_db.count("backtest_equity") == len(backtest_result.equity)
    assert test_db.count("backtest_trades") == len(backtest_result.trades)
    # 仍可正常读回
    assert load_backtest_result(session, backtest_result.run_id) is not None


# ---------------------------------------------------------------------------
# 三、故障容错（不抛异常、不破坏回测结果）
# ---------------------------------------------------------------------------


def test_save_without_session_returns_warning(backtest_result) -> None:
    report = save_backtest_result(None, backtest_result)

    assert report["ok"] is False
    assert report["warnings"] and "未提供数据库会话" in report["warnings"][0]


def test_save_with_broken_session_does_not_raise(backtest_result) -> None:
    """损坏的 session（不可用对象）必须被捕获为中文 warning，而非抛出。"""
    report = save_backtest_result(object(), backtest_result)  # type: ignore[arg-type]

    assert report["ok"] is False
    assert any("保存失败" in warning for warning in report["warnings"]), report["warnings"]


def test_save_none_result_returns_warning(session) -> None:
    report = save_backtest_result(session, None)  # type: ignore[arg-type]

    assert report["ok"] is False
    assert any("回测结果为空" in warning for warning in report["warnings"])


def test_load_without_session_raises_backtest_error(backtest_result) -> None:
    from backtest.engine import BacktestError

    with pytest.raises(BacktestError):
        load_backtest_result(None, backtest_result.run_id)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 四、删除与列表
# ---------------------------------------------------------------------------


def test_delete_removes_all_rows(session, backtest_result, test_db) -> None:
    save_backtest_result(session, backtest_result)

    assert delete_backtest_result(session, backtest_result.run_id) is True

    assert load_backtest_result(session, backtest_result.run_id) is None
    assert test_db.count("backtest_runs") == 0
    assert test_db.count("backtest_equity") == 0
    assert test_db.count("backtest_trades") == 0
    # 再删一次返回 False（不存在）
    assert delete_backtest_result(session, backtest_result.run_id) is False


def test_list_backtest_runs(session, backtest_result) -> None:
    assert list_backtest_runs(session) == []

    save_backtest_result(session, backtest_result)

    runs = list_backtest_runs(session)
    assert len(runs) == 1
    row = runs[0]
    # 契约见 list_backtest_runs docstring（UI 历史记录/对比用精简字段）
    assert row["run_id"] == backtest_result.run_id
    assert row["strategy_key"] == "ma_cross"
    assert row["start_date"] == backtest_result.start_date
    assert row["end_date"] == backtest_result.end_date
    assert row["status"] == "DONE"
    assert row["initial_cash"] == pytest.approx(backtest_result.initial_cash)
    assert row["warnings"] == backtest_result.warnings
    assert row["created_at"] is not None
    # 列表接口应带精简指标，供 UI 历史记录展示
    assert isinstance(row.get("metrics"), dict)
    assert row["metrics"].get("total_return") == pytest.approx(backtest_result.metrics["total_return"], rel=1e-9)
    # 备注（已报 P2 建议）：backtest_runs.universe_json 已落库，但 list 接口未回吐 symbols，
    # UI 历史列表无法显示“标的池”；此处不对缺失字段做断言，避免把缺陷固化成语义。

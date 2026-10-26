"""新策略与推荐引擎契约测试（value_quality / breakout_abnormal / ml_ranker / recommend）。

被测：``strategy/recommend.py``（Recommendation / recommend / recommend_dataframe）与
``strategy/builtin/*`` 注册表。覆盖 Lead 要求：
- 推荐输出**结构**（symbol/score/reason/rank/qualified/as_of）；
- **理由字段**（reason 非空中文 + factors/components 得分分解）；
- **排序确定性**（同输入两次调用完全一致、rank 升序、score 非增）；
- **无未来数据泄漏**：给 recommend 传入「含 as_of 之后数据」的完整 bars，
  结果必须与只传截至 as_of 的 bars 完全一致（证明只使用当日及之前数据）；
- 回测层 lookahead 检查：截断区间 vs 全区间，重叠部分权益/成交必须一致（对所有已注册策略生效）。

未落地的策略（当前 ml_ranker）用 ``pytest.skip`` 明确标注，落地后自动生效。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import numpy as np
import pandas as pd
import pytest

from backtest.engine import BacktestEngine
from data.provider import MockProvider
from strategy import list_strategies, recommend as recommend_module

SYMBOL = "600519.SH"
UNIVERSE = ["600519.SH", "000001.SZ", "300750.SZ"]
START = dt.date(2023, 1, 3)
AS_OF = dt.date(2024, 6, 28)
CUT_END = dt.date(2024, 3, 29)
FULL_END = dt.date(2024, 6, 28)

NEW_STRATEGY_KEYS: tuple[str, ...] = ("value_quality", "breakout_abnormal", "ml_ranker")


def _registered_keys() -> set[str]:
    return {str(item["key"]) for item in list_strategies()}


def _mock_bars(symbols: list[str], end: dt.date = dt.date(2025, 6, 30)) -> Any:
    """MockProvider 确定性日线（无网络）。"""
    return MockProvider().get_bars(symbols, START, end)


def _recommend(strategy_key: str, *, bars: Any = None, as_of: dt.date = AS_OF, top_n: int = 3) -> Any:
    return recommend_module.recommend(
        strategy_key,
        UNIVERSE,
        as_of,
        top_n=top_n,
        bars=_mock_bars(UNIVERSE) if bars is None else bars,
    )


# ---------------------------------------------------------------------------
# 一、新策略注册与回测可运行
# ---------------------------------------------------------------------------


def test_new_strategies_registered() -> None:
    """3 个新策略必须已注册（value_quality / breakout_abnormal / ml_ranker，均已交付）。"""
    registered = _registered_keys()
    missing = [key for key in NEW_STRATEGY_KEYS if key not in registered]

    assert not missing, f"新策略未注册：{missing}；当前注册表={sorted(registered)}"


@pytest.mark.parametrize("strategy_key", NEW_STRATEGY_KEYS)
def test_new_strategy_registered_and_scorable(strategy_key: str) -> None:
    """新策略必须：已注册 + 有参数 schema + 能被推荐引擎打分。"""
    assert strategy_key in _registered_keys(), f"策略 {strategy_key} 未注册"

    entry = next(item for item in list_strategies() if item["key"] == strategy_key)
    assert entry.get("display_name"), f"{strategy_key} 缺少中文展示名"
    assert entry.get("params_schema") is not None, f"{strategy_key} 缺少参数 schema"

    rows = _recommend(strategy_key)
    assert rows, f"{strategy_key} 在 MockProvider 数据上未产出任何推荐"


# ---------------------------------------------------------------------------
# 二、推荐输出结构与理由字段
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strategy_key", ["value_quality", "breakout_abnormal", "multi_factor"])
def test_recommendation_structure_and_reason(strategy_key: str) -> None:
    rows = _recommend(strategy_key, top_n=3)

    assert isinstance(rows, list) and rows, f"{strategy_key} 推荐结果为空"
    assert len(rows) <= 3, "返回条数不应超过 top_n"

    for index, item in enumerate(rows):
        payload = item.as_dict()
        assert payload["symbol"] in UNIVERSE
        assert payload["strategy_key"] == strategy_key
        assert payload["rank"] == index + 1, "rank 必须是 1..n 的连续序号"
        assert isinstance(payload["score"], float)
        assert isinstance(payload["qualified"], bool)
        # 理由字段：中文非空，供 UI「推荐理由（得分分解）」直接展示
        assert payload["reason"].strip(), f"{strategy_key} 推荐缺少理由"
        assert any("\u4e00" <= ch <= "\u9fff" for ch in payload["reason"]), "理由必须是中文"
        # 得分分解：factors/components 至少一个有内容（UI 可视化用）
        assert payload["factors"] or payload["components"], "缺少得分分解（factors/components 全空）"
        assert payload["as_of"], "缺少 as_of（推荐必须标明数据截止日）"


def test_recommendation_is_sorted_by_score() -> None:
    rows = _recommend("value_quality", top_n=5)
    scores = [item.score for item in rows]

    assert scores == sorted(scores, reverse=True), f"推荐未按 score 降序排列：{scores}"


def test_recommendation_is_deterministic() -> None:
    """同输入两次调用结果必须完全一致（UI 排序不能抖动）。"""
    first = [item.as_dict() for item in _recommend("value_quality")]
    second = [item.as_dict() for item in _recommend("value_quality")]

    assert first == second


def test_recommend_rejects_empty_universe() -> None:
    with pytest.raises(ValueError) as excinfo:
        recommend_module.recommend("value_quality", [], AS_OF, bars=_mock_bars(UNIVERSE))
    assert "自选池" in str(excinfo.value) or "universe" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 三、无未来数据泄漏（核心）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strategy_key", ["value_quality", "breakout_abnormal", "multi_factor"])
def test_recommend_uses_no_future_data(strategy_key: str) -> None:
    """传入「含 as_of 之后行情」的完整 bars 与「截至 as_of」的 bars，推荐结果必须完全一致。"""
    full_bars = _mock_bars(UNIVERSE)  # 含 2024-06-28 之后的数据
    truncated = full_bars[pd.to_datetime(full_bars["date"]).dt.date <= AS_OF].copy()  # 只用 <= as_of
    assert pd.to_datetime(truncated["date"]).dt.date.max() <= AS_OF

    with_future = [item.as_dict() for item in _recommend(strategy_key, bars=full_bars)]
    without_future = [item.as_dict() for item in _recommend(strategy_key, bars=truncated)]

    assert with_future == without_future, (
        f"{strategy_key} 推荐结果受 as_of 之后的数据影响（存在未来数据泄漏）"
    )


def test_recommend_as_of_controls_snapshot() -> None:
    """不同 as_of 应产出不同 as_of 字段（证明按日期切片而非用最新价）。"""
    early = _recommend("multi_factor", as_of=dt.date(2024, 3, 29))
    late = _recommend("multi_factor", as_of=dt.date(2024, 6, 28))

    assert early and late
    assert early[0].as_of <= late[0].as_of
    assert early[0].as_of.isoformat() != late[0].as_of.isoformat() or early[0].as_of != late[0].as_of


# ---------------------------------------------------------------------------
# 四、回测层 lookahead 检查（对所有已注册策略生效）
# ---------------------------------------------------------------------------


def test_no_lookahead_for_all_registered_strategies() -> None:
    """截断区间与全区间回测在重叠部分的权益/成交必须完全一致（无 lookahead）。"""
    engine_data = MockProvider()
    keys = sorted(_registered_keys())
    assert keys, "策略注册表为空"

    failures: list[str] = []
    for key in keys:
        try:
            truncated = BacktestEngine(data_source=engine_data).run(
                key, None, symbols=[SYMBOL], start=START, end=CUT_END, initial_cash=100000.0, benchmark=None
            )
            full = BacktestEngine(data_source=engine_data).run(
                key, None, symbols=[SYMBOL], start=START, end=FULL_END, initial_cash=100000.0, benchmark=None
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{key}: 回测抛异常 {type(exc).__name__}: {exc}")
            continue

        overlap = full.equity[full.equity["trade_date"] <= CUT_END].reset_index(drop=True)
        baseline = truncated.equity.reset_index(drop=True)
        if len(overlap) != len(baseline):
            failures.append(f"{key}: 重叠区间长度不一致 {len(overlap)} vs {len(baseline)}")
            continue
        for column in ("equity", "cash", "position_value"):
            if not np.allclose(
                overlap[column].to_numpy(dtype="float64"),
                baseline[column].to_numpy(dtype="float64"),
                rtol=0,
                atol=1e-6,
            ):
                failures.append(f"{key}: 列 {column} 在重叠区间分叉（疑似使用未来数据）")
                break

        if "trade_date" in getattr(full.trades, "columns", []):
            full_prefix = full.trades[full.trades["trade_date"] <= CUT_END]
            if len(full_prefix) != len(truncated.trades):
                failures.append(
                    f"{key}: 重叠区间成交数不一致 {len(full_prefix)} vs {len(truncated.trades)}"
                )

    assert not failures, "未来数据泄漏检查失败：\n" + "\n".join(failures)

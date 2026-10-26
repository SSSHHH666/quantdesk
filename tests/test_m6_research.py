"""M6 研究框架独立 QA 回归测试（M6 task-3）。

设计原则
--------
1. **独立于原作者注释**：所有断言都用真实数据复算的金标准或结构不变量，不读代码注释下结论。
2. **反未来函数**：
   - 截断不变性（金标准）：把 bars 截断到 T 重建面板，共同 (date,symbol) 且 date≤T 的
     ``FEATURES_ALL`` 必须逐元素相等（atol=1e-9）——任何用到 T 之后信息的特征都会破坏它；
   - 标签尾部：每只标的最后 h 行 ``future_ret_h`` / ``label_*`` 必须 NaN、``label_date`` NaT；
   - 复权标签：``future_ret_h == adjclose.shift(-h)/adjclose - 1``（adjclose=close×adj_factor）；
   - purge / embargo：每个 fold 满足 ``max(train label_date) < test_start`` 且
     ``max(train date) <= 测试开始前第 5 个交易日``（元数据由 M6Fold 暴露）。
3. **可复现**：同一配置两次运行的 metrics 与 folds 元数据完全一致。
4. **门槛行为**：gate_check 的 ``usable`` 与 ``criteria`` 全 True 严格等价；多项未过时逐项列出；
   "绝对收益>0 但相对基准超额<0" 必须 usable=False。
5. **数据来源**：source 必须全为 akshare（fail-closed，无 mock）；bars_200 就绪后追加抽查
   与 5 只抽样截断不变性。

运行（Windows 本机 DSH 沙箱必须用包装器，原因见 runtime/_run_pytest.py docstring）::

    .venv\\Scripts\\python.exe runtime\\_run_pytest.py tests/test_m6_research.py -q
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# 本机 DSH 沙箱禁止管道，sklearn/HistGradientBoosting 的 joblib 线程池会报 WinError 5；
# 必须在 import strategy.m6_research（进而 import sklearn）之前收敛为单线程。
os.environ.setdefault("OMP_NUM_THREADS", "1")

import numpy as np
import pandas as pd
import pytest

from strategy.factors import normalize_bars
from strategy.m6_research import (
    FEATURES_ALL,
    MIN_CROSS_SECTION,
    M6_LABELS,
    M6Config,
    M6Result,
    build_m6_panel,
    gate_check,
    label_summary,
    load_index_bars,
    run_m6_config,
)

ROOT = Path(__file__).resolve().parents[1]
BARS_50 = ROOT / "runtime" / "short_term" / "bars_50.csv"
BARS_200 = ROOT / "runtime" / "short_term" / "bars_200.csv"
BUILD_REPORT_200 = ROOT / "runtime" / "short_term" / "build_report_200.json"
UNIVERSE_200 = ROOT / "data" / "universe_200.json"
TRUNCATION_DATE = pd.Timestamp("2024-06-28")
SMALL_END = pd.Timestamp("2022-06-30")
ATOL = 1e-9
PERF_BUDGET_SECONDS = 120.0

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# 工具 / 夹具
# ---------------------------------------------------------------------------


def _read_bars(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"]).sort_values(["symbol", "date"]).reset_index(drop=True)
    return frame


def _to_ts(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce")


def _small_config(**overrides) -> M6Config:
    kwargs: dict = dict(
        label="ret10_ge2",
        feature_set="v1",
        model="logistic",
        train_window=120,
        test_window=40,
        min_train_rows=300,
    )
    kwargs.update(overrides)
    return M6Config(**kwargs)


def _compare_frames(full: pd.DataFrame, trunc: pd.DataFrame) -> None:
    """断言两个面板在共同 (date,symbol) 上的 FEATURES_ALL 逐元素相等。"""

    left = full.copy()
    left["_ts"] = _to_ts(left["date"])
    left = left[left["_ts"] <= TRUNCATION_DATE]
    right = trunc.copy()
    right["_ts"] = _to_ts(right["date"])
    merged = left.merge(right, on=["date", "symbol"], how="inner", suffixes=("__full", "__trunc"))
    assert len(merged) == len(left), (
        f"截断后共同样本 {len(merged)} != 全量 date≤T 样本 {len(left)}（特征列可能受未来数据影响）"
    )
    for feature in FEATURES_ALL:
        a = merged[f"{feature}__full"].to_numpy(dtype="float64")
        b = merged[f"{feature}__trunc"].to_numpy(dtype="float64")
        both_nan = np.isnan(a) & np.isnan(b)
        bad = ~(both_nan | (np.abs(a - b) <= ATOL))
        assert not bad.any(), (
            f"截断不变性被破坏：特征 {feature} 有 {int(bad.sum())} 行 "
            f"差异（max={np.nanmax(np.abs(a - b)):.3e}）→ 可能使用了 T 之后的信息"
        )


def _cross_section_label_from_bars(bars: pd.DataFrame, spec) -> pd.DataFrame:
    """独立金标准：用原始行情（close×adj_factor）逐日截面分位复算 cross_section_top 标签。"""

    frame = bars.copy()
    frame["_adj"] = (
        pd.to_numeric(frame["close"], errors="coerce")
        * pd.to_numeric(frame["adj_factor"], errors="coerce")
    )
    grouped = frame.groupby("symbol", sort=False)
    frame["_fwd"] = grouped["_adj"].shift(-spec.horizon) / frame["_adj"] - 1.0
    valid = frame["_fwd"].notna()
    ranks = frame.loc[valid].groupby("date")["_fwd"].rank(pct=True)
    counts = frame.loc[valid].groupby("date")["_fwd"].transform("count")
    labels = pd.Series(np.nan, index=frame.index)
    # 与实现契约一致：分位严格 > quantile（50 只截面时正好 Top20%），且当日截面数 ≥ 最小要求
    top = (ranks > spec.quantile) & (counts >= MIN_CROSS_SECTION)
    labels.loc[top.index] = top.astype(float)
    return pd.DataFrame(
        {
            "symbol": frame["symbol"].to_numpy(),
            "_ts": pd.to_datetime(frame["date"], errors="coerce").to_numpy(),
            "label": labels.to_numpy(dtype="float64"),
        }
    )


def _universe_symbols(path: Path) -> set[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload
    if isinstance(payload, dict):
        for key in ("symbols", "stocks", "universe", "items", "data"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
        else:  # dict of symbol -> info
            return {str(key) for key in payload}
    symbols: set[str] = set()
    for item in rows:
        if isinstance(item, str):
            symbols.add(item)
        elif isinstance(item, dict):
            for key in ("symbol", "code", "ts_code", "symbol_code"):
                if key in item:
                    symbols.add(str(item[key]))
                    break
    return symbols


@pytest.fixture(scope="module")
def bars50() -> pd.DataFrame:
    if not BARS_50.exists():
        pytest.skip(f"缺少真实行情 {BARS_50}（M6 QA 需要 bars_50.csv）")
    return _read_bars(BARS_50)


@pytest.fixture(scope="module")
def index_bars() -> pd.DataFrame | None:
    return load_index_bars()


@pytest.fixture(scope="module")
def full_panel(bars50: pd.DataFrame, index_bars: pd.DataFrame | None):
    started = time.perf_counter()
    panel = build_m6_panel(bars50, index_bars=index_bars)
    elapsed = time.perf_counter() - started
    return panel, elapsed


@pytest.fixture(scope="module")
def truncated_panels(bars50: pd.DataFrame, index_bars: pd.DataFrame | None):
    """bars-only 截断 / bars+index 同步截断 两个面板（各构建一次）。"""

    bars_trunc = bars50[bars50["date"] <= TRUNCATION_DATE].copy()
    index_trunc = None
    if index_bars is not None:
        index_trunc = index_bars[_to_ts(index_bars["date"]) <= TRUNCATION_DATE].copy()
    return build_m6_panel(bars_trunc), build_m6_panel(bars_trunc, index_bars=index_trunc)


@pytest.fixture(scope="module")
def small_panel(full_panel) -> pd.DataFrame:
    panel, _ = full_panel
    return panel[_to_ts(panel["date"]) <= SMALL_END].copy()


@pytest.fixture(scope="module")
def run_result(small_panel: pd.DataFrame, bars50: pd.DataFrame) -> M6Result:
    return run_m6_config(small_panel, _small_config(), passive_bars=bars50, with_stability=False)


# ---------------------------------------------------------------------------
# 1) 构建契约与性能守卫
# ---------------------------------------------------------------------------


def test_panel_build_performance_and_contract(full_panel, bars50: pd.DataFrame) -> None:
    panel, elapsed = full_panel
    assert elapsed < PERF_BUDGET_SECONDS, f"build_m6_panel(bars_50) 耗时 {elapsed:.1f}s ≥ 120s"
    assert not panel.empty
    assert panel["symbol"].nunique() == bars50["symbol"].nunique()
    missing = [name for name in FEATURES_ALL if name not in panel.columns]
    assert missing == [], f"面板缺少 FEATURES_ALL 列：{missing}"
    assert panel["date"].nunique() >= 1000
    # 复权口径与 adj_factor 覆盖率（静默用 1.0 兜底会污染复权标签）
    assert panel.attrs.get("price_basis") == "adjclose"
    assert panel.attrs.get("adj_factor_filled") == 0


def test_feature_list_has_no_future_or_label_columns(full_panel) -> None:
    panel, _ = full_panel
    forbidden = [name for name in FEATURES_ALL if "future" in name.lower() or name.startswith("label")]
    assert forbidden == [], f"FEATURES_ALL 不应包含未来收益/标签列：{forbidden}"
    # 标签/标签日列存在，但只用于监督与 purge，不进入特征
    for horizon in (1, 3, 5, 10):
        assert f"future_ret_{horizon}" in panel.columns
        assert f"label_date_{horizon}" in panel.columns
    for key in M6_LABELS:
        assert f"label_{key}" in panel.columns
        assert f"label_date_{key}" in panel.columns


# ---------------------------------------------------------------------------
# 2) 截断不变性（无未来函数金标准）
# ---------------------------------------------------------------------------


def test_truncation_invariance_all_features(full_panel, truncated_panels) -> None:
    panel, _ = full_panel
    bars_only, bars_and_index = truncated_panels
    _compare_frames(panel, bars_only)
    _compare_frames(panel, bars_and_index)


def test_truncated_panel_loses_no_common_rows(full_panel, truncated_panels) -> None:
    """date≤T 的样本不能因为截断而消失（特征若依赖未来数据会被 NaN 过滤掉）。"""

    panel, _ = full_panel
    full_rows = int((_to_ts(panel["date"]) <= TRUNCATION_DATE).sum())
    for truncated in truncated_panels:
        rows = int((_to_ts(truncated["date"]) <= TRUNCATION_DATE).sum())
        assert rows == full_rows


# ---------------------------------------------------------------------------
# 3) 标签正确性（复权总收益 / 标签日 / 尾部）
# ---------------------------------------------------------------------------


def test_future_returns_match_adjusted_close(full_panel, bars50: pd.DataFrame) -> None:
    panel, _ = full_panel
    frame = bars50.copy()
    frame["_adj"] = (
        pd.to_numeric(frame["close"], errors="coerce")
        * pd.to_numeric(frame["adj_factor"], errors="coerce")
    )
    grouped = frame.groupby("symbol", sort=False)
    panel_meta = pd.DataFrame(
        {"symbol": panel["symbol"].to_numpy(), "_ts": _to_ts(panel["date"]).to_numpy()}
    )
    for horizon in (1, 3, 5, 10):
        expected = pd.DataFrame(
            {
                "symbol": frame["symbol"].to_numpy(),
                "_ts": frame["date"].to_numpy(),
                "exp_ret": (grouped["_adj"].shift(-horizon) / frame["_adj"] - 1.0).to_numpy(),
                "exp_label_date": grouped["date"].shift(-horizon).to_numpy(),
            }
        )
        merged = panel_meta.merge(expected, on=["symbol", "_ts"], how="left", validate="one_to_one")
        actual = panel[f"future_ret_{horizon}"].to_numpy(dtype="float64")
        expected_ret = merged["exp_ret"].to_numpy(dtype="float64")
        assert np.array_equal(np.isnan(actual), np.isnan(expected_ret)), (
            f"h={horizon}: future_ret 的 NaN 位置与 adjclose 金标准不一致（疑似未复权/越界取值）"
        )
        valid = ~np.isnan(actual)
        assert int(valid.sum()) > 80_000
        max_diff = float(np.abs(actual[valid] - expected_ret[valid]).max())
        assert max_diff <= ATOL, f"h={horizon}: future_ret 与 adjclose 口径最大偏差 {max_diff:.3e}"

        actual_label_date = pd.to_datetime(panel[f"label_date_{horizon}"], errors="coerce").to_numpy()
        expected_label_date = pd.to_datetime(merged["exp_label_date"], errors="coerce").to_numpy()
        actual_nat = pd.isna(actual_label_date)
        expected_nat = pd.isna(expected_label_date)
        assert np.array_equal(actual_nat, expected_nat), f"h={horizon}: label_date NaT 位置不一致"
        assert np.array_equal(
            actual_label_date[~actual_nat], expected_label_date[~expected_nat]
        ), f"h={horizon}: label_date != date.shift(-h)"


def test_label_tail_is_nan(full_panel) -> None:
    """数据末端最后 h 行没有标签（不能用"数据之外的未来"凑标签）。"""

    panel, _ = full_panel
    frame = panel.copy()
    frame["_ts"] = _to_ts(frame["date"])
    frame = frame.sort_values(["symbol", "_ts"])
    violations: list[str] = []
    for symbol, group in frame.groupby("symbol", sort=True):
        for key, spec in M6_LABELS.items():
            tail = group.tail(max(1, spec.horizon))
            if pd.to_numeric(tail[f"label_{key}"], errors="coerce").notna().any():
                violations.append(f"{symbol}/{key}: label 非 NaN")
            if _to_ts(tail[f"label_date_{key}"]).notna().any():
                violations.append(f"{symbol}/{key}: label_date 非 NaT")
    for horizon in (1, 3, 5, 10):
        for symbol, group in frame.groupby("symbol", sort=True):
            tail = group.tail(horizon)
            if pd.to_numeric(tail[f"future_ret_{horizon}"], errors="coerce").notna().any():
                violations.append(f"{symbol}/future_ret_{horizon}: 尾部非 NaN")
    assert violations == [], f"标签尾部泄漏：{violations[:4]}"


def test_label_columns_follow_returns(full_panel, bars50: pd.DataFrame) -> None:
    panel, _ = full_panel
    for key, spec in M6_LABELS.items():
        label = pd.to_numeric(panel[f"label_{key}"], errors="coerce").to_numpy(dtype="float64")
        if key == "gap_up_1d":
            target = pd.to_numeric(panel["next_gap"], errors="coerce").to_numpy(dtype="float64")
            expected = np.where(np.isnan(target), np.nan, (target >= spec.threshold).astype(float))
        elif spec.kind == "binary":
            target = pd.to_numeric(panel[f"future_ret_{spec.horizon}"], errors="coerce").to_numpy(dtype="float64")
            expected = np.where(np.isnan(target), np.nan, (target >= spec.threshold).astype(float))
        elif spec.kind == "three_class":
            target = pd.to_numeric(panel[f"future_ret_{spec.horizon}"], errors="coerce").to_numpy(dtype="float64")
            expected = np.where(
                np.isnan(target),
                np.nan,
                np.where(target > spec.threshold, 1.0, np.where(target < -spec.threshold, -1.0, 0.0)),
            )
        else:  # cross_section_top：逐日截面分位 Top20%（用全样本行情独立复算，含被特征过滤的行）
            gold = _cross_section_label_from_bars(bars50, spec)
            meta = pd.DataFrame(
                {"symbol": panel["symbol"].to_numpy(), "_ts": _to_ts(panel["date"]).to_numpy()}
            ).merge(gold, on=["symbol", "_ts"], how="left", validate="one_to_one")
            expected = meta["label"].to_numpy(dtype="float64")
        assert np.array_equal(np.isnan(label), np.isnan(expected)), f"{key}: label NaN 位置不符"
        assert np.array_equal(label[~np.isnan(label)], expected[~np.isnan(expected)]), (
            f"{key}: label 取值与目标收益/阈值/分位不符"
        )

    # 截面 Top 标签占比审计：每个有效截面应恰好约 Top20%（不是全样本分位）
    cs = pd.DataFrame(
        {
            "date": panel["date"],
            "label": pd.to_numeric(panel["label_cs_top20_5d"], errors="coerce"),
            "target": pd.to_numeric(panel["future_ret_5"], errors="coerce"),
        }
    ).dropna()
    shares = cs.groupby("date")["label"].mean()
    counts = cs.groupby("date")["label"].size()
    valid_dates = counts >= MIN_CROSS_SECTION
    assert valid_dates.any()
    assert (shares[valid_dates] > 0.10).all() and (shares[valid_dates] < 0.30).all(), (
        f"截面 Top20% 标签占比异常：{shares[valid_dates].describe().to_dict()}"
    )
    assert abs(float(shares[valid_dates].median()) - (1.0 - M6_LABELS["cs_top20_5d"].quantile)) <= 0.03, (
        "截面 Top 标签中位数占比应接近 Top20%"
    )


def test_label_summary_matches_panel(full_panel) -> None:
    panel, _ = full_panel
    summary = {item["label"]: item for item in label_summary(panel)}
    assert set(summary) == set(M6_LABELS)
    for key, spec in M6_LABELS.items():
        item = summary[key]
        values = pd.to_numeric(panel[f"label_{key}"], errors="coerce").dropna()
        assert item["samples"] == len(values)
        assert item["purge_days"] == max(5, spec.horizon)
        if spec.kind in ("binary", "cross_section_top"):
            assert item["positive_rate"] == round(float((values == 1.0).mean()), 6)
        else:
            total = item["positive_rate"] + item["down_rate"] + item["flat_rate"]
            assert abs(total - 1.0) <= 1e-6
        assert "label_date_max" in item


# ---------------------------------------------------------------------------
# 4) purge / embargo / 折 OOS
# ---------------------------------------------------------------------------


def test_fold_purge_and_embargo(run_result: M6Result, small_panel: pd.DataFrame) -> None:
    result = run_result
    assert len(result.folds) >= 5
    all_dates = sorted(_to_ts(small_panel["date"]).dt.date.unique())
    embargo = int(result.config.embargo)
    for fold in result.folds:
        assert fold.train_label_max is not None, "M6Fold 未暴露 train_label_max（task-2 G）"
        assert fold.train_date_max is not None
        test_start = pd.Timestamp(fold.test_start).date()
        # purge：训练标签必须全部结算于测试开始之前
        assert pd.Timestamp(fold.train_label_max).date() < test_start, (
            f"fold {fold.fold}: train_label_max={fold.train_label_max} 未早于 test_start={test_start}"
        )
        # embargo：训练特征日 <= 测试开始前第 embargo 个交易日
        order = all_dates.index(test_start)
        embargo_limit = all_dates[max(0, order - embargo)]
        assert pd.Timestamp(fold.embargo_date).date() == embargo_limit, (
            f"fold {fold.fold}: embargo_date={fold.embargo_date} != {embargo_limit}"
        )
        assert pd.Timestamp(fold.train_date_max).date() <= embargo_limit, (
            f"fold {fold.fold}: train_date_max={fold.train_date_max} 落入 embargo 区（> {embargo_limit}）"
        )
        # 窗口方向与 purge 天数
        assert pd.Timestamp(fold.train_start).date() < pd.Timestamp(fold.train_end).date() < test_start
        assert pd.Timestamp(fold.test_start).date() <= pd.Timestamp(fold.test_end).date()
        assert fold.n_train > 0 and fold.n_test >= 20
        assert fold.purge_days == max(5, M6_LABELS[result.config.label].horizon)


def test_fold_test_windows_are_out_of_sample(run_result: M6Result) -> None:
    predictions = run_result.predictions
    stamps = _to_ts(predictions["date"])
    assert not predictions.duplicated(["date", "symbol"]).any(), "不同 fold 的测试窗口出现重叠预测"
    covered = set()
    for fold in run_result.folds:
        start = pd.Timestamp(fold.test_start)
        end = pd.Timestamp(fold.test_end)
        subset = predictions[stamps == stamps]  # keep dtype
        fold_rows = predictions[predictions["fold"] == fold.fold]
        fold_stamps = _to_ts(fold_rows["date"])
        assert len(fold_rows) > 0
        assert fold_stamps.min() == start and fold_stamps.max() == end
        assert (fold_stamps >= start).all() and (fold_stamps <= end).all()
        assert not (set(fold_stamps.dt.date.unique()) & covered), f"fold {fold.fold} 测试日与之前折重叠"
        covered |= set(fold_stamps.dt.date.unique())
    assert min(covered) >= pd.Timestamp(run_result.folds[0].test_start).date()


def test_fold_stats_use_per_date_topk(run_result: M6Result) -> None:
    """分折命中率/期望必须由“逐日 top-k 再按日聚合”得到。

    回归保护：旧实现对整个折（约 1000 个 stock-day）直接取 top-5，实测会把分折 t 从
    +1.63 翻转为 -0.36（同一份数据）；该口径与日度/组合口径必须一致。
    """

    predictions = run_result.predictions
    config = run_result.config
    assert config.top_n >= 1
    for fold in run_result.folds:
        subset = predictions[predictions["fold"] == fold.fold].dropna(subset=["target_return"])
        hits: list[float] = []
        bases: list[float] = []
        rets: list[float] = []
        for _, group in subset.groupby("date"):
            ordered = group.sort_values("score", ascending=False)
            top = ordered.head(config.top_n)
            top_labels = pd.to_numeric(top["label"], errors="coerce").dropna()
            base_labels = pd.to_numeric(group["label"], errors="coerce").dropna()
            if len(top_labels):
                hits.append(float((top_labels == 1.0).mean()))
            if len(base_labels):
                bases.append(float((base_labels == 1.0).mean()))
            rets.append(float(top["target_return"].mean()))
        assert hits, f"fold {fold.fold}: 无逐日 top-k 命中样本"
        assert abs(fold.hit_rate - round(float(np.mean(hits)), 6)) <= 1e-6, (
            f"fold {fold.fold}: hit_rate={fold.hit_rate} 与逐日 top-k 聚合 {np.mean(hits):.6f} 不符"
        )
        assert abs(fold.base_rate - round(float(np.mean(bases)), 6)) <= 1e-6
        assert abs(
            fold.net_expectancy - round(float(np.mean(rets)) - config.round_trip_cost, 6)
        ) <= 1e-6, f"fold {fold.fold}: net_expectancy 未按逐日 top-k 计算"


# ---------------------------------------------------------------------------
# 5) 可复现性
# ---------------------------------------------------------------------------


def test_same_config_is_reproducible(small_panel: pd.DataFrame, bars50: pd.DataFrame, run_result: M6Result) -> None:
    again = run_m6_config(small_panel, _small_config(), passive_bars=bars50, with_stability=False)
    for key in (
        "rank_ic",
        "hit_rate",
        "base_rate",
        "hit_rate_lift",
        "net_expectancy",
        "top_excess_vs_passive",
        "long_short_spread",
        "folds",
        "oos_samples",
    ):
        assert run_result.metrics.get(key) == again.metrics.get(key), f"指标 {key} 两次运行不一致"
    assert [fold.as_dict() for fold in run_result.folds] == [fold.as_dict() for fold in again.folds]
    assert run_result.passive.get("total_return") == again.passive.get("total_return")


# ---------------------------------------------------------------------------
# 6) 被动基准对齐
# ---------------------------------------------------------------------------


def test_three_class_hit_rate_is_count_based(small_panel: pd.DataFrame, bars50: pd.DataFrame) -> None:
    """三分类标签的命中率必须按 ``label == 1`` 计数，而不是对 label 求和。

    回归保护：three_class 标签为 -1/0/1，若用 ``sum()`` 会把 -1 抵消（实测 bars_50 上
    hit_rate 会从 38.2% 变成 2.6%）；该错误会影响 hit_rate/hit_lift/二项 p/gate 命中率门槛。
    """

    config = _small_config(label="three_class_5d")
    result = run_m6_config(small_panel, config, passive_bars=bars50, with_stability=False)
    hit = result.metrics["hit_rate"]
    base = result.metrics["base_rate"]
    assert hit is not None and 0.0 <= hit <= 1.0, f"three_class hit_rate 非法：{hit}"
    assert base is not None and 0.0 <= base <= 1.0
    # 从 predictions 独立复算：逐日 top-k 中 label==1 的占比（合并样本）
    predictions = result.predictions.dropna(subset=["target_return"])
    successes = 0
    trials = 0
    for _, group in predictions.groupby("date"):
        top = group.sort_values("score", ascending=False).head(config.top_n)
        labeled = pd.to_numeric(top["label"], errors="coerce").dropna()
        successes += int((labeled == 1.0).sum())
        trials += int(len(labeled))
    assert trials > 100
    assert abs(hit - round(successes / trials, 6)) <= 1e-6, (
        f"three_class hit_rate={hit} 与独立计数 {successes}/{trials}={successes / trials:.6f} 不符"
    )
    assert abs(result.metrics["hit_rate_lift"] - round(hit - base, 6)) <= 1e-6
    # F8 回归：命中率 p 值不能因标签取值为 -1/0/1 而退化为 None（非退化样本下必须可计算）
    p_value = result.metrics["hit_rate_binom_p"]
    assert p_value is not None and 0.0 <= p_value <= 1.0, f"three_class hit_rate_binom_p 非法：{p_value}"
    for fold in result.folds:
        assert fold.hit_rate is not None and 0.0 <= fold.hit_rate <= 1.0, (
            f"fold {fold.fold} hit_rate 非法：{fold.hit_rate}"
        )
        assert fold.base_rate is not None and 0.0 <= fold.base_rate <= 1.0


def test_passive_benchmark_aligned_with_oos(run_result: M6Result) -> None:
    passive = run_result.passive
    assert passive, "passive_oos_benchmark 未返回结果"
    predictions = run_result.predictions
    stamps = _to_ts(predictions["date"])
    expected_start = str(stamps.min().date())
    expected_end = str(stamps.max().date())
    assert passive["start"] == expected_start == run_result.metrics["oos_start"]
    assert passive["end"] == expected_end == run_result.metrics["oos_end"]
    # 标的集合必须等于 OOS 内实际出现过预测的标的集合（不得混入未来才上市/未参与 OOS 的标的）
    assert passive["symbols"] == predictions["symbol"].nunique(), (
        f"被动基准 symbols={passive['symbols']} != OOS 预测 symbols={predictions['symbol'].nunique()}"
    )
    assert passive["symbols"] <= predictions["symbol"].nunique()
    horizon = M6_LABELS[run_result.config.label].horizon
    assert passive["window_matched"]["holding_days"] == horizon
    assert passive["total_return"] is not None and passive["max_drawdown"] is not None


# ---------------------------------------------------------------------------
# 7) gate_check 门槛行为
# ---------------------------------------------------------------------------


def _fake_result(config: M6Config, **metrics) -> M6Result:
    base: dict = dict(
        rank_ic=0.05,
        hit_rate=0.50,
        base_rate=0.45,
        hit_rate_lift=0.05,
        hit_rate_p=0.01,
        hit_rate_binom_p=0.01,
        top_excess_vs_passive=0.01,
        net_expectancy=0.01,
        hit_rate_t=2.5,
    )
    base.update(metrics)
    result = M6Result(config=config)
    result.metrics = base
    return result


def test_gate_check_usable_iff_all_criteria_passed() -> None:
    config = _small_config()
    cases = {
        "pass": {},
        "all_fail": dict(rank_ic=0.01, top_excess_vs_passive=-0.02, hit_rate_p=0.5, hit_rate_binom_p=0.5),
        "low_rank_ic": dict(rank_ic=0.02),
        "negative_excess": dict(top_excess_vs_passive=-0.001),
        "insignificant_hit": dict(hit_rate_p=0.4, hit_rate_binom_p=0.4, hit_rate_lift=0.001),
    }
    for name, metrics in cases.items():
        verdict = gate_check(_fake_result(config, **metrics))
        assert verdict["usable"] == all(verdict["criteria"].values()), f"{name}: usable 与 criteria 不等价"
        assert verdict["failed"] == [
            key for key, passed in verdict["criteria"].items() if not passed
        ], f"{name}: failed 列表与 criteria 不一致"
        if verdict["usable"]:
            assert verdict["failed"] == []
        else:
            assert verdict["failed"], f"{name}: 未通过却没有列出未过项"
            for key in verdict["failed"]:
                assert key in verdict["note"], f"{name}: note 未写清未过项 {key}"


def test_gate_check_reports_multiple_failed_items() -> None:
    verdict = gate_check(
        _fake_result(
            _small_config(),
            rank_ic=0.001,
            top_excess_vs_passive=-0.03,
            hit_rate_p=0.9,
            hit_rate_binom_p=0.9,
            hit_rate_lift=-0.01,
        )
    )
    assert verdict["usable"] is False
    assert len(verdict["failed"]) >= 2, "多项不达标时必须同时列出，不能只报一个"
    assert "rank_ic_gt_0.03" in verdict["failed"]
    assert "top_excess_gt_0" in verdict["failed"]


def test_gate_check_rejects_positive_absolute_but_negative_excess() -> None:
    """绝对收益为正但相对被动/全池基准超额为负 → 不得 usable。"""

    verdict = gate_check(
        _fake_result(
            _small_config(),
            rank_ic=0.05,
            hit_rate_p=0.01,
            hit_rate_binom_p=0.01,
            hit_rate_lift=0.05,
            net_expectancy=0.03,  # 绝对 Top 收益为正
            top_excess_vs_passive=-0.02,  # 相对基准为负
        )
    )
    assert verdict["usable"] is False
    assert verdict["failed"] == ["top_excess_gt_0"]


def test_gate_check_accepts_passing_fake_result() -> None:
    verdict = gate_check(_fake_result(_small_config()))
    assert verdict["usable"] is True
    assert all(verdict["criteria"].values())
    assert verdict["failed"] == []


# ---------------------------------------------------------------------------
# 8) 数据来源（akshare / 无 mock）
# ---------------------------------------------------------------------------


def test_bars_50_source_is_akshare_and_ingestible(bars50: pd.DataFrame) -> None:
    sources = set(bars50["source"].astype(str).str.strip().unique())
    assert sources == {"akshare"}, f"bars_50 source 非纯 akshare：{sources}"
    assert not bars50["source"].astype(str).str.contains("mock", case=False).any()
    assert pd.to_numeric(bars50["adj_factor"], errors="coerce").notna().all(), "adj_factor 存在缺失"
    assert not bars50.duplicated(["date", "symbol"]).any(), "(date,symbol) 存在重复"
    assert bars50["symbol"].nunique() == 50
    assert len(bars50) == 89_000
    assert bars50["date"].min() <= pd.Timestamp("2019-01-02")
    assert bars50["date"].max() >= pd.Timestamp("2026-10-16")
    # 下游 normalize_bars 可直接摄入且保留 adj_factor（M6 复权依赖）
    normalized = normalize_bars(bars50)
    assert len(normalized) == len(bars50)
    assert "adj_factor" in normalized.columns


# ---------------------------------------------------------------------------
# 9) bars_200 就绪后的抽查（未就绪自动 skip）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bars200() -> pd.DataFrame:
    if not BARS_200.exists():
        pytest.skip("bars_200.csv 尚未就绪（data-engineer task-1 产出后自动启用）")
    if not UNIVERSE_200.exists():
        pytest.skip("bars_200.csv 存在但 data/universe_200.json 未就绪，等待 task-1 全量构建")
    # 区分 smoke / 部分产物与最终全量产物：以构建报告里的 picked 数为准
    if BUILD_REPORT_200.exists():
        try:
            report = json.loads(BUILD_REPORT_200.read_text(encoding="utf-8"))
        except Exception:  # pragma: no cover - 报告写入中
            pytest.skip("build_report_200.json 不可解析（构建进行中）")
        picked = report.get("picked")
        if isinstance(picked, int) and picked < 180:
            pytest.skip(f"bars_200 当前为 smoke/部分产物（picked={picked}<180），等待 task-1 全量构建")
    return _read_bars(BARS_200)


def test_bars_200_quality_audit(bars200: pd.DataFrame) -> None:
    sources = set(bars200["source"].astype(str).str.strip().unique())
    assert sources == {"akshare"}, f"bars_200 source 非纯 akshare：{sources}"
    assert not bars200["source"].astype(str).str.contains("mock", case=False).any()
    assert pd.to_numeric(bars200["adj_factor"], errors="coerce").notna().all(), "bars_200 adj_factor 缺失"
    assert not bars200.duplicated(["date", "symbol"]).any(), "bars_200 (date,symbol) 重复"
    assert bars200["symbol"].nunique() >= 180, f"bars_200 标的数 {bars200['symbol'].nunique()} < 180"
    # 行数自洽：每标的至少 800 根，末根 bar ≥ 2026-10-16
    counts = bars200.groupby("symbol").size()
    assert int(counts.min()) >= 800, f"存在标的行数 <800：{counts[counts < 800].to_dict()}"
    last_dates = bars200.groupby("symbol")["date"].max()
    stale = last_dates[last_dates < pd.Timestamp("2026-10-16")]
    assert stale.empty, f"存在末根 bar <2026-10-16 的标的：{stale.to_dict()}"
    assert bars200["date"].min() <= pd.Timestamp("2019-01-02")
    # 与 universe_200.json 一致
    assert UNIVERSE_200.exists(), "缺少 data/universe_200.json"
    universe = _universe_symbols(UNIVERSE_200)
    assert universe, "universe_200.json 未解析出标的"
    assert set(bars200["symbol"].astype(str)) == universe, (
        f"bars_200 与 universe_200.json 标的不一致：多 {sorted(set(bars200['symbol']) - universe)[:5]}"
        f" 少 {sorted(universe - set(bars200['symbol']))[:5]}"
    )
    # 行数自洽：总行数 = Σ 每标的行数（结构校验）
    assert len(bars200) == int(counts.sum())
    # 行完整性：OHLC 必须为正、OHL/量价关系合理（停牌/坏行不许以 0 价混入）
    numeric = {column: pd.to_numeric(bars200[column], errors="coerce") for column in
               ("open", "high", "low", "close", "volume", "amount")}
    assert all(series.notna().all() for series in numeric.values()), "bars_200 存在非数值 OHLCV"
    bad_price = bars200[
        (numeric["open"] <= 0) | (numeric["high"] <= 0) | (numeric["low"] <= 0) | (numeric["close"] <= 0)
    ]
    assert bad_price.empty, (
        f"bars_200 存在 OHLC≤0 的坏行 {len(bad_price)} 条："
        f"{bad_price[['date', 'symbol']].head(5).to_dict('records')}"
    )
    assert (numeric["high"] >= numeric["low"]).all(), "存在 high < low 的坏行"
    assert (numeric["high"] >= numeric["close"]).all(), "存在 high < close 的坏行"
    assert (numeric["low"] <= numeric["close"]).all(), "存在 low > close 的坏行"
    assert (numeric["volume"] >= 0).all() and (numeric["amount"] >= 0).all(), "存在负成交量/成交额"


def test_bars_200_sample_truncation_invariance(bars200: pd.DataFrame, index_bars) -> None:
    """200 只数据抽样复跑截断不变性，并核对复权（实际 hfq 因子）标签公式。

    说明：框架契约要求当日截面 ≥ ``MIN_CROSS_SECTION`` 才计算截面特征/标签，
    因此 5 只子集无法建面（预期行为，不是 bug）。这里取 40 只（最小可建截面）作为
    抽样样本，其中前 5 只作为重点核对对象，控制时长。
    """

    all_symbols = sorted(bars200["symbol"].astype(str).unique())
    focus_symbols = all_symbols[:5]
    build_symbols = all_symbols[: max(MIN_CROSS_SECTION, 40)]
    sample = bars200[bars200["symbol"].astype(str).isin(build_symbols)].copy()
    index_trunc = None
    if index_bars is not None:
        index_trunc = index_bars[_to_ts(index_bars["date"]) <= TRUNCATION_DATE].copy()
    full = build_m6_panel(sample, index_bars=index_bars)
    trunc = build_m6_panel(sample[sample["date"] <= TRUNCATION_DATE].copy(), index_bars=index_trunc)
    assert not full.empty and not trunc.empty
    _compare_frames(full, trunc)  # 抽样集全体（≥40 只）截断不变性

    # 复权标签公式核对（实际数据 adj_factor 为 hfq_factor；公式与 qfq/hfq 无关，只看 close×factor）
    focus = full[full["symbol"].astype(str).isin(focus_symbols)].copy()
    assert len(focus) > 0
    frame = sample[sample["symbol"].astype(str).isin(focus_symbols)].copy()
    frame["_adj"] = (
        pd.to_numeric(frame["close"], errors="coerce")
        * pd.to_numeric(frame["adj_factor"], errors="coerce")
    )
    grouped = frame.groupby("symbol", sort=False)
    meta = pd.DataFrame({"symbol": focus["symbol"].to_numpy(), "_ts": _to_ts(focus["date"]).to_numpy()})
    for horizon in (1, 3, 5, 10):
        expected = pd.DataFrame(
            {
                "symbol": frame["symbol"].to_numpy(),
                "_ts": frame["date"].to_numpy(),
                "exp_ret": (grouped["_adj"].shift(-horizon) / frame["_adj"] - 1.0).to_numpy(),
                "exp_label_date": grouped["date"].shift(-horizon).to_numpy(),
            }
        )
        merged = meta.merge(expected, on=["symbol", "_ts"], how="left", validate="one_to_one")
        actual = focus[f"future_ret_{horizon}"].to_numpy(dtype="float64")
        exp_ret = merged["exp_ret"].to_numpy(dtype="float64")
        assert np.array_equal(np.isnan(actual), np.isnan(exp_ret)), f"h={horizon}: NaN 位置不符"
        valid = ~np.isnan(actual)
        assert int(valid.sum()) > 1_000
        assert float(np.abs(actual[valid] - exp_ret[valid]).max()) <= ATOL, f"h={horizon}: 复权标签不符"
        actual_ld = pd.to_datetime(focus[f"label_date_{horizon}"], errors="coerce").to_numpy()
        exp_ld = pd.to_datetime(merged["exp_label_date"], errors="coerce").to_numpy()
        assert np.array_equal(pd.isna(actual_ld), pd.isna(exp_ld)), f"h={horizon}: label_date 位置不符"
        assert np.array_equal(actual_ld[~pd.isna(actual_ld)], exp_ld[~pd.isna(exp_ld)])

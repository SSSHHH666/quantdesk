#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M6 实验矩阵：6 标签 × 2 特征集 × 3 模型 = 36 组，严格 walk-forward OOS。

用法::

    # 冒烟（bars_50，少量配置，<3 分钟）
    .venv\\Scripts\\python.exe scripts\\m6_research.py --quick

    # 全矩阵（单进程；日志同时写 runtime/m6/matrix_run.log）
    .venv\\Scripts\\python.exe scripts\\m6_research.py --log-file runtime/m6/matrix_run.log

    # 分片跑（两个后台进程，按标签分组；输出 shards/<name>/）
    .venv\\Scripts\\python.exe scripts\\m6_research.py --labels ret3_ge2,ret10_ge2,three_class_5d \\
        --shard-dir runtime/m6/shards/s1 --log-file runtime/m6/matrix_run_s1.log
    .venv\\Scripts\\python.exe scripts\\m6_research.py --labels ret5_ge2,cs_top20_5d,gap_up_1d \\
        --shard-dir runtime/m6/shards/s2 --log-file runtime/m6/matrix_run_s2.log

    # 合并分片 → 最终产出（含最佳配置统计检验）
    .venv\\Scripts\\python.exe scripts\\m6_research.py --merge \\
        --shard-dir runtime/m6/shards --log-file runtime/m6/matrix_merge.log

产出（runtime/m6/）：
* ``m6_experiments.json``：data/configs/matrix/best/gate/label_summary/多重比较/运行时长
* ``m6_matrix.csv`` / ``m6_matrix.md``：全部 36 组 OOS 指标（负结果原样保留）
* ``m6_gate_summary.md``：逐配置门槛判定与未过项
* ``m6_oos_predictions_best.csv`` / ``m6_equity_best.csv`` / ``m6_feature_ic_best.csv``
* ``m6_bootstrap_best.json``：最佳配置 5 日块 bootstrap 95% CI
* ``matrix_run*.log``：运行日志
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# 本机 DSH Windows 沙箱禁止进程间管道：sklearn/joblib 的多线程后端会创建
# multiprocessing 管道并抛 PermissionError。固定单线程 OpenMP 既规避该限制，
# 也让 36 组矩阵结果完全可复现（可用 DSH_M6_THREADS 环境变量覆盖）。
_THREADS = os.environ.get("DSH_M6_THREADS", "1")
for _name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_name, _THREADS)

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy.m6_research import (  # noqa: E402
    FEATURES_ALL,
    FEATURES_V1,
    M6_LABELS,
    PRICE_BASIS,
    M6Config,
    M6Result,
    block_bootstrap_ci,
    build_m6_panel,
    feature_rank_ic_table,
    gate_check,
    label_summary,
    load_index_bars,
    multiple_comparison_analysis,
    run_m6_config,
)

OUT_DIR = ROOT / "runtime" / "m6"


# ---------------------------------------------------------------------------
# 日志 Tee
# ---------------------------------------------------------------------------


class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data: str) -> int:
        for stream in self.streams:
            try:
                stream.write(data)
                stream.flush()
            except Exception:
                pass
        return len(data)

    def flush(self) -> None:
        for stream in self.streams:
            try:
                stream.flush()
            except Exception:
                pass


def _setup_log(path: str | None) -> None:
    if not path:
        return
    log_path = Path(path)
    if not log_path.is_absolute():
        log_path = ROOT / log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("a", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, handle)
    sys.stderr = _Tee(sys.__stderr__, handle)
    print(f"[日志] 追加写入 {log_path}")


def _log(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------------------
# 数据/配置
# ---------------------------------------------------------------------------


def _load_bars(path: Path) -> pd.DataFrame:
    bars = pd.read_csv(path)
    bars["date"] = pd.to_datetime(bars["date"], errors="coerce").dt.date
    return bars.dropna(subset=["date"])


def _data_meta(bars: pd.DataFrame, panel: pd.DataFrame, index_bars, bars_path: Path) -> dict[str, Any]:
    import hashlib

    try:
        digest = hashlib.sha256(bars_path.read_bytes()).hexdigest()
    except Exception:  # pragma: no cover
        digest = None
    panel_year = panel.assign(_year=pd.to_datetime(panel["date"]).dt.year)
    by_year = {
        int(year): {"dates": int(group["date"].nunique()), "rows": int(len(group))}
        for year, group in panel_year.groupby("_year")
    }
    return {
        "bars": str(bars_path),
        "bars_sha256": digest,
        "symbols": int(bars["symbol"].nunique()),
        "rows": int(len(bars)),
        "start": str(bars["date"].min()),
        "end": str(bars["date"].max()),
        "index": str(ROOT / "runtime" / "m6" / "index_000300.csv") if index_bars is not None else None,
        "market_source": panel.attrs.get("market_source"),
        "price_basis": panel.attrs.get("price_basis", PRICE_BASIS),
        "adj_factor_filled": int(panel.attrs.get("adj_factor_filled", 0)),
        "panel_rows": int(len(panel)),
        "panel_dates": int(panel["date"].nunique()),
        "panel_start": str(min(panel["date"])),
        "panel_end": str(max(panel["date"])),
        "panel_by_year": by_year,
        "source_values": sorted({str(value) for value in bars["source"].unique()}) if "source" in bars.columns else [],
        "cross_section_audit": panel.attrs.get("cross_section_audit", {}),
        "feature_price_basis": panel.attrs.get("feature_price_basis", {}),
    }


def _matrix_row(result: M6Result) -> dict[str, Any]:
    metrics = result.metrics
    equity_metrics = result.equity.attrs.get("metrics", {}) if result.equity is not None else {}
    gross_metrics = result.equity.attrs.get("gross_metrics", {}) if result.equity is not None else {}
    passive = result.passive or {}
    window_matched = passive.get("window_matched") or {}
    gate = gate_check(result)
    total_cost = result.equity.attrs.get("total_cost") if result.equity is not None else None
    mean_turnover = result.equity.attrs.get("mean_turnover") if result.equity is not None else None
    return {
        "label": metrics.get("label"),
        "feature_set": metrics.get("feature_set"),
        "model": metrics.get("model"),
        "holding_days": metrics.get("holding_days"),
        "folds": metrics.get("folds"),
        "oos_samples": metrics.get("oos_samples"),
        "oos_dates": metrics.get("oos_dates"),
        "hit_rate": metrics.get("hit_rate"),
        "base_rate": metrics.get("base_rate"),
        "hit_lift": metrics.get("hit_rate_lift"),
        "hit_p": metrics.get("hit_rate_p"),
        "hit_p_overlap": metrics.get("hit_rate_p_overlap"),
        "hit_t": metrics.get("hit_rate_t"),
        "hit_fold_p": metrics.get("hit_rate_fold_t_test_p"),
        "net_expectancy": metrics.get("net_expectancy"),
        "top_excess_gross": metrics.get("top_excess_gross"),
        "top_excess_net": metrics.get("top_excess_net"),
        "top_excess_t": metrics.get("top_excess_t"),
        "excess_windows": metrics.get("excess_windows"),
        "rank_ic": metrics.get("rank_ic"),
        "ic_ir": metrics.get("ic_ir"),
        "ic_t": metrics.get("ic_t"),
        "ic_p": metrics.get("ic_p_greater"),
        "positive_ic_ratio": metrics.get("positive_ic_ratio"),
        "ic_direction_consistency": metrics.get("ic_direction_consistency"),
        "long_short_spread": metrics.get("long_short_spread"),
        "long_short_t": metrics.get("long_short_t"),
        "positive_folds_pct": metrics.get("positive_folds_pct"),
        "portfolio_total_return": equity_metrics.get("total_return"),
        "portfolio_annual_return": equity_metrics.get("annual_return"),
        "portfolio_max_drawdown": equity_metrics.get("max_drawdown"),
        "portfolio_sharpe": equity_metrics.get("sharpe"),
        "portfolio_gross_total_return": gross_metrics.get("total_return"),
        "portfolio_total_cost": round(float(total_cost), 6) if total_cost is not None else None,
        "portfolio_mean_turnover": round(float(mean_turnover), 6) if mean_turnover is not None else None,
        "passive_total_return": passive.get("total_return"),
        "passive_annual_return": passive.get("annual_return"),
        "passive_max_drawdown": passive.get("max_drawdown"),
        "passive_sharpe": passive.get("sharpe"),
        "passive_window_total_return": window_matched.get("total_return"),
        "passive_window_max_drawdown": window_matched.get("max_drawdown"),
        "passive_symbols": passive.get("symbols"),
        "gate_usable": bool(gate["usable"]),
        "gate_failed": ",".join(gate.get("failed", [])),
    }


def _diagnostics_key(config: M6Config) -> str:
    return f"{config.label}|{config.feature_set}|{config.model}"


# ---------------------------------------------------------------------------
# 单进程矩阵 / 分片
# ---------------------------------------------------------------------------


def _run_configs(
    panel: pd.DataFrame,
    bars: pd.DataFrame,
    configs: list[M6Config],
    *,
    out_dir: Path,
    data_meta: dict[str, Any],
    label_rows: list[dict[str, Any]],
    started_at: float,
) -> dict[str, Any]:
    matrix: list[dict[str, Any]] = []
    metrics_all: dict[str, Any] = {}
    diagnostics: dict[str, list[float]] = {}
    folds_all: dict[str, list[dict[str, Any]]] = {}
    failures: list[dict[str, Any]] = []
    ok = 0
    for position, config in enumerate(configs, start=1):
        key = _diagnostics_key(config)
        config_started = time.time()
        try:
            result = run_m6_config(
                panel, config, passive_bars=bars, with_stability=False, keep_predictions=False
            )
            row = _matrix_row(result)
            matrix.append(row)
            metrics_all[key] = result.metrics
            diagnostics[key] = list(result.diagnostics.get("daily_ic", []))
            folds_all[key] = [fold.as_dict() for fold in result.folds]
            ok += 1
            _log(
                f"  [OK {position}/{len(configs)}] {config.label:<14} {config.feature_set:<3} {config.model:<9} "
                f"folds={row['folds']} hit={row['hit_rate']} base={row['base_rate']} p={row['hit_p']} "
                f"excess={row['top_excess_net']} rankIC={row['rank_ic']} gate={row['gate_usable']} "
                f"({time.time() - config_started:.1f}s)"
            )
        except Exception as exc:  # noqa: BLE001
            failures.append({"config": config.as_dict(), "error": f"{type(exc).__name__}: {exc}"})
            _log(f"  [FAIL {position}/{len(configs)}] {key}: {type(exc).__name__}: {exc}")
    frame = pd.DataFrame(matrix)
    frame.to_csv(out_dir / "m6_matrix.csv", index=False, encoding="utf-8-sig")
    payload = {
        "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "phase": "shard",
        "duration_sec": round(time.time() - started_at, 1),
        "data": data_meta,
        "label_summary": label_rows,
        "configs": [config.as_dict() for config in configs],
        "matrix": matrix,
        "metrics": metrics_all,
        "diagnostics": {key: [round(value, 6) for value in values] for key, values in diagnostics.items()},
        "folds": folds_all,
        "failures": failures,
        "features": {"v1": list(FEATURES_V1), "all": list(FEATURES_ALL)},
        "ok_configs": ok,
        "notes": [
            "严格 OOS：训练 label_date < test_start 且 train date <= 测试前第 embargo 个交易日",
            "标签/特征：adjclose=close×adj_factor（总收益）；不复权价仅对照",
            "组合持有期 = 标签 horizon，非重叠再平衡，成本 23bp × 换手率",
        ],
    }
    (out_dir / "m6_experiments.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )
    return payload


# ---------------------------------------------------------------------------
# 合并 + 最佳配置统计检验
# ---------------------------------------------------------------------------


def _file_fingerprint(path: Path) -> dict[str, Any]:
    import hashlib
    from datetime import datetime

    info: dict[str, Any] = {"path": str(path), "exists": path.exists()}
    try:
        data = path.read_bytes()
        info["sha256"] = hashlib.sha256(data).hexdigest()
        info["bytes"] = len(data)
        info["mtime"] = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    except Exception:  # pragma: no cover
        info["sha256"] = None
    return info


def _merge(shard_dir: Path, out_dir: Path, args) -> int:
    shard_files = sorted(shard_dir.glob("*/m6_experiments.json"))
    if not shard_files:
        _log(f"[错误] 未找到分片结果：{shard_dir}/*/m6_experiments.json")
        return 2
    _log(f"[合并] 读取 {len(shard_files)} 个分片：{[str(path.parent.name) for path in shard_files]}")
    matrix: list[dict[str, Any]] = []
    metrics_all: dict[str, dict[str, Any]] = {}
    diagnostics: dict[str, list[float]] = {}
    folds_all: dict[str, list[dict[str, Any]]] = {}
    failures: list[dict[str, Any]] = []
    label_rows: list[dict[str, Any]] = []
    data_meta: dict[str, Any] = {}
    duration = 0.0
    for path in shard_files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        matrix.extend(payload.get("matrix", []))
        metrics_all.update(payload.get("metrics", {}) or {})
        diagnostics.update(payload.get("diagnostics", {}) or {})
        folds_all.update(payload.get("folds", {}) or {})
        failures.extend(payload.get("failures", []))
        if not label_rows:
            label_rows = payload.get("label_summary", [])
        if not data_meta:
            data_meta = payload.get("data", {})
        duration += float(payload.get("duration_sec", 0.0) or 0.0)
    _log(f"[合并] 配置 {len(matrix)} 组，失败 {len(failures)}，分片耗时合计 {duration:.1f}s")

    # 用 metrics 复原 M6Result 以便统一 gate_check
    results: list[M6Result] = []
    for row in matrix:
        key = f"{row['label']}|{row['feature_set']}|{row['model']}"
        metrics = metrics_all.get(key)
        if not metrics:
            continue
        config = M6Config(
            label=row["label"], feature_set=row["feature_set"], model=row["model"]
        )
        results.append(M6Result(config=config, metrics=metrics))
    gates = {_diagnostics_key(result.config): gate_check(result) for result in results}
    for row in matrix:
        key = f"{row['label']}|{row['feature_set']}|{row['model']}"
        gate = gates.get(key)
        if gate:
            row["gate_usable"] = bool(gate["usable"])
            row["gate_failed"] = ",".join(gate.get("failed", []))

    def _rank_ic(item: M6Result) -> float:
        value = item.metrics.get("rank_ic")
        return float(value) if value is not None else -9.0

    ranked = sorted(results, key=_rank_ic, reverse=True)
    usable = [item for item in ranked if gates[_diagnostics_key(item.config)]["usable"]]
    best = usable[0] if usable else (ranked[0] if ranked else None)
    if best is None:
        _log("[错误] 合并后没有可用配置")
        return 2
    _log(
        f"[最佳] {_diagnostics_key(best.config)} usable={gates[_diagnostics_key(best.config)]['usable']} "
        f"rankIC={best.metrics.get('rank_ic')}（通过门槛的配置数={len(usable)}）"
    )

    # 重建面板 → 最佳配置补算特征稳定性 / bootstrap / 最佳明细
    bars_path = Path(data_meta.get("bars") or (ROOT / "runtime" / "short_term" / "bars_50.csv"))
    bars = _load_bars(bars_path)
    index_bars = load_index_bars(Path(args.index)) if Path(args.index).exists() else None
    panel = build_m6_panel(bars, index_bars=index_bars)
    if not label_rows:
        label_rows = label_summary(panel)
    panel_year = panel.assign(_year=pd.to_datetime(panel["date"]).dt.year)
    data_meta.update(
        {
            "panel_rows": int(len(panel)),
            "panel_dates": int(panel["date"].nunique()),
            "panel_start": str(min(panel["date"])),
            "panel_end": str(max(panel["date"])),
            "market_source": panel.attrs.get("market_source"),
            "price_basis": panel.attrs.get("price_basis", PRICE_BASIS),
            "adj_factor_filled": int(panel.attrs.get("adj_factor_filled", 0)),
            "panel_by_year": {
                int(year): {"dates": int(group["date"].nunique()), "rows": int(len(group))}
                for year, group in panel_year.groupby("_year")
            },
            "cross_section_audit": panel.attrs.get("cross_section_audit", {}),
            "feature_price_basis": panel.attrs.get("feature_price_basis", {}),
        }
    )
    # 代码指纹：36 组结果必须能自证“同一份代码跑出来”
    data_meta["code_sha256"] = {
        "strategy/m6_research.py": _file_fingerprint(ROOT / "strategy" / "m6_research.py"),
        "scripts/m6_research.py": _file_fingerprint(Path(__file__).resolve()),
    }
    if not data_meta.get("bars_sha256"):
        data_meta["bars_sha256"] = _file_fingerprint(bars_path).get("sha256")
    data_meta["shard_code_versions"] = {
        "s1": "strategy/m6_research.py sha A421EE53…（23:15:20 起跑，F8 修复前）",
        "s2": "strategy/m6_research.py sha A421EE53…（23:17:54 起跑，F8 修复前）",
        "s3": "strategy/m6_research.py sha A95E7F34…（23:20:57 重启，F8 修复后）",
        "note": (
            "F8 修复= _daily_stats.top_hits 由 label 求和改为 label==1 计数，仅影响 three_class "
            "（-1/0/1）的 hit_rate/hit_p；二分类/0-1 标签语义等价（QA 独立 replay 15/15 metrics 一致）。"
            "three_class 全部在 s3 用修复后代码重跑；merge 阶段统一复算 gate/passive/多重比较。"
        ),
    }

    best_result = run_m6_config(
        panel, best.config, passive_bars=bars, with_stability=True, keep_predictions=True
    )
    best_gate = gate_check(best_result)
    daily_ic = best_result.diagnostics.get("daily_ic", [])
    bootstrap = block_bootstrap_ci(daily_ic, block=5, n_boot=args.bootstrap, seed=42)
    multiple = multiple_comparison_analysis(
        diagnostics, best.metrics.get("rank_ic"), block=5, n_boot=min(args.bootstrap, 1000), seed=42
    )

    # 落盘最佳配置明细
    predictions = best_result.predictions.copy()
    if not predictions.empty:
        predictions.to_csv(out_dir / "m6_oos_predictions_best.csv", index=False, encoding="utf-8-sig")
    best_result.equity.to_csv(out_dir / "m6_equity_best.csv", index=False, encoding="utf-8-sig")
    feature_ic = best_result.feature_ic.copy()
    if feature_ic.empty:
        feature_ic = pd.DataFrame(columns=["fold", "feature", "ic"])
    stability = best_result.feature_stability or {}
    top_table = pd.DataFrame(stability.get("top_features", []))
    if not top_table.empty:
        top_table.to_csv(out_dir / "m6_feature_ic_best.csv", index=False, encoding="utf-8-sig")
    else:
        feature_ic.to_csv(out_dir / "m6_feature_ic_best.csv", index=False, encoding="utf-8-sig")
    (out_dir / "m6_bootstrap_best.json").write_text(
        json.dumps(
            {
                "config": best_result.config.as_dict(),
                "bootstrap_rank_ic": bootstrap,
                "multiple_comparison": multiple,
                "folds": [fold.as_dict() for fold in best_result.folds],
            },
            ensure_ascii=False,
            indent=1,
            default=str,
        ),
        encoding="utf-8",
    )

    gate_summary = {
        key: {
            "usable": gate["usable"],
            "criteria": gate["criteria"],
            "failed": gate["failed"],
            "detail": gate["detail"],
            "rank_ic": gate["detail"]["rank_ic"],
            "top_excess_vs_passive": gate["detail"]["top_excess_vs_passive"],
            "hit_rate": gate["detail"]["hit_rate"],
            "base_rate": gate["detail"]["base_rate"],
            "hit_rate_p": gate["detail"]["hit_rate_p"],
        }
        for key, gate in sorted(gates.items())
    }

    # 模型无关的单因子 RankIC（32 特征 × ret5/ret10，全部面板 OOS 日度 IC）
    feature_ic_all = feature_rank_ic_table(panel, horizons=(5, 10))
    if not feature_ic_all.empty:
        feature_ic_all.to_csv(out_dir / "m6_feature_ic_all.csv", index=False, encoding="utf-8-sig")
        _log(
            "[单因子] ret5 最强 |RankIC|："
            + str(
                feature_ic_all[feature_ic_all["horizon"] == 5]
                .head(3)[["feature", "rank_ic", "ic_t", "positive_ic_ratio"]]
                .to_dict(orient="records")
            )
        )
        _log(
            "[单因子] ret10 最强 |RankIC|："
            + str(
                feature_ic_all[feature_ic_all["horizon"] == 10]
                .head(3)[["feature", "rank_ic", "ic_t", "positive_ic_ratio"]]
                .to_dict(orient="records")
            )
        )

    payload = {
        "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "phase": "final",
        "duration_sec": round(time.time() - args.started_at + duration, 1),
        "shard_duration_sec": round(duration, 1),
        "data": data_meta,
        "label_summary": label_rows,
        "features": {"v1": list(FEATURES_V1), "all": list(FEATURES_ALL)},
        "configs": [item.config.as_dict() for item in results],
        "matrix": matrix,
        "failures": failures,
        "gate_summary": gate_summary,
        "n_usable_configs": len(usable),
        "feature_ic_all": feature_ic_all.to_dict(orient="records") if not feature_ic_all.empty else [],
        "best_config": best_result.config.as_dict(),
        "best_selected_by": "gate 判定（RankIC>0.03 且 Top 扣费超额>0 且 命中率显著高于基础概率）；无通过者时取 RankIC 最高并标注未通过",
        "best": {
            "metrics": best_result.metrics,
            "passive": best_result.passive,
            "feature_stability": best_result.feature_stability,
            "gate": best_gate,
            "bootstrap_rank_ic": bootstrap,
            "strata": best_result.metrics.get("strata"),
            "folds": [fold.as_dict() for fold in best_result.folds],
            "notes": best_result.notes,
        },
        "multiple_comparison": multiple,
        "historical_baseline": {
            "short_term_v1": {
                "description": "短线 v1：3 只股票、2020-2026、48 折 OOS（docs/SHORT_TERM_MODEL_V1.md）",
                "hit_rate": 0.2483,
                "base_rate": 0.2483,
                "net_expectancy": -0.0022,
                "max_drawdown": -0.3241,
                "usable": False,
            },
            "screener_validation_50": {
                "description": "全市场筛选器 50 只、2020-2026、5/10/20 日 RankIC（docs/SCREENER_VALIDATION.md）",
                "rank_ic_5d": -0.029,
                "rank_ic_10d": -0.030,
                "rank_ic_20d": -0.037,
                "top_excess_significant": False,
            },
            "comparison_note": (
                "本轮为扩样本(200)+复权总收益标签+持有期对齐+严格 purge 后的再检验；"
                "历史基线的命中率/基础率与标签定义不同，只作方向性对照，不能直接相减。"
            ),
            "this_round": {
                "n_configs": len(results),
                "n_usable": len(usable),
                "best_rank_ic": best.metrics.get("rank_ic"),
                "best_hit_rate": best.metrics.get("hit_rate"),
                "best_base_rate": best.metrics.get("base_rate"),
                "best_top_excess_vs_passive": best.metrics.get("top_excess_vs_passive"),
                "verdict_changed_vs_baseline": bool(len(usable) > 0),
            },
        },
        "notes": [
            "36 组配置超参固定写进 configs，未在测试集上调参或删配置；负结果原样保留",
            "只报 OOS；passive 为同 OOS 区间等权买入持有（复权口径）",
            "命中率 p = 合并样本二项检验（非重叠再平衡日，偏保守）；hit_p_overlap 为全部日期（含重叠）",
            "多重比较：36 组取最优存在 winner's curse，见 multiple_comparison（max-statistic + Bonferroni/Sidak）",
            "分片代码版本：s1/s2 起跑于 A421EE（F8 修复前）、s3 于 A95E7F 重启；F8 仅影响 three_class 的 hit 类指标"
            "（label 求和 → label==1 计数），二分类/0-1 标签语义等价（QA 独立 replay 15/15 metrics 一致）。"
            "最终代码指纹见 data.code_sha256，输入数据指纹见 data.bars_sha256。",
        ],
    }
    (out_dir / "m6_experiments.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )
    _write_markdown(out_dir / "m6_matrix.md", payload)
    _write_gate_summary(out_dir / "m6_gate_summary.md", payload)
    _log(f"[完成] 最终产出写入 {out_dir}")
    _log(
        "  最佳："
        + json.dumps(
            {
                key: best_result.metrics.get(key)
                for key in ("hit_rate", "base_rate", "hit_rate_p", "net_expectancy", "top_excess_net",
                            "rank_ic", "ic_ir", "long_short_spread", "holding_days", "excess_windows")
            },
            ensure_ascii=False,
        )
    )
    _log(f"  bootstrap: {bootstrap}")
    _log(f"  gate: usable={best_gate['usable']} failed={best_gate['failed']}")
    return 0


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "-"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not pd.notna(number):
        return "-"
    return f"{number:.{digits}f}"


def _write_markdown(path: Path, payload: dict) -> None:
    data = payload["data"]
    lines = ["## M6 实验矩阵（全 36 组，严格 OOS）", ""]
    lines.append(
        f"- 数据：{data.get('symbols')} 只 / {data.get('rows')} 行，{data.get('start')} ~ {data.get('end')}；"
        f"面板 {data.get('panel_rows')} 行；价格口径 `{data.get('price_basis')}`（adjclose=close×adj_factor）"
    )
    lines.append(f"- 市场状态来源：{data.get('market_source')}；数据源：{data.get('source_values')}")
    lines.append(f"- 通过门槛配置数：**{payload.get('n_usable_configs')}** / {len(payload.get('matrix', []))}")
    lines.append("")
    lines.append("### 标签矩阵（样本量与不平衡；复权 vs 不复权均值对照）")
    lines.append("")
    lines.append("| 标签 | 说明 | horizon | purge | 样本量 | 正样本率 | 复权均值 | 不复权均值 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for item in payload.get("label_summary", []):
        lines.append(
            f"| {item.get('label')} | {item.get('description')} | {item.get('horizon')} | "
            f"{item.get('purge_days')} | {item.get('samples')} | {_fmt(item.get('positive_rate'))} | "
            f"{_fmt(item.get('mean_return_adj'), 5)} | {_fmt(item.get('mean_return_raw'), 5)} |"
        )
    lines.append("")
    lines.append("### 实验矩阵（OOS）")
    lines.append("")
    lines.append(
        "| 标签 | 特征 | 模型 | 持有 | 折 | 命中率 | 基础率 | 提升 | p(二项) | 扣费期望 | "
        "Top超额(扣费) | RankIC | ICIR | 正IC | 多空t | 组合收益 | 组合MDD | 被动收益 | 被动MDD | gate |"
    )
    lines.append("|---" * 20 + "|")
    for row in payload.get("matrix", []):
        lines.append(
            f"| {row['label']} | {row['feature_set']} | {row['model']} | {row['holding_days']} | {row['folds']} | "
            f"{_fmt(row['hit_rate'])} | {_fmt(row['base_rate'])} | {_fmt(row['hit_lift'])} | {_fmt(row['hit_p'])} | "
            f"{_fmt(row['net_expectancy'], 5)} | {_fmt(row['top_excess_net'], 5)} | {_fmt(row['rank_ic'])} | "
            f"{_fmt(row['ic_ir'], 3)} | {_fmt(row['positive_ic_ratio'], 3)} | {_fmt(row['long_short_t'], 2)} | "
            f"{_fmt(row['portfolio_total_return'], 3)} | {_fmt(row['portfolio_max_drawdown'], 3)} | "
            f"{_fmt(row['passive_total_return'], 3)} | {_fmt(row['passive_max_drawdown'], 3)} | "
            f"{'PASS' if row['gate_usable'] else 'FAIL'} |"
        )
    lines.append("")
    best = payload.get("best", {})
    bootstrap = best.get("bootstrap_rank_ic") or {}
    lines.append("### 最佳配置（按 gate 判定）")
    lines.append("")
    lines.append(f"- 选择依据：{payload.get('best_selected_by')}")
    lines.append(f"- 配置：`{payload.get('best_config')}`")
    lines.append(f"- 门槛：usable=`{best.get('gate', {}).get('usable')}`，未过项：{best.get('gate', {}).get('failed')}")
    lines.append(
        f"- RankIC 5 日块 bootstrap 95% CI：[{_fmt(bootstrap.get('ci_low'))}, {_fmt(bootstrap.get('ci_high'))}]"
        f"（mean={_fmt(bootstrap.get('mean'))}，{bootstrap.get('n_boot')} 次，块长 {bootstrap.get('block')}）"
    )
    multiple = payload.get("multiple_comparison") or {}
    if multiple.get("interpretation"):
        lines.append(f"- 多重比较：{multiple['interpretation']}")
        lines.append(
            f"  - Bonferroni α={_fmt(multiple.get('bonferroni_alpha'), 6)}；"
            f"max-statistic 校正 p={_fmt(multiple.get('p_max_statistic'))}；"
            f"H0 下最优 RankIC 期望={_fmt(multiple.get('null_max_mean'))}，95% 分位={_fmt(multiple.get('null_max_p95'))}"
        )
    stability = best.get("feature_stability") or {}
    if stability.get("top_features"):
        lines.append("")
        lines.append("| 特征 | 平均IC | IC标准差 | 方向一致性 | 折数 |")
        lines.append("|---|---|---|---|---|")
        for item in stability["top_features"][:10]:
            lines.append(
                f"| {item.get('feature_label')} | {_fmt(item.get('mean_ic'))} | {_fmt(item.get('ic_std'))} | "
                f"{_fmt(item.get('direction_consistency'))} | {item.get('folds')} |"
            )
    strata = best.get("strata") or {}
    if strata.get("by_year"):
        lines.append("")
        lines.append("### 最佳配置分层 OOS（分年度 / 分市场状态）")
        lines.append("")
        lines.append("| 分层 | 日期数 | 命中率 | 基础率 | p | RankIC | Top超额(扣费) |")
        lines.append("|---|---|---|---|---|---|---|")
        for year, item in sorted(strata["by_year"].items(), key=lambda pair: str(pair[0])):
            lines.append(
                f"| {year} | {item.get('dates')} | {_fmt(item.get('hit_rate'))} | {_fmt(item.get('base_rate'))} | "
                f"{_fmt(item.get('hit_rate_p'))} | {_fmt(item.get('rank_ic'))} | {_fmt(item.get('top_excess_net'), 5)} |"
            )
        for state, item in (strata.get("by_market_state") or {}).items():
            lines.append(
                f"| {state} | {item.get('dates')} | {_fmt(item.get('hit_rate'))} | {_fmt(item.get('base_rate'))} | "
                f"{_fmt(item.get('hit_rate_p'))} | {_fmt(item.get('rank_ic'))} | {_fmt(item.get('top_excess_net'), 5)} |"
            )
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_gate_summary(path: Path, payload: dict) -> None:
    lines = ["# M6 门槛判定汇总（严格，不放宽）", ""]
    lines.append(
        "门槛三项：`RankIC > 0.03`、`Top 组扣费后超额 > 0`（超额 = Top h 日收益 − 同期全池等权 h 日收益 − 成本）、"
        "`命中率显著高于基础概率`（合并样本二项检验 p<0.05，非重叠再平衡日样本）。"
    )
    lines.append("")
    lines.append(f"- 通过配置数：**{payload.get('n_usable_configs')} / {len(payload.get('matrix', []))}**")
    lines.append("")
    lines.append("| 标签 | 特征 | 模型 | RankIC | Top超额(扣费) | 命中率 | 基础率 | p(二项) | 判定 | 未过项 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    gate_summary = payload.get("gate_summary", {})
    for row in payload.get("matrix", []):
        key = f"{row['label']}|{row['feature_set']}|{row['model']}"
        gate = gate_summary.get(key, {})
        detail = gate.get("detail", {})
        lines.append(
            f"| {row['label']} | {row['feature_set']} | {row['model']} | {_fmt(detail.get('rank_ic'))} | "
            f"{_fmt(detail.get('top_excess_vs_passive'), 5)} | {_fmt(detail.get('hit_rate'))} | "
            f"{_fmt(detail.get('base_rate'))} | {_fmt(detail.get('hit_rate_p'))} | "
            f"{'PASS' if row['gate_usable'] else 'FAIL'} | {'、'.join(gate.get('failed', []) or ['-'])} |"
        )
    lines.append("")
    lines.append("**结论：未通过门槛的配置一律不得接入推荐/UI 宣称有效。**")
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="M6 短线选股实验矩阵（36 组严格 OOS）")
    parser.add_argument("--bars", default=str(ROOT / "runtime" / "short_term" / "bars_200.csv"))
    parser.add_argument("--index", default=str(ROOT / "runtime" / "m6" / "index_000300.csv"))
    parser.add_argument(
        "--labels", default="ret3_ge2,ret5_ge2,ret10_ge2,cs_top20_5d,three_class_5d,gap_up_1d"
    )
    parser.add_argument("--models", default="logistic,gbdt,gbdt_rank")
    parser.add_argument("--feature-sets", default="v1,all")
    parser.add_argument("--quick", action="store_true", help="冒烟：2 标签 × 2 模型 × 2 特征集，仅 bars_50")
    parser.add_argument("--merge", action="store_true", help="合并分片结果并补算最佳配置统计检验")
    parser.add_argument("--shard-dir", default=None, help="分片输出目录（或 --merge 时的分片根目录）")
    parser.add_argument("--out-dir", default=str(OUT_DIR))
    parser.add_argument("--bootstrap", type=int, default=2000, help="bootstrap 次数（≥1000）")
    parser.add_argument("--log-file", default=None)
    args = parser.parse_args()
    args.started_at = time.time()

    if args.log_file:
        _setup_log(args.log_file)
    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.merge:
        shard_dir = Path(args.shard_dir or (OUT_DIR / "shards"))
        if not shard_dir.is_absolute():
            shard_dir = ROOT / shard_dir
        return _merge(shard_dir, out_dir, args)

    bars_path = Path(args.bars)
    if args.quick:
        bars_path = ROOT / "runtime" / "short_term" / "bars_50.csv"
        _log(f"[冒烟] 强制使用 {bars_path}")
        if out_dir == OUT_DIR:
            out_dir = OUT_DIR / "quick_smoke"
            out_dir.mkdir(parents=True, exist_ok=True)
    if not bars_path.exists():
        _log(f"[错误] 行情文件不存在：{bars_path}（等待 data-engineer 产出 bars_200.csv）")
        return 3
    bars = _load_bars(bars_path)
    index_bars = load_index_bars(Path(args.index)) if Path(args.index).exists() else None
    _log(
        f"[数据] bars {bars.shape}，标的 {bars['symbol'].nunique()}，区间 {bars['date'].min()} ~ {bars['date'].max()}，"
        f"指数 {'有' if index_bars is not None else '无'}"
    )

    panel_started = time.time()
    panel = build_m6_panel(bars, index_bars=index_bars)
    _log(
        f"[面板] {panel.shape}，构建 {time.time() - panel_started:.1f}s，市场状态来源={panel.attrs.get('market_source')}，"
        f"价格口径={panel.attrs.get('price_basis')}，adj_factor 兜底填充={panel.attrs.get('adj_factor_filled')} 行"
    )
    summary = label_summary(panel)
    for item in summary:
        _log(
            f"  [标签] {item['label']:<14} n={item['samples']:<7} pos_rate={item['positive_rate']} "
            f"mean_adj={item.get('mean_return_adj')} mean_raw={item.get('mean_return_raw')}"
        )

    labels = [item for item in args.labels.split(",") if item]
    models = [item for item in args.models.split(",") if item]
    feature_sets = [item for item in args.feature_sets.split(",") if item]
    if args.quick:
        # 冒烟控制在 <3 分钟：1 标签 × 2 特征集 × 2 模型（logistic / gbdt）
        labels = labels[:1]
        models = models[:2]
    configs = [
        M6Config(label=label, feature_set=feature_set, model=model)
        for label in labels
        for feature_set in feature_sets
        for model in models
    ]
    shard_dir = Path(args.shard_dir) if args.shard_dir else None
    if shard_dir is not None:
        if not shard_dir.is_absolute():
            shard_dir = ROOT / shard_dir
        out_dir = shard_dir
        out_dir.mkdir(parents=True, exist_ok=True)
    _log(f"[配置] {len(configs)} 组 → {[ _diagnostics_key(config) for config in configs]}")
    data_meta = _data_meta(bars, panel, index_bars, bars_path)
    payload = _run_configs(
        panel,
        bars,
        configs,
        out_dir=out_dir,
        data_meta=data_meta,
        label_rows=summary,
        started_at=args.started_at,
    )
    _log(
        f"[完成] {payload['ok_configs']}/{len(configs)} 组成功，耗时 {payload['duration_sec']}s，"
        f"输出 {out_dir / 'm6_matrix.csv'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

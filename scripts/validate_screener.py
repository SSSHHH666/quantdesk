#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""全市场筛选器（规则打分）历史验证脚本。

用法::

    .venv\\Scripts\\python.exe scripts\\validate_screener.py
    .venv\\Scripts\\python.exe scripts\\validate_screener.py --bars runtime/short_term/bars_50.csv \\
        --start 2020-01-01 --groups 5 --horizons 5,10,20 --holding 5

输出：
* ``runtime/exports/screener_validation.json``：完整指标（JSON）
* ``runtime/exports/screener_validation.md``：Markdown 报告片段（供 docs 引用/复核）
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy.screener import ScreenerConfig  # noqa: E402
from strategy.screener_validation import (  # noqa: E402
    ValidationConfig,
    render_markdown,
    validate_screener,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="验证全市场筛选规则（分组/IC/成本后净值）")
    parser.add_argument("--bars", default=str(ROOT / "runtime" / "short_term" / "bars_50.csv"))
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default="")
    parser.add_argument("--groups", type=int, default=5)
    parser.add_argument("--horizons", default="5,10,20")
    parser.add_argument("--holding", type=int, default=5)
    parser.add_argument("--cost", type=float, default=0.0023, help="双边费用+滑点（默认 23bp）")
    parser.add_argument("--min-symbols", type=int, default=20)
    parser.add_argument("--out-json", default=str(ROOT / "runtime" / "exports" / "screener_validation.json"))
    parser.add_argument("--out-md", default=str(ROOT / "runtime" / "exports" / "screener_validation.md"))
    args = parser.parse_args()

    bars_path = Path(args.bars)
    if not bars_path.exists():
        print(f"[错误] 行情文件不存在：{bars_path}")
        return 2
    bars = pd.read_csv(bars_path)
    bars["date"] = pd.to_datetime(bars["date"], errors="coerce")
    bars = bars.dropna(subset=["date"])
    start = pd.Timestamp(args.start)
    bars = bars[bars["date"] >= start]
    if args.end:
        bars = bars[bars["date"] <= pd.Timestamp(args.end)]
    if bars.empty:
        print("[错误] 过滤后行情为空")
        return 2
    horizons = tuple(int(item) for item in str(args.horizons).split(",") if item.strip())
    config = ValidationConfig(
        groups=args.groups,
        horizons=horizons,
        holding_days=args.holding,
        round_trip_cost=args.cost,
        min_symbols_per_date=args.min_symbols,
    )
    result = validate_screener(bars, config, screener_config=ScreenerConfig())
    payload = {
        "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "bars": str(bars_path),
        "start": str(bars["date"].min().date()),
        "end": str(bars["date"].max().date()),
        "result": result.to_dict(),
    }
    out_json = Path(args.out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    out_md = Path(args.out_md)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "<!-- 由 scripts/validate_screener.py 自动生成；请勿手改本文件，"
        "修改请改脚本后重跑 -->\n"
        f"<!-- 数据：{bars_path.name}，{bars['symbol'].nunique()} 只，"
        f"{bars['date'].min().date()} ~ {bars['date'].max().date()} -->\n\n"
    )
    out_md.write_text(header + render_markdown(result) + "\n", encoding="utf-8")
    metrics = result.metrics
    conclusion = result.conclusion
    print("=" * 70)
    print(f"验证完成：{metrics['symbols']} 只、{metrics['evaluated_dates']} 个截面、"
          f"{metrics['groups']} 组、持有期 {metrics['horizons']}")
    for horizon, values in metrics.get("ic", {}).items():
        print(f"  {horizon}d RankIC={values.get('mean_rank_ic')} ICIR={values.get('ic_ir')} "
              f"正IC比例={values.get('positive_ic_ratio')}")
    for horizon, values in metrics.get("long_short", {}).items():
        print(f"  {horizon}d Top超额={values.get('top_excess')} t={values.get('top_excess_t')} "
              f"Top-Bottom={values.get('top_minus_bottom')}")
    print(f"  结论：{conclusion.get('verdict')}（usable={conclusion.get('usable')}）")
    print(f"  输出：{out_json} / {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""命令行回测脚本：跑指定策略 + 标的 + 区间，打印绩效摘要（离线可用）。

用途：Lead / 用户快速验证回测链路，无需打开 Streamlit。

示例::

    python scripts/run_backtest.py --strategy ma_cross --symbols 600519.SH \
        --start 2024-01-01 --end 2025-12-31

    # 多标的、指定参数、对比基准、保存到数据库
    python scripts/run_backtest.py --strategy multi_factor \
        --symbols 600519.SH,000001.SZ,300750.SZ,600036.SH \
        --start 2023-01-01 --end 2025-12-31 \
        --params '{"top_n": 2, "rebalance_days": 10}' \
        --benchmark 000300.SH --save

数据源
------
默认 ``--provider auto``：优先本地 SQLite 缓存（``data/cache.py::BarCache``），不可用时
自动降级为内置 ``MockProvider``（确定性离线示例数据，无网络依赖）。可用
``--provider cache|mock`` 强制指定。

价格口径（合规红线）
--------------------
撮合/手数/资金核算一律用**不复权真实价**：``BarCache.load(adjust="none")``，
不读 ``config.yaml`` 的 ``data.adjust``（行情页默认 hfq）。控制台与报告都会打印
``价格口径：不复权真实价（price_adjust=none）``。

报告输出（M6 P1）
-----------------
每次回测同时输出「同仓位被动基准」（等权买入持有、不调仓）与样本外标记：

* 控制台摘要：策略收益 vs 被动收益、超额（pp）、MDD 对照、样本外标记；
* ``runtime/exports/backtest_report_<run_id>.json`` / ``.csv``（字段见
  :mod:`backtest.validation`）。

``scripts/run_backtest.py`` 是**单区间回测**（非 walk-forward），报告固定标
“样本外：否”，不得把单区间收益当作样本外证据。

退出码：0 成功；2 参数/回测/报告错误（中文提示写 stderr）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

# 允许直接从项目根或任意目录执行：把 quantdesk 加入 sys.path
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd  # noqa: E402

from backtest.engine import BacktestEngine, BacktestError, BacktestResult  # noqa: E402
from backtest.metrics import METRIC_LABELS, format_metric  # noqa: E402
from backtest.validation import (  # noqa: E402
    OOS_MARKER_SINGLE_INTERVAL,
    PASSIVE_BENCHMARK_METHOD,
    PRICE_ADJUST_NONE,
    PRICE_CALIBER_MOCK,
    PRICE_CALIBER_RAW,
    PassiveBenchmark,
    build_backtest_report,
    compute_passive_benchmark,
    write_backtest_report,
)

#: 回测价格口径（合规红线）：撮合/手数/资金/被动基准一律不复权真实价
_BACKTEST_ADJUST = PRICE_ADJUST_NONE
#: 默认报告目录（相对项目根）
_DEFAULT_REPORT_DIR = _PROJECT_ROOT / "runtime" / "exports"

#: 命令行默认展示的 KPI（顺序即输出顺序）
_DISPLAY_METRICS: tuple[str, ...] = (
    "total_return",
    "annual_return",
    "max_drawdown",
    "sharpe",
    "sortino",
    "calmar",
    "volatility",
    "win_rate",
    "profit_factor",
    "trade_count",
    "closed_trade_count",
    "open_trade_count",
    "total_fees",
    "final_equity",
)


def _parse_date(text: str, *, name: str) -> date:
    try:
        return date.fromisoformat(str(text).strip())
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"参数错误：{name} 必须是 YYYY-MM-DD 格式，收到 {text!r}") from exc


def _parse_symbols(text: str) -> list[str]:
    symbols = [item.strip().upper() for item in str(text).replace("，", ",").split(",") if item.strip()]
    if not symbols:
        raise SystemExit("参数错误：--symbols 至少需要 1 个标的（逗号分隔，如 600519.SH,000001.SZ）")
    return list(dict.fromkeys(symbols))


def _parse_params(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise SystemExit(f"参数错误：--params 必须是合法 JSON（如 '{{\"fast\": 5}}'）：{exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit("参数错误：--params 必须是 JSON 对象（如 '{\"fast\": 5}'）")
    return value


def _load_bars(
    symbols: list[str], start: date, end: date, provider: str
) -> tuple[pd.DataFrame, str]:
    """取行情：优先本地缓存，失败降级 MockProvider（离线演示）。

    合规口径：本地缓存**必须**用不复权真实价（``adjust="none"``）；取不到不复权
    行情时不做 hfq 静默回退（那会让撮合/手数失真），而是按现有逻辑降级到
    明确标注的 Mock 演示数据（``is_mock=True`` + 控制台红色提示）。
    """

    warnings: list[str] = []
    if provider in ("auto", "cache"):
        try:
            from data.cache import BarCache  # 延迟导入：数据层不可用时也能跑 mock

            frame = BarCache().load(symbols, start, end, adjust=_BACKTEST_ADJUST)
            if frame is not None and len(frame) > 0:
                return frame, "本地 SQLite 缓存（BarCache，不复权真实价 adjust=none）"
            warnings.append("本地缓存没有不复权行情")
        except Exception as exc:  # noqa: BLE001 - 任何原因都降级
            if provider == "cache":
                raise SystemExit(f"本地缓存读取失败（--provider cache）：{exc}") from exc
            warnings.append(f"本地缓存不可用：{type(exc).__name__}: {exc}")
    from data.provider import MockProvider

    frame = MockProvider().get_bars(symbols, start, end, "1d")
    if frame is None or len(frame) == 0:
        raise SystemExit(f"未获取到 {start} ~ {end} 的行情数据（标的：{'、'.join(symbols)}）")
    source = "MockProvider 离线示例数据"
    if warnings:
        source += f"（已降级；原因：{'；'.join(warnings)}）"
    return frame, source


def _load_benchmark(
    benchmark: str | None, start: date, end: date, provider: str
) -> tuple[Any, str | None]:
    """尽力加载基准行情；失败返回 (None, 提示)，不报错。

    基准仅用于展示/计算基准收益，同样按不复权口径取数，避免与撮合口径混用。
    """

    if not benchmark:
        return None, None
    try:
        if provider in ("auto", "cache"):
            from data.cache import BarCache

            frame = BarCache().load([benchmark], start, end, adjust=_BACKTEST_ADJUST)
            if frame is not None and len(frame) > 0:
                return frame, None
    except Exception:  # noqa: BLE001 - 基准缺失不阻塞回测
        pass
    try:
        from data.provider import MockProvider

        frame = MockProvider().get_bars([benchmark], start, end, "1d")
        if frame is not None and len(frame) > 0:
            return frame, None
    except Exception as exc:  # noqa: BLE001
        return None, f"基准 {benchmark} 加载失败：{exc}"
    return None, f"基准 {benchmark} 无行情数据，回测将不绘制基准对比"


def _print_metrics(result: BacktestResult) -> None:
    print("-" * 72)
    print("绩效摘要")
    print("-" * 72)
    for key in _DISPLAY_METRICS:
        if key not in result.metrics:
            continue
        label = METRIC_LABELS.get(key, key)
        value = format_metric(key, result.metrics.get(key))
        print(f"  {label:<12}{value:>18}")
    if result.metrics.get("benchmark_return") is not None:
        print(
            f"  {'基准收益率':<12}{format_metric('benchmark_return', result.metrics['benchmark_return']):>18}"
        )
        print(
            f"  {'超额收益':<12}{format_metric('excess_return', result.metrics.get('excess_return')):>18}"
        )


def _print_warnings(result: BacktestResult) -> None:
    if result.warnings:
        print("-" * 72)
        print("回测警告（RQ_WARN_*，结果仅供参考）")
        print("-" * 72)
        for code, message in zip(result.warnings, result.warning_messages()):
            print(f"  [{code}] {message}")
    if result.notes:
        print("-" * 72)
        print("补充说明")
        print("-" * 72)
        for note in result.notes:
            print(f"  - {note}")


def _print_trades(result: BacktestResult, limit: int) -> None:
    trades = result.trades
    if trades is None or len(trades) == 0:
        print("\n无成交记录")
        return
    print("-" * 72)
    print(f"成交明细（共 {len(trades)} 笔，展示最后 {min(limit, len(trades))} 笔）")
    print("-" * 72)
    view = trades.tail(limit)[
        ["trade_date", "symbol", "side", "price", "quantity", "amount", "fee_total", "slippage_cost"]
    ]
    print(view.to_string(index=False))


def _display_path(path: Path) -> str:
    """把绝对路径转成相对项目根的展示路径（失败时原样返回）。"""

    try:
        return str(path.relative_to(_PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def _print_passive_benchmark(
    passive: PassiveBenchmark,
    report: dict[str, Any],
    json_path: Path,
    csv_path: Path,
) -> None:
    """打印「同仓位被动基准 vs 策略」标准摘要（M6 P1 报告必含项）。"""

    print("-" * 72)
    print("同仓位被动基准（等权买入持有，期间不调仓）")
    print("-" * 72)
    print(f"  {'基准定义':<10}同区间 / 同初始资金 / 同标的列表 / 单票目标仓位 "
          f"{passive.position_pct:g}%（与引擎 default_position_pct 一致）")
    print(f"  {'买入口径':<10}期初 {passive.buy_date} 收盘价（不复权）+ "
          f"{passive.slippage_bps:g}bp 买入滑点，整手取整；期间不调仓，剩余现金保留")
    print(f"  {'费用口径':<10}broker_sim.FeeModel（佣金/印花/过户/规费）与撮合同源；"
          f"期初买入费用 {passive.total_fees:,.2f} 元")
    print(f"  {'价格口径':<10}{report['price_caliber']}")
    print()
    print(f"  {'被动总收益':<12}{format_metric('total_return', report['passive_total_return']):>18}")
    print(f"  {'被动年化':<12}{format_metric('annual_return', report['passive_annual_return']):>18}")
    print(f"  {'被动最大回撤':<12}{format_metric('max_drawdown', report['passive_max_drawdown']):>18}")
    print(f"  {'被动夏普':<12}{format_metric('sharpe', report['passive_sharpe']):>18}")
    print(f"  {'被动期末权益':<12}{format_metric('final_equity', report['passive_final_equity']):>18}")
    print("-" * 72)
    print("策略 vs 被动")
    print("-" * 72)
    print(f"  {'策略总收益':<12}{format_metric('total_return', report['strategy_total_return']):>18}")
    print(f"  {'被动总收益':<12}{format_metric('total_return', report['passive_total_return']):>18}")
    print(f"  {'超额（pp）':<12}{report['excess_return_pp']:>+15.2f}pp")
    print(
        f"  {'回撤对照':<12}策略 MDD {format_metric('max_drawdown', report['strategy_max_drawdown'])}"
        f"  vs  被动 MDD {format_metric('max_drawdown', report['passive_max_drawdown'])}"
    )
    print(f"  {'样本外':<10}{OOS_MARKER_SINGLE_INTERVAL}")
    print(f"  {'报告 JSON':<10}{_display_path(json_path)}")
    print(f"  {'报告 CSV':<10}{_display_path(csv_path)}")
    if passive.notes:
        print("-" * 72)
        print("被动基准说明")
        print("-" * 72)
        for note in passive.notes:
            print(f"  - {note}")
    print("-" * 72)
    print(f"  基准定义全文：{PASSIVE_BENCHMARK_METHOD}")


def _save_result(result: BacktestResult) -> None:
    """保存回测结果到数据库；任何失败都只提示、不改变退出码。"""

    try:
        from core.db import init_db, session_scope
        from backtest.persistence import save_backtest_result

        health = init_db()
        if getattr(health, "ok", True) is False:
            print(f"\n[提示] 数据库未就绪，未保存回测结果：{getattr(health, 'message', '')}")
            return
        with session_scope() as session:
            report = save_backtest_result(session, result)
        if report.get("ok"):
            rows = report.get("rows", {})
            print(
                f"\n[已保存] run_id={result.run_id}  "
                f"backtest_runs=1、backtest_equity={rows.get('equity')} 行、"
                f"backtest_trades={rows.get('trades')} 行"
            )
        else:
            for warning in report.get("warnings", []):
                print(f"\n[提示] {warning}")
    except Exception as exc:  # noqa: BLE001 - 保存失败不影响回测结果
        print(f"\n[提示] 回测结果未保存（不影响本次回测结果）：{type(exc).__name__}: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="QuantDesk 命令行回测：跑指定策略并打印绩效摘要（离线可用）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--strategy", required=True, help="策略注册表 key，如 ma_cross / rsi_reversal / multi_factor")
    parser.add_argument("--symbols", required=True, help="标的代码，逗号分隔，如 600519.SH,000001.SZ")
    parser.add_argument("--start", required=True, help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--cash", type=float, default=100000.0, help="初始资金（元）")
    parser.add_argument("--params", default=None, help="策略参数 JSON，如 '{\"fast\": 5, \"slow\": 20}'")
    parser.add_argument("--benchmark", default="000300.SH", help="基准代码；传空字符串表示不对比基准")
    parser.add_argument(
        "--provider", choices=("auto", "cache", "mock"), default="auto", help="数据源：自动/仅缓存/仅 Mock"
    )
    parser.add_argument("--slippage-bps", type=float, default=5.0, help="滑点（基点，默认 5bp）")
    parser.add_argument("--save", action="store_true", help="把结果写入数据库（backtest_runs/equity/trades）")
    parser.add_argument("--run-id", default=None, help="指定回测 run_id（可复现/幂等保存用）")
    parser.add_argument("--trades", type=int, default=10, help="成交明细最多展示笔数")
    parser.add_argument(
        "--report-dir",
        default=str(_DEFAULT_REPORT_DIR),
        help="标准报告输出目录（JSON + CSV，默认 runtime/exports）",
    )
    args = parser.parse_args(argv)

    start = _parse_date(args.start, name="--start")
    end = _parse_date(args.end, name="--end")
    if start > end:
        raise SystemExit(f"参数错误：--start（{start}）不能晚于 --end（{end}）")
    symbols = _parse_symbols(args.symbols)
    params = _parse_params(args.params)
    if args.cash <= 0:
        raise SystemExit(f"参数错误：--cash 必须为正数，收到 {args.cash}")

    print("=" * 72)
    print("QuantDesk 回测")
    print("=" * 72)
    print(f"  策略      {args.strategy}")
    print(f"  标的      {'、'.join(symbols)}")
    print(f"  区间      {start} ~ {end}")
    print(f"  初始资金  {args.cash:,.2f} 元")
    print(f"  价格口径  {PRICE_CALIBER_RAW}")
    if params:
        print(f"  参数      {json.dumps(params, ensure_ascii=False)}")

    try:
        bars, source = _load_bars(symbols, start, end, args.provider)
        print(f"  数据源    {source}")
        print(f"  行情行数  {len(bars):,}")
        date_column = "trade_date" if "trade_date" in bars.columns else ("date" if "date" in bars.columns else None)
        if date_column is not None:
            actual_dates = pd.to_datetime(bars[date_column], errors="coerce").dropna()
            if len(actual_dates) > 0:
                actual_min = actual_dates.min().date()
                actual_max = actual_dates.max().date()
                print(f"  数据区间  {actual_min} ~ {actual_max}")
                if actual_min > start or actual_max < end:
                    print(
                        "  [提示]    数据未覆盖完整请求区间，结果以实际数据为准；"
                        "离线演示可加 --provider mock"
                    )
        benchmark_bars, benchmark_note = _load_benchmark(
            args.benchmark or None, start, end, args.provider
        )
        if benchmark_note:
            print(f"  [提示]    {benchmark_note}")
        result = BacktestEngine().run(
            args.strategy,
            params,
            symbols,
            start,
            end,
            initial_cash=args.cash,
            slippage_bps=args.slippage_bps,
            benchmark=args.benchmark or "",
            bars=bars,
            benchmark_bars=benchmark_bars,
            run_id=args.run_id,
            price_adjust=_BACKTEST_ADJUST,
        )
    except BacktestError as exc:
        print(f"\n[回测失败] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 命令行兜底，给出中文提示
        print(f"\n[回测异常] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(f"  run_id    {result.run_id}")
    if getattr(result, "is_mock", False):
        print("  数据来源  ⚠ 离线 Mock 示例数据，仅供演示（不代表真实市场表现）")
    elif (result.metadata or {}).get("data_note"):
        print(f"  数据来源  {result.metadata['data_note']}")
    print(f"  实际区间  {result.start_date} ~ {result.end_date}（{result.metrics.get('n_days')} 个交易日）")
    print(f"  期末权益  {result.final_equity:,.2f} 元")
    _print_metrics(result)
    _print_warnings(result)

    # ---- M6 P1：同仓位被动基准 + 样本外标记 + 标准报告 -------------------
    price_caliber = PRICE_CALIBER_MOCK if getattr(result, "is_mock", False) else PRICE_CALIBER_RAW
    try:
        position_pct = float((result.params or {}).get("position_pct") or 10.0)
        calendar = (
            result.equity["trade_date"].tolist()
            if result.equity is not None and "trade_date" in result.equity.columns
            else None
        )
        passive = compute_passive_benchmark(
            bars,
            symbols,
            result.start_date,
            result.end_date,
            initial_cash=args.cash,
            position_pct=position_pct,
            fee_config=result.fee_config,
            slippage_bps=args.slippage_bps,
            calendar=calendar,
        )
        report = build_backtest_report(
            result,
            passive,
            price_caliber=price_caliber,
            oos=False,
            oos_marker=OOS_MARKER_SINGLE_INTERVAL,
        )
        json_path, csv_path = write_backtest_report(report, Path(args.report_dir))
    except Exception as exc:  # noqa: BLE001 - 报告是标准输出，失败必须显式报错
        print(f"\n[报告生成失败] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    _print_passive_benchmark(passive, report, json_path, csv_path)
    _print_trades(result, max(1, args.trades))
    if args.save:
        _save_result(result)
    print("\n完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

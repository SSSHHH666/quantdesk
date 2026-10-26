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

退出码：0 成功；2 参数/回测错误（中文提示写 stderr）。
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
    """取行情：优先本地缓存，失败降级 MockProvider（离线演示）。"""

    warnings: list[str] = []
    if provider in ("auto", "cache"):
        try:
            from data.cache import BarCache  # 延迟导入：数据层不可用时也能跑 mock

            frame = BarCache().load(symbols, start, end, adjust="hfq")
            if frame is not None and len(frame) > 0:
                return frame, "本地 SQLite 缓存（BarCache，后复权）"
            warnings.append("本地缓存没有数据")
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
    """尽力加载基准行情；失败返回 (None, 提示)，不报错。"""

    if not benchmark:
        return None, None
    try:
        if provider in ("auto", "cache"):
            from data.cache import BarCache

            frame = BarCache().load([benchmark], start, end, adjust="hfq")
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
    _print_trades(result, max(1, args.trades))
    if args.save:
        _save_result(result)
    print("\n完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

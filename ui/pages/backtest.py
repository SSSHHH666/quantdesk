"""页面：回测（M2 接线版）。

职责（SPEC §10.5）：策略下拉 + 按 ``params_schema`` 动态生成参数表单、标的多选、
区间/初始资金/滑点/基准配置，点击运行调用 ``BacktestEngine.run``，展示 KPI、权益曲线
（含基准）、回撤曲线、交易明细、警告区与运行耗时；异常给中文错误卡（带 trace_id）。

接线（只调用服务层，本文件不直连网络、不写 SQL）：
    strategy.list_strategies() / get_strategy()      # 策略注册表与参数 schema
    backtest.engine.BacktestEngine.run(...)           # 事件驱动回测（M1-11）
    backtest.engine.BacktestResult                    # metrics/equity/trades/warnings
    data.provider.build_provider()                    # 数据源（akshare 未接入时降级 mock）
    data.cache.BarCache.load                          # 优先用本地缓存（覆盖区间时）

说明：回测默认用「后复权（hfq）」口径；本地缓存不完整时自动回退数据源直连。
"""

from __future__ import annotations

import datetime as dt
import traceback

import pandas as pd
import streamlit as st

from . import _common as C

PAGE_TITLE = "回测"
PAGE_ICON = "🧪"
PAGE_ORDER = 5

TODO_ITEMS = [
    "backtest_runs 历史回测列表与对比视图（M1-11 落库后接入）",
    "导出 HTML 报告（M1-11）",
    "月度收益热力图（M1-12 指标扩展）",
]


# ---------------------------------------------------------------------------
# 服务接入
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def _provider():
    """数据源单例（按 config.data 组装主备链，akshare 未接入时自动降级 mock）。"""
    from data.provider import build_provider

    return build_provider()


@st.cache_resource(show_spinner=False)
def _cache(provider):
    """本地行情缓存单例（BarCache）。"""
    from data.cache import BarCache

    return BarCache(provider=provider)


@st.cache_data(ttl=600, show_spinner=False)
def _strategies() -> list[dict]:
    """策略注册表（key/display_name/description/params_schema，动态发现内置策略）。"""
    from strategy import list_strategies

    return list_strategies()


@st.cache_data(ttl=300, show_spinner=False)
def _benchmark_bars(symbol: str, start_iso: str, end_iso: str) -> pd.DataFrame:
    """基准指数日线（缺失时返回空表，引擎会给出「基准数据缺失」说明）。"""
    if not symbol:
        return pd.DataFrame()
    start = dt.date.fromisoformat(start_iso)
    end = dt.date.fromisoformat(end_iso)
    try:
        frame = _provider().get_bars([symbol], start, end, "1d")
    except Exception:
        return pd.DataFrame()
    return frame if frame is not None else pd.DataFrame()


def _pick_data_source(provider, symbols: list[str], start: dt.date, end: dt.date):
    """选择回测数据源：本地缓存覆盖区间时用缓存，否则回退数据源直连。"""
    try:
        cache = _cache(provider)
        probe = cache.load(symbols, start, end, adjust="hfq")
        if probe is not None and not probe.empty:
            dates = pd.to_datetime(probe["date"])
            first, last = dates.min().date(), dates.max().date()
            if first <= start + dt.timedelta(days=10) and last >= end - dt.timedelta(days=10):
                return cache, f"本地缓存（{len(probe):,} 行，{first} ~ {last}）"
    except Exception:
        pass
    return provider, f"数据源直连（{provider.name}）"


# ---------------------------------------------------------------------------
# 参数表单（按 params_schema 动态渲染）
# ---------------------------------------------------------------------------
def _param_widget(strategy_key: str, name: str, spec: dict) -> object:
    """根据 schema 渲染单个参数控件并返回用户输入值（类型与 min/max/step 一致）。"""
    label = str(spec.get("label") or name)
    if spec.get("unit"):
        label = f"{label}（{spec['unit']}）"
    help_text = str(spec.get("help") or "")
    key = f"qd_bt_p__{strategy_key}__{name}"
    ptype = str(spec.get("type") or "str")
    default = spec.get("default")

    if ptype == "int":
        kwargs: dict = {"value": int(default), "step": int(spec.get("step") or 1)}
        if spec.get("min") is not None:
            kwargs["min_value"] = int(spec["min"])
        if spec.get("max") is not None:
            kwargs["max_value"] = int(spec["max"])
        return st.number_input(label, key=key, help=help_text, **kwargs)
    if ptype == "float":
        kwargs = {"value": float(default), "step": float(spec.get("step") or 0.1), "format": "%.2f"}
        if spec.get("min") is not None:
            kwargs["min_value"] = float(spec["min"])
        if spec.get("max") is not None:
            kwargs["max_value"] = float(spec["max"])
        return st.number_input(label, key=key, help=help_text, **kwargs)
    if ptype == "bool":
        return st.checkbox(label, value=bool(default), key=key, help=help_text)
    if ptype == "select":
        choices = list(spec.get("choices") or [])
        index = choices.index(default) if default in choices else 0
        return st.selectbox(label, choices, index=index, key=key, help=help_text)
    return st.text_input(label, value=str(default if default is not None else ""),
                         key=key, help=help_text)


def _render_param_form(strategy_key: str, schema: dict) -> dict:
    """把 schema 渲染为 3 列网格（``advanced`` 参数收进折叠区），返回原始输入值。"""
    normal = {k: v for k, v in schema.items() if not v.get("advanced")}
    advanced = {k: v for k, v in schema.items() if v.get("advanced")}
    values: dict = {}

    if not schema:
        st.caption("该策略无可配置参数（使用默认逻辑）。")
        return values

    def _render(items: dict) -> None:
        cols = st.columns(3)
        for index, (name, spec) in enumerate(items.items()):
            with cols[index % 3]:
                values[name] = _param_widget(strategy_key, name, spec)

    if normal:
        _render(normal)
    if advanced:
        with st.expander("高级参数", expanded=False):
            _render(advanced)
    return values


# ---------------------------------------------------------------------------
# 结果展示
# ---------------------------------------------------------------------------
def _ratio(value: object) -> str:
    """比率类指标（夏普/盈亏比）格式化。"""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return C.DASH
    return f"{number:.2f}"


def _equity_chart(result) -> None:
    """权益曲线（含基准，净值为 100）+ 回撤曲线。"""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    equity = result.equity.copy()
    if equity.empty:
        C.not_ready("权益曲线", "回测没有产生权益记录。")
        return
    equity["trade_date"] = pd.to_datetime(equity["trade_date"])
    base = float(result.initial_cash) or 1.0
    equity["strategy_nav"] = equity["equity"] / base * 100.0

    has_benchmark = (
        "benchmark_close" in equity.columns
        and equity["benchmark_close"].notna().any()
    )
    if has_benchmark:
        first = float(equity["benchmark_close"].dropna().iloc[0]) or 1.0
        equity["benchmark_nav"] = equity["benchmark_close"] / first * 100.0

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.72, 0.28], vertical_spacing=0.06,
                        subplot_titles=("权益曲线（初始净值 = 100）", "回撤（%）"))
    fig.add_trace(
        go.Scatter(x=equity["trade_date"], y=equity["strategy_nav"], name="策略净值",
                   mode="lines", line=dict(color=C.COLOR_UP, width=1.6)),
        row=1, col=1,
    )
    if has_benchmark:
        fig.add_trace(
            go.Scatter(x=equity["trade_date"], y=equity["benchmark_nav"],
                       name=str(result.benchmark or "基准"), mode="lines",
                       line=dict(color="#5f6368", width=1.2, dash="dot")),
            row=1, col=1,
        )
    fig.add_trace(
        go.Scatter(x=equity["trade_date"], y=equity["drawdown"] * 100.0, name="回撤",
                   mode="lines", fill="tozeroy",
                   line=dict(color=C.COLOR_DOWN, width=1.0)),
        row=2, col=1,
    )
    fig.update_layout(height=520, margin=dict(l=8, r=8, t=40, b=8),
                      legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
                      hovermode="x unified")
    fig.update_yaxes(title_text="净值", row=1, col=1)
    fig.update_yaxes(title_text="%", row=2, col=1)
    st.plotly_chart(fig, use_container_width=True)


def _trades_table(result) -> None:
    """交易明细表（中文列名 + 千分位 + A 股涨跌配色不适用于表格，仅格式化）。"""
    trades = result.trades
    if trades is None or trades.empty:
        C.not_ready(
            "交易明细",
            "本次回测没有产生任何成交。可能原因：策略在该区间无信号、样本不足、"
            "或标的停牌/涨跌停导致无法成交。",
        )
        return
    show = pd.DataFrame(
        {
            "日期": pd.to_datetime(trades["trade_date"]).dt.strftime("%Y-%m-%d"),
            "代码": trades["symbol"],
            "方向": [C.side_label(v) for v in trades["side"]],
            "价格": [C.money(v) for v in trades["price"]],
            "数量": [C.money(v, digits=0) for v in trades["quantity"]],
            "金额": [C.money(v) for v in trades["amount"]],
            "佣金": [C.money(v) for v in trades["commission"]],
            "印花税": [C.money(v) for v in trades["stamp_tax"]],
            "过户费": [C.money(v) for v in trades["transfer_fee"]],
            "规费": [C.money(v) for v in trades["reg_fee"]],
            "费用合计": [C.money(v) for v in trades["fee_total"]],
            "滑点成本": [C.money(v) for v in trades["slippage_cost"]],
        }
    )
    C.data_table(show, height=340)
    C.export_csv_button(trades, f"trades_{result.run_id[:8]}.csv",
                        key="qd_bt_trades_csv", hint="无成交可导出")


def build_report_html(result, display_name: str = "") -> str:
    """M5-02：生成可离线打开的 HTML 回测报告（绩效表/权益曲线 SVG/交易明细/警告）。

    纯字符串生成、无外部依赖（不引 CDN），可直接下载后在浏览器打开或归档。
    """
    import datetime as _dt
    import html as _html

    metrics = dict(getattr(result, "metrics", {}) or {})
    equity = getattr(result, "equity", None)
    trades = getattr(result, "trades", None)
    warnings = list(getattr(result, "warnings", []) or [])
    warning_texts = result.warning_messages() if hasattr(result, "warning_messages") else warnings

    metric_rows = [
        ("总收益率", C.pct(metrics.get("total_return"), signed=True)),
        ("年化收益率", C.pct(metrics.get("annual_return"), signed=True)),
        ("最大回撤", C.pct(metrics.get("max_drawdown"))),
        ("夏普比率", f"{float(metrics['sharpe']):.2f}" if metrics.get("sharpe") is not None else "—"),
        ("胜率", C.pct(metrics.get("win_rate"))),
        ("盈亏比", f"{float(metrics['profit_factor']):.2f}"
         if metrics.get("profit_factor") is not None else "—"),
        ("交易次数", str(int(metrics.get("trade_count") or 0))),
        ("波动率", C.pct(metrics.get("volatility"))),
    ]
    metric_html = "".join(
        f"<tr><th>{_html.escape(name)}</th><td>{_html.escape(value)}</td></tr>"
        for name, value in metric_rows
    )

    svg = ""
    try:
        if equity is not None and len(equity) > 1:
            values = [float(v) for v in equity["equity"].tolist()]
            low, high = min(values), max(values)
            span = (high - low) or 1.0
            width, height = 760, 200
            points = " ".join(
                f"{i * width / (len(values) - 1):.1f},{height - (v - low) / span * (height - 20) - 10:.1f}"
                for i, v in enumerate(values)
            )
            svg = (f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
                   f'style="background:#fbfcfd;border:1px solid #e5e7eb;border-radius:8px">'
                   f'<polyline fill="none" stroke="#d93025" stroke-width="1.6" points="{points}"/></svg>')
    except Exception:  # pragma: no cover - 图表失败不影响报告主体
        svg = ""

    trade_html = ""
    try:
        if trades is not None and len(trades):
            columns = ["trade_date", "symbol", "side", "price", "quantity", "amount", "fee_total"]
            head = "".join(f"<th>{_html.escape(c)}</th>" for c in columns)
            body = "".join(
                "<tr>" + "".join(f"<td>{_html.escape(str(row.get(c, '')))}</td>" for c in columns) + "</tr>"
                for row in trades.head(300).to_dict(orient="records")
            )
            trade_html = f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
        else:
            trade_html = "<p>本次回测没有成交记录。</p>"
    except Exception:  # pragma: no cover
        trade_html = "<p>交易明细生成失败。</p>"

    warn_html = "".join(f"<li>{_html.escape(str(text))}</li>" for text in warning_texts) or "<li>无警告</li>"
    generated = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title = f"回测报告 · {display_name or getattr(result, 'strategy_key', '')}"
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{_html.escape(title)}</title>
<style>
 body{{font-family:"Microsoft YaHei",system-ui,sans-serif;margin:24px;color:#1f2937;background:#fff}}
 h1{{font-size:20px;margin:0 0 4px}} .meta{{color:#6b7280;font-size:13px;margin-bottom:16px}}
 h2{{font-size:16px;margin:20px 0 8px}} table{{border-collapse:collapse;width:100%;font-size:13px}}
 th,td{{border:1px solid #e5e7eb;padding:6px 8px;text-align:left}} th{{background:#f3f4f6}}
 .warn{{background:#fffaf1;border:1px solid #f5ddb5;border-radius:8px;padding:10px 14px}}
 .foot{{color:#9aa0a6;font-size:12px;margin-top:20px}}
</style></head><body>
<h1>{_html.escape(title)}</h1>
<div class="meta">run_id: {_html.escape(str(getattr(result, 'run_id', '')))} ·
 标的: {_html.escape(', '.join(getattr(result, 'symbols', []) or []))} ·
 区间: {C.fmt_date(getattr(result, 'start_date', None))} ~ {C.fmt_date(getattr(result, 'end_date', None))} ·
 初始资金: {C.money(getattr(result, 'initial_cash', 0), digits=0)} 元 ·
 生成时间: {generated}</div>
<h2>绩效指标</h2><table>{metric_html}</table>
<h2>权益曲线</h2>{svg or '<p>无权益数据。</p>'}
<h2>交易明细（最多 300 条）</h2>{trade_html}
<h2>风险与警告</h2><div class="warn"><ul>{warn_html}</ul></div>
<div class="foot">QuantDesk 回测报告 · 模拟回测结果不代表未来收益，不构成投资建议。</div>
</body></html>"""


def _render_result(result, source_text: str, display_name: str) -> None:
    """渲染回测结果：KPI、曲线、交易、警告、备注。"""
    metrics = result.metrics or {}
    total_return = metrics.get("total_return")
    max_drawdown = metrics.get("max_drawdown")

    st.markdown("##### 绩效指标")
    C.kpi_row(
        [
            {"label": "总收益率", "value": C.pct(total_return, signed=True),
             "tone": "up" if (total_return or 0) > 0 else ("down" if (total_return or 0) < 0 else "flat"),
             "help": "期末权益 / 初始资金 − 1"},
            {"label": "年化收益率", "value": C.pct(metrics.get("annual_return"), signed=True),
             "help": "按交易日年化（CAGR）"},
            {"label": "最大回撤", "value": C.pct(max_drawdown),
             "tone": "down" if (max_drawdown or 0) < 0 else "flat",
             "help": "峰值到谷底最大跌幅（负数或 0）"},
            {"label": "夏普比率", "value": _ratio(metrics.get("sharpe")),
             "help": "年化超额收益 / 年化波动（无风险利率 0）"},
            {"label": "胜率", "value": C.pct(metrics.get("win_rate")),
             "help": "盈利平仓交易数 / 平仓交易数"},
            {"label": "交易次数", "value": str(int(metrics.get("trade_count") or 0)),
             "help": "成交笔数；< 30 笔时统计显著性不足（RQ_WARN_FEW_TRADES）"},
        ]
    )
    st.caption(
        f"盈亏比 {_ratio(metrics.get('profit_factor'))} · "
        f"波动率 {C.pct(metrics.get('volatility'))} · "
        f"交易费用合计 {C.money(metrics.get('total_fees'))} 元 · "
        f"期末权益 {C.money(metrics.get('final_equity'))} 元 · "
        f"样本 {int(metrics.get('n_days') or 0)} 个交易日"
    )

    st.markdown("##### 曲线")
    try:
        _equity_chart(result)
    except Exception:
        C.render_error_card(
            "权益曲线绘制失败",
            message="回测已完成，但图表渲染出错。",
            suggestion="可导出 CSV 查看数据；若持续出现请反馈 trace_id。",
            trace_id=C.new_trace_id(),
            exc=traceback.format_exc(),
        )

    left, right = st.columns([1.7, 1])
    with left:
        st.markdown("##### 交易明细")
        _trades_table(result)
    with right:
        st.markdown("##### 可信度与警告")
        messages = result.warning_messages()
        if messages:
            for message in messages:
                C.render_warning_card("回测警告", message)
        else:
            C.render_info_card("无警告", "本轮回测未触发 RQ_WARN_* 警告码。")
        if result.notes:
            st.caption("补充说明：" + "；".join(result.notes))
        C.status_bar(
            [
                {"label": "样本", "value": f"{int(metrics.get('n_days') or 0)} 交易日",
                 "tone": "ok" if int(metrics.get("n_days") or 0) >= 250 else "warn",
                 "help": "< 250 日给 RQ_WARN_SHORT_SAMPLE"},
                {"label": "成交", "value": f"{int(metrics.get('trade_count') or 0)} 笔",
                 "tone": "ok" if int(metrics.get("trade_count") or 0) >= 30 else "warn",
                 "help": "< 30 笔给 RQ_WARN_FEW_TRADES"},
            ]
        )

    st.caption(
        f"运行摘要：run_id={result.run_id} · 策略={display_name}（{result.strategy_key}）· "
        f"参数={result.params} · 标的={len(result.symbols)} 只 · "
        f"区间={C.fmt_date(result.start_date)} ~ {C.fmt_date(result.end_date)} · "
        f"初始资金={C.money(result.initial_cash, digits=0)} 元 · "
        f"耗时={result.duration_seconds:.2f}s · 数据源={source_text}"
    )
    C.export_csv_button(result.equity, f"equity_{result.run_id[:8]}.csv",
                        key="qd_bt_equity_csv", hint="无权益数据可导出")

    # M5-02：导出完整 HTML 报告（可离线打开；文件名含策略/区间/时间戳）
    try:
        report_html = build_report_html(result, display_name)
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = (display_name or getattr(result, "strategy_key", "backtest")).replace(" ", "")
        file_name = f"backtest_{safe_name}_{result.start_date}_{result.end_date}_{stamp}.html"
        st.download_button(
            "⬇️ 导出 HTML 报告（含绩效表/权益曲线/交易明细/警告）",
            data=report_html.encode("utf-8"),
            file_name=file_name,
            mime="text/html",
            key="qd_bt_report_export",
            help="生成单文件 HTML，可直接在浏览器打开或归档；不需要网络。",
        )
    except Exception as exc:  # pragma: no cover - 报告生成失败不影响页面
        st.caption(f"报告生成失败（可先导出 CSV）：{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
def _default_start() -> dt.date:
    """默认区间：近 3 年（SPEC §10.5）。"""
    today = dt.date.today()
    return dt.date(today.year - 3, today.month, today.day)


def render() -> None:
    C.page_header("回测", "选策略 → 配参数 → 跑历史 → 看指标与权益曲线（M2 接线版）",
                  badge="M2 接线")
    prefill_symbol = str(st.session_state.get("qd_symbol") or "").strip()
    meta = C.app_meta()
    bt_defaults = meta.get("backtest") or {}
    universe = [s for s in (meta.get("universe") or []) if s]

    # ---- 策略与标的服务（失败给中文错误卡，页面其余部分不白屏）----
    strategies: list[dict] = []
    service_error = ""
    try:
        strategies = _strategies()
    except Exception as exc:
        service_error = f"{type(exc).__name__}: {exc}"
    if not strategies:
        C.page_header("回测", "策略注册表不可用（M2 接线版）", badge="M2 接线")
        C.render_error_card(
            "策略注册表加载失败",
            message=service_error or "未发现任何已注册策略",
            suggestion="确认 strategy/builtin/*.py 已交付且无语法错误；重启 Streamlit 后重试。",
            trace_id=C.new_trace_id(),
            exc=traceback.format_exc() if service_error else None,
        )
        C.todo_expander(TODO_ITEMS)
        return

    rows: list[dict] = [{"symbol": s, "name": ""} for s in universe]
    if prefill_symbol and all(row["symbol"] != prefill_symbol for row in rows):
        rows.append({"symbol": prefill_symbol, "name": ""})
    if not rows:
        C.render_error_card(
            "自选池为空",
            message="config.data.universe 中没有任何标的，无法回测。",
            suggestion="到设置页「数据源」填写自选池（如 600519.SH），或直接在配置文件补充后刷新。",
            trace_id=C.new_trace_id(),
        )
        C.todo_expander(TODO_ITEMS)
        return
    labels = [f"{row['symbol']} {row['name']}".strip() for row in rows]
    default_symbols = [label for label, row in zip(labels, rows) if row["symbol"] in universe]
    if prefill_symbol:
        extra = next((label for label, row in zip(labels, rows) if row["symbol"] == prefill_symbol), None)
        if extra and extra not in default_symbols:
            default_symbols = [extra, *default_symbols]

    # ---- 参数区 ----
    with st.container(border=True):
        st.markdown("##### 回测参数")
        r1, r2 = st.columns([1.3, 2.7])
        with r1:
            strategy_labels = {f"{item['display_name']}（{item['key']}）": item for item in strategies}
            # 策略页「去回测（带参数）」：预选策略（setdefault 不覆盖用户后续选择）
            prefill_strategy_key = str(st.session_state.get("qd_strategy") or "").strip()
            if prefill_strategy_key:
                for label, item in strategy_labels.items():
                    if str(item["key"]) == prefill_strategy_key:
                        st.session_state.setdefault("qd_bt_strategy", label)
                        break
            selected_label = st.selectbox(
                "策略", options=list(strategy_labels), index=0, key="qd_bt_strategy",
                help="策略来自 strategy 注册表（新增策略只需新增文件后重启，无需改本页）。",
            )
            strategy = strategy_labels[selected_label]
            strategy_key = str(strategy["key"])
            if strategy.get("description"):
                st.caption(strategy["description"])
        with r2:
            st.multiselect(
                "标的（来自自选池）", options=labels, default=default_symbols,
                key="qd_bt_symbols",
                help="默认取 config.data.universe（自选池）；行情页「去回测」会带入当前标的。"
                     "自选池可在设置页编辑。",
            )
            st.caption("标的需有足够历史数据；数据不足时对应标的会被引擎跳过。")

        # 参数表单（params_schema 驱动）
        schema = dict(strategy.get("params_schema") or {})
        # 策略页「去回测（带参数）」：把带入的参数作为控件初始值（setdefault 不覆盖后续编辑）
        for param_name, param_value in (st.session_state.get("qd_bt_params") or {}).items():
            st.session_state.setdefault(f"qd_bt_p__{strategy_key}__{param_name}", param_value)
        raw_params = _render_param_form(strategy_key, schema)

        # 参数校验（服务层统一校验：类型/区间/跨字段约束，中文可读）
        params: dict | None = None
        params_error = ""
        try:
            from strategy import get_strategy

            params = get_strategy(strategy_key).validate_params(raw_params)
        except Exception as exc:
            params_error = str(exc)
            params = None
        if params_error:
            C.render_error_card(
                "策略参数不合法",
                message=params_error,
                suggestion="按上面的中文提示调整参数（例如快线周期必须小于慢线周期）后重试。",
                trace_id=C.new_trace_id(),
            )

        r4, r5, r6, r7 = st.columns([1.5, 1.1, 1.1, 1.1])
        with r4:
            cash = st.number_input(
                "初始资金（元）", min_value=10000.0,
                value=float(bt_defaults.get("default_initial_cash", 100000.0)),
                step=10000.0, format="%.0f", key="qd_bt_cash",
                help="默认取 config.backtest.default_initial_cash（core.config 读取）",
            )
        with r5:
            start = st.date_input("开始日期", value=_default_start(), key="qd_bt_start")
        with r6:
            end = st.date_input("结束日期", value=dt.date.today(), key="qd_bt_end")
        with r7:
            slippage = st.number_input("滑点（bps）", min_value=0.0, max_value=200.0,
                                       value=5.0, step=1.0, key="qd_bt_slippage",
                                       help="买入价格上浮 / 卖出价格下调该基点数")

        f1, f2 = st.columns([1.4, 2.6])
        with f1:
            benchmark = st.text_input(
                "基准指数", value=str(bt_defaults.get("benchmark", "000300.SH")),
                key="qd_bt_benchmark",
                help="默认取 config.backtest.benchmark；数据缺失时曲线不画基准并给出说明。",
            )
        with f2:
            st.caption("费率由 config.yaml 的 fees 段统一提供（回测与模拟盘同源，CF-10）；"
                       "如需自定义费率可在设置页修改配置（M3-07 保存能力）。")

        b1, b2, b3 = st.columns([1.2, 1.4, 2.4])
        with b1:
            run_clicked = st.button("🚀 运行回测", type="primary", use_container_width=True,
                                    key="qd_bt_run", disabled=params is None)
        with b2:
            st.caption("回测完成后在结果区导出 HTML 报告（含绩效表/权益曲线/交易明细/警告）。")
        with b3:
            if params is None:
                st.caption("参数不合法时无法运行；请先按上方错误提示修正。")
            else:
                st.caption("逐交易日事件驱动回测；运行期间显示进度，完成后自动展示结果。")

    # ---- 运行 ----
    selected_symbols = [
        row["symbol"]
        for label in st.session_state.get("qd_bt_symbols", []) or []
        for row in rows
        if (f"{row['symbol']} {row['name']}" if row["name"] else row["symbol"]) == label
    ]
    if run_clicked and params is not None:
        if not selected_symbols:
            C.render_error_card(
                "没有选择标的",
                message="回测至少需要 1 个标的。",
                suggestion="在上方「标的」下拉中选择至少 1 只股票（默认自选池已预选）。",
                trace_id=C.new_trace_id(),
            )
        elif start > end:
            C.render_error_card(
                "回测区间不合法",
                message=f"开始日期 {start} 晚于结束日期 {end}。",
                suggestion="调整日期顺序后重试；建议区间 ≥ 1 年以覆盖完整行情周期。",
                trace_id=C.new_trace_id(),
            )
        else:
            with st.spinner("回测运行中（逐交易日事件驱动）…"):
                started = dt.datetime.now()
                try:
                    provider = _provider()
                    source, source_text = _pick_data_source(provider, selected_symbols, start, end)
                    benchmark_bars = _benchmark_bars(
                        benchmark.strip().upper(), start.isoformat(), end.isoformat()
                    ) if benchmark.strip() else pd.DataFrame()
                    from backtest.engine import BacktestEngine

                    engine = BacktestEngine(data_source=source)
                    result = engine.run(
                        strategy_key,
                        params,
                        selected_symbols,
                        start,
                        end,
                        initial_cash=float(cash),
                        slippage_bps=float(slippage),
                        benchmark=benchmark.strip().upper() or None,
                        benchmark_bars=None if benchmark_bars.empty else benchmark_bars,
                    )
                except Exception as exc:  # BacktestError 与其它异常统一中文卡片
                    from backtest.engine import BacktestError

                    if isinstance(exc, BacktestError):
                        C.render_error_card(
                            "回测失败",
                            message=str(exc),
                            suggestion="检查标的代码与区间是否有行情；可先到行情页点「拉取/刷新数据」写入缓存，"
                                       "或缩短回测区间后重试。",
                            trace_id=C.new_trace_id(),
                        )
                    else:
                        C.render_error_card(
                            "回测运行出错",
                            message=f"{type(exc).__name__}: {exc}",
                            suggestion="请把 trace_id 与技术细节提供给维护者；可先尝试减少标的/缩短区间。",
                            trace_id=C.new_trace_id(),
                            exc=traceback.format_exc(),
                        )
                else:
                    st.session_state["qd_bt_result"] = result
                    st.session_state["qd_bt_source"] = source_text
                    try:  # M5：回测结果落库（backtest_runs），供历史回测列表加载
                        from core.db import session_scope
                        from backtest.persistence import save_backtest_result

                        with session_scope() as _session:
                            save_backtest_result(_session, result)
                    except Exception:
                        pass
                    st.session_state["qd_bt_display_name"] = str(strategy["display_name"])
                    st.session_state["qd_bt_started_at"] = started.isoformat(timespec="seconds")
                    st.success(
                        f"回测完成：耗时 {result.duration_seconds:.2f}s，"
                        f"{len(result.equity)} 个交易日 / {len(result.trades)} 笔成交，"
                        f"数据源：{source_text}"
                    )

    # ---- 结果 ----
    result = st.session_state.get("qd_bt_result")
    if result is None:
        C.not_ready(
            "尚无回测结果",
            "选择策略、标的与区间后点击「运行回测」。结果会显示 KPI、权益曲线（含基准）、"
            "回撤曲线与交易明细，并可导出 CSV。",
            todo="BacktestEngine.run（M1-11，已接入；等待你点击运行）",
            icon="🧪",
        )
    else:
        st.divider()
        _render_result(
            result,
            source_text=str(st.session_state.get("qd_bt_source") or ""),
            display_name=str(st.session_state.get("qd_bt_display_name") or ""),
        )
        if st.button("🧹 清除本次结果", key="qd_bt_clear"):
            for key in ("qd_bt_result", "qd_bt_source", "qd_bt_display_name", "qd_bt_started_at"):
                st.session_state.pop(key, None)
            st.rerun()

    # ---- 历史回测（backtest_runs 落库；可加载历史结果）----
    with st.container(border=True):
        st.markdown("##### 历史回测（最近 20 次）")
        try:
            from core.db import session_scope
            from backtest.persistence import list_backtest_runs, load_backtest_result

            with session_scope() as _session:
                runs = list_backtest_runs(_session, limit=20)
        except Exception as exc:
            runs = []
            C.render_error_card("历史回测读取失败", message=f"{type(exc).__name__}: {exc}",
                                suggestion="确认数据库已初始化（侧边栏「数据库：就绪」）；"
                                           "每次成功回测会自动落库，可先运行一次回测。",
                                trace_id=C.new_trace_id())
        if runs:
            display = []
            for row in runs:
                metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
                display.append({
                    "run_id": str(row.get("run_id") or row.get("id") or "")[:8],
                    "策略": row.get("strategy_key"),
                    "区间": f"{row.get('start_date') or ''} ~ {row.get('end_date') or ''}",
                    "总收益": C.pct(metrics.get("total_return"), signed=True),
                    "最大回撤": C.pct(metrics.get("max_drawdown")),
                    "交易次数": metrics.get("trade_count"),
                    "运行时间": C.fmt_datetime(row.get("created_at")),
                })
            C.data_table(pd.DataFrame(display), height=240)
            labels = {f"{item['run_id']} · {item['策略']} · {item['区间']}": str(item["run_id"])
                      for item in display}
            chosen = st.selectbox("选择要加载的历史回测", list(labels),
                                  key="qd_bt_history_pick")
            c_load, c_hint = st.columns([1, 3])
            with c_load:
                if st.button("📂 加载该回测结果", use_container_width=True, key="qd_bt_history_load"):
                    target = labels[chosen]
                    full_id = next((str(row.get("run_id") or row.get("id")) for row in runs
                                    if str(row.get("run_id") or row.get("id", "")).startswith(target)), "")
                    try:
                        with session_scope() as _session:
                            loaded = load_backtest_result(_session, full_id) if full_id else None
                    except Exception as exc:
                        loaded = None
                        C.render_error_card("加载历史回测失败", message=f"{type(exc).__name__}: {exc}",
                                            suggestion="该记录可能已被清理；请重新运行一次回测。",
                                            trace_id=C.new_trace_id())
                    if loaded is not None:
                        st.session_state["qd_bt_result"] = loaded
                        st.session_state["qd_bt_source"] = f"历史记录 {target}"
                        st.session_state["qd_bt_display_name"] = str(row.get("strategy_key") or "")
                        st.success("已加载历史回测结果，下方展示对应报告与导出按钮。")
                        st.rerun()
            with c_hint:
                st.caption("每次成功回测自动写入 backtest_runs；加载后可查看原权益曲线/交易明细并重新导出报告。")
        else:
            C.not_ready("暂无历史回测记录",
                        "每次成功运行回测会自动写入 backtest_runs 表；运行一次后此处会显示最近 20 次记录，"
                        "可点击加载查看原结果并导出报告。", icon="🧪")

    if service_error:
        st.caption(f"部分服务不可用，已降级：{service_error}")

    C.todo_expander(TODO_ITEMS)

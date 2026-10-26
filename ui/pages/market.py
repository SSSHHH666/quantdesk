"""页面：行情（M2 接线版）。

职责（SPEC §10.2）：标的搜索/自选池、区间与复权选择、K 线 + MA5/10/20/60 + 成交量/
副图指标、最新价/涨跌幅/成交额、数据质量标记、最近相关信号，以及「去回测 / 去下单」联动。

接线（只调用服务层，本文件不直连网络、不写 SQL）：
    data.provider.build_provider()          # 主备数据源（akshare 失败时数据层自动 mock 兜底）
    data.cache.BarCache.load / update       # 本地缓存读；「拉取/刷新数据」为显式写缓存
    DataProvider.get_symbols / health_check
    strategy.base.sma / rsi / macd          # 均线与副图（指标兼容层，TA-Lib 缺失时 pandas 兜底）

性能约定：默认只用「自选池 + 基准」生成标的列表，避免首屏拉全市场名录；用户输入搜索词时
才加载全市场名录（缓存 10 分钟）。最新价/涨跌幅按最新日线收盘计算，不调用较慢的快照接口。
异常处理：数据/服务异常一律转中文错误卡片（含 trace_id + 建议），页面不白屏。
"""

from __future__ import annotations

import datetime as dt
import traceback

import pandas as pd
import streamlit as st

from . import _common as C
from . import _watchlist_store as store

PAGE_TITLE = "行情"
PAGE_ICON = "📈"
PAGE_ORDER = 2

TODO_ITEMS = [
    "BarCache.validate(...) -> ValidationReport（DQ001–DQ007 数据质量标记）",
    "实时快照 / 分时图（akshare 快照接口较慢，待 M1-02 提供缓存版快照后接入）",
    "strategy_signals 按标的查询（最近 5 条相关信号，M3-03）",
    "自选池「加入/移出」写入（设置页 → 数据源 → 自选池，M3-07）",
]

ADJUST_OPTIONS: dict[str, str] = {"前复权": "qfq", "后复权": "hfq", "不复权": "none"}
RANGE_OPTIONS: dict[str, int | None] = {
    "近 1 月": 30, "近 3 月": 90, "近 1 年": 365, "近 3 年": 1095, "自定义": None,
}
SUB_OPTIONS = ["成交量", "RSI", "MACD", "无"]
MA_PERIODS = ((5, "#f2a900"), (10, "#7b61ff"), (20, "#1a73e8"), (60, "#5f6368"))
BENCHMARK_SYMBOL = "000300.SH"


# ---------------------------------------------------------------------------
# 服务接入（缓存：health 60s / 名录 10min / 行情 60s；写操作由按钮显式触发）
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def _provider():
    """数据源单例：按 config.data 组装主备链（akshare 失败时数据层自动 mock 兜底）。"""
    from data.provider import build_provider

    return build_provider()


@st.cache_resource(show_spinner=False)
def _cache(provider):
    """本地行情缓存单例（BarCache）：读缓存 / 按钮触发增量更新。"""
    from data.cache import BarCache

    return BarCache(provider=provider)


@st.cache_data(ttl=60, show_spinner=False)
def _health() -> dict:
    """数据源健康检查（状态徽标用）。"""
    health = _provider().health_check()
    return {
        "name": health.name,
        "status": health.status.value,
        "message": health.message,
        "latency_ms": health.latency_ms,
    }


@st.cache_data(ttl=600, show_spinner=False)
def _symbol_options() -> list[dict]:
    """全市场标的名录（仅用户搜索时加载；akshare 限流，首次可能数秒）。"""
    frame = _provider().get_symbols("ALL")
    if frame is None or frame.empty:
        return []
    return [
        {
            "symbol": str(row.get("symbol") or ""),
            "name": str(row.get("name") or ""),
            "is_st": bool(row.get("is_st", False)),
            "kind": str(row.get("kind") or "STOCK"),
        }
        for _, row in frame.iterrows()
    ]


@st.cache_data(ttl=60, show_spinner=False)
def _load_bars(symbol: str, start_iso: str, end_iso: str, adjust: str) -> dict:
    """读日线：优先本地缓存（BarCache.load），无缓存时直连数据源并标注来源。"""
    start = dt.date.fromisoformat(start_iso)
    end = dt.date.fromisoformat(end_iso)
    provider = _provider()
    cache_error = ""
    try:
        cache = _cache(provider)
        cached = cache.load([symbol], start, end, adjust=adjust)
        if cached is not None and not cached.empty:
            latest = cache.latest_date([symbol])
            return {
                "bars": cached,
                "origin": "cache",
                "origin_text": "本地缓存",
                "latest": str(latest) if latest else "",
                "cache_error": "",
            }
    except Exception as exc:  # 数据库未初始化/表缺失：降级为数据源直连
        cache_error = f"{type(exc).__name__}: {exc}"

    direct = provider.get_bars([symbol], start, end, "1d")
    if direct is not None and not direct.empty:
        return {
            "bars": direct,
            "origin": "provider",
            "origin_text": f"数据源直连（{provider.name}）",
            "latest": "",
            "cache_error": cache_error,
        }
    return {
        "bars": pd.DataFrame(),
        "origin": "empty",
        "origin_text": "无数据",
        "latest": "",
        "cache_error": cache_error,
    }


def _refresh_bars(symbol: str, start: dt.date, end: dt.date) -> None:
    """按钮触发：从数据源增量拉取并写入本地缓存（唯一写操作，失败给中文错误卡）。"""
    try:
        with st.spinner("正在从数据源拉取并写入本地缓存…"):
            report = _cache(_provider()).update([symbol], start, end)
        st.success(report.message)
        C.clear_caches()  # 清数据缓存，确保随后重读拿到新数据
        st.rerun()
    except Exception:
        C.render_error_card(
            "拉取数据失败",
            message="未能从数据源拉取行情并写入本地缓存。",
            suggestion="检查数据源配置与网络；若为数据库未初始化，请在侧边栏确认「数据库：就绪」。",
            trace_id=C.new_trace_id(),
            exc=traceback.format_exc(),
        )


# ---------------------------------------------------------------------------
# 格式化 / 绘图辅助
# ---------------------------------------------------------------------------
def _fmt_amount(value: object) -> str:
    """成交额中文单位（亿/万），无数据返回破折号。"""
    try:
        amount = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return C.DASH
    if amount >= 1e8:
        return f"{amount / 1e8:,.2f} 亿"
    if amount >= 1e4:
        return f"{amount / 1e4:,.2f} 万"
    return C.money(amount, digits=2)


def _bar_sources(bars: pd.DataFrame) -> list[str]:
    """行情实际来源（数据层在 akshare 单标的失败时会 mock 兜底，source 列可追溯）。"""
    if bars is None or bars.empty or "source" not in bars.columns:
        return []
    return sorted({str(v) for v in bars["source"].dropna().unique() if str(v)})


def _kline_chart(bars: pd.DataFrame, symbol: str, sub: str) -> None:
    """K 线 + 均线 + 副图（plotly candlestick；A 股配色：涨红跌绿）。"""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    frame = bars.sort_values("date").reset_index(drop=True).copy()
    frame["date"] = pd.to_datetime(frame["date"])
    show_sub = sub != "无"
    rows, heights = (2, [0.74, 0.26]) if show_sub else (1, [1.0])
    fig = make_subplots(rows=rows, cols=1, shared_xaxes=True, vertical_spacing=0.04,
                        row_heights=heights)

    fig.add_trace(
        go.Candlestick(
            x=frame["date"], open=frame["open"], high=frame["high"],
            low=frame["low"], close=frame["close"], name="K 线",
            increasing_line_color=C.COLOR_UP, increasing_fillcolor=C.COLOR_UP,
            decreasing_line_color=C.COLOR_DOWN, decreasing_fillcolor=C.COLOR_DOWN,
        ),
        row=1, col=1,
    )

    try:
        from strategy import sma as _sma

        def _ma(series: pd.Series, period: int) -> pd.Series:
            return _sma(series, period)
    except Exception:  # pragma: no cover - 指标层不可用时兜底
        def _ma(series: pd.Series, period: int) -> pd.Series:
            return series.rolling(period, min_periods=period).mean()

    close = pd.to_numeric(frame["close"], errors="coerce")
    for period, color in MA_PERIODS:
        fig.add_trace(
            go.Scatter(x=frame["date"], y=_ma(close, period).round(4), name=f"MA{period}",
                       mode="lines", line=dict(width=1.1, color=color)),
            row=1, col=1,
        )

    if show_sub and sub == "成交量":
        colors = [
            C.COLOR_UP if c >= o else C.COLOR_DOWN
            for c, o in zip(frame["close"], frame["open"])
        ]
        fig.add_trace(
            go.Bar(x=frame["date"], y=frame["volume"], name="成交量",
                   marker_color=colors, opacity=0.75),
            row=2, col=1,
        )
    elif show_sub and sub == "RSI":
        try:
            from strategy import rsi as _rsi

            values = _rsi(close, 14)
        except Exception as exc:
            st.warning(f"RSI 指标不可用（{exc}），已跳过副图。")
        else:
            fig.add_trace(go.Scatter(x=frame["date"], y=values, name="RSI(14)",
                                     mode="lines", line=dict(color="#7b61ff", width=1.2)),
                          row=2, col=1)
            fig.add_hline(y=70, line_dash="dot", line_color=C.COLOR_MUTED, row=2, col=1)
            fig.add_hline(y=30, line_dash="dot", line_color=C.COLOR_MUTED, row=2, col=1)
    elif show_sub and sub == "MACD":
        try:
            from strategy import macd as _macd

            macd_line, signal_line, hist = _macd(close)
        except Exception as exc:
            st.warning(f"MACD 指标不可用（{exc}），已跳过副图。")
        else:
            hist_colors = [C.COLOR_UP if v >= 0 else C.COLOR_DOWN for v in hist.fillna(0.0)]
            fig.add_trace(go.Bar(x=frame["date"], y=hist, name="MACD 柱",
                                 marker_color=hist_colors, opacity=0.75), row=2, col=1)
            fig.add_trace(go.Scatter(x=frame["date"], y=macd_line, name="DIF",
                                     mode="lines", line=dict(color="#1a73e8", width=1.1)),
                          row=2, col=1)
            fig.add_trace(go.Scatter(x=frame["date"], y=signal_line, name="DEA",
                                     mode="lines", line=dict(color="#f2a900", width=1.1)),
                          row=2, col=1)

    fig.update_layout(
        height=560 if show_sub else 430,
        margin=dict(l=8, r=8, t=28, b=8),
        legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0),
        hovermode="x unified",
        xaxis_rangeslider_visible=False,
        title=f"{symbol} 日线（价格单位：元）",
    )
    fig.update_yaxes(title_text="价格", row=1, col=1)
    if show_sub:
        fig.update_yaxes(title_text=sub, row=2, col=1)
    st.plotly_chart(fig, use_container_width=True)


def _last_two(bars: pd.DataFrame) -> tuple[dict, dict]:
    """取最近两根日线（用于按收盘价计算最新价/涨跌幅，避免额外的慢接口调用）。"""
    frame = bars.sort_values("date")
    if len(frame) < 2:
        last = frame.iloc[-1].to_dict() if len(frame) else {}
        return last, {}
    return frame.iloc[-1].to_dict(), frame.iloc[-2].to_dict()


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
def render() -> None:
    C.page_header("行情", "K 线、均线与成交量；一键去回测 / 去下单（M2 接线版）", badge="M2 接线")
    meta = C.app_meta()
    universe = [s for s in (meta.get("universe") or []) if s]

    # ---- 数据源（失败也不阻塞页面）----
    provider = None
    service_error = ""
    try:
        with st.spinner("连接数据源…"):
            provider = _provider()
    except Exception as exc:
        service_error = f"{type(exc).__name__}: {exc}"

    # ---- 标的候选：默认仅自选池 + 基准（首屏快）；搜索时加载全市场名录 ----
    base_rows: list[dict] = []
    for item in store.load_items():  # 自选池（ui/pages/_watchlist_store.py，data/watchlist.py 待交付）
        base_rows.append({"symbol": item["symbol"], "name": item.get("name") or "",
                          "is_st": False, "kind": "STOCK"})
    for symbol in universe:  # config.data.universe 兜底
        if all(row["symbol"] != symbol for row in base_rows):
            base_rows.append({"symbol": symbol, "name": "", "is_st": False, "kind": "STOCK"})
    if all(row["symbol"] != BENCHMARK_SYMBOL for row in base_rows):
        base_rows.append({"symbol": BENCHMARK_SYMBOL, "name": "沪深300", "is_st": False,
                          "kind": "INDEX"})
    merged: dict[str, dict] = {row["symbol"]: row for row in base_rows}
    rows = list(merged.values())

    c1, c2, c3 = st.columns([1.6, 1.1, 1.1])
    with c1:
        query = st.text_input(
            "标的搜索", value="", key="qd_market_search",
            placeholder="代码或名称，如 600519 / 贵州茅台（输入后可搜全市场）",
            help="留空时下拉只含自选池与基准；输入关键词后加载全市场名录（akshare 首次可能数秒）。",
        ).strip().lower()
    if query and provider is not None:
        try:
            with st.spinner("载入全市场标的名录…"):
                loaded = _symbol_options()
            if loaded:
                merged = {row["symbol"]: row for row in loaded}
                for row in rows:  # 自选池/基准始终保留
                    merged.setdefault(row["symbol"], row)
                rows = list(merged.values())
        except Exception as exc:
            st.caption(f"全市场名录加载失败，已回退自选池：{exc}")
    filtered = [
        row for row in rows
        if not query or query in row["symbol"].lower() or query in row["name"].lower()
    ] or rows

    labels = [f"{row['symbol']} {row['name']}".strip() for row in filtered]
    label_to_row = dict(zip(labels, filtered))
    # 页面切换/搜索过滤后仍保留用户上次选择的标的（keyless selectbox + 自管状态，避免选项变化冲突）
    preferred_symbol = str(
        st.session_state.get("qd_market_selected")
        or st.session_state.get("qd_symbol")
        or ""
    ).strip()
    default_index = next(
        (i for i, row in enumerate(filtered) if preferred_symbol and row["symbol"] == preferred_symbol), 0
    )
    with c1:
        selected_label = st.selectbox(
            "标的", options=labels, index=default_index,
            label_visibility="collapsed",
        )
    selected_row = label_to_row[selected_label]
    symbol = selected_row["symbol"]
    st.session_state["qd_market_selected"] = symbol

    with c2:
        range_label = st.selectbox("区间", list(RANGE_OPTIONS), index=2, key="qd_market_range")
    with c3:
        adjust_label = st.selectbox("复权方式", list(ADJUST_OPTIONS), index=0,
                                    key="qd_market_adjust",
                                    help="行情默认前复权；回测默认后复权（hfq）。")
    adjust = ADJUST_OPTIONS[adjust_label]

    if RANGE_OPTIONS[range_label] is None:
        d1, d2 = st.columns(2)
        with d1:
            start = st.date_input("开始日期", value=dt.date.today() - dt.timedelta(days=365),
                                  key="qd_market_start")
        with d2:
            end = st.date_input("结束日期", value=dt.date.today(), key="qd_market_end")
    else:
        end = dt.date.today()
        start = end - dt.timedelta(days=int(RANGE_OPTIONS[range_label]))

    b1, b2, b3 = st.columns([1.1, 1.1, 2.2])
    with b1:
        if st.button("🔄 拉取/刷新数据", use_container_width=True, key="qd_market_refresh"):
            if provider is None:
                C.render_error_card(
                    "数据源不可用，无法拉取",
                    message=service_error or "数据源未初始化",
                    suggestion="检查 config.yaml 的 data.provider 配置与依赖安装。",
                    trace_id=C.new_trace_id(),
                )
            else:
                _refresh_bars(symbol, start, end)
    with b2:
        if st.button("⭐ 加入自选", use_container_width=True, key="qd_market_add_watch"):
            ok, message = store.add_item(
                symbol, selected_row.get("name") or "", store.DEFAULT_GROUP
            )
            if ok:
                st.success(message)
            else:
                st.info(message)
    with b3:
        if st.button("⭐ 管理自选池 →", key="qd_market_go_watchlist"):
            C.goto_page("自选池")
        st.caption("「拉取/刷新数据」调用 BarCache.update 增量写入本地缓存（显式操作）；"
                   "批量初始化可先 scripts/init_db.py，再由 scripts/fetch_data.py（M1-13）执行。")

    # ---- 加载行情 ----
    result: dict = {"bars": pd.DataFrame(), "origin": "empty", "origin_text": "无数据",
                    "latest": "", "cache_error": ""}
    if provider is not None:
        try:
            with st.spinner("读取行情…"):
                result = _load_bars(symbol, start.isoformat(), end.isoformat(), adjust)
        except Exception:
            C.render_error_card(
                "读取行情失败",
                message="数据源与本地缓存均未能返回行情。",
                suggestion="点击「拉取/刷新数据」重试；若数据库未就绪，请在侧边栏确认「数据库：就绪」。",
                trace_id=C.new_trace_id(),
                exc=traceback.format_exc(),
            )
    bars: pd.DataFrame = result["bars"]
    sources = _bar_sources(bars)

    # ---- 状态徽标 ----
    health: dict = {}
    if provider is not None:
        try:
            health = _health()
        except Exception as exc:
            health = {"name": "", "status": "DOWN",
                      "message": f"健康检查失败：{exc}", "latency_ms": None}
    provider_name = str(health.get("name") or (provider.name if provider else ""))
    health_status = str(health.get("status") or "DOWN")
    config_provider = str(meta.get("provider") or "")
    degraded = bool(config_provider and provider_name and provider_name != config_provider)
    src_tone = "ok" if health_status == "OK" and not degraded else (
        "error" if health_status == "DOWN" else "warn")
    src_text = C.PROVIDER_STATUS_LABELS.get(health_status, health_status)
    if degraded:
        src_text = f"{provider_name}（{src_text}，已从 {config_provider} 降级）"

    origin_text = result["origin_text"]
    if sources:
        origin_text = f"{origin_text} · 行情源 {', '.join(sources)}"
    C.status_bar(
        [
            {"label": "数据来源", "value": origin_text,
             "tone": "ok" if result["origin"] == "cache" else (
                 "warn" if result["origin"] == "provider" else "muted"),
             "help": "本地缓存（BarCache）优先；无缓存时直连数据源"},
            {"label": "数据源", "value": src_text, "tone": src_tone,
             "help": str(health.get("message") or "健康检查结果")},
            {"label": "复权", "value": adjust_label, "tone": "muted"},
            {"label": "数据点", "value": f"{len(bars):,} 根" if len(bars) else "无数据",
             "tone": "ok" if len(bars) else "warn",
             "help": f"区间 {start} ~ {end}"},
        ]
    )
    if degraded:
        C.render_warning_card(
            "数据源已降级（离线演示）",
            f"配置主数据源为 {config_provider}，当前实际使用 {provider_name}：行情为确定性 Mock "
            "示例数据，仅供功能演示，不能用于实盘决策。",
            suggestion="待 akshare Provider（M1-02）可用后自动切换；也可在设置页把数据源改为 mock。",
        )
    elif "mock" in sources:
        C.render_warning_card(
            "部分行情来自 mock 兜底",
            "数据层在部分标的 akshare 请求失败时自动使用 Mock 示例数据（source=mock），"
            "该部分行情仅供演示。",
            suggestion="网络恢复后点击「拉取/刷新数据」重新拉取真实行情。",
        )
    if result.get("cache_error"):
        st.caption(f"本地缓存不可用，已回退数据源直连（{result['cache_error']}）。")

    # ---- 无数据引导 ----
    if bars.empty:
        C.empty_state(
            "该区间没有可用行情",
            f"标的 {symbol} 在 {start} ~ {end} 没有取到数据。可点击上方「拉取/刷新数据」从数据源拉取并写入缓存；"
            "或运行 scripts/fetch_data.py（M1-13）批量初始化自选池数据。",
            page="设置",
            button_label="去设置页检查数据源",
        )
        C.todo_expander(TODO_ITEMS)
        return

    # ---- K 线主图 + 右侧指标 ----
    sub = st.selectbox("副图指标", SUB_OPTIONS, index=0, key="qd_market_sub",
                       help="成交量 / RSI(14) / MACD(12,26,9) / 无")
    left, right = st.columns([2.35, 1])
    with left:
        try:
            _kline_chart(bars, symbol, sub)
        except Exception:
            C.render_error_card(
                "K 线绘制失败",
                message="行情已取到，但图表渲染出错。",
                suggestion="切换区间或副图后重试；若持续出现，请把 trace_id 提供给维护者。",
                trace_id=C.new_trace_id(),
                exc=traceback.format_exc(),
            )
    with right:
        last, prev = _last_two(bars)
        close = last.get("close")
        prev_close = prev.get("close")
        try:
            change = float(close) - float(prev_close) if prev_close is not None else None
            change_pct = (
                change / float(prev_close) * 100.0
                if change is not None and float(prev_close) != 0
                else None
            )
        except (TypeError, ValueError):
            change = change_pct = None
        C.kpi_card(
            "最新价（收盘）", C.money(close),
            delta=(f"{change:+.2f} 元" if change is not None else None),
            delta_value=change,
            help_text="最新日线收盘价（元）；实时快照待 M1-02 优化后接入",
        )
        C.kpi_card(
            "涨跌幅", C.pct(change_pct, ratio=False, signed=True),
            help_text="相对前一交易日收盘的涨跌幅（%）；涨红跌绿",
            tone=("up" if (change_pct or 0) > 0 else ("down" if (change_pct or 0) < 0 else "flat")),
        )
        C.kpi_card("成交额", _fmt_amount(last.get("amount")),
                   help_text="最新交易日成交额（元）")
        st.caption(f"最新交易日：{C.fmt_date(last.get('date'))}　"
                   f"缓存最新日期：{result.get('latest') or '—'}")
        C.status_bar(
            [
                {"label": "ST", "value": "是" if selected_row.get("is_st") else "否",
                 "tone": "warn" if selected_row.get("is_st") else "ok",
                 "help": "ST/退市整理：风控自动黑名单（RK002）"},
                {"label": "停牌", "value": _suspended_text(bars), "tone": "muted",
                 "help": "is_suspended 标记（DQ007）"},
                {"label": "类型", "value": "指数" if selected_row.get("kind") == "INDEX" else "股票",
                 "tone": "muted"},
            ]
        )
        st.caption("数据质量校验（DQ001–DQ007）报告待 M1-04 报告页面接入。")

    # ---- 最近相关信号（服务未就绪，占位）----
    C.table_placeholder(
        "最近 5 条相关信号",
        ["日期", "策略", "方向", "强度", "原因", "状态"],
        hint="数据未就绪：等待 strategy_signals 查询接口（M3-03）",
        todo="strategy_signals 按标的查询",
    )

    # ---- 跳转联动 ----
    b1, b2, b3 = st.columns([1, 1, 3])
    with b1:
        if st.button("去回测 →", key="qd_market_go_backtest"):
            C.goto_page("回测", symbol=symbol)
    with b2:
        if st.button("去下单 →", key="qd_market_go_trade"):
            C.goto_page("手动交易", symbol=symbol)
    with b3:
        st.caption("跳转会带上当前标的，目标页读取 qd_symbol 预填。")

    C.todo_expander(TODO_ITEMS)


def _suspended_text(bars: pd.DataFrame) -> str:
    """最近一根 K 线是否停牌（无该列时显示「未知」）。"""
    if "is_suspended" not in bars.columns or bars.empty:
        return "未知"
    try:
        return "是" if bool(bars.sort_values("date").iloc[-1]["is_suspended"]) else "否"
    except Exception:
        return "未知"

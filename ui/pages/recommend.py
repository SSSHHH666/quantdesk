"""页面：今日推荐 / 异动雷达（R4 新增，第 11 页）。

两个 Tab（对应 ``strategy/recommend.py`` 的两套打分）：
1. **远期精选**：``recommend(..., strategy_key="value_quality")`` —— 长期质量 + 动量
   （120 日动量 / 趋势稳定 / 低波动 / 回撤可控 / 上涨占比 截面 z-score 加权）；
2. **近期异动**：``recommend(..., strategy_key="breakout_abnormal")`` —— 量比放大 +
   突破 20 日高点 + 温和涨幅（排除暴涨停与放量下跌）。

每行展示：代码、名称、现价、涨跌幅、成交量比、突破幅度、推荐理由（得分分解），
并提供「加入自选」按钮（写入自选池：自选池长期精选 → 「远期」分组，全市场短线雷达 → 「近期」分组）。

数据来源：``strategy/recommend.py``（Dev2 已交付）；行情经 ``build_provider()`` 注入，
与回测/策略共用同一套数据清洗口径。服务不可用/数据不足时显示明确中文占位与建议，
**不报错、不白屏**；任何异常都会变成中文错误卡片（带 trace_id）。
"""

from __future__ import annotations

import datetime as dt
import traceback

import pandas as pd
import streamlit as st

from . import _common as C
from . import _watchlist_store as store

PAGE_TITLE = "今日推荐"
PAGE_ICON = "🎯"
PAGE_ORDER = 3

TODO_ITEMS = [
    "推荐结果落库（strategy_signals / 新表）与历史对比（M4 阶段）",
    "得分分解可视化（因子贡献柱状图，M5 视觉统一时）",
    "ML 混合开关（strategy.recommend 的 use_ml / ml_weight，待 ml_ranker 验收）",
    "推荐结果推送（Notifier，M3-04）",
]

TABS = ("全市场短线雷达", "自选池长期精选")
#: Tab → strategy.recommend 的策略 key（打分逻辑由服务层实现，页面不重算）
KIND_STRATEGY = {"全市场短线雷达": "breakout_abnormal", "自选池长期精选": "value_quality"}
#: 加入自选时的目标分组
KIND_GROUP = {"全市场短线雷达": "近期", "自选池长期精选": "远期"}
#: 名称兜底（data.market_snapshot.get_names() 未就绪时，至少不显示 None）
_NAME_FALLBACK = {"600519.SH": "贵州茅台", "000001.SZ": "平安银行", "300750.SZ": "宁德时代", "688981.SH": "中芯国际", "601318.SH": "中国平安", "000858.SZ": "五粮液", "002594.SZ": "比亚迪", "600036.SH": "招商银行", "000004.SZ": "*ST国华", "601899.SH": "紫金矿业", "000300.SH": "沪深300", "000001.SH": "上证指数", "899050.BJ": "北证50"}
#: 短线模型验证状态（Dev2 v1 OOS 命中率不足，v2 验证中；规则打分仅作研究参考）
RADAR_RESEARCH_NOTICE = (
    "研究阶段 · 短线模型验证中：Dev2 短线模型 v1 实测 OOS 命中率不足、暂无可用的排序能力，"
    "当前「全市场短线雷达」使用**规则打分**（量比放大 + 突破 + 温和涨幅）仅作研究参考，"
    "不代表未来收益、不构成买入建议，也**不暗示买入即上涨**。请勿据此实盘交易。"
)

METHOD_TEXT = {
    "自选池长期精选": "策略 key=value_quality：质量/趋势截面打分（120 日动量、60 日趋势稳定性、"
              "低波动、低回撤、上涨占比 z-score 加权），按得分降序取 Top N；"
              "建议放入「远期」自选分组观察。",
    "全市场短线雷达": "策略 key=breakout_abnormal（短线模型接入前暂用规则打分）：量比 ≥1.5、收盘突破前 20 日高点、当日涨幅 2%~9.5%"
              "（排除暴涨停与放量下跌），按异动强度打分；建议放入「近期」分组跟踪。",
}


# ---------------------------------------------------------------------------
# 服务接入（推荐 + 行情快照；均带 TTL 缓存，避免重复拉数）
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def _provider():
    from data.provider import build_provider

    return build_provider()


@st.cache_data(ttl=300, show_spinner=False)
def _quotes(symbols: tuple[str, ...]) -> dict[str, dict]:
    """推荐结果的最新价/涨跌幅（一次批量取最近 10 天日线；失败返回空）。"""
    if not symbols:
        return {}
    try:
        provider = _provider()
        end = dt.date.today()
        frame = provider.get_bars(list(symbols), end - dt.timedelta(days=10), end, "1d")
    except Exception:
        return {}
    if frame is None or frame.empty:
        return {}
    result: dict[str, dict] = {}
    for symbol, group in frame.groupby("symbol"):
        group = group.sort_values("date")
        last = group.iloc[-1]
        close = float(last["close"])
        prev_close = float(group.iloc[-2]["close"]) if len(group) > 1 else close
        result[str(symbol)] = {
            "last": close,
            "change_pct": ((close / prev_close - 1.0) * 100.0) if prev_close else 0.0,
        }
    return result


@st.cache_data(ttl=300, show_spinner=False)
def _recommendations(kind: str, universe: tuple[str, ...], as_of_iso: str) -> dict:
    """调用 strategy.recommend；返回 ``{"rows": [...], "error": "", "as_of": ...}``。"""
    strategy_key = KIND_STRATEGY[kind]
    try:
        from strategy.recommend import recommend
    except Exception as exc:
        return {
            "rows": [],
            "error": f"推荐服务未就绪（strategy/recommend.py 不可用：{type(exc).__name__}: {exc}）",
            "as_of": as_of_iso,
        }
    if not universe:
        return {"rows": [], "error": "自选池为空：请先到「自选池」页添加 3–10 只标的", "as_of": as_of_iso}
    try:
        picks = recommend(strategy_key, list(universe), as_of_iso, top_n=10,
                          data_source=_provider())
    except Exception as exc:
        return {
            "rows": [],
            "error": (f"推荐计算失败（{strategy_key}）：{type(exc).__name__}: {exc}"
                      "；请确认自选池标的有足够历史数据（建议先到行情页拉取数据）"),
            "as_of": as_of_iso,
        }
    if not picks:
        return {"rows": [], "error": "推荐引擎未返回结果（可能行情不足或当日无满足条件的标的）",
                "as_of": as_of_iso}

    quotes = _quotes(tuple(str(pick.symbol) for pick in picks))
    rows: list[dict] = []
    for pick in picks:
        data = pick.as_dict()
        factors = data.get("factors") or {}
        quote = quotes.get(str(pick.symbol), {})
        change_pct = factors.get("gain_today")
        rows.append(
            {
                "symbol": str(pick.symbol),
                "rank": int(data.get("rank") or 0),
                "score": float(data.get("score") or 0.0),
                "qualified": bool(data.get("qualified", True)),
                "price": factors.get("last_close") or quote.get("last"),
                "change_pct": (change_pct * 100.0) if change_pct is not None
                                else quote.get("change_pct"),
                "volume_ratio": factors.get("volume_ratio_5_20")
                                or factors.get("volume_ratio_1_20"),
                "breakout_pct": (factors.get("breakout_20") * 100.0)
                                if factors.get("breakout_20") is not None else None,
                "reason": str(data.get("reason") or ""),
                "components": dict(data.get("components") or {}),
                "note": str(data.get("note") or ""),
            }
        )
    return {"rows": rows, "error": "", "as_of": as_of_iso}


# ---------------------------------------------------------------------------
# 展示辅助
# ---------------------------------------------------------------------------
def _ratio_text(value: object, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return C.DASH


def _reason_text(row: dict) -> str:
    """推荐理由：服务层中文理由 + 得分分解（components 若为空则只显示理由）。"""
    reason = str(row.get("reason") or "").strip()
    components = row.get("components") or {}
    if components:
        parts = [f"{name} {float(value):+.2f}" for name, value in components.items()
                 if abs(float(value)) >= 0.01]
        if parts:
            reason = f"{reason}（得分分解：{'、'.join(parts)}）"
    if row.get("note"):
        reason = f"{reason}；{row['note']}"
    return reason or "（服务层未提供理由）"


def _add_to_watchlist(symbol: str, name: str, group: str) -> None:
    if not symbol:
        C.render_error_card(
            "无法加入自选",
            message="该推荐行缺少有效代码。",
            suggestion="等待推荐服务返回带代码的结果，或到自选池页手动添加。",
            trace_id=C.new_trace_id(),
        )
        return
    ok, message = store.add_item(symbol, name or "", group)
    if ok:
        st.success(f"{message}（可在「自选池」页查看）")
    else:
        st.info(message)


def _render_rows(rows: list[dict], kind: str, names: dict[str, str]) -> None:
    """逐行渲染推荐结果（含「加入自选」按钮）。"""
    group = KIND_GROUP[kind]
    for row in rows[:30]:
        symbol = str(row.get("symbol") or "")
        name = names.get(symbol) or _NAME_FALLBACK.get(symbol) or "（名称待补）"
        change_pct = row.get("change_pct")
        columns = st.columns([0.5, 1.35, 1.2, 0.95, 0.95, 0.95, 0.9, 3.0, 1.0])
        with columns[0]:
            st.caption(f"#{row.get('rank')}")
        with columns[1]:
            st.markdown(f"**{symbol}**")
        with columns[2]:
            st.caption(name or C.DASH)
        with columns[3]:
            st.markdown(C.money(row.get("price")))
        with columns[4]:
            st.markdown(C.colored(C.pct(change_pct, ratio=False, signed=True), change_pct),
                        unsafe_allow_html=True)
        with columns[5]:
            st.markdown(_ratio_text(row.get("volume_ratio")))
        with columns[6]:
            st.markdown(C.pct(row.get("breakout_pct"), ratio=False, signed=True)
                        if row.get("breakout_pct") is not None else C.DASH)
        with columns[7]:
            st.caption(_reason_text(row))
            st.caption("风险提示：规则打分仅供参考（模型验证中），不构成买入建议；短线追高风险大，请自行判断并严格止损。" if kind == "全市场短线雷达" else "风险提示：长期精选仍需自行判断估值与回撤风险，不构成投资建议。")
        with columns[8]:
            if st.button("加入自选", key=f"qd_rec_add_{kind}_{symbol}_{row.get('rank')}",
                         use_container_width=True):
                _add_to_watchlist(symbol, name, group)
        st.divider()


# ---------------------------------------------------------------------------
# Tab 内容
# ---------------------------------------------------------------------------
def _render_tab(kind: str) -> None:
    if kind == "全市场短线雷达":
        C.render_warning_card("⚠️ 研究阶段提示", RADAR_RESEARCH_NOTICE)
    C.render_info_card(f"{kind} · 打分方法", METHOD_TEXT[kind])

    # 股票池：自选池优先（strategy/recommend 的 universe 参数）
    watch_items = store.load_items()
    names = {item["symbol"]: (item.get("name") or _NAME_FALLBACK.get(item["symbol"], "")) for item in watch_items}
    universe = tuple(item["symbol"] for item in watch_items)
    provider_name = ""
    try:
        provider_name = str(_provider().name)
    except Exception:
        provider_name = "unknown"

    c1, c2, c3, c4 = st.columns([0.9, 0.9, 1.4, 2.0])
    with c1:
        if st.button("🔄 刷新推荐", use_container_width=True, key=f"qd_rec_refresh_{kind}"):
            C.clear_caches()
            st.rerun()
    with c2:
        if st.button("⭐ 去自选池", use_container_width=True, key=f"qd_rec_goto_wl_{kind}"):
            C.goto_page("自选池")
    with c3:
        st.caption(f"股票池：自选池 {len(universe)} 只 · 打分日期：{dt.date.today()}")
    with c4:
        st.caption("仅供参考，不构成投资建议；「加入自选」只写入自选池，不会下单。")

    if not universe:
        C.empty_state(
            "自选池为空，无法生成推荐",
            "推荐以自选池为候选池（建议 20–50 只）。先到「自选池」页添加标的，再回来生成推荐。",
            page="自选池",
            button_label="去自选池添加标的",
            key=f"qd_rec_empty_{kind}",
        )
        return

    with st.spinner(f"正在计算「{kind}」（{KIND_STRATEGY[kind]}）…"):
        result = _recommendations(kind, universe, dt.date.today().isoformat())

    if result["error"] or not result["rows"]:
        message = result["error"] or "推荐服务暂未返回结果"
        C.not_ready(
            f"「{kind}」暂无可展示结果",
            message + "。本页不会报错：数据/服务恢复后会自动展示推荐列表与「加入自选」按钮。",
            todo="strategy/recommend.py（已交付）；优先检查自选池标的是否有足够历史行情",
            icon="🎯",
        )
        C.table_placeholder(
            "推荐结果预览（数据就绪后显示）",
            ["代码", "名称", "现价", "涨跌幅", "成交量比", "突破幅度", "推荐理由（得分分解）", "操作"],
            hint="等待行情/推荐数据就绪",
            todo="DataProvider 取数 + strategy.recommend.recommend",
        )
        if provider_name == "mock":
            C.render_warning_card(
                "当前为 mock 离线演示数据",
                "推荐结果基于确定性示例行情，仅用于流程演示，不能用于实盘决策。",
                suggestion="待 akshare 数据可用后自动切换真实行情。",
            )
        return

    if provider_name == "mock":
        C.render_warning_card(
            "当前为 mock 离线演示数据",
            "上述推荐基于确定性示例行情，仅用于流程演示，不能用于实盘决策。",
            suggestion="待 akshare 数据可用后自动切换真实行情。",
        )

    qualified = sum(1 for row in result["rows"] if row.get("qualified"))
    C.status_bar(
        [
            {"label": "候选", "value": f"{len(result['rows'])} 只", "tone": "ok"},
            {"label": "入选", "value": f"{qualified} 只",
             "tone": "ok" if qualified else "warn",
             "help": "qualified=True 表示满足该策略的入选条件"},
            {"label": "策略", "value": KIND_STRATEGY[kind], "tone": "muted"},
            {"label": "数据源", "value": provider_name, "tone": "warn" if provider_name == "mock" else "ok"},
        ]
    )
    st.markdown(f"##### 推荐列表（{len(result['rows'])} 只，最多展示前 30 只）")
    _render_rows(result["rows"], kind, names)

    export = pd.DataFrame(
        [
            {
                "排名": row.get("rank"),
                "代码": row.get("symbol"),
                "名称": names.get(str(row.get("symbol")), ""),
                "现价": row.get("price"),
                "涨跌幅": row.get("change_pct"),
                "成交量比": row.get("volume_ratio"),
                "突破幅度": row.get("breakout_pct"),
                "得分": row.get("score"),
                "是否入选": "入选" if row.get("qualified") else "观察",
                "推荐理由": _reason_text(row),
            }
            for row in result["rows"]
        ]
    )
    C.export_csv_button(export, f"recommend_{kind}.csv", key=f"qd_rec_export_{kind}")


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
def render() -> None:
    C.page_header("今日推荐", "全市场短线雷达（规则打分·验证中）与自选池长期精选（质量+动量）",
                  badge="R4 新增")

    all_items = store.load_items()
    try:
        provider_label = _provider().name
    except Exception:
        provider_label = "unavailable"
    C.status_bar(
        [
            {"label": "推荐引擎", "value": "strategy/recommend.py", "tone": "ok",
             "help": "全市场短线雷达=breakout_abnormal；自选池长期精选=value_quality"},
            {"label": "自选池", "value": f"{len(all_items)} 只",
             "tone": "ok" if all_items else "warn"},
            {"label": "数据源", "value": provider_label,
             "tone": "warn" if provider_label == "mock" else "ok"},
            {"label": "免责", "value": "仅供研究，不构成投资建议", "tone": "muted"},
        ]
    )

    tab_labels = list(TABS)
    tab_widgets = st.tabs(tab_labels)
    for tab_widget, tab_kind in zip(tab_widgets, tab_labels):
        with tab_widget:
            _render_tab(tab_kind)

    C.todo_expander(TODO_ITEMS)

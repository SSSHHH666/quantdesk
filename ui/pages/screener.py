"""页面：全市场筛选（R8 新增，第 12 页）。

流程：按流动性（成交额）取前 N → 规则打分（成交额分位 + 温和涨幅 + 过热惩罚）→ Top N，
表格逐行展示并可「加入自选」。

数据来源：
- 优先 ``app_services.screen_market(...)``（Dev2 ``strategy/screener.py`` 就绪后接入）；
- 未就绪时用**现有数据源快照 + 原型规则**兜底，并明确标注「原型规则，验证中」。

合规红线：短线筛选为研究阶段，未经验证不代表买入建议，不承诺收益。
"""

from __future__ import annotations

import datetime as dt
import traceback

import pandas as pd
import streamlit as st

import app_services as S
from . import _common as C
from . import _ui_services as U
from . import _watchlist_store as store

PAGE_TITLE = "全市场筛选"
PAGE_ICON = "🔎"
PAGE_ORDER = 3

RISK_NOTICE = (
    "研究阶段声明：短线筛选为研究阶段，规则/模型均未经验证，不代表买入建议，不承诺收益；"
    "筛选结果仅供研究参考，请勿据此实盘交易。"
)
PROTOTYPE_NOTICE = (
    "当前为「原型规则，验证中」：正式的全市场筛选服务（app_services.screen_market / "
    "strategy/screener.py）尚未就绪，以下由数据源快照 + 简单规则打分兜底。"
)


@st.cache_resource(show_spinner=False)
def _provider():
    from data.provider import build_provider

    return build_provider()


def _rule_score(row: dict) -> tuple[float, list[str]]:
    """原型规则打分（0~100）：成交额分位 + 温和涨幅 + 过热惩罚。"""
    reasons: list[str] = []
    amount_rank = float(row.get("amount_rank") or 0.0)  # 0~1，越大越靠前
    change = float(row.get("change_pct") or 0.0)
    score = 40.0 * amount_rank
    reasons.append(f"成交额分位 {amount_rank * 100:.0f}%")
    if 0.5 <= change <= 7.0:
        score += 35.0
        reasons.append(f"温和上涨 {change:+.2f}%")
    elif 0.0 < change < 0.5:
        score += 15.0
        reasons.append(f"小幅上涨 {change:+.2f}%")
    elif change > 9.5:
        score -= 20.0
        reasons.append(f"涨幅过大 {change:+.2f}%（过热）")
    elif change < -3.0:
        score -= 15.0
        reasons.append(f"跌幅偏大 {change:+.2f}%")
    return round(score, 2), reasons


@st.cache_data(ttl=120, show_spinner=False)
def _scan(liquidity_top: int, top_n: int, exclude_hot: bool) -> dict:
    """全市场扫描（优先门面 screen_market；未就绪时快照+原型规则兜底）。"""
    screen = getattr(S, "screen_market", None)
    if callable(screen):
        ok, data, message = U.call(screen, liquidity_top=liquidity_top, top_n=top_n,
                                   exclude_hot=exclude_hot)
        if ok and data:
            return {"rows": data, "prototype": False, "message": message, "error": ""}
        fallback_message = f"screen_market 不可用（{message}），已降级原型规则"
    else:
        fallback_message = ""

    try:
        provider = _provider()
        symbols_frame = provider.get_symbols("STOCK")
        if symbols_frame is None or symbols_frame.empty:
            return {"rows": [], "prototype": True, "message": fallback_message,
                    "error": "数据源未返回标的主数据（请先到行情页刷新数据）"}
        symbols = [str(item) for item in symbols_frame["symbol"].tolist()]
        names = dict(zip(symbols_frame["symbol"], symbols_frame.get("name", pd.Series(dtype=str))))
        st_flags = dict(zip(symbols_frame["symbol"], symbols_frame.get("is_st", pd.Series(dtype=bool))))
        snapshot = provider.get_snapshot(symbols)
    except Exception as exc:
        return {"rows": [], "prototype": True, "message": fallback_message,
                "error": f"{type(exc).__name__}: {exc}"}
    if snapshot is None or snapshot.empty:
        return {"rows": [], "prototype": True, "message": fallback_message,
                "error": "快照为空：请先到行情页点击「拉取/刷新数据」"}

    frame = snapshot.copy()
    frame["amount"] = pd.to_numeric(frame.get("amount"), errors="coerce").fillna(0.0)
    frame = frame.sort_values("amount", ascending=False).reset_index(drop=True)
    frame["amount_rank"] = 1.0 - frame.index / max(len(frame) - 1, 1)
    frame = frame.head(max(1, int(liquidity_top)))
    data_time = ""
    try:
        data_time = str(pd.to_datetime(frame["ts"].dropna().max())) if "ts" in frame.columns else ""
    except Exception:
        data_time = ""

    rows: list[dict] = []
    for _, row in frame.iterrows():
        item = row.to_dict()
        change = float(item.get("change_pct") or 0.0)
        if exclude_hot and change >= 9.5:
            continue
        score, reasons = _rule_score(item)
        symbol = str(item.get("symbol"))
        risk = []
        if change >= 9.5:
            risk.append("过热（接近涨停）")
        if bool(st_flags.get(symbol, False)):
            risk.append("ST 风险")
        rows.append(
            {
                "symbol": symbol,
                "name": names.get(symbol) or "",
                "price": item.get("last"),
                "change_pct": change,
                "amount": item.get("amount"),
                "score": score,
                "reason": "；".join(reasons),
                "risk": "；".join(risk) or "—",
            }
        )
    rows.sort(key=lambda item: item["score"], reverse=True)
    rows = rows[: max(1, int(top_n))]
    return {"rows": rows, "prototype": True, "message": fallback_message,
            "error": "", "total": len(symbols), "filtered": len(frame),
            "scanned": len(rows), "data_time": data_time}


def _add_to_watchlist(symbol: str, name: str) -> None:
    ok, message = store.add_item(symbol, name, "近期")
    if ok:
        st.success(f"{message}（可在「自选池」页查看）")
    else:
        st.info(message)


def render() -> None:
    C.page_header("全市场筛选", "按流动性 + 规则打分筛选短线候选（研究阶段）", badge="R8 新增")
    C.render_warning_card("合规提示", RISK_NOTICE)

    c1, c2, c3, c4 = st.columns([1.1, 0.9, 1.0, 0.9])
    with c1:
        liquidity_top = st.number_input("流动性前 N", min_value=10, max_value=5000,
                                        value=300, step=50, key="qd_scr_liquidity")
    with c2:
        top_n = st.number_input("Top N", min_value=5, max_value=100, value=25, step=5,
                                key="qd_scr_top_n")
    with c3:
        exclude_hot = st.checkbox("剔除过热（涨幅≥9.5%）", value=True, key="qd_scr_exclude_hot")
    with c4:
        if st.button("🔄 刷新扫描", use_container_width=True, key="qd_scr_refresh"):
            C.clear_caches()
            st.rerun()

    try:
        with st.spinner("正在扫描全市场…"):
            data = _scan(int(liquidity_top), int(top_n), bool(exclude_hot))
    except Exception:
        C.render_error_card(
            "全市场扫描失败",
            message="扫描过程中发生异常（已捕获，页面不白屏）。",
            suggestion="先到行情页点击「拉取/刷新数据」；若数据源不可用，可在侧边栏开启演示数据模式。",
            trace_id=C.new_trace_id(),
            exc=traceback.format_exc(),
        )
        C.todo_expander(["app_services.screen_market（Dev2 strategy/screener.py 就绪后接入）"])
        return
    rows = data.get("rows") or []
    if data.get("prototype"):
        C.render_warning_card("原型规则，验证中", PROTOTYPE_NOTICE)
    if data.get("error"):
        C.render_warning_card("扫描降级/失败", str(data["error"]),
                              suggestion="先到行情页拉取数据；或开启演示数据模式。")

    C.status_bar(
        [
            {"label": "全市场总数", "value": f"{int(data.get('total') or 0):,} 只", "tone": "muted"},
            {"label": "流动性过滤后", "value": f"{int(data.get('filtered') or 0):,} 只", "tone": "muted"},
            {"label": "本次扫描", "value": f"{int(data.get('scanned') or len(rows)):,} 只", "tone": "ok"},
            {"label": "数据时间", "value": (data.get("data_time") or "—")[:19], "tone": "muted"},
            {"label": "规则", "value": "原型规则（验证中）" if data.get("prototype") else "screen_market",
             "tone": "warn" if data.get("prototype") else "ok"},
        ]
    )

    if not rows:
        C.empty_state(
            "本次筛选没有结果",
            "可能是行情未拉取、剔除过热后无候选、或数据源不可用。请先到行情页刷新数据，"
            "或在侧边栏开启演示数据模式后再试。",
            page="行情",
            button_label="去行情页拉取数据",
        )
        return

    st.markdown(f"##### 筛选结果（{len(rows)} 只，逐行可加入自选）")
    export = pd.DataFrame([{
        "序号": i + 1, "代码": r["symbol"], "名称": r.get("name") or "",
        "现价": r.get("price"), "涨跌幅": r.get("change_pct"),
        "成交额": r.get("amount"), "得分": r.get("score"),
        "入选理由": r.get("reason"), "风险标记": r.get("risk"),
    } for i, r in enumerate(rows)])
    C.export_csv_button(export, "screener.csv", key="qd_scr_export")

    for index, row in enumerate(rows, start=1):
        cols = st.columns([0.45, 1.25, 1.1, 0.85, 0.85, 0.9, 0.75, 3.0, 1.0])
        with cols[0]:
            st.caption(f"{index}")
        with cols[1]:
            st.markdown(f"**{row['symbol']}**")
        with cols[2]:
            st.caption(row.get("name") or "（名称待补）")
        with cols[3]:
            st.markdown(C.money(row.get("price")))
        with cols[4]:
            st.markdown(C.colored(C.pct(row.get("change_pct"), ratio=False, signed=True),
                                  row.get("change_pct")), unsafe_allow_html=True)
        with cols[5]:
            st.markdown(_amount_text(row.get("amount")))
        with cols[6]:
            st.markdown(f"{float(row.get('score') or 0):.1f}")
        with cols[7]:
            st.caption(f"理由：{row.get('reason')}｜风险：{row.get('risk')}")
        with cols[8]:
            if st.button("加入自选", key=f"qd_scr_add_{row['symbol']}_{index}",
                         use_container_width=True):
                _add_to_watchlist(str(row["symbol"]), row.get("name") or "")
    st.caption("说明：得分由成交额分位与涨幅规则合成，原型规则未经验证；不构成买入建议，不承诺收益。")
    C.todo_expander([
        "app_services.screen_market（Dev2 strategy/screener.py 就绪后接入，替换原型规则）",
        "风险标记补充：停牌/ST/退市整理（需数据源标记）",
        "筛选结果落库与历史对比（research_runs，M6 阶段）",
    ])


def _amount_text(value: object) -> str:
    try:
        amount = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return C.DASH
    return f"{amount / 1e8:,.2f} 亿" if amount >= 1e8 else f"{amount / 1e4:,.2f} 万"

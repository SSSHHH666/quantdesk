"""页面：持仓（M2-09 接线版）。

数据来源：``app_services.get_positions / get_account / get_equity_curve``
（PortfolioService + PortfolioRepository 统一门面）。

展示：账户摘要、持仓明细（数量/可用/T+1 冻结/成本/现价/市值/浮盈/占比）、
资金曲线、资产配置饼图、导出 CSV；一键清仓（二次确认，逐单走风控）。
无持仓/无数据时给中文空态与引导，不白屏。
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

import app_services as S
from . import _common as C
from . import _ui_services as U

PAGE_TITLE = "持仓"
PAGE_ICON = "💼"
PAGE_ORDER = 6

TODO_ITEMS = [
    "账户重置：待 app_services 提供重置入口（清空持仓/流水 + 恢复初始资金）",
    "最近成交明细：待 app_services 增加 trades 查询入口",
    "对账状态（reconcile）：待门面暴露后展示一致性红条",
]


def _holdings_frame(positions, total_asset: float) -> pd.DataFrame:
    """持仓列表 → 展示用 DataFrame（浮盈按 最新价-成本 计算，占比按总资产）。"""
    rows = []
    for position in positions or []:
        quantity = int(getattr(position, "quantity", 0) or 0)
        avg_cost = float(getattr(position, "avg_cost", 0.0) or 0.0)
        last_price = float(getattr(position, "last_price", 0.0) or 0.0)
        market_value = float(getattr(position, "market_value", 0.0) or 0.0)
        pnl = (last_price - avg_cost) * quantity if last_price and avg_cost else 0.0
        pnl_pct = (pnl / (avg_cost * quantity) * 100.0) if avg_cost and quantity else 0.0
        rows.append(
            {
                "代码": str(getattr(position, "symbol", "")),
                "名称": str(getattr(position, "name", "") or ""),
                "数量": quantity,
                "可用": int(getattr(position, "available_quantity", 0) or 0),
                "T+1 冻结": int(getattr(position, "today_buy_quantity", 0) or 0),
                "成本价": C.money(avg_cost),
                "现价": C.money(last_price),
                "市值": C.money(market_value),
                "浮动盈亏": C.money(pnl),
                "盈亏%": C.pct(pnl_pct, ratio=False, signed=True),
                "仓位占比": C.pct(market_value / total_asset * 100.0 if total_asset else 0.0,
                                ratio=False),
            }
        )
    return pd.DataFrame(rows)


def _clear_all(positions, total_asset: float) -> None:
    """一键清仓：逐单 SELL（每单都过 RiskGate），汇总结果到 session_state 后刷新。"""
    results: list[str] = []
    success = 0
    for position in positions or []:
        sellable = int(getattr(position, "available_quantity", 0) or 0)
        if sellable <= 0:
            results.append(f"{getattr(position, 'symbol', '')}：可卖 0 股，跳过（T+1 冻结）")
            continue
        price = float(getattr(position, "last_price", 0.0) or 0.0) or float(
            getattr(position, "avg_cost", 0.0) or 0.0
        )
        request = {
            "symbol": str(getattr(position, "symbol", "")),
            "side": "SELL",
            "order_type": "LIMIT",
            "price": round(price, 2),
            "quantity": sellable,
        }
        ok, result, message = U.call(S.place_order, request)
        if ok:
            success += 1
            results.append(f"{request['symbol']}：卖出 {sellable} 股成功")
        else:
            results.append(f"{request['symbol']}：被拒（{getattr(result, 'reason_code', '') or '未知'}）"
                           f"{message}")
    st.session_state["qd_pos_clear_result"] = {"success": success, "messages": results}


def render() -> None:
    C.page_header("持仓", "账户摘要、持仓明细与资金曲线（模拟盘）", badge="M2 接线")

    # 清仓结果（上一轮执行后展示一次）
    clear_result = st.session_state.pop("qd_pos_clear_result", None)
    if clear_result:
        if clear_result.get("success"):
            st.success(f"清仓完成：成功提交 {clear_result['success']} 笔卖出委托")
        for message in clear_result.get("messages") or []:
            st.caption(message)

    ok_account, account, account_message = U.call(S.get_account)
    if not ok_account or account is None:
        U.fail_card(account_message, "账户读取失败",
                    suggestion="请在侧边栏确认「数据库：就绪」，或稍后点击「刷新」重试。")
        C.todo_expander(TODO_ITEMS)
        return

    total_asset = float(account.total_asset)
    ok_positions, positions, positions_message = U.call(S.get_positions)
    if not ok_positions:
        U.fail_card(positions_message, "持仓读取失败", warning=True,
                    suggestion="可先到手动交易页买入；若持续失败请查看日志页。")
        positions = []

    # ---- 账户摘要 ----
    st.markdown("##### 账户摘要")
    position_value = sum(float(getattr(item, "market_value", 0.0) or 0.0) for item in positions)
    k1, k2, k3, k4, k5 = st.columns(5)
    with k1:
        C.kpi_card("总资产", C.money(total_asset), help_text="现金 + 持仓市值（元）")
    with k2:
        C.kpi_card("可用资金", C.money(account.cash), help_text="可用于买入的现金（元）")
    with k3:
        C.kpi_card("持仓市值", C.money(position_value), help_text="Σ 最新价 × 数量（元）")
    with k4:
        C.kpi_card("累计收益率",
                   C.pct((total_asset / float(account.initial_cash) - 1.0) if account.initial_cash else 0.0,
                         signed=True),
                   help_text="相对初始资金（元）", tone="up" if total_asset >= account.initial_cash else "down")
    with k5:
        C.kpi_card("总仓位", C.pct(position_value / total_asset if total_asset else 0.0),
                   help_text="持仓市值 / 总资产（上限 80%，RK008）")

    # ---- 控件行 ----
    c1, c2, c3, c4 = st.columns([0.8, 1.0, 1.0, 1.6])
    with c1:
        if st.button("🔄 刷新", use_container_width=True, key="qd_pos_refresh"):
            C.clear_caches()
            st.rerun()
    with c2:
        holdings = _holdings_frame(positions, total_asset)
        C.export_csv_button(holdings if not holdings.empty else None, "positions.csv",
                            key="qd_pos_export", hint="当前无持仓可导出")
    with c3:
        st.button("🧹 一键清仓", use_container_width=True, key="qd_pos_clear_btn",
                  on_click=lambda: st.session_state.__setitem__("qd_pos_show_clear", True))
    with c4:
        st.caption("一键清仓会逐单提交卖出委托（每单走风控）；A 股 T+1 当日买入不可卖。")

    # ---- 持仓明细 ----
    st.markdown("##### 持仓明细")
    if positions:
        C.data_table(holdings, height=320)
        st.caption(f"{positions_message}；停牌/ST 标的卖出会被风控拒绝（RK002/RK003）。")
    else:
        C.empty_state(
            "当前无持仓",
            "没有持仓时，可到手动交易页买入；成交后会在这里显示可用数量、T+1 冻结、成本与浮盈。",
            page="手动交易",
            button_label="去手动交易页买入",
        )

    # ---- 资金曲线 + 资产配置 ----
    ok_curve, curve_rows, curve_message = U.call(S.get_equity_curve, days=120)
    curve_frame = pd.DataFrame(curve_rows or []) if ok_curve and curve_rows else pd.DataFrame()
    left, right = st.columns([2.1, 1])
    with left:
        if curve_frame.empty:
            C.chart_placeholder("资金曲线", curve_message or "暂无资金曲线：产生成交或运行盘后总结后生成",
                                height=300)
        else:
            U.line_chart(curve_frame, "trade_date", "total_asset", title="总资产（最近 120 个交易日）",
                         y_label="元", height=300)
    with right:
        cash = float(account.cash)
        U.pie_chart(["现金", "持仓市值"], [cash, max(0.0, total_asset - cash)],
                    title="资产配置", height=300)

    # ---- 最近成交（待门面）----
    C.table_placeholder(
        "最近成交",
        ["时间", "代码", "方向", "价格", "数量", "费用"],
        hint="成交明细查询接口尚未由 app_services 暴露（暂未开放，预计 M2 后半）",
        todo="app_services 增加 trades / 订单查询入口（M2-06）",
    )

    # ---- 一键清仓（二次确认 + 逐单走风控）----
    if st.session_state.get("qd_pos_show_clear"):
        if C.confirm_action(
            "clear_positions",
            "一键清仓将把当前全部「可卖」持仓逐笔提交卖出委托（每笔都走 RiskGate 风控，可能被拒）。"
            "该操作不可撤销（之后可再手动买回）。",
            button_label="确认清仓",
            confirm_word="清仓",
            details=[
                "停牌 / ST / 跌停封死的标的会被风控拒绝（RK002/RK003/RK011），不会强行卖出。",
                "T+1 当日买入不可卖（RK016），会提示可卖 0 股并跳过。",
            ],
            disabled=not positions,
        ):
            st.session_state.pop("qd_pos_show_clear", None)
            with st.spinner("正在逐单提交卖出委托…"):
                _clear_all(positions, total_asset)
            C.clear_caches()
            st.rerun()

    # ---- 账户重置（待门面）----
    with st.expander("⚙️ 账户重置（暂未开放）", expanded=False):
        st.caption("重置会清空模拟账户的持仓、委托、成交与流水，并把资金恢复为初始值。"
                   "该能力需要 app_services 提供「账户重置」入口（暂未开放，预计 M2 后半；"
                   "当前不会执行任何操作）。")

    C.todo_expander(TODO_ITEMS)

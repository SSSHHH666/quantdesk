"""页面：手动交易（M2-08 接线版）。

流程（SPEC §10.3 / AC-F7-02③）：
    填写委托 → ``app_services.preview_order`` 只读风控预检（金额/费用/仓位/结论）
    → 二次确认 → ``app_services.place_order``（唯一下单入口，内部必过 RiskGate）
    → 成功后提示成交结果；失败展示带 RK/BR 原因码与建议的中文错误卡。

数据全部经 ``app_services``（Provider/OrderService/PortfolioService 统一门面），
页面不直接调用 Broker、不写 SQL；任何失败都有中文提示，不白屏。
"""

from __future__ import annotations

import datetime as dt

import streamlit as st

import app_services as S
from . import _common as C
from . import _ui_services as U

PAGE_TITLE = "手动交易"
PAGE_ICON = "🧾"
PAGE_ORDER = 3

TODO_ITEMS = [
    "今日委托「撤单」：待 app_services 增加订单查询/撤销入口（M2-06/M2-07）",
    "价格涨跌停区间标注：AStockRules.price_limits 由门面暴露后接入",
    "市价单：受 config.trading.allow_market_order 控制（默认禁止）",
]

SIDES = {"买入": "BUY", "卖出": "SELL"}


def _bump_price(delta: float) -> None:
    current = float(st.session_state.get("qd_mt_price", 0.0) or 0.0)
    st.session_state["qd_mt_price"] = round(max(0.0, current + delta), 2)


def _set_qty(value: int) -> None:
    st.session_state["qd_mt_qty"] = int(value)


def _build_request(symbol: str, side: str, price: float, quantity: int) -> dict:
    return {
        "symbol": symbol,
        "side": SIDES.get(side, "BUY"),
        "order_type": "LIMIT",
        "price": float(price),
        "quantity": int(quantity),
    }


def _render_account_strip() -> None:
    """账户概览（顶层 KPI）：失败给警告卡，不影响下单表单。"""
    ok, account, message = U.call(S.get_account)
    if not ok or account is None:
        U.fail_card(message, "账户读取失败", warning=True,
                    suggestion="请在侧边栏确认「数据库：就绪」；或稍后重试。")
        return
    position_value = max(0.0, float(account.total_asset) - float(account.cash))
    k1, k2, k3, k4 = st.columns(4)
    with k1:
        C.kpi_card("总资产", C.money(account.total_asset), help_text="现金 + 持仓市值（元）")
    with k2:
        C.kpi_card("可用资金", C.money(account.cash), help_text="可用于买入的现金（元）")
    with k3:
        C.kpi_card("冻结资金", C.money(account.frozen_cash), help_text="挂单冻结（元）")
    with k4:
        C.kpi_card("持仓市值", C.money(position_value),
                   help_text=f"账户：{account.name}（{account.id}）")


def _render_preview(preview) -> None:
    """风控预检面板：金额/费用/仓位/结论 + 规则明细。"""
    decision = getattr(preview, "decision", None)
    allowed = bool(getattr(decision, "allow", False))
    code = getattr(decision, "reason_code", None)
    fee = getattr(preview, "fee", None)

    k1, k2, k3, k4 = st.columns(4)
    with k1:
        C.kpi_card("预计成交金额", C.money(preview.estimated_amount), help_text="价格 × 数量（元）")
    with k2:
        C.kpi_card("预计费用合计", C.money(getattr(fee, "total", 0.0)),
                   help_text="佣金 + 印花税 + 过户费 + 规费")
    with k3:
        C.kpi_card("成交后单票占比", f"{float(preview.post_trade_position_pct):.2f}%",
                   help_text="上限 20%（RK007）")
    with k4:
        C.kpi_card("成交后总仓位", f"{float(preview.post_trade_total_position_pct):.2f}%",
                   help_text="上限 80%（RK008）")

    C.status_bar(
        [
            {"label": "风控结论", "value": "通过" if allowed else "不通过",
             "tone": "ok" if allowed else "error",
             "help": getattr(decision, "message", "") or ""},
            {"label": "原因码", "value": code or "—", "tone": "error" if code else "muted"},
            {"label": "可买数量", "value": f"{int(preview.available_to_buy):,} 股", "tone": "muted",
             "help": "按整手与可用资金计算（RK014）"},
            {"label": "可卖数量", "value": f"{int(preview.available_to_sell):,} 股", "tone": "muted",
             "help": "T+1 可用数量（RK016）"},
            {"label": "成交后可用资金", "value": C.money(preview.cash_after), "tone": "muted"},
        ]
    )
    if not allowed:
        C.render_error_card(
            "风控预检未通过",
            reason_code=code,
            message=getattr(decision, "message", "") or "该委托不满足风控规则",
        )
    if fee is not None:
        fee_rows = [
            {"费用项": "佣金", "金额（元）": C.money(fee.commission)},
            {"费用项": "印花税（仅卖出）", "金额（元）": C.money(fee.stamp_tax)},
            {"费用项": "过户费", "金额（元）": C.money(fee.transfer_fee)},
            {"费用项": "规费", "金额（元）": C.money(fee.reg_fee)},
            {"费用项": "合计", "金额（元）": C.money(fee.total)},
        ]
        C.data_table(U.rows_to_frame(fee_rows, ["费用项", "金额（元）"]), height=200)
    checks = list(getattr(decision, "checks", None) or [])
    if checks:
        with st.expander("风控规则明细（为什么通过/被拒）", expanded=not allowed):
            frame = U.rows_to_frame(
                checks,
                ["rule", "label", "passed", "actual", "limit", "message"],
                formatters={
                    "passed": lambda value: "通过" if value else "未通过",
                    "actual": lambda value: C.DASH if value is None else str(value),
                    "limit": lambda value: C.DASH if value is None else str(value),
                },
            )
            C.data_table(frame)


def _record_order(result, req: dict) -> None:
    orders = list(st.session_state.get("qd_mt_orders") or [])
    orders.append(
        {
            "时间": dt.datetime.now().strftime("%H:%M:%S"),
            "代码": req["symbol"],
            "方向": C.side_label(req["side"]),
            "价格": C.money(req["price"]),
            "数量": C.money(req["quantity"], digits=0),
            "状态": C.order_status_label(getattr(result, "status", "")),
            "原因码": getattr(result, "reason_code", "") or "—",
        }
    )
    st.session_state["qd_mt_orders"] = orders[-20:]


def render() -> None:
    C.page_header("手动交易", "风控预检 → 二次确认 → 提交委托（模拟盘）", badge="M2 接线")
    prefill = str(st.session_state.get("qd_symbol") or "").strip()

    _render_account_strip()

    # ---- 委托参数 ----
    with st.container(border=True):
        st.markdown("##### 委托参数")
        r1, r2, r3 = st.columns([1.1, 1.5, 1.0])
        with r1:
            st.text_input("账户", value=str(S.DEFAULT_ACCOUNT_ID), disabled=True,
                          key="qd_mt_account_id", help="MVP 单模拟账户；多账户待后续扩展。")
        with r2:
            st.text_input("标的代码", value=prefill, key="qd_mt_symbol",
                          placeholder="如 600519.SH / 000001.SZ",
                          help="需带交易所后缀；不确定可先去行情页搜索。")
        with r3:
            st.radio("方向", list(SIDES), horizontal=True, key="qd_mt_side")

        r4, r5, r6, r7 = st.columns([1.2, 1.4, 0.7, 0.7])
        with r4:
            st.radio("价格类型", ["限价委托"], index=0, horizontal=True, key="qd_mt_price_type",
                     help="MVP 默认仅限价；市价单需 config.trading.allow_market_order=true。")
        with r5:
            st.number_input("委托价格（元）", min_value=0.0, value=0.0, step=0.01,
                            format="%.2f", key="qd_mt_price")
            p1, p2 = st.columns(2)
            with p1:
                st.button("−0.01", on_click=_bump_price, args=(-0.01,),
                          key="qd_mt_price_minus", use_container_width=True)
            with p2:
                st.button("+0.01", on_click=_bump_price, args=(0.01,),
                          key="qd_mt_price_plus", use_container_width=True)
        with r6:
            st.number_input("数量（股）", min_value=0, value=0, step=100, format="%d",
                            key="qd_mt_qty", help="A 股整手 100 股（科创板 200 股起，RK017）")
        with r7:
            st.caption("快捷数量")
            st.button("100", on_click=_set_qty, args=(100,), key="qd_mt_q_100",
                      use_container_width=True)
            st.button("1000", on_click=_set_qty, args=(1000,), key="qd_mt_q_1000",
                      use_container_width=True)
        st.caption("提示：可先看下方预检面板的「可买数量」，再填写数量；半仓/全仓等快捷按钮待订单查询入口补齐。")

    symbol = str(st.session_state.get("qd_mt_symbol") or "").strip().upper()
    side = st.session_state.get("qd_mt_side", "买入")
    price = float(st.session_state.get("qd_mt_price", 0.0) or 0.0)
    quantity = int(st.session_state.get("qd_mt_qty", 0) or 0)
    ready = bool(symbol) and price > 0 and quantity > 0

    # ---- 风控预检（只读，不落库不下单）----
    with st.container(border=True):
        st.markdown("##### 风控预检（只读，不落库不下单）")
        if not ready:
            C.not_ready(
                "待填写委托参数",
                "填写「标的代码 / 委托价格 / 数量」后自动展示：预计金额、费用明细、成交后仓位占比、"
                "风控结论与可买/可卖数量。",
                icon="📝",
            )
        else:
            req = _build_request(symbol, side, price, quantity)
            ok, preview, message = U.call(S.preview_order, req)
            if preview is None:
                U.fail_card(message, "风控预检失败",
                            suggestion="检查标的代码格式（如 600519.SH）与数量是否为整手。")
            else:
                _render_preview(preview)
                if message:
                    st.caption(message)

    # ---- 提交（二次确认；未确认风险时按钮禁用，AC-F7-03）----
    st.markdown("##### 提交委托")
    st.checkbox("演示：显示一条风控拒绝错误卡片（RK007 样式自检）", value=False,
                key="qd_mt_demo_error")
    if st.session_state.get("qd_mt_demo_error"):
        C.sample_error_card_demo()
    submitted = C.confirm_action(
        "manual_submit",
        "提交前请确认：方向、标的、价格、数量与金额是否符合预期（模拟盘）。",
        button_label="提交委托",
        details=[
            "提交后依次经过：参数校验 → RiskGate.check → 落库 → PaperBroker 撮合 → 持仓/资金更新。",
            "风控拒绝时订单落库为「已拒绝」，并返回 RK 原因码与建议。",
            "成交后账户概览与「今日委托」会自动刷新。",
        ],
    )
    if submitted:
        if not ready:
            C.render_warning_card(
                "委托参数不完整，未提交",
                "请先填写「标的代码 / 委托价格 / 数量」（价格与数量需大于 0），再勾选确认后提交。",
                suggestion="可先用行情页查询标的，再回到本页填写。",
            )
        else:
            req = _build_request(symbol, side, price, quantity)
            ok, result, message = U.call(S.place_order, req)
            _record_order(result, req)
            if ok:
                fee = getattr(result, "fee", None)
                st.success(
                    message
                    + (f"（成交 {result.filled_quantity} 股 @ {C.money(result.avg_fill_price)} 元，"
                       f"费用 {C.money(getattr(fee, 'total', 0.0))} 元）" if result else "")
                )
                C.clear_caches()
            else:
                C.render_error_card(
                    "委托未成交/被拒绝",
                    reason_code=getattr(result, "reason_code", None),
                    message=message,
                    suggestion="按原因码与建议调整（减少数量 / 改价 / 换标的）后重试。",
                    trace_id=getattr(result, "trace_id", None),
                )

    # ---- 今日委托（本次会话）----
    st.markdown("##### 今日委托")
    orders = list(st.session_state.get("qd_mt_orders") or [])
    if orders:
        C.data_table(U.rows_to_frame(orders,
                                     ["时间", "代码", "方向", "价格", "数量", "状态", "原因码"]),
                     height=220)
        st.caption("以上为本次页面会话内提交的委托记录；历史委托查询与撤单待 M2-06 订单查询入口接入。")
    else:
        C.table_placeholder(
            "今日委托",
            ["时间", "代码", "方向", "价格", "数量", "状态", "原因码"],
            hint="本次会话尚未提交委托；提交后这里会显示结果（含风控拒绝原因码）",
            todo="历史委托查询与撤单（app_services 订单查询入口待补）",
        )

    C.todo_expander(TODO_ITEMS)

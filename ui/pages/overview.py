"""页面：总览（M2-10 接线版）。

数据来源：``app_services`` —— ``get_account`` / ``get_positions`` / ``get_equity_curve``
/ ``scheduler_status`` / ``get_provider`` / ``tail_logs``。

展示：状态条（数据源/账户/调度器/风控）、6 个 KPI、权益曲线 + 回撤、持仓 Top5 +
资产饼图、最近告警（日志 WARN/ERROR）与跳转联动。所有失败都转中文提示，不留空白。
"""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

import app_services as S
from . import _common as C
from . import _ui_services as U

PAGE_TITLE = "总览"
PAGE_ICON = "🏠"
PAGE_ORDER = 1

TODO_ITEMS = [
    "今日信号 / 今日订单：待 app_services 增加信号与订单查询入口（M2-06/M3-03）",
    "风控熔断状态（fused）展示：待门面暴露 RiskGate 运行态（M2-04）",
    "最近告警改为 notifications 表查询（M3-04；当前用日志 WARN/ERROR 兜底）",
]


def _latest_warnings(limit: int = 10) -> list[dict]:
    """用日志尾读兜底「最近告警」：解析 JSON Lines，筛选 WARN/ERROR。"""
    ok, lines, _message = U.call(S.tail_logs, 400)
    if not ok or not lines:
        return []
    warnings: list[dict] = []
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except (TypeError, ValueError):
            continue
        level = str(record.get("level") or "").upper()
        if level not in {"WARNING", "WARN", "ERROR", "CRITICAL"}:
            continue
        warnings.append(
            {
                "时间": C.fmt_datetime(record.get("ts")),
                "级别": level,
                "模块": str(record.get("logger") or record.get("module") or ""),
                "消息": str(record.get("message") or "")[:120],
                "trace_id": str(record.get("trace_id") or ""),
            }
        )
        if len(warnings) >= limit:
            break
    return list(reversed(warnings))


def render() -> None:
    C.page_header("总览", "账户与运行状态一屏总览", badge="M2 接线")

    ok_account, account, account_message = U.call(S.get_account)
    ok_positions, positions, positions_message = U.call(S.get_positions)
    if not ok_positions:
        positions = []
    ok_scheduler, job_statuses_list, scheduler_message = U.call(S.scheduler_status)
    ok_provider, provider, provider_message = U.call(S.get_provider)
    ok_curve, curve_rows, curve_message = U.call(S.get_equity_curve, days=180)

    # ---- M5-04 首屏引导（无持仓时，引导新用户 4 步走完闭环）----
    if not positions:
        with st.container(border=True):
            st.markdown("##### 👋 新用户上手指引（约 5 分钟走完闭环）")
            st.caption("按顺序点击即可：拉数据 → 看推荐 → 跑回测 → 模拟下单；每一步都会跳转到对应页面。")
            g1, g2, g3, g4 = st.columns(4)
            with g1:
                if st.button("① 拉取行情数据", use_container_width=True, key="qd_guide_market"):
                    C.goto_page("行情")
            with g2:
                if st.button("② 看今日推荐", use_container_width=True, key="qd_guide_recommend"):
                    C.goto_page("今日推荐")
            with g3:
                if st.button("③ 跑一次回测", use_container_width=True, key="qd_guide_backtest"):
                    C.goto_page("回测")
            with g4:
                if st.button("④ 模拟下单", use_container_width=True, key="qd_guide_trade"):
                    C.goto_page("手动交易")

    # ---- 状态条 4 项 ----
    provider_name = str(getattr(provider, "name", "—")) if ok_provider else "不可用"
    scheduler_running = bool(ok_scheduler) and "运行中" in scheduler_message and "未运行" not in scheduler_message
    C.status_bar(
        [
            {"label": "数据源", "value": provider_name,
             "tone": "warn" if provider_name == "mock" else ("ok" if ok_provider else "error"),
             "help": provider_message or "数据源状态"},
            {"label": "账户", "value": C.money(account.total_asset) if ok_account and account else "不可用",
             "tone": "ok" if ok_account else "error", "help": account_message or ""},
            {"label": "调度器", "value": "运行中" if scheduler_running else "已停止",
             "tone": "ok" if scheduler_running else "muted",
             "help": scheduler_message or "自动化调度器状态"},
            {"label": "风控", "value": "正常（门面未暴露熔断态）", "tone": "ok",
             "help": "RiskGate 在每笔下单内部执行；熔断态展示待 M2-04 门面暴露"},
        ]
    )

    # ---- KPI 6 个 ----
    total_asset = float(account.total_asset) if ok_account and account else 0.0
    initial_cash = float(account.initial_cash) if ok_account and account else 0.0
    cash = float(account.cash) if ok_account and account else 0.0
    position_value = sum(float(getattr(item, "market_value", 0.0) or 0.0) for item in positions)
    curve_frame = pd.DataFrame(curve_rows or []) if ok_curve and curve_rows else pd.DataFrame()
    daily_pnl = 0.0
    if not curve_frame.empty and "daily_pnl" in curve_frame.columns:
        daily_pnl = float(curve_frame.iloc[-1].get("daily_pnl") or 0.0)
    prev_asset = daily_pnl and total_asset - daily_pnl
    daily_pct = (daily_pnl / prev_asset * 100.0) if prev_asset else 0.0
    C.kpi_row(
        [
            {"label": "总资产", "value": C.money(total_asset), "help": "现金 + 持仓市值（元）"},
            {"label": "当日盈亏", "value": C.money(daily_pnl),
             "delta": C.pct(daily_pct, ratio=False, signed=True) if daily_pnl else "暂无数据",
             "delta_value": daily_pnl, "tone": "up" if daily_pnl > 0 else ("down" if daily_pnl < 0 else "flat")},
            {"label": "累计收益率",
             "value": C.pct((total_asset / initial_cash - 1.0) if initial_cash else 0.0, signed=True),
             "tone": "up" if total_asset >= initial_cash else "down",
             "help": "相对初始资金（account.initial_cash）"},
            {"label": "可用资金", "value": C.money(cash), "help": "可买入现金（元）"},
            {"label": "持仓市值", "value": C.money(position_value),
             "help": positions_message or "Σ 最新价 × 数量"},
            {"label": "总仓位", "value": C.pct(position_value / total_asset if total_asset else 0.0),
             "help": "持仓市值 / 总资产（上限 80%）"},
        ]
    )

    # ---- 权益曲线 + 回撤 ----
    left, right = st.columns([2.1, 1])
    with left:
        if curve_frame.empty:
            C.chart_placeholder("权益曲线（含回撤）",
                                curve_message or "暂无资金曲线：产生成交或运行盘后总结后生成",
                                height=300)
        else:
            frame = curve_frame.copy()
            frame["净值曲线"] = pd.to_numeric(frame["total_asset"], errors="coerce")
            peak = frame["净值曲线"].cummax()
            frame["回撤%"] = (frame["净值曲线"] / peak - 1.0) * 100.0
            U.line_chart(frame, "trade_date", "净值曲线", title="总资产曲线（最近 180 个交易日）",
                         y_label="元", height=300)
            st.caption(f"最大回撤 {C.pct(frame['回撤%'].min() / 100.0)}；"
                       "数据来自 equity_curve（每交易日盘后落库）。")
    with right:
        U.pie_chart(["现金", "持仓市值"], [cash, max(0.0, position_value)],
                    title="资产配置", height=300)

    # ---- 持仓 Top5 + 今日信号/订单 ----
    left2, right2 = st.columns([1, 1.7])
    with left2:
        st.markdown("##### 持仓 Top 5（按市值）")
        if not positions:
            C.not_ready("暂无持仓", "还没有任何持仓。可到手动交易页买入，成交后这里会显示 Top5 与资产配置。",
                        icon="💼")
        else:
            top_positions = sorted(
                positions,
                key=lambda item: float(getattr(item, "market_value", 0.0) or 0.0),
                reverse=True,
            )[:5]
            rows = [
                {
                    "代码": str(getattr(item, "symbol", "")),
                    "名称": str(getattr(item, "name", "") or "—"),
                    "市值": C.money(getattr(item, "market_value", 0.0)),
                    "数量": C.money(getattr(item, "quantity", 0), digits=0),
                    "现价": C.money(getattr(item, "last_price", 0.0)),
                }
                for item in top_positions
            ]
            C.data_table(pd.DataFrame(rows), height=220)
        if st.button("查看全部持仓 →", key="qd_overview_go_positions"):
            C.goto_page("持仓")

    with right2:
        st.markdown("##### 今日信号与订单")
        C.table_placeholder(
            "今日信号与订单",
            ["时间", "来源", "代码", "方向", "数量", "状态", "原因码"],
            hint="订单/信号查询接口尚未由 app_services 暴露（暂未开放，预计 M2-06/M3-03）",
            todo="app_services 增加 orders / strategy_signals 查询入口",
        )
        if st.button("按 trace_id 查看日志 →", key="qd_overview_go_logs"):
            C.goto_page("日志")

    # ---- 最近告警 ----
    st.markdown("##### 最近告警 / 通知")
    warnings = _latest_warnings(10)
    if warnings:
        C.data_table(pd.DataFrame(warnings), height=220)
        st.caption("以上由运行日志的 WARN/ERROR 记录兜底展示；通知渠道（桌面/邮件/Webhook）接入后改为"
                   "notifications 表查询（M3-04）。")
    else:
        C.not_ready("最近无告警", "运行日志里暂无 WARN/ERROR 记录。下单被拒、熔断触发、任务失败等事件"
                                "会在这里显示（当前用日志兜底）。", icon="🔔")

    # ---- 刷新 ----
    if st.button("🔄 刷新总览", key="qd_overview_refresh"):
        C.clear_caches()
        st.rerun()

    C.todo_expander(TODO_ITEMS)

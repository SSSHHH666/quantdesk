"""页面：自动化（M3-06 接线版）。

数据来源：``app_services`` —— ``scheduler_status / scheduler_start / scheduler_stop /
trigger_job / tail_logs``（SchedulerService + three jobs 统一门面）。

展示：调度器总开关（二次确认）、盘前/盘中/盘后任务卡片（时间/下次运行/上次结果/
立即运行）、今日管线统计占位、最近运行日志。所有失败转中文提示，不白屏。
"""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

import app_services as S
from . import _common as C
from . import _ui_services as U

PAGE_TITLE = "自动化"
PAGE_ICON = "🤖"
PAGE_ORDER = 7

JOBS = (("prepare", "盘前任务", "08:50"), ("scan", "盘中任务", "10:00"),
        ("summarize", "盘后任务", "15:30"))

TODO_ITEMS = [
    "job_runs 表格查询与今日管线统计（信号/拦截/下单/成交）：待 app_services 暴露入口（M3-02）",
    "测试通知：Notifier 门面入口待补（M3-04）",
    "策略实例启用清单：strategy_instances 查询/启停入口待 M3-05/M3-06",
]


def _tail_frame(limit: int = 50) -> tuple[pd.DataFrame, str]:
    ok, lines, message = U.call(S.tail_logs, limit)
    if not ok or not lines:
        return pd.DataFrame(), message
    rows = []
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except (TypeError, ValueError):
            rows.append({"时间": "", "级别": "", "模块": "", "消息": line[:160], "trace_id": ""})
            continue
        rows.append(
            {
                "时间": C.fmt_datetime(record.get("ts")),
                "级别": str(record.get("level") or ""),
                "模块": str(record.get("logger") or record.get("module") or ""),
                "消息": str(record.get("message") or "")[:160],
                "trace_id": str(record.get("trace_id") or ""),
            }
        )
    return pd.DataFrame(rows), message


def _scheduler_state() -> tuple[bool, dict, str]:
    """返回 (running, {job_key: JobStatus}, message)。"""
    ok, statuses, message = U.call(S.scheduler_status)
    mapping = {str(getattr(item, "job_key", "")): item for item in (statuses or [])}
    running = bool(ok) and "运行中" in message and "未运行" not in message
    return running, mapping, message


def render() -> None:
    C.page_header("自动化", "定时执行「盘前 → 盘中 → 盘后」，并可手动触发任务", badge="M3 接线")
    meta = C.app_meta()
    jobs_conf = meta.get("jobs") or {}
    running, status_map, status_message = _scheduler_state()
    auto_enabled = bool(meta.get("automation_enabled"))

    C.status_bar(
        [
            {"label": "调度器", "value": "运行中" if running else "已停止",
             "tone": "ok" if running else "muted", "help": status_message or ""},
            {"label": "自动化开关", "value": "已开启" if auto_enabled else "已关闭（默认，安全）",
             "tone": "ok" if auto_enabled else "warn",
             "help": "config.automation.enabled；关闭时 scan 只生成信号不下单"},
            {"label": "任务", "value": f"{len(JOBS)} 个（盘前/盘中/盘后）", "tone": "muted"},
        ]
    )

    # ---- 总开关 ----
    with st.container(border=True):
        st.markdown("##### 调度器总开关")
        s1, s2, s3 = st.columns([1.2, 1.2, 2.2])
        with s1:
            st.button("▶️ 启动调度器", use_container_width=True, key="qd_auto_start",
                      on_click=lambda: st.session_state.__setitem__("qd_auto_action", "start"))
        with s2:
            st.button("⏹️ 停止调度器", use_container_width=True, key="qd_auto_stop",
                      on_click=lambda: st.session_state.__setitem__("qd_auto_action", "stop"))
        with s3:
            st.caption("启动后按配置时间执行；停止调度器**不会**取消已提交订单。"
                       "开启自动交易需在设置页显式打开 automation.enabled。")
        action = st.session_state.pop("qd_auto_action", None)
        if action:
            label = "启动" if action == "start" else "停止"
            if C.confirm_action(
                f"auto_{action}",
                f"确认{label}调度器？{'启动后盘中任务会按策略生成信号，' if action == 'start' else ''}"
                "停止不影响已提交订单。",
                button_label=f"确认{label}",
                details=["调度器时区 Asia/Shanghai，仅交易日执行；非交易日标记 SKIPPED。",
                         "自动交易开关（automation.enabled）默认关闭，需在设置页开启后才会自动下单。"],
            ):
                func = S.scheduler_start if action == "start" else S.scheduler_stop
                ok, _data, message = U.call(func)
                if ok:
                    st.success(message)
                else:
                    U.fail_card(message, f"调度器{label}失败",
                                suggestion="检查 APScheduler 是否已安装、是否已有其它实例占用调度锁。")
                C.clear_caches()
                st.rerun()

    # ---- 三个任务卡片 ----
    st.markdown("##### 定时任务")
    columns = st.columns(3)
    for column, (key, name, default_time) in zip(columns, JOBS):
        status = status_map.get(key)
        with column:
            with st.container(border=True):
                st.markdown(f"**{name}**")
                st.caption(f"执行时间：{jobs_conf.get(key, default_time)}（config.automation.jobs）")
                st.caption(f"下次运行：{C.fmt_datetime(getattr(status, 'next_run_time', None))}")
                last_status = getattr(status, "last_status", None)
                st.caption(f"上次结果：{C.job_status_label(last_status) if last_status else '未运行过'}")
                if getattr(status, "last_message", ""):
                    st.caption(f"上次消息：{str(status.last_message)[:80]}")
                b1, b2 = st.columns(2)
                with b1:
                    st.button("立即运行", use_container_width=True, key=f"qd_auto_run_{key}",
                              on_click=lambda k=key: st.session_state.__setitem__("qd_auto_trigger", k))
                with b2:
                    st.button("查看记录", use_container_width=True, key=f"qd_auto_log_{key}",
                              on_click=lambda k=key: st.session_state.__setitem__("qd_auto_show_log", k))

    trigger_key = st.session_state.pop("qd_auto_trigger", None)
    if trigger_key:
        job_name = dict((key, name) for key, name, _ in JOBS).get(trigger_key, trigger_key)
        if C.confirm_action(
            f"auto_trigger_{trigger_key}",
            f"确认立即执行「{job_name}」？该任务会同步执行（可能拉取行情/生成信号，耗时数秒到数十秒）。",
            button_label="确认执行",
            details=["prepare：刷新数据 + T+1 结算 + 熔断基线；",
                     "scan：策略扫描 → 信号 → 风控 → 下单（受 automation.enabled 控制）；",
                     "summarize：当日盈亏 + 权益快照 + 日报。"],
        ):
            with st.spinner(f"正在执行「{job_name}」…"):
                ok, data, message = U.call(S.trigger_job, trigger_key)
            if ok:
                st.success(message)
            else:
                C.render_error_card("任务执行失败", message=message,
                                    suggestion="查看日志页对应 trace_id；若数据源不可用请先到行情页刷新数据。")
            C.clear_caches()
            st.rerun()

    show_log_key = st.session_state.pop("qd_auto_show_log", None)
    if show_log_key:
        with st.expander(f"「{show_log_key}」最近运行日志", expanded=True):
            frame, message = _tail_frame(50)
            if frame.empty:
                C.not_ready("暂无运行日志", message or "日志目录为空：先运行一次任务或回测。", icon="📜")
            else:
                C.data_table(frame, height=280)

    # ---- 今日管线统计 ----
    st.markdown("##### 今日管线统计")
    t1, t2, t3, t4 = st.columns(4)
    with t1:
        C.kpi_card("信号数", C.DASH, help_text="暂未开放：待 app_services 暴露信号统计（M3-02）")
    with t2:
        C.kpi_card("风控拦截数", C.DASH, help_text="暂未开放：待信号/订单统计入口（M3-02）")
    with t3:
        C.kpi_card("下单数", C.DASH, help_text="暂未开放：待订单统计入口（M2-06）")
    with t4:
        C.kpi_card("成交数", C.DASH, help_text="暂未开放：待成交统计入口（M2-05）")
    st.caption("以上统计需要 job_runs / strategy_signals / orders 查询入口（暂未开放，预计 M3 阶段）。")

    # ---- 最近运行日志 ----
    st.markdown("##### 最近运行记录")
    frame, message = _tail_frame(100)
    if frame.empty:
        C.not_ready("暂无运行记录", message or "日志目录为空：先启动调度器或手动触发一次任务。",
                    icon="📜")
    else:
        C.data_table(frame, height=300)
        st.caption("当前为运行日志尾读（最近 100 行）；结构化 job_runs 表格待 M3-02 接入。")

    # ---- 测试通知 ----
    st.markdown("##### 通知")
    st.caption("暂未开放（本轮仅提供日志/记录查看）：测试通知需要 app_services 暴露 Notifier 入口；"
               "当前通知开关与渠道配置可在设置页查看。")

    C.todo_expander(TODO_ITEMS)

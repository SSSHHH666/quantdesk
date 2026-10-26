"""页面：日志（M3-08 接线版）。

数据来源：``app_services.tail_logs``（core.logging 已脱敏的 JSON Lines 尾读）。
展示：运行日志（级别/时间/关键词/trace_id 过滤、导出 CSV）、审计流水、数据校验、
通知记录（后三类暂无查询门面，给明确中文占位而不是空白）。
"""

from __future__ import annotations

import datetime as dt
import json

import pandas as pd
import streamlit as st

import app_services as S
from . import _common as C
from . import _ui_services as U

PAGE_TITLE = "日志"
PAGE_ICON = "📜"
PAGE_ORDER = 9

LEVELS = ["全部", "DEBUG", "INFO", "WARNING", "ERROR"]

TODO_ITEMS = [
    "审计流水查询：待 app_services 暴露 audit_logs 入口（M3-08）",
    "数据校验报告（DQ001–DQ007）：待暴露 data_quality_issues 查询（M1-04/M3-08）",
    "通知记录：待暴露 notifications 查询（M3-04）",
    "日志分页加载（>2000 行）：当前最多尾读 2000 行",
]


def _parse_logs(lines: list[str]) -> pd.DataFrame:
    """JSON Lines → DataFrame（无法解析的行按原文保留，不丢弃）。"""
    rows: list[dict] = []
    for line in lines or []:
        try:
            record = json.loads(line)
        except (TypeError, ValueError):
            rows.append({"时间": "", "级别": "", "模块": "", "消息": line[:300], "trace_id": ""})
            continue
        rows.append(
            {
                "时间": C.fmt_datetime(record.get("ts")),
                "级别": str(record.get("level") or ""),
                "模块": str(record.get("logger") or record.get("module") or ""),
                "消息": str(record.get("message") or ""),
                "trace_id": str(record.get("trace_id") or ""),
            }
        )
    return pd.DataFrame(rows)


def _filtered(frame: pd.DataFrame, level: str, keyword: str, trace_id: str) -> pd.DataFrame:
    if frame.empty:
        return frame
    result = frame
    if level != "全部":
        wanted = "WARNING" if level == "WARN" else level
        result = result[result["级别"].str.upper() == wanted]
    if keyword.strip():
        text = keyword.strip()
        result = result[result["消息"].astype(str).str.contains(text, case=False, regex=False)]
    if trace_id.strip():
        text = trace_id.strip()
        result = result[result["trace_id"].astype(str).str.contains(text, regex=False)]
    return result.reset_index(drop=True)



def _query_orm(model, limit: int = 100) -> list[dict]:
    """只读查询扩展表（audit_logs / data_quality_issues / notifications）。"""
    from sqlalchemy import select
    from core.db import get_session

    session = get_session()
    try:
        rows = session.execute(
            select(model).order_by(model.id.desc()).limit(max(1, int(limit)))
        ).scalars().all()
        return [{column.name: getattr(row, column.name) for column in model.__table__.columns}
                for row in rows]
    finally:
        session.close()


def render() -> None:
    C.page_header("日志", "运行日志尾读与过滤；按 trace_id 追一次下单/任务的全链路", badge="M3 接线")

    ok, lines, message = U.call(S.tail_logs, 2000)
    if not ok:
        C.render_warning_card("日志读取失败", message,
                              suggestion="日志文件在 runtime/logs/ 下，首次运行后自动生成；"
                                         "若目录不存在可先运行一次回测或自动化任务。")
        frame = pd.DataFrame()
    else:
        frame = _parse_logs(lines or [])
    total = len(frame)

    # ---- 过滤条件 ----
    with st.container(border=True):
        f1, f2, f3, f4 = st.columns([0.9, 1.6, 1.4, 0.9])
        with f1:
            level = st.selectbox("日志级别", LEVELS, index=0, key="qd_log_level")
        with f2:
            keyword = st.text_input("关键词", value="", key="qd_log_keyword",
                                    placeholder="如 RK007 / 600519.SH / report")
        with f3:
            trace_id = st.text_input("trace_id", value=str(st.session_state.get("qd_trace_id", "")),
                                     key="qd_log_trace", help="从总览/错误卡片跳转时自动带入")
        with f4:
            if st.button("🔄 刷新", use_container_width=True, key="qd_log_refresh"):
                C.clear_caches()
                st.rerun()
        st.caption(message if ok else "（日志读取失败，请查看上方提示）")

    filtered = _filtered(frame, level, keyword, trace_id)
    st.caption(f"共 {total} 行日志，筛选后 {len(filtered)} 行；最新 2000 行（JSON Lines，已脱敏）。")
    C.export_csv_button(filtered if not filtered.empty else None, "logs.csv",
                        key="qd_log_export", hint="当前无日志可导出")

    # ---- 4 个 Tab ----
    tab_runtime, tab_audit, tab_dq, tab_notify = st.tabs(
        ["运行日志", "审计流水", "数据校验报告", "通知记录"]
    )
    with tab_runtime:
        if filtered.empty:
            C.not_ready("暂无运行日志",
                        "当前筛选条件下没有日志。可清空关键词/切换级别，或先运行一次回测/自动化任务。",
                        icon="📭")
        else:
            C.data_table(filtered, height=420)
            st.caption("点击列头可排序；可按 trace_id 过滤后导出，用于排查一次下单/任务的全链路。")

    with tab_audit:
        rows = []
        err = ""
        try:
            from core.models import AuditLogORM

            rows = _query_orm(AuditLogORM, 100)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
        if err:
            C.render_error_card("审计流水读取失败", message=err,
                                suggestion="确认数据库已初始化（侧边栏「数据库：就绪」）。",
                                trace_id=C.new_trace_id())
        elif not rows:
            C.not_ready("暂无审计记录",
                        "审计流水由系统自动写入（下单/撤单、风控参数变更、策略启停、自动化开关、备份恢复等）。"
                        "产生这些操作后即可在这里按时间/actor/动作查看。", icon="🧾")
        else:
            frame = pd.DataFrame([
                {"时间": C.fmt_datetime(r.get("ts")), "actor": r.get("actor"),
                 "动作": r.get("action"), "对象": f"{r.get('target_type') or ''}:{r.get('target_id') or ''}",
                 "结果": r.get("result"), "trace_id": r.get("trace_id"),
                 "before": str(r.get("before_json") or "")[:120], "after": str(r.get("after_json") or "")[:120]}
                for r in rows
            ])
            C.data_table(frame, height=380)
            st.caption("最近 100 条审计记录（只读，不可删除）。")

    with tab_dq:
        rows = []
        err = ""
        try:
            from core.models import DataQualityIssueORM

            rows = _query_orm(DataQualityIssueORM, 100)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
        if err:
            C.render_error_card("数据校验报告读取失败", message=err,
                                suggestion="确认数据库已初始化；行情刷新时才会写入校验结果。",
                                trace_id=C.new_trace_id())
        elif not rows:
            C.not_ready("暂无数据校验记录",
                        "DQ001–DQ007 校验在行情刷新（BarCache.validate）时自动执行；"
                        "到行情页点「拉取/刷新数据」后，问题会记录在此（缺失/重复/跳变/零量/复权因子缺失/过期/停牌未标记）。",
                        icon="🔍")
        else:
            frame = pd.DataFrame([
                {"时间": C.fmt_datetime(r.get("created_at")), "标的": r.get("symbol"),
                 "交易日": C.fmt_date(r.get("trade_date")), "问题码": r.get("issue_code"),
                 "严重度": r.get("severity"), "说明": r.get("detail"),
                 "批次": r.get("run_id")}
                for r in rows
            ])
            C.data_table(frame, height=380)
            st.caption("最近 100 条数据质量问题；不影响主流程，仅报告。")

    with tab_notify:
        rows = []
        err = ""
        try:
            from core.models import NotificationORM

            rows = _query_orm(NotificationORM, 100)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
        if err:
            C.render_error_card("通知记录读取失败", message=err,
                                suggestion="确认数据库已初始化。", trace_id=C.new_trace_id())
        elif not rows:
            C.not_ready("暂无通知记录",
                        "通知在自动化任务完成/失败、熔断触发、订单被拒、日报等事件时产生（需在设置页开启渠道）；"
                        "当前无记录说明尚未触发或通知开关关闭。", icon="🔔")
        else:
            frame = pd.DataFrame([
                {"时间": C.fmt_datetime(r.get("sent_at")), "渠道": r.get("channel"),
                 "事件": r.get("event_type"), "标题": r.get("title"),
                 "状态": r.get("status"), "错误": r.get("error")}
                for r in rows
            ])
            C.data_table(frame, height=380)
            st.caption("最近 100 条通知记录；SENT/FAILED/DISABLED。")

    C.todo_expander(TODO_ITEMS)

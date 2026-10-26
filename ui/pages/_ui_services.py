"""UI 服务调用小工具（下划线开头，不被 app.py 当作页面发现）。

把 ``app_services`` 的 ``(ok, data, message)`` 约定统一成页面易用的辅助，避免每个
页面重复写 try/except、中文错误卡与占位逻辑：

    ok, data, msg = call(S.get_account)
    if not ok: fail_card(msg, "账户读取失败")

本模块只做展示层适配（格式化、绘图、错误卡），不包含业务规则。
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping, Sequence

import pandas as pd
import streamlit as st

from . import _common as C


def call(func: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[bool, Any, str]:
    """调用 app_services 函数并兜住意外异常：``(ok, data, 中文消息)``。"""
    try:
        result = func(*args, **kwargs)
    except Exception as exc:  # app_services 承诺不抛异常，这里只兜底
        return False, None, f"{getattr(func, '__name__', '服务调用')} 异常：{type(exc).__name__}: {exc}"
    if not isinstance(result, tuple) or len(result) != 3:
        return False, None, f"{getattr(func, '__name__', '服务调用')} 返回格式异常（应返回 ok/data/message）"
    ok, data, message = result
    return bool(ok), data, str(message or "")


def fail_card(message: str, title: str = "服务调用失败", *, suggestion: str | None = None,
              warning: bool = False) -> None:
    """统一失败展示：中文错误卡（或黄色警告），不白屏。"""
    if warning:
        C.render_warning_card(title, message, suggestion=suggestion)
    else:
        C.render_error_card(title, message=message, suggestion=suggestion,
                            trace_id=C.new_trace_id())


def rows_to_frame(rows: Iterable[Any], columns: Sequence[str], *,
                  formatters: Mapping[str, Callable[[Any], Any]] | None = None) -> pd.DataFrame:
    """服务层 dict/对象列表 → DataFrame（列名固定、可选格式化）。"""
    records: list[dict[str, Any]] = []
    for row in rows or []:
        if isinstance(row, Mapping):
            item = dict(row)
        elif hasattr(row, "model_dump"):
            item = row.model_dump()
        elif hasattr(row, "__dict__"):
            item = {key: value for key, value in vars(row).items() if not key.startswith("_")}
        else:
            continue
        records.append(item)
    frame = pd.DataFrame(records, columns=list(columns))
    for column, formatter in (formatters or {}).items():
        if column in frame.columns:
            frame[column] = frame[column].map(formatter)
    return frame


def line_chart(frame: pd.DataFrame, x: str, y: str, *, title: str, y_label: str = "",
               color: str = C.COLOR_UP, height: int = 300,
               secondary: str | None = None, secondary_label: str | None = None) -> None:
    """通用折线图（权益/资金曲线）；无数据时给带文字占位而不是空框。"""
    import plotly.graph_objects as go

    if frame is None or frame.empty or x not in frame.columns or y not in frame.columns:
        C.chart_placeholder(title, "暂无数据：产生成交或运行盘后任务后生成", height=height)
        return
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(x=frame[x], y=frame[y], name=title, mode="lines",
                   line=dict(color=color, width=1.6))
    )
    if secondary and secondary in frame.columns:
        fig.add_trace(
            go.Scatter(x=frame[x], y=frame[secondary], name=secondary_label or secondary,
                       mode="lines", line=dict(color="#5f6368", width=1.1, dash="dot"))
        )
    fig.update_layout(height=height, margin=dict(l=8, r=8, t=32, b=8),
                      legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
                      hovermode="x unified", title=title)
    if y_label:
        fig.update_yaxes(title_text=y_label)
    st.plotly_chart(fig, use_container_width=True)


def pie_chart(labels: Sequence[str], values: Sequence[float], *, title: str,
              height: int = 300) -> None:
    """资产配置饼图（现金/持仓）；全 0 时给文字占位。"""
    import plotly.graph_objects as go

    total = sum(float(v) for v in values if v is not None)
    if total <= 0:
        C.chart_placeholder(title, "暂无资产数据：账户创建后自动生成", height=height)
        return
    fig = go.Figure(go.Pie(labels=list(labels), values=list(values), hole=0.35))
    fig.update_layout(height=height, margin=dict(l=8, r=8, t=32, b=8), title=title)
    st.plotly_chart(fig, use_container_width=True)


def info(message: str) -> None:
    """统一成功/中性提示（中文）。"""
    st.success(message) if message else None

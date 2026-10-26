"""CF-12 公共 UI 组件（M0-07）。

九个页面共用的展示与交互组件，集中在本文件，避免各页面重复实现、保证视觉与
交互口径一致（SPEC §8.7 F7 规则 3 / §10 全局元素 / TASKS CF-12）：

- 二次确认：``confirm_action``（破坏性操作必须先勾选确认，可选确认词）
- 中文错误卡片：``render_error_card``（原因码 + 中文原因 + 建议动作 + trace_id）
- 金额/涨跌格式：``money`` / ``pct`` / ``pnl_color`` / ``colored``
- 状态条 / KPI 卡：``status_bar`` / ``kpi_row``
- 页面标题栏：``page_header``
- 数据未就绪占位：``not_ready`` / ``chart_placeholder`` / ``table_placeholder`` / ``empty_state``
- 页面跳转联动：``goto_page``（配合 app.py 的 ``take_pending_nav``）
- 应用元信息：``app_meta``（只读 config.yaml 的展示字段，不导入 core.config）

**边界**：本文件只做「展示 + 交互」，不查库、不计算指标、不下单。所有真实数据都
来自服务层（SPEC §6 接口契约），页面通过 TODO 注记接口来源，待 M1/M2 服务就绪后接入。
"""

from __future__ import annotations

import copy
import html
import importlib.util
import math
import os
import platform
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd
import streamlit as st

# ---------------------------------------------------------------------------
# 颜色与常量（A 股习惯：涨红跌绿；SPEC §8.7 规则 3）
# ---------------------------------------------------------------------------
COLOR_UP = "#d93025"      # 上涨 / 盈利：红
COLOR_DOWN = "#0b8043"    # 下跌 / 亏损：绿
COLOR_FLAT = "#5f6368"    # 持平 / 中性：灰
COLOR_OK = "#0b8043"
COLOR_WARN = "#e37400"
COLOR_ERROR = "#c5221f"
COLOR_MUTED = "#9aa0a6"

DASH = "—"                # 无数据统一展示为破折号，而不是报错/空字符串
TONE_COLORS = {
    "up": COLOR_UP,
    "down": COLOR_DOWN,
    "flat": COLOR_FLAT,
    "ok": COLOR_OK,
    "warn": COLOR_WARN,
    "error": COLOR_ERROR,
    "muted": COLOR_MUTED,
}

ROOT = Path(__file__).resolve().parents[2]   # quantdesk/
CONFIG_PATH = ROOT / "config.yaml"


# ---------------------------------------------------------------------------
# 格式化：金额 / 百分比 / 涨跌颜色
# ---------------------------------------------------------------------------
def _to_float(value: Any) -> float | None:
    """尽力把输入转为 float；无法转换或非有限数返回 None（展示层容错，不抛异常）。"""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def money(value: Any, *, digits: int = 2, symbol: bool = False, dash: str = DASH) -> str:
    """金额格式化：千分位 + 两位小数（SPEC §8.7 规则 3），如 ``1,234,567.89``。

    - ``symbol=True`` 时前缀 ``¥``；
    - ``None``/NaN/异常输入返回 ``—``，不抛异常（页面永不因脏数据白屏）。
    """
    v = _to_float(value)
    if v is None:
        return dash
    text = f"{v:,.{digits}f}"
    return f"¥{text}" if symbol else text


def pct(value: Any, *, digits: int = 2, signed: bool = False,
        ratio: bool = True, dash: str = DASH) -> str:
    """百分比格式化。

    - ``ratio=True``（默认）：输入是比率，``0.0123`` → ``1.23%``；
    - ``ratio=False``：输入已是百分数，``1.23`` → ``1.23%``；
    - ``signed=True``：正数带 ``+``（当日盈亏等场景）。
    """
    v = _to_float(value)
    if v is None:
        return dash
    if ratio:
        v *= 100.0
    sign = "+" if signed and v > 0 else ""
    return f"{sign}{v:,.{digits}f}%"


def pnl_color(value: Any) -> str:
    """涨跌/盈亏颜色：>0 红、<0 绿、=0/None 灰（A 股习惯）。"""
    v = _to_float(value)
    if v is None or v == 0:
        return COLOR_FLAT
    return COLOR_UP if v > 0 else COLOR_DOWN


def colored(text: str, value: Any) -> str:
    """把文本包成按涨跌着色的 HTML ``span``（配合 ``unsafe_allow_html=True`` 使用）。"""
    return f'<span style="color:{pnl_color(value)}">{html.escape(str(text))}</span>'


def fmt_date(value: Any, dash: str = DASH) -> str:
    """日期格式化：``2026-10-25``；None/异常返回 ``—``。"""
    if value is None:
        return dash
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return dash


def fmt_datetime(value: Any, dash: str = DASH) -> str:
    """时间格式化：``2026-10-25 09:30:00``（统一本地时区；时区换算由服务层负责）。"""
    if value is None:
        return dash
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return dash


# ---------------------------------------------------------------------------
# 全局样式（app.py 启动时注入一次）
# ---------------------------------------------------------------------------
def inject_global_css() -> None:
    """注入全局 CSS：页面标题、KPI 卡、状态灯、占位块、表格样式（9 页视觉统一）。"""
    st.markdown(
        """
        <style>
          /* 页面标题栏 */
          .qd-page-title { font-size: 1.55rem; font-weight: 700; color: #1f2937;
                           margin: 0 0 .15rem 0; letter-spacing: .2px; }
          .qd-page-sub   { font-size: .86rem; color: #6b7280; margin-bottom: .9rem; }
          .qd-brand      { font-size: 1.12rem; font-weight: 700; color: #1f2937; }
          /* KPI 指标卡（M5-01 视觉统一：统一卡面/圆角/轻阴影） */
          .qd-kpi { border: 1px solid #e5e7eb; border-radius: 10px; padding: .7rem .85rem;
                    background: #fbfcfd; min-height: 84px;
                    box-shadow: 0 1px 2px rgba(16,24,40,.04); transition: box-shadow .15s ease; }
          .qd-kpi:hover { box-shadow: 0 2px 8px rgba(16,24,40,.08); }
          [data-testid="stVerticalBlockBorderWrapper"] { border-radius: 10px; }
          .qd-kpi-label { font-size: .78rem; color: #6b7280; margin-bottom: .2rem; }
          .qd-kpi-value { font-size: 1.32rem; font-weight: 700; line-height: 1.5; color: #1f2937; }
          .qd-kpi-delta { font-size: .78rem; margin-top: .1rem; }
          /* 状态条 */
          .qd-status-wrap { display: flex; flex-wrap: wrap; gap: .45rem; margin: .15rem 0 .5rem 0; }
          .qd-status { display: inline-flex; align-items: center; gap: .38rem;
                       border: 1px solid #e5e7eb; border-radius: 999px; padding: .22rem .7rem;
                       background: #fbfcfd; font-size: .8rem; color: #374151; }
          .qd-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
          /* 图表占位 */
          .qd-chart-ph { border: 1.5px dashed #d1d5db; border-radius: 10px; background: #fafbfc;
                         display: flex; flex-direction: column; align-items: center;
                         justify-content: center; color: #6b7280; font-size: .86rem; gap: .25rem; }
          .qd-chart-ph b { color: #4b5563; }
          /* 空表占位 */
          table.qd-table { width: 100%; border-collapse: collapse; font-size: .82rem; }
          table.qd-table th { text-align: left; background: #f3f4f6; color: #374151;
                              font-weight: 600; padding: .45rem .6rem;
                              border-bottom: 1px solid #e5e7eb; white-space: nowrap; }
          table.qd-table td { padding: .5rem .6rem; border-bottom: 1px solid #f1f2f4;
                              color: #4b5563; }
          table.qd-table td.qd-empty { text-align: center; color: #9aa0a6; }
          /* 错误 / 提示卡 */
          .qd-card { border-radius: 10px; padding: .75rem .9rem; margin: .25rem 0 .5rem 0;
                     border: 1px solid #e5e7eb; }
          .qd-card-error { border-color: #f3c2be; background: #fdf4f3; }
          .qd-card-warn  { border-color: #f5ddb5; background: #fffaf1; }
          .qd-card-info  { border-color: #cfe0f5; background: #f4f8fe; }
          .qd-mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
                     font-size: .78rem; background: #f3f4f6; border-radius: 4px; padding: .05rem .3rem; }
          .qd-footer { color: #9aa0a6; font-size: .74rem; margin-top: 1.6rem;
                       border-top: 1px solid #eceff1; padding-top: .5rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# 应用元信息（core.config.get_config 读取，供侧边栏/设置页展示）
# ---------------------------------------------------------------------------
_DEFAULT_META: dict[str, Any] = {
    "name": "QuantDesk",
    "version": "1.0.0",
    "timezone": "Asia/Shanghai",
    "log_level": "INFO",
    "provider": "未配置",
    "fallbacks": [],
    "universe": [],
    "history_years": 5,
    "adjust": "hfq",
    "request_interval_ms": 300,
    "stale_threshold_hours": 24.0,
    "account_name": "模拟账户",
    "broker": "paper",
    "initial_cash": 100000.0,
    "min_order_amount": 1000.0,
    "allow_market_order": False,
    "backtest": {"default_initial_cash": 100000.0, "benchmark": "000300.SH"},
    "risk": {},
    "jobs": {"prepare": "08:50", "scan": "10:00", "summarize": "15:30"},
    "automation_enabled": False,
    "notify_enabled": False,
    "notify_channels": {"desktop": True, "email": False, "webhook": False},
    "config_ok": False,
    "config_source": "",
    "config_path": str(CONFIG_PATH),
    "config_error": "",
}


def _meta_from_yaml() -> dict[str, Any]:
    """降级路径：``core.config`` 不可用时，直接只读解析 ``config.yaml`` 的展示字段。"""
    import yaml  # 本地导入：yaml 缺失也不影响页面启动

    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    app = raw.get("app") or {}
    data = raw.get("data") or {}
    trading = raw.get("trading") or {}
    automation = raw.get("automation") or {}
    notify = raw.get("notify") or {}
    backtest = raw.get("backtest") or {}
    return {
        "name": str(app.get("name") or _DEFAULT_META["name"]),
        "version": str(app.get("version") or _DEFAULT_META["version"]),
        "timezone": str(app.get("timezone") or _DEFAULT_META["timezone"]),
        "log_level": str(app.get("log_level") or _DEFAULT_META["log_level"]),
        "provider": str(data.get("provider") or "未配置"),
        "fallbacks": list(data.get("fallbacks") or []),
        "universe": [str(s) for s in (data.get("universe") or [])],
        "history_years": data.get("history_years", _DEFAULT_META["history_years"]),
        "adjust": str(data.get("adjust") or _DEFAULT_META["adjust"]),
        "request_interval_ms": data.get("request_interval_ms", _DEFAULT_META["request_interval_ms"]),
        "stale_threshold_hours": float(
            data.get("stale_threshold_hours", _DEFAULT_META["stale_threshold_hours"])
        ),
        "account_name": str(trading.get("account_name") or _DEFAULT_META["account_name"]),
        "broker": str(trading.get("broker") or _DEFAULT_META["broker"]),
        "initial_cash": float(trading.get("initial_cash") or _DEFAULT_META["initial_cash"]),
        "min_order_amount": float(trading.get("min_order_amount") or _DEFAULT_META["min_order_amount"]),
        "allow_market_order": bool(trading.get("allow_market_order", False)),
        "backtest": {
            "default_initial_cash": float(
                backtest.get("default_initial_cash") or _DEFAULT_META["backtest"]["default_initial_cash"]
            ),
            "benchmark": str(backtest.get("benchmark") or _DEFAULT_META["backtest"]["benchmark"]),
        },
        "risk": dict(raw.get("risk") or {}),
        "jobs": dict(automation.get("jobs") or _DEFAULT_META["jobs"]),
        "automation_enabled": bool(automation.get("enabled", False)),
        "notify_enabled": bool(notify.get("enabled", False)),
        "notify_channels": dict((notify.get("channels") or {}) or _DEFAULT_META["notify_channels"]),
        "config_source": str(CONFIG_PATH),
    }


def app_meta() -> dict[str, Any]:
    """读取应用配置中的展示字段（版本/数据源/自选池/风控默认值等）。

    优先调用 ``core.config.get_config()``（M0-02 唯一配置来源，含 ``QD_`` 环境变量覆盖）；
    配置加载失败（文件缺失/语法错误/字段非法）时**两级降级**：先尝试只读解析
    ``config.yaml``，仍失败则保留内置默认值，并以 ``config_ok=False`` +
    ``config_error`` 交给设置页展示中文提示，绝不让页面报错/白屏。
    ``QD_OFFLINE=1`` 或数据源为 mock 时置 ``offline=True``（侧边栏黄色提示）。
    """
    meta = copy.deepcopy(_DEFAULT_META)
    error = ""
    try:
        from core.config import get_config  # 懒导入：core 未就绪时 UI 仍可启动

        cfg = get_config()
        risk = cfg.risk_config()
        meta.update(
            {
                "name": cfg.app.name,
                "version": cfg.app.version,
                "timezone": cfg.app.timezone,
                "log_level": cfg.app.log_level,
                "provider": cfg.data.provider,
                "fallbacks": list(cfg.data.fallbacks),
                "universe": list(cfg.data.universe),
                "history_years": cfg.data.history_years,
                "adjust": cfg.data.adjust,
                "request_interval_ms": cfg.data.request_interval_ms,
                "stale_threshold_hours": float(cfg.data.stale_threshold_hours),
                "account_name": cfg.trading.account_name,
                "broker": cfg.trading.broker,
                "initial_cash": float(cfg.trading.initial_cash),
                "min_order_amount": float(cfg.trading.min_order_amount),
                "allow_market_order": bool(cfg.trading.allow_market_order),
                "backtest": {
                    "default_initial_cash": float(cfg.backtest.default_initial_cash),
                    "benchmark": cfg.backtest.benchmark,
                },
                "risk": risk.model_dump(mode="json"),
                "jobs": cfg.automation.jobs.model_dump(mode="json"),
                "automation_enabled": bool(cfg.automation.enabled),
                "notify_enabled": bool(cfg.notify.enabled),
                "notify_channels": cfg.notify.channels.model_dump(mode="json"),
                "config_source": str(cfg.source_path or CONFIG_PATH),
            }
        )
    except Exception as exc:  # 配置不可用：降级为 yaml 只读/内置默认值，界面继续可用
        error = f"{type(exc).__name__}: {exc}"
        try:
            meta.update(_meta_from_yaml())
        except Exception:
            pass

    meta["config_error"] = error
    meta["config_ok"] = not error
    env_offline = os.environ.get("QD_OFFLINE", "").strip().lower()
    meta["offline"] = (
        env_offline in {"1", "true", "yes", "on"}
        or str(meta.get("provider")).lower() == "mock"
    )
    return meta


def account_label(meta: Mapping[str, Any] | None = None) -> str:
    """账户下拉占位文案（M2 接入 PortfolioService 多账户后改由服务层提供）。"""
    m = meta or app_meta()
    name = str(m.get("account_name") or "模拟账户")
    return f"{name}（初始资金 {money(m.get('initial_cash'), digits=0)} 元）"


# ---------------------------------------------------------------------------
# 页面标题栏 / 分区 / 状态条 / KPI 卡
# ---------------------------------------------------------------------------
def page_header(title: str, subtitle: str | None = None, *, badge: str | None = None) -> None:
    """页面标题栏：大标题 + 一句话说明 +（可选）右侧小徽标。"""
    st.markdown(f'<div class="qd-page-title">{html.escape(title)}</div>', unsafe_allow_html=True)
    if subtitle:
        suffix = f'&nbsp;&nbsp;<span class="qd-mono">{html.escape(badge)}</span>' if badge else ""
        st.markdown(
            f'<div class="qd-page-sub">{html.escape(subtitle)}{suffix}</div>',
            unsafe_allow_html=True,
        )
    elif badge:
        st.caption(badge)


def section(title: str, caption: str | None = None) -> None:
    """分区小标题（页面内的视觉分块）。"""
    st.markdown(f"##### {title}")
    if caption:
        st.caption(caption)


def status_bar(items: Sequence[Mapping[str, Any]]) -> None:
    """状态条：一行彩色状态灯（总览页 4 项状态、侧边栏状态）。

    参数 ``items`` 为字典序列，字段：``label``（名称）、``value``（状态文案）、
    ``tone``（ok/warn/error/muted）、``help``（悬浮说明，可选）。
    """
    chips: list[str] = []
    for item in items:
        tone = str(item.get("tone") or "muted")
        color = TONE_COLORS.get(tone, COLOR_MUTED)
        label = html.escape(str(item.get("label", "")))
        value = html.escape(str(item.get("value", DASH)))
        tip = html.escape(str(item.get("help") or ""))
        title_attr = f' title="{tip}"' if tip else ""
        chips.append(
            f'<span class="qd-status"{title_attr}><span class="qd-dot" '
            f'style="background:{color}"></span>{label}：{value}</span>'
        )
    st.markdown(f'<div class="qd-status-wrap">{"".join(chips)}</div>', unsafe_allow_html=True)


def kpi_card(label: str, value: str, *, delta: str | None = None,
             delta_value: Any = None, help_text: str | None = None,
             tone: str | None = None) -> None:
    """单个 KPI 指标卡。

    - ``delta``/``delta_value``：附加一行说明，按涨跌自动着色（涨红跌绿）；
    - ``tone``：直接给数值着色（``up`` 红 / ``down`` 绿 / ``flat`` 灰），
      用于「总收益率」「最大回撤」这类本身就是涨跌语义的指标。
    """
    delta_html = ""
    if delta:
        color = pnl_color(delta_value) if delta_value is not None else COLOR_FLAT
        delta_html = f'<div class="qd-kpi-delta" style="color:{color}">{html.escape(delta)}</div>'
    tip = f' title="{html.escape(help_text)}"' if help_text else ""
    value_color = TONE_COLORS.get(str(tone), "#1f2937") if tone else "#1f2937"
    st.markdown(
        f'<div class="qd-kpi"{tip}>'
        f'<div class="qd-kpi-label">{html.escape(label)}</div>'
        f'<div class="qd-kpi-value" style="color:{value_color}">{html.escape(value)}</div>'
        f"{delta_html}</div>",
        unsafe_allow_html=True,
    )


def kpi_row(items: Sequence[Mapping[str, Any]]) -> None:
    """一行 KPI 指标卡（内部按等宽列排布，6 个卡片即 6 列）。"""
    if not items:
        return
    cols = st.columns(len(items))
    for col, item in zip(cols, items):
        with col:
            kpi_card(
                label=str(item.get("label", DASH)),
                value=str(item.get("value", DASH)),
                delta=item.get("delta"),
                delta_value=item.get("delta_value"),
                help_text=item.get("help"),
                tone=item.get("tone"),
            )


# ---------------------------------------------------------------------------
# 数据未就绪占位（中文提示，绝不抛异常 / 白屏）
# ---------------------------------------------------------------------------
def not_ready(title: str, hint: str = "数据未就绪", *, todo: str | None = None,
              icon: str = "🧩") -> None:
    """数据未就绪卡片：说明「还没有数据」以及「下一步该做什么 / 待接入哪个接口」。"""
    with st.container(border=True):
        st.markdown(f"**{icon} {title}**")
        st.markdown(hint or "数据未就绪")
        if todo:
            st.caption(f"待接入：{todo}")


def chart_placeholder(title: str, hint: str = "数据未就绪", *, todo: str | None = None,
                      height: int = 280) -> None:
    """图表占位块：虚线框 + 说明（M1/M2 接入真实 Plotly 图后替换本函数调用）。"""
    with st.container(border=True):
        st.markdown(f"**{title}**")
        st.markdown(
            f'<div class="qd-chart-ph" style="height:{int(height)}px">'
            f"<b>📊 图表占位</b><span>{html.escape(hint)}</span></div>",
            unsafe_allow_html=True,
        )
        if todo:
            st.caption(f"待接入：{todo}")


def table_placeholder(title: str, columns: Sequence[str], *,
                      hint: str = "数据未就绪", todo: str | None = None) -> None:
    """表格占位：渲染中文表头 + 「数据未就绪」行（避免 Streamlit 默认的英文空表）。"""
    cols = [str(c) for c in columns]
    head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    with st.container(border=True):
        st.markdown(f"**{title}**")
        st.markdown(
            f'<table class="qd-table"><thead><tr>{head}</tr></thead>'
            f'<tbody><tr><td class="qd-empty" colspan="{max(len(cols), 1)}">'
            f"{html.escape(hint)}</td></tr></tbody></table>",
            unsafe_allow_html=True,
        )
        if todo:
            st.caption(f"待接入：{todo}")


def _html_dataframe(df: pd.DataFrame, *, max_rows: int = 200) -> None:
    """HTML 兜底表格（当 ``st.dataframe`` 依赖缺失时使用，例如 pyarrow 与 numpy 版本不兼容）。"""
    cols = [str(c) for c in df.columns]
    rows = df.head(max_rows).astype(str).values.tolist()
    head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in row) + "</tr>"
        for row in rows
    )
    st.markdown(
        f'<table class="qd-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>',
        unsafe_allow_html=True,
    )
    if len(df) > max_rows:
        st.caption(f"仅显示前 {max_rows} 行（共 {len(df)} 行）。")


def data_table(df: pd.DataFrame, *, height: int | None = None,
               column_config: Mapping[str, Any] | None = None) -> None:
    """真实数据表（服务层就绪后使用）：统一排序、隐藏索引、适配宽度。

    环境兼容：当前 venv 的 ``pyarrow`` 与 ``numpy`` 版本不匹配时 ``st.dataframe`` 会
    抛 ImportError；此处捕获并降级为 HTML 表格，保证页面不白屏（环境问题见下）。
    TODO(env)：请运维/Lead 修复依赖（``pyarrow`` 需 numpy>=2，或降级 pyarrow），
    修复后可删除本兜底，直接使用 ``st.dataframe``。
    """
    try:
        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            height=height,
            column_config=dict(column_config or {}),
        )
    except Exception:  # pyarrow/numpy 不兼容等环境问题：降级渲染，不阻断页面
        _html_dataframe(df)


def empty_state(title: str, hint: str, *, page: str | None = None,
                button_label: str | None = None, key: str | None = None) -> None:
    """空状态引导：例如「暂无持仓 → 去手动交易」「无账户 → 创建模拟账户」。

    同屏多次调用时请传唯一 ``key``（如按 tab 名），避免 Streamlit 组件 key 重复。
    """
    with st.container(border=True):
        st.markdown(f"#### {title}")
        st.caption(hint)
        if page and button_label:
            widget_key = key or f"qd_empty_{abs(hash((title, page))) % 10**8}"
            if st.button(button_label, key=widget_key):
                goto_page(page)


# ---------------------------------------------------------------------------
# 二次确认（破坏性操作：下单、清仓、删除、重置、改风控、开自动化）
# ---------------------------------------------------------------------------
def confirm_action(key: str, prompt: str, *, button_label: str = "确认执行",
                   confirm_word: str | None = None, danger: bool = True,
                   details: Sequence[str] | None = None, disabled: bool = False) -> bool:
    """二次确认组件；返回 ``True`` 表示用户本轮点击了「确认执行」按钮。

    用法（调用方负责真正的副作用）：:

        if C.confirm_action("clear_pos", "将清空全部持仓并逐单送风控，是否继续？"):
            ...  # TODO OrderService...

    行为：必须勾选「我已了解风险」；``confirm_word`` 非空时还需输入一致确认词。
    未确认时按钮禁用，因此不会产生任何副作用（AC-F7-03）。
    """
    state_key = f"qd_confirm_{key}"
    clicked = False
    with st.container(border=True):
        st.markdown(f"**{'⚠️' if danger else 'ℹ️'} 二次确认**")
        st.markdown(prompt)
        if details:
            for line in details:
                st.caption(f"· {line}")
        acked = st.checkbox(
            "我已了解上述风险，确认执行",
            key=f"{state_key}_ack",
            disabled=disabled,
        )
        word_ok = True
        if confirm_word:
            typed = st.text_input(
                f'请输入确认词「{confirm_word}」以继续',
                key=f"{state_key}_word",
                disabled=disabled,
            )
            word_ok = typed.strip() == confirm_word
            if typed and not word_ok:
                st.caption("⚠️ 确认词不一致，请重新输入。")
        clicked = st.button(
            button_label,
            key=f"{state_key}_btn",
            type="primary" if danger else "secondary",
            disabled=disabled or not (acked and word_ok),
        )
    return bool(clicked)


# ---------------------------------------------------------------------------
# 中文错误 / 警告 / 信息卡片（原因码 + 建议，AC-F7-04）
# ---------------------------------------------------------------------------
# 原因码展示映射（展示用途；权威定义见 SPEC §9.2 与 core/models.py / trading/risk.py）
REASON_HINTS: dict[str, tuple[str, str]] = {
    "RK001": ("标的不在可交易池", "先加入自选池"),
    "RK002": ("标的在黑名单（ST/退市整理/用户禁用）", "更换标的"),
    "RK003": ("标的停牌", "复牌后再操作"),
    "RK004": ("上市不足 60 个交易日", "更换标的"),
    "RK005": ("非交易时段/非交易日", "交易时段再下单"),
    "RK006": ("行情数据过期", "刷新行情/检查数据源"),
    "RK007": ("单票仓位超限", "减少数量或调高上限"),
    "RK008": ("总仓位超限", "减仓或调高上限"),
    "RK009": ("单日亏损熔断", "今日停止买入，明日自动恢复"),
    "RK010": ("价格偏离过大", "检查价格（接近涨跌停/手误）"),
    "RK011": ("价格超出涨跌停区间", "改到有效区间"),
    "RK012": ("单笔金额超限", "拆单或调高上限"),
    "RK013": ("下单频率超限", "稍后再试"),
    "RK014": ("可用资金不足", "减少数量/卖出腾资金"),
    "RK015": ("持仓不足", "检查持仓数量"),
    "RK016": ("T+1 可用数量不足（今日买入不可卖）", "明日再卖"),
    "RK017": ("数量不符合整手规则", "按 100 股整数倍"),
    "RK018": ("参数非法（含风控内部异常 fail-closed）", "检查输入"),
    "RK019": ("重复订单（幂等键冲突）", "查今日委托"),
    "RK020": ("账户停用/自动化关闭时提交自动单", "开启账户或自动化"),
    "BR001": ("券商不可用/网络异常", "稍后重试或切换模拟盘"),
    "BR002": ("券商拒单", "查看券商回报说明"),
    "BR003": ("标的不支持", "更换标的或券商"),
    "BR004": ("适配器未实现（如 QMT 未接入）", "使用模拟盘"),
    "BR005": ("请求超时", "稍后重试"),
    "DQ001": ("缺失 K 线", "重新拉取该区间数据"),
    "DQ002": ("重复 K 线", "去重后再用"),
    "DQ003": ("价格异常跳变", "核对复权与数据源"),
    "DQ004": ("零成交量", "确认是否停牌"),
    "DQ005": ("复权因子缺失", "补拉复权因子"),
    "DQ006": ("数据过期", "刷新数据"),
    "DQ007": ("停牌未标记", "核对停牌列表"),
    "RQ_WARN_NO_ADJ": ("回测未复权", "改用后复权口径"),
    "RQ_WARN_ST_APPROX": ("ST 规则为近似处理", "关注涨跌幅上限差异"),
    "RQ_WARN_SHORT_SAMPLE": ("样本过短（<250 交易日）", "延长回测区间"),
    "RQ_WARN_FEW_TRADES": ("交易过少（<30 笔）", "指标可信度有限"),
    "RQ_WARN_HIGH_SUSPEND": ("停牌占比高", "注意成交假设偏差"),
}


def reason_hint(code: str | None) -> tuple[str, str]:
    """原因码 → (中文含义, 建议动作)；未知码返回通用提示。"""
    if not code:
        return ("未知原因", "请查看日志并联系维护者")
    return REASON_HINTS.get(str(code).strip(), (f"未登记的原因码 {code}", "请查看日志并联系维护者"))


def render_error_card(title: str, *, reason_code: str | None = None,
                      message: str | None = None, suggestion: str | None = None,
                      trace_id: str | None = None, exc: str | None = None) -> None:
    """中文错误卡片：原因码 + 中文原因 + 建议动作 + trace_id（+ 可选堆栈）。

    ``exc`` 通常传 ``traceback.format_exc()``：堆栈默认折叠，页面不白屏。
    卡片内提供「查看日志」按钮 → 跳转日志页并按 trace_id 过滤。
    """
    meaning, advice = reason_hint(reason_code)
    st.markdown(
        f'<div class="qd-card qd-card-error">'
        f"<b>❌ {html.escape(title)}</b><br/>"
        f"{('<b>原因码：</b><span class=qd-mono>' + html.escape(reason_code) + '</span> ' + html.escape(meaning) + '<br/>') if reason_code else ''}"
        f"{('<b>原因：</b>' + html.escape(message) + '<br/>') if message else ''}"
        f"{('<b>建议：</b>' + html.escape(suggestion or advice) + '<br/>') if (suggestion or reason_code) else ''}"
        f"{('<b>trace_id：</b><span class=qd-mono>' + html.escape(trace_id) + '</span>') if trace_id else ''}"
        "</div>",
        unsafe_allow_html=True,
    )
    if trace_id:
        if st.button("查看日志", key=f"qd_err_log_{abs(hash(trace_id)) % 10**8}"):
            goto_page("日志", trace_id=trace_id)
    if exc:
        with st.expander("技术细节（仅供排查，可复制给维护者）", expanded=False):
            st.code(exc)


def render_warning_card(title: str, message: str, *, suggestion: str | None = None) -> None:
    """黄色警告卡片（离线、对账不一致、调度未启动等非致命问题）。"""
    st.markdown(
        f'<div class="qd-card qd-card-warn"><b>⚠️ {html.escape(title)}</b><br/>'
        f"{html.escape(message)}"
        f"{('<br/><b>建议：</b>' + html.escape(suggestion)) if suggestion else ''}</div>",
        unsafe_allow_html=True,
    )


def render_info_card(title: str, message: str) -> None:
    """蓝色信息卡片（说明性提示、演示数据标识等）。"""
    st.markdown(
        f'<div class="qd-card qd-card-info"><b>ℹ️ {html.escape(title)}</b><br/>'
        f"{html.escape(message)}</div>",
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# 跳转联动 / trace_id / 缓存
# ---------------------------------------------------------------------------
def goto_page(title: str, **state: Any) -> None:
    """跳转到另一页面（如总览持仓行 → 持仓页、订单 → 日志页）。

    实现方式：写入 ``_qd_pending_nav`` 并 ``st.rerun``；``app.py`` 在渲染侧边栏
    导航前消费该值，因此不会触发 Streamlit「组件实例化后不可修改 state」限制。
    ``**state`` 写入 ``qd_<name>``，供目标页面 ``st.session_state.get("qd_xxx")`` 读取。
    """
    st.session_state["_qd_pending_nav"] = title
    for key, value in state.items():
        st.session_state[f"qd_{key}"] = value
    st.rerun()


def take_pending_nav() -> str | None:
    """（app.py 专用）取出待跳转的页面标题并清空，避免重复跳转。"""
    return st.session_state.pop("_qd_pending_nav", None)


def new_trace_id() -> str:
    """生成短 trace_id（UI 侧异常兜底用；真实链路的 trace_id 由服务层传递）。"""
    return f"ui-{int(time.time() * 1000):x}-{uuid.uuid4().hex[:6]}"


def clear_caches() -> None:
    """清空 Streamlit 数据缓存（刷新按钮：写操作后必须清缓存，SPEC §8.7 规则 6）。"""
    try:
        st.cache_data.clear()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 状态文案映射（枚举值 → 中文，仅供展示）
# ---------------------------------------------------------------------------
ORDER_STATUS_LABELS = {
    "PENDING": "待提交",
    "SUBMITTED": "已报",
    "PARTIALLY_FILLED": "部分成交",
    "FILLED": "已成交",
    "CANCELLED": "已撤单",
    "REJECTED": "已拒绝",
    "EXPIRED": "已失效",
}
STRATEGY_MODE_LABELS = {"OFF": "关闭", "PAPER": "模拟盘自动", "AUTO": "自动"}
SIDE_LABELS = {"BUY": "买入", "SELL": "卖出"}
JOB_STATUS_LABELS = {
    "RUNNING": "运行中",
    "DONE": "成功",
    "FAILED": "失败",
    "SKIPPED": "跳过（非交易日）",
}
PROVIDER_STATUS_LABELS = {"OK": "正常", "DEGRADED": "降级", "DOWN": "离线"}
SEVERITY_LABELS = {"ERROR": "错误", "WARN": "警告", "INFO": "提示"}


def order_status_label(value: Any) -> str:
    return ORDER_STATUS_LABELS.get(str(value), str(value) if value else DASH)


def strategy_mode_label(value: Any) -> str:
    return STRATEGY_MODE_LABELS.get(str(value), str(value) if value else DASH)


def side_label(value: Any) -> str:
    """买卖方向中文（BUY/SELL → 买入/卖出）。"""
    return SIDE_LABELS.get(str(value), str(value) if value else DASH)


def job_status_label(value: Any) -> str:
    return JOB_STATUS_LABELS.get(str(value), str(value) if value else DASH)


# ---------------------------------------------------------------------------
# 导出 / TODO 说明 / 页脚
# ---------------------------------------------------------------------------
def export_csv_button(df: pd.DataFrame | None, filename: str, *, key: str,
                      hint: str = "数据就绪后可导出 CSV") -> None:
    """导出 CSV 按钮（M0 骨架：无数据时禁用并给中文说明）。"""
    has_data = isinstance(df, pd.DataFrame) and not df.empty
    payload = df.to_csv(index=False).encode("utf-8-sig") if has_data else b""
    st.download_button(
        "⬇️ 导出 CSV",
        data=payload,
        file_name=filename,
        mime="text/csv",
        key=key,
        disabled=not has_data,
        help=None if has_data else hint,
    )


def todo_expander(items: Iterable[str], *, title: str = "🧩 接口待接入（M0 骨架说明）") -> None:
    """折叠展示本页待接入的服务层接口（不影响布局与体验）。"""
    lines = [f"- `{item}`" for item in items if item]
    if not lines:
        return
    with st.expander(title, expanded=False):
        st.markdown("\n".join(lines))
        st.caption("数据与计算全部在服务层完成；页面只负责展示与交互（ARCHITECTURE §2.2）。")


def footer(meta: Mapping[str, Any] | None = None) -> None:
    """页脚：版本、模式与合规免责声明（SPEC §11.6 / §11.8）。"""
    m = dict(meta or app_meta())
    st.markdown(
        f'<div class="qd-footer">{html.escape(str(m.get("name", "QuantDesk")))} '
        f'v{html.escape(str(m.get("version", "1.0.0")))} · 模拟盘（实盘未接入）· '
        "个人非商业研究用途，不构成投资建议；回测/模拟结果不代表未来收益。</div>",
        unsafe_allow_html=True,
    )


def env_check() -> list[tuple[str, bool, str]]:
    """本地依赖探测（仅展示，不执行业务逻辑）：(名称, 是否可用, 说明)。"""
    checks: list[tuple[str, bool, str]] = []
    specs = [
        ("streamlit", "界面框架（必需）"),
        ("pandas", "数据处理（必需）"),
        ("plotly", "图表（必需）"),
        ("yaml", "配置读取（必需）"),
        ("akshare", "A 股数据源（可选）"),
        ("talib", "TA-Lib 指标（可选，缺失时用 pandas 兜底）"),
        ("ccxt", "加密交易所适配（可选）"),
        ("apscheduler", "调度器（自动化必需）"),
    ]
    for mod, desc in specs:
        try:
            ok = importlib.util.find_spec(mod) is not None
        except (ImportError, ValueError):
            ok = False
        checks.append((f"{mod}", ok, desc))
    return checks


def python_version_text() -> str:
    """当前 Python 版本（环境页展示用）。"""
    return f"Python {platform.python_version()} · {platform.system()} {platform.release()}"


def sample_error_card_demo() -> None:
    """错误卡样式自检（M2-08 只在需要演示时调用，默认不展示）。"""
    render_error_card(
        "下单被风控拒绝",
        reason_code="RK007",
        message="买入 600519.SH 后单票仓位将达 23.4%，超过上限 20.0%。",
        suggestion="减少 300 股，或到设置页把单票上限调到 25%。",
        trace_id=new_trace_id(),
    )


__all__ = [
    "COLOR_UP", "COLOR_DOWN", "COLOR_FLAT", "COLOR_OK", "COLOR_WARN", "COLOR_ERROR",
    "DASH", "REASON_HINTS", "ORDER_STATUS_LABELS", "STRATEGY_MODE_LABELS",
    "JOB_STATUS_LABELS", "PROVIDER_STATUS_LABELS", "SEVERITY_LABELS",
    "app_meta", "account_label", "inject_global_css", "page_header", "section", "status_bar",
    "kpi_card", "kpi_row", "money", "pct", "pnl_color", "colored", "fmt_date",
    "fmt_datetime", "not_ready", "chart_placeholder", "table_placeholder",
    "data_table", "empty_state", "confirm_action", "render_error_card",
    "render_warning_card", "render_info_card", "reason_hint", "goto_page",
    "take_pending_nav", "new_trace_id", "clear_caches", "export_csv_button",
    "todo_expander", "footer", "env_check", "python_version_text",
    "order_status_label", "strategy_mode_label", "job_status_label", "side_label",
    "SIDE_LABELS",
    "sample_error_card_demo",
]

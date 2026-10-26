"""页面：策略（M3-05 接线版）。

数据来源：``strategy.base.list_strategies()``（策略注册表，内置策略自动发现）。
展示：策略列表、策略详情与参数说明、按 ``params_schema`` 动态生成参数表单并校验；
可一键带着参数跳到回测页直接运行（``app_services.run_backtest``）。
策略**实例**管理（新建/启停/自动交易）依赖 strategy_instances 服务，当前给明确占位。
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from . import _common as C
from . import _ui_services as U

PAGE_TITLE = "策略"
PAGE_ICON = "🧠"
PAGE_ORDER = 4

TODO_ITEMS = [
    "策略实例管理（新建/编辑/启停/模式）与自动交易：依赖 strategy_instances 服务（M3-05）",
    "信号预览（最近 60 交易日，只展示不下单）：待门面暴露只读信号生成入口",
    "策略异常堆栈展示：待策略运行记录落库",
]

FIELD_TYPE_LABELS = {"int": "整数", "float": "小数", "bool": "开关", "str": "文本", "select": "枚举"}


def _strategy_rows(items: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "key": item.get("key"),
                "名称": item.get("display_name"),
                "说明": (item.get("description") or "")[:60],
                "参数数": len(item.get("params_schema") or {}),
                "预热 Bar 数": item.get("startup_bars"),
            }
            for item in items
        ]
    )


def _param_widget(strategy_key: str, name: str, spec: dict) -> object:
    label = str(spec.get("label") or name)
    if spec.get("unit"):
        label = f"{label}（{spec['unit']}）"
    help_text = str(spec.get("help") or "")
    key = f"qd_stg_p__{strategy_key}__{name}"
    ptype = str(spec.get("type") or "str")
    default = spec.get("default")

    if ptype == "int":
        kwargs: dict = {"value": int(default), "step": int(spec.get("step") or 1)}
        if spec.get("min") is not None:
            kwargs["min_value"] = int(spec["min"])
        if spec.get("max") is not None:
            kwargs["max_value"] = int(spec["max"])
        return st.number_input(label, key=key, help=help_text, **kwargs)
    if ptype == "float":
        kwargs = {"value": float(default), "step": float(spec.get("step") or 0.1), "format": "%.2f"}
        if spec.get("min") is not None:
            kwargs["min_value"] = float(spec["min"])
        if spec.get("max") is not None:
            kwargs["max_value"] = float(spec["max"])
        return st.number_input(label, key=key, help=help_text, **kwargs)
    if ptype == "bool":
        return st.checkbox(label, value=bool(default), key=key, help=help_text)
    if ptype == "select":
        choices = list(spec.get("choices") or [])
        index = choices.index(default) if default in choices else 0
        return st.selectbox(label, choices, index=index, key=key, help=help_text)
    return st.text_input(label, value=str(default if default is not None else ""),
                         key=key, help=help_text)


def _render_params(strategy_key: str, schema: dict) -> dict:
    values: dict = {}
    if not schema:
        st.caption("该策略无可配置参数（使用默认逻辑）。")
        return values
    columns = st.columns(3)
    for index, (name, spec) in enumerate(schema.items()):
        with columns[index % 3]:
            values[name] = _param_widget(strategy_key, name, spec)
    return values


def render() -> None:
    C.page_header("策略", "策略注册表、参数说明与一键回测", badge="M3 接线")

    try:
        from strategy import get_strategy, list_strategies

        items = list_strategies()
    except Exception as exc:
        C.render_error_card(
            "策略注册表加载失败",
            message=f"{type(exc).__name__}: {exc}",
            suggestion="确认 strategy/base.py 与 strategy/builtin/*.py 已交付且无语法错误，重启 Streamlit 后重试。",
            trace_id=C.new_trace_id(),
        )
        C.todo_expander(TODO_ITEMS)
        return

    if not items:
        C.not_ready("暂无可用策略",
                    "策略注册表为空：请确认 strategy/builtin/ 下存在策略文件（@register 装饰器自动注册）。",
                    icon="🧠")
        C.todo_expander(TODO_ITEMS)
        return

    C.status_bar(
        [
            {"label": "已注册策略", "value": f"{len(items)} 个", "tone": "ok"},
            {"label": "策略实例", "value": "暂未开放", "tone": "warn",
             "help": "strategy_instances 服务接入后支持实例管理（M3-05）"},
            {"label": "自动交易", "value": "需在自动化页开启", "tone": "muted"},
        ]
    )

    st.markdown("##### 策略列表")
    C.data_table(_strategy_rows(items), height=220)

    # ---- 策略详情 + 参数表单 ----
    with st.container(border=True):
        st.markdown("##### 策略详情与参数")
        labels = {f"{item.get('display_name')}（{item.get('key')}）": item for item in items}
        selected = st.selectbox("选择策略", list(labels), key="qd_stg_select")
        item = labels[selected]
        strategy_key = str(item.get("key"))

        d1, d2 = st.columns([1.4, 2.6])
        with d1:
            st.markdown(f"**{item.get('display_name')}**（key=`{strategy_key}`）")
            st.caption(str(item.get("description") or "（无说明）"))
            st.caption(f"预热所需历史：{item.get('startup_bars') or 0} 根 bar；"
                       f"参数数：{len(item.get('params_schema') or {})}")
        with d2:
            schema = dict(item.get("params_schema") or {})
            values = _render_params(strategy_key, schema)

        params: dict | None = None
        error = ""
        try:
            params = get_strategy(strategy_key).validate_params(values)
        except Exception as exc:
            error = str(exc)
        if error:
            C.render_error_card(
                "策略参数不合法",
                message=error,
                suggestion="按上方中文提示调整参数（如快线周期必须小于慢线周期）后重试。",
                trace_id=C.new_trace_id(),
            )
        else:
            st.caption(f"当前参数（已校验）：{params}")

        a1, a2, a3 = st.columns([1.2, 1.2, 3])
        with a1:
            if st.button("🧪 去回测（带当前参数）", use_container_width=True,
                         key="qd_stg_go_backtest", disabled=params is None):
                C.goto_page("回测", strategy=strategy_key, bt_params=params or {})
        with a2:
            st.button("📄 查看参数说明", use_container_width=True, key="qd_stg_show_schema",
                      on_click=lambda: st.session_state.__setitem__("qd_stg_schema_open", True))
        with a3:
            st.caption("回测页会自动带入策略与参数；也可在回测页调整区间/标的/资金后运行。")

        if st.session_state.get("qd_stg_schema_open"):
            schema_rows = [
                {
                    "参数": name,
                    "中文名": spec.get("label"),
                    "类型": FIELD_TYPE_LABELS.get(str(spec.get("type")), str(spec.get("type"))),
                    "默认值": spec.get("default"),
                    "范围": f"{spec.get('min', '-')} ~ {spec.get('max', '-')}",
                    "单位": spec.get("unit") or "",
                    "说明": spec.get("help") or "",
                }
                for name, spec in (item.get("params_schema") or {}).items()
            ]
            if schema_rows:
                C.data_table(pd.DataFrame(schema_rows), height=260)
            else:
                C.not_ready("该策略无可配置参数", "策略使用内置默认逻辑。", icon="📄")

    # ---- 策略实例管理（待服务）----
    st.markdown("##### 策略实例（自动交易）")
    C.not_ready(
        "策略实例管理暂未开放",
        "实例的新建/编辑/启停、运行模式（关闭/仅信号/模拟盘自动）与信号预览，需要 strategy_instances "
        "服务与 signals 查询入口；当前可在「回测」页验证策略逻辑，在「自动化」页手动触发任务。",
        todo="strategy_instances CRUD + strategy_signals 查询（M3-05）",
        icon="🧩",
    )
    C.table_placeholder(
        "策略实例列表（数据就绪后显示）",
        ["名称", "策略", "参数摘要", "标的池", "模式", "状态", "最近信号时间"],
        hint="等待 strategy_instances 服务接入（M3-05）",
        todo="strategy_instances 查询接口",
    )

    C.todo_expander(TODO_ITEMS)

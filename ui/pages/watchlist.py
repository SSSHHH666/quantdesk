"""页面：自选池（R4 新增，第 10 页）。

职责：自选池管理 —— 列表（代码/名称/分组/最新价/涨跌幅）、增删改、批量粘贴导入、
分组切换（近期/远期）、导出 CSV。自选池是数据拉取、策略实例与自动化的默认标的集合
（SPEC §5 / §8.1）。

存储：优先使用 Dev1 的 ``data/watchlist.py``（尚未交付）；当前由
``ui/pages/_watchlist_store.py`` 以 JSON 文件兜底（``runtime/watchlist.json``，原子写入），
接口按未来服务层形状设计，替换时本页无需改动（见该文件 TODO）。

数据：最新价/涨跌幅由 ``DataProvider.get_bars`` 取最近 40 天日线计算（服务层调用，
不直连网络）；取数失败时价格列显示「—」，页面仍可正常增删改。
"""

from __future__ import annotations

import datetime as dt
import traceback

import pandas as pd
import streamlit as st

from . import _common as C
from . import _watchlist_store as store

PAGE_TITLE = "自选池"
PAGE_ICON = "⭐"
PAGE_ORDER = 3

TODO_ITEMS = [
    "自选池分组与 config.data.universe 双向同步（M3-07 设置页保存能力）",
    "按分组批量拉取行情（scripts/fetch_data.py，M1-13）",
    "自动化/策略页直接引用自选池分组（M3-02/M3-05）",
    "分组迁移：data.watchlist 的 default 组在页面上暂按「近期」展示（如需独立「未分组」再扩展）",
]

GROUP_CHOICES = ["全部", *store.GROUPS]


# ---------------------------------------------------------------------------
# 服务接入：自选池行情（一次批量取数，缓存 60 秒）
# ---------------------------------------------------------------------------
@st.cache_data(ttl=60, show_spinner=False)
def _quotes(symbols: tuple[str, ...]) -> dict[str, dict]:
    """批量取最近 40 天日线，计算每只标的的最新收盘价与涨跌幅。"""
    if not symbols:
        return {}
    from data.provider import build_provider

    provider = build_provider()
    end = dt.date.today()
    start = end - dt.timedelta(days=40)
    frame = provider.get_bars(list(symbols), start, end, "1d")
    if frame is None or frame.empty:
        return {}
    result: dict[str, dict] = {}
    for symbol, group in frame.groupby("symbol"):
        group = group.sort_values("date")
        last = group.iloc[-1]
        prev_close = float(group.iloc[-2]["close"]) if len(group) > 1 else float(last["close"])
        close = float(last["close"])
        change = close - prev_close
        result[str(symbol)] = {
            "last": close,
            "change": change,
            "change_pct": (change / prev_close * 100.0) if prev_close else 0.0,
            "amount": float(last.get("amount") or 0.0),
            "date": last.get("date"),
        }
    return result


# ---------------------------------------------------------------------------
# 渲染辅助
# ---------------------------------------------------------------------------
def _items_df(items: list[dict], quotes: dict[str, dict]) -> pd.DataFrame:
    """自选池 → 展示/导出用 DataFrame（中文列名，金额与涨跌幅格式化）。"""
    rows = []
    for item in items:
        quote = quotes.get(item["symbol"], {})
        rows.append(
            {
                "代码": item["symbol"],
                "名称": item.get("name") or "",
                "分组": item.get("group") or "",
                "最新价": C.money(quote.get("last")),
                "涨跌幅": C.pct(quote.get("change_pct"), ratio=False, signed=True),
                "成交额": quote.get("amount"),
                "备注": item.get("note") or "",
                "加入时间": item.get("added_at") or "",
            }
        )
    return pd.DataFrame(rows)


def _render_table(items: list[dict], quotes: dict[str, dict]) -> None:
    """HTML 表格（涨跌幅按 A 股习惯涨红跌绿；带成交额中文单位）。"""
    head = "".join(
        f"<th>{name}</th>"
        for name in ("代码", "名称", "分组", "最新价", "涨跌幅", "成交额", "备注", "加入时间")
    )
    body_rows: list[str] = []
    for item in items:
        quote = quotes.get(item["symbol"], {})
        amount = quote.get("amount")
        amount_text = C.DASH
        if amount is not None:
            amount_text = f"{amount / 1e8:,.2f} 亿" if amount >= 1e8 else f"{amount / 1e4:,.2f} 万"
        body_rows.append(
            "<tr>"
            f"<td>{item['symbol']}</td>"
            f"<td>{item.get('name') or C.DASH}</td>"
            f"<td>{item.get('group') or C.DASH}</td>"
            f"<td>{C.money(quote.get('last'))}</td>"
            f"<td>{C.colored(C.pct(quote.get('change_pct'), ratio=False, signed=True), quote.get('change_pct'))}</td>"
            f"<td>{amount_text}</td>"
            f"<td>{item.get('note') or C.DASH}</td>"
            f"<td>{item.get('added_at') or C.DASH}</td>"
            "</tr>"
        )
    st.markdown(
        f'<table class="qd-table"><thead><tr>{head}</tr></thead>'
        f"<tbody>{''.join(body_rows)}</tbody></table>",
        unsafe_allow_html=True,
    )


def _reload() -> None:
    """写操作后清缓存并刷新页面（自选池列表 + 行情缓存）。"""
    C.clear_caches()
    st.rerun()


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
def render() -> None:
    C.page_header("自选池", "管理关注标的：增删改、批量导入、分组切换（近期 / 远期）",
                  badge="R4 新增")

    meta = C.app_meta()
    all_items = store.load_items()
    # 首次使用（空库）时用 config.data.universe 初始化，避免页面/删除确认无标的可选
    if not all_items:
        seeded = 0
        for symbol in (meta.get("universe") or []):
            ok, _message = store.add_item(symbol, "", store.DEFAULT_GROUP)
            seeded += 1 if ok else 0
        if seeded:
            all_items = store.load_items()
            st.caption(f"已用配置自选池（config.data.universe）初始化 {seeded} 只标的，可在此增删改。")
    groups_count = {name: sum(1 for item in all_items if item["group"] == name)
                    for name in store.GROUPS}
    db_backend = store.storage_backend() == "db"
    C.status_bar(
        [
            {"label": "自选总数", "value": f"{len(all_items)} 只",
             "tone": "ok" if all_items else "warn"},
            {"label": "近期", "value": f"{groups_count.get('近期', 0)} 只", "tone": "muted"},
            {"label": "远期", "value": f"{groups_count.get('远期', 0)} 只", "tone": "muted"},
            {"label": "存储", "value": "SQLite（data/watchlist.py）" if db_backend else "JSON 兜底",
             "tone": "ok" if db_backend else "warn",
             "help": store.storage_label()},
        ]
    )
    if store.last_error():
        st.caption(f"存储提示：{store.last_error()}")

    # ---- 控件行：分组切换 / 刷新 / 导出 ----
    c1, c2, c3, c4 = st.columns([1.6, 0.9, 0.9, 0.9])
    with c1:
        group_choice = st.radio("分组", GROUP_CHOICES, horizontal=True, key="qd_wl_group")
    with c2:
        if st.button("🔄 刷新行情", use_container_width=True, key="qd_wl_refresh"):
            C.clear_caches()
            st.rerun()
    with c3:
        df_all = _items_df(all_items, {})
        C.export_csv_button(df_all, "watchlist.csv", key="qd_wl_export",
                            hint="自选池为空时不可导出")
    with c4:
        if st.button("🎯 去推荐页", use_container_width=True, key="qd_wl_go_recommend"):
            C.goto_page("今日推荐")

    rows = all_items if group_choice == "全部" else [
        item for item in all_items if item["group"] == group_choice
    ]

    # ---- 行情列（失败不影响增删改）----
    quotes: dict[str, dict] = {}
    if rows:
        try:
            with st.spinner("读取自选池行情…"):
                quotes = _quotes(tuple(item["symbol"] for item in rows))
        except Exception as exc:
            C.render_warning_card(
                "行情读取失败（不影响自选池管理）",
                f"最新价/涨跌幅暂时不可用：{type(exc).__name__}: {exc}",
                suggestion="点击「刷新行情」重试；若数据源不可用，增删改与导出仍可正常使用。",
            )

    # ---- 列表 ----
    if not rows:
        C.empty_state(
            "自选池为空（当前分组）",
            "在下方「添加标的」逐个添加，或在「批量导入」里粘贴多行代码（每行一个）；"
            "自选池会作为数据拉取、策略与自动化的默认标的集合。",
            page="行情",
            button_label="先去行情页看看",
        )
    else:
        st.markdown(f"##### 列表（{len(rows)} 只）")
        _render_table(rows, quotes)
        st.caption("行情为最近 40 天日线的最新收盘口径（缓存 60 秒）；"
                   "涨红跌绿符合 A 股习惯。点击「刷新行情」可强制重取。")

    st.divider()

    # ---- 添加 / 批量导入 ----
    left, right = st.columns([1, 1])
    with left:
        with st.container(border=True):
            st.markdown("##### ➕ 添加标的")
            a1, a2 = st.columns([1, 1])
            with a1:
                new_symbol = st.text_input("代码", placeholder="600519 或 600519.SH",
                                           key="qd_wl_new_symbol")
                new_name = st.text_input("名称（可选）", placeholder="贵州茅台",
                                         key="qd_wl_new_name")
            with a2:
                new_group = st.selectbox("分组", list(store.GROUPS), key="qd_wl_new_group")
                new_note = st.text_input("备注（可选）", placeholder="如：底仓 / 观察",
                                         key="qd_wl_new_note")
            if st.button("添加", type="primary", use_container_width=True, key="qd_wl_add"):
                ok, message = store.add_item(new_symbol, new_name, new_group, new_note)
                if ok:
                    st.success(message)
                    _reload()
                else:
                    C.render_error_card(
                        "添加失败",
                        message=message,
                        suggestion="输入 6 位代码（可带 .SH/.SZ/.BJ 后缀）；重复代码不会重复添加。",
                        trace_id=C.new_trace_id(),
                    )

    with right:
        with st.container(border=True):
            st.markdown("##### 📋 批量导入")
            bulk_text = st.text_area(
                "每行一个代码（可带名称）", height=150, key="qd_wl_bulk_text",
                placeholder="600519 贵州茅台\n000001.SZ 平安银行\n300750\n# 以 # 开头为注释行",
                help="支持 600519 / 600519.SH / SH600519 / 600519 贵州茅台 等写法；重复代码自动跳过。",
            )
            b1, b2 = st.columns([1, 1])
            with b1:
                bulk_group = st.selectbox("导入到分组", list(store.GROUPS),
                                          key="qd_wl_bulk_group")
            with b2:
                if st.button("导入", type="primary", use_container_width=True, key="qd_wl_bulk_add"):
                    entries, errors = store.parse_bulk_text(bulk_text)
                    if errors:
                        for message in errors:
                            C.render_warning_card("部分行无法导入", message)
                    if entries:
                        report = store.add_many(entries, bulk_group)
                        if report["added"]:
                            st.success(f"批量导入完成：新增 {report['added']} 只，"
                                       f"跳过 {report['skipped']} 只")
                            _reload()
                        else:
                            st.info("没有新增标的（全部已存在或无法识别）。")
                    elif not errors:
                        st.info("没有可导入的内容：请每行填写一个代码。")

    # ---- 编辑 / 删除 ----
    left2, right2 = st.columns([1, 1])
    with left2:
        with st.container(border=True):
            st.markdown("##### ✏️ 编辑")
            if not all_items:
                st.caption("自选池为空，暂无可编辑项。")
            else:
                options = [f"{item['symbol']} {item.get('name') or ''}".strip() for item in all_items]
                selected = st.selectbox("选择标的", options, key="qd_wl_edit_select")
                current = all_items[options.index(selected)]
                e1, e2 = st.columns([1, 1])
                with e1:
                    edit_name = st.text_input("名称", value=current.get("name") or "",
                                              key="qd_wl_edit_name")
                    edit_note = st.text_input("备注", value=current.get("note") or "",
                                              key="qd_wl_edit_note")
                with e2:
                    edit_group = st.selectbox(
                        "分组", list(store.GROUPS),
                        index=list(store.GROUPS).index(current.get("group") or store.DEFAULT_GROUP)
                        if (current.get("group") or store.DEFAULT_GROUP) in store.GROUPS else 0,
                        key="qd_wl_edit_group",
                    )
                    st.caption(f"加入时间：{current.get('added_at') or C.DASH}")
                if st.button("保存修改", use_container_width=True, key="qd_wl_edit_save"):
                    changed = store.update_item(
                        current["symbol"], name=edit_name, group=edit_group, note=edit_note
                    )
                    if changed:
                        st.success(f"已保存：{current['symbol']}")
                        _reload()
                    else:
                        C.render_error_card(
                            "保存失败",
                            message="未找到该标的（可能已被其它操作删除）。",
                            suggestion="刷新页面后重试。",
                            trace_id=C.new_trace_id(),
                        )

    with right2:
        with st.container(border=True):
            st.markdown("##### 🗑️ 删除")
            delete_options = [item["symbol"] for item in all_items] or [
                s for s in (C.app_meta().get("universe") or []) if s
            ]
            if not all_items:
                st.caption("自选池为空：可直接添加；下方候选来自 config.data.universe（删除不存在的标的会提示）。")
            if True:
                delete_targets = st.multiselect("选择要删除的标的", delete_options,
                                                key="qd_wl_delete_select")
                if C.confirm_action(
                    "watchlist_delete",
                    f"将从自选池删除 {len(delete_targets)} 只标的（不删除行情数据、不影响持仓与策略实例）。",
                    button_label="确认删除",
                    danger=True,
                    details=["删除后自动化与策略默认标的不再包含它们。", "可重新添加恢复。"],
                    disabled=not delete_targets,
                ):
                    removed = store.remove_symbols(delete_targets)
                    st.success(f"已删除 {removed} 只标的")
                    _reload()

            # 清空分组（破坏性操作：二次确认 + 输入确认词「清空」）
            st.markdown("---")
            st.markdown("**清空分组**")
            if group_choice not in store.GROUPS:
                st.caption("请先把上方「分组」切到「近期」或「远期」，再执行清空。")
            else:
                group_items = [item["symbol"] for item in all_items if item["group"] == group_choice]
                if C.confirm_action(
                    "watchlist_clear_group",
                    f"将清空「{group_choice}」分组下的全部 {len(group_items)} 只自选标的"
                    "（不删除行情数据、不影响持仓与策略实例）。",
                    button_label="确认清空分组",
                    confirm_word="清空",
                    danger=True,
                    details=[f"仅影响「{group_choice}」分组；另一个分组保持不变。", "可重新添加恢复。"],
                    disabled=not group_items,
                ):
                    removed = store.remove_symbols(group_items)
                    st.success(f"已清空「{group_choice}」分组：删除 {removed} 只标的")
                    _reload()

    # ---- 底部说明 ----
    if store.storage_backend() == "db":
        st.caption("自选池存储在 SQLite 表 watchlist_items（data/watchlist.py），"
                   "与数据拉取、策略与自动化共用同一份自选池数据；行情页「加入自选」也会写入这里。")
    else:
        st.caption("自选池使用 JSON 兜底（runtime/watchlist.json）：data/watchlist.py 不可用时"
                   "自动降级，服务恢复后下次写入回到 SQLite，页面接口不变。")

    if not all_items:
        st.info("提示：自选池为空时，数据拉取 / 自动化 / 回测默认标的都会为空，建议先添加 3–10 只。")

    C.todo_expander(TODO_ITEMS)

"""QuantDesk Streamlit 入口（M0-07 / 决策 D-9：页面动态发现）。

启动方式（项目根目录下）::

    .venv\\Scripts\\python.exe -m streamlit run app.py
    # 或双击 scripts\\start.bat

页面注册约定（D-9）：``ui/pages/*.py`` 每个文件即一个页面，必须暴露

    PAGE_TITLE: str       # 侧边栏中文导航文案（必填，也用于同标题去重）
    render() -> None      # 页面渲染入口（必填）
    PAGE_ICON: str        # 可选，导航图标
    PAGE_ORDER: int       # 可选，导航顺序（越小越靠前）

新增/重写页面只改自己的页面文件，**不需要改本文件**；文件导入失败或缺少
``PAGE_TITLE``/``render`` 时自动降级为「建设中 / 加载失败」占位页，入口不白屏。

本文件只负责：全局外壳（侧边栏 + 页头 + 页脚）、页面发现与导航、异常兜底。
不做任何业务逻辑（不查库、不计算、不下单）。
"""

from __future__ import annotations

import importlib
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import streamlit as st

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:            # 允许直接 `streamlit run app.py` 时导入 ui 包
    sys.path.insert(0, str(ROOT))

# set_page_config 必须是脚本中第一个 Streamlit 命令（Streamlit 硬性要求）
st.set_page_config(
    page_title="QuantDesk · 量化交易工作台",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

from ui.pages import _common as C  # noqa: E402  （依赖上面的 sys.path 设置）

PAGES_DIR = ROOT / "ui" / "pages"
SKIP_FILES = {"_common.py", "__init__.py"}
MODE_LABEL = "模拟盘 · 实盘未接入"
COPYRIGHT = "个人非商业研究用途 · 不构成投资建议"

# 文件名 → 中文标题兜底（页面文件读不到/导入失败时用于占位页标题）
FALLBACK_TITLES = {
    "overview": "总览",
    "market": "行情",
    "manual_trade": "手动交易",
    "strategies": "策略",
    "strategy": "策略",
    "backtest": "回测",
    "positions": "持仓",
    "automation": "自动化",
    "settings": "设置",
    "logs": "日志",
    "watchlist": "自选池",
    "recommend": "今日推荐",
    "screener": "全市场筛选",
}

# 冻结的页面顺序（SPEC §10 清单顺序 + R4 新增页），页面内 PAGE_ORDER 优先
DEFAULT_ORDER = {
    "总览": 1,
    "行情": 2,
    "手动交易": 3,
    "今日推荐": 3,
    "全市场筛选": 3,
    "自选池": 3,
    "策略": 4,
    "回测": 5,
    "持仓": 6,
    "自动化": 7,
    "设置": 8,
    "日志": 9,
}


@dataclass
class PageEntry:
    """一个页面模块的注册信息（动态发现的产物）。"""

    title: str
    file: str
    icon: str = "📄"
    order: int = 100
    module: Any = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.icon} {self.title}"


# ---------------------------------------------------------------------------
# 页面动态发现（D-9）
# ---------------------------------------------------------------------------
def _load_page(path: Path) -> PageEntry:
    """导入单个页面模块并校验约定；任何异常都转成 PageEntry.error（不抛出）。"""
    stem = path.stem
    fallback_title = FALLBACK_TITLES.get(stem, stem)
    try:
        module = importlib.import_module(f"ui.pages.{stem}")
    except Exception as exc:  # 语法错误 / 依赖缺失：登记为占位页
        return PageEntry(
            title=fallback_title,
            file=path.name,
            error=f"模块导入失败：{type(exc).__name__}: {exc}",
        )

    title = getattr(module, "PAGE_TITLE", None)
    render = getattr(module, "render", None)
    icon = str(getattr(module, "PAGE_ICON", "📄") or "📄")

    if not isinstance(title, str) or not title.strip():
        return PageEntry(
            title=fallback_title,
            file=path.name,
            icon=icon,
            module=module,
            error="页面缺少 PAGE_TITLE 常量（约定：PAGE_TITLE: str）",
        )
    title = title.strip()
    order = DEFAULT_ORDER.get(title, 100)
    try:
        order = int(getattr(module, "PAGE_ORDER", order))
    except (TypeError, ValueError):
        pass

    if not callable(render):
        return PageEntry(
            title=title,
            file=path.name,
            icon=icon,
            order=order,
            module=module,
            error="页面缺少 render() 函数（约定：def render() -> None）",
        )
    return PageEntry(title=title, file=path.name, icon=icon, order=order, module=module)


def discover_pages() -> tuple[list[PageEntry], list[PageEntry]]:
    """扫描 ``ui/pages/*.py``，按 (PAGE_ORDER, 文件名) 排序，返回 (可渲染页, 异常页)。"""
    entries: list[PageEntry] = []
    for path in sorted(PAGES_DIR.glob("*.py")):
        if path.name in SKIP_FILES or path.name.startswith("_"):
            continue
        entries.append(_load_page(path))
    entries.sort(key=lambda item: (item.order, item.file))

    pages: list[PageEntry] = []
    issues: list[PageEntry] = []
    seen_titles: set[str] = set()
    for entry in entries:
        if entry.error:
            issues.append(entry)          # 异常页单独登记，仍出现在导航末尾
            continue
        if entry.title in seen_titles:    # 同标题去重（如 strategy.py 与 strategies.py 并存）
            continue
        seen_titles.add(entry.title)
        pages.append(entry)
    return pages, issues


# ---------------------------------------------------------------------------
# 启动初始化（M0-L1：日志 + 数据库；进程内只执行一次，失败不阻塞页面）
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def _bootstrap() -> dict[str, Any]:
    """应用启动初始化块：结构化日志（``runtime/logs``）+ 数据库建表/自检。

    - ``st.cache_resource`` 保证同一进程只执行一次（Streamlit 每次 rerun 不重复初始化）；
    - 任一步失败只记录中文错误、**不向上抛异常**：页面继续渲染，侧边栏给出提示；
    - 成功时返回日志目录与数据库自检信息，供侧边栏状态区展示。
    """
    info: dict[str, Any] = {
        "logging_ok": False,
        "logging_error": "",
        "log_dir": "runtime/logs",
        "db_ok": False,
        "db_error": "",
        "db_message": "",
    }
    # 1) 日志：core.logging.setup_logging（幂等；失败退化为仅控制台，不阻断页面）
    try:
        from core.logging import get_log_dir, setup_logging

        setup_logging()
        info["logging_ok"] = True
        try:
            info["log_dir"] = str(get_log_dir())
        except Exception:  # pragma: no cover - 仅展示用
            pass
    except Exception as exc:
        info["logging_error"] = f"{type(exc).__name__}: {exc}"

    # 2) 数据库：core.db.init_db（幂等建表 + 启动自检）
    try:
        from core.db import init_db

        health = init_db()
        info["db_ok"] = bool(getattr(health, "ok", False))
        info["db_message"] = str(getattr(health, "message", "") or "")
        if not info["db_ok"]:
            info["db_error"] = info["db_message"] or "数据库自检未通过"
    except Exception as exc:
        info["db_error"] = f"{type(exc).__name__}: {exc}"

    # 3) 启动记录（只写一次；失败不影响页面）
    if info["logging_ok"]:
        try:
            import logging as _logging

            _logging.getLogger("quantdesk.app").info(
                "QuantDesk 启动完成：日志目录=%s，数据库=%s（%s）",
                info["log_dir"],
                "就绪" if info["db_ok"] else "初始化失败",
                info["db_message"] or info["db_error"] or "",
            )
        except Exception:  # pragma: no cover - 仅记录启动日志
            pass
    return info


# ---------------------------------------------------------------------------
# 侧边栏：应用名 / 账户 / 数据源 / 调度器 / 离线提示 / 导航
# ---------------------------------------------------------------------------
def _apply_pending_nav(pages: list[PageEntry]) -> None:
    """消费 goto_page() 写入的跳转请求（必须在创建导航控件之前调用）。"""
    pending = C.take_pending_nav()
    if not pending:
        return
    labels = {page.title: page.label for page in pages}
    if pending in labels:
        st.session_state["qd_nav"] = labels[pending]


def _apply_demo_mode(enabled: bool) -> None:
    """M5-03 演示数据模式：切换数据源为内置 mock 并重置服务单例（失败不影响页面）。"""
    import os

    try:
        if enabled:
            os.environ["QD_DATA_PROVIDER"] = "mock"
            os.environ["QD_OFFLINE"] = "1"
        else:
            os.environ.pop("QD_DATA_PROVIDER", None)
            os.environ.pop("QD_OFFLINE", None)
        try:
            from core import config as core_config

            core_config.reset_config()
        except Exception:
            pass
        try:
            import app_services

            app_services.reset_services()
        except Exception:
            pass
    except Exception:
        pass
    st.session_state["qd_demo_mode_applied"] = bool(enabled)
    try:
        st.cache_resource.clear()
        st.cache_data.clear()
    except Exception:
        pass
    st.rerun()


def render_sidebar(pages: list[PageEntry], issues: list[PageEntry],
                   meta: dict[str, Any], boot: dict[str, Any]) -> PageEntry:
    """渲染侧边栏并返回当前选中的页面。"""
    with st.sidebar:
        st.markdown(f'<div class="qd-brand">📈 {meta["name"]}</div>', unsafe_allow_html=True)
        st.caption(f"v{meta['version']} · {MODE_LABEL}")
        st.divider()

        # 账户选择（M2 接入多账户后改为 PortfolioService 提供；当前仅占位）
        st.selectbox(
            "交易账户",
            options=[C.account_label(meta)],
            index=0,
            key="qd_account",
            help="M2 接入 PortfolioService 后支持多账户切换（当前为占位选项）。",
        )

        # M5-03 演示数据模式（切换 mock provider + 内置示例数据；明确标注非真实行情）
        demo_on = bool(st.session_state.get("qd_demo_mode", False))
        demo_toggle = st.toggle(
            "🎬 演示数据模式（mock）", value=demo_on, key="qd_demo_mode",
            help="开启后使用内置 mock 示例数据（2021–2026，无需网络），方便新用户看到完整布局；"
                 "页面会明确标注「演示数据，非真实行情」。",
        )
        if demo_toggle != bool(st.session_state.get("qd_demo_mode_applied", False)):
            _apply_demo_mode(bool(demo_toggle))
        if demo_toggle:
            st.caption("演示数据，非真实行情（内置 mock 示例数据）")

        # 数据源 / 调度器 / 数据库 / 日志 状态灯（M0-L1：启动初始化结果）
        if meta.get("offline"):
            source_tone, source_value = "warn", "离线/演示数据"
        elif meta.get("config_ok"):
            source_tone, source_value = "warn", f"{meta.get('provider')}（已配置）"
        else:
            source_tone, source_value = "error", "配置未就绪"
        logging_ok = bool(boot.get("logging_ok"))
        db_ok = bool(boot.get("db_ok"))
        C.status_bar(
            [
                {"label": "数据源", "value": source_value, "tone": source_tone,
                 "help": "各页面接入 DataProvider.health_check() 后显示实时状态"},
                {"label": "调度器", "value": "已停止", "tone": "muted",
                 "help": "待接入 SchedulerService.is_running()（M3-01）"},
                {"label": "数据库", "value": "就绪" if db_ok else "初始化失败",
                 "tone": "ok" if db_ok else "error",
                 "help": str(boot.get("db_message") or boot.get("db_error") or "SQLite 建表/自检")},
                {"label": "日志", "value": "已启用" if logging_ok else "初始化失败",
                 "tone": "ok" if logging_ok else "error",
                 "help": f"日志目录：{boot.get('log_dir')}" if logging_ok
                         else str(boot.get("logging_error") or "日志初始化失败")},
            ]
        )
        # 初始化失败：中文提示但**不阻塞**页面（页面照常可浏览）
        if not (logging_ok and db_ok):
            problems: list[str] = []
            if not logging_ok:
                problems.append(f"日志初始化失败：{boot.get('logging_error') or '未知原因'}")
            if not db_ok:
                problems.append(f"数据库初始化失败：{boot.get('db_error') or '未知原因'}")
            st.warning("；".join(problems) + "（页面仍可浏览，相关功能可能不可用）", icon="⚠️")

        if meta.get("offline"):
            st.warning("离线模式：部分功能只读，数据可能为演示数据。", icon="📴")

        st.divider()
        st.markdown("**功能导航**")
        labels = [page.label for page in pages] + [issue.label for issue in issues]
        if not labels:  # 理论上不会发生：ui/pages 为空时也给出可读提示
            st.error("未发现任何页面：请检查 ui/pages/*.py 是否存在。")
            st.stop()
        selected_label = st.radio(
            "功能导航",
            options=labels,
            key="qd_nav",
            label_visibility="collapsed",
        )
        st.divider()

        if st.button("🔄 清缓存并刷新", use_container_width=True, key="qd_refresh_all"):
            C.clear_caches()
            st.rerun()

        if issues:
            with st.expander(f"⚠️ 页面自检：{len(issues)} 个异常", expanded=False):
                for issue in issues:
                    st.caption(f"{issue.file}：{issue.error}")

        st.caption(f"模式：{MODE_LABEL}")
        st.caption(COPYRIGHT)

    # 选中项 → PageEntry
    for page in pages:
        if page.label == selected_label:
            return page
    for issue in issues:
        if issue.label == selected_label:
            return issue
    return pages[0]


# ---------------------------------------------------------------------------
# 页面渲染（异常兜底：中文错误卡片 + trace_id，页面不白屏）
# ---------------------------------------------------------------------------
def render_current_page(page: PageEntry) -> None:
    """渲染当前页面；页面内异常被捕获并转成中文错误卡片。"""
    if page.error or page.module is None or not callable(getattr(page.module, "render", None)):
        C.page_header(page.title, "页面尚未就绪（M0 骨架占位）")
        C.render_error_card(
            "页面加载失败",
            message=page.error or "页面模块不可用",
            suggestion="检查该页面文件是否存在语法错误、是否暴露 PAGE_TITLE 与 render()；"
                       "其它页面不受影响，可先切换到可用页面。",
            trace_id=None,
        )
        C.not_ready(
            f"{page.title}页建设中",
            "该页面文件已登记但尚未实现，稍后由对应 M 阶段任务补齐；本页不会影响其它功能。",
            todo=f"ui/pages/{page.file}",
        )
        return

    try:
        page.module.render()
    except Exception:
        trace_id = C.new_trace_id()
        C.page_header(page.title, "页面渲染异常（已捕获）")
        C.render_error_card(
            "页面渲染出错",
            message="页面执行时发生异常，已被入口捕获，其它页面不受影响。",
            suggestion="点击侧边栏「清缓存并刷新」重试；若持续出现，请把 trace_id 与技术细节提供给维护者。",
            trace_id=trace_id,
            exc=traceback.format_exc(),
        )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    boot = _bootstrap()          # M0-L1：日志 + 数据库初始化（幂等、失败不阻塞）
    C.inject_global_css()
    meta = C.app_meta()
    pages, issues = discover_pages()
    _apply_pending_nav(pages)
    selected = render_sidebar(pages, issues, meta, boot)

    # 全局离线黄条（SPEC §10 全局元素 / §8.7 异常与降级）
    if meta.get("offline"):
        C.render_warning_card(
            "离线模式 / 演示数据源",
            "当前无法确认行情数据源连通性，页面可能展示演示数据；下单与自动化默认关闭。",
            suggestion="检查网络与 config.yaml 的 data.provider；恢复后点击侧边栏「清缓存并刷新」。",
        )
    # M5-03 演示模式显著标注（避免与真实行情混淆）
    if st.session_state.get("qd_demo_mode"):
        C.render_warning_card(
            "🎬 演示数据模式已开启",
            "当前使用内置 mock 示例数据（覆盖 2021-01-04 ~ 2026-12-31，确定性生成），"
            "**不是真实行情**，仅用于演示页面布局、回测与下单流程。",
            suggestion="需要真实数据时，关闭侧边栏「演示数据模式」并到行情页点击「拉取/刷新数据」。",
        )

    render_current_page(selected)
    C.footer(meta)


main()

"""Streamlit 逐页深度 e2e 测试（M0-L3 / AC-F7-01/03/04 + 「赚钱导向」新增页面）。

覆盖：
1. 应用启动 + 导航项齐全（当前 9 页必须齐全、顺序符合 SPEC §10）；
2. **动态发现**：逐个已注册页面切换，``at.run()`` 无异常（``len(at.exception) == 0``）且主标题出现
   —— 新增页面（自选池管理 / 今日推荐 / 异动雷达）落地后本用例自动覆盖，无需改测试；
3. 计划内新页面存在时逐个深跑（未落地则 skip 并在报告里注明）；
4. 交互反馈：日志页刷新按钮、手动交易二次确认链路（未确认禁用 → 勾选后可点 → 点击有中文反馈）；
5. 错误卡：手动交易页「RK007 样式自检」开关 → 中文错误卡（原因码 + 建议）；
6. 降级：``QD_OFFLINE=1`` 全局离线提示；空数据库时持仓/总览有空态引导（不白屏）；
7. 配置降级：``QD_CONFIG_FILE`` 指向不存在文件 → 启动成功 + 设置页中文错误卡。

隔离：每用例独立 ``tmp_path``（``QD_APP_DATA_DIR`` + ``QD_DATABASE_URL``），数据源固定
``mock``、``QD_OFFLINE=1``；不联网、不触碰项目 ``runtime/`` 库。

运行：
    .venv\\Scripts\\python.exe -m pytest tests\\test_ui_pages.py -q
    .venv\\Scripts\\python.exe -m pytest tests -q -m e2e
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

APP_DIR = Path(__file__).resolve().parents[1]
APP_FILE = APP_DIR / "app.py"

#: SPEC §10 冻结的 9 个页面（顺序即导航顺序）
REQUIRED_TITLES: tuple[str, ...] = (
    "总览",
    "行情",
    "手动交易",
    "策略",
    "回测",
    "持仓",
    "自动化",
    "设置",
    "日志",
)

#: 计划中的新页面（落地后自动纳入逐页渲染断言；当前已落地 自选池 / 今日推荐）
PLANNED_TITLES: tuple[str, ...] = ("自选池", "今日推荐", "全市场筛选", "异动雷达")

pytestmark = [pytest.mark.e2e, pytest.mark.slow]


# ---------------------------------------------------------------------------
# 环境与 AppTest 辅助
# ---------------------------------------------------------------------------


def _reset_config_cache() -> None:
    try:
        from core import config as core_config

        core_config.reset_config()
    except Exception:  # pragma: no cover
        pass


@pytest.fixture()
def isolated_app_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离运行环境：mock 数据源 + 离线 + 临时数据目录 + 临时数据库。"""
    _reset_config_cache()
    monkeypatch.delenv("QD_CONFIG_FILE", raising=False)
    monkeypatch.setenv("QD_DATA_PROVIDER", "mock")
    monkeypatch.setenv("QD_OFFLINE", "1")
    runtime_dir = tmp_path / "runtime"
    monkeypatch.setenv("QD_APP_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("QD_DATABASE_URL", f"sqlite:///{(runtime_dir / 'e2e.db').as_posix()}")
    try:
        yield runtime_dir
    finally:
        _reset_config_cache()


def _new_app() -> Any:
    from streamlit.testing.v1 import AppTest

    return AppTest.from_file(str(APP_FILE), default_timeout=90)


def _by_key(collection: Any, key: str) -> Any:
    """按 widget key 取元素（兼容不同 Streamlit 版本的 ElementList）。"""
    for element in collection:
        if getattr(element, "key", None) == key:
            return element
    keys = [getattr(element, "key", None) for element in collection]
    raise AssertionError(f"未找到 key={key!r} 的控件；现有 keys={keys}")


def _nav(at: Any) -> Any:
    return _by_key(at.radio, "qd_nav")


def _labels(at: Any) -> list[str]:
    return [str(option) for option in _nav(at).options]


def _goto(at: Any, title: str) -> None:
    """切换到含指定标题的页面并重跑。"""
    label = next(option for option in _labels(at) if title in option)
    at.session_state["qd_nav"] = label
    at.run()


def _text_blob(at: Any) -> str:
    collections = (
        at.markdown,
        at.title,
        at.header,
        at.subheader,
        at.caption,
        at.text,
        at.error,
        at.warning,
        at.info,
        at.success,
    )
    parts: list[str] = []
    for collection in collections:
        for element in collection:
            value = getattr(element, "value", None)
            if value:
                parts.append(str(value))
    return "\n".join(parts)


def _assert_no_exception(at: Any, where: str) -> None:
    assert len(at.exception) == 0, (
        f"{where} 出现未捕获异常（应被入口兜底或修复）："
        + "\n".join(str(exc.value) for exc in at.exception)
    )


def _successful_app() -> Any:
    at = _new_app()
    at.run()
    _assert_no_exception(at, "应用首屏")
    return at


# ---------------------------------------------------------------------------
# 1. 导航项齐全且顺序正确
# ---------------------------------------------------------------------------


def test_app_starts_with_required_nav_items(isolated_app_env: Path) -> None:
    at = _successful_app()
    labels = _labels(at)

    missing = [title for title in REQUIRED_TITLES if not any(title in label for label in labels)]
    assert not missing, f"导航缺少页面：{missing}；实际导航：{labels}"
    # 导航项唯一（同名页面不能被重复注册）
    assert len(labels) == len(set(labels)), f"导航出现重复项：{labels}"
    # 计划内新页面可以增加导航项，但不应出现未登记的第 12 个页面
    unregistered = [label for label in labels if not any(
        title in label for title in (*REQUIRED_TITLES, *PLANNED_TITLES)
    )]
    assert not unregistered, f"发现未登记页面（请同步 PLANNED_TITLES/SPEC §10）：{unregistered}"

    positions = [next(index for index, label in enumerate(labels) if title in label) for title in REQUIRED_TITLES]
    assert positions == sorted(positions), f"导航顺序与 SPEC §10 不一致：{labels}"


def _title_of(label: str) -> str:
    """导航项 ``🏠 总览`` → 页面标题 ``总览``（去掉图标前缀）。"""
    return label.split(" ", 1)[-1].strip() if " " in label else label.strip()


# ---------------------------------------------------------------------------
# 2. 逐页渲染（动态发现，新增页面自动覆盖）
# ---------------------------------------------------------------------------


def test_every_discovered_page_renders(isolated_app_env: Path) -> None:
    at = _successful_app()
    labels = _labels(at)
    failures: list[str] = []

    for label in labels:
        at.session_state["qd_nav"] = label
        at.run()
        if len(at.exception) > 0:
            failures.append(f"{label}: 异常 {[str(exc.value) for exc in at.exception]}")
            continue
        blob = _text_blob(at)
        # 至少要有页面标题（page_header）或该页面自身的中文内容，避免“空白页”
        title = _title_of(label)
        if title not in blob:
            failures.append(f"{label}: 主标题「{title}」未出现，页面文本片段={blob[:200]!r}")

    assert not failures, "以下页面渲染不合格：\n" + "\n".join(failures)


def test_planned_pages_render_when_landed(isolated_app_env: Path) -> None:
    """新增的 3 个页面：已落地则逐页深跑；未落地则 skip（在报告中注明未覆盖项）。"""
    at = _successful_app()
    labels = _labels(at)
    landed = [title for title in PLANNED_TITLES if any(title in label for label in labels)]
    missing = [title for title in PLANNED_TITLES if title not in landed]
    if not landed:
        pytest.skip(f"计划内新页面尚未落地：{missing}（落地后本用例自动生效）")

    failures: list[str] = []
    for title in landed:
        _goto(at, title)
        if len(at.exception) > 0:
            failures.append(f"{title}: {[str(exc.value) for exc in at.exception]}")
        elif title not in _text_blob(at):
            failures.append(f"{title}: 主标题未出现")
    assert not failures, f"新页面渲染不合格：{failures}；未落地页面={missing}"


# ---------------------------------------------------------------------------
# 3. 交互反馈：按钮 + 二次确认链路（AC-F7-03）
# ---------------------------------------------------------------------------


def test_logs_refresh_button_gives_feedback(isolated_app_env: Path) -> None:
    at = _successful_app()
    _goto(at, "日志")

    button = _by_key(at.button, "qd_log_refresh")
    assert not button.disabled, "日志页「刷新」按钮不应禁用"
    button.click()
    at.run()

    _assert_no_exception(at, "日志页点击刷新")
    assert "日志" in _text_blob(at), "点击刷新后日志页标题消失（疑似整页异常）"


def test_manual_trade_confirm_chain(isolated_app_env: Path) -> None:
    """二次确认：未勾选时提交按钮禁用 → 勾选后可用 → 点击给出中文反馈。"""
    at = _successful_app()
    _goto(at, "手动交易")

    submit = _by_key(at.button, "qd_confirm_manual_submit_btn")
    assert submit.disabled is True, "未确认风险时「提交委托」按钮必须是禁用状态（AC-F7-03）"

    # 勾选「我已了解上述风险」→ 按钮解锁
    at.session_state["qd_confirm_manual_submit_ack"] = True
    at.run()
    submit = _by_key(at.button, "qd_confirm_manual_submit_btn")
    assert submit.disabled is False, "勾选确认后「提交委托」按钮应可用"

    # 点击 → 有中文反馈（当前 M0 骨架提示“委托未提交”，待 M2 接入真实下单）
    submit.click()
    at.run()
    _assert_no_exception(at, "手动交易页确认提交")
    blob = _text_blob(at)
    assert "委托未提交" in blob or "已提交" in blob or "风控" in blob, (
        f"点击提交后没有中文反馈；页面文本片段={blob[:400]!r}"
    )


@pytest.mark.parametrize("page_title", ["手动交易", "自选池"])
def test_destructive_actions_require_explicit_ack(isolated_app_env: Path, page_title: str) -> None:
    """破坏性操作一律二次确认：未勾选风险确认时按钮必须禁用（AC-F7-03，覆盖新页面）。"""
    at = _successful_app()
    _goto(at, page_title)

    confirm_buttons = [
        button
        for button in at.button
        if str(getattr(button, "key", "")).startswith("qd_confirm_")
        and str(getattr(button, "key", "")).endswith("_btn")
    ]
    assert confirm_buttons, f"「{page_title}」页未发现 confirm_action 二次确认控件"

    for button in confirm_buttons:
        assert button.disabled is True, f"「{page_title}」页未确认风险时按钮 {button.key} 竟可点击"

    target = confirm_buttons[0]
    ack_key = str(target.key)[: -len("_btn")] + "_ack"
    at.session_state[ack_key] = True
    at.run()

    refreshed = _by_key(at.button, str(target.key))
    if refreshed.disabled:
        # 部分页面还有前置条件（如自选池「删除」需先选中标的），补选后应解锁
        gate_widgets = [widget for widget in at.multiselect if "delete" in str(getattr(widget, "key", ""))]
        if gate_widgets:
            gate = gate_widgets[0]
            options = list(gate.options)
            if options:
                at.session_state[gate.key] = [str(options[0])]
                at.run()
                refreshed = _by_key(at.button, str(target.key))

    assert refreshed.disabled is False, (
        f"勾选确认（并满足前置条件）后 {target.key} 应可用；"
        "若仍禁用，说明该破坏性操作缺少可完成的确认链路（AC-F7-03）"
    )


def test_error_card_self_check(isolated_app_env: Path) -> None:
    """错误卡（AC-F7-04）：原因码 + 中文原因 + 建议动作，且不抛异常。"""
    at = _successful_app()
    _goto(at, "手动交易")

    at.session_state["qd_mt_demo_error"] = True
    at.run()

    _assert_no_exception(at, "手动交易页错误卡自检")
    blob = _text_blob(at)
    assert "RK007" in blob, f"错误卡未展示原因码 RK007；片段={blob[:400]!r}"
    assert "建议" in blob, "错误卡未给出建议动作"
    assert "单票" in blob, "错误卡未给出中文原因（单票仓位超限）"


# ---------------------------------------------------------------------------
# 4. 降级：离线 / 空数据（不白屏）
# ---------------------------------------------------------------------------


def test_offline_degradation_banner(isolated_app_env: Path) -> None:
    at = _successful_app()
    blob = _text_blob(at)

    assert "离线" in blob, f"QD_OFFLINE=1 时应有全局离线提示；片段={blob[:400]!r}"
    assert "数据" in blob


def test_empty_database_pages_show_guidance(isolated_app_env: Path) -> None:
    """空库（无账户/无持仓/无行情）时页面给中文空态引导，而不是报错或白屏。"""
    at = _successful_app()
    failures: list[str] = []

    for title in ("持仓", "总览"):
        _goto(at, title)
        if len(at.exception) > 0:
            failures.append(f"{title}: 异常 {[str(exc.value) for exc in at.exception]}")
            continue
        blob = _text_blob(at)
        keywords = ("空", "暂无", "尚未", "没有", "未就绪", "引导", "创建")
        if not any(word in blob for word in keywords):
            failures.append(f"{title}: 空数据状态缺少中文引导；片段={blob[:300]!r}")

    assert not failures, "空数据降级不合格：\n" + "\n".join(failures)


# ---------------------------------------------------------------------------
# 5. 配置降级：坏配置也要能启动 + 中文错误卡
# ---------------------------------------------------------------------------


def test_broken_config_degrades_with_chinese_error_card(
    isolated_app_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "definitely_missing_config.yaml"
    assert not missing.exists()
    monkeypatch.setenv("QD_CONFIG_FILE", str(missing))
    _reset_config_cache()

    at = _successful_app()

    labels = _labels(at)
    assert len(labels) >= len(REQUIRED_TITLES), f"配置缺失不应导致导航丢失：{labels}"

    _goto(at, "设置")
    _assert_no_exception(at, "配置缺失时的设置页")
    blob = _text_blob(at)
    assert "配置加载失败" in blob, f"缺少中文错误卡标题；片段={blob[:800]!r}"
    assert "已降级" in blob or "默认值" in blob
    assert "配置文件不存在" in blob
    assert "config.yaml" in blob

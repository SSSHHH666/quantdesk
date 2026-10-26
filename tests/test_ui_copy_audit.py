"""UI 文案审计（AppTest 渲染 11 页，防"无原因/无建议/无降级"的裸错误文案）。

背景：用户反馈 UI 多处「无法获取」「服务不可用」，看起来像故障但没有原因与下一步动作。
修复后的口径（Lead 裁决）：
- **禁止**裸的「无法获取」「服务不可用」——若出现，必须**同一条文案内**给出
  原因 / 操作建议 / 降级说明（如「已降级」「请检查配置」「稍后重试」「尚未…」）；
- **允许**带行动指引的文案，如「还没有数据，请先到行情页拉取」「服务不可用，已降级：…」。

另外审计：推荐/自选池/持仓页不得出现字符串 ``None``（后端空值被直接渲染成假名称）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_FILE = PROJECT_ROOT / "app.py"

#: 禁止裸出现的故障措辞
FORBIDDEN_BARE_PHRASES = ("无法获取", "服务不可用")
#: 允许和禁止措辞同框出现时的"原因/建议/降级"关键词
GUIDANCE_WORDS = ("建议", "请", "降级", "检查", "重试", "配置", "原因", "稍后", "尚未", "暂无", "待")

#: 不允许出现 ``None`` 的页面（后端空值易被直接渲染）
NONE_SENSITIVE_TITLES = ("今日推荐", "自选池", "持仓")

pytestmark = [pytest.mark.e2e, pytest.mark.slow]


def _reset_config_cache() -> None:
    try:
        from core import config as core_config

        core_config.reset_config()
    except Exception:  # pragma: no cover
        pass


@pytest.fixture()
def isolated_app_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _reset_config_cache()
    monkeypatch.delenv("QD_CONFIG_FILE", raising=False)
    monkeypatch.setenv("QD_DATA_PROVIDER", "mock")
    monkeypatch.setenv("QD_OFFLINE", "1")
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("QD_APP_DATA_DIR", str(runtime))
    monkeypatch.setenv("QD_DATABASE_URL", f"sqlite:///{(runtime / 'copy.db').as_posix()}")
    try:
        yield runtime
    finally:
        _reset_config_cache()


def _new_app() -> Any:
    from streamlit.testing.v1 import AppTest

    return AppTest.from_file(str(APP_FILE), default_timeout=120)


def _by_key(collection: Any, key: str) -> Any:
    for element in collection:
        if getattr(element, "key", None) == key:
            return element
    raise AssertionError(f"未找到 key={key!r}；现有：{[getattr(e, 'key', None) for e in collection]}")


def _labels(at: Any) -> list[str]:
    return [str(option) for option in _by_key(at.radio, "qd_nav").options]


def _texts(at: Any) -> list[str]:
    """收集页面所有可见文本（含 markdown/title/error/info 等），用于逐条文案审计。"""
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
        at.code,
    )
    texts: list[str] = []
    for collection in collections:
        for element in collection:
            value = getattr(element, "value", None)
            if value:
                texts.append(str(value))
    return texts


def _goto(at: Any, title: str) -> None:
    label = next(option for option in _labels(at) if title in option)
    at.session_state["qd_nav"] = label
    at.run()


def test_eleven_pages_have_no_bare_failure_copy(isolated_app_env: Path) -> None:
    """逐页渲染 11 页：禁止裸「无法获取 / 服务不可用」，必须带原因/建议/降级。"""
    at = _new_app()
    at.run()
    labels = _labels(at)
    assert len(labels) >= 11, f"页面数应 ≥ 11（M5），实际 {len(labels)}：{labels}"

    violations: list[str] = []
    for label in labels:
        at.session_state["qd_nav"] = label
        at.run()
        if len(at.exception) > 0:
            violations.append(f"{label}: 未捕获异常 {[str(e.value) for e in at.exception]}")
            continue
        for text in _texts(at):
            for phrase in FORBIDDEN_BARE_PHRASES:
                if phrase not in text:
                    continue
                if not any(word in text for word in GUIDANCE_WORDS):
                    violations.append(f"{label}: 裸文案「{phrase}」：{text[:160]!r}")

    assert not violations, "发现无原因/无建议/无降级的裸错误文案：\n" + "\n".join(violations)


def test_recommend_related_pages_render_no_literal_none(isolated_app_env: Path) -> None:
    """推荐/自选池/持仓页不得把后端空值渲染成字符串 ``None``。"""
    at = _new_app()
    at.run()

    violations: list[str] = []
    none_pattern = re.compile(r"(?<![\w.-])None(?![\w.-])")
    for title in NONE_SENSITIVE_TITLES:
        _goto(at, title)
        for text in _texts(at):
            if none_pattern.search(text):
                violations.append(f"{title}: 出现裸 None → {text[:160]!r}")

    assert not violations, "UI 渲染出裸 None（应显示中文占位如「—」）：\n" + "\n".join(violations)


def test_empty_states_carry_actionable_guidance(isolated_app_env: Path) -> None:
    """空数据/离线状态下，页面必须给出可执行指引（而不是只有一句报错）。"""
    at = _new_app()
    at.run()

    actionable = ("请", "建议", "可以先", "到「", "去", "重试", "拉取", "刷新")
    missing: list[str] = []
    for title in ("总览", "持仓", "今日推荐"):
        _goto(at, title)
        texts = [text for text in _texts(at) if any("\u4e00" <= ch <= "\u9fff" for ch in text)]
        blob = "\n".join(texts)
        if not any(word in blob for word in actionable):
            missing.append(f"{title}: 缺少可执行指引（片段：{blob[:200]!r}）")

    assert not missing, "空数据降级缺少行动指引：\n" + "\n".join(missing)

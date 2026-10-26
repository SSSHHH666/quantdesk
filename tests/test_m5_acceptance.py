"""M5 验收测试：报告导出（HTML/CSV）、演示模式开关、11 页全渲染。

覆盖 Lead R6 要求：
1. HTML 报告：``ui/pages/backtest.build_report_html(result)`` 生成可解析 HTML，
   内容含**绩效指标**（总收益率/最大回撤/夏普…）与**交易**信息；
2. CSV 导出：权益曲线 / 成交明细 / 指标均可生成并被标准 CSV 解析器读回（内容含绩效与交易）；
3. 演示模式：``qd_demo_mode`` 开关切换后出现「演示数据模式已开启」黄条（非真实行情标注）；
4. 11 页 AppTest 全渲染（含新增 自选池 / 今日推荐），无异常且标题出现。

说明：AppTest 在部分 Streamlit 版本没有 ``at.toggle`` 访问器，因此演示模式用
**session_state + 页面文案**断言（等价于“开关存在且切换后出现演示黄条”），不依赖控件 API。
"""

from __future__ import annotations

import csv
import datetime as dt
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from backtest.engine import BacktestEngine
from data.provider import MockProvider

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_FILE = PROJECT_ROOT / "app.py"

REQUIRED_TITLES = ("总览", "行情", "手动交易", "策略", "回测", "持仓", "自动化", "设置", "日志", "自选池", "今日推荐")

pytestmark = [pytest.mark.e2e, pytest.mark.slow]


# ---------------------------------------------------------------------------
# 回测结果（导出的数据源）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def backtest_result() -> Any:
    engine = BacktestEngine(data_source=MockProvider())
    return engine.run(
        "ma_cross",
        {"fast": 5, "slow": 20},
        symbols=["600519.SH", "000001.SZ"],
        start=dt.date(2023, 1, 3),
        end=dt.date(2024, 6, 28),
        initial_cash=100000.0,
        benchmark=None,
    )


class _SilentHTMLParser(HTMLParser):
    """仅用于验证 HTML 可被标准解析器消费（不抛异常即通过）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)


# ---------------------------------------------------------------------------
# 一、HTML 报告导出
# ---------------------------------------------------------------------------


def test_html_report_generation(backtest_result) -> None:
    from ui.pages.backtest import build_report_html

    html = build_report_html(backtest_result, "双均线（验收）")

    assert isinstance(html, str) and len(html) > 1000, "HTML 报告过短，疑似空壳"
    assert html.lstrip().lower().startswith("<!doctype html") or "<html" in html.lower()
    assert "</html>" in html.lower()

    # 绩效内容
    for keyword in ("总收益率", "最大回撤", "夏普比率"):
        assert keyword in html, f"HTML 报告缺少绩效项「{keyword}」"
    # 交易内容
    assert any(word in html for word in ("交易次数", "成交", "胜率")), "HTML 报告缺少交易信息"
    # 策略/标的信息
    assert "双均线（验收）" in html

    parser = _SilentHTMLParser()
    parser.feed(html)
    assert len(parser.tags) > 10, "HTML 结构异常（标签过少）"


# ---------------------------------------------------------------------------
# 二、CSV 导出：生成 + 标准解析器可读 + 内容含绩效与交易
# ---------------------------------------------------------------------------


def test_csv_exports_are_parseable(backtest_result, tmp_path: Path) -> None:
    equity_path = tmp_path / "equity.csv"
    trades_path = tmp_path / "trades.csv"
    metrics_path = tmp_path / "metrics.csv"

    backtest_result.equity.to_csv(equity_path, index=False, encoding="utf-8-sig")
    backtest_result.trades.to_csv(trades_path, index=False, encoding="utf-8-sig")
    pd.DataFrame([backtest_result.metrics]).to_csv(metrics_path, index=False, encoding="utf-8-sig")

    with equity_path.open(encoding="utf-8-sig", newline="") as handle:
        equity_rows = list(csv.DictReader(handle))
    assert len(equity_rows) == len(backtest_result.equity) > 0
    equity_back = pd.read_csv(equity_path)
    assert {"trade_date", "equity"} <= set(equity_back.columns)
    assert equity_back["equity"].iloc[-1] == pytest.approx(backtest_result.equity["equity"].iloc[-1], abs=1e-6)

    with trades_path.open(encoding="utf-8-sig", newline="") as handle:
        trade_rows = list(csv.DictReader(handle))
    trades_back = pd.read_csv(trades_path)
    assert len(trade_rows) == len(backtest_result.trades)
    if len(backtest_result.trades):
        assert {"symbol", "side", "price", "quantity", "fee_total"} <= set(trades_back.columns)
        assert trades_back["fee_total"].sum() == pytest.approx(backtest_result.trades["fee_total"].sum(), abs=1e-6)

    metrics_back = pd.read_csv(metrics_path)
    for key in ("total_return", "max_drawdown", "sharpe", "win_rate"):
        assert key in metrics_back.columns, f"指标 CSV 缺少 {key}"
    assert float(metrics_back["total_return"].iloc[0]) == pytest.approx(
        float(backtest_result.metrics["total_return"]), abs=1e-9
    )


# ---------------------------------------------------------------------------
# 三、演示模式开关（AppTest：session_state + 黄条文案）
# ---------------------------------------------------------------------------


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
    monkeypatch.setenv("QD_DATABASE_URL", f"sqlite:///{(runtime / 'm5.db').as_posix()}")
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


def _text_blob(at: Any) -> str:
    collections = (at.markdown, at.title, at.caption, at.text, at.warning, at.info, at.error, at.success)
    parts: list[str] = []
    for collection in collections:
        for element in collection:
            value = getattr(element, "value", None)
            if value:
                parts.append(str(value))
    return "\n".join(parts)


def test_demo_mode_toggle(isolated_app_env: Path) -> None:
    """演示模式：默认关闭 → 切到 True 后出现「演示数据模式已开启」黄条 → 切回关闭后消失。"""
    at = _new_app()
    at.run()
    assert len(at.exception) == 0, [str(e.value) for e in at.exception]

    blob_off = _text_blob(at)
    assert "演示数据模式已开启" not in blob_off, "默认不应显示演示模式黄条"

    # 开启（等价于点击侧边栏 toggle；AppTest 版本差异下直接置 session_state）
    at.session_state["qd_demo_mode"] = True
    at.run()
    assert len(at.exception) == 0, [str(e.value) for e in at.exception]
    assert "qd_demo_mode_applied" in at.session_state, "会话中缺少 qd_demo_mode_applied"
    assert at.session_state["qd_demo_mode_applied"] is True, "演示模式未生效（applied 未置 True）"
    blob_on = _text_blob(at)
    assert "演示数据模式已开启" in blob_on, f"开启后缺少演示黄条：{blob_on[:400]!r}"
    assert "非真实" in blob_on or "示例数据" in blob_on or "mock" in blob_on.lower()

    # 关闭 → 黄条消失
    at.session_state["qd_demo_mode"] = False
    at.run()
    assert "qd_demo_mode_applied" in at.session_state
    assert at.session_state["qd_demo_mode_applied"] is False
    assert "演示数据模式已开启" not in _text_blob(at)


def test_eleven_pages_render(isolated_app_env: Path) -> None:
    at = _new_app()
    at.run()
    labels = [str(option) for option in _by_key(at.radio, "qd_nav").options]
    assert len(labels) >= len(REQUIRED_TITLES), f"页面数不足 11：{labels}"
    for title in REQUIRED_TITLES:
        assert any(title in label for label in labels), f"缺少页面：{title}"

    failures: list[str] = []
    for label in labels:
        at.session_state["qd_nav"] = label
        at.run()
        if len(at.exception) > 0:
            failures.append(f"{label}: 异常 {[str(e.value) for e in at.exception]}")
            continue
        title = label.split(" ", 1)[-1].strip()
        if title not in _text_blob(at):
            failures.append(f"{label}: 主标题未出现")
    assert not failures, "11 页渲染不合格：\n" + "\n".join(failures)

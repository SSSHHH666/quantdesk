"""推荐结果契约测试（防 UI 与后端字段错位）。

**契约基线（推荐结果字段 ↔ UI 消费字段）**——由 ``strategy/recommend.Recommendation``
（后端）与 ``ui/pages/recommend.py``（前端）共同遵守，改字段必须先改本注释与用例：

| 后端字段（Recommendation / as_dict） | 含义 | UI 消费位置 | 前端兜底要求 |
|---|---|---|---|
| ``symbol``         | 标的代码（600519.SH） | 推荐表「代码」列 / 加入自选 | 必填，不得为空 |
| ``name``           | 中文名称（**可为空**，但不得是字符串 ``"None"``） | 推荐表「名称」列 | 缺失时显示 ``—``（中文占位） |
| ``score``          | 综合得分（降序排序依据） | 「得分」列 / 排序 | 必填数值 |
| ``reason``         | 中文推荐理由（得分分解文本） | 「推荐理由（得分分解）」列 | 必填非空中文 |
| ``factors``        | 因子分项（z 值等） | 得分分解展示 | 至少 factors/components 之一非空 |
| ``components``     | 加权分量 | 得分分解展示 | 同上 |
| ``ml_proba``       | ML 概率（可空，仅 ml_ranker/混入时） | 间接（得分内） | 缺失显示 ``—`` |
| ``as_of``          | 数据截止日 | 页面说明「截至日期」 | 必填 |
| ``rank``           | 名次（1..n） | 表格排序 | 必填，升序连续 |
| ``qualified``      | 是否满足买入条件 | 视觉标记 | 布尔 |

服务层契约：``app_services.get_recommendations(group, top_n)`` 必须返回
``(ok: bool, rows: list[dict], message: str)``；失败/无数据时**降级**为空列表 + 中文
原因与操作建议（不得抛异常、不得返回裸 "无法获取"）。
"""

from __future__ import annotations

import datetime as dt

import pytest

from data.provider import MockProvider
import strategy.recommend as recommend_module

UNIVERSE = ["600519.SH", "000001.SZ", "300750.SZ"]
START = dt.date(2023, 1, 3)
AS_OF = dt.date(2024, 6, 28)

pytestmark = pytest.mark.unit


def _bars():
    return MockProvider().get_bars(UNIVERSE, START, AS_OF)


def _recommend(strategy_key: str = "value_quality", top_n: int = 3):
    return recommend_module.recommend(strategy_key, UNIVERSE, AS_OF, top_n=top_n, bars=_bars())


# ---------------------------------------------------------------------------
# 一、Recommendation 字段契约
# ---------------------------------------------------------------------------


def test_recommendation_required_fields_present() -> None:
    rows = _recommend()
    assert rows, "MockProvider 数据下应产出推荐"

    payload = rows[0].as_dict()
    for key in ("symbol", "score", "reason", "as_of", "rank", "qualified"):
        assert key in payload, f"推荐结果缺少契约字段 {key!r}（UI 依赖）"

    assert isinstance(payload["score"], float)
    assert payload["reason"].strip(), "reason 不得为空"
    assert any("\u4e00" <= ch <= "\u9fff" for ch in payload["reason"]), "reason 必须是中文"
    assert payload["factors"] or payload["components"], "得分分解（factors/components）不得同时为空"


def test_recommendation_name_is_nullable_but_never_string_none() -> None:
    """``name`` 允许为空（None/""），但**绝不能是字符串 "None"**（UI 会照原样渲染）。"""
    rows = _recommend()
    assert rows
    payload = rows[0].as_dict()

    if "name" not in payload:
        pytest.xfail(
            "推荐契约缺口（P1，已报 lead/Dev2）：Recommendation.as_dict() 尚无 name 字段；"
            "补上后本用例自动生效，届时断言 name 非字符串 \"None\" 且缺失时 UI 落中文占位"
        )

    name = payload["name"]
    assert name is None or isinstance(name, str), f"name 类型应为 str|None，实际 {type(name).__name__}"
    assert name != "None", "name 不能是字符串 \"None\"（UI 会把 None 原样渲染成假名称）"
    if isinstance(name, str):
        assert name.strip() == name or not name.strip() or "None" not in name


def test_recommendation_sorted_and_ranked() -> None:
    rows = _recommend(top_n=3)
    scores = [row.score for row in rows]

    assert scores == sorted(scores, reverse=True), f"推荐未按 score 降序：{scores}"
    assert [row.rank for row in rows] == list(range(1, len(rows) + 1))


def test_recommend_is_deterministic() -> None:
    first = [row.as_dict() for row in _recommend()]
    second = [row.as_dict() for row in _recommend()]

    assert first == second


# ---------------------------------------------------------------------------
# 二、服务层 get_recommendations 各组返回可渲染结构
# ---------------------------------------------------------------------------


@pytest.fixture()
def warm_db(tmp_path, monkeypatch):
    """给服务层准备：临时库 + 长历史单源行情 + 自选池（避免降级分支掩盖字段契约）。"""
    import datetime as dt2
    import os

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from core import config as core_config
    from core.models import Base, DailyBarORM

    tmp = tmp_path / "runtime"
    tmp.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("QD_DATA_PROVIDER", "mock")
    monkeypatch.setenv("QD_OFFLINE", "1")
    monkeypatch.setenv("QD_APP_DATA_DIR", str(tmp))
    monkeypatch.setenv("QD_DATABASE_URL", f"sqlite:///{(tmp / 'rec.db').as_posix()}")
    core_config.reset_config()

    engine = create_engine(f"sqlite:///{(tmp / 'rec.db').as_posix()}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        start = dt2.date(2023, 1, 3)
        for symbol, base in (("600519.SH", 1500.0), ("000001.SZ", 10.0), ("300750.SZ", 180.0)):
            for offset in range(1100):
                price = base * (1 + 0.0004 * offset + 0.02 * ((offset % 7) - 3) / 3)
                session.add(
                    DailyBarORM(
                        symbol=symbol,
                        trade_date=start + dt2.timedelta(days=offset),
                        open=price,
                        high=price * 1.01,
                        low=price * 0.99,
                        close=price,
                        volume=1_000_000,
                        amount=price * 1_000_000,
                        adj_factor=1.0,
                        turnover_rate=1.0,
                        is_suspended=False,
                        source="akshare",
                    )
                )
        session.commit()
    finally:
        session.close()
    engine.dispose()

    # 通过服务层写自选池（会按 config.data.universe 或显式 add 初始化）
    from data.watchlist import get_service

    service = get_service()
    service.ensure_schema()
    for symbol in UNIVERSE:
        try:
            service.add(symbol, group="short_term")
        except Exception:  # noqa: BLE001 - 已存在时忽略
            pass

    try:
        yield
    finally:
        core_config.reset_config()


@pytest.mark.parametrize("group", ["远期精选", "近期异动"])
@pytest.mark.integration
@pytest.mark.slow
def test_get_recommendations_returns_renderable_structure(warm_db, group: str) -> None:
    import app_services

    result = app_services.get_recommendations(group, 3)

    assert isinstance(result, tuple) and len(result) == 3, f"应返回 (ok, rows, message)，实际 {type(result)}"
    ok, rows, message = result
    assert isinstance(ok, bool)
    assert isinstance(rows, list), f"rows 必须是 list（UI 直接迭代），实际 {type(rows).__name__}"
    assert isinstance(message, str) and message.strip(), "必须返回中文说明"
    assert any("\u4e00" <= ch <= "\u9fff" for ch in message)

    if not ok:
        # 降级分支：空列表 + 中文原因/操作建议（不得是裸“无法获取/服务不可用”）
        assert rows == []
        assert any(word in message for word in ("建议", "请", "降级", "尚未", "暂无")), (
            f"降级信息缺少操作建议：{message}"
        )
        assert "无法获取" not in message
        return

    assert rows, "ok=True 时不应返回空列表"
    for row in rows:
        assert isinstance(row, dict), "推荐条目应为 dict（UI 直接渲染）"
        assert row.get("symbol"), "推荐条目缺少 symbol"
        assert row.get("score") is not None, "推荐条目缺少 score"
        assert str(row.get("reason") or "").strip(), "推荐条目缺少 reason"
        name = row.get("name")
        assert name != "None", "name 不得是字符串 \"None\""

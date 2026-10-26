"""自选池数据层测试（``data/watchlist.py`` 公开 API，契约 2026-10-26 新语义）。

数据层已重构为**多分组模型**（与其对齐，不再依赖 UI 内部模块）：
- 分组：``GROUP_NAMES = {default: 全部, long_term: 远期, short_term: 近期, manual: 手动}``；
  ``normalize_group()`` 同时接受分组 id 与中文名，返回 **id**（大小写敏感）；
- 同一 symbol 可同时存在于多个分组（**按 (symbol, group) 唯一**），``count()`` 计条目数；
- ``WatchlistItem.group_name`` 存**中文名**；新增 ``source``（MANUAL/IMPORT）；
- ``name`` 由 symbols/快照解析（未知为空字符串，UI 需中文兜底），add 的 name 参数不落库；
- ``remove(symbol)`` 删除该标的所有分组；``remove(symbol, group=...)`` 只删指定分组；
- 导入/导出 JSON 键：symbol/name/group/group_name/group_label/note/source/added_at/updated_at。

隔离：用例自建临时 SQLite + 直接构造 ``WatchlistService``，不依赖全局单例。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config import load_config
from core.models import Base
from data.watchlist import (
    GROUP_DEFAULT,
    GROUP_LONG_TERM,
    GROUP_NAMES,
    GROUP_SHORT_TERM,
    VALID_GROUPS,
    WatchlistError,
    WatchlistItem,
    WatchlistService,
    normalize_group,
    validate_symbol,
)

pytestmark = pytest.mark.integration

S1, S2, S3 = "601899.SH", "002594.SZ", "600036.SH"


def _label(group_id: str) -> str:
    """分组 id → 中文显示名（契约：group_name 存中文）。"""
    return GROUP_NAMES[group_id][0]


@pytest.fixture()
def service(tmp_path: Path):
    """临时 SQLite 上的 WatchlistService（不依赖任何全局单例）。"""
    engine = create_engine(f"sqlite:///{(tmp_path / 'watchlist_test.db').as_posix()}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    cfg = load_config(environ={})
    svc = WatchlistService(config=cfg, session_factory=factory)
    svc.ensure_schema()
    try:
        yield svc
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 一、代码与分组校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("symbol", ["600519.SH", "600519.sh", " 600519.SH ", "899050.BJ", "000001.SZ"])
def test_validate_symbol_accepts_standard_codes(symbol: str) -> None:
    assert validate_symbol(symbol) == symbol.strip().upper()


@pytest.mark.parametrize("symbol", ["600519", "abc", "", None, "600519.XX", "60051.SH"])
def test_validate_symbol_rejects_invalid_with_chinese_error(symbol: object) -> None:
    with pytest.raises(WatchlistError) as excinfo:
        validate_symbol(symbol)
    assert "格式非法" in str(excinfo.value)


def test_normalize_group_accepts_ids_and_chinese_labels() -> None:
    for group_id in VALID_GROUPS:
        assert normalize_group(group_id) == group_id
        assert normalize_group(_label(group_id)) == group_id
    assert len(VALID_GROUPS) == len(GROUP_NAMES) >= 4


def test_normalize_group_rejects_invalid_and_is_case_sensitive() -> None:
    with pytest.raises(WatchlistError) as excinfo:
        normalize_group("不存在的分组")
    assert "分组" in str(excinfo.value)
    # 契约：大小写敏感（"LONG_TERM" 不是合法分组 id）
    with pytest.raises(WatchlistError):
        normalize_group("LONG_TERM")


# ---------------------------------------------------------------------------
# 二、CRUD：多分组 / 幂等 / 中文 group_name / source
# ---------------------------------------------------------------------------


def test_add_creates_item_with_chinese_group_and_source(service: WatchlistService) -> None:
    item = service.add(S1, group=GROUP_SHORT_TERM, name="紫金矿业")

    assert isinstance(item, WatchlistItem)
    assert item.symbol == S1
    assert item.group_name == _label(GROUP_SHORT_TERM) == "近期"
    assert item.source == "MANUAL"
    assert isinstance(item.name, str)  # 名称由 symbols/快照解析，未知为空串
    assert service.count() == 1


def test_same_symbol_can_live_in_multiple_groups(service: WatchlistService) -> None:
    service.add(S1, group=GROUP_SHORT_TERM)
    service.add(S1, group=GROUP_LONG_TERM)

    assert service.count() == 2, "多分组模型：同一标的可同时存在于两个分组"
    assert service.get(S1, GROUP_SHORT_TERM) is not None
    assert service.get(S1, GROUP_LONG_TERM) is not None
    assert [i.group_name for i in service.list_items(GROUP_LONG_TERM)] == ["远期"]


def test_add_is_upsert_per_group(service: WatchlistService) -> None:
    first = service.add(S1, group=GROUP_SHORT_TERM)
    again = service.add(S1, group=GROUP_SHORT_TERM, note="备注更新")

    assert service.count() == 1, "同一 (symbol, group) 重复 add 不应新增条目"
    assert again.note == "备注更新"
    assert again.added_at == first.added_at
    assert again.updated_at is not None


def test_list_items_filters_by_id_or_label(service: WatchlistService) -> None:
    service.add(S1, group=GROUP_SHORT_TERM)
    service.add(S2, group=GROUP_LONG_TERM)

    assert [i.symbol for i in service.list_items(GROUP_SHORT_TERM)] == [S1]
    assert [i.symbol for i in service.list_items("远期")] == [S2]
    assert len(service.list_items()) == 2


def test_set_group_moves_item_between_groups(service: WatchlistService) -> None:
    service.add(S1, group=GROUP_SHORT_TERM)

    assert service.set_group(S1, GROUP_LONG_TERM) is True
    assert service.get(S1, GROUP_LONG_TERM) is not None
    assert service.get(S1, GROUP_SHORT_TERM) is None
    assert service.count() == 1
    assert service.set_group("000001.SZ", GROUP_LONG_TERM) is False


def test_remove_single_group_and_all_groups(service: WatchlistService) -> None:
    service.add(S1, group=GROUP_SHORT_TERM)
    service.add(S1, group=GROUP_LONG_TERM)
    service.add(S2, group=GROUP_SHORT_TERM)

    assert service.remove(S1, group=GROUP_LONG_TERM) is True
    assert service.count() == 2
    assert service.get(S1, GROUP_LONG_TERM) is None

    assert service.remove(S1) is True
    assert service.get(S1, GROUP_SHORT_TERM) is None
    assert service.count() == 1
    assert service.remove(S1) is False


# ---------------------------------------------------------------------------
# 三、批量导入：去重 / 错误清单 / ST 提示 / source
# ---------------------------------------------------------------------------


def test_add_many_counts_errors_and_source(service: WatchlistService) -> None:
    report = service.add_many(
        [{"symbol": S1}, {"symbol": S1}, {"symbol": "abc"}, S2],
        group=GROUP_SHORT_TERM,
    )

    assert report["added"] == 2, report
    assert report["updated"] == 1, report
    assert len(report["errors"]) == 1
    assert "格式非法" in report["errors"][0]["error"]
    assert service.count() == 2
    assert {i.symbol for i in service.list_items()} == {S1, S2}
    assert {i.source for i in service.list_items()} == {"IMPORT"}


def test_add_many_flags_st_without_blocking(service: WatchlistService) -> None:
    report = service.add_many([{"symbol": S1, "name": "*ST紫金"}, {"symbol": S2}], group=GROUP_SHORT_TERM)

    assert report["st_warnings"] == [S1], "ST 只做风险提示，不阻断入库"
    assert service.get(S1) is not None


def test_add_many_replace_clears_group_first(service: WatchlistService) -> None:
    service.add(S3, group=GROUP_SHORT_TERM)

    report = service.add_many([{"symbol": S1}], group=GROUP_SHORT_TERM, replace=True)

    assert report["added"] == 1
    assert [i.symbol for i in service.list_items(GROUP_SHORT_TERM)] == [S1]


# ---------------------------------------------------------------------------
# 四、JSON 导入 / 导出（新键集合）
# ---------------------------------------------------------------------------


def test_import_json_and_export_roundtrip(service: WatchlistService, tmp_path: Path) -> None:
    report = service.import_json(
        [
            {"symbol": S1, "name": "紫金矿业", "group": GROUP_LONG_TERM, "note": "核心"},
            {"symbol": S2, "group": GROUP_SHORT_TERM},
        ],
        group=GROUP_DEFAULT,
    )
    assert report["total"] == 2 and report["added"] == 2

    target = tmp_path / "export.json"
    exported = service.export_json(target)
    payload = json.loads(target.read_text(encoding="utf-8"))

    assert payload == exported
    assert {row["symbol"] for row in payload} == {S1, S2}
    keys = set(payload[0].keys())
    assert {"symbol", "name", "group", "group_name", "note", "source"} <= keys
    row = next(row for row in payload if row["symbol"] == S1)
    assert normalize_group(row["group"]) == GROUP_LONG_TERM
    assert row["note"] == "核心"


def test_import_json_missing_or_broken_file_raises(service: WatchlistService, tmp_path: Path) -> None:
    with pytest.raises(WatchlistError) as excinfo:
        service.import_json(tmp_path / "not_exists.json")
    assert "不存在" in str(excinfo.value)

    broken = tmp_path / "broken.json"
    broken.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(WatchlistError) as excinfo:
        service.import_json(broken)
    assert "解析失败" in str(excinfo.value)

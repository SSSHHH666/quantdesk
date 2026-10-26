"""自选池 UI 适配层（下划线开头，不被 app.py 发现）。

**唯一数据来源：`data/watchlist.py`（经 `app_services` 门面）**
—— 不再维护 JSON 副本，避免自选池重复实现（Lead 决策）。

    get_watchlist(group) / add_watchlist(...) / remove_watchlist(symbol)

- 门面内部使用 `core.db` 会话（等价 `session_scope`）操作独立表 ``watchlist_items``，
  UI 不直接建连接、不写 SQL；表不在 22 张核心业务表内（CF-7 不变）。
- 任何失败（数据库不可用等）由门面返回中文 message：本适配层记录到 `last_error()`，
  页面转为「只读展示 + 中文提示」，不会白屏。
- 分组映射：UI「近期」↔ ``short_term``、UI「远期」↔ ``long_term``；服务层遗留
  ``default`` 展示为「近期」。

本文件同时提供输入归一化（``600519`` → ``600519.SH``）与批量粘贴解析，供页面与推荐页共用。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

import app_services as S

#: UI 分组（近期=关注中，远期=观察池）
GROUPS: tuple[str, ...] = ("近期", "远期")
DEFAULT_GROUP = "近期"

_UI_TO_GROUP = {"近期": "short_term", "远期": "long_term"}
_GROUP_TO_UI = {"short_term": "近期", "long_term": "远期", "default": "近期"}

_CODE_RE = re.compile(r"^(\d{6})(?:\.(SH|SZ|BJ))?$")
_PREFIX_RE = re.compile(r"^(SH|SZ|BJ)\.?(\d{6})$")

_last_error = ""


# ---------------------------------------------------------------------------
# 输入归一化 / 批量解析（纯展示层工具，不涉及存储）
# ---------------------------------------------------------------------------
def infer_exchange(code: str) -> str:
    """按 6 位代码推断交易所后缀（A 股常见规则；异常输入默认 SH）。"""
    if code.startswith(("4", "8")) or code.startswith("920"):
        return "BJ"
    if code.startswith(("6", "9")):
        return "SH"
    if code.startswith(("0", "2", "3")):
        return "SZ"
    return "SH"


def normalize_symbol(raw: Any) -> str | None:
    """把用户输入归一化为 ``600519.SH``；无法识别返回 ``None``。"""
    text = str(raw or "").strip().upper().replace(" ", "")
    if not text:
        return None
    match = _PREFIX_RE.match(text)
    if match:
        return f"{match.group(2)}.{match.group(1)}"
    match = _CODE_RE.match(text)
    if match:
        code = match.group(1)
        return f"{code}.{match.group(2) or infer_exchange(code)}"
    return None


def parse_bulk_text(text: str) -> tuple[list[dict[str, str]], list[str]]:
    """解析批量粘贴文本（每行一个标的，支持「代码 [名称]」）。"""
    items: list[dict[str, str]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for lineno, line in enumerate((text or "").splitlines(), start=1):
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        parts = re.split(r"[,;\t ]+", raw, maxsplit=1)
        symbol = normalize_symbol(parts[0])
        if symbol is None:
            errors.append(f"第 {lineno} 行「{raw}」无法识别：应为 6 位代码（如 600519 或 600519.SH）")
            continue
        if symbol in seen:
            errors.append(f"第 {lineno} 行「{raw}」重复（{symbol} 已在本次导入中）")
            continue
        seen.add(symbol)
        items.append({"symbol": symbol, "name": parts[1].strip() if len(parts) > 1 else ""})
    return items, errors


# ---------------------------------------------------------------------------
# 存储状态 / 只读降级
# ---------------------------------------------------------------------------
def _record_error(message: str) -> None:
    global _last_error
    _last_error = str(message or "")


def last_error() -> str:
    """最近一次存储错误（空表示正常）；页面据此提示/只读降级。"""
    return _last_error


def read_only() -> bool:
    """是否处于只读降级（存储不可用）。"""
    return bool(_last_error)


def storage_label() -> str:
    """当前存储说明（页面展示用）。"""
    return "SQLite 表 watchlist_items（data/watchlist.py · 经 app_services 门面）"


def storage_backend() -> str:
    """固定返回 ``db``：唯一来源为 data/watchlist.py；不可用时由页面只读展示。"""
    return "db"


def _to_ui_item(raw: Any) -> dict[str, Any]:
    """服务层条目（WatchlistItem / dict）→ 页面字段（中文分组）。"""
    if hasattr(raw, "to_dict"):
        data = raw.to_dict()
    elif isinstance(raw, dict):
        data = dict(raw)
    else:
        data = {
            "symbol": getattr(raw, "symbol", ""),
            "name": getattr(raw, "name", ""),
            "group": getattr(raw, "group_name", "default"),
            "note": getattr(raw, "note", ""),
            "added_at": getattr(raw, "added_at", None),
        }
    stored_group = str(data.get("group") or data.get("group_name") or "default")
    return {
        "symbol": str(data.get("symbol") or ""),
        "name": str(data.get("name") or ""),
        "group": _GROUP_TO_UI.get(stored_group, DEFAULT_GROUP),
        "note": str(data.get("note") or ""),
        "added_at": str(data.get("added_at") or ""),
    }


# ---------------------------------------------------------------------------
# 对外接口（页面只依赖这组函数）
# ---------------------------------------------------------------------------
def load_items() -> list[dict[str, Any]]:
    """读取全部自选标的；存储不可用时返回空列表并记录错误（页面只读降级）。"""
    ok, data, message = S.get_watchlist(None)
    if not ok or data is None:
        _record_error(message or "自选池读取失败（数据库不可用）")
        return []
    _record_error("")
    return [_to_ui_item(item) for item in data]


def list_items(group: str | None = None) -> list[dict[str, Any]]:
    """按 UI 分组过滤（``None`` 表示全部）。"""
    items = load_items()
    if group and group in GROUPS:
        return [item for item in items if item["group"] == group]
    return items


def is_in_watchlist(symbol: str) -> bool:
    target = normalize_symbol(symbol)
    return bool(target) and any(item["symbol"] == target for item in load_items())


def add_item(symbol: str, name: str = "", group: str = DEFAULT_GROUP,
             note: str = "") -> tuple[bool, str]:
    """新增（已存在时门面会更新名称/分组/备注；本层先查重，避免误导为“新增”）。"""
    canonical = normalize_symbol(symbol)
    if canonical is None:
        return False, f"代码「{symbol}」无法识别：应为 6 位代码（如 600519 或 600519.SH）"
    if is_in_watchlist(canonical):
        return False, f"{canonical} 已在自选池中"
    ok, _data, message = S.add_watchlist(
        canonical, group=_UI_TO_GROUP.get(group, "short_term"),
        name=str(name or "").strip(), note=str(note or "").strip(),
    )
    if ok:
        _record_error("")
        return True, message or f"已加入自选池：{canonical}"
    _record_error(message)
    return False, message or "加入自选池失败（数据库不可用）"


def add_many(entries: Iterable[dict[str, str]], group: str = DEFAULT_GROUP) -> dict[str, Any]:
    """批量新增；返回 ``{"added": n, "skipped": n, "messages": [...]}``。"""
    added = 0
    skipped = 0
    messages: list[str] = []
    for entry in entries:
        symbol = normalize_symbol(entry.get("symbol"))
        if symbol is None:
            skipped += 1
            messages.append(f"{entry.get('symbol')!r} 无法识别，已跳过")
            continue
        if is_in_watchlist(symbol):
            skipped += 1
            messages.append(f"{symbol} 已在自选池，已跳过")
            continue
        ok, message = add_item(symbol, entry.get("name") or "", group, "")
        if ok:
            added += 1
        else:
            skipped += 1
            messages.append(message)
    return {"added": added, "skipped": skipped, "messages": messages}


def remove_symbols(symbols: Iterable[str]) -> int:
    """删除若干标的，返回删除数量（失败项记录到 last_error）。"""
    removed = 0
    for raw in symbols:
        canonical = normalize_symbol(raw)
        if canonical is None:
            continue
        ok, _data, message = S.remove_watchlist(canonical)
        if ok:
            removed += 1
        else:
            _record_error(message)
    if removed:
        _record_error("")
    return removed


def update_item(symbol: str, *, name: str | None = None, group: str | None = None,
                note: str | None = None) -> bool:
    """修改名称/分组/备注（门面 add_watchlist 为 upsert；不改代码）。"""
    canonical = normalize_symbol(symbol)
    if canonical is None:
        return False
    current = next((item for item in load_items() if item["symbol"] == canonical), None)
    if current is None:
        return False
    ui_group = group if group in GROUPS else current["group"]
    ok, _data, message = S.add_watchlist(
        canonical,
        group=_UI_TO_GROUP.get(ui_group, "short_term"),
        name=name if name is not None else current["name"],
        note=note if note is not None else current["note"],
    )
    if not ok:
        _record_error(message)
    return ok


def symbols(group: str | None = None) -> list[str]:
    """自选池代码列表（行情页/推荐页默认标的使用）。"""
    return [item["symbol"] for item in list_items(group)]

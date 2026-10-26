"""A 股撮合规则与费用模型（**全项目唯一实现**，契约 CF-10）。

任务编号：M1-10（A 股撮合规则与费用模型）
口径来源：``docs/SPEC.md`` §4（A 股交易规则）、§6.4（回测接口）、§7（费率配置）
决策依据：``docs/SPEC.md`` 附录 B 决策 **D-3**、**D-4**、**D-5**

为什么只写一份
--------------
``trading/broker.py`` 的 ``PaperBroker`` 直接复用本模块的 :class:`AStockRules`
与 :class:`FeeModel`，从而保证“**回测 = 模拟盘**”的费用与规则口径完全一致
（SPEC §4.10 一致性表），禁止在别处再写第二份涨跌停/费用逻辑。

本模块提供的唯一实现
--------------------
* :class:`AStockRules` —— 涨跌停幅度（主板 10% / 主板 ST 5% / 创业板·科创板 20% /
  科创 ST 20% / 北交所 30%）、整手与科创板 200 股起、T+1 可卖校验、
  停牌/一字板/涨跌停不可成交判定、滑点、板块识别。
* :class:`FeeModel` —— 佣金（万 2.5，最低 5 元）、印花税（卖出 0.05%）、
  过户费（0.001%）、规费（0.00541%）；**每项独立四舍五入到 0.01 元后再累加**。
* :class:`SimBroker` —— 回测撮合器：``match(pending_orders, bar_by_symbol, date)``。
  撮合价基准为 T+1 开盘价 ± 滑点（D-4）；涨停价买入按“不成交”保守处理（D-5）。

金额与舍入口径（避免浮点误差）
------------------------------
所有金额/费用先用 :class:`decimal.Decimal` 精确计算，再按 **ROUND_HALF_UP**
（四舍五入，与券商对账单一致）量化到 0.01 元，最后才转成 ``float`` 对外返回
（数据库字段为 REAL，便于直接落库）。价格同样量化到 0.01 元（人民币最小报价单位）。

安全口径（fail-closed）
----------------------
* **卖出必须提供持仓**：``SimBroker.match*`` 的 ``positions`` 参数用于校验
  ``可卖数量 = available_quantity − 当日已卖``（T+1）。未传持仓、无该标的持仓、
  超过总持仓或超过可卖数量 → 一律拒绝成交（``RK015``/``RK016``），并写入
  ``MatchReport.warnings``，**绝不静默放行**；
* **前收盘价必须可算**：缺少有效 ``prev_close`` 时无法计算涨跌停，委托按
  ``NO_PREV_CLOSE`` 拒绝并记 warning（历史上由回测引擎从 bars 推导后传入）。

依赖与领域模型
--------------
* 依赖 ``core.models``（SPEC §3.1：backtest 依赖 core）提供的 ``FeeConfig`` /
  ``FeeBreakdown`` / ``Fill`` —— **领域模型唯一来源是 core/models.py（CF-7）**，
  本模块不重复定义；费用明细可直接赋给 ``OrderResult.fee``，成交可直接交给
  ``PortfolioService.apply_fill``。``Unfilled`` / ``MatchReport`` 是回测内部辅助
  结构，仅在本模块定义。
* 不 import ``trading`` / ``ui``；Bar / Order / Position 采取“鸭子类型”读取
  （支持 dataclass、pydantic 模型、dict、``pd.Series``），字段名与 core 一致。

数据契约（duck-typed 字段名）
------------------------------
* Bar: ``open/high/low/close``（必需）、``prev_close``（可选，用于算涨跌停；
  缺失时回退到订单 ``prev_close``，仍缺失则该笔跳过涨跌停判定并在 ``warnings`` 提示）、
  ``is_suspended``（可选，停牌标记）、``board``/``is_st``（可选，标的静态信息）。
* Order: ``id``/``order_id``、``symbol``、``side``、``quantity``、``price``（限价，
  MARKET 单可为 None/NaN）、``order_type``（LIMIT/MARKET，可选，默认 LIMIT）、
  ``defer_count``（可选，已顺延天数，超过 ``max_defer_days`` 则 EXPIRED）、
  ``board``/``is_st``/``prev_close``（可选）。
* Position: ``quantity``、``available_quantity``、``today_buy_quantity``（可选）。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from typing import Any

# 领域模型唯一来源：``core/models.py``（CF-7）。费用明细/成交回报直接复用该模型，
# 保证 ``FeeModel.calculate`` 结果能赋给 ``OrderResult.fee``、``SimBroker`` 成交能
# 直接交给 ``PortfolioService.apply_fill``，避免两套类型做转换。
try:  # pragma: no cover - 依赖环境
    from core.models import FeeBreakdown, FeeConfig, Fill
except ImportError as _core_exc:  # pragma: no cover
    raise ImportError(
        "回测撮合模块导入失败：无法从 core.models 导入 FeeConfig / FeeBreakdown / Fill（唯一来源）。"
        f"请先安装依赖（pydantic、sqlalchemy）：{_core_exc}"
    ) from _core_exc

__all__ = [
    # 常量
    "SIDE_BUY",
    "SIDE_SELL",
    "BOARD_MAIN",
    "BOARD_GEM",
    "BOARD_STAR",
    "BOARD_BSE",
    "BOARD_LABELS",
    "PRICE_LIMIT_PCT",
    "MAIN_ST_PRICE_LIMIT_PCT",
    "PRICE_TICK",
    "DEFAULT_SLIPPAGE_BPS",
    "DEFAULT_MAX_DEFER_DAYS",
    "REASON_MESSAGES",
    # 数据结构
    "FeeConfig",
    "FeeBreakdown",
    "Fill",
    "Unfilled",
    "MatchReport",
    # 唯一实现
    "AStockRules",
    "FeeModel",
    "SimBroker",
    # 工具函数
    "reason_message",
    "normalize_side",
    "normalize_board",
    "guess_board",
    "round_price",
    "round_money",
    "apply_slippage",
    "resolve_fee_config",
    "is_buy_fill",
    "fill_cash_delta",
]


# ---------------------------------------------------------------------------
# 一、常量与口径（SPEC §4）
# ---------------------------------------------------------------------------

SIDE_BUY = "BUY"
SIDE_SELL = "SELL"

#: 板块常量（与 ``symbols.board`` 字段取值一致）
BOARD_MAIN = "MAIN"  # 沪深主板
BOARD_GEM = "GEM"  # 创业板
BOARD_STAR = "STAR"  # 科创板
BOARD_BSE = "BSE"  # 北交所

BOARD_LABELS: dict[str, str] = {
    BOARD_MAIN: "沪深主板",
    BOARD_GEM: "创业板",
    BOARD_STAR: "科创板",
    BOARD_BSE: "北交所",
}

#: 各板块涨跌幅限制（SPEC §4.3）；主板 ST 单独 5%
PRICE_LIMIT_PCT: dict[str, Decimal] = {
    BOARD_MAIN: Decimal("0.10"),
    BOARD_GEM: Decimal("0.20"),
    BOARD_STAR: Decimal("0.20"),
    BOARD_BSE: Decimal("0.30"),
}

#: 仅**主板** ST/*ST 适用 5%；创业板/科创板 ST 仍为 20%（全面注册制口径，SPEC §4.3 注）
MAIN_ST_PRICE_LIMIT_PCT = Decimal("0.05")

#: 人民币最小报价单位（0.01 元）
PRICE_TICK = Decimal("0.01")

#: 默认滑点（5bp，SPEC §7 ``fees.slippage_bps``）
DEFAULT_SLIPPAGE_BPS = 5.0

#: 委托默认最多顺延 1 个交易日，之后 EXPIRED（SPEC §4.3）
DEFAULT_MAX_DEFER_DAYS = 1

#: 主板/创业板买入最小单位（股）
LOT_SIZE_MAIN = 100
#: 科创板买入最小数量（股，此后可按 1 股递增）
STAR_MIN_BUY = 200
#: 北交所买入最小数量（股，此后可按 1 股递增）
BSE_MIN_BUY = 100

_EPS = 1e-9


#: 撮合/校验原因码 -> 中文说明（回测成交明细、模拟盘拒单、UI 提示统一引用）
REASON_MESSAGES: dict[str, str] = {
    "FILLED": "已成交",
    "EXPIRED": "委托过期作废",
    "SUSPENDED": "标的停牌，当日不可成交",
    "PRICE_LIMIT_UNMATCHED": "涨跌停/一字板导致不可成交（保守口径）",
    "NO_BAR": "当日无行情数据，无法撮合",
    "NO_PREV_CLOSE": "缺少前收盘价，无法计算涨跌停",
    "RK011": "委托价超出涨跌停区间",
    "RK015": "持仓不足",
    "RK016": "T+1 可用数量不足（今日买入不可卖）",
    "RK017": "数量不符合整手规则",
    "RK018": "委托参数非法",
}


def reason_message(code: str, detail: str = "") -> str:
    """把原因码翻成中文（未知码原样返回），可附加细节。"""

    base = REASON_MESSAGES.get(str(code), str(code))
    return f"{base}：{detail}" if detail else base


def normalize_side(side: Any) -> str:
    """统一交易方向为 ``BUY``/``SELL``（兼容枚举与中文“买/卖”）。"""

    raw = side
    if isinstance(raw, Enum):  # 兼容 core.models.Side 等 str 枚举
        raw = raw.value
    elif not isinstance(raw, str) and hasattr(raw, "value"):
        raw = raw.value
    text = str(raw).strip().upper() if raw is not None else ""
    mapping = {
        "BUY": SIDE_BUY,
        "B": SIDE_BUY,
        "LONG": SIDE_BUY,
        "买": SIDE_BUY,
        "买入": SIDE_BUY,
        "SELL": SIDE_SELL,
        "S": SIDE_SELL,
        "SHORT": SIDE_SELL,
        "卖": SIDE_SELL,
        "卖出": SIDE_SELL,
    }
    try:
        return mapping[text]
    except KeyError as exc:
        raise ValueError(f"非法的交易方向：{side!r}（仅支持 BUY / SELL）") from exc


def normalize_board(board: Any, symbol: str | None = None) -> str:
    """统一板块写法为 ``MAIN/GEM/STAR/BSE``（支持中文别名与交易所后缀）。"""

    if board is None or (isinstance(board, str) and not board.strip()):
        if symbol is None:
            raise ValueError("board 不能为空；请传入 MAIN/GEM/STAR/BSE 或标的代码自动识别")
        return guess_board(symbol)
    text = str(board).strip().upper()
    aliases = {
        "MAIN": BOARD_MAIN,
        "主板": BOARD_MAIN,
        "沪市主板": BOARD_MAIN,
        "深市主板": BOARD_MAIN,
        "SH": BOARD_MAIN,
        "SZ": BOARD_MAIN,
        "GEM": BOARD_GEM,
        "创业板": BOARD_GEM,
        "STAR": BOARD_STAR,
        "科创板": BOARD_STAR,
        "BSE": BOARD_BSE,
        "北交所": BOARD_BSE,
        "BJ": BOARD_BSE,
    }
    try:
        return aliases[text]
    except KeyError as exc:
        raise ValueError(
            f"无法识别的板块 {board!r}；可选：MAIN（沪深主板）/GEM（创业板）/STAR（科创板）/BSE（北交所）"
        ) from exc


def guess_board(symbol: str) -> str:
    """按 A 股代码前缀推断板块（代码须 6 位，支持 ``600519.SH`` 写法）。

    * 沪市主板：600/601/603/605；深市主板：000/001/002/003
    * 创业板：300/301；科创板：688/689
    * 北交所：430/83x/87x/88x、920xxx
    """

    code = str(symbol or "").strip().upper().split(".")[0].strip()
    if not code.isdigit():
        raise ValueError(f"无法从标的代码 {symbol!r} 识别板块：代码应为 6 位数字（如 600519.SH）")
    if code.startswith(("688", "689")):
        return BOARD_STAR
    if code.startswith(("300", "301")):
        return BOARD_GEM
    if code.startswith(("600", "601", "603", "605")):
        return BOARD_MAIN
    if code.startswith(("000", "001", "002", "003")):
        return BOARD_MAIN
    if code.startswith("920") or code.startswith("430") or code[:2] in ("83", "87", "88"):
        return BOARD_BSE
    raise ValueError(
        f"无法从标的代码 {symbol!r} 识别板块（支持沪深主板/创业板/科创板/北交所）；请显式传入 board"
    )


# ---------------------------------------------------------------------------
# 二、数值工具：Decimal 精确计算 + 四舍五入到分
# ---------------------------------------------------------------------------


def _to_decimal(value: Any, *, name: str = "数值") -> Decimal:
    """把 int/float/str/Decimal 转成 Decimal（float 走最短往返表示，避免 0.1 误差）。"""

    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError(f"{name}不能是布尔值")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{name}无法转换为数值：{value!r}") from exc


def _quantize_money(value: Decimal) -> Decimal:
    """四舍五入（ROUND_HALF_UP）到 0.01 元。"""

    return value.quantize(PRICE_TICK, rounding=ROUND_HALF_UP)


def round_money(value: Any) -> float:
    """金额四舍五入到分，返回 ``float``（用于落库/展示）。"""

    return float(_quantize_money(_to_decimal(value, name="金额")))


def round_price(value: Any) -> float:
    """价格四舍五入到 0.01 元（人民币最小报价单位）。"""

    return float(_quantize_money(_to_decimal(value, name="价格")))


def _is_finite_number(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _field(obj: Any, name: str) -> Any:
    """按属性/字典键读取字段（不存在或为 None 时返回 None）。"""

    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    try:
        return getattr(obj, name)
    except AttributeError:
        return None


def _first_field(obj: Any, names: Sequence[str]) -> Any:
    """按候选字段名依次读取，返回第一个非 None 值。"""

    for name in names:
        value = _field(obj, name)
        if value is not None:
            return value
    return None


def _positions_map(positions: Any) -> dict[str, Any]:
    """把持仓集合规范成 ``symbol -> position`` 映射（dict 或 list 均支持）。"""

    if positions is None:
        return {}
    if isinstance(positions, Mapping):
        return {str(k): v for k, v in positions.items()}
    result: dict[str, Any] = {}
    for item in positions:
        symbol = _first_field(item, ("symbol", "code", "ts_code"))
        if symbol is not None:
            result[str(symbol)] = item
    return result


# ---------------------------------------------------------------------------
# 三、A 股撮合规则（唯一实现）
# ---------------------------------------------------------------------------


class AStockRules:
    """A 股交易规则（静态方法集合；PaperBroker 与回测引擎共用）。

    所有方法均为**纯函数**（无 IO、无随机、无状态），相同输入必得相同输出。
    """

    # -- 涨跌停 -----------------------------------------------------------
    @staticmethod
    def limit_pct(board: str = BOARD_MAIN, is_st: bool = False) -> float:
        """返回涨跌幅限制比例（0.10 表示 ±10%）。

        规则（SPEC §4.3）：先判板块，再判 ST —— 仅**主板 ST** 为 5%；
        创业板/科创板 ST 仍为 20%；北交所 30%。
        """

        normalized = normalize_board(board)
        if normalized == BOARD_MAIN and bool(is_st):
            return float(MAIN_ST_PRICE_LIMIT_PCT)
        return float(PRICE_LIMIT_PCT[normalized])

    @staticmethod
    def price_limits(prev_close: float, board: str = BOARD_MAIN, is_st: bool = False) -> tuple[float, float]:
        """计算涨停价与跌停价：``round(prev_close × (1 ± 幅度), 2)``（四舍五入到分）。

        Args:
            prev_close: 上一交易日收盘价（不复权；除权日取交易所除权参考价）。
            board: ``MAIN/GEM/STAR/BSE``。
            is_st: 是否 ST/*ST（仅主板有效）。

        Returns:
            ``(limit_up, limit_down)``，均为 0.01 元精度的 float。

        Raises:
            ValueError: 前收盘价非正数或板块非法（中文消息）。
        """

        if not _is_finite_number(prev_close) or float(prev_close) <= 0:
            raise ValueError(f"前收盘价必须为正数，收到 {prev_close!r}")
        pct = _to_decimal(AStockRules.limit_pct(board, is_st), name="涨跌幅比例")
        prev = _to_decimal(prev_close, name="前收盘价")
        limit_up = _quantize_money(prev * (Decimal("1") + pct))
        limit_down = _quantize_money(prev * (Decimal("1") - pct))
        if limit_down < PRICE_TICK:
            # 极低价标的的跌停价不得低于最小报价单位 0.01 元
            limit_down = PRICE_TICK
        return float(limit_up), float(limit_down)

    # -- 整手规则 ---------------------------------------------------------
    @staticmethod
    def validate_lot(
        side: str,
        quantity: int,
        available: int | None = None,
        board: str = BOARD_MAIN,
    ) -> tuple[bool, str | None]:
        """校验最小交易单位与整手规则（SPEC §4.4）。

        规则：
          * 买入：主板/创业板 ``≥100 股且为 100 的整数倍``；科创板 ``≥200 股``
            （此后按 1 股递增）；北交所 ``≥100 股``（按 1 股递增）。
          * 卖出：``quantity ≤ available``；非全部卖出时，主板/创业板须为 100 的
            整数倍；全部卖出（``quantity == available``）允许零股一次性卖出；
            科创板/北交所按 1 股递增。
          * 数量 ≤ 0 一律拒绝。

        Args:
            side: BUY/SELL。
            quantity: 委托数量（股）。
            available: 可卖数量（仅 SELL 使用；None 表示调用方未提供持仓信息，
                此时跳过可用数量校验、只校验整手）。
            board: 板块。

        Returns:
            ``(True, None)`` 或 ``(False, 原因码)``；原因码为 ``RK017``（整手/
            数量非法）或 ``RK016``（T+1 可用数量不足），中文说明见
            :func:`reason_message`。
        """

        direction = normalize_side(side)
        normalized_board = normalize_board(board)
        if isinstance(quantity, bool):
            return False, "RK017"
        try:
            qty = int(quantity)
        except (TypeError, ValueError):
            return False, "RK017"
        if isinstance(quantity, float) and not float(quantity).is_integer():
            return False, "RK017"
        if qty <= 0:
            return False, "RK017"

        if direction == SIDE_BUY:
            if normalized_board == BOARD_STAR:
                return (True, None) if qty >= STAR_MIN_BUY else (False, "RK017")
            if normalized_board == BOARD_BSE:
                return (True, None) if qty >= BSE_MIN_BUY else (False, "RK017")
            if qty < LOT_SIZE_MAIN or qty % LOT_SIZE_MAIN != 0:
                return False, "RK017"
            return True, None

        # 卖出
        if available is not None:
            try:
                available_qty = int(available)
            except (TypeError, ValueError):
                return False, "RK016"
            if available_qty < 0:
                return False, "RK016"
            if qty > available_qty:
                return False, "RK016"
        if normalized_board in (BOARD_STAR, BOARD_BSE):
            # 科创板/北交所卖出按 1 股递增
            return True, None
        # 主板/创业板：非全部卖出须整手；零股必须一次性卖出
        if available is not None and qty == int(available):
            return True, None
        if qty % LOT_SIZE_MAIN != 0:
            return False, "RK017"
        return True, None

    @staticmethod
    def validate_t1(quantity: int, position: Any) -> tuple[bool, str | None]:
        """T+1 可卖校验：``quantity ≤ available_quantity``，且不得超过总持仓。

        当日买入的股份计入 ``today_buy_quantity``，不计入 ``available_quantity``
        （SPEC §4.2）；每交易日盘前由 ``PortfolioService.settle_new_day`` 重置。

        Returns:
            ``(True, None)`` / ``(False, "RK015")``（持仓不足）/
            ``(False, "RK016")``（T+1 可用不足）。
        """

        try:
            qty = int(quantity)
        except (TypeError, ValueError):
            return False, "RK017"
        if qty <= 0:
            return False, "RK017"
        total = _first_field(position, ("quantity", "total_quantity", "volume")) or 0
        available = _first_field(position, ("available_quantity", "available", "sellable_quantity"))
        try:
            total_qty = int(total)
        except (TypeError, ValueError):
            total_qty = 0
        if qty > total_qty:
            return False, "RK015"
        if available is None:
            return (True, None) if qty <= total_qty else (False, "RK015")
        try:
            available_qty = int(available)
        except (TypeError, ValueError):
            available_qty = 0
        if qty > available_qty:
            return False, "RK016"
        return True, None

    @staticmethod
    def validate_price_in_limits(
        side: str, price: float | None, limits: tuple[float, float] | None
    ) -> tuple[bool, str | None]:
        """委托价是否落在涨跌停区间内（SPEC §4.3 → ``RK011``）。

        ``price`` 为 None（市价单）或 ``limits`` 为 None 时视为通过。
        """

        if price is None or limits is None:
            return True, None
        if not _is_finite_number(price) or float(price) <= 0:
            return False, "RK018"
        direction = normalize_side(side)
        limit_up, limit_down = float(limits[0]), float(limits[1])
        if direction == SIDE_BUY and float(price) > limit_up + _EPS:
            return False, "RK011"
        if direction == SIDE_SELL and float(price) < limit_down - _EPS:
            return False, "RK011"
        return True, None

    # -- 不可成交判定 -----------------------------------------------------
    @staticmethod
    def is_suspended(bar: Any) -> bool:
        """是否停牌/无有效行情（``is_suspended`` 为真或 OHLC 缺失/NaN）。"""

        if bar is None:
            return True
        if bool(_first_field(bar, ("is_suspended", "suspended", "isSuspend"))):
            return True
        status = _first_field(bar, ("trade_status", "status"))
        if isinstance(status, str) and status.strip().upper() in ("SUSPENDED", "STOP", "HALTED"):
            return True
        return _bar_price(bar, "open") is None

    @staticmethod
    def is_one_word_limit(bar: Any, level: float | None = None) -> bool:
        """是否一字板：``open == high == low``（可再要求等于给定涨/跌停价）。"""

        open_price = _bar_price(bar, "open")
        high_price = _bar_price(bar, "high")
        low_price = _bar_price(bar, "low")
        if open_price is None or high_price is None or low_price is None:
            return False
        if abs(open_price - high_price) > _EPS or abs(high_price - low_price) > _EPS:
            return False
        if level is None:
            return True
        return abs(open_price - float(level)) <= _EPS

    @staticmethod
    def can_fill(
        side: str,
        price: float | None,
        bar: Any,
        limits: tuple[float, float] | None = None,
    ) -> bool:
        """单个委托当日能否成交（保守口径，回测与模拟盘一致，SPEC §4.3）。

        判定规则（买方向，卖方向对称）：

        1. 停牌/无有效 OHLC → 不可成交；
        2. 一字涨停（``open=high=low=涨停价``）→ 买入不可成交；
        3. 委托价 = 涨停价 → 买入按**不成交**处理（D-5，避免“排板必成”的乐观偏差）；
        4. 委托价 < 涨停价：开盘价 ≤ 委托价 → 可成交；否则仅当 ``low ≤ 委托价``
           才视为日内可成交；
        5. 市价单（``price is None``）：以开盘价判定，若开盘价已到涨停价 → 不成交。

        Args:
            side: BUY/SELL。
            price: 限价（None 表示市价单）。
            bar: 当日行情（含 open/high/low）。
            limits: ``(涨停价, 跌停价)``；None 表示不校验涨跌停。

        Returns:
            可成交返回 True。
        """

        direction = normalize_side(side)
        if AStockRules.is_suspended(bar):
            return False
        open_price = _bar_price(bar, "open")
        high_price = _bar_price(bar, "high")
        low_price = _bar_price(bar, "low")
        if open_price is None or open_price <= 0:
            return False
        limit_up, limit_down = (None, None) if limits is None else (float(limits[0]), float(limits[1]))
        order_price = None if price is None else float(price)

        if direction == SIDE_BUY:
            if limit_up is not None:
                if AStockRules.is_one_word_limit(bar, limit_up):
                    return False
                if order_price is None:
                    if open_price >= limit_up - _EPS:
                        return False
                elif order_price >= limit_up - _EPS:
                    return False
            if order_price is None:
                return True
            if open_price <= order_price + _EPS:
                return True
            return low_price is not None and low_price <= order_price + _EPS

        # 卖出（对称）
        if limit_down is not None:
            if AStockRules.is_one_word_limit(bar, limit_down):
                return False
            if order_price is None:
                if open_price <= limit_down + _EPS:
                    return False
            elif order_price <= limit_down + _EPS:
                return False
        if order_price is None:
            return True
        if open_price >= order_price - _EPS:
            return True
        return high_price is not None and high_price >= order_price - _EPS

    @staticmethod
    def apply_slippage(base_price: float, side: str, slippage_bps: float = DEFAULT_SLIPPAGE_BPS) -> float:
        """对成交基准价施加不利滑点并四舍五入到 0.01 元。

        买入 = 基准价 × (1 + bps/10000)，卖出 = 基准价 × (1 − bps/10000)。
        """

        direction = normalize_side(side)
        if not _is_finite_number(base_price) or float(base_price) <= 0:
            raise ValueError(f"滑点基准价必须为正数，收到 {base_price!r}")
        try:
            bps = float(slippage_bps)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"滑点 slippage_bps 必须是数值，收到 {slippage_bps!r}") from exc
        if not math.isfinite(bps) or bps < 0:
            raise ValueError(f"滑点 slippage_bps 必须是非负有限数值，收到 {slippage_bps!r}")
        base = _to_decimal(base_price, name="成交基准价")
        rate = _to_decimal(bps) / Decimal("10000")
        if direction == SIDE_BUY:
            return float(_quantize_money(base * (Decimal("1") + rate)))
        return float(_quantize_money(base * (Decimal("1") - rate)))


# ---------------------------------------------------------------------------
# 四、费用模型（唯一实现）
# ---------------------------------------------------------------------------


#: 费率配置的字段名（与 ``core.models.FeeConfig`` / ``config.yaml`` 的 ``fees`` 段一致）
_FEE_FIELDS: tuple[str, ...] = (
    "commission_rate",
    "min_commission",
    "stamp_tax_rate",
    "transfer_fee_rate",
    "reg_fee_rate",
    "include_reg_fee_in_commission",
    "slippage_bps",
)


def resolve_fee_config(value: Any = None) -> FeeConfig:
    """把 None / dict / ``core.models.FeeConfig`` / 配置对象统一转成 ``FeeConfig``。

    ``PaperBroker`` 与回测引擎都通过本函数解析 ``config.yaml`` 的 ``fees`` 段，
    保证两处费率口径一致（CF-10）。非法费率给出中文错误提示。
    """

    if value is None:
        return FeeConfig()
    if isinstance(value, FeeConfig):
        return value
    data: dict[str, Any] = {}
    if isinstance(value, Mapping):
        for name in _FEE_FIELDS:
            if name in value and value[name] is not None:
                data[name] = value[name]
    else:
        for name in _FEE_FIELDS:
            raw = _field(value, name)
            if raw is not None:
                data[name] = raw
    try:
        return FeeConfig(**data)
    except Exception as exc:  # pydantic ValidationError → 中文提示
        raise ValueError(f"费率配置非法，请检查 config.yaml 的 fees 段：{exc}") from exc


def is_buy_fill(fill: Any) -> bool:
    """成交方向是否为买入（兼容 ``core.models.Fill.side`` 为空的情况）。"""

    side = _field(fill, "side")
    if side is None:
        return False
    return normalize_side(side) == SIDE_BUY


def fill_cash_delta(fill: Any) -> float:
    """成交对现金的影响（买入为负：金额+费用；卖出为正：金额−费用）。"""

    amount = _to_decimal(_field(fill, "amount") or Decimal("0"), name="成交金额")
    fee = _to_decimal(_field(fill, "fee_total") or Decimal("0"), name="总费用")
    if is_buy_fill(fill):
        return float(-(amount + fee))
    return float(amount - fee)


class FeeModel:
    """A 股股票费用模型（唯一实现，回测与 PaperBroker 共用）。

    公式（SPEC §4.5）::

        成交金额 = 成交价 × 数量
        佣金     = max(成交金额 × 佣金费率, 最低佣金 5 元)
        印花税   = 成交金额 × 0.05%      （仅卖出）
        过户费   = 成交金额 × 0.001%     （买卖双向）
        规费     = 成交金额 × 0.00541%   （买卖双向，可配置并入佣金）
        单笔总费用 = 佣金 + 印花税 + 过户费 + 规费
        买入总支出 = 成交金额 + 总费用；卖出净收入 = 成交金额 − 总费用

    每项费用**独立四舍五入到 0.01 元**后再累加（AC-F3-03 用例锁定）。
    """

    def __init__(self, config: Any = None) -> None:
        self.config = resolve_fee_config(config)

    # -- 对外主接口（契约 CF-10） -----------------------------------------
    def calculate(
        self,
        side: str,
        price: float,
        quantity: int,
        fees: Any = None,
    ) -> FeeBreakdown:
        """计算一笔成交的费用明细。

        Args:
            side: ``BUY``/``SELL``。
            price: 成交价（元，通常已含滑点）。
            quantity: 成交数量（股，正整数）。
            fees: 费率配置（``core.models.FeeConfig`` / dict / 配置对象）；
                None 表示使用本实例构造时的配置。

        Returns:
            ``core.models.FeeBreakdown``（各费用已四舍五入到分；可直接赋给
            ``OrderResult.fee``）。成交金额可用 :meth:`FeeModel.amount` 计算。

        Raises:
            ValueError: 方向/价格/数量非法（中文消息）。
        """

        direction = normalize_side(side)
        if not _is_finite_number(price) or float(price) <= 0:
            raise ValueError(f"成交价必须为正数，收到 {price!r}")
        if isinstance(quantity, bool):
            raise ValueError("成交数量必须是正整数，收到布尔值")
        try:
            qty = int(quantity)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"成交数量必须是正整数，收到 {quantity!r}") from exc
        if isinstance(quantity, float) and not float(quantity).is_integer():
            raise ValueError(f"成交数量必须是整数股，收到 {quantity!r}")
        if qty <= 0:
            raise ValueError(f"成交数量必须大于 0，收到 {quantity!r}")

        config = self.config if fees is None else resolve_fee_config(fees)
        amount = _quantize_money(_to_decimal(price) * Decimal(qty))

        commission_raw = _quantize_money(amount * _to_decimal(config.commission_rate))
        min_commission = _quantize_money(_to_decimal(config.min_commission))
        commission = commission_raw if commission_raw >= min_commission else min_commission
        stamp_tax = (
            _quantize_money(amount * _to_decimal(config.stamp_tax_rate))
            if direction == SIDE_SELL
            else Decimal("0.00")
        )
        transfer_fee = _quantize_money(amount * _to_decimal(config.transfer_fee_rate))
        reg_fee = _quantize_money(amount * _to_decimal(config.reg_fee_rate))
        if config.include_reg_fee_in_commission:
            commission += reg_fee
            reg_fee = Decimal("0.00")
        total = commission + stamp_tax + transfer_fee + reg_fee
        return FeeBreakdown(
            commission=float(commission),
            stamp_tax=float(stamp_tax),
            transfer_fee=float(transfer_fee),
            reg_fee=float(reg_fee),
            total=float(total),
            slippage_cost=0.0,
        )

    # -- 便捷方法 ---------------------------------------------------------
    @staticmethod
    def amount(price: float, quantity: int) -> float:
        """成交金额 = 成交价 × 数量（四舍五入到分）。"""

        if not _is_finite_number(price) or float(price) <= 0:
            raise ValueError(f"成交价必须为正数，收到 {price!r}")
        return round_money(_to_decimal(price) * Decimal(int(quantity)))


# ---------------------------------------------------------------------------
# 五、回测撮合器
# ---------------------------------------------------------------------------


# 成交回报 Fill 直接使用 ``core.models.Fill``（CF-7 唯一来源），本模块不重复定义；
# ``SimBroker`` 返回的 Fill 可直接交给 ``PortfolioService.apply_fill``。
# 便捷判断见上方 :func:`is_buy_fill` / :func:`fill_cash_delta`。


@dataclass(frozen=True)
class Unfilled:
    """未能成交的委托（用于回测报告与 PaperBroker 拒单/顺延说明）。"""

    order_id: str
    symbol: str
    side: str
    quantity: int
    reason_code: str
    message: str
    trade_date: date | None = None
    price: float | None = None
    deferred: bool = False
    expired: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "reason_code": self.reason_code,
            "message": self.message,
            "trade_date": self.trade_date,
            "price": self.price,
            "deferred": self.deferred,
            "expired": self.expired,
        }


@dataclass
class MatchReport:
    """撮合结果汇总（``SimBroker.match_with_report`` 返回）。

    * ``fills``：已成交（引擎据此扣减待执行队列）；
    * ``deferred``：价格/停牌原因当日未成交、顺延下一交易日；
    * ``expired``：顺延天数用尽，作废（``EXPIRED``）；
    * ``rejected``：参数/整手/T+1 等确定性拒绝，不再重试；
    * ``warnings``：数据口径警告（如缺少前收盘价、未知板块兜底等）。
    """

    fills: list[Fill] = field(default_factory=list)
    deferred: list[Unfilled] = field(default_factory=list)
    expired: list[Unfilled] = field(default_factory=list)
    rejected: list[Unfilled] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def filled_order_ids(self) -> set[str]:
        return {fill.order_id for fill in self.fills}

    def as_dict(self) -> dict[str, Any]:
        return {
            "fills": [
                fill.model_dump() if hasattr(fill, "model_dump") else dict(fill)
                for fill in self.fills
            ],
            "deferred": [u.as_dict() for u in self.deferred],
            "expired": [u.as_dict() for u in self.expired],
            "rejected": [u.as_dict() for u in self.rejected],
            "warnings": list(self.warnings),
        }


class SimBroker:
    """回测撮合器：把“前一日挂单”按当日 bar 撮合成交（唯一撮合实现）。

    撮合口径（SPEC §4.3 / §4.8 / 决策 D-4）：

    * 基准价 = 当日**开盘价**（集合竞价成交价的近似，不用最高/最低价臆造成交）；
    * 限价单：开盘价不满足限价时退化为“日内可成交价”判定（买：``low ≤ 限价``，
      卖：``high ≥ 限价``），成交价取限价；
    * 买入/卖出成交价再加**不利滑点**（默认 5bp），并按限价与当日高低价夹取；
    * 涨停价买入、跌停价卖出、一字板、停牌 → 当日不成交（保守，D-5），
      默认最多顺延 1 个交易日，之后 ``EXPIRED``；
    * 费用由 :class:`FeeModel` 计算（与 PaperBroker 同源）。

    本类**无状态**：不修改传入的订单对象，也不读写数据库；``defer_count``
    由调用方（回测引擎）在订单上递增，``SimBroker`` 只读取。
    """

    def __init__(
        self,
        fee_config: Any = None,
        slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
        max_defer_days: int = DEFAULT_MAX_DEFER_DAYS,
    ) -> None:
        self.fee_config = resolve_fee_config(fee_config)
        if slippage_bps is None:
            slippage_bps = self.fee_config.slippage_bps
        try:
            self.slippage_bps = float(slippage_bps)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"滑点 slippage_bps 必须是数值，收到 {slippage_bps!r}") from exc
        if not math.isfinite(self.slippage_bps) or self.slippage_bps < 0:
            raise ValueError(f"滑点 slippage_bps 必须是非负有限数值，收到 {slippage_bps!r}")
        self.max_defer_days = max(0, int(max_defer_days))
        self.fee_model = FeeModel(self.fee_config)

    # -- 对外接口（契约 CF-10） -------------------------------------------
    def match(
        self,
        pending_orders: Sequence[Any],
        bar_by_symbol: Mapping[str, Any],
        day: date | None = None,
        *,
        positions: Any = None,
    ) -> list[Fill]:
        """撮合挂单，**只返回成交**（未成交/作废明细用 :meth:`match_with_report`）。

        Args:
            pending_orders: 待撮合委托（通常是前一日收盘后生成、T+1 执行的订单）。
            bar_by_symbol: ``symbol -> 当日 Bar``。
            day: 交易日（写入成交明细）。
            positions: 持仓（**卖单必需**；缺失时卖单按 fail-closed 拒绝，见
                :meth:`match_with_report`）。

        Returns:
            成交列表 :class:`Fill`（按委托顺序）。
        """

        return self.match_with_report(pending_orders, bar_by_symbol, day, positions=positions).fills

    def match_with_report(
        self,
        pending_orders: Sequence[Any],
        bar_by_symbol: Mapping[str, Any],
        day: date | None = None,
        positions: Any = None,
    ) -> MatchReport:
        """撮合并返回完整报告（成交/顺延/作废/拒绝/警告）。

        **卖出校验（fail-closed）**：卖单必须提供持仓信息，且按
        ``可卖数量 = available_quantity − 当日已卖数量``（T+1 口径）校验：

        * 未传 ``positions`` → 全部卖单拒绝（原因码 ``RK015``）并记 warning，绝不放行；
        * 无该标的持仓 → 拒绝 ``RK015``（持仓不足）；
        * 卖出数量 > 总持仓 → 拒绝 ``RK015``；卖出数量 > 当日可卖 → 拒绝 ``RK016``
          （T+1 可用数量不足，当日买入不可卖）。

        Args:
            pending_orders: 待撮合委托。
            bar_by_symbol: 当日行情。
            day: 交易日。
            positions: 持仓（dict ``symbol -> Position`` 或 Position 列表；卖单必需）。
        """

        report = MatchReport()
        if not pending_orders:
            return report
        bars = bar_by_symbol or {}
        position_map = _positions_map(positions)
        positions_provided = positions is not None
        warning_set: set[str] = set()
        #: 同一批撮合内已成交的卖出数量（T+1 口径下当日可卖数量需要滚动扣减）
        sold_today: dict[str, int] = {}

        for index, order in enumerate(pending_orders):
            outcome, payload = self._match_one(
                order, index, bars, day, position_map, positions_provided, sold_today, warning_set
            )
            if outcome == "fill":
                report.fills.append(payload)
                if normalize_side(getattr(payload, "side", None)) == SIDE_SELL:
                    symbol = str(getattr(payload, "symbol", "") or "")
                    sold_today[symbol] = sold_today.get(symbol, 0) + int(getattr(payload, "quantity", 0) or 0)
            elif outcome == "defer":
                report.deferred.append(payload)
            elif outcome == "expire":
                report.expired.append(payload)
            else:
                report.rejected.append(payload)

        report.warnings = sorted(warning_set)
        return report

    # -- 内部实现 ---------------------------------------------------------
    def _match_one(
        self,
        order: Any,
        index: int,
        bars: Mapping[str, Any],
        day: date | None,
        positions: Mapping[str, Any],
        positions_provided: bool,
        sold_today: Mapping[str, int],
        warnings: set[str],
    ) -> tuple[str, Any]:
        """处理单笔委托，返回 ``(结果类型, Fill 或 Unfilled)``。"""

        order_id = str(_first_field(order, ("id", "order_id", "orderId", "client_order_id")) or f"#{index}")
        symbol = str(_first_field(order, ("symbol", "code", "ts_code")) or "").strip()
        if not symbol:
            return "reject", self._unfilled(order_id, "", "", 0, "RK018", "委托缺少标的代码 (symbol)", day)

        try:
            side = normalize_side(_first_field(order, ("side", "direction")))
        except ValueError as exc:
            return "reject", self._unfilled(order_id, symbol, "", 0, "RK018", str(exc), day)

        quantity_raw = _first_field(order, ("quantity", "qty", "volume", "order_quantity"))
        try:
            if isinstance(quantity_raw, bool):
                raise ValueError
            quantity = int(quantity_raw)
            if isinstance(quantity_raw, float) and not float(quantity_raw).is_integer():
                raise ValueError
        except (TypeError, ValueError):
            return "reject", self._unfilled(
                order_id, symbol, side, 0, "RK018", f"委托数量必须是正整数，收到 {quantity_raw!r}", day
            )

        order_type = str(_first_field(order, ("order_type", "type", "price_type")) or "LIMIT").upper()
        raw_price = _first_field(order, ("price", "limit_price", "order_price"))
        if isinstance(raw_price, float) and math.isnan(raw_price):
            raw_price = None
        is_market = order_type.startswith("MARKET") or raw_price is None
        limit_price: float | None = None
        if not is_market:
            try:
                limit_price = float(raw_price)
            except (TypeError, ValueError):
                return "reject", self._unfilled(
                    order_id, symbol, side, quantity, "RK018", f"委托价必须是数值，收到 {raw_price!r}", day
                )
            if not math.isfinite(limit_price) or limit_price <= 0:
                return "reject", self._unfilled(
                    order_id, symbol, side, quantity, "RK018", f"委托价必须为正数，收到 {raw_price!r}", day
                )

        bar = bars.get(symbol)
        if bar is None:
            return self._unfilled_or_expired(
                order, order_id, symbol, side, quantity, limit_price, day, "NO_BAR", deferred=True
            )

        # 板块与 ST：订单 > bar > 代码推断
        provided_board = _first_field(order, ("board",)) or _first_field(bar, ("board",))
        try:
            board = normalize_board(provided_board, symbol=symbol)
        except ValueError as exc:
            warnings.add(f"{symbol}: {exc}，按沪深主板处理")
            board = BOARD_MAIN
        is_st_raw = _first_field(order, ("is_st", "isST"))
        if is_st_raw is None:
            is_st_raw = _first_field(bar, ("is_st", "isST"))
        is_st = bool(is_st_raw)

        # 停牌 / 无有效行情
        if AStockRules.is_suspended(bar):
            return self._unfilled_or_expired(
                order, order_id, symbol, side, quantity, limit_price, day, "SUSPENDED", deferred=True
            )

        # 卖出校验（fail-closed，SPEC §4.2 T+1 / 持仓不足）
        # 规则：可卖数量 = available_quantity − 同一批撮合内当日已卖数量；
        #       未提供 positions 或该标的无持仓 → 一律拒绝，绝不放行。
        available: int | None = None
        if side == SIDE_SELL:
            if not positions_provided:
                warnings.add(
                    f"{symbol}: 卖出委托未提供持仓信息，按 fail-closed 拒绝（无法校验 T+1 可卖数量）"
                )
                return "reject", self._unfilled(
                    order_id,
                    symbol,
                    side,
                    quantity,
                    "RK015",
                    reason_message("RK015") + "（未提供持仓信息，卖出 fail-closed 拒绝）",
                    day,
                    limit_price,
                )
            position = positions.get(symbol)
            if position is None:
                warnings.add(f"{symbol}: 卖出委托无对应持仓，已拒绝")
                return "reject", self._unfilled(
                    order_id, symbol, side, quantity, "RK015", reason_message("RK015"), day, limit_price
                )
            total_quantity = int(_first_field(position, ("quantity", "total_quantity")) or 0)
            raw_available = int(_first_field(position, ("available_quantity", "available")) or 0)
            available = max(0, raw_available - int(sold_today.get(symbol, 0) or 0))
            if quantity > total_quantity:
                return "reject", self._unfilled(
                    order_id,
                    symbol,
                    side,
                    quantity,
                    "RK015",
                    reason_message("RK015", f"委托 {quantity} 股超过总持仓 {total_quantity} 股"),
                    day,
                    limit_price,
                )
            if quantity > available:
                return "reject", self._unfilled(
                    order_id,
                    symbol,
                    side,
                    quantity,
                    "RK016",
                    reason_message("RK016", f"可卖 {available} 股，委托 {quantity} 股"),
                    day,
                    limit_price,
                )
        ok, code = AStockRules.validate_lot(side, quantity, available if side == SIDE_SELL else None, board)
        if not ok:
            return "reject", self._unfilled(
                order_id, symbol, side, quantity, code or "RK017", reason_message(code or "RK017"), day, limit_price
            )

        # 涨跌停价与价格合法性（前收盘价 fail-closed：缺失即拒绝，不静默放行）
        prev_close = _first_field(bar, ("prev_close", "pre_close", "prevClose", "preClose")) or _first_field(
            order, ("prev_close", "pre_close")
        )
        if prev_close is None or not _is_finite_number(prev_close) or float(prev_close) <= 0:
            warnings.add(
                f"{symbol}: 缺少有效前收盘价，无法计算涨跌停，委托按 fail-closed 拒绝"
                "（请由历史 bars 提供 prev_close）"
            )
            return "reject", self._unfilled(
                order_id,
                symbol,
                side,
                quantity,
                "NO_PREV_CLOSE",
                reason_message("NO_PREV_CLOSE", "缺少前收盘价，按 fail-closed 拒绝成交"),
                day,
                limit_price,
            )
        limits = AStockRules.price_limits(float(prev_close), board, is_st)
        ok, code = AStockRules.validate_price_in_limits(side, limit_price, limits)
        if not ok:
            return "reject", self._unfilled(
                order_id, symbol, side, quantity, code or "RK011", reason_message(code or "RK011"), day, limit_price
            )

        # 不可成交判定（停牌/一字板/涨跌停价委托/未触及限价）
        if not AStockRules.can_fill(side, limit_price, bar, limits):
            return self._unfilled_or_expired(
                order,
                order_id,
                symbol,
                side,
                quantity,
                limit_price,
                day,
                "PRICE_LIMIT_UNMATCHED",
                deferred=True,
            )

        open_price = _bar_price(bar, "open")
        high_price = _bar_price(bar, "high")
        low_price = _bar_price(bar, "low")
        if open_price is None or open_price <= 0:
            return self._unfilled_or_expired(
                order, order_id, symbol, side, quantity, limit_price, day, "NO_BAR", deferred=True
            )

        # 成交基准价：T+1 开盘价；限价单开盘不满足限价时退化为限价
        if limit_price is None:
            reference = open_price
        elif side == SIDE_BUY:
            reference = open_price if open_price <= limit_price + _EPS else limit_price
        else:
            reference = open_price if open_price >= limit_price - _EPS else limit_price

        fill_price = AStockRules.apply_slippage(reference, side, self.slippage_bps)
        if limit_price is not None:
            fill_price = min(fill_price, limit_price) if side == SIDE_BUY else max(fill_price, limit_price)
        if high_price is not None and fill_price > high_price:
            fill_price = round_price(high_price)
        if low_price is not None and fill_price < low_price:
            fill_price = round_price(low_price)
        # 成交价不得越出涨跌停区间（滑点只施加不利偏移，不应制造越界成交）
        limit_up, limit_down = limits
        if side == SIDE_BUY:
            fill_price = min(fill_price, limit_up)
        else:
            fill_price = max(fill_price, limit_down)
        if fill_price <= 0:
            return "reject", self._unfilled(
                order_id, symbol, side, quantity, "RK018", "滑点后成交价非法（≤0）", day, limit_price
            )

        final_price = round_price(fill_price)
        fees = self.fee_model.calculate(side, final_price, quantity, self.fee_config)
        payload: dict[str, Any] = {
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "price": final_price,
            "quantity": quantity,
            "amount": FeeModel.amount(final_price, quantity),
            "commission": fees.commission,
            "stamp_tax": fees.stamp_tax,
            "transfer_fee": fees.transfer_fee,
            "reg_fee": fees.reg_fee,
            "fee_total": fees.total,
            "slippage_cost": round_money(
                abs(Decimal(str(final_price)) - Decimal(str(reference))) * quantity
            ),
            "trade_date": day,
            # Fill 为 core.models 模型（extra="allow"），以下为回测扩展字段：
            "reference_price": round_price(reference),
            "status": "FILLED",
        }
        if day is not None:
            payload["traded_at"] = datetime.combine(day, time(15, 0))
        return "fill", Fill(**payload)

    def _unfilled_or_expired(
        self,
        order: Any,
        order_id: str,
        symbol: str,
        side: str,
        quantity: int,
        price: float | None,
        day: date | None,
        reason_code: str,
        *,
        deferred: bool,
    ) -> tuple[str, Any]:
        """按已顺延天数决定“继续顺延”还是“作废（EXPIRED）”。"""

        if not deferred:
            return "reject", self._unfilled(order_id, symbol, side, quantity, reason_code, reason_message(reason_code), day, price)
        defer_count_raw = _first_field(order, ("defer_count", "defer_days", "deferred_days")) or 0
        try:
            defer_count = int(defer_count_raw)
        except (TypeError, ValueError):
            defer_count = 0
        if defer_count >= self.max_defer_days:
            message = f"{reason_message(reason_code)}（已顺延 {defer_count} 个交易日，委托作废）"
            return "expire", Unfilled(
                order_id=order_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                reason_code="EXPIRED",
                message=message,
                trade_date=day,
                price=price,
                deferred=False,
                expired=True,
            )
        return "defer", Unfilled(
            order_id=order_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            reason_code=reason_code,
            message=reason_message(reason_code),
            trade_date=day,
            price=price,
            deferred=True,
        )

    @staticmethod
    def _unfilled(
        order_id: str,
        symbol: str,
        side: str,
        quantity: int,
        reason_code: str,
        message: str,
        day: date | None = None,
        price: float | None = None,
    ) -> Unfilled:
        return Unfilled(
            order_id=order_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            reason_code=reason_code,
            message=message,
            trade_date=day,
            price=price,
        )


# ---------------------------------------------------------------------------
# 六、模块级便捷函数（与 AStockRules 静态方法同源，方便函数式调用）
# ---------------------------------------------------------------------------


def apply_slippage(
    base_price: float, side: str, slippage_bps: float = DEFAULT_SLIPPAGE_BPS
) -> float:
    """见 :meth:`AStockRules.apply_slippage`。"""

    return AStockRules.apply_slippage(base_price, side, slippage_bps)


def _bar_price(bar: Any, name: str) -> float | None:
    """从 bar（dict/对象/Series）读取单个价格，非法或缺失返回 None。"""

    candidates = {
        "open": ("open", "Open", "OPEN", "open_price"),
        "high": ("high", "High", "HIGH", "high_price"),
        "low": ("low", "Low", "LOW", "low_price"),
        "close": ("close", "Close", "CLOSE", "close_price"),
    }[name]
    raw = _first_field(bar, candidates)
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value

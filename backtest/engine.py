"""事件驱动回测引擎（任务 M1-11，契约 CF-5）。

口径来源：``docs/SPEC.md`` §4（A 股规则）、§4.6（复权口径）、§6.4（接口）、
§8.3（F3 回测规则）
依赖方向：``backtest`` 依赖 ``core`` + ``strategy``，**不依赖 ``trading``**（SPEC §3.1）；
撮合与费用**全部复用** :mod:`backtest.broker_sim`（唯一实现，CF-10），本模块不重复实现。

逐交易日循环（SPEC §8.3 规则细节 1）
------------------------------------
每个交易日 ``T`` 依次执行::

    1. 日初 T+1 结算：available_quantity = quantity，today_buy_quantity = 0
    2. 撮合 T-1 日收盘后挂出的委托：以 T 日开盘价 ± 滑点成交（SimBroker）
       ├─ 成交 → 更新现金/持仓/成交明细
       ├─ 顺延 → 保留到下一交易日（默认最多 1 日，之后 EXPIRED）
       └─ 拒单 → 丢弃并记录原因
    3. 按 T 日收盘价估值（停牌/缺数据标的沿用最近收盘价）
    4. 记录权益曲线（equity/cash/position_value/benchmark_close/drawdown）
    5. 构造 Context（bars 只含 ≤ T 日的历史，防未来函数）→ strategy.on_bar(ctx) → Signal
    6. 信号去重 + 单票目标仓位（``position_pct``）换算成数量 → 生成 T+1 待执行委托

价格口径（P0：撮合/手数/资金用**不复权**价）
--------------------------------------------
回测的**撮合、手数、资金核算**使用不复权价格（``BarCache.load(adjust="none")`` 或
数据源返回的不复权行情）——hfq 合成价（如 600519 约 12,000 元）会让 1 手成本虚高 8 倍，
导致最小手数与仓位判断全部失真。复权价仅用于收益展示/分红处理（见 ``TODO(M4)``）。

若数据源确实取不到不复权行情，引擎会回退到 ``adjust="hfq"`` 并：

* 给出 ``RQ_WARN_SYNTHETIC_PRICE`` 警告与中文说明；
* ``result.metadata["price_mode"] = "adjusted"``、``synthetic_prices=True``；
* **关闭最小 1 手规则**（合成价下的手数不代表真实交易）。

信号 → 委托的仓位约定
---------------------
``Signal`` 只有方向与强度、没有数量（契约 CF-3），数量由引擎统一换算：

* **买入**：目标市值 = ``账户总资产 × position_pct%``，与现有持仓的差额买入；
  再按整手/科创板 200 股起规则取整，并受可用现金（含当日卖出回款）约束；
* **最小 1 手规则（fail-safe）**：目标仓位金额 < 1 手成本，但 1 手成本
  ≤ ``max_position_per_symbol_pct% × 总资产``（默认 20%）→ **按 1 手买入**，并在
  ``warnings``（``RQ_WARN_POSITION_MIN_LOT``）与 ``notes`` 标注“已按最小 1 手调整”；
  若 1 手成本超过单票仓位上限 → 不交易，``notes`` 说明“资金不足，无法满足单票仓位上限”
  并建议提高本金/调低 ``position_pct``/选择低价标的；该上限**仅用于撮合层可买性判断**；
* **卖出**：清空该标的全部持仓（T+1 由下一交易日日初结算保证可卖）；
* ``position_pct`` 取策略参数（内置策略均声明该参数），缺失时用
  ``BacktestEngine(default_position_pct=10.0)``。

数据防御（P0：混源禁止回测、Mock 显式标注）
-------------------------------------------
* bars 含 ``source`` 列且同一 symbol 在区间内 source 不唯一 → **抛 ``BacktestError``**
  （“数据源混用，禁止回测”）；该检查是外部直接传 bars 时的第二道防线
  （数据层 ``BarCache.load`` 已做单源 fail-closed）；
* 全部来源为 mock → 允许但 ``result.is_mock=True``、``RQ_WARN_MOCK_DATA`` 警告、
  ``metadata`` 标注“离线 Mock 数据，仅供演示”（UI 需显示红色提示条）；
* 无 ``source`` 列时兜底检测异常价格跳变（相邻日涨幅 > 涨跌停幅度 ×3 且复权因子未变化）
  → ``RQ_WARN_PRICE_JUMP`` 警告 + notes 示例日期，提示疑似混源/未复权；
* **前收盘价 fail-closed**：缺少有效 ``prev_close`` 时不生成委托并在 notes 记录；
  SimBroker 亦会以 ``NO_PREV_CLOSE`` 拒绝成交（绝不静默跳过涨跌停校验）。

风控说明
--------
M1 回测**不经过** ``trading/risk.py`` 的 RiskGate（M1-11 只要求撮合规则正确），
因此不做“单笔金额上限 RK012 / 仓位上限”等风控拦截；单票仓位上限仅用于最小 1 手
可买性判断。风控口径由模拟盘（M2）验证。后续 ``TODO(M4)``：提供
``apply_risk_rules=True/False`` 开关（默认关）。

数据来源
--------
* 推荐：``run(..., bars=...)`` 直接注入行情（``DataFrame`` 或 ``{symbol: DataFrame}``）；
* 或构造 ``BacktestEngine(data_source=provider_or_cache)``，引擎按
  ``load(symbols, start, end, adjust="none")`` / ``get_bars(symbols, start, end, "1d")``
  约定取数；缓存类数据源优先不复权，失败才回退 hfq 并告警；
* 数据层未注入且未传 ``bars`` 时，尝试 ``data/cache.py::BarCache``，失败给出中文提示。
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from datetime import time as dt_time
from typing import Any, ClassVar

import numpy as np
import pandas as pd

try:  # pragma: no cover - 依赖环境
    from core.models import Account, Bar, Position
except ImportError as _core_exc:  # pragma: no cover
    raise ImportError(
        "回测引擎导入失败：无法从 core.models 导入 Account/Bar/Position（唯一来源）。"
        f"请先安装依赖：{_core_exc}"
    ) from _core_exc

from backtest.broker_sim import (
    BOARD_MAIN,
    BOARD_STAR,
    DEFAULT_MAX_DEFER_DAYS,
    DEFAULT_SLIPPAGE_BPS,
    AStockRules,
    FeeModel,
    SimBroker,
    guess_board,
    normalize_board,
    normalize_side,
    resolve_fee_config,
    round_money,
)
from backtest.metrics import compute_metrics
from strategy.base import Context, Strategy, StrategyError, get_strategy

logger = logging.getLogger(__name__)

__all__ = [
    "RQ_WARN_NO_ADJ",
    "RQ_WARN_ST_APPROX",
    "RQ_WARN_SHORT_SAMPLE",
    "RQ_WARN_FEW_TRADES",
    "RQ_WARN_HIGH_SUSPEND",
    "RQ_WARN_NO_BENCHMARK",
    "RQ_WARN_MOCK_DATA",
    "RQ_WARN_PRICE_JUMP",
    "RQ_WARN_POSITION_MIN_LOT",
    "RQ_WARN_SYNTHETIC_PRICE",
    "WARNING_MESSAGES",
    "BacktestError",
    "BacktestResult",
    "PendingOrder",
    "BacktestEngine",
    "run_backtest",
]

# ---------------------------------------------------------------------------
# 一、警告码（单一来源：backtest/warnings.py；本模块 re-export 以兼容旧导入）
# ---------------------------------------------------------------------------

from backtest.warnings import (  # noqa: E402  （放在 __all__ 之后，便于阅读）
    RQ_WARN_FEW_TRADES,
    RQ_WARN_HIGH_SUSPEND,
    RQ_WARN_MOCK_DATA,
    RQ_WARN_NO_ADJ,
    RQ_WARN_NO_BENCHMARK,
    RQ_WARN_POSITION_MIN_LOT,
    RQ_WARN_PRICE_JUMP,
    RQ_WARN_SHORT_SAMPLE,
    RQ_WARN_ST_APPROX,
    RQ_WARN_SYNTHETIC_PRICE,
    WARNING_MESSAGES,
)

#: 样本期下限（交易日，SPEC §8.3 防过拟合最低要求）
MIN_SAMPLE_DAYS = 250
#: 交易次数下限（SPEC §8.3）
MIN_TRADES = 30
#: 停牌占比告警阈值
HIGH_SUSPEND_RATIO = 0.05
#: 现金判定容差（元）
_CASH_EPS = 1e-6


class BacktestError(RuntimeError):
    """回测失败（``str(exc)`` 为中文可操作提示，UI 直接展示；SPEC §8.3 异常降级）。"""


# ---------------------------------------------------------------------------
# 二、数据结构
# ---------------------------------------------------------------------------


@dataclass
class PendingOrder:
    """待撮合委托（回测内部结构，字段符合 :class:`backtest.broker_sim.SimBroker` 的鸭子类型约定）。

    信号在 T 日收盘产生、T+1 开盘以市价（``price=None``）撮合；``defer_count`` 由引擎
    维护，顺延超过 ``max_defer_days`` 后由 SimBroker 判作 ``EXPIRED``。
    """

    id: str
    symbol: str
    side: str
    quantity: int
    price: float | None = None
    order_type: str = "MARKET"
    board: str = BOARD_MAIN
    is_st: bool = False
    prev_close: float | None = None
    defer_count: int = 0
    created_date: date | None = None
    reason: str = ""
    #: 生成委托时的参考价（当日收盘价；仅用于同一交易日内多笔买单的资金分配估算）
    est_price: float | None = None


@dataclass
class BacktestResult:
    """回测结果（SPEC §6.4：metrics/equity/trades/warnings/run_id）。

    Attributes:
        metrics: :func:`backtest.metrics.compute_metrics` 输出（键名冻结）。
        equity: 权益曲线，列 ``trade_date/equity/cash/position_value/benchmark_close/drawdown``；
            有基准时追加 ``benchmark_equity``（按初始资金归一）与 ``benchmark_return``。
        trades: 成交明细，列与 ``backtest_trades`` 表对齐（含各项费用与滑点成本）。
        warnings: 警告码列表（``RQ_WARN_*``，SPEC §9.2），UI 必须展示。
        notes: 补充说明（中文、非警告码），如“最后一日委托未及成交”。
        is_mock: 行情是否全部来自离线 Mock（UI 需显示红色“仅供演示”提示条）。
        metadata: 数据来源元信息（``is_mock/sources/price_mode/synthetic_prices/data_note``）。
    """

    run_id: str
    strategy_key: str
    params: dict[str, Any]
    symbols: list[str]
    start_date: date | None
    end_date: date | None
    initial_cash: float
    metrics: dict[str, Any]
    equity: pd.DataFrame
    trades: pd.DataFrame
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    fee_config: dict[str, Any] = field(default_factory=dict)
    benchmark: str | None = None
    duration_seconds: float = 0.0
    is_mock: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def final_equity(self) -> float:
        """期末权益（无权益记录时返回初始资金）。"""

        if self.equity is None or len(self.equity) == 0:
            return float(self.initial_cash)
        return float(self.equity.iloc[-1]["equity"])

    def summary(self) -> dict[str, Any]:
        """精简摘要（落库 ``backtest_runs`` / UI KPI 用，不含大表）。"""

        return {
            "run_id": self.run_id,
            "strategy_key": self.strategy_key,
            "params": dict(self.params),
            "symbols": list(self.symbols),
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "initial_cash": self.initial_cash,
            "final_equity": self.final_equity,
            "metrics": dict(self.metrics),
            "warnings": list(self.warnings),
            "notes": list(self.notes),
            "fee_config": dict(self.fee_config),
            "benchmark": self.benchmark,
            "duration_seconds": self.duration_seconds,
            "is_mock": self.is_mock,
            "metadata": dict(self.metadata),
        }

    def warning_messages(self) -> list[str]:
        """警告码对应的中文说明（UI 直接渲染）。"""

        return [WARNING_MESSAGES.get(code, code) for code in self.warnings]


# ---------------------------------------------------------------------------
# 三、行情清洗工具
# ---------------------------------------------------------------------------

#: 常见列名别名 -> 回测内部规范列名
_COLUMN_ALIASES: dict[str, str] = {
    "date": "date",
    "trade_date": "date",
    "tradedate": "date",
    "datetime": "date",
    "ts": "date",
    "symbol": "symbol",
    "code": "symbol",
    "ts_code": "symbol",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
    "vol": "volume",
    "amount": "amount",
    "turnover": "amount",
    "prev_close": "prev_close",
    "pre_close": "prev_close",
    "preclose": "prev_close",
    "is_suspended": "is_suspended",
    "suspended": "is_suspended",
    "adj_factor": "adj_factor",
    "adjfactor": "adj_factor",
    "board": "board",
    "is_st": "is_st",
    "isst": "is_st",
}

_REQUIRED_COLUMNS = ("date", "open", "high", "low", "close")
_OHLC_COLUMNS = ("open", "high", "low", "close")


@dataclass
class _PreparedData:
    """清洗后的行情数据（供逐日循环快速访问）。"""

    frames: dict[str, pd.DataFrame]
    bars_by_day: dict[str, dict[date, Bar]]
    date_pos: dict[str, dict[date, int]]
    calendar: list[date]
    boards: dict[str, str]
    warnings: set[str]
    notes: set[str]
    stats: dict[str, Any]
    #: 行情是否全部来自离线 Mock（供 result.is_mock 与 UI 红色提示条）
    is_mock: bool = False
    #: ``symbol -> source``（数据来源追溯；无 source 列时为空）
    sources: dict[str, str] = field(default_factory=dict)
    #: 行情是否为复权合成价（无法获取不复权数据时；此时关闭最小 1 手规则）
    synthetic_prices: bool = False


def _as_date(value: Any, *, name: str = "日期") -> date:
    """把 date/datetime/字符串统一成 :class:`datetime.date`（非法值给中文错误）。"""

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise BacktestError(f"{name}格式非法：{value!r}（应为 YYYY-MM-DD）") from exc
    if pd.isna(timestamp):
        raise BacktestError(f"{name}格式非法：{value!r}（应为 YYYY-MM-DD）")
    return timestamp.date()


def _canonicalize(frame: pd.DataFrame, *, where: str = "行情数据") -> pd.DataFrame:
    """规范列名（缺失必需列给中文错误）。"""

    if not isinstance(frame, pd.DataFrame):
        raise BacktestError(f"{where}必须是 pandas.DataFrame，收到 {type(frame).__name__}")
    renamed: dict[Any, str] = {}
    used: set[str] = set()
    for column in frame.columns:
        key = str(column).strip()
        canonical = _COLUMN_ALIASES.get(key.lower())
        if canonical and canonical not in used:
            renamed[column] = canonical
            used.add(canonical)
    result = frame.rename(columns=renamed).copy()
    missing = [name for name in _REQUIRED_COLUMNS if name not in result.columns]
    if missing:
        raise BacktestError(
            f"{where}缺少必需列 {missing}；需要 date/open/high/low/close"
            "（symbol 可放在列里或作为 dict 键）"
        )
    if "symbol" not in result.columns:
        result["symbol"] = ""
    return result


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _filter_date_range(frame: pd.DataFrame, begin: date, finish: date) -> pd.DataFrame:
    """按回测区间 ``[begin, finish]`` 过滤行情（含首尾）。"""

    if frame is None or len(frame) == 0:
        return frame
    if "date" not in frame.columns:
        return frame
    dates = pd.to_datetime(frame["date"], errors="coerce").dt.date
    keep = (dates >= begin) & (dates <= finish)
    return frame[keep.fillna(False)].copy()


def _attach_benchmark_columns(
    equity_frame: pd.DataFrame, benchmark_close: dict[date, float], initial_cash: float
) -> pd.DataFrame:
    """给权益曲线补基准列（M1-14 基准对比）。

    * ``benchmark_equity``：基准净值，按首个基准收盘价等比缩放到 ``initial_cash``
      （与组合权益同起点，便于同图对比）；
    * ``benchmark_return``：基准累计收益率（相对 ``initial_cash``）。

    基准值缺失时用前值填充；整列为空时保持原样。
    """

    if equity_frame is None or len(equity_frame) == 0:
        return equity_frame
    closes = pd.Series(
        [benchmark_close.get(day) for day in equity_frame["trade_date"]], dtype="float64"
    )
    if not closes.notna().any():
        return equity_frame
    first = float(closes.dropna().iloc[0])
    if not math.isfinite(first) or first <= 0:
        return equity_frame
    frame = equity_frame.copy()
    frame["benchmark_equity"] = (closes.ffill() * (float(initial_cash) / first)).round(2)
    frame["benchmark_return"] = frame["benchmark_equity"] / float(initial_cash) - 1.0
    return frame


def _board_of(symbol: str, board_value: Any = None) -> str:
    """确定板块（显式 board 优先，否则按代码推断；无法识别按主板处理）。"""

    try:
        return normalize_board(board_value, symbol=symbol)
    except ValueError:
        try:
            return guess_board(symbol)
        except ValueError:
            return BOARD_MAIN


# ---------------------------------------------------------------------------
# 四、回测引擎
# ---------------------------------------------------------------------------


class BacktestEngine:
    """事件驱动回测引擎（M1-11）。

    典型用法::

        from datetime import date
        from backtest.engine import BacktestEngine

        engine = BacktestEngine()
        result = engine.run(
            "ma_cross", {"fast": 5, "slow": 20, "position_pct": 10},
            symbols=["600519.SH", "000001.SZ"],
            start=date(2023, 1, 1), end=date(2025, 12, 31),
            bars=bars_df,          # 也可用 data_source 注入数据层（优先不复权）
            benchmark="000300.SH",
        )
        result.metrics["total_return"], result.warnings

    Args:
        data_source: 可选数据源（``DataProvider.get_bars`` / ``BarCache.load`` /
            ``callable(symbols, start, end) -> DataFrame``）。
        default_position_pct: 策略未声明 ``position_pct`` 时的单票目标仓位（%）。
        max_defer_days: 委托最多顺延交易日数（默认 1，SPEC §4.3）。
        max_position_per_symbol_pct: 单票仓位上限（%，默认 20，对应
            ``risk.max_position_per_symbol_pct``）。回测**不跑完整 RiskGate**，
            该上限仅用于“最小 1 手是否可买”的可买性判断。

    价格口径（``price_adjust``）：撮合/手数/资金核算用**不复权**价（``"none"``，默认）；
    若显式设为 ``"hfq"`` 或取不到不复权数据而回退复权价，则给出
    ``RQ_WARN_SYNTHETIC_PRICE`` 并**强制关闭最小 1 手规则**（详见模块 docstring）。
    """

    #: 回测中构造 Account/Position 的虚拟账户 ID
    ACCOUNT_ID: ClassVar[str] = "backtest"

    def __init__(
        self,
        data_source: Any = None,
        *,
        default_position_pct: float = 10.0,
        max_defer_days: int = DEFAULT_MAX_DEFER_DAYS,
        max_position_per_symbol_pct: float = 20.0,
        price_adjust: str | None = None,
    ) -> None:
        self.data_source = data_source
        self.default_position_pct = float(default_position_pct)
        if not math.isfinite(self.default_position_pct) or self.default_position_pct <= 0:
            raise BacktestError(f"default_position_pct 必须为正数，收到 {default_position_pct!r}")
        self.max_defer_days = max(0, int(max_defer_days))
        self.max_position_per_symbol_pct = float(max_position_per_symbol_pct)
        if (
            not math.isfinite(self.max_position_per_symbol_pct)
            or self.max_position_per_symbol_pct <= 0
            or self.max_position_per_symbol_pct > 100
        ):
            raise BacktestError(
                "max_position_per_symbol_pct 必须在 (0, 100] 之间，"
                f"收到 {max_position_per_symbol_pct!r}"
            )
        #: 价格口径：``none``=不复权（撮合/手数/资金默认口径）；``hfq``=复权合成价（仅展示/兼容）
        self.price_adjust = self._resolve_price_adjust(price_adjust)
        #: 最近一次取数的价格口径：raw（不复权）/ adjusted（复权合成）/ as_provided
        self._last_price_mode: str = "raw"

    @staticmethod
    def _resolve_price_adjust(value: str | None) -> str:
        """校验价格口径；None 时依次读 ``config.backtest.price_adjust`` → 默认 ``none``。"""

        if value is None:
            try:  # 配置键为可选（CF-9 冻结键之外的扩展，Dev1 后续可加入）；读不到用 none
                from core.config import get_config  # type: ignore

                config = get_config()
                candidate = getattr(getattr(config, "backtest", None), "price_adjust", None)
                if isinstance(candidate, str) and candidate.strip():
                    value = candidate
            except Exception:  # pragma: no cover - 配置不可用不影响回测
                value = None
        normalized = str(value or "none").strip().lower()
        if normalized in ("raw", "none", "不复权"):
            return "none"
        if normalized in ("hfq", "adjusted", "复权", "后复权"):
            return "hfq"
        raise BacktestError(f"price_adjust 仅支持 'none'（不复权，默认）或 'hfq'（复权），收到 {value!r}")

    # ------------------------------------------------------------------
    # 对外主接口（CF-5）
    # ------------------------------------------------------------------
    def run(
        self,
        strategy_key: str,
        params: dict[str, Any] | None,
        symbols: list[str],
        start: date,
        end: date,
        initial_cash: float = 100000.0,
        fee_config: Any = None,
        slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
        benchmark: str = "000300.SH",
        *,
        bars: Any = None,
        benchmark_bars: Any = None,
        run_id: str | None = None,
        price_adjust: str | None = None,
    ) -> BacktestResult:
        """运行一次回测（逐日循环、结果可复现）。

        Args:
            strategy_key: 注册表 key（:func:`strategy.base.get_strategy`）。
            params: 策略参数（经 ``Strategy.validate_params`` 校验并补默认值）。
            symbols: 标的池（至少 1 个）。
            start / end: 回测区间（含首尾）。
            initial_cash: 初始资金（元，默认 10 万，SPEC §7）。
            fee_config: 费率配置（``core.models.FeeConfig``/dict/None=默认）。
            slippage_bps: 滑点（基点，默认 5bp）。
            benchmark: 基准指数代码（默认 ``000300.SH``；数据缺失时曲线不画基准并提示）。
            bars: 行情数据（``DataFrame`` 或 ``{symbol: DataFrame}``）；优先于 data_source。
            benchmark_bars: 基准行情（``DataFrame``/``Series``，可选）。
            run_id: 指定 run_id（可复现/测试用；默认自动生成 UUID）。
            price_adjust: 价格口径覆盖（``None`` 用引擎设置）：``none``=不复权（撮合/手数/
                资金默认口径）；``hfq``=复权合成价（强制关闭最小 1 手并告警）。

        Returns:
            :class:`BacktestResult`。

        Raises:
            BacktestError: 参数非法、区间无数据、数据源混用、策略异常、指标计算失败
                （中文消息）。

        最小 1 手规则（fail-safe，解决高价股“买不进”）
        --------------------------------------------
        目标仓位金额 < 1 手成本时：

        * 若 1 手成本 ≤ ``max_position_per_symbol_pct% × 总资产``（默认 20%）→
          **按 1 手买入**，并在 ``warnings``（``RQ_WARN_POSITION_MIN_LOT``）与
          ``notes`` 标注“已按最小 1 手调整”；单票占比允许临时超过 ``position_pct``，
          但不会超过单票仓位上限；
        * 若 1 手成本 > 单票仓位上限 → 不交易，``notes`` 说明“资金不足，无法满足
          单票仓位上限”，并建议提高本金 / 调低 ``position_pct`` / 选择低价标的；
        * 若行情为复权合成价（``synthetic_prices``）→ **不启用**该规则，避免失真。
        """

        started_at = time.perf_counter()
        begin = _as_date(start, name="回测开始日期")
        finish = _as_date(end, name="回测结束日期")
        if begin > finish:
            raise BacktestError(f"回测开始日期 {begin} 不能晚于结束日期 {finish}")
        cash = _safe_float(initial_cash)
        if cash is None or cash <= 0:
            raise BacktestError(f"初始资金必须是正数，收到 {initial_cash!r}")
        symbol_list = [str(item).strip() for item in (symbols or []) if str(item).strip()]
        if not symbol_list:
            raise BacktestError("标的池不能为空：请至少选择 1 个标的")
        symbol_list = list(dict.fromkeys(symbol_list))  # 去重且保序（结果可复现）

        try:
            strategy_cls = get_strategy(strategy_key)
            normalized_params = strategy_cls.validate_params(params)
            strategy = strategy_cls(normalized_params)
        except StrategyError as exc:
            raise BacktestError(f"回测启动失败（策略 {strategy_key}）：{exc}") from exc

        fees = resolve_fee_config(fee_config)
        slippage = self._validate_slippage(slippage_bps)

        # 1) 装载并清洗行情（按回测区间过滤；价格口径见模块 docstring）
        self._active_price_adjust = self._resolve_price_adjust(
            price_adjust if price_adjust is not None else self.price_adjust
        )
        self._last_price_mode = "raw"
        raw = self._load_bars(symbol_list, begin, finish, bars=bars)
        raw = _filter_date_range(raw, begin, finish)
        prepared = self._prepare_frames(raw, symbol_list)
        self._apply_price_mode(prepared)
        if not prepared.frames:
            raise BacktestError(
                f"回测区间 {begin} ~ {finish} 内没有可用行情数据；请先拉取数据或检查标的代码"
            )
        if not prepared.calendar:
            raise BacktestError(f"回测区间 {begin} ~ {finish} 内没有交易日")

        broker = SimBroker(fee_config=fees, slippage_bps=slippage, max_defer_days=self.max_defer_days)
        fee_model = FeeModel(fees)

        # 2) 账户与状态（T+1 语义见 SPEC §4.2）
        account = Account(
            id=self.ACCOUNT_ID,
            name="回测账户",
            initial_cash=float(cash),
            cash=float(cash),
            total_asset=float(cash),
        )
        positions: dict[str, Position] = {}
        pending: list[PendingOrder] = []
        trade_rows: list[dict[str, Any]] = []
        equity_rows: list[dict[str, Any]] = []
        warning_set = set(prepared.warnings)
        note_set = set(prepared.notes)
        metadata: dict[str, Any] = {
            "is_mock": prepared.is_mock,
            "sources": dict(prepared.sources),
            "price_mode": self._last_price_mode,
            "synthetic_prices": bool(prepared.synthetic_prices),
            "price_jumps": int(prepared.stats.get("price_jumps", 0) or 0),
        }
        if prepared.is_mock:
            metadata["data_note"] = "离线 Mock 数据，仅供演示，不代表真实市场表现"
        elif prepared.synthetic_prices:
            metadata["data_note"] = "复权合成价格（未取到不复权数据），手数/资金不代表真实交易"
        elif prepared.sources:
            metadata["data_note"] = f"行情来源：{'、'.join(sorted(set(prepared.sources.values())))}"
        else:
            metadata["data_note"] = "行情未标注 source（按未知来源处理）"

        benchmark_close = self._load_benchmark(benchmark, benchmark_bars, prepared)
        if benchmark_close is None:
            note_set.add("基准数据缺失：权益曲线未绘制基准（不影响组合指标）")

        # 3) 策略初始化（防未来函数：开局不提供任何行情）
        try:
            strategy.on_init(
                Context(
                    now=datetime.combine(prepared.calendar[0], dt_time(9, 0)),
                    bars={},
                    positions={},
                    account=account,
                    params=dict(strategy.params),
                    is_backtest=True,
                )
            )
        except Exception as exc:
            raise BacktestError(f"策略 {strategy_key} on_init 执行失败：{exc}") from exc

        order_seq = 0
        #: 已提示过的（类型:标的）提示，避免逐日刷屏
        hint_dedup: set[str] = set()
        # TODO(M4): 回测暂不接 RiskGate（RK012 单笔金额上限 / 仓位上限 / 熔断等）。
        # 后续提供 `apply_risk_rules: bool = False` 开关（默认关，保持 M1 现状），
        # 打开后买单需过风控；卖出豁免单笔金额上限。见 docs/TASKS.md M4。
        # 4) 逐交易日循环
        for day in prepared.calendar:
            # 4.1 日初 T+1 结算（SPEC §4.2）
            for position in positions.values():
                position.available_quantity = int(position.quantity)
                position.today_buy_quantity = 0
                position.frozen_quantity = 0

            # 4.2 撮合前一日挂单（T+1 开盘价 ± 滑点）；卖单优先以释放资金
            day_bars = {
                symbol: prepared.bars_by_day[symbol][day]
                for symbol in prepared.frames
                if day in prepared.bars_by_day.get(symbol, {})
                and not prepared.bars_by_day[symbol][day].is_suspended
            }
            ordered_pending = sorted(pending, key=lambda order: (0 if order.side == "SELL" else 1, order.id))
            pending_by_id = {order.id: order for order in ordered_pending}
            matched = broker.match_with_report(ordered_pending, day_bars, day, positions=positions)
            note_set.update(matched.warnings)

            for fill in matched.fills:
                if not self._apply_fill(fill, positions, account, trade_rows):
                    note_set.add(
                        f"{getattr(fill, 'symbol', '')} {day} 的成交因资金/持仓不足被丢弃"
                        "（跳空或费用导致，回测按保守口径处理）"
                    )
            pending = []
            for item in matched.deferred:
                original = pending_by_id.get(item.order_id)
                if original is not None:
                    original.defer_count += 1
                    pending.append(original)
            for item in matched.expired:
                note_set.add(f"{item.symbol} 委托作废（{item.reason_code}）：{item.message}")
            for item in matched.rejected:
                note_set.add(f"{item.symbol} 委托被拒（{item.reason_code}）：{item.message}")

            # 4.3 收盘估值 + 权益曲线
            position_value = self._mark_to_market(positions, prepared, day)
            total_asset = round_money(account.cash + position_value)
            account.total_asset = total_asset
            equity_rows.append(
                {
                    "trade_date": day,
                    "equity": total_asset,
                    "cash": round_money(account.cash),
                    "position_value": position_value,
                    "benchmark_close": benchmark_close.get(day) if benchmark_close else None,
                }
            )

            # 4.4 策略信号（bars 只含 ≤ day 的历史，防未来函数）
            ctx_bars = {
                symbol: prepared.frames[symbol].iloc[: prepared.date_pos[symbol][day] + 1]
                for symbol in prepared.frames
                if day in prepared.date_pos.get(symbol, {})
            }
            ctx = Context(
                now=datetime.combine(day, dt_time(15, 0)),
                bars=ctx_bars,
                positions=dict(positions),
                account=account,
                params=dict(strategy.params),
                is_backtest=True,
            )
            try:
                signals = strategy.on_bar(ctx)
            except Exception as exc:
                raise BacktestError(
                    f"策略 {strategy_key} 在 {day} 的 on_bar 执行失败，回测已中止：{exc}"
                ) from exc
            if signals is None:
                signals = []
            elif not isinstance(signals, (list, tuple)):
                signals = [signals]

            # 4.5 信号去重 → 生成 T+1 委托
            budget = round_money(
                account.cash + self._estimate_pending_sell_value(pending, prepared, day)
            )
            pending_symbols = {order.symbol for order in pending}
            seen: set[tuple[str, str]] = set()
            for signal in signals:
                try:
                    symbol = str(getattr(signal, "symbol", "") or "").strip()
                    side = normalize_side(getattr(signal, "side", None))
                except (TypeError, ValueError) as exc:
                    note_set.add(f"策略返回了非法信号（已忽略）：{exc}")
                    continue
                if not symbol or symbol not in prepared.frames:
                    continue
                if day not in prepared.date_pos.get(symbol, {}):
                    continue
                if (symbol, side) in seen:
                    continue
                seen.add((symbol, side))
                if symbol in pending_symbols:
                    continue
                bar = prepared.bars_by_day.get(symbol, {}).get(day)
                if bar is None or bar.is_suspended:
                    continue
                # 前收盘价 fail-closed：无法计算涨跌停时不生成委托（P1 口径）
                if _safe_float(getattr(bar, "prev_close", None)) is None:
                    note_set.add(f"{symbol} {day} 缺少前收盘价，按 fail-closed 不生成委托")
                    continue
                try:
                    if not strategy.confirm_signal(signal, ctx):
                        continue
                except Exception as exc:  # pragma: no cover - 策略自定义钩子异常
                    note_set.add(f"策略 confirm_signal 异常（已忽略该信号）：{exc}")
                    continue

                order_seq += 1
                order_id = f"BT-{order_seq:06d}"
                if side == "BUY":
                    order, buy_note = self._build_buy_order(
                        order_id,
                        symbol,
                        bar,
                        ctx,
                        positions,
                        account,
                        prepared.boards.get(symbol, BOARD_MAIN),
                        strategy,
                        budget,
                        fee_model,
                        slippage,
                        day,
                        min_lot_enabled=not prepared.synthetic_prices,
                    )
                    if order is not None:
                        budget = round_money(
                            budget - self._estimate_order_cost(order, fee_model, slippage)
                        )
                    if buy_note:
                        kind = str(buy_note.get("kind", "") or "")
                        if kind == "min_lot":
                            warning_set.add(RQ_WARN_POSITION_MIN_LOT)
                        dedupe_key = f"{kind}:{symbol}"
                        if dedupe_key not in hint_dedup:
                            hint_dedup.add(dedupe_key)
                            note_set.add(str(buy_note.get("message", "") or ""))
                else:
                    order = self._build_sell_order(
                        order_id,
                        symbol,
                        bar,
                        positions,
                        prepared.boards.get(symbol, BOARD_MAIN),
                        day,
                    )
                    if order is not None:
                        reference = _safe_float(ctx.close(symbol)) or float(bar.close)
                        budget = round_money(budget + reference * order.quantity * 0.99)
                if order is not None:
                    order.reason = str(getattr(signal, "reason", "") or "")
                    pending.append(order)
                    pending_symbols.add(symbol)

        # 5) 收尾：最后一日委托、权益曲线、基准、指标
        if pending:
            note_set.add(
                f"回测最后交易日生成的 {len(pending)} 笔委托在区间内无法成交（T+1 撮合特性），已忽略"
            )
        equity_frame = self._build_equity_frame(equity_rows)
        trades_frame = self._build_trades_frame(trade_rows)
        if equity_frame is None or len(equity_frame) == 0:
            raise BacktestError("回测未产生权益记录：请检查行情数据与策略配置")

        benchmark_for_metrics = None
        if benchmark_close:
            equity_frame = _attach_benchmark_columns(equity_frame, benchmark_close, float(cash))
            values = [benchmark_close.get(row["trade_date"]) for row in equity_rows]
            if any(value is not None for value in values):
                series = pd.Series(
                    values, index=[row["trade_date"] for row in equity_rows], dtype="float64"
                )
                benchmark_for_metrics = series.ffill()
        else:
            warning_set.add(RQ_WARN_NO_BENCHMARK)
        try:
            metrics = compute_metrics(
                equity_frame, trades_frame, benchmark_for_metrics, initial_cash=float(cash)
            )
        except Exception as exc:
            raise BacktestError(f"绩效指标计算失败：{exc}") from exc

        warnings = self._collect_warnings(warning_set, prepared.stats, trades_frame)
        result = BacktestResult(
            run_id=run_id or uuid.uuid4().hex,
            strategy_key=strategy_key,
            params=dict(normalized_params),
            symbols=list(symbol_list),
            start_date=prepared.calendar[0],
            end_date=prepared.calendar[-1],
            initial_cash=float(cash),
            metrics=metrics,
            equity=equity_frame,
            trades=trades_frame,
            warnings=warnings,
            notes=sorted(note_set),
            fee_config=fees.model_dump(),
            benchmark=benchmark,
            duration_seconds=round(time.perf_counter() - started_at, 4),
            is_mock=prepared.is_mock,
            metadata=metadata,
        )
        logger.info(
            "回测完成：策略=%s 区间=%s~%s 标的=%d 交易=%d 收益率=%s mock=%s price_mode=%s 耗时=%.2fs",
            strategy_key,
            result.start_date,
            result.end_date,
            len(prepared.frames),
            len(trades_frame),
            metrics.get("total_return"),
            prepared.is_mock,
            self._last_price_mode,
            result.duration_seconds,
        )
        return result

    # ------------------------------------------------------------------
    # 数据装载与清洗
    # ------------------------------------------------------------------
    def prepare_market_data(
        self,
        symbols: list[str],
        start: date,
        end: date,
        *,
        bars: Any = None,
        price_adjust: str | None = None,
    ) -> _PreparedData:
        """装载并清洗行情（供推荐引擎等复用同一套口径，不跑回测）。

        与 :meth:`run` 使用完全相同的取数/清洗/板块识别/数据防御逻辑；只读、无副作用。
        价格口径默认沿用引擎设置（``none``=不复权）。
        """

        begin = _as_date(start, name="数据开始日期")
        finish = _as_date(end, name="数据结束日期")
        if begin > finish:
            raise BacktestError(f"数据开始日期 {begin} 不能晚于结束日期 {finish}")
        self._active_price_adjust = self._resolve_price_adjust(
            price_adjust if price_adjust is not None else self.price_adjust
        )
        self._last_price_mode = "raw"
        raw = self._load_bars(list(map(str, symbols)), begin, finish, bars=bars)
        raw = _filter_date_range(raw, begin, finish)
        prepared = self._prepare_frames(raw, list(map(str, symbols)))
        self._apply_price_mode(prepared)
        return prepared

    def _apply_price_mode(self, prepared: _PreparedData) -> None:
        """把复权合成价标记写进 prepared（警告 + notes + 关闭最小 1 手规则）。"""

        if self._last_price_mode != "adjusted":
            return
        prepared.synthetic_prices = True
        prepared.warnings.add(RQ_WARN_SYNTHETIC_PRICE)
        prepared.notes.add(
            "未能获取不复权行情，使用复权合成价格回测：手数/资金不代表真实交易，"
            "已关闭最小 1 手规则（建议用 adjust='none' 数据重跑）"
        )

    @staticmethod
    def _validate_slippage(value: Any) -> float:
        try:
            slippage = float(value)
        except (TypeError, ValueError) as exc:
            raise BacktestError(f"滑点必须是数值，收到 {value!r}") from exc
        if not math.isfinite(slippage) or slippage < 0:
            raise BacktestError(f"滑点必须是非负数值，收到 {value!r}")
        return slippage

    def _load_bars(
        self, symbols: list[str], start: date, end: date, *, bars: Any
    ) -> pd.DataFrame:
        """装载行情成规范 DataFrame（含 symbol/date/OHLCV 等列）。"""

        if bars is not None:
            self._last_price_mode = "as_provided"
            if isinstance(bars, pd.DataFrame):
                canonical = _canonicalize(bars)
                if "symbol" not in bars.columns:
                    if len(symbols) != 1:
                        raise BacktestError(
                            "传入的 bars 缺少 symbol 列，但标的池有多个标的；"
                            "请改用 {symbol: DataFrame} 字典，或在 DataFrame 中加 symbol 列"
                        )
                    canonical["symbol"] = symbols[0]
                return canonical
            if isinstance(bars, dict):
                parts: list[pd.DataFrame] = []
                for symbol, frame in bars.items():
                    if frame is None or len(frame) == 0:
                        continue
                    canonical = _canonicalize(frame, where=f"标的 {symbol} 的行情")
                    canonical["symbol"] = str(symbol)
                    parts.append(canonical)
                return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
            raise BacktestError(
                f"bars 参数格式不支持（{type(bars).__name__}）；请传 DataFrame 或 {{symbol: DataFrame}}"
            )
        return self._load_from_data_source(symbols, start, end)

    def _load_from_data_source(self, symbols: list[str], start: date, end: date) -> pd.DataFrame:
        """用注入的数据源取数（兼容 DataProvider / BarCache / callable 三种约定）。

        **价格口径**（``self._active_price_adjust``）：

        * ``none``（默认）：``load(adjust="none")`` 不复权价（撮合/手数/资金口径）；
          不复权为空时回退 ``hfq`` 并标记 ``adjusted``（触发合成价警告 + 关闭最小 1 手）；
        * ``hfq``：显式使用复权合成价（仅兼容/展示场景），同样标记 ``adjusted`` 并告警。
        """

        self._last_price_mode = "raw"
        requested = str(getattr(self, "_active_price_adjust", self.price_adjust) or "none")
        source = self.data_source
        if source is None:
            try:  # 数据层可能尚未交付：延迟导入并给出可操作提示
                from data.cache import BarCache  # type: ignore

                source = BarCache()
            except Exception as exc:
                raise BacktestError(
                    "回测数据源不可用：请通过 `BacktestEngine(data_source=...)` 注入，"
                    "或在 `run(..., bars=...)` 直接传入行情 DataFrame；"
                    f"尝试加载数据层 data/cache.py 失败：{exc}"
                ) from exc
        try:
            if hasattr(source, "load") and not hasattr(source, "get_bars"):
                frame = self._load_from_cache_like(source, symbols, start, end, requested)
            elif hasattr(source, "get_bars"):
                # DataProvider 约定默认返回不复权行情（无 adjust 参数，见 SPEC §6.1）
                frame = source.get_bars(symbols, start, end, "1d")
                if requested == "hfq":
                    self._last_price_mode = "adjusted"
            elif callable(source):
                frame = source(symbols, start, end)
            else:
                raise BacktestError(
                    "data_source 必须提供 get_bars(...) 或 load(...) 方法，或本身是 callable"
                )
        except BacktestError:
            raise
        except Exception as exc:
            raise BacktestError(f"数据源取数失败（{type(source).__name__}）：{exc}") from exc
        if frame is None or (isinstance(frame, pd.DataFrame) and frame.empty):
            raise BacktestError(
                f"数据源未返回 {start} ~ {finish_display(start, end)} 的行情数据；请先拉取并缓存数据"
            )
        return _canonicalize(frame, where="数据源返回的行情")

    def _load_from_cache_like(
        self, source: Any, symbols: list[str], start: date, end: date, requested: str = "none"
    ) -> pd.DataFrame:
        """缓存类数据源：按请求口径取价；复权合成价标记 ``adjusted``。"""

        if requested == "hfq":
            try:
                frame = source.load(symbols, start, end, adjust="hfq")
            except TypeError:  # 签名兼容：不支持 adjust 关键字
                frame = source.load(symbols, start, end)
            except Exception as exc:
                raise BacktestError(
                    f"数据源取数失败（{type(source).__name__}）：{exc}"
                ) from exc
            if frame is not None and isinstance(frame, pd.DataFrame) and len(frame) > 0:
                self._last_price_mode = "adjusted"
                return frame
            return pd.DataFrame()

        frame: pd.DataFrame | None = None
        try:
            frame = source.load(symbols, start, end, adjust="none")
        except TypeError:  # 签名兼容：不支持 adjust 关键字
            try:
                frame = source.load(symbols, start, end)
            except Exception as exc:
                raise BacktestError(f"数据源取数失败（{type(source).__name__}）：{exc}") from exc
        except Exception as exc:
            # 数据层 fail-closed（如混源 DataIntegrityError）必须原样传递中文原因
            raise BacktestError(
                f"数据层拒绝返回行情（fail-closed，{type(source).__name__}）：{exc}"
            ) from exc
        if frame is not None and isinstance(frame, pd.DataFrame) and len(frame) > 0:
            self._last_price_mode = "raw"
            return frame
        # 不复权无数据 → 回退后复权合成价（关闭最小 1 手规则并告警）
        try:
            fallback = source.load(symbols, start, end, adjust="hfq")
        except TypeError:
            return pd.DataFrame()
        except Exception as exc:
            raise BacktestError(
                f"数据源取数失败（{type(source).__name__}）：{exc}"
            ) from exc
        if fallback is not None and isinstance(fallback, pd.DataFrame) and len(fallback) > 0:
            self._last_price_mode = "adjusted"
            return fallback
        return pd.DataFrame()

    def _prepare_frames(self, raw: pd.DataFrame, symbols: list[str]) -> _PreparedData:
        """清洗/排序行情，预生成 Bar 对象与日期索引；含数据源一致性/异常跳变检查。"""

        empty = _PreparedData({}, {}, {}, [], {}, set(), set(), {})
        if raw is None or len(raw) == 0:
            return empty

        frame = raw.copy()
        frame["symbol"] = frame["symbol"].astype(str).str.strip()
        frame = frame[frame["symbol"].isin(set(symbols))]
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
        frame = frame.dropna(subset=["date", "symbol"])
        if frame.empty:
            return empty

        for column, default in (("volume", 0.0), ("amount", 0.0)):
            if column not in frame.columns:
                frame[column] = default
            frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
        for column in _OHLC_COLUMNS:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        if "is_suspended" not in frame.columns:
            frame["is_suspended"] = False
        frame["is_suspended"] = frame["is_suspended"].fillna(False).astype(bool)
        has_adj = "adj_factor" in frame.columns and frame["adj_factor"].notna().any()
        has_st = "is_st" in frame.columns and frame["is_st"].notna().any()

        warnings: set[str] = set()
        notes: set[str] = set()
        if not has_adj:
            warnings.add(RQ_WARN_NO_ADJ)
            notes.add("行情未提供复权因子 adj_factor，按不复权价格回测（SPEC §4.6）")
        if not has_st:
            warnings.add(RQ_WARN_ST_APPROX)
        stats: dict[str, Any] = {"bars": int(len(frame)), "suspended": 0}

        # ---- 数据源一致性检查（P0：混源禁止回测；全 Mock 只允许演示） ----
        sources_map: dict[str, str] = {}
        is_mock = False
        if "source" in frame.columns:
            mock_aliases = {"mock", "mockprovider", "mock_provider", "mock数据", "演示", "离线mock"}
            all_sources: set[str] = set()
            for symbol_raw, group in frame.groupby("symbol", sort=True):
                symbol = str(symbol_raw)
                raw_values = [
                    str(value).strip()
                    for value in group["source"].dropna().tolist()
                    if str(value).strip()
                ]
                unique = {value.lower() for value in raw_values}
                if not unique:
                    continue
                if len(unique) > 1:
                    detail = "、".join(sorted(unique))
                    raise BacktestError(
                        f"数据源混用，禁止回测：标的 {symbol} 在所选区间内同时存在多个数据源"
                        f"（{detail}）。混源价格序列不可比，会算出虚假收益；"
                        "请先在数据层统一来源（或只回测单一来源区间）后重试。"
                    )
                value = sorted(unique)[0]
                sources_map[symbol] = value
                all_sources.add(value)
            if all_sources and all(
                value in mock_aliases or value.startswith("mock") for value in all_sources
            ):
                is_mock = True
                warnings.add(RQ_WARN_MOCK_DATA)
                notes.add("行情全部来自离线 Mock 示例数据，仅供演示（不代表真实市场表现）")
            elif all_sources:
                notes.add(f"行情数据源：{'、'.join(sorted(all_sources))}")
            else:
                notes.add("行情 source 列存在但全为空值，按未知来源处理（跳过数据源一致性检查）")

        frames: dict[str, pd.DataFrame] = {}
        bars_by_day: dict[str, dict[date, Bar]] = {}
        date_pos: dict[str, dict[date, int]] = {}
        boards: dict[str, str] = {}
        dropped: list[str] = []

        for symbol_raw, group in frame.groupby("symbol", sort=True):
            symbol = str(symbol_raw)
            group = group.sort_values("date", kind="mergesort").reset_index(drop=True)
            group = group.drop_duplicates(subset=["date"], keep="last").reset_index(drop=True)
            if group.empty:
                dropped.append(symbol)
                continue
            valid_price = (
                group[list(_OHLC_COLUMNS)].notna().all(axis=1)
                & (group["close"] > 0)
                & (group["open"] > 0)
            )
            group["is_suspended"] = group["is_suspended"] | ~valid_price
            group["close"] = group["close"].ffill()
            group = group[group["close"].notna()].reset_index(drop=True)
            if group.empty:
                dropped.append(symbol)
                continue
            for column in ("open", "high", "low"):
                group[column] = group[column].fillna(group["close"])
            derived_prev = group["close"].shift(1)
            if "prev_close" in group.columns and group["prev_close"].notna().any():
                provided = pd.to_numeric(group["prev_close"], errors="coerce")
                group["prev_close"] = provided.where(provided > 0, derived_prev)
            else:
                group["prev_close"] = derived_prev
            board_value = None
            if "board" in group.columns and group["board"].notna().any():
                board_value = str(group["board"].dropna().iloc[0]).strip()
            board = _board_of(symbol, board_value)
            if board_value:
                try:
                    board = normalize_board(board_value, symbol=symbol)
                except ValueError as exc:
                    notes.add(f"标的 {symbol} 板块无法识别（{exc}），按沪深主板规则回测")
            group["board"] = board
            if "is_st" in group.columns:
                group["is_st"] = group["is_st"].fillna(False).astype(bool)
            else:
                group["is_st"] = False
            group["symbol"] = symbol

            # ---- 价格异常跳变兜底检测（无 source 列时识别疑似混源/未复权） ----
            try:
                closes = group["close"].to_numpy(dtype="float64")
                prev_closes = group["prev_close"].to_numpy(dtype="float64")
                adj_values = (
                    pd.to_numeric(group["adj_factor"], errors="coerce").to_numpy(dtype="float64")
                    if has_adj
                    else None
                )
                is_st_first = bool(group["is_st"].iloc[0]) if len(group) else False
                threshold = max(AStockRules.limit_pct(board, is_st_first) * 3.0, 0.30)
                jump_count = 0
                examples: list[str] = []
                for index in range(1, len(closes)):
                    previous = prev_closes[index]
                    if not math.isfinite(previous) or previous <= 0:
                        continue
                    daily_return = closes[index] / previous - 1.0
                    if not math.isfinite(daily_return) or abs(daily_return) <= threshold:
                        continue
                    # 复权因子变化视为除权除息，不计入异常跳变
                    if (
                        adj_values is not None
                        and math.isfinite(adj_values[index])
                        and math.isfinite(adj_values[index - 1])
                        and abs(adj_values[index] - adj_values[index - 1]) > 1e-9
                    ):
                        continue
                    jump_count += 1
                    if len(examples) < 3:
                        examples.append(f"{group['date'].iloc[index]} {daily_return * 100:+.1f}%")
                if jump_count:
                    warnings.add(RQ_WARN_PRICE_JUMP)
                    stats["price_jumps"] = int(stats.get("price_jumps", 0)) + jump_count
                    notes.add(
                        f"{symbol} 存在 {jump_count} 处异常价格跳变（阈值 ±{threshold * 100:.0f}%），"
                        f"示例：{'、'.join(examples)}；疑似数据源混用/未复权，建议修复数据层后重跑"
                    )
            except Exception:  # pragma: no cover - 检测本身不得影响回测
                pass

            frames[symbol] = group
            boards[symbol] = board
            date_pos[symbol] = {value: idx for idx, value in enumerate(group["date"].tolist())}
            bar_map: dict[date, Bar] = {}
            for idx in range(len(group)):
                row = group.iloc[idx]
                try:
                    bar = Bar(
                        symbol=symbol,
                        trade_date=row["date"],
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]),
                        amount=float(row["amount"]),
                        adj_factor=_safe_float(row["adj_factor"]) if has_adj else None,
                        is_suspended=bool(row["is_suspended"]),
                        prev_close=_safe_float(row["prev_close"]),
                        board=board,
                        is_st=bool(row["is_st"]),
                    )
                except Exception:  # pragma: no cover - 极端脏数据：跳过该 bar
                    continue
                bar_map[bar.trade_date] = bar
            bars_by_day[symbol] = bar_map
            stats["suspended"] += int(group["is_suspended"].sum())

        if dropped:
            notes.add(f"以下标的没有可用行情，已跳过：{'、'.join(dropped)}")
        calendar = sorted({value for mapping in date_pos.values() for value in mapping})
        stats["calendar_days"] = len(calendar)
        return _PreparedData(
            frames,
            bars_by_day,
            date_pos,
            calendar,
            boards,
            warnings,
            notes,
            stats,
            is_mock=is_mock,
            sources=sources_map,
        )

    def _load_benchmark(
        self, benchmark: str | None, benchmark_bars: Any, prepared: _PreparedData
    ) -> dict[date, float] | None:
        """准备基准收盘价（``date -> close``）；缺失或无法解析返回 None。"""

        values: dict[date, float] = {}
        if benchmark_bars is not None:
            try:
                if isinstance(benchmark_bars, pd.Series):
                    days = [_as_date(item, name="基准日期") for item in benchmark_bars.index]
                    numbers = pd.to_numeric(benchmark_bars, errors="coerce").to_numpy(dtype="float64")
                    for day, value in zip(days, numbers):
                        if math.isfinite(float(value)):
                            values[day] = float(value)
                elif isinstance(benchmark_bars, pd.DataFrame):
                    # 基准数据只需 date + close（不要求 OHLC），单独做列名规范化
                    candidate = benchmark_bars
                    if not any(
                        str(name).lower() in ("date", "trade_date", "tradedate")
                        for name in candidate.columns
                    ):
                        candidate = candidate.copy()
                        candidate["date"] = list(candidate.index)
                    renamed: dict[Any, str] = {}
                    for column in candidate.columns:
                        key = str(column).strip().lower()
                        if key in ("date", "trade_date", "tradedate", "datetime", "ts"):
                            renamed[column] = "date"
                        elif key in ("close", "benchmark_close", "price", "nav", "value"):
                            renamed[column] = "close"
                    canonical = candidate.rename(columns=renamed).copy()
                    if not {"date", "close"} <= set(canonical.columns):
                        raise ValueError("基准行情需要 date + close 两列")
                    canonical["date"] = pd.to_datetime(canonical["date"], errors="coerce").dt.date
                    for _, row in canonical.iterrows():
                        if pd.isna(row["date"]):
                            continue
                        close = _safe_float(row["close"])
                        if close is not None:
                            values[row["date"]] = close
                else:
                    raise TypeError("基准行情必须是 DataFrame 或 Series")
            except Exception as exc:
                logger.warning("基准行情解析失败，忽略基准：%s", exc)
                return None
        elif benchmark and benchmark in prepared.frames:
            for _, row in prepared.frames[benchmark].iterrows():
                close = _safe_float(row["close"])
                if close is not None:
                    values[row["date"]] = close
        if not values:
            return None
        ordered: dict[date, float] = {}
        last: float | None = None
        for day in prepared.calendar:
            if day in values:
                last = values[day]
            if last is not None:
                ordered[day] = last
        return ordered or None

    # ------------------------------------------------------------------
    # 资金与持仓
    # ------------------------------------------------------------------
    def _apply_fill(
        self,
        fill: Any,
        positions: dict[str, Position],
        account: Account,
        trade_rows: list[dict[str, Any]],
    ) -> bool:
        """把一笔成交应用到账户（返回 False 表示因资金/持仓不足被丢弃）。"""

        symbol = str(getattr(fill, "symbol", "") or "")
        try:
            side = normalize_side(getattr(fill, "side", None))
            price = float(getattr(fill, "price"))
            quantity = int(getattr(fill, "quantity"))
            amount = round_money(float(getattr(fill, "amount", price * quantity)))
            fee_total = round_money(float(getattr(fill, "fee_total", 0.0)))
            slippage_cost = round_money(float(getattr(fill, "slippage_cost", 0.0)))
        except (TypeError, ValueError) as exc:
            logger.warning("成交数据非法，已丢弃：%s", exc)
            return False
        if not symbol or quantity <= 0 or price <= 0:
            return False
        position = positions.get(symbol)
        if position is None:
            position = Position(account_id=self.ACCOUNT_ID, symbol=symbol)

        if side == "BUY":
            cost = round_money(amount + fee_total)
            if cost > round_money(account.cash) + _CASH_EPS:
                logger.info(
                    "%s 买入成交被丢弃：需要 %.2f 元（含费用），可用现金 %.2f 元",
                    symbol,
                    cost,
                    account.cash,
                )
                return False
            account.cash = round_money(account.cash - cost)
            total_cost = round_money(position.avg_cost * position.quantity + amount + fee_total)
            position.quantity = int(position.quantity + quantity)
            position.avg_cost = round(total_cost / position.quantity, 4) if position.quantity else 0.0
            position.today_buy_quantity = int(position.today_buy_quantity + quantity)
        else:
            if quantity > int(position.quantity):
                logger.info("%s 卖出成交被丢弃：持仓 %s 股不足 %s 股", symbol, position.quantity, quantity)
                return False
            account.cash = round_money(account.cash + amount - fee_total)
            position.quantity = int(position.quantity - quantity)
            position.available_quantity = max(0, int(position.available_quantity) - quantity)
            if position.quantity == 0:
                position.avg_cost = 0.0
        position.last_price = round(price, 4)
        position.market_value = round_money(position.last_price * position.quantity)
        if position.quantity > 0:
            positions[symbol] = position
        else:
            positions.pop(symbol, None)

        trade_rows.append(
            {
                "trade_date": getattr(fill, "trade_date", None),
                "symbol": symbol,
                "side": side,
                "price": price,
                "quantity": quantity,
                "amount": amount,
                "commission": round_money(float(getattr(fill, "commission", 0.0))),
                "stamp_tax": round_money(float(getattr(fill, "stamp_tax", 0.0))),
                "transfer_fee": round_money(float(getattr(fill, "transfer_fee", 0.0))),
                "reg_fee": round_money(float(getattr(fill, "reg_fee", 0.0))),
                "fee_total": fee_total,
                "slippage_cost": slippage_cost,
                "order_id": str(getattr(fill, "order_id", "") or ""),
            }
        )
        return True

    def _mark_to_market(
        self, positions: dict[str, Position], prepared: _PreparedData, day: date
    ) -> float:
        """按当日收盘价估值（停牌/缺数据沿用最近收盘价），返回持仓市值合计。"""

        total = 0.0
        for symbol, position in positions.items():
            close_price: float | None = None
            if day in prepared.date_pos.get(symbol, {}):
                close_price = _safe_float(
                    prepared.frames[symbol].iloc[prepared.date_pos[symbol][day]]["close"]
                )
            if close_price is None:
                close_price = float(position.last_price or position.avg_cost or 0.0)
            if close_price <= 0:
                close_price = float(position.avg_cost or 0.0)
            position.last_price = round(close_price, 4)
            position.market_value = round_money(close_price * position.quantity)
            total = round_money(total + position.market_value)
        return total

    # ------------------------------------------------------------------
    # 信号 -> 委托
    # ------------------------------------------------------------------
    def _position_pct(self, strategy: Strategy) -> float:
        raw = strategy.params.get("position_pct", self.default_position_pct)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = self.default_position_pct
        if not math.isfinite(value) or value <= 0:
            value = self.default_position_pct
        return min(value, 100.0)

    def _build_buy_order(
        self,
        order_id: str,
        symbol: str,
        bar: Bar,
        ctx: Context,
        positions: dict[str, Position],
        account: Account,
        board: str,
        strategy: Strategy,
        budget: float,
        fee_model: FeeModel,
        slippage_bps: float,
        day: date,
        *,
        min_lot_enabled: bool = True,
    ) -> tuple[PendingOrder | None, dict[str, str] | None]:
        """按 ``position_pct`` 目标仓位生成买单（整手 + 现金约束 + 最小 1 手规则）。

        Returns:
            ``(委托或 None, 说明或 None)``；说明 ``kind`` ∈
            ``min_lot``（已按最小 1 手调整）/ ``insufficient``（超单票上限）/
            ``cash_short``（现金不足）/ ``synthetic_price``（合成价关闭该规则）。
        """

        value = float(getattr(bar, "close", 0.0) or 0.0)
        close_price = _safe_float(ctx.close(symbol))
        reference = close_price if close_price is not None else value
        if reference <= 0:
            return None, None
        position = positions.get(symbol)
        held = int(position.quantity) if position is not None else 0
        total_asset = float(account.total_asset)
        pct = self._position_pct(strategy)
        target_value = total_asset * pct / 100.0
        target_shares = int(target_value // reference)
        quantity = _round_buy_quantity(target_shares - held, board, held)
        note: dict[str, str] | None = None
        lot_size = _lot_size(board)

        if quantity <= 0 and held == 0:
            lot_cost = reference * lot_size
            max_value = total_asset * self.max_position_per_symbol_pct / 100.0
            if not min_lot_enabled:
                return None, {
                    "kind": "synthetic_price",
                    "message": (
                        f"{symbol} 行情为复权合成价格（未取到不复权数据），"
                        "手数/资金不代表真实交易，已跳过该买入；请用不复权数据回测"
                    ),
                }
            if lot_cost > max_value + _CASH_EPS:
                return None, {
                    "kind": "insufficient",
                    "message": (
                        f"{symbol} 资金不足，无法满足单票仓位上限：1 手（{lot_size} 股）约 "
                        f"{lot_cost:,.0f} 元 > 上限 {max_value:,.0f} 元"
                        f"（总资产 {total_asset:,.0f} 元 × {self.max_position_per_symbol_pct:g}%）；"
                        "建议提高本金、调低 position_pct 或选择低价标的"
                    ),
                }
            quantity = lot_size
            note = {
                "kind": "min_lot",
                "message": (
                    f"{symbol} 目标仓位约 {target_value:,.0f} 元不足 1 手（{lot_size} 股约 "
                    f"{lot_cost:,.0f} 元），已按最小 1 手调整"
                ),
            }

        estimate_price = AStockRules.apply_slippage(reference, "BUY", slippage_bps)
        affordable = _affordable_quantity(quantity, estimate_price, budget, board, fee_model, held)
        if affordable <= 0:
            estimated_cost = round_money(estimate_price * quantity)
            return None, {
                "kind": "cash_short",
                "message": (
                    f"{symbol} 可用资金不足，无法买入最小 {quantity} 股"
                    f"（约 {estimated_cost:,.0f} 元 + 费用）；建议减少持仓数量或提高本金"
                ),
            }
        if note is not None and affordable < quantity:
            return None, {
                "kind": "cash_short",
                "message": (
                    f"{symbol} 可用资金不足，无法买入最小 1 手"
                    f"（约 {reference * quantity:,.0f} 元 + 费用）"
                ),
            }
        quantity = affordable
        ok, _code = AStockRules.validate_lot("BUY", quantity, None, board)
        if not ok:
            return None, None
        order = PendingOrder(
            id=order_id,
            symbol=symbol,
            side="BUY",
            quantity=quantity,
            price=None,
            order_type="MARKET",
            board=board,
            is_st=bool(getattr(bar, "is_st", False)),
            prev_close=_safe_float(getattr(bar, "prev_close", None)),
            created_date=day,
            est_price=reference,
        )
        return order, note

    def _build_sell_order(
        self,
        order_id: str,
        symbol: str,
        bar: Bar,
        positions: dict[str, Position],
        board: str,
        day: date,
    ) -> PendingOrder | None:
        """生成卖单：清空该标的全部持仓（T+1 由日初结算保证可卖）。"""

        position = positions.get(symbol)
        if position is None or int(position.quantity) <= 0:
            return None
        quantity = int(position.quantity)
        ok, _code = AStockRules.validate_lot("SELL", quantity, quantity, board)
        if not ok:
            quantity = quantity // 100 * 100
            if quantity <= 0:
                return None
        return PendingOrder(
            id=order_id,
            symbol=symbol,
            side="SELL",
            quantity=quantity,
            price=None,
            order_type="MARKET",
            board=board,
            is_st=bool(getattr(bar, "is_st", False)),
            prev_close=_safe_float(getattr(bar, "prev_close", None)),
            created_date=day,
        )

    def _estimate_pending_sell_value(
        self, pending: list[PendingOrder], prepared: _PreparedData, day: date
    ) -> float:
        """估算当日挂出卖单的预计回款（T+0 可用于买入，SPEC §4.2）。"""

        total = 0.0
        for order in pending:
            if order.side != "SELL":
                continue
            close_price = 0.0
            if day in prepared.date_pos.get(order.symbol, {}):
                close_price = (
                    _safe_float(
                        prepared.frames[order.symbol].iloc[
                            prepared.date_pos[order.symbol][day]
                        ]["close"]
                    )
                    or 0.0
                )
            total += close_price * order.quantity * 0.99
        return round_money(total)

    @staticmethod
    def _estimate_order_cost(order: PendingOrder, fee_model: FeeModel, slippage_bps: float) -> float:
        """估算一笔买单占用的现金（用于同日多笔买入的资金分配）。"""

        base_price = order.est_price or order.prev_close or 0.0
        price = AStockRules.apply_slippage(max(base_price, 0.01), "BUY", slippage_bps)
        amount = round_money(price * order.quantity)
        fee = fee_model.calculate("BUY", price, order.quantity).total
        return round_money(amount + fee)

    # ------------------------------------------------------------------
    # 结果组装
    # ------------------------------------------------------------------
    @staticmethod
    def _build_equity_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
        columns = ["trade_date", "equity", "cash", "position_value", "benchmark_close", "drawdown"]
        if not rows:
            return pd.DataFrame(columns=columns)
        frame = pd.DataFrame(rows)
        values = frame["equity"].to_numpy(dtype="float64")
        peaks = np.maximum.accumulate(values)
        with np.errstate(divide="ignore", invalid="ignore"):
            drawdown = values / peaks - 1.0
        frame["drawdown"] = np.where(np.isfinite(drawdown), np.minimum(drawdown, 0.0), 0.0)
        return frame[columns]

    @staticmethod
    def _build_trades_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
        columns = [
            "trade_date",
            "symbol",
            "side",
            "price",
            "quantity",
            "amount",
            "commission",
            "stamp_tax",
            "transfer_fee",
            "reg_fee",
            "fee_total",
            "slippage_cost",
            "order_id",
        ]
        if not rows:
            return pd.DataFrame(columns=columns)
        return pd.DataFrame(rows)[columns]

    def _collect_warnings(
        self, warning_set: set[str], stats: dict[str, Any], trades_frame: pd.DataFrame
    ) -> list[str]:
        """补充样本期/交易次数/停牌占比等警告（SPEC §8.3 防过拟合最低要求）。"""

        warnings = set(warning_set)
        calendar_days = int(stats.get("calendar_days", 0) or 0)
        if 0 < calendar_days < MIN_SAMPLE_DAYS:
            warnings.add(RQ_WARN_SHORT_SAMPLE)
        trade_count = int(len(trades_frame)) if trades_frame is not None else 0
        if trade_count < MIN_TRADES:
            warnings.add(RQ_WARN_FEW_TRADES)
        bars = int(stats.get("bars", 0) or 0)
        suspended = int(stats.get("suspended", 0) or 0)
        if bars > 0 and suspended / bars > HIGH_SUSPEND_RATIO:
            warnings.add(RQ_WARN_HIGH_SUSPEND)
        order = [
            RQ_WARN_MOCK_DATA,
            RQ_WARN_SYNTHETIC_PRICE,
            RQ_WARN_PRICE_JUMP,
            RQ_WARN_POSITION_MIN_LOT,
            RQ_WARN_NO_ADJ,
            RQ_WARN_ST_APPROX,
            RQ_WARN_NO_BENCHMARK,
            RQ_WARN_SHORT_SAMPLE,
            RQ_WARN_FEW_TRADES,
            RQ_WARN_HIGH_SUSPEND,
        ]
        return [code for code in order if code in warnings]


# ---------------------------------------------------------------------------
# 五、工具函数
# ---------------------------------------------------------------------------


def finish_display(start: date, end: date) -> str:  # pragma: no cover - 展示辅助
    """``start ~ end`` 展示串（静态方法外的模块级辅助，便于错误信息复用）。"""

    return f"{start} ~ {end}"


def _lot_size(board: str) -> int:
    """板块最小买入单位（股）：科创板 200 股起，其余 100 股。"""

    return 200 if board == BOARD_STAR else 100


def _round_buy_quantity(quantity: int, board: str, held: int) -> int:
    """按板块整手规则向下取整买入数量（科创板 200 股起、北交所 100 股起）。"""

    if quantity <= 0:
        return 0
    if board == BOARD_STAR:
        if held == 0 and quantity < 200:
            return 0
        return int(quantity)
    if board == "BSE":
        if held == 0 and quantity < 100:
            return 0
        return int(quantity)
    value = int(quantity) // 100 * 100
    if held == 0 and value < 100:
        return 0
    return max(0, value)


def _affordable_quantity(
    quantity: int,
    price: float,
    cash_budget: float,
    board: str,
    fee_model: FeeModel,
    held: int,
) -> int:
    """在现金约束内尽可能多买（按板块步长递减，费用一并计入）。"""

    if quantity <= 0 or price <= 0 or cash_budget <= 0:
        return 0
    step = 100 if board not in (BOARD_STAR, "BSE") else 1
    value = _round_buy_quantity(quantity, board, held)
    while value > 0:
        amount = round_money(price * value)
        fee = fee_model.calculate("BUY", price, value).total
        if amount + fee <= cash_budget + _CASH_EPS:
            return value
        value -= step
    return 0


def run_backtest(
    strategy_key: str,
    params: dict[str, Any] | None,
    symbols: list[str],
    start: date,
    end: date,
    **kwargs: Any,
) -> BacktestResult:
    """便捷函数：``BacktestEngine().run(...)``（UI 回测页直接调用）。"""

    return BacktestEngine().run(strategy_key, params, symbols, start, end, **kwargs)


"""回测结果持久化（任务 M1-13 / M4-03 的数据基础）。

把 :class:`backtest.engine.BacktestResult` 写入三张表（run_id 关联，SPEC §5.2 B 域）：

* ``backtest_runs``：一次回测的元信息 + 指标 JSON + 警告 JSON；
* ``backtest_equity``：逐交易日权益（``PK(run_id, trade_date)``）；
* ``backtest_trades``：成交明细（含各项费用与滑点成本）。

设计约束
--------
* **写库失败不得影响回测结果返回**：:func:`save_backtest_result` 内部捕获异常并
  回滚，返回中文 ``warnings`` 报告（``ok=False``），调用方可直接展示回测结果；
* **幂等**：同一 ``run_id`` 重复保存时先删旧记录再写入，避免主键冲突与脏数据；
* 仅依赖 ``core.models`` 的 ORM 与 ``backtest.engine`` 的结果结构，不依赖 UI/trading。

典型用法::

    from core.db import init_db, get_session
    from backtest.engine import BacktestEngine
    from backtest.persistence import save_backtest_result, load_backtest_result

    init_db()
    result = BacktestEngine().run("ma_cross", None, ["600519.SH"],
                                  date(2024, 1, 1), date(2025, 12, 31), bars=bars)
    with get_session() as session:
        report = save_backtest_result(session, result)     # 失败也不抛，返回 warnings
        loaded = load_backtest_result(session, result.run_id)
"""

from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backtest.engine import BacktestError, BacktestResult
from core.models import (
    BacktestEquityORM,
    BacktestRunORM,
    BacktestStatus,
    BacktestTradeORM,
    now_local_naive,
)

logger = logging.getLogger(__name__)

__all__ = [
    "save_backtest_result",
    "load_backtest_result",
    "delete_backtest_result",
    "list_backtest_runs",
    "EQUITY_COLUMNS",
    "TRADE_COLUMNS",
]

#: ``backtest_equity`` 表对应列（其余列如 benchmark_equity 不入库，可由 benchmark_close 推导）
EQUITY_COLUMNS: list[str] = ["trade_date", "equity", "cash", "position_value", "benchmark_close", "drawdown"]

#: ``backtest_trades`` 表对应列
TRADE_COLUMNS: list[str] = [
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
    "slippage_cost",
    "fee_total",
]


# ---------------------------------------------------------------------------
# JSON / 数值工具
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """递归把 numpy/pandas 类型转成可 JSON 序列化的 Python 原生类型。"""

    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.ndarray):
        return [_jsonable(item) for item in value.tolist()]
    return value


def _dumps(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, allow_nan=False)


def _loads_json(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise BacktestError(f"回测记录 JSON 解析失败：{exc}") from exc


def _as_date(value: Any, *, name: str = "日期") -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise BacktestError(f"{name}格式非法：{value!r}") from exc
    if pd.isna(timestamp):
        raise BacktestError(f"{name}格式非法：{value!r}")
    return timestamp.date()


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 保存
# ---------------------------------------------------------------------------


def save_backtest_result(session: Session, result: BacktestResult) -> dict[str, Any]:
    """把回测结果写入 ``backtest_runs`` / ``backtest_equity`` / ``backtest_trades``。

    **失败不抛异常**：返回 ``{"ok": bool, "run_id": str, "rows": {...}, "warnings": [...]}``，
    调用方可继续使用 ``result``；写库失败时已回滚本次事务。

    Args:
        session: SQLAlchemy 会话（调用方负责最终关闭；本函数内部 ``commit``）。
        result: 回测结果。

    Returns:
        ``{"ok": True, "run_id": ..., "rows": {"runs":1,"equity":n,"trades":m}, "warnings": []}``
        或失败信息（中文 ``warnings``）。
    """

    report: dict[str, Any] = {
        "ok": False,
        "run_id": getattr(result, "run_id", ""),
        "rows": {"runs": 0, "equity": 0, "trades": 0},
        "warnings": [],
    }
    if session is None:
        report["warnings"].append("未提供数据库会话，回测结果未保存（仅影响落库，不影响本次回测结果）")
        return report
    if result is None:
        report["warnings"].append("回测结果为空，未保存")
        return report

    run_id = str(getattr(result, "run_id", "") or "").strip()
    if not run_id:
        report["warnings"].append("回测结果缺少 run_id，未保存")
        return report

    equity_frame = getattr(result, "equity", None)
    trades_frame = getattr(result, "trades", None)
    if not isinstance(equity_frame, pd.DataFrame) or len(equity_frame) == 0:
        report["warnings"].append("权益曲线为空，未保存回测结果")
        return report

    try:
        # 幂等：先清除同一 run_id 的旧数据（避免主键冲突）
        _delete_run_rows(session, run_id)
        session.flush()

        # is_mock/metadata 无独立表字段（CF-7 冻结），随 metrics_json 的 "_meta" 一起持久化
        metrics_payload = dict(getattr(result, "metrics", {}) or {})
        metrics_payload["_meta"] = {
            "is_mock": bool(getattr(result, "is_mock", False)),
            "metadata": dict(getattr(result, "metadata", {}) or {}),
        }

        run_row = BacktestRunORM(
            id=run_id,
            strategy_key=str(getattr(result, "strategy_key", "") or ""),
            params_json=_dumps(getattr(result, "params", {}) or {}),
            universe_json=_dumps(list(getattr(result, "symbols", []) or [])),
            start_date=_as_date(getattr(result, "start_date", None), name="回测开始日期"),
            end_date=_as_date(getattr(result, "end_date", None), name="回测结束日期"),
            initial_cash=_as_float(getattr(result, "initial_cash", 0.0)),
            fee_config_json=_dumps(getattr(result, "fee_config", {}) or {}),
            status=BacktestStatus.DONE.value,
            metrics_json=_dumps(metrics_payload),
            warnings_json=_dumps(list(getattr(result, "warnings", []) or [])),
            error_msg=None,
            created_at=now_local_naive(),
            finished_at=now_local_naive(),
        )
        session.add(run_row)
        report["rows"]["runs"] = 1

        equity_rows: list[BacktestEquityORM] = []
        for record in equity_frame.to_dict(orient="records"):
            equity_rows.append(
                BacktestEquityORM(
                    run_id=run_id,
                    trade_date=_as_date(record.get("trade_date"), name="权益曲线交易日"),
                    equity=_as_float(record.get("equity")),
                    cash=_as_float(record.get("cash")),
                    position_value=_as_float(record.get("position_value")),
                    benchmark_close=(
                        _as_float(record.get("benchmark_close"))
                        if record.get("benchmark_close") is not None
                        and not pd.isna(record.get("benchmark_close"))
                        else None
                    ),
                    drawdown=(
                        _as_float(record.get("drawdown"))
                        if record.get("drawdown") is not None and not pd.isna(record.get("drawdown"))
                        else None
                    ),
                )
            )
        session.add_all(equity_rows)
        report["rows"]["equity"] = len(equity_rows)

        trade_rows: list[BacktestTradeORM] = []
        if isinstance(trades_frame, pd.DataFrame) and len(trades_frame) > 0:
            for record in trades_frame.to_dict(orient="records"):
                trade_rows.append(
                    BacktestTradeORM(
                        run_id=run_id,
                        trade_date=_as_date(record.get("trade_date"), name="成交交易日"),
                        symbol=str(record.get("symbol", "") or ""),
                        side=str(record.get("side", "") or "").upper(),
                        price=_as_float(record.get("price")),
                        quantity=_as_int(record.get("quantity")),
                        amount=_as_float(record.get("amount")),
                        commission=_as_float(record.get("commission")),
                        stamp_tax=_as_float(record.get("stamp_tax")),
                        transfer_fee=_as_float(record.get("transfer_fee")),
                        reg_fee=_as_float(record.get("reg_fee")),
                        slippage_cost=_as_float(record.get("slippage_cost")),
                        fee_total=_as_float(record.get("fee_total")),
                    )
                )
            session.add_all(trade_rows)
        report["rows"]["trades"] = len(trade_rows)

        session.commit()
        report["ok"] = True
        logger.info(
            "回测结果已保存：run_id=%s 权益 %d 行、成交 %d 行",
            run_id,
            len(equity_rows),
            len(trade_rows),
        )
        return report
    except Exception as exc:  # noqa: BLE001 - 写库失败不能影响回测结果
        try:
            session.rollback()
        except Exception:  # pragma: no cover - 回滚本身失败
            logger.exception("回滚回测结果事务失败")
        message = f"回测结果保存失败（不影响本次回测结果）：{type(exc).__name__}: {exc}"
        logger.warning(message)
        report["ok"] = False
        report["warnings"].append(message)
        return report


def _delete_run_rows(session: Session, run_id: str) -> None:
    """删除某次回测在三个表中的全部旧记录（幂等保存用）。"""

    session.execute(delete(BacktestEquityORM).where(BacktestEquityORM.run_id == run_id))
    session.execute(delete(BacktestTradeORM).where(BacktestTradeORM.run_id == run_id))
    session.execute(delete(BacktestRunORM).where(BacktestRunORM.id == run_id))


def delete_backtest_result(session: Session, run_id: str) -> bool:
    """删除某次回测记录（三表级联删除）；记录不存在返回 False。"""

    if session is None:
        return False
    existing = session.get(BacktestRunORM, run_id)
    if existing is None:
        return False
    _delete_run_rows(session, run_id)
    session.commit()
    return True


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def load_backtest_result(session: Session, run_id: str) -> BacktestResult | None:
    """按 ``run_id`` 读回完整回测结果（含权益曲线与成交明细）。

    Args:
        session: SQLAlchemy 会话。
        run_id: 回测 ID。

    Returns:
        :class:`BacktestResult`；记录不存在返回 ``None``（UI 据此提示“回测记录不存在”）。
        ``notes`` 因 ``backtest_runs`` 无对应字段，读回时为空列表；其余字段完整还原。

    Raises:
        BacktestError: JSON 损坏等无法解析的数据（中文提示）。
    """

    if session is None:
        raise BacktestError("未提供数据库会话，无法读取回测记录")
    run = session.get(BacktestRunORM, str(run_id))
    if run is None:
        logger.info("回测记录不存在：run_id=%s", run_id)
        return None

    params = _loads_json(run.params_json, {})
    metrics_raw = _loads_json(run.metrics_json, {})
    # "_meta" 保存 is_mock/metadata（backtest_runs 无独立字段，见 save_backtest_result 注释）
    meta_payload = {}
    if isinstance(metrics_raw, dict):
        meta_payload = metrics_raw.pop("_meta", {}) or {}
    metrics = metrics_raw if isinstance(metrics_raw, dict) else {}
    warnings = _loads_json(run.warnings_json, [])
    fee_config = _loads_json(run.fee_config_json, {})
    symbols = _loads_json(run.universe_json, [])
    is_mock = bool(meta_payload.get("is_mock", False)) if isinstance(meta_payload, dict) else False
    metadata = dict(meta_payload.get("metadata", {}) or {}) if isinstance(meta_payload, dict) else {}

    equity_rows = session.execute(
        select(BacktestEquityORM)
        .where(BacktestEquityORM.run_id == run_id)
        .order_by(BacktestEquityORM.trade_date)
    ).scalars()
    equity_records: list[dict[str, Any]] = []
    for row in equity_rows:
        equity_records.append(
            {
                "trade_date": row.trade_date,
                "equity": float(row.equity),
                "cash": float(row.cash),
                "position_value": float(row.position_value),
                "benchmark_close": None if row.benchmark_close is None else float(row.benchmark_close),
                "drawdown": None if row.drawdown is None else float(row.drawdown),
            }
        )
    equity_frame = pd.DataFrame(equity_records, columns=EQUITY_COLUMNS)
    if len(equity_frame) > 0 and equity_frame["benchmark_close"].notna().any():
        equity_frame = _attach_benchmark_from_close(
            equity_frame, float(run.initial_cash or 0.0)
        )

    trade_rows = session.execute(
        select(BacktestTradeORM)
        .where(BacktestTradeORM.run_id == run_id)
        .order_by(BacktestTradeORM.id)
    ).scalars()
    trade_records: list[dict[str, Any]] = []
    for row in trade_rows:
        trade_records.append(
            {
                "trade_date": row.trade_date,
                "symbol": row.symbol,
                "side": row.side,
                "price": float(row.price),
                "quantity": int(row.quantity),
                "amount": float(row.amount),
                "commission": float(row.commission),
                "stamp_tax": float(row.stamp_tax),
                "transfer_fee": float(row.transfer_fee),
                "reg_fee": float(row.reg_fee),
                "fee_total": float(row.fee_total),
                "slippage_cost": float(row.slippage_cost),
            }
        )
    trades_frame = pd.DataFrame(trade_records, columns=TRADE_COLUMNS)

    return BacktestResult(
        run_id=str(run.id),
        strategy_key=str(run.strategy_key),
        params=dict(params or {}),
        symbols=list(symbols or []),
        start_date=run.start_date,
        end_date=run.end_date,
        initial_cash=float(run.initial_cash or 0.0),
        metrics=dict(metrics or {}),
        equity=equity_frame,
        trades=trades_frame,
        warnings=list(warnings or []),
        notes=[],
        fee_config=dict(fee_config or {}),
        benchmark=None,
        duration_seconds=0.0,
        is_mock=is_mock,
        metadata=metadata,
    )


def _attach_benchmark_from_close(equity_frame: pd.DataFrame, initial_cash: float) -> pd.DataFrame:
    """读库后由 ``benchmark_close`` 还原 ``benchmark_equity`` / ``benchmark_return`` 两列。"""

    if equity_frame is None or len(equity_frame) == 0 or initial_cash <= 0:
        return equity_frame
    closes = pd.to_numeric(equity_frame["benchmark_close"], errors="coerce").astype("float64")
    if not closes.notna().any():
        return equity_frame
    first = float(closes.dropna().iloc[0])
    if not math.isfinite(first) or first <= 0:
        return equity_frame
    frame = equity_frame.copy()
    frame["benchmark_equity"] = (closes.ffill() * (initial_cash / first)).round(2)
    frame["benchmark_return"] = frame["benchmark_equity"] / initial_cash - 1.0
    return frame


# ---------------------------------------------------------------------------
# 列表（供 UI 历史回测/对比选择）
# ---------------------------------------------------------------------------


def list_backtest_runs(session: Session, *, limit: int = 50) -> list[dict[str, Any]]:
    """列出最近的回测记录（按创建时间倒序），供 UI 选择对比。

    Returns:
        ``[{"run_id","strategy_key","start_date","end_date","initial_cash","status",
        "metrics","warnings","created_at","finished_at"}]``
    """

    if session is None:
        return []
    safe_limit = max(1, min(int(limit), 500))
    rows = session.execute(
        select(BacktestRunORM).order_by(BacktestRunORM.created_at.desc()).limit(safe_limit)
    ).scalars()
    result: list[dict[str, Any]] = []
    for row in rows:
        result.append(
            {
                "run_id": str(row.id),
                "strategy_key": str(row.strategy_key),
                "start_date": row.start_date,
                "end_date": row.end_date,
                "initial_cash": float(row.initial_cash or 0.0),
                "status": str(row.status),
                "metrics": _loads_json(row.metrics_json, {}),
                "warnings": _loads_json(row.warnings_json, []),
                "created_at": row.created_at,
                "finished_at": row.finished_at,
            }
        )
    return result

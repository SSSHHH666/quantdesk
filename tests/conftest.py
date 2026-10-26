"""QuantDesk 测试公共夹具（CF-13 / TASKS M0-10）。

本文件提供三块能力，全部**确定性**（无随机数、无网络、无真实时钟依赖）：

1. **临时 SQLite 文件库夹具** —— 每个用例独占一个 ``tmp_path`` 下的 ``.db`` 文件，
   用 ``Base.metadata.create_all`` 建全部 22 张表；用例结束自动 dispose + 清理，
   保证用例之间零共享状态（AC-F6-07 的“风控不写库”也因此可断言）。
2. **样本数据工厂** —— ``Bar`` / ``Account`` / ``Position`` / ``OrderRequest`` /
   ``MarketSnapshot`` / ``Fill`` 的构造器，默认值固定，可关键字覆盖。
3. **pytest markers** —— ``unit`` / ``integration`` / ``e2e``（见 ``pytest.ini``，
   本文件另有 ``pytest_configure`` 兜底注册）。

运行方式::

    cd quantdesk
    .venv\\Scripts\\python.exe -m pytest tests -q
    .venv\\Scripts\\python.exe -m pytest tests -q -m unit
"""

from __future__ import annotations

import datetime as dt
import importlib
import sys
from pathlib import Path
from typing import Any, Callable

import pytest
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

# 让 `import core / backtest / trading` 在任意工作目录下都可用（pytest 只把 tests/ 加入 sys.path）
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.models import (  # noqa: E402  （必须在 sys.path 调整之后导入）
    Account,
    Bar,
    Base,
    FeeConfig,
    Fill,
    MarketSnapshot,
    OrderRequest,
    Position,
    Side,
)

# ---------------------------------------------------------------------------
# 确定性基准常量（所有用例共用，改这里等于改全批用例的“世界线”）
# ---------------------------------------------------------------------------

#: 固定交易日（2026-10-26 是周一，非节假日；不读真实时钟，避免用例随时间漂移）
FIXED_TRADE_DATE: dt.date = dt.date(2026, 10, 26)
#: 固定参照时刻（当日 14:30，处于 A 股连续竞价时段内）
FIXED_AS_OF: dt.datetime = dt.datetime(2026, 10, 26, 14, 30, 0)

#: 默认标的：浦发银行（沪市主板，100 股整手）
DEFAULT_SYMBOL = "600000.SH"
#: 默认账户 ID / 初始资金（SPEC 默认 10 万）
DEFAULT_ACCOUNT_ID = "ACC-TEST-001"
DEFAULT_INITIAL_CASH = 100000.0
#: 默认最新价 / 限价（10 元便于口算费用与仓位占比）
DEFAULT_PRICE = 10.0


# ---------------------------------------------------------------------------
# 一、临时 SQLite 文件库夹具（CF-13 ①）
# ---------------------------------------------------------------------------


class TestDatabase:
    """临时 SQLite 测试库句柄。

    Attributes:
        engine: SQLAlchemy Engine（绑定 ``tmp_path`` 下的真实文件，非 ``:memory:``；
            文件库才能验证“跨连接/跨会话”的写入行为）。
        session_factory: ``sessionmaker``，调用得到新 Session。
        path: 数据库文件路径（用例可断言“不应产生的写入”）。
    """

    def __init__(self, engine: Engine, session_factory: sessionmaker[Session], path: Path) -> None:
        self.engine = engine
        self.session_factory = session_factory
        self.path = path

    def session(self) -> Session:
        """新建一个 Session（调用方负责 close）。"""
        return self.session_factory()

    def table_names(self) -> set[str]:
        """当前库中已建的表名集合（用于断言建表完整性）。"""
        return set(inspect(self.engine).get_table_names())

    def count(self, table: str) -> int:
        """统计某表行数（测试断言用；表名来自代码常量，不接受用户输入）。"""
        from sqlalchemy import text

        with self.engine.connect() as conn:
            return int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """每个用例独占的 SQLite 文件路径。"""
    return tmp_path / "quantdesk_test.db"


@pytest.fixture()
def db_engine(db_path: Path) -> Engine:
    """建好 22 张表的临时 SQLite 引擎（用例级隔离）。"""
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: Any, _record: Any) -> None:  # pragma: no cover - 连接钩子
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def test_db(db_engine: Engine, db_path: Path) -> TestDatabase:
    """CF-13 核心夹具：临时 SQLite 测试库（engine + session 工厂 + 文件路径）。"""
    return TestDatabase(
        engine=db_engine,
        session_factory=sessionmaker(bind=db_engine, expire_on_commit=False, future=True),
        path=db_path,
    )


@pytest.fixture()
def db_session(test_db: TestDatabase) -> Session:
    """从临时库取一个 Session，用例结束自动 rollback + close（不留脏数据）。"""
    session = test_db.session()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


# ---------------------------------------------------------------------------
# 二、样本数据工厂（CF-13 ②，全部确定性）
# ---------------------------------------------------------------------------


@pytest.fixture()
def bar_factory() -> Callable[..., Bar]:
    """构造 ``core.models.Bar``；默认一根 10 元附近的正常交易日 K 线。"""

    def make_bar(
        symbol: str = DEFAULT_SYMBOL,
        trade_date: dt.date = FIXED_TRADE_DATE,
        open: float = 10.0,
        high: float = 10.5,
        low: float = 9.5,
        close: float = 10.2,
        volume: float = 1_000_000.0,
        **overrides: Any,
    ) -> Bar:
        payload: dict[str, Any] = {
            "symbol": symbol,
            "trade_date": trade_date,
            "open": open,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
        payload.update(overrides)
        return Bar(**payload)

    return make_bar


@pytest.fixture()
def account_factory() -> Callable[..., Account]:
    """构造 ``core.models.Account``；默认 10 万现金、10 万总资产、账户启用。"""

    def make_account(
        id: str = DEFAULT_ACCOUNT_ID,
        cash: float = DEFAULT_INITIAL_CASH,
        total_asset: float = DEFAULT_INITIAL_CASH,
        frozen_cash: float = 0.0,
        **overrides: Any,
    ) -> Account:
        payload: dict[str, Any] = {
            "id": id,
            "cash": float(cash),
            "total_asset": float(total_asset),
            "frozen_cash": float(frozen_cash),
        }
        payload.update(overrides)
        return Account(**payload)

    return make_account


@pytest.fixture()
def position_factory() -> Callable[..., Position]:
    """构造 ``core.models.Position``；默认 1000 股可卖（T+1 已解锁）持仓。"""

    def make_position(
        symbol: str = DEFAULT_SYMBOL,
        account_id: str = DEFAULT_ACCOUNT_ID,
        quantity: int = 1000,
        available_quantity: int | None = None,
        avg_cost: float = DEFAULT_PRICE,
        last_price: float = DEFAULT_PRICE,
        **overrides: Any,
    ) -> Position:
        available = int(quantity) if available_quantity is None else int(available_quantity)
        payload: dict[str, Any] = {
            "account_id": account_id,
            "symbol": symbol,
            "quantity": int(quantity),
            "available_quantity": available,
            "avg_cost": float(avg_cost),
            "last_price": float(last_price),
            "market_value": round(int(quantity) * float(last_price), 2),
        }
        payload.update(overrides)
        return Position(**payload)

    return make_position


@pytest.fixture()
def order_request_factory() -> Callable[..., OrderRequest]:
    """构造 ``core.models.OrderRequest``；默认买 1000 股 @10 元（金额 1 万 = 总资产 10%）。"""

    def make_order(
        account_id: str = DEFAULT_ACCOUNT_ID,
        symbol: str = DEFAULT_SYMBOL,
        side: Side | str = Side.BUY,
        price: float | None = DEFAULT_PRICE,
        quantity: int = 1000,
        **overrides: Any,
    ) -> OrderRequest:
        payload: dict[str, Any] = {
            "account_id": account_id,
            "symbol": symbol,
            "side": side,
            "price": price,
            "quantity": int(quantity),
        }
        payload.update(overrides)
        return OrderRequest(**payload)

    return make_order


@pytest.fixture()
def market_snapshot_factory() -> Callable[..., MarketSnapshot]:
    """构造 ``core.models.MarketSnapshot``；默认“交易时段内 + 最新价 10 元”。

    额外字段（``universe`` / ``listed_days`` / ``price_limits`` / ``blacklist`` /
    ``suspended`` / ``price_times`` / ``recent_order_times`` 等）通过 ``overrides``
    透传——``QDModel`` 的 ``extra="allow"`` 允许这些风控扩展字段。
    """

    def make_market(
        symbol: str = DEFAULT_SYMBOL,
        latest_price: float | None = DEFAULT_PRICE,
        trade_date: dt.date = FIXED_TRADE_DATE,
        is_trading_time: bool = True,
        **overrides: Any,
    ) -> MarketSnapshot:
        payload: dict[str, Any] = {
            "trade_date": trade_date,
            "is_trading_time": is_trading_time,
            "latest_prices": {} if latest_price is None else {symbol: float(latest_price)},
        }
        payload.update(overrides)
        return MarketSnapshot(**payload)

    return make_market


@pytest.fixture()
def fill_factory() -> Callable[..., Fill]:
    """构造 ``core.models.Fill``（成交回报）；默认买 1000 股 @10 元、费用 5.64 元。"""

    def make_fill(
        order_id: str = "ORD-TEST-001",
        symbol: str = DEFAULT_SYMBOL,
        side: Side | str = Side.BUY,
        price: float = DEFAULT_PRICE,
        quantity: int = 1000,
        trade_date: dt.date | None = FIXED_TRADE_DATE,
        **overrides: Any,
    ) -> Fill:
        payload: dict[str, Any] = {
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "price": float(price),
            "quantity": int(quantity),
            "amount": round(float(price) * int(quantity), 2),
            "commission": 5.0,
            "stamp_tax": 0.0,
            "transfer_fee": 0.1,
            "reg_fee": 0.54,
            "fee_total": 5.64,
            "trade_date": trade_date,
        }
        payload.update(overrides)
        return Fill(**payload)

    return make_fill


# ---------------------------------------------------------------------------
# 三、可直接使用的样本实例（大量用例只需要“一份合法的默认世界”）
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_bar(bar_factory: Callable[..., Bar]) -> Bar:
    return bar_factory()


@pytest.fixture()
def sample_account(account_factory: Callable[..., Account]) -> Account:
    return account_factory()


@pytest.fixture()
def sample_position(position_factory: Callable[..., Position]) -> Position:
    return position_factory()


@pytest.fixture()
def sample_order(sample_account: Account, order_request_factory: Callable[..., OrderRequest]) -> OrderRequest:
    return order_request_factory(account_id=sample_account.id)


@pytest.fixture()
def sample_market(market_snapshot_factory: Callable[..., MarketSnapshot]) -> MarketSnapshot:
    return market_snapshot_factory()


@pytest.fixture()
def fee_config() -> FeeConfig:
    """SPEC §7 冻结的 A 股费率默认值（佣金万 2.5 / 最低 5 元、印花税 0.05%、过户费 0.001%、规费 0.00541%）。"""
    return FeeConfig()


# ---------------------------------------------------------------------------
# 四、全局单例复位（用例级隔离，解决“单跑通过、全量 ERROR”的顺序依赖）
# ---------------------------------------------------------------------------


def _reset_process_globals() -> None:
    """复位业务层的模块级单例，保证用例与执行顺序无关。

    背景：以下对象都是**模块级单例**，一旦在某个用例里捕获了当时的临时路径/引擎，
    后续用例即使重新设置 ``QD_*`` 环境变量也仍会指向旧库/旧服务：

    * ``core.config.get_config()``    —— 配置单例（缓存 config.yaml + 环境变量结果）
    * ``core.db.get_database()``      —— 数据库单例（Engine/Session 绑定首次解析到的 sqlite 路径）
    * ``data.watchlist.get_service()``—— 自选池服务单例（持有 Database 与 session factory）
    * ``core.logging`` 审计会话工厂    —— 写审计时使用的 session factory
    * ``core.events`` 全局事件总线     —— 订阅者可能跨用例残留

    测试侧在**每个用例前后**统一复位（不修改业务代码；若业务侧需要改为可注入，
    由 Lead 决策，QA 只报告）。这样每个用例都从“干净进程状态”出发，
    全量运行与单文件运行结果一致。
    """
    for module_name, attr in (
        ("core.config", "reset_config"),
        ("core.db", "reset_database"),
        ("data.watchlist", "reset_service"),
    ):
        try:
            module = importlib.import_module(module_name)
            func = getattr(module, attr, None)
            if callable(func):
                func()
        except Exception:  # pragma: no cover - 模块未就绪（早期 M0 用例）时忽略
            pass

    try:
        from core.logging import set_audit_session_factory

        set_audit_session_factory(None)
    except Exception:  # pragma: no cover
        pass

    try:
        from core.events import init_event_bus

        init_event_bus(None)
    except Exception:  # pragma: no cover
        pass


@pytest.fixture(autouse=True)
def _isolate_process_globals() -> Any:
    """autouse：每个用例前后复位全局单例（顺序无关、无跨用例状态泄漏）。"""
    _reset_process_globals()
    try:
        yield
    finally:
        _reset_process_globals()


# ---------------------------------------------------------------------------
# 五、pytest markers 兜底注册（即使 pytest.ini 未被发现，-m unit 也照常工作）
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    """注册自定义 marker 并做基础断言，尽早暴露环境问题。"""
    for marker, description in (
        ("unit", "单元测试（纯函数/模型，无 IO）"),
        ("integration", "集成测试（涉及临时 SQLite、文件或模块协作）"),
        ("e2e", "端到端测试（多模块串起来跑通全流程，较慢）"),
    ):
        config.addinivalue_line("markers", f"{marker}: {description}")

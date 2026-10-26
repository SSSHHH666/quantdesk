"""QuantDesk 核心基础设施包（Dev1 维护）。

按 SPEC §3.1 依赖方向硬约束：``core/*`` 只依赖标准库 + pydantic / SQLAlchemy / PyYAML，
**不 import 任何业务模块**（data/strategy/backtest/trading/automation/ui）。

本包统一导出五块能力，调用方可以直接 ``from core import ...``：

- 领域模型与 ORM：``from .models import *``
  （枚举、pydantic 模型、22 张表 ORM、原因码；``Signal`` 的唯一来源）
- 事件总线：``from .events import *``
  （``EventBus``、``Topic`` 主题常量、全局单例 ``get_event_bus()``）
- 结构化日志与审计：``from .logging import *``
  （``setup_logging`` / ``get_logger`` / ``trace_context`` / ``write_audit``）
- 配置加载与校验：``from .config import *``
  （``load_config`` / ``get_config`` / ``ConfigError`` / ``AppConfig``）
- 数据库引擎与会话：``from .db import *``
  （``Database`` / ``init_db`` / ``get_engine`` / ``get_session_factory`` / ``session_scope``）

常用示例::

    from core import setup_logging, trace_context, get_logger, get_event_bus, Topic
    from core import load_config, init_db, session_scope
    from core import OrderRequest, RiskDecision, Side, OrderStatus

    config = load_config()
    setup_logging(config)                       # 启动时一次
    init_db(config)                             # 建库建表 + 启动自检 + 审计会话接线
    with trace_context() as tid:                # 一次调用链
        get_logger("order").info("下单开始")
        get_event_bus().publish(Topic.ORDER_SUBMITTED, {"order_id": "..."}, trace_id=tid)
"""

from __future__ import annotations

__version__ = "1.0.0"

from .models import *  # noqa: F401,F403  （领域模型/ORM/枚举/原因码）
from .models import __all__ as _MODEL_EXPORTS
from .events import *  # noqa: F401,F403  （事件总线/主题常量）
from .events import __all__ as _EVENT_EXPORTS
from .logging import *  # noqa: F401,F403  （结构化日志/审计/trace_id）
from .logging import __all__ as _LOGGING_EXPORTS
from .config import *  # noqa: F401,F403  （配置加载与校验）
from .config import __all__ as _CONFIG_EXPORTS
from .db import *  # noqa: F401,F403  （数据库引擎/会话/建表/备份/自检）
from .db import __all__ as _DB_EXPORTS

#: 本包全部公开符号（合并各子模块的 __all__）
__all__: list[str] = [
    *_MODEL_EXPORTS,
    *_EVENT_EXPORTS,
    *_LOGGING_EXPORTS,
    *_CONFIG_EXPORTS,
    *_DB_EXPORTS,
    "__version__",
]

# 自检：确保合并后的导出列表无重名（避免 `from core import *` 覆盖歧义）
assert len(__all__) == len(set(__all__)), "core.__all__ 存在重复符号，请检查子模块导出"

"""QuantDesk 数据层包（Dev1，任务 M1-01 ~ M1-04 / R6-P0）。

对上层（策略/回测/交易/自动化/UI）只暴露数据层能力，内部依赖方向为::

    data/* → core/*（禁止反向 import；data 包不 import 任何业务模块）

常用入口::

    from data import BarCache, MockProvider, ProviderRegistry, build_provider
    from data import name_of, names_of, collect_daily, get_names, latest_snapshot

    provider = build_provider(config)          # 按 config.data 组装主备链
    cache = BarCache(provider=provider, config=config)
    report = cache.update(["600519.SH"], start, end)   # 增量更新
    bars = cache.load(["600519.SH"], start, end, adjust="hfq")
    issues = cache.validate(["600519.SH"], start, end) # DQ001–DQ007 校验

    snapshot = collect_daily()                 # 全市场快照采集 + 落库（失败降级上次快照）
    get_names(["600519.SH", "000001.SZ"])      # {"600519.SH": "贵州茅台", ...}
"""

from __future__ import annotations

from .cache import *  # noqa: F401,F403  （BarCache/报告模型/DQ 常量）
from .cache import __all__ as _CACHE_EXPORTS
from .market_snapshot import *  # noqa: F401,F403  （全市场快照采集/查询/名称）
from .market_snapshot import __all__ as _SNAPSHOT_EXPORTS
from .provider import *  # noqa: F401,F403  （DataProvider/注册表/MockProvider）
from .provider import __all__ as _PROVIDER_EXPORTS
from .symbols import *  # noqa: F401,F403  （名称映射 name_of/names_of）
from .symbols import __all__ as _SYMBOL_EXPORTS
from .watchlist import *  # noqa: F401,F403  （自选池 CRUD/分组/JSON 导入导出）
from .watchlist import __all__ as _WATCHLIST_EXPORTS

__all__: list[str] = [
    *_PROVIDER_EXPORTS,
    *_CACHE_EXPORTS,
    *_WATCHLIST_EXPORTS,
    *_SYMBOL_EXPORTS,
    *_SNAPSHOT_EXPORTS,
]

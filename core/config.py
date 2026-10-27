"""QuantDesk 配置加载与校验（任务 M0-02 / 契约冻结项 CF-9）。

职责
----
1. 读取 ``config.yaml``（键名与默认值严格按 SPEC §7 冻结清单），并用 pydantic 模型校验；
2. 支持 ``QD_`` 前缀环境变量覆盖（如 ``QD_APP_LOG_LEVEL=DEBUG``、``QD_DATA_UNIVERSE='[...]'``）；
3. 配置非法时抛出 :class:`ConfigError`，消息为中文并**指出具体配置字段路径**与合法范围；
4. 提供单例访问 :func:`get_config` / :func:`reload_config`，避免各模块重复读盘。

设计约定
--------
- **冻结键校验**：:data:`_FROZEN_CONFIG_KEYS` 与 SPEC §7 一一对应；出现未知键 / 缺少配置段直接报错，
  防止 Dev/用户自创配置键导致口径漂移（CF-9）。
- **类型与语义校验**：日志级别、时区、数据源、复权口径、券商、时间格式、百分比范围、费率范围等
  均在 pydantic 层校验，错误消息统一中文。
- **密码只从环境变量读**：``notify.email`` 不含 password 字段；写 ``password`` 会被判为未知键并报错。
- **路径解析**：``app.data_dir``、``database.url`` 中的相对路径统一相对**配置文件所在目录**解析，
  保证双击 ``start.bat`` / 任意工作目录启动都能找到 ``runtime/``。
- 本模块只依赖标准库 + PyYAML + pydantic + ``core/models.py``，不 import 任何业务模块。

自检（quantdesk 目录）：``python -m core.config``
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Literal, Mapping
from zoneinfo import ZoneInfo

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)

from .models import FeeConfig, RiskConfig

__all__ = [
    "CONFIG_ENV_PREFIX",
    "DEFAULT_CONFIG_PATH",
    "PROJECT_ROOT",
    "ConfigError",
    "record_config_warning",
    "get_config_warnings",
    "reset_config_warnings",
    "AppSection",
    "DatabaseSection",
    "DataSection",
    "TradingSection",
    "BacktestSection",
    "AutomationSection",
    "AutomationJobsSection",
    "NotifySection",
    "NotifyChannelsSection",
    "EmailSection",
    "WebhookSection",
    "RecommendSection",
    "ResearchSection",
    "AppConfig",
    "load_config",
    "get_config",
    "reload_config",
    "set_config",
    "reset_config",
    "apply_env_overrides",
    "collect_leaf_paths",
    "validate_frozen_keys",
]

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------

#: 项目根目录（quantdesk/，本文件在 quantdesk/core/ 下）
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

#: 默认配置文件路径
DEFAULT_CONFIG_PATH: Path = PROJECT_ROOT / "config.yaml"

#: 环境变量前缀（SPEC：QD_ 前缀）
CONFIG_ENV_PREFIX = "QD_"

#: 可用它指定配置文件路径（非配置项，仅启动辅助）
CONFIG_FILE_ENV = "QD_CONFIG_FILE"

#: 标的代码格式（如 600519.SH / 000001.SZ / 899050.BJ）
_SYMBOL_PATTERN = r"^[0-9]{6}\.(SH|SZ|BJ)$"

#: 允许的数据源 / 复权口径 / 券商（SPEC §7）
_DATA_PROVIDERS = ("akshare", "mock")
_ADJUST_MODES = ("hfq", "qfq", "none")
_BROKERS = ("paper", "ccxt", "qmt")
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

#: SPEC §7 冻结的配置键清单（用于检测未知键/缺失段，禁止自创）
_FROZEN_CONFIG_KEYS: dict[str, set[str]] = {
    "app": {"name", "version", "timezone", "data_dir", "log_level"},
    "database": {"url", "echo", "backup_keep"},
    "data": {
        "provider", "fallbacks", "universe", "history_years", "adjust",
        "request_interval_ms", "max_retries", "stale_threshold_hours",
    },
    "fees": {
        "commission_rate", "min_commission", "stamp_tax_rate", "transfer_fee_rate",
        "reg_fee_rate", "include_reg_fee_in_commission", "slippage_bps",
    },
    "trading": {"account_name", "broker", "initial_cash", "min_order_amount", "allow_market_order"},
    "risk": {
        "enabled", "allow_auto_orders", "max_position_per_symbol_pct", "max_total_position_pct",
        "max_daily_loss_pct", "max_price_deviation_pct", "max_single_order_value_pct",
        "max_orders_per_minute", "min_listed_days", "blacklist",
    },
    "backtest": {"default_initial_cash", "benchmark", "price_adjust"},
    "automation": {"enabled", "jobs"},
    "automation.jobs": {"prepare", "scan", "summarize"},
    "notify": {"enabled", "channels", "email", "webhook"},
    "notify.channels": {"desktop", "email", "webhook"},
    "notify.email": {"smtp_host", "smtp_port", "username", "password_env", "to"},
    "notify.webhook": {"url"},
    # ---- Phase 2（SPEC v1.4，F10–F13/M6；CF-9 扩展，Lead 已批准）----
    "recommend": {
        "enabled", "far_top_n", "near_top_n", "far_refresh_days", "near_refresh",
        "min_history_days", "min_listed_days", "exclude_st", "radar_enabled",
        "radar_volume_ratio", "radar_turnover_pct", "radar_amplitude_pct", "radar_dedup_days",
    },
    "research": {
        "ml_enabled", "walk_forward_folds", "purge_days", "embargo_days",
        "label_horizon_days", "param_robustness_pct",
    },
}


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class ConfigError(ValueError):
    """配置错误（中文、可操作提示：指出字段路径与合法范围）。"""


# ---------------------------------------------------------------------------
# 配置回退警告（SPEC v1.6：个别键非法时回退安全默认 + 中文报错，而非阻断启动）
# ---------------------------------------------------------------------------

_config_warnings: list[str] = []
_config_warnings_lock = threading.RLock()


def record_config_warning(message: str) -> None:
    """记录一条配置回退警告（中文），供启动日志 / UI 设置页展示。"""
    with _config_warnings_lock:
        _config_warnings.append(message)


def get_config_warnings(*, clear: bool = False) -> list[str]:
    """返回本次配置加载产生的回退警告（``clear=True`` 时清空）。"""
    with _config_warnings_lock:
        warnings = list(_config_warnings)
        if clear:
            _config_warnings.clear()
    return warnings


def reset_config_warnings() -> None:
    """清空配置回退警告（测试隔离/重新加载前调用）。"""
    with _config_warnings_lock:
        _config_warnings.clear()


# ---------------------------------------------------------------------------
# 各配置段模型（字段与 SPEC §7 一一对应）
# ---------------------------------------------------------------------------


class _SectionModel(BaseModel):
    """配置段基类：禁止未知字段（CF-9 冻结键），字符串去首尾空白。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=False, str_strip_whitespace=True)


class AppSection(_SectionModel):
    """``app:`` 应用元信息与运行目录。"""

    name: str = "QuantDesk"
    version: str = "2.0.0"
    timezone: str = "Asia/Shanghai"
    data_dir: str = "./runtime"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: Any) -> Any:
        """日志级别不区分大小写（``info`` → ``INFO``）。"""
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        """时区必须是操作系统可识别的 IANA 名称（如 Asia/Shanghai）。"""
        try:
            ZoneInfo(value)
        except Exception as exc:
            raise ValueError(
                f"时区 {value!r} 无法识别，请使用 IANA 时区名（如 Asia/Shanghai）；"
                f"Windows 如缺时区库请安装 tzdata（{exc}）"
            ) from exc
        return value

    @field_validator("data_dir")
    @classmethod
    def _check_data_dir(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("运行数据目录 app.data_dir 不能为空（默认 ./runtime）")
        return value.strip()


class DatabaseSection(_SectionModel):
    """``database:`` SQLite 连接与备份策略。"""

    url: str = "sqlite:///./runtime/quantdesk.db"
    echo: bool = False
    backup_keep: int = Field(default=7, ge=1, le=365)

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        """MVP 只支持 SQLite（SPEC §5.1），其它数据库连接串给出中文提示。"""
        if not value.startswith("sqlite://"):
            raise ValueError(
                f"数据库连接串 {value!r} 不受支持：MVP 仅支持 SQLite（应形如 sqlite:///./runtime/quantdesk.db）"
            )
        return value


class DataSection(_SectionModel):
    """``data:`` 数据源、自选池与增量更新参数。"""

    provider: Literal["akshare", "mock"] = "akshare"
    fallbacks: list[Literal["akshare", "mock"]] = Field(default_factory=lambda: ["mock"])
    universe: list[str] = Field(
        default_factory=lambda: ["600519.SH", "000001.SZ", "300750.SZ"]
    )
    history_years: int = Field(default=5, ge=1, le=30)
    adjust: Literal["hfq", "qfq", "none"] = "hfq"
    request_interval_ms: int = Field(default=300, ge=0, le=60_000)
    max_retries: int = Field(default=3, ge=0, le=10)
    stale_threshold_hours: float = Field(default=24.0, gt=0.0, le=720.0)

    @field_validator("universe")
    @classmethod
    def _check_universe(cls, value: list[str]) -> list[str]:
        """自选池：至少 1 只、代码格式合法、去重且保留顺序。"""
        if not value:
            raise ValueError("自选池 data.universe 不能为空，请至少配置 1 只标的（如 600519.SH）")
        cleaned: list[str] = []
        for item in value:
            symbol = str(item).strip().upper()
            import re

            if not re.match(_SYMBOL_PATTERN, symbol):
                raise ValueError(
                    f"自选池标的 {item!r} 格式非法：应为 6 位数字 + 交易所后缀（如 600519.SH / 000001.SZ / 899050.BJ）"
                )
            if symbol not in cleaned:
                cleaned.append(symbol)
        return cleaned

    @field_validator("fallbacks")
    @classmethod
    def _dedupe_fallbacks(cls, value: list[str]) -> list[str]:
        """降级链去重（保持顺序）。"""
        cleaned: list[str] = []
        for item in value:
            if item not in cleaned:
                cleaned.append(item)
        return cleaned


class TradingSection(_SectionModel):
    """``trading:`` 模拟账户与下单限制。"""

    account_name: str = "模拟账户"
    broker: Literal["paper", "ccxt", "qmt"] = "paper"
    initial_cash: float = Field(default=100_000.0, gt=0.0)
    min_order_amount: float = Field(default=1000.0, ge=0.0)
    allow_market_order: bool = False


class BacktestSection(_SectionModel):
    """``backtest:`` 回测默认参数。"""

    default_initial_cash: float = Field(default=100_000.0, gt=0.0)
    benchmark: str = "000300.SH"
    #: 回测价格口径：none=不复权价撮合（默认，真实价格/整手/资金核算）；hfq=合成价仅用于展示
    price_adjust: Literal["none", "hfq"] = "none"

    @field_validator("benchmark")
    @classmethod
    def _check_benchmark(cls, value: str) -> str:
        import re

        benchmark = value.strip().upper()
        if not re.match(_SYMBOL_PATTERN, benchmark):
            raise ValueError(f"回测基准 {value!r} 格式非法：应为 6 位数字 + 交易所后缀（如 000300.SH）")
        return benchmark

    @field_validator("price_adjust", mode="before")
    @classmethod
    def _fallback_price_adjust(cls, value: Any) -> Any:
        """SPEC v1.6：非法价格口径**回退 none** 并给中文报错（不阻断启动）。"""
        text = str(value).strip().lower() if value is not None else "none"
        if text not in ("none", "hfq"):
            message = (
                f"配置项 backtest.price_adjust 非法：{value!r}（仅支持 none/hfq）；"
                "已按 SPEC v1.6 回退为 none（不复权真实价撮合/算手数）"
            )
            record_config_warning(message)
            logging.getLogger("quantdesk.core.config").warning(message)
            return "none"
        return text


class AutomationJobsSection(_SectionModel):
    """``automation.jobs:`` 三个固定定时任务的时间（HH:MM）。"""

    prepare: str = "08:50"
    scan: str = "10:00"
    summarize: str = "15:30"

    @field_validator("prepare", "scan", "summarize")
    @classmethod
    def _check_time(cls, value: str) -> str:
        """校验 HH:MM 24 小时制时间。"""
        import re

        text = value.strip()
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", text):
            raise ValueError(f"定时时间 {value!r} 格式非法：应为 24 小时制 HH:MM（如 08:50 / 15:30）")
        return text


class AutomationSection(_SectionModel):
    """``automation:`` 自动化总开关与任务时间。"""

    enabled: bool = False
    jobs: AutomationJobsSection = Field(default_factory=AutomationJobsSection)


class NotifyChannelsSection(_SectionModel):
    """``notify.channels:`` 通知渠道开关。"""

    desktop: bool = True
    email: bool = False
    webhook: bool = False


class EmailSection(_SectionModel):
    """``notify.email:`` SMTP 配置（密码只从环境变量读）。"""

    smtp_host: str = ""
    smtp_port: int = Field(default=465, ge=1, le=65535)
    username: str = ""
    password_env: str = "QD_SMTP_PASSWORD"
    to: str = ""

    @field_validator("password_env")
    @classmethod
    def _check_password_env(cls, value: str) -> str:
        """密码环境变量名不能为空，且不允许把密码本身写进配置。"""
        if not value.strip():
            raise ValueError("notify.email.password_env 不能为空：密码只从环境变量读取（默认 QD_SMTP_PASSWORD）")
        return value.strip()


class WebhookSection(_SectionModel):
    """``notify.webhook:`` Webhook 地址。"""

    url: str = ""


class NotifySection(_SectionModel):
    """``notify:`` 通知总开关与渠道配置。"""

    enabled: bool = False
    channels: NotifyChannelsSection = Field(default_factory=NotifyChannelsSection)
    email: EmailSection = Field(default_factory=EmailSection)
    webhook: WebhookSection = Field(default_factory=WebhookSection)


class RecommendSection(_SectionModel):
    """``recommend:`` 选股推荐 / 异动雷达配置（Phase 2，SPEC v1.4 §7）。"""

    enabled: bool = False                      # 推荐榜单总开关（Phase 2 默认关闭）
    far_top_n: int = Field(default=20, ge=1, le=200)      # 远期榜条数
    near_top_n: int = Field(default=20, ge=1, le=200)     # 近期榜条数
    far_refresh_days: int = Field(default=20, ge=1, le=250)  # 远期榜刷新周期（交易日）
    near_refresh: Literal["daily"] = "daily"             # 近期榜刷新频率
    min_history_days: int = Field(default=250, ge=0, le=2500)  # 最短历史（不足不推荐）
    min_listed_days: int = Field(default=60, ge=0, le=2500)    # 最短上市交易日
    exclude_st: bool = True                    # 是否剔除 ST/退市/新股
    radar_enabled: bool = True                 # 异动雷达开关
    radar_volume_ratio: float = Field(default=2.0, gt=0.0, le=100.0)   # 量比阈值
    radar_turnover_pct: float = Field(default=5.0, ge=0.0, le=100.0)   # 换手率阈值（%）
    radar_amplitude_pct: float = Field(default=7.0, ge=0.0, le=100.0)  # 振幅阈值（%）
    radar_dedup_days: int = Field(default=3, ge=0, le=60)  # 同标的同类型去重天数


class ResearchSection(_SectionModel):
    """``research:`` 策略研究 / 样本外验证配置（Phase 2，SPEC v1.4 §7）。"""

    ml_enabled: bool = False                   # ML 排序（需 scikit-learn；CPU-only；缺失时降级）
    walk_forward_folds: int = Field(default=3, ge=3, le=20)  # 样本外折数（≥3）
    purge_days: int = Field(default=5, ge=0, le=60)          # 训练/验证之间 purge 交易日
    embargo_days: int = Field(default=1, ge=0, le=60)        # embargo 交易日
    label_horizon_days: int = Field(default=5, ge=1, le=60)  # ML 标签未来收益天数
    param_robustness_pct: float = Field(default=20.0, ge=0.0, le=100.0)  # 参数稳健性邻域（±%）


class AppConfig(BaseModel):
    """QuantDesk 全局配置（与 ``config.yaml`` 结构一一对应，SPEC §7 冻结）。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=False, str_strip_whitespace=True)

    app: AppSection = Field(default_factory=AppSection)
    database: DatabaseSection = Field(default_factory=DatabaseSection)
    data: DataSection = Field(default_factory=DataSection)
    fees: FeeConfig = Field(default_factory=FeeConfig)
    trading: TradingSection = Field(default_factory=TradingSection)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    backtest: BacktestSection = Field(default_factory=BacktestSection)
    automation: AutomationSection = Field(default_factory=AutomationSection)
    notify: NotifySection = Field(default_factory=NotifySection)
    # Phase 2（SPEC v1.4）：MVP 阶段忽略、但必须能通过配置校验（CF-9 扩展）
    recommend: RecommendSection = Field(default_factory=RecommendSection)
    research: ResearchSection = Field(default_factory=ResearchSection)

    #: 配置文件所在目录（用于解析相对路径；由 load_config 注入）
    _base_dir: Path = PrivateAttr(default=PROJECT_ROOT)
    #: 配置来源文件路径（便于日志/设置页展示）
    _source_path: Path | None = PrivateAttr(default=None)

    # -- 交叉字段校验 -----------------------------------------------------
    @model_validator(mode="after")
    def _cross_check(self) -> "AppConfig":
        """跨字段配置一致性检查（给出中文可操作提示）。"""
        if (
            self.risk.enabled
            and self.risk.max_position_per_symbol_pct > self.risk.max_total_position_pct
        ):
            raise ValueError(
                f"风控参数不合法：单票仓位上限 {self.risk.max_position_per_symbol_pct}% "
                f"不应大于总仓位上限 {self.risk.max_total_position_pct}%"
            )
        return self

    # -- 路径与派生配置 ---------------------------------------------------
    @property
    def base_dir(self) -> Path:
        """配置文件所在目录（相对路径以此为基准）。"""
        return self._base_dir

    @property
    def source_path(self) -> Path | None:
        """配置来源文件路径（未从文件加载时为 ``None``）。"""
        return self._source_path

    def resolve_path(self, raw: str | Path) -> Path:
        """把配置中的相对路径解析为绝对路径（相对配置文件目录）。"""
        path = Path(raw)
        if path.is_absolute():
            return path
        return (self._base_dir / path).resolve()

    @property
    def data_dir_path(self) -> Path:
        """运行期数据目录（``app.data_dir``，已解析为绝对路径）。"""
        return self.resolve_path(self.app.data_dir)

    @property
    def log_dir(self) -> Path:
        """日志目录：``<data_dir>/logs``。"""
        return self.data_dir_path / "logs"

    @property
    def backup_dir(self) -> Path:
        """备份目录：``<data_dir>/backups``。"""
        return self.data_dir_path / "backups"

    @property
    def export_dir(self) -> Path:
        """导出目录：``<data_dir>/exports``。"""
        return self.data_dir_path / "exports"

    @property
    def sqlite_path(self) -> Path | None:
        """SQLite 数据库文件绝对路径；内存库/非 SQLite 返回 ``None``。"""
        url = self.database.url
        if not url.startswith("sqlite:///"):
            return None
        tail = url[len("sqlite:///"):]
        if not tail or tail == ":memory:" or tail.startswith("file:"):
            return None
        return self.resolve_path(tail)

    def risk_config(self) -> RiskConfig:
        """合并 ``risk`` 段与 ``trading`` / ``automation`` 中的风控相关参数。

        说明（SPEC v1.2 §9.1）：
        - ``min_order_amount`` 的配置键在 ``trading`` 段；
        - ``allow_market_order`` 在 ``trading`` 段；
        - ``allow_auto_orders``（自动交易授权，默认 false → AUTO 单 RK020 fail-closed）在 ``risk`` 段，
          且 ``automation.enabled=true`` 时按 SPEC §8.6 规则 9 自动映射为授权；
        - ``automation.enabled`` 同时供 RK020 判定；
        这些项统一合并成 :class:`RiskConfig` 交给 ``RiskGate``。
        """
        merged = self.risk.model_dump()
        merged.update(
            min_order_amount=self.trading.min_order_amount,
            allow_market_order=self.trading.allow_market_order,
            automation_enabled=self.automation.enabled,
        )
        # 自动交易授权：显式 risk.allow_auto_orders=true 或 automation.enabled=true 均视为授权
        merged["allow_auto_orders"] = bool(
            merged.get("allow_auto_orders", False) or self.automation.enabled
        )
        return RiskConfig(**merged)

    def fee_config(self) -> FeeConfig:
        """返回费用配置（``fees`` 段，契约 :class:`FeeConfig`）。"""
        return self.fees.model_copy(deep=True)

    def to_dict(self) -> dict[str, Any]:
        """转为可 JSON 序列化的普通字典（用于日志/设置页展示；不含私钥）。"""
        return self.model_dump(mode="json")


# ---------------------------------------------------------------------------
# 冻结键校验 / 环境变量覆盖
# ---------------------------------------------------------------------------


def collect_leaf_paths(node: Mapping[str, Any], prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """收集字典中所有叶子节点的路径（``app.log_level`` → ``("app","log_level")``）。"""
    paths: list[tuple[str, ...]] = []
    for key, value in node.items():
        path = (*prefix, str(key))
        if isinstance(value, Mapping):
            paths.extend(collect_leaf_paths(value, path))
        else:
            paths.append(path)
    return paths


def _path_join(path: tuple[str, ...]) -> str:
    return ".".join(path)


def validate_frozen_keys(raw: Mapping[str, Any]) -> None:
    """校验配置键与 SPEC §7 冻结清单完全一致（缺段/缺键/未知键均报中文错误）。

    :raises ConfigError: 键集合不符合冻结清单
    """
    if not isinstance(raw, Mapping):
        raise ConfigError("配置文件根节点必须是映射（YAML 顶层应为 app/database/data/... 各段）")
    problems: list[str] = []
    for section, expected in _FROZEN_CONFIG_KEYS.items():
        # 找到该段在 raw 中的实际值
        node: Any = raw
        found = True
        for part in section.split("."):
            if isinstance(node, Mapping) and part in node:
                node = node[part]
            else:
                found = False
                break
        if not found:
            problems.append(f"缺少配置段 {section}（SPEC §7 冻结）")
            continue
        if not isinstance(node, Mapping):
            problems.append(f"配置段 {section} 必须是映射（当前为 {type(node).__name__}）")
            continue
        actual = set(node.keys())
        unknown = actual - expected
        missing = expected - actual
        if unknown:
            problems.append(
                f"配置段 {section} 含未知配置项：{', '.join(sorted(unknown))}"
                f"（SPEC §7 未定义，禁止自创键；如需新增请走 Lead 契约变更流程）"
            )
        if missing:
            problems.append(f"配置段 {section} 缺少配置项：{', '.join(sorted(missing))}")
    if problems:
        raise ConfigError("配置键校验失败：\n- " + "\n- ".join(problems))


def _env_name(path: tuple[str, ...]) -> str:
    """叶子路径 → 环境变量名：``('data','provider')`` → ``QD_DATA_PROVIDER``。"""
    return CONFIG_ENV_PREFIX + "_".join(part.upper() for part in path)


def _set_path(root: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    """把 ``value`` 写入 ``root`` 的指定嵌套路径（中间缺字典则创建）。"""
    node = root
    for part in path[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[path[-1]] = value


_TRUE_TEXT = {"1", "true", "yes", "on", "y", "t", "是"}
_FALSE_TEXT = {"0", "false", "no", "off", "n", "f", "否"}


def _coerce_env_value(env_name: str, text: str, current: Any) -> Any:
    """按配置项当前类型把环境变量字符串转成正确类型（失败给中文错误）。"""
    raw = text.strip()
    if isinstance(current, bool):
        lowered = raw.lower()
        if lowered in _TRUE_TEXT:
            return True
        if lowered in _FALSE_TEXT:
            return False
        raise ConfigError(
            f"环境变量 {env_name} 非法：需要布尔值（true/false、1/0、yes/no），当前为 {text!r}"
        )
    if isinstance(current, int) and not isinstance(current, bool):
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigError(f"环境变量 {env_name} 非法：需要整数，当前为 {text!r}") from exc
    if isinstance(current, float):
        try:
            return float(raw)
        except ValueError as exc:
            raise ConfigError(f"环境变量 {env_name} 非法：需要数字，当前为 {text!r}") from exc
    if isinstance(current, (list, dict)):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"环境变量 {env_name} 非法：需要 JSON 格式（如 '[\"600519.SH\"]'），当前为 {text!r}"
            ) from exc
    if current is None:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def apply_env_overrides(
    raw: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], list[tuple[str, tuple[str, ...], str]]]:
    """应用 ``QD_`` 前缀环境变量覆盖，返回（新配置字典, 已应用列表）。

    覆盖名称规则：``QD_`` + 配置路径（大写，点换下划线），例如：

    - ``QD_APP_LOG_LEVEL=DEBUG`` → ``app.log_level``
    - ``QD_DATABASE_URL=sqlite:///./runtime/test.db`` → ``database.url``
    - ``QD_DATA_UNIVERSE='["600519.SH","000001.SZ"]'`` → ``data.universe``
    - ``QD_AUTOMATION_JOBS_SCAN=09:45`` → ``automation.jobs.scan``

    说明：只对配置文件中**已存在**的键做覆盖（缺失键由 SPEC 默认值兜底）；
    未知 ``QD_*`` 变量（如密码 ``QD_SMTP_PASSWORD``）会被忽略，不视为配置错误。
    """
    env = environ if environ is not None else os.environ
    data = copy.deepcopy(dict(raw))
    applied: list[tuple[str, tuple[str, ...], str]] = []

    # 配置文件中所有叶子路径 → 环境变量名（同一变量名冲突时以先出现者为准）
    mapping: dict[str, tuple[str, ...]] = {}
    for path in collect_leaf_paths(data):
        mapping.setdefault(_env_name(path), path)

    for env_name in sorted(env.keys()):
        if not env_name.startswith(CONFIG_ENV_PREFIX):
            continue
        if env_name == CONFIG_FILE_ENV:
            continue
        path = mapping.get(env_name)
        if path is None:
            continue  # 未知/非配置类环境变量（如密码），忽略
        current = data
        for part in path:
            current = current[part]
        value = _coerce_env_value(env_name, env[env_name], current)
        _set_path(data, path, value)
        applied.append((env_name, path, env[env_name]))
    return data, applied


# ---------------------------------------------------------------------------
# 校验错误 → 中文提示
# ---------------------------------------------------------------------------

_ERROR_TYPE_MESSAGES: dict[str, str] = {
    "missing": "缺少必填配置项",
    "int_parsing": "必须是整数",
    "int_type": "必须是整数",
    "float_parsing": "必须是数字",
    "float_type": "必须是数字",
    "bool_parsing": "必须是布尔值 true/false",
    "bool_type": "必须是布尔值 true/false",
    "string_type": "必须是字符串",
    "list_type": "必须是列表",
    "dict_type": "必须是映射",
    "literal_error": "取值不在允许范围内",
    "extra_forbidden": "未知配置项（SPEC §7 未定义，禁止自创键）",
    "greater_than": "必须大于下限",
    "greater_than_equal": "必须大于等于下限",
    "less_than": "必须小于上限",
    "less_than_equal": "必须小于等于上限",
}


def _format_validation_error(exc: ValidationError) -> str:
    """把 pydantic 校验错误翻译为中文（包含字段路径与当前值）。"""
    lines: list[str] = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error.get("loc", ()))
        err_type = str(error.get("type", ""))
        message = str(error.get("msg", ""))
        # pydantic 对字段校验器抛出的 ValueError 会加前缀 "Value error, "
        for prefix in ("Value error, ", "Assertion failed, "):
            if message.startswith(prefix):
                message = message[len(prefix):]
        translated = _ERROR_TYPE_MESSAGES.get(err_type)
        if translated and not any(ch >= "\u4e00" for ch in message):
            message = f"{translated}（{message}）"
        elif not translated and not any(ch >= "\u4e00" for ch in message):
            message = f"取值非法（{message}）"
        current = error.get("input", "<未提供>")
        if loc:
            lines.append(f"配置项 {loc} 非法：{message}；当前值 = {current!r}")
        else:
            lines.append(f"配置非法：{message}；当前值 = {current!r}")
    return "配置校验失败：\n- " + "\n- ".join(lines)


# ---------------------------------------------------------------------------
# 加载入口
# ---------------------------------------------------------------------------


def _read_yaml(config_path: Path) -> dict[str, Any]:
    """读取 YAML 文件；文件不存在/语法错误/根节点非映射时给中文错误。"""
    if not config_path.exists():
        raise ConfigError(
            f"配置文件不存在：{config_path}\n"
            f"请确认 quantdesk/config.yaml 存在（默认路径 {DEFAULT_CONFIG_PATH}），"
            f"或用环境变量 {CONFIG_FILE_ENV} 指定路径"
        )
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"配置文件读取失败：{config_path}（{exc}）") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件 YAML 语法错误：{config_path}\n{exc}") from exc
    if raw is None:
        raise ConfigError(f"配置文件为空：{config_path}（应包含 SPEC §7 各配置段）")
    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件根节点必须是映射（dict），当前为 {type(raw).__name__}：{config_path}")
    return raw


def load_config(
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    use_env: bool = True,
    validate_keys: bool = True,
) -> AppConfig:
    """加载并校验配置，返回 :class:`AppConfig`。

    :param path: 配置文件路径；为空时依次取 ``QD_CONFIG_FILE`` 环境变量、``quantdesk/config.yaml``
    :param environ: 环境变量映射（默认 ``os.environ``；测试可注入）
    :param use_env: 是否应用 ``QD_`` 前缀环境变量覆盖
    :param validate_keys: 是否校验配置键与 SPEC §7 冻结清单一致（默认开启）
    :raises ConfigError: 文件不存在 / YAML 语法错误 / 键集合不符 / 字段非法
    """
    env = environ if environ is not None else os.environ
    reset_config_warnings()  # 每次加载清空回退警告，避免跨次累积
    if path is None:
        path = env.get(CONFIG_FILE_ENV) or DEFAULT_CONFIG_PATH
    config_path = Path(path)
    raw = _read_yaml(config_path)

    if validate_keys:
        validate_frozen_keys(raw)

    if use_env:
        raw, _applied = apply_env_overrides(raw, env)

    try:
        config = AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from exc

    config._base_dir = config_path.resolve().parent
    config._source_path = config_path.resolve()
    return config


# ---------------------------------------------------------------------------
# 单例访问
# ---------------------------------------------------------------------------

_config: AppConfig | None = None
_config_lock = threading.RLock()


def get_config(
    *, refresh: bool = False, path: str | Path | None = None, environ: Mapping[str, str] | None = None
) -> AppConfig:
    """获取全局配置单例（首次调用时加载；``refresh=True`` 强制重新加载）。"""
    global _config
    with _config_lock:
        if _config is None or refresh:
            _config = load_config(path, environ=environ)
        return _config


def reload_config(
    path: str | Path | None = None, *, environ: Mapping[str, str] | None = None
) -> AppConfig:
    """重新加载配置文件并替换全局单例。"""
    return get_config(refresh=True, path=path, environ=environ)


def _reset_database_singleton() -> None:
    """配置切换时重置 ``core.db`` 的全局 engine/session 单例（延迟导入避免循环依赖）。"""
    try:
        from .db import reset_database  # 延迟导入：core/db.py 依赖本模块
    except Exception:  # pragma: no cover - 极端导入失败时忽略
        return
    try:
        reset_database()
    except Exception:  # pragma: no cover - 重置失败不应阻断配置切换
        pass


def set_config(config: AppConfig, *, reset_database: bool | None = None) -> None:
    """注入配置单例；``database.url`` / ``app.data_dir`` 变化时自动重置全局数据库单例。

    M3-07 设置页修改配置后无需重启即可生效（换库不重启）。

    :param reset_database: 显式指定是否重置数据库单例；``None`` 表示仅在数据库 URL 或
                           数据目录变化时自动重置（首次注入不重置，避免无谓开销）
    """
    global _config
    with _config_lock:
        previous = _config
        _config = config
    should_reset = reset_database
    if should_reset is None:
        should_reset = previous is not None and (
            previous.database.url != config.database.url
            or previous.app.data_dir != config.app.data_dir
        )
    if should_reset:
        _reset_database_singleton()


def reset_config() -> None:
    """清空配置单例，并**连锁重置**数据库/会话单例（测试隔离与配置热切换用）。"""
    global _config
    with _config_lock:
        _config = None
    _reset_database_singleton()


# ---------------------------------------------------------------------------
# 自检（quantdesk 目录：``python -m core.config``）
# ---------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    import shutil
    import tempfile

    # 1) 项目自带 config.yaml 可加载，且键与 SPEC §7 完全一致
    cfg = load_config(environ={})
    assert cfg.app.name == "QuantDesk" and cfg.app.log_level == "INFO"
    assert cfg.data.provider == "akshare" and cfg.data.universe == ["600519.SH", "000001.SZ", "300750.SZ"]
    assert cfg.database.backup_keep == 7 and cfg.data.history_years == 5
    assert cfg.fees.commission_rate == 0.00025 and cfg.fees.slippage_bps == 5.0
    assert cfg.trading.initial_cash == 100000.0 and cfg.trading.min_order_amount == 1000.0
    assert cfg.automation.enabled is False and cfg.automation.jobs.scan == "10:00"
    assert cfg.backtest.default_initial_cash == 100000.0 and cfg.backtest.benchmark == "000300.SH"
    assert cfg.backtest.price_adjust == "none"  # R6：默认用不复权价撮合，hfq 仅合成价展示
    assert cfg.notify.enabled is False and cfg.notify.channels.desktop is True
    # Phase 2（SPEC v1.4）：recommend/research 键存在且默认值正确（MVP 阶段不启用）
    assert cfg.recommend.enabled is False and cfg.recommend.far_top_n == 20
    assert cfg.recommend.radar_volume_ratio == 2.0 and cfg.recommend.radar_dedup_days == 3
    assert cfg.research.ml_enabled is False and cfg.research.walk_forward_folds == 3
    assert cfg.research.purge_days == 5 and cfg.research.embargo_days == 1
    assert cfg.research.label_horizon_days == 5 and cfg.research.param_robustness_pct == 20.0
    assert isinstance(cfg.fees, FeeConfig) and isinstance(cfg.risk, RiskConfig)
    # SPEC v1.2 §7：risk.allow_auto_orders 默认 false（AUTO 单 RK020 fail-closed）
    assert cfg.risk.model_dump().get("allow_auto_orders") is False
    assert cfg.sqlite_path is not None and cfg.sqlite_path.name == "quantdesk.db"
    assert cfg.log_dir.name == "logs" and cfg.backup_dir.name == "backups"

    # 派生配置：合并 trading/automation 到风控参数
    risk_cfg = cfg.risk_config()
    assert risk_cfg.min_order_amount == 1000.0
    assert risk_cfg.allow_market_order is False and risk_cfg.automation_enabled is False
    assert risk_cfg.model_dump().get("allow_auto_orders") is False  # 默认未授权自动交易

    # 2) 环境变量覆盖（类型按原配置项推断）
    overrides = {
        "QD_APP_LOG_LEVEL": "debug",
        "QD_DATA_UNIVERSE": '["600519.SH", "000001.SZ"]',
        "QD_DATA_PROVIDER": "mock",
        "QD_TRADING_INITIAL_CASH": "200000",
        "QD_TRADING_ALLOW_MARKET_ORDER": "true",
        "QD_RISK_MAX_TOTAL_POSITION_PCT": "60",
        "QD_RISK_ALLOW_AUTO_ORDERS": "true",
        "QD_AUTOMATION_ENABLED": "yes",
        "QD_AUTOMATION_JOBS_SCAN": "09:45",
        "QD_NOTIFY_EMAIL_SMTP_PORT": "587",
        "QD_RECOMMEND_ENABLED": "true",
        "QD_RECOMMEND_FAR_TOP_N": "30",
        "QD_RESEARCH_WALK_FORWARD_FOLDS": "5",
        "QD_BACKTEST_PRICE_ADJUST": "hfq",
        "QD_SMTP_PASSWORD": "should-be-ignored",  # 密码类环境变量不属于配置项
    }
    cfg_env = load_config(environ=overrides)
    assert cfg_env.app.log_level == "DEBUG"
    assert cfg_env.data.provider == "mock" and cfg_env.data.universe == ["600519.SH", "000001.SZ"]
    assert cfg_env.trading.initial_cash == 200000.0 and cfg_env.trading.allow_market_order is True
    assert cfg_env.risk.max_total_position_pct == 60.0
    assert cfg_env.risk.model_dump().get("allow_auto_orders") is True
    assert cfg_env.risk_config().model_dump().get("allow_auto_orders") is True  # 显式授权
    assert cfg_env.automation.enabled is True and cfg_env.automation.jobs.scan == "09:45"
    assert cfg_env.notify.email.smtp_port == 587
    assert cfg_env.recommend.enabled is True and cfg_env.recommend.far_top_n == 30
    assert cfg_env.research.walk_forward_folds == 5  # Phase 2 键同样支持 QD_ 覆盖
    assert cfg_env.backtest.price_adjust == "hfq"    # R6：backtest.price_adjust 支持 QD_ 覆盖
    # automation.enabled=true 自动映射为自动交易授权（SPEC §8.6 规则 9）
    assert cfg_env.risk_config().automation_enabled is True
    raw, applied = apply_env_overrides({"app": {"log_level": "INFO"}}, overrides)
    # 环境变量覆盖只做原始值替换（大小写归一化由 pydantic 校验层完成）
    assert raw["app"]["log_level"] == "debug"
    assert all(isinstance(item, tuple) and len(item) == 3 for item in applied)

    # 2.1) SPEC v1.6：非法 price_adjust 回退 none + 中文报错（不阻断配置加载）
    cfg_fallback = load_config(environ={"QD_BACKTEST_PRICE_ADJUST": "qfq"})
    assert cfg_fallback.backtest.price_adjust == "none", cfg_fallback.backtest.price_adjust
    fallback_warnings = get_config_warnings()
    assert fallback_warnings, "非法 price_adjust 未产生中文回退警告"
    assert "price_adjust" in fallback_warnings[0] and "回退" in fallback_warnings[0], fallback_warnings
    reset_config_warnings()

    def _mutate_config(mutator) -> Path:
        """复制项目 config.yaml 到临时目录后按需修改，返回临时文件路径。"""
        base = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
        mutator(base)
        tmp_dir = Path(tempfile.mkdtemp(prefix="qd_cfg_"))
        path = tmp_dir / "config.yaml"
        path.write_text(yaml.safe_dump(base, allow_unicode=True), encoding="utf-8")
        return path

    # 3) 非法路径：日志级别 / 初始资金 / 未知键 / 未知配置段
    cases = [
        (lambda c: c["app"].__setitem__("log_level", "VERBOSE"), "app.log_level"),
        (lambda c: c["trading"].__setitem__("initial_cash", -5), "trading.initial_cash"),
        (lambda c: c["app"].__setitem__("foo", 1), "未知配置项"),
        (lambda c: c["fees"].__setitem__("commission_rate", 1.5), "fees.commission_rate"),
        (lambda c: c["automation"]["jobs"].__setitem__("scan", "25:00"), "automation.jobs.scan"),
        (lambda c: c["database"].__setitem__("url", "postgresql://x"), "database.url"),
        (lambda c: c["notify"]["email"].__setitem__("password", "plain"), "未知配置项"),
        (lambda c: c["recommend"].__setitem__("far_top_n", 0), "recommend.far_top_n"),
        (lambda c: c["research"].__setitem__("walk_forward_folds", 2), "research.walk_forward_folds"),
    ]
    for mutator, expected_field in cases:
        bad_path = _mutate_config(mutator)
        try:
            load_config(bad_path, environ={})
        except ConfigError as exc:
            text = str(exc)
            assert expected_field in text, f"期望错误消息包含 {expected_field!r}，实际：{text}"
            assert any(ch >= "\u4e00" for ch in text), f"错误消息必须中文：{text}"
        else:
            raise AssertionError(f"非法配置未被拦截：{expected_field}")
        finally:
            shutil.rmtree(bad_path.parent, ignore_errors=True)

    # 4) 非法环境变量值（布尔/JSON）
    for env, expected in (
        ({"QD_TRADING_ALLOW_MARKET_ORDER": "maybe"}, "QD_TRADING_ALLOW_MARKET_ORDER"),
        ({"QD_DATA_UNIVERSE": "600519.SH"}, "QD_DATA_UNIVERSE"),
    ):
        try:
            load_config(environ=env)
        except ConfigError as exc:
            assert expected in str(exc), str(exc)
        else:
            raise AssertionError(f"非法环境变量未被拦截：{env}")

    # 5) 文件缺失 / YAML 语法错误
    try:
        load_config(PROJECT_ROOT / "not-exists.yaml", environ={})
    except ConfigError as exc:
        assert "配置文件不存在" in str(exc)
    else:
        raise AssertionError("缺失配置文件未被拦截")
    broken_path = Path(tempfile.mkdtemp(prefix="qd_cfg_")) / "broken.yaml"
    broken_path.write_text("app: [unclosed", encoding="utf-8")
    try:
        load_config(broken_path, environ={})
    except ConfigError as exc:
        assert "YAML" in str(exc)
    else:
        raise AssertionError("YAML 语法错误未被拦截")
    finally:
        shutil.rmtree(broken_path.parent, ignore_errors=True)

    # 6) 单例
    reset_config()
    first = get_config(environ={})
    assert get_config(environ={}) is first
    reset_config()
    assert get_config(environ={}) is not first

    # 7) 配置热切换：set_config 换库自动重置全局数据库单例（M3-07 换库不重启）
    # 注意：必须使用 core.config 模块函数（而非 __main__ 副本），否则单例会落在两个模块里
    import core.config as config_module
    from core import db as core_db
    from core.db import get_database

    core_db.reset_database()
    config_module.reset_config()
    cfg_a = load_config(environ={})
    config_module.set_config(cfg_a)
    assert config_module.get_config() is cfg_a
    db_a = get_database()
    assert db_a.config is cfg_a
    cfg_b = cfg_a.model_copy(deep=True)
    cfg_b.database.url = "sqlite:///./runtime/quantdesk_hotswitch_test.db"
    config_module.set_config(cfg_b)  # database.url 变化 → 自动 reset_database()
    db_b = get_database()
    assert db_b is not db_a and db_b.config is cfg_b, "换库未重置全局数据库单例"
    config_module.reset_config()  # 连锁重置数据库单例
    assert core_db._database is None
    core_db.reset_database()
    reset_config()  # 同步清理本模块（__main__）的单例

    print(
        "core/config.py 自检通过：默认加载/键集合冻结校验/QD_ 环境变量覆盖（含类型转换）/"
        "非法值中文定位/文件缺失与 YAML 语法错误/风控费用派生配置/单例"
    )

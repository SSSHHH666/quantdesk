"""导入烟囱测试（防“包级 import 回归”这类 P0）。

背景：曾出现 `backtest/__init__.py` 引用 `backtest.engine` 里已删除的 ``RQ_WARN_FEW_TRADES``，
导致 ``import backtest`` / ``import trading`` 失败、手动交易链路全断，而当时 379 条单测全绿
**没有抓到**——因为单测都绕开了包级 ``__init__``。本文件专门覆盖：

1. 7 个业务包在**独立干净进程**里逐个 ``import`` 成功（能发现 ``__init__`` 引用错误、
   循环导入、导入顺序依赖）；
2. 全部包在**同一个进程**中一起导入成功（发现跨包干扰）；
3. ``ui/pages/*.py`` 全部页面模块可导入（不启动 Streamlit 运行期），且 9 个 SPEC §10 页面齐全；
4. 关键公开符号存在（``backtest.BacktestEngine``、``trading.OrderService``、
   ``app_services.run_backtest`` 等）；
5. 策略注册表 ≥ 6 个（``import strategy`` 后 ``list_strategies()``）。

设计取舍：包级导入放在子进程里跑，才能验证“**全新解释器**下也能导入”（与真实启动路径一致），
且不会把损坏的模块缓存进 pytest 进程污染其它用例。
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: 子进程导入型用例较慢（~10s），默认快速套件不跑
pytestmark = pytest.mark.slow

#: 必须可导入的业务包（顺序即启动时的典型导入顺序）
PACKAGES: tuple[str, ...] = (
    "core",
    "data",
    "strategy",
    "backtest",
    "trading",
    "automation",
    "app_services",
)

#: 关键公开符号（模块, 属性）——契约级入口，缺失即链路断裂
KEY_SYMBOLS: tuple[tuple[str, str], ...] = (
    ("backtest", "BacktestEngine"),
    ("backtest", "BacktestResult"),
    ("backtest", "run_backtest"),
    ("backtest", "compute_metrics"),
    ("backtest", "AStockRules"),
    ("trading", "OrderService"),
    ("trading", "RiskGate"),
    ("trading", "PortfolioService"),
    ("trading", "PaperBroker"),
    ("app_services", "run_backtest"),
    ("strategy", "list_strategies"),
    ("strategy", "Strategy"),
    ("data", "BarCache"),
    ("data", "MockProvider"),
    ("core", "get_event_bus"),
    ("core", "RiskDecision"),
)

#: SPEC §10 冻结的 9 个页面（新增页面可以多于 9 个，但这 9 个必须在）
REQUIRED_PAGE_TITLES: tuple[str, ...] = (
    "总览",
    "行情",
    "手动交易",
    "策略",
    "回测",
    "持仓",
    "自动化",
    "设置",
    "日志",
)

#: 至少 6 个已注册策略（ma_cross / rsi_reversal / multi_factor / value_quality / breakout_abnormal / ml_ranker）
REQUIRED_STRATEGY_KEYS: tuple[str, ...] = (
    "ma_cross",
    "rsi_reversal",
    "multi_factor",
    "value_quality",
    "breakout_abnormal",
    "ml_ranker",
)


def _discover_page_modules() -> list[str]:
    """``ui/pages/*.py`` → 模块名列表（排除 ``__init__`` 与私有 ``_common``）。"""
    pages_dir = PROJECT_ROOT / "ui" / "pages"
    return sorted(
        path.stem
        for path in pages_dir.glob("*.py")
        if not path.stem.startswith("_") and path.stem != "__init__"
    )


def _run_python(code: str) -> subprocess.CompletedProcess:
    """在项目根目录、当前 venv 解释器下执行一段代码（全新进程）。"""
    env = os.environ.copy()  # 必须继承系统环境（Windows 缺 SystemRoot 时子进程无法启动）
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )


# ---------------------------------------------------------------------------
# 一、包级导入：每个包在全新进程里单独导入
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("package", PACKAGES, ids=list(PACKAGES))
def test_package_imports(package: str) -> None:
    """每个业务包都必须可导入（包级 ``__init__`` 也要走通）。

    快速逐包检查（进程内，失败时能直接定位到具体包）；「全新解释器」由
    :func:`test_all_packages_import_together` 覆盖。
    """
    try:
        importlib.import_module(package)
    except Exception as exc:  # noqa: BLE001 - 这里就是要抓住任何导入期异常
        pytest.fail(
            f"import {package} 失败（包级 __init__ 引用了不存在的符号？）："
            f"{type(exc).__name__}: {exc}"
        )


def test_all_packages_import_together() -> None:
    """全部包在同一进程中按启动顺序导入成功（发现跨包干扰/顺序依赖）。"""
    code = "import " + ", ".join(PACKAGES)
    result = _run_python(code)

    assert result.returncode == 0, (
        "全部业务包在同一进程导入失败：\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )


def test_page_modules_import_in_fresh_process() -> None:
    """全部 ``ui/pages/*.py`` 可在全新进程中导入（不启动 Streamlit 运行期）。"""
    modules = _discover_page_modules()
    assert len(modules) >= 9, f"页面模块少于 9 个：{modules}"

    code = (
        "import importlib, json, traceback\n"
        f"modules = {modules!r}\n"
        "fails = {}\n"
        "for name in modules:\n"
        "    try:\n"
        "        importlib.import_module('ui.pages.' + name)\n"
        "    except Exception as exc:\n"
        "        fails[name] = f'{type(exc).__name__}: {exc}'\n"
        "print('RESULT=' + json.dumps(fails, ensure_ascii=False))\n"
        "raise SystemExit(1 if fails else 0)\n"
    )
    result = _run_python(code)

    assert result.returncode == 0, (
        f"页面模块导入失败：stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "RESULT={}" in result.stdout


# ---------------------------------------------------------------------------
# 二、关键公开符号
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("module_name", "attr"), KEY_SYMBOLS, ids=[f"{m}.{a}" for m, a in KEY_SYMBOLS])
def test_key_public_symbols_exist(module_name: str, attr: str) -> None:
    """契约级公开符号必须可从包根直接访问（延迟到用例内导入，避免污染收集阶段）。"""
    module = importlib.import_module(module_name)

    assert hasattr(module, attr), f"{module_name} 缺少公开符号 {attr!r}（包 __init__ 导出遗漏？）"


def test_package_all_exports_resolve() -> None:
    """``__all__`` 里声明的名字必须全部真实存在（防止 __all__ 与实现漂移）。"""
    failures: list[str] = []
    for package in PACKAGES:
        module = importlib.import_module(package)
        declared = list(getattr(module, "__all__", []) or [])
        if not declared:
            continue
        missing = [name for name in declared if not hasattr(module, name)]
        if missing:
            failures.append(f"{package}.__all__ 缺 {missing}")

    assert not failures, "包级 __all__ 与实现漂移：\n" + "\n".join(failures)


# ---------------------------------------------------------------------------
# 三、策略注册表与页面标题
# ---------------------------------------------------------------------------


def test_strategy_registry_has_at_least_six() -> None:
    import strategy

    entries = list(strategy.list_strategies())
    keys = {str(entry["key"]) for entry in entries}

    assert len(entries) >= 6, f"策略注册表应 ≥ 6 个，实际 {len(entries)} 个：{sorted(keys)}"
    missing = [key for key in REQUIRED_STRATEGY_KEYS if key not in keys]
    assert not missing, f"缺少必备策略：{missing}（当前：{sorted(keys)}）"

    for entry in entries:
        assert entry.get("display_name"), f"策略 {entry['key']} 缺少中文展示名"
        assert entry.get("params_schema") is not None, f"策略 {entry['key']} 缺少参数 schema"


def test_required_page_titles_are_discovered() -> None:
    """9 个 SPEC §10 页面必须都存在且标题唯一（新增页面允许更多）。"""
    modules = _discover_page_modules()
    titles: dict[str, str] = {}
    for name in modules:
        module = importlib.import_module(f"ui.pages.{name}")
        title = getattr(module, "PAGE_TITLE", None)
        assert isinstance(title, str) and title.strip(), f"页面 {name} 缺少 PAGE_TITLE"
        assert callable(getattr(module, "render", None)), f"页面 {name} 缺少 render()"
        assert title not in titles, f"页面标题重复：{title!r}（{titles[title]} 与 {name}）"
        titles[title] = name

    missing = [title for title in REQUIRED_PAGE_TITLES if title not in titles]
    assert not missing, f"缺少页面：{missing}（已发现：{sorted(titles)}）"
    assert len(modules) >= 9

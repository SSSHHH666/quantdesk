#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""QuantDesk 环境自检脚本（M0-08）。

用途：
    检查当前 Python 解释器、虚拟环境以及关键依赖是否满足 QuantDesk 的运行要求，
    并在缺少依赖时给出可直接复制的安装命令。

用法（在项目根目录或任意目录均可）：
    .venv\\Scripts\\python.exe scripts\\check_env.py

退出码：
    0 —— 核心依赖全部齐备（可选依赖缺失不算失败）
    1 —— 存在核心依赖缺失，或 Python 版本 / 解释器不满足要求

说明：
    本脚本只做“检测 + 提示”，不会自动安装任何依赖，不会写入数据库，
    也不会启动 Streamlit 服务，可反复安全执行。
"""

from __future__ import annotations

import importlib.metadata as metadata
import importlib.util
import os
import platform
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量与依赖清单
# ---------------------------------------------------------------------------

# scripts/check_env.py -> 上一级即 quantdesk 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"

MIN_PYTHON = (3, 10)  # QuantDesk 最低 Python 版本要求

# (展示名, 导入模块名, 发行包名, 用途说明, 是否核心依赖)
DEPENDENCIES: list[tuple[str, str, str, str, bool]] = [
    ("streamlit", "streamlit", "streamlit", "Web UI 框架（页面与交互）", True),
    ("pandas", "pandas", "pandas", "表格与时间序列数据处理", True),
    ("plotly", "plotly", "plotly", "交互式行情 / 权益曲线图表", True),
    ("SQLAlchemy", "sqlalchemy", "SQLAlchemy", "ORM 与 SQLite 数据库访问", True),
    ("pydantic", "pydantic", "pydantic", "配置与领域模型校验", True),
    ("PyYAML", "yaml", "PyYAML", "读取 config.yaml 配置", True),
    ("APScheduler", "apscheduler", "APScheduler", "盘前 / 盘中 / 盘后定时任务", True),
    ("akshare", "akshare", "akshare", "A 股行情与基本面数据源", True),
    ("numpy", "numpy", "numpy", "数值计算（pandas 的底层依赖）", True),
    ("pytest", "pytest", "pytest", "单元测试与验收测试（开发依赖）", False),
    ("TA-Lib", "talib", "TA-Lib", "技术指标（缺失时自动使用 pandas 兜底）", False),
    ("ccxt", "ccxt", "ccxt", "加密交易所适配（可选，不装不影响 A 股流程）", False),
]

STATUS_OK = "正常"
STATUS_MISSING = "缺失"
STATUS_METADATA_ONLY = "已装"
STATUS_ERROR = "异常"


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def display_width(text: str) -> int:
    """按终端显示宽度计算字符串宽度（中文按 2 列），用于对齐表格。"""
    width = 0
    for char in text:
        if unicodedata.east_asian_width(char) in ("W", "F"):
            width += 2
        else:
            width += 1
    return width


def pad(text: str, width: int) -> str:
    """右侧补空格到指定显示宽度。"""
    return text + " " * max(0, width - display_width(text))


def quote_path(path: str) -> str:
    """在需要时为路径加引号，便于用户复制到命令行执行。"""
    return f'"{path}"' if " " in path else path


def detect_package(module_name: str, dist_name: str) -> tuple[str, str | None, str | None]:
    """检测一个依赖的安装状态，并实际导入一次以发现“装了但坏了”的情况。

    返回 (状态, 版本号, 备注)：
    - (正常, 版本, None)                     元数据存在且导入成功
    - (已装, None, 说明)                     能导入但读不到发行包元数据
    - (异常, 版本或 None, 原因)              有安装痕迹但导入失败（常见于传递依赖缺失）
    - (缺失, None, None)                     找不到该包
    """
    version: str | None = None
    try:
        version = metadata.version(dist_name)
    except metadata.PackageNotFoundError:
        version = None
    except Exception as exc:  # 元数据损坏等异常不应让自检崩溃
        return STATUS_ERROR, None, f"读取包信息失败：{exc}"

    # 先确认模块文件是否存在；存在则实际 import，捕获传递依赖缺失 / DLL 加载失败等问题
    try:
        spec = importlib.util.find_spec(module_name)
    except Exception:
        spec = None

    if version is None and spec is None:
        return STATUS_MISSING, None, None

    try:
        importlib.import_module(module_name)
    except Exception as exc:
        return STATUS_ERROR, version, f"已安装但导入失败（{type(exc).__name__}: {exc}）"

    if version:
        return STATUS_OK, version, None
    return STATUS_METADATA_ONLY, None, "模块可导入但缺少发行包元数据，可能是手动拷贝安装"


def python_version_ok() -> bool:
    return sys.version_info[:2] >= MIN_PYTHON


def venv_description() -> tuple[str, bool]:
    """判断当前解释器是否位于虚拟环境，并区分是否为项目内 .venv。"""
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    if not in_venv:
        return "否（当前使用全局 / 系统 Python）", False
    try:
        running = Path(sys.executable).resolve()
        if running == VENV_PYTHON.resolve():
            return "是（项目内 .venv）", True
    except OSError:
        pass
    return f"是（非项目内虚拟环境：{sys.prefix}）", False


def ensure_utf8_pipe_output() -> None:
    """输出被重定向 / 管道捕获时改用 UTF-8，避免 Windows 默认 GBK 导致乱码。

    直接运行在控制台（isatty 为真）时不干预，Python 会使用控制台原生编码。
    """
    if sys.stdout.isatty():
        return
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def print_divider(char: str = "=", length: int = 68) -> None:
    print(char * length)


def print_header() -> None:
    print_divider()
    print(" QuantDesk 环境自检（M0-08）")
    print(f" 项目目录：{PROJECT_ROOT}")
    print(f" 检查时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print_divider()
    print()


# ---------------------------------------------------------------------------
# 各检查项
# ---------------------------------------------------------------------------


def check_python() -> bool:
    venv_text, is_project_venv = venv_description()
    print("[1] Python 解释器")
    print_divider("-")
    print(f"  版本        ：{platform.python_version()}（{'满足' if python_version_ok() else '不满足'}要求：>= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}）")
    print(f"  实现        ：{platform.python_implementation()}")
    print(f"  可执行文件  ：{sys.executable}")
    print(f"  虚拟环境    ：{venv_text}")
    print(f"  操作系统    ：{platform.system()} {platform.release()}（{platform.machine()}）")
    try:
        pip_version = metadata.version("pip")
        print(f"  pip 版本    ：{pip_version}")
    except metadata.PackageNotFoundError:
        print("  pip 版本    ：未安装（无法使用 pip 安装依赖）")

    if not is_project_venv:
        print()
        print("  提示：推荐使用项目内虚拟环境，避免污染全局 Python。创建方式：")
        print(f'    "{sys.executable}" -m venv "{PROJECT_ROOT / ".venv"}"')
    print()
    ok = python_version_ok()
    if not ok:
        print(f"  [问题] 当前 Python 版本过低，QuantDesk 需要 Python >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}，请升级解释器。")
        print()
    return ok


def check_dependencies() -> tuple[list[tuple[str, str, str, str]], list[tuple[str, str, str, str]]]:
    """检查依赖，返回 (不可用的核心依赖, 不可用的可选依赖)，元素含原因备注。"""
    name_width = max(display_width(name) for name, *_ in DEPENDENCIES) + 2
    version_width = 12

    print("[2] 核心依赖（必须全部正常）")
    print_divider("-")
    missing_core: list[tuple[str, str, str, str]] = []
    for name, module_name, dist_name, purpose, required in DEPENDENCIES:
        if not required:
            continue
        status, version, note = detect_package(module_name, dist_name)
        if status == STATUS_OK:
            print(f"  [{status}] {pad(name, name_width)}{pad(version or '-', version_width)}{purpose}")
        else:
            print(f"  [{status}] {pad(name, name_width)}{pad(version or '-', version_width)}{purpose}")
            if note:
                print(f"         {note}")
            missing_core.append((name, dist_name, purpose, note or ""))
    print()

    print("[3] 可选 / 开发依赖（缺失不影响启动，按需安装）")
    print_divider("-")
    missing_optional: list[tuple[str, str, str, str]] = []
    for name, module_name, dist_name, purpose, required in DEPENDENCIES:
        if required:
            continue
        status, version, note = detect_package(module_name, dist_name)
        if status == STATUS_OK:
            print(f"  [{status}] {pad(name, name_width)}{pad(version or '-', version_width)}{purpose}")
        else:
            label = "未装" if status == STATUS_MISSING else status
            print(f"  [{label}] {pad(name, name_width)}{pad(version or '-', version_width)}{purpose}")
            if note:
                print(f"         {note}")
            missing_optional.append((name, dist_name, purpose, note or ""))
    print()

    print("  说明：")
    if any(name == "TA-Lib" for name, *_ in missing_optional):
        print("    - TA-Lib 未安装或不可用：系统会自动使用 pandas 同名指标兜底，功能不受影响。")
    print("    - pytest 仅在跑测试时需要；ccxt 仅在接入加密交易所时需要。")
    if any(name == "TA-Lib" for name, *_ in missing_optional):
        print("    - 如仍想安装 TA-Lib，可执行：")
        print(f'        {quote_path(sys.executable)} -m pip install TA-Lib')
        print("      若编译失败请直接忽略；也可从 https://github.com/cgohlke/talib-build/releases")
        print("      下载 CPython 3.12 / Windows 64 位 wheel 后离线安装。")
    print()
    return missing_core, missing_optional


# ---------------------------------------------------------------------------
# 结论与安装建议
# ---------------------------------------------------------------------------


def print_conclusion(
    python_ok: bool,
    missing_core: list[tuple[str, str, str, str]],
    missing_optional: list[tuple[str, str, str, str]],
) -> int:
    total_core = sum(1 for *_, required in DEPENDENCIES if required)
    ready_core = total_core - len(missing_core)
    success = python_ok and not missing_core

    print("[4] 检查结论")
    print_divider("-")
    if success:
        print(f"  核心依赖：{ready_core}/{total_core} 齐备，环境可用。")
        if missing_optional:
            names = "、".join(name for name, *_ in missing_optional)
            talib_hint = "（TA-Lib 缺失时自动用 pandas 兜底）" if any(name == "TA-Lib" for name, *_ in missing_optional) else ""
            print(f"  可选依赖：{names} 未安装或不可用，不影响启动{talib_hint}。")
        print()
        print("  下一步：双击 scripts\\start.bat 启动 QuantDesk，")
        print("          或在项目根目录执行：")
        print(f'            {quote_path(sys.executable)} -m streamlit run app.py --server.headless true')
        print()
        return 0

    print(f"  核心依赖：{ready_core}/{total_core} 齐备，存在缺失或异常。")
    if not python_ok:
        print(f"  Python 版本：不满足（需要 >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}）。")
    print()

    if missing_core:
        print("  缺失 / 异常清单：")
        for name, dist_name, purpose, note in missing_core:
            detail = f"；{note}" if note else ""
            print(f"    - {name}：{purpose}{detail}")
        print()

    if not missing_core:
        print("  依赖均已安装，但 Python 版本不满足要求。")
        print(f"  请安装 Python >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 后重新创建项目虚拟环境：")
        print(f'    python -m venv "{PROJECT_ROOT / ".venv"}"')
        print()
        return 1

    py = quote_path(sys.executable)
    if any(note for *_, note in missing_core):
        print("  检测到“已安装但导入失败”的依赖，请先执行以下命令定位缺失的传递依赖：")
        print(f"    {py} -m pip check")
        print("  按提示补装缺失的包；必要时重装对应包：")
        print(f"    {py} -m pip install --force-reinstall " + " ".join(dist_name for _, dist_name, _, note in missing_core if note))
        print()

    print("  修复建议（复制到 PowerShell / CMD 执行均可）：")
    requirements = PROJECT_ROOT / "requirements.txt"
    if requirements.exists():
        print(f"    {py} -m pip install -r {quote_path(str(requirements))}")
    print(f"    {py} -m pip install " + " ".join(dist_name for _, dist_name, _, _ in missing_core))
    print()
    print("  网络较慢或超时，可改用国内镜像重试：")
    print(f"    {py} -m pip install -i https://mirrors.aliyun.com/pypi/simple/ " + " ".join(dist_name for _, dist_name, _, _ in missing_core))
    print(f"    {py} -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple " + " ".join(dist_name for _, dist_name, _, _ in missing_core))
    print()
    print("  修复后请重新运行本脚本，直到核心依赖全部显示 [正常]。")
    print()
    return 1


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> int:
    ensure_utf8_pipe_output()
    print_header()
    python_ok = check_python()
    missing_core, missing_optional = check_dependencies()
    return print_conclusion(python_ok, missing_core, missing_optional)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消环境检查。")
        sys.exit(1)
    except Exception as exc:  # 自检脚本自身异常也要给出中文提示，而不是只抛堆栈
        print(f"\n[错误] 环境自检脚本执行异常：{exc}")
        print("请将以上信息反馈给集成负责人（Integrator）。")
        sys.exit(1)

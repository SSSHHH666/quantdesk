#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""QuantDesk 一键全链路验证脚本（v2.0.0 发版验证）。

一条命令串起「全量测试 + 环境自检 + 发版自检 + 数据体检 + 界面 HTTP 冒烟」，
逐项打印 PASS/FAIL 与耗时，任一项失败则进程退出码为 1。

用法（项目根目录）::

    .venv\\Scripts\\python.exe scripts\\verify_all.py
    .venv\\Scripts\\python.exe scripts\\verify_all.py --skip-ui          # 跳过界面冒烟
    .venv\\Scripts\\python.exe scripts\\verify_all.py --skip-tests       # 跳过全量测试（快速冒烟）
    .venv\\Scripts\\python.exe scripts\\verify_all.py --with-m6          # 追加 M6 研究链路冒烟（约 2 分钟）

设计约束
--------
* **仅标准库**：不 import 项目模块（版本号缺失时降级为读 config.yaml），无第三方依赖；
* **无外部网络**：界面冒烟只访问 127.0.0.1 回环地址；
* **跨平台**：Windows / macOS / Linux 均可运行；Windows 下不弹窗、不依赖 PowerShell；
* **沙箱友好**：pytest 优先使用 ``runtime/_run_pytest.py`` 包装器（存在时），否则退回
  ``python -m pytest``；子进程输出重定向到日志文件（不经管道），避免 DSH 沙箱限制；
* **自动清理**：界面冒烟自动挑选空闲端口、轮询 HTTP、超时/结束后必定结束进程。

退出码
------
0 = 全部启用项 PASS（含 SKIP 视为通过）；1 = 存在 FAIL 项。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_ROOT = ROOT / "runtime" / "verify_v2"

PASS, FAIL, SKIP, WARN = "PASS", "FAIL", "SKIP", "WARN"

#: 本机基线（用于提示「结果与基线不一致」，不据此判失败）
BASELINE_PASSED = 405
BASELINE_DESELECTED = 60


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class StepResult:
    """单个验证项的结果。"""

    index: str
    name: str
    command: str
    status: str = FAIL
    seconds: float = 0.0
    detail: str = ""
    log: str = ""
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "name": self.name,
            "command": self.command,
            "status": self.status,
            "seconds": round(self.seconds, 2),
            "detail": self.detail,
            "log": self.log,
            "extra": self.extra,
        }


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def _print(text: str = "") -> None:
    """打印（控制台编码不支持时为替换字符，绝不因编码崩溃）。"""
    try:
        print(text, flush=True)
    except UnicodeEncodeError:  # pragma: no cover - 极少数 GBK 控制台
        print(text.encode("utf-8", "replace").decode("ascii", "replace"), flush=True)


def _child_env() -> dict:
    """子进程环境：强制 UTF-8 输出，避免中文日志在重定向时乱码。"""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _read_text(path: Path) -> str:
    """按 UTF-8 读日志（坏字节替换，不抛异常）。"""
    try:
        return Path(path).read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _tail(text: str, lines: int = 6) -> str:
    """取日志末尾若干非空行。"""
    rows = [row.rstrip() for row in text.splitlines() if row.strip()]
    return "\n".join(rows[-lines:])


def _terminate(process: subprocess.Popen, grace: float = 8.0) -> None:
    """结束子进程：先 terminate，超时再 kill（跨平台安全，绝不抛异常）。"""
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=grace)
        return
    except Exception:  # noqa: BLE001 - 清理阶段不抛异常
        pass
    try:
        process.kill()
        process.wait(timeout=grace)
    except Exception:  # noqa: BLE001
        pass


def _run_capture(command: list[str], log_path: Path, timeout: float) -> tuple[int, float, bool]:
    """运行子进程并把 stdout+stderr 写入日志文件（不经管道，沙箱友好）。

    返回 ``(退出码, 耗时秒, 是否超时)``；超时返回退出码 -1。
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    with log_path.open("wb") as handle:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=_child_env(),
        )
        try:
            code = process.wait(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            _terminate(process)
            code, timed_out = -1, True
    return code, time.perf_counter() - started, timed_out


def _project_version() -> str:
    """读取项目版本号（优先 core.config，失败时正则读 config.yaml，绝不抛异常）。"""
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from core.config import get_config  # noqa: PLC0415 - 延迟导入，缺失时降级

        return str(get_config().app.version)
    except Exception:  # noqa: BLE001 - 版本号只是展示信息
        pass
    try:
        text = (ROOT / "config.yaml").read_text(encoding="utf-8", errors="replace")
        match = re.search(r"^\s*version:\s*['\"]?([\w.\-]+)", text, re.MULTILINE)
        return match.group(1) if match else "unknown"
    except OSError:
        return "unknown"


def _free_port() -> int:
    """向系统申请一个空闲 TCP 端口（仅本机回环）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ---------------------------------------------------------------------------
# 各验证项
# ---------------------------------------------------------------------------
def step_tests(log_dir: Path, enabled: bool, timeout: float) -> StepResult:
    """1) 全量测试：pytest tests -q。"""
    wrapper = ROOT / "runtime" / "_run_pytest.py"
    if wrapper.exists():
        command = [sys.executable, str(wrapper), "tests", "-q"]
        note = "沙箱包装器 runtime/_run_pytest.py"
    else:
        command = [sys.executable, "-m", "pytest", "tests", "-q"]
        note = "python -m pytest"
    result = StepResult("1", "全量测试", _short_cmd(command), detail=note)
    if not enabled:
        result.status = SKIP
        result.detail = "已按 --skip-tests 跳过"
        return result

    log_path = log_dir / "tests.log"
    result.log = _rel(log_path)
    _print(f"  [运行] pytest tests -q（{note}，约 60 秒）…")
    code, seconds, timed_out = _run_capture(command, log_path, timeout)
    result.seconds = seconds
    text = _read_text(log_path)
    summary = _pytest_summary(text)
    passed = _find_int(r"(\d+) passed", text)
    failed = _find_int(r"(\d+) failed", text)
    errors = _find_int(r"(\d+) errors?", text)
    if timed_out:
        result.status = FAIL
        result.detail = f"超时（>{int(timeout)}s），见日志 {result.log}"
    elif code != 0 or failed or errors:
        result.status = FAIL
        result.detail = f"退出码 {code}；{summary or _tail(text, 3)}；见日志 {result.log}"
    else:
        result.status = PASS
        result.detail = summary or f"退出码 0（{seconds:.1f}s）"
        if passed is not None and passed != BASELINE_PASSED:
            result.status = WARN
            result.detail += f"（注意：基线为 {BASELINE_PASSED} passed，本次 {passed} passed）"
        result.extra["passed"] = passed
        result.extra["deselected"] = _find_int(r"(\d+) deselected", text)
    return result


def step_check_env(log_dir: Path, timeout: float) -> StepResult:
    """2) 环境自检：scripts/check_env.py。"""
    command = [sys.executable, str(ROOT / "scripts" / "check_env.py")]
    result = StepResult("2", "环境自检", _short_cmd(command))
    log_path = log_dir / "check_env.log"
    result.log = _rel(log_path)
    _print("  [运行] scripts/check_env.py …")
    code, seconds, timed_out = _run_capture(command, log_path, timeout)
    result.seconds = seconds
    text = _read_text(log_path)
    if timed_out:
        result.status = FAIL
        result.detail = f"超时（>{int(timeout)}s）"
    elif code != 0:
        result.status = FAIL
        result.detail = f"退出码 {code}；{_tail(text, 4)}"
    else:
        match = re.search(r"核心依赖[：:]\s*(\S+)", text)
        result.detail = f"退出码 0；核心依赖 {match.group(1) if match else '正常'}"
        result.status = PASS
    return result


def step_release_check(log_dir: Path, timeout: float) -> StepResult:
    """3) 发版自检：scripts/release_check.py --skip-tests（要求 FAIL 0）。"""
    command = [sys.executable, str(ROOT / "scripts" / "release_check.py"), "--skip-tests"]
    result = StepResult("3", "发版自检", _short_cmd(command))
    log_path = log_dir / "release_check.log"
    result.log = _rel(log_path)
    _print("  [运行] scripts/release_check.py --skip-tests …")
    code, seconds, timed_out = _run_capture(command, log_path, timeout)
    result.seconds = seconds
    text = _read_text(log_path)
    counts = re.search(r"汇总[：:]\s*PASS\s*(\d+)\s*/\s*WARN\s*(\d+)\s*/\s*FAIL\s*(\d+)", text)
    if timed_out:
        result.status = FAIL
        result.detail = f"超时（>{int(timeout)}s）"
    elif code != 0:
        result.status = FAIL
        result.detail = f"退出码 {code}；{counts.group(0) if counts else _tail(text, 4)}"
    elif "FAIL 0" not in text:
        result.status = FAIL
        result.detail = f"未在输出中找到「FAIL 0」；{_tail(text, 4)}"
    else:
        result.status = PASS
        result.detail = counts.group(0) if counts else "FAIL 0"
        result.extra["pass"] = int(counts.group(1)) if counts else None
        result.extra["warn"] = int(counts.group(2)) if counts else None
        result.extra["fail"] = int(counts.group(3)) if counts else 0
    return result


def step_verify_data(log_dir: Path, timeout: float) -> StepResult:
    """4) 数据体检：scripts/verify_data.py（要求结论 PASS）。"""
    command = [sys.executable, str(ROOT / "scripts" / "verify_data.py")]
    result = StepResult("4", "数据体检", _short_cmd(command))
    log_path = log_dir / "verify_data.log"
    result.log = _rel(log_path)
    _print("  [运行] scripts/verify_data.py …")
    code, seconds, timed_out = _run_capture(command, log_path, timeout)
    result.seconds = seconds
    text = _read_text(log_path)
    conclusion = re.search(r"结论[：:]\s*(PASS|FAIL\s*·[^\n]*)", text)
    if timed_out:
        result.status = FAIL
        result.detail = f"超时（>{int(timeout)}s）"
    elif code != 0 or not conclusion or conclusion.group(1).startswith("FAIL"):
        result.status = FAIL
        result.detail = f"退出码 {code}；{_tail(text, 4)}"
    else:
        result.status = PASS
        stats = re.search(r"(ERROR\s*\d+\s*个\s*/\s*WARN\s*\d+\s*个\s*/\s*INFO\s*\d+\s*个)", text)
        result.detail = f"结论 PASS；{stats.group(1) if stats else '无 ERROR/WARN'}"
    return result


def step_streamlit_ui(log_dir: Path, enabled: bool, timeout: float, port: int = 0) -> StepResult:
    """5) 界面 HTTP 冒烟：后台启动 Streamlit，轮询 127.0.0.1 直到 200 且含 streamlit 标记。"""
    result = StepResult("5", "界面 HTTP 冒烟", "python -m streamlit run app.py --server.headless true")
    if not enabled:
        result.status = SKIP
        result.detail = "已按 --skip-ui 跳过"
        return result

    chosen_port = port or _free_port()
    url = f"http://127.0.0.1:{chosen_port}/"
    log_path = log_dir / "streamlit.log"
    index_path = log_dir / "streamlit_index.html"
    result.log = _rel(log_path)
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        "app.py",
        "--server.headless",
        "true",
        "--server.port",
        str(chosen_port),
        "--server.fileWatcherType",
        "none",
        "--browser.gatherUsageStats",
        "false",
    ]
    _print(f"  [运行] streamlit run app.py（端口 {chosen_port}，轮询 {url}）…")

    started = time.perf_counter()
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as handle:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=_child_env(),
        )
    status_code: int | None = None
    body = ""
    title = ""
    health = ""
    error_note = ""
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                error_note = f"Streamlit 进程提前退出（退出码 {process.returncode}）"
                break
            try:
                with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310 - 仅本机回环
                    status_code = int(getattr(response, "status", response.getcode()))
                    body = response.read(200_000).decode("utf-8", "replace")
                if status_code == 200 and "streamlit" in body.lower():
                    break
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(0.5)
        if status_code == 200 and "streamlit" in body.lower():
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{chosen_port}/_stcore/health", timeout=3
                ) as health_response:  # noqa: S310
                    health = health_response.read(64).decode("utf-8", "replace").strip()
            except (urllib.error.URLError, OSError, ValueError):
                health = ""
            match = re.search(r"<title>(.*?)</title>", body, re.DOTALL)
            title = (match.group(1).strip() if match else "") or "Streamlit"
            index_path.write_text(body, encoding="utf-8")
            result.status = PASS
            result.detail = (
                f"HTTP {status_code}，标题「{title}」，{len(body)} 字节，启动 {time.perf_counter() - started:.2f}s"
                + (f"，health={health}" if health else "")
            )
            result.extra["url"] = url
            result.extra["title"] = title
            result.extra["bytes"] = len(body)
        else:
            result.status = FAIL
            result.detail = error_note or (
                f"{int(timeout)}s 内未就绪（最后状态码 {status_code}），见日志 {_rel(log_path)}"
            )
    finally:
        _terminate(process)
    result.seconds = time.perf_counter() - started
    if result.status == FAIL:
        result.detail += f"；日志：{result.log}"
    return result


def step_m6(log_dir: Path, enabled: bool, timeout: float) -> StepResult:
    """6) 可选：M6 研究链路冒烟（--with-m6，约 2 分钟）。"""
    command = [sys.executable, str(ROOT / "scripts" / "m6_research.py"), "--quick"]
    result = StepResult("6", "M6 研究冒烟", _short_cmd(command))
    if not enabled:
        result.status = SKIP
        result.detail = "未启用（加 --with-m6 运行，约 2 分钟）"
        return result

    log_path = log_dir / "m6_quick.log"
    result.log = _rel(log_path)
    _print("  [运行] scripts/m6_research.py --quick（约 2 分钟）…")
    code, seconds, timed_out = _run_capture(command, log_path, timeout)
    result.seconds = seconds
    text = _read_text(log_path)
    ok_count = len(re.findall(r"\[OK \d/4\]", text))
    done = re.search(r"\[完成\]\s*4/4 组成功[，,]\s*耗时\s*([\d.]+)s", text)
    if timed_out:
        result.status = FAIL
        result.detail = f"超时（>{int(timeout)}s），见日志 {result.log}"
    elif code != 0 or ok_count != 4:
        result.status = FAIL
        result.detail = f"退出码 {code}；成功配置 {ok_count}/4；{_tail(text, 4)}"
    else:
        result.status = PASS
        result.detail = f"4/4 组成功" + (f"，内部耗时 {done.group(1)}s" if done else "")
        result.extra["configs_ok"] = ok_count
    return result


# ---------------------------------------------------------------------------
# 输出辅助
# ---------------------------------------------------------------------------
def _rel(path: Path) -> str:
    """相对项目根目录的路径（Windows 用反斜杠显示）。"""
    try:
        return str(Path(path).resolve().relative_to(ROOT)).replace("/", os.sep)
    except ValueError:
        return str(path)


def _short_cmd(command: list[str]) -> str:
    """把 python 解释器缩成 ``python``，便于阅读。"""
    parts = list(command)
    for index, part in enumerate(parts):
        if "python" in Path(part).name.lower():
            parts[index] = "python"
    return " ".join(parts)


def _find_int(pattern: str, text: str) -> int | None:
    match = re.search(pattern, text)
    return int(match.group(1)) if match else None


def _pytest_summary(text: str) -> str:
    """提取 pytest 最后一行汇总（如 405 passed, 60 deselected in 58.71s）。"""
    rows = [
        row.strip()
        for row in text.splitlines()
        if re.search(r"\d+ (passed|failed|error)", row) and "=" not in row
    ]
    return rows[-1] if rows else ""


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="QuantDesk 一键全链路验证（测试/环境/发版自检/数据/界面/M6）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--skip-tests", action="store_true", help="跳过全量 pytest（快速冒烟）")
    parser.add_argument("--skip-ui", action="store_true", help="跳过 Streamlit 界面 HTTP 冒烟")
    parser.add_argument("--with-m6", action="store_true", help="追加 M6 研究链路冒烟（约 2 分钟）")
    parser.add_argument("--ui-port", type=int, default=0, help="界面冒烟端口（0=自动选空闲端口）")
    parser.add_argument("--ui-timeout", type=float, default=90.0, help="界面就绪超时秒数")
    parser.add_argument("--tests-timeout", type=float, default=900.0, help="pytest 超时秒数")
    parser.add_argument("--m6-timeout", type=float, default=900.0, help="M6 冒烟超时秒数")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started_at = datetime.now()
    run_dir = LOG_ROOT / started_at.strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    version = _project_version()
    _print("=" * 78)
    _print(f" QuantDesk v{version} 一键全链路验证")
    _print(f" 时间    ：{started_at.strftime('%Y-%m-%d %H:%M:%S')}")
    _print(f" 项目根  ：{ROOT}")
    _print(f" 解释器  ：{sys.executable}")
    _print(f" 平台    ：{platform.platform()}")
    _print(f" 日志目录：{_rel(run_dir)}")
    _print("=" * 78)

    results: list[StepResult] = []
    total = 6

    def announce(result: StepResult) -> StepResult:
        results.append(result)
        icon = result.status
        _print(f"[{result.index}/{total}] {result.name} …… {icon}（{result.seconds:.1f}s）")
        for line in result.detail.splitlines():
            _print(f"      {line}")
        return result

    # 1) 全量测试
    announce(step_tests(run_dir, not args.skip_tests, args.tests_timeout))
    # 2) 环境自检
    announce(step_check_env(run_dir, timeout=180.0))
    # 3) 发版自检
    announce(step_release_check(run_dir, timeout=300.0))
    # 4) 数据体检
    announce(step_verify_data(run_dir, timeout=180.0))
    # 5) 界面冒烟
    announce(step_streamlit_ui(run_dir, not args.skip_ui, args.ui_timeout, args.ui_port))
    # 6) 可选 M6 冒烟
    announce(step_m6(run_dir, args.with_m6, args.m6_timeout))

    elapsed = (datetime.now() - started_at).total_seconds()
    counts = {key: sum(1 for item in results if item.status == key) for key in (PASS, FAIL, SKIP, WARN)}
    failed = [item for item in results if item.status == FAIL]

    _print("-" * 78)
    _print(f" 汇总：PASS {counts[PASS]} / FAIL {counts[FAIL]} / SKIP {counts[SKIP]}"
           + (f" / WARN {counts[WARN]}" if counts[WARN] else ""))
    _print(f" 总耗时：{elapsed:.1f}s")
    if failed:
        _print(f" 结论：[未通过] {len(failed)} 项失败：" + "、".join(f"{item.name}（{item.index}）" for item in failed))
    else:
        _print(" 结论：[通过] 全部验证项通过")
    _print("=" * 78)

    summary = {
        "version": version,
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_seconds": round(elapsed, 2),
        "python": sys.executable,
        "platform": platform.platform(),
        "options": {
            "skip_tests": args.skip_tests,
            "skip_ui": args.skip_ui,
            "with_m6": args.with_m6,
            "ui_port": args.ui_port,
        },
        "counts": counts,
        "results": [item.as_dict() for item in results],
        "conclusion": "FAIL" if failed else "PASS",
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (run_dir / "summary.txt").write_text(
        "\n".join(
            [
                f"QuantDesk v{version} verify_all @ {summary['started_at']}",
                f"python: {sys.executable}",
                f"platform: {platform.platform()}",
                f"counts: {counts}",
                f"elapsed: {elapsed:.1f}s",
                *(
                    f"[{item.status}] {item.index}) {item.name} | {item.command} | "
                    f"{item.seconds:.2f}s | {item.detail}"
                    for item in results
                ),
                f"conclusion: {'FAIL' if failed else 'PASS'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _print(f" 证据日志：{_rel(run_dir)}（summary.json / summary.txt + 各项日志）")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

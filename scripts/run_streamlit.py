"""QuantDesk Streamlit 启动包装器（供 ``scripts\\start.bat`` 调用）。

职责
----
- 启动 ``python -m streamlit run app.py``，把**子进程输出同时**打印到控制台并追加写入
  ``runtime/logs/start.log``（UTF-8），窗口保持可见、日志可事后排查；
- 透传退出码；Ctrl+C/关闭窗口时尽量优雅结束子进程；
- 不修改任何业务代码，仅做启动日志与进程转发。

用法（一般由 start.bat 调用，也可手工排查）::

    .venv\\Scripts\\python.exe scripts\\run_streamlit.py --port 8501
    .venv\\Scripts\\python.exe scripts\\run_streamlit.py --port 8502 --log runtime\\logs\\start.log
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _log_line(handle, message: str) -> None:
    """同时输出到控制台与日志文件（日志 UTF-8，控制台编码不兼容时用 ? 代替，不崩溃）。"""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    record = f"[{stamp}] {message}"
    handle.write(record + "\n")
    handle.flush()
    try:
        print(message, flush=True)
    except UnicodeEncodeError:  # pragma: no cover - 极少数控制台编码
        print(message.encode("utf-8", "replace").decode("ascii", "replace"), flush=True)


def build_streamlit_command(port: int, app: str = "app.py") -> list[str]:
    """构造 streamlit 启动命令（固定 headless/不收集统计，端口由调用方决定）。"""
    return [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        app,
        "--server.headless",
        "true",
        "--server.port",
        str(int(port)),
        "--browser.gatherUsageStats",
        "false",
    ]


def main(argv: list[str] | None = None) -> int:
    """启动并转发输出；返回 streamlit 子进程退出码。"""
    parser = argparse.ArgumentParser(description="QuantDesk Streamlit 启动包装器（控制台 + runtime/logs/start.log）")
    parser.add_argument("--port", type=int, default=8501, help="监听端口（默认 8501）")
    parser.add_argument("--log", type=Path, default=None, help="启动日志路径（默认 runtime/logs/start.log）")
    parser.add_argument("--app", default="app.py", help="Streamlit 入口文件（默认 app.py）")
    args = parser.parse_args(argv)

    port = int(args.port)
    if port < 1 or port > 65535:
        print(f"[错误] 端口非法：{port}（应为 1~65535）")
        return 2
    log_path = Path(args.log) if args.log is not None else ROOT / "runtime" / "logs" / "start.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    command = build_streamlit_command(port, args.app)
    with log_path.open("a", encoding="utf-8") as handle:
        _log_line(handle, "=" * 70)
        _log_line(handle, f"QuantDesk 启动：{datetime.now().isoformat(sep=' ', timespec='seconds')}")
        _log_line(handle, f"项目目录：{ROOT}")
        _log_line(handle, f"解释器  ：{sys.executable}")
        _log_line(handle, f"访问地址：http://localhost:{port}")
        _log_line(handle, f"命令    ：{' '.join(command)}")
        _log_line(handle, "=" * 70)

        env = dict(__import__("os").environ)
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            process = subprocess.Popen(
                command,
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
        except FileNotFoundError as exc:
            _log_line(handle, f"[错误] 无法启动 streamlit：{exc}")
            _log_line(handle, "       请先执行 scripts\\check_env.py 并安装 requirements.txt")
            return 1
        except Exception as exc:  # noqa: BLE001 - 启动失败给中文提示
            _log_line(handle, f"[错误] 启动失败：{type(exc).__name__}: {exc}")
            return 1

        try:
            assert process.stdout is not None
            for line in process.stdout:
                _log_line(handle, line.rstrip())
        except KeyboardInterrupt:  # pragma: no cover - 用户 Ctrl+C
            _log_line(handle, "[提示] 收到 Ctrl+C，正在停止 QuantDesk …")
            process.terminate()
        finally:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                process.kill()
                process.wait(timeout=5)
        code = int(process.returncode or 0)
        _log_line(handle, "=" * 70)
        _log_line(handle, f"QuantDesk 已退出（退出码 {code}）；日志：{log_path}")
        return code


if __name__ == "__main__":
    raise SystemExit(main())

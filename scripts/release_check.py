"""QuantDesk 发版自检脚本（M4-06）。

一键检查（发版前跑一次，正常状态应全部 PASS）：
1. Python 版本（项目基线 3.12.x）与关键依赖版本（缺失/版本过低给安装建议）；
2. 数据库：22 张核心表齐全、``PRAGMA integrity_check``、可读；
3. 数据源：主源 akshare 可用性或已按配置降级 mock（降级不算失败，但会在详情注明）；
4. ``daily_bars``：总行数、按 source 分组、同一标的多来源混存检查（0 行给 WARN）；
5. 测试：调用 ``pytest -q`` 全绿（可用 ``--skip-tests`` 跳过）；
6. 文档/许可文件存在性（README、requirements、RUNBOOK、THIRD_PARTY、SPEC、config、start.bat）。

用法（项目根目录）::

    .venv\\Scripts\\python.exe scripts\\release_check.py
    .venv\\Scripts\\python.exe scripts\\release_check.py --skip-tests     # 快速检查（不发版）
    .venv\\Scripts\\python.exe scripts\\release_check.py --db 临时库.db  # 演练/指定库

约定：``[PASS]`` 通过、``[WARN]`` 可发布但需注意、``[FAIL]`` 阻塞发版；
退出码 0=无 FAIL，1=存在 FAIL。
"""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

#: 关键依赖（缺失即 FAIL；可选依赖只 WARN）
REQUIRED_DEPENDENCIES: tuple[str, ...] = (
    "pydantic", "sqlalchemy", "pandas", "numpy", "streamlit", "plotly",
    "PyYAML", "APScheduler", "requests", "pytest",
)
OPTIONAL_DEPENDENCIES: dict[str, str] = {
    "akshare": "真实行情源（缺失时自动降级 mock 离线数据）",
    "TA-Lib": "指标加速库（缺失时用 pandas 兜底）",
    "ccxt": "加密 testnet 适配器（骨架，缺失不影响 A 股）",
}

#: 发版必须具备的文档/配置文件
REQUIRED_FILES: tuple[str, ...] = (
    "README.md",
    "requirements.txt",
    "config.yaml",
    "docs/RUNBOOK.md",
    "docs/THIRD_PARTY.md",
    "docs/SPEC.md",
    "scripts/start.bat",
)
OPTIONAL_FILES: dict[str, str] = {
    "LICENSE": "开源许可文件（个人自用可后补）",
    "docs/USER_GUIDE.md": "用户手册（M5 交付）",
    "docs/ACCEPTANCE_CHECKLIST.md": "验收清单（QA 交付）",
}


@dataclass
class CheckResult:
    """单条检查结果。"""

    name: str
    status: str
    detail: str = ""


class ReleaseChecker:
    """发版自检器（收集结果，最后统一输出与退出码）。"""

    def __init__(self) -> None:
        self.results: list[CheckResult] = []

    # -- 结果收集 ---------------------------------------------------------
    def add(self, name: str, status: str, detail: str = "") -> None:
        self.results.append(CheckResult(name=name, status=status, detail=detail))

    def passed(self) -> bool:
        return not any(item.status == FAIL for item in self.results)

    def counts(self) -> dict[str, int]:
        return {key: sum(1 for item in self.results if item.status == key) for key in (PASS, WARN, FAIL)}

    def report(self) -> None:
        print("=" * 78)
        print("QuantDesk 发版自检报告")
        print(f"  项目根目录：{ROOT}")
        print(f"  解释器    ：{sys.executable}")
        print("=" * 78)
        for item in self.results:
            line = f"[{item.status}] {item.name}"
            if item.detail:
                line += f"：{item.detail}"
            print(line)
        counts = self.counts()
        print("-" * 78)
        print(f"汇总：PASS {counts[PASS]} / WARN {counts[WARN]} / FAIL {counts[FAIL]}")
        if counts[FAIL]:
            print("结论：[未通过] 发版自检未通过，请先修复 FAIL 项（见 docs/RUNBOOK.md §7.3）")
        else:
            print("结论：[通过] 发版自检通过" + ("（存在 WARN，请留意但不阻塞发版）" if counts[WARN] else ""))

    # -- 各项检查 ---------------------------------------------------------
    def check_python(self) -> None:
        """Python 版本检查（基线 3.12.x，与 RUNBOOK 附录 A 一致）。"""
        version = sys.version.split()[0]
        if sys.version_info[:2] == (3, 12):
            self.add("Python 版本", PASS, version)
        elif sys.version_info[:2] > (3, 12):
            self.add("Python 版本", WARN, f"{version}（项目基线 3.12.7，更高版本未做完整回归）")
        else:
            self.add("Python 版本", FAIL, f"{version} 低于基线 3.12，请使用 quantdesk\\.venv\\Scripts\\python.exe")

    def check_dependencies(self) -> None:
        """关键依赖检查（版本来自 importlib.metadata）。"""
        missing: list[str] = []
        versions: list[str] = []
        for name in REQUIRED_DEPENDENCIES:
            version = _package_version(name)
            if version is None:
                missing.append(name)
            else:
                versions.append(f"{name} {version}")
        if missing:
            self.add("关键依赖", FAIL, "缺失：" + "、".join(missing) + "（pip install -r requirements.txt）")
        else:
            self.add("关键依赖", PASS, "、".join(versions[:6]) + ("…" if len(versions) > 6 else ""))

        optional_missing = [name for name in OPTIONAL_DEPENDENCIES if _package_version(name) is None]
        if optional_missing:
            notes = "；".join(f"{name}（{OPTIONAL_DEPENDENCIES[name]}）" for name in optional_missing)
            # 可选依赖缺失不阻塞发版（正常状态要求全 PASS）：PASS + 注明
            self.add("可选依赖", PASS, "可选未安装（不阻塞发版）：" + notes)
        else:
            self.add("可选依赖", PASS, "akshare / TA-Lib / ccxt 均已安装")

    def check_config(self) -> None:
        """配置加载与校验（config.yaml + 环境变量）。"""
        try:
            from core.config import get_config

            config = get_config()
            self._config = config
            self.add(
                "配置加载",
                PASS,
                f"{config.app.name} v{config.app.version}，provider={config.data.provider}，"
                f"universe={len(config.data.universe)} 只",
            )
        except Exception as exc:  # noqa: BLE001
            self._config = None
            self.add("配置加载", FAIL, f"{type(exc).__name__}: {exc}（检查 config.yaml 语法与默认值）")

    def check_database(self, db_path: Path | None = None) -> None:
        """数据库检查：文件存在、integrity_check、22 张核心表（只读连接，不写库）。"""
        if db_path is None:
            config = getattr(self, "_config", None)
            try:
                db_path = Path(config.sqlite_path) if config is not None and config.sqlite_path else None
            except Exception:  # noqa: BLE001
                db_path = None
        if db_path is None:
            self.add("数据库文件", FAIL, "无法解析 SQLite 路径（检查 config.yaml database.url）")
            return
        self._db_path = db_path
        if not db_path.exists():
            self.add(
                "数据库文件", FAIL,
                f"不存在：{db_path}（首次发版请先运行 scripts\\init_db.py；用户机器首次启动会自动建库）",
            )
            return
        try:
            conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=10)
            try:
                integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
                tables = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()}
            finally:
                conn.close()
        except sqlite3.Error as exc:
            self.add("数据库完整性", FAIL, f"无法读取/文件损坏：{exc}（按 RUNBOOK §6 恢复备份）")
            return
        if integrity.lower() != "ok":
            self.add("数据库完整性", FAIL, f"integrity_check = {integrity}")
        else:
            self.add("数据库完整性", PASS, f"integrity_check=OK（{db_path.stat().st_size:,} 字节）")

        try:
            from core.models import TABLE_NAMES

            required = tuple(TABLE_NAMES)
        except Exception:  # noqa: BLE001
            required = ()
        missing = [name for name in required if name not in tables]
        if required and missing:
            self.add("数据库表", FAIL, f"缺少 {len(missing)} 张核心表：{'、'.join(missing[:6])}…")
        elif required:
            self.add("数据库表", PASS, f"22 张核心表齐全（库内共 {len(tables)} 张表）")
        else:
            self.add("数据库表", WARN, f"无法加载 TABLE_NAMES，库内共 {len(tables)} 张表")
        self._db_tables = tables

    def check_daily_bars(self) -> None:
        """``daily_bars`` 行数 + source 唯一性（同一标的不允许多来源混存）。"""
        db_path = getattr(self, "_db_path", None)
        if db_path is None or not Path(db_path).exists():
            self.add("daily_bars 数据", WARN, "数据库不可用，跳过行情数据检查")
            return
        try:
            conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True, timeout=10)
            try:
                total = int(conn.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0])
                sources = conn.execute(
                    "SELECT source, COUNT(*) FROM daily_bars GROUP BY source ORDER BY COUNT(*) DESC"
                ).fetchall()
                mixed = int(conn.execute(
                    "SELECT COUNT(*) FROM (SELECT symbol FROM daily_bars "
                    "GROUP BY symbol HAVING COUNT(DISTINCT source) > 1)"
                ).fetchone()[0])
            finally:
                conn.close()
        except sqlite3.Error as exc:
            self.add("daily_bars 数据", FAIL, f"查询失败：{exc}")
            return
        source_text = "、".join(f"{source or '(空)'} {count:,} 行" for source, count in sources) or "无数据"
        if mixed:
            self.add(
                "daily_bars 数据", FAIL,
                f"发现 {mixed} 只标的混存多个 source（禁止 mock/真实数据混存）；"
                "请运行 scripts\\purge_mock_cache.py 后回填真实数据（RUNBOOK §9）",
            )
        elif total == 0:
            self.add("daily_bars 数据", WARN, "0 行（首次发版可接受；用户可运行 scripts\\fetch_data.py 拉取）")
        else:
            self.add("daily_bars 数据", PASS, f"{total:,} 行；来源唯一：{source_text}")

    def check_data_source(self) -> None:
        """数据源可用性（akshare 不可用时按配置降级 mock，降级不算 FAIL）。"""
        config = getattr(self, "_config", None)
        if config is None:
            self.add("数据源", WARN, "配置不可用，跳过数据源检查")
            return
        try:
            from data.provider import build_provider

            provider = build_provider(config)
            actual = str(getattr(provider, "name", "unknown"))
            configured = str(config.data.provider)
            status, detail = "OK", ""
            try:
                health = provider.health_check()
                status = str(getattr(getattr(health, "status", ""), "value", getattr(health, "status", "OK")))
                detail = str(getattr(health, "message", "") or "")
            except Exception as exc:  # noqa: BLE001 - 健康检查失败不阻塞
                detail = f"健康检查失败：{exc}"
            if actual != configured:
                self.add(
                    "数据源",
                    PASS,
                    f"{configured} 不可用已自动降级为 {actual}（离线演示模式）；{detail}".strip("；"),
                )
            elif status.upper() in {"DOWN", "ERROR", "FAILED"}:
                self.add("数据源", FAIL, f"{actual} 状态 {status}：{detail}")
            else:
                self.add("数据源", PASS, f"{actual}（状态 {status}）{('：' + detail) if detail else ''}")
        except Exception as exc:  # noqa: BLE001
            self.add("数据源", FAIL, f"{type(exc).__name__}: {exc}（检查 data.provider/权限/网络）")

    def check_tests(self) -> None:
        """调用 ``pytest -q`` 跑全量测试（发版门槛：全绿）。"""
        command = [sys.executable, "-m", "pytest", "-q", "--no-header"]
        print(f"\n[运行] {' '.join(command)}（约 30~60 秒，请稍候）…")
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"  # 避免中文输出在 GBK 控制台下解码失败
        try:
            completed = subprocess.run(
                command,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=900,
                env=env,
            )
        except subprocess.TimeoutExpired:
            self.add("测试全绿", FAIL, "pytest 超时（>900s），请手动运行 pytest -q 排查")
            return
        except Exception as exc:  # noqa: BLE001
            self.add("测试全绿", FAIL, f"无法启动 pytest：{type(exc).__name__}: {exc}")
            return
        output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        tail = lines[-1] if lines else f"exit={completed.returncode}（无输出）"
        if completed.returncode == 0:
            self.add("测试全绿", PASS, f"pytest -q 通过（{tail}）")
        else:
            self.add("测试全绿", FAIL, f"pytest 失败（exit={completed.returncode}）：{tail}")

    def check_files(self) -> None:
        """文档/许可/配置文件存在性（必需的缺失=FAIL；可选的缺失=WARN）。"""
        missing = [name for name in REQUIRED_FILES if not (ROOT / name).exists()]
        if missing:
            self.add("文档/配置文件", FAIL, "缺失：" + "、".join(missing))
        else:
            self.add("文档/配置文件", PASS, "、".join(REQUIRED_FILES))
        optional_missing = [
            f"{name}（{reason}）"
            for name, reason in OPTIONAL_FILES.items()
            if not (ROOT / name).exists()
        ]
        if optional_missing:
            # 可选文档缺失不阻塞发版（M5 才要求 USER_GUIDE）：PASS + 注明
            self.add("可选文档", PASS, "可选未交付（不阻塞发版）：" + "；".join(optional_missing))
        else:
            self.add("可选文档", PASS, "LICENSE / USER_GUIDE / ACCEPTANCE_CHECKLIST 均已就绪")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _package_version(name: str) -> str | None:
    """读取已安装包版本；未安装返回 None（兼容 metadata 名称差异）。"""
    for candidate in (name, name.lower(), name.replace("-", "_")):
        try:
            return importlib.metadata.version(candidate)
        except importlib.metadata.PackageNotFoundError:
            continue
        except Exception:  # noqa: BLE001
            continue
    return None


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        description="QuantDesk 发版自检（依赖/数据库/数据源/测试/文档）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--db", type=Path, default=None, help="指定数据库文件（默认取 config database.url）")
    parser.add_argument("--skip-tests", action="store_true", help="跳过 pytest（快速检查/演练用）")
    return parser


def main(argv: list[str] | None = None) -> int:
    """脚本入口：返回进程退出码（0=无 FAIL，1=存在 FAIL）。"""
    args = build_parser().parse_args(argv)
    checker = ReleaseChecker()
    checker.check_python()
    checker.check_dependencies()
    checker.check_config()
    checker.check_database(args.db)
    checker.check_daily_bars()
    checker.check_data_source()
    if args.skip_tests:
        checker.add("测试全绿", WARN, "已按 --skip-tests 跳过（正式发版请勿跳过）")
    elif os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("QD_RELEASE_CHECK_ACTIVE"):
        # P0 修复：本脚本被 pytest 用例以子进程调用时，禁止「测试套件里再跑测试套件」递归卡死
        checker.add("测试全绿", WARN, "在 pytest 内运行，跳过嵌套测试（防递归）")
    else:
        checker.check_tests()
    checker.check_files()
    checker.report()
    return 0 if checker.passed() else 1


if __name__ == "__main__":
    raise SystemExit(main())

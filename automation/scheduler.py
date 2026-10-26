"""QuantDesk 自动化调度器（M3-01，APScheduler 封装）。

能力（SPEC §6.5）
----------------
- ``start()`` / ``stop()`` / ``is_running()``：一键启停，重复 start/stop 幂等；
- ``status()``：返回 ``list[JobStatus]``（job_key/name/next_run_time/last_run/last_status）；
- ``trigger_now(job_key)``：手动立即执行并返回 ``trace_id``（不影响定时计划）；
- 三个固定 job（盘前 08:50 / 盘中 10:00 / 盘后 15:30，时间读 ``automation.jobs.*``）；
- 错过执行：``misfire_grace_time=300s``、``coalesce=True``（错过只补一次、不补历史）；
- **单实例锁**：同一台机器只允许一个调度器进程运行（运行时锁文件），避免重复下单；
- 启停/手动触发写审计（``automation.start`` / ``automation.stop`` / ``job.manual_trigger``）。

安全默认
--------
``automation.enabled`` 默认 false：调度器可以启动/手动触发 job，但 ``scan`` 内的自动下单
由 ``job_scan`` 再校验该开关，关闭时只生成信号（R3 要求）。
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from core.logging import AuditAction, write_audit
from core.models import JobResult, JobRunStatus, JobStatus, now_local

from automation.jobs import (
    JOB_KEYS,
    JOB_NAMES,
    get_job_context,
    job_prepare,
    job_scan,
    job_summarize,
    run_job,
)

logger = logging.getLogger(__name__)

_DEFAULT_JOB_FUNCS: dict[str, Callable[[str | None], JobResult]] = {
    "prepare": job_prepare,
    "scan": job_scan,
    "summarize": job_summarize,
}


class _InstanceLock:
    """跨进程单实例锁（Windows: msvcrt 文件锁；其它平台: fcntl）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any = None
        self._locked = False

    def acquire(self) -> tuple[bool, str]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._handle = open(self.path, "a+", encoding="utf-8")  # noqa: SIM115 - 需跨调用持有
        except OSError as exc:
            return False, f"无法打开调度器锁文件 {self.path}：{exc}"
        try:
            if os.name == "nt":  # pragma: no cover - Windows 分支
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - POSIX 分支
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            detail = "调度器已在另一个进程/实例中运行"
            try:
                self._handle.close()
            finally:
                self._handle = None
            return False, f"{detail}（锁文件：{self.path}）；同一时间只允许一个调度器，防止重复下单"
        self._locked = True
        try:
            self._handle.seek(0)
            self._handle.truncate()
            self._handle.write(f"{os.getpid()}|{now_local().isoformat()}\n")
            self._handle.flush()
        except OSError:  # pragma: no cover
            pass
        return True, "单实例锁已获取"

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if self._locked and os.name == "nt":  # pragma: no cover
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            elif self._locked:  # pragma: no cover
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            logger.debug("释放调度器锁失败", exc_info=True)
        finally:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None
            self._locked = False


class SchedulerService:
    """APScheduler 调度服务（BackgroundScheduler，本地时区 Asia/Shanghai）。

    :param config: 全局配置（``AppConfig``）；为空时读 ``core.config.get_config()``。
    :param job_funcs: 注入 job 函数（测试用）；默认 ``prepare/scan/summarize``。
    :param job_recorder: job 执行记录写入器（默认走 job 上下文的 ``job_run_writer``）。
    :param lock_path: 单实例锁文件路径；默认 ``runtime/automation.lock``。
    :param scheduler: 注入 APScheduler 实例（测试可用假对象）。
    :param clock: 时钟，默认 ``now_local``。
    """

    def __init__(
        self,
        config: Any = None,
        *,
        job_funcs: dict[str, Callable[[str | None], JobResult]] | None = None,
        job_recorder: Any = None,
        lock_path: str | Path | None = None,
        scheduler: Any = None,
        clock: Callable[[], datetime] | None = None,
        audit_writer: Callable[..., Any] | None = None,
    ) -> None:
        if config is None:
            from core.config import get_config

            config = get_config()
        self.config = config
        self.clock = clock or now_local
        self._job_funcs = dict(job_funcs or _DEFAULT_JOB_FUNCS)
        self._job_recorder = job_recorder
        self._audit_writer = audit_writer
        self._lock_path = Path(lock_path) if lock_path else self._default_lock_path()
        self._instance_lock = _InstanceLock(self._lock_path)
        self._scheduler = scheduler
        self._started = False
        self._lock = threading.RLock()
        self._last_runs: dict[str, dict[str, Any]] = {key: {} for key in JOB_KEYS}
        self._apscheduler_available = self._check_apscheduler()

    # -- 启动 / 停止 / 状态 ----------------------------------------------

    def is_available(self) -> bool:
        """APScheduler 是否可用（缺失时 UI 显示安装提示）。"""
        return self._apscheduler_available

    def is_running(self) -> bool:
        """调度器是否运行中。"""
        if self._scheduler is not None:
            return bool(self._started and getattr(self._scheduler, "running", True))
        return self._started

    def start(self) -> bool:
        """启动调度器（幂等）；返回是否处于运行态。

        :raises RuntimeError: APScheduler 未安装或单实例锁被占用（中文提示）。
        """
        with self._lock:
            if self.is_running():
                logger.info("调度器已在运行，重复 start 幂等返回")
                return True
            if not self._apscheduler_available and self._scheduler is None:
                raise RuntimeError(
                    "调度器启动失败：APScheduler 未安装。请执行 "
                    "`pip install APScheduler` 后重启（自动化依赖 APScheduler，SPEC §4）"
                )
            acquired, detail = self._instance_lock.acquire()
            if not acquired:
                raise RuntimeError(f"调度器启动失败：{detail}")
            try:
                if self._scheduler is None:
                    from apscheduler.schedulers.background import BackgroundScheduler
                    from apscheduler.triggers.cron import CronTrigger
                    import zoneinfo

                    timezone = zoneinfo.ZoneInfo(str(getattr(getattr(self.config, "app", None), "timezone", "Asia/Shanghai")))
                    self._scheduler = BackgroundScheduler(timezone=timezone)
                self._register_jobs()
                if hasattr(self._scheduler, "start"):
                    self._scheduler.start()
                self._started = True
            except Exception:
                self._started = False
                self._instance_lock.release()
                raise
        self._audit(AuditAction.AUTOMATION_START, {"running": True, "jobs": list(JOB_KEYS)})
        logger.info("调度器已启动：jobs=%s", list(JOB_KEYS))
        return True

    def stop(self) -> bool:
        """停止调度器（幂等）；不取消已提交订单（SPEC §8.5）。"""
        with self._lock:
            if not self._started:
                self._instance_lock.release()
                return False
            if self._scheduler is not None and hasattr(self._scheduler, "shutdown"):
                try:
                    self._scheduler.shutdown(wait=False)
                except Exception:  # noqa: BLE001 - 关闭失败也要释放锁
                    logger.error("调度器关闭异常", exc_info=True)
            self._started = False
            self._instance_lock.release()
        self._audit(AuditAction.AUTOMATION_STOP, {"running": False})
        logger.info("调度器已停止（已提交订单不受影响）")
        return True

    def status(self) -> list[JobStatus]:
        """返回三个 job 的状态（下次运行/上次结果），供自动化页展示。"""
        rows: list[JobStatus] = []
        aps_jobs: dict[str, Any] = {}
        if self._scheduler is not None:
            try:
                aps_jobs = {job.id: job for job in self._scheduler.get_jobs()}
            except Exception:  # noqa: BLE001
                aps_jobs = {}
        for key in JOB_KEYS:
            last = self._last_runs.get(key, {})
            aps_job = aps_jobs.get(key)
            rows.append(
                JobStatus(
                    job_key=key,
                    name=JOB_NAMES.get(key, key),
                    next_run_time=getattr(aps_job, "next_run_time", None),
                    last_run=last.get("last_run"),
                    last_status=last.get("last_status"),
                    last_message=last.get("last_message", ""),
                    enabled=True,
                )
            )
        return rows

    def trigger_now(self, job_key: str) -> str:
        """手动立即执行任务，返回 ``trace_id``（写审计与 job_runs，不影响定时计划）。"""
        key = str(job_key or "").strip().lower()
        if key not in JOB_KEYS:
            raise ValueError(f"未知任务：{job_key!r}（只支持 {', '.join(JOB_KEYS)}）")
        trace_id = uuid.uuid4().hex
        self._audit(AuditAction.JOB_MANUAL_TRIGGER, {"job_key": key, "trace_id": trace_id})
        self._execute(key, trace_id)
        return trace_id

    # -- 内部 -------------------------------------------------------------

    def _execute(self, key: str, trace_id: str | None = None) -> JobResult:
        """执行 job 并更新最近状态（异常隔离：单次失败不影响后续调度）。"""
        func = self._job_funcs.get(key, run_job if key else None)
        trace = trace_id or uuid.uuid4().hex
        try:
            if func is None:
                result = run_job(key, trace)
            elif key in self._job_funcs:
                result = func(trace)
            else:  # pragma: no cover - 理论上不可达
                result = run_job(key, trace)
        except Exception as exc:  # noqa: BLE001 - 调度器必须继续存活
            logger.error("定时任务执行失败：job=%s trace=%s", key, trace, exc_info=True)
            result = JobResult(
                job_key=key,
                trace_id=trace,
                status=JobRunStatus.FAILED,
                message=f"任务执行失败：{type(exc).__name__}: {exc}",
                error=f"{type(exc).__name__}: {exc}",
            )
        if not isinstance(result, JobResult):  # 兼容注入假 job 返回 None
            result = JobResult(job_key=key, trace_id=trace, status=JobRunStatus.SUCCESS, message="任务完成")
        self._last_runs[key] = {
            "last_run": result.finished_at or now_local(),
            "last_status": str(getattr(result.status, "value", result.status)),
            "last_message": result.message,
        }
        self._record_run(result)
        logger.info("定时任务完成：job=%s status=%s message=%s", key, self._last_runs[key]["last_status"], result.message)
        return result

    def _record_run(self, result: JobResult) -> None:
        recorder = self._job_recorder
        if recorder is None:
            try:
                ctx = get_job_context()
                recorder = ctx.job_run_writer
            except Exception:  # noqa: BLE001 - 无上下文时跳过落库
                recorder = None
        if recorder is None:
            return
        payload = {
            "job_key": result.job_key,
            "job_name": JOB_NAMES.get(result.job_key, result.job_key),
            "trace_id": result.trace_id,
            "started_at": result.started_at,
            "finished_at": result.finished_at or now_local(),
            "status": str(getattr(result.status, "value", result.status)),
            "message": result.message,
            "stats": result.stats,
        }
        try:
            handler = getattr(recorder, "record", None)
            if callable(handler):
                handler(payload)
            elif callable(recorder):
                recorder(payload)
        except Exception:  # noqa: BLE001 - 写 job_runs 失败不影响调度
            logger.error("写 job_runs 失败：job=%s", result.job_key, exc_info=True)

    def _register_jobs(self) -> None:
        """把三个 job 注册为每日 cron 任务（时间读 config.automation.jobs.*）。"""
        from apscheduler.triggers.cron import CronTrigger

        jobs_cfg = getattr(getattr(self.config, "automation", None), "jobs", None)
        for key in JOB_KEYS:
            raw_time = str(getattr(jobs_cfg, key, "") or {"prepare": "08:50", "scan": "10:00", "summarize": "15:30"}[key])
            try:
                hour, minute = [int(part) for part in raw_time.split(":")[:2]]
            except (TypeError, ValueError):
                raise ValueError(f"自动化任务时间配置非法：automation.jobs.{key}={raw_time!r}（应为 HH:MM）")
            trigger = CronTrigger(hour=hour, minute=minute)
            self._scheduler.add_job(
                self._job_entry,
                trigger=trigger,
                id=key,
                name=JOB_NAMES.get(key, key),
                args=[key],
                replace_existing=True,
                misfire_grace_time=300,
                coalesce=True,
                max_instances=1,
            )

    def _job_entry(self, key: str) -> None:
        """APScheduler 实际调用的入口（异常已在 _execute 内隔离）。"""
        self._execute(key)

    def _default_lock_path(self) -> Path:
        base = getattr(self.config, "base_dir", None)
        if base is None:
            try:
                from core.config import PROJECT_ROOT

                base = PROJECT_ROOT
            except Exception:  # noqa: BLE001
                base = Path.cwd()
        return Path(base) / "runtime" / "automation.lock"

    def _check_apscheduler(self) -> bool:
        if self._scheduler is not None:
            return True
        try:
            import apscheduler  # noqa: F401, PLC0415

            return True
        except ImportError:
            return False

    def _audit(self, action: str, after: dict[str, Any]) -> None:
        try:
            if callable(self._audit_writer):
                self._audit_writer(action, ("automation", action), None, after, "scheduler", None, "SUCCESS")
                return
            write_audit(action, ("automation", action), None, after, actor="scheduler")
        except Exception:  # noqa: BLE001 - 审计失败不阻塞启停
            logger.debug("写调度器审计失败：action=%s", action, exc_info=True)


# ---------------------------------------------------------------------------
# 全局单例（UI/自动化页只通过这里拿调度器）
# ---------------------------------------------------------------------------

_DEFAULT_SCHEDULER: SchedulerService | None = None


def init_scheduler(service: SchedulerService | None = None, **kwargs: Any) -> SchedulerService:
    """初始化全局调度器单例。"""
    global _DEFAULT_SCHEDULER
    _DEFAULT_SCHEDULER = service or SchedulerService(**kwargs)
    return _DEFAULT_SCHEDULER


def get_scheduler() -> SchedulerService:
    """获取全局调度器单例（未初始化时自动按配置创建）。"""
    global _DEFAULT_SCHEDULER
    if _DEFAULT_SCHEDULER is None:
        _DEFAULT_SCHEDULER = SchedulerService()
    return _DEFAULT_SCHEDULER


def reset_scheduler() -> None:
    """停止并清空全局调度器（测试隔离）。"""
    global _DEFAULT_SCHEDULER
    if _DEFAULT_SCHEDULER is not None:
        try:
            _DEFAULT_SCHEDULER.stop()
        except Exception:  # noqa: BLE001
            pass
    _DEFAULT_SCHEDULER = None


__all__ = [
    "SchedulerService",
    "init_scheduler",
    "get_scheduler",
    "reset_scheduler",
]

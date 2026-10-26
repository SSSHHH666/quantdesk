"""QuantDesk 自动化包（M3）：调度器 / 三个定时任务 / 通知渠道。"""

from automation.jobs import (
    AUDIT_ACTION_SCAN,
    JOB_KEYS,
    JOB_NAMES,
    JOB_PREPARE,
    JOB_SCAN,
    JOB_SUMMARIZE,
    JobContext,
    SqlJobRunRecorder,
    SqlSignalStore,
    SqlStrategyReader,
    build_job_context,
    get_job_context,
    init_job_context,
    job_prepare,
    job_scan,
    job_summarize,
    reset_job_context,
    run_job,
)
from automation.notify import CHANNEL_DESKTOP, CHANNEL_EMAIL, CHANNEL_WEBHOOK, Notifier, NotifyReport
from automation.scheduler import SchedulerService, get_scheduler, init_scheduler, reset_scheduler

__all__ = [
    # 调度器（M3-01）
    "SchedulerService",
    "init_scheduler",
    "get_scheduler",
    "reset_scheduler",
    # 三个 job（M3-02/M3-03）
    "JOB_KEYS",
    "JOB_NAMES",
    "JOB_PREPARE",
    "JOB_SCAN",
    "JOB_SUMMARIZE",
    "AUDIT_ACTION_SCAN",
    "JobContext",
    "init_job_context",
    "get_job_context",
    "reset_job_context",
    "build_job_context",
    "job_prepare",
    "job_scan",
    "job_summarize",
    "run_job",
    "SqlStrategyReader",
    "SqlSignalStore",
    "SqlJobRunRecorder",
    # 通知（M3-04）
    "Notifier",
    "NotifyReport",
    "CHANNEL_DESKTOP",
    "CHANNEL_EMAIL",
    "CHANNEL_WEBHOOK",
]

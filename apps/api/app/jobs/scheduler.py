from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler

from app.core.config import get_settings
from app.jobs.counterparty_invoice_sync_job import run_counterparty_invoice_sync_job
from app.jobs.employee_sync_job import run_employee_sync_job

_scheduler: BackgroundScheduler | None = None


def get_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        settings = get_settings()
        _scheduler = BackgroundScheduler(timezone="Europe/Moscow")
        if settings.scheduler_enabled:
            register_jobs(_scheduler)
    return _scheduler


def register_jobs(scheduler: BackgroundScheduler) -> None:
    settings = get_settings()
    if settings.employee_sync_enabled:
        scheduler.add_job(
            run_employee_sync_job,
            "interval",
            hours=settings.employee_sync_interval_hours,
            id="employee_iiko_sync",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    if settings.counterparty_invoice_sync_enabled:
        scheduler.add_job(
            run_counterparty_invoice_sync_job,
            "interval",
            hours=settings.counterparty_invoice_sync_interval_hours,
            id="counterparty_invoice_sync",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from app.core.config import get_settings
from app.jobs.counterparty_invoice_sync_job import run_counterparty_invoice_sync_job
from app.jobs.employee_sync_job import run_employee_sync_job
from app.jobs.lease_accrual_job import run_lease_accrual_job
from app.jobs.sbis_sync_job import run_sbis_sync_job
from app.jobs.supplier_closing_activation_job import run_supplier_closing_activation_job
from app.jobs.supplier_service_period_job import run_supplier_service_period_job

# Синглтон на процесс: lifespan приложения стартует и гасит ОДИН И ТОТ ЖЕ объект.
# В проде это ровно один цикл, но в тестах TestClient поднимает lifespan на каждый тест —
# 250+ start/shutdown подряд. После первого shutdown() внутренний ThreadPoolExecutor
# apscheduler мёртв навсегда: повторный start() поднимет планировщик, который не может
# исполнить ни одну джобу. Прогон тестов держится только на SCHEDULER_ENABLED=false
# (см. apps/api/tests/conftest.py) — без него джобы ещё и пишут в тестовую БД параллельно
# с teardown-транкейтом. Не включай планировщик в тестах: сначала научи get_scheduler()
# отдавать свежий объект после shutdown.
_scheduler: BackgroundScheduler | None = None
MOSCOW_TZ = ZoneInfo("Europe/Moscow")


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
    # Аренда за месяц — ДО признания расходов и активации закрывающих: свежесозданное
    # обязательство должно попасть в тот же ночной проход, а не ждать сутки.
    scheduler.add_job(
        run_lease_accrual_job,
        "cron",
        hour=0,
        minute=3,
        id="lease_monthly_accrual",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        run_supplier_service_period_job,
        "cron",
        hour=0,
        minute=5,
        id="supplier_service_period_recognition",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    # Правило 4 канона ДЗ/КЗ: будущий УПД вступает в силу в свою дату (гасит дебиторку /
    # встаёт в кредиторку). Сразу после ночного признания расходов.
    scheduler.add_job(
        run_supplier_closing_activation_job,
        "cron",
        hour=0,
        minute=10,
        id="supplier_closing_activation",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    if settings.employee_sync_enabled:
        scheduler.add_job(
            run_employee_sync_job,
            "interval",
            hours=settings.employee_sync_interval_hours,
            id="employee_iiko_sync",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            # Тикаем сразу при старте api: контейнер пересоздаётся на каждом деплое, и при
            # 6-часовом интервале cron-тик почти никогда не доживает до запуска — новые
            # сотрудники/курьеры из iiko висят несопоставленными. Прогон при старте
            # подтягивает их без ожидания следующего интервала.
            next_run_time=datetime.now(MOSCOW_TZ),
        )
    if settings.counterparty_invoice_sync_enabled:
        scheduler.add_job(
            run_counterparty_invoice_sync_job,
            "interval",
            minutes=settings.counterparty_invoice_sync_interval_minutes,
            id="counterparty_invoice_sync",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    if settings.sbis_sync_enabled:
        scheduler.add_job(
            run_sbis_sync_job,
            "interval",
            minutes=settings.sbis_sync_interval_minutes,
            id="sbis_document_sync",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

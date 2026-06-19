from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.services.iiko_sync import SyncResult, sync_employees
from app.services.position_registry import refresh_position_registry

logger = logging.getLogger(__name__)


async def _run() -> SyncResult:
    # BackgroundScheduler выполняет джоб через asyncio.run() в worker-потоке — на каждый
    # тик это НОВЫЙ event loop. Глобальный engine (app.db.session) привязан к loop'у
    # FastAPI; переиспользование его отсюда корёжит asyncpg-пул ("attached to a different
    # loop") и роняет последующие API-запросы в Network Error. Поэтому поднимаем
    # одноразовый engine (NullPool — нечего переиспользовать между loop'ами) в ЭТОМ loop'е
    # и закрываем его по завершении — точно так же, как counterparty_invoice_sync_job.
    # Перед синком освежаем реестр должностей, чтобы skip-логика по должностям не работала
    # по старому снапшоту.
    settings = get_settings()
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_maker() as session:
            await refresh_position_registry(session)
        async with session_maker() as session:
            return await sync_employees(session, run_reason="cron")
    finally:
        await engine.dispose()


def run_employee_sync_job() -> None:
    try:
        result = asyncio.run(_run())
    except Exception:
        logger.exception("iiko employee sync job failed")
        raise
    logger.info("iiko employee sync completed: %s", result.as_dict())

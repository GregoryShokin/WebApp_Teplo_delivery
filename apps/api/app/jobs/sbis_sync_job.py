from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.services.sbis.sync import sync_sbis_documents

logger = logging.getLogger(__name__)


async def _run() -> None:
    # Как и counterparty_invoice_sync_job: BackgroundScheduler тикает в свежем event loop,
    # поэтому app-глобальный движок использовать нельзя — одноразовый NullPool-движок.
    settings = get_settings()
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_maker() as session:
            result = await sync_sbis_documents(session, run_reason="cron")
        logger.info("sbis document sync completed: %s", result.as_dict())
    finally:
        await engine.dispose()


def run_sbis_sync_job() -> None:
    try:
        asyncio.run(_run())
    except Exception:
        logger.exception("sbis document sync job failed")
        raise

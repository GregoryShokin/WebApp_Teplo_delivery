from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.services.kassa.iiko_cashshift_sync import sync_iiko_cashshifts

logger = logging.getLogger(__name__)

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


async def _run() -> None:
    # BackgroundScheduler runs this via asyncio.run() in a worker thread — a FRESH event loop
    # per tick. Reusing the app-global async engine (bound to FastAPI's loop) corrupts its
    # asyncpg pool, breaking every later API request. So build a throwaway NullPool engine in
    # THIS loop and dispose it (see counterparty_invoice_sync_job).
    settings = get_settings()
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        today = datetime.now(MOSCOW_TZ).date()
        date_from = today - timedelta(days=settings.kassa_cashshift_sync_days)
        async with session_maker() as session:
            report = await sync_iiko_cashshifts(session, date_from=date_from, date_to=today)
        logger.info("kassa cashshift sync completed: %s", report.as_dict())
    finally:
        await engine.dispose()


def run_kassa_cashshift_sync_job() -> None:
    try:
        asyncio.run(_run())
    except Exception:
        logger.exception("kassa cashshift sync job failed")
        raise

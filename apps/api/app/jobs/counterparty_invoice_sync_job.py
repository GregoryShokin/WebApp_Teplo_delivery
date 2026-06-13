from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.services.counterparty_invoice_sync import sync_counterparty_invoices

logger = logging.getLogger(__name__)


async def _run() -> None:
    # BackgroundScheduler runs this via asyncio.run() in a worker thread — a FRESH event
    # loop on every tick. The app-global async engine (app.db.session) is bound to FastAPI's
    # loop, and reusing it from here corrupts its asyncpg pool ("attached to a different
    # loop" / "unknown protocol state"), which then breaks every subsequent API request with
    # a Network Error. So build a throwaway engine in THIS loop (NullPool — nothing to reuse
    # across loops) and dispose it when done.
    settings = get_settings()
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_maker() as session:
            result = await sync_counterparty_invoices(
                session,
                days=settings.counterparty_invoice_sync_cron_days,
                run_reason="cron",
            )
        logger.info("counterparty invoice sync completed: %s", result.as_dict())
    finally:
        await engine.dispose()


def run_counterparty_invoice_sync_job() -> None:
    try:
        asyncio.run(_run())
    except Exception:
        logger.exception("counterparty invoice sync job failed")
        raise

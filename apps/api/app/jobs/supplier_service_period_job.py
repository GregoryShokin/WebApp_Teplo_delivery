from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.services.supplier_service_periods import recognize_due_expenses

logger = logging.getLogger(__name__)


async def _run() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_maker() as session:
            recognized = await recognize_due_expenses(session)
        if recognized:
            logger.info("supplier service periods recognized: %s", recognized)
    finally:
        await engine.dispose()


def run_supplier_service_period_job() -> None:
    try:
        asyncio.run(_run())
    except Exception:
        logger.exception("supplier service period recognition failed")
        raise

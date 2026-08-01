"""Помесячное признание абонентских платежей, по которым закрывающих документов не будет.

Идёт ДО признания расходов (``supplier_service_period_job``): самоакт, созданный этой
джобой, должен попасть в тот же ночной проход, а не ждать сутки — иначе расход месяца
появлялся бы в P&L на день позже, чем нужно. Тот же порядок, что у аренды.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.services.subscription_accruals import accrue_due_months

logger = logging.getLogger(__name__)


async def _run() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_maker() as session:
            created = await accrue_due_months(session)
        if created:
            logger.info("subscription months recognized without documents: %s", len(created))
    finally:
        await engine.dispose()


def run_subscription_accrual_job() -> None:
    try:
        asyncio.run(_run())
    except Exception:
        logger.exception("subscription monthly accrual failed")
        raise

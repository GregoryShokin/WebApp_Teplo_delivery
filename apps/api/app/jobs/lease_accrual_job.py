from __future__ import annotations

import asyncio
import logging
from datetime import date

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.services.lease_accruals import accrue_month

logger = logging.getLogger(__name__)


async def _run() -> None:
    """Начислить аренду за текущий месяц по всем действующим договорам.

    Идемпотентно: ключ ``lease:{id}:{YYYY-MM}`` под уникальным индексом, поэтому повторный
    прогон (в том числе из второго воркера) ничего не задваивает. Начисляем ЗАРАНЕЕ, в начале
    месяца: документ с будущей датой ложится в ``pending`` и вступит в силу сам — так владелец
    видит предстоящий платёж в календаре, а долгом он станет в свою дату.
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_maker() as session:
            created = await accrue_month(session, date.today())
            if created:
                await session.commit()
                logger.info("lease accruals created: %s", len(created))
    finally:
        await engine.dispose()


def run_lease_accrual_job() -> None:
    try:
        asyncio.run(_run())
    except Exception:
        logger.exception("lease monthly accrual failed")
        raise

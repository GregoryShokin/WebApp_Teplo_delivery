from __future__ import annotations

import asyncio
import logging
from datetime import date

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.services.service_agreement_accruals import accrue_month

logger = logging.getLogger(__name__)


async def _run() -> None:
    """Начислить долг по договорам услуг за ЗАКОНЧИВШИЕСЯ месяцы.

    Отличие от аренды: аренда начисляется вперёд (документ с будущей датой ложится в pending и
    вступает в силу сам, чтобы платёж был виден в календаре заранее), а услуга без документов —
    только по факту оказания, после окончания месяца. Ставка у таких договоров может смениться
    или услугу приостановят, и начисленный вперёд долг пришлось бы отменять задним числом.

    Идемпотентно: ключ ``svc:{id}:{YYYY-MM}`` под уникальным индексом ``source + external_id``,
    поэтому повторный прогон (в том числе из второго воркера) ничего не задваивает.
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_maker() as session:
            created = await accrue_month(session, date.today())
            if created:
                await session.commit()
                logger.info("service agreement accruals created: %s", len(created))
    finally:
        await engine.dispose()


def run_service_agreement_accrual_job() -> None:
    try:
        asyncio.run(_run())
    except Exception:
        logger.exception("service agreement monthly accrual failed")
        raise

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.services.supplier_prepayments import activate_due_closing_invoices

logger = logging.getLogger(__name__)


async def _run() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_maker() as session:
            result = await activate_due_closing_invoices(session)
        if result.get("activated"):
            logger.info("supplier closing documents activated: %s", result)
    finally:
        await engine.dispose()


def run_supplier_closing_activation_job() -> None:
    """Правило 4 канона ДЗ/КЗ: закрывающие документы (УПД/акт) с наступившей ДАТОЙ ДОКУМЕНТА
    вступают в силу — гасят открытую дебиторку / встают в кредиторку. Контрагенты присылают
    УПД заранее (ЭкоЦентр — УПД июля датой 31.07); джоба проводит их в свою дату."""
    try:
        asyncio.run(_run())
    except Exception:
        logger.exception("supplier closing document activation failed")
        raise

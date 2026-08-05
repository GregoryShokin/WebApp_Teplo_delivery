from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.services.pnl.iiko_sync import sync_month

logger = logging.getLogger(__name__)


def target_months(today: date) -> tuple[date, date]:
    """Текущий месяц и предыдущий закрытый месяц для ночного зеркала ОПиУ."""
    current = today.replace(day=1)
    previous = (current - timedelta(days=1)).replace(day=1)
    return current, previous


async def _run(today: date | None = None) -> None:
    """Обновить iiko-факты ОПиУ и товарную расшифровку за два рабочих месяца.

    Предыдущий месяц перечитывается вместе с текущим: закрывающие инвентаризации и
    накладные часто появляются уже после первого числа. Каждый месяц коммитится отдельно,
    поэтому временная ошибка второго запроса не откатывает уже обновлённый первый.
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    errors: list[tuple[date, Exception]] = []
    try:
        for month in target_months(today or date.today()):
            try:
                async with session_maker() as session:
                    result = await sync_month(session, month)
                    await session.commit()
                logger.info(
                    "pnl iiko sync completed: month=%s facts=%s rows=%s changed=%s",
                    month.isoformat(),
                    len(result.facts),
                    result.rows_seen,
                    result.changed,
                )
            except Exception as exc:  # noqa: BLE001 — второй месяц всё равно пробуем
                errors.append((month, exc))
                logger.exception("pnl iiko sync failed: month=%s", month.isoformat())
    finally:
        await engine.dispose()

    if errors:
        failed = ", ".join(month.isoformat() for month, _error in errors)
        raise RuntimeError(f"pnl iiko sync failed for: {failed}") from errors[0][1]


def run_pnl_iiko_sync_job() -> None:
    try:
        asyncio.run(_run())
    except Exception:
        logger.exception("pnl iiko sync job failed")
        raise

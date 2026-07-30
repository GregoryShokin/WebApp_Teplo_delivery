from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.services.fixed_assets import close_month

logger = logging.getLogger(__name__)


def previous_month(today: date) -> date:
    """Первое число месяца, который только что закончился."""
    first = today.replace(day=1)
    return (first - timedelta(days=1)).replace(day=1)


async def _run(today: date | None = None) -> None:
    """Закрыть ПРОШЕДШИЙ месяц: начислить амортизацию по всем объектам.

    Джоба ходит 1-го числа и закрывает месяц, который только что кончился (решение владельца
    2026-07-30). Именно прошедший, а не текущий: амортизация начисляется за отработанное
    время, и начислять за месяц, который ещё идёт, нечего.

    Идемпотентно: строка ключуется парой «объект и месяц» под уникальным индексом, поэтому
    повторный прогон (в том числе ручной перезапуск владельцем) ничего не задваивает и не
    затирает суммы, поправленные вручную.

    Прогон пишется в журнал ``agent_run`` — в логах контейнера он бы не пережил пересборку,
    а на вопрос «почему за сентябрь начислено меньше» нужно уметь ответить через полгода.
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    period = previous_month(today or date.today())
    try:
        async with session_maker() as session:
            run = await close_month(session, period_month=period, reason="cron")
            logger.info(
                "fixed assets month closed: %s, entries=%s, amount=%s",
                period.isoformat(),
                run.result.get("entries"),
                run.result.get("amount"),
            )
    finally:
        await engine.dispose()


def run_depreciation_job() -> None:
    try:
        asyncio.run(_run())
    except Exception:
        logger.exception("fixed assets month close failed")
        raise

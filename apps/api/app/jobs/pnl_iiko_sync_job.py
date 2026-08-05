from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.services import accounting_periods
from app.services.pnl.iiko_sync import sync_month

logger = logging.getLogger(__name__)


#: До какого числа нового месяца перечитываем предыдущий. Закрывающие инвентаризации и
#: накладные приходят в первые дни; дальше пересчёт приносит только вред.
LATE_DOCUMENT_WINDOW_DAYS = 10


def target_months(today: date) -> tuple[date, ...]:
    """Месяцы ночного зеркала ОПиУ: текущий всегда, предыдущий — пока идут поздние документы.

    ПОЧЕМУ У ПРОШЛОГО МЕСЯЦА ЕСТЬ СРОК ГОДНОСТИ. Разметка товаров глобальна: у правила нет
    периода действия, ``load_whitelist`` применяет к любому месяцу то, что записано СЕЙЧАС.
    Пока джоба ходила в прошлый месяц каждую ночь, любая правка разметки — хоть руками на
    странице августа, хоть автоматикой внутри того же прогона — переписывала июльские цифры,
    уже показанные и сверенные. Синхронный путь это запрещает (правка действует с
    редактируемого месяца вперёд), и ночной обязан вести себя так же.

    Окно, а не запрет: до десятого числа прошлый месяц ещё честно добирает поздние документы,
    ради которых его и перечитывают. После — застывает, даже если период не закрыли явно.
    Явный замок (``AccountingPeriodClose``) сильнее и действует с первого дня.
    """
    current = today.replace(day=1)
    if today.day > LATE_DOCUMENT_WINDOW_DAYS:
        return (current,)
    previous = (current - timedelta(days=1)).replace(day=1)
    return current, previous


async def _run(today: date | None = None) -> None:
    """Обновить iiko-факты ОПиУ и товарную расшифровку за два рабочих месяца.

    Предыдущий месяц перечитывается вместе с текущим: закрывающие инвентаризации и
    накладные часто появляются уже после первого числа. Каждый месяц коммитится отдельно,
    поэтому временная ошибка второго запроса не откатывает уже обновлённый первый.

    ЗАКРЫТЫЙ МЕСЯЦ ПРОПУСКАЕТСЯ. Ровно то, ради чего перечитывается предыдущий месяц —
    поздние документы, — после закрытия становится вредом: цифры уже показаны и сверены,
    а ночной прогон изменил бы их без единого следа. Замок снимается человеком в разделе
    «Учёт», и тогда следующая же ночь дотянет всё, что накопилось.
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    errors: list[tuple[date, Exception]] = []
    try:
        for month in target_months(today or date.today()):
            try:
                async with session_maker() as session:
                    if await accounting_periods.is_month_closed(session, month):
                        logger.info(
                            "pnl iiko sync skipped: month=%s закрыт в учёте",
                            month.isoformat(),
                        )
                        continue
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

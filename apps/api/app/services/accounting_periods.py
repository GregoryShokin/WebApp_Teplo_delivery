"""Закрытый учётный период: месяц, цифры которого менять уже нельзя.

ЗАЧЕМ ЗАМОК, А НЕ СНИМОК. У основных средств закрытие месяца делает снимок
(``AssetBalanceSnapshot``), и это там необходимо: остаточная стоимость выводится из
первоначальной, а та пересчитывается ЗАДНИМ ЧИСЛОМ при любой коррекции — без снимка баланс
июля в сентябре тихо стал бы другим.

В расчётах с контрагентами пересчёта задним числом нет вовсе. Признанное начисление меняется
только осознанным действием человека: правка периода, откат расхода, повторное признание.
Значит достаточно эти действия запретить — и данные останутся верными без второй копии, которая
сама по себе стала бы источником расхождений.

ЧТО ИМЕННО ЗАПРЕЩЕНО. Не «любые изменения по контрагенту», а изменения РАСХОДА уже закрытого
месяца. Новый документ за закрытый месяц завести можно — жизнь такова, что УПД за июль
приходит десятого августа. Он признается в свой месяц, и если тот закрыт, признание отложится
разговором с человеком, а не пройдёт молча.

ОТКРЫТЬ ОБРАТНО можно — тем же правом ``accounting.periods.close``. Замок здесь не про
неприкосновенность, а про то, чтобы отчёт нельзя было изменить НЕЗАМЕТНО: открытие месяца
остаётся в журнале, а молчаливая правка не оставляла следа вовсе.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AccountingPeriodClose


class PeriodClosed(ValueError):
    """Месяц закрыт — изменение его цифр отклонено, с объяснением."""


def month_start(value: date) -> date:
    return value.replace(day=1)


async def closed_months(session: AsyncSession) -> set[date]:
    rows = await session.scalars(select(AccountingPeriodClose.period_month))
    return set(rows.all())


async def is_month_closed(session: AsyncSession, month: date) -> bool:
    found = await session.scalar(
        select(AccountingPeriodClose.id).where(
            AccountingPeriodClose.period_month == month_start(month)
        )
    )
    return found is not None


async def assert_month_open(session: AsyncSession, month: date | None, *, action: str) -> None:
    """Бросить ``PeriodClosed``, если месяц закрыт. ``None`` — расход ещё не в месяце, можно.

    ``action`` попадает в текст сообщения: человек должен понимать не только «нельзя», но и
    что именно он пытался сделать и как быть дальше.
    """
    if month is None:
        return
    if await is_month_closed(session, month):
        raise PeriodClosed(
            f"{month_start(month):%m.%Y} закрыт — {action} в закрытом месяце нельзя. "
            "Откройте период в разделе «Учёт», если правка действительно нужна"
        )


def months_between(start: date, end: date) -> list[date]:
    """Все календарные месяцы периода — единица, которой мыслит и отчёт, и замок."""
    month = month_start(start)
    last = month_start(end)
    out: list[date] = []
    while month <= last:
        out.append(month)
        year, next_month = divmod(month.month, 12)
        month = date(month.year + year, next_month + 1, 1)
    return out


async def assert_period_open(
    session: AsyncSession, start: date | None, end: date | None, *, action: str
) -> None:
    """Бросить ``PeriodClosed``, если закрыт ХОТЬ ОДИН месяц периода.

    Проверять надо весь период, а не месяц признания. ``recognition_month`` у начисления один —
    месяц окончания услуги, — а отчёт о расходе раскладывает сумму по ВСЕМ месяцам периода
    (``spread_over_months``). Пока замок сторожил только месяц признания, акт за июль-сентябрь,
    признанный в сентябре, добавлял 12 000 ₽ в июль, который был закрыт и сверен с банком, —
    и делал это в обход всех трёх гардов сразу. Единица у замка и у отчёта должна быть одна.
    """
    if start is None or end is None:
        return
    closed = await closed_months(session)
    if not closed:
        return
    hit = [month for month in months_between(start, end) if month in closed]
    if hit:
        listed = ", ".join(f"{month:%m.%Y}" for month in hit)
        raise PeriodClosed(
            f"{listed} закрыт — {action} в закрытом месяце нельзя. "
            "Откройте период в разделе «Учёт», если правка действительно нужна"
        )


async def close_month(
    session: AsyncSession,
    *,
    period_month: date,
    actor_user_id: uuid.UUID | None,
    note: str | None = None,
) -> AccountingPeriodClose:
    """Закрыть месяц. Идемпотентно: повторное закрытие возвращает существующую запись."""
    month = month_start(period_month)
    existing = await session.scalar(
        select(AccountingPeriodClose).where(AccountingPeriodClose.period_month == month)
    )
    if existing is not None:
        return existing
    # Незакончившийся месяц закрывать бессмысленно: в нём ещё будут документы и платежи, и
    # замок пришлось бы снимать в тот же день. Та же граница, что у закрытия месяца ОС.
    from app.services import clock

    if month >= month_start(clock.moscow_today()):
        raise PeriodClosed(
            f"{month:%m.%Y} ещё не закончился — закрывать можно только прошедшие месяцы"
        )
    row = AccountingPeriodClose(
        period_month=month,
        closed_by_user_id=actor_user_id,
        note=(note or "").strip() or None,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def reopen_month(
    session: AsyncSession, *, period_month: date
) -> AccountingPeriodClose | None:
    """Снять замок с месяца. Возвращает снятую запись либо ``None``, если месяц был открыт."""
    month = month_start(period_month)
    row = await session.scalar(
        select(AccountingPeriodClose).where(AccountingPeriodClose.period_month == month)
    )
    if row is None:
        return None
    await session.delete(row)
    await session.commit()
    return row

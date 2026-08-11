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

import re
import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AccountingPeriodClose


class PeriodClosed(ValueError):
    """Месяц закрыт — изменение его цифр отклонено, с объяснением."""


# Дата, с которой ведётся учёт расчётов с контрагентами. На неё разобраны и внесены входящие
# остатки, и всё, что относится к более ранним периодам, уже учтено ими — второй раз те же
# деньги в расход не пойдут. Решение владельца 02.08.2026.
#
# ОТСЕЧКА ИДЁТ ПО ПЕРИОДУ УСЛУГИ, А НЕ ПО ДАТЕ ДЕНЕГ. Услугу почти всегда оплачивают в конце
# предыдущего месяца: платёж 29.06 — это платёж ЗА ИЮЛЬ. Отсечка по дате платежа выбросила бы
# 76 580 ₽ живых июльских предоплат (Директ, Синапсис, ДоксИнБокс), и расход июля недосчитался
# бы их — при том что услуга оказана и деньги ушли.
ACCOUNTING_START = date(2026, 7, 1)


_MONTH_NAMES: dict[str, int] = {
    "январь": 1,
    "января": 1,
    "янв": 1,
    "февраль": 2,
    "февраля": 2,
    "фев": 2,
    "март": 3,
    "марта": 3,
    "мар": 3,
    "апрель": 4,
    "апреля": 4,
    "апр": 4,
    "май": 5,
    "мая": 5,
    "июнь": 6,
    "июня": 6,
    "июн": 6,
    "июль": 7,
    "июля": 7,
    "июл": 7,
    "август": 8,
    "августа": 8,
    "авг": 8,
    "сентябрь": 9,
    "сентября": 9,
    "сен": 9,
    "сент": 9,
    "октябрь": 10,
    "октября": 10,
    "окт": 10,
    "ноябрь": 11,
    "ноября": 11,
    "ноя": 11,
    "декабрь": 12,
    "декабря": 12,
    "дек": 12,
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}


def parse_month_input(value: Any, *, default_year: int | None = None) -> date | None:
    """Принять человеческое представление месяца и вернуть его первое число.

    ``default_year`` нужен для локализованных значений браузера вроде ``июль`` или
    ``июль-01``. В разборе ДДС это год самой проводки, поэтому короткий ввод не может
    случайно уехать в текущий календарный год сервера.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return date(value.year, value.month, 1)
    if isinstance(value, date):
        return value.replace(day=1)

    text = str(value).strip().casefold().replace("ё", "е")
    if not text:
        return None

    def build(year: int, month: int) -> date:
        try:
            return date(year, month, 1)
        except ValueError as error:
            raise ValueError(f"Некорректный месяц расхода: {value!r}") from error

    # Год первым: 2026-07, 2026/7/15, 2026.07.01, 202607 и ISO datetime.
    match = re.match(r"^(\d{4})\s*[-./]\s*(\d{1,2})(?:\s*[-./]\s*\d{1,2})?(?:[t\s].*)?$", text)
    if match:
        return build(int(match.group(1)), int(match.group(2)))
    match = re.fullmatch(r"(\d{4})(\d{2})", text)
    if match:
        return build(int(match.group(1)), int(match.group(2)))

    # Обычная русская дата или месяц с годом: 01.07.2026, 07.2026, 7/2026.
    match = re.fullmatch(r"\d{1,2}\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{4})", text)
    if match:
        return build(int(match.group(2)), int(match.group(1)))
    match = re.fullmatch(r"(\d{1,2})\s*[-./]\s*(\d{4})", text)
    if match:
        return build(int(match.group(2)), int(match.group(1)))

    words = re.findall(r"[a-zа-я]+", text)
    named_month = next((_MONTH_NAMES[word] for word in words if word in _MONTH_NAMES), None)
    if named_month is not None:
        year_match = re.search(r"(?<!\d)(\d{4})(?!\d)", text)
        year = int(year_match.group(1)) if year_match else default_year
        if year is not None:
            return build(year, named_month)

    # Короткое число означает номер месяца в году проводки: 7, 07, 07-01.
    match = re.fullmatch(r"(\d{1,2})(?:\s*[-./]\s*\d{1,2})?", text)
    if match and default_year is not None:
        return build(default_year, int(match.group(1)))

    raise ValueError(
        "Не удалось распознать месяц расхода. Можно указать, например: "
        "2026-07, 07.2026, 1 июля 2026 или июль"
    )


def month_start(value: date) -> date:
    return value.replace(day=1)


def before_accounting_start(period_end: date | None) -> bool:
    """Период услуги целиком раньше начала учёта — расход по нему уже во входящих остатках.

    Только ЯВНО известный период: месяц платежа для этого не годится. У предоплаты он
    систематически на месяц раньше периода услуги, и по фолбэку под отсечку попал бы платёж
    Директа от 29.06 за июль.
    """
    return period_end is not None and period_end < ACCOUNTING_START


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


async def assert_cashflow_month_open(
    session: AsyncSession,
    *,
    expense_month: date | None,
    operation_date: date,
    action: str,
) -> None:
    """Замок для проводки ДДС: расход стоит в размеченном месяце, иначе в месяце денег.

    Отдельная функция, потому что каждая дверь считала этот месяц заново и одна за другой
    считала неверно. Проверка перед выкаткой 06.08.2026 нашла четыре двери, меняющие цифры
    закрытого месяца молча: исключение банковской операции (за июль под ним ходят 239
    проводок на 3 556 683,75 ₽), снятие исключения, переразметка операции с новым
    контрагентом и вторая строка ручного разбора.

    Период до начала учёта пропускаем: закрывать там нечего, отчётов за те месяцы нет.
    """
    month = month_start(expense_month or operation_date)
    if month < ACCOUNTING_START:
        return
    await assert_month_open(session, month, action=action)


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

"""Учёт ОС как источник ОПиУ: амортизация и убыток от выбытия.

Обе величины модуль уже считает и отдаёт готовыми рядами — их докстринги прямо называют
строки отчёта. Поэтому здесь нет ни своей арифметики, ни своих запросов к движениям ОС:
считать амортизацию заново значило бы завести второе место, которое обязано совпадать с
первым, и однажды не совпадёт.

Обе строки неденежные: они не имеют проводки в ДДС и не участвуют в уравнении сходимости.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import asset_balance
from app.services.asset_disposal import disposal_series


async def depreciation_for_month(session: AsyncSession, month_start: date) -> Decimal | None:
    """Начисленная амортизация месяца. ``None`` — начислений не было вовсе.

    Ноль и «нет данных» здесь разные вещи: ноль означает, что месяц закрыт и амортизировать
    нечего, а отсутствие строк — что ночная джоба до месяца не дошла.
    """
    rows = await asset_balance.depreciation_series(
        session, date_from=month_start, date_to=month_start
    )
    for period, amount in rows:
        if period == month_start:
            return amount
    return None


async def disposal_loss_for_month(session: AsyncSession, month_start: date) -> Decimal | None:
    """Убыток от выбытия ОС за месяц — остаточная стоимость списанных объектов.

    Строки в эталоне владельца нет, она заведена по его решению 03.08.2026: модуль эту цифру
    уже считает, а без неё списанное оборудование исчезает из прибыли бесследно.
    """
    rows = await disposal_series(session, date_from=month_start, date_to=month_start)
    for period, amount, _count in rows:
        if period == month_start:
            return amount
    return None

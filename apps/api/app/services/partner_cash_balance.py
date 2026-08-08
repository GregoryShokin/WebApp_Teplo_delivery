"""Долг партнёра по наличным, взятым из смены («В гостях у Алисы») — на произвольную дату.

ОТКУДА БЕРЁТСЯ ДОЛГ. Партнёр забирает часть наличной выручки прямо из смены: в iiko это
изъятие на счёт «Алиса наличные», и оно намеренно НЕ порождает проводку ДДС — деньги ушли не
в расход, а в дебиторку партнёра. Начисление поэтому считается из журнала смен, а не из ДДС:
в ДДС этих денег нет вовсе, и искать их там значило бы не найти ничего.

ЧЕМ ГАСИТСЯ. Партнёр возвращает наличные внесением в кассу. Единственный надёжный ключ здесь —
КОНТРАГЕНТ на проводке: статья «Поступление денег с торг. точек» несёт весь поток инкассаций с
точек (на копии прода это 452 проводки), и считать по ней значило бы вычитать из долга партнёра
обычную выручку. Контрагент проставляется автоматически — его несёт пресет внесения, — но
только если владелец пресет к контрагенту привязал; какой именно контрагент считается партнёром,
объявлено настройкой.

ПОЧЕМУ НАСТРОЙКА, А НЕ ИМЯ В КОДЕ. Партнёр — это договорённость бизнеса, а не константа
программы: «Алиса» может смениться, а строка баланса останется той же. Пока настройка пуста,
расчёт честно отдаёт начисление и говорит, что гашения не привязаны, — вместо того чтобы
угадывать по статье и молча занижать долг.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AppSetting,
    CashflowTransaction,
    IikoCashShift,
    IikoCashShiftPayout,
)
from app.services.banking.cashflow_classify import EXCLUDED_QUALITY
from app.services.kassa.iiko_cashshift_sync import MOSCOW_TZ

_ZERO = Decimal("0.00")

# Кого считать партнёром. Значение — идентификатор контрагента строкой.
PARTNER_COUNTERPARTY_SETTING = "kassa.partner_cash_counterparty_id"

# Категория изъятия из смены, означающая «партнёр забрал наличные». Проставляется синком смен
# по счёту-назначению iiko (см. ``iiko_cashshift_sync.ALISA_ACCOUNT_ID``).
PARTNER_PAYOUT_CATEGORY = "alisa"


def _money(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class PartnerCashBalance:
    as_of: date
    taken: Decimal  # забрал из смен
    returned: Decimal  # вернул внесениями
    counterparty_id: uuid.UUID | None

    @property
    def outstanding(self) -> Decimal:
        """Долг не уходит в минус: переплата партнёра — это уже не наша дебиторка."""
        return max(_money(self.taken - self.returned), _ZERO)

    @property
    def repayments_linked(self) -> bool:
        """Есть ли вообще чем гасить. Нет — значит показанный долг ЗАВЫШЕН на возвраты."""
        return self.counterparty_id is not None


async def _partner_counterparty_id(session: AsyncSession) -> uuid.UUID | None:
    raw = await session.scalar(
        select(AppSetting.value).where(AppSetting.key == PARTNER_COUNTERPARTY_SETTING)
    )
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw).strip('"'))
    except (ValueError, AttributeError):
        return None


async def build_partner_cash_balance(session: AsyncSession, *, as_of: date) -> PartnerCashBalance:
    """Сколько партнёр должен на дату: забрал из смен минус вернул в кассу."""
    # Дата изъятия — день ЗАКРЫТИЯ смены по Москве: изъятие оформляется внутри смены, а сама
    # смена может открыться вечером и закрыться за полночь. Открытие взято фолбэком для смен,
    # которые ещё не закрыты.
    shift_day = func.date(
        func.timezone(
            str(MOSCOW_TZ), func.coalesce(IikoCashShift.close_date, IikoCashShift.open_date)
        )
    )
    taken = await session.scalar(
        select(func.coalesce(func.sum(IikoCashShiftPayout.amount), 0))
        .join(IikoCashShift, IikoCashShift.id == IikoCashShiftPayout.shift_id)
        .where(
            IikoCashShiftPayout.category == PARTNER_PAYOUT_CATEGORY,
            shift_day <= as_of,
        )
    )

    counterparty_id = await _partner_counterparty_id(session)
    returned = _ZERO
    if counterparty_id is not None:
        returned = _money(
            await session.scalar(
                select(func.coalesce(func.sum(CashflowTransaction.amount), 0)).where(
                    CashflowTransaction.counterparty_id == counterparty_id,
                    CashflowTransaction.direction == "in",
                    CashflowTransaction.operation_date <= as_of,
                    CashflowTransaction.quality_status != EXCLUDED_QUALITY,
                )
            )
        )

    return PartnerCashBalance(
        as_of=as_of,
        taken=_money(taken),
        returned=returned,
        counterparty_id=counterparty_id,
    )


__all__ = [
    "PARTNER_COUNTERPARTY_SETTING",
    "PARTNER_PAYOUT_CATEGORY",
    "PartnerCashBalance",
    "build_partner_cash_balance",
]

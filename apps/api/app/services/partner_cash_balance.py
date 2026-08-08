"""Долг партнёра («В гостях у Алисы») по наличным из смены — на произвольную дату.

ЧТО ЗДЕСЬ ЗА ОТНОШЕНИЯ. Партнёр продаёт нашу еду у себя, выручка пробивается через нашу iiko, и
наличную её часть партнёр забирает прямо из смены: в iiko это изъятие на счёт «Алиса наличные»,
проводки ДДС оно намеренно не порождает — деньги ушли не в расход, а в дебиторку. Из взятого
партнёру принадлежит его комиссия (20 % выручки, ставка живёт в ``pnl_partner_commission_rule``),
остальное он возвращает внесением в кассу.

    долг = взято наличными − заработанная комиссия − возвращено

ТРИ ИСТОЧНИКА, И НИ ОДИН НЕ ЗАМЕНЯЕТ ДРУГОЙ. Взятое — журнал смен iiko; комиссия — регистр
партнёрских комиссий ОПиУ; возвращённое — проводки ДДС с контрагентом партнёра. Считать комиссию
самому «выручка × 20 %» значило бы завести вторую формулу того же и однажды разойтись с ОПиУ,
где та же комиссия уже сидит в расходах.

ГАШЕНИЕ — СТРОГО ПО КОНТРАГЕНТУ. Статья «Поступление денег с торг. точек» несёт весь поток
инкассаций с точек (на копии прода 452 проводки), и правило «минус всё по статье» обнулило бы
долг партнёра при первой же сдаче выручки. Контрагент приходит из пресета внесения; кто именно
считается партнёром — объявлено настройкой, потому что это договорённость бизнеса, а не
константа программы.

КОМИССИЯ БЕРЁТСЯ ТОЛЬКО ЗА ЗАВЕРШЁННЫЕ МЕСЯЦЫ. Регистр хранит выручку помесячно, разбивки внутри
месяца нет. Размазать её по дням — выдумка (выручка по дням неровная), поэтому на срезе внутри
месяца комиссия текущего месяца не вычитается, а строка честно помечается неполной: долг тогда
ЗАВЫШЕН на невычтенную комиссию. Врать точной цифрой хуже, чем назвать её неполноту.

СЧЁТ ИДЁТ С НАЧАЛА УЧЁТА. Изъятия до 01.07.2026 в долг не входят (решение владельца 08.08.2026):
июньские 45 947 ₽ относятся к периоду, которого в учёте нет, и тянуть их в первый баланс значило
бы смешать две разные эпохи.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AppSetting,
    CashflowTransaction,
    IikoCashShift,
    IikoCashShiftPayout,
    PnlPartnerCommissionFact,
)
from app.services.accounting_periods import ACCOUNTING_START
from app.services.banking.cashflow_classify import EXCLUDED_QUALITY
from app.services.kassa.iiko_cashshift_sync import MOSCOW_TZ

_ZERO = Decimal("0.00")
_ONE_DAY = timedelta(days=1)

# Кого считать партнёром. Значение — идентификатор контрагента строкой.
PARTNER_COUNTERPARTY_SETTING = "kassa.partner_cash_counterparty_id"

# Категория изъятия из смены, означающая «партнёр забрал наличные». Проставляется синком смен
# по счёту-назначению iiko (см. ``iiko_cashshift_sync.ALISA_ACCOUNT_ID``).
PARTNER_PAYOUT_CATEGORY = "alisa"

# Ключ партнёра в регистре комиссий ОПиУ (``pnl_partner_commission_rule.partner_key``).
PARTNER_COMMISSION_KEY = "в гостях у алисы"


def _money(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _last_complete_month(as_of: date) -> date | None:
    """Первое число последнего месяца, ЦЕЛИКОМ уместившегося в срез.

    Срез 31.07 закрывает июль, срез 15.08 — только июль: августовской выручки ещё нет.
    """
    month = _month_start(as_of)
    ends_on_month_end = (as_of + _ONE_DAY).month != as_of.month
    if ends_on_month_end:
        return month
    previous_end = month - _ONE_DAY
    return None if previous_end < ACCOUNTING_START else _month_start(previous_end)


@dataclass(frozen=True)
class PartnerCashBalance:
    as_of: date
    taken: Decimal  # забрал наличными из смен
    commission: Decimal  # заработал за завершённые месяцы
    returned: Decimal  # вернул внесениями
    counterparty_id: uuid.UUID | None
    # Месяцы, где изъятия есть, а факта комиссии нет: долг завышен на неизвестную величину.
    months_without_commission: list[date] = field(default_factory=list)
    # Срез пришёлся на середину месяца — комиссия текущего месяца не вычтена.
    open_month: bool = False

    @property
    def outstanding(self) -> Decimal:
        """Долг не уходит в минус: переплата партнёра — это уже не наша дебиторка."""
        return max(_money(self.taken - self.commission - self.returned), _ZERO)

    @property
    def repayments_linked(self) -> bool:
        """Есть ли вообще чем гасить. Нет — значит показанный долг ЗАВЫШЕН на возвраты."""
        return self.counterparty_id is not None

    @property
    def complete(self) -> bool:
        """Все три слагаемых известны — цифре можно верить."""
        return self.repayments_linked and not self.open_month and not self.months_without_commission


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


def _shift_day():
    """День смены по Москве: изъятие оформляется внутри смены, а смена закрывается за полночь."""
    return func.date(
        func.timezone(
            str(MOSCOW_TZ), func.coalesce(IikoCashShift.close_date, IikoCashShift.open_date)
        )
    )


async def build_partner_cash_balance(session: AsyncSession, *, as_of: date) -> PartnerCashBalance:
    """Сколько партнёр должен на дату: взял наличными − его комиссия − вернул."""
    shift_day = _shift_day()
    taken = _money(
        await session.scalar(
            select(func.coalesce(func.sum(IikoCashShiftPayout.amount), 0))
            .join(IikoCashShift, IikoCashShift.id == IikoCashShiftPayout.shift_id)
            .where(
                IikoCashShiftPayout.category == PARTNER_PAYOUT_CATEGORY,
                shift_day >= ACCOUNTING_START,
                shift_day <= as_of,
            )
        )
    )

    last_month = _last_complete_month(as_of)
    commission = _ZERO
    covered: set[date] = set()
    if last_month is not None:
        rows = (
            await session.execute(
                select(
                    PnlPartnerCommissionFact.period_month,
                    PnlPartnerCommissionFact.commission_amount,
                ).where(
                    PnlPartnerCommissionFact.partner_key == PARTNER_COMMISSION_KEY,
                    PnlPartnerCommissionFact.period_month >= ACCOUNTING_START,
                    PnlPartnerCommissionFact.period_month <= last_month,
                )
            )
        ).all()
        commission = _money(sum((row[1] for row in rows), _ZERO))
        covered = {row[0] for row in rows}

    # Месяцы, где партнёр наличные брал, а комиссия за месяц не посчитана: вычесть нечего, и
    # долг на эту величину завышен. Молчать нельзя — цифра выглядит точной.
    months_taken = {
        row[0]
        for row in (
            await session.execute(
                select(func.distinct(func.date_trunc("month", shift_day)))
                .select_from(IikoCashShiftPayout)
                .join(IikoCashShift, IikoCashShift.id == IikoCashShiftPayout.shift_id)
                .where(
                    IikoCashShiftPayout.category == PARTNER_PAYOUT_CATEGORY,
                    shift_day >= ACCOUNTING_START,
                    shift_day <= as_of,
                )
            )
        ).all()
    }
    months_taken = {value.date() if hasattr(value, "date") else value for value in months_taken}
    missing = sorted(
        month
        for month in months_taken
        if last_month is not None and month <= last_month and month not in covered
    )

    counterparty_id = await _partner_counterparty_id(session)
    returned = _ZERO
    if counterparty_id is not None:
        returned = _money(
            await session.scalar(
                select(func.coalesce(func.sum(CashflowTransaction.amount), 0)).where(
                    CashflowTransaction.counterparty_id == counterparty_id,
                    CashflowTransaction.direction == "in",
                    CashflowTransaction.operation_date >= ACCOUNTING_START,
                    CashflowTransaction.operation_date <= as_of,
                    CashflowTransaction.quality_status != EXCLUDED_QUALITY,
                )
            )
        )

    return PartnerCashBalance(
        as_of=as_of,
        taken=taken,
        commission=commission,
        returned=returned,
        counterparty_id=counterparty_id,
        months_without_commission=missing,
        open_month=_month_start(as_of) not in covered and _month_start(as_of) in months_taken,
    )


__all__ = [
    "PARTNER_COMMISSION_KEY",
    "PARTNER_COUNTERPARTY_SETTING",
    "PARTNER_PAYOUT_CATEGORY",
    "PartnerCashBalance",
    "build_partner_cash_balance",
]

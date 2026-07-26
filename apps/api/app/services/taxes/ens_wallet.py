"""Расчётный ЕНС-кошелёк — дебиторка бюджета без API ФНС.

Решение владельца 26.07.2026: переплату на ЕНС не вводим руками (протухает), а СЧИТАЕМ —
обе половины формулы система знает сама:

    сальдо = всё, что реально ушло в ФНС (факты из выписки)
           − всё, что налоговая забрала (наши признанные начисления)

Сценарий владельца, ради которого это строится: переплатили 20 000 → сальдо выросло само;
бухгалтер прислала платёжки на 15 000 меньше начислений (тратит переплату) → мы платим
меньше → сальдо тает само. Прислала копейка в копейку — сальдо не меняется.

Правила признания начислений («сколько забрала налоговая») — наша управленческая
методология, не витрина личного кабинета ФНС (там свои резервы и формальные даты):

* **УСН** — аванс закрытого периода признаётся, когда наступил срок уплаты; при ранней
  уплате — в момент уплаты (уплаченный заранее аванс не «переплата», он уже потреблён);
* **допвзнос 1 % и фиксированные взносы ИП** — кассово, по мере уплаты, но не больше
  начисленного (сроки у них далеко: 01.07 следующего года и 28.12); переплата возникает
  только СВЕРХ начисленного;
* **взносы за работников и НДФЛ** — начисления из разноса оборотки (``source_kind=
  'tax_notice'``), признаются по сроку уплаты. Помесячный шум «окон» НДФЛ (платится по
  датам выплат, а не по месяцу) даёт колебание в пределах пары тысяч — за квартал сходится;
* **травматизм** — МИМО кошелька: он платится в СФР отдельной платёжкой, не через ЕНС;
* **доисторический хвост** — зарплатные платежи до первого месяца, покрытого обороткой
  (за январь–февраль оборотки нет), считаем взаимно закрытыми: начислений по ним нет,
  и судить о переплате нечем.

Старт кошелька — 1 января года среза, с нуля (решение владельца). Отрицательное сальдо
наружу не отдаём: недоплата — это просрочка, она живёт в строке задолженности (иначе один
и тот же долг посчитается дважды). Публикуем ``balance = max(0, raw)`` и обе половины
формулы — приход и признанное — чтобы цифра объяснялась сама.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tax import TaxPayment
from app.services.taxes.engine import (
    PERIOD_SEQ,
    compute_tax_state,
    period_end_date,
)
from app.services.taxes.repository import load_tax_inputs, year_config

ZERO = Decimal("0")

# Виды, деньги по которым проходят через ЕНС. Травматизм платится в СФР напрямую;
# пени и «прочее» нейтральны для кошелька (начислений по ним мы не знаем — уплата
# признаётся равной списанию и сальдо не двигает, поэтому просто не включаем).
# ``enp_payroll`` в ``tax_payment`` существовать не может (CHECK ``ck_tax_payment_kind``):
# факт-контур выписки обязан разносить платёж ЕНП по видам.
ENS_KINDS: tuple[str, ...] = (
    "usn_advance",
    "ndfl",
    "contrib_employees",
    "contrib_fixed",
    "contrib_extra_1pct",
)

_PAYROLL_KINDS: tuple[str, ...] = ("ndfl", "contrib_employees")


def usn_due_date(year: int, period_code: str) -> date:
    """Срок уплаты аванса/налога УСН для ИП: 28-е число месяца после периода."""
    if period_code == "q1":
        return date(year, 4, 28)
    if period_code == "h1":
        return date(year, 7, 28)
    if period_code == "9m":
        return date(year, 10, 28)
    return date(year + 1, 4, 28)  # годовой налог ИП — до 28 апреля следующего года


@dataclass(frozen=True)
class EnsWallet:
    """Расчётное сальдо ЕНС: приход, признанные начисления и переплата."""

    as_of: date
    inflow: Decimal  # факты уплаты через ЕНС с 1 января
    recognized: Decimal  # начисления, признанные «списанными» налоговой
    balance: Decimal  # переплата: max(0, inflow − recognized)
    # Отрицательный сырой остаток означает «фактов меньше начислений» — недоимка или
    # дыра факт-слоя. Наружу как долг не отдаём (долг живёт в обязательствах), но
    # честно сообщаем для пояснения в интерфейсе.
    shortfall: Decimal  # max(0, recognized − inflow)


async def _bank_facts_sum(
    session: AsyncSession,
    *,
    kinds: tuple[str, ...],
    start: date,
    end: date,
    paid_before: date | None = None,
) -> Decimal:
    """Сумма банковских/ручных фактов уплаты по видам за окно дат."""
    stmt = select(func.coalesce(func.sum(TaxPayment.amount), 0)).where(
        TaxPayment.status == "paid",
        TaxPayment.kind.in_(kinds),
        TaxPayment.source_kind != "tax_notice",
        TaxPayment.paid_on >= start,
        TaxPayment.paid_on <= end,
    )
    if paid_before is not None:
        stmt = stmt.where(TaxPayment.paid_on < paid_before)
    return Decimal(str(await session.scalar(stmt) or 0))


async def _payroll_accrual_rows(
    session: AsyncSession, *, year: int
) -> list[tuple[date, Decimal]]:
    """Зарплатные начисления из разноса оборотки: (срок уплаты, сумма)."""
    rows = (
        await session.execute(
            select(TaxPayment.paid_on, TaxPayment.amount).where(
                TaxPayment.status == "paid",
                TaxPayment.source_kind == "tax_notice",
                TaxPayment.kind.in_(("contrib_employees", "ndfl")),
                TaxPayment.for_year == year,
            )
        )
    ).all()
    return [(paid_on, Decimal(str(amount))) for paid_on, amount in rows]


async def compute_ens_wallet(session: AsyncSession, *, as_of: date) -> EnsWallet:
    """Посчитать расчётное сальдо ЕНС на дату среза. Старт — 1 января, с нуля."""
    year = as_of.year
    cfg = year_config(year)
    jan1 = date(year, 1, 1)

    # ── Признанные начисления ────────────────────────────────────────────────
    recognized = ZERO

    # УСН считается НАРАСТАЮЩИМ ИТОГОМ, поэтому и признание кумулятивное: суммировать
    # ``amount_due`` по периодам нельзя — он уже за вычетом уплаченных авансов, и переплата
    # первого квартала «съела» бы начисление второго. Чистое начисление периода —
    # ``tax_computed − deduction_applied`` на конец периода. Признаём по сроку уплаты;
    # раннюю уплату потребляем сразу (в пределах начисленного по закрытым периодам).
    assessed_by_due = ZERO
    assessed_latest_closed = ZERO
    for period_code in PERIOD_SEQ:
        p_end = period_end_date(year, period_code)
        if p_end > as_of:
            break
        state = compute_tax_state(await load_tax_inputs(session, as_of=p_end))
        assessed = max(state.tax_computed - state.deduction_applied, ZERO)
        assessed_latest_closed = assessed
        if usn_due_date(year, period_code) <= as_of:
            assessed_by_due = assessed
    usn_facts = await _bank_facts_sum(
        session, kinds=("usn_advance",), start=jan1, end=as_of
    )
    recognized += max(assessed_by_due, min(usn_facts, assessed_latest_closed))

    # Допвзнос 1 % и фиксированные взносы: кассово, но не больше начисленного.
    state_now = compute_tax_state(await load_tax_inputs(session, as_of=as_of))
    extra_facts = await _bank_facts_sum(
        session, kinds=("contrib_extra_1pct",), start=jan1, end=as_of
    )
    if as_of >= date(year + 1, 7, 1):
        recognized += state_now.extra_accrued
    else:
        recognized += min(extra_facts, state_now.extra_accrued)

    fixed_facts = await _bank_facts_sum(
        session, kinds=("contrib_fixed",), start=jan1, end=as_of
    )
    if as_of >= date(year, 12, 28):
        recognized += cfg.fixed_contribution
    else:
        recognized += min(fixed_facts, cfg.fixed_contribution)

    # Зарплатные начисления из разноса оборотки — по сроку уплаты.
    payroll_rows = await _payroll_accrual_rows(session, year=year)
    first_due: date | None = min((d for d, _ in payroll_rows), default=None)
    recognized += sum((a for d, a in payroll_rows if d <= as_of), ZERO)

    # Доисторический хвост: зарплатные платежи до срока первого месяца оборотки —
    # начислений по ним нет, считаем взаимно закрытыми (признаём ровно на сумму факта).
    if first_due is not None:
        prehistoric = await _bank_facts_sum(
            session,
            kinds=_PAYROLL_KINDS,
            start=jan1,
            end=as_of,
            paid_before=first_due,
        )
        recognized += prehistoric

    # ── Приход: все факты уплаты через ЕНС с 1 января ────────────────────────
    inflow = await _bank_facts_sum(session, kinds=ENS_KINDS, start=jan1, end=as_of)

    raw = inflow - recognized
    return EnsWallet(
        as_of=as_of,
        inflow=inflow,
        recognized=recognized,
        balance=max(ZERO, raw),
        shortfall=max(ZERO, -raw),
    )

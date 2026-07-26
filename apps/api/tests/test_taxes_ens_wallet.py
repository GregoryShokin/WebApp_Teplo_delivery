"""Расчётный ЕНС-кошелёк: переплата в бюджет считается, а не вводится руками.

Решение владельца 26.07.2026: сальдо = факты уплаты через ЕНС − признанные начисления.
Переплатили — сальдо выросло само; бухгалтер прислала платёжки меньше начислений
(тратит переплату) — мы платим меньше, сальдо тает само. Старт — 1 января, с нуля.
"""

from __future__ import annotations

import calendar
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.tax import IikoRevenuePeriod, TaxPayment
from app.services.taxes.ens_wallet import compute_ens_wallet, usn_due_date
from app.services.taxes.repository import DEFAULT_DEPARTMENT


def _paid(
    kind: str,
    amount: str,
    paid_on: date,
    *,
    period: str | None = None,
    source: str = "bank_statement",
) -> TaxPayment:
    return TaxPayment(
        id=uuid.uuid4(),
        bundle_id=uuid.uuid4(),
        paid_on=paid_on,
        kind=kind,
        amount=Decimal(amount),
        recipient="sfr" if kind == "contrib_injury" else "fns",
        for_year=2026,
        for_period=period,
        status="paid",
        source_kind=source,
        quality_status="confirmed",
    )


async def _seed_revenue(session: AsyncSession, months: dict[int, str]) -> None:
    for m, revenue in months.items():
        session.add(
            IikoRevenuePeriod(
                id=uuid.uuid4(),
                period_start=date(2026, m, 1),
                period_end=date(2026, m, calendar.monthrange(2026, m)[1]),
                granularity="month",
                department=DEFAULT_DEPARTMENT,
                revenue_net=Decimal(revenue),
                source="iiko_olap",
            )
        )
    await session.flush()


async def test_overpayment_grows_and_melts_by_owner_scenario(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Сценарий владельца: переплата 20 000 появляется сама и тает, когда её тратят.

    Выручка Q1 = 2 000 000 → УСН q1 = 120 000 (вычетов нет). Уплатили 140 000 —
    после срока 28.04 кошелёк показывает переплату 20 000. Во II квартале выручка
    ещё 1 000 000 → доплата за h1 = 60 000, бухгалтер шлёт платёжку на 40 000
    (тратит переплату) — мы платим 40 000, и после срока 28.07 переплата тает до 0.
    """
    async with async_session_factory() as session:
        await _seed_revenue(session, {1: "1000000", 2: "500000", 3: "500000"})
        session.add(_paid("usn_advance", "140000", date(2026, 4, 27), period="q1"))
        await session.commit()

        # После срока q1: начислено 120 000, уплачено 140 000 → переплата 20 000.
        wallet = await compute_ens_wallet(session, as_of=date(2026, 5, 1))
        assert wallet.balance == Decimal("20000.00")
        assert wallet.shortfall == Decimal("0")

        # II квартал: ещё 1 000 000 выручки, платим на 20 000 меньше начисленного.
        await _seed_revenue(session, {4: "400000", 5: "300000", 6: "300000"})
        session.add(_paid("usn_advance", "40000", date(2026, 7, 27), period="h1"))
        await session.commit()

        wallet = await compute_ens_wallet(session, as_of=date(2026, 7, 29))

    # Начислено за год 180 000, уплачено 180 000 — переплата потрачена целиком.
    assert wallet.balance == Decimal("0")
    assert wallet.shortfall == Decimal("0")


async def test_exact_payments_keep_overpayment_intact(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Платёжки копейка в копейку по начислениям — переплата остаётся нетронутой."""
    async with async_session_factory() as session:
        await _seed_revenue(session, {1: "1000000", 2: "500000", 3: "500000"})
        # Переплата 20 000 сверх начисленных 120 000 за q1…
        session.add(_paid("usn_advance", "140000", date(2026, 4, 27), period="q1"))
        await _seed_revenue(session, {4: "400000", 5: "300000", 6: "300000"})
        # …а за II квартал платим ровно начисленное (60 000).
        session.add(_paid("usn_advance", "60000", date(2026, 7, 27), period="h1"))
        await session.commit()

        wallet = await compute_ens_wallet(session, as_of=date(2026, 7, 29))

    assert wallet.balance == Decimal("20000.00")


async def test_underpayment_is_not_negative_balance(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Фактов меньше начислений — кошелёк не уходит в минус: недоимка живёт в долгах.

    Иначе один и тот же долг посчитался бы дважды: минусом кошелька и строкой
    задолженности.
    """
    async with async_session_factory() as session:
        await _seed_revenue(session, {1: "1000000", 2: "500000", 3: "500000"})
        session.add(_paid("usn_advance", "100000", date(2026, 4, 27), period="q1"))
        await session.commit()

        wallet = await compute_ens_wallet(session, as_of=date(2026, 5, 1))

    assert wallet.balance == Decimal("0")
    assert wallet.shortfall == Decimal("20000.00")


async def test_early_usn_payment_is_consumed_not_overpayment(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Аванс, уплаченный ДО срока, потреблён начислением — это не переплата."""
    async with async_session_factory() as session:
        await _seed_revenue(session, {1: "1000000", 2: "500000", 3: "500000"})
        session.add(_paid("usn_advance", "120000", date(2026, 4, 10), period="q1"))
        await session.commit()

        # 15 апреля: срок (28.04) ещё не наступил, но деньги уже ушли.
        wallet = await compute_ens_wallet(session, as_of=date(2026, 4, 15))

    assert wallet.balance == Decimal("0")
    assert wallet.shortfall == Decimal("0")


async def test_injury_and_notice_rows_stay_out_of_wallet(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Травматизм (в СФР, мимо ЕНС) и разнос-начисления (tax_notice) — не приход кошелька."""
    async with async_session_factory() as session:
        session.add(_paid("contrib_injury", "100", date(2026, 7, 10)))
        session.add(
            _paid(
                "contrib_employees",
                "13595.93",
                date(2026, 8, 28),
                period="2026-07",
                source="tax_notice",
            )
        )
        await session.commit()

        wallet = await compute_ens_wallet(session, as_of=date(2026, 7, 20))

    assert wallet.inflow == Decimal("0")
    assert wallet.balance == Decimal("0")


async def test_self_contributions_consumed_up_to_accrual(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Взносы «за себя» потребляются кассово, но не больше начисленного.

    Фиксированные: годовая сумма 57 390 — уплата 14 347,50 потреблена целиком.
    Допвзнос 1%: начислено (выручка 1 300 000 − 300 000) × 1% = 10 000 — уплата
    12 000 даёт переплату ровно 2 000 (сверх начисленного). УСН при этом уплачен
    ровно по начислению (78 000 − вычет 14 347,50), иначе единый котёл законно
    съест переплату в счёт недоимки по УСН.
    """
    async with async_session_factory() as session:
        await _seed_revenue(session, {1: "800000", 2: "500000"})
        session.add(_paid("contrib_fixed", "14347.50", date(2026, 1, 21)))
        session.add(_paid("contrib_extra_1pct", "12000", date(2026, 6, 9)))
        session.add(_paid("usn_advance", "63652.50", date(2026, 4, 27), period="q1"))
        await session.commit()

        wallet = await compute_ens_wallet(session, as_of=date(2026, 6, 15))

    assert wallet.balance == Decimal("2000.00")
    assert wallet.shortfall == Decimal("0")


async def test_prehistoric_payroll_facts_are_mutually_settled(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Зарплатные платежи до первого месяца оборотки взаимно закрыты, не переплата.

    Оборотки за январь–февраль нет — начисления начинаются с марта (срок 28.04).
    Платёж января за янв–фев не должен показываться переплатой: судить не по чему.
    """
    async with async_session_factory() as session:
        # Факт до первой оборотки: взносы за янв–фев.
        session.add(_paid("contrib_employees", "26891.50", date(2026, 3, 26)))
        # Разнос оборотки марта: начисления со сроком 28.04.
        session.add(
            _paid(
                "contrib_employees",
                "13595.93",
                date(2026, 4, 28),
                period="2026-03",
                source="tax_notice",
            )
        )
        session.add(
            _paid(
                "ndfl", "6500", date(2026, 4, 28), period="2026-03", source="tax_notice"
            )
        )
        # Факт уплаты мартовского ЕНП — день в день со сроком, разнесён по видам
        # (kind='enp_payroll' в tax_payment запрещён CHECK-констрейнтом).
        session.add(_paid("contrib_employees", "13595.93", date(2026, 4, 28)))
        session.add(_paid("ndfl", "6500", date(2026, 4, 28)))
        await session.commit()

        wallet = await compute_ens_wallet(session, as_of=date(2026, 5, 1))

    # 26 891,50 признано «доисторией» (факт=начисление), март закрыт день в день.
    assert wallet.balance == Decimal("0")
    assert wallet.shortfall == Decimal("0")


def test_usn_due_dates() -> None:
    """Сроки уплаты УСН для ИП: 28-е после квартала, годовой — 28 апреля следующего года."""
    assert usn_due_date(2026, "q1") == date(2026, 4, 28)
    assert usn_due_date(2026, "h1") == date(2026, 7, 28)
    assert usn_due_date(2026, "9m") == date(2026, 10, 28)
    assert usn_due_date(2026, "year") == date(2027, 4, 28)

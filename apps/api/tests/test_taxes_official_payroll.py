"""Прогнозные начисления официального контура (решение владельца 26.07.2026).

Сотрудник отработал месяц → долг перед бюджетом появился, не дожидаясь оборотки.
Формулы сверены с обороткой бухгалтера копейка в копейку (июль 2026, оклад 50 000):
НДФЛ (50 000 − 1 400) × 13% = 6 318; взносы МСП 40 639,50 × 30% + 9 360,50 × 15% =
13 595,93; травматизм 100,00. Фикс-взнос ИП — годовое обязательство целиком.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.employee import Employee
from app.models.tax import TaxPayment, TaxPayrollLedger
from app.services.taxes.engine import YEAR_CONFIGS
from app.services.taxes.obligations import list_payable_obligations
from app.services.taxes.official_payroll import (
    child_deduction_monthly,
    fixed_contribution_remainder,
    month_contributions,
    month_ndfl,
    months_without_turnover,
    official_month_accrual,
)

CFG = YEAR_CONFIGS[2026]


def _official_employee(
    *,
    salary: str = "50000",
    children: int = 1,
    single_parent: bool = False,
    status: str = "working",
    hire: date | None = date(2026, 5, 18),
    fire: date | None = None,
) -> Employee:
    return Employee(
        id=uuid.uuid4(),
        full_name="Victoria Manager",
        iiko_id=f"iiko-{uuid.uuid4()}",
        is_official=True,
        official_full_name="Водолазова Виктория Сергеевна",
        official_tab_number="206",
        official_salary=Decimal(salary),
        official_children_count=children,
        official_single_parent=single_parent,
        official_status=status,
        hire_date=hire,
        fire_date=fire,
    )


def _paid(kind: str, amount: str, paid_on: date, period: str | None = None) -> TaxPayment:
    return TaxPayment(
        id=uuid.uuid4(),
        bundle_id=uuid.uuid4(),
        paid_on=paid_on,
        kind=kind,
        amount=Decimal(amount),
        recipient="fns",
        for_year=2026,
        for_period=period,
        status="paid",
        source_kind="bank_statement",
        quality_status="confirmed",
    )


# ── математика месяца (золотые числа оборотки) ───────────────────────────────


def test_month_math_matches_accountant_turnover() -> None:
    assert month_contributions(Decimal("50000"), CFG) == Decimal("13595.93")
    assert month_ndfl(Decimal("50000"), Decimal("1400"), CFG) == Decimal("6318")
    # Под порогом 1,5 МРОТ — чистые 30% (июнь: начислено 28 571 → 8 571,30).
    assert month_contributions(Decimal("28571"), CFG) == Decimal("8571.30")


def test_child_deduction_sums_per_child() -> None:
    """Вычеты СУММИРУЮТСЯ по детям (не «на семью»): 2 детей → 4 200, 3 → 10 200."""
    assert child_deduction_monthly(0, False, CFG) == Decimal("0")
    assert child_deduction_monthly(1, False, CFG) == Decimal("1400.00")
    assert child_deduction_monthly(2, False, CFG) == Decimal("4200.00")
    assert child_deduction_monthly(3, False, CFG) == Decimal("10200.00")
    assert child_deduction_monthly(4, False, CFG) == Decimal("16200.00")
    # Единственный родитель — в двойном размере.
    assert child_deduction_monthly(1, True, CFG) == Decimal("2800.00")


async def test_deduction_stops_over_income_limit(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Доход свыше 450 000 нарастающим итогом — вычет с этого месяца не применяется.

    Оклад 50 000, работает с января: сентябрь — доход ровно 450 000 (вычет ещё есть,
    НДФЛ 6 318), октябрь — 500 000 (вычета нет, НДФЛ 6 500).
    """
    async with async_session_factory() as session:
        session.add(_official_employee(hire=date(2025, 1, 1)))
        await session.commit()

        september = await official_month_accrual(session, year=2026, month=9, cfg=CFG)
        october = await official_month_accrual(session, year=2026, month=10, cfg=CFG)

    assert september is not None and september.ndfl == Decimal("6318")
    assert october is not None and october.ndfl == Decimal("6500")


# ── прогнозное начисление месяца ─────────────────────────────────────────────


async def test_accrual_full_month_only(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Полный отработанный месяц считается; месяц приёма (неполный) — ждёт оборотку."""
    async with async_session_factory() as session:
        session.add(_official_employee())  # принята 18.05
        await session.commit()

        may = await official_month_accrual(session, year=2026, month=5, cfg=CFG)
        june = await official_month_accrual(session, year=2026, month=6, cfg=CFG)

    assert may is None  # неполный месяц — не гадаем
    assert june is not None
    assert june.ndfl == Decimal("6318")
    assert june.contributions == Decimal("13595.93")
    assert june.injury == Decimal("100.00")
    assert june.enp_total == Decimal("19913.93")


async def test_maternity_leave_excluded(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        session.add(_official_employee(status="maternity_leave", hire=date(2025, 1, 1)))
        await session.commit()

        accrual = await official_month_accrual(session, year=2026, month=6, cfg=CFG)

    assert accrual is None  # декрет — начислений нет


async def test_fired_employee_stops_accruing(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Уволенный сотрудник не плодит начислений за месяцы после увольнения."""
    async with async_session_factory() as session:
        session.add(
            _official_employee(hire=date(2025, 1, 1), fire=date(2026, 6, 15))
        )
        await session.commit()

        may = await official_month_accrual(session, year=2026, month=5, cfg=CFG)
        june = await official_month_accrual(session, year=2026, month=6, cfg=CFG)
        july = await official_month_accrual(session, year=2026, month=7, cfg=CFG)

    assert may is not None  # последний полный месяц
    assert june is None  # месяц увольнения неполный — по документам
    assert july is None


# ── дополнение к оборотке ────────────────────────────────────────────────────


async def test_months_without_turnover_is_complement(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        session.add(
            TaxPayrollLedger(
                id=uuid.uuid4(),
                year=2026,
                month=6,
                tab_number="206",
                employee="ВОДОЛАЗОВА В.С.",
                contributions=Decimal("8571.30"),
                ndfl=Decimal("3532.00"),
            )
        )
        await session.commit()

        months = await months_without_turnover(
            session, year=2026, today=date(2026, 8, 10)
        )

    assert 6 not in months  # оборотка есть — месяц покрыт
    assert 7 in months  # завершился, оборотки нет — прогноз
    assert 8 not in months  # идущий месяц не отработан


# ── фикс-взнос ИП ────────────────────────────────────────────────────────────


async def test_fixed_remainder_shrinks_with_payments(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Годовая сумма известна заранее; уплаты и открытые плановые её уменьшают."""
    async with async_session_factory() as session:
        session.add(_paid("contrib_fixed", "14347.50", date(2026, 3, 27), "q1"))
        await session.commit()

        remainder = await fixed_contribution_remainder(session, year=2026, cfg=CFG)

    assert remainder == Decimal("57390.00") - Decimal("14347.50")


# ── интеграция с обязательствами ─────────────────────────────────────────────


async def test_projection_appears_in_obligations(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Июнь без оборотки: прогнозные ЕНП и травматизм + годовой остаток фикса ИП."""
    async with async_session_factory() as session:
        session.add(_official_employee())
        await session.commit()

        obligations = await list_payable_obligations(session, today=date(2026, 7, 15))

    by_kind = {(o.kind, o.for_period): o for o in obligations}
    enp = by_kind[("enp_payroll", "2026-06")]
    assert enp.is_projection
    assert enp.amount == Decimal("19913.93")
    assert enp.due_date == date(2026, 7, 28)
    injury = by_kind[("contrib_injury", "2026-06")]
    assert injury.is_projection
    assert injury.amount == Decimal("100.00")
    assert injury.due_date == date(2026, 7, 15)
    fixed = by_kind[("contrib_fixed", "year")]
    assert fixed.is_projection
    assert fixed.amount == Decimal("57390.00")
    assert fixed.due_date == date(2026, 12, 28)


async def test_projection_replaced_by_turnover(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Пришла оборотка месяца — прогноз замолкает, работает строка сверки."""
    async with async_session_factory() as session:
        session.add(_official_employee())
        session.add(
            TaxPayrollLedger(
                id=uuid.uuid4(),
                year=2026,
                month=6,
                tab_number="206",
                employee="ВОДОЛАЗОВА В.С.",
                contributions=Decimal("8571.30"),
                ndfl=Decimal("3532.00"),
            )
        )
        await session.commit()

        obligations = await list_payable_obligations(session, today=date(2026, 7, 15))

    enp = [o for o in obligations if o.kind == "enp_payroll"]
    assert len(enp) == 1
    assert not enp[0].is_projection  # строка сверки, не прогноз
    assert str(enp[0].amount) == "12103.30"  # фактическое начисление оборотки


async def test_projection_settled_by_paid_facts(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Разнесённый факт уплаты гасит прогнозный ЕНП месяца (исторический бэкфилл)."""
    async with async_session_factory() as session:
        session.add(_official_employee())
        session.add(_paid("contrib_employees", "13595.93", date(2026, 7, 28), "2026-06"))
        await session.commit()

        obligations = await list_payable_obligations(session, today=date(2026, 7, 15))

    assert ("enp_payroll", "2026-06") not in {
        (o.kind, o.for_period) for o in obligations
    }


async def test_projection_injury_covered_by_document(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Платёжка травматизма со сроком в окне месяца N+1 гасит прогноз месяца N."""
    async with async_session_factory() as session:
        session.add(_official_employee())
        session.add(
            TaxPayment(
                id=uuid.uuid4(),
                bundle_id=uuid.uuid4(),
                paid_on=date(2026, 7, 10),
                kind="contrib_injury",
                amount=Decimal("57.14"),
                recipient="sfr",
                for_year=2026,
                for_period="year",
                status="paid",
                source_kind="bank_statement",
                quality_status="confirmed",
            )
        )
        await session.commit()

        obligations = await list_payable_obligations(session, today=date(2026, 7, 15))

    assert ("contrib_injury", "2026-06") not in {
        (o.kind, o.for_period) for o in obligations
    }

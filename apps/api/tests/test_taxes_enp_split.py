"""Разнос зарплатного ЕНП из оборотки на НДФЛ и взносы за работников.

Главное, что проверяем: взносы за работников из оборотки становятся вычетом по УСН,
а НДФЛ — нет; разнос идемпотентен и агрегирует нескольких сотрудников за месяц.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.tax import TaxPayment, TaxPayrollLedger
from app.services.taxes.enp_split import payroll_enp_due, rebuild_payroll_enp_split
from app.services.taxes.repository import load_tax_inputs


def _ledger(
    year: int,
    month: int,
    tab: str,
    *,
    contributions: str,
    ndfl: str,
    employee: str = "ИВАНОВА И.И.",
) -> TaxPayrollLedger:
    return TaxPayrollLedger(
        id=uuid.uuid4(),
        year=year,
        month=month,
        tab_number=tab,
        employee=employee,
        contributions=Decimal(contributions),
        ndfl=Decimal(ndfl),
    )


def test_due_is_28_of_next_month() -> None:
    """Зарплатный ЕНП за месяц N платится до 28 числа N+1 (декабрь → январь)."""
    assert payroll_enp_due(2026, 6) == date(2026, 7, 28)
    assert payroll_enp_due(2026, 12) == date(2027, 1, 28)


async def test_split_creates_ndfl_and_contributions(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        session.add(_ledger(2026, 7, "206", contributions="13595.93", ndfl="6318.00"))
        await session.flush()

        result = await rebuild_payroll_enp_split(session, year=2026)
        await session.commit()

        assert result.created == 2
        payments = (await session.execute(select(TaxPayment))).scalars().all()
        by_kind = {p.kind: p for p in payments}
        contrib = by_kind["contrib_employees"]
        assert contrib.amount == Decimal("13595.93")
        assert contrib.for_period == "2026-07"
        assert contrib.paid_on == date(2026, 8, 28)  # срок уплаты за июль
        assert contrib.recipient == "fns"
        assert contrib.status == "paid"
        assert contrib.quality_status == "confirmed"  # из документа, не реконструкция
        assert by_kind["ndfl"].amount == Decimal("6318.00")
        # НДФЛ и взносы одного месяца — один банковский пакет.
        assert contrib.bundle_id == by_kind["ndfl"].bundle_id


async def test_split_aggregates_multiple_employees(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Переходный май: взносы двух сотрудниц суммируются в одну строку месяца."""
    async with async_session_factory() as session:
        session.add(_ledger(2026, 5, "205", contributions="3947.40", ndfl="1711.00"))
        session.add(_ledger(2026, 5, "206", contributions="7894.80", ndfl="3239.00"))
        await session.flush()

        await rebuild_payroll_enp_split(session, year=2026)
        await session.commit()

        contrib = await session.scalar(
            select(TaxPayment.amount).where(
                TaxPayment.kind == "contrib_employees",
                TaxPayment.for_period == "2026-05",
            )
        )
        assert contrib == Decimal("11842.20")  # 3947.40 + 7894.80


async def test_split_is_idempotent(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Повторный разнос обновляет строки месяца, а не плодит вторую пару."""
    async with async_session_factory() as session:
        session.add(_ledger(2026, 7, "206", contributions="13595.93", ndfl="6318.00"))
        await session.flush()
        await rebuild_payroll_enp_split(session, year=2026)

        again = await rebuild_payroll_enp_split(session, year=2026)
        await session.commit()

        assert again.created == 0
        assert again.updated == 2
        count = await session.scalar(select(func.count()).select_from(TaxPayment))
        assert count == 2


async def test_split_replaces_reconstructed_row(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Точный разнос из оборотки вытесняет реконструированную строку за тот же месяц."""
    async with async_session_factory() as session:
        session.add(
            TaxPayment(
                id=uuid.uuid4(),
                bundle_id=uuid.uuid4(),
                paid_on=date(2026, 5, 26),
                kind="ndfl",
                amount=Decimal("6318.00"),
                recipient="fns",
                for_year=2026,
                for_period="2026-05",
                status="paid",
                source_kind="bank_statement",
                quality_status="reconstructed",
            )
        )
        session.add(_ledger(2026, 5, "206", contributions="11842.20", ndfl="4950.00"))
        await session.flush()

        await rebuild_payroll_enp_split(session, year=2026)
        await session.commit()

        ndfl_rows = (
            await session.execute(
                select(TaxPayment).where(
                    TaxPayment.kind == "ndfl", TaxPayment.for_period == "2026-05"
                )
            )
        ).scalars().all()
        assert len(ndfl_rows) == 1  # реконструированная вытеснена
        assert ndfl_rows[0].amount == Decimal("4950.00")
        assert ndfl_rows[0].quality_status == "confirmed"


async def test_contributions_feed_usn_deduction_but_ndfl_does_not(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ядро: взносы за работников из разноса идут в вычет УСН, а НДФЛ — нет."""
    async with async_session_factory() as session:
        session.add(_ledger(2026, 5, "206", contributions="11842.20", ndfl="4950.00"))
        await session.flush()
        await rebuild_payroll_enp_split(session, year=2026)
        await session.commit()

        # Взносы мая уплачены к сроку 28.06 → попадают в окно вычета полугодия.
        inputs = await load_tax_inputs(session, as_of=date(2026, 6, 30))
        assert inputs.employees_paid == Decimal("11842.20")  # только взносы, НДФЛ исключён

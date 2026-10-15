"""Потолок аванса окладника при смене должности внутри полумесяца.

Ведомость администрации режет оклад по дням в должности (``_load_admin_position_segments``,
регрессия Дудникова 15.10.2026). Потолок аванса — «заработано на дату» по тем же правилам:
повышенному до окладной должности 08-го оклад капает с 08-го, а не с 1-го числа; при смене
одной окладной должности на другую каждая часть полупериода идёт своим окладом.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Employee, EmployeePositionAssignment, PayrollRate
from app.services.payroll_advance_availability import available_to_advance, earned_as_of

# Полупериод 1–15 мая (15 дней); 12-е — не день выплаты, отсечка не срабатывает.
AS_OF = date(2026, 5, 12)


async def _make_employee_with_positions(
    session: AsyncSession,
    positions: list[tuple[str, date, date | None]],
) -> Employee:
    """Сотрудник с историей назначений ``(должность, с, по)``."""
    employee = Employee(
        id=uuid.uuid4(),
        full_name="Окладник Смена Должности",
        iiko_id=f"iiko-{uuid.uuid4()}",
        status="active",
        is_senior=False,
        is_deputy_senior=False,
        hire_date=None,
        fire_date=None,
        pin_hash="hashed-pin",
        pin_set_at=datetime(2026, 1, 1, tzinfo=UTC),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session.add(employee)
    await session.flush()
    for position, effective_from, effective_to in positions:
        session.add(
            EmployeePositionAssignment(
                id=uuid.uuid4(),
                employee_id=employee.id,
                position=position,
                effective_from=effective_from,
                effective_to=effective_to,
            )
        )
    await session.flush()
    return employee


async def _set_oklad(session: AsyncSession, *, position: str, amount: Decimal) -> None:
    session.add(
        PayrollRate(
            id=uuid.uuid4(),
            employee_id=None,
            position_group=position,
            category="admin",
            station=None,
            rate_type="monthly",
            amount=amount,
            is_active=True,
            effective_from=date(2026, 1, 1),
        )
    )
    await session.flush()


async def test_advance_after_mid_half_promotion_counts_only_days_in_position(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Курьер до 07.05, с 08.05 — старший курьер: на 12.05 заработано 5 дней оклада, не 12."""
    async with async_session_factory() as session:
        employee = await _make_employee_with_positions(
            session,
            [
                ("Курьер", date(2026, 1, 1), date(2026, 5, 7)),
                ("Старший курьер", date(2026, 5, 8), None),
            ],
        )
        await _set_oklad(session, position="Старший курьер", amount=Decimal("40000"))
        await session.commit()

        availability = await available_to_advance(session, employee, AS_OF)
        assert availability.basis == "okladnik"
        # split → ½ оклада 20000 × 5/15 (8–12 мая). С 1-го числа вышло бы 16000.
        assert availability.earned_to_date == Decimal("6666.67")
        assert availability.available == Decimal("6666.67")

        # Исторический срез долга («Учёт ДЗ/КЗ») считает тем же расчётом.
        snapshot = await earned_as_of(session, employee, AS_OF)
        assert snapshot.earned == Decimal("6666.67")


async def test_advance_after_mid_half_demotion_counts_each_oklad_by_days(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Управляющий до 05.05, с 06.05 — менеджер: дни управляющим идут его окладом."""
    async with async_session_factory() as session:
        employee = await _make_employee_with_positions(
            session,
            [
                ("Управляющий", date(2026, 1, 1), date(2026, 5, 5)),
                ("Менеджер", date(2026, 5, 6), None),
            ],
        )
        await _set_oklad(session, position="Управляющий", amount=Decimal("60000"))
        await _set_oklad(session, position="Менеджер", amount=Decimal("30000"))
        await session.commit()

        availability = await available_to_advance(session, employee, AS_OF)
        assert availability.basis == "okladnik"
        # 30000 × 5/15 (1–5 мая) + 15000 × 7/15 (6–12 мая) = 10000 + 7000.
        # Окладом менеджера с 1-го числа вышло бы 12000.
        assert availability.earned_to_date == Decimal("17000.00")


async def test_advance_before_mid_half_promotion_ignores_future_position(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Повышение с 13.05 заведено заранее: на 12.05 заработано только окладом менеджера."""
    async with async_session_factory() as session:
        employee = await _make_employee_with_positions(
            session,
            [
                ("Менеджер", date(2026, 1, 1), date(2026, 5, 12)),
                ("Управляющий", date(2026, 5, 13), None),
            ],
        )
        await _set_oklad(session, position="Управляющий", amount=Decimal("60000"))
        await _set_oklad(session, position="Менеджер", amount=Decimal("30000"))
        await session.commit()

        availability = await available_to_advance(session, employee, AS_OF)
        # 15000 × 12/15 — дни будущей должности в «заработано на дату» не попадают.
        assert availability.earned_to_date == Decimal("12000.00")

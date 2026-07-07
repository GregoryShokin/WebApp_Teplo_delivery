from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.models import DepositPayoutSchedule, Employee, SalaryAdvance
from app.services import dismissal_reconciliation_service


def _dismissing_employee(full_name: str) -> Employee:
    return Employee(
        id=uuid.uuid4(),
        full_name=full_name,
        iiko_id=f"iiko-{uuid.uuid4()}",
        category="category_2",
        status="dismissing",
        fire_date=date(2026, 6, 15),
        is_senior=False,
        is_deputy_senior=False,
        pin_hash="hashed-pin",
        pin_set_at=datetime(2026, 5, 1, tzinfo=UTC),
        created_at=datetime(2026, 5, 1, tzinfo=UTC),
        updated_at=datetime(2026, 5, 1, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_reconcile_flips_dismissing_to_inactive_when_fully_settled(
    async_session_factory: Any,
) -> None:
    async with async_session_factory() as session:
        employee = _dismissing_employee("Clean Leaver")
        session.add(employee)
        await session.flush()

        state = await dismissal_reconciliation_service.settlement_state(session, employee.id)
        assert state.fully_settled is True
        assert state.outstanding() == []

        flipped = await dismissal_reconciliation_service.reconcile_dismissing_employee(
            session, employee.id
        )
        assert flipped is True
        assert employee.status == "inactive"


@pytest.mark.asyncio
async def test_reconcile_keeps_dismissing_with_outstanding_loan(
    async_session_factory: Any,
) -> None:
    async with async_session_factory() as session:
        employee = _dismissing_employee("Loan Debtor")
        session.add(employee)
        await session.flush()
        session.add(
            SalaryAdvance(
                id=uuid.uuid4(),
                employee_id=employee.id,
                role="cook",
                kind="loan",
                amount=Decimal("5000.00"),
                per_installment_amount=Decimal("1000.00"),
                installments_count=5,
                recovered_amount=Decimal("1000.00"),
                status="issued",
                issued_on=date(2026, 6, 1),
            )
        )
        await session.flush()

        state = await dismissal_reconciliation_service.settlement_state(session, employee.id)
        assert state.advances_settled is False
        assert state.fully_settled is False
        assert "займы/авансы" in state.outstanding()

        flipped = await dismissal_reconciliation_service.reconcile_dismissing_employee(
            session, employee.id
        )
        assert flipped is False
        assert employee.status == "dismissing"


@pytest.mark.asyncio
async def test_reconcile_keeps_dismissing_with_pending_deposit_schedule(
    async_session_factory: Any,
) -> None:
    async with async_session_factory() as session:
        employee = _dismissing_employee("Deposit Pending")
        session.add(employee)
        await session.flush()
        session.add(
            DepositPayoutSchedule(
                id=uuid.uuid4(),
                employee_id=employee.id,
                status="pending",
            )
        )
        await session.flush()

        state = await dismissal_reconciliation_service.settlement_state(session, employee.id)
        assert state.deposit_settled is False
        assert state.fully_settled is False

        flipped = await dismissal_reconciliation_service.reconcile_dismissing_employee(
            session, employee.id
        )
        assert flipped is False
        assert employee.status == "dismissing"

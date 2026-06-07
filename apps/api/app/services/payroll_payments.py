from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PayrollLine, PayrollPayment, PayrollRun, PayrollRunEvent
from app.services.payroll_runner import PayrollConflictError, PayrollNotFoundError, money_text

PAYROLL_PAYMENT_METHODS = frozenset({"business_card", "cash", "transfer", "other"})


async def mark_payment(
    session: AsyncSession,
    run_id: uuid.UUID,
    employee_id: uuid.UUID,
    *,
    paid_at: date,
    method: str,
    actor_user_id: uuid.UUID | None,
) -> PayrollPayment:
    run = await _get_payment_run(session, run_id)
    _validate_method(method)
    amount = await _employee_payable_amount(session, run_id, employee_id)

    payment_id = uuid.uuid4()
    stmt = (
        pg_insert(PayrollPayment)
        .values(
            id=payment_id,
            run_id=run_id,
            employee_id=employee_id,
            amount=amount,
            paid_at=paid_at,
            method=method,
            paid_by_user_id=actor_user_id,
        )
        .on_conflict_do_update(
            index_elements=[PayrollPayment.run_id, PayrollPayment.employee_id],
            set_={
                "amount": amount,
                "paid_at": paid_at,
                "method": method,
                "paid_by_user_id": actor_user_id,
            },
        )
        .returning(PayrollPayment.id)
    )
    payment_id = (await session.execute(stmt)).scalar_one()
    _add_payment_event(
        session,
        run=run,
        action="payment_marked",
        actor_user_id=actor_user_id,
        payload={
            "employee_id": str(employee_id),
            "amount": money_text(amount),
            "method": method,
            "paid_at": paid_at.isoformat(),
        },
    )
    await session.commit()
    payment = await session.get(PayrollPayment, payment_id)
    if payment is None:
        raise PayrollNotFoundError("Payroll payment not found")
    await session.refresh(payment)
    return payment


async def unmark_payment(
    session: AsyncSession,
    run_id: uuid.UUID,
    employee_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None,
) -> None:
    run = await _get_payment_run(session, run_id)
    payment = await session.scalar(
        select(PayrollPayment).where(
            PayrollPayment.run_id == run_id,
            PayrollPayment.employee_id == employee_id,
        )
    )
    if payment is None:
        raise PayrollNotFoundError("Payroll payment not found")

    await session.execute(delete(PayrollPayment).where(PayrollPayment.id == payment.id))
    _add_payment_event(
        session,
        run=run,
        action="payment_unmarked",
        actor_user_id=actor_user_id,
        payload={
            "employee_id": str(employee_id),
            "amount": money_text(payment.amount),
            "method": payment.method,
            "paid_at": payment.paid_at.isoformat(),
        },
    )
    await session.commit()


async def mark_all_payments(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    paid_at: date,
    method: str,
    actor_user_id: uuid.UUID | None,
) -> int:
    run = await _get_payment_run(session, run_id)
    _validate_method(method)
    rows = await _unpaid_employee_amounts(session, run_id)
    if not rows:
        return 0

    values = [
        {
            "id": uuid.uuid4(),
            "run_id": run_id,
            "employee_id": employee_id,
            "amount": amount,
            "paid_at": paid_at,
            "method": method,
            "paid_by_user_id": actor_user_id,
        }
        for employee_id, amount in rows
    ]
    await session.execute(pg_insert(PayrollPayment).values(values))
    _add_payment_event(
        session,
        run=run,
        action="payment_marked",
        actor_user_id=actor_user_id,
        payload={
            "employee_ids": [str(employee_id) for employee_id, _amount in rows],
            "count": len(rows),
            "amount_total": money_text(
                sum((amount for _employee_id, amount in rows), Decimal("0"))
            ),
            "method": method,
            "paid_at": paid_at.isoformat(),
        },
    )
    await session.commit()
    return len(rows)


async def _get_payment_run(session: AsyncSession, run_id: uuid.UUID) -> PayrollRun:
    run = await session.get(PayrollRun, run_id)
    if run is None:
        raise PayrollNotFoundError("Payroll run not found")
    if run.is_imported_legacy:
        raise PayrollConflictError("Импортированная ведомость — выплаты не отмечаются")
    if run.status != "finalized":
        raise PayrollConflictError("Сначала финализируйте ведомость")
    return run


def _validate_method(method: str) -> None:
    if method not in PAYROLL_PAYMENT_METHODS:
        raise PayrollConflictError("Некорректный способ выплаты")


async def _employee_payable_amount(
    session: AsyncSession,
    run_id: uuid.UUID,
    employee_id: uuid.UUID,
) -> Decimal:
    amount = await session.scalar(
        select(func.sum(PayrollLine.total_payable)).where(
            PayrollLine.run_id == run_id,
            PayrollLine.employee_id == employee_id,
        )
    )
    if amount is None:
        raise PayrollConflictError("У сотрудника нет начислений в этой ведомости")
    return Decimal(amount)


async def _unpaid_employee_amounts(
    session: AsyncSession,
    run_id: uuid.UUID,
) -> list[tuple[uuid.UUID, Decimal]]:
    existing_payment = (
        select(PayrollPayment.id)
        .where(
            PayrollPayment.run_id == run_id,
            PayrollPayment.employee_id == PayrollLine.employee_id,
        )
        .exists()
    )
    result = await session.execute(
        select(PayrollLine.employee_id, func.sum(PayrollLine.total_payable))
        .where(PayrollLine.run_id == run_id, ~existing_payment)
        .group_by(PayrollLine.employee_id)
        .order_by(PayrollLine.employee_id)
    )
    return [(employee_id, Decimal(amount)) for employee_id, amount in result.all()]


def _add_payment_event(
    session: AsyncSession,
    *,
    run: PayrollRun,
    action: str,
    actor_user_id: uuid.UUID | None,
    payload: dict[str, Any],
) -> None:
    session.add(
        PayrollRunEvent(
            run_id=run.id,
            period_id=run.period_id,
            action=action,
            actor_user_id=actor_user_id,
            payload=payload,
        )
    )

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Employee, PayrollLine, PayrollPayment, PayrollRun, PayrollRunEvent
from app.services.banking import BankClient, TbankClient
from app.services.payroll_runner import PayrollConflictError, PayrollNotFoundError, money_text


async def set_payout_split(
    session: AsyncSession,
    run_id: uuid.UUID,
    employee_id: uuid.UUID,
    *,
    amount_cash: Decimal,
    actor_user_id: uuid.UUID | None,
) -> PayrollPayment:
    await _get_payout_run(session, run_id)
    total = await _employee_payable_amount(session, run_id, employee_id)
    cash = _money(amount_cash)
    if cash < 0 or cash > total:
        raise PayrollConflictError("Наличная часть должна быть от 0 до суммы к выплате")

    payment = await session.scalar(
        select(PayrollPayment).where(
            PayrollPayment.run_id == run_id,
            PayrollPayment.employee_id == employee_id,
        )
    )
    if payment is not None and payment.status == "paid":
        raise PayrollConflictError("Выплата уже отмечена, сначала откатите")

    account = total - cash
    if payment is None:
        payment = PayrollPayment(
            id=uuid.uuid4(),
            run_id=run_id,
            employee_id=employee_id,
            amount=total,
            amount_cash=cash,
            amount_account=account,
            status="planned",
        )
        session.add(payment)
    else:
        payment.amount = total
        payment.amount_cash = cash
        payment.amount_account = account
        payment.status = "planned"
        if account == 0:
            payment.draft_document_id = None
            payment.draft_status = None
            payment.draft_synced_at = None

    await session.commit()
    await session.refresh(payment)
    return payment


async def create_or_update_drafts(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None,
    bank_client: BankClient | None = None,
) -> int:
    run = await _get_payout_run(session, run_id)
    client = bank_client or TbankClient(session)
    rows = await _draftable_payment_rows(session, run_id)
    for payment, employee in rows:
        document_id = payout_document_id(run_id, payment.employee_id)
        had_draft = payment.draft_document_id == document_id
        result = await client.create_payment_draft(
            document_id=document_id,
            amount=_money(payment.amount_account),
            purpose=_draft_purpose(run_id),
            recipient_name=employee.full_name,
        )
        payment.draft_document_id = result.document_id
        payment.draft_status = "updated" if had_draft else result.status
        payment.draft_synced_at = datetime.now(UTC)
        payment.status = "draft_created"
        _add_payout_event(
            session,
            run=run,
            action="bank_draft_created",
            actor_user_id=actor_user_id,
            payload={
                "employee_id": str(payment.employee_id),
                "amount_account": money_text(payment.amount_account),
                "document_id": result.document_id,
                "draft_status": payment.draft_status,
                "provider_ref": result.provider_ref,
            },
        )
    await session.commit()
    return len(rows)


async def get_payout_deltas(session: AsyncSession, run_id: uuid.UUID) -> list[dict[str, Any]]:
    await _get_payout_run(session, run_id)
    rows = await _payment_rows_with_totals(session, run_id)
    deltas: list[dict[str, Any]] = []
    for payment, _employee, new_amount in rows:
        previous_amount = _money(payment.amount)
        delta = new_amount - previous_amount
        deltas.append(
            {
                "employee_id": payment.employee_id,
                "previous_amount": previous_amount,
                "new_amount": new_amount,
                "delta": delta,
                "classification": _delta_classification(delta),
            }
        )
    return deltas


async def apply_payout_deltas(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None,
    bank_client: BankClient | None = None,
) -> int:
    run = await _get_payout_run(session, run_id)
    client = bank_client or TbankClient(session)
    rows = await _payment_rows_with_totals(session, run_id)
    applied_count = 0

    for payment, employee, new_amount in rows:
        previous_amount = _money(payment.amount)
        delta = new_amount - previous_amount
        if delta == 0:
            continue

        if payment.status == "paid":
            if delta > 0:
                document_id = await next_topup_document_id(session, run_id, payment.employee_id)
                result = await client.create_payment_draft(
                    document_id=document_id,
                    amount=delta,
                    purpose=_draft_purpose(run_id),
                    recipient_name=employee.full_name,
                )
                _set_payment_total(payment, new_amount)
                _add_payout_event(
                    session,
                    run=run,
                    action="bank_draft_topup",
                    actor_user_id=actor_user_id,
                    payload={
                        "employee_id": str(payment.employee_id),
                        "previous_amount": money_text(previous_amount),
                        "new_amount": money_text(new_amount),
                        "delta": money_text(delta),
                        "document_id": result.document_id,
                        "draft_status": result.status,
                        "provider_ref": result.provider_ref,
                    },
                )
                applied_count += 1
            else:
                overpaid = -delta
                payment.overpaid_amount = _money(payment.overpaid_amount) + overpaid
                _set_payment_total(payment, new_amount)
                _add_payout_event(
                    session,
                    run=run,
                    action="payout_overpaid",
                    actor_user_id=actor_user_id,
                    payload={
                        "employee_id": str(payment.employee_id),
                        "previous_amount": money_text(previous_amount),
                        "new_amount": money_text(new_amount),
                        "overpaid_amount": money_text(overpaid),
                        "overpaid_total": money_text(payment.overpaid_amount),
                    },
                )
                applied_count += 1
            continue

        _set_payment_total(payment, new_amount)
        payload: dict[str, Any] = {
            "employee_id": str(payment.employee_id),
            "previous_amount": money_text(previous_amount),
            "new_amount": money_text(new_amount),
            "delta": money_text(delta),
            "amount_cash": money_text(payment.amount_cash),
            "amount_account": money_text(payment.amount_account),
        }
        if payment.amount_account > 0:
            document_id = payout_document_id(run_id, payment.employee_id)
            had_draft = payment.draft_document_id == document_id
            result = await client.create_payment_draft(
                document_id=document_id,
                amount=_money(payment.amount_account),
                purpose=_draft_purpose(run_id),
                recipient_name=employee.full_name,
            )
            payment.draft_document_id = result.document_id
            payment.draft_status = "updated" if had_draft else result.status
            payment.draft_synced_at = datetime.now(UTC)
            payment.status = "draft_created"
            payload.update(
                {
                    "document_id": result.document_id,
                    "draft_status": payment.draft_status,
                    "provider_ref": result.provider_ref,
                }
            )
        else:
            payment.status = "planned"
            payment.draft_document_id = None
            payment.draft_status = None
            payment.draft_synced_at = None
        _add_payout_event(
            session,
            run=run,
            action="bank_draft_updated",
            actor_user_id=actor_user_id,
            payload=payload,
        )
        applied_count += 1

    await session.commit()
    return applied_count


def payout_document_id(run_id: uuid.UUID, employee_id: uuid.UUID) -> str:
    return f"teplo-{run_id}-{employee_id}"


async def next_topup_document_id(
    session: AsyncSession,
    run_id: uuid.UUID,
    employee_id: uuid.UUID,
) -> str:
    events = (
        await session.scalars(
            select(PayrollRunEvent).where(
                PayrollRunEvent.run_id == run_id,
                PayrollRunEvent.action == "bank_draft_topup",
            )
        )
    ).all()
    index = 1 + sum(
        1
        for event in events
        if isinstance(event.payload, dict)
        and event.payload.get("employee_id") == str(employee_id)
    )
    return f"{payout_document_id(run_id, employee_id)}-topup-{index}"


async def _get_payout_run(session: AsyncSession, run_id: uuid.UUID) -> PayrollRun:
    run = await session.get(PayrollRun, run_id)
    if run is None:
        raise PayrollNotFoundError("Payroll run not found")
    if run.is_imported_legacy:
        raise PayrollConflictError("Импортированная ведомость — выплаты не отмечаются")
    if run.status != "finalized":
        raise PayrollConflictError("Сначала финализируйте ведомость")
    return run


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
    return _money(amount)


async def _draftable_payment_rows(
    session: AsyncSession,
    run_id: uuid.UUID,
) -> list[tuple[PayrollPayment, Employee]]:
    result = await session.execute(
        select(PayrollPayment, Employee)
        .join(Employee, Employee.id == PayrollPayment.employee_id)
        .where(
            PayrollPayment.run_id == run_id,
            PayrollPayment.amount_account > 0,
            PayrollPayment.status != "paid",
        )
        .order_by(Employee.full_name, PayrollPayment.employee_id)
    )
    return list(result.all())


async def _payment_rows_with_totals(
    session: AsyncSession,
    run_id: uuid.UUID,
) -> list[tuple[PayrollPayment, Employee, Decimal]]:
    result = await session.execute(
        select(
            PayrollPayment,
            Employee,
            func.coalesce(func.sum(PayrollLine.total_payable), 0),
        )
        .join(Employee, Employee.id == PayrollPayment.employee_id)
        .outerjoin(
            PayrollLine,
            and_(
                PayrollLine.run_id == PayrollPayment.run_id,
                PayrollLine.employee_id == PayrollPayment.employee_id,
            ),
        )
        .where(PayrollPayment.run_id == run_id)
        .group_by(PayrollPayment.id, Employee.id)
        .order_by(Employee.full_name, PayrollPayment.employee_id)
    )
    return [
        (payment, employee, _money(new_amount))
        for payment, employee, new_amount in result.all()
    ]


def _set_payment_total(payment: PayrollPayment, new_amount: Decimal) -> None:
    new_amount = _money(new_amount)
    amount_cash = min(_money(payment.amount_cash), new_amount)
    payment.amount = new_amount
    payment.amount_cash = amount_cash
    payment.amount_account = new_amount - amount_cash


def _delta_classification(delta: Decimal) -> str:
    if delta > 0:
        return "topup"
    if delta < 0:
        return "overpay"
    return "unchanged"


def _draft_purpose(run_id: uuid.UUID) -> str:
    return f"Выплата зарплаты по ведомости {run_id}"


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _add_payout_event(
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

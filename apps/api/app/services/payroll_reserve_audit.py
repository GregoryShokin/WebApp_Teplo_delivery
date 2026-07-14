"""Read-only audit of payroll reserves, employee payments and their cashflow rows."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CashflowTransaction,
    DdsArticle,
    Employee,
    PayrollLine,
    PayrollPayment,
    PayrollRun,
    SafeAllocation,
    Wallet,
)
from app.services.banking.cashflow_classify import EXCLUDED_QUALITY
from app.services.payroll_reserves import run_payment_settlement

PAYROLL_CASHFLOW_SOURCE_KINDS = ("payroll_payout", "payroll_bank_to_safe")


def _money(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _reserve_status(amount: Decimal, amount_paid: Decimal) -> str:
    if amount_paid <= 0:
        return "reserved"
    if amount_paid >= amount:
        return "paid"
    return "partially_paid"


@dataclass(frozen=True, slots=True)
class PayrollReserveAuditItem:
    id: uuid.UUID
    wallet_id: uuid.UUID
    wallet_name: str
    location: str
    amount: Decimal
    stored_amount_paid: Decimal
    stored_status: str
    effective_out: Decimal
    excluded_out: Decimal
    expected_amount_paid: Decimal
    expected_status: str
    has_drift: bool


@dataclass(frozen=True, slots=True)
class PayrollPaymentAuditItem:
    employee_id: uuid.UUID
    employee_name: str
    accrued: Decimal
    deposit_scheduled: Decimal
    paid: Decimal
    booked: Decimal
    status: str | None


@dataclass(frozen=True, slots=True)
class PayrollCashflowAuditItem:
    id: uuid.UUID
    wallet_id: uuid.UUID
    wallet_name: str
    operation_date: date
    source_kind: str
    direction: str
    amount: Decimal
    quality_status: str
    article_code: str | None
    purpose: str | None


@dataclass(frozen=True, slots=True)
class PayrollReserveAudit:
    run_id: uuid.UUID
    run_status: str
    run_kind: str
    accrued_total: Decimal
    deposit_scheduled_total: Decimal
    payment_total: Decimal
    booked_total: Decimal
    fully_settled: bool
    effective_payroll_out_total: Decimal
    excluded_payroll_out_total: Decimal
    reserves: tuple[PayrollReserveAuditItem, ...]
    payments: tuple[PayrollPaymentAuditItem, ...]
    cashflows: tuple[PayrollCashflowAuditItem, ...]

    @property
    def has_reserve_drift(self) -> bool:
        return any(item.has_drift for item in self.reserves)


async def build_payroll_reserve_audit(
    session: AsyncSession,
    run_id: uuid.UUID,
) -> PayrollReserveAudit:
    """Build an audit snapshot without locking or mutating payroll data."""
    run = await session.get(PayrollRun, run_id)
    if run is None:
        raise LookupError(f"Payroll run {run_id} not found")
    settlement = await run_payment_settlement(session, run_id)

    reserve_rows = (
        await session.execute(
            select(SafeAllocation, Wallet.name)
            .join(Wallet, Wallet.id == SafeAllocation.wallet_id)
            .where(SafeAllocation.source_run_id == run_id)
            .order_by(SafeAllocation.created_at, SafeAllocation.id)
        )
    ).all()
    cashflow_rows = (
        await session.execute(
            select(CashflowTransaction, Wallet.name, DdsArticle.code)
            .join(Wallet, Wallet.id == CashflowTransaction.wallet_id)
            .outerjoin(DdsArticle, DdsArticle.id == CashflowTransaction.article_id)
            .where(
                CashflowTransaction.source_id == run_id,
                CashflowTransaction.source_kind.in_(PAYROLL_CASHFLOW_SOURCE_KINDS),
            )
            .order_by(
                CashflowTransaction.operation_date,
                CashflowTransaction.created_at,
                CashflowTransaction.id,
            )
        )
    ).all()
    cashflows = tuple(
        PayrollCashflowAuditItem(
            id=txn.id,
            wallet_id=txn.wallet_id,
            wallet_name=wallet_name,
            operation_date=txn.operation_date,
            source_kind=txn.source_kind,
            direction=txn.direction,
            amount=_money(txn.amount),
            quality_status=txn.quality_status,
            article_code=article_code,
            purpose=txn.payment_purpose,
        )
        for txn, wallet_name, article_code in cashflow_rows
    )

    reserves: list[PayrollReserveAuditItem] = []
    for reserve, wallet_name in reserve_rows:
        effective_out = sum(
            (
                item.amount
                for item in cashflows
                if item.source_kind == "payroll_payout"
                and item.direction == "out"
                and item.wallet_id == reserve.wallet_id
                and item.quality_status != EXCLUDED_QUALITY
            ),
            Decimal("0"),
        )
        excluded_out = sum(
            (
                item.amount
                for item in cashflows
                if item.source_kind == "payroll_payout"
                and item.direction == "out"
                and item.wallet_id == reserve.wallet_id
                and item.quality_status == EXCLUDED_QUALITY
            ),
            Decimal("0"),
        )
        amount = _money(reserve.amount)
        stored_amount_paid = _money(reserve.amount_paid)
        expected_amount_paid = min(amount, _money(effective_out))
        expected_status = (
            "cancelled"
            if settlement.settled and expected_amount_paid < amount
            else _reserve_status(amount, expected_amount_paid)
        )
        if reserve.status == "cancelled":
            # Дефинализация штатно отменяет резерв до закрытия обязательства. Но у полностью
            # выплаченной ведомости отменённый резерв всё равно должен хранить честный объём
            # фактических расходов и быть cancelled только при освобождённом остатке.
            has_drift = settlement.settled and (
                stored_amount_paid != expected_amount_paid or expected_status != "cancelled"
            )
        else:
            has_drift = (
                stored_amount_paid != expected_amount_paid or reserve.status != expected_status
            )
        reserves.append(
            PayrollReserveAuditItem(
                id=reserve.id,
                wallet_id=reserve.wallet_id,
                wallet_name=wallet_name,
                location=reserve.location,
                amount=amount,
                stored_amount_paid=stored_amount_paid,
                stored_status=reserve.status,
                effective_out=_money(effective_out),
                excluded_out=_money(excluded_out),
                expected_amount_paid=expected_amount_paid,
                expected_status=expected_status,
                has_drift=has_drift,
            )
        )

    line_rows = (
        await session.execute(
            select(
                PayrollLine.employee_id,
                Employee.full_name,
                func.coalesce(func.sum(PayrollLine.total_payable), 0),
                func.coalesce(func.sum(PayrollLine.deposit_payout_scheduled), 0),
            )
            .join(Employee, Employee.id == PayrollLine.employee_id)
            .where(PayrollLine.run_id == run_id)
            .group_by(PayrollLine.employee_id, Employee.full_name)
            .order_by(Employee.full_name, PayrollLine.employee_id)
        )
    ).all()
    payment_by_employee = {
        payment.employee_id: payment
        for payment in (
            await session.scalars(select(PayrollPayment).where(PayrollPayment.run_id == run_id))
        ).all()
    }
    payments = tuple(
        PayrollPaymentAuditItem(
            employee_id=employee_id,
            employee_name=employee_name,
            accrued=_money(accrued),
            deposit_scheduled=_money(deposit_scheduled),
            paid=_money(payment_by_employee.get(employee_id).amount)
            if employee_id in payment_by_employee
            else Decimal("0.00"),
            booked=_money(payment_by_employee.get(employee_id).booked_amount)
            if employee_id in payment_by_employee
            else Decimal("0.00"),
            status=payment_by_employee.get(employee_id).status
            if employee_id in payment_by_employee
            else None,
        )
        for employee_id, employee_name, accrued, deposit_scheduled in line_rows
    )

    effective_payroll_out_total = sum(
        (
            item.amount
            for item in cashflows
            if item.source_kind == "payroll_payout"
            and item.direction == "out"
            and item.quality_status != EXCLUDED_QUALITY
        ),
        Decimal("0"),
    )
    excluded_payroll_out_total = sum(
        (
            item.amount
            for item in cashflows
            if item.source_kind == "payroll_payout"
            and item.direction == "out"
            and item.quality_status == EXCLUDED_QUALITY
        ),
        Decimal("0"),
    )
    return PayrollReserveAudit(
        run_id=run.id,
        run_status=run.status,
        run_kind="admin"
        if isinstance(run.summary, dict) and run.summary.get("kind") == "admin"
        else "production",
        accrued_total=_money(sum((item.accrued for item in payments), Decimal("0"))),
        deposit_scheduled_total=_money(
            sum((item.deposit_scheduled for item in payments), Decimal("0"))
        ),
        payment_total=_money(sum((item.paid for item in payments), Decimal("0"))),
        booked_total=_money(sum((item.booked for item in payments), Decimal("0"))),
        fully_settled=settlement.settled,
        effective_payroll_out_total=_money(effective_payroll_out_total),
        excluded_payroll_out_total=_money(excluded_payroll_out_total),
        reserves=tuple(reserves),
        payments=payments,
        cashflows=cashflows,
    )

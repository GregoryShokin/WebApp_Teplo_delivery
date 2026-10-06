"""Unpaid salary and scheduled deposit returns, kept as separate obligations.

PayrollPayment.amount/booked_amount remain salary-only for compatibility. Deposit
payments are derived from active employee bookings under the deposit DDS article.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CashflowTransaction,
    DdsArticle,
    PayrollLine,
    PayrollPayment,
    PayrollPayoutBooking,
)

DDS_ARTICLE_DEPOSIT_PAYOUT = "vydacha_depozita_sotrudniku"
ZERO = Decimal("0.00")


async def deposit_paid_by_employee(
    session: AsyncSession, run_id: uuid.UUID
) -> dict[uuid.UUID, Decimal]:
    rows = (
        await session.execute(
            select(
                PayrollPayoutBooking.employee_id,
                func.count(PayrollPayoutBooking.id),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                DdsArticle.code == DDS_ARTICLE_DEPOSIT_PAYOUT,
                                PayrollPayoutBooking.amount,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
            )
            .join(
                CashflowTransaction,
                CashflowTransaction.id == PayrollPayoutBooking.cashflow_transaction_id,
            )
            .outerjoin(DdsArticle, DdsArticle.id == CashflowTransaction.article_id)
            .where(
                PayrollPayoutBooking.run_id == run_id,
                PayrollPayoutBooking.reversal_transaction_id.is_(None),
                CashflowTransaction.direction == "out",
                CashflowTransaction.quality_status != "excluded",
            )
            .group_by(PayrollPayoutBooking.employee_id)
        )
    ).all()
    result = {eid: Decimal(amount) for eid, _count, amount in rows}
    # Before payout-bookings, full payments booked deposit returns in aggregate.
    # Attribute only the actual unlinked deposit cashflow, never merely a paid flag.
    legacy_candidates = (
        await session.execute(
            select(
                PayrollLine.employee_id,
                func.sum(PayrollLine.deposit_payout_scheduled),
            )
            .join(
                PayrollPayment,
                (PayrollPayment.run_id == PayrollLine.run_id)
                & (PayrollPayment.employee_id == PayrollLine.employee_id),
            )
            .where(PayrollLine.run_id == run_id, PayrollPayment.status == "paid")
            .group_by(PayrollLine.employee_id)
            .having(func.sum(PayrollLine.deposit_payout_scheduled) > 0)
            .order_by(PayrollLine.employee_id)
        )
    ).all()
    unlinked = [pair for pair in legacy_candidates if pair[0] not in result]
    if unlinked:
        net = await session.scalar(
            select(
                func.coalesce(
                    func.sum(
                        case(
                            (CashflowTransaction.direction == "out", CashflowTransaction.amount),
                            else_=-CashflowTransaction.amount,
                        )
                    ),
                    0,
                )
            )
            .join(DdsArticle, DdsArticle.id == CashflowTransaction.article_id)
            .where(
                CashflowTransaction.source_kind == "payroll_payout",
                CashflowTransaction.source_id == run_id,
                CashflowTransaction.quality_status != "excluded",
                DdsArticle.code == DDS_ARTICLE_DEPOSIT_PAYOUT,
            )
        )
        available = max(ZERO, Decimal(net or 0) - sum(result.values(), ZERO))
        # Old aggregate payouts returned deposits only in full. Do not guess which
        # employee owns an incomplete/unattributed amount (or a just-created paid flag).
        if available >= sum((Decimal(scheduled) for _eid, scheduled in unlinked), ZERO):
            for eid, scheduled in unlinked:
                result[eid] = Decimal(scheduled)
    return result


@dataclass(frozen=True)
class EmployeeObligation:
    salary: Decimal
    salary_paid: Decimal
    salary_booked: Decimal
    deposit: Decimal
    deposit_paid: Decimal

    @property
    def salary_remaining(self) -> Decimal:
        return max(ZERO, self.salary - self.salary_paid)

    @property
    def deposit_remaining(self) -> Decimal:
        return max(ZERO, self.deposit - self.deposit_paid)

    @property
    def remaining(self) -> Decimal:
        return self.salary_remaining + self.deposit_remaining


async def run_obligations(
    session: AsyncSession, run_id: uuid.UUID
) -> dict[uuid.UUID, EmployeeObligation]:
    lines = (
        await session.execute(
            select(
                PayrollLine.employee_id,
                func.sum(PayrollLine.total_payable),
                func.sum(PayrollLine.deposit_payout_scheduled),
            )
            .where(PayrollLine.run_id == run_id)
            .group_by(PayrollLine.employee_id)
        )
    ).all()
    payments = {
        p.employee_id: p
        for p in (
            await session.scalars(select(PayrollPayment).where(PayrollPayment.run_id == run_id))
        ).all()
    }
    deposits = await deposit_paid_by_employee(session, run_id)
    return {
        eid: EmployeeObligation(
            Decimal(salary or 0),
            Decimal(payments[eid].amount or 0) if eid in payments else ZERO,
            Decimal(payments[eid].booked_amount or 0) if eid in payments else ZERO,
            Decimal(deposit or 0),
            deposits.get(eid, ZERO),
        )
        for eid, salary, deposit in lines
    }

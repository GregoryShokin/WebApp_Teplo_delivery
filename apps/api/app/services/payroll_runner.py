from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AccumulationFundAccount,
    AttendanceEntry,
    DepositAccount,
    DepositTransaction,
    Employee,
    PayrollLine,
    PayrollPeriod,
    PayrollRun,
)
from app.services.attendance_loader import load_attendance_entries
from app.services.payroll_calculator import (
    calculate_payroll_lines,
    decimal,
    deduplicate_issues,
    money,
    needs_setup_issue,
    summarize_lines,
)


class PayrollNotFoundError(LookupError):
    pass


class PayrollConflictError(RuntimeError):
    pass


def compute_next_payroll_period_dates(today: date) -> tuple[date, date, date]:
    days_since_payday = (today.weekday() - 1) % 7
    payroll_date = today - timedelta(days=days_since_payday)
    start_date = payroll_date - timedelta(days=7)
    end_date = payroll_date - timedelta(days=1)
    return start_date, end_date, payroll_date


async def auto_create_next_period(
    session: AsyncSession,
    *,
    today: date | None = None,
) -> PayrollPeriod:
    existing = await session.scalar(select(PayrollPeriod).order_by(desc(PayrollPeriod.start_date)))
    if existing is None:
        start_date, end_date, payroll_date = compute_next_payroll_period_dates(
            today or datetime.now(UTC).date()
        )
    else:
        start_date = existing.end_date + timedelta(days=1)
        end_date = start_date + timedelta(days=6)
        payroll_date = end_date + timedelta(days=1)

    duplicate = await session.scalar(
        select(PayrollPeriod).where(
            PayrollPeriod.period_type == "week",
            PayrollPeriod.start_date == start_date,
            PayrollPeriod.end_date == end_date,
        )
    )
    if duplicate is not None:
        return duplicate

    period = PayrollPeriod(
        period_type="week",
        start_date=start_date,
        end_date=end_date,
        payroll_date=payroll_date,
        status="open",
    )
    session.add(period)
    await session.commit()
    await session.refresh(period)
    return period


async def run_payroll(
    session: AsyncSession,
    period_id: uuid.UUID,
    *,
    iiko_records: Iterable[Mapping[str, Any]] | None = None,
) -> PayrollRun:
    period = await session.get(PayrollPeriod, period_id)
    if period is None:
        raise PayrollNotFoundError("Payroll period not found")
    if period.status == "finalized":
        raise PayrollConflictError("Payroll period is finalized")

    finalized_run = await session.scalar(
        select(PayrollRun).where(
            PayrollRun.period_id == period.id,
            PayrollRun.status == "finalized",
        )
    )
    if finalized_run is not None:
        raise PayrollConflictError("Payroll run is finalized")

    run = PayrollRun(
        period_id=period.id,
        started_at=datetime.now(UTC),
        status="running",
        blocking_issues=[],
        summary={},
    )
    session.add(run)
    await session.flush()

    try:
        entries = await load_attendance_entries(session, period, iiko_records=iiko_records)
        blocking_issues = await collect_blocking_issues(session, entries)
        if blocking_issues:
            run.status = "blocked"
            run.finished_at = datetime.now(UTC)
            run.blocking_issues = blocking_issues
            run.summary = {"blocking_issue_count": len(blocking_issues)}
            await session.commit()
            await session.refresh(run)
            return run

        calculation = await calculate_payroll_lines(session, period, run.id, entries)
        if calculation.blocking_issues:
            run.status = "blocked"
            run.finished_at = datetime.now(UTC)
            run.blocking_issues = calculation.blocking_issues
            run.summary = calculation.summary
            await session.commit()
            await session.refresh(run)
            return run

        for line in calculation.lines:
            session.add(line)
        await session.flush()

        subledger_summary = await update_deposits_and_fund(session, period, run, calculation.lines)
        run.status = "completed"
        run.finished_at = datetime.now(UTC)
        run.blocking_issues = []
        run.summary = calculation.summary | subledger_summary
        await session.commit()
        await session.refresh(run)
        return run
    except Exception as exc:
        run.status = "failed"
        run.finished_at = datetime.now(UTC)
        run.summary = {"error": str(exc)[:500]}
        await session.commit()
        raise


async def collect_blocking_issues(
    session: AsyncSession,
    entries: Iterable[AttendanceEntry],
) -> list[dict[str, Any]]:
    entries = list(entries)
    if not entries:
        return [{"type": "missing_attendance", "message": "No attendance entries for period"}]
    employee_ids = {entry.employee_id for entry in entries}
    employees = {
        employee.id: employee
        for employee in (
            await session.scalars(select(Employee).where(Employee.id.in_(employee_ids)))
        ).all()
    }
    issues: list[dict[str, Any]] = []
    for entry in entries:
        employee = employees.get(entry.employee_id)
        if employee is None:
            issues.append(
                {
                    "type": "unknown_employee",
                    "employee_id": str(entry.employee_id),
                    "work_date": entry.work_date.isoformat(),
                }
            )
            continue
        if employee.status == "requires_setup":
            issues.append(needs_setup_issue(employee))
        if entry.quality_status != "ok":
            issues.append(
                {
                    "type": "attendance_quality_review",
                    "employee_id": str(employee.id),
                    "employee_name": employee.full_name,
                    "work_date": entry.work_date.isoformat(),
                    "quality_status": entry.quality_status,
                    "notes": entry.notes,
                }
            )
        if employee.fire_date and entry.work_date > employee.fire_date:
            issues.append(
                {
                    "type": "post_termination_attendance",
                    "employee_id": str(employee.id),
                    "employee_name": employee.full_name,
                    "work_date": entry.work_date.isoformat(),
                    "fire_date": employee.fire_date.isoformat(),
                }
            )
    return deduplicate_issues(issues)


async def update_deposits_and_fund(
    session: AsyncSession,
    period: PayrollPeriod,
    run: PayrollRun,
    lines: Iterable[PayrollLine],
) -> dict[str, Any]:
    lines = list(lines)
    now = datetime.now(UTC)
    employee_ids = {line.employee_id for line in lines}
    deposit_accounts = await get_deposit_accounts(session, employee_ids)
    deposit_transactions: list[DepositTransaction] = []

    for line in lines:
        amount = decimal((line.components or {}).get("deposit_withholding", 0))
        if amount <= 0:
            continue
        account = deposit_accounts.get(line.employee_id)
        if account is None:
            account = DepositAccount(employee_id=line.employee_id, balance=Decimal("0"))
            session.add(account)
            deposit_accounts[line.employee_id] = account
        account.balance = decimal(account.balance) + amount
        account.last_updated = now
        transaction = DepositTransaction(
            employee_id=line.employee_id,
            run_id=run.id,
            transaction_type="accrual",
            amount=amount,
            created_at=now,
        )
        session.add(transaction)
        deposit_transactions.append(transaction)

    employees_by_id = await get_employees_by_id(session, employee_ids)
    write_off_transactions = await apply_configured_deposit_write_offs(
        session,
        run,
        period,
        deposit_accounts,
        employees_by_id,
        now,
    )
    deposit_transactions.extend(write_off_transactions)

    fund_accrual_total = await accrue_fund(session, period, lines)
    fund_payout_total = await payout_previous_year_fund_if_due(session, period)

    return {
        "deposit_transaction_count": len(deposit_transactions),
        "fund_accrual_total": money(fund_accrual_total),
        "fund_payout_total": money(fund_payout_total),
    }


async def get_deposit_accounts(
    session: AsyncSession,
    employee_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, DepositAccount]:
    employee_ids = set(employee_ids)
    if not employee_ids:
        return {}
    result = await session.scalars(
        select(DepositAccount).where(DepositAccount.employee_id.in_(employee_ids))
    )
    return {account.employee_id: account for account in result.all()}


async def get_employees_by_id(
    session: AsyncSession,
    employee_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, Employee]:
    employee_ids = set(employee_ids)
    if not employee_ids:
        return {}
    result = await session.scalars(select(Employee).where(Employee.id.in_(employee_ids)))
    return {employee.id: employee for employee in result.all()}


async def apply_configured_deposit_write_offs(
    session: AsyncSession,
    run: PayrollRun,
    period: PayrollPeriod,
    accounts_by_employee_id: dict[uuid.UUID, DepositAccount],
    employees_by_id: Mapping[uuid.UUID, Employee],
    now: datetime,
) -> list[DepositTransaction]:
    from app.services.payroll_calculator import load_payroll_settings

    settings = await load_payroll_settings(session)
    write_offs = matching_deposit_write_offs(settings, period, employees_by_id)
    return apply_deposit_write_offs_to_accounts(
        accounts_by_employee_id,
        write_offs,
        run.id,
        now,
        session=session,
    )


def matching_deposit_write_offs(
    settings: Mapping[str, Any],
    period: PayrollPeriod,
    employees_by_id: Mapping[uuid.UUID, Employee],
) -> list[dict[str, Any]]:
    write_offs = settings.get("payroll.deposit_write_offs") or []
    if not isinstance(write_offs, list):
        return []
    by_iiko_id = {employee.iiko_id: employee for employee in employees_by_id.values()}
    result = []
    for item in write_offs:
        if not isinstance(item, Mapping):
            continue
        period_start = item.get("period_start")
        if period_start and str(period_start) != period.start_date.isoformat():
            continue
        employee: Employee | None = None
        employee_id = item.get("employee_id")
        employee_iiko_id = item.get("employee_iiko_id")
        if employee_id:
            employee = employees_by_id.get(uuid.UUID(str(employee_id)))
        elif employee_iiko_id:
            employee = by_iiko_id.get(str(employee_iiko_id))
        if employee is None:
            continue
        result.append({"employee_id": employee.id, "amount": decimal(item.get("amount", 0))})
    return result


def apply_deposit_write_offs_to_accounts(
    accounts_by_employee_id: dict[uuid.UUID, DepositAccount],
    write_offs: Iterable[Mapping[str, Any]],
    run_id: uuid.UUID | None,
    now: datetime,
    *,
    session: AsyncSession | None = None,
) -> list[DepositTransaction]:
    transactions = []
    for item in write_offs:
        employee_id = item["employee_id"]
        account = accounts_by_employee_id.get(employee_id)
        if account is None:
            continue
        amount = min(decimal(item.get("amount", 0)), decimal(account.balance))
        if amount <= 0:
            continue
        account.balance = decimal(account.balance) - amount
        account.last_updated = now
        transaction = DepositTransaction(
            employee_id=employee_id,
            run_id=run_id,
            transaction_type="write_off",
            amount=amount,
            created_at=now,
        )
        if session is not None:
            session.add(transaction)
        transactions.append(transaction)
    return transactions


async def accrue_fund(
    session: AsyncSession,
    period: PayrollPeriod,
    lines: Iterable[PayrollLine],
) -> Decimal:
    total = Decimal("0")
    for line in lines:
        amount = decimal(line.fund_accrual)
        if amount <= 0:
            continue
        account = await get_or_create_fund_account(session, line.employee_id, period.end_date.year)
        account.accumulated_amount = decimal(account.accumulated_amount) + amount
        account.status = "active"
        total += amount
    return total


async def get_or_create_fund_account(
    session: AsyncSession,
    employee_id: uuid.UUID,
    year: int,
) -> AccumulationFundAccount:
    account = await session.scalar(
        select(AccumulationFundAccount).where(
            AccumulationFundAccount.employee_id == employee_id,
            AccumulationFundAccount.year == year,
        )
    )
    if account is not None:
        return account
    account = AccumulationFundAccount(
        employee_id=employee_id,
        year=year,
        accumulated_amount=Decimal("0"),
        paid_out_amount=Decimal("0"),
        status="active",
    )
    session.add(account)
    await session.flush()
    return account


async def payout_previous_year_fund_if_due(
    session: AsyncSession,
    period: PayrollPeriod,
) -> Decimal:
    from app.services.payroll_calculator import load_payroll_settings

    settings = await load_payroll_settings(session)
    payment_day = str(settings.get("payroll.deposit_fund_payment_date", "01-15"))
    if period.payroll_date.strftime("%m-%d") != payment_day:
        return Decimal("0")

    result = await session.scalars(
        select(AccumulationFundAccount).where(
            AccumulationFundAccount.year == period.payroll_date.year - 1,
            AccumulationFundAccount.status == "active",
        )
    )
    return apply_fund_payouts_if_due(result.all(), period.payroll_date, payment_day)


def apply_fund_payouts_to_accounts(accounts: Iterable[AccumulationFundAccount]) -> Decimal:
    total = Decimal("0")
    for account in accounts:
        amount = decimal(account.accumulated_amount) - decimal(account.paid_out_amount)
        if amount <= 0:
            continue
        account.paid_out_amount = decimal(account.paid_out_amount) + amount
        account.status = "paid_out"
        total += amount
    return total


def apply_fund_payouts_if_due(
    accounts: Iterable[AccumulationFundAccount],
    payroll_date: date,
    payment_day: str = "01-15",
) -> Decimal:
    if payroll_date.strftime("%m-%d") != payment_day:
        return Decimal("0")
    return apply_fund_payouts_to_accounts(accounts)


async def finalize_payroll_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    finalized_by_user_id: uuid.UUID | None = None,
) -> PayrollRun:
    run = await session.get(PayrollRun, run_id)
    if run is None:
        raise PayrollNotFoundError("Payroll run not found")
    if run.status == "finalized":
        raise PayrollConflictError("Payroll run is already finalized")
    if run.blocking_issues:
        raise PayrollConflictError("Payroll run has blocking issues")
    if run.status != "completed":
        raise PayrollConflictError("Payroll run is not ready for finalization")

    period = await session.get(PayrollPeriod, run.period_id)
    if period is None:
        raise PayrollNotFoundError("Payroll period not found")
    if period.status == "finalized":
        raise PayrollConflictError("Payroll period is already finalized")

    now = datetime.now(UTC)
    run.status = "finalized"
    run.finished_at = run.finished_at or now
    period.status = "finalized"
    period.finalized_at = now
    period.finalized_by_user_id = finalized_by_user_id
    await session.commit()
    await session.refresh(run)
    return run


async def list_runs(session: AsyncSession) -> list[dict[str, Any]]:
    result = await session.execute(
        select(PayrollRun, PayrollPeriod)
        .join(PayrollPeriod, PayrollRun.period_id == PayrollPeriod.id)
        .order_by(desc(PayrollRun.started_at))
    )
    return [serialize_run(run, period) for run, period in result.all()]


async def get_run(session: AsyncSession, run_id: uuid.UUID) -> dict[str, Any]:
    row = (
        await session.execute(
            select(PayrollRun, PayrollPeriod)
            .join(PayrollPeriod, PayrollRun.period_id == PayrollPeriod.id)
            .where(PayrollRun.id == run_id)
        )
    ).one_or_none()
    if row is None:
        raise PayrollNotFoundError("Payroll run not found")
    run, period = row
    return serialize_run(run, period)


async def get_run_lines(session: AsyncSession, run_id: uuid.UUID) -> list[PayrollLine]:
    run = await session.get(PayrollRun, run_id)
    if run is None:
        raise PayrollNotFoundError("Payroll run not found")
    result = await session.scalars(
        select(PayrollLine)
        .where(PayrollLine.run_id == run_id)
        .order_by(PayrollLine.role, PayrollLine.employee_id)
    )
    return list(result.all())


def serialize_period(period: PayrollPeriod) -> dict[str, Any]:
    return {
        "id": period.id,
        "period_type": period.period_type,
        "start_date": period.start_date,
        "end_date": period.end_date,
        "payroll_date": period.payroll_date,
        "status": period.status,
        "finalized_at": period.finalized_at,
        "finalized_by_user_id": period.finalized_by_user_id,
    }


def serialize_run(run: PayrollRun, period: PayrollPeriod | None = None) -> dict[str, Any]:
    data = {
        "id": run.id,
        "period_id": run.period_id,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "status": run.status,
        "blocking_issues": run.blocking_issues or [],
        "summary": run.summary or {},
    }
    if period is not None:
        data["period"] = serialize_period(period)
    return data


def summarize_persisted_lines(lines: Iterable[PayrollLine]) -> dict[str, Any]:
    return summarize_lines(lines)

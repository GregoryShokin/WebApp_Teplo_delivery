"""Импорт исторических зарплат из CSV Google Sheets.

Usage:
    python -m app.scripts.import_legacy_payroll /path/to/file.csv [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import uuid
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import delete, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models import (
    AccumulationFundAccount,
    AccumulationFundTransaction,
    DepositTransaction,
    Employee,
    EmployeeRoleAssignment,
    PayrollAdjustment,
    PayrollAdjustmentCategory,
    PayrollLine,
    PayrollPeriod,
    PayrollRun,
)
from app.services.payroll_adjustment_service import ensure_legacy_categories

DEPARTMENT_NAME = "Производство Черникова"
MONEY = Decimal("0.01")
LEGACY_FUND_COMMENT_PREFIX = "[legacy_import]"

PAYMENT_TYPE_MAP: dict[str, str | tuple[str, str] | tuple[str, str, str]] = {
    "Оклад": "base_pay",
    "Процент": "percent_pay",
    "Больничные и отпуска": "vacation_pay",
    "Больничные и отпуска и пособия": "vacation_pay",
    "НДФЛ Начислено": "ndfl_withheld",
    "Премия": ("adjustment", "bonus", "premium"),
    "Штрафы по ревизиям": ("adjustment", "penalty", "audit_penalty"),
    "Штрафы и удержания": ("adjustment", "penalty", "manual_penalty"),
    "Депозит удержание": ("deposit", "accrual"),
    "Депозит возврат": ("deposit", "payout"),
    "Депозит списание": ("deposit", "write_off"),
    "Накопительный фонд": ("fund", "accrual"),
    "Списание накоплений": ("fund", "forfeit"),
}

LEGACY_CSV_COLUMNS = [
    "Date",
    "Amount",
    "Employee Name",
    "Position",
    "Department",
    "Payment Type",
    "Description",
    "Year",
]
REQUIRED_COLUMNS = set(LEGACY_CSV_COLUMNS)


@dataclass(slots=True)
class LegacyAdjustmentRow:
    adjustment_type: str
    category_code: str
    amount: Decimal
    work_date: date
    comment: str | None


@dataclass(slots=True)
class LegacyDepositRow:
    transaction_type: str
    amount: Decimal
    work_date: date


@dataclass(slots=True)
class LegacyFundRow:
    transaction_type: str
    amount: Decimal
    work_date: date
    comment: str | None


@dataclass(slots=True)
class LegacyEmployeeBucket:
    base_pay: Decimal = Decimal("0")
    percent_pay: Decimal = Decimal("0")
    vacation_pay: Decimal = Decimal("0")
    ndfl_withheld: Decimal = Decimal("0")
    adjustments: list[LegacyAdjustmentRow] = field(default_factory=list)
    deposits: list[LegacyDepositRow] = field(default_factory=list)
    funds: list[LegacyFundRow] = field(default_factory=list)
    days: dict[date, dict[str, Decimal]] = field(default_factory=dict)


@dataclass(slots=True)
class WeeklyImportSummary:
    period_start: date
    period_end: date
    run_id: uuid.UUID
    lines_count: int = 0
    base_pay: Decimal = Decimal("0")
    percent_pay: Decimal = Decimal("0")
    vacation_pay: Decimal = Decimal("0")
    bonus_total: Decimal = Decimal("0")
    penalty_total: Decimal = Decimal("0")
    deposit_accrual: Decimal = Decimal("0")
    deposit_payout: Decimal = Decimal("0")
    deposit_write_off: Decimal = Decimal("0")
    fund_accrual: Decimal = Decimal("0")
    fund_forfeit: Decimal = Decimal("0")
    ndfl_withheld: Decimal = Decimal("0")
    total_payable: Decimal = Decimal("0")


@dataclass(slots=True)
class LegacyImportSummary:
    periods_created: int = 0
    runs_created: int = 0
    lines_created: int = 0
    adjustments_created: int = 0
    deposits_created: int = 0
    funds_created: int = 0
    skipped_departments: int = 0
    unmatched_employees: set[str] = field(default_factory=set)
    unknown_payment_types: set[str] = field(default_factory=set)
    year_mismatches: list[str] = field(default_factory=list)
    weekly: list[WeeklyImportSummary] = field(default_factory=list)


def parse_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%d.%m.%Y").date()


def parse_amount(value: str) -> Decimal:
    cleaned = value.replace("\u00a0", "").replace(" ", "").strip()
    if not cleaned:
        return Decimal("0")
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif cleaned.count(",") > 1:
        cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        whole, fraction = cleaned.rsplit(",", 1)
        if len(fraction) == 3 and whole.replace("-", "").isdigit():
            cleaned = whole + fraction
        else:
            cleaned = whole + "." + fraction
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid amount: {value!r}") from exc


def payroll_period_for_date(value: date) -> tuple[date, date]:
    days_since_tuesday = (value.weekday() - 1) % 7
    start = value - timedelta(days=days_since_tuesday)
    return start, start + timedelta(days=6)


def iter_legacy_csv_rows(path: Path) -> Iterator[tuple[int, dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.reader(file)
        first_row = next(reader, None)
        if first_row is None:
            return

        if REQUIRED_COLUMNS.issubset(set(first_row)):
            dict_reader = csv.DictReader(file, fieldnames=first_row)
            for line_no, row in enumerate(dict_reader, start=2):
                yield line_no, row
            return

        yield 1, legacy_row_from_values(first_row, line_no=1)
        for line_no, row in enumerate(reader, start=2):
            if not any(clean(value) for value in row):
                continue
            yield line_no, legacy_row_from_values(row, line_no=line_no)


def legacy_row_from_values(values: list[str], *, line_no: int) -> dict[str, str]:
    if len(values) != len(LEGACY_CSV_COLUMNS):
        raise ValueError(
            f"CSV line {line_no} has {len(values)} columns, expected {len(LEGACY_CSV_COLUMNS)}"
        )
    return dict(zip(LEGACY_CSV_COLUMNS, values, strict=True))


async def import_csv_with_session(
    session: AsyncSession,
    path: Path,
) -> LegacyImportSummary:
    await ensure_legacy_categories(session)
    employees_by_name = await load_employees_by_name(session)
    categories_by_code = await load_categories_by_code(session)

    summary = LegacyImportSummary()
    buckets = read_legacy_buckets(path, employees_by_name, summary)
    if not buckets:
        return summary

    now = datetime.now(UTC)
    fund_account_ids_to_recalculate: set[uuid.UUID] = set()
    grouped_by_period: dict[tuple[date, date], dict[uuid.UUID, LegacyEmployeeBucket]] = defaultdict(
        dict
    )
    for (period_start, period_end, employee_id), bucket in buckets.items():
        grouped_by_period[(period_start, period_end)][employee_id] = bucket

    for (period_start, period_end), employee_buckets in sorted(grouped_by_period.items()):
        period, period_created = await get_or_create_period(
            session,
            period_start=period_start,
            period_end=period_end,
            now=now,
        )
        if period_created:
            summary.periods_created += 1

        run, run_created = await get_or_create_legacy_run(session, period, now=now)
        if run_created:
            summary.runs_created += 1

        employee_ids = set(employee_buckets)
        fund_account_ids_to_recalculate.update(
            await clear_existing_legacy_rows(session, run, period, employee_ids)
        )

        weekly_summary = WeeklyImportSummary(
            period_start=period_start,
            period_end=period_end,
            run_id=run.id,
        )
        for employee_id, bucket in sorted(employee_buckets.items(), key=lambda item: str(item[0])):
            role = await get_role_for_date(session, employee_id, period_end)
            line = build_payroll_line(
                run=run,
                employee_id=employee_id,
                role=role or "imported_legacy",
                bucket=bucket,
            )
            session.add(line)
            summary.lines_created += 1
            weekly_summary.lines_count += 1

            for adjustment in bucket.adjustments:
                category = categories_by_code[adjustment.category_code]
                session.add(
                    PayrollAdjustment(
                        id=uuid.uuid4(),
                        employee_id=employee_id,
                        work_date=adjustment.work_date,
                        type=adjustment.adjustment_type,
                        category_id=category.id,
                        amount=money(adjustment.amount),
                        comment=adjustment.comment,
                        created_by_label="import:legacy",
                    )
                )
                summary.adjustments_created += 1

            for deposit in bucket.deposits:
                session.add(
                    DepositTransaction(
                        id=uuid.uuid4(),
                        employee_id=employee_id,
                        run_id=run.id,
                        transaction_type=deposit.transaction_type,
                        amount=money(deposit.amount),
                        created_at=datetime.combine(deposit.work_date, time.min, tzinfo=UTC),
                    )
                )
                summary.deposits_created += 1

            for fund in bucket.funds:
                year = fund.work_date.year
                account = await get_or_create_fund_account(session, employee_id, year)
                amount = money(fund.amount)
                update_fund_account_from_transaction(account, fund.transaction_type, amount)
                session.add(
                    AccumulationFundTransaction(
                        id=uuid.uuid4(),
                        account_id=account.id,
                        employee_id=employee_id,
                        year=year,
                        run_id=run.id,
                        transaction_type=fund.transaction_type,
                        amount=amount,
                        comment=legacy_fund_comment(fund.comment),
                        created_at=datetime.combine(fund.work_date, time.min, tzinfo=UTC),
                    )
                )
                fund_account_ids_to_recalculate.add(account.id)
                summary.funds_created += 1

            add_bucket_to_weekly_summary(weekly_summary, bucket, line.total_payable)

        run.summary = {
            "source": "google_sheets_import",
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "lines_count": weekly_summary.lines_count,
            "base_pay": money_string(weekly_summary.base_pay),
            "percent_pay": money_string(weekly_summary.percent_pay),
            "vacation_pay": money_string(weekly_summary.vacation_pay),
            "bonus_total": money_string(weekly_summary.bonus_total),
            "penalty_total": money_string(weekly_summary.penalty_total),
            "deposit_accrual": money_string(weekly_summary.deposit_accrual),
            "deposit_payout": money_string(weekly_summary.deposit_payout),
            "deposit_write_off": money_string(weekly_summary.deposit_write_off),
            "fund_accrual": money_string(weekly_summary.fund_accrual),
            "fund_forfeit": money_string(weekly_summary.fund_forfeit),
            "ndfl_withheld": money_string(weekly_summary.ndfl_withheld),
            "total_payable": money_string(weekly_summary.total_payable),
        }
        summary.weekly.append(weekly_summary)
        await session.flush()

    await recalculate_fund_accounts(session, fund_account_ids_to_recalculate)
    return summary


async def import_csv(
    path: Path,
    dry_run: bool = False,
    *,
    reset: bool = False,
) -> LegacyImportSummary:
    async with AsyncSessionLocal() as session:
        if reset:
            reset_count = await reset_legacy_import(session)
            print(f"Reset: removed {reset_count} legacy runs and dependencies")
        summary = await import_csv_with_session(session, path)
        if dry_run:
            await session.rollback()
        else:
            await session.commit()
        print_report(summary, dry_run=dry_run)
        return summary


def read_legacy_buckets(
    path: Path,
    employees_by_name: dict[str, Employee],
    summary: LegacyImportSummary,
) -> dict[tuple[date, date, uuid.UUID], LegacyEmployeeBucket]:
    buckets: dict[tuple[date, date, uuid.UUID], LegacyEmployeeBucket] = defaultdict(
        LegacyEmployeeBucket
    )
    for line_no, row in iter_legacy_csv_rows(path):
        if clean(row.get("Department")) != DEPARTMENT_NAME:
            summary.skipped_departments += 1
            continue
        employee_name = clean(row.get("Employee Name"))
        employee = employees_by_name.get(employee_name)
        if employee is None:
            if employee_name:
                summary.unmatched_employees.add(employee_name)
            continue

        work_date = parse_date(row["Date"])
        year_value = clean(row.get("Year"))
        if year_value and year_value.isdigit() and int(year_value) != work_date.year:
            summary.year_mismatches.append(f"line {line_no}: {year_value} != {work_date.year}")

        payment_type = clean(row.get("Payment Type"))
        mapping = PAYMENT_TYPE_MAP.get(payment_type)
        if mapping is None:
            summary.unknown_payment_types.add(payment_type or "<blank>")
            continue

        amount = parse_amount(row["Amount"])
        period_start, period_end = payroll_period_for_date(work_date)
        bucket = buckets[(period_start, period_end, employee.id)]
        apply_row_to_bucket(
            bucket,
            work_date=work_date,
            amount=amount,
            mapping=mapping,
            description=clean(row.get("Description")) or None,
        )
    return dict(buckets)


def apply_row_to_bucket(
    bucket: LegacyEmployeeBucket,
    *,
    work_date: date,
    amount: Decimal,
    mapping: str | tuple[str, str] | tuple[str, str, str],
    description: str | None,
) -> None:
    if isinstance(mapping, str):
        value = abs(amount) if mapping == "ndfl_withheld" else amount
        setattr(bucket, mapping, getattr(bucket, mapping) + value)
        add_day_amount(bucket, work_date, mapping, value)
        return

    amount = abs(amount)
    if amount == 0:
        return
    if mapping[0] == "adjustment":
        _, adjustment_type, category_code = mapping
        bucket.adjustments.append(
            LegacyAdjustmentRow(
                adjustment_type=adjustment_type,
                category_code=category_code,
                amount=amount,
                work_date=work_date,
                comment=description,
            )
        )
    elif mapping[0] == "deposit":
        _, transaction_type = mapping
        bucket.deposits.append(
            LegacyDepositRow(
                transaction_type=transaction_type,
                amount=amount,
                work_date=work_date,
            )
        )
    elif mapping[0] == "fund":
        _, transaction_type = mapping
        bucket.funds.append(
            LegacyFundRow(
                transaction_type=transaction_type,
                amount=amount,
                work_date=work_date,
                comment=description,
            )
        )


def add_day_amount(
    bucket: LegacyEmployeeBucket,
    work_date: date,
    field_name: str,
    amount: Decimal,
) -> None:
    day = bucket.days.setdefault(
        work_date,
        {
            "base_pay": Decimal("0"),
            "percent_pay": Decimal("0"),
            "vacation_pay": Decimal("0"),
            "fund_accrual": Decimal("0"),
            "ndfl_withheld": Decimal("0"),
        },
    )
    day[field_name] += amount


async def load_employees_by_name(session: AsyncSession) -> dict[str, Employee]:
    employees = (await session.scalars(select(Employee))).all()
    return {employee.full_name: employee for employee in employees}


async def load_categories_by_code(
    session: AsyncSession,
) -> dict[str, PayrollAdjustmentCategory]:
    categories = (await session.scalars(select(PayrollAdjustmentCategory))).all()
    return {category.code: category for category in categories}


async def get_or_create_period(
    session: AsyncSession,
    *,
    period_start: date,
    period_end: date,
    now: datetime,
) -> tuple[PayrollPeriod, bool]:
    period = await session.scalar(
        select(PayrollPeriod).where(
            PayrollPeriod.period_type == "week",
            PayrollPeriod.start_date == period_start,
            PayrollPeriod.end_date == period_end,
        )
    )
    created = period is None
    if period is None:
        period = PayrollPeriod(
            id=uuid.uuid4(),
            period_type="week",
            start_date=period_start,
            end_date=period_end,
            payroll_date=period_end + timedelta(days=1),
        )
        session.add(period)
    period.status = "finalized"
    period.finalized_at = now
    await session.flush()
    return period, created


async def get_or_create_legacy_run(
    session: AsyncSession,
    period: PayrollPeriod,
    *,
    now: datetime,
) -> tuple[PayrollRun, bool]:
    run = await session.scalar(
        select(PayrollRun)
        .where(
            PayrollRun.period_id == period.id,
            PayrollRun.is_imported_legacy.is_(True),
        )
        .limit(1)
    )
    created = run is None
    if run is None:
        run = PayrollRun(id=uuid.uuid4(), period_id=period.id)
        session.add(run)
    run.started_at = now
    run.finished_at = now
    run.status = "finalized"
    run.is_imported_legacy = True
    run.blocking_issues = []
    run.summary = {"source": "google_sheets_import"}
    await session.flush()
    return run, created


async def clear_existing_legacy_rows(
    session: AsyncSession,
    run: PayrollRun,
    period: PayrollPeriod,
    employee_ids: set[uuid.UUID],
) -> set[uuid.UUID]:
    await session.execute(delete(PayrollLine).where(PayrollLine.run_id == run.id))
    await session.execute(delete(DepositTransaction).where(DepositTransaction.run_id == run.id))
    fund_account_ids: set[uuid.UUID] = set()
    if employee_ids:
        await session.execute(
            delete(PayrollAdjustment).where(
                PayrollAdjustment.created_by_label == "import:legacy",
                PayrollAdjustment.employee_id.in_(employee_ids),
                PayrollAdjustment.work_date >= period.start_date,
                PayrollAdjustment.work_date <= period.end_date,
            )
        )
        start_at = datetime.combine(period.start_date, time.min, tzinfo=UTC)
        end_at = datetime.combine(period.end_date + timedelta(days=1), time.min, tzinfo=UTC)
        fund_filter = (
            AccumulationFundTransaction.comment.like(f"{LEGACY_FUND_COMMENT_PREFIX}%"),
            AccumulationFundTransaction.employee_id.in_(employee_ids),
            AccumulationFundTransaction.created_at >= start_at,
            AccumulationFundTransaction.created_at < end_at,
        )
        fund_account_ids = {
            account_id
            for account_id in (
                await session.scalars(
                    select(AccumulationFundTransaction.account_id).where(*fund_filter)
                )
            ).all()
            if account_id is not None
        }
        await session.execute(delete(AccumulationFundTransaction).where(*fund_filter))
    return fund_account_ids


async def reset_legacy_import(session: AsyncSession) -> int:
    legacy_runs = (
        await session.scalars(select(PayrollRun).where(PayrollRun.is_imported_legacy.is_(True)))
    ).all()
    legacy_run_ids = [run.id for run in legacy_runs]
    legacy_period_ids = [run.period_id for run in legacy_runs]

    fund_account_ids = {
        account_id
        for account_id in (
            await session.scalars(
                select(AccumulationFundTransaction.account_id).where(
                    AccumulationFundTransaction.comment.like(f"{LEGACY_FUND_COMMENT_PREFIX}%")
                )
            )
        ).all()
        if account_id is not None
    }

    if legacy_run_ids:
        await session.execute(
            delete(DepositTransaction).where(DepositTransaction.run_id.in_(legacy_run_ids))
        )
    await session.execute(
        delete(PayrollAdjustment).where(PayrollAdjustment.created_by_label == "import:legacy")
    )
    await session.execute(
        delete(AccumulationFundTransaction).where(
            AccumulationFundTransaction.comment.like(f"{LEGACY_FUND_COMMENT_PREFIX}%")
        )
    )
    if legacy_run_ids:
        await session.execute(delete(PayrollLine).where(PayrollLine.run_id.in_(legacy_run_ids)))
        await session.execute(delete(PayrollRun).where(PayrollRun.id.in_(legacy_run_ids)))
    if legacy_period_ids:
        await session.execute(
            delete(PayrollPeriod).where(
                PayrollPeriod.id.in_(legacy_period_ids),
                ~exists().where(PayrollRun.period_id == PayrollPeriod.id),
            )
        )
    if fund_account_ids:
        await session.execute(
            delete(AccumulationFundAccount).where(
                AccumulationFundAccount.id.in_(fund_account_ids),
                ~exists().where(
                    AccumulationFundTransaction.account_id == AccumulationFundAccount.id
                ),
            )
        )
        await recalculate_fund_accounts(session, fund_account_ids)
    await session.flush()
    return len(legacy_run_ids)


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
        id=uuid.uuid4(),
        employee_id=employee_id,
        year=year,
        accumulated_amount=Decimal("0"),
        paid_out_amount=Decimal("0"),
        forfeited_amount=Decimal("0"),
        status="active",
    )
    session.add(account)
    await session.flush()
    return account


def update_fund_account_from_transaction(
    account: AccumulationFundAccount,
    transaction_type: str,
    amount: Decimal,
) -> None:
    if transaction_type == "accrual":
        account.accumulated_amount = decimal_value(account.accumulated_amount) + amount
    elif transaction_type == "payout":
        account.paid_out_amount = decimal_value(account.paid_out_amount) + amount
    elif transaction_type == "forfeit":
        account.forfeited_amount = decimal_value(account.forfeited_amount) + amount
    sync_fund_account_status(account)


def sync_fund_account_status(account: AccumulationFundAccount) -> None:
    accumulated = decimal_value(account.accumulated_amount)
    paid_out = decimal_value(account.paid_out_amount)
    forfeited = decimal_value(account.forfeited_amount)
    outstanding = accumulated - paid_out - forfeited
    if outstanding > 0 or (accumulated == 0 and paid_out == 0 and forfeited == 0):
        account.status = "active"
    elif forfeited > 0:
        account.status = "forfeited"
    elif paid_out > 0:
        account.status = "paid_out"
    else:
        account.status = "active"


async def recalculate_fund_accounts(
    session: AsyncSession,
    account_ids: set[uuid.UUID],
) -> None:
    account_ids = {account_id for account_id in account_ids if account_id is not None}
    if not account_ids:
        return
    await session.flush()
    accounts = (
        await session.scalars(
            select(AccumulationFundAccount).where(AccumulationFundAccount.id.in_(account_ids))
        )
    ).all()
    if not accounts:
        return
    transactions = (
        await session.scalars(
            select(AccumulationFundTransaction).where(
                AccumulationFundTransaction.account_id.in_(account_ids)
            )
        )
    ).all()
    totals: dict[uuid.UUID, dict[str, Decimal]] = defaultdict(
        lambda: {
            "accumulated_amount": Decimal("0"),
            "paid_out_amount": Decimal("0"),
            "forfeited_amount": Decimal("0"),
        }
    )
    for transaction in transactions:
        amount = decimal_value(transaction.amount)
        account_totals = totals[transaction.account_id]
        if transaction.transaction_type in {"accrual", "initial_balance"}:
            account_totals["accumulated_amount"] += amount
        elif transaction.transaction_type == "payout":
            account_totals["paid_out_amount"] += amount
        elif transaction.transaction_type == "forfeit":
            account_totals["forfeited_amount"] += amount
    for account in accounts:
        account_totals = totals[account.id]
        account.accumulated_amount = money(account_totals["accumulated_amount"])
        account.paid_out_amount = money(account_totals["paid_out_amount"])
        account.forfeited_amount = money(account_totals["forfeited_amount"])
        sync_fund_account_status(account)


async def get_role_for_date(
    session: AsyncSession,
    employee_id: uuid.UUID,
    target_date: date,
) -> str | None:
    assignment = await session.scalar(
        select(EmployeeRoleAssignment)
        .where(
            EmployeeRoleAssignment.employee_id == employee_id,
            EmployeeRoleAssignment.effective_from <= target_date,
            or_(
                EmployeeRoleAssignment.effective_to.is_(None),
                EmployeeRoleAssignment.effective_to >= target_date,
            ),
        )
        .order_by(
            EmployeeRoleAssignment.is_primary.desc(),
            EmployeeRoleAssignment.effective_from.desc(),
        )
        .limit(1)
    )
    return assignment.payroll_role if assignment is not None else None


def build_payroll_line(
    *,
    run: PayrollRun,
    employee_id: uuid.UUID,
    role: str,
    bucket: LegacyEmployeeBucket,
) -> PayrollLine:
    bonus_total = sum(
        (item.amount for item in bucket.adjustments if item.adjustment_type == "bonus"),
        Decimal("0"),
    )
    penalty_total = sum(
        (item.amount for item in bucket.adjustments if item.adjustment_type == "penalty"),
        Decimal("0"),
    )
    deposit_accrual = sum(
        (item.amount for item in bucket.deposits if item.transaction_type == "accrual"),
        Decimal("0"),
    )
    deposit_payout = sum(
        (item.amount for item in bucket.deposits if item.transaction_type == "payout"),
        Decimal("0"),
    )
    deduction = penalty_total + deposit_accrual
    total_payable = (
        bucket.base_pay
        + bucket.percent_pay
        + bucket.vacation_pay
        + bonus_total
        - penalty_total
        - deposit_accrual
        + deposit_payout
        - bucket.ndfl_withheld
    )
    return PayrollLine(
        id=uuid.uuid4(),
        run_id=run.id,
        employee_id=employee_id,
        role=role,
        base_pay=money(bucket.base_pay),
        premium=Decimal("0"),
        percent_pay=money(bucket.percent_pay),
        vacation_pay=money(bucket.vacation_pay),
        ndfl_withheld=money(bucket.ndfl_withheld),
        fund_accrual=Decimal("0"),
        deduction=money(deduction),
        total_payable=money(total_payable),
        deposit_excluded_for_run=False,
        deposit_exclusion_reason=None,
        components={
            "days": day_components(bucket),
            "deposit_withholding": money_string(deposit_accrual),
            "deposit_payout": money_string(deposit_payout),
            "ndfl_withheld": money_string(bucket.ndfl_withheld),
            "adjustments": adjustment_components(bucket),
            "imported": True,
            "source": "google_sheets_import",
        },
    )


def day_components(bucket: LegacyEmployeeBucket) -> list[dict[str, str]]:
    return [
        {
            "date": work_date.isoformat(),
            "base_pay": money_string(values["base_pay"]),
            "percent_pay": money_string(values["percent_pay"]),
            "vacation_pay": money_string(values["vacation_pay"]),
            "fund_accrual": money_string(values["fund_accrual"]),
            "ndfl_withheld": money_string(values["ndfl_withheld"]),
        }
        for work_date, values in sorted(bucket.days.items())
    ]


def adjustment_components(bucket: LegacyEmployeeBucket) -> dict[str, Any]:
    bonuses: list[dict[str, Any]] = []
    penalties: list[dict[str, Any]] = []
    for adjustment in bucket.adjustments:
        item = {
            "type": adjustment.adjustment_type,
            "category_code": adjustment.category_code,
            "work_date": adjustment.work_date.isoformat(),
            "amount": money_string(adjustment.amount),
            "comment": adjustment.comment,
        }
        if adjustment.adjustment_type == "bonus":
            bonuses.append(item)
        else:
            penalties.append(item)
    return {"bonuses": bonuses, "penalties": penalties, "primary_role_chosen": None}


def add_bucket_to_weekly_summary(
    summary: WeeklyImportSummary,
    bucket: LegacyEmployeeBucket,
    total_payable: Decimal,
) -> None:
    summary.base_pay += money(bucket.base_pay)
    summary.percent_pay += money(bucket.percent_pay)
    summary.vacation_pay += money(bucket.vacation_pay)
    summary.ndfl_withheld += money(bucket.ndfl_withheld)
    summary.bonus_total += sum(
        (money(item.amount) for item in bucket.adjustments if item.adjustment_type == "bonus"),
        Decimal("0"),
    )
    summary.penalty_total += sum(
        (money(item.amount) for item in bucket.adjustments if item.adjustment_type == "penalty"),
        Decimal("0"),
    )
    summary.deposit_accrual += sum(
        (money(item.amount) for item in bucket.deposits if item.transaction_type == "accrual"),
        Decimal("0"),
    )
    summary.deposit_payout += sum(
        (money(item.amount) for item in bucket.deposits if item.transaction_type == "payout"),
        Decimal("0"),
    )
    summary.deposit_write_off += sum(
        (money(item.amount) for item in bucket.deposits if item.transaction_type == "write_off"),
        Decimal("0"),
    )
    summary.fund_accrual += sum(
        (money(item.amount) for item in bucket.funds if item.transaction_type == "accrual"),
        Decimal("0"),
    )
    summary.fund_forfeit += sum(
        (money(item.amount) for item in bucket.funds if item.transaction_type == "forfeit"),
        Decimal("0"),
    )
    summary.total_payable += money(total_payable)


def clean(value: Any) -> str:
    return " ".join(str(value or "").replace("\u00a0", " ").split())


def money(value: Decimal) -> Decimal:
    return value.quantize(MONEY)


def decimal_value(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def money_string(value: Decimal) -> str:
    return f"{money(value):.2f}"


def legacy_fund_comment(description: str | None) -> str:
    return (
        f"{LEGACY_FUND_COMMENT_PREFIX} {description}"
        if description
        else LEGACY_FUND_COMMENT_PREFIX
    )


def print_report(summary: LegacyImportSummary, *, dry_run: bool) -> None:
    prefix = "DRY RUN: would import" if dry_run else "Imported"
    print(
        f"{prefix} {summary.runs_created} runs, {summary.lines_created} lines, "
        f"{summary.adjustments_created} adjustments, {summary.deposits_created} deposits, "
        f"{summary.funds_created} fund transactions"
    )
    if summary.skipped_departments:
        print(f"Skipped by department: {summary.skipped_departments}")
    if summary.unmatched_employees:
        print(f"Unmatched employees ({len(summary.unmatched_employees)}):")
        for name in sorted(summary.unmatched_employees):
            print(f"  - {name}")
    if summary.unknown_payment_types:
        print("Unknown payment types:")
        for payment_type in sorted(summary.unknown_payment_types):
            print(f"  - {payment_type}")
    if summary.year_mismatches:
        print("Year mismatches:")
        for item in summary.year_mismatches:
            print(f"  - {item}")
    if summary.weekly:
        print("Weekly totals:")
        for week in summary.weekly:
            print(
                f"  - {week.period_start.isoformat()}..{week.period_end.isoformat()}: "
                f"lines={week.lines_count}, total_payable={money_string(week.total_payable)}, "
                f"ndfl={money_string(week.ndfl_withheld)}"
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Удалить все is_imported_legacy записи перед импортом",
    )
    args = parser.parse_args()
    asyncio.run(import_csv(args.csv_path, dry_run=args.dry_run, reset=args.reset))


if __name__ == "__main__":
    main()

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
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models import (
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

PAYMENT_TYPE_MAP: dict[str, str | tuple[str, str] | tuple[str, str, str]] = {
    "Оклад": "base_pay",
    "Процент": "percent_pay",
    "Больничные и отпуска": "vacation_pay",
    "НДФЛ Начислено": "ndfl_withheld",
    "Премия": ("adjustment", "bonus", "premium"),
    "Штрафы по ревизиям": ("adjustment", "penalty", "audit_penalty"),
    "Штрафы и удержания": ("adjustment", "penalty", "manual_penalty"),
    "Депозит удержание": ("deposit", "accrual"),
    "Депозит возврат": ("deposit", "payout"),
}

REQUIRED_COLUMNS = {
    "Date",
    "Amount",
    "Employee Name",
    "Position",
    "Department",
    "Payment Type",
    "Description",
    "Year",
}


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
class LegacyEmployeeBucket:
    base_pay: Decimal = Decimal("0")
    percent_pay: Decimal = Decimal("0")
    vacation_pay: Decimal = Decimal("0")
    ndfl_withheld: Decimal = Decimal("0")
    adjustments: list[LegacyAdjustmentRow] = field(default_factory=list)
    deposits: list[LegacyDepositRow] = field(default_factory=list)
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
    ndfl_withheld: Decimal = Decimal("0")
    total_payable: Decimal = Decimal("0")


@dataclass(slots=True)
class LegacyImportSummary:
    periods_created: int = 0
    runs_created: int = 0
    lines_created: int = 0
    adjustments_created: int = 0
    deposits_created: int = 0
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
        await clear_existing_legacy_rows(session, run, period, employee_ids)

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
            "ndfl_withheld": money_string(weekly_summary.ndfl_withheld),
            "total_payable": money_string(weekly_summary.total_payable),
        }
        summary.weekly.append(weekly_summary)
        await session.flush()

    return summary


async def import_csv(path: Path, dry_run: bool = False) -> LegacyImportSummary:
    async with AsyncSessionLocal() as session:
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
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        missing_columns = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing_columns:
            raise ValueError(f"CSV missing columns: {', '.join(sorted(missing_columns))}")
        for line_no, row in enumerate(reader, start=2):
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
) -> None:
    await session.execute(delete(PayrollLine).where(PayrollLine.run_id == run.id))
    await session.execute(delete(DepositTransaction).where(DepositTransaction.run_id == run.id))
    if employee_ids:
        await session.execute(
            delete(PayrollAdjustment).where(
                PayrollAdjustment.created_by_label == "import:legacy",
                PayrollAdjustment.employee_id.in_(employee_ids),
                PayrollAdjustment.work_date >= period.start_date,
                PayrollAdjustment.work_date <= period.end_date,
            )
        )


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
    summary.total_payable += money(total_payable)


def clean(value: Any) -> str:
    return str(value or "").strip()


def money(value: Decimal) -> Decimal:
    return value.quantize(MONEY)


def money_string(value: Decimal) -> str:
    return f"{money(value):.2f}"


def print_report(summary: LegacyImportSummary, *, dry_run: bool) -> None:
    prefix = "DRY RUN: would import" if dry_run else "Imported"
    print(
        f"{prefix} {summary.runs_created} runs, {summary.lines_created} lines, "
        f"{summary.adjustments_created} adjustments, {summary.deposits_created} deposits"
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
    args = parser.parse_args()
    asyncio.run(import_csv(args.csv_path, dry_run=args.dry_run))


if __name__ == "__main__":
    main()

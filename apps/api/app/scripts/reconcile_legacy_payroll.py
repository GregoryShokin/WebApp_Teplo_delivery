"""Сверка импортированных зарплат с исходным CSV.

Usage:
    python -m app.scripts.reconcile_legacy_payroll /path/to/file.csv
        [--employee NAME] [--week DD.MM.YYYY] [--verbose]

Options:
  --employee NAME   Сверить только указанного сотрудника.
  --week DD.MM.YYYY Сверить только указанную неделю (вт-пн, дата любая внутри).
  --verbose         Показывать совпадающие строки тоже.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models import (
    AccumulationFundTransaction,
    DepositTransaction,
    Employee,
    PayrollAdjustment,
    PayrollAdjustmentCategory,
    PayrollLine,
    PayrollPeriod,
    PayrollRun,
)
from app.scripts.import_legacy_payroll import (
    DEPARTMENT_NAME,
    LEGACY_FUND_COMMENT_PREFIX,
    PAYMENT_TYPE_MAP,
    clean,
    iter_legacy_csv_rows,
    money,
    money_string,
    parse_amount,
    parse_date,
    payroll_period_for_date,
)

AggregateKey = tuple[date, str, str]
PaymentTypeMapping = str | tuple[str, str] | tuple[str, str, str]

EXPECTED_SKIPPED_PAYMENT_TYPES: set[str] = set()
LINE_BUCKETS = ("base_pay", "percent_pay", "vacation_pay", "ndfl_withheld")
TOLERANCE = Decimal("0.01")
SEPARATOR = "=" * 60


@dataclass(frozen=True, slots=True)
class ReconcileFilters:
    employee: str | None = None
    week_start: date | None = None
    week_end: date | None = None


@dataclass(slots=True)
class PaymentTypeStats:
    rows: int = 0
    amount: Decimal = Decimal("0")


@dataclass(slots=True)
class CsvReadResult:
    aggregate: dict[AggregateKey, Decimal]
    relevant_rows: int = 0
    relevant_amount: Decimal = Decimal("0")
    expected_skipped: dict[str, PaymentTypeStats] = field(default_factory=dict)
    unexpected_skipped: dict[str, PaymentTypeStats] = field(default_factory=dict)
    employee_rows: Counter[str] = field(default_factory=Counter)
    periods: set[tuple[date, date]] = field(default_factory=set)


@dataclass(slots=True)
class DbReadResult:
    aggregate: dict[AggregateKey, Decimal]
    records_count: int = 0
    amount: Decimal = Decimal("0")
    periods: set[tuple[date, date]] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class Mismatch:
    key: AggregateKey
    csv_value: Decimal
    db_value: Decimal
    delta: Decimal


@dataclass(slots=True)
class ReconcileResult:
    csv_path: Path
    csv: CsvReadResult
    db: DbReadResult
    mismatches: list[Mismatch]
    matches: list[tuple[AggregateKey, Decimal]]
    employees_not_in_db: dict[str, int]
    filters: ReconcileFilters

    @property
    def has_discrepancies(self) -> bool:
        return bool(self.mismatches or self.csv.unexpected_skipped)


def read_csv_expected(path: Path, filters: ReconcileFilters) -> CsvReadResult:
    aggregate: defaultdict[AggregateKey, Decimal] = defaultdict(lambda: Decimal("0"))
    result = CsvReadResult(aggregate={})

    for _, row in iter_legacy_csv_rows(path):
        if clean(row.get("Department")) != DEPARTMENT_NAME:
            continue

        employee_name = clean(row.get("Employee Name"))
        work_date = parse_date(row["Date"])
        period_start, period_end = payroll_period_for_date(work_date)
        if filters.employee and employee_name != filters.employee:
            continue
        if filters.week_start and period_start != filters.week_start:
            continue

        amount = parse_amount(row["Amount"])
        payment_type = clean(row.get("Payment Type"))
        result.employee_rows[employee_name] += 1
        result.periods.add((period_start, period_end))

        if payment_type in EXPECTED_SKIPPED_PAYMENT_TYPES:
            add_payment_type_stats(result.expected_skipped, payment_type, amount)
            continue

        mapping = PAYMENT_TYPE_MAP.get(payment_type)
        if mapping is None:
            add_payment_type_stats(result.unexpected_skipped, payment_type or "<blank>", amount)
            continue

        bucket = bucket_for_mapping(mapping)
        expected_amount = amount_for_mapping(mapping, amount)
        aggregate[(period_start, employee_name, bucket)] += expected_amount
        result.relevant_rows += 1
        result.relevant_amount += expected_amount

    result.aggregate = quantized_aggregate(aggregate)
    result.relevant_amount = money(result.relevant_amount)
    return result


def add_payment_type_stats(
    stats_by_type: dict[str, PaymentTypeStats],
    payment_type: str,
    amount: Decimal,
) -> None:
    stats = stats_by_type.setdefault(payment_type, PaymentTypeStats())
    stats.rows += 1
    stats.amount += amount


def bucket_for_mapping(mapping: PaymentTypeMapping) -> str:
    if isinstance(mapping, str):
        return mapping
    if mapping[0] == "adjustment":
        _, adjustment_type, category_code = mapping
        return f"adjustment:{adjustment_type}:{category_code}"
    if mapping[0] == "deposit":
        _, transaction_type = mapping
        return f"deposit:{transaction_type}"
    if mapping[0] == "fund":
        _, transaction_type = mapping
        return f"fund:{transaction_type}"
    raise ValueError(f"Unsupported payment mapping: {mapping!r}")


def amount_for_mapping(mapping: PaymentTypeMapping, amount: Decimal) -> Decimal:
    if isinstance(mapping, str):
        return abs(amount) if mapping == "ndfl_withheld" else amount
    return abs(amount)


async def read_db_actual(session: AsyncSession, filters: ReconcileFilters) -> DbReadResult:
    legacy_periods = await load_legacy_periods(session, filters)
    legacy_period_starts = {period_start for period_start, _ in legacy_periods}
    result = DbReadResult(aggregate={}, periods=set(legacy_periods))
    aggregate: defaultdict[AggregateKey, Decimal] = defaultdict(lambda: Decimal("0"))

    await add_payroll_line_aggregates(session, filters, aggregate, result)
    await add_adjustment_aggregates(
        session,
        filters,
        legacy_periods,
        legacy_period_starts,
        aggregate,
        result,
    )
    await add_deposit_aggregates(session, filters, aggregate, result)
    await add_fund_aggregates(session, filters, legacy_periods, aggregate, result)

    result.aggregate = quantized_aggregate(aggregate)
    result.amount = money(result.amount)
    return result


async def load_legacy_periods(
    session: AsyncSession,
    filters: ReconcileFilters,
) -> list[tuple[date, date]]:
    query = (
        select(PayrollPeriod.start_date, PayrollPeriod.end_date)
        .select_from(PayrollRun)
        .join(PayrollPeriod, PayrollRun.period_id == PayrollPeriod.id)
        .where(PayrollRun.is_imported_legacy.is_(True))
    )
    if filters.week_start:
        query = query.where(PayrollPeriod.start_date == filters.week_start)

    rows = (await session.execute(query)).all()
    return sorted({(period_start, period_end) for period_start, period_end in rows})


async def add_payroll_line_aggregates(
    session: AsyncSession,
    filters: ReconcileFilters,
    aggregate: defaultdict[AggregateKey, Decimal],
    result: DbReadResult,
) -> None:
    query = (
        select(
            PayrollPeriod.start_date,
            PayrollPeriod.end_date,
            Employee.full_name,
            PayrollLine.base_pay,
            PayrollLine.percent_pay,
            PayrollLine.vacation_pay,
            PayrollLine.ndfl_withheld,
        )
        .select_from(PayrollLine)
        .join(PayrollRun, PayrollLine.run_id == PayrollRun.id)
        .join(PayrollPeriod, PayrollRun.period_id == PayrollPeriod.id)
        .join(Employee, PayrollLine.employee_id == Employee.id)
        .where(PayrollRun.is_imported_legacy.is_(True))
    )
    if filters.employee:
        query = query.where(Employee.full_name == filters.employee)
    if filters.week_start:
        query = query.where(PayrollPeriod.start_date == filters.week_start)

    rows = (await session.execute(query)).all()
    for row in rows:
        period_start, period_end, employee_name, *amounts = row
        result.periods.add((period_start, period_end))
        result.records_count += 1
        for bucket, raw_amount in zip(LINE_BUCKETS, amounts, strict=True):
            amount = decimal_value(raw_amount)
            result.amount += amount
            if amount:
                aggregate[(period_start, employee_name, bucket)] += amount


async def add_adjustment_aggregates(
    session: AsyncSession,
    filters: ReconcileFilters,
    legacy_periods: list[tuple[date, date]],
    legacy_period_starts: set[date],
    aggregate: defaultdict[AggregateKey, Decimal],
    result: DbReadResult,
) -> None:
    if not legacy_periods:
        return

    first_date = min(period_start for period_start, _ in legacy_periods)
    last_date = max(period_end for _, period_end in legacy_periods)
    query = (
        select(
            PayrollAdjustment.work_date,
            Employee.full_name,
            PayrollAdjustment.type,
            PayrollAdjustmentCategory.code,
            PayrollAdjustment.custom_label,
            PayrollAdjustment.amount,
        )
        .select_from(PayrollAdjustment)
        .join(Employee, PayrollAdjustment.employee_id == Employee.id)
        .join(
            PayrollAdjustmentCategory,
            PayrollAdjustment.category_id == PayrollAdjustmentCategory.id,
            isouter=True,
        )
        .where(
            PayrollAdjustment.created_by_label == "import:legacy",
            PayrollAdjustment.work_date >= first_date,
            PayrollAdjustment.work_date <= last_date,
        )
    )
    if filters.employee:
        query = query.where(Employee.full_name == filters.employee)

    rows = (await session.execute(query)).all()
    for work_date, employee_name, adjustment_type, category_code, custom_label, raw_amount in rows:
        period_start, period_end = payroll_period_for_date(work_date)
        if period_start not in legacy_period_starts:
            continue
        if filters.week_start and period_start != filters.week_start:
            continue

        code = category_code or custom_label or "<missing_category>"
        bucket = f"adjustment:{adjustment_type}:{code}"
        amount = decimal_value(raw_amount)
        result.periods.add((period_start, period_end))
        result.records_count += 1
        result.amount += amount
        if amount:
            aggregate[(period_start, employee_name, bucket)] += amount


async def add_deposit_aggregates(
    session: AsyncSession,
    filters: ReconcileFilters,
    aggregate: defaultdict[AggregateKey, Decimal],
    result: DbReadResult,
) -> None:
    query = (
        select(
            PayrollPeriod.start_date,
            PayrollPeriod.end_date,
            Employee.full_name,
            DepositTransaction.transaction_type,
            DepositTransaction.amount,
        )
        .select_from(DepositTransaction)
        .join(PayrollRun, DepositTransaction.run_id == PayrollRun.id)
        .join(PayrollPeriod, PayrollRun.period_id == PayrollPeriod.id)
        .join(Employee, DepositTransaction.employee_id == Employee.id)
        .where(
            DepositTransaction.run_id.is_not(None),
            PayrollRun.is_imported_legacy.is_(True),
        )
    )
    if filters.employee:
        query = query.where(Employee.full_name == filters.employee)
    if filters.week_start:
        query = query.where(PayrollPeriod.start_date == filters.week_start)

    rows = (await session.execute(query)).all()
    for period_start, period_end, employee_name, transaction_type, raw_amount in rows:
        amount = decimal_value(raw_amount)
        bucket = f"deposit:{transaction_type}"
        result.periods.add((period_start, period_end))
        result.records_count += 1
        result.amount += amount
        if amount:
            aggregate[(period_start, employee_name, bucket)] += amount


async def add_fund_aggregates(
    session: AsyncSession,
    filters: ReconcileFilters,
    legacy_periods: list[tuple[date, date]],
    aggregate: defaultdict[AggregateKey, Decimal],
    result: DbReadResult,
) -> None:
    if not legacy_periods:
        return

    first_date = min(period_start for period_start, _ in legacy_periods)
    last_date = max(period_end for _, period_end in legacy_periods)
    first_at = datetime.combine(first_date, time.min, tzinfo=UTC)
    last_at = datetime.combine(last_date + timedelta(days=1), time.min, tzinfo=UTC)
    query = (
        select(
            AccumulationFundTransaction.created_at,
            Employee.full_name,
            AccumulationFundTransaction.transaction_type,
            AccumulationFundTransaction.amount,
        )
        .select_from(AccumulationFundTransaction)
        .join(Employee, AccumulationFundTransaction.employee_id == Employee.id)
        .where(
            AccumulationFundTransaction.transaction_type.in_(("accrual", "payout", "forfeit")),
            AccumulationFundTransaction.comment.like(f"{LEGACY_FUND_COMMENT_PREFIX}%"),
            AccumulationFundTransaction.created_at >= first_at,
            AccumulationFundTransaction.created_at < last_at,
        )
    )
    if filters.employee:
        query = query.where(Employee.full_name == filters.employee)

    rows = (await session.execute(query)).all()
    for created_at, employee_name, transaction_type, raw_amount in rows:
        work_date = created_at.date()
        period_start, period_end = payroll_period_for_date(work_date)
        if filters.week_start and period_start != filters.week_start:
            continue

        amount = decimal_value(raw_amount)
        bucket = f"fund:{transaction_type}"
        result.periods.add((period_start, period_end))
        result.records_count += 1
        result.amount += amount
        if amount:
            aggregate[(period_start, employee_name, bucket)] += amount


def decimal_value(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def quantized_aggregate(
    aggregate: dict[AggregateKey, Decimal] | defaultdict[AggregateKey, Decimal],
) -> dict[AggregateKey, Decimal]:
    return {
        key: amount
        for key, raw_amount in aggregate.items()
        if (amount := money(raw_amount)) != Decimal("0")
    }


async def reconcile(
    csv_path: Path,
    filters: ReconcileFilters,
    *,
    verbose: bool,
) -> ReconcileResult:
    csv_result = read_csv_expected(csv_path, filters)
    async with AsyncSessionLocal() as session:
        db_employee_names = set(await session.scalars(select(Employee.full_name)))
        db_result = await read_db_actual(session, filters)

    employees_not_in_db = {
        name: rows
        for name, rows in sorted(csv_result.employee_rows.items())
        if name and name not in db_employee_names
    }
    mismatches, matches = compare_aggregates(csv_result.aggregate, db_result.aggregate, verbose)
    return ReconcileResult(
        csv_path=csv_path,
        csv=csv_result,
        db=db_result,
        mismatches=mismatches,
        matches=matches,
        employees_not_in_db=employees_not_in_db,
        filters=filters,
    )


def compare_aggregates(
    csv_aggregate: dict[AggregateKey, Decimal],
    db_aggregate: dict[AggregateKey, Decimal],
    verbose: bool,
) -> tuple[list[Mismatch], list[tuple[AggregateKey, Decimal]]]:
    mismatches: list[Mismatch] = []
    matches: list[tuple[AggregateKey, Decimal]] = []
    for key in sorted(set(csv_aggregate) | set(db_aggregate), key=sort_key):
        csv_value = csv_aggregate.get(key, Decimal("0"))
        db_value = db_aggregate.get(key, Decimal("0"))
        delta = db_value - csv_value
        if abs(delta) > TOLERANCE:
            mismatches.append(Mismatch(key, csv_value, db_value, delta))
        elif verbose:
            matches.append((key, csv_value))
    return mismatches, matches


def sort_key(key: AggregateKey) -> tuple[date, str, str]:
    period_start, employee_name, bucket = key
    return period_start, employee_name.lower(), bucket


def print_report(result: ReconcileResult, *, verbose: bool) -> None:
    print(SEPARATOR)
    print("СВЕРКА ИМПОРТА ЗАРПЛАТ")
    print(f"CSV: {result.csv_path}")
    print(f"Период: {format_report_period(result)}")
    if result.filters.employee:
        print(f"Сотрудник: {result.filters.employee}")
    if result.filters.week_start and result.filters.week_end:
        print(f"Неделя: {format_period(result.filters.week_start, result.filters.week_end)}")
    print(SEPARATOR)
    print()
    print(
        f"Итого CSV (relevant): {result.csv.relevant_rows} строк, "
        f"сумма Amount = {money_string(result.csv.relevant_amount)} ₽"
    )
    print(
        f"Итого БД (legacy):     {result.db.records_count} записей, "
        f"сумма = {money_string(result.db.amount)} ₽"
    )
    print()
    print(f"--- Расхождения ({len(result.mismatches)} штук) ---")
    print()
    if result.mismatches:
        for mismatch in result.mismatches:
            print_mismatch(mismatch)
    else:
        print("Расхождений по агрегатам нет.")
        print()

    if verbose:
        print(f"--- Совпадения ({len(result.matches)} штук) ---")
        print()
        for key, value in result.matches:
            period_start, employee_name, bucket = key
            print(
                f"✓ {format_period(period_start)} | {employee_name} | {bucket}: "
                f"{money_string(value)}"
            )
        print()

    print_expected_skipped(result.csv.expected_skipped)
    print_unexpected_skipped(result.csv.unexpected_skipped)
    print_employees_not_in_db(result.employees_not_in_db)
    print(SEPARATOR)
    print(
        f"ИТОГ: {len(result.mismatches)} расхождений "
        f"в {len(result.mismatches)} комбинациях сотрудник×неделя×тип"
    )
    if result.csv.unexpected_skipped:
        print(f"Неизвестные типы CSV: {len(result.csv.unexpected_skipped)}")
    print(SEPARATOR)


def print_mismatch(mismatch: Mismatch) -> None:
    period_start, employee_name, bucket = mismatch.key
    direction = delta_direction(mismatch.csv_value, mismatch.db_value, mismatch.delta)
    print(f"📅 {format_period(period_start)} | {employee_name} | {bucket}")
    print(f"  CSV: {money_string(mismatch.csv_value)}")
    print(f"  БД:   {money_string(mismatch.db_value)}")
    print(f"  Δ:   {money_string(mismatch.delta)} ({direction})")
    print()


def delta_direction(csv_value: Decimal, db_value: Decimal, delta: Decimal) -> str:
    if db_value == Decimal("0") and csv_value != Decimal("0"):
        return "отсутствует в БД"
    if csv_value == Decimal("0") and db_value != Decimal("0"):
        return "отсутствует в CSV"
    if delta < Decimal("0"):
        return "БД меньше"
    return "БД больше"


def print_expected_skipped(stats_by_type: dict[str, PaymentTypeStats]) -> None:
    print("--- Пропущенные типы (известно, ожидаемое поведение) ---")
    if not stats_by_type:
        print("Нет.")
        print()
        return
    for payment_type in sorted(stats_by_type):
        stats = stats_by_type[payment_type]
        print(f"- «{payment_type}»: {stats.rows} строк в CSV, сумма {money_string(stats.amount)} ₽")
    print()


def print_unexpected_skipped(stats_by_type: dict[str, PaymentTypeStats]) -> None:
    if not stats_by_type:
        return
    print("--- Неизвестные типы (не expected_skipped, требуют проверки) ---")
    for payment_type in sorted(stats_by_type):
        stats = stats_by_type[payment_type]
        print(f"- «{payment_type}»: {stats.rows} строк в CSV, сумма {money_string(stats.amount)} ₽")
    print()


def print_employees_not_in_db(employees_not_in_db: dict[str, int]) -> None:
    print("--- Сотрудники в CSV но не в БД ---")
    if not employees_not_in_db:
        print("Нет.")
        print()
        return
    for employee_name, rows in employees_not_in_db.items():
        print(f"- {employee_name} ({rows} строк)")
    print()


def format_report_period(result: ReconcileResult) -> str:
    periods = result.csv.periods | result.db.periods
    if result.filters.week_start and result.filters.week_end:
        return format_period(result.filters.week_start, result.filters.week_end)
    if not periods:
        return "нет данных"
    first_period_start = min(period_start for period_start, _ in periods)
    last_period_end = max(end for _, end in periods)
    return f"{first_period_start} ... {last_period_end}"


def format_period(period_start: date, period_end: date | None = None) -> str:
    end = period_end or period_start + timedelta(days=6)
    return f"{period_start}..{end}"


def parse_filters(args: argparse.Namespace) -> ReconcileFilters:
    week_start: date | None = None
    week_end: date | None = None
    if args.week:
        week_start, week_end = payroll_period_for_date(parse_date(args.week))
    return ReconcileFilters(
        employee=clean(args.employee) or None,
        week_start=week_start,
        week_end=week_end,
    )


async def async_main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--employee")
    parser.add_argument("--week")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    filters = parse_filters(args)
    result = await reconcile(args.csv_path, filters, verbose=args.verbose)
    print_report(result, verbose=args.verbose)
    return 1 if result.has_discrepancies else 0


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()

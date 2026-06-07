from __future__ import annotations

import argparse
import asyncio
import csv
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models import (
    AccumulationFundAccount,
    AccumulationFundTransaction,
    AgentAction,
    AgentRun,
    DepositAccount,
    DepositTransaction,
    Employee,
    EmployeeRoleAssignment,
    PayrollRate,
    PayrollRoleCategoryAvailability,
)
from app.services.staff_taxonomy import target_position_for_payroll_role

MONEY = Decimal("0.01")
DEFAULT_RATE_EFFECTIVE_FROM = date(2026, 1, 1)
FUND_EXPORT_INITIAL_BALANCE_COMMENT = "Импорт из Расчет зарплат NEW / Выгрузка"

ROLE_BY_SHEET_VALUE = {
    "администратор": "administrator",
    "сушист": "sushi",
    "пиццерист": "pizza",
    "шаурмист": "shawarma",
    "заготовщик": "prep",
}
CATEGORY_BY_SHEET_VALUE = {
    "1": "category_1",
    "2": "category_2",
    "3": "category_3",
    "4": "category_4",
    "5": "intern",
    "6": "freelancer",
}
COOKING_STATIONS = frozenset({"sushi", "pizza", "shawarma"})


@dataclass(frozen=True, slots=True)
class RoleSnapshot:
    payroll_role: str
    category: str
    effective_from: date
    is_primary: bool


@dataclass(frozen=True, slots=True)
class EmployeeSnapshot:
    full_name: str
    position: str
    category: str
    default_cooking_station: str | None
    is_senior: bool
    is_deputy_senior: bool
    hire_date: date
    roles: tuple[RoleSnapshot, ...]


@dataclass(frozen=True, slots=True)
class DepositSnapshot:
    full_name: str
    collected: Decimal
    target: Decimal
    required_withholding: Decimal
    current_withholding: Decimal
    dismissal_note: str | None
    payout_coefficient: str | None


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    employees: dict[str, EmployeeSnapshot]
    deposits: dict[str, DepositSnapshot]
    fund_accruals: dict[tuple[int, str], Decimal]
    fund_writeoffs: dict[tuple[int, str], Decimal]
    sheet_date: date


def parse_source(staff_csv: Path, export_csv: Path) -> SourceSnapshot:
    employees, deposits, sheet_date = parse_staff_sheet(staff_csv)
    fund_accruals, fund_writeoffs = parse_export_sheet(export_csv)
    return SourceSnapshot(
        employees=employees,
        deposits=deposits,
        fund_accruals=fund_accruals,
        fund_writeoffs=fund_writeoffs,
        sheet_date=sheet_date,
    )


def parse_staff_sheet(
    path: Path,
) -> tuple[dict[str, EmployeeSnapshot], dict[str, DepositSnapshot], date]:
    with path.open(encoding="utf-8", newline="") as file:
        rows = list(csv.reader(file))

    grouped_roles: dict[str, list[tuple[str, str, date]]] = defaultdict(list)
    bonuses: dict[str, set[str]] = defaultdict(set)
    sheet_dates: list[date] = []
    deposits: dict[str, DepositSnapshot] = {}

    for raw_row in rows[2:]:
        row = padded(raw_row, 17)
        staff_name = clean(row[0])
        if staff_name:
            payroll_role = role_code(row[1])
            category = category_code(row[2])
            hired_at = parse_date(row[4])
            grouped_roles[staff_name].append((payroll_role, category, hired_at))
            sheet_dates.append(parse_date(row[5]))
            bonus_text = normalize(row[3])
            if "старш" in bonus_text:
                bonuses[staff_name].add("senior")
            if "зам" in bonus_text:
                bonuses[staff_name].add("deputy_senior")

        deposit_name = clean(row[9])
        if deposit_name:
            deposits[deposit_name] = DepositSnapshot(
                full_name=deposit_name,
                collected=parse_money(row[11]),
                target=parse_money(row[12]),
                required_withholding=parse_money(row[13]),
                current_withholding=parse_money(row[14]),
                dismissal_note=clean(row[15]) or None,
                payout_coefficient=clean(row[16]) or None,
            )

    employees: dict[str, EmployeeSnapshot] = {}
    for full_name, role_rows in grouped_roles.items():
        roles = tuple(
            RoleSnapshot(
                payroll_role=payroll_role,
                category=category,
                effective_from=effective_from,
                is_primary=index == 0,
            )
            for index, (payroll_role, category, effective_from) in enumerate(role_rows)
        )
        primary = roles[0]
        employees[full_name] = EmployeeSnapshot(
            full_name=full_name,
            position=target_position_for_payroll_role(primary.payroll_role),
            category=primary.category,
            default_cooking_station=(
                primary.payroll_role if primary.payroll_role in COOKING_STATIONS else None
            ),
            is_senior="senior" in bonuses[full_name],
            is_deputy_senior="deputy_senior" in bonuses[full_name],
            hire_date=min(role.effective_from for role in roles),
            roles=roles,
        )

    return employees, deposits, max(sheet_dates) if sheet_dates else date.today()


def parse_export_sheet(
    path: Path,
) -> tuple[dict[tuple[int, str], Decimal], dict[tuple[int, str], Decimal]]:
    fund_accruals: dict[tuple[int, str], Decimal] = defaultdict(Decimal)
    fund_writeoffs: dict[tuple[int, str], Decimal] = defaultdict(Decimal)
    with path.open(encoding="utf-8", newline="") as file:
        for raw_row in csv.reader(file):
            row = padded(raw_row, 8)
            kind = clean(row[5])
            name = clean(row[2])
            year_text = clean(row[7])
            if not name or not year_text.isdigit():
                continue
            year = int(year_text)
            amount = parse_money(row[1])
            if kind == "Накопительный фонд":
                fund_accruals[(year, name)] += amount
            elif kind == "Списание накоплений":
                fund_writeoffs[(year, name)] += amount
    return dict(fund_accruals), dict(fund_writeoffs)


async def apply_source(
    source: SourceSnapshot,
    *,
    fund_year: int,
    dry_run: bool,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    import_date = source.sheet_date
    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "source_date": import_date.isoformat(),
        "fund_year": fund_year,
        "employees_updated": 0,
        "roles_upserted": 0,
        "roles_closed": 0,
        "deposit_accounts_updated": 0,
        "deposit_transactions_created": 0,
        "fund_accounts_updated": 0,
        "fund_initial_balances_removed": 0,
        "fund_export_balances_skipped": 0,
        "missing_employees": [],
        "deposit_dismissal_notes": [],
    }

    async with AsyncSessionLocal() as session:
        agent_run = AgentRun(
            id=uuid.uuid4(),
            agent_name="staff_google_sheet_import",
            started_at=now,
            finished_at=None,
            status="running",
            params={
                "source": "Расчет зарплат NEW",
                "sheets": ["Штат", "Выгрузка"],
                "source_date": import_date.isoformat(),
                "fund_year": fund_year,
                "dry_run": dry_run,
            },
            result={},
        )
        session.add(agent_run)
        await session.flush()

        await ensure_prep_intern_setup(session)
        employees = await load_employees(session, source.employees.keys())

        for full_name, employee_snapshot in source.employees.items():
            employee = employees.get(full_name)
            if employee is None:
                summary["missing_employees"].append(full_name)
                continue

            before = await db_snapshot(session, employee, fund_year)
            apply_employee_snapshot(employee, employee_snapshot, now)
            role_counts = await sync_roles(session, employee, employee_snapshot.roles, import_date)
            summary["roles_upserted"] += role_counts["upserted"]
            summary["roles_closed"] += role_counts["closed"]

            deposit = source.deposits.get(full_name)
            if deposit is not None:
                deposit_result = await sync_deposit(session, employee, deposit, now)
                summary["deposit_accounts_updated"] += 1
                summary["deposit_transactions_created"] += deposit_result["transactions_created"]
                if deposit.dismissal_note or deposit.payout_coefficient:
                    summary["deposit_dismissal_notes"].append(
                        {
                            "employee": full_name,
                            "note": deposit.dismissal_note,
                            "payout_coefficient": deposit.payout_coefficient,
                        }
                    )

            fund_result = await sync_fund(
                session,
                employee,
                accumulated=source.fund_accruals.get((fund_year, full_name), Decimal("0")),
                forfeited=source.fund_writeoffs.get((fund_year, full_name), Decimal("0")),
                year=fund_year,
                now=now,
            )
            summary["fund_accounts_updated"] += 1
            summary["fund_initial_balances_removed"] += fund_result["initial_balances_removed"]
            summary["fund_export_balances_skipped"] += fund_result["export_balances_skipped"]

            after = await db_snapshot(session, employee, fund_year)
            session.add(
                AgentAction(
                    id=uuid.uuid4(),
                    agent_run_id=agent_run.id,
                    action_type="staff_google_sheet_import_employee",
                    target_table="employee",
                    target_id=employee.id,
                    before_value=before,
                    after_value=after,
                )
            )
            summary["employees_updated"] += 1

        agent_run.finished_at = now
        agent_run.status = "success"
        agent_run.result = summary

        if dry_run:
            await session.rollback()
        else:
            await session.commit()

    return summary


async def load_employees(
    session: AsyncSession,
    full_names: Any,
) -> dict[str, Employee]:
    rows = await session.scalars(select(Employee).where(Employee.full_name.in_(list(full_names))))
    return {employee.full_name: employee for employee in rows.all()}


def apply_employee_snapshot(employee: Employee, snapshot: EmployeeSnapshot, now: datetime) -> None:
    employee.position = snapshot.position
    employee.category = snapshot.category
    employee.default_cooking_station = snapshot.default_cooking_station
    employee.is_senior = snapshot.is_senior
    employee.is_deputy_senior = snapshot.is_deputy_senior
    employee.status = "active"
    employee.hire_date = snapshot.hire_date
    employee.tenure_started_at = snapshot.hire_date
    employee.fire_date = None
    employee.fire_reason = None
    employee.requires_role_review = False
    employee.role_review_payload = None
    employee.updated_at = now


async def sync_roles(
    session: AsyncSession,
    employee: Employee,
    roles: tuple[RoleSnapshot, ...],
    import_date: date,
) -> dict[str, int]:
    result = {"upserted": 0, "closed": 0}
    active = list(
        (
            await session.scalars(
                select(EmployeeRoleAssignment).where(
                    EmployeeRoleAssignment.employee_id == employee.id,
                    EmployeeRoleAssignment.effective_from <= import_date,
                    or_(
                        EmployeeRoleAssignment.effective_to.is_(None),
                        EmployeeRoleAssignment.effective_to > import_date,
                    ),
                )
            )
        ).all()
    )
    by_role = {assignment.payroll_role: assignment for assignment in active}
    target_roles = {role.payroll_role for role in roles}

    for assignment in active:
        assignment.is_primary = False
        if assignment.payroll_role not in target_roles:
            assignment.effective_to = import_date
            result["closed"] += 1
    await session.flush()

    target_assignments: list[tuple[EmployeeRoleAssignment, RoleSnapshot]] = []
    for role in roles:
        assignment = by_role.get(role.payroll_role)
        if assignment is None or assignment.effective_to is not None:
            assignment = EmployeeRoleAssignment(
                id=uuid.uuid4(),
                employee_id=employee.id,
                payroll_role=role.payroll_role,
                category=role.category,
                is_primary=False,
                is_substitute=False,
                effective_from=role.effective_from,
                effective_to=None,
            )
            session.add(assignment)
        else:
            assignment.category = role.category
            assignment.is_substitute = False
            assignment.effective_from = role.effective_from
            assignment.effective_to = None
        result["upserted"] += 1
        target_assignments.append((assignment, role))

    await session.flush()
    for assignment, role in target_assignments:
        assignment.is_primary = role.is_primary
    await session.flush()
    return result


async def sync_deposit(
    session: AsyncSession,
    employee: Employee,
    deposit: DepositSnapshot,
    now: datetime,
) -> dict[str, int]:
    employee.deposit_target_override = deposit.target.quantize(MONEY)
    employee.deposit_withholding_override = deposit.required_withholding.quantize(MONEY)
    employee.deposit_excluded = False
    employee.deposit_excluded_until = None
    employee.updated_at = now

    account = await session.scalar(
        select(DepositAccount).where(DepositAccount.employee_id == employee.id).with_for_update()
    )
    if account is None:
        account = DepositAccount(
            id=uuid.uuid4(),
            employee_id=employee.id,
            balance=Decimal("0"),
            initial_balance=Decimal("0"),
            last_updated=now,
        )
        session.add(account)

    amount = deposit.collected.quantize(MONEY)
    account.balance = amount
    account.initial_balance = amount
    account.last_updated = now

    existing_transaction = await session.scalar(
        select(DepositTransaction.id).where(DepositTransaction.employee_id == employee.id).limit(1)
    )
    if existing_transaction is None and amount > 0:
        session.add(
            DepositTransaction(
                id=uuid.uuid4(),
                employee_id=employee.id,
                run_id=None,
                transaction_type="accrual",
                amount=amount,
                created_at=now,
            )
        )
        return {"transactions_created": 1}
    return {"transactions_created": 0}


async def sync_fund(
    session: AsyncSession,
    employee: Employee,
    *,
    accumulated: Decimal,
    forfeited: Decimal,
    year: int,
    now: datetime,
) -> dict[str, int]:
    transactions = (
        await session.scalars(
            select(AccumulationFundTransaction).where(
                AccumulationFundTransaction.employee_id == employee.id,
                AccumulationFundTransaction.year == year,
                AccumulationFundTransaction.transaction_type == "initial_balance",
                AccumulationFundTransaction.comment == FUND_EXPORT_INITIAL_BALANCE_COMMENT,
            )
        )
    ).all()
    account_ids = {transaction.account_id for transaction in transactions}
    for transaction in transactions:
        await session.delete(transaction)
    if transactions:
        await session.flush()
    for account_id in account_ids:
        await recalculate_fund_account(session, account_id, now)

    return {
        "initial_balances_removed": len(transactions),
        "export_balances_skipped": int(accumulated > 0 or forfeited > 0),
    }


async def recalculate_fund_account(
    session: AsyncSession,
    account_id: uuid.UUID,
    now: datetime,
) -> None:
    account = await session.get(AccumulationFundAccount, account_id)
    if account is None:
        return
    transactions = (
        await session.scalars(
            select(AccumulationFundTransaction).where(
                AccumulationFundTransaction.account_id == account_id
            )
        )
    ).all()
    accumulated = Decimal("0")
    paid_out = Decimal("0")
    forfeited = Decimal("0")
    for transaction in transactions:
        amount = transaction.amount.quantize(MONEY)
        if transaction.transaction_type in {"accrual", "initial_balance"}:
            accumulated += amount
        elif transaction.transaction_type == "payout":
            paid_out += amount
        elif transaction.transaction_type == "forfeit":
            forfeited += amount

    account.accumulated_amount = accumulated.quantize(MONEY)
    account.paid_out_amount = paid_out.quantize(MONEY)
    account.forfeited_amount = forfeited.quantize(MONEY)
    outstanding = account.accumulated_amount - account.paid_out_amount - account.forfeited_amount
    if outstanding > 0 or (account.accumulated_amount == 0 and account.forfeited_amount == 0):
        account.status = "active"
        if account.paid_out_amount == 0:
            account.paid_out_at = None
        if account.forfeited_amount == 0:
            account.forfeited_at = None
            account.forfeit_reason = None
    elif account.forfeited_amount > 0:
        account.status = "forfeited"
        account.forfeited_at = account.forfeited_at or now
        account.forfeit_reason = account.forfeit_reason or "Списание накопительного фонда"
    elif account.paid_out_amount > 0:
        account.status = "paid_out"


async def ensure_prep_intern_setup(session: AsyncSession) -> None:
    availability = await session.get(
        PayrollRoleCategoryAvailability,
        ("Заготовщик", "intern"),
    )
    if availability is None:
        session.add(
            PayrollRoleCategoryAvailability(
                position_group="Заготовщик",
                category="intern",
                is_enabled=True,
            )
        )
    else:
        availability.is_enabled = True

    rate = await session.scalar(
        select(PayrollRate).where(
            PayrollRate.position_group == "Заготовщик",
            PayrollRate.category == "intern",
            PayrollRate.rate_type == "daily",
            PayrollRate.effective_from == DEFAULT_RATE_EFFECTIVE_FROM,
            PayrollRate.station.is_(None),
        )
    )
    if rate is None:
        session.add(
            PayrollRate(
                id=uuid.uuid4(),
                position_group="Заготовщик",
                category="intern",
                station=None,
                rate_type="daily",
                amount=Decimal("2200"),
                is_active=True,
                effective_from=DEFAULT_RATE_EFFECTIVE_FROM,
                effective_to=None,
            )
        )
    else:
        rate.amount = Decimal("2200")
        rate.is_active = True


async def db_snapshot(session: AsyncSession, employee: Employee, fund_year: int) -> dict[str, Any]:
    assignments = list(
        (
            await session.scalars(
                select(EmployeeRoleAssignment)
                .where(EmployeeRoleAssignment.employee_id == employee.id)
                .order_by(EmployeeRoleAssignment.payroll_role)
            )
        ).all()
    )
    deposit = await session.scalar(
        select(DepositAccount).where(DepositAccount.employee_id == employee.id)
    )
    fund = await session.scalar(
        select(AccumulationFundAccount).where(
            AccumulationFundAccount.employee_id == employee.id,
            AccumulationFundAccount.year == fund_year,
        )
    )
    return {
        "employee": {
            "full_name": employee.full_name,
            "position": employee.position,
            "category": employee.category,
            "default_cooking_station": employee.default_cooking_station,
            "is_senior": employee.is_senior,
            "is_deputy_senior": employee.is_deputy_senior,
            "status": employee.status,
            "hire_date": iso_date(employee.hire_date),
            "tenure_started_at": iso_date(employee.tenure_started_at),
            "deposit_target_override": money_string(employee.deposit_target_override),
            "deposit_withholding_override": money_string(employee.deposit_withholding_override),
        },
        "assignments": [
            {
                "payroll_role": assignment.payroll_role,
                "category": assignment.category,
                "is_primary": assignment.is_primary,
                "is_substitute": assignment.is_substitute,
                "effective_from": iso_date(assignment.effective_from),
                "effective_to": iso_date(assignment.effective_to),
            }
            for assignment in assignments
        ],
        "deposit": None
        if deposit is None
        else {
            "balance": money_string(deposit.balance),
            "initial_balance": money_string(deposit.initial_balance),
        },
        "fund": None
        if fund is None
        else {
            "year": fund.year,
            "accumulated_amount": money_string(fund.accumulated_amount),
            "paid_out_amount": money_string(fund.paid_out_amount),
            "forfeited_amount": money_string(fund.forfeited_amount),
            "status": fund.status,
        },
    }


def padded(row: list[str], width: int) -> list[str]:
    return [*row, *([""] * max(width - len(row), 0))]


def clean(value: str | None) -> str:
    return (value or "").replace("\xa0", " ").strip()


def normalize(value: str | None) -> str:
    return clean(value).replace("ё", "е").casefold()


def parse_money(value: str | None) -> Decimal:
    text = clean(value).replace(" ", "").replace(",", ".")
    if not text:
        return Decimal("0")
    return Decimal(text).quantize(MONEY)


def parse_date(value: str | None) -> date:
    text = clean(value)
    if not text:
        raise ValueError("Пустая дата в источнике")
    return datetime.strptime(text, "%d.%m.%Y").date()


def role_code(value: str) -> str:
    key = normalize(value)
    if key not in ROLE_BY_SHEET_VALUE:
        raise ValueError(f"Неизвестная роль из листа Штат: {value!r}")
    return ROLE_BY_SHEET_VALUE[key]


def category_code(value: str) -> str:
    key = clean(value)
    if key not in CATEGORY_BY_SHEET_VALUE:
        raise ValueError(f"Неизвестная категория из листа Штат: {value!r}")
    return CATEGORY_BY_SHEET_VALUE[key]


def money_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(Decimal(value).quantize(MONEY))


def iso_date(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Import current staff, deposits, and fund balances "
            "from payroll Google Sheet CSV exports."
        )
    )
    parser.add_argument("--staff-csv", required=True, type=Path)
    parser.add_argument("--export-csv", required=True, type=Path)
    parser.add_argument("--fund-year", type=int, default=date.today().year)
    parser.add_argument("--apply", action="store_true", help="Commit changes. Omit for dry run.")
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    source = parse_source(args.staff_csv, args.export_csv)
    summary = await apply_source(source, fund_year=args.fund_year, dry_run=not args.apply)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentAction, AgentRun, Employee, EmployeeRoleAssignment, ShiftLedgerEntry
from app.services.attendance_loader import (
    MOSCOW_TZ,
    fetch_iiko_attendance_records,
    first_text,
    first_value,
    is_work_attendance,
    parse_datetime,
)
from app.services.employee_assignments import get_assignments


@dataclass(frozen=True, slots=True)
class AttendanceSnapshot:
    employee_id: uuid.UUID
    opened_at: datetime
    closed_at: datetime | None = None
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class LedgerAssignment:
    payroll_role: str | None
    category: str | None


class ShiftLedgerNotFoundError(LookupError):
    pass


class ShiftLedgerValidationError(ValueError):
    pass


SCHEDULE_TABLE_CANDIDATES = (
    "scheduled_shift",
    "shift_schedule_entry",
    "employee_schedule_shift",
)
SCHEDULE_DATE_COLUMNS = ("work_date", "shift_date", "date")
SCHEDULE_ROLE_COLUMNS = ("payroll_role", "primary_role", "role", "station")
SCHEDULE_CATEGORY_COLUMNS = ("category", "payroll_category")


async def build_ledger_for_date(
    session: AsyncSession,
    work_date: date,
    *,
    iiko_records: Iterable[Mapping[str, Any]] | None = None,
    schedule_assignments: Mapping[uuid.UUID | str, Any] | None = None,
    primary_assignments: Mapping[uuid.UUID | str, Any] | None = None,
) -> list[ShiftLedgerEntry]:
    snapshots = await load_iiko_attendance_snapshots(
        session,
        work_date,
        iiko_records=iiko_records,
    )
    employee_ids = {snapshot.employee_id for snapshot in snapshots}
    if schedule_assignments is None:
        schedule_assignments = await load_schedule_assignments(session, work_date, employee_ids)
    if primary_assignments is None:
        primary_assignments = await load_primary_assignments(session, work_date, employee_ids)

    existing_by_employee = await load_existing_entries(session, work_date, employee_ids)
    entries: list[ShiftLedgerEntry] = []

    for snapshot in sorted(snapshots, key=lambda item: (item.opened_at, str(item.employee_id))):
        schedule_assignment = assignment_for_employee(schedule_assignments, snapshot.employee_id)
        primary_assignment = assignment_for_employee(primary_assignments, snapshot.employee_id)
        default_assignment, source = resolve_default_assignment(
            schedule_assignment,
            primary_assignment,
        )
        entry = existing_by_employee.get(snapshot.employee_id)

        if entry is None:
            entry = ShiftLedgerEntry(
                id=uuid.uuid4(),
                work_date=work_date,
                employee_id=snapshot.employee_id,
                opened_at=snapshot.opened_at,
                closed_at=snapshot.closed_at,
            )
            session.add(entry)

        entry.opened_at = snapshot.opened_at
        entry.closed_at = snapshot.closed_at
        entry.notes = snapshot.notes

        if entry.source != "manual_correction":
            entry.payroll_role = default_assignment.payroll_role
            entry.category = default_assignment.category
            entry.source = source
            entry.is_resolved = bool(entry.payroll_role and entry.category)

        entries.append(entry)

    await session.flush()
    await session.commit()
    return entries


async def manually_correct(
    session: AsyncSession,
    entry_id: uuid.UUID,
    payroll_role: str,
    category: str,
) -> ShiftLedgerEntry:
    payroll_role = payroll_role.strip()
    category = category.strip()
    if not payroll_role:
        raise ShiftLedgerValidationError("payroll_role is required")
    if not category:
        raise ShiftLedgerValidationError("category is required")

    entry = await session.get(ShiftLedgerEntry, entry_id)
    if entry is None:
        raise ShiftLedgerNotFoundError("Shift ledger entry not found")

    before = ledger_entry_snapshot(entry)
    entry.payroll_role = payroll_role
    entry.category = category
    entry.source = "manual_correction"
    entry.is_resolved = True
    after = ledger_entry_snapshot(entry)

    now = datetime.now(UTC)
    agent_run = AgentRun(
        id=uuid.uuid4(),
        agent_name="shift_ledger_manual_correction",
        finished_at=now,
        status="success",
        params={"entry_id": str(entry.id)},
        result={"source": "manual_correction"},
    )
    session.add(agent_run)
    session.add(
        AgentAction(
            id=uuid.uuid4(),
            agent_run_id=agent_run.id,
            action_type="manual_correction",
            target_table="shift_ledger_entry",
            target_id=entry.id,
            before_value=before,
            after_value=after,
        )
    )
    await session.commit()
    await session.refresh(entry)
    return entry


async def list_ledger_for_date(session: AsyncSession, work_date: date) -> list[dict[str, Any]]:
    result = await session.execute(
        select(ShiftLedgerEntry, Employee)
        .join(Employee, Employee.id == ShiftLedgerEntry.employee_id)
        .where(ShiftLedgerEntry.work_date == work_date)
        .order_by(ShiftLedgerEntry.opened_at, Employee.full_name)
    )
    return [serialize_ledger_entry(entry, employee) for entry, employee in result.all()]


async def load_iiko_attendance_snapshots(
    session: AsyncSession,
    work_date: date,
    *,
    iiko_records: Iterable[Mapping[str, Any]] | None = None,
) -> list[AttendanceSnapshot]:
    records = (
        list(iiko_records)
        if iiko_records is not None
        else await fetch_iiko_attendance_records(session, work_date, work_date)
    )
    if not records:
        return []

    employees_by_iiko_id = {
        employee.iiko_id: employee for employee in (await session.scalars(select(Employee))).all()
    }
    grouped: dict[uuid.UUID, AttendanceSnapshot] = {}

    for record in records:
        if not is_work_attendance(record):
            continue

        started_at = parse_datetime(first_value(record, "dateFrom", "OpenTime", "started_at"))
        if started_at is None or started_at.astimezone(MOSCOW_TZ).date() != work_date:
            continue

        iiko_id = first_text(record, "employeeId", "employee_id", "iiko_id", "iikoId")
        if not iiko_id:
            continue

        employee = employees_by_iiko_id.get(iiko_id)
        if employee is None:
            employee = Employee(
                id=uuid.uuid4(),
                iiko_id=iiko_id,
                full_name=first_text(record, "employeeName", "Employee", "name") or iiko_id,
                status="requires_setup",
                position=None,
                category=None,
                is_senior=False,
                is_deputy_senior=False,
                iiko_sync_at=datetime.now(UTC),
            )
            session.add(employee)
            await session.flush()
            employees_by_iiko_id[iiko_id] = employee

        ended_at = parse_datetime(first_value(record, "dateTo", "CloseTime", "ended_at"))
        note = first_text(record, "notes", "comment")
        previous = grouped.get(employee.id)
        if previous is None:
            grouped[employee.id] = AttendanceSnapshot(
                employee_id=employee.id,
                opened_at=started_at,
                closed_at=ended_at,
                notes=note or None,
            )
            continue

        closed_at = None
        if previous.closed_at is not None and ended_at is not None:
            closed_at = max(previous.closed_at, ended_at)
        notes = ";".join(part for part in (previous.notes, note) if part)
        grouped[employee.id] = AttendanceSnapshot(
            employee_id=employee.id,
            opened_at=min(previous.opened_at, started_at),
            closed_at=closed_at,
            notes=notes or None,
        )

    return list(grouped.values())


async def load_existing_entries(
    session: AsyncSession,
    work_date: date,
    employee_ids: set[uuid.UUID],
) -> dict[uuid.UUID, ShiftLedgerEntry]:
    if not employee_ids:
        return {}
    result = await session.scalars(
        select(ShiftLedgerEntry).where(
            ShiftLedgerEntry.work_date == work_date,
            ShiftLedgerEntry.employee_id.in_(employee_ids),
        )
    )
    return {entry.employee_id: entry for entry in result.all()}


async def load_primary_assignments(
    session: AsyncSession,
    work_date: date,
    employee_ids: set[uuid.UUID],
) -> dict[uuid.UUID, LedgerAssignment]:
    assignments: dict[uuid.UUID, LedgerAssignment] = {}
    for employee_id in sorted(employee_ids, key=str):
        employee_assignments = await get_assignments(session, employee_id, work_date)
        primary = next(
            (assignment for assignment in employee_assignments if assignment.is_primary),
            employee_assignments[0] if employee_assignments else None,
        )
        if primary is not None:
            assignments[employee_id] = LedgerAssignment(
                payroll_role=primary.payroll_role,
                category=primary.category,
            )
    return assignments


async def load_schedule_assignments(
    session: AsyncSession,
    work_date: date,
    employee_ids: set[uuid.UUID],
) -> dict[uuid.UUID, LedgerAssignment]:
    if not employee_ids:
        return {}
    schedule_shape = await find_schedule_shape(session)
    if schedule_shape is None:
        return {}

    table_name, date_column, role_column, category_column, uses_primary_role_id = schedule_shape
    try:
        if uses_primary_role_id:
            query = text(
                f"""
                select s.employee_id, a.payroll_role, a.category
                  from {_quote_identifier(table_name)} s
                  left join employee_role_assignment a on a.id = s.primary_role_id
                 where s.{_quote_identifier(date_column)} = :work_date
                """
            )
        else:
            category_expr = (
                f"{_quote_identifier(category_column)} as category"
                if category_column is not None
                else "null as category"
            )
            query = text(
                f"""
                select employee_id,
                       {_quote_identifier(role_column)} as payroll_role,
                       {category_expr}
                  from {_quote_identifier(table_name)}
                 where {_quote_identifier(date_column)} = :work_date
                """
            )
        rows = (await session.execute(query, {"work_date": work_date})).mappings().all()
    except SQLAlchemyError:
        return {}

    requested_ids = {str(employee_id) for employee_id in employee_ids}
    assignments: dict[uuid.UUID, LedgerAssignment] = {}
    for row in rows:
        employee_id = uuid_or_none(row.get("employee_id"))
        if employee_id is None or str(employee_id) not in requested_ids:
            continue
        assignments[employee_id] = LedgerAssignment(
            payroll_role=clean_text(row.get("payroll_role")),
            category=clean_text(row.get("category")),
        )
    return assignments


async def find_schedule_shape(
    session: AsyncSession,
) -> tuple[str, str, str | None, str | None, bool] | None:
    for table_name in SCHEDULE_TABLE_CANDIDATES:
        columns = await table_columns(session, table_name)
        if "employee_id" not in columns:
            continue
        date_column = next((column for column in SCHEDULE_DATE_COLUMNS if column in columns), None)
        if date_column is None:
            continue
        if "primary_role_id" in columns:
            return (table_name, date_column, None, None, True)
        role_column = next((column for column in SCHEDULE_ROLE_COLUMNS if column in columns), None)
        if role_column is None:
            continue
        category_column = next(
            (column for column in SCHEDULE_CATEGORY_COLUMNS if column in columns),
            None,
        )
        return (table_name, date_column, role_column, category_column, False)
    return None


async def table_columns(session: AsyncSession, table_name: str) -> set[str]:
    try:
        rows = (
            await session.execute(
                text(
                    """
                    select column_name
                      from information_schema.columns
                     where table_schema = current_schema()
                       and table_name = :table_name
                    """
                ),
                {"table_name": table_name},
            )
        ).all()
    except SQLAlchemyError:
        return set()
    return {str(row[0]) for row in rows}


def resolve_default_assignment(
    schedule_assignment: LedgerAssignment | None,
    primary_assignment: LedgerAssignment | None,
) -> tuple[LedgerAssignment, str]:
    if schedule_assignment is not None:
        return schedule_assignment, "schedule"
    if primary_assignment is not None:
        return primary_assignment, "fallback_primary"
    return LedgerAssignment(payroll_role=None, category=None), "fallback_primary"


def assignment_for_employee(
    assignments: Mapping[uuid.UUID | str, Any],
    employee_id: uuid.UUID,
) -> LedgerAssignment | None:
    value = assignments.get(employee_id)
    if value is None:
        value = assignments.get(str(employee_id))
    return coerce_assignment(value)


def coerce_assignment(value: Any) -> LedgerAssignment | None:
    if value is None:
        return None
    if isinstance(value, LedgerAssignment):
        return value
    if isinstance(value, EmployeeRoleAssignment):
        return LedgerAssignment(payroll_role=value.payroll_role, category=value.category)
    if isinstance(value, Mapping):
        return LedgerAssignment(
            payroll_role=clean_text(value.get("payroll_role") or value.get("role")),
            category=clean_text(value.get("category")),
        )
    payroll_role = getattr(value, "payroll_role", None) or getattr(value, "role", None)
    return LedgerAssignment(
        payroll_role=clean_text(payroll_role),
        category=clean_text(getattr(value, "category", None)),
    )


def serialize_ledger_entry(entry: ShiftLedgerEntry, employee: Employee) -> dict[str, Any]:
    return ledger_entry_snapshot(entry) | {
        "employee_name": employee.full_name,
        "employee_iiko_id": employee.iiko_id,
    }


def ledger_entry_snapshot(entry: ShiftLedgerEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "work_date": entry.work_date,
        "employee_id": entry.employee_id,
        "payroll_role": entry.payroll_role,
        "category": entry.category,
        "source": entry.source,
        "opened_at": entry.opened_at,
        "closed_at": entry.closed_at,
        "notes": entry.notes,
        "is_resolved": entry.is_resolved,
    }


def uuid_or_none(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    if not text_value or text_value.casefold() in {"none", "null", "nan"}:
        return None
    return text_value


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'

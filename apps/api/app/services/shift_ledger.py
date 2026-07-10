from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AgentAction,
    AgentRun,
    Employee,
    EmployeeRoleAssignment,
    PayrollPeriod,
    PayrollRun,
    ScheduledShift,
    ShiftLedgerEntry,
    ShiftSchedule,
)
from app.services.attendance_loader import (
    MOSCOW_TZ,
    fetch_iiko_attendance_records,
    first_text,
    first_value,
    is_work_attendance,
    parse_datetime,
)
from app.services.employee_assignments import get_assignments
from app.services.freelancer import attendance as freelancer_attendance
from app.services.position_registry import production_payroll_positions
from app.services.staff_taxonomy import PAYROLL_ROLE_LABELS


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
    is_primary: bool = False
    is_substitute: bool = False


class ShiftLedgerNotFoundError(LookupError):
    pass


class ShiftLedgerValidationError(ValueError):
    pass


logger = logging.getLogger(__name__)


async def build_ledger_for_date(
    session: AsyncSession,
    work_date: date,
    *,
    iiko_records: Iterable[Mapping[str, Any]] | None = None,
    schedule_assignments: Mapping[uuid.UUID | str, Any] | None = None,
    primary_assignments: Mapping[uuid.UUID | str, Any] | None = None,
    role_assignments: Mapping[uuid.UUID | str, Any] | None = None,
) -> list[ShiftLedgerEntry]:
    snapshots = await load_iiko_attendance_snapshots(
        session,
        work_date,
        iiko_records=iiko_records,
    )
    employee_ids = {snapshot.employee_id for snapshot in snapshots}
    if schedule_assignments is None:
        schedule_assignments = await load_schedule_assignments(session, work_date, employee_ids)
    if role_assignments is None:
        role_assignments = (
            primary_assignments
            if primary_assignments is not None
            else await load_available_role_assignments(session, work_date, employee_ids)
        )

    existing_by_shift = await load_existing_entries(session, work_date, employee_ids)
    entries: list[ShiftLedgerEntry] = []

    for snapshot in sorted(snapshots, key=lambda item: (item.opened_at, str(item.employee_id))):
        schedule_assignment = assignment_for_employee(schedule_assignments, snapshot.employee_id)
        available_roles = assignments_for_employee(role_assignments, snapshot.employee_id)
        default_assignment, source = resolve_default_assignment(
            schedule_assignment,
            available_roles,
        )
        entry = existing_by_shift.get((snapshot.employee_id, snapshot.opened_at))

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
) -> ShiftLedgerEntry:
    payroll_role = payroll_role.strip()
    if not payroll_role:
        raise ShiftLedgerValidationError("payroll_role is required")

    entry = await session.get(ShiftLedgerEntry, entry_id)
    if entry is None:
        raise ShiftLedgerNotFoundError("Shift ledger entry not found")

    active_assignments = assignments_for_employee(
        await load_currently_active_role_assignments(session, {entry.employee_id}),
        entry.employee_id,
    )
    assignment = assignment_by_role(active_assignments, payroll_role)
    if assignment is None:
        raise ShiftLedgerValidationError("Эта роль не закреплена за сотрудником в Штате")

    before = ledger_entry_snapshot(entry)
    entry.payroll_role = payroll_role
    entry.category = assignment.category
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
    await session.flush()
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
    rows = result.all()
    employee_ids = {entry.employee_id for entry, _employee in rows}
    roles_by_employee = await load_available_role_assignments(session, work_date, employee_ids)
    return [
        serialize_ledger_entry(
            entry,
            employee,
            assignments_for_employee(roles_by_employee, entry.employee_id),
        )
        for entry, employee in rows
    ]


async def list_ledger_matrix(session: AsyncSession, selected_date: date) -> dict[str, Any]:
    start_date, end_date = ledger_week_bounds(selected_date)
    days = list(iter_dates(start_date, end_date))
    # Учёт смен показывает штатных поваров/кассиров, уже размеченные payroll-смены
    # и нецелевых сотрудников с доступными payroll-ролями для ручного разбора.
    result = await session.execute(
        select(ShiftLedgerEntry, Employee)
        .join(Employee, Employee.id == ShiftLedgerEntry.employee_id)
        .where(
            ShiftLedgerEntry.work_date >= start_date,
            ShiftLedgerEntry.work_date <= end_date,
            or_(
                Employee.position.in_(production_payroll_positions()),
                ShiftLedgerEntry.payroll_role.in_(tuple(PAYROLL_ROLE_LABELS)),
                select(EmployeeRoleAssignment.id)
                .where(
                    EmployeeRoleAssignment.employee_id == ShiftLedgerEntry.employee_id,
                    EmployeeRoleAssignment.effective_from <= ShiftLedgerEntry.work_date,
                    or_(
                        EmployeeRoleAssignment.effective_to.is_(None),
                        EmployeeRoleAssignment.effective_to > ShiftLedgerEntry.work_date,
                    ),
                )
                .exists(),
            ),
        )
        .order_by(Employee.full_name, ShiftLedgerEntry.work_date, ShiftLedgerEntry.opened_at)
    )
    rows = result.all()
    employee_ids = {entry.employee_id for entry, _employee in rows}
    roles_by_employee = await load_currently_active_role_assignments(session, employee_ids)
    latest_locked_date = await get_latest_locked_payroll_date(session)
    employee_rows: dict[uuid.UUID, dict[str, Any]] = {}

    for entry, employee in rows:
        employee_row = employee_rows.setdefault(
            employee.id,
            {
                "id": str(employee.id),
                "full_name": employee.full_name,
                "iiko_id": employee.iiko_id,
                "_days": {day: [] for day in days},
            },
        )
        employee_row["_days"][entry.work_date].append(entry)

    today = ledger_today()
    serialized_employees: list[dict[str, Any]] = []
    for employee_id, employee_row in employee_rows.items():
        serialized_days: list[dict[str, Any]] = []
        for day in days:
            available_roles = assignments_for_employee(roles_by_employee, employee_id)
            entries = employee_row["_days"][day]
            payroll_locked = is_payroll_locked(day, latest_locked_date)
            serialized_days.append(
                {
                    "date": day.isoformat(),
                    "payroll_locked": payroll_locked,
                    "available_roles": serialize_available_roles(available_roles),
                    "summary": serialize_shift_summary(entries),
                    "shifts": [
                        serialize_matrix_shift(
                            entry,
                            available_roles,
                            payroll_locked=payroll_locked,
                        )
                        for entry in sorted(entries, key=lambda item: item.opened_at)
                    ],
                }
            )
        serialized_employees.append(
            {
                "id": employee_row["id"],
                "full_name": employee_row["full_name"],
                "iiko_id": employee_row["iiko_id"],
                "days": serialized_days,
            }
        )

    return {
        "selected_date": selected_date.isoformat(),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "days": [
            {
                "date": day.isoformat(),
                "is_today": day == today,
            }
            for day in days
        ],
        "employees": serialized_employees,
    }


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
    # Индекс привязок пула «Внештат №N»: явку с плейсхолдера перенаправляем на
    # временного внештатника, чья карточка покрывает дату явки. Грузим индекс
    # лишь при наличии плейсхолдеров — иначе лишний запрос ни к чему.
    has_placeholder = any(
        getattr(emp, "is_freelancer_placeholder", False) for emp in employees_by_iiko_id.values()
    )
    binding_index = (
        await freelancer_attendance.load_binding_index(session)
        if has_placeholder
        else freelancer_attendance.BindingIndex({})
    )
    grouped: dict[tuple[uuid.UUID, datetime], AttendanceSnapshot] = {}
    skipped_missing_employee_iiko_ids: set[str] = set()
    skipped_missing_employee_records = 0

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
            skipped_missing_employee_iiko_ids.add(iiko_id)
            skipped_missing_employee_records += 1
            continue

        ended_at = parse_datetime(first_value(record, "dateTo", "CloseTime", "ended_at"))

        if employee.is_freelancer_placeholder:
            case_minutes = (
                max(0, int((ended_at - started_at).total_seconds() // 60))
                if ended_at is not None
                else 0
            )
            employee = await freelancer_attendance.remap_attendance_employee(
                session,
                employee,
                work_date,
                index=binding_index,
                minutes=case_minutes,
                opened_at=started_at,
            )
            if employee is None:
                # Явка на плейсхолдер без привязки — кейс разбора, в леджер не пишем.
                continue
        note = first_text(record, "notes", "comment")
        snapshot_key = (employee.id, started_at)
        previous = grouped.get(snapshot_key)
        if previous is None:
            grouped[snapshot_key] = AttendanceSnapshot(
                employee_id=employee.id,
                opened_at=started_at,
                closed_at=ended_at,
                notes=note or None,
            )
            continue

        closed_at = max(
            (item for item in (previous.closed_at, ended_at) if item is not None),
            default=None,
        )
        notes = ";".join(part for part in (previous.notes, note) if part)
        grouped[snapshot_key] = AttendanceSnapshot(
            employee_id=employee.id,
            opened_at=min(previous.opened_at, started_at),
            closed_at=closed_at,
            notes=notes or None,
        )

    if skipped_missing_employee_iiko_ids:
        logger.info(
            "attendance: employee iiko_id не найден в БД, записи пропущены: count=%s ids=%s",
            skipped_missing_employee_records,
            ", ".join(sorted(skipped_missing_employee_iiko_ids)),
        )

    return list(grouped.values())


async def load_existing_entries(
    session: AsyncSession,
    work_date: date,
    employee_ids: set[uuid.UUID],
) -> dict[tuple[uuid.UUID, datetime], ShiftLedgerEntry]:
    if not employee_ids:
        return {}
    result = await session.scalars(
        select(ShiftLedgerEntry).where(
            ShiftLedgerEntry.work_date == work_date,
            ShiftLedgerEntry.employee_id.in_(employee_ids),
        )
    )
    return {(entry.employee_id, entry.opened_at): entry for entry in result.all()}


async def load_available_role_assignments(
    session: AsyncSession,
    work_date: date,
    employee_ids: set[uuid.UUID],
) -> dict[uuid.UUID, list[LedgerAssignment]]:
    if not employee_ids:
        return {}
    result = await session.scalars(
        select(EmployeeRoleAssignment)
        .where(
            EmployeeRoleAssignment.employee_id.in_(employee_ids),
            EmployeeRoleAssignment.effective_from <= work_date,
            or_(
                EmployeeRoleAssignment.effective_to.is_(None),
                EmployeeRoleAssignment.effective_to > work_date,
            ),
        )
        .order_by(
            EmployeeRoleAssignment.employee_id,
            EmployeeRoleAssignment.is_primary.desc(),
            EmployeeRoleAssignment.payroll_role,
        )
    )
    assignments: dict[uuid.UUID, list[LedgerAssignment]] = {}
    for assignment in result.all():
        ledger_assignment = coerce_assignment(assignment)
        if ledger_assignment is not None:
            assignments.setdefault(assignment.employee_id, []).append(ledger_assignment)
    return assignments


async def load_currently_active_role_assignments(
    session: AsyncSession,
    employee_ids: set[uuid.UUID],
) -> dict[uuid.UUID, list[LedgerAssignment]]:
    if not employee_ids:
        return {}
    today = ledger_today()
    result = await session.scalars(
        select(EmployeeRoleAssignment)
        .where(
            EmployeeRoleAssignment.employee_id.in_(employee_ids),
            EmployeeRoleAssignment.effective_from <= today,
            or_(
                EmployeeRoleAssignment.effective_to.is_(None),
                EmployeeRoleAssignment.effective_to > today,
            ),
        )
        .order_by(
            EmployeeRoleAssignment.employee_id,
            EmployeeRoleAssignment.is_primary.desc(),
            EmployeeRoleAssignment.payroll_role,
        )
    )
    assignments: dict[uuid.UUID, list[LedgerAssignment]] = {}
    for assignment in result.all():
        ledger_assignment = coerce_assignment(assignment)
        if ledger_assignment is not None:
            assignments.setdefault(assignment.employee_id, []).append(ledger_assignment)
    return assignments


async def get_latest_locked_payroll_date(session: AsyncSession) -> date | None:
    # Замораживаем ячейки роли в Учёте смен ТОЛЬКО для зафинализированных периодов.
    # `completed` означает «расчёт прошёл без блокеров», но ещё не выплачен — менеджер
    # должен иметь возможность поправить роль и пересчитать.
    #
    # Учитываем ТОЛЬКО недельные (производственные) периоды: Учёт смен — это
    # производственный сменный контур, и блокировать его должна только финализация
    # производственной недели. Полумесячные (`half_month`) периоды — это окладная
    # админская ведомость, которая ролей из табеля не потребляет; её финализация
    # 15-го числа не должна морозить сменные ячейки первой половины месяца
    # (иначе финализация админ-ведомости 1–15 блокирует ещё не закрытую
    # производственную неделю, например 9–15 июня).
    #
    # Игнорируем финализированные периоды с end_date в будущем: payroll не может быть
    # финализирован за ещё не наступившие дни. Это защита от битых legacy-дат
    # (например, год 2175), из-за которых max(end_date) уезжал в будущее и блокировал
    # весь табель целиком.
    today = ledger_today()
    return await session.scalar(
        select(func.max(PayrollPeriod.end_date))
        .select_from(PayrollRun)
        .join(PayrollPeriod, PayrollPeriod.id == PayrollRun.period_id)
        .where(
            PayrollRun.status == "finalized",
            PayrollPeriod.period_type == "week",
            PayrollPeriod.end_date <= today,
        )
    )


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
                is_primary=primary.is_primary,
                is_substitute=primary.is_substitute,
            )
    return assignments


async def load_schedule_assignments(
    session: AsyncSession,
    work_date: date,
    employee_ids: set[uuid.UUID],
) -> dict[uuid.UUID, LedgerAssignment]:
    # Роль из ОПУБЛИКОВАННОГО графика на этот день. Опубликованный график — audit
    # baseline плана; черновики и замещённые (superseded) версии в учёт смен не
    # берём. Если действующих версий вдруг несколько, побеждает самая свежая по
    # published_at (в норме публикация помечает предыдущую как superseded, так что
    # published-версия на дату одна). Категорию из графика не тянем — резолвер
    # (resolve_default_assignment) восстановит её из ролей сотрудника «роль-в-роль».
    if not employee_ids:
        return {}
    result = await session.execute(
        select(ScheduledShift.employee_id, ScheduledShift.payroll_role)
        .join(ShiftSchedule, ShiftSchedule.id == ScheduledShift.shift_schedule_id)
        .where(
            ShiftSchedule.status == "published",
            ScheduledShift.business_date == work_date,
            ScheduledShift.employee_id.in_(employee_ids),
        )
        .order_by(ShiftSchedule.published_at.asc().nulls_first())
    )
    assignments: dict[uuid.UUID, LedgerAssignment] = {}
    for employee_id, payroll_role in result.all():
        assignments[employee_id] = LedgerAssignment(
            payroll_role=clean_text(payroll_role),
            category=None,
        )
    return assignments


def resolve_default_assignment(
    schedule_assignment: LedgerAssignment | None,
    available_roles: list[LedgerAssignment],
) -> tuple[LedgerAssignment, str]:
    if not available_roles:
        return LedgerAssignment(payroll_role=None, category=None), "fallback_primary"
    if schedule_assignment is not None and schedule_assignment.payroll_role is not None:
        scheduled_role = assignment_by_role(available_roles, schedule_assignment.payroll_role)
        if scheduled_role is not None:
            return scheduled_role, "schedule"
    if len(available_roles) == 1:
        return available_roles[0], "fallback_primary"
    primary_assignment = next(
        (assignment for assignment in available_roles if assignment.is_primary),
        None,
    )
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


def assignments_for_employee(
    assignments: Mapping[uuid.UUID | str, Any],
    employee_id: uuid.UUID,
) -> list[LedgerAssignment]:
    value = assignments.get(employee_id)
    if value is None:
        value = assignments.get(str(employee_id))
    if value is None:
        return []
    if isinstance(value, Iterable) and not isinstance(
        value,
        (str, bytes, Mapping, LedgerAssignment, EmployeeRoleAssignment),
    ):
        return [
            assignment
            for item in value
            if (assignment := coerce_assignment(item)) is not None
            and assignment.payroll_role is not None
            and assignment.category is not None
        ]
    assignment = coerce_assignment(value)
    if assignment is None or assignment.payroll_role is None or assignment.category is None:
        return []
    return [assignment]


def assignment_by_role(
    assignments: list[LedgerAssignment],
    payroll_role: str,
) -> LedgerAssignment | None:
    return next(
        (assignment for assignment in assignments if assignment.payroll_role == payroll_role),
        None,
    )


def coerce_assignment(value: Any) -> LedgerAssignment | None:
    if value is None:
        return None
    if isinstance(value, LedgerAssignment):
        return value
    if isinstance(value, EmployeeRoleAssignment):
        return LedgerAssignment(
            payroll_role=value.payroll_role,
            category=value.category,
            is_primary=value.is_primary,
            is_substitute=value.is_substitute,
        )
    if isinstance(value, Mapping):
        return LedgerAssignment(
            payroll_role=clean_text(value.get("payroll_role") or value.get("role")),
            category=clean_text(value.get("category")),
            is_primary=bool(value.get("is_primary")),
            is_substitute=bool(value.get("is_substitute")),
        )
    payroll_role = getattr(value, "payroll_role", None) or getattr(value, "role", None)
    return LedgerAssignment(
        payroll_role=clean_text(payroll_role),
        category=clean_text(getattr(value, "category", None)),
        is_primary=bool(getattr(value, "is_primary", False)),
        is_substitute=bool(getattr(value, "is_substitute", False)),
    )


def serialize_ledger_entry(
    entry: ShiftLedgerEntry,
    employee: Employee,
    available_roles: list[LedgerAssignment],
) -> dict[str, Any]:
    category = category_for_role(entry.payroll_role, available_roles)
    is_resolved = entry.is_resolved and entry.payroll_role is not None and category is not None
    return ledger_entry_snapshot(entry) | {
        "employee_name": employee.full_name,
        "employee_iiko_id": employee.iiko_id,
        "category": category,
        "available_roles": serialize_available_roles(available_roles),
        "status": ledger_status(available_roles, is_resolved),
        "is_resolved": is_resolved,
    }


def serialize_matrix_shift(
    entry: ShiftLedgerEntry,
    available_roles: list[LedgerAssignment],
    *,
    payroll_locked: bool = False,
) -> dict[str, Any]:
    category = category_for_role(entry.payroll_role, available_roles)
    is_resolved = entry.is_resolved and entry.payroll_role is not None and category is not None
    return {
        "ledger_entry_id": str(entry.id) if entry.id is not None else None,
        "opened_at": entry.opened_at.isoformat() if entry.opened_at is not None else None,
        "closed_at": entry.closed_at.isoformat() if entry.closed_at is not None else None,
        "payroll_role": entry.payroll_role,
        "category": category,
        "is_resolved": is_resolved,
        "status": ledger_status(available_roles, is_resolved),
        "payroll_locked": payroll_locked,
    }


def serialize_shift_summary(entries: list[ShiftLedgerEntry]) -> dict[str, Any]:
    closed_values = [entry.closed_at for entry in entries if entry.closed_at is not None]
    earliest_open = min((entry.opened_at for entry in entries), default=None)
    latest_close = max(closed_values, default=None)
    return {
        "earliest_open": earliest_open.isoformat() if earliest_open is not None else None,
        "latest_close": latest_close.isoformat() if latest_close is not None else None,
        "shift_count": len(entries),
    }


def serialize_available_roles(assignments: list[LedgerAssignment]) -> list[dict[str, Any]]:
    return [
        {
            "payroll_role": assignment.payroll_role,
            "category": assignment.category,
            "is_substitute": assignment.is_substitute,
        }
        for assignment in assignments
        if assignment.payroll_role is not None and assignment.category is not None
    ]


def category_for_role(
    payroll_role: str | None,
    assignments: list[LedgerAssignment],
) -> str | None:
    if payroll_role is None:
        return None
    assignment = assignment_by_role(assignments, payroll_role)
    return assignment.category if assignment is not None else None


def ledger_status(available_roles: list[LedgerAssignment], is_resolved: bool) -> str:
    if not available_roles:
        return "needs_employee_setup"
    if is_resolved:
        return "resolved"
    return "needs_role_selection"


def is_payroll_locked(work_date: date, latest_locked_date: date | None) -> bool:
    return latest_locked_date is not None and work_date <= latest_locked_date


def ledger_week_bounds(selected_date: date) -> tuple[date, date]:
    return selected_date - timedelta(days=6), selected_date


def ledger_today() -> date:
    return datetime.now(MOSCOW_TZ).date()


def iter_dates(start_date: date, end_date: date) -> Iterable[date]:
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def ledger_entry_snapshot(entry: ShiftLedgerEntry) -> dict[str, Any]:
    return {
        "id": str(entry.id) if entry.id is not None else None,
        "work_date": entry.work_date.isoformat() if entry.work_date is not None else None,
        "employee_id": str(entry.employee_id) if entry.employee_id is not None else None,
        "payroll_role": entry.payroll_role,
        "category": entry.category,
        "source": entry.source,
        "opened_at": entry.opened_at.isoformat() if entry.opened_at is not None else None,
        "closed_at": entry.closed_at.isoformat() if entry.closed_at is not None else None,
        "notes": entry.notes,
        "is_resolved": entry.is_resolved,
    }


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    if not text_value or text_value.casefold() in {"none", "null", "nan"}:
        return None
    return text_value

from __future__ import annotations

import importlib
import json
import os
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models import AgentAction, AgentRun, DataSource, Employee, SourceCredential

ACTIVE_STATUSES = {"active", "enabled", "not_deleted", "not deleted", "не удален", "не удалён"}
DELETED_STATUSES = {
    "deleted",
    "inactive",
    "disabled",
    "terminated",
    "fired",
    "archived",
    "уволен",
    "удален",
    "удалён",
}


@dataclass(slots=True)
class SyncResult:
    created: int = 0
    updated: int = 0
    deactivated: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "created": self.created,
            "updated": self.updated,
            "deactivated": self.deactivated,
        }


@dataclass(slots=True)
class IikoEmployeeRecord:
    iiko_id: str
    full_name: str
    is_deleted: bool = False
    hire_date: date | None = None
    fire_date: date | None = None


@dataclass(slots=True)
class EmployeeMutation:
    action_type: str
    employee: Employee
    before: dict[str, Any] | None


@dataclass(slots=True)
class EmployeeSyncPlan:
    result: SyncResult
    mutations: list[EmployeeMutation]
    created_employees: list[Employee]


def _candidate_project_roots() -> list[Path]:
    current = Path(__file__).resolve()
    roots = [parent for parent in current.parents if (parent / "scripts/iiko").exists()]
    roots.extend(
        Path(path) for path in ("/app", Path.cwd(), Path.cwd().parent, Path.cwd().parent.parent)
    )
    result: list[Path] = []
    for root in roots:
        if root not in result:
            result.append(root)
    return result


def _load_export_employees_module() -> ModuleType:
    for root in _candidate_project_roots():
        script_dir = root / "scripts/iiko"
        if not (script_dir / "export_employees.py").exists():
            continue
        script_dir_str = str(script_dir)
        if script_dir_str not in sys.path:
            sys.path.insert(0, script_dir_str)
        return importlib.import_module("export_employees")
    raise RuntimeError("scripts/iiko/export_employees.py is not available")


async def _load_source_credential_env(session: AsyncSession) -> None:
    data_source = await session.scalar(select(DataSource).where(DataSource.code == "iiko"))
    if data_source is None:
        return

    credential = await session.scalar(
        select(SourceCredential).where(
            SourceCredential.data_source_id == data_source.id,
            SourceCredential.status == "active",
        )
    )
    if credential is None:
        return

    raw_secret = os.environ.get(credential.vault_key)
    if not raw_secret:
        return

    try:
        payload = json.loads(raw_secret)
    except json.JSONDecodeError:
        return
    if not isinstance(payload, dict):
        return

    for key, value in payload.items():
        if isinstance(value, str) and key.startswith("IIKO_SERVER_") and not os.environ.get(key):
            os.environ[key] = value


def fetch_iiko_employee_records() -> list[Mapping[str, Any]]:
    export_employees = _load_export_employees_module()
    export_employees.load_local_env()
    client = export_employees.IikoClient()
    _status, data = client.request("/employees", params={"includeDeleted": "true"})
    return list(export_employees.records_from_response("/employees", data))


async def sync_employees(
    session: AsyncSession | None = None,
    *,
    iiko_records: Iterable[Mapping[str, Any]] | None = None,
    run_reason: str = "manual",
    today: date | None = None,
    now: datetime | None = None,
) -> SyncResult:
    owns_session = session is None
    if owns_session:
        async with AsyncSessionLocal() as owned_session:
            return await sync_employees(
                owned_session,
                iiko_records=iiko_records,
                run_reason=run_reason,
                today=today,
                now=now,
            )

    assert session is not None
    await _load_source_credential_env(session)

    sync_today = today or datetime.now(UTC).date()
    sync_now = now or datetime.now(UTC)
    records = list(iiko_records) if iiko_records is not None else fetch_iiko_employee_records()

    agent_run = AgentRun(
        agent_name="iiko_employee_sync",
        status="running",
        params={"reason": run_reason},
        result={},
    )
    session.add(agent_run)
    await session.flush()

    try:
        result = await _apply_employee_records(session, records, agent_run.id, sync_today, sync_now)
        agent_run.status = "success"
        agent_run.finished_at = datetime.now(UTC)
        agent_run.result = result.as_dict()
        await session.commit()
        return result
    except Exception as exc:
        agent_run.status = "failed"
        agent_run.finished_at = datetime.now(UTC)
        agent_run.result = {"error": str(exc)[:500]}
        await session.commit()
        raise


async def _apply_employee_records(
    session: AsyncSession,
    records: Iterable[Mapping[str, Any]],
    agent_run_id: Any,
    sync_today: date,
    sync_now: datetime,
) -> SyncResult:
    existing_by_iiko_id = {
        employee.iiko_id: employee
        for employee in (await session.scalars(select(Employee))).all()
    }
    plan = plan_employee_sync(existing_by_iiko_id, records, sync_today, sync_now)

    for employee in plan.created_employees:
        session.add(employee)
    await session.flush()

    for mutation in plan.mutations:
        _add_action(
            session,
            agent_run_id,
            mutation.action_type,
            mutation.employee,
            before=mutation.before,
            after=_employee_snapshot(mutation.employee),
        )

    return plan.result


def plan_employee_sync(
    existing_by_iiko_id: dict[str, Employee],
    records: Iterable[Mapping[str, Any]],
    sync_today: date,
    sync_now: datetime,
) -> EmployeeSyncPlan:
    result = SyncResult()
    mutations: list[EmployeeMutation] = []
    created_employees: list[Employee] = []

    for raw_record in records:
        record = normalize_iiko_employee(raw_record)
        if record is None:
            continue

        employee = existing_by_iiko_id.get(record.iiko_id)
        if employee is None:
            if record.is_deleted:
                continue
            employee = Employee(
                full_name=record.full_name,
                iiko_id=record.iiko_id,
                position=None,
                category=None,
                is_senior=False,
                is_deputy_senior=False,
                status="needs_setup",
                hire_date=record.hire_date,
                iiko_sync_at=sync_now,
            )
            existing_by_iiko_id[record.iiko_id] = employee
            created_employees.append(employee)
            result.created += 1
            mutations.append(EmployeeMutation("create", employee, None))
            continue

        before = _employee_snapshot(employee)
        changed = False
        deactivated = False

        if record.is_deleted:
            if employee.status != "inactive" or employee.fire_date is None:
                employee.status = "inactive"
                employee.fire_date = record.fire_date or sync_today
                deactivated = True
                changed = True
        else:
            if employee.full_name != record.full_name:
                employee.full_name = record.full_name
                changed = True
            if record.hire_date and employee.hire_date != record.hire_date:
                employee.hire_date = record.hire_date
                changed = True

        employee.iiko_sync_at = sync_now

        if deactivated:
            result.deactivated += 1
            mutations.append(EmployeeMutation("deactivate", employee, before))
        elif changed:
            result.updated += 1
            mutations.append(EmployeeMutation("update", employee, before))

    return EmployeeSyncPlan(result=result, mutations=mutations, created_employees=created_employees)


def normalize_iiko_employee(raw_record: Mapping[str, Any]) -> IikoEmployeeRecord | None:
    iiko_id = _first_text(
        raw_record,
        "id",
        "employeeId",
        "employee_id",
        "iikoId",
        "iiko_id",
        "code",
    )
    if not iiko_id:
        return None

    full_name = (
        _first_text(raw_record, "fullName", "full_name", "name", "displayName", "fio")
        or " ".join(
            part
            for part in (
                _first_text(raw_record, "lastName", "last_name", "surname"),
                _first_text(raw_record, "firstName", "first_name"),
                _first_text(raw_record, "middleName", "middle_name", "patronymic"),
            )
            if part
        ).strip()
    )
    if not full_name:
        return None

    return IikoEmployeeRecord(
        iiko_id=iiko_id,
        full_name=full_name,
        is_deleted=_is_deleted(raw_record),
        hire_date=_first_date(raw_record, "hireDate", "hire_date", "employmentDate"),
        fire_date=_first_date(raw_record, "fireDate", "fire_date", "terminationDate"),
    )


def _first_text(record: Mapping[str, Any], *keys: str) -> str:
    lowered = {str(key).casefold(): value for key, value in record.items()}
    for key in keys:
        value = record.get(key)
        if value is None:
            value = lowered.get(key.casefold())
        if value is None:
            continue
        text = str(value).replace("\xa0", " ").strip()
        if text and text.casefold() not in {"none", "null", "nan"}:
            return text
    return ""


def _first_date(record: Mapping[str, Any], *keys: str) -> date | None:
    value = _first_text(record, *keys)
    if not value:
        return None
    for separator in ("T", " "):
        value = value.split(separator, 1)[0]
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _is_deleted(record: Mapping[str, Any]) -> bool:
    status_value = _first_text(record, "status", "employeeStatus").casefold()
    if status_value in DELETED_STATUSES:
        return True
    if status_value in ACTIVE_STATUSES:
        return False

    for key in ("deleted", "isDeleted", "is_deleted", "inactive", "deactivated", "fired"):
        value = _first_text(record, key).casefold()
        if value in {"1", "true", "yes", "y", "да", "истина"}:
            return True
    active = _first_text(record, "active", "isActive", "is_active").casefold()
    return active in {"0", "false", "no", "n", "нет", "ложь"}


def _employee_snapshot(employee: Employee) -> dict[str, Any]:
    return {
        "id": str(employee.id),
        "iiko_id": employee.iiko_id,
        "full_name": employee.full_name,
        "position": employee.position,
        "category": employee.category,
        "is_senior": employee.is_senior,
        "is_deputy_senior": employee.is_deputy_senior,
        "status": employee.status,
        "hire_date": employee.hire_date.isoformat() if employee.hire_date else None,
        "fire_date": employee.fire_date.isoformat() if employee.fire_date else None,
    }


def _add_action(
    session: AsyncSession,
    agent_run_id: Any,
    action_type: str,
    employee: Employee,
    *,
    before: dict[str, Any] | None,
    after: dict[str, Any],
) -> None:
    session.add(
        AgentAction(
            agent_run_id=agent_run_id,
            action_type=action_type,
            target_table="employee",
            target_id=employee.id,
            before_value=before,
            after_value=after,
        )
    )

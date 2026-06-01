from __future__ import annotations

import http.client
import importlib
import json
import os
import re
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

import anyio
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models import (
    AgentAction,
    AgentRun,
    DataSource,
    Employee,
    EmployeeRoleAssignment,
    PayrollLine,
    ShiftLedgerEntry,
    SourceCredential,
)
from app.services import employee_change_events as employee_change_event_service
from app.services import employee_effective_events as employee_effective_event_service
from app.services.employee_status import (
    compute_status,
    is_cook_position,
    is_target_position,
    position_group_for_position,
)
from app.services.staff_taxonomy import canonical_position_name

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
SERVICE_ACCOUNT_LOGINS = {
    "apiimportuser",
    "deliveryuser",
    "iikominiuser",
    "iikouser",
    "pluginuser_default",
    "systemintegrationuser",
    "transportuser",
}
SERVICE_ACCOUNT_NAME_MARKERS = (
    "docsinbox user",
    "пользователь api импорта",
    "пользователь front.api",
    "пользователь iikomini",
    "пользователь iikotransport",
    "пользователь для интеграции",
    "пользователь для централизованной доставки",
    "сотрудник техподдержки iiko",
    "сотрудник техподдержки лемма",
)
SyncMode = Literal["incremental", "reset"]
GHOST_CLEANUP_NOTE = "ghost cleanup auto"
RESET_MISSING_NOTE = "missing from iiko reset"
SKIP_MISSING_NAME_OR_POSITION = "missing_name_or_position"
SKIP_NON_CANONICAL_OR_UNSUPPORTED_POSITION = "non_canonical_or_unsupported_position"
IIKO_ID_KEYS = ("id", "employeeId", "employee_id", "iikoId", "iiko_id", "code")
IIKO_INCOMPLETE_READ_ATTEMPTS = 3
IIKO_INCOMPLETE_READ_RETRY_DELAY_SECONDS = 5
IIKO_EMPLOYEE_XML_CONTENT_TYPE = "application/xml; charset=utf-8"


@dataclass(slots=True)
class SyncResult:
    created: int = 0
    updated: int = 0
    deactivated: int = 0
    ghost_cleaned: int = 0
    existing_assignments_preserved: bool | None = None

    def as_dict(self) -> dict[str, int]:
        return {
            "created": self.created,
            "updated": self.updated,
            "deactivated": self.deactivated,
        }

    def audit_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            **self.as_dict(),
            "ghost_cleaned": self.ghost_cleaned,
        }
        if self.existing_assignments_preserved is not None:
            result["existing_assignments_preserved"] = self.existing_assignments_preserved
        return result


@dataclass(slots=True)
class IikoEmployeeRecord:
    iiko_id: str
    full_name: str
    position: str | None = None
    is_deleted: bool = False
    is_service_account: bool = False
    hire_date: date | None = None
    fire_date: date | None = None


@dataclass(frozen=True, slots=True)
class IikoEmployeeRole:
    id: str
    name: str
    code: str | None = None
    deleted: bool = False


@dataclass(frozen=True, slots=True)
class IikoEmployeeCreateResult:
    iiko_id: str
    full_name: str
    position: str
    role_id: str
    role_code: str | None
    is_target_position: bool
    hire_date: date | None = None


@dataclass(frozen=True, slots=True)
class IikoEmployeeUpdateResult:
    iiko_id: str
    full_name: str | None
    position: str | None
    role_id: str | None
    role_code: str | None
    hire_date: date | None = None


class IikoEmployeeOperationError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502) -> None:
        self.status_code = status_code
        super().__init__(message)


@dataclass(slots=True)
class EmployeeMutation:
    action_type: str
    employee: Employee
    before: dict[str, Any] | None
    note: str | None = None


@dataclass(slots=True)
class SkippedEmployeeRecord:
    iiko_id: str | None
    reason: str


@dataclass(slots=True)
class EmployeeSyncPlan:
    result: SyncResult
    mutations: list[EmployeeMutation]
    created_employees: list[Employee]
    skipped_records: list[SkippedEmployeeRecord]
    ghost_cleanup_employees: list[Employee]


def _candidate_project_roots() -> list[Path]:
    current = Path(__file__).resolve()
    roots = [
        parent for parent in current.parents if (parent / "integrations/iiko/scripts").exists()
    ]
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
        script_dir = root / "integrations/iiko/scripts"
        if not (script_dir / "export_employees.py").exists():
            continue
        script_dir_str = str(script_dir)
        if script_dir_str not in sys.path:
            sys.path.insert(0, script_dir_str)
        return importlib.import_module("export_employees")
    raise RuntimeError("integrations/iiko/scripts/export_employees.py is not available")


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
    _status, data = _request_iiko_with_incomplete_read_retry(
        client,
        "/employees",
    )
    _roles_status, roles_data = _request_iiko_with_incomplete_read_retry(
        client,
        "/employees/roles",
    )
    employee_records = list(export_employees.records_from_response("/employees", data))
    role_records = list(export_employees.records_from_response("/employees/roles", roles_data))
    return _enrich_employee_records_with_roles(employee_records, role_records)


async def get_iiko_employee_roles(session: AsyncSession) -> list[IikoEmployeeRole]:
    await _load_source_credential_env(session)
    return await anyio.to_thread.run_sync(fetch_iiko_employee_roles)


def fetch_iiko_employee_roles(*, include_deleted: bool = False) -> list[IikoEmployeeRole]:
    export_employees = _load_export_employees_module()
    export_employees.load_local_env()
    client = export_employees.IikoClient()
    roles = _fetch_iiko_employee_roles_with_client(
        export_employees,
        client,
        include_deleted=True,
    )
    if include_deleted:
        return roles
    return [role for role in roles if not role.deleted]


async def create_iiko_employee(
    session: AsyncSession,
    *,
    full_name: str,
    role_id: str,
    pin_code: str,
) -> IikoEmployeeCreateResult:
    await _load_source_credential_env(session)
    return await anyio.to_thread.run_sync(
        _create_iiko_employee_sync,
        full_name.strip(),
        role_id.strip(),
        pin_code.strip(),
    )


async def dismiss_iiko_employee(session: AsyncSession, *, iiko_id: str) -> None:
    await _load_source_credential_env(session)
    await anyio.to_thread.run_sync(_dismiss_iiko_employee_sync, iiko_id)


async def update_iiko_employee(
    session: AsyncSession,
    *,
    iiko_id: str,
    full_name: str | None = None,
    position: str | None = None,
) -> IikoEmployeeUpdateResult:
    await _load_source_credential_env(session)
    return await anyio.to_thread.run_sync(
        _update_iiko_employee_sync,
        iiko_id,
        full_name,
        position,
    )


def _request_iiko_with_incomplete_read_retry(
    client: Any,
    path: str,
    **kwargs: Any,
) -> tuple[Any, Any]:
    for attempt in range(IIKO_INCOMPLETE_READ_ATTEMPTS):
        try:
            return client.request(path, **kwargs)
        except http.client.IncompleteRead:
            if attempt == IIKO_INCOMPLETE_READ_ATTEMPTS - 1:
                raise
            time.sleep(IIKO_INCOMPLETE_READ_RETRY_DELAY_SECONDS)
    raise RuntimeError("unreachable")


def _fetch_iiko_employee_roles_with_client(
    export_employees: ModuleType,
    client: Any,
    *,
    include_deleted: bool,
) -> list[IikoEmployeeRole]:
    try:
        _status, data = _request_iiko_with_incomplete_read_retry(
            client,
            "/employees/roles",
            params={"includeDeleted": "true" if include_deleted else "false"},
        )
    except export_employees.IikoHTTPError as exc:
        raise _iiko_operation_error("получить роли сотрудников из iiko", exc) from exc

    records = list(export_employees.records_from_response("/employees/roles", data))
    roles: list[IikoEmployeeRole] = []
    for record in records:
        role_id = _first_text(record, "id", "roleId")
        role_name = _first_text(record, "name", "title", "roleName")
        if not role_id or not role_name:
            continue
        roles.append(
            IikoEmployeeRole(
                id=role_id,
                name=role_name,
                code=_first_text(record, "code", "roleCode") or None,
                deleted=_is_deleted(record),
            )
        )
    roles.sort(key=lambda role: (role.deleted, role.name.casefold(), role.id))
    return roles


def _create_iiko_employee_sync(
    full_name: str,
    role_id: str,
    pin_code: str,
) -> IikoEmployeeCreateResult:
    if not full_name:
        raise IikoEmployeeOperationError("ФИО сотрудника обязательно", status_code=400)
    if not role_id:
        raise IikoEmployeeOperationError("Выберите роль iiko", status_code=400)
    if re.fullmatch(r"\d{4}", pin_code) is None:
        raise IikoEmployeeOperationError("ПИН-код должен состоять из 4 цифр", status_code=400)

    export_employees = _load_export_employees_module()
    export_employees.load_local_env()
    client = export_employees.IikoClient()
    roles = _fetch_iiko_employee_roles_with_client(
        export_employees,
        client,
        include_deleted=True,
    )
    role = next((item for item in roles if item.id == role_id), None)
    if role is None:
        raise IikoEmployeeOperationError("Роль iiko не найдена", status_code=400)
    if role.deleted:
        raise IikoEmployeeOperationError("Выбранная роль iiko удалена", status_code=400)

    employee_id = str(uuid.uuid4())
    body = _build_iiko_employee_xml(employee_id, full_name, role, pin_code)
    try:
        _status, data = _request_iiko_with_incomplete_read_retry(
            client,
            f"/employees/byId/{employee_id}",
            method="PUT",
            raw_body=body,
            content_type=IIKO_EMPLOYEE_XML_CONTENT_TYPE,
        )
    except export_employees.IikoHTTPError as exc:
        raise _iiko_operation_error("создать сотрудника в iiko", exc) from exc

    returned_id = _extract_iiko_id_from_response(export_employees, data) or employee_id
    return IikoEmployeeCreateResult(
        iiko_id=returned_id,
        full_name=full_name,
        position=canonical_position_name(role.name) or role.name,
        role_id=role.id,
        role_code=role.code,
        is_target_position=is_target_position(role.name),
        hire_date=_extract_hire_date_from_response(export_employees, data),
    )


def _dismiss_iiko_employee_sync(iiko_id: str) -> None:
    iiko_id = iiko_id.strip()
    if not iiko_id:
        raise IikoEmployeeOperationError("У сотрудника нет iiko id", status_code=400)

    export_employees = _load_export_employees_module()
    export_employees.load_local_env()
    client = export_employees.IikoClient()
    try:
        _status, data = _request_iiko_with_incomplete_read_retry(
            client,
            f"/employees/byId/{iiko_id}",
        )
        body = _build_iiko_employee_deleted_xml(data)
        _request_iiko_with_incomplete_read_retry(
            client,
            f"/employees/byId/{iiko_id}",
            method="PUT",
            raw_body=body,
            content_type=IIKO_EMPLOYEE_XML_CONTENT_TYPE,
        )
    except export_employees.IikoHTTPError as exc:
        raise _iiko_operation_error("уволить сотрудника в iiko", exc) from exc


def _update_iiko_employee_sync(
    iiko_id: str,
    full_name: str | None,
    position: str | None,
) -> IikoEmployeeUpdateResult:
    iiko_id = iiko_id.strip()
    if not iiko_id:
        raise IikoEmployeeOperationError("У сотрудника нет iiko id", status_code=400)

    target_full_name = full_name.strip() if full_name is not None else None
    if full_name is not None and not target_full_name:
        raise IikoEmployeeOperationError("ФИО сотрудника обязательно", status_code=400)

    canonical_position = canonical_position_name(position) if position is not None else None
    if position is not None and canonical_position is None:
        raise IikoEmployeeOperationError(
            "Должность не входит в канонический список",
            status_code=400,
        )

    export_employees = _load_export_employees_module()
    export_employees.load_local_env()
    client = export_employees.IikoClient()

    target_role: IikoEmployeeRole | None = None
    if canonical_position is not None:
        roles = _fetch_iiko_employee_roles_with_client(
            export_employees,
            client,
            include_deleted=True,
        )
        target_role = _iiko_employee_role_for_position(roles, canonical_position)
        if target_role is None:
            raise IikoEmployeeOperationError(
                f"В iiko не найдена роль для должности «{canonical_position}»",
                status_code=400,
            )

    try:
        _status, data = _request_iiko_with_incomplete_read_retry(
            client,
            f"/employees/byId/{iiko_id}",
        )
        body = _build_iiko_employee_update_xml(
            data,
            full_name=target_full_name,
            role=target_role,
        )
        _request_iiko_with_incomplete_read_retry(
            client,
            f"/employees/byId/{iiko_id}",
            method="PUT",
            raw_body=body,
            content_type=IIKO_EMPLOYEE_XML_CONTENT_TYPE,
        )
    except export_employees.IikoHTTPError as exc:
        raise _iiko_operation_error("обновить сотрудника в iiko", exc) from exc

    return IikoEmployeeUpdateResult(
        iiko_id=iiko_id,
        full_name=target_full_name,
        position=canonical_position,
        role_id=target_role.id if target_role is not None else None,
        role_code=target_role.code if target_role is not None else None,
        hire_date=_extract_hire_date_from_employee_xml(data),
    )


def _build_iiko_employee_xml(
    employee_id: str,
    full_name: str,
    role: IikoEmployeeRole,
    pin_code: str,
) -> bytes:
    root = ET.Element("employee")
    _set_xml_text(root, "id", employee_id)
    _set_xml_text(root, "code", "")
    _set_xml_text(root, "name", full_name)
    _set_xml_text(root, "login", "")
    _set_xml_text(root, "pinCode", pin_code)
    _set_xml_text(root, "mainRoleId", role.id)
    _set_xml_text(root, "rolesIds", role.id)
    _set_xml_text(root, "mainRoleCode", role.code or "")
    _set_xml_text(root, "roleCodes", role.code or "")
    _set_xml_text(root, "deleted", "false")
    _set_xml_text(root, "supplier", "false")
    _set_xml_text(root, "employee", "true")
    _set_xml_text(root, "client", "false")
    _set_xml_text(root, "representsStore", "false")
    _set_xml_text(root, "publicExternalData", "")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _build_iiko_employee_deleted_xml(data: bytes) -> bytes:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise IikoEmployeeOperationError(
            "iiko вернул карточку сотрудника в неподдерживаемом формате",
            status_code=502,
        ) from exc

    employee = _find_employee_xml_element(root)
    if employee is None:
        raise IikoEmployeeOperationError("iiko не вернул карточку сотрудника", status_code=502)
    _set_xml_text(employee, "deleted", "true")
    return ET.tostring(employee, encoding="utf-8", xml_declaration=True)


def _build_iiko_employee_update_xml(
    data: bytes,
    *,
    full_name: str | None,
    role: IikoEmployeeRole | None,
) -> bytes:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise IikoEmployeeOperationError(
            "iiko вернул карточку сотрудника в неподдерживаемом формате",
            status_code=502,
        ) from exc

    employee = _find_employee_xml_element(root)
    if employee is None:
        raise IikoEmployeeOperationError("iiko не вернул карточку сотрудника", status_code=502)

    if full_name is not None:
        _set_xml_text(employee, "name", full_name)
    if role is not None:
        _set_xml_text(employee, "mainRoleId", role.id)
        _set_xml_text(employee, "rolesIds", role.id)
        _set_xml_text(employee, "mainRoleCode", role.code or "")
        _set_xml_text(employee, "roleCodes", role.code or "")

    return ET.tostring(employee, encoding="utf-8", xml_declaration=True)


def _find_employee_xml_element(root: ET.Element) -> ET.Element | None:
    if _local_xml_name(root.tag) in {"employee", "employeeDto"}:
        return root
    for child in root.iter():
        if _local_xml_name(child.tag) in {"employee", "employeeDto"}:
            return child
    return None


def _set_xml_text(root: ET.Element, tag: str, value: str) -> None:
    element = _find_direct_xml_child(root, tag)
    if element is None:
        element = ET.SubElement(root, _xml_child_tag(root, tag))
    element.text = value


def _find_direct_xml_child(root: ET.Element, tag: str) -> ET.Element | None:
    for child in list(root):
        if _local_xml_name(child.tag) == tag:
            return child
    return None


def _xml_child_tag(root: ET.Element, tag: str) -> str:
    if root.tag.startswith("{") and "}" in root.tag:
        namespace = root.tag.split("}", 1)[0][1:]
        return f"{{{namespace}}}{tag}"
    return tag


def _local_xml_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_child_text(root: ET.Element, tag: str) -> str:
    element = _find_direct_xml_child(root, tag)
    if element is None or element.text is None:
        return ""
    return element.text.strip()


def _extract_hire_date_from_employee_xml(data: bytes) -> date | None:
    if not data.strip():
        return None
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    employee = _find_employee_xml_element(root)
    if employee is None:
        return None
    return _parse_iiko_date_text(
        _xml_child_text(employee, "hireDate")
        or _xml_child_text(employee, "hire_date")
        or _xml_child_text(employee, "employmentDate")
    )


def _extract_iiko_id_from_response(export_employees: ModuleType, data: bytes) -> str | None:
    if not data.strip():
        return None
    records = export_employees.records_from_response("/employees", data)
    if not records:
        return None
    return _extract_iiko_id(records[0]) or None


def _extract_hire_date_from_response(export_employees: ModuleType, data: bytes) -> date | None:
    if not data.strip():
        return None
    try:
        records = list(export_employees.records_from_response("/employees", data))
    except Exception:
        records = []
    if records:
        hire_date = _first_date(records[0], "hireDate", "hire_date", "employmentDate")
        if hire_date is not None:
            return hire_date
    return _extract_hire_date_from_employee_xml(data)


def _iiko_employee_role_for_position(
    roles: Iterable[IikoEmployeeRole],
    position: str,
) -> IikoEmployeeRole | None:
    for role in roles:
        if role.deleted:
            continue
        if canonical_position_name(role.name) == position:
            return role
    return None


def _parse_iiko_date_text(value: str) -> date | None:
    if not value:
        return None
    for separator in ("T", " "):
        value = value.split(separator, 1)[0]
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _iiko_operation_error(operation: str, exc: Exception) -> IikoEmployeeOperationError:
    status = getattr(exc, "status", None)
    message = getattr(exc, "message", None) or str(exc)
    message = _sanitize_iiko_error_message(message)
    detail = f"Не удалось {operation}"
    if status:
        detail = f"{detail}: iiko вернул HTTP {status}"
    if message:
        detail = f"{detail}: {message}"
    return IikoEmployeeOperationError(detail)


def _sanitize_iiko_error_message(message: str) -> str:
    text = str(message).replace("\n", " ").replace("\r", " ").strip()
    return text[:500]


async def sync_employees(
    session: AsyncSession | None = None,
    *,
    iiko_records: Iterable[Mapping[str, Any]] | None = None,
    run_reason: str = "manual",
    mode: SyncMode = "incremental",
    today: date | None = None,
    now: datetime | None = None,
) -> SyncResult:
    if mode not in ("incremental", "reset"):
        raise ValueError("Employee sync mode must be 'incremental' or 'reset'")

    owns_session = session is None
    if owns_session:
        async with AsyncSessionLocal() as owned_session:
            return await sync_employees(
                owned_session,
                iiko_records=iiko_records,
                run_reason=run_reason,
                mode=mode,
                today=today,
                now=now,
            )

    assert session is not None
    await _load_source_credential_env(session)

    sync_today = today or datetime.now(UTC).date()
    sync_now = now or datetime.now(UTC)
    records = (
        list(iiko_records)
        if iiko_records is not None
        else await anyio.to_thread.run_sync(fetch_iiko_employee_records)
    )

    agent_run = AgentRun(
        agent_name="iiko_employee_sync",
        status="running",
        params={"reason": run_reason, "mode": mode},
        result={},
    )
    session.add(agent_run)
    await session.flush()

    try:
        result = await _apply_employee_records(
            session,
            records,
            agent_run.id,
            sync_today,
            sync_now,
            mode=mode,
        )
        agent_run.status = "success"
        agent_run.finished_at = datetime.now(UTC)
        agent_run.result = result.audit_dict()
        await session.commit()
        return result
    except Exception as exc:
        agent_run.status = "failed"
        failed_at = datetime.now(UTC)
        agent_run.finished_at = failed_at
        agent_run.result = {"error": str(exc)[:500]}
        await employee_change_event_service.add_iiko_error_event(
            session,
            error_message=str(exc),
            changed_at=failed_at,
            related_agent_run_id=agent_run.id,
        )
        await session.commit()
        raise


async def _apply_employee_records(
    session: AsyncSession,
    records: Iterable[Mapping[str, Any]],
    agent_run_id: Any,
    sync_today: date,
    sync_now: datetime,
    *,
    mode: SyncMode = "incremental",
) -> SyncResult:
    existing_by_iiko_id = {
        employee.iiko_id: employee for employee in (await session.scalars(select(Employee))).all()
    }
    plan = plan_employee_sync(existing_by_iiko_id, records, sync_today, sync_now, mode=mode)

    for employee in plan.created_employees:
        session.add(employee)
    await session.flush()

    if mode == "reset":
        plan.result.ghost_cleaned = await _delete_ghost_employees_without_real_payroll_history(
            session,
            plan.ghost_cleanup_employees,
        )
        plan.result.existing_assignments_preserved = True

    for mutation in plan.mutations:
        after = _employee_snapshot(mutation.employee)
        if mutation.note:
            after = {**after, "notes": mutation.note}
        if _mutation_changed_position(mutation, after):
            await employee_effective_event_service.set_position(
                session,
                mutation.employee.id,
                str(after["position"]),
                effective_from=sync_today,
                comment="IIko sync",
            )
        action_id = _add_action(
            session,
            agent_run_id,
            mutation.action_type,
            mutation.employee,
            before=mutation.before,
            after=after,
        )
        await employee_change_event_service.add_iiko_sync_event(
            session,
            action_type=mutation.action_type,
            employee_id=mutation.employee.id,
            before=mutation.before,
            after=after,
            changed_at=sync_now,
            related_agent_run_id=agent_run_id,
            related_agent_action_id=action_id,
        )

    for skipped in plan.skipped_records:
        action_id = _add_skipped_action(session, agent_run_id, skipped)
        await employee_change_event_service.add_iiko_skipped_event(
            session,
            iiko_id=skipped.iiko_id,
            reason=skipped.reason,
            changed_at=sync_now,
            related_agent_run_id=agent_run_id,
            related_agent_action_id=action_id,
        )

    return plan.result


def _mutation_changed_position(
    mutation: EmployeeMutation,
    after: Mapping[str, Any],
) -> bool:
    position = after.get("position")
    if not position:
        return False
    if mutation.action_type == "create":
        return True
    return (mutation.before or {}).get("position") != position


def plan_employee_sync(
    existing_by_iiko_id: dict[str, Employee],
    records: Iterable[Mapping[str, Any]],
    sync_today: date,
    sync_now: datetime,
    *,
    mode: SyncMode = "incremental",
) -> EmployeeSyncPlan:
    if mode not in ("incremental", "reset"):
        raise ValueError("Employee sync mode must be 'incremental' or 'reset'")

    result = SyncResult()
    mutations: list[EmployeeMutation] = []
    created_employees: list[Employee] = []
    skipped_records: list[SkippedEmployeeRecord] = []
    ghost_cleanup_employees: list[Employee] = []
    seen_iiko_ids: set[str] = set()

    for raw_record in records:
        raw_iiko_id = _extract_iiko_id(raw_record)
        existing_employee = existing_by_iiko_id.get(raw_iiko_id) if raw_iiko_id else None
        record = normalize_iiko_employee(
            raw_record,
            allow_non_target_position=existing_employee is not None,
        )
        if record is None:
            skipped_records.append(
                SkippedEmployeeRecord(
                    raw_iiko_id or None,
                    _skip_reason_for_raw_record(
                        raw_record, allow_non_target_position=existing_employee is not None
                    ),
                )
            )
            continue

        seen_iiko_ids.add(record.iiko_id)
        employee = existing_by_iiko_id.get(record.iiko_id)
        is_active_target = _is_active_target_employee(record)
        if employee is None:
            if not is_active_target:
                continue
            employee = Employee(
                full_name=record.full_name,
                iiko_id=record.iiko_id,
                position=record.position,
                category=None,
                default_cooking_station=None,
                is_senior=False,
                is_deputy_senior=False,
                hire_date=record.hire_date,
                tenure_started_at=record.hire_date,
                pin_assumed_from_iiko=True,
                iiko_sync_at=sync_now,
            )
            employee.status = compute_status(
                employee,
                is_iiko_deleted=False,
                position_group=position_group_for_position(employee.position),
            )
            existing_by_iiko_id[record.iiko_id] = employee
            created_employees.append(employee)
            result.created += 1
            mutations.append(EmployeeMutation("create", employee, None))
            continue

        before = _employee_snapshot(employee)
        changed = False
        deactivated = False

        if not is_active_target:
            if employee.full_name != record.full_name:
                employee.full_name = record.full_name
                changed = True
            canonical_position = canonical_position_name(record.position)
            if canonical_position is not None and employee.position != canonical_position:
                employee.position = canonical_position
                changed = True
            if employee.status != "inactive" or employee.fire_date is None:
                employee.status = "inactive"
                employee.fire_date = record.fire_date or sync_today
                deactivated = True
                changed = True
        else:
            if employee.full_name != record.full_name:
                employee.full_name = record.full_name
                changed = True
            canonical_position = canonical_position_name(record.position)
            if canonical_position is not None and employee.position != canonical_position:
                employee.position = canonical_position
                changed = True
            if record.hire_date and employee.hire_date != record.hire_date:
                employee.hire_date = record.hire_date
                changed = True
            if mode == "incremental":
                if (
                    not is_cook_position(employee.position)
                    and employee.default_cooking_station is not None
                ):
                    employee.default_cooking_station = None
                    changed = True
                if employee.fire_date is not None:
                    employee.fire_date = None
                    changed = True
                computed_status = compute_status(
                    employee,
                    is_iiko_deleted=False,
                    position_group=position_group_for_position(employee.position),
                )
                if employee.status != computed_status:
                    employee.status = computed_status
                    changed = True

        employee.iiko_sync_at = sync_now

        if deactivated:
            result.deactivated += 1
            mutations.append(EmployeeMutation("deactivate", employee, before))
        elif changed:
            result.updated += 1
            mutations.append(EmployeeMutation("update", employee, before))

    if mode == "reset":
        for employee in existing_by_iiko_id.values():
            if employee.iiko_id in seen_iiko_ids or _is_ghost_employee(employee):
                continue
            before = _employee_snapshot(employee)
            deactivated = False
            if employee.status != "inactive":
                employee.status = "inactive"
                deactivated = True
            if employee.fire_date is None:
                employee.fire_date = sync_today
                deactivated = True
            employee.iiko_sync_at = sync_now
            if deactivated:
                result.deactivated += 1
                mutations.append(
                    EmployeeMutation(
                        "deactivate",
                        employee,
                        before,
                        note=RESET_MISSING_NOTE,
                    )
                )

    for employee in existing_by_iiko_id.values():
        if not _is_ghost_employee(employee):
            continue
        ghost_cleanup_employees.append(employee)
        before = _employee_snapshot(employee)
        changed = False
        if employee.status != "inactive":
            employee.status = "inactive"
            changed = True
        if employee.fire_date is None:
            employee.fire_date = sync_today
            changed = True
        employee.iiko_sync_at = sync_now
        if changed:
            result.deactivated += 1
            mutations.append(
                EmployeeMutation(
                    "deactivate",
                    employee,
                    before,
                    note=GHOST_CLEANUP_NOTE,
                )
            )

    return EmployeeSyncPlan(
        result=result,
        mutations=mutations,
        created_employees=created_employees,
        skipped_records=skipped_records,
        ghost_cleanup_employees=ghost_cleanup_employees,
    )


def normalize_iiko_employee(
    raw_record: Mapping[str, Any],
    *,
    allow_non_target_position: bool = True,
) -> IikoEmployeeRecord | None:
    iiko_id = _extract_iiko_id(raw_record)
    if not iiko_id:
        return None

    full_name = _extract_full_name(raw_record, iiko_id)
    if not full_name:
        return None

    raw_position = _extract_position(raw_record)
    position = canonical_position_name(raw_position) or raw_position
    is_deleted = _is_deleted(raw_record)
    if not allow_non_target_position and not is_deleted and not is_target_position(position):
        return None

    return IikoEmployeeRecord(
        iiko_id=iiko_id,
        full_name=full_name,
        position=position,
        is_deleted=is_deleted,
        is_service_account=_is_service_account(raw_record, full_name),
        hire_date=_first_date(raw_record, "hireDate", "hire_date", "employmentDate"),
        fire_date=_first_date(raw_record, "fireDate", "fire_date", "terminationDate"),
    )


def _skip_reason_for_raw_record(
    raw_record: Mapping[str, Any],
    *,
    allow_non_target_position: bool,
) -> str:
    iiko_id = _extract_iiko_id(raw_record)
    full_name = _extract_full_name(raw_record, iiko_id) if iiko_id else None
    raw_position = _extract_position(raw_record)
    position = canonical_position_name(raw_position) or raw_position
    if (
        iiko_id
        and full_name
        and raw_position
        and not allow_non_target_position
        and not _is_deleted(raw_record)
        and not is_target_position(position)
    ):
        return SKIP_NON_CANONICAL_OR_UNSUPPORTED_POSITION
    return SKIP_MISSING_NAME_OR_POSITION


def _enrich_employee_records_with_roles(
    employee_records: Iterable[Mapping[str, Any]],
    role_records: Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    roles_by_id: dict[str, str] = {}
    roles_by_code: dict[str, str] = {}
    for role in role_records:
        role_name = _first_text(role, "name", "title", "roleName")
        if not role_name:
            continue
        role_id = _first_text(role, "id", "roleId")
        role_code = _first_text(role, "code", "roleCode")
        if role_id:
            roles_by_id[role_id] = role_name
        if role_code:
            roles_by_code[role_code] = role_name

    enriched_records: list[Mapping[str, Any]] = []
    for record in employee_records:
        position = _extract_position(record)
        if not position:
            main_role_id = _first_text(record, "mainRoleId", "main_role_id", "roleId")
            main_role_code = _first_text(record, "mainRoleCode", "main_role_code", "roleCode")
            position = roles_by_id.get(main_role_id) or roles_by_code.get(main_role_code)
        if position:
            enriched_record = dict(record)
            enriched_record["position"] = position
            enriched_records.append(enriched_record)
        else:
            enriched_records.append(record)
    return enriched_records


def _extract_position(record: Mapping[str, Any]) -> str | None:
    position = _first_text(
        record,
        "position",
        "jobTitle",
        "job_title",
        "title",
        "mainRoleName",
        "main_role_name",
        "roleName",
        "role_name",
    )
    return position or None


def _is_active_target_employee(record: IikoEmployeeRecord) -> bool:
    return (
        not record.is_deleted
        and not record.is_service_account
        and is_target_position(record.position)
    )


def _is_service_account(record: Mapping[str, Any], full_name: str) -> bool:
    code = _first_text(record, "code", "employeeCode").casefold()
    login = _first_text(record, "login", "customUserName", "userName", "username").casefold()
    normalized_name = full_name.casefold()
    if code.startswith("dxbx") or login.startswith("dxbx"):
        return True
    if login in SERVICE_ACCOUNT_LOGINS:
        return True
    return any(marker in normalized_name for marker in SERVICE_ACCOUNT_NAME_MARKERS)


async def _delete_ghost_employees_without_real_payroll_history(
    session: AsyncSession,
    employees: Iterable[Employee],
) -> int:
    cleanup_candidates = [
        employee
        for employee in employees
        if employee.id is not None and _is_ghost_employee(employee)
    ]
    ids = list(dict.fromkeys(employee.id for employee in cleanup_candidates))
    if not ids:
        return 0

    protected_ids = await _employee_ids_with_real_payroll_lines(session, ids)
    cleanup_ids = [employee_id for employee_id in ids if employee_id not in protected_ids]
    if not cleanup_ids:
        return 0

    await session.execute(
        delete(ShiftLedgerEntry).where(ShiftLedgerEntry.employee_id.in_(cleanup_ids))
    )
    await session.execute(
        delete(EmployeeRoleAssignment).where(EmployeeRoleAssignment.employee_id.in_(cleanup_ids))
    )
    await session.execute(delete(PayrollLine).where(PayrollLine.employee_id.in_(cleanup_ids)))
    for employee in cleanup_candidates:
        if employee.id in cleanup_ids:
            await session.delete(employee)
    await session.flush()
    return len(cleanup_ids)


async def _employee_ids_with_real_payroll_lines(
    session: AsyncSession,
    employee_ids: Iterable[Any],
) -> set[Any]:
    ids = list(dict.fromkeys(employee_ids))
    if not ids:
        return set()

    amount_filter = or_(
        PayrollLine.base_pay != 0,
        PayrollLine.premium != 0,
        PayrollLine.percent_pay != 0,
        PayrollLine.fund_accrual != 0,
        PayrollLine.deduction != 0,
        PayrollLine.total_payable != 0,
    )
    return set(
        (
            await session.scalars(
                select(PayrollLine.employee_id)
                .where(PayrollLine.employee_id.in_(ids), amount_filter)
                .distinct()
            )
        ).all()
    )


def _extract_iiko_id(record: Mapping[str, Any]) -> str:
    return _first_text(record, *IIKO_ID_KEYS)


def _extract_full_name(record: Mapping[str, Any], iiko_id: str) -> str | None:
    for candidate in _candidate_texts(
        record,
        "fullName",
        "full_name",
        "name",
        "displayName",
        "fio",
        "employeeName",
    ):
        if not _is_identifier_fallback_name(candidate, record, iiko_id):
            return candidate

    composed_name = " ".join(
        part
        for part in (
            _first_text(record, "lastName", "last_name", "surname"),
            _first_text(record, "firstName", "first_name"),
            _first_text(record, "middleName", "middle_name", "patronymic"),
        )
        if part
    ).strip()
    if composed_name and not _is_identifier_fallback_name(composed_name, record, iiko_id):
        return composed_name

    for candidate in _candidate_texts(record, "customUserName", "userName", "username"):
        if not _is_identifier_fallback_name(candidate, record, iiko_id):
            return candidate

    return None


def _candidate_texts(record: Mapping[str, Any], *keys: str) -> Iterable[str]:
    seen: set[str] = set()
    for key in keys:
        candidate = _first_text(record, key)
        normalized = candidate.casefold()
        if candidate and normalized not in seen:
            seen.add(normalized)
            yield candidate


def _is_identifier_fallback_name(
    value: str,
    record: Mapping[str, Any],
    iiko_id: str,
) -> bool:
    if _is_uuid_like_text(value):
        return True
    normalized = value.casefold()
    identifiers = {iiko_id}
    identifiers.update(_first_text(record, key) for key in IIKO_ID_KEYS)
    return normalized in {identifier.casefold() for identifier in identifiers if identifier}


def _is_uuid_like_text(value: str) -> bool:
    text = value.strip().strip("{}")
    try:
        parsed = uuid.UUID(text)
    except ValueError:
        return False
    normalized = text.casefold()
    return normalized in {str(parsed), parsed.hex}


def _is_ghost_employee(employee: Employee) -> bool:
    return employee.position is None and _is_uuid_like_text(employee.full_name)


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
        "default_cooking_station": employee.default_cooking_station,
        "is_senior": employee.is_senior,
        "is_deputy_senior": employee.is_deputy_senior,
        "status": employee.status,
        "hire_date": employee.hire_date.isoformat() if employee.hire_date else None,
        "fire_date": employee.fire_date.isoformat() if employee.fire_date else None,
        "pin_assumed_from_iiko": employee.pin_assumed_from_iiko,
    }


def _add_action(
    session: AsyncSession,
    agent_run_id: Any,
    action_type: str,
    employee: Employee,
    *,
    before: dict[str, Any] | None,
    after: dict[str, Any],
) -> uuid.UUID:
    action_id = uuid.uuid4()
    session.add(
        AgentAction(
            id=action_id,
            agent_run_id=agent_run_id,
            action_type=action_type,
            target_table="employee",
            target_id=employee.id,
            before_value=before,
            after_value=after,
        )
    )
    return action_id


def _add_skipped_action(
    session: AsyncSession,
    agent_run_id: Any,
    skipped: SkippedEmployeeRecord,
) -> uuid.UUID:
    action_id = uuid.uuid4()
    session.add(
        AgentAction(
            id=action_id,
            agent_run_id=agent_run_id,
            action_type=f"skipped: {skipped.reason}",
            target_table="employee",
            target_id=None,
            before_value=None,
            after_value={
                "iiko_id": skipped.iiko_id,
                "reason": skipped.reason,
            },
        )
    )
    return action_id

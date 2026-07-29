from __future__ import annotations

import contextlib
import hashlib
import http.client
import importlib
import json
import os
import re
import sys
import time
import urllib.error
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

from app.api.deps import CurrentActor
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
from app.services import employee_position_service, payroll_config, position_registry
from app.services.employee_status import (
    compute_status,
    is_cook_position,
    is_target_position,
    position_group_for_position,
)
from app.services.staff_taxonomy import canonical_position_name, target_position_for_payroll_role

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


@dataclass(frozen=True, slots=True)
class IikoRoleUpsertResult:
    role_id: str
    code: str
    name: str
    schedule_type: str


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
        if (
            isinstance(value, str)
            and key.startswith(("IIKO_SERVER_", "IIKO_CLOUD_"))
            and not os.environ.get(key)
        ):
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
    pin_code: str | None = None,
) -> IikoEmployeeUpdateResult:
    await _load_source_credential_env(session)
    return await anyio.to_thread.run_sync(
        _update_iiko_employee_sync,
        iiko_id,
        full_name,
        position,
        pin_code,
    )


async def upsert_iiko_role(
    session: AsyncSession,
    *,
    name: str,
    code: str,
    role_id: str | None = None,
    schedule_type: str = "SESSION",
) -> IikoRoleUpsertResult:
    """Создать/обновить роль в iiko: PUT /resto/api/employees/roles/byCode/{code}."""
    await _load_source_credential_env(session)
    return await anyio.to_thread.run_sync(
        _upsert_iiko_role_sync,
        name.strip(),
        code.strip(),
        (role_id or "").strip() or None,
        schedule_type.strip() or "SESSION",
    )


async def delete_iiko_role(session: AsyncSession, *, role_id: str) -> None:
    """Удалить роль в iiko: DELETE /resto/api/employees/roles/byId/{id}."""
    await _load_source_credential_env(session)
    await anyio.to_thread.run_sync(_delete_iiko_role_sync, role_id.strip())


async def sync_employee_roles_to_iiko(session: AsyncSession, employee_id: uuid.UUID) -> None:
    employee = await session.get(Employee, employee_id)
    if employee is None or not employee.iiko_id:
        return

    canonical_main_position = canonical_position_name(employee.position)
    if canonical_main_position is None:
        return

    today = datetime.now(UTC).date()
    assignments = (
        await session.scalars(
            select(EmployeeRoleAssignment).where(
                EmployeeRoleAssignment.employee_id == employee_id,
                EmployeeRoleAssignment.is_substitute.is_(True),
                EmployeeRoleAssignment.effective_from <= today,
                or_(
                    EmployeeRoleAssignment.effective_to.is_(None),
                    EmployeeRoleAssignment.effective_to > today,
                ),
            )
        )
    ).all()
    target_positions = {canonical_main_position}
    for assignment in assignments:
        try:
            target_positions.add(target_position_for_payroll_role(assignment.payroll_role))
        except KeyError:
            continue

    await _load_source_credential_env(session)
    await anyio.to_thread.run_sync(
        _sync_employee_roles_to_iiko_sync,
        employee.iiko_id,
        canonical_main_position,
        tuple(sorted(target_positions)),
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
        except urllib.error.URLError as exc:
            # iiko-сервер недоступен (отказ соединения / DNS / таймаут на уровне
            # сети). refresh_token() бросает сырой URLError мимо обработчиков
            # IikoHTTPError — оборачиваем в понятную 503, чтобы UI показывал
            # «iiko недоступен», а не невнятный 500/Network Error.
            raise IikoEmployeeOperationError(
                "iiko недоступен — сервер не отвечает. Попробуйте позже.",
                status_code=503,
            ) from exc
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


def _logout_iiko_client(client: Any) -> None:
    """iikoServer держит лицензию на токен — после write-операций освобождаем её."""
    token = getattr(client, "token", "")
    if not token:
        return
    # logout best-effort: если не вышло, лицензию отпустит таймаут iikoServer
    with contextlib.suppress(Exception):
        client.request("/logout")
    client.token = ""


def _upsert_iiko_role_sync(
    name: str,
    code: str,
    role_id: str | None,
    schedule_type: str,
) -> IikoRoleUpsertResult:
    if not name:
        raise IikoEmployeeOperationError("Название роли iiko обязательно", status_code=400)
    if not code:
        raise IikoEmployeeOperationError("Код роли iiko обязателен", status_code=400)

    export_employees = _load_export_employees_module()
    export_employees.load_local_env()
    client = export_employees.IikoClient()
    try:
        if role_id is None:
            existing = _fetch_iiko_employee_roles_with_client(
                export_employees,
                client,
                include_deleted=True,
            )
            matched = next((role for role in existing if (role.code or "") == code), None)
            role_id = matched.id if matched is not None else str(uuid.uuid4())
        body = _build_iiko_role_xml(
            role_id=role_id,
            code=code,
            name=name,
            schedule_type=schedule_type,
        )
        try:
            _request_iiko_with_incomplete_read_retry(
                client,
                f"/employees/roles/byCode/{code}",
                method="PUT",
                raw_body=body,
                content_type=IIKO_EMPLOYEE_XML_CONTENT_TYPE,
            )
        except export_employees.IikoHTTPError as exc:
            raise _iiko_operation_error("сохранить роль в iiko", exc) from exc

        # Подтверждаем фактический id/code роли после записи.
        roles = _fetch_iiko_employee_roles_with_client(
            export_employees,
            client,
            include_deleted=True,
        )
        saved = next(
            (role for role in roles if (role.code or "") == code and not role.deleted),
            None,
        )
        if saved is None:
            raise IikoEmployeeOperationError(
                f"iiko не подтвердил сохранение роли «{name}» (код {code})",
            )
        return IikoRoleUpsertResult(
            role_id=saved.id,
            code=saved.code or code,
            name=saved.name,
            schedule_type=schedule_type,
        )
    finally:
        _logout_iiko_client(client)


def _delete_iiko_role_sync(role_id: str) -> None:
    if not role_id:
        raise IikoEmployeeOperationError("У должности нет id роли iiko", status_code=400)

    export_employees = _load_export_employees_module()
    export_employees.load_local_env()
    client = export_employees.IikoClient()
    try:
        try:
            _request_iiko_with_incomplete_read_retry(
                client,
                f"/employees/roles/byId/{role_id}",
                method="DELETE",
            )
        except export_employees.IikoHTTPError as exc:
            raise _iiko_operation_error("удалить роль в iiko", exc) from exc
    finally:
        _logout_iiko_client(client)


def _build_iiko_role_xml(
    *,
    role_id: str,
    code: str,
    name: str,
    schedule_type: str,
) -> bytes:
    root = ET.Element("role")
    _set_xml_text(root, "id", role_id)
    _set_xml_text(root, "code", code)
    _set_xml_text(root, "name", name)
    _set_xml_text(root, "scheduleType", schedule_type)
    _set_xml_text(root, "paymentPerHour", "0")
    _set_xml_text(root, "steadySalary", "0")
    _set_xml_text(root, "comment", "")
    _set_xml_text(root, "deleted", "false")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


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
    pin_code: str | None = None,
) -> IikoEmployeeUpdateResult:
    iiko_id = iiko_id.strip()
    if not iiko_id:
        raise IikoEmployeeOperationError("У сотрудника нет iiko id", status_code=400)

    target_full_name = full_name.strip() if full_name is not None else None
    if full_name is not None and not target_full_name:
        raise IikoEmployeeOperationError("ФИО сотрудника обязательно", status_code=400)

    # pin_code == "" — явный сброс ПИН плейсхолдера (при архивации внештатника);
    # непустое значение — установка ПИН нового внештатника.
    target_pin: str | None = None
    if pin_code is not None:
        target_pin = pin_code.strip()
        if target_pin and re.fullmatch(r"\d{4}", target_pin) is None:
            raise IikoEmployeeOperationError(
                "ПИН-код должен состоять из 4 цифр", status_code=400
            )

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
            pin_code=target_pin,
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


def _sync_employee_roles_to_iiko_sync(
    iiko_id: str,
    main_position: str,
    target_positions: tuple[str, ...],
) -> None:
    iiko_id = iiko_id.strip()
    if not iiko_id:
        raise IikoEmployeeOperationError("У сотрудника нет iiko id", status_code=400)

    export_employees = _load_export_employees_module()
    export_employees.load_local_env()
    client = export_employees.IikoClient()
    roles = _fetch_iiko_employee_roles_with_client(
        export_employees,
        client,
        include_deleted=True,
    )
    main_role = _iiko_employee_role_for_position(roles, main_position)
    if main_role is None:
        raise IikoEmployeeOperationError(
            f"В iiko не найдена роль для должности «{main_position}»",
            status_code=400,
        )

    roles_by_position: dict[str, IikoEmployeeRole] = {main_position: main_role}
    for position in target_positions:
        role = _iiko_employee_role_for_position(roles, position)
        if role is None:
            raise IikoEmployeeOperationError(
                f"В iiko не найдена роль для должности «{position}»",
                status_code=400,
            )
        roles_by_position[position] = role

    ordered_roles = [
        roles_by_position[position]
        for position in [main_position, *sorted(set(target_positions) - {main_position})]
    ]

    try:
        _status, data = _request_iiko_with_incomplete_read_retry(
            client,
            f"/employees/byId/{iiko_id}",
        )
        body = _build_iiko_employee_roles_update_xml(
            data,
            main_role=main_role,
            roles=ordered_roles,
        )
        _request_iiko_with_incomplete_read_retry(
            client,
            f"/employees/byId/{iiko_id}",
            method="PUT",
            raw_body=body,
            content_type=IIKO_EMPLOYEE_XML_CONTENT_TYPE,
        )
    except export_employees.IikoHTTPError as exc:
        raise _iiko_operation_error("обновить роли сотрудника в iiko", exc) from exc


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
    pin_code: str | None = None,
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
    if pin_code is not None:
        # Непустой — установить ПИН нового внештатника; пустой — сброс при архивации.
        _set_xml_text(employee, "pinCode", pin_code)
    if role is not None:
        _set_xml_text(employee, "mainRoleId", role.id)
        _set_xml_text(employee, "rolesIds", role.id)
        _set_xml_text(employee, "mainRoleCode", role.code or "")
        _set_xml_text(employee, "roleCodes", role.code or "")

    return ET.tostring(employee, encoding="utf-8", xml_declaration=True)


def _build_iiko_employee_roles_update_xml(
    data: bytes,
    *,
    main_role: IikoEmployeeRole,
    roles: Iterable[IikoEmployeeRole],
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

    ordered_roles = list(dict.fromkeys(roles))
    _set_xml_text(employee, "mainRoleId", main_role.id)
    _set_xml_text(employee, "rolesIds", ",".join(role.id for role in ordered_roles))
    _set_xml_text(employee, "mainRoleCode", main_role.code or "")
    _set_xml_text(
        employee,
        "roleCodes",
        ",".join(role.code or "" for role in ordered_roles if role.code),
    )
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
    roles = list(roles)
    info = position_registry.position_info(position)
    if info is not None and info.iiko_role_id:
        for role in roles:
            if not role.deleted and role.id == info.iiko_role_id:
                return role
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
    if iiko_records is not None:
        records = list(iiko_records)
    else:
        records = await anyio.to_thread.run_sync(fetch_iiko_employee_records)
        if mode == "reset" and not records:
            # Сброс синхронизирует штат «как в iiko», в том числе увольняет тех, кого в выгрузке
            # нет. Пустая выгрузка означала бы «в iiko не осталось никого» и уволила бы весь штат
            # разом — а на практике это всегда сбой самой выгрузки (обрыв, лимит, пустой ответ
            # WAF). Гард стоит только на ЖИВОЙ выгрузке: явно переданные записи — осознанный
            # вызов (скрипты, тесты), там решает вызывающий. Инкрементальному режиму пустой
            # ответ безобиден: отсутствующих он не трогает.
            raise ValueError(
                "iiko вернул пустой список сотрудников — сброс отменён, чтобы не уволить весь штат"
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
    records = list(records)
    existing_by_iiko_id = {
        employee.iiko_id: employee for employee in (await session.scalars(select(Employee))).all()
    }
    plan = plan_employee_sync(existing_by_iiko_id, records, sync_today, sync_now, mode=mode)

    for employee in plan.created_employees:
        session.add(employee)
    await session.flush()

    system_actor = CurrentActor(roles=frozenset(), user_id=None)
    for mutation in plan.mutations:
        if mutation.action_type != "create":
            continue
        position = canonical_position_name(mutation.employee.position)
        if position is None:
            continue
        await employee_position_service.change_position(
            session,
            mutation.employee.id,
            position,
            effective_from=sync_today,
            comment="IIko sync initial position",
            actor=system_actor,
        )

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

    await process_employee_extra_roles_for_records(session, records)
    return plan.result


async def process_employee_extra_roles_for_records(
    session: AsyncSession,
    records: Iterable[Mapping[str, Any]],
    *,
    force: bool = False,
) -> None:
    records = list(records)
    iiko_ids = {_extract_iiko_id(record) for record in records}
    iiko_ids.discard("")
    if not iiko_ids:
        return
    employees_by_iiko_id = {
        employee.iiko_id: employee
        for employee in (
            await session.scalars(select(Employee).where(Employee.iiko_id.in_(iiko_ids)))
        ).all()
    }
    for record in records:
        employee = employees_by_iiko_id.get(_extract_iiko_id(record))
        if employee is None:
            continue
        if "extra_iiko_positions" not in record:
            continue
        await process_employee_extra_roles(session, employee, record, force=force)


async def refresh_role_review_for_all_employees(
    session: AsyncSession,
    *,
    force: bool = True,
) -> None:
    employees = (
        await session.scalars(select(Employee).where(Employee.role_review_payload.is_not(None)))
    ).all()
    for employee in employees:
        payload = employee.role_review_payload or {}
        if not isinstance(payload, Mapping):
            continue
        extra_positions = payload.get("extra_iiko_positions")
        if not isinstance(extra_positions, list):
            continue
        await process_employee_extra_roles(
            session,
            employee,
            {"extra_iiko_positions": extra_positions},
            force=force,
        )
    await session.commit()


async def process_employee_extra_roles(
    session: AsyncSession,
    employee: Employee,
    iiko_payload: Mapping[str, Any],
    *,
    force: bool = False,
) -> None:
    extra_positions = _canonical_extra_positions_from_payload(iiko_payload, employee.position)
    if not extra_positions:
        employee.requires_role_review = False
        employee.role_review_payload = None
        return

    pairs = await payroll_config.get_substitute_pairs(session)
    covered_positions = await _active_substitute_target_positions(session, employee.id)
    unresolved: list[str] = []
    reasons_by_position: dict[str, str] = {}
    for position in extra_positions:
        if not payroll_config.is_substitute_pair_allowed(pairs, employee.position, position):
            unresolved.append(position)
            reasons_by_position[position] = "unconfigured_pair"
        elif position not in covered_positions:
            unresolved.append(position)
            reasons_by_position[position] = "missing_substitute"

    if not unresolved:
        employee.requires_role_review = False
        employee.role_review_payload = None
        return

    roles_hash = _iiko_positions_hash(extra_positions)
    existing_payload = employee.role_review_payload or {}
    acknowledged_hash = (
        existing_payload.get("acknowledged_iiko_roles_hash")
        if isinstance(existing_payload, Mapping)
        else None
    )
    payload = {
        "extra_iiko_positions": unresolved,
        "all_extra_iiko_positions": extra_positions,
        "reason": (
            "unconfigured_pair"
            if any(reason == "unconfigured_pair" for reason in reasons_by_position.values())
            else "missing_substitute"
        ),
        "reasons_by_position": reasons_by_position,
        "iiko_roles_hash": roles_hash,
    }
    if acknowledged_hash:
        payload["acknowledged_iiko_roles_hash"] = acknowledged_hash

    if not force and acknowledged_hash == roles_hash:
        employee.requires_role_review = False
        employee.role_review_payload = payload
        return

    employee.requires_role_review = True
    employee.role_review_payload = payload


async def _active_substitute_target_positions(
    session: AsyncSession,
    employee_id: uuid.UUID,
) -> set[str]:
    today = datetime.now(UTC).date()
    assignments = (
        await session.scalars(
            select(EmployeeRoleAssignment).where(
                EmployeeRoleAssignment.employee_id == employee_id,
                EmployeeRoleAssignment.is_substitute.is_(True),
                EmployeeRoleAssignment.effective_from <= today,
                or_(
                    EmployeeRoleAssignment.effective_to.is_(None),
                    EmployeeRoleAssignment.effective_to > today,
                ),
            )
        )
    ).all()
    positions: set[str] = set()
    for assignment in assignments:
        try:
            positions.add(target_position_for_payroll_role(assignment.payroll_role))
        except KeyError:
            continue
    return positions


def _canonical_extra_positions_from_payload(
    payload: Mapping[str, Any],
    main_position: str | None,
) -> list[str]:
    raw_positions = payload.get("extra_iiko_positions") or []
    if not isinstance(raw_positions, Iterable) or isinstance(raw_positions, str | bytes | Mapping):
        return []
    main_canonical = canonical_position_name(main_position)
    positions: list[str] = []
    seen: set[str] = set()
    for item in raw_positions:
        canonical = canonical_position_name(str(item))
        if canonical is None or canonical == main_canonical or canonical in seen:
            continue
        seen.add(canonical)
        positions.append(canonical)
    return positions


def _iiko_positions_hash(positions: Iterable[str]) -> str:
    payload = "|".join(sorted(positions))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
                employee.requires_position_review = True
                changed = True
            elif canonical_position is not None and employee.requires_position_review:
                employee.requires_position_review = False
                changed = True
            # `dismissing` — липкий: сотрудник в процессе увольнения уже деактивирован
            # в iiko, синк НЕ должен сваливать его в inactive до закрытия расчётов.
            if (
                employee.status not in ("inactive", "dismissing")
                or employee.fire_date is None
            ):
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
                employee.requires_position_review = True
                changed = True
            elif canonical_position is not None and employee.requires_position_review:
                employee.requires_position_review = False
                changed = True
            if record.hire_date and employee.hire_date != record.hire_date:
                employee.hire_date = record.hire_date
                changed = True
            # `dismissing` не трогаем: не чистим fire_date и не пересчитываем статус,
            # даже если iiko временно ещё отдаёт сотрудника активным.
            if mode == "incremental" and employee.status != "dismissing":
                if (
                    not is_cook_position(employee.position)
                    and employee.default_cooking_station is not None
                ):
                    employee.default_cooking_station = None
                    changed = True
                if employee.fire_date is not None:
                    employee.fire_date = None
                    changed = True
                # NOTE: assignments are not loaded here (plan_employee_sync is
                # sync and has no session), so compute_status falls back to the
                # shortcut path (default_cooking_station + category). If the
                # shortcut goes stale, employee.status is later refreshed via
                # employee_assignments._refresh_employee_status on the next
                # role mutation.
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
            if (
                employee.iiko_id in seen_iiko_ids
                or _is_ghost_employee(employee)
                or employee.status == "dismissing"  # липкий: не сбрасывать reset'ом
            ):
                continue
            # Временные внештатники живут без своей iiko-карты (синтетический
            # iiko_id), поэтому их отсутствие в выгрузке iiko — норма, не увольнение.
            if getattr(employee, "is_freelancer_temp", False):
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
        if employee.status not in ("inactive", "dismissing"):
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
        main_role_id = _first_text(record, "mainRoleId", "main_role_id", "roleId")
        main_role_code = _first_text(record, "mainRoleCode", "main_role_code", "roleCode")
        if not position:
            position = roles_by_id.get(main_role_id) or roles_by_code.get(main_role_code)
        extra_positions = _extra_positions_from_iiko_roles(
            record,
            roles_by_id=roles_by_id,
            roles_by_code=roles_by_code,
            main_role_id=main_role_id,
            main_role_code=main_role_code,
            main_position=position,
        )
        if position or extra_positions:
            enriched_record = dict(record)
            if extra_positions:
                enriched_record["extra_iiko_positions"] = extra_positions
        if position:
            enriched_record["position"] = position
            enriched_records.append(enriched_record)
        elif extra_positions:
            enriched_records.append(enriched_record)
        else:
            enriched_records.append(record)
    return enriched_records


def _extra_positions_from_iiko_roles(
    record: Mapping[str, Any],
    *,
    roles_by_id: Mapping[str, str],
    roles_by_code: Mapping[str, str],
    main_role_id: str,
    main_role_code: str,
    main_position: str | None,
) -> list[str]:
    main_canonical = canonical_position_name(main_position)
    positions: list[str] = []
    seen: set[str] = set()

    for role_id in _split_iiko_multi_value(_first_text(record, "rolesIds", "roles_ids")):
        if role_id == main_role_id:
            continue
        canonical = canonical_position_name(roles_by_id.get(role_id))
        if canonical is None or canonical == main_canonical or canonical in seen:
            continue
        seen.add(canonical)
        positions.append(canonical)

    for role_code in _split_iiko_multi_value(_first_text(record, "roleCodes", "role_codes")):
        if role_code == main_role_code:
            continue
        canonical = canonical_position_name(roles_by_code.get(role_code))
        if canonical is None or canonical == main_canonical or canonical in seen:
            continue
        seen.add(canonical)
        positions.append(canonical)

    raw_extra = record.get("extra_iiko_positions")
    if isinstance(raw_extra, Iterable) and not isinstance(raw_extra, str | bytes | Mapping):
        for item in raw_extra:
            canonical = canonical_position_name(str(item))
            if canonical is None or canonical == main_canonical or canonical in seen:
                continue
            seen.add(canonical)
            positions.append(canonical)
    return positions


def _split_iiko_multi_value(value: str) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in re.split(r"[,;\s]+", value) if item.strip()]


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
        "requires_role_review": employee.requires_role_review,
        "requires_position_review": employee.requires_position_review,
        "role_review_payload": employee.role_review_payload,
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

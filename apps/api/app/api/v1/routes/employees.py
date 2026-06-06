from __future__ import annotations

import http.client as _http_client
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentActor, get_current_actor
from app.core.security import hash_password
from app.db.session import get_session
from app.models import (
    AgentAction,
    AgentRun,
    DepositAccount,
    DepositTransaction,
    Employee,
    EmployeeAllowanceEvent,
    EmployeeChangeEvent,
    EmployeeDismissalReason,
    EmployeePendingIikoAction,
    EmployeePositionAssignment,
    EmployeePositionEvent,
    EmployeeRoleAssignment,
    ShiftLedgerEntry,
)
from app.schemas.employees import (
    DepositDismissAction,
    EmployeeAllowanceEventRead,
    EmployeeChangeEventFilter,
    EmployeeChangeEventRead,
    EmployeeCreateRequest,
    EmployeeDismissalReasonCreate,
    EmployeeDismissalReasonRead,
    EmployeeDismissalReasonUpdate,
    EmployeeDismissRequest,
    EmployeeHireDateRequest,
    EmployeeNoticeActionRead,
    EmployeeNoticeCancelRequest,
    EmployeeNoticeInfo,
    EmployeeNoticeRequest,
    EmployeePatch,
    EmployeePatchRoleAssignment,
    EmployeePendingIikoActionRead,
    EmployeePinChangeRequest,
    EmployeePositionAssignmentRead,
    EmployeePositionChange,
    EmployeePositionEventRead,
    EmployeeRead,
    EmployeeRoleAssignmentCreate,
    EmployeeRoleAssignmentPatch,
    EmployeeRoleAssignmentRead,
    IikoEmployeeRoleRead,
    SyncResultRead,
)
from app.services import deposit_service, employee_position_service, notice_service
from app.services import employee_assignments as employee_assignment_service
from app.services import employee_change_events as employee_change_event_service
from app.services import employee_effective_events as employee_effective_event_service
from app.services.accumulation_fund_service import forfeit_active_fund_on_dismiss
from app.services.employee_status import (
    COOKING_STATIONS,
    EMPLOYEE_CATEGORIES,
    EMPLOYEE_STATUSES,
    compute_status,
    is_cook_position,
    position_group_for_position,
)
from app.services.iiko_sync import (
    IikoEmployeeOperationError,
    IikoEmployeeRole,
    get_iiko_employee_roles,
    sync_employees,
    update_iiko_employee,
)
from app.services.iiko_sync import (
    create_iiko_employee as create_iiko_employee_in_iiko,
)
from app.services.iiko_sync import (
    dismiss_iiko_employee as dismiss_iiko_employee_in_iiko,
)
from app.services.payroll_calculator import decimal
from app.services.shift_ledger import ledger_entry_snapshot
from app.services.staff_access import (
    StaffAction,
    StaffArea,
    can_access_position,
    employee_access_filter,
    employee_ids_with_staff_access,
    ensure_any_staff_access,
    ensure_employee_access,
    ensure_position_access,
    ensure_staff_area_access,
    filter_employees_by_staff_access,
)
from app.services.staff_taxonomy import (
    PAYROLL_ROLE_LABELS,
    canonical_position_name,
    categories_for_payroll_role,
    is_create_position,
    payroll_roles_for_position,
    reset_inapplicable_premiums,
    validate_premiums,
)

router = APIRouter()


async def _require_staff_read(actor: Annotated[CurrentActor, Depends(get_current_actor)]) -> None:
    ensure_any_staff_access(actor, StaffAction.READ)


async def _require_staff_history(
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> None:
    ensure_any_staff_access(actor, StaffAction.HISTORY_READ)


async def _require_staff_create(
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> None:
    ensure_any_staff_access(actor, StaffAction.CREATE)


async def _require_staff_edit(actor: Annotated[CurrentActor, Depends(get_current_actor)]) -> None:
    ensure_any_staff_access(actor, StaffAction.EDIT)


async def _require_staff_dismiss(
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> None:
    ensure_any_staff_access(actor, StaffAction.DISMISS)


async def _require_staff_reinstate(
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> None:
    ensure_any_staff_access(actor, StaffAction.REINSTATE)


async def _require_staff_administration_edit(
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> None:
    ensure_staff_area_access(actor, StaffArea.ADMINISTRATION, StaffAction.EDIT)


STAFF_READ_ACCESS = (Depends(_require_staff_read),)
STAFF_HISTORY_ACCESS = (Depends(_require_staff_history),)
STAFF_CREATE_ACCESS = (Depends(_require_staff_create),)
STAFF_WRITE_ACCESS = (Depends(_require_staff_edit),)
STAFF_DISMISS_ACCESS = (Depends(_require_staff_dismiss),)
STAFF_REINSTATE_ACCESS = (Depends(_require_staff_reinstate),)
STAFF_SYNC_ACCESS = (Depends(_require_staff_administration_edit),)
MONEY_STEP = Decimal("0.01")

READ_ONLY_FIELDS = {
    "id",
    "iiko_id",
    "pin_assumed_from_iiko",
    "iiko_sync_at",
    "created_at",
    "updated_at",
}
APP_MANAGED_FIELDS = {
    "full_name",
    "position",
    "category",
    "default_cooking_station",
    "is_senior",
    "is_deputy_senior",
    "hire_date",
    "fire_date",
    "requires_role_review",
    "pin_code",
    "roles",
}
PATCH_META_FIELDS = {"effective_from", "comment", "transfer_from_existing"}
COMPUTED_FIELDS = {"status"}
ALLOWED_CREATE_PAYROLL_ROLES = frozenset(PAYROLL_ROLE_LABELS)
PAYROLL_EFFECTIVE_FIELDS = {
    "position",
    "category",
    "default_cooking_station",
    "is_senior",
    "is_deputy_senior",
    "roles",
}


@dataclass(frozen=True, slots=True)
class ResolvedCreateRole:
    payroll_role: str
    category: str
    is_primary: bool


@dataclass(frozen=True, slots=True)
class DismissDepositDecision:
    action: DepositDismissAction
    payout_amount: Decimal
    writeoff_amount: Decimal
    balance: Decimal


@router.get("", response_model=list[EmployeeRead], dependencies=STAFF_READ_ACCESS)
@router.get(
    "/",
    response_model=list[EmployeeRead],
    include_in_schema=False,
    dependencies=STAFF_READ_ACCESS,
)
async def list_employees(
    session: Annotated[AsyncSession, Depends(get_session)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    category: str | None = None,
    cooking_station: Annotated[str | None, Query(alias="cooking_station")] = None,
    search: str | None = None,
    include_pending: bool = False,
    actor: Annotated[CurrentActor | None, Depends(get_current_actor)] = None,
) -> list[Employee] | list[EmployeeRead]:
    today = date.today()
    query = select(Employee).options(selectinload(Employee.role_assignments))
    if actor is not None:
        query = query.where(employee_access_filter(actor, StaffAction.READ))
    if status_filter:
        if status_filter not in EMPLOYEE_STATUSES:
            raise HTTPException(status_code=400, detail="Некорректный статус сотрудника")
        query = query.where(Employee.status == status_filter)
    if category:
        if category not in EMPLOYEE_CATEGORIES:
            raise HTTPException(status_code=400, detail="Некорректная категория сотрудника")
        query = query.where(_active_assignment_exists(today, category=category))
    if cooking_station:
        if cooking_station not in COOKING_STATIONS:
            raise HTTPException(status_code=400, detail="Некорректная станция кухни")
        query = query.where(_active_assignment_exists(today, payroll_role=cooking_station))
    if search:
        query = query.where(Employee.full_name.ilike(f"%{search}%"))

    result = await session.scalars(query.order_by(Employee.full_name))
    employees = list(result.all())
    if actor is not None:
        employees = filter_employees_by_staff_access(employees, actor, StaffAction.READ)
    await _attach_active_notices(session, employees, today)
    if include_pending:
        rows: list[EmployeeRead] = []
        for employee in employees:
            payload = EmployeeRead.model_validate(employee)
            pending_assignments = await employee_assignment_service.get_assignments_with_pending(
                session,
                employee.id,
                today,
            )
            payload.assignments = [
                EmployeeRoleAssignmentRead.model_validate(assignment)
                for assignment in pending_assignments
            ]
            rows.append(payload)
        return rows
    return employees


@router.post("/sync", response_model=SyncResultRead, dependencies=STAFF_SYNC_ACCESS)
async def trigger_employee_sync(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    mode: Annotated[Literal["incremental", "reset"], Query()] = "incremental",
) -> dict[str, int]:
    try:
        result = await sync_employees(session, run_reason="manual", mode=mode)
    except _http_client.IncompleteRead as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="iiko не отвечает — сервер оборвал соединение. Попробуйте через минуту.",
        ) from exc
    return result.as_dict()


@router.get(
    "/iiko-roles",
    response_model=list[IikoEmployeeRoleRead],
    dependencies=STAFF_CREATE_ACCESS,
)
async def list_iiko_employee_roles(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list[IikoEmployeeRoleRead]:
    try:
        roles = await get_iiko_employee_roles(session)
    except _http_client.IncompleteRead as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="iiko не отвечает — сервер оборвал соединение. Попробуйте через минуту.",
        ) from exc
    except IikoEmployeeOperationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    return [
        IikoEmployeeRoleRead(
            id=role.id,
            name=role.name,
            code=role.code,
            deleted=role.deleted,
        )
        for role in roles
        if is_create_position(role.name)
        and can_access_position(actor, role.name, StaffAction.CREATE)
    ]


@router.get(
    "/changes",
    response_model=list[EmployeeChangeEventRead],
    dependencies=STAFF_HISTORY_ACCESS,
)
async def list_employee_changes(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_context: Annotated[CurrentActor, Depends(get_current_actor)],
    employee_id: uuid.UUID | None = None,
    changed_from: datetime | None = None,
    changed_to: datetime | None = None,
    effective_from: date | None = None,
    effective_to: date | None = None,
    change_type: str | None = None,
    source: Literal["app", "iiko_sync", "system_migration"] | None = None,
    actor: str | None = None,
    event_status: Annotated[
        Literal["success", "error", "requires_review", "skipped"] | None,
        Query(alias="status"),
    ] = None,
    only_errors: bool = False,
    only_requires_review: bool = False,
    include_system_migrations: bool = False,
) -> list[EmployeeChangeEvent]:
    filters = EmployeeChangeEventFilter(
        employee_id=employee_id,
        changed_from=changed_from,
        changed_to=changed_to,
        effective_from=effective_from,
        effective_to=effective_to,
        change_type=change_type,
        source=source,
        actor=actor,
        status=event_status,
        only_errors=only_errors,
        only_requires_review=only_requires_review,
        include_system_migrations=include_system_migrations,
    )

    if filters.employee_id is not None:
        await _get_employee_or_404(
            session,
            filters.employee_id,
            actor=actor_context,
            action=StaffAction.HISTORY_READ,
        )

    query = select(EmployeeChangeEvent)
    if filters.employee_id is not None:
        query = query.where(EmployeeChangeEvent.employee_id == filters.employee_id)
    elif actor_context is not None:
        query = query.where(
            EmployeeChangeEvent.employee_id.in_(
                select(Employee.id).where(
                    employee_access_filter(actor_context, StaffAction.HISTORY_READ)
                )
            )
        )
    if filters.changed_from is not None:
        query = query.where(EmployeeChangeEvent.changed_at >= filters.changed_from)
    if filters.changed_to is not None:
        query = query.where(EmployeeChangeEvent.changed_at <= filters.changed_to)
    if filters.effective_from is not None:
        query = query.where(EmployeeChangeEvent.effective_from >= filters.effective_from)
    if filters.effective_to is not None:
        query = query.where(EmployeeChangeEvent.effective_to <= filters.effective_to)
    if filters.change_type is not None:
        query = query.where(EmployeeChangeEvent.change_type == filters.change_type)
    if filters.source is not None:
        query = query.where(EmployeeChangeEvent.source == filters.source)
    elif not filters.include_system_migrations:
        query = query.where(EmployeeChangeEvent.source != "system_migration")
    if filters.actor:
        actor_text = filters.actor.strip()
        try:
            actor_user_id = uuid.UUID(actor_text)
        except ValueError:
            query = query.where(EmployeeChangeEvent.actor_label.ilike(f"%{actor_text}%"))
        else:
            query = query.where(EmployeeChangeEvent.actor_user_id == actor_user_id)

    if filters.status is not None:
        query = query.where(EmployeeChangeEvent.status == filters.status)
    else:
        status_filters: set[str] = set()
        if filters.only_errors:
            status_filters.add("error")
        if filters.only_requires_review:
            status_filters.add("requires_review")
        if status_filters:
            query = query.where(EmployeeChangeEvent.status.in_(sorted(status_filters)))

    result = await session.scalars(
        query.order_by(EmployeeChangeEvent.changed_at.desc(), EmployeeChangeEvent.created_at.desc())
    )
    rows = list(result.all())
    if filters.employee_id is None and actor_context is not None:
        rows = _filter_change_events_by_staff_access(
            rows,
            session,
            actor_context,
            StaffAction.HISTORY_READ,
        )
    return rows


@router.get(
    "/dismissal-reasons",
    response_model=list[EmployeeDismissalReasonRead],
    dependencies=STAFF_READ_ACCESS,
)
async def list_employee_dismissal_reasons(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_context: Annotated[CurrentActor, Depends(get_current_actor)],
    include_inactive: bool = False,
) -> list[EmployeeDismissalReason]:
    return await employee_change_event_service.list_dismissal_reasons(
        session,
        include_inactive=include_inactive,
    )


@router.post(
    "/dismissal-reasons",
    response_model=EmployeeDismissalReasonRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=STAFF_WRITE_ACCESS,
)
async def create_employee_dismissal_reason(
    payload: EmployeeDismissalReasonCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_context: Annotated[CurrentActor, Depends(get_current_actor)],
) -> EmployeeDismissalReason:
    code = payload.code or f"custom_{uuid.uuid4().hex[:12]}"
    existing = await session.scalar(
        select(EmployeeDismissalReason).where(EmployeeDismissalReason.code == code)
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="Причина увольнения с таким кодом уже есть")
    reason = EmployeeDismissalReason(
        code=code,
        label=payload.label,
        requires_comment=payload.requires_comment,
        is_system=False,
        is_active=payload.is_active,
        sort_order=payload.sort_order,
    )
    session.add(reason)
    await session.commit()
    await session.refresh(reason)
    return reason


@router.patch(
    "/dismissal-reasons/{reason_id}",
    response_model=EmployeeDismissalReasonRead,
    dependencies=STAFF_WRITE_ACCESS,
)
async def update_employee_dismissal_reason(
    reason_id: uuid.UUID,
    payload: EmployeeDismissalReasonUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor_context: Annotated[CurrentActor, Depends(get_current_actor)],
) -> EmployeeDismissalReason:
    if not payload.model_fields_set:
        raise HTTPException(status_code=400, detail="Пустое изменение причины увольнения")

    reason = await session.get(EmployeeDismissalReason, reason_id)
    if reason is None:
        raise HTTPException(status_code=404, detail="Причина увольнения не найдена")

    if "label" in payload.model_fields_set and payload.label is not None:
        reason.label = payload.label
    if "requires_comment" in payload.model_fields_set and payload.requires_comment is not None:
        reason.requires_comment = payload.requires_comment
    if "is_active" in payload.model_fields_set and payload.is_active is not None:
        reason.is_active = payload.is_active
    if "sort_order" in payload.model_fields_set and payload.sort_order is not None:
        reason.sort_order = payload.sort_order

    await session.commit()
    await session.refresh(reason)
    return reason


@router.post(
    "/{employee_id}/notice",
    response_model=EmployeeNoticeActionRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=STAFF_WRITE_ACCESS,
)
async def record_employee_notice(
    employee_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    payload: Annotated[EmployeeNoticeRequest | None, Body()] = None,
) -> EmployeeNoticeActionRead:
    payload = payload or EmployeeNoticeRequest()
    await _get_employee_or_404(
        session,
        employee_id,
        actor=actor,
        action=StaffAction.EDIT,
    )
    try:
        event = await notice_service.record_notice(
            session,
            employee_id=employee_id,
            notice_date=payload.notice_date,
            comment=payload.comment,
            actor=actor,
        )
    except notice_service.NoticeAlreadyExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    return _notice_action_payload(event, date.today())


@router.delete(
    "/{employee_id}/notice",
    response_model=EmployeeNoticeActionRead,
    dependencies=STAFF_WRITE_ACCESS,
)
async def cancel_employee_notice(
    employee_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    payload: Annotated[EmployeeNoticeCancelRequest | None, Body()] = None,
) -> EmployeeNoticeActionRead:
    payload = payload or EmployeeNoticeCancelRequest()
    await _get_employee_or_404(
        session,
        employee_id,
        actor=actor,
        action=StaffAction.EDIT,
    )
    try:
        event = await notice_service.cancel_notice(
            session,
            employee_id=employee_id,
            comment=payload.comment,
            actor=actor,
        )
    except notice_service.NoticeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    await session.commit()
    return _notice_action_payload(event, date.today())


@router.post(
    "/{employee_id}/hire-date",
    response_model=EmployeeRead,
    dependencies=STAFF_WRITE_ACCESS,
)
async def set_employee_hire_date(
    employee_id: uuid.UUID,
    payload: EmployeeHireDateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> Employee:
    employee = await _get_employee_or_404(
        session,
        employee_id,
        actor=actor,
        action=StaffAction.EDIT,
    )
    if employee.status == "inactive":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Дата приёма недоступна для уволенного сотрудника",
        )

    today = date.today()
    if payload.hire_date > today:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Дата приёма не может быть в будущем",
        )
    if payload.hire_date < _date_years_ago(today, 10):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Дата приёма не может быть раньше чем 10 лет назад",
        )

    before = {
        "hire_date": employee.hire_date.isoformat() if employee.hire_date else None,
        "tenure_started_at": (
            employee.tenure_started_at.isoformat() if employee.tenure_started_at else None
        ),
    }
    now = datetime.now(UTC)
    employee.hire_date = payload.hire_date
    employee.tenure_started_at = payload.hire_date
    employee.updated_at = now
    after = {
        "hire_date": payload.hire_date.isoformat(),
        "tenure_started_at": payload.hire_date.isoformat(),
    }
    await _add_set_hire_date_action(
        session,
        employee=employee,
        before=before,
        after=after,
        now=now,
        actor=actor,
        comment=payload.comment,
    )
    await session.commit()
    await session.refresh(employee)
    if isinstance(session, AsyncSession):
        return await _get_employee_or_404(
            session,
            employee_id,
            include_assignments=True,
            actor=actor,
            action=StaffAction.READ,
        )
    return employee


@router.post(
    "",
    response_model=EmployeeRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=STAFF_CREATE_ACCESS,
)
@router.post(
    "/",
    response_model=EmployeeRead,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
    dependencies=STAFF_CREATE_ACCESS,
)
async def create_employee(
    payload: EmployeeCreateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> Employee:
    try:
        iiko_role = await _resolve_create_iiko_position(session, payload.iiko_role_id)
        canonical_position = canonical_position_name(iiko_role.name)
        if canonical_position is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Выбранная должность iiko недоступна для создания сотрудника",
            )
        ensure_position_access(actor, canonical_position, StaffAction.CREATE)
        _validate_premium_flags(
            canonical_position,
            is_senior=payload.is_senior,
            is_deputy_senior=payload.is_deputy_senior,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
        resolved_roles = await _resolve_create_roles(
            session,
            payload,
            position=canonical_position,
        )
        await _validate_premium_capacity(
            session,
            canonical_position,
            is_senior=payload.is_senior,
            is_deputy_senior=payload.is_deputy_senior,
            exclude_employee_id=None,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    except _http_client.IncompleteRead as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="iiko не отвечает — сотрудник не создан. Попробуйте через минуту.",
        ) from exc
    except IikoEmployeeOperationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    try:
        iiko_employee = await create_iiko_employee_in_iiko(
            session,
            full_name=payload.full_name,
            role_id=iiko_role.id,
            pin_code=payload.pin_code,
        )
    except _http_client.IncompleteRead as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="iiko не отвечает — сотрудник не создан. Попробуйте через минуту.",
        ) from exc
    except IikoEmployeeOperationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    now = datetime.now(UTC)
    today = date.today()
    pin_hash = hash_password(payload.pin_code)
    employee = await session.scalar(
        select(Employee).where(Employee.iiko_id == iiko_employee.iiko_id)
    )
    before = _employee_lifecycle_snapshot(employee) if employee is not None else None
    created = employee is None
    if employee is None:
        employee = Employee(
            full_name=iiko_employee.full_name,
            iiko_id=iiko_employee.iiko_id,
            position=canonical_position,
            category=None,
            default_cooking_station=None,
            is_senior=payload.is_senior,
            is_deputy_senior=payload.is_deputy_senior,
            hire_date=iiko_employee.hire_date or today,
            tenure_started_at=iiko_employee.hire_date or today,
            pin_hash=pin_hash,
            pin_assumed_from_iiko=False,
            pin_set_at=now,
            iiko_sync_at=now,
        )
        session.add(employee)
    else:
        employee.full_name = iiko_employee.full_name
        employee.position = canonical_position
        employee.is_senior = payload.is_senior
        employee.is_deputy_senior = payload.is_deputy_senior
        employee.hire_date = iiko_employee.hire_date or employee.hire_date or today
        employee.tenure_started_at = employee.tenure_started_at or employee.hire_date
        employee.pin_hash = pin_hash
        employee.pin_assumed_from_iiko = False
        employee.pin_set_at = now
        employee.fire_date = None
        employee.fire_reason = None
        employee.iiko_sync_at = now

    await session.flush()
    await employee_position_service.change_position(
        session,
        employee.id,
        canonical_position,
        effective_from=today,
        comment="Initial position",
        actor=actor,
    )
    if payload.is_senior:
        await employee_effective_event_service.set_allowance(
            session,
            employee.id,
            "senior",
            True,
            effective_from=today,
            comment="Initial allowance",
        )
    if payload.is_deputy_senior:
        await employee_effective_event_service.set_allowance(
            session,
            employee.id,
            "deputy_senior",
            True,
            effective_from=today,
            comment="Initial allowance",
        )
    assignment_snapshots: list[dict[str, Any]] = []
    try:
        for role in sorted(resolved_roles, key=lambda item: not item.is_primary):
            assignment = await employee_assignment_service.add_role(
                session,
                employee.id,
                role.payroll_role,
                role.category,
                is_primary=role.is_primary,
                effective_from=today,
                commit=False,
            )
            assignment_snapshots.append(_employee_assignment_snapshot(assignment))
    except employee_assignment_service.EmployeeAssignmentError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    assignments = await employee_assignment_service.get_assignments(session, employee.id, today)
    employee.status = compute_status(
        employee,
        is_iiko_deleted=False,
        position_group=position_group_for_position(canonical_position),
        assignments=assignments,
    )
    employee.updated_at = now
    await session.flush()
    after = {
        **_employee_lifecycle_snapshot(employee),
        "iiko_role_id": iiko_employee.role_id,
        "iiko_role_name": iiko_role.name,
        "iiko_role_code": iiko_employee.role_code,
        "is_target_position": iiko_employee.is_target_position,
        "roles": assignment_snapshots,
    }
    await _add_employee_lifecycle_action(
        session,
        action_type="create" if created else "upsert_from_iiko_create",
        employee=employee,
        before=before,
        after=after,
        now=now,
        actor=actor,
    )

    await session.commit()
    await session.refresh(employee)
    return await _get_employee_or_404(
        session,
        employee.id,
        include_assignments=True,
        actor=actor,
        action=StaffAction.READ,
    )


@router.post(
    "/{employee_id}/dismiss",
    response_model=EmployeeRead,
    dependencies=STAFF_DISMISS_ACCESS,
)
async def dismiss_employee(
    employee_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    payload: Annotated[EmployeeDismissRequest | None, Body()] = None,
) -> Employee:
    dismiss_payload = payload or EmployeeDismissRequest()
    employee = await _get_employee_or_404(
        session,
        employee_id,
        actor=actor,
        action=StaffAction.DISMISS,
    )
    if employee.status == "inactive" or employee.fire_date is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Сотрудник уже уволен")
    try:
        dismissal_reason = await employee_change_event_service.resolve_dismissal_reason(
            session,
            dismiss_payload,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    fire_date = dismiss_payload.fire_date or date.today()
    if fire_date < date.today() and not dismissal_reason.comment:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Для изменения задним числом комментарий обязателен",
        )

    deposit_account = await deposit_service.get_deposit_account(
        session,
        employee_id,
        for_update=True,
    )
    deposit_balance = decimal(deposit_account.balance) if deposit_account else Decimal("0")
    deposit_decision = _resolve_dismiss_deposit_decision(dismiss_payload, deposit_balance)
    active_notice = await notice_service.get_active_notice(session, employee_id, fire_date)
    before = _employee_lifecycle_snapshot(employee)
    try:
        await dismiss_iiko_employee_in_iiko(session, iiko_id=employee.iiko_id)
    except _http_client.IncompleteRead as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="iiko не отвечает — локальный статус не изменён. Попробуйте через минуту.",
        ) from exc
    except IikoEmployeeOperationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    now = datetime.now(UTC)
    employee.status = "inactive"
    employee.fire_date = fire_date
    employee.fire_reason = dismissal_reason.display_text
    employee.updated_at = now
    after = _employee_lifecycle_snapshot(employee)
    agent_run_id = await _add_employee_lifecycle_action(
        session,
        action_type="dismiss",
        employee=employee,
        before=before,
        after=after,
        now=now,
        actor=actor,
        reason=dismissal_reason.display_text,
        dismissal_reason=dismissal_reason,
    )
    await _close_active_shift_entries_on_dismiss(
        session,
        employee,
        now=now,
        agent_run_id=agent_run_id,
    )
    await _apply_dismiss_deposit_decision(
        session,
        employee_id=employee.id,
        account=deposit_account,
        decision=deposit_decision,
        active_notice=active_notice,
        fire_date=fire_date,
        now=now,
        actor=actor,
        comment=dismiss_payload.deposit_comment,
    )
    await forfeit_active_fund_on_dismiss(session, employee, fire_date=fire_date, now=now)

    await session.commit()
    await session.refresh(employee)
    if isinstance(session, AsyncSession):
        return await _get_employee_or_404(
            session,
            employee_id,
            include_assignments=True,
            actor=actor,
            action=StaffAction.READ,
        )
    return employee


@router.post(
    "/{employee_id}/reinstate",
    response_model=EmployeeRead,
    dependencies=STAFF_REINSTATE_ACCESS,
)
async def reinstate_employee(
    employee_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> Employee:
    employee = await _get_employee_or_404(
        session,
        employee_id,
        actor=actor,
        action=StaffAction.REINSTATE,
    )
    before = _employee_lifecycle_snapshot(employee)
    now = datetime.now(UTC)
    employee.fire_date = None
    employee.fire_reason = None
    employee.tenure_started_at = date.today()

    assignments = None
    if isinstance(session, AsyncSession):
        assignments = await employee_assignment_service.get_assignments(
            session,
            employee.id,
            date.today(),
        )
    employee.status = compute_status(
        employee,
        is_iiko_deleted=False,
        position_group=position_group_for_position(employee.position),
        assignments=assignments,
    )
    employee.updated_at = now
    after = _employee_lifecycle_snapshot(employee)
    await _add_employee_lifecycle_action(
        session,
        action_type="reinstate",
        employee=employee,
        before=before,
        after=after,
        now=now,
        actor=actor,
    )

    await session.commit()
    await session.refresh(employee)
    if isinstance(session, AsyncSession):
        return await _get_employee_or_404(
            session,
            employee_id,
            include_assignments=True,
            actor=actor,
            action=StaffAction.READ,
        )
    return employee


@router.post(
    "/{employee_id}/pin",
    response_model=EmployeeRead,
    dependencies=STAFF_WRITE_ACCESS,
)
async def change_employee_pin(
    employee_id: uuid.UUID,
    payload: EmployeePinChangeRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> Employee:
    employee = await _get_employee_or_404(
        session,
        employee_id,
        actor=actor,
        action=StaffAction.EDIT,
    )
    before = _employee_lifecycle_snapshot(employee)
    now = datetime.now(UTC)
    employee.pin_hash = hash_password(payload.pin_code)
    employee.pin_assumed_from_iiko = False
    employee.pin_set_at = now

    assignments = None
    if isinstance(session, AsyncSession):
        assignments = await employee_assignment_service.get_assignments(
            session,
            employee.id,
            date.today(),
        )
    employee.status = compute_status(
        employee,
        is_iiko_deleted=employee.status == "inactive",
        position_group=position_group_for_position(employee.position),
        assignments=assignments,
    )
    employee.updated_at = now
    after = {**_employee_lifecycle_snapshot(employee), "pin_changed": True}
    await _add_employee_lifecycle_action(
        session,
        action_type="change_pin",
        employee=employee,
        before=before,
        after=after,
        now=now,
        actor=actor,
    )
    await session.commit()
    await session.refresh(employee)
    if isinstance(session, AsyncSession):
        return await _get_employee_or_404(
            session,
            employee_id,
            include_assignments=True,
            actor=actor,
            action=StaffAction.READ,
        )
    return employee


@router.get(
    "/{employee_id}/assignments",
    response_model=list[EmployeeRoleAssignmentRead],
    dependencies=STAFF_READ_ACCESS,
)
async def list_employee_assignments(
    employee_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    on_date: date | None = None,
    include_pending: bool = False,
) -> list[EmployeeRoleAssignment]:
    await _get_employee_or_404(
        session,
        employee_id,
        actor=actor,
        action=StaffAction.READ,
    )
    if include_pending:
        return await employee_assignment_service.get_assignments_with_pending(
            session,
            employee_id,
            on_date or date.today(),
        )
    return await employee_assignment_service.get_assignments(
        session,
        employee_id,
        on_date or date.today(),
    )


@router.post(
    "/{employee_id}/assignments",
    response_model=EmployeeRoleAssignmentRead,
    dependencies=STAFF_WRITE_ACCESS,
)
async def create_employee_assignment(
    employee_id: uuid.UUID,
    payload: EmployeeRoleAssignmentCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> EmployeeRoleAssignment:
    await _get_employee_or_404(
        session,
        employee_id,
        actor=actor,
        action=StaffAction.ASSIGN_ROLES_CATEGORIES,
    )
    if (
        payload.effective_from is not None
        and payload.effective_from < date.today()
        and (not payload.comment or not payload.comment.strip())
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Для изменения задним числом комментарий обязателен",
        )
    try:
        assignment = await employee_assignment_service.add_role(
            session,
            employee_id,
            payload.payroll_role,
            payload.category,
            is_primary=payload.is_primary,
            is_substitute=payload.is_substitute,
            effective_from=payload.effective_from,
        )
    except employee_assignment_service.EmployeeAssignmentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except employee_assignment_service.EmployeeAssignmentError as exc:
        status_code = (
            status.HTTP_422_UNPROCESSABLE_ENTITY
            if payload.is_substitute
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    now = datetime.now(UTC)
    await _add_manual_action(
        session,
        action_type="add_role_assignment",
        target_table="employee_role_assignment",
        target_id=assignment.id,
        before=None,
        after=_assignment_snapshot(assignment),
        now=now,
        actor=actor,
        employee_id=employee_id,
        comment=payload.comment.strip() if payload.comment else None,
    )
    await session.commit()
    await session.refresh(assignment)
    return assignment


@router.patch(
    "/{employee_id}/assignments/{assignment_id}",
    response_model=EmployeeRoleAssignmentRead,
    dependencies=STAFF_WRITE_ACCESS,
)
async def patch_employee_assignment(
    employee_id: uuid.UUID,
    assignment_id: uuid.UUID,
    payload: EmployeeRoleAssignmentPatch,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> EmployeeRoleAssignment:
    await _get_employee_or_404(
        session,
        employee_id,
        actor=actor,
        action=StaffAction.ASSIGN_ROLES_CATEGORIES,
    )
    if not payload.model_fields_set:
        raise HTTPException(status_code=400, detail="Пустое изменение назначения роли")
    if "payroll_role" in payload.model_fields_set and payload.payroll_role is None:
        raise HTTPException(status_code=400, detail="Роль начисления не может быть пустой")
    if "category" in payload.model_fields_set and payload.category is None:
        raise HTTPException(status_code=400, detail="Категория не может быть пустой")
    assignment_effective_from = (
        payload.effective_from if "effective_from" in payload.model_fields_set else None
    )
    assignment_comment = payload.comment.strip() if payload.comment else None
    if (
        assignment_effective_from is not None
        and assignment_effective_from < date.today()
        and not assignment_comment
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Для изменения задним числом комментарий обязателен",
        )
    assignment_before = await _get_assignment_or_404(session, employee_id, assignment_id)
    before = _assignment_snapshot(assignment_before)
    try:
        assignment = await employee_assignment_service.update_assignment(
            session,
            employee_id,
            assignment_id,
            payroll_role=(
                payload.payroll_role if "payroll_role" in payload.model_fields_set else None
            ),
            category=payload.category if "category" in payload.model_fields_set else None,
            is_primary=payload.is_primary if "is_primary" in payload.model_fields_set else None,
            is_substitute=(
                payload.is_substitute if "is_substitute" in payload.model_fields_set else None
            ),
            effective_from=assignment_effective_from,
        )
    except employee_assignment_service.EmployeeAssignmentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except employee_assignment_service.EmployeeAssignmentError as exc:
        status_code = (
            status.HTTP_422_UNPROCESSABLE_ENTITY
            if (
                payload.is_substitute is True
                or getattr(assignment_before, "is_substitute", False)
            )
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    now = datetime.now(UTC)
    await _add_manual_action(
        session,
        action_type="update_role_assignment",
        target_table="employee_role_assignment",
        target_id=assignment.id,
        before=before,
        after=_assignment_snapshot(assignment),
        now=now,
        actor=actor,
        employee_id=employee_id,
        comment=assignment_comment,
    )
    await session.commit()
    await session.refresh(assignment)
    return assignment


@router.delete(
    "/{employee_id}/assignments/{assignment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=STAFF_WRITE_ACCESS,
)
async def delete_employee_assignment(
    employee_id: uuid.UUID,
    assignment_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> None:
    await _get_employee_or_404(
        session,
        employee_id,
        actor=actor,
        action=StaffAction.ASSIGN_ROLES_CATEGORIES,
    )
    assignment_before = await _get_assignment_or_404(session, employee_id, assignment_id)
    before = _assignment_snapshot(assignment_before)
    try:
        if assignment_before.effective_from > date.today():
            try:
                await employee_assignment_service.cancel_pending_assignment(
                    session,
                    employee_id,
                    assignment_id,
                )
            except employee_assignment_service.PrimaryAssignmentRequiredError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=str(exc),
                ) from exc
            after = None
        else:
            assignment = await employee_assignment_service.remove_assignment(
                session, employee_id, assignment_id
            )
            after = _assignment_snapshot(assignment)
    except employee_assignment_service.EmployeeAssignmentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except employee_assignment_service.EmployeeAssignmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    now = datetime.now(UTC)
    await _add_manual_action(
        session,
        action_type="remove_role_assignment",
        target_table="employee_role_assignment",
        target_id=assignment_id,
        before=before,
        after=after,
        now=now,
        actor=actor,
        employee_id=employee_id,
    )
    await session.commit()


@router.get(
    "/iiko-actions/pending",
    response_model=list[EmployeePendingIikoActionRead],
    dependencies=STAFF_READ_ACCESS,
)
async def list_pending_iiko_actions(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list[EmployeePendingIikoAction]:
    query = (
        select(EmployeePendingIikoAction)
        .where(EmployeePendingIikoAction.status == "pending")
        .where(
            EmployeePendingIikoAction.employee_id.in_(
                select(Employee.id).where(employee_access_filter(actor, StaffAction.READ))
            )
        )
        .order_by(EmployeePendingIikoAction.effective_on, EmployeePendingIikoAction.created_at)
    )
    result = await session.scalars(query)
    return _filter_pending_iiko_actions_by_staff_access(
        list(result.all()),
        session,
        actor,
        StaffAction.READ,
    )


@router.post("/iiko-actions/apply-due", dependencies=STAFF_SYNC_ACCESS)
async def apply_due_iiko_actions(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    today: date | None = None,
) -> dict[str, int]:
    return await employee_effective_event_service.apply_due_iiko_actions(session, today=today)


@router.get(
    "/{employee_id}/position-events",
    response_model=list[EmployeePositionEventRead],
    dependencies=STAFF_HISTORY_ACCESS,
)
async def list_employee_position_events(
    employee_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list[EmployeePositionEvent]:
    await _get_employee_or_404(
        session,
        employee_id,
        actor=actor,
        action=StaffAction.HISTORY_READ,
    )
    return await employee_effective_event_service.list_position_events(session, employee_id)


@router.patch(
    "/{employee_id}/position",
    response_model=EmployeePositionAssignmentRead,
    dependencies=STAFF_WRITE_ACCESS,
)
async def change_employee_position(
    employee_id: uuid.UUID,
    payload: EmployeePositionChange,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    employee = await _get_employee_or_404(
        session,
        employee_id,
        actor=actor,
        action=StaffAction.EDIT,
    )
    today = date.today()
    if payload.effective_from < today and not payload.comment:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Для изменения задним числом комментарий обязателен",
        )

    target_position = canonical_position_name(payload.position)
    if target_position is None:
        raise HTTPException(status_code=400, detail="Должность не входит в канонический список")
    ensure_position_access(actor, target_position, StaffAction.EDIT)

    current_position = await employee_position_service.current_position(session, employee.id)
    position_changed = current_position != target_position
    if position_changed and payload.effective_from <= today:
        try:
            await update_iiko_employee(
                session,
                iiko_id=employee.iiko_id,
                position=target_position,
            )
        except _http_client.IncompleteRead as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="iiko не отвечает — локальные данные не изменены. Попробуйте через минуту.",
            ) from exc
        except IikoEmployeeOperationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    try:
        assignment = await employee_position_service.change_position(
            session,
            employee.id,
            target_position,
            effective_from=payload.effective_from,
            comment=payload.comment,
            actor=actor,
        )
    except employee_position_service.EmployeePositionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    if position_changed and payload.effective_from > today:
        await employee_effective_event_service.schedule_iiko_position_update(
            session,
            employee,
            position=target_position,
            effective_on=payload.effective_from,
            related_entity_type="employee_position_assignment",
            related_entity_id=assignment.id,
        )

    if payload.effective_from <= today:
        employee.position = target_position
        employee.is_senior, employee.is_deputy_senior = reset_inapplicable_premiums(
            target_position,
            is_senior=employee.is_senior,
            is_deputy_senior=employee.is_deputy_senior,
        )
        assignments = await employee_assignment_service.get_assignments(session, employee.id, today)
        employee.status = compute_status(
            employee,
            is_iiko_deleted=employee.status == "inactive",
            position_group=position_group_for_position(target_position),
            assignments=assignments,
        )

    await session.commit()
    await session.refresh(assignment)
    return _position_assignment_payload(assignment)


@router.get(
    "/{employee_id}/position-history",
    response_model=list[EmployeePositionAssignmentRead],
    dependencies=STAFF_HISTORY_ACCESS,
)
async def list_position_history_endpoint(
    employee_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list[dict[str, Any]]:
    await _get_employee_or_404(
        session,
        employee_id,
        actor=actor,
        action=StaffAction.HISTORY_READ,
    )
    items = await employee_position_service.list_position_history(session, employee_id)
    return [_position_assignment_payload(item) for item in items]


@router.get(
    "/{employee_id}/allowance-events",
    response_model=list[EmployeeAllowanceEventRead],
    dependencies=STAFF_HISTORY_ACCESS,
)
async def list_employee_allowance_events(
    employee_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list[EmployeeAllowanceEvent]:
    await _get_employee_or_404(
        session,
        employee_id,
        actor=actor,
        action=StaffAction.HISTORY_READ,
    )
    return await employee_effective_event_service.list_allowance_events(session, employee_id)


@router.get("/{employee_id}", response_model=EmployeeRead, dependencies=STAFF_READ_ACCESS)
async def get_employee(
    employee_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor | None, Depends(get_current_actor)] = None,
) -> Employee:
    employee = await _get_employee_or_404(
        session,
        employee_id,
        include_assignments=True,
        actor=actor,
        action=StaffAction.READ,
    )
    await _attach_active_notices(session, [employee], date.today())
    return employee


@router.patch(
    "/{employee_id}",
    response_model=EmployeeRead,
    dependencies=STAFF_WRITE_ACCESS,
)
async def patch_employee(
    employee_id: uuid.UUID,
    payload: Annotated[dict[str, Any], Body()],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> Employee:
    normalized_payload = _normalize_patch_payload(payload)
    _validate_patch_payload(normalized_payload)

    try:
        patch = EmployeePatch.model_validate(normalized_payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors(include_context=False),
        ) from exc
    employee = await _get_employee_or_404(
        session,
        employee_id,
        actor=actor,
        action=StaffAction.EDIT,
    )
    if "hire_date" in patch.model_fields_set:
        raise HTTPException(
            status_code=(
                status.HTTP_409_CONFLICT
                if employee.hire_date is not None
                else status.HTTP_400_BAD_REQUEST
            ),
            detail=(
                "Дата приёма уже установлена и не подлежит изменению"
                if employee.hire_date is not None
                else "Дата приёма устанавливается отдельным действием"
            ),
        )
    today = date.today()
    effective_from = patch.effective_from or today
    effective_comment = patch.comment
    if (
        effective_from < today
        and (PAYROLL_EFFECTIVE_FIELDS & patch.model_fields_set)
        and not effective_comment
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Для изменения задним числом комментарий обязателен",
        )
    applies_to_current_snapshot = effective_from <= today
    before_assignments = await employee_assignment_service.get_assignments(
        session,
        employee.id,
        today,
    )
    before = _employee_lifecycle_snapshot_with_roles(employee, before_assignments)
    is_iiko_deleted = employee.status == "inactive"
    target_full_name = employee.full_name
    full_name_changed = False
    if "full_name" in patch.model_fields_set:
        if patch.full_name is None:
            raise HTTPException(status_code=400, detail="ФИО обязательно")
        target_full_name = patch.full_name
        full_name_changed = target_full_name != employee.full_name

    current_position = await employee_position_service.current_position(session, employee.id)
    position_changed = False
    target_position = current_position
    if "position" in patch.model_fields_set:
        if patch.position is None:
            raise HTTPException(status_code=400, detail="Должность обязательна")
        target_position = canonical_position_name(patch.position)
        if target_position is None:
            raise HTTPException(status_code=400, detail="Должность не входит в канонический список")
        position_changed = target_position != current_position
        ensure_position_access(actor, target_position, StaffAction.EDIT)

    roles_changed = "roles" in patch.model_fields_set and patch.roles is not None
    role_or_category_fields = {
        "roles",
        "category",
        "default_cooking_station",
    }
    if role_or_category_fields & patch.model_fields_set:
        ensure_employee_access(
            actor,
            employee,
            StaffAction.ASSIGN_ROLES_CATEGORIES,
        )
        if target_position is not None:
            ensure_position_access(
                actor,
                target_position,
                StaffAction.ASSIGN_ROLES_CATEGORIES,
            )
    target_roles = list(patch.roles or []) if roles_changed else None
    target_category = patch.category if "category" in patch.model_fields_set else employee.category
    target_default_cooking_station = (
        patch.default_cooking_station
        if "default_cooking_station" in patch.model_fields_set
        else employee.default_cooking_station
    )
    if target_roles is not None:
        target_primary_role = next((role for role in target_roles if role.is_primary), None)
        target_category = target_primary_role.category if target_primary_role else None
        target_default_cooking_station = (
            target_primary_role.payroll_role
            if target_primary_role and target_primary_role.payroll_role in COOKING_STATIONS
            else None
        )
    target_is_senior = (
        patch.is_senior if "is_senior" in patch.model_fields_set else employee.is_senior
    )
    target_is_deputy_senior = (
        patch.is_deputy_senior
        if "is_deputy_senior" in patch.model_fields_set
        else employee.is_deputy_senior
    )

    _validate_explicit_premium_patch(
        patch,
        target_position,
    )
    if (
        position_changed
        or ("is_senior" in patch.model_fields_set and bool(target_is_senior))
        or ("is_deputy_senior" in patch.model_fields_set and bool(target_is_deputy_senior))
    ):
        await _ensure_or_transfer_premium_capacity(
            session,
            target_position,
            is_senior=bool(target_is_senior),
            is_deputy_senior=bool(target_is_deputy_senior),
            exclude_employee_id=employee.id,
            transfer_from_existing=patch.transfer_from_existing,
            effective_from=effective_from,
            comment=effective_comment,
        )

    await _validate_patch_assignment_shortcut(
        session,
        target_position,
        category=target_category,
        default_cooking_station=target_default_cooking_station,
        explicit_category="category" in patch.model_fields_set or roles_changed,
        explicit_default_cooking_station="default_cooking_station" in patch.model_fields_set
        or roles_changed,
    )
    if target_roles is not None:
        await _validate_patch_roles(session, target_position, target_roles)
        if not applies_to_current_snapshot:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Изменение ролей задним числом или будущей датой пока не поддерживается",
            )

    should_update_iiko_position_now = position_changed and applies_to_current_snapshot
    if full_name_changed or should_update_iiko_position_now:
        try:
            await update_iiko_employee(
                session,
                iiko_id=employee.iiko_id,
                full_name=target_full_name if full_name_changed else None,
                position=target_position if should_update_iiko_position_now else None,
            )
        except _http_client.IncompleteRead as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="iiko не отвечает — локальные данные не изменены. Попробуйте через минуту.",
            ) from exc
        except IikoEmployeeOperationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    position_assignment: EmployeePositionAssignment | None = None
    allowance_events: list[EmployeeAllowanceEvent] = []
    try:
        if position_changed:
            position_assignment = await employee_position_service.change_position(
                session,
                employee.id,
                target_position,
                effective_from=effective_from,
                comment=effective_comment or "через PATCH /employees",
                actor=actor,
            )
            if applies_to_current_snapshot:
                employee.position = target_position
            if effective_from > today:
                await employee_effective_event_service.schedule_iiko_position_update(
                    session,
                    employee,
                    position=target_position,
                    effective_on=effective_from,
                    related_entity_type="employee_position_assignment",
                    related_entity_id=position_assignment.id,
                )
        for field, allowance_type in (
            ("is_senior", "senior"),
            ("is_deputy_senior", "deputy_senior"),
        ):
            if field not in patch.model_fields_set:
                continue
            event = await employee_effective_event_service.set_allowance(
                session,
                employee.id,
                allowance_type,
                bool(getattr(patch, field)),
                effective_from=effective_from,
                comment=effective_comment,
            )
            allowance_events.append(event)
    except employee_effective_event_service.EmployeeEffectiveEventNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except employee_position_service.EmployeePositionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except employee_effective_event_service.EmployeeEffectiveEventError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    now = datetime.now(UTC)
    pin_action_type: str | None = None
    pin_audit_before: dict[str, Any] | None = None
    pin_audit_after: dict[str, Any] | None = None
    if patch.pin_code is not None:
        pin_action_type = "pin_changed" if employee.pin_hash else "pin_set"
        pin_audit_before = {
            "pin_set_at": employee.pin_set_at.isoformat() if employee.pin_set_at else None
        }
        employee.pin_hash = hash_password(patch.pin_code)
        employee.pin_assumed_from_iiko = False
        employee.pin_set_at = now
        pin_audit_after = {"pin_set_at": employee.pin_set_at.isoformat()}

    for field in patch.model_fields_set:
        if field in PATCH_META_FIELDS or field in {
            "position",
            "is_senior",
            "is_deputy_senior",
            "pin_code",
            "roles",
        }:
            continue
        if field == "full_name":
            employee.full_name = target_full_name
        elif field == "requires_role_review":
            employee.requires_role_review = bool(patch.requires_role_review)
            if patch.requires_role_review is False:
                review_payload = dict(employee.role_review_payload or {})
                roles_hash = review_payload.get("iiko_roles_hash")
                if roles_hash:
                    review_payload["acknowledged_iiko_roles_hash"] = roles_hash
                    employee.role_review_payload = review_payload
        elif field in {"category", "default_cooking_station"} and not applies_to_current_snapshot:
            continue
        else:
            setattr(employee, field, getattr(patch, field))

    if applies_to_current_snapshot:
        if not is_cook_position(target_position):
            employee.default_cooking_station = None
        if not payroll_roles_for_position(target_position):
            employee.category = None

    assignments = None
    try:
        shortcut_changed = {
            "category",
            "default_cooking_station",
            "position",
        } & patch.model_fields_set
        if target_roles is not None and applies_to_current_snapshot:
            await _sync_patch_roles(
                session,
                employee,
                target_roles,
                effective_from=effective_from,
            )
        elif position_changed and applies_to_current_snapshot:
            await _close_invalid_assignments_for_position(session, employee, today)
        if target_roles is None and shortcut_changed and applies_to_current_snapshot:
            await employee_assignment_service.sync_primary_from_shortcut(
                session,
                employee,
                effective_from=effective_from,
                commit=False,
            )
        elif (
            target_roles is None
            and shortcut_changed
            and ({"category", "default_cooking_station"} & patch.model_fields_set)
        ):
            await _sync_future_assignment_shortcut(
                session,
                employee,
                position=target_position,
                category=target_category,
                default_cooking_station=target_default_cooking_station,
                effective_from=effective_from,
            )
        assignments = await employee_assignment_service.get_assignments(
            session,
            employee.id,
            today,
        )
        if patch.pin_code is not None and not assignments:
            shortcut_assignment = await employee_assignment_service.sync_primary_from_shortcut(
                session,
                employee,
                effective_from=today,
                commit=False,
            )
            if shortcut_assignment is not None:
                assignments = await employee_assignment_service.get_assignments(
                    session,
                    employee.id,
                    today,
                )
    except employee_assignment_service.EmployeeAssignmentError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    employee.is_senior, employee.is_deputy_senior = reset_inapplicable_premiums(
        target_position,
        is_senior=employee.is_senior,
        is_deputy_senior=employee.is_deputy_senior,
    )
    employee.status = compute_status(
        employee,
        is_iiko_deleted=is_iiko_deleted,
        position_group=position_group_for_position(target_position),
        assignments=assignments,
    )
    employee.updated_at = now
    after = _employee_lifecycle_snapshot_with_roles(employee, assignments or [])
    if position_assignment is not None and (
        effective_from != today or not applies_to_current_snapshot
    ):
        await _add_manual_action(
            session,
            action_type="schedule_position_change" if effective_from > today else "update_position",
            target_table="employee_position_assignment",
            target_id=position_assignment.id,
            before={"position": before.get("position")},
            after=_position_assignment_snapshot(position_assignment),
            now=now,
            actor=actor,
            employee_id=employee.id,
            comment=effective_comment,
        )
    for allowance_event in allowance_events:
        if effective_from == today and applies_to_current_snapshot:
            continue
        await _add_manual_action(
            session,
            action_type="set_allowance" if allowance_event.is_enabled else "unset_allowance",
            target_table="employee_allowance_event",
            target_id=allowance_event.id,
            before=None,
            after=employee_effective_event_service.allowance_event_snapshot(allowance_event),
            now=now,
            actor=actor,
            employee_id=employee.id,
            comment=effective_comment,
        )
    if pin_action_type and pin_audit_after is not None:
        await _add_employee_lifecycle_action(
            session,
            action_type=pin_action_type,
            employee=employee,
            before=pin_audit_before,
            after=pin_audit_after,
            now=now,
            actor=actor,
        )
    if _snapshots_differ_ignoring_fields(before, after, {"pin_set_at"}):
        await _add_employee_lifecycle_action(
            session,
            action_type="update",
            employee=employee,
            before=before,
            after=after,
            now=now,
            actor=actor,
        )

    await session.commit()
    await session.refresh(employee)
    if isinstance(session, AsyncSession):
        return await _get_employee_or_404(
            session,
            employee_id,
            include_assignments=True,
            actor=actor,
            action=StaffAction.READ,
        )
    return employee


async def _get_employee_or_404(
    session: AsyncSession,
    employee_id: uuid.UUID,
    *,
    include_assignments: bool = False,
    actor: CurrentActor | None = None,
    action: StaffAction = StaffAction.READ,
) -> Employee:
    if include_assignments and isinstance(session, AsyncSession):
        employee = await session.scalar(
            select(Employee)
            .options(selectinload(Employee.role_assignments))
            .where(Employee.id == employee_id)
        )
    else:
        employee = await session.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Сотрудник не найден")
    if actor is not None:
        ensure_employee_access(actor, employee, action)
    return employee


def _filter_change_events_by_staff_access(
    events: list[EmployeeChangeEvent],
    session: AsyncSession,
    actor: CurrentActor,
    action: StaffAction,
) -> list[EmployeeChangeEvent]:
    allowed_employee_ids = _session_employee_ids_with_staff_access(session, actor, action)
    if allowed_employee_ids is None:
        return events
    return [
        event
        for event in events
        if event.employee_id is not None and event.employee_id in allowed_employee_ids
    ]


def _filter_pending_iiko_actions_by_staff_access(
    actions: list[EmployeePendingIikoAction],
    session: AsyncSession,
    actor: CurrentActor,
    action: StaffAction,
) -> list[EmployeePendingIikoAction]:
    allowed_employee_ids = _session_employee_ids_with_staff_access(session, actor, action)
    if allowed_employee_ids is None:
        return actions
    return [
        action_item
        for action_item in actions
        if action_item.employee_id in allowed_employee_ids
    ]


def _session_employee_ids_with_staff_access(
    session: AsyncSession,
    actor: CurrentActor,
    action: StaffAction,
) -> frozenset[uuid.UUID] | None:
    employees = getattr(session, "employees", None)
    if employees is None:
        return None
    return employee_ids_with_staff_access(employees, actor, action)


async def _attach_active_notices(
    session: AsyncSession,
    employees: list[Employee],
    today: date,
) -> None:
    notices = await notice_service.list_active_notices(
        session,
        [employee.id for employee in employees],
        today,
    )
    for employee in employees:
        notice = notices.get(employee.id)
        employee.active_notice = _notice_info(notice, today) if notice else None


def _notice_info(
    event: EmployeeChangeEvent | None,
    today: date,
) -> EmployeeNoticeInfo | None:
    if event is None or event.effective_from is None:
        return None
    days_since = (today - event.effective_from).days
    return EmployeeNoticeInfo(
        notice_date=event.effective_from,
        days_since=days_since,
        will_trigger_full_payout=days_since >= 14,
    )


def _notice_action_payload(
    event: EmployeeChangeEvent,
    today: date,
) -> EmployeeNoticeActionRead:
    effective_from = event.effective_from or today
    return EmployeeNoticeActionRead(
        event_id=event.id,
        effective_from=effective_from,
        days_until_today=(today - effective_from).days,
    )


async def _get_assignment_or_404(
    session: AsyncSession,
    employee_id: uuid.UUID,
    assignment_id: uuid.UUID,
) -> EmployeeRoleAssignment:
    assignment = await session.scalar(
        select(EmployeeRoleAssignment).where(
            EmployeeRoleAssignment.id == assignment_id,
            EmployeeRoleAssignment.employee_id == employee_id,
        )
    )
    if assignment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Назначение роли не найдено",
        )
    return assignment


def _active_assignment_exists(
    on_date: date,
    *,
    category: str | None = None,
    payroll_role: str | None = None,
) -> Any:
    assignment_query = select(EmployeeRoleAssignment.id).where(
        EmployeeRoleAssignment.employee_id == Employee.id,
        EmployeeRoleAssignment.effective_from <= on_date,
        or_(
            EmployeeRoleAssignment.effective_to.is_(None),
            EmployeeRoleAssignment.effective_to > on_date,
        ),
    )
    if category is not None:
        assignment_query = assignment_query.where(EmployeeRoleAssignment.category == category)
    if payroll_role is not None:
        assignment_query = assignment_query.where(
            EmployeeRoleAssignment.payroll_role == payroll_role
        )
    return assignment_query.exists()


def _normalize_patch_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    if "cooking_station" in normalized:
        if "default_cooking_station" in normalized:
            raise HTTPException(
                status_code=400,
                detail="Укажите только cooking_station или default_cooking_station",
            )
        normalized["default_cooking_station"] = normalized.pop("cooking_station")
    return normalized


def _validate_patch_payload(payload: dict[str, Any]) -> None:
    read_only = READ_ONLY_FIELDS & payload.keys()
    if read_only:
        raise HTTPException(
            status_code=400,
            detail=f"Поля только для чтения: {', '.join(sorted(read_only))}",
        )

    computed = COMPUTED_FIELDS & payload.keys()
    if computed:
        raise HTTPException(
            status_code=400,
            detail=f"Вычисляемые поля нельзя изменить: {', '.join(sorted(computed))}",
        )

    unknown = set(payload) - APP_MANAGED_FIELDS - PATCH_META_FIELDS
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Неподдерживаемые поля: {', '.join(sorted(unknown))}",
        )

    if "full_name" in payload and payload["full_name"] is None:
        raise HTTPException(status_code=400, detail="ФИО обязательно")
    if "full_name" in payload and isinstance(payload["full_name"], str):
        normalized_name = payload["full_name"].strip()
        if len(normalized_name.split()) < 2:
            raise HTTPException(status_code=400, detail="Укажите минимум фамилию и имя")
    for premium_field in ("is_senior", "is_deputy_senior"):
        if premium_field in payload and payload[premium_field] is None:
            raise HTTPException(
                status_code=400,
                detail=f"Поле {premium_field} не может быть пустым",
            )

    category_value = payload.get("category")
    if category_value is not None and category_value not in EMPLOYEE_CATEGORIES:
        raise HTTPException(status_code=400, detail="Некорректная категория сотрудника")

    position_value = payload.get("position")
    if position_value is not None and canonical_position_name(position_value) is None:
        raise HTTPException(status_code=400, detail="Должность не входит в канонический список")

    station_value = payload.get("default_cooking_station")
    if station_value is not None and station_value not in COOKING_STATIONS:
        raise HTTPException(status_code=400, detail="Некорректная станция кухни")


async def _resolve_create_iiko_position(
    session: AsyncSession,
    iiko_role_id: str,
) -> IikoEmployeeRole:
    iiko_roles = await get_iiko_employee_roles(session)
    role_by_id = {role.id: role for role in iiko_roles}
    iiko_role = role_by_id.get(iiko_role_id)
    if iiko_role is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Выбранная должность iiko недоступна",
        )
    if not is_create_position(iiko_role.name):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Выбранная должность iiko недоступна для создания сотрудника",
        )
    return iiko_role


async def _resolve_create_roles(
    session: AsyncSession,
    payload: EmployeeCreateRequest,
    *,
    position: str,
) -> list[ResolvedCreateRole]:
    allowed_roles = payroll_roles_for_position(position)
    if not allowed_roles:
        if payload.roles:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Для выбранной должности роли не предусмотрены",
            )
        if payload.category is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Категории доступны только для ролей",
            )
        return []
    if position == "Кассир" and not payload.roles:
        if payload.category is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Для кассира укажите категорию",
            )
        if payload.category not in categories_for_payroll_role("administrator"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Категория недоступна для этой роли",
            )
        try:
            await employee_assignment_service.ensure_category_available(
                session,
                "administrator",
                payload.category,
            )
        except employee_assignment_service.EmployeeAssignmentError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        return [
            ResolvedCreateRole(
                payroll_role="administrator",
                category=payload.category,
                is_primary=True,
            )
        ]
    if not payload.roles:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Для выбранной должности укажите основную роль",
        )

    resolved: list[ResolvedCreateRole] = []
    for requested_role in payload.roles:
        payroll_role = requested_role.payroll_role
        if payroll_role not in ALLOWED_CREATE_PAYROLL_ROLES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Выбранная роль недоступна для создания сотрудника",
            )
        if payroll_role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Роль не соответствует выбранной должности iiko",
            )
        if requested_role.category not in categories_for_payroll_role(payroll_role):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Категория недоступна для этой роли",
            )
        try:
            await employee_assignment_service.ensure_category_available(
                session,
                payroll_role,
                requested_role.category,
            )
        except employee_assignment_service.EmployeeAssignmentError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        resolved.append(
            ResolvedCreateRole(
                payroll_role=payroll_role,
                category=requested_role.category,
                is_primary=requested_role.is_primary,
            )
        )
    return resolved


def _normalized_create_position_name(value: str) -> str:
    return value.replace("\xa0", " ").strip().replace("Ё", "Е").replace("ё", "е").casefold()


def _payroll_role_matches_create_position(payroll_role: str, position_group: str | None) -> bool:
    if position_group == "cook":
        return payroll_role in employee_assignment_service.COOK_PAYROLL_ROLES
    if position_group == "cashier":
        return payroll_role == "administrator"
    return False


def _validate_premium_flags(
    position: str | None,
    *,
    is_senior: bool,
    is_deputy_senior: bool,
    status_code: int = status.HTTP_400_BAD_REQUEST,
) -> None:
    try:
        validate_premiums(
            position,
            is_senior=is_senior,
            is_deputy_senior=is_deputy_senior,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


def _validate_explicit_premium_patch(
    patch: EmployeePatch,
    position: str | None,
) -> None:
    if "is_senior" in patch.model_fields_set and patch.is_senior:
        _validate_premium_flags(
            position,
            is_senior=True,
            is_deputy_senior=False,
        )
    if "is_deputy_senior" in patch.model_fields_set and patch.is_deputy_senior:
        _validate_premium_flags(
            position,
            is_senior=False,
            is_deputy_senior=True,
        )


async def _validate_premium_capacity(
    session: AsyncSession,
    position: str | None,
    *,
    is_senior: bool,
    is_deputy_senior: bool,
    exclude_employee_id: uuid.UUID | None,
    status_code: int = status.HTTP_400_BAD_REQUEST,
) -> None:
    canonical_position = canonical_position_name(position)
    if canonical_position not in {"Повар", "Кассир"}:
        return

    requested = {
        "is_senior": is_senior,
        "is_deputy_senior": is_deputy_senior,
    }
    labels = {
        "is_senior": "Старший",
        "is_deputy_senior": "Зам старшего",
    }

    for field, should_be_enabled in requested.items():
        if not should_be_enabled:
            continue
        holders = await _active_premium_holders(
            session,
            canonical_position,
            field,
            exclude_employee_id=exclude_employee_id,
        )
        if not holders:
            continue
        names = ", ".join(employee.full_name for employee in holders)
        raise HTTPException(
            status_code=status_code,
            detail=(
                f"Нельзя назначить «{labels[field]}» для должности «{canonical_position}»: "
                f"уже назначены {names}"
            ),
        )


async def _ensure_or_transfer_premium_capacity(
    session: AsyncSession,
    position: str | None,
    *,
    is_senior: bool,
    is_deputy_senior: bool,
    exclude_employee_id: uuid.UUID | None,
    transfer_from_existing: bool,
    effective_from: date,
    comment: str | None,
) -> None:
    canonical_position = canonical_position_name(position)
    if canonical_position not in {"Повар", "Кассир"}:
        return

    requested = {
        "is_senior": is_senior,
        "is_deputy_senior": is_deputy_senior,
    }
    allowance_type_by_field = {
        "is_senior": "senior",
        "is_deputy_senior": "deputy_senior",
    }

    for field, should_be_enabled in requested.items():
        if not should_be_enabled:
            continue
        holders = await _active_premium_holders(
            session,
            canonical_position,
            field,
            exclude_employee_id=exclude_employee_id,
        )
        if not holders:
            continue
        existing = holders[0]
        if not transfer_from_existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_premium_conflict_detail(field, canonical_position, existing),
            )
        await employee_effective_event_service.set_allowance(
            session,
            existing.id,
            allowance_type_by_field[field],
            False,
            effective_from=effective_from,
            comment=comment or "Перенос надбавки другому сотруднику",
        )


async def _active_premium_holders(
    session: AsyncSession,
    position: str,
    field: str,
    *,
    exclude_employee_id: uuid.UUID | None,
) -> list[Employee]:
    employees = list((await session.scalars(select(Employee))).all())
    today = date.today()
    return [
        employee
        for employee in employees
        if employee.id != exclude_employee_id
        and canonical_position_name(employee.position) == position
        and getattr(employee, field)
        and _employee_counts_as_active(employee, today)
    ]


def _premium_conflict_detail(field: str, position: str, existing: Employee) -> dict[str, str]:
    if field == "is_senior":
        code = "senior_already_assigned"
        label = "Старший"
    else:
        code = "deputy_senior_already_assigned"
        label = "Зам старшего"
    return {
        "code": code,
        "message": f"{label} {position.lower()} уже назначен: {existing.full_name}",
        "existing_employee_id": str(existing.id),
        "existing_full_name": existing.full_name,
    }


def _employee_counts_as_active(employee: Employee, today: date) -> bool:
    if employee.status == "inactive":
        return False
    return employee.fire_date is None or employee.fire_date > today


async def _validate_patch_assignment_shortcut(
    session: AsyncSession,
    position: str | None,
    *,
    category: str | None,
    default_cooking_station: str | None,
    explicit_category: bool,
    explicit_default_cooking_station: bool,
) -> None:
    canonical_position = canonical_position_name(position)
    if canonical_position is None:
        return

    if (
        canonical_position != "Повар"
        and explicit_default_cooking_station
        and default_cooking_station
    ):
        raise HTTPException(status_code=400, detail="Цех допустим только для поваров")

    if not payroll_roles_for_position(canonical_position):
        if explicit_category and category is not None:
            raise HTTPException(status_code=400, detail="Категории доступны только для ролей")
        return

    payroll_role: str | None = None
    if canonical_position == "Кассир":
        payroll_role = "administrator"
    elif canonical_position == "Повар" and default_cooking_station is not None:
        payroll_role = default_cooking_station

    if payroll_role is None or category is None:
        return
    if category not in categories_for_payroll_role(payroll_role):
        raise HTTPException(status_code=400, detail="Категория недоступна для этой роли")
    try:
        await employee_assignment_service.ensure_category_available(session, payroll_role, category)
    except employee_assignment_service.EmployeeAssignmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _validate_patch_roles(
    session: AsyncSession,
    position: str | None,
    roles: list[EmployeePatchRoleAssignment],
) -> None:
    canonical_position = canonical_position_name(position)
    allowed_roles = set(payroll_roles_for_position(canonical_position))
    if roles and not allowed_roles:
        raise HTTPException(status_code=400, detail="Для выбранной должности роли не предусмотрены")
    for role in roles:
        if role.payroll_role not in allowed_roles:
            raise HTTPException(
                status_code=400, detail="Роль не соответствует должности сотрудника"
            )
        if role.category not in categories_for_payroll_role(role.payroll_role):
            raise HTTPException(status_code=400, detail="Категория недоступна для этой роли")
        try:
            await employee_assignment_service.ensure_category_available(
                session,
                role.payroll_role,
                role.category,
            )
        except employee_assignment_service.EmployeeAssignmentError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _sync_patch_roles(
    session: AsyncSession,
    employee: Employee,
    roles: list[EmployeePatchRoleAssignment],
    *,
    effective_from: date,
) -> None:
    active_assignments = await employee_assignment_service.get_assignments(
        session,
        employee.id,
        effective_from,
    )
    active_assignments = [
        assignment for assignment in active_assignments if not assignment.is_substitute
    ]
    desired_by_role = {role.payroll_role: role for role in roles}
    active_by_role = {assignment.payroll_role: assignment for assignment in active_assignments}

    for assignment in active_assignments:
        if assignment.payroll_role in desired_by_role:
            continue
        assignment.is_primary = False
        assignment.effective_to = effective_from

    # Сначала снимаем флаг is_primary со ВСЕХ оставшихся открытых assignment'ов
    # и flush'им до constraint check'а. Иначе при смене primary с одной роли на другую
    # одновременно две строки имеют is_primary=true → нарушается partial unique constraint
    # uq_employee_role_assignment_one_open_primary.
    for assignment in active_assignments:
        if assignment.payroll_role in desired_by_role:
            assignment.is_primary = False
    await session.flush()

    for role in roles:
        assignment = active_by_role.get(role.payroll_role)
        if assignment is None:
            assignment = EmployeeRoleAssignment(
                employee_id=employee.id,
                payroll_role=role.payroll_role,
                category=role.category,
                is_primary=role.is_primary,
                effective_from=effective_from,
            )
            session.add(assignment)
            await session.flush()
        elif assignment.category != role.category:
            if assignment.effective_from < effective_from:
                assignment.is_primary = False
                assignment.effective_to = effective_from
                assignment = EmployeeRoleAssignment(
                    employee_id=employee.id,
                    payroll_role=role.payroll_role,
                    category=role.category,
                    is_primary=role.is_primary,
                    effective_from=effective_from,
                )
                session.add(assignment)
                await session.flush()
            else:
                assignment.category = role.category
        assignment.is_primary = role.is_primary

    primary_role = next((role for role in roles if role.is_primary), None)
    employee.category = primary_role.category if primary_role else None
    employee.default_cooking_station = (
        primary_role.payroll_role
        if primary_role and primary_role.payroll_role in COOKING_STATIONS
        else None
    )
    await session.flush()


async def _close_invalid_assignments_for_position(
    session: AsyncSession,
    employee: Employee,
    as_of: date,
) -> None:
    allowed_roles = set(payroll_roles_for_position(employee.position))
    assignments = await employee_assignment_service.get_assignments(session, employee.id, as_of)
    for assignment in assignments:
        if assignment.is_substitute:
            continue
        if assignment.payroll_role in allowed_roles:
            continue
        assignment.is_primary = False
        assignment.effective_to = as_of

    remaining = [
        assignment
        for assignment in assignments
        if not assignment.is_substitute
        if assignment.payroll_role in allowed_roles
        and assignment.effective_from <= as_of
        and (assignment.effective_to is None or assignment.effective_to > as_of)
    ]
    if remaining and not any(assignment.is_primary for assignment in remaining):
        remaining[0].is_primary = True

    primary = next((assignment for assignment in remaining if assignment.is_primary), None)
    if primary is None:
        if not allowed_roles:
            employee.category = None
            employee.default_cooking_station = None
        return
    employee.category = primary.category
    employee.default_cooking_station = (
        primary.payroll_role if primary.payroll_role in COOKING_STATIONS else None
    )


async def _sync_future_assignment_shortcut(
    session: AsyncSession,
    employee: Employee,
    *,
    position: str | None,
    category: str | None,
    default_cooking_station: str | None,
    effective_from: date,
) -> None:
    canonical_position = canonical_position_name(position)
    if canonical_position is None or category is None:
        return

    payroll_role: str | None = None
    if canonical_position == "Кассир":
        payroll_role = "administrator"
    elif canonical_position == "Повар":
        payroll_role = default_cooking_station
    if payroll_role is None:
        return

    await employee_assignment_service.set_primary(
        session,
        employee.id,
        payroll_role,
        category,
        effective_from=effective_from,
        commit=False,
    )


def _date_years_ago(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(month=2, day=28, year=value.year - years)


def _employee_lifecycle_snapshot(employee: Employee) -> dict[str, Any]:
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
        "tenure_started_at": (
            employee.tenure_started_at.isoformat() if employee.tenure_started_at else None
        ),
        "fire_date": employee.fire_date.isoformat() if employee.fire_date else None,
        "fire_reason": employee.fire_reason,
        "requires_role_review": employee.requires_role_review,
        "requires_position_review": employee.requires_position_review,
        "role_review_payload": employee.role_review_payload,
        "pin_set_at": employee.pin_set_at.isoformat() if employee.pin_set_at else None,
        "iiko_sync_at": employee.iiko_sync_at.isoformat() if employee.iiko_sync_at else None,
    }


def _employee_lifecycle_snapshot_with_roles(
    employee: Employee,
    assignments: list[EmployeeRoleAssignment],
) -> dict[str, Any]:
    return {
        **_employee_lifecycle_snapshot(employee),
        "roles": [_employee_assignment_snapshot(assignment) for assignment in assignments],
    }


def _snapshots_differ_ignoring_fields(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    ignored_fields: set[str],
) -> bool:
    if before is None or after is None:
        return before != after
    before_filtered = {key: value for key, value in before.items() if key not in ignored_fields}
    after_filtered = {key: value for key, value in after.items() if key not in ignored_fields}
    return before_filtered != after_filtered


def _employee_assignment_snapshot(
    assignment: EmployeeRoleAssignment,
) -> dict[str, Any]:
    return _assignment_snapshot(assignment)


def _position_assignment_payload(assignment: EmployeePositionAssignment) -> dict[str, Any]:
    return {
        "id": assignment.id,
        "employee_id": assignment.employee_id,
        "position": assignment.position,
        "effective_from": assignment.effective_from,
        "effective_to": assignment.effective_to,
        "comment": assignment.comment,
        "created_by_name": None,
        "created_at": assignment.created_at,
    }


def _position_assignment_snapshot(assignment: EmployeePositionAssignment) -> dict[str, Any]:
    return {
        "id": str(assignment.id),
        "employee_id": str(assignment.employee_id),
        "position": assignment.position,
        "effective_from": assignment.effective_from.isoformat(),
        "effective_to": assignment.effective_to.isoformat() if assignment.effective_to else None,
        "comment": assignment.comment,
    }


def _assignment_snapshot(assignment: EmployeeRoleAssignment) -> dict[str, Any]:
    return {
        "id": str(assignment.id),
        "employee_id": str(assignment.employee_id),
        "payroll_role": assignment.payroll_role,
        "category": assignment.category,
        "is_primary": assignment.is_primary,
        "is_substitute": assignment.is_substitute,
        "effective_from": assignment.effective_from.isoformat(),
        "effective_to": assignment.effective_to.isoformat() if assignment.effective_to else None,
    }


def _date_from_payload(payload: dict[str, Any] | None, field: str) -> date | None:
    value = (payload or {}).get(field)
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _allowance_change_type(allowance_type: str, is_enabled: bool) -> str:
    if allowance_type == "senior":
        return "set_senior" if is_enabled else "unset_senior"
    if allowance_type == "deputy_senior":
        return "set_deputy_senior" if is_enabled else "unset_deputy_senior"
    return "set_allowance" if is_enabled else "unset_allowance"


def _allowance_change_summary(allowance_type: str, is_enabled: bool) -> str:
    if allowance_type == "senior":
        return "Назначен Старший" if is_enabled else "Снят Старший"
    if allowance_type == "deputy_senior":
        return "Назначен Зам старшего" if is_enabled else "Снят Зам старшего"
    return "Назначена надбавка" if is_enabled else "Снята надбавка"


def _resolve_dismiss_deposit_decision(
    payload: EmployeeDismissRequest,
    balance: Decimal,
) -> DismissDepositDecision:
    balance = decimal(balance)
    action = payload.deposit_action
    if action == DepositDismissAction.NONE:
        if balance > 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Депозит не пуст, выберите действие",
            )
        return DismissDepositDecision(
            action=action,
            payout_amount=Decimal("0"),
            writeoff_amount=Decimal("0"),
            balance=balance,
        )

    if balance <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Депозит пуст, действие не требуется",
        )

    if action == DepositDismissAction.PAYOUT_FULL:
        return DismissDepositDecision(
            action=action,
            payout_amount=balance,
            writeoff_amount=Decimal("0"),
            balance=balance,
        )

    if action == DepositDismissAction.WRITE_OFF:
        return DismissDepositDecision(
            action=action,
            payout_amount=Decimal("0"),
            writeoff_amount=balance,
            balance=balance,
        )

    payout_amount = decimal(payload.deposit_payout_amount or Decimal("0"))
    if payout_amount <= 0 or payout_amount >= balance:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Сумма частичной выплаты должна быть больше 0 и меньше баланса депозита",
        )
    return DismissDepositDecision(
        action=action,
        payout_amount=payout_amount,
        writeoff_amount=balance - payout_amount,
        balance=balance,
    )


async def _apply_dismiss_deposit_decision(
    session: AsyncSession,
    *,
    employee_id: uuid.UUID,
    account: DepositAccount | None,
    decision: DismissDepositDecision,
    active_notice: EmployeeChangeEvent | None,
    fire_date: date,
    now: datetime,
    actor: CurrentActor,
    comment: str | None,
) -> None:
    if decision.action == DepositDismissAction.NONE:
        return

    before = deposit_service.deposit_account_snapshot(account)
    account = deposit_service.ensure_account(session, employee_id, account, now)
    transactions: list[DepositTransaction] = []
    if decision.payout_amount > 0:
        transactions.append(
            deposit_service.add_transaction(
                session,
                employee_id=employee_id,
                transaction_type="dismissal_payout",
                amount=decision.payout_amount,
                now=now,
            )
        )
    if decision.writeoff_amount > 0:
        transactions.append(
            deposit_service.add_transaction(
                session,
                employee_id=employee_id,
                transaction_type="dismissal_writeoff",
                amount=decision.writeoff_amount,
                now=now,
            )
        )
    account.balance = Decimal("0")
    account.last_updated = now

    after = deposit_service.deposit_account_snapshot(account) | {
        "dismissal": {
            "action": decision.action.value,
            "balance_before": deposit_service.decimal_string(decision.balance),
            "payout_amount": deposit_service.decimal_string(decision.payout_amount),
            "writeoff_amount": deposit_service.decimal_string(decision.writeoff_amount),
            "fire_date": fire_date.isoformat(),
            "comment": comment,
            "notice": _notice_audit_payload(active_notice, fire_date),
        },
        "transactions": [
            deposit_service.transaction_payload(transaction) for transaction in transactions
        ],
    }
    await deposit_service.add_deposit_action(
        session,
        action_type="deposit_on_dismiss",
        target_table="deposit_account",
        target_id=account.id,
        employee_id=employee_id,
        before=before,
        after=after,
        now=now,
        actor=actor,
        comment=comment,
        agent_name="employee_lifecycle_manual",
    )


def _notice_audit_payload(
    active_notice: EmployeeChangeEvent | None,
    fire_date: date,
) -> dict[str, Any] | None:
    if active_notice is None or active_notice.effective_from is None:
        return None
    days_to_fire = (fire_date - active_notice.effective_from).days
    return {
        "event_id": str(active_notice.id),
        "notice_date": active_notice.effective_from.isoformat(),
        "days_to_fire": days_to_fire,
        "will_trigger_full_payout": days_to_fire >= 14,
    }


async def _add_employee_lifecycle_action(
    session: AsyncSession,
    *,
    action_type: str,
    employee: Employee,
    before: dict[str, Any] | None,
    after: dict[str, Any],
    now: datetime,
    actor: CurrentActor,
    reason: str | None = None,
    dismissal_reason: employee_change_event_service.ResolvedDismissalReason | None = None,
) -> uuid.UUID:
    agent_run = AgentRun(
        id=uuid.uuid4(),
        agent_name="employee_lifecycle_manual",
        finished_at=now,
        status="success",
        params={
            "employee_id": str(employee.id),
            "actor_roles": sorted(actor.roles),
            "reason": reason,
        },
        result={"action_type": action_type},
    )
    session.add(agent_run)
    await session.flush()
    agent_action = AgentAction(
        id=uuid.uuid4(),
        agent_run_id=agent_run.id,
        action_type=action_type,
        target_table="employee",
        target_id=employee.id,
        before_value=before,
        after_value=after,
    )
    session.add(agent_action)
    await employee_change_event_service.add_employee_lifecycle_events(
        session,
        action_type=action_type,
        employee_id=employee.id,
        before=before,
        after=after,
        changed_at=now,
        actor_label=employee_change_event_service.actor_label_from_roles(actor.roles),
        related_agent_run_id=agent_run.id,
        related_agent_action_id=agent_action.id,
        dismissal_reason=dismissal_reason,
    )
    return agent_run.id


async def _add_set_hire_date_action(
    session: AsyncSession,
    *,
    employee: Employee,
    before: dict[str, Any],
    after: dict[str, Any],
    now: datetime,
    actor: CurrentActor,
    comment: str | None,
) -> uuid.UUID:
    hire_date = employee.hire_date
    if hire_date is None:
        raise ValueError("hire_date must be set before audit event")
    agent_run = AgentRun(
        id=uuid.uuid4(),
        agent_name="employee_lifecycle_manual",
        finished_at=now,
        status="success",
        params={
            "employee_id": str(employee.id),
            "actor_roles": sorted(actor.roles),
        },
        result={"action_type": "set_hire_date"},
    )
    session.add(agent_run)
    await session.flush()
    agent_action = AgentAction(
        id=uuid.uuid4(),
        agent_run_id=agent_run.id,
        action_type="set_hire_date",
        target_table="employee",
        target_id=employee.id,
        before_value=before,
        after_value=after,
    )
    session.add(agent_action)
    await employee_change_event_service.add_employee_change_event(
        session,
        employee_id=employee.id,
        change_type="set_hire_date",
        source="app",
        changed_at=now,
        effective_from=hire_date,
        actor_label=employee_change_event_service.actor_label_from_roles(actor.roles),
        summary=(
            f"Установлена дата приёма {hire_date.strftime('%d.%m.%Y')}"
            if before.get("hire_date") is None
            else f"Изменена дата приёма {hire_date.strftime('%d.%m.%Y')}"
        ),
        before_value=before,
        after_value=after,
        comment=comment,
        related_agent_run_id=agent_run.id,
        related_agent_action_id=agent_action.id,
        related_entity_type="employee",
        related_entity_id=employee.id,
        payroll_impact=True,
    )
    return agent_run.id


async def _add_manual_action(
    session: AsyncSession,
    *,
    action_type: str,
    target_table: str,
    target_id: uuid.UUID,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    now: datetime,
    actor: CurrentActor,
    employee_id: uuid.UUID | None = None,
    comment: str | None = None,
) -> uuid.UUID:
    agent_run = AgentRun(
        id=uuid.uuid4(),
        agent_name="employee_manual_change",
        finished_at=now,
        status="success",
        params={
            "employee_id": str(employee_id) if employee_id else None,
            "actor_roles": sorted(actor.roles),
        },
        result={"action_type": action_type},
    )
    session.add(agent_run)
    await session.flush()
    agent_action = AgentAction(
        id=uuid.uuid4(),
        agent_run_id=agent_run.id,
        action_type=action_type,
        target_table=target_table,
        target_id=target_id,
        before_value=before,
        after_value=after,
    )
    session.add(agent_action)
    if target_table == "employee_role_assignment" and employee_id is not None:
        await employee_change_event_service.add_assignment_change_events(
            session,
            action_type=action_type,
            employee_id=employee_id,
            assignment_id=target_id,
            before=before,
            after=after,
            changed_at=now,
            actor_label=employee_change_event_service.actor_label_from_roles(actor.roles),
            related_agent_run_id=agent_run.id,
            related_agent_action_id=agent_action.id,
            comment=comment,
        )
    elif (
        target_table in {"employee_position_event", "employee_position_assignment"}
        and employee_id is not None
    ):
        await employee_change_event_service.add_employee_change_event(
            session,
            employee_id=employee_id,
            source="app",
            changed_at=now,
            actor_label=employee_change_event_service.actor_label_from_roles(actor.roles),
            related_agent_run_id=agent_run.id,
            related_agent_action_id=agent_action.id,
            related_entity_type=target_table,
            related_entity_id=target_id,
            change_type="update_position",
            effective_from=_date_from_payload(after, "effective_from"),
            effective_to=_date_from_payload(after, "effective_to"),
            summary="Изменена должность",
            before_value=before,
            after_value=after,
            comment=comment,
            payroll_impact=True,
        )
    elif target_table == "employee_allowance_event" and employee_id is not None:
        allowance_type = str((after or {}).get("allowance_type") or "")
        is_enabled = bool((after or {}).get("is_enabled"))
        change_type = _allowance_change_type(allowance_type, is_enabled)
        await employee_change_event_service.add_employee_change_event(
            session,
            employee_id=employee_id,
            source="app",
            changed_at=now,
            actor_label=employee_change_event_service.actor_label_from_roles(actor.roles),
            related_agent_run_id=agent_run.id,
            related_agent_action_id=agent_action.id,
            related_entity_type=target_table,
            related_entity_id=target_id,
            change_type=change_type,
            effective_from=_date_from_payload(after, "effective_from"),
            effective_to=_date_from_payload(after, "effective_to"),
            summary=_allowance_change_summary(allowance_type, is_enabled),
            before_value=before,
            after_value=after,
            comment=comment,
            payroll_impact=True,
        )
    return agent_run.id


async def _close_active_shift_entries_on_dismiss(
    session: AsyncSession,
    employee: Employee,
    *,
    now: datetime,
    agent_run_id: uuid.UUID,
) -> None:
    result = await session.scalars(
        select(ShiftLedgerEntry).where(
            ShiftLedgerEntry.employee_id == employee.id,
            ShiftLedgerEntry.closed_at.is_(None),
        )
    )
    for entry in result.all():
        before = ledger_entry_snapshot(entry)
        entry.closed_at = now
        notes = [entry.notes] if entry.notes else []
        notes.append("Закрыто при увольнении сотрудника")
        entry.notes = "; ".join(notes)
        after = ledger_entry_snapshot(entry)
        session.add(
            AgentAction(
                id=uuid.uuid4(),
                agent_run_id=agent_run_id,
                action_type="close_active_shift_on_dismiss",
                target_table="shift_ledger_entry",
                target_id=entry.id,
                before_value=before,
                after_value=after,
            )
        )

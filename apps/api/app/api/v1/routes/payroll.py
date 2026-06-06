from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, ensure_permission, get_current_actor, require_permission
from app.db.session import get_session
from app.models import (
    AgentAction,
    AgentRun,
    DeferredAuditCharge,
    DepositTransaction,
    PayrollLine,
    PayrollRun,
)
from app.schemas.payroll import (
    DeferredChargeCreate,
    DeferredChargeRead,
    PayrollAggregateRead,
    PayrollLineDepositOverridePatch,
    PayrollLineRead,
    PayrollPeriodRead,
    PayrollPersonalReportRead,
    PayrollRunCreate,
    PayrollRunRead,
)
from app.schemas.payroll_config import PayrollRoleCategoryOptionRead
from app.services.deferred_audit_charge_service import (
    DeferredChargeConflictError,
    DeferredChargeNotFoundError,
    DeferredChargeValidationError,
    cancel_deferred_charge,
    create_deferred_charge,
    list_deferred_charges,
)
from app.services.payroll_aggregate_service import build_aggregate
from app.services.payroll_config import list_enabled_role_categories
from app.services.payroll_personal_report import build_personal_report
from app.services.payroll_runner import (
    PayrollConflictError,
    PayrollNotFoundError,
    auto_create_next_period,
    finalize_payroll_run,
    get_run,
    get_run_lines,
    list_runs,
    run_payroll,
    serialize_period,
)

router = APIRouter()
PAYROLL_RUNS_READ_ACCESS = (Depends(require_permission("payroll.runs.read")),)
PAYROLL_RUNS_START_ACCESS = (Depends(require_permission("payroll.runs.start")),)
PAYROLL_RUNS_FINALIZE_ACCESS = (Depends(require_permission("payroll.runs.finalize")),)
PAYROLL_ACCRUALS_READ_ACCESS = (Depends(require_permission("payroll.accruals.read")),)
PAYROLL_PERSONAL_REPORTS_READ_ACCESS = (
    Depends(require_permission("payroll.personal_reports.read")),
)
REVISION_DEFERRALS_READ_ACCESS = (Depends(require_permission("revisions.deferrals.read")),)
REVISION_DEFERRALS_MANAGE_ACCESS = (
    Depends(require_permission("revisions.deferrals.manage")),
)
PAYROLL_PRODUCTION_DEPOSITS_EDIT_ACCESS = (
    Depends(require_permission("payroll.production_deposits.edit")),
)
SOURCE_RATES_READ_ACCESS = (Depends(require_permission("source.rates.read")),)


@router.post(
    "/periods/auto-create-next",
    response_model=PayrollPeriodRead,
    dependencies=PAYROLL_RUNS_START_ACCESS,
)
async def post_auto_create_next_period(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    period = await auto_create_next_period(session)
    return serialize_period(period)


@router.get("/runs", response_model=list[PayrollRunRead], dependencies=PAYROLL_RUNS_READ_ACCESS)
async def get_runs(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list[dict]:
    return await list_runs(session)


@router.get(
    "/role-categories",
    response_model=dict[str, list[PayrollRoleCategoryOptionRead]],
    dependencies=SOURCE_RATES_READ_ACCESS,
)
async def get_role_categories(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, list[dict[str, str]]]:
    return await list_enabled_role_categories(session)


@router.post(
    "/deferred-charges",
    response_model=DeferredChargeRead,
    dependencies=REVISION_DEFERRALS_MANAGE_ACCESS,
)
async def create_deferred_charge_endpoint(
    payload: DeferredChargeCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    try:
        charge = await create_deferred_charge(session, payload, actor=actor)
    except DeferredChargeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DeferredChargeValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return deferred_charge_payload(charge)


@router.get(
    "/deferred-charges",
    response_model=list[DeferredChargeRead],
    dependencies=REVISION_DEFERRALS_READ_ACCESS,
)
async def list_deferred_charges_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    audit_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    charges = await list_deferred_charges(
        session,
        status=status_filter,
        audit_id=audit_id,
    )
    return [deferred_charge_payload(charge) for charge in charges]


@router.post(
    "/deferred-charges/{charge_id}/cancel",
    response_model=DeferredChargeRead,
    dependencies=REVISION_DEFERRALS_MANAGE_ACCESS,
)
async def cancel_deferred_charge_endpoint(
    charge_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    try:
        charge = await cancel_deferred_charge(session, charge_id, actor=actor)
    except DeferredChargeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DeferredChargeConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return deferred_charge_payload(charge)


@router.get(
    "/employee-report",
    response_model=PayrollPersonalReportRead,
    dependencies=PAYROLL_PERSONAL_REPORTS_READ_ACCESS,
)
async def get_employee_payroll_report(
    employee_id: uuid.UUID,
    date_from: date,
    date_to: date,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    if date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")
    if (date_to - date_from).days > 730:
        raise HTTPException(status_code=422, detail="Период не больше 2 лет")
    try:
        return await build_personal_report(session, employee_id, date_from, date_to)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get(
    "/aggregate",
    response_model=PayrollAggregateRead,
    dependencies=PAYROLL_ACCRUALS_READ_ACCESS,
)
async def get_payroll_aggregate(
    date_from: date,
    date_to: date,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    if date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")
    if (date_to - date_from).days > 730:
        raise HTTPException(status_code=422, detail="Период не больше 2 лет")
    return await build_aggregate(session, date_from, date_to)


@router.post("/runs", response_model=PayrollRunRead)
async def post_run(
    payload: PayrollRunCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    ensure_permission(
        actor,
        "payroll.runs.recalculate" if payload.force_refresh else "payroll.runs.start",
    )
    period_id = payload.period_id
    if period_id is None:
        period = await auto_create_next_period(session)
        period_id = period.id

    try:
        run = await run_payroll(session, period_id, force_refresh=payload.force_refresh)
        return await get_run(session, run.id)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get(
    "/runs/{run_id}",
    response_model=PayrollRunRead,
    dependencies=PAYROLL_RUNS_READ_ACCESS,
)
async def get_run_detail(
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    try:
        return await get_run(session, run_id)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get(
    "/runs/{run_id}/lines",
    response_model=list[PayrollLineRead],
    dependencies=PAYROLL_ACCRUALS_READ_ACCESS,
)
async def get_lines(
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list[PayrollLineRead]:
    try:
        lines = await get_run_lines(session, run_id)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    payouts_by_employee = await get_deposit_payouts_by_employee(
        session, run_id, (line.employee_id for line in lines)
    )
    return [serialize_payroll_line(line, payouts_by_employee) for line in lines]


@router.patch(
    "/lines/{line_id}",
    response_model=PayrollLineRead,
    dependencies=PAYROLL_PRODUCTION_DEPOSITS_EDIT_ACCESS,
)
async def patch_line_deposit_override(
    line_id: uuid.UUID,
    payload: PayrollLineDepositOverridePatch,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> PayrollLineRead:
    line = await session.get(PayrollLine, line_id)
    if line is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payroll line not found")
    run = await session.get(PayrollRun, line.run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payroll run not found")
    if run.status == "finalized":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ведомость зафинализирована, изменения невозможны",
        )

    before = payroll_line_deposit_override_snapshot(line)
    line.deposit_excluded_for_run = payload.deposit_excluded_for_run
    line.deposit_exclusion_reason = clean_optional_text(payload.deposit_exclusion_reason)
    after = payroll_line_deposit_override_snapshot(line)
    await add_payroll_line_deposit_override_action(
        session,
        line=line,
        run=run,
        before=before,
        after=after,
        actor=actor,
    )
    await session.commit()
    await session.refresh(line)
    payouts = await get_deposit_payouts_by_employee(session, line.run_id, [line.employee_id])
    return serialize_payroll_line(line, payouts)


@router.post(
    "/runs/{run_id}/finalize",
    response_model=PayrollRunRead,
    dependencies=PAYROLL_RUNS_FINALIZE_ACCESS,
)
async def post_finalize(
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    try:
        run = await finalize_payroll_run(session, run_id)
        return await get_run(session, run.id)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


async def get_deposit_payouts_by_employee(
    session: AsyncSession,
    run_id: uuid.UUID,
    employee_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, float]:
    unique_employee_ids = list(set(employee_ids))
    if not unique_employee_ids:
        return {}
    result = await session.execute(
        select(
            DepositTransaction.employee_id,
            func.coalesce(func.sum(DepositTransaction.amount), 0),
        )
        .where(
            DepositTransaction.run_id == run_id,
            DepositTransaction.transaction_type == "payout",
            DepositTransaction.employee_id.in_(unique_employee_ids),
        )
        .group_by(DepositTransaction.employee_id)
    )
    return {employee_id: money_float(amount) for employee_id, amount in result.all()}


def serialize_payroll_line(
    line: PayrollLine,
    payouts_by_employee: dict[uuid.UUID, float],
) -> PayrollLineRead:
    components = line.components if isinstance(line.components, dict) else {}
    if getattr(line, "ndfl_withheld", None) is None:
        line.ndfl_withheld = Decimal("0")
    return PayrollLineRead.model_validate(line).model_copy(
        update={
            "deposit_withholding": money_float(components.get("deposit_withholding", 0)),
            "deposit_payout": payouts_by_employee.get(line.employee_id, 0),
            "ndfl_deduction": money_float(getattr(line, "ndfl_withheld", 0)),
        }
    )


def deferred_charge_payload(charge: DeferredAuditCharge) -> dict[str, Any]:
    source_audit = getattr(charge, "source_audit", None)
    source_item = getattr(charge, "source_item", None)
    created_by = getattr(charge, "created_by", None)
    return {
        "id": charge.id,
        "source_audit_id": charge.source_audit_id,
        "source_item_id": charge.source_item_id,
        "source_audit_date": (source_audit.business_date if source_audit is not None else None),
        "source_item_name": (
            source_item.product_name_snapshot if source_item is not None else None
        ),
        "allocation_group": charge.allocation_group,
        "total_penalty_amount": decimal_string(charge.total_penalty_amount),
        "splits_count": charge.splits_count,
        "status": charge.status,
        "reason": charge.reason,
        "created_by_name": created_by.full_name if created_by is not None else None,
        "created_at": charge.created_at,
        "updated_at": charge.updated_at,
        "recipients": [
            {
                "id": recipient.id,
                "employee_id": recipient.employee_id,
                "employee_name": getattr(getattr(recipient, "employee", None), "full_name", None),
                "employee_position": getattr(
                    getattr(recipient, "employee", None), "position", None
                ),
                "per_split_amount": decimal_string(recipient.per_split_amount),
                "splits_remaining": recipient.splits_remaining,
                "collapsed_at": recipient.collapsed_at,
                "collapse_run_id": recipient.collapse_run_id,
                "splits": [
                    {
                        "id": split.id,
                        "split_index": split.split_index,
                        "amount": decimal_string(split.amount),
                        "run_id": split.run_id,
                        "adjustment_id": split.adjustment_id,
                        "applied_at": split.applied_at,
                    }
                    for split in sorted(recipient.splits, key=lambda item: item.split_index)
                ],
            }
            for recipient in sorted(
                charge.recipients,
                key=lambda item: (
                    getattr(getattr(item, "employee", None), "full_name", "") or "",
                    str(item.employee_id),
                ),
            )
        ],
    }


async def add_payroll_line_deposit_override_action(
    session: AsyncSession,
    *,
    line: PayrollLine,
    run: PayrollRun,
    before: dict[str, object],
    after: dict[str, object],
    actor: CurrentActor,
) -> uuid.UUID:
    now = datetime.now(UTC)
    agent_run = AgentRun(
        id=uuid.uuid4(),
        agent_name="payroll_manual_change",
        finished_at=now,
        status="success",
        params={
            "run_id": str(run.id),
            "period_id": str(run.period_id),
            "employee_id": str(line.employee_id),
            "actor_roles": sorted(actor.roles),
        },
        result={"action_type": "payroll_line_deposit_override"},
    )
    session.add(agent_run)
    await session.flush()
    session.add(
        AgentAction(
            id=uuid.uuid4(),
            agent_run_id=agent_run.id,
            action_type="payroll_line_deposit_override",
            target_table="payroll_line",
            target_id=line.id,
            before_value=before,
            after_value=after,
        )
    )
    return agent_run.id


def payroll_line_deposit_override_snapshot(line: PayrollLine) -> dict[str, object]:
    return {
        "id": str(line.id),
        "run_id": str(line.run_id),
        "employee_id": str(line.employee_id),
        "role": line.role,
        "deposit_excluded_for_run": bool(line.deposit_excluded_for_run),
        "deposit_exclusion_reason": line.deposit_exclusion_reason,
    }


def clean_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def money_float(value: object) -> float:
    if value is None:
        return 0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0


def decimal_string(value: object) -> str:
    return f"{Decimal(str(value)).quantize(Decimal('0.01'))}"

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, get_current_actor, require_finance_manager_plus
from app.db.session import get_session
from app.models import AgentAction, AgentRun, DepositTransaction, PayrollLine, PayrollRun
from app.schemas.payroll import (
    PayrollLineDepositOverridePatch,
    PayrollLineRead,
    PayrollPeriodRead,
    PayrollRunCreate,
    PayrollRunRead,
)
from app.schemas.payroll_config import PayrollRoleCategoryOptionRead
from app.services.payroll_config import list_enabled_role_categories
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


@router.post("/periods/auto-create-next", response_model=PayrollPeriodRead)
async def post_auto_create_next_period(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    require_finance_manager_plus(actor)
    period = await auto_create_next_period(session)
    return serialize_period(period)


@router.get("/runs", response_model=list[PayrollRunRead])
async def get_runs(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list[dict]:
    return await list_runs(session)


@router.get("/role-categories", response_model=dict[str, list[PayrollRoleCategoryOptionRead]])
async def get_role_categories(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, list[dict[str, str]]]:
    return await list_enabled_role_categories(session)


@router.post("/runs", response_model=PayrollRunRead)
async def post_run(
    payload: PayrollRunCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    require_finance_manager_plus(actor)
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


@router.get("/runs/{run_id}", response_model=PayrollRunRead)
async def get_run_detail(
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    try:
        return await get_run(session, run_id)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/runs/{run_id}/lines", response_model=list[PayrollLineRead])
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


@router.patch("/lines/{line_id}", response_model=PayrollLineRead)
async def patch_line_deposit_override(
    line_id: uuid.UUID,
    payload: PayrollLineDepositOverridePatch,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> PayrollLineRead:
    require_finance_manager_plus(actor)
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


@router.post("/runs/{run_id}/finalize", response_model=PayrollRunRead)
async def post_finalize(
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    require_finance_manager_plus(actor)
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
    return PayrollLineRead.model_validate(line).model_copy(
        update={
            "deposit_withholding": money_float(components.get("deposit_withholding", 0)),
            "deposit_payout": payouts_by_employee.get(line.employee_id, 0),
            "ndfl_deduction": 0,
        }
    )


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

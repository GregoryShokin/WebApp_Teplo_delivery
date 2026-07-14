from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, ensure_permission, get_current_actor, require_permission
from app.auth.permissions import payout_channel_permission
from app.db.session import get_session
from app.models import (
    AgentAction,
    AgentRun,
    DeferredAuditCharge,
    DepositTransaction,
    PayrollLine,
    PayrollPayment,
    PayrollPeriod,
    PayrollRun,
    PayrollRunEvent,
    User,
    Wallet,
)
from app.schemas.inventory import PayoutOptionRead
from app.schemas.payroll import (
    AdvanceRecoveryDeferralRequest,
    CashWalletRead,
    DeferredChargeCreate,
    DeferredChargeRead,
    EmployeePayoutConfirmRequest,
    EmployeePayoutCreate,
    EmployeePayoutRead,
    EmployeeRecoveryDetailRead,
    PayrollAggregateRead,
    PayrollAuditEventRead,
    PayrollBankDraftRead,
    PayrollLineDepositOverridePatch,
    PayrollLineRead,
    PayrollPaymentMarkRequest,
    PayrollPaymentPartialRequest,
    PayrollPaymentsBulkMarkRequest,
    PayrollPaymentsMarkAllRequest,
    PayrollPaymentsMarkAllResponse,
    PayrollPayoutAllocationRead,
    PayrollPayoutApplyDeltasResponse,
    PayrollPayoutDeltaRead,
    PayrollPayoutDraftsResponse,
    PayrollPeriodRead,
    PayrollPersonalReportRead,
    PayrollPoolPayoutRequest,
    PayrollPoolPayoutResponse,
    PayrollReserveEmployeePayRequest,
    PayrollReserveEmployeePayResponse,
    PayrollRunCreate,
    PayrollRunPayoutCashPatch,
    PayrollRunRead,
    PayrollRunUnfinalize,
    PayrollSolvencyRead,
    RecoveryOverridesRequest,
)
from app.schemas.payroll_config import PayrollRoleCategoryOptionRead
from app.services.banking import BankCredentialsError, BankFetchError
from app.services.deferred_audit_charge_service import (
    DeferredChargeConflictError,
    DeferredChargeNotFoundError,
    DeferredChargeValidationError,
    cancel_deferred_charge,
    create_deferred_charge,
    list_deferred_charges,
)
from app.services.employee_payouts import (
    BANK_PAYOUT_WALLET_TYPES,
    confirm_employee_payout_by_operation,
    create_bank_employee_payout,
    create_cash_employee_payout,
)
from app.services.inventory_audit_service import list_open_production_payouts
from app.services.payroll_admin import compute_on_demand_debt, run_admin_payroll
from app.services.payroll_advance_service import (
    get_employee_recovery_detail,
    set_advance_recovery_deferral,
    set_advance_recovery_overrides,
)
from app.services.payroll_aggregate_service import build_aggregate
from app.services.payroll_config import list_enabled_role_categories
from app.services.payroll_payments import (
    mark_all_payments,
    mark_partial_payment,
    mark_payment,
    mark_payments_selected,
    unmark_payment,
)
from app.services.payroll_payouts import (
    apply_payout_deltas,
    apply_run_payout_delta,
    create_or_update_drafts,
    create_or_update_run_draft,
    get_payout_deltas,
    get_run_bank_draft,
    get_run_payout_allocation,
    get_run_payout_delta,
    list_cash_wallets,
    set_run_payout_cash,
)
from app.services.payroll_personal_report import build_personal_report
from app.services.payroll_reserves import (
    PoolPayoutResult,
    pay_employee_from_reserve,
    pay_run_from_pool,
    run_solvency,
)
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
    unfinalize_payroll_run,
)

router = APIRouter()
PAYROLL_RUNS_READ_ACCESS = (Depends(require_permission("payroll.runs.read")),)
PAYROLL_RUNS_START_ACCESS = (Depends(require_permission("payroll.runs.start")),)
PAYROLL_RUNS_FINALIZE_ACCESS = (Depends(require_permission("payroll.runs.finalize")),)
PAYROLL_RUNS_REOPEN_ACCESS = (Depends(require_permission("payroll.runs.reopen")),)
PAYROLL_RUNS_MARK_PAID_ACCESS = (Depends(require_permission("payroll.runs.mark_paid")),)
PAYROLL_RUNS_BANK_DRAFT_ACCESS = (Depends(require_permission("payroll.runs.bank_draft")),)
PAYROLL_LOANS_ISSUE_ACCESS = (Depends(require_permission("payroll.loans.issue")),)
PAYROLL_ACCRUALS_READ_ACCESS = (Depends(require_permission("payroll.accruals.read")),)
PAYROLL_PERSONAL_REPORTS_READ_ACCESS = (
    Depends(require_permission("payroll.personal_reports.read")),
)
REVISION_DEFERRALS_READ_ACCESS = (Depends(require_permission("revisions.deferrals.read")),)
REVISION_DEFERRALS_MANAGE_ACCESS = (Depends(require_permission("revisions.deferrals.manage")),)
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
    "/audit-events",
    response_model=list[PayrollAuditEventRead],
    dependencies=PAYROLL_RUNS_FINALIZE_ACCESS,
)
async def get_audit_events(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
    action: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    statement = (
        select(PayrollRunEvent, User.full_name)
        .outerjoin(User, User.id == PayrollRunEvent.actor_user_id)
        .order_by(PayrollRunEvent.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if action is not None:
        statement = statement.where(PayrollRunEvent.action == action)
    if date_from is not None:
        statement = statement.where(
            PayrollRunEvent.created_at >= datetime.combine(date_from, time.min, tzinfo=UTC)
        )
    if date_to is not None:
        statement = statement.where(
            PayrollRunEvent.created_at
            < datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=UTC)
        )

    rows = (await session.execute(statement)).all()
    return [
        {
            "id": event.id,
            "action": event.action,
            "actor": actor_name,
            "reason": event.reason,
            "payload": event.payload,
            "created_at": event.created_at,
        }
        for event, actor_name in rows
    ]


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


@router.get(
    "/deferred-charges/payout-options",
    response_model=list[PayoutOptionRead],
    dependencies=REVISION_DEFERRALS_MANAGE_ACCESS,
)
async def deferred_charge_payout_options(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list[dict[str, Any]]:
    return await list_open_production_payouts(session, count=12)


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
        run = await run_payroll(
            session,
            period_id,
            force_refresh=payload.force_refresh,
            # «Пересчитать» (force_refresh) перечитывает явки из iiko, подхватывая
            # смены, добавленные/исправленные после первого расчёта.
            refresh_attendance=payload.force_refresh,
        )
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
    payments_by_employee = await get_payments_by_employee(
        session, run_id, (line.employee_id for line in lines)
    )
    on_demand_ids = [line.employee_id for line in lines if line_is_on_demand(line)]
    on_demand_debt_by_employee = (
        await compute_on_demand_debt(session, on_demand_ids) if on_demand_ids else {}
    )
    return [
        serialize_payroll_line(
            line, payouts_by_employee, payments_by_employee, on_demand_debt_by_employee
        )
        for line in lines
    ]


@router.post(
    "/employee-payouts",
    response_model=EmployeePayoutRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=(Depends(require_permission("payroll.employee_payouts.create")),),
)
async def post_employee_payout(
    payload: EmployeePayoutCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> EmployeePayoutRead:
    """Разовая выплата сотруднику.

    Наличный/сейфовый счёт — прямая проводка (``paid`` сразу). Банковский счёт — черновик
    Т-Банк по реквизитам ИП (``pending``), подтверждается привязкой к операции выписки.
    """
    wallet = await session.get(Wallet, payload.wallet_id)
    if wallet is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Счёт не найден")
    is_bank = wallet.type in BANK_PAYOUT_WALLET_TYPES
    try:
        if is_bank:
            payout = await create_bank_employee_payout(
                session,
                employee_id=payload.employee_id,
                amount=payload.amount,
                wallet_id=payload.wallet_id,
                payout_date=payload.payout_date,
                kind=payload.kind,
                article_id=payload.article_id,
                note=payload.note,
                created_by_user_id=actor.user_id,
            )
        else:
            payout = await create_cash_employee_payout(
                session,
                employee_id=payload.employee_id,
                amount=payload.amount,
                wallet_id=payload.wallet_id,
                payout_date=payload.payout_date,
                kind=payload.kind,
                article_id=payload.article_id,
                note=payload.note,
                created_by_user_id=actor.user_id,
            )
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    await session.commit()
    await session.refresh(payout)
    return EmployeePayoutRead.model_validate(payout)


@router.post(
    "/employee-payouts/{payout_id}/confirm",
    response_model=EmployeePayoutRead,
    dependencies=(Depends(require_permission("payroll.employee_payouts.create")),),
)
async def post_employee_payout_confirm(
    payout_id: uuid.UUID,
    payload: EmployeePayoutConfirmRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> EmployeePayoutRead:
    """Подтвердить банковскую выплату привязкой к реальной исходящей операции выписки."""
    try:
        payout = await confirm_employee_payout_by_operation(
            session,
            payout_id=payout_id,
            bank_operation_id=payload.bank_operation_id,
        )
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    await session.commit()
    await session.refresh(payout)
    return EmployeePayoutRead.model_validate(payout)


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
    payments = await get_payments_by_employee(session, line.run_id, [line.employee_id])
    return serialize_payroll_line(line, payouts, payments)


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
        run = await finalize_payroll_run(
            session,
            run_id,
            finalized_by_user_id=actor.user_id,
        )
        return await get_run(session, run.id)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post(
    "/runs/{run_id}/unfinalize",
    response_model=PayrollRunRead,
    dependencies=PAYROLL_RUNS_REOPEN_ACCESS,
)
async def post_unfinalize(
    run_id: uuid.UUID,
    payload: PayrollRunUnfinalize,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    try:
        run = await unfinalize_payroll_run(
            session,
            run_id,
            reason=payload.reason,
            actor_user_id=actor.user_id,
        )
        return await get_run(session, run.id)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.patch(
    "/runs/{run_id}/payout-cash",
    response_model=PayrollRunRead,
    dependencies=PAYROLL_RUNS_BANK_DRAFT_ACCESS,
)
async def patch_run_payout_cash(
    run_id: uuid.UUID,
    payload: PayrollRunPayoutCashPatch,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> PayrollRunRead:
    # Наличная выплата с конкретного счёта требует права на этот канал (Сейф / ТК Черникова).
    if payload.amount_cash and payload.amount_cash > 0 and payload.cash_wallet_code:
        channel_perm = payout_channel_permission(payload.cash_wallet_code)
        if channel_perm is not None:
            ensure_permission(actor, channel_perm)
    try:
        await set_run_payout_cash(
            session,
            run_id,
            amount_cash=payload.amount_cash,
            cash_wallet_code=payload.cash_wallet_code,
            actor_user_id=actor.user_id,
        )
        return await get_run(session, run_id)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post(
    "/runs/{run_id}/advances/{advance_id}/defer-recovery",
    response_model=PayrollRunRead,
    dependencies=PAYROLL_LOANS_ISSUE_ACCESS,
)
async def post_defer_advance_recovery(
    run_id: uuid.UUID,
    advance_id: uuid.UUID,
    payload: AdvanceRecoveryDeferralRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    """Отсрочить/вернуть удержание займа в этой ведомости и пересчитать её."""
    try:
        await set_advance_recovery_deferral(
            session,
            run_id=run_id,
            advance_id=advance_id,
            defer=payload.defer,
            reason=payload.reason,
            actor_user_id=actor.user_id,
        )
        run = await session.get(PayrollRun, run_id)
        period = await session.get(PayrollPeriod, run.period_id)
        if period.period_type == "half_month":
            await run_admin_payroll(session, run.period_id)
        else:
            await run_payroll(session, run.period_id, force_refresh=True)
        return await get_run(session, run_id)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get(
    "/runs/{run_id}/employees/{employee_id}/recoveries",
    response_model=EmployeeRecoveryDetailRead,
    dependencies=PAYROLL_RUNS_READ_ACCESS,
)
async def get_employee_recoveries(
    run_id: uuid.UUID,
    employee_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    """Детализация удержаний сотрудника в ведомости (окно «Удержания сотрудника»)."""
    try:
        return await get_employee_recovery_detail(
            session, run_id=run_id, employee_id=employee_id
        )
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.put(
    "/runs/{run_id}/recoveries",
    response_model=PayrollRunRead,
    dependencies=PAYROLL_LOANS_ISSUE_ACCESS,
)
async def put_run_recoveries(
    run_id: uuid.UUID,
    payload: RecoveryOverridesRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    """Задать ручные суммы удержания авансов/займов в ведомости и пересчитать её."""
    try:
        await set_advance_recovery_overrides(
            session,
            run_id=run_id,
            items=[(item.advance_id, item.amount) for item in payload.items],
            reason=payload.reason,
            actor_user_id=actor.user_id,
        )
        run = await session.get(PayrollRun, run_id)
        period = await session.get(PayrollPeriod, run.period_id)
        if period.period_type == "half_month":
            await run_admin_payroll(session, run.period_id)
        else:
            await run_payroll(session, run.period_id, force_refresh=True)
        return await get_run(session, run_id)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post(
    "/runs/{run_id}/bank-draft",
    response_model=PayrollBankDraftRead | None,
    dependencies=PAYROLL_RUNS_BANK_DRAFT_ACCESS,
)
async def post_run_bank_draft(
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    bank_provider: Annotated[Literal["tbank", "sber"], Query()] = "tbank",
) -> PayrollBankDraftRead | None:
    # Формирование банк-черновика требует права на канал «банк-черновик» (то же право для Сбера).
    ensure_permission(actor, "finance.payout_channel.bank_draft")
    try:
        return await create_or_update_run_draft(
            session,
            run_id,
            actor_user_id=actor.user_id,
            provider=bank_provider,
        )
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except BankCredentialsError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except BankFetchError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.get(
    "/runs/{run_id}/payout-allocation",
    response_model=PayrollPayoutAllocationRead,
    dependencies=PAYROLL_RUNS_BANK_DRAFT_ACCESS,
)
async def get_run_payout_allocation_endpoint(
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> PayrollPayoutAllocationRead:
    """Превью разнесения выплаты по статьям ДДС (две корзины) при текущем сплите."""
    try:
        return await get_run_payout_allocation(session, run_id)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get(
    "/cash-wallets",
    response_model=list[CashWalletRead],
    dependencies=PAYROLL_RUNS_BANK_DRAFT_ACCESS,
)
async def get_cash_wallets_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[CashWalletRead]:
    """Наличные кошельки для выбора при сплите (Сейф, Торговая касса Черникова)."""
    return await list_cash_wallets(session)


@router.get(
    "/runs/{run_id}/bank-draft",
    response_model=PayrollBankDraftRead,
    dependencies=PAYROLL_RUNS_BANK_DRAFT_ACCESS,
)
async def get_run_bank_draft_endpoint(
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> PayrollBankDraftRead:
    try:
        draft = await get_run_bank_draft(session, run_id)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if draft is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bank draft not found")
    return draft


@router.get(
    "/runs/{run_id}/bank-draft/delta",
    response_model=PayrollPayoutDeltaRead,
    dependencies=PAYROLL_RUNS_BANK_DRAFT_ACCESS,
)
async def get_run_bank_draft_delta(
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    try:
        return await get_run_payout_delta(session, run_id)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post(
    "/runs/{run_id}/bank-draft/apply-delta",
    response_model=PayrollPayoutApplyDeltasResponse,
    dependencies=PAYROLL_RUNS_BANK_DRAFT_ACCESS,
)
async def post_apply_run_bank_draft_delta(
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> PayrollPayoutApplyDeltasResponse:
    # Доплата/пересоздание банк-черновика — тоже канал «банк-черновик».
    ensure_permission(actor, "finance.payout_channel.bank_draft")
    try:
        applied_count = await apply_run_payout_delta(
            session,
            run_id,
            actor_user_id=actor.user_id,
        )
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except BankCredentialsError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except BankFetchError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return PayrollPayoutApplyDeltasResponse(applied_count=applied_count)


@router.post(
    "/runs/{run_id}/payouts/drafts",
    response_model=PayrollPayoutDraftsResponse,
    dependencies=PAYROLL_RUNS_BANK_DRAFT_ACCESS,
)
async def post_payout_drafts(
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> PayrollPayoutDraftsResponse:
    try:
        drafts_count = await create_or_update_drafts(
            session,
            run_id,
            actor_user_id=actor.user_id,
        )
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except BankCredentialsError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except BankFetchError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return PayrollPayoutDraftsResponse(drafts_count=drafts_count)


@router.get(
    "/runs/{run_id}/payouts/deltas",
    response_model=list[PayrollPayoutDeltaRead],
    dependencies=PAYROLL_RUNS_BANK_DRAFT_ACCESS,
)
async def get_payout_delta_list(
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list[dict[str, Any]]:
    try:
        return await get_payout_deltas(session, run_id)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post(
    "/runs/{run_id}/payouts/apply-deltas",
    response_model=PayrollPayoutApplyDeltasResponse,
    dependencies=PAYROLL_RUNS_BANK_DRAFT_ACCESS,
)
async def post_apply_payout_deltas(
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> PayrollPayoutApplyDeltasResponse:
    try:
        applied_count = await apply_payout_deltas(
            session,
            run_id,
            actor_user_id=actor.user_id,
        )
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except BankCredentialsError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except BankFetchError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return PayrollPayoutApplyDeltasResponse(applied_count=applied_count)


@router.post(
    "/runs/{run_id}/payments",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=PAYROLL_RUNS_MARK_PAID_ACCESS,
)
async def post_mark_payment(
    run_id: uuid.UUID,
    payload: PayrollPaymentMarkRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> None:
    try:
        await mark_payment(
            session,
            run_id,
            payload.employee_id,
            paid_at=payload.paid_at,
            method=payload.method,
            actor_user_id=actor.user_id,
        )
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.delete(
    "/runs/{run_id}/payments/{employee_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=PAYROLL_RUNS_MARK_PAID_ACCESS,
)
async def delete_mark_payment(
    run_id: uuid.UUID,
    employee_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> None:
    try:
        await unmark_payment(
            session,
            run_id,
            employee_id,
            actor_user_id=actor.user_id,
        )
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post(
    "/runs/{run_id}/payments/mark-all",
    response_model=PayrollPaymentsMarkAllResponse,
    dependencies=PAYROLL_RUNS_MARK_PAID_ACCESS,
)
async def post_mark_all_payments(
    run_id: uuid.UUID,
    payload: PayrollPaymentsMarkAllRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> PayrollPaymentsMarkAllResponse:
    try:
        marked_count = await mark_all_payments(
            session,
            run_id,
            paid_at=payload.paid_at,
            method=payload.method,
            actor_user_id=actor.user_id,
        )
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return PayrollPaymentsMarkAllResponse(marked_count=marked_count)


@router.post(
    "/runs/{run_id}/payments/bulk-mark",
    response_model=PayrollPaymentsMarkAllResponse,
    dependencies=PAYROLL_RUNS_MARK_PAID_ACCESS,
)
async def post_bulk_mark_payments(
    run_id: uuid.UUID,
    payload: PayrollPaymentsBulkMarkRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> PayrollPaymentsMarkAllResponse:
    try:
        marked_count = await mark_payments_selected(
            session,
            run_id,
            payload.employee_ids,
            paid_at=payload.paid_at,
            actor_user_id=actor.user_id,
        )
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return PayrollPaymentsMarkAllResponse(marked_count=marked_count)


@router.post(
    "/runs/{run_id}/payments/partial",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=PAYROLL_RUNS_MARK_PAID_ACCESS,
)
async def post_mark_partial_payment(
    run_id: uuid.UUID,
    payload: PayrollPaymentPartialRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> None:
    try:
        await mark_partial_payment(
            session,
            run_id,
            payload.employee_id,
            amount=payload.amount,
            paid_at=payload.paid_at,
            method=payload.method,
            comment=payload.comment,
            actor_user_id=actor.user_id,
        )
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


def _pool_payout_response(result: PoolPayoutResult) -> PayrollPoolPayoutResponse:
    return PayrollPoolPayoutResponse(
        reserve_id=result.reserve_id,
        primary_booked=result.primary_booked,
        overflow_reserve_id=result.overflow_reserve_id,
        overflow_booked=result.overflow_booked,
        employees_paid=result.employees_paid,
    )


@router.post(
    "/reserves/{reserve_id}/payout",
    response_model=PayrollPoolPayoutResponse,
    dependencies=PAYROLL_RUNS_MARK_PAID_ACCESS,
)
async def post_pay_run_from_pool(
    reserve_id: uuid.UUID,
    payload: PayrollPoolPayoutRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> PayrollPoolPayoutResponse:
    """Выплатить сотрудникам ведомости из пула-резерва (Сейф/касса) с перетоком на второй пул."""
    try:
        result = await pay_run_from_pool(
            session,
            reserve_id=reserve_id,
            selected_ids=set(payload.selected_ids) if payload.selected_ids is not None else None,
            boundary_override=payload.boundary_id,
            allow_overflow=payload.allow_overflow,
            paid_at=payload.paid_at,
            actor_user_id=actor.user_id,
        )
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _pool_payout_response(result)


@router.post(
    "/reserves/{reserve_id}/pay-employee",
    response_model=PayrollReserveEmployeePayResponse,
    dependencies=PAYROLL_RUNS_MARK_PAID_ACCESS,
)
async def post_pay_employee_from_reserve(
    reserve_id: uuid.UUID,
    payload: PayrollReserveEmployeePayRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> PayrollReserveEmployeePayResponse:
    """Выплатить одному сотруднику ручную сумму из резерва (карандаш → сумма → ✓); остаток
    резерва остаётся earmark'ом для следующей выплаты."""
    try:
        result = await pay_employee_from_reserve(
            session,
            reserve_id=reserve_id,
            employee_id=payload.employee_id,
            amount=payload.amount,
            paid_at=payload.paid_at,
            actor_user_id=actor.user_id,
        )
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return PayrollReserveEmployeePayResponse(
        booked=result.booked,
        employee_total_paid=result.employee_total_paid,
        employee_remaining=result.employee_remaining,
        reserve_status=result.reserve_status,
        reserve_outstanding=result.reserve_outstanding,
    )


@router.get(
    "/runs/{run_id}/solvency",
    response_model=PayrollSolvencyRead,
    dependencies=PAYROLL_RUNS_READ_ACCESS,
)
async def get_run_solvency(
    run_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> PayrollSolvencyRead:
    """Хватит ли денег на выплату ведомости (advisory — банк/овердрафт по последней выписке)."""
    run = await session.get(PayrollRun, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ведомость не найдена")
    breakdown = await run_solvency(session, run)
    return PayrollSolvencyRead(
        available=breakdown.available,
        required_total=breakdown.required_total,
        remaining=breakdown.remaining,
        shortfall=breakdown.shortfall,
        overdraft_limit=breakdown.overdraft_limit,
        safe_balance=breakdown.safe_balance,
        kassa_balance=breakdown.kassa_balance,
        bank_total=breakdown.bank_total,
        reserved_other=breakdown.reserved_other,
        solvent=breakdown.solvent,
    )


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


async def get_payments_by_employee(
    session: AsyncSession,
    run_id: uuid.UUID,
    employee_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, PayrollPayment]:
    unique_employee_ids = list(set(employee_ids))
    if not unique_employee_ids:
        return {}
    if not hasattr(session, "scalars"):
        return {}
    result = await session.scalars(
        select(PayrollPayment).where(
            PayrollPayment.run_id == run_id,
            PayrollPayment.employee_id.in_(unique_employee_ids),
        )
    )
    return {payment.employee_id: payment for payment in result.all()}


def line_is_on_demand(line: PayrollLine) -> bool:
    """Строка ведомости в режиме оклада «по востребованию» (ЗП собственника)."""
    components = line.components if isinstance(line.components, dict) else {}
    proration = components.get("proration")
    return isinstance(proration, dict) and bool(proration.get("on_demand"))


def serialize_payroll_line(
    line: PayrollLine,
    payouts_by_employee: dict[uuid.UUID, float],
    payments_by_employee: dict[uuid.UUID, PayrollPayment] | None = None,
    on_demand_debt_by_employee: dict[uuid.UUID, dict[str, Any]] | None = None,
) -> PayrollLineRead:
    components = line.components if isinstance(line.components, dict) else {}
    if getattr(line, "ndfl_withheld", None) is None:
        line.ndfl_withheld = Decimal("0")
    if getattr(line, "deposit_payout_scheduled", None) is None:
        line.deposit_payout_scheduled = Decimal("0")
    payment = (payments_by_employee or {}).get(line.employee_id)
    is_paid = payment is not None and payment.status == "paid"
    is_partial = payment is not None and payment.status == "partially_paid"
    has_payment = is_paid or is_partial
    is_on_demand = line_is_on_demand(line)
    debt = (on_demand_debt_by_employee or {}).get(line.employee_id) if is_on_demand else None
    return PayrollLineRead.model_validate(line).model_copy(
        update={
            "deposit_withholding": money_float(components.get("deposit_withholding", 0)),
            "deposit_payout": payouts_by_employee.get(line.employee_id, 0),
            "advance_issued": money_float(components.get("advance_issued", 0)),
            "ndfl_deduction": money_float(getattr(line, "ndfl_withheld", 0)),
            "payment_status": (
                "paid" if is_paid else ("partially_paid" if is_partial else "pending")
            ),
            "amount_cash": money_float(payment.amount_cash) if payment is not None else 0,
            "amount_account": money_float(payment.amount_account) if payment is not None else 0,
            "payout_status": payment.status if payment is not None else "pending",
            "draft_status": payment.draft_status if payment is not None else None,
            "overpaid_amount": money_float(payment.overpaid_amount) if payment is not None else 0,
            "paid_amount": money_float(payment.amount) if has_payment else None,
            "paid_at": payment.paid_at if has_payment else None,
            "paid_method": payment.method if has_payment else None,
            "payment_comment": payment.comment if is_partial else None,
            "on_demand": is_on_demand,
            "on_demand_accrued": money_float(debt["accrued"]) if debt is not None else 0,
            "on_demand_paid": money_float(debt["paid"]) if debt is not None else 0,
            "on_demand_debt": money_float(debt["debt"]) if debt is not None else 0,
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
        "start_period_start": charge.start_period_start,
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

"""API авансов и займов сотрудников.

Доступное к авансу, выдача (классификация аванс/заём по правам инициатора),
отмена, списание и реестр. Маршрутизация прав по пайплайну: админ-должности →
`payroll.advances.admin.issue`, прочие → `payroll.advances.production.issue`;
выдача сверх заработанного (заём) требует `payroll.loans.issue` (право B).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    CurrentActor,
    ensure_any_permission,
    ensure_permission,
    get_current_actor,
    require_permission,
)
from app.auth.permissions import permission_is_granted
from app.db.session import get_session
from app.models import Account, Employee, SalaryAdvance, SalaryAdvanceBankDraft, Wallet
from app.services.employee_effective_events import get_position_on_date
from app.services.payroll_advance_availability import AdvanceAvailability, available_to_advance
from app.services.payroll_advance_service import (
    ADVANCE_TK_WALLET_CODE,
    advance_payout_status,
    cancel_advance,
    cancel_kassa_advance,
    disburse_bank_advance,
    get_loan_max,
    issue_advance,
    list_advance_drafts,
    list_advances,
    set_loan_max,
    write_off_advance,
)
from app.services.payroll_payouts import list_cash_wallets
from app.services.payroll_runner import PayrollConflictError, PayrollNotFoundError
from app.services.position_registry import admin_payroll_positions

router = APIRouter()

# Расчётный «сегодня» — по Москве (как весь payroll-контур), чтобы дефолтная дата и
# отсечка «день выплаты» считались в тех же сутках, что дата выплаты, а не в UTC.
_MOSCOW_TZ = ZoneInfo("Europe/Moscow")

ADVANCES_READ_ACCESS = (Depends(require_permission("payroll.advances.read")),)
LOAN_ISSUE_ACCESS = (Depends(require_permission("payroll.loans.issue")),)

_ISSUE_ADMIN = "payroll.advances.admin.issue"
_ISSUE_PRODUCTION = "payroll.advances.production.issue"
_LOAN_ISSUE = "payroll.loans.issue"
_BACKDATE = "payroll.advances.backdate"
_ISSUE_CODES = (_ISSUE_ADMIN, _ISSUE_PRODUCTION)


class AdvanceAvailabilityRead(BaseModel):
    employee_id: uuid.UUID
    as_of: date
    period_start: date | None
    period_end: date | None
    basis: str
    earned_to_date: float
    already_advanced: float
    available: float
    note: str | None = None
    payout_reached: bool = False


class AdvanceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employee_id: uuid.UUID
    role: str
    kind: str
    amount: float
    per_installment_amount: float
    installments_count: int
    recovered_amount: float
    status: str
    issued_on: date
    recovery_start_date: date | None = None
    payout_method: str | None = None
    wallet_id: uuid.UUID | None = None
    comment: str | None = None
    # Сводный статус выплаты (наличная/банковская): disbursed / sent_to_bank /
    # awaiting_payout / failed / cancelled. См. advance_payout_status.
    payout_status: str = "disbursed"


def _advance_read(
    advance: SalaryAdvance, draft: SalaryAdvanceBankDraft | None
) -> AdvanceRead:
    read = AdvanceRead.model_validate(advance)
    read.payout_status = advance_payout_status(advance, draft)
    return read


class AdvanceIssueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employee_id: uuid.UUID
    amount: Decimal = Field(gt=0)
    # None → авто-классификация по заработанному; 'loan' → заём даже в пределах заработанного.
    kind: Literal["advance", "loan"] | None = None
    issued_on: date | None = None
    payout_method: str | None = None
    # Счёт-источник выдачи (ТК Черникова / Сейф / банк) — определяет проводку ДДС.
    wallet_id: uuid.UUID | None = None
    installments_count: int = Field(default=1, ge=1)
    # Сумма доли удержания (приоритет над installments_count для займа).
    installment_amount: Decimal | None = Field(default=None, gt=0)
    # Дата начала удержания займа (NULL = с ближайшей ведомости).
    recovery_start_date: date | None = None
    comment: str | None = None
    # Превышение потолка займа (требует права B + подтверждения в UI).
    override_ceiling: bool = False


class WriteOffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = None


class AdvanceConfigRead(BaseModel):
    loan_max: float


class AdvanceConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    loan_max: Decimal = Field(gt=0)


def _availability_read(av: AdvanceAvailability) -> AdvanceAvailabilityRead:
    return AdvanceAvailabilityRead(
        employee_id=av.employee_id,
        as_of=av.as_of,
        period_start=av.period_start,
        period_end=av.period_end,
        basis=av.basis,
        earned_to_date=float(av.earned_to_date),
        already_advanced=float(av.already_advanced),
        available=float(av.available),
        note=av.note,
        payout_reached=av.payout_reached,
    )


async def _require_employee(session: AsyncSession, employee_id: uuid.UUID) -> Employee:
    employee = await session.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Сотрудник не найден")
    return employee


@router.get(
    "/availability",
    response_model=AdvanceAvailabilityRead,
    dependencies=ADVANCES_READ_ACCESS,
)
async def get_advance_availability(
    employee_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    as_of: date | None = None,
    apply_payout_gate: bool = True,
) -> AdvanceAvailabilityRead:
    employee = await _require_employee(session, employee_id)
    # apply_payout_gate=false — для реконсиляции уже прошедших операций (дата операции
    # в прошлом не должна «обнулять» исторический аванс отсечкой «день выплаты»).
    availability = await available_to_advance(
        session,
        employee,
        as_of or datetime.now(_MOSCOW_TZ).date(),
        apply_payout_gate=apply_payout_gate,
    )
    return _availability_read(availability)


@router.get("", response_model=list[AdvanceRead], dependencies=ADVANCES_READ_ACCESS)
async def get_advances(
    session: Annotated[AsyncSession, Depends(get_session)],
    employee_id: uuid.UUID | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> list[AdvanceRead]:
    statuses = (
        tuple(part.strip() for part in status_filter.split(",") if part.strip())
        if status_filter
        else None
    )
    rows = await list_advances(session, employee_id=employee_id, statuses=statuses)
    drafts = await list_advance_drafts(session, [row.id for row in rows])
    return [_advance_read(row, drafts.get(row.id)) for row in rows]


class AdvanceIssueWalletRead(BaseModel):
    """Счёт выдачи: наличный (ДДС+iiko сразу) или банковский (черновик→вебхук→iiko)."""

    id: uuid.UUID
    code: str
    name: str
    channel: Literal["cash", "bank"]


async def _resolve_bank_issue_wallet(
    session: AsyncSession, provider: str = "tbank"
) -> Wallet | None:
    """Кошелёк банковской выдачи для провайдера (tbank/sber) = активный счёт, привязанный к
    настроенному счёту исходящих платежей этого банка. Источник истины — конфиг платежей."""
    from app.core.config import get_settings
    from app.services.banking.payout import payer_account_for

    payer_account = payer_account_for(get_settings(), provider)
    if not payer_account:
        return None
    return await session.scalar(
        select(Wallet)
        .join(Account, Account.id == Wallet.account_id)
        .where(Account.account_number == payer_account, Wallet.status == "active")
    )


@router.get(
    "/issue-wallets",
    response_model=list[AdvanceIssueWalletRead],
    dependencies=ADVANCES_READ_ACCESS,
)
async def get_issue_wallets(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[AdvanceIssueWalletRead]:
    """Счета для выдачи аванса/займа: наличные (ТК Черникова/Сейф) + банковский расчётный.

    Наличная выдача проводится в ДДС сразу (+ изъятие в iiko для ТК Черникова);
    банковская — создаёт черновик платежа в Т-Банке, ДДС из выписки, iiko по «исполнен».
    """
    wallets = [
        AdvanceIssueWalletRead(id=w.id, code=w.code, name=w.name, channel="cash")
        for w in await list_cash_wallets(session)
    ]
    # Банковские счета выдачи: Т-Банк и Сбер (черновик в соответствующем банке). Дедуп по id
    # на случай, если провайдеры резолвятся в один кошелёк (mock).
    seen_bank_ids: set[uuid.UUID] = set()
    for provider in ("tbank", "sber"):
        bank = await _resolve_bank_issue_wallet(session, provider)
        if bank is not None and bank.id not in seen_bank_ids:
            seen_bank_ids.add(bank.id)
            wallets.append(
                AdvanceIssueWalletRead(id=bank.id, code=bank.code, name=bank.name, channel="bank")
            )
    return wallets


@router.post("", response_model=AdvanceRead, status_code=status.HTTP_201_CREATED)
async def post_advance(
    payload: AdvanceIssueRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> AdvanceRead:
    employee = await _require_employee(session, payload.employee_id)
    today = datetime.now(_MOSCOW_TZ).date()
    # Будущей датой нельзя никому; прошлой — только по отдельному праву backdate;
    # сегодняшней/без даты — по обычному праву выдачи (проверяется ниже по пайплайну).
    if payload.issued_on is not None and payload.issued_on > today:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Нельзя провести выдачу будущей датой",
        )
    if payload.issued_on is not None and payload.issued_on < today:
        ensure_permission(actor, _BACKDATE)
    permission_date = payload.issued_on or today
    position = await get_position_on_date(session, employee.id, permission_date)
    position = position or employee.position or ""
    issue_code = _ISSUE_ADMIN if position in admin_payroll_positions() else _ISSUE_PRODUCTION
    ensure_permission(actor, issue_code)
    allow_loan = permission_is_granted(_LOAN_ISSUE, actor.permissions)
    # Счёт «ТК Черникова» сегодняшней датой — не мгновенная выплата, а РАЗРЕШЕНИЕ на
    # выдачу через кассу (исполнит администратор во вкладке «К выдаче»). Задним числом
    # деньги уже выданы — остаётся мгновенная фиксация, как раньше. Отдельного права
    # нет: разрешение покрыто правом выдачи авансов/займов.
    kassa_pending = False
    if payload.wallet_id is not None and (payload.issued_on is None or payload.issued_on >= today):
        wallet = await session.get(Wallet, payload.wallet_id)
        kassa_pending = wallet is not None and wallet.code == ADVANCE_TK_WALLET_CODE
    try:
        advance = await issue_advance(
            session,
            employee_id=payload.employee_id,
            amount=payload.amount,
            allow_loan=allow_loan,
            requested_kind=payload.kind,
            override_ceiling=payload.override_ceiling,
            issued_on=payload.issued_on,
            payout_method=payload.payout_method,
            wallet_id=payload.wallet_id,
            installments_count=payload.installments_count,
            installment_amount=payload.installment_amount,
            recovery_start_date=payload.recovery_start_date,
            comment=payload.comment,
            actor_user_id=actor.user_id,
            kassa_pending=kassa_pending,
        )
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    drafts = await list_advance_drafts(session, [advance.id])
    return _advance_read(advance, drafts.get(advance.id))


@router.post("/{advance_id}/mark-paid", response_model=AdvanceRead)
async def post_mark_advance_paid(
    advance_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> AdvanceRead:
    """«Выплачено»: подтвердить фактическую выдачу банк-аванса (списание резерва с Сейфа)."""
    ensure_any_permission(actor, _ISSUE_CODES)
    try:
        advance = await disburse_bank_advance(session, advance_id=advance_id)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    drafts = await list_advance_drafts(session, [advance.id])
    return _advance_read(advance, drafts.get(advance.id))


@router.post("/{advance_id}/cancel", response_model=AdvanceRead)
async def post_cancel_advance(
    advance_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> AdvanceRead:
    ensure_any_permission(actor, _ISSUE_CODES)
    try:
        advance = await cancel_advance(session, advance_id)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    drafts = await list_advance_drafts(session, [advance.id])
    return _advance_read(advance, drafts.get(advance.id))


@router.post("/{advance_id}/revoke-kassa", response_model=AdvanceRead)
async def post_revoke_kassa_advance(
    advance_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> AdvanceRead:
    """Отозвать разрешение на выдачу через кассу (пока администратор не исполнил).

    Только для pending-разрешений: после исполнения админом — 409 «уже выдано»
    (выданный аванс снимается обычной отменой выдачи, это другая операция).
    """
    ensure_any_permission(actor, _ISSUE_CODES)
    try:
        advance = await cancel_kassa_advance(
            session, advance_id=advance_id, by_kassa_admin=False
        )
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    drafts = await list_advance_drafts(session, [advance.id])
    return _advance_read(advance, drafts.get(advance.id))


@router.post(
    "/{advance_id}/write-off",
    response_model=AdvanceRead,
    dependencies=LOAN_ISSUE_ACCESS,
)
async def post_write_off_advance(
    advance_id: uuid.UUID,
    payload: WriteOffRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AdvanceRead:
    try:
        advance = await write_off_advance(session, advance_id, reason=payload.reason)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    drafts = await list_advance_drafts(session, [advance.id])
    return _advance_read(advance, drafts.get(advance.id))


@router.get("/config", response_model=AdvanceConfigRead, dependencies=ADVANCES_READ_ACCESS)
async def get_advance_config(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AdvanceConfigRead:
    return AdvanceConfigRead(loan_max=float(await get_loan_max(session)))


@router.put("/config", response_model=AdvanceConfigRead, dependencies=LOAN_ISSUE_ACCESS)
async def put_advance_config(
    payload: AdvanceConfigUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AdvanceConfigRead:
    try:
        loan_max = await set_loan_max(session, payload.loan_max)
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return AdvanceConfigRead(loan_max=float(loan_max))

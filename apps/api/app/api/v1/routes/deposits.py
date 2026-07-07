from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    CurrentActor,
    ensure_permission,
    get_current_actor,
    require_any_permission,
    require_permission,
)
from app.auth.permissions import payout_channel_permission
from app.db.session import get_session
from app.models import DepositAccount, DepositTransaction, Employee
from app.services import deposit_schedule, deposit_service
from app.services.deposit_bank_draft import (
    PRODUCTION_DEPOSIT_PAYOUT_DRAFT_SOURCE_KIND,
    book_deposit_bank_to_safe_transfer,
    send_deposit_payout_bank_draft,
)
from app.services.deposit_iiko_payout_production import post_production_deposit_payout_to_iiko
from app.services.payroll_calculator import (
    decimal,
    employee_deposit_target,
    employee_deposit_withholding,
    is_deposit_currently_excluded,
    load_payroll_settings,
)
from app.services.position_registry import production_payroll_positions

router = APIRouter()
DEPOSITS_READ_ACCESS = (
    Depends(
        require_any_permission(("payroll.production_deposits.read", "source.deposit_settings.read"))
    ),
)
DEPOSITS_EDIT_ACCESS = (
    Depends(
        require_any_permission(("payroll.production_deposits.edit", "source.deposit_settings.edit"))
    ),
)
# Гранулярные права операций (этап RBAC депозитов): выдача и списание — отдельно;
# настройка (исключения/начальный баланс) остаётся под DEPOSITS_EDIT_ACCESS.
DEPOSITS_PAYOUT_ACCESS = (Depends(require_permission("payroll.production_deposits.payout")),)
DEPOSITS_WRITEOFF_ACCESS = (Depends(require_permission("payroll.production_deposits.write_off")),)


class DepositEmployeeRead(BaseModel):
    id: uuid.UUID
    full_name: str
    position: str
    category: str | None = None
    balance: str
    initial_balance: str
    target: str | None = None
    withholding: str | None = None
    is_excluded: bool
    excluded_until: str | None = None
    progress_pct: str
    # Отложенная выдача депозита (этап 4): есть ли pending-план и на какую сумму.
    scheduled_payout_pending: bool = False
    scheduled_payout_amount: str | None = None


class DepositTransactionRead(BaseModel):
    id: uuid.UUID
    employee_id: uuid.UUID
    run_id: uuid.UUID | None = None
    transaction_type: str
    amount: str
    created_at: str | None = None


class DepositConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deposit_target_override: Decimal | None = Field(default=None, ge=0)
    deposit_withholding_override: Decimal | None = Field(default=None, ge=0)
    deposit_excluded: bool | None = None
    deposit_excluded_until: date | None = None
    deposit_excluded_reason: str | None = Field(default=None, max_length=1000)


class DepositConfigRead(BaseModel):
    employee_id: uuid.UUID
    deposit_target_override: str | None = None
    deposit_withholding_override: str | None = None
    deposit_excluded: bool
    deposit_excluded_until: str | None = None
    deposit_excluded_reason: str | None = None


class DepositPayoutRequest(BaseModel):
    amount: Decimal | None = Field(default=None, gt=0)
    comment: str | None = Field(default=None, max_length=1000)
    # Счёт немедленной выдачи: ТК Черникова (по умолч., +iiko-изъятие) / Сейф (без iiko).
    # bank_draft появится на этапе 3; отложенная выдача через ЗП — на этапе 4.
    payout_method: Literal["cash_tk", "cash_safe", "bank_draft"] = "cash_tk"


class DepositSchedulePayoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Сумма выдачи; None = весь накопленный баланс на момент ближайшей ведомости.
    amount: Decimal | None = Field(default=None, gt=0)
    account_choice: Literal["safe", "cash_tk", "bank_draft"] = "safe"


class DepositScheduleRead(BaseModel):
    employee_id: uuid.UUID
    status: str
    requested_amount: str | None = None
    account_choice: str
    created_at: str | None = None


class DepositScheduledPayoutSettingsRead(BaseModel):
    enabled: bool


class DepositScheduleCancelRead(BaseModel):
    cancelled: bool


class DepositWriteoffRequest(BaseModel):
    amount: Decimal = Field(gt=0)
    reason: str | None = Field(default=None, max_length=1000)


class DepositInitialBalanceRequest(BaseModel):
    amount: Decimal = Field(ge=0)


class DepositOperationRead(BaseModel):
    employee_id: uuid.UUID
    balance: str
    transaction: DepositTransactionRead


@router.get("", response_model=list[DepositEmployeeRead], dependencies=DEPOSITS_READ_ACCESS)
async def list_deposits(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list[dict[str, Any]]:
    result = await session.scalars(
        select(Employee)
        # Плейсхолдеры пула «Внештат №N» — не сотрудники, депозитов у них нет.
        .where(
            Employee.position.in_(production_payroll_positions()),
            Employee.is_freelancer_placeholder.is_(False),
        )
        .order_by(Employee.full_name)
    )
    employees = result.all()
    accounts = await deposit_service.load_accounts(session, [employee.id for employee in employees])
    settings = await load_payroll_settings(session)
    schedules = await deposit_schedule.load_pending_schedules(
        session, [employee.id for employee in employees]
    )
    today = date.today()
    return [
        _deposit_employee_payload(
            employee,
            accounts.get(employee.id),
            settings,
            today,
            schedules.get(employee.id),
        )
        for employee in employees
    ]


@router.get(
    "/{employee_id}/transactions",
    response_model=list[DepositTransactionRead],
    dependencies=DEPOSITS_READ_ACCESS,
)
async def get_deposit_transactions(
    employee_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list[dict[str, Any]]:
    await _get_employee_or_404(session, employee_id)
    result = await session.scalars(
        select(DepositTransaction)
        .where(DepositTransaction.employee_id == employee_id)
        .order_by(DepositTransaction.created_at.desc())
    )
    return [deposit_service.transaction_payload(transaction) for transaction in result.all()]


@router.patch(
    "/{employee_id}/config",
    response_model=DepositConfigRead,
    dependencies=DEPOSITS_EDIT_ACCESS,
)
async def patch_deposit_config(
    employee_id: uuid.UUID,
    payload: DepositConfigPatch,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    employee = await _get_employee_or_404(session, employee_id)
    before = _deposit_config_snapshot(employee)

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(employee, field, value)
    employee.updated_at = datetime.now(UTC)

    after = _deposit_config_snapshot(employee)
    await deposit_service.add_deposit_action(
        session,
        action_type="deposit_config_update",
        target_table="employee",
        target_id=employee.id,
        employee_id=employee.id,
        before=before,
        after=after,
        now=employee.updated_at,
        actor=actor,
    )
    await session.commit()
    await session.refresh(employee)
    return _deposit_config_snapshot(employee)


@router.post(
    "/{employee_id}/payout",
    response_model=DepositOperationRead,
    dependencies=DEPOSITS_PAYOUT_ACCESS,
)
async def payout_deposit(
    employee_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    payload: Annotated[DepositPayoutRequest | None, Body()] = None,
) -> dict[str, Any]:
    payload = payload or DepositPayoutRequest()
    # Немедленная выдача с конкретного счёта требует права на этот канал
    # (Сейф / ТК Черникова / банк-черновик), помимо права выдавать депозит.
    channel_perm = payout_channel_permission(payload.payout_method)
    if channel_perm is not None:
        ensure_permission(actor, channel_perm)
    employee = await _get_employee_or_404(session, employee_id)
    now = datetime.now(UTC)
    account = await deposit_service.get_deposit_account(session, employee_id, for_update=True)
    balance = decimal(account.balance) if account else Decimal("0")
    amount = decimal(payload.amount) if payload.amount is not None else balance
    if amount <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Сумма выплаты должна быть больше 0",
        )
    if amount > balance:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Сумма выплаты больше текущего баланса",
        )
    before = deposit_service.deposit_account_snapshot(account)
    account = deposit_service.ensure_account(session, employee_id, account, now)
    account.balance = balance - amount
    account.last_updated = now
    transaction = deposit_service.add_transaction(
        session,
        employee_id=employee_id,
        transaction_type="payout",
        amount=amount,
        now=now,
    )
    # Реальная выдача денег → проводка ДДС (расход с выбранного счёта; для
    # банк-черновика — с Сейфа).
    payout_wallet = await deposit_service.book_production_deposit_payout_cashflow(
        session,
        transaction=transaction,
        payout_method=payload.payout_method,
        transaction_date=now.date(),
        comment=payload.comment,
    )
    # Банк-черновик: транзит банк→Сейф (расход выше списан с Сейфа), сам черновик — после commit.
    if payload.payout_method == "bank_draft":
        await book_deposit_bank_to_safe_transfer(
            session,
            source_kind=PRODUCTION_DEPOSIT_PAYOUT_DRAFT_SOURCE_KIND,
            source_id=transaction.id,
            amount=amount,
            operation_date=now.date(),
            purpose=f"Выдача депозита {employee.full_name} (через Сейф)",
        )
    after = deposit_service.deposit_account_snapshot(account) | {
        "transaction": deposit_service.transaction_payload(transaction),
        "comment": payload.comment,
    }
    await deposit_service.add_deposit_action(
        session,
        action_type="deposit_payout",
        target_table="deposit_account",
        target_id=account.id,
        employee_id=employee_id,
        before=before,
        after=after,
        now=now,
        actor=actor,
        comment=payload.comment,
    )
    await session.commit()
    await session.refresh(account)
    # iiko-изъятие из «Главной кассы» — только при выдаче с ТК Черникова (= iiko Главная
    # касса). После commit: БД — источник истины, ошибка iiko не откатывает выдачу.
    if payout_wallet is not None and payout_wallet.code == "tk_chernikova":
        post_production_deposit_payout_to_iiko(
            amount=amount, payout_date=now.date(), source_id=transaction.id
        )
    # Банк-черновик на ИП Шокину (как ЗП) — после commit, ошибка банка не валит выдачу.
    if payload.payout_method == "bank_draft":
        await send_deposit_payout_bank_draft(
            session,
            document_id=f"teplo-deposit-{transaction.id}",
            amount=amount,
            purpose=f"Выдача депозита {employee.full_name} (через Сейф)",
        )
    return _operation_payload(account, transaction)


@router.get(
    "/scheduled-payout/settings",
    response_model=DepositScheduledPayoutSettingsRead,
    dependencies=DEPOSITS_READ_ACCESS,
)
async def get_scheduled_payout_settings(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    enabled = await deposit_schedule.is_scheduled_payout_enabled(session)
    return {"enabled": enabled}


@router.post(
    "/{employee_id}/schedule-payout",
    response_model=DepositScheduleRead,
    dependencies=DEPOSITS_PAYOUT_ACCESS,
)
async def schedule_deposit_payout(
    employee_id: uuid.UUID,
    payload: DepositSchedulePayoutRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    """Запланировать выдачу депозита в ближайшей ЗП-ведомости (отложенная выдача)."""
    if not await deposit_schedule.is_scheduled_payout_enabled(session):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Отложенная выдача депозита выключена",
        )
    await _get_employee_or_404(session, employee_id)
    schedule = await deposit_schedule.create_or_replace_schedule(
        session,
        employee_id,
        requested_amount=decimal(payload.amount) if payload.amount is not None else None,
        account_choice=payload.account_choice,
        created_by_user_id=actor.user_id,
    )
    await session.commit()
    await session.refresh(schedule)
    return _schedule_payload(schedule)


@router.delete(
    "/{employee_id}/schedule-payout",
    response_model=DepositScheduleCancelRead,
    dependencies=DEPOSITS_PAYOUT_ACCESS,
)
async def cancel_scheduled_deposit_payout(
    employee_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    """Отменить запланированную выдачу депозита (сотрудник передумал увольняться)."""
    await _get_employee_or_404(session, employee_id)
    cancelled = await deposit_schedule.cancel_pending_schedule(session, employee_id)
    await session.commit()
    return {"cancelled": cancelled}


@router.post(
    "/{employee_id}/writeoff",
    response_model=DepositOperationRead,
    dependencies=DEPOSITS_WRITEOFF_ACCESS,
)
async def writeoff_deposit(
    employee_id: uuid.UUID,
    payload: DepositWriteoffRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    await _get_employee_or_404(session, employee_id)
    now = datetime.now(UTC)
    account = await deposit_service.get_deposit_account(session, employee_id, for_update=True)
    balance = decimal(account.balance) if account else Decimal("0")
    amount = decimal(payload.amount)
    if amount > balance:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Сумма списания больше текущего баланса",
        )
    before = deposit_service.deposit_account_snapshot(account)
    account = deposit_service.ensure_account(session, employee_id, account, now)
    account.balance = balance - amount
    account.last_updated = now
    transaction = deposit_service.add_transaction(
        session,
        employee_id=employee_id,
        transaction_type="write_off",
        amount=amount,
        now=now,
    )
    after = deposit_service.deposit_account_snapshot(account) | {
        "transaction": deposit_service.transaction_payload(transaction),
        "reason": payload.reason,
    }
    await deposit_service.add_deposit_action(
        session,
        action_type="deposit_writeoff",
        target_table="deposit_account",
        target_id=account.id,
        employee_id=employee_id,
        before=before,
        after=after,
        now=now,
        actor=actor,
        comment=payload.reason,
    )
    await session.commit()
    await session.refresh(account)
    return _operation_payload(account, transaction)


@router.post(
    "/{employee_id}/initial-balance",
    response_model=DepositOperationRead,
    dependencies=DEPOSITS_EDIT_ACCESS,
)
async def set_initial_deposit_balance(
    employee_id: uuid.UUID,
    payload: DepositInitialBalanceRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    await _get_employee_or_404(session, employee_id)
    now = datetime.now(UTC)
    account = await deposit_service.get_deposit_account(session, employee_id, for_update=True)
    if account is not None and decimal(getattr(account, "initial_balance", 0)) > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Исторический баланс уже установлен. Используйте корректировку через "
                "ручную транзакцию."
            ),
        )
    before = deposit_service.deposit_account_snapshot(account)
    existing_balance = decimal(account.balance) if account else Decimal("0")
    account = deposit_service.ensure_account(session, employee_id, account, now)
    amount = decimal(payload.amount)
    account.initial_balance = amount
    if existing_balance == 0:
        account.balance = amount
    account.last_updated = now
    transaction = deposit_service.add_transaction(
        session,
        employee_id=employee_id,
        transaction_type="accrual",
        amount=amount,
        now=now,
    )
    after = deposit_service.deposit_account_snapshot(account) | {
        "transaction": deposit_service.transaction_payload(transaction),
        "initial": True,
    }
    await deposit_service.add_deposit_action(
        session,
        action_type="deposit_initial_balance",
        target_table="deposit_account",
        target_id=account.id,
        employee_id=employee_id,
        before=before,
        after=after,
        now=now,
        actor=actor,
    )
    await session.commit()
    await session.refresh(account)
    return _operation_payload(account, transaction)


async def _get_employee_or_404(session: AsyncSession, employee_id: uuid.UUID) -> Employee:
    employee = await session.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Сотрудник не найден")
    return employee


def _deposit_employee_payload(
    employee: Employee,
    account: DepositAccount | None,
    settings: dict[str, Any],
    today: date,
    schedule: Any | None = None,
) -> dict[str, Any]:
    balance = decimal(account.balance) if account else Decimal("0")
    initial_balance = decimal(getattr(account, "initial_balance", 0)) if account else Decimal("0")
    target = employee_deposit_target(settings, employee)
    withholding = employee_deposit_withholding(settings, employee)
    progress = Decimal("0")
    if target is not None and target > 0:
        progress = min((balance / target) * Decimal("100"), Decimal("100"))
    return {
        "id": employee.id,
        "full_name": employee.full_name,
        "position": employee.position,
        "category": employee.category,
        "balance": deposit_service.decimal_string(balance),
        "initial_balance": deposit_service.decimal_string(initial_balance),
        "target": deposit_service.decimal_string(target),
        "withholding": deposit_service.decimal_string(withholding),
        "is_excluded": is_deposit_currently_excluded(employee, today),
        "excluded_until": deposit_service.date_string(
            getattr(employee, "deposit_excluded_until", None)
        ),
        "progress_pct": deposit_service.decimal_string(progress),
        "scheduled_payout_pending": schedule is not None,
        "scheduled_payout_amount": (
            deposit_service.decimal_string(schedule.requested_amount)
            if schedule is not None and schedule.requested_amount is not None
            else None
        ),
    }


def _operation_payload(
    account: DepositAccount,
    transaction: DepositTransaction,
) -> dict[str, Any]:
    return {
        "employee_id": account.employee_id,
        "balance": deposit_service.decimal_string(account.balance),
        "transaction": deposit_service.transaction_payload(transaction),
    }


def _schedule_payload(schedule: Any) -> dict[str, Any]:
    return {
        "employee_id": schedule.employee_id,
        "status": schedule.status,
        "requested_amount": deposit_service.decimal_string(schedule.requested_amount)
        if schedule.requested_amount is not None
        else None,
        "account_choice": schedule.account_choice,
        "created_at": schedule.created_at.isoformat() if schedule.created_at else None,
    }


def _deposit_config_snapshot(employee: Employee) -> dict[str, Any]:
    return {
        "employee_id": str(employee.id),
        "deposit_target_override": deposit_service.decimal_string(
            getattr(employee, "deposit_target_override", None)
        ),
        "deposit_withholding_override": deposit_service.decimal_string(
            getattr(employee, "deposit_withholding_override", None)
        ),
        "deposit_excluded": bool(getattr(employee, "deposit_excluded", False)),
        "deposit_excluded_until": deposit_service.date_string(
            getattr(employee, "deposit_excluded_until", None)
        ),
        "deposit_excluded_reason": getattr(employee, "deposit_excluded_reason", None),
    }

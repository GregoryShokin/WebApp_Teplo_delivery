from __future__ import annotations

import calendar
import uuid
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import extract, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, get_current_actor, require_permission
from app.db.session import get_session
from app.models import (
    AccumulationFundAccount,
    AccumulationFundTransaction,
    AgentAction,
    AgentRun,
    AppSetting,
    Employee,
)
from app.services.accumulation_fund_service import (
    decimal_string,
    fund_outstanding,
    payout_fund_accounts_for_year,
)
from app.services.attendance_loader import PAYROLL_TARGET_POSITIONS
from app.services.payroll_calculator import (
    _fund_rate_for_months,
    decimal,
    is_fund_currently_excluded,
    load_payroll_settings,
    tenure_months_on,
)

router = APIRouter()
FUND_READ_ACCESS = (Depends(require_permission("payroll.fund.read")),)
FUND_EDIT_ACCESS = (Depends(require_permission("payroll.fund.edit")),)
FUND_TIERS_SETTING_KEY = "payroll.fund_rates_by_tenure"
MONEY = Decimal("0.01")
FUND_TARGET_POSITIONS_ERROR = "Накопительный фонд ведётся только для Поваров и Кассиров"


class FundPayoutRequest(BaseModel):
    comment: str | None = Field(default=None, max_length=1000)


class FundTierItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_months: int = Field(ge=0, le=600)
    rate: Decimal = Field(ge=0, le=1)


class FundTiersWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tiers: list[FundTierItem]


class FundTiersRead(BaseModel):
    tiers: list[FundTierItem]
    updated_at: datetime | None = None
    updated_by_label: str | None = None


class FundInitialBalanceRequest(BaseModel):
    amount: Decimal = Field(ge=0)
    comment: str | None = Field(default=None, max_length=1000)


class FundInitialBalanceRead(BaseModel):
    employee_id: uuid.UUID
    year: int
    accumulated: str
    transaction_id: uuid.UUID
    created_at: datetime


class FundExclusionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fund_excluded: bool
    fund_excluded_until: date | None = None
    fund_excluded_reason: str | None = Field(default=None, max_length=1000)


class FundExclusionRead(BaseModel):
    employee_id: uuid.UUID
    fund_excluded: bool
    fund_excluded_until: date | None
    fund_excluded_reason: str | None
    is_currently_excluded: bool


class FundRosterAccount(BaseModel):
    year: int
    accumulated: str
    is_initial_set: bool
    initial_set_at: datetime | None = None
    initial_set_by_label: str | None = None


class FundRosterExclusion(BaseModel):
    fund_excluded: bool
    fund_excluded_until: date | None = None
    fund_excluded_reason: str | None = None
    is_currently_excluded: bool


class FundRosterRow(BaseModel):
    employee_id: uuid.UUID
    full_name: str
    position: str | None
    hire_date: date | None
    tenure_months: int | None
    current_rate_percent: str | None
    fund_exclusion: FundRosterExclusion
    fund_account: FundRosterAccount | None = None


@router.get("/tiers", response_model=FundTiersRead, dependencies=FUND_READ_ACCESS)
async def get_fund_tiers(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> FundTiersRead:
    setting = await _get_fund_tiers_setting(session)
    return FundTiersRead(
        tiers=_normalize_fund_tiers(setting.value if setting else []),
        updated_at=getattr(setting, "updated_at", None) if setting else None,
        updated_by_label=None,
    )


@router.put("/tiers", response_model=FundTiersRead, dependencies=FUND_EDIT_ACCESS)
async def put_fund_tiers(
    payload: FundTiersWrite,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> FundTiersRead:
    tiers = _validate_and_sort_fund_tiers(payload.tiers)
    setting = await _get_fund_tiers_setting(session)
    if setting is None:
        setting = AppSetting(
            id=uuid.uuid4(),
            key=FUND_TIERS_SETTING_KEY,
            value=[],
            value_type="object",
            category="Зарплата",
            display_name="Накопительный фонд",
            description="Процент накопительного фонда от оклада по стажу.",
            widget_type="json",
            widget_options=None,
            unit="%",
        )
        session.add(setting)

    now = datetime.now(UTC)
    before = {
        "key": FUND_TIERS_SETTING_KEY,
        "tiers": _tiers_to_json(_normalize_fund_tiers(setting.value)),
    }
    setting.value = _tiers_to_json(tiers)
    setting.updated_at = now
    after = {"key": FUND_TIERS_SETTING_KEY, "tiers": _tiers_to_json(tiers)}
    await _add_fund_action(
        session,
        action_type="payroll_fund_tiers_update",
        target_table="app_setting",
        target_id=setting.id,
        before=before,
        after=after,
        actor=actor,
        now=now,
    )
    await session.commit()
    return FundTiersRead(
        tiers=tiers,
        updated_at=setting.updated_at,
        updated_by_label=_actor_label(actor),
    )


@router.get(
    "/initial-balance-roster",
    response_model=list[FundRosterRow],
    dependencies=FUND_READ_ACCESS,
)
async def get_initial_balance_roster(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
    year: Annotated[int | None, Query()] = None,
) -> list[FundRosterRow]:
    year = year or date.today().year
    today = date.today()
    setting = await _get_fund_tiers_setting(session)
    settings = {
        FUND_TIERS_SETTING_KEY: _tiers_to_json(
            _normalize_fund_tiers(setting.value if setting else [])
        )
    }
    employees = (
        await session.scalars(
            select(Employee).where(
                Employee.status == "active",
                Employee.position.in_(PAYROLL_TARGET_POSITIONS),
            )
        )
    ).all()
    employee_ids = [employee.id for employee in employees]
    accounts: list[AccumulationFundAccount] = []
    initial_transactions: list[AccumulationFundTransaction] = []
    if employee_ids:
        accounts = (
            await session.scalars(
                select(AccumulationFundAccount).where(
                    AccumulationFundAccount.year == year,
                    AccumulationFundAccount.employee_id.in_(employee_ids),
                )
            )
        ).all()
        initial_transactions = (
            await session.scalars(
                select(AccumulationFundTransaction)
                .where(
                    AccumulationFundTransaction.year == year,
                    AccumulationFundTransaction.employee_id.in_(employee_ids),
                    AccumulationFundTransaction.transaction_type == "initial_balance",
                )
                .order_by(AccumulationFundTransaction.created_at.desc())
            )
        ).all()

    account_by_employee = {account.employee_id: account for account in accounts}
    initial_by_account: dict[uuid.UUID, AccumulationFundTransaction] = {}
    for transaction in initial_transactions:
        initial_by_account.setdefault(transaction.account_id, transaction)

    rows = [
        _fund_roster_row(
            employee,
            account_by_employee.get(employee.id),
            initial_by_account=initial_by_account,
            settings=settings,
            today=today,
            year=year,
        )
        for employee in employees
    ]
    return sorted(
        rows,
        key=lambda row: (
            bool(row.fund_account and row.fund_account.is_initial_set),
            row.full_name.casefold(),
        ),
    )


@router.post(
    "/{employee_id}/initial-balance",
    response_model=FundInitialBalanceRead,
    dependencies=FUND_EDIT_ACCESS,
)
async def set_fund_initial_balance(
    employee_id: uuid.UUID,
    payload: FundInitialBalanceRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> FundInitialBalanceRead:
    employee = await session.scalar(
        select(Employee).where(Employee.id == employee_id).with_for_update()
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Сотрудник не найден")
    if employee.position not in PAYROLL_TARGET_POSITIONS:
        raise HTTPException(
            status_code=422,
            detail=FUND_TARGET_POSITIONS_ERROR,
        )
    if employee.status != "active":
        raise HTTPException(
            status_code=422,
            detail="Невозможно установить начальный баланс уволенному сотруднику",
        )

    year = date.today().year
    now = datetime.now(UTC)
    account = await session.scalar(
        select(AccumulationFundAccount)
        .where(
            AccumulationFundAccount.employee_id == employee_id,
            AccumulationFundAccount.year == year,
        )
        .with_for_update()
    )
    existing_transactions = (
        await session.scalars(
            select(AccumulationFundTransaction)
            .where(
                AccumulationFundTransaction.employee_id == employee_id,
                AccumulationFundTransaction.year == year,
                AccumulationFundTransaction.transaction_type.in_(("accrual", "initial_balance")),
            )
            .limit(1)
        )
    ).all()
    if account is not None and (
        decimal(account.accumulated_amount) > 0 or existing_transactions
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Начальный баланс фонда уже установлен",
        )

    before = _account_snapshot(account)
    amount = decimal(payload.amount).quantize(MONEY)
    if account is None:
        account = AccumulationFundAccount(
            id=uuid.uuid4(),
            employee_id=employee_id,
            year=year,
            accumulated_amount=Decimal("0"),
            paid_out_amount=Decimal("0"),
            forfeited_amount=Decimal("0"),
            status="active",
        )
        session.add(account)

    account.accumulated_amount = amount
    account.status = "active"
    transaction = AccumulationFundTransaction(
        id=uuid.uuid4(),
        account_id=account.id,
        employee_id=employee_id,
        year=year,
        run_id=None,
        transaction_type="initial_balance",
        amount=amount,
        comment=payload.comment,
        created_at=now,
    )
    session.add(transaction)
    after = _account_snapshot(account) | {"transaction": transaction_payload(transaction, account)}
    await _add_fund_action(
        session,
        action_type="fund_initial_balance",
        target_table="accumulation_fund_account",
        target_id=account.id,
        before=before,
        after=after,
        actor=actor,
        now=now,
        params={"employee_id": str(employee_id), "year": year, "comment": payload.comment},
    )
    await session.commit()
    return FundInitialBalanceRead(
        employee_id=employee_id,
        year=year,
        accumulated=decimal_string(account.accumulated_amount),
        transaction_id=transaction.id,
        created_at=transaction.created_at,
    )


@router.patch(
    "/{employee_id}/exclusion",
    response_model=FundExclusionRead,
    dependencies=FUND_EDIT_ACCESS,
)
async def patch_fund_exclusion(
    employee_id: uuid.UUID,
    payload: FundExclusionPatch,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> FundExclusionRead:
    employee = await _get_employee_or_404(session, employee_id)
    if employee.position not in PAYROLL_TARGET_POSITIONS:
        raise HTTPException(status_code=422, detail=FUND_TARGET_POSITIONS_ERROR)

    before = _fund_exclusion_snapshot(employee)
    now = datetime.now(UTC)
    employee.fund_excluded = payload.fund_excluded
    employee.fund_excluded_until = payload.fund_excluded_until if payload.fund_excluded else None
    employee.fund_excluded_reason = payload.fund_excluded_reason if payload.fund_excluded else None
    employee.updated_at = now
    after = _fund_exclusion_snapshot(employee)

    await _add_fund_exclusion_action(
        session,
        action_type="fund_exclusion_update",
        employee_id=employee.id,
        before=before,
        after=after,
        now=now,
        actor=actor,
    )
    await session.commit()
    await session.refresh(employee)
    return _fund_exclusion_payload(employee, date.today())


@router.get("/summary", dependencies=FUND_READ_ACCESS)
async def get_fund_summary(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
    year: Annotated[int | None, Query()] = None,
) -> dict[str, Any]:
    year = year or date.today().year
    accounts = (
        await session.scalars(
            select(AccumulationFundAccount)
            .join(Employee, Employee.id == AccumulationFundAccount.employee_id)
            .where(
                AccumulationFundAccount.year == year,
                Employee.position.in_(PAYROLL_TARGET_POSITIONS),
            )
        )
    ).all()
    active_accounts = [account for account in accounts if account.status == "active"]
    payout_transactions = (
        await session.scalars(
            select(AccumulationFundTransaction).join(
                Employee, Employee.id == AccumulationFundTransaction.employee_id
            ).where(
                AccumulationFundTransaction.transaction_type == "payout",
                extract("year", AccumulationFundTransaction.created_at) == year,
                Employee.position.in_(PAYROLL_TARGET_POSITIONS),
            )
        )
    ).all()
    forfeit_transactions = (
        await session.scalars(
            select(AccumulationFundTransaction).join(
                Employee, Employee.id == AccumulationFundTransaction.employee_id
            ).where(
                AccumulationFundTransaction.transaction_type == "forfeit",
                extract("year", AccumulationFundTransaction.created_at) == year,
                Employee.position.in_(PAYROLL_TARGET_POSITIONS),
            )
        )
    ).all()
    return {
        "year": year,
        "total_outstanding": decimal_string(
            sum((fund_outstanding(account) for account in active_accounts), Decimal("0"))
        ),
        "total_paid_out_ytd": decimal_string(
            sum((decimal(transaction.amount) for transaction in payout_transactions), Decimal("0"))
        ),
        "total_forfeited_ytd": decimal_string(
            sum((decimal(transaction.amount) for transaction in forfeit_transactions), Decimal("0"))
        ),
        "active_employees_count": len(
            {account.employee_id for account in active_accounts if fund_outstanding(account) > 0}
        ),
        "next_payout_date": date(year + 1, 1, 15).isoformat(),
    }


@router.get("", dependencies=FUND_READ_ACCESS)
async def list_fund_accounts(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
    year: Annotated[int | None, Query()] = None,
) -> list[dict[str, Any]]:
    year = year or date.today().year
    settings = await load_payroll_settings(session)
    today = date.today()
    result = await session.execute(
        select(AccumulationFundAccount, Employee)
        .join(Employee, Employee.id == AccumulationFundAccount.employee_id)
        .where(
            AccumulationFundAccount.year == year,
            Employee.position.in_(PAYROLL_TARGET_POSITIONS),
        )
        .order_by(Employee.full_name)
    )
    return [
        account_payload(account, employee, settings=settings, today=today)
        for account, employee in result.all()
    ]


@router.post("/payout/{year}", dependencies=FUND_EDIT_ACCESS)
async def post_fund_payout(
    year: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    payload: Annotated[FundPayoutRequest | None, Body()] = None,
) -> dict[str, Any]:
    payload = payload or FundPayoutRequest()
    result = await payout_fund_accounts_for_year(
        session,
        year,
        now=datetime.now(UTC),
        comment=payload.comment or f"Ручная выплата фонда за {year} год",
    )
    await session.commit()
    return {
        "year": result.year,
        "paid_out_count": result.paid_out_count,
        "total_paid_out": decimal_string(result.total_paid_out),
    }


@router.get("/{employee_id}", dependencies=FUND_READ_ACCESS)
async def get_employee_fund(
    employee_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
    year: Annotated[int | None, Query()] = None,
) -> dict[str, Any]:
    employee = await session.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")
    if employee.position not in PAYROLL_TARGET_POSITIONS:
        raise HTTPException(status_code=422, detail=FUND_TARGET_POSITIONS_ERROR)

    settings = await load_payroll_settings(session)
    today = date.today()
    account_query = select(AccumulationFundAccount).where(
        AccumulationFundAccount.employee_id == employee_id
    )
    if year is not None:
        account_query = account_query.where(AccumulationFundAccount.year == year)
    accounts = (
        await session.scalars(account_query.order_by(AccumulationFundAccount.year.desc()))
    ).all()
    account_ids = [account.id for account in accounts]
    transactions: list[AccumulationFundTransaction] = []
    if account_ids:
        transaction_query = select(AccumulationFundTransaction).where(
            AccumulationFundTransaction.account_id.in_(account_ids)
        )
        if year is not None:
            transaction_query = transaction_query.where(AccumulationFundTransaction.year == year)
        transactions = (
            await session.scalars(
                transaction_query.order_by(AccumulationFundTransaction.created_at.desc())
            )
        ).all()

    account_by_id = {account.id: account for account in accounts}
    accounts_payload = [
        account_payload(account, employee, settings=settings, today=today) for account in accounts
    ]
    return {
        "employee": employee_payload(employee, settings=settings, today=today),
        "account": accounts_payload[0] if year is not None and accounts_payload else None,
        "accounts": accounts_payload,
        "transactions": [
            transaction_payload(transaction, account_by_id.get(transaction.account_id))
            for transaction in transactions
        ],
    }


def account_payload(
    account: AccumulationFundAccount,
    employee: Employee,
    *,
    settings: dict[str, Any],
    today: date,
) -> dict[str, Any]:
    months = tenure_months_on(employee, today)
    current_rate = _fund_rate_for_months(settings, months)
    outstanding = fund_outstanding(account)
    return {
        "id": str(account.id),
        "employee_id": str(employee.id),
        "full_name": employee.full_name,
        "position": employee.position,
        "year": account.year,
        "status": account.status,
        "tenure_months": months,
        "tenure_started_at": (
            employee.tenure_started_at.isoformat()
            if employee.tenure_started_at
            else employee.hire_date.isoformat()
            if employee.hire_date
            else None
        ),
        "current_rate_percent": percent_string(current_rate * Decimal("100"), places=3),
        "accumulated": decimal_string(account.accumulated_amount),
        "paid_out": decimal_string(account.paid_out_amount),
        "forfeited": decimal_string(getattr(account, "forfeited_amount", 0)),
        "outstanding": decimal_string(outstanding),
        "paid_out_at": account.paid_out_at.isoformat() if account.paid_out_at else None,
        "forfeited_at": account.forfeited_at.isoformat() if account.forfeited_at else None,
        "forfeit_reason": account.forfeit_reason,
        "planned_payout_date": date(account.year + 1, 1, 15).isoformat(),
    }


def employee_payload(
    employee: Employee,
    *,
    settings: dict[str, Any],
    today: date,
) -> dict[str, Any]:
    months = tenure_months_on(employee, today)
    current_rate = _fund_rate_for_months(settings, months)
    next_threshold = next_fund_threshold(settings, months)
    tenure_start = employee.tenure_started_at or employee.hire_date
    return {
        "id": str(employee.id),
        "full_name": employee.full_name,
        "position": employee.position,
        "status": employee.status,
        "hire_date": employee.hire_date.isoformat() if employee.hire_date else None,
        "tenure_started_at": tenure_start.isoformat() if tenure_start else None,
        "tenure_months": months,
        "current_rate_percent": percent_string(current_rate * Decimal("100"), places=3),
        "next_threshold_months": next_threshold["months"] if next_threshold else None,
        "next_threshold_date": (
            add_calendar_months(tenure_start, next_threshold["months"]).isoformat()
            if tenure_start and next_threshold
            else None
        ),
        "next_rate_percent": (
            percent_string(next_threshold["rate"] * Decimal("100"), places=3)
            if next_threshold
            else None
        ),
        "fund_exclusion": _fund_exclusion_payload(employee, today),
    }


def transaction_payload(
    transaction: AccumulationFundTransaction,
    account: AccumulationFundAccount | None,
) -> dict[str, Any]:
    return {
        "id": str(transaction.id),
        "account_id": str(transaction.account_id),
        "employee_id": str(transaction.employee_id),
        "year": transaction.year,
        "transaction_type": transaction.transaction_type,
        "amount": decimal_string(transaction.amount),
        "rate_percent": (
            percent_string(transaction.rate_percent, places=5)
            if transaction.rate_percent is not None
            else None
        ),
        "base_pay_amount": (
            decimal_string(transaction.base_pay_amount)
            if transaction.base_pay_amount is not None
            else None
        ),
        "run_id": str(transaction.run_id) if transaction.run_id else None,
        "comment": transaction.comment,
        "created_at": transaction.created_at.isoformat() if transaction.created_at else None,
        "account_status": account.status if account else None,
    }


def next_fund_threshold(settings: dict[str, Any], months: int) -> dict[str, Any] | None:
    thresholds = sorted(
        (
            {"months": threshold_months(tier), "rate": decimal(tier.get("rate", 0))}
            for tier in settings.get("payroll.fund_rates_by_tenure", [])
            if isinstance(tier, dict) and threshold_months(tier) is not None
        ),
        key=lambda item: item["months"],
    )
    return next((item for item in thresholds if item["months"] > months), None)


def threshold_months(tier: dict[str, Any]) -> int | None:
    if "min_months" in tier:
        return int(decimal(tier["min_months"]))
    if "min_years" in tier:
        return int(decimal(tier["min_years"]) * Decimal("12"))
    return None


def add_calendar_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def percent_string(value: Any, *, places: int) -> str:
    quant = Decimal("1").scaleb(-places)
    return str(decimal(value).quantize(quant))


async def _get_fund_tiers_setting(session: AsyncSession) -> AppSetting | None:
    return await session.scalar(select(AppSetting).where(AppSetting.key == FUND_TIERS_SETTING_KEY))


def _normalize_fund_tiers(value: Any) -> list[FundTierItem]:
    if not isinstance(value, list):
        return []
    tiers: list[FundTierItem] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        min_months = _tier_min_months(item)
        if min_months is None:
            continue
        try:
            tiers.append(FundTierItem(min_months=min_months, rate=decimal(item.get("rate", 0))))
        except Exception:
            continue
    return sorted(tiers, key=lambda tier: tier.min_months)


def _tier_min_months(tier: Mapping[str, Any]) -> int | None:
    if "min_months" in tier:
        return int(decimal(tier["min_months"]))
    if "min_years" in tier:
        return round(decimal(tier["min_years"]) * Decimal("12"))
    return None


def _validate_and_sort_fund_tiers(tiers: list[FundTierItem]) -> list[FundTierItem]:
    if not 1 <= len(tiers) <= 10:
        raise HTTPException(
            status_code=422,
            detail="Нужно указать от 1 до 10 порогов фонда",
        )
    sorted_tiers = sorted(tiers, key=lambda tier: tier.min_months)
    seen_months: set[int] = set()
    previous_rate: Decimal | None = None
    for tier in sorted_tiers:
        if tier.min_months in seen_months:
            raise HTTPException(
                status_code=422,
                detail="Пороги стажа не должны повторяться",
            )
        seen_months.add(tier.min_months)
        if previous_rate is not None and tier.rate < previous_rate:
            raise HTTPException(
                status_code=422,
                detail="Ставка фонда не может уменьшаться с ростом стажа",
            )
        previous_rate = tier.rate
    return sorted_tiers


def _tiers_to_json(tiers: list[FundTierItem]) -> list[dict[str, int | float]]:
    return [
        {"min_months": tier.min_months, "rate": float(tier.rate)}
        for tier in sorted(tiers, key=lambda item: item.min_months)
    ]


def _fund_roster_row(
    employee: Employee,
    account: AccumulationFundAccount | None,
    *,
    initial_by_account: dict[uuid.UUID, AccumulationFundTransaction],
    settings: dict[str, Any],
    today: date,
    year: int,
) -> FundRosterRow:
    tenure_months = None
    current_rate_percent = None
    if employee.hire_date is not None:
        tenure_months = tenure_months_on(employee, today)
        current_rate_percent = percent_string(
            _fund_rate_for_months(settings, tenure_months) * Decimal("100"),
            places=3,
        )
    fund_account = None
    if account is not None:
        initial_transaction = initial_by_account.get(account.id)
        fund_account = FundRosterAccount(
            year=account.year,
            accumulated=decimal_string(account.accumulated_amount),
            is_initial_set=initial_transaction is not None,
            initial_set_at=(
                initial_transaction.created_at if initial_transaction is not None else None
            ),
            initial_set_by_label=None,
        )
    return FundRosterRow(
        employee_id=employee.id,
        full_name=employee.full_name,
        position=employee.position,
        hire_date=employee.hire_date,
        tenure_months=tenure_months,
        current_rate_percent=current_rate_percent,
        fund_exclusion=_fund_roster_exclusion(employee, today),
        fund_account=fund_account,
    )


async def _get_employee_or_404(session: AsyncSession, employee_id: uuid.UUID) -> Employee:
    employee = await session.scalar(
        select(Employee).where(Employee.id == employee_id).with_for_update()
    )
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Сотрудник не найден")
    return employee


def _fund_exclusion_payload(employee: Employee, today: date) -> FundExclusionRead:
    return FundExclusionRead(
        employee_id=employee.id,
        fund_excluded=bool(getattr(employee, "fund_excluded", False)),
        fund_excluded_until=getattr(employee, "fund_excluded_until", None),
        fund_excluded_reason=getattr(employee, "fund_excluded_reason", None),
        is_currently_excluded=is_fund_currently_excluded(employee, today),
    )


def _fund_roster_exclusion(employee: Employee, today: date) -> FundRosterExclusion:
    return FundRosterExclusion(
        fund_excluded=bool(getattr(employee, "fund_excluded", False)),
        fund_excluded_until=getattr(employee, "fund_excluded_until", None),
        fund_excluded_reason=getattr(employee, "fund_excluded_reason", None),
        is_currently_excluded=is_fund_currently_excluded(employee, today),
    )


def _fund_exclusion_snapshot(employee: Employee) -> dict[str, Any]:
    return {
        "employee_id": str(employee.id),
        "fund_excluded": bool(getattr(employee, "fund_excluded", False)),
        "fund_excluded_until": date_string(getattr(employee, "fund_excluded_until", None)),
        "fund_excluded_reason": getattr(employee, "fund_excluded_reason", None),
    }


def _account_snapshot(account: AccumulationFundAccount | None) -> dict[str, Any] | None:
    if account is None:
        return None
    return {
        "id": str(account.id),
        "employee_id": str(account.employee_id),
        "year": account.year,
        "accumulated": decimal_string(account.accumulated_amount),
        "paid_out": decimal_string(account.paid_out_amount),
        "forfeited": decimal_string(getattr(account, "forfeited_amount", 0)),
        "status": account.status,
    }


async def _add_fund_exclusion_action(
    session: AsyncSession,
    *,
    action_type: str,
    employee_id: uuid.UUID,
    before: dict[str, Any],
    after: dict[str, Any],
    actor: CurrentActor,
    now: datetime,
) -> None:
    await _add_fund_action(
        session,
        action_type=action_type,
        target_table="employee",
        target_id=employee_id,
        before=before,
        after=after,
        actor=actor,
        now=now,
        params={"employee_id": str(employee_id)},
    )


async def _add_fund_action(
    session: AsyncSession,
    *,
    action_type: str,
    target_table: str,
    target_id: uuid.UUID,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    actor: CurrentActor,
    now: datetime,
    params: dict[str, Any] | None = None,
) -> None:
    label = _actor_label(actor)
    agent_run = AgentRun(
        id=uuid.uuid4(),
        agent_name="payroll_fund_admin",
        finished_at=now,
        status="success",
        params={
            "actor_roles": sorted(actor.roles),
            "actor_label": label,
            **(params or {}),
        },
        result={"action_type": action_type},
    )
    session.add(agent_run)
    await session.flush()
    session.add(
        AgentAction(
            id=uuid.uuid4(),
            agent_run_id=agent_run.id,
            action_type=action_type,
            target_table=target_table,
            target_id=target_id,
            before_value=before,
            after_value=after,
        )
    )


def _actor_label(actor: CurrentActor) -> str:
    return ", ".join(sorted(actor.roles)) or "Система"


def date_string(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None

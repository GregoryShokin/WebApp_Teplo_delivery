from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, get_current_actor, require_finance_manager_plus
from app.db.session import get_session
from app.models import Employee, PayrollAdjustment, PayrollAdjustmentCategory
from app.schemas.payroll_adjustments import (
    PayrollAdjustmentCategoryCreate,
    PayrollAdjustmentCategoryPatch,
    PayrollAdjustmentCategoryRead,
    PayrollAdjustmentCreate,
    PayrollAdjustmentPatch,
    PayrollAdjustmentRead,
)
from app.services.payroll_adjustment_service import (
    PayrollAdjustmentLockedError,
    assert_date_not_locked,
    is_date_locked,
    load_locked_dates_for_period,
)

router = APIRouter()

ADJUSTMENT_TYPES = {"bonus", "penalty"}
ADJUSTMENT_EMPLOYEE_POSITIONS = {"Повар", "Кассир"}
UNPROCESSABLE_STATUS = 422


@router.get("/adjustments", response_model=list[PayrollAdjustmentRead])
async def list_adjustments(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
    employee_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    type_filter: Annotated[str, Query(alias="type")] = "all",
) -> list[dict[str, Any]]:
    if type_filter not in ADJUSTMENT_TYPES | {"all"}:
        raise HTTPException(status_code=UNPROCESSABLE_STATUS, detail="Некорректный тип")

    query = (
        select(PayrollAdjustment, Employee, PayrollAdjustmentCategory)
        .join(Employee, Employee.id == PayrollAdjustment.employee_id)
        .outerjoin(
            PayrollAdjustmentCategory,
            PayrollAdjustmentCategory.id == PayrollAdjustment.category_id,
        )
    )
    if employee_id is not None:
        query = query.where(PayrollAdjustment.employee_id == employee_id)
    if date_from is not None:
        query = query.where(PayrollAdjustment.work_date >= date_from)
    if date_to is not None:
        query = query.where(PayrollAdjustment.work_date <= date_to)
    if type_filter != "all":
        query = query.where(PayrollAdjustment.type == type_filter)
    query = query.order_by(PayrollAdjustment.work_date.desc(), PayrollAdjustment.created_at.desc())

    rows = (await session.execute(query)).all()
    if not rows:
        return []
    start = min(adjustment.work_date for adjustment, _employee, _category in rows)
    end = max(adjustment.work_date for adjustment, _employee, _category in rows)
    locked_dates = await load_locked_dates_for_period(
        session,
        period_start=start,
        period_end=end,
    )
    return [
        adjustment_payload(
            adjustment,
            employee,
            category,
            is_locked=adjustment.work_date in locked_dates,
        )
        for adjustment, employee, category in rows
    ]


@router.post("/adjustments", response_model=PayrollAdjustmentRead)
async def create_adjustment(
    payload: PayrollAdjustmentCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    require_finance_manager_plus(actor)
    employee = await get_adjustment_employee(session, payload.employee_id)
    validate_adjustment_type(payload.type)
    validate_adjustment_target(employee)
    validate_adjustment_date(payload.work_date)
    validate_category_xor(payload.category_id, payload.custom_label)
    category = await get_category_for_payload(session, payload.category_id, payload.type)
    await ensure_date_unlocked(session, payload.work_date)

    now = datetime.now(UTC)
    adjustment = PayrollAdjustment(
        employee_id=payload.employee_id,
        work_date=payload.work_date,
        type=payload.type,
        category_id=payload.category_id,
        custom_label=clean_optional_text(payload.custom_label),
        amount=payload.amount,
        comment=clean_optional_text(payload.comment),
        created_by_label=actor_label(actor),
        created_at=now,
        updated_at=now,
    )
    session.add(adjustment)
    await session.commit()
    await session.refresh(adjustment)
    return adjustment_payload(
        adjustment,
        employee,
        category,
        is_locked=False,
    )


@router.patch("/adjustments/{adjustment_id}", response_model=PayrollAdjustmentRead)
async def patch_adjustment(
    adjustment_id: uuid.UUID,
    payload: PayrollAdjustmentPatch,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    require_finance_manager_plus(actor)
    adjustment = await get_adjustment_or_404(session, adjustment_id)
    await ensure_date_unlocked(session, adjustment.work_date)

    updates = payload.model_dump(exclude_unset=True)
    next_employee_id = updates.get("employee_id", adjustment.employee_id)
    next_work_date = updates.get("work_date", adjustment.work_date)
    next_type = updates.get("type", adjustment.type)
    validate_adjustment_type(next_type)
    validate_adjustment_date(next_work_date)
    employee = await get_adjustment_employee(session, next_employee_id)
    validate_adjustment_target(employee)
    await ensure_date_unlocked(session, next_work_date)

    if "category_id" in updates and "custom_label" in updates:
        raise category_xor_error()
    if "category_id" in updates:
        next_category_id = updates["category_id"]
        next_custom_label = None
    elif "custom_label" in updates:
        next_category_id = None
        next_custom_label = clean_optional_text(updates["custom_label"])
    else:
        next_category_id = adjustment.category_id
        next_custom_label = adjustment.custom_label
    validate_category_xor(next_category_id, next_custom_label)
    category = await get_category_for_payload(session, next_category_id, next_type)

    adjustment.employee_id = next_employee_id
    adjustment.work_date = next_work_date
    adjustment.type = next_type
    adjustment.category_id = next_category_id
    adjustment.custom_label = next_custom_label
    if "amount" in updates:
        adjustment.amount = updates["amount"]
    if "comment" in updates:
        adjustment.comment = clean_optional_text(updates["comment"])
    adjustment.updated_at = datetime.now(UTC)

    await session.commit()
    await session.refresh(adjustment)
    return adjustment_payload(
        adjustment,
        employee,
        category,
        is_locked=await is_date_locked(session, adjustment.work_date),
    )


@router.delete("/adjustments/{adjustment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_adjustment(
    adjustment_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> Response:
    require_finance_manager_plus(actor)
    adjustment = await get_adjustment_or_404(session, adjustment_id)
    await ensure_date_unlocked(session, adjustment.work_date)
    await session.delete(adjustment)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/adjustment-categories", response_model=list[PayrollAdjustmentCategoryRead])
async def list_adjustment_categories(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
    type_filter: Annotated[str | None, Query(alias="type")] = None,
    include_inactive: bool = False,
) -> list[dict[str, Any]]:
    if type_filter is not None:
        validate_adjustment_type(type_filter)
    query = select(PayrollAdjustmentCategory)
    if type_filter is not None:
        query = query.where(PayrollAdjustmentCategory.type == type_filter)
    if not include_inactive:
        query = query.where(PayrollAdjustmentCategory.is_active.is_(True))
    query = query.order_by(
        PayrollAdjustmentCategory.type,
        PayrollAdjustmentCategory.sort_order,
        PayrollAdjustmentCategory.display_name,
    )
    result = await session.scalars(query)
    return [category_payload(category) for category in result.all()]


@router.post("/adjustment-categories", response_model=PayrollAdjustmentCategoryRead)
async def create_adjustment_category(
    payload: PayrollAdjustmentCategoryCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    require_finance_manager_plus(actor)
    validate_adjustment_type(payload.type)
    now = datetime.now(UTC)
    category = PayrollAdjustmentCategory(
        type=payload.type,
        code=payload.code or f"custom_{uuid.uuid4().hex[:12]}",
        display_name=payload.display_name.strip(),
        default_amount=payload.default_amount,
        description=clean_optional_text(payload.description),
        sort_order=payload.sort_order,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    session.add(category)
    await session.commit()
    await session.refresh(category)
    return category_payload(category)


@router.patch("/adjustment-categories/{category_id}", response_model=PayrollAdjustmentCategoryRead)
async def patch_adjustment_category(
    category_id: uuid.UUID,
    payload: PayrollAdjustmentCategoryPatch,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    require_finance_manager_plus(actor)
    category = await session.get(PayrollAdjustmentCategory, category_id)
    if category is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Категория не найдена")
    for field, value in payload.model_dump(exclude_unset=True).items():
        if field in {"display_name", "description"}:
            value = clean_optional_text(value)
        setattr(category, field, value)
    category.updated_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(category)
    return category_payload(category)


async def get_adjustment_or_404(
    session: AsyncSession,
    adjustment_id: uuid.UUID,
) -> PayrollAdjustment:
    adjustment = await session.get(PayrollAdjustment, adjustment_id)
    if adjustment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Корректировка не найдена")
    return adjustment


async def get_adjustment_employee(session: AsyncSession, employee_id: uuid.UUID) -> Employee:
    employee = await session.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Сотрудник не найден")
    return employee


async def get_category_for_payload(
    session: AsyncSession,
    category_id: uuid.UUID | None,
    adjustment_type: str,
) -> PayrollAdjustmentCategory | None:
    if category_id is None:
        return None
    category = await session.get(PayrollAdjustmentCategory, category_id)
    if category is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Категория не найдена")
    if category.type != adjustment_type:
        raise HTTPException(
            status_code=UNPROCESSABLE_STATUS,
            detail="Тип категории не совпадает с типом корректировки",
        )
    return category


async def ensure_date_unlocked(session: AsyncSession, work_date: date) -> None:
    try:
        await assert_date_not_locked(session, work_date)
    except PayrollAdjustmentLockedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


def validate_adjustment_type(value: str) -> None:
    if value not in ADJUSTMENT_TYPES:
        raise HTTPException(status_code=UNPROCESSABLE_STATUS, detail="Некорректный тип")


def validate_adjustment_target(employee: Employee) -> None:
    if employee.position not in ADJUSTMENT_EMPLOYEE_POSITIONS:
        raise HTTPException(
            status_code=UNPROCESSABLE_STATUS,
            detail="Премии и штрафы доступны только для поваров и кассиров",
        )


def validate_adjustment_date(work_date: date) -> None:
    if work_date > date.today() + timedelta(days=30):
        raise HTTPException(
            status_code=UNPROCESSABLE_STATUS,
            detail="Дата корректировки слишком далеко в будущем",
        )


def validate_category_xor(category_id: uuid.UUID | None, custom_label: str | None) -> None:
    if (category_id is None) == (not clean_optional_text(custom_label)):
        raise category_xor_error()


def category_xor_error() -> HTTPException:
    return HTTPException(
        status_code=UNPROCESSABLE_STATUS,
        detail="Укажите категорию или своё название, но не оба поля",
    )


def adjustment_payload(
    adjustment: PayrollAdjustment,
    employee: Employee,
    category: PayrollAdjustmentCategory | None,
    *,
    is_locked: bool,
) -> dict[str, Any]:
    return {
        "id": adjustment.id,
        "employee_id": adjustment.employee_id,
        "employee_full_name": employee.full_name,
        "employee_position": employee.position,
        "work_date": adjustment.work_date,
        "type": adjustment.type,
        "category_id": adjustment.category_id,
        "category_display_name": category.display_name if category is not None else None,
        "custom_label": adjustment.custom_label,
        "amount": decimal_string(adjustment.amount),
        "comment": adjustment.comment,
        "created_by_user_id": adjustment.created_by_user_id,
        "created_by_label": adjustment.created_by_label,
        "created_at": adjustment.created_at,
        "updated_at": adjustment.updated_at,
        "is_locked": is_locked,
    }


def category_payload(category: PayrollAdjustmentCategory) -> dict[str, Any]:
    return {
        "id": category.id,
        "type": category.type,
        "code": category.code,
        "display_name": category.display_name,
        "description": category.description,
        "default_amount": decimal_string(category.default_amount),
        "is_active": category.is_active,
        "sort_order": category.sort_order,
        "created_at": category.created_at,
        "updated_at": category.updated_at,
    }


def decimal_string(value: Any) -> str | None:
    if value is None:
        return None
    return f"{Decimal(str(value)).quantize(Decimal('0.01'))}"


def clean_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def actor_label(actor: CurrentActor) -> str | None:
    return ", ".join(sorted(actor.roles)) or None

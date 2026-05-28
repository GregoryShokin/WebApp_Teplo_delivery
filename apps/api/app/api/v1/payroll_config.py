from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, get_current_actor
from app.db.session import get_session
from app.schemas.payroll_config import (
    PayrollCategoryCoefficientBase,
    PayrollCategoryCoefficientRead,
    PayrollDeductionCategoryBase,
    PayrollDeductionCategoryRead,
    PayrollRateBase,
    PayrollRateCellRead,
    PayrollRateRead,
    PayrollRevenueShareBase,
    PayrollRevenueShareRead,
    PayrollRevenueTierBase,
    PayrollRevenueTierRead,
    PayrollRoleCategoryAvailabilityRead,
    PayrollRoleCategoryAvailabilityToggle,
    PayrollSeniorityPremiumBase,
    PayrollSeniorityPremiumRead,
)
from app.services.payroll_config import (
    PayrollConfigConflictError,
    PayrollConfigValidationError,
    create_deduction_category_version,
    create_rate_version,
    create_revenue_share_version,
    create_seniority_premium_version,
    list_category_coefficients,
    list_deduction_categories,
    list_rate_matrix,
    list_rates,
    list_revenue_shares,
    list_revenue_tiers,
    list_role_category_availability,
    list_seniority_premiums,
    replace_category_coefficient_versions,
    replace_revenue_tier_versions,
    set_role_category_availability,
)

router = APIRouter()

PAYROLL_CONFIG_WRITERS = frozenset({"finance_manager", "owner", "admin"})


def require_payroll_config_writer(actor: CurrentActor) -> None:
    if actor.roles & PAYROLL_CONFIG_WRITERS:
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")


@router.get("/rates", response_model=list[PayrollRateCellRead])
async def get_rates(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
    history: Annotated[bool, Query()] = False,
    include_disabled: Annotated[bool, Query()] = False,
) -> list:
    if history:
        return await list_rates(session, history=True)
    return await list_rate_matrix(session, include_disabled=include_disabled)


@router.post("/rates", response_model=PayrollRateRead)
@router.put("/rates", response_model=PayrollRateRead)
async def put_rate(
    payload: PayrollRateBase,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
):
    require_payroll_config_writer(actor)
    try:
        return await create_rate_version(session, payload)
    except PayrollConfigConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except PayrollConfigValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/availability", response_model=list[PayrollRoleCategoryAvailabilityRead])
async def get_availability(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> list:
    return await list_role_category_availability(session)


@router.put(
    "/availability/{position_group}/{category}",
    response_model=PayrollRoleCategoryAvailabilityRead,
)
async def put_availability(
    position_group: str,
    category: str,
    payload: PayrollRoleCategoryAvailabilityToggle,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
):
    require_payroll_config_writer(actor)
    try:
        return await set_role_category_availability(
            session,
            position_group=position_group,
            category=category,
            is_enabled=payload.is_enabled,
        )
    except PayrollConfigConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except PayrollConfigValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/revenue-share", response_model=list[PayrollRevenueShareRead])
async def get_revenue_share(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
    history: Annotated[bool, Query()] = False,
) -> list:
    return await list_revenue_shares(session, history=history)


@router.put("/revenue-share", response_model=PayrollRevenueShareRead)
async def put_revenue_share(
    payload: PayrollRevenueShareBase,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
):
    require_payroll_config_writer(actor)
    try:
        return await create_revenue_share_version(session, payload)
    except PayrollConfigConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except PayrollConfigValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.get("/revenue-tiers", response_model=list[PayrollRevenueTierRead])
async def get_revenue_tiers(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
    history: Annotated[bool, Query()] = False,
) -> list:
    return await list_revenue_tiers(session, history=history)


@router.put("/revenue-tiers", response_model=list[PayrollRevenueTierRead])
async def put_revenue_tiers(
    payload: list[PayrollRevenueTierBase],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
):
    require_payroll_config_writer(actor)
    try:
        return await replace_revenue_tier_versions(session, payload)
    except PayrollConfigConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except PayrollConfigValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.get("/category-coefficients", response_model=list[PayrollCategoryCoefficientRead])
async def get_category_coefficients(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
    history: Annotated[bool, Query()] = False,
) -> list:
    return await list_category_coefficients(session, history=history)


@router.put("/category-coefficients", response_model=list[PayrollCategoryCoefficientRead])
async def put_category_coefficients(
    payload: list[PayrollCategoryCoefficientBase],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
):
    require_payroll_config_writer(actor)
    try:
        return await replace_category_coefficient_versions(session, payload)
    except PayrollConfigConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except PayrollConfigValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.get("/deductions", response_model=list[PayrollDeductionCategoryRead])
async def get_deductions(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
    history: Annotated[bool, Query()] = False,
) -> list:
    return await list_deduction_categories(session, history=history)


@router.put("/deductions", response_model=PayrollDeductionCategoryRead)
async def put_deduction(
    payload: PayrollDeductionCategoryBase,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
):
    require_payroll_config_writer(actor)
    try:
        return await create_deduction_category_version(session, payload)
    except PayrollConfigConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except PayrollConfigValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.get("/seniority-premium", response_model=list[PayrollSeniorityPremiumRead])
async def get_seniority_premium(
    session: Annotated[AsyncSession, Depends(get_session)],
    _actor: Annotated[CurrentActor, Depends(get_current_actor)],
    history: Annotated[bool, Query()] = False,
) -> list:
    return await list_seniority_premiums(session, history=history)


@router.put("/seniority-premium", response_model=PayrollSeniorityPremiumRead)
async def put_seniority_premium(
    payload: PayrollSeniorityPremiumBase,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
):
    require_payroll_config_writer(actor)
    try:
        return await create_seniority_premium_version(session, payload)
    except PayrollConfigConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except PayrollConfigValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

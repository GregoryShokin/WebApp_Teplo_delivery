from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    PayrollDeductionCategory,
    PayrollRate,
    PayrollRevenueShare,
    PayrollSeniorityPremium,
)
from app.schemas.payroll_config import (
    PayrollDeductionCategoryBase,
    PayrollRateBase,
    PayrollRevenueShareBase,
    PayrollSeniorityPremiumBase,
)


class PayrollConfigConflictError(RuntimeError):
    pass


class PayrollConfigValidationError(ValueError):
    pass


VALID_PAYROLL_RATE_CATEGORIES = frozenset(
    {"category_1", "category_2", "category_3", "intern", "freelancer"}
)


async def list_rates(session: AsyncSession, *, history: bool = False) -> list[PayrollRate]:
    statement = select(PayrollRate)
    if not history:
        statement = statement.where(_current_filter(PayrollRate, date.today()))
    statement = statement.order_by(
        PayrollRate.position_group,
        PayrollRate.category,
        PayrollRate.station,
        PayrollRate.rate_type,
        PayrollRate.effective_from.desc(),
    )
    return list((await session.scalars(statement)).all())


async def create_rate_version(
    session: AsyncSession,
    payload: PayrollRateBase,
) -> PayrollRate:
    _validate_rate_payload(payload)
    _validate_effective_range(payload.effective_from, payload.effective_to)
    natural_filters = [
        PayrollRate.position_group == payload.position_group,
        PayrollRate.category == payload.category,
        PayrollRate.station.is_(None)
        if payload.station is None
        else PayrollRate.station == payload.station,
        PayrollRate.rate_type == payload.rate_type,
    ]
    await _ensure_no_existing_version(session, PayrollRate, natural_filters, payload.effective_from)
    await _close_previous_versions(session, PayrollRate, natural_filters, payload.effective_from)
    record = PayrollRate(**payload.model_dump())
    return await _insert_version(session, record)


async def list_revenue_shares(
    session: AsyncSession,
    *,
    history: bool = False,
) -> list[PayrollRevenueShare]:
    statement = select(PayrollRevenueShare)
    if not history:
        statement = statement.where(_current_filter(PayrollRevenueShare, date.today()))
    statement = statement.order_by(
        PayrollRevenueShare.position_group,
        PayrollRevenueShare.category,
        PayrollRevenueShare.effective_from.desc(),
    )
    return list((await session.scalars(statement)).all())


async def create_revenue_share_version(
    session: AsyncSession,
    payload: PayrollRevenueShareBase,
) -> PayrollRevenueShare:
    _validate_effective_range(payload.effective_from, payload.effective_to)
    natural_filters = [
        PayrollRevenueShare.position_group == payload.position_group,
        PayrollRevenueShare.category == payload.category,
    ]
    await _ensure_no_existing_version(
        session,
        PayrollRevenueShare,
        natural_filters,
        payload.effective_from,
    )
    await _close_previous_versions(
        session,
        PayrollRevenueShare,
        natural_filters,
        payload.effective_from,
    )
    record = PayrollRevenueShare(**payload.model_dump())
    return await _insert_version(session, record)


async def list_deduction_categories(
    session: AsyncSession,
    *,
    history: bool = False,
) -> list[PayrollDeductionCategory]:
    statement = select(PayrollDeductionCategory)
    if not history:
        statement = statement.where(_current_filter(PayrollDeductionCategory, date.today()))
    statement = statement.order_by(
        PayrollDeductionCategory.display_name,
        PayrollDeductionCategory.code,
        PayrollDeductionCategory.effective_from.desc(),
    )
    return list((await session.scalars(statement)).all())


async def create_deduction_category_version(
    session: AsyncSession,
    payload: PayrollDeductionCategoryBase,
) -> PayrollDeductionCategory:
    _validate_effective_range(payload.effective_from, payload.effective_to)
    natural_filters = [PayrollDeductionCategory.code == payload.code]
    await _ensure_no_existing_version(
        session,
        PayrollDeductionCategory,
        natural_filters,
        payload.effective_from,
    )
    await _close_previous_versions(
        session,
        PayrollDeductionCategory,
        natural_filters,
        payload.effective_from,
    )
    record = PayrollDeductionCategory(**payload.model_dump())
    return await _insert_version(session, record)


async def list_seniority_premiums(
    session: AsyncSession,
    *,
    history: bool = False,
) -> list[PayrollSeniorityPremium]:
    statement = select(PayrollSeniorityPremium)
    if not history:
        statement = statement.where(_current_filter(PayrollSeniorityPremium, date.today()))
    statement = statement.order_by(
        PayrollSeniorityPremium.role,
        PayrollSeniorityPremium.effective_from.desc(),
    )
    return list((await session.scalars(statement)).all())


async def create_seniority_premium_version(
    session: AsyncSession,
    payload: PayrollSeniorityPremiumBase,
) -> PayrollSeniorityPremium:
    _validate_effective_range(payload.effective_from, payload.effective_to)
    natural_filters = [PayrollSeniorityPremium.role == payload.role]
    await _ensure_no_existing_version(
        session,
        PayrollSeniorityPremium,
        natural_filters,
        payload.effective_from,
    )
    await _close_previous_versions(
        session,
        PayrollSeniorityPremium,
        natural_filters,
        payload.effective_from,
    )
    record = PayrollSeniorityPremium(**payload.model_dump())
    return await _insert_version(session, record)


async def _ensure_no_existing_version(
    session: AsyncSession,
    model: type[Any],
    natural_filters: Iterable[Any],
    effective_from: date,
) -> None:
    existing = await session.scalar(
        select(model).where(*natural_filters, model.effective_from == effective_from)
    )
    if existing is not None:
        raise PayrollConfigConflictError("Version already exists for this effective date")


async def _close_previous_versions(
    session: AsyncSession,
    model: type[Any],
    natural_filters: Iterable[Any],
    effective_from: date,
) -> None:
    statement = select(model).where(
        *natural_filters,
        model.effective_from < effective_from,
        or_(model.effective_to.is_(None), model.effective_to > effective_from),
    )
    for record in (await session.scalars(statement)).all():
        record.effective_to = effective_from


async def _insert_version(
    session: AsyncSession,
    record: Any,
) -> Any:
    session.add(record)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise PayrollConfigConflictError("Version already exists for this effective date") from exc
    await session.refresh(record)
    return record


def _current_filter(model: type[Any], as_of: date) -> Any:
    return and_(
        model.effective_from <= as_of,
        or_(model.effective_to.is_(None), model.effective_to > as_of),
    )


def _validate_rate_payload(payload: PayrollRateBase) -> None:
    if payload.category not in VALID_PAYROLL_RATE_CATEGORIES:
        raise PayrollConfigValidationError("Invalid payroll rate category")
    if payload.is_active and payload.amount is None:
        raise PayrollConfigValidationError("Active payroll rate requires amount")


def _validate_effective_range(effective_from: date, effective_to: date | None) -> None:
    if effective_to is not None and effective_to <= effective_from:
        raise PayrollConfigValidationError("effective_to must be later than effective_from")

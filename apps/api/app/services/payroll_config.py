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
    PayrollRoleCategoryAvailability,
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
PAYROLL_RATE_CATEGORY_ORDER = ("category_1", "category_2", "category_3", "intern", "freelancer")


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


async def list_rate_matrix(
    session: AsyncSession,
    *,
    include_disabled: bool = False,
) -> list[dict[str, Any]]:
    as_of = date.today()
    positions = await _list_rate_positions(session)
    availability = await _availability_by_key(session)

    current_rates = await session.scalars(
        select(PayrollRate)
        .where(PayrollRate.rate_type == "daily", _current_filter(PayrollRate, as_of))
        .order_by(PayrollRate.effective_from.desc(), PayrollRate.created_at.desc())
    )
    rates_by_key: dict[tuple[str, str], PayrollRate] = {}
    station_by_position: dict[str, str | None] = {}
    for rate in current_rates.all():
        key = (rate.position_group, rate.category)
        rates_by_key.setdefault(key, rate)
        if rate.station is not None or rate.position_group not in station_by_position:
            station_by_position[rate.position_group] = rate.station

    cells: list[dict[str, Any]] = []
    for position_group in positions:
        for category in PAYROLL_RATE_CATEGORY_ORDER:
            key = (position_group, category)
            is_enabled = availability.get(key, False)
            if not include_disabled and not is_enabled:
                continue
            rate = rates_by_key.get(key)
            cells.append(
                {
                    "id": rate.id if rate is not None else None,
                    "position_group": position_group,
                    "category": category,
                    "station": (
                        rate.station
                        if rate is not None
                        else station_by_position.get(position_group)
                    ),
                    "rate_type": rate.rate_type if rate is not None else "daily",
                    "amount": rate.amount if rate is not None else None,
                    "is_active": rate.is_active if rate is not None else True,
                    "is_enabled": is_enabled,
                    "effective_from": rate.effective_from if rate is not None else None,
                    "effective_to": rate.effective_to if rate is not None else None,
                    "created_at": rate.created_at if rate is not None else None,
                }
            )
    return cells


async def list_role_category_availability(session: AsyncSession) -> list[dict[str, Any]]:
    positions = await _list_rate_positions(session)
    availability = await _availability_by_key(session)
    return [
        {
            "position_group": position_group,
            "category": category,
            "is_enabled": availability.get((position_group, category), False),
        }
        for position_group in positions
        for category in PAYROLL_RATE_CATEGORY_ORDER
    ]


async def set_role_category_availability(
    session: AsyncSession,
    *,
    position_group: str,
    category: str,
    is_enabled: bool,
) -> PayrollRoleCategoryAvailability:
    _validate_rate_category(category)
    record = await session.get(PayrollRoleCategoryAvailability, (position_group, category))
    if record is None:
        record = PayrollRoleCategoryAvailability(
            position_group=position_group,
            category=category,
            is_enabled=is_enabled,
        )
        session.add(record)
    else:
        record.is_enabled = is_enabled

    if is_enabled:
        await _ensure_rate_shell(session, position_group=position_group, category=category)

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise PayrollConfigConflictError(
            "Availability already exists for this role/category"
        ) from exc
    await session.refresh(record)
    return record


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
    existing = await _find_existing_version(
        session,
        PayrollRate,
        natural_filters,
        payload.effective_from,
    )
    if existing is not None:
        return await _update_rate_version(session, existing, payload)
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
    existing = await _find_existing_version(session, model, natural_filters, effective_from)
    if existing is not None:
        raise PayrollConfigConflictError("Version already exists for this effective date")


async def _find_existing_version(
    session: AsyncSession,
    model: type[Any],
    natural_filters: Iterable[Any],
    effective_from: date,
) -> Any | None:
    return await session.scalar(
        select(model).where(*natural_filters, model.effective_from == effective_from)
    )


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


async def _update_rate_version(
    session: AsyncSession,
    record: PayrollRate,
    payload: PayrollRateBase,
) -> PayrollRate:
    record.station = payload.station
    record.rate_type = payload.rate_type
    record.amount = payload.amount
    record.is_active = payload.is_active
    record.effective_to = payload.effective_to
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise PayrollConfigConflictError("Version already exists for this effective date") from exc
    await session.refresh(record)
    return record


async def _list_rate_positions(session: AsyncSession) -> list[str]:
    rate_positions = await session.scalars(select(PayrollRate.position_group).distinct())
    availability_positions = await session.scalars(
        select(PayrollRoleCategoryAvailability.position_group).distinct()
    )
    return sorted(
        set(rate_positions.all()) | set(availability_positions.all()),
        key=lambda value: value.casefold(),
    )


async def _availability_by_key(session: AsyncSession) -> dict[tuple[str, str], bool]:
    records = await session.scalars(select(PayrollRoleCategoryAvailability))
    return {(record.position_group, record.category): record.is_enabled for record in records.all()}


async def _ensure_rate_shell(
    session: AsyncSession,
    *,
    position_group: str,
    category: str,
) -> None:
    existing = await session.scalar(
        select(PayrollRate).where(
            PayrollRate.position_group == position_group,
            PayrollRate.category == category,
            PayrollRate.rate_type == "daily",
            _current_filter(PayrollRate, date.today()),
        )
    )
    if existing is not None:
        return

    station = await _infer_station_for_position(session, position_group)
    session.add(
        PayrollRate(
            position_group=position_group,
            category=category,
            station=station,
            rate_type="daily",
            amount=None,
            is_active=True,
            effective_from=date.today(),
            effective_to=None,
        )
    )


async def _infer_station_for_position(session: AsyncSession, position_group: str) -> str | None:
    return await session.scalar(
        select(PayrollRate.station)
        .where(PayrollRate.position_group == position_group, PayrollRate.station.is_not(None))
        .order_by(PayrollRate.effective_from.desc())
        .limit(1)
    )


def _current_filter(model: type[Any], as_of: date) -> Any:
    return and_(
        model.effective_from <= as_of,
        or_(model.effective_to.is_(None), model.effective_to > as_of),
    )


def _validate_rate_payload(payload: PayrollRateBase) -> None:
    _validate_rate_category(payload.category)


def _validate_rate_category(category: str) -> None:
    if category not in VALID_PAYROLL_RATE_CATEGORIES:
        raise PayrollConfigValidationError("Invalid payroll rate category")


def _validate_effective_range(effective_from: date, effective_to: date | None) -> None:
    if effective_to is not None and effective_to <= effective_from:
        raise PayrollConfigValidationError("effective_to must be later than effective_from")

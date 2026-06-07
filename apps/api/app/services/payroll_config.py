from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AppSetting,
    AppSettingHistory,
    CategoryCoefficient,
    PayrollDeductionCategory,
    PayrollRate,
    PayrollRevenueShare,
    PayrollRoleCategoryAvailability,
    PayrollSeniorityPremium,
    RevenueTier,
)
from app.schemas.payroll_config import (
    PayrollCategoryCoefficientBase,
    PayrollDeductionCategoryBase,
    PayrollRateBase,
    PayrollRevenueShareBase,
    PayrollRevenueTierBase,
    PayrollSeniorityPremiumBase,
)
from app.services.staff_taxonomy import (
    CANONICAL_POSITIONS,
    PAYROLL_ROLE_LABELS,
    canonical_position_name,
    categories_for_payroll_role,
)


class PayrollConfigConflictError(RuntimeError):
    pass


class PayrollConfigValidationError(ValueError):
    pass


VALID_PAYROLL_RATE_CATEGORIES = frozenset(
    {"category_1", "category_2", "category_3", "category_4", "intern", "freelancer"}
)
PAYROLL_RATE_CATEGORY_ORDER = (
    "category_1",
    "category_2",
    "category_3",
    "category_4",
    "intern",
    "freelancer",
)
PAYROLL_RATE_CATEGORY_LABELS = {
    "category_1": "1-я",
    "category_2": "2-я",
    "category_3": "3-я",
    "category_4": "4-я",
    "intern": "Стажёр",
    "freelancer": "Внештатный",
}
VALID_SENIORITY_PREMIUM_POSITIONS = frozenset({"Повар", "Кассир"})
VALID_SENIORITY_PREMIUM_ROLES = frozenset({"senior", "deputy_senior"})
SUBSTITUTE_PAIRS_SETTING_KEY = "payroll.substitute_pairs"
SUBSTITUTE_TARGET_POSITIONS = frozenset({"Повар", "Кассир"})
DEFAULT_SUBSTITUTE_PAIRS = (
    {"from_position": "Управляющий", "to_position": "Повар", "add_to_schedule": False},
    {"from_position": "Менеджер", "to_position": "Кассир", "add_to_schedule": False},
)


class SubstitutePair(BaseModel):
    from_position: str
    to_position: str
    add_to_schedule: bool = False


async def get_substitute_pairs(session: AsyncSession) -> list[SubstitutePair]:
    setting = await session.scalar(
        select(AppSetting).where(AppSetting.key == SUBSTITUTE_PAIRS_SETTING_KEY)
    )
    raw_value = setting.value if setting is not None else list(DEFAULT_SUBSTITUTE_PAIRS)
    return _validate_substitute_pairs(raw_value)


async def set_substitute_pairs(
    session: AsyncSession,
    pairs: Iterable[SubstitutePair | dict[str, Any]],
    actor: Any,
) -> list[SubstitutePair]:
    normalized = _validate_substitute_pairs(list(pairs))
    value = [pair.model_dump() for pair in normalized]
    setting = await session.scalar(
        select(AppSetting).where(AppSetting.key == SUBSTITUTE_PAIRS_SETTING_KEY)
    )
    old_value = setting.value if setting is not None else None
    actor_id = getattr(actor, "user_id", None) or getattr(actor, "id", None)

    if setting is None:
        setting = AppSetting(
            key=SUBSTITUTE_PAIRS_SETTING_KEY,
            value=value,
            value_type="array",
            category="payroll",
            display_name="Подменные роли",
            description="Какие должности могут выходить на смены поваров и кассиров.",
            widget_type="json",
            widget_options=None,
            unit=None,
            updated_by_user_id=actor_id,
        )
        session.add(setting)
        await session.flush()
    else:
        setting.value = value
        setting.updated_at = datetime.now(UTC)
        setting.updated_by_user_id = actor_id

    session.add(
        AppSettingHistory(
            setting_id=setting.id,
            old_value=old_value,
            new_value=value,
            changed_by_user_id=actor_id,
        )
    )
    await session.commit()
    return normalized


def is_substitute_pair_allowed(
    pairs: Iterable[SubstitutePair],
    from_pos: str | None,
    to_pos: str | None,
) -> bool:
    canonical_from = canonical_position_name(from_pos)
    canonical_to = canonical_position_name(to_pos)
    if canonical_from is None or canonical_to is None:
        return False
    return any(
        pair.from_position == canonical_from and pair.to_position == canonical_to for pair in pairs
    )


def is_substitute_pair_added_to_schedule(
    pairs: Iterable[SubstitutePair],
    from_pos: str | None,
    to_pos: str | None,
) -> bool:
    canonical_from = canonical_position_name(from_pos)
    canonical_to = canonical_position_name(to_pos)
    if canonical_from is None or canonical_to is None:
        return False
    return any(
        pair.from_position == canonical_from
        and pair.to_position == canonical_to
        and pair.add_to_schedule
        for pair in pairs
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


async def list_enabled_role_categories(session: AsyncSession) -> dict[str, list[dict[str, str]]]:
    availability = await _availability_by_key(session)
    return {
        payroll_role: [
            {
                "code": category,
                "name": PAYROLL_RATE_CATEGORY_LABELS[category],
            }
            for category in categories_for_payroll_role(payroll_role)
            if availability.get((position_group, category), False)
        ]
        for payroll_role, position_group in PAYROLL_ROLE_LABELS.items()
    }


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


async def list_revenue_tiers(
    session: AsyncSession,
    *,
    history: bool = False,
) -> list[RevenueTier]:
    statement = select(RevenueTier)
    if not history:
        statement = statement.where(_current_filter(RevenueTier, date.today()))
    statement = statement.order_by(RevenueTier.min_revenue, RevenueTier.effective_from.desc())
    return list((await session.scalars(statement)).all())


async def replace_revenue_tier_versions(
    session: AsyncSession,
    payloads: Iterable[PayrollRevenueTierBase],
) -> list[RevenueTier]:
    payloads = list(payloads)
    _validate_revenue_tier_payloads(payloads)
    effective_from = payloads[0].effective_from
    await _ensure_no_existing_set_version(session, RevenueTier, effective_from)
    await _close_all_previous_versions(session, RevenueTier, effective_from)
    records = [RevenueTier(**payload.model_dump()) for payload in payloads]
    return await _insert_versions(session, records)


async def list_category_coefficients(
    session: AsyncSession,
    *,
    history: bool = False,
) -> list[CategoryCoefficient]:
    statement = select(CategoryCoefficient)
    if not history:
        statement = statement.where(_current_filter(CategoryCoefficient, date.today()))
    statement = statement.order_by(
        CategoryCoefficient.category,
        CategoryCoefficient.effective_from.desc(),
    )
    return list((await session.scalars(statement)).all())


async def replace_category_coefficient_versions(
    session: AsyncSession,
    payloads: Iterable[PayrollCategoryCoefficientBase],
) -> list[CategoryCoefficient]:
    payloads = list(payloads)
    _validate_category_coefficient_payloads(payloads)
    effective_from = payloads[0].effective_from
    await _ensure_no_existing_set_version(session, CategoryCoefficient, effective_from)
    await _close_all_previous_versions(session, CategoryCoefficient, effective_from)
    records = [CategoryCoefficient(**payload.model_dump()) for payload in payloads]
    return await _insert_versions(session, records)


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
        PayrollSeniorityPremium.position,
        PayrollSeniorityPremium.role,
        PayrollSeniorityPremium.effective_from.desc(),
    )
    return list((await session.scalars(statement)).all())


async def create_seniority_premium_version(
    session: AsyncSession,
    payload: PayrollSeniorityPremiumBase,
) -> PayrollSeniorityPremium:
    _validate_seniority_premium_payload(payload)
    _validate_effective_range(payload.effective_from, None)
    natural_filters = [
        PayrollSeniorityPremium.position == payload.position,
        PayrollSeniorityPremium.role == payload.role,
    ]
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
    record = PayrollSeniorityPremium(**payload.model_dump(), percent_of_base=None)
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


async def _ensure_no_existing_set_version(
    session: AsyncSession,
    model: type[Any],
    effective_from: date,
) -> None:
    existing = await session.scalar(select(model).where(model.effective_from == effective_from))
    if existing is not None:
        raise PayrollConfigConflictError("Version already exists for this effective date")


async def _close_all_previous_versions(
    session: AsyncSession,
    model: type[Any],
    effective_from: date,
) -> None:
    statement = select(model).where(
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


async def _insert_versions(
    session: AsyncSession,
    records: list[Any],
) -> list[Any]:
    session.add_all(records)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise PayrollConfigConflictError("Version already exists for this effective date") from exc
    for record in records:
        await session.refresh(record)
    return records


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
            is_active=False,
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
    _validate_active_rate_amount(payload.is_active, payload.amount)


def _validate_active_rate_amount(is_active: bool, amount: Any) -> None:
    normalized_amount = Decimal(str(amount)) if amount is not None else None
    if is_active is True and (normalized_amount is None or normalized_amount <= 0):
        raise PayrollConfigValidationError("Активная ставка требует сумму > 0")
    if normalized_amount is not None and normalized_amount < 0:
        raise PayrollConfigValidationError("Сумма ставки не может быть отрицательной")


def _validate_seniority_premium_payload(payload: PayrollSeniorityPremiumBase) -> None:
    if payload.position not in VALID_SENIORITY_PREMIUM_POSITIONS:
        raise PayrollConfigValidationError("Invalid seniority premium position")
    if payload.role not in VALID_SENIORITY_PREMIUM_ROLES:
        raise PayrollConfigValidationError("Invalid seniority premium role")


def _validate_rate_category(category: str) -> None:
    if category not in VALID_PAYROLL_RATE_CATEGORIES:
        raise PayrollConfigValidationError("Invalid payroll rate category")


def _validate_substitute_pairs(raw_pairs: Any) -> list[SubstitutePair]:
    if not isinstance(raw_pairs, list | tuple):
        raise PayrollConfigValidationError("Подменные пары должны быть списком")

    normalized: list[SubstitutePair] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_pairs:
        if isinstance(item, SubstitutePair):
            from_position = item.from_position
            to_position = item.to_position
            add_to_schedule = item.add_to_schedule
        elif isinstance(item, BaseModel):
            payload = item.model_dump()
            from_position = str(payload.get("from_position") or "").strip()
            to_position = str(payload.get("to_position") or "").strip()
            add_to_schedule = _validate_substitute_pair_bool(payload.get("add_to_schedule", False))
        elif isinstance(item, Mapping):
            from_position = str(item.get("from_position") or "").strip()
            to_position = str(item.get("to_position") or "").strip()
            add_to_schedule = _validate_substitute_pair_bool(item.get("add_to_schedule", False))
        else:
            raise PayrollConfigValidationError("Подменная пара должна быть объектом")

        canonical_from = canonical_position_name(from_position)
        canonical_to = canonical_position_name(to_position)
        if canonical_from not in CANONICAL_POSITIONS:
            raise PayrollConfigValidationError("Кто подменяет должен быть канонической должностью")
        if canonical_to not in SUBSTITUTE_TARGET_POSITIONS:
            raise PayrollConfigValidationError("Кого подменяет может быть только Повар или Кассир")
        if canonical_from == canonical_to:
            raise PayrollConfigValidationError("Должности в подменной паре не должны совпадать")

        key = (canonical_from, canonical_to)
        if key in seen:
            raise PayrollConfigValidationError("Подменные пары не должны повторяться")
        seen.add(key)
        normalized.append(
            SubstitutePair(
                from_position=canonical_from,
                to_position=canonical_to,
                add_to_schedule=add_to_schedule,
            )
        )
    return normalized


def _validate_substitute_pair_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    raise PayrollConfigValidationError("Флаг добавления в график должен быть true/false")


def _validate_revenue_tier_payloads(payloads: list[PayrollRevenueTierBase]) -> None:
    if not payloads:
        raise PayrollConfigValidationError("At least one revenue tier is required")
    effective_dates = {payload.effective_from for payload in payloads}
    if len(effective_dates) != 1:
        raise PayrollConfigValidationError("All revenue tiers must share effective_from")

    rows = sorted(payloads, key=lambda payload: Decimal(str(payload.min_revenue)))
    seen_min_revenue: set[Decimal] = set()
    for index, payload in enumerate(rows):
        _validate_effective_range(payload.effective_from, payload.effective_to)
        min_revenue = Decimal(str(payload.min_revenue))
        max_revenue = Decimal(str(payload.max_revenue)) if payload.max_revenue is not None else None
        if min_revenue in seen_min_revenue:
            raise PayrollConfigValidationError("Revenue tier min_revenue must be unique")
        seen_min_revenue.add(min_revenue)
        if max_revenue is not None and max_revenue <= min_revenue:
            raise PayrollConfigValidationError("Revenue tier max_revenue must exceed min_revenue")
        if max_revenue is None and index != len(rows) - 1:
            raise PayrollConfigValidationError("Only the last revenue tier can have empty max")
        if index < len(rows) - 1:
            next_min_revenue = Decimal(str(rows[index + 1].min_revenue))
            if max_revenue is not None and max_revenue > next_min_revenue:
                raise PayrollConfigValidationError("Revenue tiers must not overlap")


def _validate_category_coefficient_payloads(
    payloads: list[PayrollCategoryCoefficientBase],
) -> None:
    if not payloads:
        raise PayrollConfigValidationError("At least one category coefficient is required")
    effective_dates = {payload.effective_from for payload in payloads}
    if len(effective_dates) != 1:
        raise PayrollConfigValidationError("All category coefficients must share effective_from")

    categories = [payload.category for payload in payloads]
    if set(categories) != VALID_PAYROLL_RATE_CATEGORIES or len(categories) != len(set(categories)):
        raise PayrollConfigValidationError(
            "Category coefficients must include each payroll category"
        )
    for payload in payloads:
        _validate_rate_category(payload.category)
        _validate_effective_range(payload.effective_from, payload.effective_to)


def _validate_effective_range(effective_from: date, effective_to: date | None) -> None:
    if effective_to is not None and effective_to <= effective_from:
        raise PayrollConfigValidationError("effective_to must be later than effective_from")

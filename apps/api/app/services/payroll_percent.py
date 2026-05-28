from __future__ import annotations

from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CategoryCoefficient, RevenueTier

REVENUE_TIER_CONFIG_KEY = "payroll.revenue_tiers_by_date"
CATEGORY_COEFFICIENT_CONFIG_KEY = "payroll.category_coefficients_by_date"
FULL_SHIFT_HOURS = Decimal("12")

DEFAULT_REVENUE_TIERS = (
    {
        "min_revenue": Decimal("50000"),
        "max_revenue": Decimal("140000"),
        "rate_percent": Decimal("0.03500"),
        "effective_from": date(2026, 1, 1),
        "effective_to": None,
    },
    {
        "min_revenue": Decimal("140000"),
        "max_revenue": Decimal("190000"),
        "rate_percent": Decimal("0.04500"),
        "effective_from": date(2026, 1, 1),
        "effective_to": None,
    },
    {
        "min_revenue": Decimal("190000"),
        "max_revenue": Decimal("550000"),
        "rate_percent": Decimal("0.05500"),
        "effective_from": date(2026, 1, 1),
        "effective_to": None,
    },
    {
        "min_revenue": Decimal("550000"),
        "max_revenue": None,
        "rate_percent": Decimal("0.06500"),
        "effective_from": date(2026, 1, 1),
        "effective_to": None,
    },
)

DEFAULT_CATEGORY_COEFFICIENTS = (
    {
        "category": "category_1",
        "coefficient": Decimal("3.000"),
        "effective_from": date(2026, 1, 1),
        "effective_to": None,
    },
    {
        "category": "category_2",
        "coefficient": Decimal("2.250"),
        "effective_from": date(2026, 1, 1),
        "effective_to": None,
    },
    {
        "category": "category_3",
        "coefficient": Decimal("1.500"),
        "effective_from": date(2026, 1, 1),
        "effective_to": None,
    },
    {
        "category": "intern",
        "coefficient": Decimal("0.000"),
        "effective_from": date(2026, 1, 1),
        "effective_to": None,
    },
    {
        "category": "freelancer",
        "coefficient": Decimal("0.000"),
        "effective_from": date(2026, 1, 1),
        "effective_to": None,
    },
)


@dataclass(slots=True)
class PercentShift:
    employee_id: Hashable
    category: str
    hours: Decimal | int | float | str
    coefficient: Decimal | int | float | str | None = None


async def load_revenue_tier_versions(
    session: AsyncSession,
    start_date: date,
    end_date: date,
) -> list[dict[str, Any]]:
    result = await session.scalars(
        select(RevenueTier)
        .where(
            RevenueTier.effective_from <= end_date,
            or_(RevenueTier.effective_to.is_(None), RevenueTier.effective_to > start_date),
        )
        .order_by(RevenueTier.effective_from.desc(), RevenueTier.min_revenue)
    )
    return [
        {
            "min_revenue": tier.min_revenue,
            "max_revenue": tier.max_revenue,
            "rate_percent": tier.rate_percent,
            "effective_from": tier.effective_from,
            "effective_to": tier.effective_to,
        }
        for tier in result.all()
    ]


async def load_category_coefficient_versions(
    session: AsyncSession,
    start_date: date,
    end_date: date,
) -> list[dict[str, Any]]:
    result = await session.scalars(
        select(CategoryCoefficient)
        .where(
            CategoryCoefficient.effective_from <= end_date,
            or_(
                CategoryCoefficient.effective_to.is_(None),
                CategoryCoefficient.effective_to > start_date,
            ),
        )
        .order_by(CategoryCoefficient.category, CategoryCoefficient.effective_from.desc())
    )
    return [
        {
            "category": coefficient.category,
            "coefficient": coefficient.coefficient,
            "effective_from": coefficient.effective_from,
            "effective_to": coefficient.effective_to,
        }
        for coefficient in result.all()
    ]


def compute_daily_percent_pool(
    daily_revenue: Decimal | int | float | str,
    work_date: date,
    revenue_tiers: Iterable[Mapping[str, Any]] | None = None,
) -> Decimal:
    revenue = decimal(daily_revenue)
    rate = revenue_tier_rate(revenue, work_date, revenue_tiers)
    if rate is None:
        return Decimal("0")
    return revenue * rate


def distribute_percent_pool(
    daily_pool: Decimal | int | float | str,
    employee_shifts_today: Iterable[PercentShift | Mapping[str, Any]],
) -> dict[Hashable, Decimal]:
    shifts = list(employee_shifts_today)
    weights: dict[Hashable, Decimal] = {}
    for shift in shifts:
        employee_id = shift_employee_id(shift)
        weights[employee_id] = weights.get(employee_id, Decimal("0")) + shift_weight(shift)
    total_weight = sum(weights.values(), Decimal("0"))
    if total_weight <= 0:
        return {shift_employee_id(shift): Decimal("0") for shift in shifts}

    pool = decimal(daily_pool)
    return {
        employee_id: (pool * weight / total_weight).to_integral_value(rounding=ROUND_FLOOR)
        if weight > 0
        else Decimal("0")
        for employee_id, weight in weights.items()
    }


def revenue_tier_rate(
    daily_revenue: Decimal | int | float | str,
    work_date: date,
    revenue_tiers: Iterable[Mapping[str, Any]] | None = None,
) -> Decimal | None:
    revenue = decimal(daily_revenue)
    candidates: list[tuple[date, Decimal, Decimal]] = []
    for tier in revenue_tiers or DEFAULT_REVENUE_TIERS:
        effective_from = date_or_none(tier.get("effective_from")) or date.min
        effective_to = date_or_none(tier.get("effective_to"))
        if effective_from > work_date:
            continue
        if effective_to is not None and effective_to <= work_date:
            continue

        min_revenue = decimal(tier.get("min_revenue", tier.get("from", 0)))
        max_revenue = decimal_or_none(tier.get("max_revenue"))
        if revenue < min_revenue:
            continue
        if max_revenue is not None and revenue >= max_revenue:
            continue

        rate = decimal(tier.get("rate_percent", tier.get("rate", 0)))
        candidates.append((effective_from, min_revenue, rate))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2]


def category_coefficient(
    category: str,
    work_date: date,
    coefficients: Iterable[Mapping[str, Any]] | None = None,
) -> Decimal:
    normalized_category = str(category or "").strip()
    candidates: list[tuple[date, Decimal]] = []
    for item in coefficients or DEFAULT_CATEGORY_COEFFICIENTS:
        if str(item.get("category") or "").strip() != normalized_category:
            continue

        effective_from = date_or_none(item.get("effective_from")) or date.min
        effective_to = date_or_none(item.get("effective_to"))
        if effective_from > work_date:
            continue
        if effective_to is not None and effective_to <= work_date:
            continue
        candidates.append((effective_from, decimal(item.get("coefficient", 0))))

    if not candidates:
        return Decimal("0")
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def shift_weight(shift: PercentShift | Mapping[str, Any]) -> Decimal:
    explicit_coefficient = shift_value(shift, "coefficient")
    coefficient = (
        decimal(explicit_coefficient)
        if explicit_coefficient is not None
        else category_coefficient(str(shift_value(shift, "category") or ""), date.max)
    )
    hours = min(decimal(shift_value(shift, "hours") or 0), FULL_SHIFT_HOURS)
    if coefficient <= 0 or hours <= 0:
        return Decimal("0")
    return coefficient * hours / FULL_SHIFT_HOURS


def shift_employee_id(shift: PercentShift | Mapping[str, Any]) -> Hashable:
    employee_id = shift_value(shift, "employee_id")
    if not isinstance(employee_id, Hashable):
        raise TypeError("employee_id must be hashable")
    return employee_id


def shift_value(shift: PercentShift | Mapping[str, Any], key: str) -> Any:
    if isinstance(shift, Mapping):
        return shift.get(key)
    return getattr(shift, key)


def decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or 0))


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    return decimal(value)


def date_or_none(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None

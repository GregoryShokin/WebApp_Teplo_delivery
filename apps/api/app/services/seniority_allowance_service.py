from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PayrollSeniorityPremium

SENIORITY_ALLOWANCE_MAP_CONFIG_KEY = "payroll.seniority_allowance_by_date"
SENIORITY_ALLOWANCE_SINGLE_MAP_CONFIG_KEY = "payroll.seniority_allowance_map"


async def get_active_allowance_amount(
    session: AsyncSession,
    *,
    position: str,
    role: str,
    on_date: date,
) -> Decimal:
    """Active fixed seniority allowance amount for a date, or 0 if it is not set."""
    allowances = await load_seniority_allowance_map(session, on_date)
    return allowances.get((position, role), Decimal("0"))


async def load_seniority_allowance_map(
    session: AsyncSession,
    on_date: date,
) -> dict[tuple[str, str], Decimal]:
    result = await session.scalars(
        select(PayrollSeniorityPremium)
        .where(
            PayrollSeniorityPremium.effective_from <= on_date,
            sa.or_(
                PayrollSeniorityPremium.effective_to.is_(None),
                PayrollSeniorityPremium.effective_to > on_date,
            ),
        )
        .order_by(PayrollSeniorityPremium.effective_from.desc())
    )
    rates: dict[tuple[str, str], Decimal] = {}
    for row in result.all():
        if row.effective_from > on_date:
            continue
        if row.effective_to is not None and row.effective_to <= on_date:
            continue
        key = (row.position, row.role)
        rates.setdefault(key, _decimal(row.amount))
    return rates


async def load_seniority_allowance_maps(
    session: AsyncSession,
    work_dates: Iterable[date],
) -> dict[date, dict[tuple[str, str], Decimal]]:
    dates = sorted(set(work_dates))
    if not dates:
        return {}
    result = await session.scalars(
        select(PayrollSeniorityPremium)
        .where(
            PayrollSeniorityPremium.effective_from <= dates[-1],
            sa.or_(
                PayrollSeniorityPremium.effective_to.is_(None),
                PayrollSeniorityPremium.effective_to > dates[0],
            ),
        )
        .order_by(PayrollSeniorityPremium.effective_from.desc())
    )
    rows = list(result.all())
    maps: dict[date, dict[tuple[str, str], Decimal]] = {}
    for work_date in dates:
        rates: dict[tuple[str, str], Decimal] = {}
        for row in rows:
            if row.effective_from > work_date:
                continue
            if row.effective_to is not None and row.effective_to <= work_date:
                continue
            rates.setdefault((row.position, row.role), _decimal(row.amount))
        maps[work_date] = rates
    return maps


def allowance_amount_from_settings(
    settings: Mapping[str, Any],
    *,
    position: str | None,
    role: str | None,
    on_date: date | None,
) -> Decimal:
    if not position or not role:
        return Decimal("0")

    if on_date is not None:
        maps_by_date = settings.get(SENIORITY_ALLOWANCE_MAP_CONFIG_KEY)
        if isinstance(maps_by_date, Mapping):
            day_map = maps_by_date.get(on_date)
            if day_map is None:
                day_map = maps_by_date.get(on_date.isoformat())
            amount = _amount_from_map(day_map, position=position, role=role)
            if amount is not None:
                return amount

    amount = _amount_from_map(
        settings.get(SENIORITY_ALLOWANCE_SINGLE_MAP_CONFIG_KEY),
        position=position,
        role=role,
    )
    return amount if amount is not None else Decimal("0")


def _amount_from_map(
    value: Any,
    *,
    position: str,
    role: str,
) -> Decimal | None:
    if not isinstance(value, Mapping):
        return None
    amount = value.get((position, role))
    if amount is None:
        amount = value.get(f"{position}:{role}")
    if amount is None:
        return None
    return _decimal(amount)


def _decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or 0))

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor
from app.models import RevenueForecast
from app.services.iiko_revenue import fetch_daily_revenue

METHOD_CODE = "avg_6_same_weekday"
HISTORY_WINDOW_WEEKS = 6
MIN_HISTORY_POINTS = 4
FORECAST_FRESH_DAYS = 7
MONEY_QUANT = Decimal("0.01")
DEFAULT_COEFF = Decimal("1.0")

EVENT_REVIEW_DATES: frozenset[tuple[int, int]] = frozenset(
    {
        (2, 14),
        (2, 22),
        (2, 23),
        (3, 8),
        (6, 1),
        (9, 1),
        *((12, day) for day in range(26, 32)),
        *((1, day) for day in range(2, 11)),
    }
)


def is_event_review_date(target: date) -> bool:
    return (target.month, target.day) in EVENT_REVIEW_DATES


async def compute_forecast(
    session: AsyncSession,
    business_date: date,
    *,
    force_refresh_iiko: bool = False,
    revenue_by_date: dict[date, Decimal] | None = None,
) -> RevenueForecast:
    candidates = _same_weekday_history_dates(business_date)
    revenues = revenue_by_date
    if revenues is None:
        revenues = await _fetch_daily_revenue(
            session,
            min(candidates),
            business_date - timedelta(days=1),
            force_refresh=force_refresh_iiko,
        )

    forecast = await _find_forecast(session, business_date)
    if forecast is None:
        forecast = _new_forecast(business_date)
        session.add(forecast)

    now = datetime.now(UTC)
    history_points = _build_history_points(candidates, revenues)
    forecast.weekday = business_date.weekday()
    forecast.method_code = METHOD_CODE
    forecast.history_window_weeks = HISTORY_WINDOW_WEEKS
    forecast.history_points = history_points
    forecast.event_review_recommended = is_event_review_date(business_date)
    forecast.computed_at = now
    forecast.updated_at = now

    if forecast.manual_override_amount is not None:
        await _commit_refresh(session, forecast)
        return forecast

    valid_amounts = [
        _decimal_from_history_point(point)
        for point in history_points
        if point["included"] and point["amount"] is not None
    ]
    if len(valid_amounts) >= MIN_HISTORY_POINTS:
        base_average = _money(sum(valid_amounts, Decimal("0")) / Decimal(len(valid_amounts)))
        forecast.base_average_amount = base_average
        forecast.forecast_amount = _money(
            base_average * _coeff(forecast.season_coeff) * _coeff(forecast.event_coeff)
        )
        forecast.quality_status = "ok"
    else:
        forecast.base_average_amount = None
        forecast.forecast_amount = None
        forecast.quality_status = "requires_review"

    await _commit_refresh(session, forecast)
    return forecast


async def get_forecasts_in_range(
    session: AsyncSession,
    date_from: date,
    date_to: date,
) -> list[RevenueForecast]:
    forecasts = await _get_forecasts_between(session, date_from, date_to)
    return sorted(forecasts, key=lambda forecast: forecast.business_date)


async def apply_manual_override(
    session: AsyncSession,
    business_date: date,
    *,
    amount: Decimal,
    reason: str | None,
    actor: CurrentActor,
) -> RevenueForecast:
    forecast = await _find_forecast(session, business_date)
    if forecast is None:
        forecast = _new_forecast(business_date)
        session.add(forecast)

    now = datetime.now(UTC)
    forecast.manual_override_amount = _money(amount)
    forecast.manual_override_reason = reason.strip() if reason and reason.strip() else None
    forecast.manual_override_set_by_user_id = _actor_user_id(actor)
    forecast.manual_override_set_at = now
    forecast.forecast_amount = _money(amount)
    forecast.quality_status = "manual_override"
    forecast.event_review_recommended = is_event_review_date(business_date)
    forecast.updated_at = now
    await _commit_refresh(session, forecast)
    return forecast


async def remove_manual_override(
    session: AsyncSession,
    business_date: date,
    *,
    actor: CurrentActor,
) -> RevenueForecast:
    del actor
    forecast = await _find_forecast(session, business_date)
    if forecast is None or forecast.manual_override_amount is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Ручной прогноз не найден",
        )

    forecast.manual_override_amount = None
    forecast.manual_override_reason = None
    forecast.manual_override_set_by_user_id = None
    forecast.manual_override_set_at = None
    forecast.updated_at = datetime.now(UTC)
    await session.flush()
    return await compute_forecast(session, business_date)


async def compute_forecast_for_range(
    session: AsyncSession,
    date_from: date,
    date_to: date,
    *,
    force_refresh_iiko: bool = False,
) -> list[RevenueForecast]:
    if date_from > date_to:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Дата окончания не может быть раньше даты начала",
        )

    dates = _date_range(date_from, date_to)
    existing = {
        forecast.business_date: forecast
        for forecast in await _get_forecasts_between(session, date_from, date_to)
    }
    fresh_after = datetime.now(UTC) - timedelta(days=FORECAST_FRESH_DAYS)
    dates_to_compute = [
        target
        for target in dates
        if force_refresh_iiko or not _is_fresh(existing.get(target), fresh_after)
    ]
    if not dates_to_compute:
        return []

    revenue_by_date = await _fetch_daily_revenue(
        session,
        date_from - timedelta(days=HISTORY_WINDOW_WEEKS * 7),
        date_to,
        force_refresh=force_refresh_iiko,
    )

    recomputed: list[RevenueForecast] = []
    for target in dates_to_compute:
        recomputed.append(
            await compute_forecast(
                session,
                target,
                force_refresh_iiko=force_refresh_iiko,
                revenue_by_date=revenue_by_date,
            )
        )
    return recomputed


def _same_weekday_history_dates(business_date: date) -> list[date]:
    return [
        business_date - timedelta(days=7 * weeks_ago)
        for weeks_ago in range(1, HISTORY_WINDOW_WEEKS + 1)
    ]


def _build_history_points(
    candidates: list[date],
    revenues: dict[date, Decimal],
) -> list[dict[str, str | bool | None]]:
    points: list[dict[str, str | bool | None]] = []
    for history_date in candidates:
        amount = revenues.get(history_date)
        included = amount is not None and amount > 0
        points.append(
            {
                "date": history_date.isoformat(),
                "amount": str(_money(amount)) if amount is not None else None,
                "included": included,
            }
        )
    return points


def _decimal_from_history_point(point: dict) -> Decimal:
    return Decimal(str(point["amount"]))


async def _find_forecast(session: AsyncSession, business_date: date) -> RevenueForecast | None:
    result = await session.scalars(
        select(RevenueForecast).where(RevenueForecast.business_date == business_date)
    )
    return next(
        (
            forecast
            for forecast in result.all()
            if forecast.business_date == business_date
        ),
        None,
    )


async def _get_forecasts_between(
    session: AsyncSession,
    date_from: date,
    date_to: date,
) -> list[RevenueForecast]:
    result = await session.scalars(
        select(RevenueForecast)
        .where(RevenueForecast.business_date >= date_from)
        .where(RevenueForecast.business_date <= date_to)
        .order_by(RevenueForecast.business_date)
    )
    return [
        forecast
        for forecast in result.all()
        if date_from <= forecast.business_date <= date_to
    ]


def _new_forecast(business_date: date) -> RevenueForecast:
    return RevenueForecast(
        id=uuid.uuid4(),
        business_date=business_date,
        weekday=business_date.weekday(),
        method_code=METHOD_CODE,
        history_window_weeks=HISTORY_WINDOW_WEEKS,
        history_points=[],
        season_coeff=DEFAULT_COEFF,
        event_coeff=DEFAULT_COEFF,
        forecast_amount=None,
        quality_status="requires_review",
        event_review_recommended=is_event_review_date(business_date),
    )


async def _fetch_daily_revenue(
    session: AsyncSession,
    date_from: date,
    date_to: date,
    *,
    force_refresh: bool,
) -> dict[date, Decimal]:
    if _supports_force_refresh(fetch_daily_revenue):
        return await fetch_daily_revenue(
            session,
            date_from,
            date_to,
            force_refresh=force_refresh,
        )
    return await fetch_daily_revenue(session, date_from, date_to)


def _supports_force_refresh(callable_: object) -> bool:
    try:
        return "force_refresh" in inspect.signature(callable_).parameters
    except (TypeError, ValueError):
        return False


def _is_fresh(forecast: RevenueForecast | None, fresh_after: datetime) -> bool:
    return (
        forecast is not None
        and forecast.computed_at is not None
        and forecast.computed_at >= fresh_after
    )


def _date_range(date_from: date, date_to: date) -> list[date]:
    return [
        date_from + timedelta(days=offset)
        for offset in range((date_to - date_from).days + 1)
    ]


def _money(amount: Decimal) -> Decimal:
    return amount.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def _coeff(value: Decimal | None) -> Decimal:
    return value if value is not None else DEFAULT_COEFF


async def _commit_refresh(session: AsyncSession, item: RevenueForecast) -> None:
    await session.commit()
    await session.refresh(item)


def _actor_user_id(actor: CurrentActor) -> uuid.UUID | None:
    user_id = getattr(actor, "user_id", None)
    return user_id if isinstance(user_id, uuid.UUID) else None

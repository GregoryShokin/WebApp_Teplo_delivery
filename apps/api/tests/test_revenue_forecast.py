from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi import HTTPException

from app.api.deps import CurrentActor
from app.models import RevenueForecast
from app.services import revenue_forecast_service


class FakeScalarResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class RevenueForecastFakeSession:
    def __init__(self, forecasts: list[RevenueForecast] | None = None) -> None:
        self.forecasts = {forecast.id: forecast for forecast in forecasts or []}
        self.commits = 0
        self.flushes = 0
        self.refreshed: list[Any] = []

    def add(self, item: Any) -> None:
        if isinstance(item, RevenueForecast):
            self.forecasts[item.id] = item

    async def scalars(self, _query: Any) -> FakeScalarResult:
        return FakeScalarResult(list(self.forecasts.values()))

    async def commit(self) -> None:
        self.commits += 1

    async def flush(self) -> None:
        self.flushes += 1

    async def refresh(self, item: Any) -> None:
        self.refreshed.append(item)


def actor() -> CurrentActor:
    return CurrentActor(roles=frozenset({"finance_manager"}), user_id=uuid.uuid4())


def revenue_for_history(target: date, amounts: list[Decimal | int | None]) -> dict[date, Decimal]:
    revenues: dict[date, Decimal] = {}
    for index, amount in enumerate(amounts, start=1):
        if amount is None:
            continue
        revenues[target - timedelta(days=7 * index)] = Decimal(str(amount))
    return revenues


def fresh_forecast(target: date) -> RevenueForecast:
    return RevenueForecast(
        id=uuid.uuid4(),
        business_date=target,
        weekday=target.weekday(),
        method_code="avg_6_same_weekday",
        history_window_weeks=6,
        history_points=[],
        season_coeff=Decimal("1.0"),
        event_coeff=Decimal("1.0"),
        forecast_amount=Decimal("100.00"),
        quality_status="ok",
        event_review_recommended=False,
        computed_at=datetime.now(UTC) - timedelta(days=1),
    )


async def test_compute_uses_last_6_same_weekday(monkeypatch: pytest.MonkeyPatch) -> None:
    target = date(2026, 6, 4)
    session = RevenueForecastFakeSession()
    calls: list[tuple[date, date, bool]] = []

    async def fake_fetch_daily_revenue(
        _session: Any,
        date_from: date,
        date_to: date,
        *,
        force_refresh: bool = False,
    ) -> dict[date, Decimal]:
        calls.append((date_from, date_to, force_refresh))
        return revenue_for_history(target, [120000, 95000, 108000, 115000, 99000, 101000])

    monkeypatch.setattr(
        revenue_forecast_service,
        "fetch_daily_revenue",
        fake_fetch_daily_revenue,
    )

    forecast = await revenue_forecast_service.compute_forecast(
        session,  # type: ignore[arg-type]
        target,
        force_refresh_iiko=True,
    )

    assert calls == [(date(2026, 4, 23), date(2026, 6, 3), True)]
    assert [point["date"] for point in forecast.history_points] == [
        "2026-05-28",
        "2026-05-21",
        "2026-05-14",
        "2026-05-07",
        "2026-04-30",
        "2026-04-23",
    ]


async def test_compute_excludes_null_and_zero_revenue() -> None:
    target = date(2026, 6, 4)
    session = RevenueForecastFakeSession()

    forecast = await revenue_forecast_service.compute_forecast(
        session,  # type: ignore[arg-type]
        target,
        revenue_by_date=revenue_for_history(target, [120000, None, 0, 115000, None, 101000]),
    )

    assert [point["included"] for point in forecast.history_points] == [
        True,
        False,
        False,
        True,
        False,
        True,
    ]
    assert forecast.quality_status == "requires_review"
    assert forecast.forecast_amount is None


async def test_compute_ok_with_exactly_4_points() -> None:
    target = date(2026, 6, 4)
    session = RevenueForecastFakeSession()

    forecast = await revenue_forecast_service.compute_forecast(
        session,  # type: ignore[arg-type]
        target,
        revenue_by_date=revenue_for_history(target, [100000, 200000, 300000, 400000, 0, None]),
    )

    assert forecast.quality_status == "ok"
    assert forecast.base_average_amount == Decimal("250000.00")
    assert forecast.forecast_amount == Decimal("250000.00")


async def test_compute_with_5_points_ok() -> None:
    target = date(2026, 6, 4)
    session = RevenueForecastFakeSession()

    forecast = await revenue_forecast_service.compute_forecast(
        session,  # type: ignore[arg-type]
        target,
        revenue_by_date=revenue_for_history(target, [100000, 200000, 300000, 400000, 500000, None]),
    )

    assert forecast.quality_status == "ok"
    assert forecast.forecast_amount == Decimal("300000.00")


@pytest.mark.parametrize(
    "target",
    [
        date(2026, 2, 14),
        date(2026, 2, 23),
        date(2026, 3, 8),
        date(2026, 6, 1),
        date(2026, 9, 1),
        date(2026, 12, 28),
        date(2026, 1, 5),
    ],
)
async def test_event_review_flag_for_holidays(target: date) -> None:
    session = RevenueForecastFakeSession()

    forecast = await revenue_forecast_service.compute_forecast(
        session,  # type: ignore[arg-type]
        target,
        revenue_by_date=revenue_for_history(
            target, [100000, 100000, 100000, 100000, 100000, 100000]
        ),
    )

    assert forecast.event_review_recommended is True


@pytest.mark.parametrize("target", [date(2026, 2, 15), date(2026, 2, 24)])
async def test_event_review_flag_for_regular_days(target: date) -> None:
    session = RevenueForecastFakeSession()

    forecast = await revenue_forecast_service.compute_forecast(
        session,  # type: ignore[arg-type]
        target,
        revenue_by_date=revenue_for_history(
            target, [100000, 100000, 100000, 100000, 100000, 100000]
        ),
    )

    assert forecast.event_review_recommended is False


async def test_apply_manual_override_sets_amount_and_quality() -> None:
    target = date(2026, 6, 4)
    session = RevenueForecastFakeSession()
    forecast = await revenue_forecast_service.compute_forecast(
        session,  # type: ignore[arg-type]
        target,
        revenue_by_date=revenue_for_history(
            target, [100000, 200000, 300000, 400000, 500000, 600000]
        ),
    )
    history_before = list(forecast.history_points)
    base_before = forecast.base_average_amount
    current_actor = actor()

    overridden = await revenue_forecast_service.apply_manual_override(
        session,  # type: ignore[arg-type]
        target,
        amount=Decimal("130000"),
        reason="запланирована акция",
        actor=current_actor,
    )

    assert overridden.forecast_amount == Decimal("130000.00")
    assert overridden.manual_override_amount == Decimal("130000.00")
    assert overridden.quality_status == "manual_override"
    assert overridden.manual_override_set_by_user_id == current_actor.user_id
    assert overridden.base_average_amount == base_before
    assert overridden.history_points == history_before


async def test_remove_override_triggers_recompute(monkeypatch: pytest.MonkeyPatch) -> None:
    target = date(2026, 6, 4)
    forecast = RevenueForecast(
        id=uuid.uuid4(),
        business_date=target,
        weekday=target.weekday(),
        method_code="avg_6_same_weekday",
        history_window_weeks=6,
        history_points=[],
        season_coeff=Decimal("1.0"),
        event_coeff=Decimal("1.0"),
        manual_override_amount=Decimal("130000.00"),
        manual_override_reason="акция",
        forecast_amount=Decimal("130000.00"),
        quality_status="manual_override",
        event_review_recommended=False,
    )
    session = RevenueForecastFakeSession([forecast])

    async def fake_fetch_daily_revenue(*_args: Any, **_kwargs: Any) -> dict[date, Decimal]:
        return revenue_for_history(target, [100000, 200000, 300000, 400000, 500000, 600000])

    monkeypatch.setattr(
        revenue_forecast_service,
        "fetch_daily_revenue",
        fake_fetch_daily_revenue,
    )

    removed = await revenue_forecast_service.remove_manual_override(
        session,  # type: ignore[arg-type]
        target,
        actor=actor(),
    )

    assert session.flushes == 1
    assert removed.manual_override_amount is None
    assert removed.quality_status == "ok"
    assert removed.forecast_amount == Decimal("350000.00")


async def test_remove_override_404_if_no_override() -> None:
    target = date(2026, 6, 4)
    session = RevenueForecastFakeSession([fresh_forecast(target)])

    with pytest.raises(HTTPException) as exc_info:
        await revenue_forecast_service.remove_manual_override(
            session,  # type: ignore[arg-type]
            target,
            actor=actor(),
        )

    assert exc_info.value.status_code == 404


async def test_recompute_range_makes_single_iiko_call(monkeypatch: pytest.MonkeyPatch) -> None:
    session = RevenueForecastFakeSession()
    date_from = date(2026, 6, 1)
    date_to = date(2026, 6, 30)
    calls: list[tuple[date, date, bool]] = []

    async def fake_fetch_daily_revenue(
        _session: Any,
        range_from: date,
        range_to: date,
        *,
        force_refresh: bool = False,
    ) -> dict[date, Decimal]:
        calls.append((range_from, range_to, force_refresh))
        return {
            range_from + timedelta(days=offset): Decimal("100000")
            for offset in range((range_to - range_from).days + 1)
        }

    monkeypatch.setattr(
        revenue_forecast_service,
        "fetch_daily_revenue",
        fake_fetch_daily_revenue,
    )

    recomputed = await revenue_forecast_service.compute_forecast_for_range(
        session,  # type: ignore[arg-type]
        date_from,
        date_to,
        force_refresh_iiko=True,
    )

    assert len(recomputed) == 30
    assert calls == [(date(2026, 4, 20), date(2026, 6, 30), True)]


async def test_recompute_skips_fresh_records(monkeypatch: pytest.MonkeyPatch) -> None:
    target = date(2026, 6, 4)
    session = RevenueForecastFakeSession([fresh_forecast(target)])

    async def fake_fetch_daily_revenue(*_args: Any, **_kwargs: Any) -> dict[date, Decimal]:
        raise AssertionError("fresh forecast should not call iiko")

    monkeypatch.setattr(
        revenue_forecast_service,
        "fetch_daily_revenue",
        fake_fetch_daily_revenue,
    )

    recomputed = await revenue_forecast_service.compute_forecast_for_range(
        session,  # type: ignore[arg-type]
        target,
        target,
    )

    assert recomputed == []


async def test_forecast_unique_per_business_date() -> None:
    target = date(2026, 6, 4)
    session = RevenueForecastFakeSession()

    first = await revenue_forecast_service.compute_forecast(
        session,  # type: ignore[arg-type]
        target,
        revenue_by_date=revenue_for_history(
            target, [100000, 100000, 100000, 100000, 100000, 100000]
        ),
    )
    second = await revenue_forecast_service.compute_forecast(
        session,  # type: ignore[arg-type]
        target,
        revenue_by_date=revenue_for_history(
            target, [200000, 200000, 200000, 200000, 200000, 200000]
        ),
    )

    assert first.id == second.id
    assert len(session.forecasts) == 1
    assert second.forecast_amount == Decimal("200000.00")


async def test_override_preserves_history_points_for_review() -> None:
    target = date(2026, 6, 4)
    session = RevenueForecastFakeSession()
    forecast = await revenue_forecast_service.compute_forecast(
        session,  # type: ignore[arg-type]
        target,
        revenue_by_date=revenue_for_history(target, [100000, None, 0, 400000, 500000, 600000]),
    )
    history_before = list(forecast.history_points)
    base_before = forecast.base_average_amount

    overridden = await revenue_forecast_service.apply_manual_override(
        session,  # type: ignore[arg-type]
        target,
        amount=Decimal("130000"),
        reason=None,
        actor=actor(),
    )

    assert overridden.history_points == history_before
    assert overridden.base_average_amount == base_before

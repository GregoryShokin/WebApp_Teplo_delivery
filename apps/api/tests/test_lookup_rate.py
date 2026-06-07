"""Регрессионные тесты на поиск ставки по роли/категории/станции.

Закрепляют фикс: ставка с заданной станцией НЕ должна отсекаться, когда в
запросе станция пустая (раньше жёсткий `continue` приводил к лавине
`missing_role_category_rate` после iiko-синка). См. payroll_calculator.py
role_category_rate_details_from_versions.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.services.payroll_calculator import (
    PAYROLL_RATE_CONFIG_KEY,
    role_category_rate_from_versions,
)

WORK_DATE = date(2026, 1, 1)


def _settings(rates: list[dict]) -> dict:
    return {PAYROLL_RATE_CONFIG_KEY: rates}


def _rate(*, station: str | None, amount: str) -> dict:
    return {
        "position_group": "sushi",
        "category": "category_1",
        "station": station,
        "rate_type": "daily",
        "amount": amount,
        "effective_from": date(2025, 1, 1),
        "effective_to": None,
    }


def test_station_specific_rate_matches_empty_station_query() -> None:
    # Ставка со станцией + пустая станция в запросе → ставка НАХОДИТСЯ (фикс).
    settings = _settings([_rate(station="hot", amount="100")])
    result = role_category_rate_from_versions(
        settings, "sushi", "category_1", WORK_DATE, None
    )
    assert result == Decimal("100")


def test_station_specific_rate_matches_same_station_query() -> None:
    settings = _settings([_rate(station="hot", amount="120")])
    result = role_category_rate_from_versions(
        settings, "sushi", "category_1", WORK_DATE, "hot"
    )
    assert result == Decimal("120")


def test_station_specific_rate_skipped_for_different_station_query() -> None:
    # Обе станции заданы и различаются → ставка отсекается, других нет → None.
    settings = _settings([_rate(station="hot", amount="100")])
    result = role_category_rate_from_versions(
        settings, "sushi", "category_1", WORK_DATE, "cold"
    )
    assert result is None


def test_stationless_rate_matches_any_station_query() -> None:
    settings = _settings([_rate(station=None, amount="90")])
    result = role_category_rate_from_versions(
        settings, "sushi", "category_1", WORK_DATE, "hot"
    )
    assert result == Decimal("90")


def test_station_specific_preferred_over_stationless_for_empty_query() -> None:
    # При пустой станции обе ставки подходят; станционная имеет приоритет (station_score).
    settings = _settings(
        [_rate(station=None, amount="90"), _rate(station="hot", amount="100")]
    )
    result = role_category_rate_from_versions(
        settings, "sushi", "category_1", WORK_DATE, None
    )
    assert result == Decimal("100")

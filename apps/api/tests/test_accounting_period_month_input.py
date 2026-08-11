from __future__ import annotations

from datetime import date, datetime

import pytest

from app.services.accounting_periods import parse_month_input


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (date(2026, 7, 31), date(2026, 7, 1)),
        (datetime(2026, 7, 31, 12, 30), date(2026, 7, 1)),
        ("2026-07", date(2026, 7, 1)),
        ("2026/7/15", date(2026, 7, 1)),
        ("202607", date(2026, 7, 1)),
        ("07.2026", date(2026, 7, 1)),
        ("01.07.2026", date(2026, 7, 1)),
        ("1 июля 2026", date(2026, 7, 1)),
        ("ИЮЛЬ", date(2026, 7, 1)),
        ("июль-01", date(2026, 7, 1)),
        ("July 2026", date(2026, 7, 1)),
        ("7", date(2026, 7, 1)),
    ],
)
def test_parse_month_input_accepts_common_formats(raw: object, expected: date) -> None:
    assert parse_month_input(raw, default_year=2026) == expected


@pytest.mark.parametrize("raw", ["13.2026", "непонятно", "июль"])
def test_parse_month_input_rejects_invalid_or_ambiguous_without_fallback(raw: str) -> None:
    with pytest.raises(ValueError, match="месяц"):
        parse_month_input(raw)

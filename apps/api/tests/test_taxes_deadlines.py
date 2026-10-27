"""Регресс: выходной срок СФР не становится преждевременной просрочкой."""

from datetime import UTC, date, datetime

import pytest

from app.services.taxes.deadlines import injury_due
from app.services.taxes.document_parser import _injury_due
from app.services.taxes.official_payroll import injury_due as projected_injury_due
from app.services.taxes.promote import _injury_due_fallback


@pytest.mark.parametrize(
    ("year", "month", "expected"),
    [
        (2026, 10, date(2026, 11, 16)),  # воскресенье → понедельник
        (2026, 7, date(2026, 8, 17)),  # суббота → понедельник
        (2026, 1, date(2026, 2, 16)),
        (2026, 2, date(2026, 3, 16)),
        (2026, 4, date(2026, 5, 15)),  # рабочий день остаётся прежним
        (2026, 12, date(2027, 1, 15)),
        (2027, 12, date(2028, 1, 17)),  # переход года + выходной
    ],
)
def test_injury_deadline_is_consistent_across_document_plan_and_projection(
    year: int, month: int, expected: date
) -> None:
    assert injury_due(year, month) == expected
    assert _injury_due(year, month) == expected
    assert projected_injury_due(year, month) == expected
    received = datetime(year, month, 27, tzinfo=UTC)
    assert _injury_due_fallback("contrib_injury", None, received) == expected


def test_existing_recognized_injury_deadline_rolls_forward_before_promotion() -> None:
    assert _injury_due_fallback("contrib_injury", date(2026, 11, 15), None) == date(2026, 11, 16)
    assert _injury_due_fallback("usn_advance", date(2026, 11, 15), None) == date(2026, 11, 15)
    assert _injury_due_fallback("contrib_injury", None, None) is None

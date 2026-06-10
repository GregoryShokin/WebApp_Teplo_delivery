"""Доступно к авансу — earned-to-date.

Фаза 2a: окладничий earned-to-date по календарной формуле (оклад × прошло_дней /
дней_в_периоде). Чистые юнит-тесты без БД.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.models import Employee, PayrollPeriod
from app.services.payroll_admin import (
    PAYOUT_MODE_FIRST_HALF,
    PAYOUT_MODE_SPLIT,
    okladnik_earned_to_date,
)


def _period(start: date, end: date) -> PayrollPeriod:
    return PayrollPeriod(
        period_type="half_month",
        start_date=start,
        end_date=end,
        payroll_date=end,
        status="open",
    )


def _employee(hire: date | None = None, fire: date | None = None) -> Employee:
    return Employee(hire_date=hire, fire_date=fire)


def test_earned_to_date_prorates_by_calendar_days() -> None:
    # split → база полупериода = оклад/2 = 45000; на 5-е число 5/15 = 15000.00
    period = _period(date(2026, 6, 1), date(2026, 6, 15))
    assert okladnik_earned_to_date(
        Decimal("90000"), PAYOUT_MODE_SPLIT, _employee(), period, date(2026, 6, 5)
    ) == Decimal("15000.00")


def test_earned_to_date_full_base_at_period_end() -> None:
    period = _period(date(2026, 6, 1), date(2026, 6, 15))
    assert okladnik_earned_to_date(
        Decimal("90000"), PAYOUT_MODE_SPLIT, _employee(), period, date(2026, 6, 15)
    ) == Decimal("45000.00")


def test_earned_to_date_zero_before_period_start() -> None:
    period = _period(date(2026, 6, 1), date(2026, 6, 15))
    assert okladnik_earned_to_date(
        Decimal("90000"), PAYOUT_MODE_SPLIT, _employee(), period, date(2026, 5, 31)
    ) == Decimal("0.00")


def test_earned_to_date_second_half_uses_actual_month_length() -> None:
    # Вторая половина июня (16–30) = 15 дней; на 20-е число прошло 5 дней.
    # split → база 45000; 45000 × 5/15 = 15000.00
    period = _period(date(2026, 6, 16), date(2026, 6, 30))
    assert okladnik_earned_to_date(
        Decimal("90000"), PAYOUT_MODE_SPLIT, _employee(), period, date(2026, 6, 20)
    ) == Decimal("15000.00")


def test_earned_to_date_respects_payout_mode_first_half() -> None:
    # first_half: весь оклад начисляется в первой половине, во второй — ноль.
    first = _period(date(2026, 6, 1), date(2026, 6, 15))
    second = _period(date(2026, 6, 16), date(2026, 6, 30))
    # В первой половине на 15-е заработан весь оклад 90000.
    assert okladnik_earned_to_date(
        Decimal("90000"), PAYOUT_MODE_FIRST_HALF, _employee(), first, date(2026, 6, 15)
    ) == Decimal("90000.00")
    # Во второй половине заработка нет (оклад уже выплачен на 15-е).
    assert okladnik_earned_to_date(
        Decimal("90000"), PAYOUT_MODE_FIRST_HALF, _employee(), second, date(2026, 6, 30)
    ) == Decimal("0.00")


def test_earned_to_date_caps_at_fire_date() -> None:
    # Уволен 8-го: на 12-е заработано только по 8-е = 8/15 от 45000 = 24000.00
    period = _period(date(2026, 6, 1), date(2026, 6, 15))
    emp = _employee(fire=date(2026, 6, 8))
    assert okladnik_earned_to_date(
        Decimal("90000"), PAYOUT_MODE_SPLIT, emp, period, date(2026, 6, 12)
    ) == Decimal("24000.00")

"""Отбор фактов для расчёта: какие месяцы обязаны быть покрыты выручкой.

УСН отчитывается поквартально, поэтому спрос — только с ЗАКРЫТЫХ кварталов (решение
владельца 26.07.2026): дыра в идущем квартале — норма (платёжки по нему ещё не существует),
дыра в закрытом — авария (расчёт по отчётному периоду занижен).
"""

from __future__ import annotations

from datetime import date

from app.services.taxes.repository import expected_months


def test_open_quarter_months_are_not_expected() -> None:
    """26 июля: Q3 идёт — июль не обязателен, спрос только с янв–июн (Q1+Q2)."""
    assert expected_months(date(2026, 1, 1), date(2026, 7, 26)) == {
        (2026, m) for m in range(1, 7)
    }


def test_quarter_becomes_expected_on_its_last_day() -> None:
    """Квартал закрывается своим последним днём — с этого момента его месяцы обязательны."""
    assert expected_months(date(2026, 1, 1), date(2026, 6, 30)) == {
        (2026, m) for m in range(1, 7)
    }
    # Днём раньше Q2 ещё открыт — обязателен только Q1.
    assert expected_months(date(2026, 1, 1), date(2026, 6, 29)) == {
        (2026, m) for m in range(1, 4)
    }


def test_no_closed_quarters_no_expectations() -> None:
    """Февраль: ни один квартал не закрыт — сверять нечего, требований к базе нет."""
    assert expected_months(date(2026, 1, 1), date(2026, 2, 15)) == set()

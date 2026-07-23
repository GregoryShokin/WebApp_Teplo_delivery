"""Разбор документов налогового агента.

Фикстуры — реальные формы (0401060 и Т-53), ОБЕЗЛИЧЕННЫЕ: реквизиты плательщика и ФИО
сотрудника заменены на синтетические. Бизнес-величины сохранены — на них держатся тесты
и они не идентифицируют лицо: платёжка УСН за полугодие = 478 376, допвзнос = 105 628,
ведомости на 20 986 и 22 696.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from app.services.taxes.document_parser import (
    parse_payment_order,
    parse_payroll_statement,
)

FIXTURES = Path(__file__).parent / "fixtures" / "taxes"


def _po(name: str, filename: str, year: int = 2026):
    data = (FIXTURES / name).read_bytes()
    return parse_payment_order(data, filename=filename, default_year=year)


def _ved(name: str, filename: str):
    data = (FIXTURES / name).read_bytes()
    return parse_payroll_statement(data, filename=filename)


# ── платёжные поручения ──────────────────────────────────────────────────────


def test_usn_h1_payment_order() -> None:
    """Исправленная платёжка УСН за полугодие = 478 376 ₽, срок 28.07, вид usn_advance."""
    doc = _po("usn_h1_478376.docx", "УСН 2 кв до 28.07.docx")

    assert doc.amount == Decimal("478376")
    assert doc.kbk == "18201061201010000510"
    assert doc.recipient == "fns"
    assert doc.tax_kind == "usn_advance"
    assert doc.due_date == date(2026, 7, 28)
    assert doc.period_hint == "h1"
    assert doc.needs_review is False


def test_extra_1pct_due_date_read_from_body_not_filename() -> None:
    """Допвзнос 1% = 105 628 ₽. Срок 25.09 стоит в ТЕЛЕ (в имени файла его нет)."""
    doc = _po("extra_1pct_105628.docx", "1% за 2 кв 2026.docx")

    assert doc.amount == Decimal("105628")
    assert doc.tax_kind == "contrib_extra_1pct"
    assert doc.due_date == date(2026, 9, 25)
    assert doc.needs_review is False


def test_enp_payroll_is_flagged_for_review() -> None:
    """Зарплатный ЕНП: НДФЛ и взносы смешаны — вид enp_payroll, помечен на разнос."""
    doc = _po("enp_payroll_14902.docx", "ЕНП_до 28.07.docx")

    assert doc.amount == Decimal("14902.30")
    assert doc.tax_kind == "enp_payroll"
    assert doc.needs_review is True
    assert any("смешаны" in r for r in doc.review_reasons)


def test_zero_stub_is_zero_not_missing() -> None:
    """Нулевая платёжка-заглушка: сумма 0 (не None) — отличаем ноль от «не распознали»."""
    doc = _po("usn_zero_stub.docx", "УСН 2 кв до 28.07.docx")

    assert doc.amount == Decimal("0")
    assert doc.tax_kind == "usn_advance"
    # Ноль распознан штатно — на разбор он не жалуется, «ноль странен» ловит движок сверки.
    assert doc.needs_review is False


# ── ведомости Т-53 ───────────────────────────────────────────────────────────


def test_vedomost_advance() -> None:
    """Аванс июля: сотрудник таб. 206, 20 986 ₽, расчётный период — июль."""
    doc = _ved("vedomost_advance_20986.xls", "ВЕД-13 АВАНС 20.07.xls")

    assert doc.doc_number == "20-13"
    assert doc.payout_kind == "advance"
    assert doc.period_start == date(2026, 7, 1)
    assert doc.period_end == date(2026, 7, 31)
    assert len(doc.rows) == 1
    assert doc.rows[0].tab_number == "206"
    assert doc.rows[0].employee == "ИВАНОВА И.И."
    assert doc.rows[0].amount == Decimal("20986")
    assert doc.total == Decimal("20986")
    assert doc.needs_review is False


def test_vedomost_salary() -> None:
    """Зарплата июля = 22 696 ₽. Аванс 20 986 + ЗП 22 696 = на руки 43 682 (с детским вычетом)."""
    doc = _ved("vedomost_salary_22696.xls", "ВЕД-14 ЗП 05.08.xls")

    assert doc.doc_number == "20-14"
    assert doc.payout_kind == "salary"
    assert doc.rows[0].employee == "ИВАНОВА И.И."
    assert doc.total == Decimal("22696")


def test_advance_plus_salary_equals_net_pay() -> None:
    """Контроль на сумме двух ведомостей: 20 986 + 22 696 = 43 682 (оклад 50 000 − НДФЛ 6 318)."""
    adv = _ved("vedomost_advance_20986.xls", "ВЕД-13 АВАНС 20.07.xls")
    sal = _ved("vedomost_salary_22696.xls", "ВЕД-14 ЗП 05.08.xls")

    assert adv.total + sal.total == Decimal("43682")

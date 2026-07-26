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
    parse_turnover_statement,
)

FIXTURES = Path(__file__).parent / "fixtures" / "taxes"


def _po(name: str, filename: str, year: int = 2026):
    data = (FIXTURES / name).read_bytes()
    return parse_payment_order(data, filename=filename, default_year=year)


def _ved(name: str, filename: str):
    data = (FIXTURES / name).read_bytes()
    return parse_payroll_statement(data, filename=filename)


def _osv(name: str, filename: str):
    data = (FIXTURES / name).read_bytes()
    return parse_turnover_statement(data, filename=filename)


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


def test_enp_payroll_is_recognized_not_flagged() -> None:
    """Зарплатный ЕНП: вид enp_payroll распознан и НЕ уходит в «нужна проверка».

    Разнос НДФЛ/взносов делает оборотка бухгалтера (rebuild_payroll_enp_split), а не
    уведомление, поэтому платёжка-ЕНП тут справочная и ручного подтверждения не требует.
    """
    doc = _po("enp_payroll_14902.docx", "ЕНП_до 28.07.docx")

    assert doc.amount == Decimal("14902.30")
    assert doc.tax_kind == "enp_payroll"
    assert doc.needs_review is False
    assert not any("смешаны" in r for r in doc.review_reasons)


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


# ── сальдо-оборотные ведомости по зарплате (лист 'л1') ────────────────────────


def test_turnover_july_full_breakdown() -> None:
    """Оборотка июля: полная раскладка одной строки. Позиции колонок читаются несмотря на то,
    что подпись позиции «аванс» в июле — «УДЕРЖАНИЯ ПО КАССЕ» (заголовки «плавают»)."""
    doc = _osv("oborotka_07.xls", "ОБОРОТКА 07.xls")

    assert (doc.year, doc.month, doc.period_code) == (2026, 7, "2026-07")
    assert len(doc.rows) == 1
    row = doc.rows[0]
    assert row.tab_number == "206"
    assert row.employee == "ИВАНОВА И.И."
    assert row.oklad == Decimal("50000.00")
    assert row.ndfl == Decimal("6318.00")
    assert row.advance == Decimal("20986.00")
    assert row.contributions == Decimal("13595.93")
    assert row.injury == Decimal("100.00")
    assert row.deduction == Decimal("1400")
    assert row.to_pay == Decimal("22696.00")
    # Связность: начислено − НДФЛ − аванс = к выплате → разбор не жалуется.
    assert row.accrued - row.ndfl - row.advance == row.to_pay
    assert doc.needs_review is False


def test_turnover_may_two_employees_with_empty_cells() -> None:
    """Переходный май: две сотрудницы; у первой вычет и «к выплате» пусты (None ≠ 0)."""
    doc = _osv("oborotka_05.xls", "ОБОРОТКА 05.xls")

    assert doc.period_code == "2026-05"
    assert [r.tab_number for r in doc.rows] == ["205", "206"]
    first, second = doc.rows
    assert first.deduction is None and first.to_pay is None  # пустые ячейки, не нули
    assert first.ndfl == Decimal("1711.00")
    assert second.to_pay == Decimal("23077.00")
    assert doc.needs_review is False


def test_turnover_totals_sum_the_column() -> None:
    """Итоги по колонкам суммируют строки (строка «Итого» из файла в подсчёт не берётся)."""
    doc = _osv("oborotka_05.xls", "ОБОРОТКА 05.xls")

    assert doc.accrued_total == Decimal("39474.00")  # 13158 + 26316
    assert doc.ndfl_total == Decimal("4950.00")  # 1711 + 3239
    assert doc.contributions_total == Decimal("11842.20")  # 3947.40 + 7894.80


def test_turnover_enp_reference_matches_payment() -> None:
    """Оборотка июня — эталон для разноса зарплатного ЕНП: взносы за месяц уходят целиком,
    и вместе с НДФЛ образуют базу зарплатного ЕНП (травматизм платится отдельно в СФР)."""
    doc = _osv("oborotka_06.xls", "ОБОРОТКА 06.xls")

    assert doc.contributions_total == Decimal("8571.30")
    assert doc.ndfl_total == Decimal("3532.00")
    assert doc.injury_total == Decimal("57.14")  # мимо ЕНС, отдельным платежом в СФР


def test_non_turnover_xls_is_flagged_not_misread() -> None:
    """Платёжную ведомость Т-53 (лист 'T') парсер оборотки не принимает молча за оборотку."""
    doc = _osv("vedomost_advance_20986.xls", "ВЕД-13 АВАНС 20.07.xls")

    assert doc.rows == []
    assert doc.needs_review is True
    assert any("л1" in r for r in doc.review_reasons)


def test_ens_filename_is_payroll_enp_not_extra_contribution() -> None:
    """«ЕНС до 27.03» — зарплатный ЕНП, а не допвзнос 1%.

    Сверка с выпиской 27.07.2026: платёжке соответствует реальный платёж 26.03 на
    19 460,93 ₽ — зарплатный ЕНП за февраль. Классификатор относил её к допвзносу 1%
    («ЕНС» не было в паттернах), что исказило бы вычет УСН при продвижении.
    """
    from app.services.taxes.document_parser import _classify_kind

    assert _classify_kind("ЕНС до 27.03.docx", "") == "enp_payroll"
    assert _classify_kind("ЕНП до 28.05.docx", "") == "enp_payroll"
    # Допвзнос 1% по-прежнему узнаётся своим паттерном и не перехватывается «ЕНС».
    assert _classify_kind("1% за 2 кв 2026.docx", "") == "contrib_extra_1pct"

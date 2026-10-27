"""Границы дат и полный пакет документов: только синтетические данные, без БД и сети."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services import invoice_recognition as ir


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-10-27", date(2026, 10, 27)),
        ("2026-09-30", date(2026, 9, 30)),
        ("2026-01-28", date(2026, 1, 28)),
        ("2026-12-31", date(2026, 12, 31)),
        ("2024-02-29", date(2024, 2, 29)),
        ("27.10.2026", date(2026, 10, 27)),
        ("27-10-26", date(2026, 10, 27)),
        ("1/2/2026", date(2026, 2, 1)),
        ("27 октября 2026 г.", date(2026, 10, 27)),
    ],
)
def test_date_formats_parse_the_whole_value(raw: str, expected: date) -> None:
    assert ir._parse_date(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "2026-02-30", "2026-13-27", "2026-10-32", "2026-10-271", "12026-10-27",
        "x2026-10-27", "2026-10-27text", "027.10.2026", "27.10.20261", "27-10.2026",
    ],
)
def test_invalid_or_partial_date_tokens_are_not_reinterpreted(raw: str) -> None:
    assert ir._parse_date(raw) is None


@pytest.mark.parametrize(
    ("text", "start", "end"),
    [
        ("услуги за 3 кв. 2026 г.", date(2026, 7, 1), date(2026, 9, 30)),
        ("за III квартал 2026", date(2026, 7, 1), date(2026, 9, 30)),
        ("за 1-й квартал 2026", date(2026, 1, 1), date(2026, 3, 31)),
        ("Q2 2026", date(2026, 4, 1), date(2026, 6, 30)),
        ("2026 Q4", date(2026, 10, 1), date(2026, 12, 31)),
    ],
)
def test_explicit_quarter_is_a_calendar_period(text: str, start: date, end: date) -> None:
    periods = ir._extract_service_periods(text)
    assert [(p[0], p[1]) for p in periods] == [(start, end)]


BILL = """Счет на оплату № B-130 от 27.10.2026
ООО ТЕСТ ПОСТАВЩИК ИНН 1234567890
Обработка данных за 3 кв. 2026 г.
Всего к оплате 9 000,00
"""
ACT = """Акт выполненных работ № A-131 от 26.10.2026
ООО ТЕСТ ПОСТАВЩИК ИНН 1234567890
Обработка данных за 3 кв. 2026 г.
Всего к оплате 8 700,00
"""


@pytest.mark.parametrize("blocks", [(ACT, BILL), (BILL, ACT)])
def test_two_documents_on_one_page_keep_bill_and_own_closing_fields(blocks) -> None:
    result = ir.deterministic_recognize_pages(["\n".join(blocks)])
    assert result.document_kind == "invoice"
    assert result.invoice_number == "B-130"
    assert result.invoice_date == date(2026, 10, 27)
    assert result.amount == Decimal("9000")
    assert result.service_period_start == date(2026, 7, 1)
    assert result.service_period_end == date(2026, 9, 30)
    assert result.companion is not None
    assert result.companion.document_kind == "act"
    assert result.companion.invoice_number == "A-131"
    assert result.companion.invoice_date == date(2026, 10, 26)
    assert result.companion.amount == Decimal("8700")
    assert result.package_ambiguous is False


def test_invoice_reference_in_act_is_not_a_second_document() -> None:
    result = ir.deterministic_recognize_pages([
        ACT + "Основание: Счет на оплату № B-130 от 27.10.2026\n"
    ])
    assert result.document_kind == "act"
    assert result.companion is None


def _settings():
    return SimpleNamespace(
        anthropic_api_key="unused-test-key", invoice_recognition_model="unused-test-model",
        invoice_recognition_min_confidence=0.75,
    )


def _payload(kind="invoice", number="B-130", amount="9000", day="2026-10-27"):
    return {
        "is_invoice": True, "document_kind": kind, "invoice_number": number,
        "invoice_date": day, "amount": amount, "confidence": 1.0,
        "recipient_name": "ООО ТЕСТ ПОСТАВЩИК", "inn": "1234567890",
        "line_items": [{"description": "Обработка данных за 3 кв. 2026 г.", "amount": amount}],
        "service_duration_months": 3,
    }


async def _recognize_scan(monkeypatch, payload):
    async def call(*args, **kwargs):
        assert "companion" in kwargs["tool"]["input_schema"]["properties"]
        return payload

    monkeypatch.setattr(ir, "call_tool", call)
    monkeypatch.setattr(ir, "extract_pdf_pages", lambda pdf: [""])
    return await ir.recognize(b"synthetic-pdf", settings=_settings())


async def test_llm_scan_package_preserves_both_documents_and_iso_dates(monkeypatch) -> None:
    payload = {
        **_payload(), "has_multiple_documents": True,
        "companion": _payload("act", "A-131", "8700", "2026-10-26"),
    }
    result = await _recognize_scan(monkeypatch, payload)
    assert result.document_kind == "invoice"
    assert result.invoice_date == date(2026, 10, 27)
    assert result.service_period_start == date(2026, 7, 1)
    assert result.service_period_end == date(2026, 9, 30)
    assert result.companion is not None
    assert result.companion.invoice_number == "A-131"
    assert result.companion.invoice_date == date(2026, 10, 26)
    assert result.companion.amount == Decimal("8700")
    assert result.package_ambiguous is False


async def test_llm_reversed_package_is_normalized_to_primary_bill(monkeypatch) -> None:
    payload = {
        **_payload("act", "A-131", "8700", "2026-10-26"),
        "companion": _payload(),
    }
    result = await _recognize_scan(monkeypatch, payload)
    assert result.document_kind == "invoice"
    assert result.invoice_number == "B-130"
    assert result.companion.invoice_number == "A-131"
    assert result.companion.amount == Decimal("8700")


async def test_incomplete_scan_package_is_not_made_confident_by_requisites(monkeypatch) -> None:
    result = await _recognize_scan(monkeypatch, {
        **_payload("act"), "has_multiple_documents": True,
        "bank_acnt": "40700000000000000000", "bank_bik": "044000000",
        "corr_account": "30100000000000000000",
    })
    assert result.package_ambiguous is True
    assert result.review_reasons
    assert result.confidence == 0.0


async def test_companion_without_own_amount_never_inherits_bill_amount(monkeypatch) -> None:
    companion = _payload("act", "A-131")
    companion.pop("amount")
    result = await _recognize_scan(monkeypatch, {**_payload(), "companion": companion})
    assert result.companion is not None
    assert result.companion.amount is None
    assert result.package_ambiguous is True
    assert result.confidence == 0.0


async def test_invalid_llm_date_demands_review_without_year_guess(monkeypatch) -> None:
    result = await _recognize_scan(monkeypatch, _payload(day="2026-02-31"))
    assert result.invoice_date is None
    assert result.review_reasons
    assert result.confidence == 0.0


async def test_single_document_llm_payload_stays_compatible(monkeypatch) -> None:
    result = await _recognize_scan(monkeypatch, _payload())
    assert result.document_kind == "invoice"
    assert result.companion is None
    assert result.invoice_date == date(2026, 10, 27)
    assert result.confidence >= 0.75


def test_wrapped_basis_reference_does_not_create_a_bill() -> None:
    result = ir.deterministic_recognize_pages([
        ACT + "Основание:\nСчет № B-130 от 27.10.2026\n"
    ])
    assert result.document_kind == "act"
    assert result.companion is None


def test_bank_account_label_does_not_create_a_bill_in_act() -> None:
    result = ir.deterministic_recognize_pages([
        ACT + "Банковские реквизиты\nСчет № 40700000000000000000\n"
    ])
    assert result.document_kind == "act"
    assert result.companion is None


def test_reference_to_future_act_in_invoice_terms_is_not_a_package() -> None:
    result = ir.deterministic_recognize_pages([
        BILL + "Акт выполненных работ предоставляется после оказания услуг.\n"
    ])
    assert result.document_kind == "invoice"
    assert result.companion is None


def test_conflicting_quarters_require_a_period_choice() -> None:
    result = ir.deterministic_recognize(BILL + "Дополнительные услуги за 2 кв. 2026 г.\n")
    assert result.service_period_ambiguous is True
    assert result.service_period_start is None
    assert result.service_period_end is None


async def test_conflicting_package_recipients_require_review(monkeypatch) -> None:
    companion = {**_payload("act"), "inn": "9876543210"}
    result = await _recognize_scan(monkeypatch, {**_payload(), "companion": companion})
    assert result.package_ambiguous is True
    assert result.confidence == 0.0


@pytest.mark.parametrize("suffix", ["2026-10-27", "27.10.2026"])
def test_document_header_accepts_full_iso_or_dmy_date(suffix: str) -> None:
    assert ir._pick_invoice_date(f"Счет № A-1 от {suffix}") == date(2026, 10, 27)


@pytest.mark.parametrize(
    "token", ["27.10.20261", "2026-10-271", "12026-10-27", "27.10.2026x", "2026-10-27text"]
)
def test_document_header_does_not_truncate_long_date_tokens(token: str) -> None:
    assert ir._pick_invoice_date(f"Счет № A-1 от {token}") is None


@pytest.mark.parametrize("missing", ["invoice_number", "invoice_date"])
async def test_scan_companion_missing_own_identity_requires_review(monkeypatch, missing) -> None:
    companion = _payload("act", "A-131", "8700", "2026-10-26")
    companion.pop(missing)
    result = await _recognize_scan(monkeypatch, {**_payload(), "companion": companion})
    assert getattr(result.companion, missing) is None
    assert result.package_ambiguous is True
    assert result.confidence == 0.0


@pytest.mark.parametrize("include_act", [False, True])
@pytest.mark.parametrize("one_page", [False, True])
def test_distinct_bills_are_not_silently_merged(include_act, one_page) -> None:
    other_bill = BILL.replace("B-130", "B-132").replace("9 000,00", "8 000,00")
    pages = [BILL, other_bill] + ([ACT] if include_act else [])
    result = ir.deterministic_recognize_pages(["\n".join(pages)] if one_page else pages)
    assert result.package_ambiguous is True
    assert result.review_reasons
    assert result.confidence == 0.0


def test_repeated_invoice_header_keeps_a_single_document() -> None:
    result = ir.deterministic_recognize_pages([BILL, BILL])
    assert result.package_ambiguous is False
    assert result.companion is None


@pytest.mark.parametrize("token", ["27.10.2026г.", "27.10.2026 г.", "27.10.2026."])
def test_document_header_keeps_common_year_suffix_and_punctuation(token: str) -> None:
    assert ir._pick_invoice_date(f"Счет № A-1 от {token}") == date(2026, 10, 27)


def test_ambiguous_closing_period_holds_the_entire_package_for_review() -> None:
    conflicting_act = ACT + "Дополнительные услуги за 2 кв. 2026 г.\n"
    result = ir.deterministic_recognize_pages([BILL, conflicting_act])
    assert result.service_period_start == date(2026, 7, 1)
    assert result.companion.service_period_ambiguous is True
    assert result.companion.service_period_start is None
    assert result.package_ambiguous is True
    assert result.confidence == 0.0


@pytest.mark.parametrize(
    "text",
    [
        "Акт сверки № 7062 за 01.01.2026-31.03.2026 между сторонами "
        "по договору 0101/02429 от 11.01.2021",
        "АКТ СВЕРКИ за 01.07.2026-30.09.2026 на дату 30.09.2026\n"
        "Представитель по доверенности б/н от\n15.01.2025г.",
    ],
)
def test_reference_dates_do_not_replace_absent_document_date(text: str) -> None:
    assert ir._pick_invoice_date(text) is None


def test_invoice_contract_keeps_its_own_date_after_older_reference() -> None:
    text = (
        "Услуги по договору № OLD от 11.01.2021\n"
        "Счет-договор № BILL-1 от 27.10.2026"
    )
    assert ir._pick_invoice_date(text) == date(2026, 10, 27)

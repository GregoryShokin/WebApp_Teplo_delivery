"""Ручная загрузка обычного счёта → предложения → полная карточка → один bank draft."""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from cp_helpers import make_counterparty, make_invoice
from sqlalchemy import func, select

from app.core.config import get_settings
from app.models import (
    Counterparty,
    CounterpartyPayableProfile,
    CounterpartyPaymentDraft,
    DdsArticle,
    SupplierInvoice,
)
from app.services import invoice_recognition, invoice_review, utility_intake, utility_ocr
from app.services.anthropic_client import LlmCallError
from app.services.banking.base import PaymentDraftResult
from app.services.banking.exceptions import BankFetchError
from app.services.counterparty_payments import (
    CounterpartyPaymentError,
    create_payment_draft_for_invoices,
)
from app.services.email_invoice_ingest import confirm_intake_with_review

TEXT = (
    Path(__file__).resolve().parents[1] / "fixtures/invoice-review-subscription.txt"
).read_text()
REQ = {
    "recipientName": 'ООО "Ревви"',
    "inn": "7811751392",
    "kpp": "781101001",
    "bankAcnt": "40702810400000012349",
    "bankBik": "044525225",
    "recipientCorrAccountNumber": "30101810400000000225",
}


def test_duration_is_not_a_start_date_and_one_ruble_line_is_kept():
    rec = invoice_recognition.deterministic_recognize(TEXT)
    invoice_review.add_duration_evidence(rec, TEXT)
    assert rec.amount == Decimal("59941.00")
    assert rec.service_duration_months == 6
    assert len(rec.line_items) == 2
    assert rec.line_items[1]["amount"] == "1,00"
    assert rec.service_period_start is None and rec.service_period_end is None
    assert rec.vat_mode == "none"


async def upload(session, monkeypatch):
    async def extract(*args, **kwargs):
        return TEXT, "pdf_text"

    monkeypatch.setattr(utility_ocr, "extract_text", extract)
    monkeypatch.setattr(invoice_recognition, "extract_pdf_pages", lambda _: [TEXT])
    (intake,) = await utility_intake.ingest_document(
        session, content=b"%PDF-1.4 preview", filename="subscription.pdf", settings=get_settings()
    )
    return intake


async def test_upload_proposes_without_creating_counterparty_or_invoice(
    async_session_factory, monkeypatch
):
    async def no_llm(*args, **kwargs):
        pytest.fail("Внешняя модель при выключенном разрешении вызываться не должна")

    monkeypatch.setattr(invoice_recognition, "llm_recognize", no_llm)
    async with async_session_factory() as session:
        intake = await upload(session, monkeypatch)
        assert intake.recognition["amount"] == "59941.00"
        assert intake.recognition["service_duration_months"] == 6
        assert intake.counterparty_id is None and intake.invoice_id is None
        assert await session.scalar(select(func.count()).select_from(Counterparty)) == 0
        assert await session.scalar(select(func.count()).select_from(SupplierInvoice)) == 0


async def test_full_confirmation_is_active_and_rerecognition_preserves_manual_fields(
    async_session_factory, monkeypatch
):
    async with async_session_factory() as session:
        intake = await upload(session, monkeypatch)
        article = await session.scalar(
            select(DdsArticle).where(
                DdsArticle.is_active.is_(True), DdsArticle.movement_type == "outflow"
            )
        )
        await confirm_intake_with_review(
            session,
            intake,
            actor_user_id=None,
            new_counterparty_name='ООО "Ревви"',
            new_counterparty_inn=REQ["inn"],
            amount="59941.00",
            invoice_number="MANUAL-01",
            invoice_date="2026-09-23",
            service_period_start="2026-10-01",
            service_period_end="2027-03-31",
            vat_amount="",
            requisites=REQ,
            apply_requisites=True,
            setup_counterparty=True,
            dds_article_id=article.id,
            service_billing_mode="fixed_tariff",
            document_kind="invoice",
        )
        await session.commit()
        cp = await session.get(Counterparty, intake.counterparty_id)
        profile = await session.scalar(
            select(CounterpartyPayableProfile).where(
                CounterpartyPayableProfile.counterparty_id == cp.id
            )
        )
        invoice = await session.get(SupplierInvoice, intake.invoice_id)
        assert cp.status == "active" and cp.origin == "manual"
        assert profile.requisites_verified and profile.service_billing_mode == "fixed_tariff"
        assert invoice.service_period_end == date(2027, 3, 31)
        await invoice_review.recognize_intake(session, intake, settings=get_settings())
        assert intake.recognition["invoice_number"] == "MANUAL-01"
        assert intake.recognition["service_period_start"] == "2026-10-01"
        assert intake.recognition["requisites_reviewed"] == REQ


async def test_bad_period_rolls_back_card_and_verification(async_session_factory, monkeypatch):
    async with async_session_factory() as session:
        intake = await upload(session, monkeypatch)
        await session.commit()
        with pytest.raises(ValueError, match="обе даты"):
            await confirm_intake_with_review(
                session,
                intake,
                actor_user_id=None,
                new_counterparty_name="Новая карточка",
                amount="59941.00",
                service_period_start="2026-10-01",
                requisites=REQ,
                apply_requisites=True,
            )
        await session.rollback()
        assert await session.scalar(select(func.count()).select_from(Counterparty)) == 0


async def test_existing_inn_reuses_card(async_session_factory, monkeypatch):
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Ревви в реестре", inn=REQ["inn"])
        intake = await upload(session, monkeypatch)
        assert intake.counterparty_id == cp.id
        await confirm_intake_with_review(
            session,
            intake,
            actor_user_id=None,
            new_counterparty_name="Не создавать второй",
            new_counterparty_inn=REQ["inn"],
            amount="59941.00",
        )
        assert intake.counterparty_id == cp.id
        assert await session.scalar(select(func.count()).select_from(Counterparty)) == 1


async def test_llm_can_complete_duration_without_inventing_period(monkeypatch):
    async def model(*args, **kwargs):
        return invoice_recognition.RecognizedInvoice(service_duration_months=6, engine="llm")

    monkeypatch.setattr(invoice_recognition, "llm_recognize", model)
    monkeypatch.setattr(invoice_recognition, "extract_pdf_pages", lambda _: [TEXT])
    settings = SimpleNamespace(
        anthropic_api_key="test-key", invoice_recognition_min_confidence=0.75
    )
    rec = await invoice_recognition.recognize(b"test", settings=settings, force_llm=True)
    assert rec.service_duration_months == 6 and rec.service_period_start is None


async def test_timeout_retry_uses_same_bank_identity_and_changed_payment_is_blocked(
    async_session_factory,
):
    calls = []

    class Bank:
        async def create_payment_draft(self, **kwargs):
            calls.append(kwargs["document_id"])
            if len(calls) == 1:
                raise BankFetchError("tbank", "timeout after acceptance")
            return PaymentDraftResult(
                status="created", document_id=kwargs["document_id"], provider_ref="mock-same"
            )

    bank = Bank()
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session,
            name=REQ["recipientName"],
            inn=REQ["inn"],
            requisites=REQ,
            requisites_verified=True,
        )
        invoice = await make_invoice(
            session, counterparty_id=cp.id, amount="59941.00", number="SUB-6"
        )
        await session.commit()
        with pytest.raises(BankFetchError):
            await create_payment_draft_for_invoices(
                session, invoice_ids=[invoice.id], actor_user_id=None, bank_client=bank
            )
        invoice.amount = Decimal("100.00")
        await session.commit()
        with pytest.raises(CounterpartyPaymentError, match="данные платежа изменились"):
            await create_payment_draft_for_invoices(
                session, invoice_ids=[invoice.id], actor_user_id=None, bank_client=bank
            )
        invoice_id = invoice.id
        await session.rollback()
        invoice = await session.get(SupplierInvoice, invoice_id)
        invoice.amount = Decimal("59941.00")
        await session.commit()
        draft = await create_payment_draft_for_invoices(
            session, invoice_ids=[invoice.id], actor_user_id=None, bank_client=bank
        )
        assert len(calls) == 2 and calls[0] == calls[1]
        assert draft.last_error is None
        assert await session.scalar(select(func.count()).select_from(CounterpartyPaymentDraft)) == 1


async def test_sent_invoice_cannot_be_changed(async_session_factory, monkeypatch):
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session,
            name=REQ["recipientName"],
            inn=REQ["inn"],
            requisites=REQ,
            requisites_verified=True,
        )
        intake = await upload(session, monkeypatch)
        await confirm_intake_with_review(
            session, intake, actor_user_id=None, counterparty_id=cp.id, amount="59941.00"
        )
        await session.commit()
        invoice_id = intake.invoice_id
        await create_payment_draft_for_invoices(
            session, invoice_ids=[invoice_id], actor_user_id=None
        )
        with pytest.raises(ValueError, match="данные нельзя менять"):
            await confirm_intake_with_review(session, intake, actor_user_id=None, amount="100.00")
        await session.rollback()
        invoice = await session.get(SupplierInvoice, invoice_id)
        assert invoice.amount == Decimal("59941.00")


async def test_concurrent_clicks_create_one_draft(async_session_factory):
    calls = []

    class Bank:
        async def create_payment_draft(self, **kwargs):
            calls.append(kwargs["document_id"])
            await asyncio.sleep(0.05)
            return PaymentDraftResult(document_id=kwargs["document_id"], status="created")

    async with async_session_factory() as session:
        cp = await make_counterparty(
            session,
            name=REQ["recipientName"],
            inn=REQ["inn"],
            requisites=REQ,
            requisites_verified=True,
        )
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="59941.00")
        invoice_id = invoice.id
        await session.commit()

    async def send():
        async with async_session_factory() as session:
            return await create_payment_draft_for_invoices(
                session, invoice_ids=[invoice_id], actor_user_id=None, bank_client=Bank()
            )

    results = await asyncio.gather(send(), send(), return_exceptions=True)
    assert len(calls) == 1
    assert sum(isinstance(result, CounterpartyPaymentError) for result in results) == 1


async def test_model_failure_keeps_local_fields(monkeypatch):
    async def fail(*args, **kwargs):
        raise LlmCallError("synthetic timeout")

    monkeypatch.setattr(invoice_recognition, "call_tool", fail)
    monkeypatch.setattr(invoice_recognition, "extract_pdf_pages", lambda _: [TEXT])
    settings = get_settings().model_copy(update={"anthropic_api_key": "test-only-key"})
    rec = await invoice_recognition.recognize(b"%PDF-test", settings=settings, force_llm=True)
    assert rec.amount == Decimal("59941.00") and rec.inn == REQ["inn"]
    assert any("не завершила" in note for note in rec.notes)


async def test_scan_gets_structured_proposals_without_invented_dates(monkeypatch):
    async def model(*args, **kwargs):
        return {
            "is_invoice": True,
            "document_kind": "invoice",
            "confidence": 0.95,
            "recipient_name": REQ["recipientName"],
            "inn": REQ["inn"],
            "amount": "59941.00",
            "vat_mode": "none",
            "service_duration_months": 6,
            "line_items": [
                {"description": "Подписка", "quantity": "6", "unit": "мес", "amount": "59940.00"},
                {"description": "Пробный доступ", "quantity": "1", "unit": "мес", "amount": "1.00"},
            ],
        }

    monkeypatch.setattr(invoice_recognition, "call_tool", model)
    monkeypatch.setattr(invoice_recognition, "extract_pdf_pages", lambda _: [""])
    settings = get_settings().model_copy(update={"anthropic_api_key": "test-only-key"})
    rec = await invoice_recognition.recognize(b"%PDF-scan-test", settings=settings, force_llm=True)
    assert rec.engine == "llm" and rec.service_duration_months == 6
    assert len(rec.line_items) == 2 and rec.service_period_start is None

"""Push of a warehouse invoice into iiko: XML builder + prepare (no network).

The live send (POST to iiko) is not exercised here — it creates a real document.
These tests cover the deterministic parts: XML structure per direction, exclusion of
«персонал» lines, and the skip reasons (no iiko GUID / no store / no товарные lines).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from cp_helpers import make_counterparty
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import InvoiceLineItem, SupplierInvoice
from app.services.warehouse_invoice_push import (
    PushLine,
    _parse_push_result,
    build_invoice_xml,
    prepare_push,
)

ISSUED = datetime(2026, 6, 15, 14, 30, tzinfo=UTC)


def test_build_xml_incoming_payable() -> None:
    xml = build_invoice_xml(
        doc_id="D1",
        direction="payable",
        partner_guid="SUP-1",
        number="W-1",
        incoming_date="2026-06-15",
        date_incoming="2026-06-15T14:30:00",
        store_guid="ST-1",
        lines=[
            PushLine(
                "P1", "2", "500", "1000", num=1, unit_guid="U1",
                vat_percent="20", vat_sum="166.67",
            )
        ],
    )
    assert xml.startswith("<document>")  # single document, NOT the list wrapper
    assert "<incomingInvoiceDtoes>" not in xml
    assert "<supplier>SUP-1</supplier>" in xml
    assert "<defaultStore>ST-1</defaultStore>" in xml
    assert "<incomingDate>2026-06-15</incomingDate>" in xml
    assert "<num>1</num>" in xml
    assert "<product>P1</product>" in xml
    assert "<amountUnit>U1</amountUnit>" in xml
    assert "<store>ST-1</store>" in xml
    assert "<vatPercent>20</vatPercent>" in xml
    assert "<id>D1</id>" in xml


def test_build_xml_outgoing_receivable() -> None:
    xml = build_invoice_xml(
        doc_id="D2",
        direction="receivable",
        partner_guid="CA-1",
        number="W-2",
        incoming_date="2026-06-15",
        date_incoming="2026-06-15T14:30:00",
        store_guid="ST-1",
        lines=[PushLine("P1", "1", "100", "100", num=1)],
    )
    assert xml.startswith("<document>")
    assert "<outgoingInvoiceDtoes>" not in xml
    assert "<counteragentId>CA-1</counteragentId>" in xml
    assert "<vatPercent>" not in xml  # no VAT given → tag omitted


def test_parse_push_result_rejected_on_http_200() -> None:
    """iiko returns HTTP 200 with <valid>false</valid> when it rejects the document
    (e.g. re-pushing a PROCESSED number). Captured from a real iiko reply."""
    body = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b"<documentValidationResult><valid>false</valid><warning>false</warning>"
        b"<documentNumber>TESTPUSH-1</documentNumber>"
        b"<errorMessage>Changing processed or deleted document is not allowed: "
        b"existing #TESTPUSH-1 of 15.06.2026 16:06 is in status PROCESSED</errorMessage>"
        b"</documentValidationResult>"
    )
    res = _parse_push_result(body)
    assert res.valid is False
    assert res.error is not None and "not allowed" in res.error
    assert res.doc_number == "TESTPUSH-1"


def test_parse_push_result_accepted() -> None:
    body = (
        b"<documentValidationResult><valid>true</valid><warning>false</warning>"
        b"<documentNumber>W-1</documentNumber></documentValidationResult>"
    )
    res = _parse_push_result(body)
    assert res.valid is True
    assert res.error is None
    assert res.doc_number == "W-1"


def test_parse_push_result_unparseable_is_failure() -> None:
    res = _parse_push_result(b"<<not xml")
    assert res.valid is False
    assert res.error is not None


async def _invoice_with_lines(
    session: AsyncSession, cp_id, *, store_guid: str | None, staff_second: bool
) -> SupplierInvoice:
    invoice = SupplierInvoice(
        counterparty_id=cp_id,
        source="manual",
        direction="payable",
        number="W-10",
        amount=Decimal("1500.00"),
        payment_status="unpaid",
        issued_at=ISSUED,
        store_guid=store_guid,
    )
    session.add(invoice)
    await session.flush()
    session.add(
        InvoiceLineItem(
            invoice_id=invoice.id, product_guid="PROD-A", name="Лосось",
            quantity=Decimal("2"), price=Decimal("500"), sum=Decimal("1000"),
            is_staff=False, sort_order=0,
        )
    )
    session.add(
        InvoiceLineItem(
            invoice_id=invoice.id, product_guid="PROD-B", name="Печенье",
            quantity=Decimal("1"), price=Decimal("500"), sum=Decimal("500"),
            is_staff=staff_second, sort_order=1,
        )
    )
    await session.commit()
    return invoice


async def test_prepare_push_excludes_staff_lines(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Поставщик", inn="7710000050", iiko_guid="SUP-GUID-1"
        )
        invoice = await _invoice_with_lines(session, cp.id, store_guid="ST-1", staff_second=True)

        prepared = await prepare_push(session, invoice)
        assert prepared.skip_reason is None
        assert prepared.xml is not None
        assert "<supplier>SUP-GUID-1</supplier>" in prepared.xml
        assert "PROD-A" in prepared.xml
        assert "PROD-B" not in prepared.xml  # персонал-строка не уходит в iiko


async def test_prepare_push_skips_without_iiko_guid(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Без GUID", inn="7710000051")
        invoice = await _invoice_with_lines(session, cp.id, store_guid="ST-1", staff_second=False)

        prepared = await prepare_push(session, invoice)
        assert prepared.xml is None
        assert prepared.skip_reason is not None and "GUID" in prepared.skip_reason


async def test_prepare_push_skips_without_store(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Поставщик2", inn="7710000052", iiko_guid="SUP-GUID-2"
        )
        invoice = await _invoice_with_lines(session, cp.id, store_guid=None, staff_second=False)

        prepared = await prepare_push(session, invoice)
        assert prepared.xml is None
        assert prepared.skip_reason is not None and "склад" in prepared.skip_reason


async def test_prepare_push_skips_all_staff(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Накладная целиком из персонал-строк → в iiko уходить нечему → skip."""
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Поставщик3", inn="7710000053", iiko_guid="SUP-GUID-3"
        )
        invoice = SupplierInvoice(
            counterparty_id=cp.id, source="manual", direction="payable", number="W-11",
            amount=Decimal("500.00"), payment_status="unpaid", issued_at=ISSUED, store_guid="ST-1",
        )
        session.add(invoice)
        await session.flush()
        session.add(
            InvoiceLineItem(
                invoice_id=invoice.id, product_guid="PROD-S", name="Печенье",
                quantity=Decimal("1"), price=Decimal("500"), sum=Decimal("500"),
                is_staff=True, sort_order=0,
            )
        )
        await session.commit()

        prepared = await prepare_push(session, invoice)
        assert prepared.xml is None
        assert prepared.skip_reason is not None

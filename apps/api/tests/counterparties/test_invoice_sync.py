"""iiko → SupplierInvoice ingest: idempotency, parsing, skips, brand routing.

Drives :func:`ingest_iiko_payables` against captured XML (no network), exercising
the parse + upsert pipeline on the real ``teplo_test`` schema.
"""

from __future__ import annotations

from decimal import Decimal

from cp_helpers import (
    add_routing_rule,
    invoices_xml,
    make_counterparty,
    make_invoice,
    outgoing_invoices_xml,
    suppliers_xml,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    Counterparty,
    CounterpartyAlias,
    CounterpartyPayableProfile,
    SupplierInvoice,
)
from app.services.counterparty_invoice_sync import ingest_iiko_payables

SUPPLIER_GUID = "guid-amay-001"


def _one_supplier(*, inn: str | None = "7701234567", **extra) -> str:
    return suppliers_xml([{"id": SUPPLIER_GUID, "name": "Поставщик Амай", "inn": inn, **extra}])


def _doc(**overrides) -> dict:
    doc = {
        "id": "doc-1",
        "supplier": SUPPLIER_GUID,
        "status": "PROCESSED",
        "document_number": "СЧ-001",
        "incoming_date": "2026-06-01",
        "items": [
            {"sum": "110.00", "vat_percent": "10", "vat_sum": "10.00"},
            {"sum": "122.00", "vat_percent": "22", "vat_sum": "22.00"},
        ],
    }
    doc.update(overrides)
    return doc


async def _count(session: AsyncSession, model) -> int:
    return await session.scalar(select(func.count()).select_from(model))


async def test_ingest_creates_invoice_with_gross_amount_and_grouped_vat(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        result = await ingest_iiko_payables(
            session, suppliers_xml=_one_supplier(), invoices_xml=invoices_xml([_doc()])
        )
        await session.commit()

        assert result.invoices_seen == 1
        assert result.invoices_created == 1
        assert result.counterparties_created == 1

        invoice = await session.scalar(select(SupplierInvoice))
        assert invoice is not None
        assert invoice.source == "iiko"
        assert invoice.external_id == "doc-1"
        assert invoice.amount == Decimal("232.00")  # gross item sums, VAT inclusive
        assert invoice.vat_total == Decimal("32.00")
        assert invoice.vat_breakdown == {"10": "10.00", "22": "22.00"}
        assert invoice.payment_status == "unpaid"

        # Auto-created counterparty needs operator setup and carries the iiko link.
        counterparty = await session.get(Counterparty, invoice.counterparty_id)
        assert counterparty is not None
        assert counterparty.status == "requires_setup"
        alias = await session.scalar(
            select(CounterpartyAlias).where(CounterpartyAlias.counterparty_id == counterparty.id)
        )
        assert alias is not None and alias.alias == SUPPLIER_GUID


async def test_ingest_is_idempotent_by_source_external_id(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        suppliers, invoices = _one_supplier(), invoices_xml([_doc()])

        first = await ingest_iiko_payables(session, suppliers_xml=suppliers, invoices_xml=invoices)
        await session.commit()
        second = await ingest_iiko_payables(session, suppliers_xml=suppliers, invoices_xml=invoices)
        await session.commit()

        assert first.invoices_created == 1
        assert second.invoices_created == 0
        assert second.invoices_updated == 1
        # No duplicate row and no duplicate auto-created counterparty.
        assert await _count(session, SupplierInvoice) == 1
        assert await _count(session, Counterparty) == 1


async def test_ingest_skips_unposted_status(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        result = await ingest_iiko_payables(
            session,
            suppliers_xml=_one_supplier(),
            invoices_xml=invoices_xml([_doc(status="NEW")]),
        )
        await session.commit()
        assert result.skipped_status == 1
        assert result.invoices_created == 0
        assert await _count(session, SupplierInvoice) == 0


async def test_ingest_skips_zero_amount_gift(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A 0₽ document (iiko «подарок»/bonus, no priced items) is not a payable — skip it
    entirely so it never lands in the inbox, and create no counterparty for it."""
    async with async_session_factory() as session:
        result = await ingest_iiko_payables(
            session,
            suppliers_xml=_one_supplier(),
            invoices_xml=invoices_xml([_doc(items=[])]),
        )
        await session.commit()
        assert result.skipped_zero_amount == 1
        assert result.invoices_created == 0
        assert await _count(session, SupplierInvoice) == 0
        assert await _count(session, Counterparty) == 0  # no phantom supplier for a gift


async def test_ingest_skips_store_and_unknown_and_idless(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        suppliers = suppliers_xml(
            [
                {"id": SUPPLIER_GUID, "name": "Поставщик", "inn": "7701234567"},
                {"id": "store-guid", "name": "Склад", "represents_store": True},
            ]
        )
        documents = invoices_xml(
            [
                _doc(id="store-doc", supplier="store-guid"),  # represents_store → skip
                _doc(id="orphan-doc", supplier="missing-guid"),  # unknown supplier → skip
                _doc(id=""),  # no external id → skip
            ]
        )
        result = await ingest_iiko_payables(
            session, suppliers_xml=suppliers, invoices_xml=documents
        )
        await session.commit()

        assert result.skipped_store == 1
        assert result.skipped_unknown_supplier == 1
        assert result.skipped_no_id == 1
        assert result.invoices_created == 0
        assert await _count(session, SupplierInvoice) == 0


async def test_ingest_update_never_overrides_paid_status_or_amount(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        await ingest_iiko_payables(
            session, suppliers_xml=_one_supplier(), invoices_xml=invoices_xml([_doc()])
        )
        await session.commit()
        invoice = await session.scalar(select(SupplierInvoice))
        invoice.payment_status = "paid"  # simulate a completed payment on our side
        await session.commit()

        # iiko re-exports the same doc with a different total.
        changed = _doc(items=[{"sum": "999.00", "vat_percent": "22", "vat_sum": "180.16"}])
        await ingest_iiko_payables(
            session, suppliers_xml=_one_supplier(), invoices_xml=invoices_xml([changed])
        )
        await session.commit()

        await session.refresh(invoice)
        assert invoice.payment_status == "paid"  # never reverted by sync
        assert invoice.amount == Decimal("232.00")  # amount frozen once not unpaid


async def test_ingest_refreshes_amount_while_unpaid(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        await ingest_iiko_payables(
            session, suppliers_xml=_one_supplier(), invoices_xml=invoices_xml([_doc()])
        )
        await session.commit()

        changed = _doc(items=[{"sum": "500.00", "vat_percent": "10", "vat_sum": "45.45"}])
        await ingest_iiko_payables(
            session, suppliers_xml=_one_supplier(), invoices_xml=invoices_xml([changed])
        )
        await session.commit()

        invoice = await session.scalar(select(SupplierInvoice))
        assert invoice.amount == Decimal("500.00")
        assert invoice.vat_breakdown == {"10": "45.45"}


async def test_ingest_routes_one_brand_to_legal_entities_by_prefix(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Амай: one iiko supplier GUID → ООО ТОРА (ТРКА…) / ИП Скачкова (0ЭКА…)."""
    async with async_session_factory() as session:
        tora = await make_counterparty(session, name="ООО ТОРА", inn="7700000001")
        skachkova = await make_counterparty(session, name="ИП Скачкова", inn="770000000002")
        await add_routing_rule(
            session, iiko_supplier_guid=SUPPLIER_GUID, prefix="ТРКА", counterparty_id=tora.id
        )
        await add_routing_rule(
            session,
            iiko_supplier_guid=SUPPLIER_GUID,
            prefix="0ЭКА",
            counterparty_id=skachkova.id,
        )
        await session.commit()

        documents = invoices_xml(
            [
                _doc(id="tora-doc", transport_invoice_number="ТРКА-0001"),
                _doc(id="skachkova-doc", transport_invoice_number="0ЭКА-0002"),
            ]
        )
        result = await ingest_iiko_payables(
            session, suppliers_xml=_one_supplier(), invoices_xml=documents
        )
        await session.commit()

        assert result.invoices_created == 2
        # Routing bypassed supplier auto-resolution → no new requires_setup counterparty.
        assert result.counterparties_created == 0

        by_external = {
            inv.external_id: inv.counterparty_id
            for inv in (await session.scalars(select(SupplierInvoice))).all()
        }
        assert by_external["tora-doc"] == tora.id
        assert by_external["skachkova-doc"] == skachkova.id


BARTER_GUID = "guid-barter-001"


def _barter_supplier() -> str:
    return suppliers_xml([{"id": BARTER_GUID, "name": "Ёбидоёби", "inn": "7706666666"}])


def _outgoing_doc(**overrides) -> dict:
    doc = {
        "id": "ar-doc-1",
        "counteragent": BARTER_GUID,
        "status": "PROCESSED",
        "document_number": "0015",
        "incoming_date": "2026-06-01",
        "items": [{"sum": "1000.00", "vat_percent": "10", "vat_sum": "90.91"}],
    }
    doc.update(overrides)
    return doc


async def test_ingest_outgoing_creates_receivable_and_marks_barter(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        result = await ingest_iiko_payables(
            session,
            suppliers_xml=_barter_supplier(),
            invoices_xml=invoices_xml([]),  # no incoming this run
            outgoing_invoices_xml=outgoing_invoices_xml([_outgoing_doc()]),
        )
        await session.commit()

        assert result.receivables_seen == 1
        assert result.receivables_created == 1
        assert result.invoices_created == 0

        invoice = await session.scalar(select(SupplierInvoice))
        assert invoice.direction == "receivable"
        assert invoice.amount == Decimal("1000.00")

        # An outgoing invoice ⇒ the counterparty is a barter partner.
        profile = await session.scalar(
            select(CounterpartyPayableProfile).where(
                CounterpartyPayableProfile.counterparty_id == invoice.counterparty_id
            )
        )
        assert profile.relationship == "barter"


async def test_reverse_sync_skips_our_own_pushed_manual_invoice(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A manually-created warehouse invoice pushed into iiko keeps ``source='manual'`` but
    gets an ``external_id`` from the iiko response. The reverse incoming-invoice sync must
    recognise it by that external_id and skip it — not create a second iiko-sourced clone
    that would re-appear unpaid in the inbox and double-count the obligation."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Наш поставщик", inn="7708888888")
        await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="232.00",
            number="СЧ-OUR-1",
            source="manual",
            external_id="doc-1",  # = iiko doc id assigned on push
            payment_status="paid",
        )
        await session.commit()

        # iiko re-exports the same document (id="doc-1") on the next reverse sync run.
        result = await ingest_iiko_payables(
            session, suppliers_xml=_one_supplier(), invoices_xml=invoices_xml([_doc()])
        )
        await session.commit()

        assert result.skipped_own_pushed == 1
        assert result.invoices_created == 0
        assert result.counterparties_created == 0  # no phantom supplier for our own doc
        # Still exactly one invoice — our manual one — with payment state untouched.
        assert await _count(session, SupplierInvoice) == 1
        only = await session.scalar(select(SupplierInvoice))
        assert only.source == "manual"
        assert only.payment_status == "paid"


async def test_reverse_sync_skips_our_own_pushed_kassa_invoice(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Накладная из Кассы (source='kassa_invoice'), запушенная в iiko, тоже «наша» —
    обратный синк должен узнавать её по external_id и пропускать, а не плодить iiko-клон.
    Регресс: дедуп проверял только source='manual' → Кассо-накладные дублировались."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Наш поставщик-Касса", inn="7708888899")
        await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="232.00",
            number="K-OUR-1",
            source="kassa_invoice",
            external_id="doc-1",  # = iiko doc id assigned on push
            payment_status="unpaid",
        )
        await session.commit()

        result = await ingest_iiko_payables(
            session, suppliers_xml=_one_supplier(), invoices_xml=invoices_xml([_doc()])
        )
        await session.commit()

        assert result.skipped_own_pushed == 1
        assert result.invoices_created == 0
        assert await _count(session, SupplierInvoice) == 1
        only = await session.scalar(select(SupplierInvoice))
        assert only.source == "kassa_invoice"


async def test_reverse_sync_shared_number_prefers_exact_external_id_match(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Несколько Кассо-накладных с ОДНИМ номером (напр. «4»): одна уже привязана к iiko-доку
    (external_id), другая ещё нет. Синк должен дедупить по ТОЧНОМУ external_id и НЕ пытаться
    backfill-ить занятый external_id на однономерную накладную (иначе UniqueViolation).
    Регресс прод-инцидента после добавления kassa_invoice в OUR_PUSHED_SOURCES."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Местный закуп", inn="7706660001")
        # Уже привязана к iiko-документу doc-1 (= _doc()).
        await make_invoice(
            session, counterparty_id=cp.id, amount="4636.00", number="4",
            source="kassa_invoice", external_id="doc-1", payment_status="unpaid",
        )
        # Другая накладная с тем же номером «4», но без external_id (другая покупка).
        await make_invoice(
            session, counterparty_id=cp.id, amount="7744.51", number="4",
            source="kassa_invoice", external_id=None, payment_status="paid",
        )
        await session.commit()

        result = await ingest_iiko_payables(
            session, suppliers_xml=_one_supplier(), invoices_xml=invoices_xml([_doc()])
        )
        await session.commit()

        assert result.skipped_own_pushed == 1  # дедуплено по точному external_id
        assert result.invoices_created == 0
        assert await _count(session, SupplierInvoice) == 2  # ничего не создано/не сломано
        # external_id занятой накладной НЕ перенесён на однономерную (она осталась NULL).
        other = await session.scalar(
            select(SupplierInvoice).where(SupplierInvoice.amount == 7744.51)
        )
        assert other.external_id is None


async def test_reverse_sync_dedupes_pushed_manual_by_number_when_external_id_null(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """If the post-push id lookup failed (external_id still NULL), the reverse sync must still
    recognise our pushed manual invoice by documentNumber, skip it, and backfill external_id —
    otherwise it would clone the document as a second unpaid obligation."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Наш поставщик", inn="7707777777")
        await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="232.00",
            number="СЧ-001",  # = documentNumber iiko echoes back in _doc()
            source="manual",
            external_id=None,  # id lookup failed on push
            payment_status="paid",
        )
        await session.commit()

        result = await ingest_iiko_payables(
            session, suppliers_xml=_one_supplier(), invoices_xml=invoices_xml([_doc()])
        )
        await session.commit()

        assert result.skipped_own_pushed == 1
        assert result.invoices_created == 0
        assert await _count(session, SupplierInvoice) == 1
        only = await session.scalar(select(SupplierInvoice))
        assert only.source == "manual"
        assert only.external_id == "doc-1"  # backfilled from the iiko document id
        assert only.payment_status == "paid"


async def test_ingest_outgoing_is_idempotent(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        suppliers = _barter_supplier()
        outgoing = outgoing_invoices_xml([_outgoing_doc()])
        empty_incoming = invoices_xml([])

        first = await ingest_iiko_payables(
            session,
            suppliers_xml=suppliers,
            invoices_xml=empty_incoming,
            outgoing_invoices_xml=outgoing,
        )
        await session.commit()
        second = await ingest_iiko_payables(
            session,
            suppliers_xml=suppliers,
            invoices_xml=empty_incoming,
            outgoing_invoices_xml=outgoing,
        )
        await session.commit()

        assert first.receivables_created == 1
        assert second.receivables_created == 0
        assert second.receivables_updated == 1
        assert await _count(session, SupplierInvoice) == 1

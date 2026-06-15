"""Push a manually-created warehouse invoice INTO iiko (two-way integration, Phase 3).

iiko stays the stock-accounting source, so invoices created in our system are sent to
iiko as documents: payable → ``POST /documents/import/incomingInvoice`` (``<supplier>``),
receivable → ``.../outgoingInvoice`` (``<counteragentId>``). Lines flagged «персонал» are
omitted from the pushed document (they are a separate cash-flow, not iiko stock). The
partner GUID comes from ``CounterpartyAlias`` (source=iiko); the store GUID from the
invoice or the ``iiko.default_store_guid`` setting (Бар Черникова).

The same client-generated ``<id>`` is reused on retry (kept in ``raw_payload.push_doc_id``)
so a re-push overwrites rather than duplicates. The actual send is an explicit action —
auto-push on create is intentionally NOT enabled until the live behaviour is verified on
prod (creating a real iiko document is irreversible).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import Any
from xml.sax.saxutils import escape

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AppSetting,
    CounterpartyAlias,
    IikoProduct,
    InvoiceLineItem,
    SupplierInvoice,
)
from app.services.iiko_inventory import _load_inventory_module

STORE_SETTING_KEY = "iiko.default_store_guid"
IIKO_SOURCE = "iiko"
INCOMING_ENDPOINT = "/documents/import/incomingInvoice"
OUTGOING_ENDPOINT = "/documents/import/outgoingInvoice"


class WarehousePushError(RuntimeError):
    """Domain error for the iiko push (maps to HTTP 404/409)."""


@dataclass
class PushLine:
    product_guid: str
    quantity: str
    price: str
    sum: str
    num: int = 1
    unit_guid: str | None = None
    vat_percent: str | None = None
    vat_sum: str | None = None


@dataclass
class PreparedPush:
    xml: str | None
    doc_id: str
    skip_reason: str | None = None


def _el(tag: str, value: Any) -> str:
    return f"<{tag}>{escape(str(value))}</{tag}>"


def build_invoice_xml(
    *,
    doc_id: str,
    direction: str,
    partner_guid: str,
    number: str,
    incoming_date: str,
    date_incoming: str,
    store_guid: str,
    lines: list[PushLine],
) -> str:
    """Build the iiko import XML mirroring a real document's shape: ``incomingDate`` is a
    DATE (``yyyy-MM-dd``), ``dateIncoming`` a tz-less datetime, plus a document-level
    ``defaultStore``. payable → incomingInvoice (``<supplier>``), receivable →
    outgoingInvoice (``<counteragentId>``)."""
    items: list[str] = []
    for line in lines:
        parts = [
            _el("num", line.num),
            _el("product", line.product_guid),
            _el("amount", line.quantity),
            _el("actualAmount", line.quantity),
            _el("price", line.price),
            _el("sum", line.sum),
            _el("store", store_guid),
        ]
        if line.unit_guid:
            parts.append(_el("amountUnit", line.unit_guid))
        if line.vat_percent is not None:
            parts.append(_el("vatPercent", line.vat_percent))
        if line.vat_sum is not None:
            parts.append(_el("vatSum", line.vat_sum))
        items.append("<item>" + "".join(parts) + "</item>")
    items_xml = "<items>" + "".join(items) + "</items>"
    # Field set/shape mirrors a real exported document (DISTRIBUTION_BY_AMOUNT etc.).
    head = (
        _el("id", doc_id)
        + "<transportInvoiceNumber></transportInvoiceNumber>"
        + _el("incomingDate", incoming_date)
        + "<useDefaultDocumentTime>false</useDefaultDocumentTime>"
        + _el("dateIncoming", date_incoming)
        + _el("documentNumber", number)
        + "<status>PROCESSED</status>"
        + "<distributionAlgorithm>DISTRIBUTION_BY_AMOUNT</distributionAlgorithm>"
        + _el("defaultStore", store_guid)
    )
    # iiko import wants a single <document> as root (NOT the <incomingInvoiceDtoes> list
    # wrapper the export returns — that yields a generic 400 before iiko even parses it).
    if direction == "payable":
        return "<document>" + head + _el("supplier", partner_guid) + items_xml + "</document>"
    return "<document>" + head + _el("counteragentId", partner_guid) + items_xml + "</document>"


async def _store_guid(session: AsyncSession, invoice: SupplierInvoice) -> str | None:
    if invoice.store_guid:
        return invoice.store_guid
    setting = await session.scalar(select(AppSetting).where(AppSetting.key == STORE_SETTING_KEY))
    if setting is None:
        return None
    value = setting.value
    if isinstance(value, dict):
        return value.get("guid") or value.get("store_guid")
    return str(value) if value else None


async def prepare_push(session: AsyncSession, invoice: SupplierInvoice) -> PreparedPush:
    """Resolve GUIDs + build XML, or return a skip reason. No network — unit-testable."""
    doc_id = str((invoice.raw_payload or {}).get("push_doc_id") or uuid.uuid4())
    partner_guid = await session.scalar(
        select(CounterpartyAlias.alias).where(
            CounterpartyAlias.counterparty_id == invoice.counterparty_id,
            CounterpartyAlias.source == IIKO_SOURCE,
        )
    )
    if not partner_guid:
        return PreparedPush(None, doc_id, "Нет iiko-GUID контрагента")
    store_guid = await _store_guid(session, invoice)
    if not store_guid:
        return PreparedPush(None, doc_id, "Не настроен склад (iiko.default_store_guid)")
    rows = (
        await session.scalars(
            select(InvoiceLineItem)
            .where(
                InvoiceLineItem.invoice_id == invoice.id,
                InvoiceLineItem.is_staff.is_(False),
            )
            .order_by(InvoiceLineItem.sort_order)
        )
    ).all()
    # iiko хочет GUID единицы измерения (amountUnit) у позиции — берём из кэша номенклатуры.
    product_ids = [row.iiko_product_id for row in rows if row.iiko_product_id]
    unit_by_product: dict[uuid.UUID, str | None] = {}
    if product_ids:
        products = (
            await session.scalars(select(IikoProduct).where(IikoProduct.id.in_(product_ids)))
        ).all()
        unit_by_product = {p.id: p.main_unit_guid for p in products}
    lines: list[PushLine] = []
    for index, line in enumerate(rows, start=1):
        if not line.product_guid:
            continue
        lines.append(
            PushLine(
                product_guid=line.product_guid,
                quantity=str(line.quantity),
                price=str(line.price),
                sum=str(line.sum),
                num=index,
                unit_guid=unit_by_product.get(line.iiko_product_id),
                vat_percent=str(line.vat_percent) if line.vat_percent is not None else None,
                vat_sum=str(line.vat_sum) if line.vat_sum is not None else None,
            )
        )
    if not lines:
        return PreparedPush(None, doc_id, "Нет товарных строк с iiko-GUID (персонал/ручные)")
    dt = invoice.issued_at
    if dt is None and invoice.invoice_date is not None:
        dt = datetime.combine(invoice.invoice_date, time())
    xml = build_invoice_xml(
        doc_id=doc_id,
        direction=invoice.direction,
        partner_guid=partner_guid,
        number=invoice.number or "",
        incoming_date=dt.date().isoformat(),
        date_incoming=dt.replace(tzinfo=None).isoformat(timespec="seconds"),
        store_guid=store_guid,
        lines=lines,
    )
    return PreparedPush(xml, doc_id)


def _send_to_iiko(direction: str, xml: str) -> tuple[int, bytes]:
    inventory = _load_inventory_module()
    inventory.load_local_env()
    client = inventory.IikoClient()
    endpoint = INCOMING_ENDPOINT if direction == "payable" else OUTGOING_ENDPOINT
    # iiko требует ровно application/xml (text/xml → 415).
    return client.request(endpoint, method="POST", raw_body=xml, content_type="application/xml")


def _parse_document_id(body: bytes) -> str | None:
    try:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(body)
    except ET.ParseError:
        return None
    for tag in ("id", "documentId"):
        node = root.find(f".//{tag}")
        if node is not None and node.text:
            return node.text.strip()
    return None


async def push_invoice_to_iiko(session: AsyncSession, invoice_id: uuid.UUID) -> SupplierInvoice:
    """Send the invoice to iiko (REAL document). Updates push status; never raises on
    iiko errors — the invoice is kept with ``failed``/``skipped`` status instead."""
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        raise WarehousePushError("Накладная не найдена")

    prepared = await prepare_push(session, invoice)
    raw = dict(invoice.raw_payload or {})
    raw["push_doc_id"] = prepared.doc_id
    invoice.raw_payload = raw

    if prepared.xml is None:
        invoice.iiko_push_status = "skipped"
        invoice.iiko_push_error = prepared.skip_reason
        await session.commit()
        return invoice

    try:
        status, body = await anyio.to_thread.run_sync(
            _send_to_iiko, invoice.direction, prepared.xml
        )
    except Exception as exc:  # noqa: BLE001 - keep the invoice, record the error
        invoice.iiko_push_status = "failed"
        invoice.iiko_push_error = str(exc)[:500]
        await session.commit()
        return invoice

    if status >= 400:
        invoice.iiko_push_status = "failed"
        invoice.iiko_push_error = f"iiko HTTP {status}: {body[:300].decode('utf-8', 'replace')}"
        await session.commit()
        return invoice

    invoice.external_id = _parse_document_id(body) or invoice.external_id
    invoice.iiko_push_status = "pushed"
    invoice.iiko_pushed_at = datetime.now(UTC)
    invoice.iiko_push_error = None
    await session.commit()
    return invoice

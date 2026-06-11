"""Cyclic ingest of supplier invoices from iiko (incoming invoices).

Reads ``/suppliers`` and ``/documents/export/incomingInvoice`` (GET-only) and
upserts :class:`SupplierInvoice` obligations idempotently by the iiko document id.
iiko stays the source of *receipts*; payment status lives only in our system —
there is no write-back (the iikoServer API cannot mark an invoice paid).

Fetch (network) and ingest (parse + upsert) are deliberately split so the parsing
pipeline can be exercised against captured XML without live iiko access.
"""

from __future__ import annotations

import importlib
import sys
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from types import ModuleType

import anyio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AgentRun,
    Counterparty,
    CounterpartyAlias,
    CounterpartyCollectionSource,
    CounterpartyPayableProfile,
    CounterpartyRole,
    CounterpartyRoutingRule,
    SupplierInvoice,
)

from app.services.counterparty_registry import compute_invoice_due_date

# Reuse the iiko credential loader (DB SourceCredential -> env) used by employee sync.
from app.services.iiko_sync import _load_source_credential_env

IIKO_SOURCE = "iiko"
SUPPLIERS_ENDPOINT = "/suppliers"
INVOICE_ENDPOINT = "/documents/export/incomingInvoice"
# Only confirmed (posted) receipts are real obligations; NEW is unposted, DELETED is void.
INGESTED_IIKO_STATUSES = frozenset({"PROCESSED"})


@dataclass
class IikoSupplier:
    id: str
    name: str
    inn: str | None
    deleted: bool
    represents_store: bool


@dataclass
class CounterpartyInvoiceSyncResult:
    suppliers_seen: int = 0
    invoices_seen: int = 0
    invoices_created: int = 0
    invoices_updated: int = 0
    counterparties_created: int = 0
    skipped_status: int = 0
    skipped_store: int = 0
    skipped_no_id: int = 0
    skipped_unknown_supplier: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


# --- iiko module loading (mirrors app.services.iiko_sync) ---------------------


def _candidate_project_roots() -> list[Path]:
    current = Path(__file__).resolve()
    roots = [
        parent for parent in current.parents if (parent / "integrations/iiko/scripts").exists()
    ]
    roots.extend(Path(path) for path in ("/app", Path.cwd(), Path.cwd().parent))
    result: list[Path] = []
    for root in roots:
        if root not in result:
            result.append(root)
    return result


def _load_orders_module() -> ModuleType:
    for root in _candidate_project_roots():
        script_dir = root / "integrations/iiko/scripts"
        if not (script_dir / "export_orders_delivery.py").exists():
            continue
        script_dir_str = str(script_dir)
        if script_dir_str not in sys.path:
            sys.path.insert(0, script_dir_str)
        return importlib.import_module("export_orders_delivery")
    raise RuntimeError("integrations/iiko/scripts/export_orders_delivery.py is not available")


# --- parsing helpers ----------------------------------------------------------


def _text(element: ET.Element, tag: str) -> str | None:
    child = element.find(tag)
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    return value or None


def _parse_iiko_date(value: str | None) -> date | None:
    # Handles "2026-06-10", "2026-06-10T09:00:00" and the literal string "null".
    if not value or value.strip().lower() == "null":
        return None
    head = value.split("T", 1)[0].strip()
    try:
        return date.fromisoformat(head)
    except ValueError:
        return None


def _decimal(value: str | None) -> Decimal:
    try:
        return Decimal((value or "0").strip())
    except (InvalidOperation, AttributeError):
        return Decimal(0)


def _invoice_amount(doc: ET.Element) -> Decimal:
    # iiko incoming invoices carry no document total; item ``sum`` is gross
    # (VAT-inclusive, equals price*amount), so the payable is the item sum.
    total = Decimal(0)
    items = doc.find("items")
    if items is not None:
        for item in items.findall("item"):
            total += _decimal(item.findtext("sum"))
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _normalize_rate(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        rate = Decimal(value.strip())
    except (InvalidOperation, AttributeError):
        return None
    if rate <= 0:
        return None
    if rate == rate.to_integral_value():
        return str(int(rate))
    return format(rate.normalize(), "f")


def _invoice_vat(doc: ET.Element) -> tuple[Decimal, dict[str, str]]:
    """Group item ``vatSum`` by ``vatPercent`` (e.g. 10% / 22%); 0/none is ignored."""
    breakdown: dict[str, Decimal] = {}
    items = doc.find("items")
    if items is not None:
        for item in items.findall("item"):
            rate = _normalize_rate(item.findtext("vatPercent"))
            vat = _decimal(item.findtext("vatSum"))
            if rate is None or vat == 0:
                continue
            breakdown[rate] = breakdown.get(rate, Decimal(0)) + vat
    total = sum(breakdown.values(), Decimal(0))
    return (
        total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        {
            rate: str(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            for rate, amount in breakdown.items()
        },
    )


def _doc_prefix(doc: ET.Element) -> str | None:
    """Legal-entity discriminator: the supplier doc-number prefix (before the dash)."""
    raw = (
        _text(doc, "transportInvoiceNumber")
        or _text(doc, "incomingDocumentNumber")
        or _text(doc, "invoice")
    )
    if not raw:
        return None
    return raw.split("-", 1)[0].strip() or None


def _guess_counterparty_type(inn: str | None) -> str:
    # 12-digit INN belongs to an individual entrepreneur, 10-digit to a legal entity.
    if inn and len(inn) == 12:
        return "individual"
    return "legal_entity"


def _parse_suppliers(suppliers_xml: bytes | str) -> dict[str, IikoSupplier]:
    root = ET.fromstring(suppliers_xml)
    suppliers: dict[str, IikoSupplier] = {}
    for element in root.findall(".//employee"):
        supplier_id = _text(element, "id")
        if not supplier_id:
            continue
        suppliers[supplier_id] = IikoSupplier(
            id=supplier_id,
            name=_text(element, "name") or supplier_id,
            inn=_text(element, "taxpayerIdNumber"),
            deleted=(_text(element, "deleted") or "false").lower() == "true",
            represents_store=(_text(element, "representsStore") or "false").lower() == "true",
        )
    return suppliers


# --- counterparty resolution --------------------------------------------------


async def _routed_counterparty_id(
    session: AsyncSession, supplier_guid: str, prefix: str | None
) -> uuid.UUID | None:
    """Brand routing: one iiko supplier → several legal entities by doc-number prefix."""
    if not prefix:
        return None
    return await session.scalar(
        select(CounterpartyRoutingRule.counterparty_id).where(
            CounterpartyRoutingRule.iiko_supplier_guid == supplier_guid,
            CounterpartyRoutingRule.prefix == prefix,
        )
    )


async def _resolve_counterparty(
    session: AsyncSession, supplier: IikoSupplier, *, result: CounterpartyInvoiceSyncResult
) -> uuid.UUID:
    alias = await session.scalar(
        select(CounterpartyAlias).where(
            func.lower(CounterpartyAlias.alias) == supplier.id.lower(),
            CounterpartyAlias.source == IIKO_SOURCE,
        )
    )
    if alias is not None:
        return alias.counterparty_id

    counterparty = None
    if supplier.inn:
        counterparty = await session.scalar(
            select(Counterparty).where(Counterparty.inn == supplier.inn)
        )

    if counterparty is None:
        counterparty = Counterparty(
            name=supplier.name,
            inn=supplier.inn or None,
            type=_guess_counterparty_type(supplier.inn),
            status="requires_setup",
        )
        session.add(counterparty)
        await session.flush()
        session.add(CounterpartyRole(counterparty_id=counterparty.id, role="supplier"))
        session.add(
            CounterpartyPayableProfile(
                counterparty_id=counterparty.id, internal_name=supplier.name
            )
        )
        result.counterparties_created += 1

    session.add(
        CounterpartyAlias(counterparty_id=counterparty.id, alias=supplier.id, source=IIKO_SOURCE)
    )
    # Surface the iiko link as a collection source for the supplier source-data page.
    session.add(
        CounterpartyCollectionSource(
            counterparty_id=counterparty.id, kind="iiko", value=supplier.id
        )
    )
    await session.flush()
    return counterparty.id


async def _profile_due_terms(
    session: AsyncSession, counterparty_id: uuid.UUID
) -> tuple[int | None, int | None]:
    row = (
        await session.execute(
            select(
                CounterpartyPayableProfile.payment_delay_days,
                CounterpartyPayableProfile.payment_due_day_of_month,
            ).where(CounterpartyPayableProfile.counterparty_id == counterparty_id)
        )
    ).first()
    return (row[0], row[1]) if row is not None else (None, None)


# --- ingest -------------------------------------------------------------------


async def ingest_iiko_payables(
    session: AsyncSession, *, suppliers_xml: bytes | str, invoices_xml: bytes | str
) -> CounterpartyInvoiceSyncResult:
    result = CounterpartyInvoiceSyncResult()
    suppliers = _parse_suppliers(suppliers_xml)
    result.suppliers_seen = len(suppliers)

    documents = ET.fromstring(invoices_xml).findall(".//document")
    result.invoices_seen = len(documents)

    for doc in documents:
        status = (_text(doc, "status") or "").upper()
        if status not in INGESTED_IIKO_STATUSES:
            result.skipped_status += 1
            continue
        external_id = _text(doc, "id")
        if not external_id:
            result.skipped_no_id += 1
            continue
        supplier_guid = _text(doc, "supplier")
        supplier = suppliers.get(supplier_guid) if supplier_guid else None
        if supplier is None:
            result.skipped_unknown_supplier += 1
            continue
        if supplier.represents_store:
            result.skipped_store += 1
            continue

        counterparty_id = await _routed_counterparty_id(session, supplier.id, _doc_prefix(doc))
        if counterparty_id is None:
            counterparty_id = await _resolve_counterparty(session, supplier, result=result)
        amount = _invoice_amount(doc)
        vat_total, vat_breakdown = _invoice_vat(doc)
        number = _text(doc, "documentNumber") or _text(doc, "transportInvoiceNumber")
        invoice_date = _parse_iiko_date(_text(doc, "incomingDate") or _text(doc, "dateIncoming"))
        due_date = _parse_iiko_date(_text(doc, "dueDate"))
        raw_payload = {
            child.tag: (child.text or "").strip()
            for child in list(doc)
            if child.tag != "items"
        }

        existing = await session.scalar(
            select(SupplierInvoice).where(
                SupplierInvoice.source == IIKO_SOURCE,
                SupplierInvoice.external_id == external_id,
            )
        )
        if existing is None:
            if due_date is None and invoice_date is not None:
                delay, due_day = await _profile_due_terms(session, counterparty_id)
                due_date = compute_invoice_due_date(
                    invoice_date, delay_days=delay, due_day_of_month=due_day
                )
            session.add(
                SupplierInvoice(
                    counterparty_id=counterparty_id,
                    source=IIKO_SOURCE,
                    external_id=external_id,
                    number=number,
                    invoice_date=invoice_date,
                    due_date=due_date,
                    amount=amount,
                    vat_total=vat_total,
                    vat_breakdown=vat_breakdown,
                    payment_status="unpaid",
                    raw_payload=raw_payload,
                )
            )
            result.invoices_created += 1
        else:
            # Refresh source-owned fields; never override our payment_status /
            # allocations. Amount only changes while still fully unpaid.
            existing.number = number
            existing.invoice_date = invoice_date
            existing.counterparty_id = counterparty_id
            if existing.payment_status == "unpaid":
                existing.amount = amount
                existing.vat_total = vat_total
                existing.vat_breakdown = vat_breakdown
                if due_date is not None:
                    existing.due_date = due_date
            existing.raw_payload = raw_payload
            result.invoices_updated += 1

    return result


# --- fetch + orchestration ----------------------------------------------------


def fetch_iiko_payables(*, days: int = 30) -> tuple[bytes, bytes]:
    module = _load_orders_module()
    module.load_local_env()
    client = module.IikoClient()
    _status, suppliers_xml = client.request(SUPPLIERS_ENDPOINT)
    today = datetime.now(tz=UTC).date()
    date_from = today - timedelta(days=days)
    _status2, invoices_xml = client.request(
        INVOICE_ENDPOINT,
        params={"from": date_from.isoformat(), "to": today.isoformat()},
    )
    return suppliers_xml, invoices_xml


async def sync_counterparty_invoices(
    session: AsyncSession, *, days: int = 30, run_reason: str = "manual"
) -> CounterpartyInvoiceSyncResult:
    await _load_source_credential_env(session)
    run = AgentRun(
        agent_name="counterparty_invoice_sync",
        status="running",
        params={"days": days, "reason": run_reason},
    )
    session.add(run)
    await session.flush()
    try:
        suppliers_xml, invoices_xml = await anyio.to_thread.run_sync(
            lambda: fetch_iiko_payables(days=days)
        )
        result = await ingest_iiko_payables(
            session, suppliers_xml=suppliers_xml, invoices_xml=invoices_xml
        )
    except Exception as exc:  # noqa: BLE001 - record failure on the run, then re-raise
        run.status = "error"
        run.finished_at = datetime.now(tz=UTC)
        run.result = {"error": str(exc)}
        await session.commit()
        raise
    run.status = "success"
    run.finished_at = datetime.now(tz=UTC)
    run.result = result.as_dict()
    await session.commit()
    return result

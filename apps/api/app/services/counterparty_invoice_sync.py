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
    SupplierInvoiceTombstone,
)
from app.services.counterparty_registry import compute_invoice_due_date

# Reuse the iiko credential loader (DB SourceCredential -> env) used by employee sync.
from app.services.iiko_sync import _load_source_credential_env

IIKO_SOURCE = "iiko"
# Invoices we create in our system and push INTO iiko get an external_id assigned from the
# iiko response (warehouse_invoice_push). The reverse sync must recognise them by that
# external_id and NOT re-create an iiko-sourced clone. They keep their original source —
# 'manual' (Склад) or 'kassa_invoice' (Касса) — so BOTH must be treated as «our own pushed».
MANUAL_SOURCE = "manual"
KASSA_INVOICE_SOURCE = "kassa_invoice"
# Sources of invoices authored in our system that may be pushed into iiko (round-trip guard).
OUR_PUSHED_SOURCES = (MANUAL_SOURCE, KASSA_INVOICE_SOURCE)
SUPPLIERS_ENDPOINT = "/suppliers"
INVOICE_ENDPOINT = "/documents/export/incomingInvoice"
# Outgoing invoices = goods we ship out (our AR). In this business only barter
# partners receive outgoing invoices, so a receivable ⇒ relationship=barter.
OUTGOING_INVOICE_ENDPOINT = "/documents/export/outgoingInvoice"
# Product directory — resolves line-item product GUID → name (for barter номенклатура).
PRODUCTS_ENDPOINT = "/products"
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
    receivables_seen: int = 0
    receivables_created: int = 0
    receivables_updated: int = 0
    counterparties_created: int = 0
    skipped_status: int = 0
    skipped_store: int = 0
    skipped_no_id: int = 0
    skipped_unknown_supplier: int = 0
    skipped_zero_amount: int = 0
    # Documents whose iiko id is tombstoned (intentionally deleted on our side): never
    # re-import, so a manual cleanup stays gone instead of resurrecting on the next sync.
    skipped_tombstoned: int = 0
    # Documents that are our own manually-created invoices already pushed into iiko —
    # matched by external_id to an existing source='manual' record, so we skip them
    # instead of creating an iiko-sourced duplicate.
    skipped_own_pushed: int = 0

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


def _parse_products(products_xml: bytes | str | None) -> dict[str, str]:
    """Build a product GUID → name map from the iiko /products directory."""
    if not products_xml:
        return {}
    root = ET.fromstring(products_xml)
    names: dict[str, str] = {}
    for node in root:
        pid = _text(node, "id")
        name = _text(node, "name")
        if pid and name:
            names[pid] = name
    return names


def _parse_line_items(doc: ET.Element, name_by_id: dict[str, str]) -> list[dict[str, str | None]]:
    """Line items for номенклатура matching. Product GUID is ``productId`` on outgoing
    docs and ``product`` on incoming; ``productArticle`` is the shared article code."""
    items = doc.find("items")
    if items is None:
        return []
    parsed: list[dict[str, str | None]] = []
    for item in items.findall("item"):
        product_id = _text(item, "productId") or _text(item, "product")
        parsed.append(
            {
                "product_id": product_id,
                "article": _text(item, "productArticle"),
                "name": name_by_id.get(product_id) if product_id else None,
                "quantity": _text(item, "amount"),
                "amount": str(_decimal(item.findtext("sum"))),
            }
        )
    return parsed


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
            CounterpartyPayableProfile(counterparty_id=counterparty.id, internal_name=supplier.name)
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


async def _mark_relationship_barter(session: AsyncSession, counterparty_id: uuid.UUID) -> None:
    """A counterparty with an outgoing (receivable) invoice is a barter partner."""
    profile = await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == counterparty_id
        )
    )
    if profile is None:
        session.add(
            CounterpartyPayableProfile(counterparty_id=counterparty_id, relationship="barter")
        )
        await session.flush()
    elif profile.relationship != "barter":
        profile.relationship = "barter"


async def _is_barter(session: AsyncSession, counterparty_id: uuid.UUID) -> bool:
    relationship = await session.scalar(
        select(CounterpartyPayableProfile.relationship).where(
            CounterpartyPayableProfile.counterparty_id == counterparty_id
        )
    )
    return relationship == "barter"


async def _ingest_documents(
    session: AsyncSession,
    documents: list[ET.Element],
    suppliers: dict[str, IikoSupplier],
    *,
    direction: str,
    result: CounterpartyInvoiceSyncResult,
    name_by_id: dict[str, str] | None = None,
    tombstoned: frozenset[str] = frozenset(),
) -> None:
    # incoming invoices carry the supplier GUID, outgoing the counteragent GUID.
    counterparty_field = "supplier" if direction == "payable" else "counteragentId"
    for doc in documents:
        status = (_text(doc, "status") or "").upper()
        if status not in INGESTED_IIKO_STATUSES:
            result.skipped_status += 1
            continue
        external_id = _text(doc, "id")
        if not external_id:
            result.skipped_no_id += 1
            continue
        # Intentionally deleted on our side — never re-import (keeps manual cleanups gone).
        if external_id in tombstoned:
            result.skipped_tombstoned += 1
            continue
        # Our own invoice pushed into iiko (source='manual' from Склад or 'kassa_invoice' from
        # Касса): skip so the reverse sync doesn't clone it as a second iiko obligation or clobber
        # our fields (issued_at, staff_amount, push_doc_id, normalized lines). Primary key is
        # external_id (= the iiko doc id). But if the post-push id lookup failed (replica lag /
        # export error), external_id is still NULL — fall back to matching by documentNumber and
        # BACKFILL external_id, so this branch (and all future syncs) dedupe correctly. Match on
        # BOTH our authored sources: kassa_invoice was missing here → round-trip clones.
        # 1) Точное совпадение по external_id — наша уже привязанная к этому iiko-документу
        #    накладная. Приоритет именно ему: иначе fallback по номеру мог бы зацепить ДРУГУЮ
        #    нашу накладную с тем же номером (у Кассо-накладных номер часто общий, напр. «4»).
        own_pushed = await session.scalar(
            select(SupplierInvoice).where(
                SupplierInvoice.source.in_(OUR_PUSHED_SOURCES),
                SupplierInvoice.external_id == external_id,
            )
        )
        # 2) Fallback: пуш прошёл, но обратный id-lookup не сработал (external_id ещё NULL) —
        #    узнаём накладную по documentNumber и BACKFILL-им external_id. ТОЛЬКО если такой
        #    кандидат РОВНО один — при общих номерах гадать нельзя (иначе backfill занятого
        #    external_id → UniqueViolation), пропускаем и оставляем как есть.
        own_number = _text(doc, "documentNumber") or _text(doc, "transportInvoiceNumber")
        if own_pushed is None and own_number:
            candidates = (
                await session.scalars(
                    select(SupplierInvoice).where(
                        SupplierInvoice.source.in_(OUR_PUSHED_SOURCES),
                        SupplierInvoice.external_id.is_(None),
                        SupplierInvoice.number == own_number,
                    )
                )
            ).all()
            if len(candidates) == 1:
                own_pushed = candidates[0]
                own_pushed.external_id = external_id
        if own_pushed is not None:
            result.skipped_own_pushed += 1
            continue
        supplier_guid = _text(doc, counterparty_field)
        supplier = suppliers.get(supplier_guid) if supplier_guid else None
        if supplier is None:
            result.skipped_unknown_supplier += 1
            continue
        if supplier.represents_store:
            result.skipped_store += 1
            continue

        # 0₽ documents are gifts/bonuses (iiko comment «подарок» etc.) — nothing to pay and
        # no value to net in barter. Keep them out of the inbox entirely, before we even
        # resolve/create a counterparty for a gift-only supplier.
        amount = _invoice_amount(doc)
        if amount == 0:
            result.skipped_zero_amount += 1
            continue

        counterparty_id = None
        if direction == "payable":
            counterparty_id = await _routed_counterparty_id(session, supplier.id, _doc_prefix(doc))
        if counterparty_id is None:
            counterparty_id = await _resolve_counterparty(session, supplier, result=result)
        if direction == "receivable":
            await _mark_relationship_barter(session, counterparty_id)

        # Capture line items only for barter partners (receivable just marked above).
        is_barter = direction == "receivable" or await _is_barter(session, counterparty_id)
        line_items = _parse_line_items(doc, name_by_id or {}) if is_barter else []

        vat_total, vat_breakdown = _invoice_vat(doc)
        number = _text(doc, "documentNumber") or _text(doc, "transportInvoiceNumber")
        invoice_date = _parse_iiko_date(_text(doc, "incomingDate") or _text(doc, "dateIncoming"))
        due_date = _parse_iiko_date(_text(doc, "dueDate"))
        raw_payload = {
            child.tag: (child.text or "").strip() for child in list(doc) if child.tag != "items"
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
                    direction=direction,
                    external_id=external_id,
                    number=number,
                    invoice_date=invoice_date,
                    due_date=due_date,
                    amount=amount,
                    vat_total=vat_total,
                    vat_breakdown=vat_breakdown,
                    line_items=line_items,
                    payment_status="unpaid",
                    raw_payload=raw_payload,
                )
            )
            if direction == "payable":
                result.invoices_created += 1
            else:
                result.receivables_created += 1
        else:
            # Refresh source-owned fields; never override our payment_status /
            # allocations. Amount only changes while still fully unpaid.
            existing.number = number
            existing.invoice_date = invoice_date
            existing.counterparty_id = counterparty_id
            existing.direction = direction
            if existing.payment_status == "unpaid":
                existing.amount = amount
                existing.vat_total = vat_total
                existing.vat_breakdown = vat_breakdown
                if due_date is not None:
                    existing.due_date = due_date
            existing.raw_payload = raw_payload
            if is_barter:
                existing.line_items = line_items
            if direction == "payable":
                result.invoices_updated += 1
            else:
                result.receivables_updated += 1


async def ingest_iiko_payables(
    session: AsyncSession,
    *,
    suppliers_xml: bytes | str,
    invoices_xml: bytes | str,
    outgoing_invoices_xml: bytes | str | None = None,
    products_xml: bytes | str | None = None,
) -> CounterpartyInvoiceSyncResult:
    result = CounterpartyInvoiceSyncResult()
    suppliers = _parse_suppliers(suppliers_xml)
    result.suppliers_seen = len(suppliers)
    name_by_id = _parse_products(products_xml)

    # Tombstoned iiko doc ids — intentionally deleted, must not be re-imported. Load once.
    tombstoned = frozenset(
        (await session.scalars(select(SupplierInvoiceTombstone.external_id))).all()
    )

    # Receivables (outgoing) first: they mark partners as barter, so the payables that
    # follow capture line items for those partners in the same run.
    if outgoing_invoices_xml is not None:
        outgoing = ET.fromstring(outgoing_invoices_xml).findall(".//document")
        result.receivables_seen = len(outgoing)
        await _ingest_documents(
            session,
            outgoing,
            suppliers,
            direction="receivable",
            result=result,
            name_by_id=name_by_id,
            tombstoned=tombstoned,
        )

    incoming = ET.fromstring(invoices_xml).findall(".//document")
    result.invoices_seen = len(incoming)
    await _ingest_documents(
        session,
        incoming,
        suppliers,
        direction="payable",
        result=result,
        name_by_id=name_by_id,
        tombstoned=tombstoned,
    )

    return result


# --- fetch + orchestration ----------------------------------------------------


def fetch_iiko_payables(*, days: int = 30) -> tuple[bytes, bytes, bytes, bytes]:
    module = _load_orders_module()
    module.load_local_env()
    client = module.IikoClient()
    _status, suppliers_xml = client.request(SUPPLIERS_ENDPOINT)
    today = datetime.now(tz=UTC).date()
    date_from = today - timedelta(days=days)
    params = {"from": date_from.isoformat(), "to": today.isoformat()}
    _status2, invoices_xml = client.request(INVOICE_ENDPOINT, params=params)
    _status3, outgoing_xml = client.request(OUTGOING_INVOICE_ENDPOINT, params=params)
    _status4, products_xml = client.request(PRODUCTS_ENDPOINT)
    return suppliers_xml, invoices_xml, outgoing_xml, products_xml


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
        suppliers_xml, invoices_xml, outgoing_xml, products_xml = await anyio.to_thread.run_sync(
            lambda: fetch_iiko_payables(days=days)
        )
        result = await ingest_iiko_payables(
            session,
            suppliers_xml=suppliers_xml,
            invoices_xml=invoices_xml,
            products_xml=products_xml,
            outgoing_invoices_xml=outgoing_xml,
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

"""Циклический ingest накладных из iiko (реверс-синхронизация, документы — через Cloud API).

Документы читаются из iiko Cloud: ``incoming_invoice/list`` + ``outgoing_invoice/list`` (сводки за
окно) → ``get`` по каждому проведённому (список возвращает сводки БЕЗ items — проверено на живом
API). Обязательства :class:`SupplierInvoice` апсертятся идемпотентно по iiko ``documentId``
(= ``external_id``; id-пространство одно у Cloud и RMS). Статус оплаты живёт только у нас —
обратной записи нет.

Справочник поставщиков ПОКА читается из iikoServer RMS ``/suppliers``: Cloud-эндпоинт
``/api/inventory/v1/counteragents`` на живом API отвечает HTTP 500 «UocID is missing in
userContext» (сломан на стороне iiko, 09.07.2026) и не отдаёт флаги deleted/representsStore.
Имена номенклатуры для бартерных позиций резолвятся из локального кэша :class:`IikoProduct`
(отдельный products-фид больше не нужен).

Fetch (сеть) и ingest (разбор + upsert) разделены намеренно: конвейер разбора тестируется на
захваченных JSON-документах без живого iiko.
"""

from __future__ import annotations

import importlib
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from types import ModuleType
from typing import Any

import anyio
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AgentRun,
    Counterparty,
    CounterpartyAlias,
    CounterpartyCollectionSource,
    CounterpartyPayableProfile,
    CounterpartyRole,
    CounterpartyRoutingRule,
    DdsArticle,
    IikoProduct,
    InvoiceLineItem,
    SupplierInvoice,
    SupplierInvoiceTombstone,
)
from app.services.counterparty_registry import compute_invoice_due_date
from app.services.iiko_cloud_client import IIKO_ORGANIZATION_ID, iiko_auth_token, iiko_opener
from app.services.iiko_invoice_cloud import get_invoice, list_invoices

# Reuse the iiko credential loader (DB SourceCredential -> env) used by employee sync.
from app.services.iiko_sync import _load_source_credential_env

IIKO_SOURCE = "iiko"
# Invoices we create in our system and push INTO iiko get an external_id assigned from the
# iiko response (warehouse_invoice_push). The reverse sync must recognise them by that
# external_id and NOT re-create an iiko-sourced clone. They keep their original source —
# 'manual' (Склад), 'kassa_invoice' (накладная Кассы) или 'kassa_cheque' (чек Кассы — тоже
# уходит в iiko приходной накладной) — поэтому ВСЕ ТРИ должны считаться «our own pushed».
# Иначе пушнутый чек затягивается обратно вторым обязательством source='iiko' (round-trip
# clone): неоплаченный дубль без нормализованных позиций.
MANUAL_SOURCE = "manual"
KASSA_INVOICE_SOURCE = "kassa_invoice"
KASSA_CHEQUE_SOURCE = "kassa_cheque"
# Sources of invoices authored in our system that may be pushed into iiko (round-trip guard).
OUR_PUSHED_SOURCES = (MANUAL_SOURCE, KASSA_INVOICE_SOURCE, KASSA_CHEQUE_SOURCE)
# Справочник поставщиков — RMS (Cloud counteragents сломан на стороне iiko, см. докстринг).
SUPPLIERS_ENDPOINT = "/suppliers"
# Outgoing invoices = goods we ship out (our AR). In this business only barter
# partners receive outgoing invoices, so a receivable ⇒ relationship=barter.
# Only confirmed (posted) receipts are real obligations; NEW is unposted, DELETED is void.
INGESTED_IIKO_STATUSES = frozenset({"PROCESSED"})
# Ретраи Cloud ``get`` на HTTP 429 (rate-limit): паузы между попытками, сек. Живой прогон на
# окне 30 дней показал, что лимит iiko жёсткий — бэкофф длинный (суммарно ~77 сек).
_CLOUD_GET_RETRY_DELAYS = (2.0, 5.0, 10.0, 20.0, 40.0)
# Троттлинг между ``get``-вызовами; для ночного джоба задержка окна некритична.
_CLOUD_GET_THROTTLE_SECONDS = 0.4


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
    # Own (Касса/Склад) pushed invoices whose amount/date we pulled BACK from iiko because they
    # were edited there while still unpaid (goods-only iiko sum + our excluded staff/expense lines).
    own_pushed_updated: int = 0

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


def _decimal(value: Any) -> Decimal:
    """Число/строка из JSON → Decimal (через str — float-дребезг не тащим)."""
    if value is None:
        return Decimal(0)
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, ValueError):
        return Decimal(0)


def _invoice_amount(doc: dict) -> Decimal:
    # Обязательство = Σ item.sum: сумма позиции в iiko — gross (с НДС, price*amount).
    total = Decimal(0)
    for item in doc.get("items") or []:
        total += _decimal(item.get("sum"))
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _normalize_rate(value: Any) -> str | None:
    if value is None:
        return None
    try:
        rate = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, ValueError):
        return None
    if rate <= 0:
        return None
    if rate == rate.to_integral_value():
        return str(int(rate))
    return format(rate.normalize(), "f")


def _invoice_vat(doc: dict) -> tuple[Decimal, dict[str, str]]:
    """НДС позиции = ``sum − sumWithoutVat`` (Cloud не отдаёт ``vatSum``), группируем по
    ``vatPercent`` (напр. 10% / 22%); нулевая ставка/нулевой НДС не попадают в раскладку."""
    breakdown: dict[str, Decimal] = {}
    for item in doc.get("items") or []:
        rate = _normalize_rate(item.get("vatPercent"))
        if rate is None or item.get("sumWithoutVat") is None:
            continue
        vat = _decimal(item.get("sum")) - _decimal(item.get("sumWithoutVat"))
        if vat <= 0:
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


def _doc_prefix(doc: dict) -> str | None:
    """Legal-entity discriminator: the supplier doc-number prefix (before the dash)."""
    raw = (
        doc.get("transportInvoiceNumber")
        or doc.get("incomingDocumentNumber")
        or doc.get("invoice")
    )
    if not raw:
        return None
    return str(raw).split("-", 1)[0].strip() or None


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


async def _products_from_cache(
    session: AsyncSession, documents: list[dict]
) -> dict[str, IikoProduct]:
    """GUID → номенклатура из локального кэша :class:`IikoProduct` (имена бартерных позиций +
    привязка материализованных строк к товару).

    Отдельный products-фид с iiko больше не тянем: кэш синкается периодическим джобом, а товар из
    накладной по определению есть в номенклатуре."""
    guids: set[str] = set()
    for doc in documents:
        for item in doc.get("items") or []:
            pid = item.get("product") or item.get("productId")
            if pid:
                guids.add(str(pid))
    if not guids:
        return {}
    rows = (
        await session.scalars(select(IikoProduct).where(IikoProduct.iiko_id.in_(guids)))
    ).all()
    return {product.iiko_id: product for product in rows}


async def _materialize_iiko_lines(
    session: AsyncSession,
    invoice: SupplierInvoice,
    doc: dict,
    products: dict[str, IikoProduct],
    *,
    replace: bool,
) -> None:
    """Материализовать позиции iiko-накладной в :class:`InvoiceLineItem` из Cloud-``items``.

    Раньше строки iiko-накладных не сохранялись («накладная из старой синхронизации» в UI), из-за
    чего они были нередактируемыми. Cloud ``get`` отдаёт полные позиции — сохраняем их с привязкой
    к номенклатуре (GUID → кэш ``IikoProduct``), и накладная правится как своя (правка уходит в
    iiko через propagate — ``external_id`` есть). ``replace=False`` — только досоздать, если строк
    ещё нет (оплаченные не пересобираем: их состав заморожен вместе с суммой)."""
    items = doc.get("items") or []
    if not items:
        return
    existing = await session.scalar(
        select(func.count())
        .select_from(InvoiceLineItem)
        .where(InvoiceLineItem.invoice_id == invoice.id)
    )
    if existing and not replace:
        return
    if existing:
        await session.execute(
            delete(InvoiceLineItem).where(InvoiceLineItem.invoice_id == invoice.id)
        )
    for index, item in enumerate(sorted(items, key=lambda it: it.get("num") or 0)):
        guid = item.get("product") or item.get("productId")
        guid = str(guid) if guid else None
        product = products.get(guid) if guid else None
        vat_sum = None
        if item.get("sumWithoutVat") is not None:
            diff = _decimal(item.get("sum")) - _decimal(item.get("sumWithoutVat"))
            vat_sum = diff if diff > 0 else None
        session.add(
            InvoiceLineItem(
                invoice_id=invoice.id,
                iiko_product_id=product.id if product else None,
                product_guid=guid,
                name=(product.name if product else None) or item.get("productArticle") or "Позиция",
                article=item.get("productArticle") or (product.code if product else None),
                unit=product.unit if product else None,
                quantity=_decimal(item.get("actualAmount") or item.get("amount")),
                price=_decimal(item.get("price")),
                sum=_decimal(item.get("sum")),
                vat_percent=(
                    _decimal(item.get("vatPercent"))
                    if item.get("vatPercent") is not None
                    else None
                ),
                vat_sum=vat_sum,
                is_staff=False,
                sort_order=index,
            )
        )


def _parse_line_items(doc: dict, name_by_id: dict[str, str]) -> list[dict[str, str | None]]:
    """Line items for номенклатура matching (barter). Product GUID is ``product`` in Cloud
    (оба направления); ``productArticle`` is the shared article code."""
    parsed: list[dict[str, str | None]] = []
    for item in doc.get("items") or []:
        product_id = item.get("product") or item.get("productId")
        product_id = str(product_id) if product_id else None
        quantity = item.get("amount")
        parsed.append(
            {
                "product_id": product_id,
                "article": item.get("productArticle"),
                "name": name_by_id.get(product_id) if product_id else None,
                "quantity": str(quantity) if quantity is not None else None,
                "amount": str(_decimal(item.get("sum"))),
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


async def _excluded_push_line_sum(session: AsyncSession, invoice_id: uuid.UUID) -> Decimal:
    """Σ строк накладной, которые НЕ уходят в iiko при пуше (is_staff или не-товарная статья
    ДДС — питание/расходы персонала, содержание точек). Зеркало фильтра warehouse_invoice_push:
    iiko-документ несёт только товарные строки, поэтому при обратном подтягивании суммы из iiko
    эти строки прибавляем, чтобы восстановить полную сумму обязательства."""
    from app.services.warehouse_invoices import SUPPLIER_PAYMENT_ARTICLE_NAME

    goods_articles = select(DdsArticle.id).where(DdsArticle.name == SUPPLIER_PAYMENT_ARTICLE_NAME)
    total = await session.scalar(
        select(func.coalesce(func.sum(InvoiceLineItem.sum), 0)).where(
            InvoiceLineItem.invoice_id == invoice_id,
            or_(
                InvoiceLineItem.is_staff.is_(True),
                and_(
                    InvoiceLineItem.dds_article_id.is_not(None),
                    InvoiceLineItem.dds_article_id.not_in(goods_articles),
                ),
            ),
        )
    )
    return Decimal(total or 0)


async def _ingest_documents(
    session: AsyncSession,
    documents: list[dict],
    suppliers: dict[str, IikoSupplier],
    *,
    direction: str,
    result: CounterpartyInvoiceSyncResult,
    products: dict[str, IikoProduct] | None = None,
    tombstoned: frozenset[str] = frozenset(),
) -> None:
    products = products or {}
    name_by_id = {guid: product.name for guid, product in products.items()}
    for doc in documents:
        status = str(doc.get("status") or "").upper()
        if status not in INGESTED_IIKO_STATUSES:
            result.skipped_status += 1
            continue
        external_id = doc.get("documentId") or doc.get("id")
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
        own_number = doc.get("number") or doc.get("transportInvoiceNumber")
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
            # Накладная создана у нас (Касса/Склад) и запушена в iiko. По умолчанию обратный синк
            # её НЕ трогает (наши поля — источник истины). НО если она ещё НЕ оплачена и НЕ
            # отправлена в банк, подтягиваем правки суммы/даты, сделанные в iiko: к iiko-сумме
            # (только товарные строки) прибавляем исключённые при пуше строки (персонал/расходные),
            # сохраняя payment_status, staff_amount, source и нормализованные позиции.
            if own_pushed.payment_status == "unpaid" and own_pushed.draft_id is None:
                iiko_amount = _invoice_amount(doc)
                if iiko_amount > 0:
                    new_amount = iiko_amount + await _excluded_push_line_sum(
                        session, own_pushed.id
                    )
                    iiko_date = _parse_iiko_date(
                        doc.get("incomingDate") or doc.get("date")
                    )
                    vat_total, vat_breakdown = _invoice_vat(doc)
                    if own_pushed.amount != new_amount or (
                        iiko_date is not None and own_pushed.invoice_date != iiko_date
                    ):
                        own_pushed.amount = new_amount
                        if iiko_date is not None:
                            own_pushed.invoice_date = iiko_date
                        own_pushed.vat_total = vat_total
                        own_pushed.vat_breakdown = vat_breakdown
                        result.own_pushed_updated += 1
                        continue
            result.skipped_own_pushed += 1
            continue
        # В Cloud партнёр у обоих направлений — поле ``counteragent``.
        supplier_guid = doc.get("counteragent")
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
        number = doc.get("number") or doc.get("transportInvoiceNumber")
        invoice_date = _parse_iiko_date(doc.get("incomingDate") or doc.get("date"))
        due_date = _parse_iiko_date(doc.get("dueDate"))
        # Скалярные поля документа как есть (items — в line_items, вложенное не тащим).
        raw_payload = {
            key: value
            for key, value in doc.items()
            if key != "items" and not isinstance(value, (list, dict))
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
            created = SupplierInvoice(
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
            session.add(created)
            await session.flush()  # id — для материализации строк
            # Позиции — в нормальные строки (редактируемость iiko-накладных).
            await _materialize_iiko_lines(session, created, doc, products, replace=True)
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
            # Пока не оплачена — строки отражают iiko-версию (replace); у оплаченной состав
            # заморожен: только досоздаём, если строк ещё нет (старая синхронизация).
            await _materialize_iiko_lines(
                session, existing, doc, products,
                replace=existing.payment_status == "unpaid",
            )
            if direction == "payable":
                result.invoices_updated += 1
            else:
                result.receivables_updated += 1


async def ingest_iiko_payables(
    session: AsyncSession,
    *,
    suppliers_xml: bytes | str,
    incoming_docs: list[dict],
    outgoing_docs: list[dict] | None = None,
) -> CounterpartyInvoiceSyncResult:
    """Разобрать и апсертнуть фиды: справочник поставщиков (RMS XML) + документы (Cloud JSON)."""
    result = CounterpartyInvoiceSyncResult()
    suppliers = _parse_suppliers(suppliers_xml)
    result.suppliers_seen = len(suppliers)
    # Номенклатура (имена бартерных line_items + привязка материализованных строк) — из
    # локального кэша, не из сети.
    products = await _products_from_cache(
        session, [*(outgoing_docs or []), *incoming_docs]
    )

    # Tombstoned iiko doc ids — intentionally deleted, must not be re-imported. Load once.
    tombstoned = frozenset(
        (await session.scalars(select(SupplierInvoiceTombstone.external_id))).all()
    )

    # Receivables (outgoing) first: they mark partners as barter, so the payables that
    # follow capture line items for those partners in the same run.
    if outgoing_docs is not None:
        result.receivables_seen = len(outgoing_docs)
        await _ingest_documents(
            session,
            outgoing_docs,
            suppliers,
            direction="receivable",
            result=result,
            products=products,
            tombstoned=tombstoned,
        )

    result.invoices_seen = len(incoming_docs)
    await _ingest_documents(
        session,
        incoming_docs,
        suppliers,
        direction="payable",
        result=result,
        products=products,
        tombstoned=tombstoned,
    )

    return result


# --- fetch + orchestration ----------------------------------------------------


def _fetch_cloud_document(direction: str, document_id: str, *, token, opener) -> dict:
    """Полный документ через Cloud ``get`` с ретраем на 429 (rate-limit). Ошибка — исключение:
    незаметно потерять документ хуже, чем уронить прогон (AgentRun зафиксирует error)."""
    delays = iter(_CLOUD_GET_RETRY_DELAYS)
    while True:
        status, resp = get_invoice(
            direction, IIKO_ORGANIZATION_ID, document_id, token=token, opener=opener
        )
        if status == 429:
            delay = next(delays, None)
            if delay is None:
                raise RuntimeError(f"iiko Cloud get {direction}/{document_id}: HTTP 429 (retries)")
            time.sleep(delay)
            continue
        if status != 200 or not isinstance(resp, dict):
            raise RuntimeError(
                f"iiko Cloud get {direction}/{document_id}: HTTP {status}: {str(resp)[:200]}"
            )
        return resp


def _fetch_cloud_documents(
    direction: str, *, date_from: date, date_to: date, token, opener
) -> list[dict]:
    """Cloud ``list`` (сводки) → ``get`` по каждому проведённому. Непроведённые/удалённые сводки
    в ``get`` не ходят — возвращаются мини-доками ``{documentId, status}``, чтобы ingest посчитал
    их в ``skipped_status`` (семантика легаси-экспорта сохранена)."""
    status, resp = list_invoices(
        direction,
        IIKO_ORGANIZATION_ID,
        date_from=date_from.isoformat(),
        date_to=date_to.isoformat(),
        token=token,
        opener=opener,
    )
    if status != 200 or not isinstance(resp, list):
        raise RuntimeError(f"iiko Cloud list {direction}: HTTP {status}: {str(resp)[:200]}")
    documents: list[dict] = []
    for summary in resp:
        doc_id = summary.get("documentId")
        if not doc_id:
            documents.append({"documentId": None, "status": "PROCESSED"})  # → skipped_no_id
            continue
        if summary.get("deleted") is True or summary.get("processed") is not True:
            documents.append(
                {
                    "documentId": doc_id,
                    "status": "DELETED" if summary.get("deleted") else "NEW",
                }
            )
            continue
        time.sleep(_CLOUD_GET_THROTTLE_SECONDS)
        documents.append(_fetch_cloud_document(direction, doc_id, token=token, opener=opener))
    return documents


def fetch_iiko_payables(*, days: int = 30) -> tuple[bytes, list[dict], list[dict]]:
    """Справочник поставщиков (RMS XML) + полные документы обоих направлений (Cloud JSON)."""
    module = _load_orders_module()
    module.load_local_env()
    client = module.IikoClient()
    _status, suppliers_xml = client.request(SUPPLIERS_ENDPOINT)
    today = datetime.now(tz=UTC).date()
    date_from = today - timedelta(days=days)
    opener = iiko_opener()
    token = iiko_auth_token(opener)  # один auth на весь прогон
    incoming_docs = _fetch_cloud_documents(
        "payable", date_from=date_from, date_to=today, token=token, opener=opener
    )
    outgoing_docs = _fetch_cloud_documents(
        "receivable", date_from=date_from, date_to=today, token=token, opener=opener
    )
    return suppliers_xml, incoming_docs, outgoing_docs


def fetch_iiko_suppliers() -> bytes:
    """Lightweight fetch of just the iiko ``/suppliers`` directory (no invoices) — for the
    onboarding picker, where invoices/products would be wasted network round-trips."""
    module = _load_orders_module()
    module.load_local_env()
    client = module.IikoClient()
    _status, suppliers_xml = client.request(SUPPLIERS_ENDPOINT)
    return suppliers_xml


async def list_unlinked_iiko_suppliers(session: AsyncSession) -> list[dict[str, str | None]]:
    """iiko suppliers from the directory that have no counterparty yet — onboarding candidates.

    A prepayment-only supplier (we pay first, goods arrive later) never appears on a posted
    invoice in the sync window, so the reverse sync never auto-creates it. This lets a manager
    pick such a supplier and create the counterparty + iiko alias up front. Excludes deleted,
    store-representing, and already-linked (alias source='iiko') suppliers.
    """
    await _load_source_credential_env(session)
    suppliers_xml = await anyio.to_thread.run_sync(fetch_iiko_suppliers)
    suppliers = _parse_suppliers(suppliers_xml)
    linked = {
        alias.lower()
        for alias in (
            await session.scalars(
                select(CounterpartyAlias.alias).where(CounterpartyAlias.source == IIKO_SOURCE)
            )
        ).all()
    }
    candidates = [
        {"guid": sp.id, "name": sp.name, "inn": sp.inn}
        for sp in suppliers.values()
        if not sp.deleted and not sp.represents_store and sp.id.lower() not in linked
    ]
    candidates.sort(key=lambda item: (item["name"] or "").lower())
    return candidates


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
        suppliers_xml, incoming_docs, outgoing_docs = await anyio.to_thread.run_sync(
            lambda: fetch_iiko_payables(days=days)
        )
        result = await ingest_iiko_payables(
            session,
            suppliers_xml=suppliers_xml,
            incoming_docs=incoming_docs,
            outgoing_docs=outgoing_docs,
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

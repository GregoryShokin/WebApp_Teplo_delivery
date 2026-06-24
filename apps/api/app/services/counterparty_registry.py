"""Read queries and registry/profile/category management for the counterparties page.

Thin data layer over the payable models: inbox of invoices, ledger registry with
unpaid aggregates, counterparty card, manual invoice entry, profile + requisites
(including best-effort autofill of requisites from past bank operations by INN).
"""

from __future__ import annotations

import calendar
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    BankOperation,
    Counterparty,
    CounterpartyAlias,
    CounterpartyCollectionSource,
    CounterpartyLedgerCategory,
    CounterpartyPayableProfile,
    CounterpartyPaymentDraft,
    CounterpartyRole,
    CounterpartyRoutingRule,
    DdsArticle,
    InvoicePaymentAllocation,
    SupplierInvoice,
    Wallet,
)

COLLECTION_SOURCE_KINDS = frozenset({"iiko", "email", "telegram", "manual"})
RELATIONSHIP_KINDS = frozenset({"official", "informal", "barter"})
ARCHIVED_STATUS = "archived"

OPEN_STATUSES = ("unpaid", "partially_paid")


class CounterpartyRegistryError(RuntimeError):
    """Domain error for registry operations (maps to HTTP 409)."""


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _due_from_day_of_month(invoice_date: date, day: int) -> date:
    def clamp(year: int, month: int, target_day: int) -> date:
        last = calendar.monthrange(year, month)[1]
        return date(year, month, min(target_day, last))

    candidate = clamp(invoice_date.year, invoice_date.month, day)
    if candidate < invoice_date:
        year, month = invoice_date.year, invoice_date.month
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        candidate = clamp(year, month, day)
    return candidate


def compute_invoice_due_date(
    invoice_date: date | None,
    *,
    delay_days: int | None,
    due_day_of_month: int | None,
) -> date | None:
    """Derive a due date from supplier terms: N days after delivery, else day-of-month."""
    if invoice_date is None:
        return None
    if delay_days is not None:
        return invoice_date + timedelta(days=delay_days)
    if due_day_of_month is not None:
        return _due_from_day_of_month(invoice_date, due_day_of_month)
    return None


def _normalize_vat_breakdown(
    vat_breakdown: dict[str, Any] | None,
) -> tuple[dict[str, str], Decimal]:
    """Clean a rate->amount map: drop empty/zero, stringify amounts, return total."""
    clean: dict[str, str] = {}
    total = Decimal("0.00")
    for rate, value in (vat_breakdown or {}).items():
        rate_key = str(rate).strip()
        amount = _money(value)
        if not rate_key or amount <= 0:
            continue
        clean[rate_key] = str(amount)
        total += amount
    return clean, _money(total)


# --- dataclasses --------------------------------------------------------------


@dataclass
class InvoiceItem:
    id: uuid.UUID
    counterparty_id: uuid.UUID
    counterparty_name: str
    ledger_category_id: uuid.UUID | None
    source: str
    direction: str
    number: str | None
    invoice_date: date | None
    due_date: date | None
    amount: Decimal
    vat_total: Decimal
    vat_breakdown: dict[str, Any]
    allocated: Decimal
    remaining: Decimal
    payment_status: str
    draft_id: uuid.UUID | None
    barter_settlement_id: uuid.UUID | None = None
    # Barter role once settled: "loan" / "return" (by chronology) — None while open or
    # non-barter. Lets the badge say «мы заняли»/«нам вернули» rather than guess by direction.
    barter_role: str | None = None
    iiko_push_status: str = "not_pushed"


@dataclass
class RegistryItem:
    counterparty_id: uuid.UUID
    name: str
    inn: str | None
    status: str
    relationship: str
    ledger_category_id: uuid.UUID | None
    brand_group: str | None
    internal_name: str | None
    payment_delay_days: int | None
    requisites_verified: bool
    kassa_enabled: bool
    has_iiko_guid: bool
    unpaid_count: int
    unpaid_remaining: Decimal
    # Barter: open receivables (they owe us). Net balance = unpaid_remaining − receivable_remaining.
    receivable_remaining: Decimal


@dataclass
class CounterpartyCard:
    counterparty_id: uuid.UUID
    name: str
    inn: str | None
    type: str
    status: str
    relationship: str
    # Barter net (signed): + we owe them, − they owe us. 0 for non-barter.
    barter_balance: Decimal
    profile: dict[str, Any] | None
    aliases: list[dict[str, Any]] = field(default_factory=list)
    collection_sources: list[dict[str, Any]] = field(default_factory=list)
    routing_rules: list[dict[str, Any]] = field(default_factory=list)
    invoices: list[InvoiceItem] = field(default_factory=list)
    drafts: list[dict[str, Any]] = field(default_factory=list)


# --- allocation aggregates ----------------------------------------------------


async def _allocations_by_invoice(
    session: AsyncSession, invoice_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, Decimal]:
    if not invoice_ids:
        return {}
    rows = await session.execute(
        select(
            InvoicePaymentAllocation.invoice_id,
            func.coalesce(func.sum(InvoicePaymentAllocation.amount), 0),
        )
        .where(InvoicePaymentAllocation.invoice_id.in_(list(invoice_ids)))
        .group_by(InvoicePaymentAllocation.invoice_id)
    )
    return {invoice_id: _money(total) for invoice_id, total in rows}


# --- invoices inbox -----------------------------------------------------------


async def list_invoices(
    session: AsyncSession,
    *,
    statuses: Sequence[str] | None = None,
    counterparty_id: uuid.UUID | None = None,
    category_id: uuid.UUID | None = None,
    in_draft: bool | None = None,
    direction: str | None = None,
    relationship: str | None = None,
    source: str | None = None,
) -> list[InvoiceItem]:
    query = (
        select(SupplierInvoice, Counterparty.name, CounterpartyPayableProfile.ledger_category_id)
        .join(Counterparty, Counterparty.id == SupplierInvoice.counterparty_id)
        .join(
            CounterpartyPayableProfile,
            CounterpartyPayableProfile.counterparty_id == SupplierInvoice.counterparty_id,
            isouter=True,
        )
    )
    if statuses:
        query = query.where(SupplierInvoice.payment_status.in_(tuple(statuses)))
    if direction is not None:
        query = query.where(SupplierInvoice.direction == direction)
    if source is not None:
        query = query.where(SupplierInvoice.source == source)
    if counterparty_id is not None:
        query = query.where(SupplierInvoice.counterparty_id == counterparty_id)
    if category_id is not None:
        query = query.where(CounterpartyPayableProfile.ledger_category_id == category_id)
    if relationship == "non_barter":
        # «Обычная» накладная-инбокс: всё, кроме бартера (официальные + неофициальные).
        query = query.where(CounterpartyPayableProfile.relationship != "barter")
    elif relationship is not None:
        query = query.where(CounterpartyPayableProfile.relationship == relationship)
    if in_draft is True:
        query = query.where(SupplierInvoice.draft_id.isnot(None))
    elif in_draft is False:
        query = query.where(SupplierInvoice.draft_id.is_(None))
    # Чистый хронологический порядок по дате накладной (новые сверху). НЕ сортируем по
    # due_date: срок оплаты есть только у поставщиков с условиями/реквизитами, поэтому
    # ключ «due_date NULLS LAST» задирал их наверх, а закуп без реквизитов уезжал вниз.
    query = query.order_by(
        SupplierInvoice.invoice_date.desc().nulls_last(),
        SupplierInvoice.issued_at.desc().nulls_last(),
        SupplierInvoice.id.desc(),
    )

    rows = list(await session.execute(query))
    invoices = [row[0] for row in rows]
    allocations = await _allocations_by_invoice(session, [inv.id for inv in invoices])
    items = [
        _build_invoice_item(
            invoice, counterparty_name, ledger_category_id, allocations.get(invoice.id)
        )
        for invoice, counterparty_name, ledger_category_id in rows
    ]
    # Attach the barter role (loan/return by chronology) to settled barter invoices so the
    # inbox/card badge reflects who lent — not the raw приход/расход direction.
    from app.services import counterparty_barter_match as barter_match

    settlement_ids = [inv.barter_settlement_id for inv in invoices if inv.barter_settlement_id]
    if settlement_ids:
        roles = await barter_match.settled_roles(session, settlement_ids)
        for item in items:
            item.barter_role = roles.get(item.id)
    return items


def _build_invoice_item(
    invoice: SupplierInvoice,
    counterparty_name: str,
    ledger_category_id: uuid.UUID | None,
    allocated: Decimal | None,
) -> InvoiceItem:
    amount = _money(invoice.amount)
    allocated_money = _money(allocated)
    return InvoiceItem(
        id=invoice.id,
        counterparty_id=invoice.counterparty_id,
        counterparty_name=counterparty_name,
        ledger_category_id=ledger_category_id,
        source=invoice.source,
        direction=invoice.direction,
        number=invoice.number,
        invoice_date=invoice.invoice_date,
        due_date=invoice.due_date,
        amount=amount,
        vat_total=_money(invoice.vat_total),
        vat_breakdown=invoice.vat_breakdown or {},
        allocated=allocated_money,
        remaining=_money(amount - allocated_money),
        payment_status=invoice.payment_status,
        draft_id=invoice.draft_id,
        barter_settlement_id=invoice.barter_settlement_id,
        iiko_push_status=invoice.iiko_push_status,
    )


async def get_invoice_item(session: AsyncSession, invoice_id: uuid.UUID) -> InvoiceItem | None:
    row = (
        await session.execute(
            select(
                SupplierInvoice,
                Counterparty.name,
                CounterpartyPayableProfile.ledger_category_id,
            )
            .join(Counterparty, Counterparty.id == SupplierInvoice.counterparty_id)
            .join(
                CounterpartyPayableProfile,
                CounterpartyPayableProfile.counterparty_id == SupplierInvoice.counterparty_id,
                isouter=True,
            )
            .where(SupplierInvoice.id == invoice_id)
        )
    ).first()
    if row is None:
        return None
    invoice, counterparty_name, ledger_category_id = row
    allocations = await _allocations_by_invoice(session, [invoice.id])
    return _build_invoice_item(
        invoice, counterparty_name, ledger_category_id, allocations.get(invoice.id)
    )


# --- registry -----------------------------------------------------------------


async def list_registry(
    session: AsyncSession,
    *,
    category_id: uuid.UUID | None = None,
    include_archived: bool = False,
    kassa_only: bool = False,
) -> list[RegistryItem]:
    supplier_ids = select(CounterpartyRole.counterparty_id).where(
        CounterpartyRole.role == "supplier"
    )
    query = (
        select(Counterparty, CounterpartyPayableProfile)
        .join(
            CounterpartyPayableProfile,
            CounterpartyPayableProfile.counterparty_id == Counterparty.id,
            isouter=True,
        )
        .where(Counterparty.id.in_(supplier_ids))
    )
    if not include_archived:
        query = query.where(Counterparty.status != ARCHIVED_STATUS)
    if category_id is not None:
        query = query.where(CounterpartyPayableProfile.ledger_category_id == category_id)
    if kassa_only:
        # Только помеченные «Активен в Кассе» (NULL-профили отсекаются автоматически).
        query = query.where(CounterpartyPayableProfile.kassa_enabled.is_(True))
    rows = list(await session.execute(query))

    # Контрагенты, сматченные с iiko (есть alias source='iiko') — нужно для оплаты накладной.
    have_guid = {
        cp_id
        for (cp_id,) in await session.execute(
            select(CounterpartyAlias.counterparty_id)
            .where(CounterpartyAlias.source == "iiko")
            .distinct()
        )
    }

    # Open invoices remaining per counterparty, split by direction (payable vs receivable).
    open_rows = await session.execute(
        select(
            SupplierInvoice.counterparty_id,
            SupplierInvoice.direction,
            func.count(SupplierInvoice.id),
            func.coalesce(func.sum(SupplierInvoice.amount), 0),
        )
        .where(SupplierInvoice.payment_status.in_(OPEN_STATUSES))
        .group_by(SupplierInvoice.counterparty_id, SupplierInvoice.direction)
    )
    open_by_dir: dict[tuple[uuid.UUID, str], tuple[int, Decimal]] = {
        (cp_id, direction): (int(count), _money(total))
        for cp_id, direction, count, total in open_rows
    }
    alloc_rows = await session.execute(
        select(
            SupplierInvoice.counterparty_id,
            SupplierInvoice.direction,
            func.coalesce(func.sum(InvoicePaymentAllocation.amount), 0),
        )
        .join(InvoicePaymentAllocation, InvoicePaymentAllocation.invoice_id == SupplierInvoice.id)
        .where(SupplierInvoice.payment_status.in_(OPEN_STATUSES))
        .group_by(SupplierInvoice.counterparty_id, SupplierInvoice.direction)
    )
    alloc_by_dir: dict[tuple[uuid.UUID, str], Decimal] = {
        (cp_id, direction): _money(total) for cp_id, direction, total in alloc_rows
    }

    def _remaining(cp_id: uuid.UUID, direction: str) -> tuple[int, Decimal]:
        count, total = open_by_dir.get((cp_id, direction), (0, Decimal("0.00")))
        rem = _money(total - alloc_by_dir.get((cp_id, direction), Decimal("0.00")))
        return count, rem

    items: list[RegistryItem] = []
    for counterparty, profile in rows:
        payable_count, payable_remaining = _remaining(counterparty.id, "payable")
        _r_count, receivable_remaining = _remaining(counterparty.id, "receivable")
        items.append(
            RegistryItem(
                counterparty_id=counterparty.id,
                name=counterparty.name,
                inn=counterparty.inn,
                status=counterparty.status,
                relationship=profile.relationship if profile else "official",
                ledger_category_id=profile.ledger_category_id if profile else None,
                brand_group=profile.brand_group if profile else None,
                internal_name=profile.internal_name if profile else None,
                payment_delay_days=profile.payment_delay_days if profile else None,
                requisites_verified=bool(profile.requisites_verified) if profile else False,
                kassa_enabled=bool(profile.kassa_enabled) if profile else False,
                has_iiko_guid=counterparty.id in have_guid,
                unpaid_count=payable_count,
                unpaid_remaining=payable_remaining,
                receivable_remaining=receivable_remaining,
            )
        )
    items.sort(key=lambda item: item.name.lower())
    return items


# --- card ---------------------------------------------------------------------


async def get_counterparty_card(
    session: AsyncSession, counterparty_id: uuid.UUID
) -> CounterpartyCard | None:
    counterparty = await session.get(Counterparty, counterparty_id)
    if counterparty is None:
        return None
    profile = await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == counterparty_id
        )
    )
    aliases = list(
        (
            await session.execute(
                select(CounterpartyAlias).where(
                    CounterpartyAlias.counterparty_id == counterparty_id
                )
            )
        )
        .scalars()
        .all()
    )
    sources = await list_collection_sources(session, counterparty_id)
    routing = await list_routing_rules(session, counterparty_id)
    invoices = await list_invoices(session, counterparty_id=counterparty_id)
    # Barter net (signed): open payables we owe − open receivables they owe us.
    barter_balance = _money(
        sum(
            (
                item.remaining if item.direction == "payable" else -item.remaining
                for item in invoices
                if item.payment_status in OPEN_STATUSES
            ),
            Decimal("0.00"),
        )
    )
    drafts = list(
        (
            await session.execute(
                select(CounterpartyPaymentDraft)
                .where(CounterpartyPaymentDraft.counterparty_id == counterparty_id)
                .order_by(CounterpartyPaymentDraft.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return CounterpartyCard(
        counterparty_id=counterparty.id,
        name=counterparty.name,
        inn=counterparty.inn,
        type=counterparty.type,
        status=counterparty.status,
        relationship=profile.relationship if profile else "official",
        barter_balance=barter_balance,
        profile=_profile_dict(profile),
        aliases=[{"alias": a.alias, "source": a.source} for a in aliases],
        collection_sources=[
            {
                "id": s.id,
                "kind": s.kind,
                "value": s.value,
                "is_active": s.is_active,
                "note": s.note,
            }
            for s in sources
        ],
        routing_rules=routing,
        invoices=invoices,
        drafts=[
            {
                "id": d.id,
                "amount": _money(d.amount),
                "status": d.status,
                "provider_ref": d.provider_ref,
                "created_at": d.created_at,
            }
            for d in drafts
        ],
    )


def _profile_dict(profile: CounterpartyPayableProfile | None) -> dict[str, Any] | None:
    if profile is None:
        return None
    return {
        "ledger_category_id": profile.ledger_category_id,
        "relationship": profile.relationship,
        "brand_group": profile.brand_group,
        "internal_name": profile.internal_name,
        "payment_delay_days": profile.payment_delay_days,
        "payment_due_day_of_month": profile.payment_due_day_of_month,
        "manager_name": profile.manager_name,
        "manager_phone": profile.manager_phone,
        "requisites": profile.requisites or {},
        "requisites_verified": profile.requisites_verified,
        "kassa_enabled": profile.kassa_enabled,
        "status": profile.status,
    }


# --- profile / requisites -----------------------------------------------------


async def _get_or_create_profile(
    session: AsyncSession, counterparty_id: uuid.UUID
) -> CounterpartyPayableProfile:
    profile = await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == counterparty_id
        )
    )
    if profile is None:
        profile = CounterpartyPayableProfile(counterparty_id=counterparty_id)
        session.add(profile)
        await session.flush()
    return profile


async def update_profile(
    session: AsyncSession,
    counterparty_id: uuid.UUID,
    *,
    ledger_category_id: uuid.UUID | None = None,
    relationship: str | None = None,
    brand_group: str | None = None,
    internal_name: str | None = None,
    payment_delay_days: int | None = None,
    payment_due_day_of_month: int | None = None,
    manager_name: str | None = None,
    manager_phone: str | None = None,
    status: str | None = None,
) -> CounterpartyPayableProfile:
    counterparty = await session.get(Counterparty, counterparty_id)
    if counterparty is None:
        raise CounterpartyRegistryError("Контрагент не найден")
    if relationship is not None and relationship not in RELATIONSHIP_KINDS:
        raise CounterpartyRegistryError("Неизвестный тип контрагента")
    profile = await _get_or_create_profile(session, counterparty_id)
    profile.ledger_category_id = ledger_category_id
    if relationship is not None:
        profile.relationship = relationship
    profile.brand_group = brand_group
    profile.internal_name = internal_name
    profile.payment_delay_days = payment_delay_days
    profile.payment_due_day_of_month = payment_due_day_of_month
    profile.manager_name = manager_name
    profile.manager_phone = manager_phone
    if status:
        profile.status = status
    await session.commit()
    await session.refresh(profile)
    return profile


async def set_kassa_enabled(
    session: AsyncSession, counterparty_id: uuid.UUID, *, enabled: bool
) -> CounterpartyPayableProfile:
    """Переключить признак «Активен в Кассе» (видимость в дропдауне накладной Кассы)."""
    counterparty = await session.get(Counterparty, counterparty_id)
    if counterparty is None:
        raise CounterpartyRegistryError("Контрагент не найден")
    profile = await _get_or_create_profile(session, counterparty_id)
    profile.kassa_enabled = enabled
    await session.commit()
    await session.refresh(profile)
    return profile


async def set_requisites(
    session: AsyncSession,
    counterparty_id: uuid.UUID,
    *,
    requisites: dict[str, Any],
    verified: bool,
    actor_user_id: uuid.UUID | None,
) -> CounterpartyPayableProfile:
    counterparty = await session.get(Counterparty, counterparty_id)
    if counterparty is None:
        raise CounterpartyRegistryError("Контрагент не найден")
    profile = await _get_or_create_profile(session, counterparty_id)
    profile.requisites = dict(requisites or {})
    profile.requisites_verified = verified
    if verified:
        profile.requisites_verified_at = datetime.now(tz=UTC)
        profile.requisites_verified_by_user_id = actor_user_id
    else:
        profile.requisites_verified_at = None
        profile.requisites_verified_by_user_id = None
    await session.commit()
    await session.refresh(profile)
    return profile


async def autofill_requisites_from_bank(
    session: AsyncSession, counterparty_id: uuid.UUID
) -> dict[str, Any]:
    """Best-effort suggestion of requisites from the latest outgoing bank op by INN."""
    counterparty = await session.get(Counterparty, counterparty_id)
    if counterparty is None or not counterparty.inn:
        return {}
    operation = await session.scalar(
        select(BankOperation)
        .where(
            BankOperation.direction == "out",
            BankOperation.counterparty_inn_raw == counterparty.inn,
        )
        .order_by(BankOperation.operation_date.desc())
        .limit(1)
    )
    suggestion: dict[str, Any] = {"recipientName": counterparty.name, "inn": counterparty.inn}
    if operation is None:
        return suggestion
    payload = operation.raw_payload or {}
    receiver = payload.get("receiver") if isinstance(payload.get("receiver"), dict) else {}

    def pick(*keys: str) -> Any:
        for source in (receiver, payload):
            for key in keys:
                value = source.get(key)
                if value not in (None, "") and not isinstance(value, (dict, list)):
                    return str(value)
        return None

    for target, keys in {
        "kpp": ("kpp", "recipientKpp"),
        "bankAcnt": ("acct", "account", "accountNumber", "bankAccount"),
        "bankBik": ("bic", "bik", "bankBic", "bankBik"),
        "recipientCorrAccountNumber": ("corrAccount", "bankCorrAccount", "correspondentAccount"),
    }.items():
        value = pick(*keys)
        if value:
            suggestion[target] = value
    if operation.counterparty_account_raw:
        suggestion.setdefault("bankAcnt", operation.counterparty_account_raw)
    return suggestion


# --- manual invoice -----------------------------------------------------------


async def create_manual_invoice(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    amount: Decimal,
    number: str | None,
    invoice_date: date | None,
    due_date: date | None,
    note: str | None,
    vat_breakdown: dict[str, Any] | None = None,
    actor_user_id: uuid.UUID | None,
) -> SupplierInvoice:
    counterparty = await session.get(Counterparty, counterparty_id)
    if counterparty is None:
        raise CounterpartyRegistryError("Контрагент не найден")
    if counterparty.status == ARCHIVED_STATUS:
        raise CounterpartyRegistryError("Контрагент в архиве — новые накладные недоступны")
    if _money(amount) <= 0:
        raise CounterpartyRegistryError("Сумма должна быть больше нуля")
    clean_vat, vat_total = _normalize_vat_breakdown(vat_breakdown)
    if vat_total > _money(amount):
        raise CounterpartyRegistryError("Сумма НДС не может превышать сумму накладной")
    if due_date is None and invoice_date is not None:
        terms = (
            await session.execute(
                select(
                    CounterpartyPayableProfile.payment_delay_days,
                    CounterpartyPayableProfile.payment_due_day_of_month,
                ).where(CounterpartyPayableProfile.counterparty_id == counterparty_id)
            )
        ).first()
        if terms is not None:
            due_date = compute_invoice_due_date(
                invoice_date, delay_days=terms[0], due_day_of_month=terms[1]
            )
    invoice = SupplierInvoice(
        counterparty_id=counterparty_id,
        source="manual",
        amount=_money(amount),
        vat_total=vat_total,
        vat_breakdown=clean_vat,
        number=number,
        invoice_date=invoice_date,
        due_date=due_date,
        note=note,
        payment_status="unpaid",
        created_by_user_id=actor_user_id,
    )
    session.add(invoice)
    await session.commit()
    await session.refresh(invoice)
    return invoice


async def void_invoice(session: AsyncSession, invoice_id: uuid.UUID) -> SupplierInvoice:
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        raise CounterpartyRegistryError("Накладная не найдена")
    if invoice.draft_id is not None:
        raise CounterpartyRegistryError("Накладная отправлена в банк — сначала отмените черновик")
    has_allocations = await session.scalar(
        select(InvoicePaymentAllocation.id)
        .where(InvoicePaymentAllocation.invoice_id == invoice_id)
        .limit(1)
    )
    if has_allocations is not None:
        raise CounterpartyRegistryError("По накладной есть оплаты — аннулирование недоступно")
    invoice.payment_status = "void"
    await session.commit()
    await session.refresh(invoice)
    return invoice


# --- categories ---------------------------------------------------------------


async def list_categories(
    session: AsyncSession, *, include_inactive: bool = False
) -> list[CounterpartyLedgerCategory]:
    query = select(CounterpartyLedgerCategory).order_by(
        CounterpartyLedgerCategory.sort_order, CounterpartyLedgerCategory.name
    )
    if not include_inactive:
        query = query.where(CounterpartyLedgerCategory.is_active.is_(True))
    return list((await session.execute(query)).scalars().all())


async def create_category(
    session: AsyncSession, *, code: str, name: str, sort_order: int = 0
) -> CounterpartyLedgerCategory:
    existing = await session.scalar(
        select(CounterpartyLedgerCategory).where(CounterpartyLedgerCategory.code == code)
    )
    if existing is not None:
        raise CounterpartyRegistryError("Категория с таким кодом уже существует")
    category = CounterpartyLedgerCategory(code=code, name=name, sort_order=sort_order)
    session.add(category)
    await session.commit()
    await session.refresh(category)
    return category


async def update_category(
    session: AsyncSession,
    category_id: uuid.UUID,
    *,
    name: str | None = None,
    sort_order: int | None = None,
    is_active: bool | None = None,
) -> CounterpartyLedgerCategory:
    category = await session.get(CounterpartyLedgerCategory, category_id)
    if category is None:
        raise CounterpartyRegistryError("Категория не найдена")
    if name is not None:
        category.name = name
    if sort_order is not None:
        category.sort_order = sort_order
    if is_active is not None:
        category.is_active = is_active
    await session.commit()
    await session.refresh(category)
    return category


# --- counterparty create / archive / needs-setup ------------------------------


async def create_counterparty(
    session: AsyncSession,
    *,
    name: str,
    inn: str | None,
    cp_type: str,
    relationship: str = "official",
    internal_name: str | None = None,
    ledger_category_id: uuid.UUID | None = None,
    brand_group: str | None = None,
    payment_delay_days: int | None = None,
    payment_due_day_of_month: int | None = None,
    manager_name: str | None = None,
    manager_phone: str | None = None,
) -> Counterparty:
    clean_name = (name or "").strip()
    if not clean_name:
        raise CounterpartyRegistryError("Укажите название контрагента")
    clean_inn = (inn or "").strip() or None
    if clean_inn:
        existing = await session.scalar(select(Counterparty).where(Counterparty.inn == clean_inn))
        if existing is not None:
            raise CounterpartyRegistryError("Контрагент с таким ИНН уже существует")
    counterparty = Counterparty(name=clean_name, inn=clean_inn, type=cp_type, status="active")
    session.add(counterparty)
    await session.flush()
    session.add(CounterpartyRole(counterparty_id=counterparty.id, role="supplier"))
    session.add(
        CounterpartyPayableProfile(
            counterparty_id=counterparty.id,
            relationship=relationship,
            internal_name=internal_name,
            ledger_category_id=ledger_category_id,
            brand_group=brand_group,
            payment_delay_days=payment_delay_days,
            payment_due_day_of_month=payment_due_day_of_month,
            manager_name=manager_name,
            manager_phone=manager_phone,
        )
    )
    await session.commit()
    await session.refresh(counterparty)
    return counterparty


async def set_counterparty_archived(
    session: AsyncSession, counterparty_id: uuid.UUID, *, archived: bool
) -> Counterparty:
    counterparty = await session.get(Counterparty, counterparty_id)
    if counterparty is None:
        raise CounterpartyRegistryError("Контрагент не найден")
    if archived:
        in_draft = await session.scalar(
            select(SupplierInvoice.id)
            .where(
                SupplierInvoice.counterparty_id == counterparty_id,
                SupplierInvoice.draft_id.isnot(None),
            )
            .limit(1)
        )
        if in_draft is not None:
            raise CounterpartyRegistryError(
                "У контрагента есть накладные, отправленные в банк — сначала закройте черновики"
            )
        counterparty.status = ARCHIVED_STATUS
    else:
        counterparty.status = "active"
    await session.commit()
    await session.refresh(counterparty)
    return counterparty


async def list_needs_setup(session: AsyncSession) -> list[dict[str, Any]]:
    """Suppliers auto-created by iiko sync that still need a manager to fill data in."""
    rows = list(
        (
            await session.execute(
                select(Counterparty)
                .where(Counterparty.status == "requires_setup")
                .order_by(Counterparty.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        {"counterparty_id": cp.id, "name": cp.name, "inn": cp.inn, "created_at": cp.created_at}
        for cp in rows
    ]


# --- collection sources -------------------------------------------------------


async def list_collection_sources(
    session: AsyncSession, counterparty_id: uuid.UUID
) -> list[CounterpartyCollectionSource]:
    return list(
        (
            await session.execute(
                select(CounterpartyCollectionSource)
                .where(CounterpartyCollectionSource.counterparty_id == counterparty_id)
                .order_by(CounterpartyCollectionSource.kind, CounterpartyCollectionSource.value)
            )
        )
        .scalars()
        .all()
    )


async def add_collection_source(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    kind: str,
    value: str | None,
    note: str | None = None,
) -> CounterpartyCollectionSource:
    counterparty = await session.get(Counterparty, counterparty_id)
    if counterparty is None:
        raise CounterpartyRegistryError("Контрагент не найден")
    normalized_kind = (kind or "").strip().lower()
    if normalized_kind not in COLLECTION_SOURCE_KINDS:
        raise CounterpartyRegistryError("Неизвестный тип источника")
    clean_value = (value or "").strip() or None
    if normalized_kind in {"email", "telegram"} and not clean_value:
        raise CounterpartyRegistryError("Для email/telegram укажите адрес или хэндл")
    if normalized_kind == "email" and clean_value:
        clean_value = clean_value.lower()
    if clean_value:
        clash = await session.scalar(
            select(CounterpartyCollectionSource).where(
                func.lower(CounterpartyCollectionSource.value) == clean_value.lower(),
                CounterpartyCollectionSource.counterparty_id != counterparty_id,
            )
        )
        if clash is not None:
            raise CounterpartyRegistryError("Этот источник уже привязан к другому контрагенту")
    source = CounterpartyCollectionSource(
        counterparty_id=counterparty_id, kind=normalized_kind, value=clean_value, note=note
    )
    session.add(source)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise CounterpartyRegistryError("Такой источник уже добавлен") from exc
    await session.refresh(source)
    return source


async def remove_collection_source(session: AsyncSession, source_id: uuid.UUID) -> None:
    source = await session.get(CounterpartyCollectionSource, source_id)
    if source is None:
        raise CounterpartyRegistryError("Источник не найден")
    await session.delete(source)
    await session.commit()


# --- brand routing rules ------------------------------------------------------


async def _counterparty_iiko_guids(session: AsyncSession, counterparty_id: uuid.UUID) -> list[str]:
    return list(
        (
            await session.execute(
                select(CounterpartyAlias.alias).where(
                    CounterpartyAlias.counterparty_id == counterparty_id,
                    CounterpartyAlias.source == "iiko",
                )
            )
        )
        .scalars()
        .all()
    )


async def list_routing_rules(
    session: AsyncSession, counterparty_id: uuid.UUID
) -> list[dict[str, Any]]:
    guids = await _counterparty_iiko_guids(session, counterparty_id)
    if not guids:
        return []
    rows = (
        await session.execute(
            select(CounterpartyRoutingRule, Counterparty.name)
            .join(Counterparty, Counterparty.id == CounterpartyRoutingRule.counterparty_id)
            .where(CounterpartyRoutingRule.iiko_supplier_guid.in_(guids))
            .order_by(CounterpartyRoutingRule.prefix)
        )
    ).all()
    return [
        {
            "id": rule.id,
            "prefix": rule.prefix,
            "target_counterparty_id": rule.counterparty_id,
            "target_name": target_name,
        }
        for rule, target_name in rows
    ]


async def add_routing_rule(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    prefix: str,
    target_counterparty_id: uuid.UUID,
) -> CounterpartyRoutingRule:
    guids = await _counterparty_iiko_guids(session, counterparty_id)
    if not guids:
        raise CounterpartyRegistryError(
            "У контрагента нет привязки к iiko — маршрутизация по префиксу недоступна"
        )
    clean_prefix = (prefix or "").strip()
    if not clean_prefix:
        raise CounterpartyRegistryError("Укажите префикс номера документа")
    target = await session.get(Counterparty, target_counterparty_id)
    if target is None:
        raise CounterpartyRegistryError("Целевое юрлицо не найдено")
    rule = CounterpartyRoutingRule(
        iiko_supplier_guid=guids[0],
        prefix=clean_prefix,
        counterparty_id=target_counterparty_id,
    )
    session.add(rule)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise CounterpartyRegistryError("Правило для этого префикса уже существует") from exc
    await session.refresh(rule)
    return rule


async def remove_routing_rule(session: AsyncSession, rule_id: uuid.UUID) -> None:
    rule = await session.get(CounterpartyRoutingRule, rule_id)
    if rule is None:
        raise CounterpartyRegistryError("Правило не найдено")
    await session.delete(rule)
    await session.commit()


# --- DDS lookups for manual payment ------------------------------------------


async def list_wallets(session: AsyncSession) -> list[Wallet]:
    """Active DDS wallets/accounts selectable as the source of a manual payment."""
    return list(
        (
            await session.execute(
                select(Wallet).where(Wallet.status == "active").order_by(Wallet.name)
            )
        )
        .scalars()
        .all()
    )


async def list_expense_articles(session: AsyncSession) -> list[DdsArticle]:
    """Active outflow DDS articles a manual supplier payment can be booked to."""
    return list(
        (
            await session.execute(
                select(DdsArticle)
                .where(DdsArticle.is_active.is_(True), DdsArticle.movement_type == "outflow")
                .order_by(DdsArticle.name)
            )
        )
        .scalars()
        .all()
    )

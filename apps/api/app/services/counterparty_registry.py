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

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
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
    SbisDocument,
    SupplierInvoice,
    SupplierPrepayment,
    Wallet,
)
from app.services import supplier_service_periods as service_periods

# Минимальный набор для платёжки — общий канон банковского слоя. Здесь он ре-экспортом:
# на ``registry.OFFICIAL_SUPPLIER_REQUIRED_REQUISITES`` ссылается аренда помещений.
from app.services.banking.requisites import (
    OFFICIAL_SUPPLIER_REQUIRED_REQUISITES,
    payee_account_error,
)

COLLECTION_SOURCE_KINDS = frozenset({"iiko", "email", "telegram", "manual", "sbis"})
RELATIONSHIP_KINDS = frozenset({"official", "informal", "barter"})
ARCHIVED_STATUS = "archived"
ARCHIVED_STATUSES = frozenset({ARCHIVED_STATUS, "inactive"})

OPEN_STATUSES = ("unpaid", "partially_paid")
# Кросс-канальный дедуп «почта/ручной ввод ↔ СБИС»: счёт и его закрывающий УПД по одной
# поставке приходят РАЗНЫМИ каналами с разными номерами и датами (пример: счёт
# «0000-002629» письмом и УПД «2653» через ЭДО), поэтому строгий дедуп (сумма+дата+номер)
# их не ловит — пару ловим по контрагенту+сумме в узком окне дат. Окно короче месяца,
# чтобы две помесячные накладные с равной суммой (подписка) НЕ слились.
CROSS_CHANNEL_DEDUP_WINDOW_DAYS = 7
# Банк и налоговая — не поставщики, платёжных реквизитов у них не требуем. Роль при
# создании выводится из типа ровно по этому же правилу (routes/counterparties.py).
NON_SUPPLIER_TYPES = frozenset({"bank", "tax_authority"})


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
    operational_scope: str
    direction: str
    number: str | None
    invoice_date: date | None
    due_date: date | None
    service_period_start: date | None
    service_period_end: date | None
    service_period_status: str
    # Требует ли контрагент период на каждом платеже — берётся из профиля, а не из накладной,
    # чтобы UI показал «нужен период» даже когда флаг включили уже после создания накладной.
    service_period_required: bool
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
    # Текст последней ошибки пуша в iiko — для детализации красного статуса в списке.
    iiko_push_error: str | None = None
    # iiko documentId: заполнен → накладная существует в iiko (для source=iiko — всегда).
    external_id: str | None = None
    # Статус привязанного банковского черновика: created/updated («Отправлено в банк»),
    # paid + pays_via_safe → «Деньги в Сейфе» (наличные ещё не выданы поставщику).
    draft_status: str | None = None
    draft_pays_via_safe: bool = False
    # Контроль ошибочных цен: clean/flagged/confirmed — для подсветки строки в списке
    # («flagged» = подозрительные цены, оплата/банк заблокированы до подтверждения).
    price_control_status: str = "clean"
    # Правило 4 канона: закрывающий документ с будущей датой (activation_status='pending') ещё
    # не долг — платить по нему нельзя (гард в create_payment_draft_for_invoices). Отдаём пару
    # наружу, чтобы UI не предлагал такую накладную к оплате и объяснял, с какой даты можно.
    doc_kind: str = "closing"
    activation_status: str = "active"


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
    # Откуда карточка появилась: iiko / manual / email / sbis (бейдж в реестре).
    origin: str | None
    unpaid_count: int
    unpaid_remaining: Decimal
    # Barter: open receivables (they owe us). Net balance = unpaid_remaining − receivable_remaining.
    receivable_remaining: Decimal
    # Дебиторка предоплат: мы заплатили вперёд, поставщик должен. Σ открытых supplier_prepayment
    # (amount − amount_settled). Держим ОТДЕЛЬНО от барт-receivable_remaining.
    prepayment_balance: Decimal = Decimal("0.00")


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
    counterparty_ids: Sequence[uuid.UUID] | None = None,
    category_id: uuid.UUID | None = None,
    in_draft: bool | None = None,
    direction: str | None = None,
    relationship: str | None = None,
    source: str | None = None,
    operational_scope: str | None = None,
    exclude_sources: Sequence[str] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    not_in_iiko: bool | None = None,
) -> list[InvoiceItem]:
    query = (
        select(
            SupplierInvoice,
            Counterparty.name,
            CounterpartyPayableProfile.ledger_category_id,
            CounterpartyPayableProfile.service_period_required,
        )
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
    if operational_scope is not None:
        query = query.where(SupplierInvoice.operational_scope == operational_scope)
    if exclude_sources:
        # Почтовые счета (непроизводственные услуги) живут на «Странице на оплату», а не
        # среди производственных накладных — исключаем их из этого списка.
        query = query.where(SupplierInvoice.source.not_in(tuple(exclude_sources)))
    if counterparty_id is not None:
        query = query.where(SupplierInvoice.counterparty_id == counterparty_id)
    if counterparty_ids:
        query = query.where(SupplierInvoice.counterparty_id.in_(tuple(counterparty_ids)))
    if date_from is not None:
        query = query.where(SupplierInvoice.invoice_date >= date_from)
    if date_to is not None:
        query = query.where(SupplierInvoice.invoice_date <= date_to)
    if not_in_iiko:
        # «Не в iiko» = наши накладные (созданы вручную / из Кассы), для которых документ
        # в iiko ещё не создан: never-pushed или упавший пуш. source=iiko по определению
        # уже в iiko, а skipped — намеренно без iiko, поэтому оба сюда не попадают.
        query = query.where(
            SupplierInvoice.source.in_(("manual", "kassa_invoice")),
            SupplierInvoice.iiko_push_status.in_(("not_pushed", "failed")),
        )
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
    drafts = await _drafts_by_id(session, [inv.draft_id for inv in invoices if inv.draft_id])
    items = [
        _build_invoice_item(
            invoice,
            counterparty_name,
            ledger_category_id,
            allocations.get(invoice.id),
            drafts.get(invoice.draft_id) if invoice.draft_id else None,
            service_period_required=bool(period_required),
        )
        for invoice, counterparty_name, ledger_category_id, period_required in rows
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


async def _drafts_by_id(
    session: AsyncSession, draft_ids: list[uuid.UUID]
) -> dict[uuid.UUID, CounterpartyPaymentDraft]:
    """Батч черновиков по id — для статуса «Отправлено в банк»/«Деньги в Сейфе» в списке."""
    if not draft_ids:
        return {}
    rows = (
        await session.scalars(
            select(CounterpartyPaymentDraft).where(CounterpartyPaymentDraft.id.in_(set(draft_ids)))
        )
    ).all()
    return {draft.id: draft for draft in rows}


def _build_invoice_item(
    invoice: SupplierInvoice,
    counterparty_name: str,
    ledger_category_id: uuid.UUID | None,
    allocated: Decimal | None,
    draft: CounterpartyPaymentDraft | None = None,
    *,
    service_period_required: bool = False,
) -> InvoiceItem:
    amount = _money(invoice.amount)
    allocated_money = _money(allocated)
    period_status = service_periods.effective_period_status(
        invoice.service_period_status, required=service_period_required
    )
    return InvoiceItem(
        id=invoice.id,
        counterparty_id=invoice.counterparty_id,
        counterparty_name=counterparty_name,
        ledger_category_id=ledger_category_id,
        source=invoice.source,
        operational_scope=invoice.operational_scope,
        direction=invoice.direction,
        number=invoice.number,
        invoice_date=invoice.invoice_date,
        due_date=invoice.due_date,
        service_period_start=invoice.service_period_start,
        service_period_end=invoice.service_period_end,
        service_period_status=period_status,
        service_period_required=service_period_required,
        amount=amount,
        vat_total=_money(invoice.vat_total),
        vat_breakdown=invoice.vat_breakdown or {},
        allocated=allocated_money,
        remaining=_money(amount - allocated_money),
        payment_status=invoice.payment_status,
        draft_id=invoice.draft_id,
        barter_settlement_id=invoice.barter_settlement_id,
        iiko_push_status=invoice.iiko_push_status,
        iiko_push_error=invoice.iiko_push_error,
        external_id=invoice.external_id,
        draft_status=draft.status if draft else None,
        draft_pays_via_safe=bool(draft.pays_via_safe) if draft else False,
        price_control_status=invoice.price_control_status,
        doc_kind=invoice.doc_kind,
        activation_status=invoice.activation_status,
    )


async def get_invoice_item(session: AsyncSession, invoice_id: uuid.UUID) -> InvoiceItem | None:
    row = (
        await session.execute(
            select(
                SupplierInvoice,
                Counterparty.name,
                CounterpartyPayableProfile.ledger_category_id,
                CounterpartyPayableProfile.service_period_required,
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
    invoice, counterparty_name, ledger_category_id, period_required = row
    allocations = await _allocations_by_invoice(session, [invoice.id])
    drafts = await _drafts_by_id(session, [invoice.draft_id] if invoice.draft_id else [])
    return _build_invoice_item(
        invoice,
        counterparty_name,
        ledger_category_id,
        allocations.get(invoice.id),
        drafts.get(invoice.draft_id) if invoice.draft_id else None,
        service_period_required=bool(period_required),
    )


# --- registry -----------------------------------------------------------------


async def list_registry(
    session: AsyncSession,
    *,
    category_id: uuid.UUID | None = None,
    include_archived: bool = False,
    kassa_only: bool = False,
    include_non_suppliers: bool = False,
) -> list[RegistryItem]:
    query = select(Counterparty, CounterpartyPayableProfile).join(
        CounterpartyPayableProfile,
        CounterpartyPayableProfile.counterparty_id == Counterparty.id,
        isouter=True,
    )
    if not include_non_suppliers:
        # По умолчанию — только поставщики: реестр кормит пикеры накладных и платежей,
        # куда банк/налоговая/безролевые карточки попадать не должны (оператор мог бы
        # провести накладную на банк). Полный список — явный opt-in: страница реестра
        # (единственный UI управления всеми карточками) и справочник классификации ДДС.
        supplier_ids = select(CounterpartyRole.counterparty_id).where(
            CounterpartyRole.role == "supplier"
        )
        query = query.where(Counterparty.id.in_(supplier_ids))
    if not include_archived:
        query = query.where(Counterparty.status.notin_(ARCHIVED_STATUSES))
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
    # Дебиторка предоплат по контрагенту: Σ открытых supplier_prepayment (amount − settled).
    prepay_rows = await session.execute(
        select(
            SupplierPrepayment.counterparty_id,
            func.coalesce(
                func.sum(SupplierPrepayment.amount - SupplierPrepayment.amount_settled), 0
            ),
        )
        .where(SupplierPrepayment.status.in_(("open", "partially_settled")))
        .group_by(SupplierPrepayment.counterparty_id)
    )
    prepay_by_cp: dict[uuid.UUID, Decimal] = {cp_id: _money(total) for cp_id, total in prepay_rows}

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
                status=(
                    ARCHIVED_STATUS
                    if counterparty.status in ARCHIVED_STATUSES
                    else counterparty.status
                ),
                relationship=profile.relationship if profile else "official",
                ledger_category_id=profile.ledger_category_id if profile else None,
                brand_group=profile.brand_group if profile else None,
                internal_name=profile.internal_name if profile else None,
                payment_delay_days=profile.payment_delay_days if profile else None,
                requisites_verified=bool(profile.requisites_verified) if profile else False,
                kassa_enabled=bool(profile.kassa_enabled) if profile else False,
                has_iiko_guid=counterparty.id in have_guid,
                origin=counterparty.origin,
                unpaid_count=payable_count,
                unpaid_remaining=payable_remaining,
                receivable_remaining=receivable_remaining,
                prepayment_balance=prepay_by_cp.get(counterparty.id, Decimal("0.00")),
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
        status=ARCHIVED_STATUS if counterparty.status in ARCHIVED_STATUSES else counterparty.status,
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
        "relationship_manual": profile.relationship_manual,
        "brand_group": profile.brand_group,
        "internal_name": profile.internal_name,
        "payment_delay_days": profile.payment_delay_days,
        "payment_due_day_of_month": profile.payment_due_day_of_month,
        "service_period_required": profile.service_period_required,
        "default_service_period_offset_months": profile.default_service_period_offset_months,
        "closing_doc_expected_day": profile.closing_doc_expected_day,
        "settlement_contour": profile.settlement_contour,
        "manager_name": profile.manager_name,
        "manager_phone": profile.manager_phone,
        "default_dds_article_id": profile.default_dds_article_id,
        "confirm_no_dds_article": profile.confirm_no_dds_article,
        "requisites": profile.requisites or {},
        "requisites_verified": profile.requisites_verified,
        "kassa_enabled": profile.kassa_enabled,
        "bank_payments_create_prepayment": profile.bank_payments_create_prepayment,
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


async def activate_configured_placeholder(
    session: AsyncSession,
    counterparty: Counterparty,
    profile: CounterpartyPayableProfile,
) -> bool:
    """Finish setup after the manager saves the required card decision.

    ``requires_setup`` is a queue state of placeholders created by source syncs.  The
    card form used to save the payable profile without ever clearing that state, so a
    fully configured SABY counterparty stayed blocked forever.  A pending SABY document
    also means that SABY is the collection source the manager is configuring; attach it
    in the same transaction so the next sync can route all accumulated documents.
    """
    if counterparty.status != "requires_setup":
        return False
    if profile.default_dds_article_id is None and not profile.confirm_no_dds_article:
        return False

    counterparty.status = "active"

    document_filters = [SbisDocument.counterparty_id == counterparty.id]
    if counterparty.inn:
        document_filters.append(SbisDocument.counterparty_inn == counterparty.inn)
    pending_sbis_document = await session.scalar(
        select(SbisDocument.id)
        .where(
            SbisDocument.intake_status == "new_counterparty",
            or_(*document_filters),
        )
        .limit(1)
    )
    if pending_sbis_document is None:
        return True

    sbis_source = await session.scalar(
        select(CounterpartyCollectionSource.id).where(
            CounterpartyCollectionSource.counterparty_id == counterparty.id,
            CounterpartyCollectionSource.kind == "sbis",
        )
    )
    if sbis_source is None:
        session.add(
            CounterpartyCollectionSource(
                counterparty_id=counterparty.id,
                kind="sbis",
                value=counterparty.inn,
                note="подключено при настройке карточки документа СБИС",
            )
        )
    return True


_UNSET = object()


def _require_official_supplier_requisites(requisites: dict[str, Any]) -> None:
    """Официальному поставщику нужен полный набор реквизитов — иначе платить нечем.

    Одна проверка на создание и на подтверждение реквизитов: раньше правило жило только
    в create_counterparty, и через правку можно было получить контрагента, которого
    нельзя создать — с отметкой «реквизиты проверены» поверх пустого набора.
    """
    missing = [
        label
        for key, label in OFFICIAL_SUPPLIER_REQUIRED_REQUISITES.items()
        if not str(requisites.get(key) or "").strip()
    ]
    if missing:
        raise CounterpartyRegistryError(
            "Заполните обязательные реквизиты официального поставщика: " + ", ".join(missing)
        )


def _require_dds_article_decision(
    default_dds_article_id: uuid.UUID | None, confirm_no_dds_article: bool
) -> None:
    """Статья ДДС — решение, а не пустое поле: либо выбрана, либо явно подтверждено, что её нет.

    Одна проверка на создание и на правку карточки, иначе контуры разъезжаются.
    """
    if default_dds_article_id is None and not confirm_no_dds_article:
        raise CounterpartyRegistryError(
            "Выберите статью ДДС или явно подтвердите, что у контрагента нет статьи"
        )
    if default_dds_article_id is not None and confirm_no_dds_article:
        raise CounterpartyRegistryError(
            "Нельзя одновременно выбрать статью ДДС и указать, что статьи нет"
        )


async def update_profile(
    session: AsyncSession,
    counterparty_id: uuid.UUID,
    *,
    name: str | object = _UNSET,
    inn: str | None | object = _UNSET,
    cp_type: str | object = _UNSET,
    ledger_category_id: uuid.UUID | None | object = _UNSET,
    relationship: str | None | object = _UNSET,
    brand_group: str | None | object = _UNSET,
    internal_name: str | None | object = _UNSET,
    payment_delay_days: int | None | object = _UNSET,
    payment_due_day_of_month: int | None | object = _UNSET,
    manager_name: str | None | object = _UNSET,
    manager_phone: str | None | object = _UNSET,
    default_dds_article_id: uuid.UUID | None | object = _UNSET,
    confirm_no_dds_article: bool = False,
    service_period_required: bool | None | object = _UNSET,
    default_service_period_offset_months: int | None | object = _UNSET,
    bank_payments_create_prepayment: bool | None | object = _UNSET,
    closing_doc_expected_day: int | None | object = _UNSET,
    settlement_contour: str | None | object = _UNSET,
    status: str | None | object = _UNSET,
) -> CounterpartyPayableProfile:
    counterparty = await session.get(Counterparty, counterparty_id)
    if counterparty is None:
        raise CounterpartyRegistryError("Контрагент не найден")
    # Решение по статье ДДС проверяем только когда её реально меняют: у PATCH
    # незаданное поле значит «не трогать», а не «сбросить в NULL».
    if default_dds_article_id is not _UNSET:
        _require_dds_article_decision(default_dds_article_id, confirm_no_dds_article)
    if (
        relationship is not _UNSET
        and relationship is not None
        and relationship not in RELATIONSHIP_KINDS
    ):
        raise CounterpartyRegistryError("Неизвестный тип контрагента")
    if name is not _UNSET:
        clean_name = str(name or "").strip()
        if not clean_name:
            raise CounterpartyRegistryError("Укажите название контрагента")
        counterparty.name = clean_name
    if inn is not _UNSET:
        clean_inn = str(inn or "").strip() or None
        if clean_inn and clean_inn != counterparty.inn:
            duplicate = await session.scalar(
                select(Counterparty.id).where(
                    Counterparty.inn == clean_inn,
                    Counterparty.id != counterparty_id,
                )
            )
            if duplicate is not None:
                raise CounterpartyRegistryError("Контрагент с таким ИНН уже существует")
        counterparty.inn = clean_inn
    if cp_type is not _UNSET:
        counterparty.type = str(cp_type)
    profile = await _get_or_create_profile(session, counterparty_id)
    if ledger_category_id is not _UNSET:
        profile.ledger_category_id = ledger_category_id
    if relationship is not _UNSET and relationship is not None:
        # Явная смена типа в карточке закрепляет выбор: оформление займа больше не перебьёт
        # его обратно в barter (см. warehouse_invoices._ensure_barter_relationship; авто-пометка
        # по расходной из iiko-синка снята 19.07.2026). Сохранение без изменения типа (правили
        # другие поля) замок не ставит — чтобы случайно не «залочить» контрагента.
        if relationship != profile.relationship:
            profile.relationship_manual = True
            # Банковские реквизиты относятся только к официальному каналу оплаты — то же
            # правило, что в create_counterparty. Без этого правкой получался контрагент,
            # которого нельзя создать: informal с подтверждёнными реквизитами и без ИНН,
            # причём вкладка «Реквизиты» у informal скрыта и вычистить их было нечем.
            if relationship != "official":
                profile.requisites = {}
                profile.requisites_verified = False
                profile.requisites_verified_at = None
                profile.requisites_verified_by_user_id = None
        profile.relationship = relationship
    if brand_group is not _UNSET:
        profile.brand_group = brand_group
    if internal_name is not _UNSET:
        profile.internal_name = internal_name
    if payment_delay_days is not _UNSET:
        profile.payment_delay_days = payment_delay_days
    if payment_due_day_of_month is not _UNSET:
        profile.payment_due_day_of_month = payment_due_day_of_month
    if manager_name is not _UNSET:
        profile.manager_name = manager_name
    if manager_phone is not _UNSET:
        profile.manager_phone = manager_phone
    # Статья ДДС по умолчанию для оплат этого контрагента (та же колонка, что
    # закрепляет чекбокс «Закрепить за контрагентом» в окне оплаты «Страницы на оплату»).
    if default_dds_article_id is not _UNSET:
        profile.default_dds_article_id = default_dds_article_id
        # Решение по статье меняется только вместе с самой статьёй — иначе PATCH без
        # флага молча снял бы подтверждение и запер карточку.
        profile.confirm_no_dds_article = confirm_no_dds_article
    if service_period_required is not _UNSET and service_period_required is not None:
        profile.service_period_required = service_period_required
    if (
        bank_payments_create_prepayment is not _UNSET
        and bank_payments_create_prepayment is not None
    ):
        profile.bank_payments_create_prepayment = bank_payments_create_prepayment
    if default_service_period_offset_months is not _UNSET:
        profile.default_service_period_offset_months = default_service_period_offset_months
    # До какого числа ждём закрывающий документ. None — законное значение («ждём с 1-го»),
    # поэтому пропускаем только _UNSET: иначе поле нельзя было бы очистить.
    if closing_doc_expected_day is not _UNSET:
        if closing_doc_expected_day is not None and not 1 <= int(closing_doc_expected_day) <= 28:
            raise CounterpartyRegistryError("День ожидания документа — число от 1 до 28")
        profile.closing_doc_expected_day = closing_doc_expected_day
    if settlement_contour is not _UNSET:
        if settlement_contour is not None and settlement_contour not in ("goods", "service"):
            raise CounterpartyRegistryError("Неизвестный контур расчётов")
        profile.settlement_contour = settlement_contour
    if status is not _UNSET and status:
        profile.status = status
    await activate_configured_placeholder(session, counterparty, profile)
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
    new_requisites = dict(requisites or {})
    profile = await _get_or_create_profile(session, counterparty_id)
    # Подтверждение реквизитов — не голая отметка: битый контрольный разряд счёта банк
    # всё равно отклонит (422), поэтому не даём пометить такие реквизиты «подтверждёнными».
    if verified:
        account_error = payee_account_error(new_requisites)
        if account_error:
            raise CounterpartyRegistryError(account_error)
        # …и не даём подтвердить неполный набор: гард «реквизиты не подтверждены» —
        # единственное, что стоит между пустой карточкой и платежом. Неполные реквизиты
        # сохранять по-прежнему можно, но без отметки «проверены».
        if profile.relationship == "official" and counterparty.type not in NON_SUPPLIER_TYPES:
            _require_official_supplier_requisites(new_requisites)
    profile.requisites = new_requisites
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


# Автозаполнение реквизитов из прошлых платежей переехало в
# ``counterparty_requisites_history``: оно нужно и до создания карточки (окно «Новый
# контрагент»), поэтому больше не привязано к ``counterparty_id``.


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
    if counterparty.status in ARCHIVED_STATUSES:
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
    # Дедуп как у почты/ЭДО (сумма+дата+номер): тот же закрывающий документ мог уже прийти
    # другим каналом — второй раз вставать не должен (двойной учёт). Ручной ввод в реестр =
    # closing, поэтому дедупим только против closing (канон: счёт и УПД — разные роли).
    clean_number = (number or "").strip() or None
    duplicate_candidates = (
        await session.scalars(
            select(SupplierInvoice).where(
                SupplierInvoice.counterparty_id == counterparty_id,
                SupplierInvoice.amount == _money(amount),
                SupplierInvoice.payment_status != "void",
                SupplierInvoice.doc_kind == "closing",
                SupplierInvoice.source.in_(("email", "sbis", "manual")),
            )
        )
    ).all()
    for candidate in duplicate_candidates:
        if candidate.invoice_date == invoice_date and (candidate.number or None) == clean_number:
            raise CounterpartyRegistryError(
                "Такой счёт уже есть (совпали контрагент, сумма, дата и номер) — "
                "он пришёл почтой/ЭДО или был внесён ранее"
            )
    # Кросс-канальное окно против ЭДО: счёт/закрывающий УПД той же поставки мог уже
    # прийти через СБИС под ДРУГИМ номером — совпадения номера не ждём.
    if invoice_date is not None:
        for candidate in duplicate_candidates:
            if (
                candidate.source == "sbis"
                and candidate.invoice_date is not None
                and abs((candidate.invoice_date - invoice_date).days)
                <= CROSS_CHANNEL_DEDUP_WINDOW_DAYS
            ):
                raise CounterpartyRegistryError(
                    f"Похоже, этот счёт уже пришёл через СБИС: №{candidate.number or 'б/н'} "
                    f"от {candidate.invoice_date.strftime('%d.%m.%Y')} на ту же сумму"
                )
    invoice = SupplierInvoice(
        counterparty_id=counterparty_id,
        source="manual",
        # Ручной ввод в реестр — это оприходование поставки/услуги (закрывающий документ), а не
        # счёт на оплату: он гасит дебиторку / встаёт в кредиторку.
        doc_kind="closing",
        operational_scope="finance",
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
    await session.flush()
    # Закрывающий документ поставщика с предоплатной моделью гасит дебиторку независимо от
    # канала (почта/ЭДО/ручной ввод) — иначе накладная встаёт «к оплате» и её оплатят повторно.
    # Будущей датой (правило 4) — отложится в 'pending' до своей даты.
    from app.services.supplier_prepayments import apply_closing_document

    await apply_closing_document(session, invoice)
    await session.commit()
    await session.refresh(invoice)
    return invoice


async def void_invoice(session: AsyncSession, invoice_id: uuid.UUID) -> SupplierInvoice:
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        raise CounterpartyRegistryError("Накладная не найдена")
    if invoice.draft_id is not None:
        raise CounterpartyRegistryError("Накладная отправлена в банк — сначала отмените черновик")
    if invoice.barter_role is not None:
        # Бартерный леджер живёт своими правилами: возвратная накладная создаётся сразу
        # 'paid' и без аллокаций, поэтому проверка ниже её пропустила бы — а строки
        # BarterReturnLine остались бы, и заём числился бы погашенным несуществующим
        # документом (симметрично гардам ensure_invoice_deletable и _apply_iiko_deletion).
        raise CounterpartyRegistryError(
            "Бартерная накладная аннулированию не подлежит — гашение ведётся в карточке партнёра"
        )
    has_allocations = await session.scalar(
        select(InvoicePaymentAllocation.id)
        .where(InvoicePaymentAllocation.invoice_id == invoice_id)
        .limit(1)
    )
    if has_allocations is not None:
        raise CounterpartyRegistryError("По накладной есть оплаты — аннулирование недоступно")
    invoice.payment_status = "void"
    # Аннулированной накладной не существует — её начисление тоже отменяем, иначе джоба
    # признания превратит расход по ней в фантом в P&L.
    await service_periods.cancel_invoice_accrual(session, invoice_id)
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
    default_dds_article_id: uuid.UUID | None = None,
    confirm_no_dds_article: bool = False,
    service_period_required: bool = False,
    default_service_period_offset_months: int | None = None,
    requisites: dict[str, Any] | None = None,
    requisites_verified: bool = False,
    requisites_verified_by_user_id: uuid.UUID | None = None,
    iiko_supplier_guid: str | None = None,
    role: str = "supplier",
) -> Counterparty:
    clean_name = (name or "").strip()
    if not clean_name:
        raise CounterpartyRegistryError("Укажите название контрагента")
    _require_dds_article_decision(default_dds_article_id, confirm_no_dds_article)
    requested_requisites = {
        str(key): value.strip() if isinstance(value, str) else value
        for key, value in (requisites or {}).items()
        if not isinstance(value, str) or value.strip()
    }
    # Банковские реквизиты относятся только к официальному каналу оплаты. Для
    # informal используется выплата на карту ИП/Сейф, для barter — взаимозачёт.
    # ИНН — НЕ реквизит оплаты, а ключ идентификации: по нему счета из iiko/почты/ЭДО
    # находят карточку. Храним его для любого типа отношений, иначе синки плодят дубли.
    accepts_requisites = relationship == "official"
    clean_requisites = requested_requisites if accepts_requisites else {}
    requisites_verified = requisites_verified and accepts_requisites
    clean_inn = (str(clean_requisites.get("inn") or inn or "")).strip() or None
    if accepts_requisites:
        if clean_inn:
            clean_requisites["inn"] = clean_inn
        clean_requisites.setdefault("recipientName", clean_name)
    if relationship == "official" and role == "supplier":
        _require_official_supplier_requisites(clean_requisites)
    if requisites_verified:
        account_error = payee_account_error(clean_requisites)
        if account_error:
            raise CounterpartyRegistryError(account_error)
    if clean_inn:
        existing = await session.scalar(select(Counterparty).where(Counterparty.inn == clean_inn))
        if existing is not None:
            raise CounterpartyRegistryError("Контрагент с таким ИНН уже существует")
    # Onboarding a supplier straight from the iiko directory: bind its GUID as an alias so the
    # reverse invoice-sync recognises this counterparty instead of auto-creating a duplicate
    # (a prepayment-only supplier has no posted invoice yet, hence no auto-create). Guard against
    # double-binding the same iiko supplier to two counterparties.
    clean_guid = (iiko_supplier_guid or "").strip() or None
    if clean_guid:
        linked = await session.scalar(
            select(CounterpartyAlias).where(
                func.lower(CounterpartyAlias.alias) == clean_guid.lower(),
                CounterpartyAlias.source == "iiko",
            )
        )
        if linked is not None:
            raise CounterpartyRegistryError("Этот поставщик iiko уже привязан к контрагенту")
    counterparty = Counterparty(
        name=clean_name, inn=clean_inn, type=cp_type, status="active", origin="manual"
    )
    session.add(counterparty)
    await session.flush()
    session.add(CounterpartyRole(counterparty_id=counterparty.id, role=role))
    session.add(
        CounterpartyPayableProfile(
            counterparty_id=counterparty.id,
            relationship=relationship,
            internal_name=internal_name or (clean_name if clean_guid else None),
            ledger_category_id=ledger_category_id,
            brand_group=brand_group,
            payment_delay_days=payment_delay_days,
            payment_due_day_of_month=payment_due_day_of_month,
            manager_name=manager_name,
            manager_phone=manager_phone,
            default_dds_article_id=default_dds_article_id,
            confirm_no_dds_article=confirm_no_dds_article,
            service_period_required=service_period_required,
            default_service_period_offset_months=default_service_period_offset_months,
            requisites=clean_requisites,
            requisites_verified=requisites_verified,
            requisites_verified_at=datetime.now(tz=UTC) if requisites_verified else None,
            requisites_verified_by_user_id=(
                requisites_verified_by_user_id if requisites_verified else None
            ),
        )
    )
    if clean_guid:
        session.add(
            CounterpartyAlias(counterparty_id=counterparty.id, alias=clean_guid, source="iiko")
        )
        # Mirror the iiko link as a collection source (same as the auto-sync path) so it shows
        # on the supplier source-data page.
        session.add(
            CounterpartyCollectionSource(
                counterparty_id=counterparty.id, kind="iiko", value=clean_guid
            )
        )
    await session.commit()
    await session.refresh(counterparty)
    return counterparty


async def _set_counterparty_archive_state(
    session: AsyncSession, counterparty: Counterparty, *, archived: bool
) -> None:
    if archived:
        in_draft = await session.scalar(
            select(SupplierInvoice.id)
            .where(
                SupplierInvoice.counterparty_id == counterparty.id,
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


async def set_counterparty_archived(
    session: AsyncSession, counterparty_id: uuid.UUID, *, archived: bool
) -> Counterparty:
    counterparty = await session.get(Counterparty, counterparty_id)
    if counterparty is None:
        raise CounterpartyRegistryError("Контрагент не найден")
    await _set_counterparty_archive_state(session, counterparty, archived=archived)
    await session.commit()
    await session.refresh(counterparty)
    return counterparty


async def update_counterparty_identity(
    session: AsyncSession,
    counterparty_id: uuid.UUID,
    *,
    changes: dict[str, Any],
) -> Counterparty:
    """Compatibility entry point for legacy clients; owns base-card validation centrally."""
    counterparty = await session.get(Counterparty, counterparty_id)
    if counterparty is None:
        raise CounterpartyRegistryError("Контрагент не найден")

    if "name" in changes:
        clean_name = str(changes["name"] or "").strip()
        if not clean_name:
            raise CounterpartyRegistryError("Укажите название контрагента")
        counterparty.name = clean_name

    if "inn" in changes:
        clean_inn = str(changes["inn"] or "").strip() or None
        if clean_inn:
            duplicate = await session.scalar(
                select(Counterparty.id).where(
                    Counterparty.inn == clean_inn,
                    Counterparty.id != counterparty_id,
                )
            )
            if duplicate is not None:
                raise CounterpartyRegistryError("Контрагент с таким ИНН уже существует")
        counterparty.inn = clean_inn

    if "type" in changes and changes["type"] is not None:
        counterparty.type = str(changes["type"])

    if "status" in changes and changes["status"] is not None:
        requested_status = str(changes["status"])
        if requested_status in {"inactive", ARCHIVED_STATUS}:
            await _set_counterparty_archive_state(session, counterparty, archived=True)
        elif requested_status == "active":
            await _set_counterparty_archive_state(session, counterparty, archived=False)
        elif requested_status == "requires_setup":
            counterparty.status = requested_status
        else:
            raise CounterpartyRegistryError("Неизвестный статус контрагента")

    await session.commit()
    await session.refresh(counterparty)
    return counterparty


async def add_counterparty_alias(
    session: AsyncSession,
    counterparty_id: uuid.UUID,
    *,
    alias: str,
    source: str | None = None,
) -> CounterpartyAlias:
    if await session.get(Counterparty, counterparty_id) is None:
        raise CounterpartyRegistryError("Контрагент не найден")
    clean_alias = (alias or "").strip()
    if not clean_alias:
        raise CounterpartyRegistryError("Укажите псевдоним контрагента")
    item = CounterpartyAlias(
        counterparty_id=counterparty_id,
        alias=clean_alias,
        source=source,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


async def delete_counterparty_alias(session: AsyncSession, alias_id: uuid.UUID) -> None:
    alias = await session.get(CounterpartyAlias, alias_id)
    if alias is None:
        raise CounterpartyRegistryError("Псевдоним контрагента не найден")
    await session.delete(alias)
    await session.commit()


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


# Накопительные/фондовые субсчета Тинькофф — это копилки (деньги туда только откладываются
# внутренними переводами), а не источник оплаты поставщику. Прячем их из пикеров оплаты,
# чтобы случайно не «заплатить» накладную из фонда.
NON_PAYOUT_WALLET_CODES = ("tbank_kopilka", "guarant_fund", "tbank_nakopit")


async def list_wallets(session: AsyncSession) -> list[Wallet]:
    """Active DDS wallets/accounts selectable as the source of a manual payment.

    Savings/fund sub-accounts are excluded — you can't pay a supplier out of a piggy-bank.
    """
    return list(
        (
            await session.execute(
                select(Wallet)
                .where(
                    Wallet.status == "active",
                    Wallet.code.notin_(NON_PAYOUT_WALLET_CODES),
                )
                .order_by(Wallet.name)
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

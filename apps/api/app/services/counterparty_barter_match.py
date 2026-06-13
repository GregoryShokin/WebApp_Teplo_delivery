"""Settle barter loans: net payable (they lent us) ↔ receivable (we returned) invoices.

Returns are recorded at the SAME ruble sum as the loan (not by kg), but a return may
be batched into one invoice covering several loans. So we match by exact amount —
1:1 or subset-sum for batched returns — and disambiguate by the **номенклатура** set
(product ids) of the invoice line items. A match where the sum is exact AND the product
set is exact AND unique is auto-settled; anything ambiguous is left for a manager to
confirm explicitly.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BarterSettlement, SupplierInvoice

# Max invoices a single batched return may cover (keeps subset enumeration cheap).
MAX_SUBSET_SIZE = 5


class BarterMatchError(RuntimeError):
    """Domain error for barter settlement (maps to HTTP 409)."""


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _products(invoice: SupplierInvoice) -> frozenset[str]:
    """Set of product keys on the invoice (product_id, fallback article)."""
    keys: set[str] = set()
    for item in invoice.line_items or []:
        key = item.get("product_id") or item.get("article")
        if key:
            keys.add(str(key))
    return frozenset(keys)


@dataclass
class _Ref:
    invoice: SupplierInvoice
    amount: Decimal
    products: frozenset[str]
    date: date | None


@dataclass
class BarterSettlementView:
    id: uuid.UUID
    amount: Decimal
    is_auto: bool
    payable_numbers: list[str]
    receivable_numbers: list[str]


@dataclass
class BarterDetail:
    relationship_balance: Decimal  # signed: + we owe, − they owe (open, unsettled)
    open_payables: list[dict[str, Any]]
    open_receivables: list[dict[str, Any]]
    settlements: list[BarterSettlementView] = field(default_factory=list)


@dataclass
class BarterSuggestion:
    """A proposed netting the UI highlights so the manager doesn't pick blind.

    ``confident`` mirrors the auto-settle rule (exact sum + exact product set, unique);
    non-confident pairs match by sum only and need номенклатура verified by a human.
    """

    payable_ids: list[uuid.UUID]
    receivable_ids: list[uuid.UUID]
    amount: Decimal
    confident: bool


def _ref(invoice: SupplierInvoice) -> _Ref:
    return _Ref(
        invoice=invoice,
        amount=_money(invoice.amount),
        products=_products(invoice),
        date=invoice.invoice_date,
    )


def _date_eligible(pool_ref: _Ref, anchor: _Ref, *, anchor_is_payable: bool) -> bool:
    """A loan (payable) can only be covered by a return (receivable) dated on/after it —
    you can't return goods that weren't borrowed yet. Missing dates → don't block.

    Without this, two identical loans (same sum + номенклатура) on different dates both
    look like valid subset members, the match stops being unique, and auto-settle bails.
    """
    if pool_ref.date is None or anchor.date is None:
        return True
    if anchor_is_payable:
        # anchor = loan, pool_ref = return → return must be on/after the loan
        return pool_ref.date >= anchor.date
    # anchor = return, pool_ref = loan → loan must be on/before the return
    return pool_ref.date <= anchor.date


async def _load_open(
    session: AsyncSession, counterparty_id: uuid.UUID
) -> tuple[list[_Ref], list[_Ref]]:
    """Open (unsettled, untouched) payables and receivables for netting."""
    rows = (
        (
            await session.execute(
                select(SupplierInvoice).where(
                    SupplierInvoice.counterparty_id == counterparty_id,
                    SupplierInvoice.payment_status == "unpaid",
                    SupplierInvoice.barter_settlement_id.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    payables = [_ref(inv) for inv in rows if inv.direction == "payable"]
    receivables = [_ref(inv) for inv in rows if inv.direction == "receivable"]
    return payables, receivables


def _subsets_summing(pool: Sequence[_Ref], target: Decimal) -> list[tuple[_Ref, ...]]:
    """All subsets of ``pool`` (size 1..MAX_SUBSET_SIZE) whose amounts sum to target."""
    matches: list[tuple[_Ref, ...]] = []
    limit = min(MAX_SUBSET_SIZE, len(pool))
    for size in range(1, limit + 1):
        for combo in itertools.combinations(pool, size):
            if sum((ref.amount for ref in combo), Decimal("0.00")) == target:
                matches.append(combo)
    return matches


def _union_products(subset: Sequence[_Ref]) -> frozenset[str]:
    keys: set[str] = set()
    for ref in subset:
        keys |= ref.products
    return frozenset(keys)


def _first_confident_match(
    payables: list[_Ref], receivables: list[_Ref]
) -> tuple[list[_Ref], list[_Ref]] | None:
    """Find a unique anchor↔subset match: exact sum AND exact product set, anchor on
    either side. Returns (payable_refs, receivable_refs) or None."""
    for anchors, pool, anchor_is_payable in (
        (receivables, payables, False),
        (payables, receivables, True),
    ):
        for anchor in anchors:
            if not anchor.products:
                continue  # cannot verify номенклатура → never auto
            eligible = [
                ref
                for ref in pool
                if _date_eligible(ref, anchor, anchor_is_payable=anchor_is_payable)
            ]
            exact = [
                subset
                for subset in _subsets_summing(eligible, anchor.amount)
                if _union_products(subset) == anchor.products
            ]
            if len(exact) == 1:
                subset = list(exact[0])
                if anchor_is_payable:
                    return [anchor], subset  # anchor payable ↔ subset of receivables
                return subset, [anchor]  # anchor receivable ↔ subset of payables
    return None


def _suggestion(
    payable_refs: Sequence[_Ref], receivable_refs: Sequence[_Ref], *, confident: bool
) -> BarterSuggestion:
    return BarterSuggestion(
        payable_ids=[ref.invoice.id for ref in payable_refs],
        receivable_ids=[ref.invoice.id for ref in receivable_refs],
        amount=_money(sum((ref.amount for ref in payable_refs), Decimal("0.00"))),
        confident=confident,
    )


def _suggest_pairs(payables: list[_Ref], receivables: list[_Ref]) -> list[BarterSuggestion]:
    """Non-overlapping netting proposals for the UI to highlight.

    Pass 1 collects confident matches (same rule as auto-settle: exact sum + exact
    product set, unique). Pass 2 proposes the smallest remaining batch matching by sum
    only — номенклатура unverified, so the manager must confirm (``confident=False``).
    """
    used: set[uuid.UUID] = set()
    suggestions: list[BarterSuggestion] = []

    def avail(refs: list[_Ref]) -> list[_Ref]:
        return [ref for ref in refs if ref.invoice.id not in used]

    while True:
        match = _first_confident_match(avail(payables), avail(receivables))
        if match is None:
            break
        payable_refs, receivable_refs = match
        suggestions.append(_suggestion(payable_refs, receivable_refs, confident=True))
        used.update(ref.invoice.id for ref in (*payable_refs, *receivable_refs))

    for anchor_is_payable in (True, False):
        for anchor in avail(payables if anchor_is_payable else receivables):
            if anchor.invoice.id in used:
                continue
            pool = avail(receivables if anchor_is_payable else payables)
            pool = [
                ref
                for ref in pool
                if _date_eligible(ref, anchor, anchor_is_payable=anchor_is_payable)
            ]
            subsets = _subsets_summing(pool, anchor.amount)
            if not subsets:
                continue
            subset = list(min(subsets, key=len))  # prefer the smallest batch (1:1 first)
            payable_refs, receivable_refs = (
                ([anchor], subset) if anchor_is_payable else (subset, [anchor])
            )
            suggestions.append(_suggestion(payable_refs, receivable_refs, confident=False))
            used.update(ref.invoice.id for ref in (*payable_refs, *receivable_refs))

    return suggestions


async def suggest_barter_matches(
    session: AsyncSession, counterparty_id: uuid.UUID
) -> list[BarterSuggestion]:
    payables, receivables = await _load_open(session, counterparty_id)
    return _suggest_pairs(payables, receivables)


async def _create_settlement(
    session: AsyncSession,
    counterparty_id: uuid.UUID,
    payables: Sequence[_Ref],
    receivables: Sequence[_Ref],
    *,
    is_auto: bool,
    actor_user_id: uuid.UUID | None,
) -> BarterSettlement:
    amount = _money(sum((ref.amount for ref in payables), Decimal("0.00")))
    settlement = BarterSettlement(
        counterparty_id=counterparty_id,
        amount=amount,
        is_auto=is_auto,
        created_by_user_id=actor_user_id,
    )
    session.add(settlement)
    await session.flush()
    for ref in (*payables, *receivables):
        ref.invoice.barter_settlement_id = settlement.id
        # Netted in kind → closed; drops out of inbox / registry / balance (status filters).
        ref.invoice.payment_status = "paid"
    await session.flush()
    return settlement


async def auto_settle_barter(
    session: AsyncSession,
    counterparty_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> int:
    """Auto-settle every unique 100% match (exact sum + exact product set). Returns count."""
    settled = 0
    while True:
        payables, receivables = await _load_open(session, counterparty_id)
        match = _first_confident_match(payables, receivables)
        if match is None:
            break
        payable_refs, receivable_refs = match
        await _create_settlement(
            session,
            counterparty_id,
            payable_refs,
            receivable_refs,
            is_auto=True,
            actor_user_id=actor_user_id,
        )
        settled += 1
    if settled:
        await session.commit()
    return settled


async def confirm_barter_settlement(
    session: AsyncSession,
    counterparty_id: uuid.UUID,
    *,
    payable_ids: Sequence[uuid.UUID],
    receivable_ids: Sequence[uuid.UUID],
    actor_user_id: uuid.UUID | None = None,
) -> BarterSettlement:
    """Manually net a chosen set of payables and receivables (sums must be equal)."""
    if not payable_ids or not receivable_ids:
        raise BarterMatchError("Выберите накладные с обеих сторон")
    payables, receivables = await _load_open(session, counterparty_id)
    by_id = {ref.invoice.id: ref for ref in (*payables, *receivables)}
    try:
        payable_refs = [by_id[i] for i in payable_ids]
        receivable_refs = [by_id[i] for i in receivable_ids]
    except KeyError as exc:
        raise BarterMatchError("Накладная недоступна для зачёта") from exc
    if any(ref.invoice.direction != "payable" for ref in payable_refs) or any(
        ref.invoice.direction != "receivable" for ref in receivable_refs
    ):
        raise BarterMatchError("Перепутаны стороны зачёта")
    p_sum = _money(sum((ref.amount for ref in payable_refs), Decimal("0.00")))
    r_sum = _money(sum((ref.amount for ref in receivable_refs), Decimal("0.00")))
    if p_sum != r_sum:
        raise BarterMatchError(f"Суммы не совпадают: {p_sum} ≠ {r_sum}")
    settlement = await _create_settlement(
        session,
        counterparty_id,
        payable_refs,
        receivable_refs,
        is_auto=False,
        actor_user_id=actor_user_id,
    )
    await session.commit()
    return settlement


def _invoice_dict(ref: _Ref) -> dict[str, Any]:
    inv = ref.invoice
    return {
        "id": inv.id,
        "number": inv.number,
        "invoice_date": inv.invoice_date,
        "amount": ref.amount,
        "products": [
            item.get("name") or item.get("article") or item.get("product_id")
            for item in (inv.line_items or [])
        ],
    }


async def barter_detail(session: AsyncSession, counterparty_id: uuid.UUID) -> BarterDetail:
    payables, receivables = await _load_open(session, counterparty_id)
    payable_sum = _money(sum((ref.amount for ref in payables), Decimal("0.00")))
    receivable_sum = _money(sum((ref.amount for ref in receivables), Decimal("0.00")))
    settlements = await _load_settlements(session, counterparty_id)
    return BarterDetail(
        relationship_balance=_money(payable_sum - receivable_sum),
        open_payables=[_invoice_dict(ref) for ref in payables],
        open_receivables=[_invoice_dict(ref) for ref in receivables],
        settlements=settlements,
    )


async def _load_settlements(
    session: AsyncSession, counterparty_id: uuid.UUID
) -> list[BarterSettlementView]:
    settlements = (
        (
            await session.execute(
                select(BarterSettlement)
                .where(BarterSettlement.counterparty_id == counterparty_id)
                .order_by(BarterSettlement.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    if not settlements:
        return []
    invoices = (
        (
            await session.execute(
                select(SupplierInvoice).where(
                    SupplierInvoice.barter_settlement_id.in_([s.id for s in settlements])
                )
            )
        )
        .scalars()
        .all()
    )
    views: list[BarterSettlementView] = []
    for settlement in settlements:
        linked = [inv for inv in invoices if inv.barter_settlement_id == settlement.id]
        views.append(
            BarterSettlementView(
                id=settlement.id,
                amount=_money(settlement.amount),
                is_auto=settlement.is_auto,
                payable_numbers=[inv.number or "—" for inv in linked if inv.direction == "payable"],
                receivable_numbers=[
                    inv.number or "—" for inv in linked if inv.direction == "receivable"
                ],
            )
        )
    return views

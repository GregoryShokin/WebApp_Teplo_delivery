"""Settle barter loans by netting opposite-direction invoices of the same partner.

A barter invoice's **direction** is only physics — payable = приходная (goods came to
us), receivable = расходная (goods left us). It does NOT say who lent: the same приход
can be a partner lending us OR a partner returning our earlier loan. The **role**
(заём/возврат) is decided by **chronology** inside a matched pair, not by direction:
the earlier invoice is the LOAN, the later one is the RETURN. Whose loan follows from
the loan side's direction — расход-loan = we lent, приход-loan = they lent us. Equal
dates are broken by invoice **number** (larger = later = the return).

Returns are recorded at the SAME ruble sum as the loan (not by kg); a return may be
batched into one invoice covering several earlier loans. So we match by exact amount —
1:1 or subset-sum — disambiguated by the **номенклатура** set (product ids) and by
chronology (a return only covers loans dated on/before it). When several identical
loans compete, they are closed **FIFO** (earliest loan first). A match that is exact
in sum + product set and FIFO-unambiguous is auto-settled; the rest is left for a
manager to confirm explicitly.
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
    # True = we were the lender (loan side is расход/receivable, partner returned via
    # приход); False = the partner lent us. Drives the «мы заняли»/«заняли нам» wording.
    we_lent: bool
    loan_numbers: list[str]
    return_numbers: list[str]


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
    we_lent: bool  # who is the lender in this proposed netting (by chronology)


def _ref(invoice: SupplierInvoice) -> _Ref:
    return _Ref(
        invoice=invoice,
        amount=_money(invoice.amount),
        products=_products(invoice),
        date=invoice.invoice_date,
    )


def _order_key(ref: _Ref) -> tuple[date, str]:
    """Chronological sort key: by date, ties broken by invoice number (larger = later)."""
    return (ref.date or date.min, ref.invoice.number or "")


def _chrono_sorted(refs: Sequence[_Ref]) -> list[_Ref]:
    return sorted(refs, key=_order_key)


def _chronology_ok(anchor: _Ref, subset: Sequence[_Ref]) -> bool:
    """A loan↔return group nets only if it splits cleanly in time: every loan is dated
    on/before its return. Role is by DATE, not direction — so the anchor is valid either
    as the return covering earlier loans (all subset ≤ anchor) OR as a loan covered by
    later returns (all subset ≥ anchor). A subset straddling the anchor date is rejected.
    Missing dates never block (legacy invoices without a date stay matchable).
    """
    sub_dates = [ref.date for ref in subset if ref.date is not None]
    if anchor.date is None or len(sub_dates) != len(subset):
        return True
    return all(d <= anchor.date for d in sub_dates) or all(d >= anchor.date for d in sub_dates)


def _we_lent(payables: Sequence[_Ref], receivables: Sequence[_Ref]) -> bool:
    """Within a netted group, who was the lender? The return is the latest invoice
    (by date, then number); the loan side is the opposite direction. A return that came
    in as приход (payable) means we were owed goods back → WE lent. Returns True iff so.
    """
    refs = [*payables, *receivables]
    latest = max(refs, key=_order_key)
    return latest.invoice.direction == "payable"


def _loan_direction(payables: Sequence[_Ref], receivables: Sequence[_Ref]) -> str:
    """Which direction holds the loans (the earlier side). расход if we lent, else приход."""
    return "receivable" if _we_lent(payables, receivables) else "payable"


def _split_by_direction(
    anchor: _Ref, subset: Sequence[_Ref]
) -> tuple[list[_Ref], list[_Ref]]:
    """Group an anchor + its matched subset into (payables, receivables) — the settlement
    stores invoices by direction; role is recovered later from chronology, not from this."""
    members = list(subset)
    if anchor.invoice.direction == "payable":
        return [anchor], members
    return members, [anchor]


def _fifo_key(subset: Sequence[_Ref]) -> tuple[tuple[date, str], ...]:
    """Sort key making the subset with the EARLIEST invoices win — FIFO loan closing."""
    return tuple(sorted(_order_key(ref) for ref in subset))


def _fifo_pick(subsets: Sequence[Sequence[_Ref]]) -> list[_Ref] | None:
    """Pick the FIFO-earliest subset. Returns None when the two best are tied (e.g. all
    dates missing) — a genuine ambiguity that must go to a manager, not auto-settle."""
    ordered = sorted(subsets, key=_fifo_key)
    if not ordered:
        return None
    if len(ordered) >= 2 and _fifo_key(ordered[0]) == _fifo_key(ordered[1]):
        return None
    return list(ordered[0])


async def _load_open(
    session: AsyncSession, counterparty_id: uuid.UUID
) -> tuple[list[_Ref], list[_Ref]]:
    """Open (unsettled, untouched) payables and receivables for netting.

    Только НЕявный бартер (``barter_role IS NULL`` — iiko-синканные / договорные накладные,
    роль которых выводится по хронологии). Явные займы/возвраты (``barter_role`` = loan/return)
    ведутся отдельным путём (``BarterReturnLine`` / ``barter_return_status``) и НЕ участвуют в
    хроно-зачёте: иначе явный заём (unpaid, settlement_id IS NULL) мог бы быть сведён здесь и
    помечен ``paid`` второй раз — двойной учёт того же движения.
    """
    rows = (
        (
            await session.execute(
                select(SupplierInvoice).where(
                    SupplierInvoice.counterparty_id == counterparty_id,
                    SupplierInvoice.payment_status == "unpaid",
                    SupplierInvoice.barter_settlement_id.is_(None),
                    SupplierInvoice.barter_role.is_(None),
                    # Счёт (doc_kind='bill') — не долг: в бартер-зачёт не входит. Иначе бартер
                    # пометил бы его paid прямым присвоением (в обход _recompute_status → ДЗ не
                    # завелась бы, пришедший УПД повис бы фантомной КЗ), да и гасить реальную
                    # дебиторку контрагента о «намерение оплаты» — двойной учёт.
                    SupplierInvoice.doc_kind != "bill",
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
    """Find one auto-settleable match: exact sum + exact product set + clean chronology,
    anchor on either side. Anchors are scanned earliest-first and identical loans close
    FIFO, so repeated calls (the auto-settle loop) close loans in date order. Returns
    (payable_refs, receivable_refs) or None."""
    for anchors, pool in ((receivables, payables), (payables, receivables)):
        for anchor in _chrono_sorted(anchors):
            if not anchor.products:
                continue  # cannot verify номенклатура → never auto
            valid = [
                subset
                for subset in _subsets_summing(pool, anchor.amount)
                if _union_products(subset) == anchor.products and _chronology_ok(anchor, subset)
            ]
            chosen = _fifo_pick(valid)
            if chosen is not None:
                return _split_by_direction(anchor, chosen)
    return None


def _suggestion(
    payable_refs: Sequence[_Ref], receivable_refs: Sequence[_Ref], *, confident: bool
) -> BarterSuggestion:
    return BarterSuggestion(
        payable_ids=[ref.invoice.id for ref in payable_refs],
        receivable_ids=[ref.invoice.id for ref in receivable_refs],
        amount=_money(sum((ref.amount for ref in payable_refs), Decimal("0.00"))),
        confident=confident,
        we_lent=_we_lent(payable_refs, receivable_refs),
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

    for anchor_pool, other_pool in ((payables, receivables), (receivables, payables)):
        for anchor in _chrono_sorted(avail(anchor_pool)):
            if anchor.invoice.id in used:
                continue
            pool = avail(other_pool)
            subsets = [
                subset
                for subset in _subsets_summing(pool, anchor.amount)
                if _chronology_ok(anchor, subset)
            ]
            if not subsets:
                continue
            # smallest batch first (1:1 over a sweep), then FIFO-earliest among equal sizes
            subset = min(subsets, key=lambda s: (len(s), _fifo_key(s)))
            payable_refs, receivable_refs = _split_by_direction(anchor, subset)
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
        payables = [_ref(inv) for inv in linked if inv.direction == "payable"]
        receivables = [_ref(inv) for inv in linked if inv.direction == "receivable"]
        loan_dir = _loan_direction(payables, receivables)
        views.append(
            BarterSettlementView(
                id=settlement.id,
                amount=_money(settlement.amount),
                is_auto=settlement.is_auto,
                we_lent=loan_dir == "receivable",
                loan_numbers=[inv.number or "—" for inv in linked if inv.direction == loan_dir],
                return_numbers=[inv.number or "—" for inv in linked if inv.direction != loan_dir],
            )
        )
    return views


async def settled_roles(
    session: AsyncSession, settlement_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Map every invoice in the given settlements to its role — ``"loan"`` or
    ``"return"`` — decided by chronology (earlier side lends, later side returns).
    Lets the inbox/card badge label a settled barter invoice without re-deriving roles.
    """
    ids = list(dict.fromkeys(settlement_ids))
    if not ids:
        return {}
    invoices = (
        (
            await session.execute(
                select(SupplierInvoice).where(SupplierInvoice.barter_settlement_id.in_(ids))
            )
        )
        .scalars()
        .all()
    )
    grouped: dict[uuid.UUID, list[SupplierInvoice]] = {}
    for inv in invoices:
        grouped.setdefault(inv.barter_settlement_id, []).append(inv)
    roles: dict[uuid.UUID, str] = {}
    for linked in grouped.values():
        payables = [_ref(inv) for inv in linked if inv.direction == "payable"]
        receivables = [_ref(inv) for inv in linked if inv.direction == "receivable"]
        loan_dir = _loan_direction(payables, receivables)
        for inv in linked:
            roles[inv.id] = "loan" if inv.direction == loan_dir else "return"
    return roles

"""Reconcile unpaid invoices against real T-Bank operations by amount.

A pragmatic backlog-reconciliation + enrichment step: for each unpaid invoice find
T-Bank outgoing payments with the exact amount, exclude card/acquirer noise (where
the "receiver" is the bank itself), and present them as SUGGESTIONS. On manager
confirmation the operation is allocated to the invoice (marked paid) and — optionally
— the counterparty is enriched with the payee's official name, INN and requisites
from the bank ``receiver`` block. Identity is never auto-committed: amount collisions
(e.g. small round sums hitting card payments) would otherwise assign the wrong payee.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    BankOperation,
    Counterparty,
    CounterpartyPayableProfile,
    InvoicePaymentAllocation,
    SupplierInvoice,
)
from app.services.counterparty_matching import (
    CounterpartyMatchError,
    _allocated_amount,
    _op_already_allocated,
    _recompute_status,
)

# Acquirer / bank own INNs whose payments are card operations, not real payees.
BANK_NOISE_INNS = frozenset({"7710140679"})

# Warehouse invoice ↔ bank time-match defaults (issued_at receipt time ↔ posted_at).
# Owner's rule: amount may be fuzzy (±10%) ONLY when the time is tight, so a fuzzy-amount
# candidate is "confident" only inside the tight window. All three are overridable per call
# (and surfaced as settings). The fallback date (when posted_at is NULL) is taken in the
# business TZ so an evening receipt doesn't slip to the next calendar day.
WAREHOUSE_AMOUNT_TOLERANCE_PCT = Decimal("10")
WAREHOUSE_WINDOW_HOURS = 48
WAREHOUSE_TIGHT_WINDOW_MINUTES = 120
BUSINESS_TZ = ZoneInfo("Europe/Moscow")


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _receiver_block(raw_payload: dict[str, Any] | None) -> dict[str, Any]:
    block = (raw_payload or {}).get("receiver")
    return block if isinstance(block, dict) else {}


def _is_card_noise(operation: BankOperation) -> bool:
    raw = operation.raw_payload or {}
    if str(raw.get("category") or "").strip() == "cardOperation":
        return True
    receiver = _receiver_block(raw)
    inn = str(receiver.get("inn") or operation.counterparty_inn_raw or "")
    if inn in BANK_NOISE_INNS:
        return True
    name = str(receiver.get("name") or operation.counterparty_name_raw or "")
    return "ТБанк" in name or "ТБАНК" in name.upper()


def _payee_requisites(operation: BankOperation) -> dict[str, Any]:
    receiver = _receiver_block(operation.raw_payload)
    requisites = {
        "recipientName": receiver.get("name") or operation.counterparty_name_raw,
        "inn": receiver.get("inn") or operation.counterparty_inn_raw,
        "kpp": receiver.get("kpp"),
        "bankAcnt": receiver.get("acct") or operation.counterparty_account_raw,
        "bankBik": receiver.get("bicRu") or receiver.get("bic"),
        "recipientCorrAccountNumber": receiver.get("corAcct"),
    }
    return {key: str(value).strip() for key, value in requisites.items() if value not in (None, "")}


@dataclass
class MatchCandidate:
    bank_operation_id: uuid.UUID
    operation_date: date
    amount: Decimal
    official_name: str | None
    inn: str | None
    requisites: dict[str, Any]


@dataclass
class MatchSuggestion:
    invoice_id: uuid.UUID
    invoice_number: str | None
    invoice_amount: Decimal
    counterparty_id: uuid.UUID
    counterparty_name: str
    counterparty_has_inn: bool
    candidates: list[MatchCandidate] = field(default_factory=list)
    # A suggestion is "confident" when exactly one real (non-card) payee matches.
    confident: bool = False


async def suggest_invoice_matches(
    session: AsyncSession, *, counterparty_id: uuid.UUID | None = None
) -> list[MatchSuggestion]:
    invoice_query = (
        select(SupplierInvoice, Counterparty)
        .join(Counterparty, Counterparty.id == SupplierInvoice.counterparty_id)
        .where(SupplierInvoice.payment_status.in_(("unpaid", "partially_paid")))
        .where(SupplierInvoice.draft_id.is_(None))
        # Receivables (AR) are money owed to us — never matched to outgoing bank payments.
        .where(SupplierInvoice.direction == "payable")
    )
    if counterparty_id is not None:
        invoice_query = invoice_query.where(SupplierInvoice.counterparty_id == counterparty_id)

    suggestions: list[MatchSuggestion] = []
    for invoice, counterparty in (await session.execute(invoice_query)).all():
        remaining = _money(invoice.amount) - await _allocated_amount(session, invoice.id)
        if remaining <= 0:
            continue
        operations = (
            await session.execute(
                select(BankOperation).where(
                    BankOperation.provider == "tbank",
                    BankOperation.direction == "out",
                    BankOperation.amount == _money(invoice.amount),
                )
            )
        ).scalars().all()

        candidates: list[MatchCandidate] = []
        for operation in operations:
            if _is_card_noise(operation):
                continue
            if await _op_already_allocated(session, operation.id):
                continue
            requisites = _payee_requisites(operation)
            payee_inn = requisites.get("inn")
            # Once the counterparty has an INN, only trust same-INN payees.
            if counterparty.inn and payee_inn and payee_inn != counterparty.inn:
                continue
            candidates.append(
                MatchCandidate(
                    bank_operation_id=operation.id,
                    operation_date=operation.operation_date,
                    amount=_money(operation.amount),
                    official_name=requisites.get("recipientName"),
                    inn=payee_inn,
                    requisites=requisites,
                )
            )
        if not candidates:
            continue
        distinct_inns = {candidate.inn for candidate in candidates if candidate.inn}
        suggestions.append(
            MatchSuggestion(
                invoice_id=invoice.id,
                invoice_number=invoice.number,
                invoice_amount=_money(invoice.amount),
                counterparty_id=counterparty.id,
                counterparty_name=counterparty.name,
                counterparty_has_inn=bool(counterparty.inn),
                candidates=candidates,
                confident=len(distinct_inns) == 1,
            )
        )
    return suggestions


# --- warehouse time-match (issued_at ↔ posted_at) ----------------------------
#
# A separate matcher from ``suggest_invoice_matches`` (which matches an EXACT amount and
# ignores the date). Here a warehouse invoice carries a receipt timestamp (``issued_at``)
# and we rank T-Bank outgoing operations by how close ``posted_at`` is and how close the
# amount is, per the owner's rule (±10% amount allowed only when the time is tight).


@dataclass
class TimeMatchCandidate:
    bank_operation_id: uuid.UUID
    operation_date: date
    posted_at: datetime | None
    amount: Decimal
    official_name: str | None
    inn: str | None
    requisites: dict[str, Any]
    tier: int  # 1 tight-time+exact-amount … 4 fallback by operation_date (posted_at NULL)
    minutes_delta: int | None  # |posted_at − issued_at| in minutes, None on date fallback


@dataclass
class TimeMatchSuggestion:
    invoice_id: uuid.UUID
    invoice_number: str | None
    invoice_amount: Decimal
    remaining: Decimal
    issued_at: datetime | None
    counterparty_id: uuid.UUID
    counterparty_name: str
    counterparty_has_inn: bool
    candidates: list[TimeMatchCandidate] = field(default_factory=list)
    confident: bool = False


def _as_utc(value: datetime) -> datetime:
    """Make a datetime tz-aware (assume UTC if naive) and normalise to UTC for deltas."""
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC)


async def suggest_invoice_matches_by_time(
    session: AsyncSession,
    *,
    invoice_id: uuid.UUID,
    window_hours: int | None = None,
    amount_tolerance_pct: Decimal | float | None = None,
    tight_window_minutes: int | None = None,
) -> TimeMatchSuggestion | None:
    """Rank T-Bank outgoing operations against one warehouse invoice by receipt time +
    amount. Returns ``None`` if the invoice can't be time-matched (not a plain payable, or
    barter, or already paid). Confirmation is a separate step (``confirm_invoice_match``)."""
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        return None
    # Only plain payables match outgoing payments; barter loans are settled in goods, AR is
    # money owed to us, void/paid have nothing to match.
    if (
        invoice.direction != "payable"
        or invoice.barter_role is not None
        or invoice.payment_status not in ("unpaid", "partially_paid")
    ):
        return None
    # Неофициальный поставщик: с р/с ему напрямую не платим (только через карту ИП → Сейф),
    # поэтому банковские кандидаты — заведомо чужие операции (шум и риск ложного матча).
    profile_relationship = await session.scalar(
        select(CounterpartyPayableProfile.relationship).where(
            CounterpartyPayableProfile.counterparty_id == invoice.counterparty_id
        )
    )
    if profile_relationship == "informal":
        return None
    counterparty = await session.get(Counterparty, invoice.counterparty_id)

    window_hours = window_hours if window_hours is not None else WAREHOUSE_WINDOW_HOURS
    tight_minutes = (
        tight_window_minutes if tight_window_minutes is not None else WAREHOUSE_TIGHT_WINDOW_MINUTES
    )
    tol_pct = (
        Decimal(str(amount_tolerance_pct))
        if amount_tolerance_pct is not None
        else WAREHOUSE_AMOUNT_TOLERANCE_PCT
    )

    remaining = _money(invoice.amount) - await _allocated_amount(session, invoice.id)
    suggestion = TimeMatchSuggestion(
        invoice_id=invoice.id,
        invoice_number=invoice.number,
        invoice_amount=_money(invoice.amount),
        remaining=remaining,
        issued_at=invoice.issued_at,
        counterparty_id=invoice.counterparty_id,
        counterparty_name=counterparty.name if counterparty else "—",
        counterparty_has_inn=bool(counterparty and counterparty.inn),
    )
    if remaining <= 0 or invoice.issued_at is None:
        return suggestion

    tol_abs = _money(remaining * tol_pct / Decimal("100"))
    low, high = _money(remaining - tol_abs), _money(remaining + tol_abs)
    issued_utc = _as_utc(invoice.issued_at)
    issued_date = invoice.issued_at.astimezone(BUSINESS_TZ).date()
    # operation_date is always present; narrow the SQL scan by it (± window in days + slack).
    day_slack = math.ceil(window_hours / 24) + 1
    day_lo = issued_date - timedelta(days=day_slack)
    day_hi = issued_date + timedelta(days=day_slack)

    operations = (
        (
            await session.execute(
                select(BankOperation).where(
                    BankOperation.provider == "tbank",
                    BankOperation.direction == "out",
                    BankOperation.transfer_group_id.is_(None),  # exclude internal transfers
                    BankOperation.amount >= low,
                    BankOperation.amount <= high,
                    BankOperation.operation_date >= day_lo,
                    BankOperation.operation_date <= day_hi,
                )
            )
        )
        .scalars()
        .all()
    )

    candidates: list[TimeMatchCandidate] = []
    for operation in operations:
        if _is_card_noise(operation):
            continue
        if await _op_already_allocated(session, operation.id):
            continue
        requisites = _payee_requisites(operation)
        payee_inn = requisites.get("inn")
        if counterparty and counterparty.inn and payee_inn and payee_inn != counterparty.inn:
            continue
        amt = _money(abs(operation.amount))
        if amt < low or amt > high:  # SQL already bounds this; guard against sign edge
            continue
        amount_exact = amt == remaining

        tier: int | None
        minutes_delta: int | None
        if operation.posted_at is not None:
            delta_min = int(
                abs((_as_utc(operation.posted_at) - issued_utc).total_seconds()) // 60
            )
            minutes_delta = delta_min
            same_day = operation.operation_date == issued_date
            # When the time is known, the window always applies — a same-day op that is
            # actually ~12h off must NOT sneak past a tight window (and become confident).
            if delta_min > window_hours * 60:
                continue
            if delta_min <= tight_minutes and amount_exact:
                tier = 1
            elif same_day and amount_exact:
                tier = 2
            else:
                tier = 3
        else:
            # posted_at unknown → weakest signal: same business date only.
            if operation.operation_date != issued_date:
                continue
            tier, minutes_delta = 4, None

        candidates.append(
            TimeMatchCandidate(
                bank_operation_id=operation.id,
                operation_date=operation.operation_date,
                posted_at=operation.posted_at,
                amount=amt,
                official_name=requisites.get("recipientName"),
                inn=payee_inn,
                requisites=requisites,
                tier=tier,
                minutes_delta=minutes_delta,
            )
        )

    # Best first: lower tier wins; within a tier, the closest time wins.
    candidates.sort(key=lambda c: (c.tier, c.minutes_delta if c.minutes_delta is not None else 10**9))  # noqa: E501
    suggestion.candidates = candidates
    # Confident = exactly one strong candidate (tight/same-day + exact amount). Robust to
    # empty INNs (manual counterparties often have none), unlike a distinct-INN count.
    strong = [c for c in candidates if c.tier <= 2]
    suggestion.confident = len(strong) == 1
    return suggestion


async def _apply_bank_allocation(
    session: AsyncSession,
    *,
    invoice: SupplierInvoice,
    operation: BankOperation,
    amount: Decimal,
    actor_user_id: uuid.UUID | None,
) -> None:
    """Add one bank allocation to the invoice WITHOUT committing — the commit-less core
    shared by ``confirm_invoice_match`` and the split orchestrator. The caller validates
    occupancy/remaining and runs ``_recompute_status`` + commit."""
    session.add(
        InvoicePaymentAllocation(
            invoice_id=invoice.id,
            source_kind="bank",
            bank_operation_id=operation.id,
            amount=_money(amount),
            created_by_user_id=actor_user_id,
        )
    )
    await session.flush()


def assert_bank_matchable(invoice: SupplierInvoice, operation: BankOperation) -> None:
    """Guard shared by confirm + split: only a plain payable matches an OUTGOING supplier
    payment. Mirrors the suggestion-layer filters so a crafted POST (bypassing the picker)
    can't allocate a receivable/barter invoice or a card/internal-transfer/incoming operation."""
    if invoice.direction != "payable" or invoice.barter_role is not None:
        raise CounterpartyMatchError("Накладная не поддерживает банковский мэтч")
    if (
        operation.direction != "out"
        or _is_card_noise(operation)
        or operation.transfer_group_id is not None
    ):
        raise CounterpartyMatchError("Операция не является исходящим платежом поставщику")


async def confirm_invoice_match(
    session: AsyncSession,
    *,
    invoice_id: uuid.UUID,
    bank_operation_id: uuid.UUID,
    enrich: bool,
    actor_user_id: uuid.UUID | None,
) -> dict[str, Any]:
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        raise CounterpartyMatchError("Накладная не найдена")
    operation = await session.get(BankOperation, bank_operation_id)
    if operation is None:
        raise CounterpartyMatchError("Банковская операция не найдена")
    assert_bank_matchable(invoice, operation)
    if await _op_already_allocated(session, operation.id):
        raise CounterpartyMatchError("Эта операция уже использована в сверке")

    remaining = _money(invoice.amount) - await _allocated_amount(session, invoice.id)
    if remaining <= 0:
        raise CounterpartyMatchError("Накладная уже оплачена")
    await _apply_bank_allocation(
        session,
        invoice=invoice,
        operation=operation,
        amount=min(remaining, _money(abs(operation.amount))),
        actor_user_id=actor_user_id,
    )
    await _recompute_status(session, invoice)

    enriched = False
    if enrich:
        enriched = await _enrich_counterparty(
            session, invoice.counterparty_id, operation, actor_user_id
        )

    await session.commit()
    return {
        "invoice_id": str(invoice.id),
        "payment_status": invoice.payment_status,
        "enriched": enriched,
    }


async def _enrich_counterparty(
    session: AsyncSession,
    counterparty_id: uuid.UUID,
    operation: BankOperation,
    actor_user_id: uuid.UUID | None,
) -> bool:
    requisites = _payee_requisites(operation)
    payee_inn = requisites.get("inn")
    counterparty = await session.get(Counterparty, counterparty_id)
    if counterparty is None:
        return False
    # Never overwrite an existing, different INN — that would mean a wrong payee.
    if counterparty.inn and payee_inn and counterparty.inn != payee_inn:
        raise CounterpartyMatchError(
            "У контрагента уже указан другой ИНН — обновление реквизитов отклонено"
        )

    if requisites.get("recipientName"):
        counterparty.name = requisites["recipientName"]
    if payee_inn and not counterparty.inn:
        counterparty.inn = payee_inn
    if counterparty.status == "requires_setup":
        counterparty.status = "active"

    profile = await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == counterparty_id
        )
    )
    if profile is None:
        profile = CounterpartyPayableProfile(counterparty_id=counterparty_id)
        session.add(profile)
        await session.flush()
    profile.requisites = requisites
    profile.requisites_verified = True
    profile.requisites_verified_at = datetime.now(tz=UTC)
    profile.requisites_verified_by_user_id = actor_user_id
    return True

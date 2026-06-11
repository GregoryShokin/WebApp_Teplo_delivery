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

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

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
    if await _op_already_allocated(session, operation.id):
        raise CounterpartyMatchError("Эта операция уже использована в сверке")

    remaining = _money(invoice.amount) - await _allocated_amount(session, invoice.id)
    if remaining <= 0:
        raise CounterpartyMatchError("Накладная уже оплачена")
    allocation_amount = min(remaining, _money(abs(operation.amount)))
    session.add(
        InvoicePaymentAllocation(
            invoice_id=invoice.id,
            source_kind="bank",
            bank_operation_id=operation.id,
            amount=allocation_amount,
            created_by_user_id=actor_user_id,
        )
    )
    await session.flush()
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

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Account,
    BankOperation,
    CashflowTransaction,
    ClassificationRule,
    Counterparty,
    OwnAccountsRegistry,
    ReconciliationCase,
    Wallet,
)
from app.services.banking.base import clean_digits

# A manually booked DDS entry (e.g. a supplier paid straight from a bank wallet via
# ``pay_invoice_from_wallet``) records the cash fact before the bank feed does. When the
# statement later imports the same operation we must LINK it to that entry instead of
# booking a second expense — otherwise the ДДС double-counts the payment. We match it
# conservatively (same wallet + direction + exact amount, a small date drift, same payee
# INN when the statement carries one) and only against these manual source kinds.
PREBOOKED_DATE_WINDOW_DAYS = 3
PREBOOKABLE_SOURCE_KINDS = ("counterparty_payment", "kassa_cheque")


@dataclass(frozen=True)
class ClassificationResult:
    classified: int = 0
    internal_transfer: int = 0
    needs_review: int = 0
    excluded: int = 0


async def run_classification_rules(
    session: AsyncSession, operations: list[BankOperation]
) -> ClassificationResult:
    rules = (
        await session.scalars(
            select(ClassificationRule)
            .where(ClassificationRule.is_active.is_(True))
            .order_by(ClassificationRule.priority, ClassificationRule.name)
        )
    ).all()
    counts = {"classified": 0, "internal_transfer": 0, "needs_review": 0, "excluded": 0}
    # Transactions claimed by an op earlier in this same run aren't flushed yet, so the
    # "already linked" subquery can't see them — track them in-memory to keep matching 1:1.
    claimed_transaction_ids: set[UUID] = set()

    for operation in operations:
        if operation.cashflow_transaction_id is None:
            prebooked = await _find_prebooked_payment(
                session, operation, claimed=claimed_transaction_ids
            )
            if prebooked is not None:
                operation.cashflow_transaction_id = prebooked.id
                operation.classification_status = "classified"
                claimed_transaction_ids.add(prebooked.id)
                counts["classified"] += 1
                continue
        matched = False
        for rule in rules:
            if not await _rule_matches(session, rule, operation):
                continue
            matched = True
            await apply_operation_action(
                session,
                operation,
                action=rule.action,
                article_id=rule.article_id,
                counterparty_id=rule.counterparty_id,
                quality_status="auto",
            )
            if rule.action == "set_article":
                counts["classified"] += 1
            elif rule.action == "mark_internal_transfer":
                counts["internal_transfer"] += 1
            elif rule.action == "exclude":
                counts["excluded"] += 1
            break
        if not matched:
            operation.classification_status = "needs_review"
            await create_or_update_reconciliation_case(
                session,
                kind="unclassified_operation",
                provider=operation.provider,
                bank_operation_id=operation.id,
                payload=_operation_review_payload(operation),
            )
            counts["needs_review"] += 1

    await session.flush()
    from app.services.banking.transfer_matching import find_and_link_transfer_pairs

    await find_and_link_transfer_pairs(session)
    return ClassificationResult(**counts)


async def apply_operation_action(
    session: AsyncSession,
    operation: BankOperation,
    *,
    action: str,
    article_id: UUID | None = None,
    counterparty_id: UUID | None = None,
    quality_status: str = "auto",
) -> None:
    if action == "set_article":
        if article_id is None:
            operation.classification_status = "needs_review"
            await create_or_update_reconciliation_case(
                session,
                kind="unclassified_operation",
                provider=operation.provider,
                bank_operation_id=operation.id,
                payload={**_operation_review_payload(operation), "reason": "rule_without_article"},
            )
            return
        wallet = await _wallet_for_operation(session, operation)
        if wallet is None:
            operation.classification_status = "needs_review"
            await create_or_update_reconciliation_case(
                session,
                kind="unclassified_operation",
                provider=operation.provider,
                bank_operation_id=operation.id,
                payload={**_operation_review_payload(operation), "reason": "wallet_not_found"},
            )
            return
        transaction = None
        if operation.cashflow_transaction_id is not None:
            transaction = await session.get(CashflowTransaction, operation.cashflow_transaction_id)
        if transaction is None:
            # Manual classification can reach this directly (bypassing the reconcile pass
            # in ``run_classification_rules``) — link a pre-booked manual payment here too
            # so a duplicate expense is never created. Keep its article/source/allocation.
            prebooked = await _find_prebooked_payment(session, operation, claimed=set())
            if prebooked is not None:
                operation.cashflow_transaction_id = prebooked.id
                operation.classification_status = "classified"
                return
        if transaction is None:
            transaction = CashflowTransaction(
                wallet_id=wallet.id,
                direction=operation.direction,
                amount=operation.amount,
                operation_date=operation.operation_date,
                article_id=article_id,
                counterparty_id=counterparty_id,
                source_kind="bank_operation",
                source_id=operation.id,
                payment_purpose=operation.payment_purpose,
                quality_status=quality_status,
            )
            session.add(transaction)
            await session.flush()
            operation.cashflow_transaction_id = transaction.id
        else:
            transaction.wallet_id = wallet.id
            transaction.direction = operation.direction
            transaction.amount = operation.amount
            transaction.operation_date = operation.operation_date
            transaction.article_id = article_id
            transaction.counterparty_id = counterparty_id
            transaction.payment_purpose = operation.payment_purpose
            transaction.quality_status = quality_status
        operation.classification_status = "classified"
        return

    if action == "mark_internal_transfer":
        operation.classification_status = "internal_transfer"
        operation.cashflow_transaction_id = None
        return

    if action == "exclude":
        operation.classification_status = "excluded"
        operation.cashflow_transaction_id = None
        return

    operation.classification_status = "needs_review"
    await create_or_update_reconciliation_case(
        session,
        kind="unclassified_operation",
        provider=operation.provider,
        bank_operation_id=operation.id,
        payload={**_operation_review_payload(operation), "reason": f"unknown_action:{action}"},
    )


async def create_or_update_reconciliation_case(
    session: AsyncSession,
    *,
    kind: str,
    provider: str | None = None,
    bank_operation_id: UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> ReconciliationCase:
    query = select(ReconciliationCase).where(
        ReconciliationCase.kind == kind,
        ReconciliationCase.status == "pending",
    )
    if bank_operation_id is not None:
        query = query.where(ReconciliationCase.bank_operation_id == bank_operation_id)
    elif provider is not None:
        query = query.where(ReconciliationCase.provider == provider)
    existing = await session.scalar(query)
    if existing is not None:
        existing.payload = payload or existing.payload or {}
        return existing
    case = ReconciliationCase(
        kind=kind,
        status="pending",
        provider=provider,
        bank_operation_id=bank_operation_id,
        payload=payload or {},
    )
    session.add(case)
    await session.flush()
    return case


async def close_reconciliation_case(
    session: AsyncSession,
    case: ReconciliationCase,
    *,
    status: str,
    resolution_payload: dict[str, Any] | None = None,
) -> None:
    case.status = status
    case.resolved_at = datetime.now(UTC)
    case.resolution_payload = resolution_payload or {}


async def _rule_matches(
    session: AsyncSession, rule: ClassificationRule, operation: BankOperation
) -> bool:
    if rule.provider and rule.provider != operation.provider:
        return False
    if rule.direction and rule.direction != operation.direction:
        return False
    expected_inn = clean_digits(rule.counterparty_inn_match)
    if expected_inn and expected_inn != clean_digits(operation.counterparty_inn_raw):
        return False
    if rule.counterparty_name_pattern and not _contains(
        operation.counterparty_name_raw, rule.counterparty_name_pattern
    ):
        return False
    if rule.purpose_pattern and not _contains(operation.payment_purpose, rule.purpose_pattern):
        return False
    amount = Decimal(operation.amount)
    if rule.amount_min is not None and amount < Decimal(rule.amount_min):
        return False
    if rule.amount_max is not None and amount > Decimal(rule.amount_max):
        return False
    if rule.action == "mark_internal_transfer":
        return await _counterparty_is_own_account(session, operation)
    return True


async def _counterparty_is_own_account(session: AsyncSession, operation: BankOperation) -> bool:
    account_number = clean_digits(operation.counterparty_account_raw)
    inn = clean_digits(operation.counterparty_inn_raw)
    if not account_number and not inn:
        return False
    conditions = []
    if account_number:
        conditions.append(OwnAccountsRegistry.account_number == account_number)
    if inn:
        conditions.append(OwnAccountsRegistry.legal_entity_inn == inn)
    query = select(OwnAccountsRegistry).where(
        OwnAccountsRegistry.is_active.is_(True), or_(*conditions)
    )
    return await session.scalar(query) is not None


async def _wallet_for_operation(session: AsyncSession, operation: BankOperation) -> Wallet | None:
    if operation.account_id is not None:
        wallet = await session.scalar(
            select(Wallet).where(
                Wallet.account_id == operation.account_id,
                Wallet.status == "active",
            )
        )
        if wallet is not None:
            return wallet
    return await session.scalar(
        select(Wallet)
        .join(Account, Account.id == Wallet.account_id)
        .where(
            Account.bank_code == operation.provider,
            Wallet.type == "bank",
            Wallet.status == "active",
            Wallet.is_internal_transfer_eligible.is_(True),
        )
        .order_by(Wallet.code)
    )


async def _find_prebooked_payment(
    session: AsyncSession, operation: BankOperation, *, claimed: set[UUID]
) -> CashflowTransaction | None:
    """A manually pre-booked DDS entry that this bank operation settles, or ``None``.

    Linking the bank operation to it — instead of booking a fresh ``bank_operation``
    expense — is what prevents a double expense in the ДДС. Conservative on purpose:
    same wallet (so cash/card entries on other wallets are never touched), same
    direction, exact amount, a small date drift, not yet linked to any bank operation,
    and the same payee INN whenever the statement carries one.
    """
    wallet = await _wallet_for_operation(session, operation)
    if wallet is None:
        return None
    low = operation.operation_date - timedelta(days=PREBOOKED_DATE_WINDOW_DAYS)
    high = operation.operation_date + timedelta(days=PREBOOKED_DATE_WINDOW_DAYS)
    already_linked = (
        select(BankOperation.id)
        .where(BankOperation.cashflow_transaction_id == CashflowTransaction.id)
        .exists()
    )
    query = (
        select(CashflowTransaction)
        .where(
            CashflowTransaction.wallet_id == wallet.id,
            CashflowTransaction.direction == operation.direction,
            CashflowTransaction.amount == operation.amount,
            CashflowTransaction.source_kind.in_(PREBOOKABLE_SOURCE_KINDS),
            CashflowTransaction.operation_date >= low,
            CashflowTransaction.operation_date <= high,
            ~already_linked,
        )
        .order_by(CashflowTransaction.created_at)
    )
    if claimed:
        query = query.where(CashflowTransaction.id.not_in(claimed))
    candidates = (await session.scalars(query)).all()
    if not candidates:
        return None

    op_inn = clean_digits(operation.counterparty_inn_raw)
    if not op_inn:
        # Statement has no INN to disambiguate — fall back to the oldest candidate (FIFO).
        return candidates[0]
    fallback: CashflowTransaction | None = None
    for candidate in candidates:
        candidate_inn = await _transaction_counterparty_inn(session, candidate)
        if candidate_inn == op_inn:
            return candidate
        if candidate_inn is None and fallback is None:
            fallback = candidate
    # A candidate with a *different* known INN is never matched — only same-INN or unknown.
    return fallback


async def _transaction_counterparty_inn(
    session: AsyncSession, transaction: CashflowTransaction
) -> str | None:
    if transaction.counterparty_id is None:
        return None
    inn = await session.scalar(
        select(Counterparty.inn).where(Counterparty.id == transaction.counterparty_id)
    )
    return clean_digits(inn) or None


def _contains(value: str | None, pattern: str) -> bool:
    return pattern.casefold() in (value or "").casefold()


def _operation_review_payload(operation: BankOperation) -> dict[str, Any]:
    return {
        "provider": operation.provider,
        "provider_operation_id": operation.provider_operation_id,
        "operation_date": operation.operation_date.isoformat(),
        "direction": operation.direction,
        "amount": str(operation.amount),
        "counterparty_name_raw": operation.counterparty_name_raw,
        "counterparty_inn_raw": operation.counterparty_inn_raw,
        "counterparty_account_raw": operation.counterparty_account_raw,
        "payment_purpose": operation.payment_purpose,
    }

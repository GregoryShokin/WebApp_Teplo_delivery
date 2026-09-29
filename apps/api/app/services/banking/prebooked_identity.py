"""Resolve bank payment markers through the document that owns a prebooked cashflow."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Account,
    BankOperation,
    CashflowTransaction,
    CounterpartyPaymentDraft,
    DepositBankDraft,
    EmployeePayout,
    PayrollBankDraft,
    ReconciliationCase,
    SalaryAdvanceBankDraft,
    SupplierPrepayment,
)
from app.services.banking.base import clean_digits
from app.services.banking.payment_purpose import (
    extract_payment_match_markers,
    is_owner_card_payment_purpose,
    payment_match_marker,
)
from app.services.banking.tbank import _document_number

OWNER_CARD_WAITING_REASON = "owner_card_payment_awaiting_source"
PREPAYMENT_WAITING_REASON = "tagged_prepayment_awaiting_source"
SOURCE_PAYMENT_REFUSAL = (
    "Эта операция относится к банковскому черновику платежа. "
    "Дождитесь проведения исходного платежа и его сопоставления с выпиской; "
    "изменяйте исходный документ в разделе платежей, а не создавайте второй расход или перевод."
)
_LEGACY_COURIER_OPERATION_RE = re.compile(r"\bоперация\s*#\s*(\d+)\b", re.IGNORECASE)


@dataclass(frozen=True)
class _Identity:
    markers: frozenset[str]
    document_number: str | None = None
    provider: str | None = None
    payer_account: str | None = None
    requires_marker: bool = False


def _request_payload(payload: Any) -> Mapping[str, Any]:
    """Payroll is flat; advance/deposit/payout use request; payroll top-ups use payload."""
    if not isinstance(payload, Mapping):
        return {}
    for key in ("request", "payload"):
        child = payload.get(key)
        if isinstance(child, Mapping):
            return _request_payload(child)
    return payload


def _identity_from_record(record: Any, fallback_document: str, amount: Decimal) -> _Identity:
    request = _request_payload(record.payload) if record is not None else {}
    request_markers = extract_payment_match_markers(
        str(request.get("paymentPurpose") or request.get("purpose") or "")
    )
    requires_marker = bool(request_markers)
    request_amount = request.get("amount")
    record_amount = getattr(record, "amount", None)
    # A payroll top-up request is for the delta; its cashflow may still be an aggregate.
    # A matching marker must never make unequal cash facts interchangeable.
    expected_amount = request_amount if request_amount is not None else record_amount
    if expected_amount is not None:
        try:
            if Decimal(str(expected_amount)) != Decimal(str(amount)):
                return _Identity(frozenset(), requires_marker=requires_marker)
        except InvalidOperation:
            return _Identity(frozenset(), requires_marker=requires_marker)
    document_id = str(getattr(record, "document_id", None) or fallback_document)
    markers = request_markers
    if not markers:
        markers = frozenset({payment_match_marker(document_id)})
    return _Identity(
        markers=markers,
        document_number=str(request.get("documentNumber") or _document_number(document_id)),
        provider=getattr(record, "bank_provider", None),
        payer_account=clean_digits(request.get("accountNumber") or request.get("payerAccount"))
        or None,
        requires_marker=requires_marker,
    )


async def _cashflow_identity(session: AsyncSession, transaction: CashflowTransaction) -> _Identity:
    source_id = transaction.source_id
    source_kind = transaction.source_kind
    if source_kind == "supplier_prepayment" and source_id is not None:
        prepayment = await session.get(SupplierPrepayment, source_id)
        if prepayment is None or prepayment.cashflow_transaction_id != transaction.id:
            return _Identity(frozenset())
        drafts = (
            await session.scalars(
                select(CounterpartyPaymentDraft).where(
                    CounterpartyPaymentDraft.creates_prepayment.is_(True),
                    CounterpartyPaymentDraft.payload["dds_prepayment_id"].astext == str(source_id),
                )
            )
        ).all()
        if len(drafts) != 1:
            # Historical facts have no reliable draft FK: never infer one from amount/date.
            # Ambiguous explicit links still cannot fall back to an untagged statement.
            return _Identity(
                frozenset(),
                requires_marker=any(
                    _identity_from_record(
                        draft, draft.document_id, transaction.amount
                    ).requires_marker
                    for draft in drafts
                ),
            )
        return _identity_from_record(drafts[0], drafts[0].document_id, transaction.amount)
    if source_kind in {"counterparty_payment", "supplier_bank_to_safe"} and source_id is not None:
        draft = await session.get(CounterpartyPaymentDraft, source_id)
        if draft is None:
            # Direct wallet invoice payments have this source kind too, but no bank draft.
            return _Identity(frozenset())
        return _identity_from_record(draft, f"teplo-cp-{source_id}", transaction.amount)
    if source_kind in {"payroll_payout", "payroll_bank_to_safe"} and source_id is not None:
        draft = await session.scalar(
            select(PayrollBankDraft).where(PayrollBankDraft.run_id == source_id)
        )
        return _identity_from_record(draft, f"teplo-payroll-{source_id}", transaction.amount)
    if source_kind == "salary_advance_bank_to_safe" and source_id is not None:
        draft = await session.scalar(
            select(SalaryAdvanceBankDraft).where(SalaryAdvanceBankDraft.advance_id == source_id)
        )
        return _identity_from_record(draft, f"teplo-advance-{source_id}", transaction.amount)
    if source_kind == "employee_payout_bank_to_safe" and source_id is not None:
        payout = await session.get(EmployeePayout, source_id)
        return _identity_from_record(payout, f"teplo-emppayout-{source_id}", transaction.amount)
    if source_kind == "production_deposit_payout_draft" and source_id is not None:
        # Current source_id is DepositBankDraft.id; older direct transits used the payout id.
        draft = await session.get(DepositBankDraft, source_id)
        return _identity_from_record(draft, f"teplo-deposit-{source_id}", transaction.amount)
    if source_kind == "courier_deposit_return_draft":
        if source_id is not None:
            draft = await session.get(DepositBankDraft, source_id)
            if draft is not None:
                return _identity_from_record(
                    draft, f"teplo-deposit-{source_id}", transaction.amount
                )
        # Legacy courier ids are integers; their transit could only retain the id in its purpose.
        match = _LEGACY_COURIER_OPERATION_RE.search(transaction.payment_purpose or "")
        if match is not None:
            return _identity_from_record(
                None, f"teplo-courier-deposit-{match.group(1)}", transaction.amount
            )
    if source_kind == "manual_bank_to_safe" and source_id is not None:
        operation = await session.get(BankOperation, source_id)
        if operation is not None:
            return _Identity(
                extract_payment_match_markers(operation.payment_purpose),
                document_number=operation.document_number,
                provider=operation.provider,
                requires_marker=bool(extract_payment_match_markers(operation.payment_purpose)),
            )
    return _Identity(frozenset())


async def cashflow_requires_payment_marker(
    session: AsyncSession, transaction: CashflowTransaction
) -> bool:
    """A source actually sent a tag, rather than deriving one for legacy compatibility."""
    return (await _cashflow_identity(session, transaction)).requires_marker


async def _identity_matches_operation(
    session: AsyncSession, identity: _Identity, operation: BankOperation
) -> bool:
    markers = extract_payment_match_markers(operation.payment_purpose)
    if len(markers) != 1 or identity.markers != markers:
        return False
    if identity.provider and identity.provider != operation.provider:
        return False
    # Sber does not receive our T-Bank documentNumber. Its purpose marker is common to both.
    if (
        operation.provider == "tbank"
        and operation.document_number
        and identity.document_number
        and clean_digits(operation.document_number).lstrip("0")
        != clean_digits(identity.document_number).lstrip("0")
    ):
        return False
    if identity.payer_account and operation.account_id is not None:
        account = await session.get(Account, operation.account_id)
        if account is not None and clean_digits(account.account_number) != identity.payer_account:
            return False
    return True


async def bank_draft_matches_payment_identity(
    session: AsyncSession, draft: CounterpartyPaymentDraft, operation: BankOperation
) -> bool:
    """Check a statement marker against the exact bank request saved on its draft."""
    return await _identity_matches_operation(
        session, _identity_from_record(draft, draft.document_id, operation.amount), operation
    )


async def cashflow_matches_payment_identity(
    session: AsyncSession, transaction: CashflowTransaction, operation: BankOperation
) -> bool:
    """A marker identifies one source document, with the statement's monetary checks intact."""
    return await _identity_matches_operation(
        session, await _cashflow_identity(session, transaction), operation
    )


async def tagged_source_payment_reclassification_reason(
    session: AsyncSession, operation: BankOperation
) -> str | None:
    """Recognize source payments before manual routes can create another monetary fact."""
    # Exact common text belongs to the owner-card contour even if the bank omitted its tag.
    if is_owner_card_payment_purpose(operation.payment_purpose):
        return OWNER_CARD_WAITING_REASON
    markers = extract_payment_match_markers(operation.payment_purpose)
    if not markers:
        return None
    # Official counterparty payments retain their existing behavior. Standalone bank
    # prepayments need this narrow guard: old paid facts have no reliable draft linkage.
    drafts = (
        await session.scalars(
            select(CounterpartyPaymentDraft).where(
                CounterpartyPaymentDraft.creates_prepayment.is_(True),
                CounterpartyPaymentDraft.bank_provider == operation.provider,
                CounterpartyPaymentDraft.amount == operation.amount,
                CounterpartyPaymentDraft.status.in_(("created", "updated", "paid")),
            )
        )
    ).all()
    for draft in drafts:
        identity = _identity_from_record(draft, draft.document_id, operation.amount)
        if await _identity_matches_operation(session, identity, operation):
            return PREPAYMENT_WAITING_REASON
    return None


async def defer_unmatched_owner_card_operation(
    session: AsyncSession, operation: BankOperation
) -> bool:
    """Do not guess an expense while a marked source payment awaits its cashflow."""
    reason = await tagged_source_payment_reclassification_reason(session, operation)
    if reason is None:
        return False
    operation.classification_status = "needs_review"
    case = await session.scalar(
        select(ReconciliationCase).where(
            ReconciliationCase.kind == "unclassified_operation",
            ReconciliationCase.bank_operation_id == operation.id,
            ReconciliationCase.status == "pending",
        )
    )
    payload = {
        "reason": reason,
        "provider": operation.provider,
        "provider_operation_id": operation.provider_operation_id,
        "payment_purpose": operation.payment_purpose,
        "amount": str(operation.amount),
        "match_markers": sorted(extract_payment_match_markers(operation.payment_purpose)),
    }
    if case is None:
        session.add(
            ReconciliationCase(
                kind="unclassified_operation",
                status="pending",
                provider=operation.provider,
                bank_operation_id=operation.id,
                payload=payload,
            )
        )
    else:
        case.payload = payload
    await session.flush()
    return True

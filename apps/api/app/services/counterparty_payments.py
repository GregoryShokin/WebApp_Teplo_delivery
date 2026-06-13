"""Counterparty payment drafts → T-Bank.

Generalises the payroll bank-draft flow (``payroll_payouts`` +
``banking.tbank.build_payment_draft_api_payload``) to supplier invoices: a manager
selects one or more invoices of a single legal entity and sends them as one bank
draft. A draft is not a payment — the invoices stay unpaid until the bank feed is
matched (see ``counterparty_matching``).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models import (
    CashflowTransaction,
    Counterparty,
    CounterpartyPayableProfile,
    CounterpartyPaymentDraft,
    DdsArticle,
    InvoicePaymentAllocation,
    SupplierInvoice,
    Wallet,
)
from app.services.banking import BankClient, TbankClient
from app.services.banking.exceptions import BankFetchError
from app.services.banking.tbank import build_payment_draft_api_payload
from app.services.counterparty_matching import _invoice_remaining, _recompute_status

MOCK_PAYER_ACCOUNT = "00000000000000000000"
DRAFTABLE_STATUSES = frozenset({"unpaid", "partially_paid"})
DRAFT_STATUSES = frozenset({"created", "updated", "paid", "failed"})
# DDS article a manual supplier payment books to by default.
DEFAULT_SUPPLIER_ARTICLE_CODE = "payment_to_supplier"


class CounterpartyPaymentError(RuntimeError):
    """Domain error for counterparty payments (maps to HTTP 409)."""


class RequisitesNotVerifiedError(CounterpartyPaymentError):
    """Raised when a draft is attempted without verified requisites."""


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _payer_account(settings: Settings) -> str:
    if settings.tbank_api_account_number:
        return settings.tbank_api_account_number
    if settings.teplo_bank_client_mode == "mock":
        return MOCK_PAYER_ACCOUNT
    raise CounterpartyPaymentError("Не настроен T-Bank расчётный счёт плательщика")


async def _allocated_amount(session: AsyncSession, invoice_id: uuid.UUID) -> Decimal:
    total = await session.scalar(
        select(func.coalesce(func.sum(InvoicePaymentAllocation.amount), 0)).where(
            InvoicePaymentAllocation.invoice_id == invoice_id
        )
    )
    return _money(total)


def _aggregate_vat(invoices: Sequence[SupplierInvoice]) -> dict[str, Decimal]:
    """Sum VAT across the pack per rate (e.g. 10% / 22%)."""
    aggregate: dict[str, Decimal] = {}
    for invoice in invoices:
        for rate, amount in (invoice.vat_breakdown or {}).items():
            aggregate[rate] = aggregate.get(rate, Decimal(0)) + Decimal(str(amount))
    return {rate: _money(amount) for rate, amount in aggregate.items() if _money(amount) > 0}


def _vat_suffix(vat: dict[str, Decimal]) -> str:
    if not vat:
        return "Без НДС."
    parts = [
        f"{rate}% - {format(vat[rate], 'f').replace('.', ',')} руб."
        for rate in sorted(vat, key=lambda rate: float(rate))
    ]
    # Each part ends with "руб." so the last one provides the terminal period.
    return "В т.ч. НДС: " + "; ".join(parts)


def _payment_purpose(
    counterparty: Counterparty, invoices: Sequence[SupplierInvoice], vat: dict[str, Decimal]
) -> str:
    # VAT info is legally required and never truncated; fit the descriptive base
    # into the remaining budget so the whole purpose stays within 210 chars.
    suffix = _vat_suffix(vat)
    numbers = ", ".join(inv.number for inv in invoices if inv.number)
    base = f"Оплата поставщику {counterparty.name}"
    if numbers:
        base = f"{base} по счетам {numbers}"
    base = " ".join(base.split())
    budget = 210 - len(suffix) - 2
    if budget <= 0:
        return suffix[:210]
    return f"{base[:budget].rstrip()}. {suffix}"[:210]


def _safe_status(status: str | None) -> str:
    return status if status in DRAFT_STATUSES else "created"


async def create_payment_draft_for_invoices(
    session: AsyncSession,
    *,
    invoice_ids: Sequence[uuid.UUID],
    actor_user_id: uuid.UUID | None,
    bank_client: BankClient | None = None,
) -> CounterpartyPaymentDraft:
    unique_ids = list(dict.fromkeys(invoice_ids))
    if not unique_ids:
        raise CounterpartyPaymentError("Не выбраны накладные для оплаты")

    invoices = list(
        (await session.execute(select(SupplierInvoice).where(SupplierInvoice.id.in_(unique_ids))))
        .scalars()
        .all()
    )
    if len(invoices) != len(unique_ids):
        raise CounterpartyPaymentError("Некоторые накладные не найдены")

    counterparty_ids = {inv.counterparty_id for inv in invoices}
    if len(counterparty_ids) != 1:
        raise CounterpartyPaymentError(
            "В один черновик можно собрать только накладные одного юрлица"
        )
    counterparty_id = counterparty_ids.pop()

    if any(inv.payment_status not in DRAFTABLE_STATUSES for inv in invoices):
        raise CounterpartyPaymentError(
            "Среди выбранных есть оплаченные или аннулированные накладные"
        )
    if any(inv.direction != "payable" for inv in invoices):
        raise CounterpartyPaymentError("Доходные накладные нельзя отправить в банк")
    if any(inv.draft_id is not None for inv in invoices):
        raise CounterpartyPaymentError("Некоторые накладные уже отправлены в банк")

    counterparty = await session.get(Counterparty, counterparty_id)
    if counterparty is None:
        raise CounterpartyPaymentError("Контрагент не найден")
    if counterparty.status == "archived":
        raise CounterpartyPaymentError("Контрагент в архиве — отправка в банк недоступна")
    profile = await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == counterparty_id
        )
    )
    if profile is not None and profile.relationship == "informal":
        raise CounterpartyPaymentError(
            "Контрагент оплачивается картой/наличными — отправка в банк недоступна"
        )
    if profile is None or not profile.requisites_verified:
        raise RequisitesNotVerifiedError(
            "Реквизиты контрагента не подтверждены — отправка в банк недоступна"
        )

    total = Decimal(0)
    for inv in invoices:
        total += _money(inv.amount) - await _allocated_amount(session, inv.id)
    total = _money(total)
    if total <= 0:
        raise CounterpartyPaymentError("Сумма к оплате равна нулю")

    settings = get_settings()
    payer_account = _payer_account(settings)
    purpose = _payment_purpose(counterparty, invoices, _aggregate_vat(invoices))

    draft = CounterpartyPaymentDraft(
        id=uuid.uuid4(),
        counterparty_id=counterparty_id,
        document_id="",
        amount=total,
        status="created",
        created_by_user_id=actor_user_id,
    )
    document_id = f"teplo-cp-{draft.id}"
    draft.document_id = document_id[:64]

    requisites: dict[str, Any] = dict(profile.requisites or {})
    requisites.setdefault("recipientName", counterparty.name)
    if counterparty.inn:
        requisites.setdefault("inn", counterparty.inn)

    try:
        payload = build_payment_draft_api_payload(
            document_id=document_id,
            amount=total,
            purpose=purpose,
            requisites=requisites,
            payer_account=payer_account,
        )
    except ValueError as exc:
        raise CounterpartyPaymentError(f"Реквизиты неполны: {exc}") from exc

    client = bank_client or TbankClient(session)
    try:
        result = await client.create_payment_draft(
            document_id=document_id,
            amount=total,
            purpose=purpose,
            requisites=requisites,
            payer_account=payer_account,
        )
    except BankFetchError as exc:
        draft.status = "failed"
        draft.payload = payload
        draft.last_error = str(exc)
        session.add(draft)
        await session.commit()
        raise

    draft.status = _safe_status(result.status)
    draft.provider_ref = result.provider_ref
    draft.payload = payload
    draft.synced_at = datetime.now(tz=UTC)
    session.add(draft)
    await session.flush()
    for inv in invoices:
        inv.draft_id = draft.id
    await session.commit()
    await session.refresh(draft)
    return draft


async def cancel_payment_draft(session: AsyncSession, *, draft_id: uuid.UUID) -> None:
    """Unlink invoices and remove a draft that has not been paid/matched yet."""
    draft = await session.get(CounterpartyPaymentDraft, draft_id)
    if draft is None:
        raise CounterpartyPaymentError("Черновик не найден")
    if draft.status == "paid":
        raise CounterpartyPaymentError("Черновик уже оплачен — отмена недоступна")
    invoices = list(
        (await session.execute(select(SupplierInvoice).where(SupplierInvoice.draft_id == draft_id)))
        .scalars()
        .all()
    )
    for inv in invoices:
        inv.draft_id = None
    await session.delete(draft)
    await session.commit()


async def get_payment_draft(
    session: AsyncSession, draft_id: uuid.UUID
) -> CounterpartyPaymentDraft | None:
    return await session.get(CounterpartyPaymentDraft, draft_id)


async def list_draft_invoices(session: AsyncSession, draft_id: uuid.UUID) -> list[SupplierInvoice]:
    return list(
        (
            await session.execute(
                select(SupplierInvoice)
                .where(SupplierInvoice.draft_id == draft_id)
                .order_by(SupplierInvoice.invoice_date)
            )
        )
        .scalars()
        .all()
    )


async def pay_invoice_from_wallet(
    session: AsyncSession,
    *,
    invoice_id: uuid.UUID,
    wallet_id: uuid.UUID,
    amount: Decimal,
    operation_date: date,
    article_id: uuid.UUID | None = None,
    comment: str | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> SupplierInvoice:
    """Manually pay (part of) a payable invoice from a DDS wallet — bypasses the bank.

    Creates a real DDS expense (``cashflow_transaction``, source_kind
    ``counterparty_payment``) on the chosen wallet and allocates it to the invoice,
    supporting splits and partial payments; the unpaid remainder can still be sent to
    the bank as a draft. Intended for cash/card payments that don't go through the bank
    transfer flow — bank transfers should use the draft+reconcile path to avoid a
    duplicate DDS entry.
    """
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        raise CounterpartyPaymentError("Накладная не найдена")
    if invoice.payment_status == "void":
        raise CounterpartyPaymentError("Накладная аннулирована")
    if invoice.direction != "payable":
        raise CounterpartyPaymentError("Доходную накладную нельзя оплатить со счёта")

    requested = _money(amount)
    remaining = await _invoice_remaining(session, invoice)
    if requested <= 0 or requested > remaining:
        raise CounterpartyPaymentError("Сумма оплаты вне допустимого остатка")

    wallet = await session.get(Wallet, wallet_id)
    if wallet is None or wallet.status != "active":
        raise CounterpartyPaymentError("Счёт не найден или неактивен")

    resolved_article_id = article_id
    if resolved_article_id is None:
        resolved_article_id = await session.scalar(
            select(DdsArticle.id).where(DdsArticle.code == DEFAULT_SUPPLIER_ARTICLE_CODE)
        )
    elif await session.get(DdsArticle, resolved_article_id) is None:
        raise CounterpartyPaymentError("Статья ДДС не найдена")

    purpose = f"Оплата накладной {invoice.number}" if invoice.number else "Оплата поставщику"
    transaction = CashflowTransaction(
        wallet_id=wallet.id,
        direction="out",
        amount=requested,
        operation_date=operation_date,
        article_id=resolved_article_id,
        counterparty_id=invoice.counterparty_id,
        source_kind="counterparty_payment",
        source_id=invoice.id,
        payment_purpose=purpose,
        comment=comment,
        quality_status="final",
    )
    session.add(transaction)
    await session.flush()

    session.add(
        InvoicePaymentAllocation(
            invoice_id=invoice.id,
            source_kind="cash",
            cashflow_transaction_id=transaction.id,
            amount=requested,
            created_by_user_id=actor_user_id,
        )
    )
    await session.flush()
    await _recompute_status(session, invoice)
    await session.commit()
    await session.refresh(invoice)
    return invoice

"""Тест поглотителя авто-классифицированных операций prebooked-проводкой оплаты поставщику.

Гонка «правило↔пометка оплаты»: операция выписки авто-классифицируется правилом в свою
``bank_operation``-строку ДО того, как черновик помечается ``paid`` и создаёт prebooked-проводку
``counterparty_payment``. ``reconcile_needs_review_prebooked`` их не сводит (операция уже
``classified``, а не ``needs_review``) → платёж двоится. Поглотитель находит авто-строку
детерминированно по documentNumber черновика и сводит её с prebooked-проводкой.
Инцидент 07.07: ИП Егиазарян 370 ₽ + ООО «Альянс Юг» 32 824,98 ₽.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    Account,
    BankOperation,
    CashflowTransaction,
    Counterparty,
    CounterpartyPaymentDraft,
    DdsArticle,
    InvoicePaymentAllocation,
    SupplierInvoice,
    Wallet,
)
from app.services.banking.classifier import absorb_auto_classified_counterparty_payment
from app.services.banking.tbank import _document_number

OP_DATE = date(2026, 7, 7)


async def _bank_wallet(session: AsyncSession) -> Wallet:
    account = Account(
        id=uuid.uuid4(),
        bank_code="tbank",
        account_number=f"4080281{uuid.uuid4().int % 10**12:012d}",
        legal_entity="ИП Шокина Е.А.",
        status="active",
    )
    session.add(account)
    await session.flush()
    wallet = Wallet(
        id=uuid.uuid4(),
        code=f"bank-abs-{uuid.uuid4().hex[:8]}",
        name="Тест банк (absorb)",
        type="bank",
        status="active",
        account_id=account.id,
        opening_balance=Decimal("0"),
    )
    session.add(wallet)
    await session.flush()
    return wallet


async def _draft_and_prebooked(
    session: AsyncSession, *, wallet: Wallet, amount: Decimal
) -> tuple[CounterpartyPaymentDraft, CashflowTransaction]:
    draft = CounterpartyPaymentDraft(
        id=uuid.uuid4(),
        document_id=f"teplo-cp-{uuid.uuid4()}",
        amount=amount,
        status="paid",
    )
    session.add(draft)
    await session.flush()
    article_id = await session.scalar(
        select(DdsArticle.id).where(DdsArticle.code == "payment_to_supplier")
    )
    prebooked = CashflowTransaction(
        wallet_id=wallet.id,
        direction="out",
        amount=amount,
        operation_date=OP_DATE,
        article_id=article_id,
        source_kind="counterparty_payment",
        source_id=draft.id,
        payment_purpose="Оплата по статусу платежа банка",
        quality_status="final",
    )
    session.add(prebooked)
    await session.flush()
    return draft, prebooked


async def _auto_classified_op(
    session: AsyncSession,
    *,
    wallet: Wallet,
    amount: Decimal,
    document_number: str,
    quality: str = "auto",
) -> tuple[BankOperation, CashflowTransaction]:
    """Операция выписки, УЖЕ авто-классифицированная правилом в свою bank_operation-строку."""
    article_id = await session.scalar(
        select(DdsArticle.id).where(DdsArticle.code == "payment_to_supplier")
    )
    auto_row = CashflowTransaction(
        wallet_id=wallet.id,
        direction="out",
        amount=amount,
        operation_date=OP_DATE,
        article_id=article_id,
        source_kind="bank_operation",
        payment_purpose="Оплата поставщику по счетам. Без НДС.",
        quality_status=quality,
    )
    session.add(auto_row)
    await session.flush()
    op = BankOperation(
        id=uuid.uuid4(),
        provider="tbank",
        provider_operation_id=f"op-{uuid.uuid4()}",
        account_id=wallet.account_id,
        operation_date=OP_DATE,
        direction="out",
        amount=amount,
        document_number=document_number,
        payment_purpose="Оплата поставщику по счетам. Без НДС.",
        raw_payload={},
        classification_status="classified",
        cashflow_transaction_id=auto_row.id,
    )
    session.add(op)
    await session.flush()
    auto_row.source_id = op.id
    await session.flush()
    return op, auto_row


async def test_absorb_relinks_operation_and_deletes_auto_row(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    amount = Decimal("370.00")
    async with async_session_factory() as session:
        wallet = await _bank_wallet(session)
        draft, prebooked = await _draft_and_prebooked(session, wallet=wallet, amount=amount)
        docnum = _document_number(draft.document_id)
        op, auto_row = await _auto_classified_op(
            session, wallet=wallet, amount=amount, document_number=docnum
        )
        await session.commit()
        op_id, prebooked_id, auto_id = op.id, prebooked.id, auto_row.id

    async with async_session_factory() as session:
        absorbed = await absorb_auto_classified_counterparty_payment(session)
        await session.commit()
    assert absorbed == 1

    async with async_session_factory() as session:
        op = await session.get(BankOperation, op_id)
        auto_row = await session.get(CashflowTransaction, auto_id)
    # Операция перепривязана к prebooked-проводке; авто-строка удалена.
    assert op.cashflow_transaction_id == prebooked_id
    assert op.classification_status == "classified"
    assert auto_row is None


async def test_absorb_skips_documentnumber_mismatch(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    amount = Decimal("370.00")
    async with async_session_factory() as session:
        wallet = await _bank_wallet(session)
        draft, _prebooked = await _draft_and_prebooked(session, wallet=wallet, amount=amount)
        our_docnum = _document_number(draft.document_id)
        wrong_docnum = str((int(our_docnum) + 1) % 999999 or 1)
        op, auto_row = await _auto_classified_op(
            session, wallet=wallet, amount=amount, document_number=wrong_docnum
        )
        await session.commit()
        op_id, auto_id = op.id, auto_row.id

    async with async_session_factory() as session:
        absorbed = await absorb_auto_classified_counterparty_payment(session)
        await session.commit()
    assert absorbed == 0

    async with async_session_factory() as session:
        op = await session.get(BankOperation, op_id)
        auto_row = await session.get(CashflowTransaction, auto_id)
    # Ничего не тронуто — операция всё ещё на своей авто-строке.
    assert op.cashflow_transaction_id == auto_id
    assert auto_row is not None


async def test_absorb_skips_auto_row_with_allocation(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Авто-строку с зависимостями (аллокация накладной) не трогаем — там есть гашение."""
    amount = Decimal("370.00")
    async with async_session_factory() as session:
        wallet = await _bank_wallet(session)
        draft, _prebooked = await _draft_and_prebooked(session, wallet=wallet, amount=amount)
        docnum = _document_number(draft.document_id)
        op, auto_row = await _auto_classified_op(
            session, wallet=wallet, amount=amount, document_number=docnum
        )
        cp = Counterparty(id=uuid.uuid4(), name="Поставщик", inn="7707133576", type="legal_entity")
        session.add(cp)
        await session.flush()
        invoice = SupplierInvoice(
            id=uuid.uuid4(),
            counterparty_id=cp.id,
            number="X-1",
            amount=amount,
            direction="payable",
            source="email",
            payment_status="paid",
        )
        session.add(invoice)
        await session.flush()
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id,
                source_kind="cash",
                cashflow_transaction_id=auto_row.id,
                amount=amount,
            )
        )
        await session.commit()
        op_id, auto_id = op.id, auto_row.id

    async with async_session_factory() as session:
        absorbed = await absorb_auto_classified_counterparty_payment(session)
        await session.commit()
    assert absorbed == 0

    async with async_session_factory() as session:
        op = await session.get(BankOperation, op_id)
        auto_row = await session.get(CashflowTransaction, auto_id)
    assert op.cashflow_transaction_id == auto_id
    assert auto_row is not None


async def test_absorb_skips_non_auto_quality(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ручную/final разметку не поглощаем — только правило-``auto``."""
    amount = Decimal("370.00")
    async with async_session_factory() as session:
        wallet = await _bank_wallet(session)
        draft, _prebooked = await _draft_and_prebooked(session, wallet=wallet, amount=amount)
        docnum = _document_number(draft.document_id)
        op, auto_row = await _auto_classified_op(
            session, wallet=wallet, amount=amount, document_number=docnum, quality="final"
        )
        await session.commit()
        op_id, auto_id = op.id, auto_row.id

    async with async_session_factory() as session:
        absorbed = await absorb_auto_classified_counterparty_payment(session)
        await session.commit()
    assert absorbed == 0

    async with async_session_factory() as session:
        op = await session.get(BankOperation, op_id)
        auto_row = await session.get(CashflowTransaction, auto_id)
    assert op.cashflow_transaction_id == auto_id
    assert auto_row is not None

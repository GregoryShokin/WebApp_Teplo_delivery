"""Тест reconcile-прохода: needs_review-операция склеивается с prebooked-проводкой,
появившейся позже неё (гонка вебхук↔поллинг)."""

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
    DdsArticle,
    ReconciliationCase,
    Wallet,
)
from app.services.banking.classifier import reconcile_needs_review_prebooked

OP_DATE = date(2026, 6, 25)


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
        code=f"bank-rec-{uuid.uuid4().hex[:8]}",
        name="Тест банк (reconcile)",
        type="bank",
        status="active",
        account_id=account.id,
        opening_balance=Decimal("0"),
    )
    session.add(wallet)
    await session.flush()
    return wallet


async def _needs_review_op(
    session: AsyncSession, *, account_id: uuid.UUID, amount: Decimal, inn: str | None = None
) -> BankOperation:
    op = BankOperation(
        id=uuid.uuid4(),
        provider="tbank",
        provider_operation_id=f"op-{uuid.uuid4()}",
        account_id=account_id,
        operation_date=OP_DATE,
        direction="out",
        amount=amount,
        counterparty_inn_raw=inn,
        payment_purpose="Оплата поставщику",
        raw_payload={},
        classification_status="needs_review",
        cashflow_transaction_id=None,
    )
    session.add(op)
    await session.flush()
    return op


async def _prebooked(
    session: AsyncSession, *, wallet_id: uuid.UUID, amount: Decimal
) -> CashflowTransaction:
    article_id = await session.scalar(
        select(DdsArticle.id).where(DdsArticle.code == "payment_to_supplier")
    )
    ct = CashflowTransaction(
        wallet_id=wallet_id,
        direction="out",
        amount=amount,
        operation_date=OP_DATE,
        article_id=article_id,
        source_kind="counterparty_payment",
        payment_purpose="Оплата поставщикам",
        quality_status="final",
    )
    session.add(ct)
    await session.flush()
    return ct


async def test_reconcile_links_late_prebooked_and_closes_case(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        wallet = await _bank_wallet(session)
        op = await _needs_review_op(
            session, account_id=wallet.account_id, amount=Decimal("29168.99")
        )
        ct = await _prebooked(session, wallet_id=wallet.id, amount=Decimal("29168.99"))
        session.add(
            ReconciliationCase(
                id=uuid.uuid4(),
                kind="unclassified_operation",
                status="pending",
                provider="tbank",
                bank_operation_id=op.id,
                payload={},
            )
        )
        await session.commit()
        op_id, ct_id = op.id, ct.id

    async with async_session_factory() as session:
        linked = await reconcile_needs_review_prebooked(session)
        await session.commit()
    assert linked == 1

    async with async_session_factory() as session:
        op = await session.get(BankOperation, op_id)
        case = await session.scalar(
            select(ReconciliationCase).where(ReconciliationCase.bank_operation_id == op_id)
        )
    assert op.cashflow_transaction_id == ct_id
    assert op.classification_status == "classified"
    assert case.status == "resolved"


async def test_reconcile_ignores_amount_mismatch(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        wallet = await _bank_wallet(session)
        op = await _needs_review_op(session, account_id=wallet.account_id, amount=Decimal("100.00"))
        await _prebooked(session, wallet_id=wallet.id, amount=Decimal("999.00"))
        await session.commit()
        op_id = op.id

    async with async_session_factory() as session:
        linked = await reconcile_needs_review_prebooked(session)
        await session.commit()
    assert linked == 0

    async with async_session_factory() as session:
        op = await session.get(BankOperation, op_id)
    assert op.cashflow_transaction_id is None
    assert op.classification_status == "needs_review"


async def test_reconcile_skips_mismatched_inn(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Операция с ИНН не должна привязываться к prebooked с ИЗВЕСТНЫМ другим ИНН."""
    async with async_session_factory() as session:
        wallet = await _bank_wallet(session)
        op = await _needs_review_op(
            session, account_id=wallet.account_id, amount=Decimal("500.00"), inn="7707133576"
        )
        # Контрагент с ДРУГИМ ИНН на prebooked-проводке.
        from app.models import Counterparty

        cp = Counterparty(id=uuid.uuid4(), name="Чужой", inn="9999999999", type="legal_entity")
        session.add(cp)
        await session.flush()
        article_id = await session.scalar(
            select(DdsArticle.id).where(DdsArticle.code == "payment_to_supplier")
        )
        session.add(
            CashflowTransaction(
                wallet_id=wallet.id,
                direction="out",
                amount=Decimal("500.00"),
                operation_date=OP_DATE,
                article_id=article_id,
                counterparty_id=cp.id,
                source_kind="counterparty_payment",
                quality_status="final",
            )
        )
        await session.commit()
        op_id = op.id

    async with async_session_factory() as session:
        linked = await reconcile_needs_review_prebooked(session)
        await session.commit()
    assert linked == 0

    async with async_session_factory() as session:
        op = await session.get(BankOperation, op_id)
    assert op.cashflow_transaction_id is None

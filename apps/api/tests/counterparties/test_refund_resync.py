"""Обратный ход возврата переплаты: зачёт следует за переразметкой проводки.

Возврат денег от поставщика гасит его дебиторку БЕЗ аллокации — просто растит
``amount_settled``. Из-за этого зачёт был односторонним: ``refund_counterparty_prepayments``
зовётся только при СОЗДАНИИ прихода, и переразметка проводки в учёте не отражалась. Сняли
возвратную статью — дебиторка оставалась списанной навсегда; поставили её обычному приходу —
зачёт не применялся вовсе. Кейс Лигая 26.07.2026: ошибку в сумме возврата (2882 вместо 2822)
нельзя было исправить из интерфейса, потребовалась правка базы.

``resync_counterparty_refunds`` пересобирает зачёт из фактов: объём возвратов берётся из
проводок контрагента с возвратной статьёй, а ``amount_settled`` сбрасывается до аллокационной
части (гашения накладными хранятся строками) и добирается возвратами по FIFO.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from cp_helpers import make_counterparty, make_expense_article, make_invoice, make_wallet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import CashflowTransaction, DdsArticle, SupplierPrepayment
from app.services.supplier_prepayments import (
    SUPPLIER_REFUND_ARTICLE_CODE,
    create_supplier_prepayment,
    refund_counterparty_prepayments,
    resync_counterparty_refunds,
    settle_invoice_from_prepayment,
)

OP_DATE = date(2026, 7, 20)


async def _refund_article(session: AsyncSession) -> DdsArticle:
    return await make_expense_article(
        session,
        code=SUPPLIER_REFUND_ARTICLE_CODE,
        name="Возврат переплаты от поставщиков",
    )


async def _make_refund_txn(
    session: AsyncSession,
    *,
    wallet_id: uuid.UUID,
    counterparty_id: uuid.UUID,
    article_id: uuid.UUID,
    amount: str,
) -> CashflowTransaction:
    """Приход-возврат, как его заводит ручка «Новый платёж» (source_kind='new_payment_income')."""
    txn = CashflowTransaction(
        wallet_id=wallet_id,
        direction="in",
        amount=Decimal(amount),
        operation_date=OP_DATE,
        article_id=article_id,
        counterparty_id=counterparty_id,
        source_kind="new_payment_income",
        payment_purpose="Возврат переплаты",
        quality_status="final",
    )
    session.add(txn)
    await session.flush()
    return txn


async def _prepayment(
    session: AsyncSession, *, counterparty_id: uuid.UUID, wallet_id: uuid.UUID, amount: str
) -> SupplierPrepayment:
    await make_expense_article(session, code="advance_to_supplier", name="Аванс поставщику")
    return await create_supplier_prepayment(
        session,
        counterparty_id=counterparty_id,
        wallet_id=wallet_id,
        amount=Decimal(amount),
        operation_date=OP_DATE,
    )


async def test_refund_unwinds_when_article_dropped(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Сняли возвратную статью — дебиторка обязана вернуться (раньше оставалась списанной)."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Возврат-1", inn="6155020101")
        wallet = await make_wallet(session, name="Сейф-1")
        article = await _refund_article(session)
        other = await make_expense_article(
            session, code="payment_to_supplier", name="Оплата поставщикам"
        )
        prepayment = await _prepayment(
            session, counterparty_id=cp.id, wallet_id=wallet.id, amount="1000.00"
        )

        txn = await _make_refund_txn(
            session,
            wallet_id=wallet.id,
            counterparty_id=cp.id,
            article_id=article.id,
            amount="400.00",
        )
        await refund_counterparty_prepayments(
            session, counterparty_id=cp.id, amount=Decimal("400.00")
        )
        await session.refresh(prepayment)
        assert prepayment.amount_settled == Decimal("400.00")
        assert prepayment.status == "partially_settled"

        # Переразметка: статья больше не возвратная → зачёт снимается целиком.
        txn.article_id = other.id
        await resync_counterparty_refunds(session, cp.id)
        await session.refresh(prepayment)
        assert prepayment.amount_settled == Decimal("0.00")
        assert prepayment.status == "open"

        # Вернули возвратную статью → зачёт применяется снова (идемпотентно).
        txn.article_id = article.id
        await resync_counterparty_refunds(session, cp.id)
        await resync_counterparty_refunds(session, cp.id)
        await session.refresh(prepayment)
        assert prepayment.amount_settled == Decimal("400.00")
        assert prepayment.status == "partially_settled"


async def test_refund_resync_keeps_invoice_allocations(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Пересборка не трогает гашения накладными: они хранятся аллокациями и остаются как есть."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Возврат-2", inn="6155020202")
        wallet = await make_wallet(session, name="Сейф-2")
        article = await _refund_article(session)
        prepayment = await _prepayment(
            session, counterparty_id=cp.id, wallet_id=wallet.id, amount="1000.00"
        )
        invoice = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="600.00",
            invoice_date=OP_DATE,
            operational_scope="finance",
        )
        await settle_invoice_from_prepayment(
            session, invoice_id=invoice.id, prepayment_id=prepayment.id
        )
        await session.refresh(prepayment)
        assert prepayment.amount_settled == Decimal("600.00")

        await _make_refund_txn(
            session,
            wallet_id=wallet.id,
            counterparty_id=cp.id,
            article_id=article.id,
            amount="250.00",
        )
        await resync_counterparty_refunds(session, cp.id)
        await session.refresh(prepayment)
        # 600 аллокацией + 250 возвратом; аллокационная часть пересборкой не потеряна.
        assert prepayment.amount_settled == Decimal("850.00")
        assert prepayment.status == "partially_settled"


async def test_refund_resync_ignores_excluded_transaction(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Мягко исключённая проводка выпадает из баланса — гасить дебиторку она не вправе."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Возврат-3", inn="6155020303")
        wallet = await make_wallet(session, name="Сейф-3")
        article = await _refund_article(session)
        prepayment = await _prepayment(
            session, counterparty_id=cp.id, wallet_id=wallet.id, amount="500.00"
        )
        txn = await _make_refund_txn(
            session,
            wallet_id=wallet.id,
            counterparty_id=cp.id,
            article_id=article.id,
            amount="200.00",
        )
        await resync_counterparty_refunds(session, cp.id)
        await session.refresh(prepayment)
        assert prepayment.amount_settled == Decimal("200.00")

        txn.quality_status = "excluded"
        await resync_counterparty_refunds(session, cp.id)
        await session.refresh(prepayment)
        assert prepayment.amount_settled == Decimal("0.00")
        assert prepayment.status == "open"


async def test_refund_resync_moves_with_counterparty(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Проводку перевесили на другого поставщика — зачёт уходит вместе с ней."""
    async with async_session_factory() as session:
        donor = await make_counterparty(session, name="Возврат-донор", inn="6155020404")
        target = await make_counterparty(session, name="Возврат-получатель", inn="6155020505")
        wallet = await make_wallet(session, name="Сейф-4")
        article = await _refund_article(session)
        donor_prepayment = await _prepayment(
            session, counterparty_id=donor.id, wallet_id=wallet.id, amount="700.00"
        )
        target_prepayment = await _prepayment(
            session, counterparty_id=target.id, wallet_id=wallet.id, amount="700.00"
        )
        txn = await _make_refund_txn(
            session,
            wallet_id=wallet.id,
            counterparty_id=donor.id,
            article_id=article.id,
            amount="300.00",
        )
        await resync_counterparty_refunds(session, donor.id)
        await session.refresh(donor_prepayment)
        assert donor_prepayment.amount_settled == Decimal("300.00")

        txn.counterparty_id = target.id
        await resync_counterparty_refunds(session, donor.id)
        await resync_counterparty_refunds(session, target.id)
        await session.refresh(donor_prepayment)
        await session.refresh(target_prepayment)
        assert donor_prepayment.amount_settled == Decimal("0.00")
        assert target_prepayment.amount_settled == Decimal("300.00")


async def test_refund_resync_skips_prepaid_bill_receivable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ДЗ по оплаченному счёту возврат не гасит — её settled приходит только от закрывающих."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Возврат-5", inn="6155020606")
        wallet = await make_wallet(session, name="Сейф-5")
        article = await _refund_article(session)
        bill_receivable = SupplierPrepayment(
            counterparty_id=cp.id,
            kind="prepaid_bill",
            amount=Decimal("900.00"),
            amount_settled=Decimal("0.00"),
            status="open",
        )
        session.add(bill_receivable)
        await session.flush()

        await _make_refund_txn(
            session,
            wallet_id=wallet.id,
            counterparty_id=cp.id,
            article_id=article.id,
            amount="400.00",
        )
        await resync_counterparty_refunds(session, cp.id)
        await session.refresh(bill_receivable)
        assert bill_receivable.amount_settled == Decimal("0.00")
        assert bill_receivable.status == "open"

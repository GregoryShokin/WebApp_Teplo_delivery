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
from sqlalchemy import select
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


async def _incoming_bank_operation(session: AsyncSession, *, amount: str):
    """Входящая операция выписки на банковском счёте — так приходит возврат от поставщика."""
    from cp_helpers import make_account, make_bank_operation

    account = await make_account(session)
    await make_wallet(session, name="Р/с возвратов", wallet_type="bank", account_id=account.id)
    return await make_bank_operation(
        session, amount=amount, direction="in", account_id=account.id, operation_date=OP_DATE
    )


async def test_refund_from_bank_statement_settles_the_advance(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Возврат, размеченный в разборе выписки, гасит аванс — и исключение операции это снимает.

    Кейс ИП Скачковой (22.09.2026): возврат 10 112,13 ₽ пришёл выпиской и был размечен статьёй
    возврата через разбор операции. Эта дверь зачёт не делала — его заводили только «Новый
    платёж» и ручная переразметка проводки, — и товарный аванс с тем же остатком висел открытой
    дебиторкой, хотя деньги уже вернулись.
    """
    from app.services.banking.classifier import apply_operation_action

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Возврат-выпиской", inn="6155020606")
        wallet = await make_wallet(session, name="Сейф-6")
        article = await _refund_article(session)
        prepayment = await _prepayment(
            session, counterparty_id=cp.id, wallet_id=wallet.id, amount="1000.00"
        )
        operation = await _incoming_bank_operation(session, amount="1000.00")
        await session.commit()

        await apply_operation_action(
            session,
            operation,
            action="set_article",
            article_id=article.id,
            counterparty_id=cp.id,
            quality_status="owner_review",
        )
        await session.commit()
        await session.refresh(prepayment)
        assert prepayment.amount_settled == Decimal("1000.00")
        assert prepayment.status == "refunded"

        await apply_operation_action(session, operation, action="exclude")
        await session.commit()
        await session.refresh(prepayment)
        assert prepayment.amount_settled == Decimal("0.00")
        assert prepayment.status == "open"


async def test_refund_share_of_a_split_bank_operation_settles_the_advance(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Сплит операции выписки с возвратной долей гасит аванс на сумму этой доли."""
    from app.services.banking.classifier import OperationSplitLine, apply_operation_split

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Возврат-долей", inn="6155020707")
        wallet = await make_wallet(session, name="Сейф-7")
        article = await _refund_article(session)
        other = await make_expense_article(session, code="prochie_dohody", name="Прочие доходы")
        prepayment = await _prepayment(
            session, counterparty_id=cp.id, wallet_id=wallet.id, amount="1000.00"
        )
        operation = await _incoming_bank_operation(session, amount="900.00")
        await session.commit()

        await apply_operation_split(
            session,
            operation,
            splits=[
                OperationSplitLine(article.id, Decimal("600.00")),
                OperationSplitLine(other.id, Decimal("300.00")),
            ],
            counterparty_id=cp.id,
        )
        await session.commit()
        await session.refresh(prepayment)
        assert prepayment.amount_settled == Decimal("600.00")
        assert prepayment.status == "partially_settled"


async def test_excluding_a_manual_refund_returns_the_receivable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Исключили ручной приход-возврат — зачёт аванса уходит вместе с ним."""
    from app.services.banking.cashflow_classify import apply_cashflow_exclude

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Возврат-исключён", inn="6155020808")
        wallet = await make_wallet(session, name="Сейф-8")
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
        await refund_counterparty_prepayments(
            session, counterparty_id=cp.id, amount=Decimal("200.00")
        )
        await session.commit()

        await apply_cashflow_exclude(session, txn)
        await session.commit()
        await session.refresh(prepayment)
        assert prepayment.amount_settled == Decimal("0.00")
        assert prepayment.status == "open"


async def test_refund_resync_keeps_a_prepayment_written_off_by_decision(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Аванс, закрытый решением человека без строки гашения, пересборка возвратов не открывает.

    ``writeoff_pre_accounting`` закрывает исторический остаток прямым ``amount_settled`` и
    ставит ``settled_on``. Сброс к аллокациям воскресил бы списанную дебиторку (38 479 ₽
    «Поставки овощей»), а возврат зачёлся бы в неё, а не в живой аванс.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Возврат-списание", inn="6155020909")
        wallet = await make_wallet(session, name="Сейф-9")
        article = await _refund_article(session)
        written_off = await _prepayment(
            session, counterparty_id=cp.id, wallet_id=wallet.id, amount="700.00"
        )
        written_off.amount_settled = Decimal("700.00")
        written_off.status = "settled"
        written_off.settled_on = date(2026, 7, 20)
        live = await _prepayment(
            session, counterparty_id=cp.id, wallet_id=wallet.id, amount="300.00"
        )
        await _make_refund_txn(
            session,
            wallet_id=wallet.id,
            counterparty_id=cp.id,
            article_id=article.id,
            amount="300.00",
        )
        await resync_counterparty_refunds(session, cp.id)
        await session.refresh(written_off)
        await session.refresh(live)
        assert (written_off.amount_settled, written_off.status) == (Decimal("700.00"), "settled")
        assert (live.amount_settled, live.status) == (Decimal("300.00"), "refunded")


async def test_barter_loan_money_is_not_an_overpayment_refund(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Деньги за наш товарный заём идут той же возвратной статьёй, но аванс не гасят.

    ``barter_loan_money`` гасит заём (``BarterReturnLine``), а не предоплату поставщику. Пока
    пересборка возвратов считала их возвратом, первый же разбор настоящего возврата в выписке
    списывал открытый аванс партнёра на всю сумму бартерных денег — 1 000 вместо 100.
    """
    from datetime import UTC, datetime

    from cp_helpers import make_account, make_bank_operation

    from app.models import IikoProduct, InvoiceLineItem
    from app.services.banking.classifier import apply_operation_action
    from app.services.barter_loan_money import MoneyReturnLine, settle_receivable_loan_with_money
    from app.services.warehouse_invoices import LineInput, create_warehouse_invoice

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Бартер-деньгами", inn="6155021010")
        refund = await _refund_article(session)
        safe = await make_wallet(session, name="Сейф-10", wallet_type="cash_safe")
        product = IikoProduct(
            iiko_id=str(uuid.uuid4()),
            name="Моцарелла",
            type="GOODS",
            unit="кг",
            synced_at=datetime.now(UTC),
        )
        session.add(product)
        await session.flush()
        advance = await _prepayment(
            session, counterparty_id=cp.id, wallet_id=safe.id, amount="1000.00"
        )
        loan = await create_warehouse_invoice(
            session,
            counterparty_id=cp.id,
            issued_at=datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
            mode="loan",
            we_lend=True,
            lines=[
                LineInput(
                    name="Моцарелла",
                    quantity=Decimal("10"),
                    price=Decimal("300"),
                    iiko_product_id=product.id,
                )
            ],
        )
        line = (
            await session.scalars(
                select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == loan.id)
            )
        ).one()
        await settle_receivable_loan_with_money(
            session,
            loan_id=loan.id,
            operation_date=OP_DATE,
            lines=[
                MoneyReturnLine(
                    loan_line_item_id=line.id, quantity=Decimal("4"), unit_price=Decimal("350")
                )
            ],
            wallet_id=safe.id,
        )
        account = await make_account(session)
        await make_wallet(session, name="Р/с-10", wallet_type="bank", account_id=account.id)
        operation = await make_bank_operation(
            session, amount="100.00", direction="in", account_id=account.id, operation_date=OP_DATE
        )
        await session.commit()

        await apply_operation_action(
            session,
            operation,
            action="set_article",
            article_id=refund.id,
            counterparty_id=cp.id,
            quality_status="owner_review",
        )
        await session.commit()
        await session.refresh(advance)
        assert advance.amount_settled == Decimal("100.00")
        assert advance.status == "partially_settled"

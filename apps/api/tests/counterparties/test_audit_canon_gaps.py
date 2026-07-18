"""АУДИТ-4, вторая волна: проверка заявок adversarial-workflow по канону ДЗ/КЗ.

Каждый тест — эмпирическая проверка ОДНОЙ заявки. Красный тест = заявка подтверждена.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from cp_helpers import make_bank_operation, make_counterparty, make_invoice, make_wallet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import CashflowTransaction, SupplierInvoice, SupplierPrepayment
from app.services.supplier_prepayments import ensure_prepayment_from_bank_transaction


async def _receivable(session: AsyncSession, counterparty_id: uuid.UUID) -> Decimal:
    rows = (
        await session.scalars(
            select(SupplierPrepayment).where(
                SupplierPrepayment.counterparty_id == counterparty_id,
                SupplierPrepayment.status.in_(("open", "partially_settled")),
            )
        )
    ).all()
    return sum(
        (Decimal(str(r.amount)) - Decimal(str(r.amount_settled)) for r in rows), Decimal("0.00")
    )


async def _payable(session: AsyncSession, counterparty_id: uuid.UUID) -> Decimal:
    """КЗ так, как её считает плитка: неоплаченный остаток активных закрывающих."""
    from app.services.counterparty_matching import _invoice_remaining

    rows = (
        await session.scalars(
            select(SupplierInvoice).where(
                SupplierInvoice.counterparty_id == counterparty_id,
                SupplierInvoice.direction == "payable",
                SupplierInvoice.doc_kind == "closing",
                SupplierInvoice.activation_status == "active",
                SupplierInvoice.payment_status.in_(("unpaid", "partially_paid")),
            )
        )
    ).all()
    total = Decimal("0.00")
    for inv in rows:
        total += max(await _invoice_remaining(session, inv), Decimal("0.00"))
    return total


async def _paid_bank_tx(
    session: AsyncSession, *, counterparty_id: uuid.UUID, amount: str, inn: str
) -> CashflowTransaction:
    wallet = await make_wallet(session, name=f"Б-{inn}", wallet_type="bank")
    tx = CashflowTransaction(
        wallet_id=wallet.id,
        direction="out",
        amount=Decimal(amount),
        operation_date=date(2026, 7, 10),
        counterparty_id=counterparty_id,
        source_kind="bank_operation",
        payment_purpose="оплата поставщику",
        quality_status="auto",
    )
    session.add(tx)
    await session.flush()
    return tx


async def test_void_bill_receivable_follows_money_not_status(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ДЗ по счёту следует за ДЕНЬГАМИ, а не за статусом счёта. Два края:
    (а) счёт аннулировали, но деньги реально уходили и оплату не снимали — ДЗ ОСТАЁТСЯ
        (деньги у поставщика, закрывающего нет — канонная предоплата);
    (б) после аннулирования оплату сняли (аллокации удалены) — чокпоинт обязан прибрать ДЗ.
        Ранний return на void ДО чокпоинта замораживал её навсегда."""
    from cp_helpers import make_expense_article

    from app.models import InvoicePaymentAllocation
    from app.services.counterparty_matching import _recompute_status
    from app.services.counterparty_payments import pay_invoice_from_wallet

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Void-счёт", inn="6155991001")
        bill = await make_invoice(
            session, counterparty_id=cp.id, amount="300.00", doc_kind="bill",
            number="СЧ-void", invoice_date=date(2026, 7, 1),
        )
        wallet = await make_wallet(session, name="Касса-void", wallet_type="cash_safe")
        article = await make_expense_article(session)
        await session.commit()
        await pay_invoice_from_wallet(
            session, invoice_id=bill.id, wallet_id=wallet.id, amount=Decimal("300.00"),
            operation_date=date(2026, 7, 2), article_id=article.id,
        )
        assert await _receivable(session, cp.id) == Decimal("300.00")

        # (а) Аннулировали счёт, оплата на месте: деньги у поставщика → ДЗ остаётся.
        row = await session.get(SupplierInvoice, bill.id)
        row.payment_status = "void"
        await session.flush()
        await _recompute_status(session, row)
        await session.commit()
        total = await _receivable(session, cp.id)
        assert total == Decimal("300.00"), (
            f"ДЗ {total}: аннулирование счёта не должно стирать дебиторку за реально ушедшие деньги"
        )

        # (б) Оплату сняли (пере-разбор удалил аллокации) — чокпоинт прибирает ДЗ и у void-счёта.
        allocs = (
            await session.scalars(
                select(InvoicePaymentAllocation).where(
                    InvoicePaymentAllocation.invoice_id == bill.id
                )
            )
        ).all()
        for alloc in allocs:
            await session.delete(alloc)
        await session.flush()
        await _recompute_status(session, row)
        await session.commit()
        total = await _receivable(session, cp.id)
        assert total == Decimal("0.00"), (
            f"ДЗ {total} по void-счёту без единой оплатной аллокации — фантом заморожен навсегда"
        )


async def test_pending_future_closing_not_matchable_by_bank(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Заявка: будущий УПД (activation_status='pending') доступен банковской сверке —
    ``counterparty_bank_match`` не фильтрует activation_status (правило 4 нарушено)."""
    from app.services.counterparty_bank_match import suggest_invoice_matches

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Будущий-УПД", inn="6155991002")
        await make_invoice(
            session, counterparty_id=cp.id, amount="900.00", doc_kind="closing",
            activation_status="pending", number="УПД-31.07", invoice_date=date(2026, 7, 31),
        )
        await make_bank_operation(
            session, amount="900.00", direction="out", inn="6155991002",
            provider="tbank", operation_date=date(2026, 7, 10),
        )
        await session.commit()

        suggestions = await suggest_invoice_matches(session, counterparty_id=cp.id)
        offered = [s for s in suggestions if s.candidates]
        assert not offered, (
            "Будущий (pending) УПД предложен банковской сверке как кандидат — правило 4 нарушено: "
            f"{[(s.invoice_number, len(s.candidates)) for s in offered]}"
        )


# NB: тест «счёт не должен предлагаться сверке» УДАЛЁН осознанно. После фикса «бюджета
# платежа» привязка операции к счёту канонически безопасна: носитель ДЗ ровно один
# (rule-1-предоплата ИЛИ prepaid_bill, но не обе), задвоения нет — это штатный путь оплаты
# счёта постфактум. Покрыто test_audit_double_receivable.


async def test_iiko_sync_closing_settles_open_receivable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Правило 2 для канала iiko (реальная дверь — ``ingest_iiko_payables``): товарник оплачен
    авансом, приходная приходит синком → закрывающий гасит дебиторку, а не висит параллельно."""
    from cp_helpers import cloud_invoice_docs, suppliers_xml

    from app.services.counterparty_invoice_sync import ingest_iiko_payables

    guid = "guid-audit-rule2"
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Товарник-iiko", inn="6155991004", iiko_guid=guid
        )
        tx = await _paid_bank_tx(
            session, counterparty_id=cp.id, amount="1000.00", inn="6155991004"
        )
        pre = await ensure_prepayment_from_bank_transaction(session, tx)
        await session.commit()
        assert pre is not None and pre.amount == Decimal("1000.00")

        await ingest_iiko_payables(
            session,
            suppliers_xml=suppliers_xml(
                [{"id": guid, "name": "Товарник-iiko", "inn": "6155991004"}]
            ),
            incoming_docs=cloud_invoice_docs(
                [
                    {
                        "id": "doc-audit-rule2",
                        "supplier": guid,
                        "status": "PROCESSED",
                        "document_number": "ПН-iiko",
                        "incoming_date": "2026-07-12",
                        "items": [{"sum": "1000.00"}],
                    }
                ]
            ),
        )
        await session.commit()

        dz = await _receivable(session, cp.id)
        kz = await _payable(session, cp.id)
        assert (dz, kz) == (Decimal("0.00"), Decimal("0.00")), (
            f"ДЗ {dz} и КЗ {kz} висят параллельно на одну поставку — правило 2 не отработало "
            "для канала iiko (apply_closing_document не вызван в синке)"
        )


async def test_warehouse_invoice_settles_open_receivable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Правило 2 для канала «склад» (реальная дверь — ``create_warehouse_invoice``): аванс
    1000, складская приходная на 750 → зачёт, ДЗ 250, КЗ 0."""
    from cp_helpers import make_expense_article

    from app.models import IikoProduct
    from app.services.warehouse_invoices import LineInput, create_warehouse_invoice

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Товарник-склад", inn="6155991005")
        await make_expense_article(session, code="payment_to_supplier", name="Оплата поставщикам")
        product = IikoProduct(
            iiko_id=str(uuid.uuid4()),
            name="Лосось-аудит",
            type="GOODS",
            unit="кг",
            synced_at=datetime.now(UTC),
        )
        session.add(product)
        await session.flush()
        tx = await _paid_bank_tx(
            session, counterparty_id=cp.id, amount="1000.00", inn="6155991005"
        )
        pre = await ensure_prepayment_from_bank_transaction(session, tx)
        await session.commit()
        assert pre is not None and pre.amount == Decimal("1000.00")

        await create_warehouse_invoice(
            session,
            counterparty_id=cp.id,
            issued_at=datetime(2026, 7, 12, 10, 0, tzinfo=UTC),
            lines=[
                LineInput(
                    name="Лосось-аудит",
                    quantity=Decimal("1"),
                    price=Decimal("750"),
                    iiko_product_id=product.id,
                )
            ],
        )

        dz = await _receivable(session, cp.id)
        kz = await _payable(session, cp.id)
        assert (dz, kz) == (Decimal("250.00"), Decimal("0.00")), (
            f"ДЗ {dz} / КЗ {kz}: складская приходная 750 обязана зачесть аванс 1000 → ДЗ 250, КЗ 0"
        )

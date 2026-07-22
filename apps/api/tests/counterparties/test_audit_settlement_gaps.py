"""АУДИТ-4, четвёртая волна: два независимых корня с риском ДВОЙНОЙ ОПЛАТЫ.

A. Авто-гашение закрывающих не исключает документы, замороженные в банк-черновике
   (``draft_id IS NOT NULL``): обязательство гасится зачётом из предоплаты, хотя платёж по
   нему уже ушёл в банк → поставщику платят второй раз.
B. Исключение операции («Исключить» / «Внутренний перевод») снимает только cash-зачёты
   правила 1, но не bank-аллокации той же операции: накладная остаётся «оплаченной»
   операцией, которой больше нет в учёте.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from cp_helpers import (
    make_bank_operation,
    make_counterparty,
    make_draft,
    make_invoice,
    make_wallet,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    CashflowTransaction,
    InvoicePaymentAllocation,
    SupplierInvoice,
    SupplierPrepayment,
)


async def test_closing_frozen_in_bank_draft_not_settled_from_prepayment(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A. Закрывающий отправлен в банк (draft_id) — деньги в пути. Появившаяся дебиторка НЕ
    должна гасить его зачётом: иначе документ «закрыт» дважды — и зачётом, и реальным
    платежом, который вот-вот исполнится."""
    from app.services.supplier_prepayments import auto_settle_invoice_from_open_prepayments

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Заморозка-зачёт", inn="6155993001")
        upd = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="1000.00",
            doc_kind="closing",
            number="УПД-в-банке",
            invoice_date=date(2026, 7, 1),
            operational_scope="finance",
        )
        draft = await make_draft(session, counterparty_id=cp.id, amount="1000.00")
        upd.draft_id = draft.id  # отправлен в банк, платёж в пути
        session.add(
            SupplierPrepayment(
                counterparty_id=cp.id,
                kind="subscription",
                amount=Decimal("1000.00"),
                amount_settled=Decimal("0.00"),
                status="open",
                note="аванс",
            )
        )
        await session.flush()

        settled = await auto_settle_invoice_from_open_prepayments(session, upd)
        await session.commit()

        assert settled == Decimal("0.00"), (
            f"Закрывающий, отправленный в банк, погашен зачётом на {settled} — при исполнении "
            "черновика поставщик получит оплату ВТОРОЙ раз"
        )


async def test_excluded_operation_releases_its_invoice_allocation(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """B. Оператор исключил банк-операцию из учёта («Исключить» — реальная дверь
    ``apply_operation_action``). Её bank-аллокация на накладной обязана сняться — иначе
    накладная числится оплаченной операцией, которой в учёте нет, а по счёту ещё и висит ДЗ
    без единого рубля за ней."""
    from app.services.banking.classifier import apply_operation_action
    from app.services.counterparty_bank_match import confirm_invoice_match

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Исключение-аллокация", inn="6155993002")
        bill = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="900.00",
            doc_kind="bill",
            number="СЧ-исключение",
            invoice_date=date(2026, 7, 1),
        )
        wallet = await make_wallet(session, name="Банк-искл", wallet_type="bank")
        operation = await make_bank_operation(
            session,
            amount="900.00",
            direction="out",
            inn="6155993002",
            operation_date=date(2026, 7, 10),
            classification_status="classified",
        )
        tx = CashflowTransaction(
            wallet_id=wallet.id,
            direction="out",
            amount=Decimal("900.00"),
            operation_date=date(2026, 7, 10),
            counterparty_id=cp.id,
            source_kind="bank_operation",
            source_id=operation.id,
            payment_purpose="оплата счёта",
            quality_status="auto",
        )
        session.add(tx)
        await session.flush()
        operation.cashflow_transaction_id = tx.id
        await session.commit()

        await confirm_invoice_match(
            session,
            invoice_id=bill.id,
            bank_operation_id=operation.id,
            enrich=False,
            actor_user_id=None,
        )
        assert (await session.get(SupplierInvoice, bill.id)).payment_status == "paid"

        # Оператор: «эта операция — не расход, исключить».
        await apply_operation_action(session, operation, action="exclude")
        await session.commit()

        invoice = await session.get(SupplierInvoice, bill.id)
        allocs = await session.scalar(
            select(func.count())
            .select_from(InvoicePaymentAllocation)
            .where(InvoicePaymentAllocation.bank_operation_id == operation.id)
        )
        receivable = await session.scalar(
            select(func.coalesce(func.sum(SupplierPrepayment.amount), 0)).where(
                SupplierPrepayment.counterparty_id == cp.id,
                SupplierPrepayment.status.in_(("open", "partially_settled")),
            )
        )
        assert (invoice.payment_status, allocs, Decimal(str(receivable))) == (
            "unpaid",
            0,
            Decimal("0.00"),
        ), (
            f"После исключения операции: счёт={invoice.payment_status}, аллокаций={allocs}, "
            f"ДЗ={receivable} — деньги из учёта убрали, а их следы остались"
        )

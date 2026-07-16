"""Гашение накладной из выданной предоплаты (дебиторка): денег не двигает, статус-гард.

settle_invoice_from_prepayment списывает остаток предоплаты против payable-накладной без
движения денег. Закрытую/возвращённую предоплату гасить нельзя — иначе списали бы остаток
уже не существующей дебиторки.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from cp_helpers import make_counterparty, make_invoice
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import SupplierPrepayment
from app.services.counterparty_payments import CounterpartyPaymentError
from app.services.supplier_prepayments import settle_invoice_from_prepayment


async def _prepayment(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    status: str,
    amount: str = "1000.00",
    settled: str = "0.00",
) -> SupplierPrepayment:
    prepayment = SupplierPrepayment(
        counterparty_id=counterparty_id,
        kind="goods",
        amount=Decimal(amount),
        amount_settled=Decimal(settled),
        status=status,
    )
    session.add(prepayment)
    await session.flush()
    return prepayment


@pytest.mark.parametrize("status", ["refunded", "settled"])
async def test_settle_rejects_non_open_prepayment(
    async_session_factory: async_sessionmaker[AsyncSession], status: str
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name=f"Поставщик-{status}")
        pre = await _prepayment(session, counterparty_id=cp.id, status=status)
        inv = await make_invoice(session, counterparty_id=cp.id, amount="1000.00")
        await session.commit()

        with pytest.raises(CounterpartyPaymentError, match="недоступна"):
            await settle_invoice_from_prepayment(
                session, invoice_id=inv.id, prepayment_id=pre.id
            )


async def test_settle_open_prepayment_allocates(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик-open")
        pre = await _prepayment(session, counterparty_id=cp.id, status="open")
        inv = await make_invoice(session, counterparty_id=cp.id, amount="600.00")
        await session.commit()

        result = await settle_invoice_from_prepayment(
            session, invoice_id=inv.id, prepayment_id=pre.id
        )
        assert result.payment_status == "paid"
        await session.refresh(pre)
        assert pre.amount_settled == Decimal("600.00")
        assert pre.status == "partially_settled"


async def test_opening_prepayment_no_cashflow_and_upd_settling_keeps_running_balance(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Баланс «денег у поставщика» = Σ пополнений − Σ реализаций (формула владельца):
    остаток 5к + оплата 38к − закрывающий УПД 33к = 10к. Начальный остаток денег не двигает."""
    from sqlalchemy import func as sa_func
    from sqlalchemy import select

    from app.models import CashflowTransaction
    from app.services.supplier_prepayments import (
        auto_settle_invoice_from_open_prepayments,
        create_opening_prepayment,
    )

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Синапсис", inn="3525357535")
        # Начальный остаток кабинета: 5 000 (историческая переплата) — без ДДС-проводки.
        cashflows_before = await session.scalar(
            select(sa_func.count()).select_from(CashflowTransaction)
        )
        opening = await create_opening_prepayment(
            session, counterparty_id=cp.id, amount=Decimal("5000.00"), kind="ad"
        )
        cashflows_after = await session.scalar(
            select(sa_func.count()).select_from(CashflowTransaction)
        )
        assert cashflows_after == cashflows_before  # денег не двигали
        assert opening.wallet_id is None and opening.cashflow_transaction_id is None

        # Новая оплата 38 000 (упрощённо тоже как открытая предоплата в дебиторке).
        top_up = SupplierPrepayment(
            counterparty_id=cp.id,
            kind="ad",
            amount=Decimal("38000.00"),
            amount_settled=Decimal("0.00"),
            status="open",
        )
        session.add(top_up)
        await session.flush()

        # Закрывающий УПД на 33 000 гасит дебиторку (FIFO: сначала начальный остаток).
        invoice = await make_invoice(
            session, counterparty_id=cp.id, amount="33000.00", number="УПД-333", source="sbis"
        )
        settled = await auto_settle_invoice_from_open_prepayments(session, invoice)
        await session.commit()
        assert settled == Decimal("33000.00")

        await session.refresh(opening)
        await session.refresh(top_up)
        balance = (opening.amount - opening.amount_settled) + (
            top_up.amount - top_up.amount_settled
        )
        assert balance == Decimal("10000.00")  # 5к + 38к − 33к
        assert opening.status == "settled"  # начальный остаток съеден целиком
        assert top_up.status == "partially_settled"
        await session.refresh(invoice)
        assert invoice.payment_status == "paid"  # УПД закрыт дебиторкой, к оплате не попал


async def test_bank_debit_tops_up_prepayment_when_profile_flag_on(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Предоплатная модель по банк-фиду (кейс Манго): исходящее списание в пользу
    контрагента с флагом → открытая предоплата, привязанная к транзакции (без новой
    ДДС-проводки). Идемпотентно; без флага — ничего."""
    from datetime import date as date_cls

    from cp_helpers import make_counterparty, make_wallet
    from sqlalchemy import func, select

    from app.models import CashflowTransaction, CounterpartyPayableProfile
    from app.services.supplier_prepayments import ensure_prepayment_from_bank_transaction

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Манго Телеком", inn="7709501144")
        profile = (
            await session.execute(
                select(CounterpartyPayableProfile).where(
                    CounterpartyPayableProfile.counterparty_id == cp.id
                )
            )
        ).scalar_one()
        wallet = await make_wallet(session, name="T-Bank", wallet_type="bank")
        transaction = CashflowTransaction(
            wallet_id=wallet.id,
            direction="out",
            amount=Decimal("4900.00"),
            operation_date=date_cls(2026, 7, 15),
            counterparty_id=cp.id,
            source_kind="bank_operation",
            payment_purpose="MANGO OFFICE оплата услуг связи",
            quality_status="auto",
        )
        session.add(transaction)
        await session.flush()

        # Флаг выключен — предоплата не создаётся.
        assert await ensure_prepayment_from_bank_transaction(session, transaction) is None

        profile.bank_payments_create_prepayment = True
        await session.flush()
        prepayment = await ensure_prepayment_from_bank_transaction(session, transaction)
        assert prepayment is not None
        assert prepayment.amount == Decimal("4900.00")
        assert prepayment.cashflow_transaction_id == transaction.id
        assert prepayment.status == "open"

        # Повторный вызов (повторная классификация) — та же запись, не дубль.
        again = await ensure_prepayment_from_bank_transaction(session, transaction)
        assert again is not None and again.id == prepayment.id
        count = await session.scalar(
            select(func.count()).select_from(SupplierPrepayment)
        )
        assert count == 1

"""Пакет «счёт + УПД» одним письмом: два реальных контрагента, две разные хронологии.

ООО «Назад в будущее» (STARTER) присылает 02.07 комплект ЗА ИЮНЬ: счёт и УПД от 30.06 — услуга
оказана, месяц закрыт. ООО «ЭкоЦентр» (региональный оператор) присылает 15.07 комплект ЗА ИЮЛЬ:
счёт и УПД датированы 31.07, то есть месяц ещё идёт, а документы уже на руках.

Разница принципиальная. У первого расход и долг возникают сразу — услуга позади. У второго
принимать УПД сразу нельзя: он бы закрыл дебиторку и признал расход за месяц, который ещё не
кончился. Правило 4 канона держит такой документ в ``pending`` до его собственной даты, и в свою
дату его активирует ночная джоба.

Проверено на проде 01.08.2026: оба письма пришли, счета разобраны, а УПД тем же письмом были
ОТБРОШЕНЫ — до 16.07 код игнорировал ``upd``/``act`` как «не счёт на оплату», и закрывающие
заводили руками. Сегодняшний код их принимает; эти тесты фиксируют, что именно из этого выходит.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from cp_helpers import make_counterparty, make_invoice, make_wallet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    CashflowTransaction,
    InvoicePaymentAllocation,
    SupplierExpenseAccrual,
    SupplierInvoice,
    SupplierPrepayment,
)
from app.services import supplier_prepayments as prepayments
from app.services import supplier_service_periods as periods

pytestmark = pytest.mark.usefixtures("migrated_db")


async def _pay_bill(
    session: AsyncSession, *, bill: SupplierInvoice, on: date, wallet_name: str
) -> CashflowTransaction:
    """Оплата счёта: деньги ушли, по канону возникает ДЗ (prepaid_bill)."""
    wallet = await make_wallet(session, name=wallet_name)
    tx = CashflowTransaction(
        wallet_id=wallet.id,
        direction="out",
        amount=bill.amount,
        operation_date=on,
        counterparty_id=bill.counterparty_id,
        source_kind="bank_feed",
    )
    session.add(tx)
    await session.flush()
    session.add(
        InvoicePaymentAllocation(
            invoice_id=bill.id,
            source_kind="bank",
            cashflow_transaction_id=tx.id,
            amount=bill.amount,
        )
    )
    bill.payment_status = "paid"
    await session.flush()
    # Единый чокпоинт канона: ДЗ по оплаченному счёту заводится здесь, какой бы дверью ни
    # прошла оплата.
    await prepayments.reconcile_bill_prepayment(session, bill)
    await session.flush()
    return tx


async def _payable(session: AsyncSession, counterparty_id: uuid.UUID) -> Decimal:
    """Кредиторка: неоплаченный остаток действующих закрывающих документов."""
    rows = (
        await session.scalars(
            select(SupplierInvoice).where(
                SupplierInvoice.counterparty_id == counterparty_id,
                SupplierInvoice.doc_kind == "closing",
                SupplierInvoice.payment_status != "void",
            )
        )
    ).all()
    total = Decimal("0.00")
    for invoice in rows:
        if invoice.activation_status != "active":
            continue
        allocated = await session.scalar(
            select(InvoicePaymentAllocation.amount).where(
                InvoicePaymentAllocation.invoice_id == invoice.id
            )
        ) or Decimal("0.00")
        total += max(periods.money(invoice.amount) - periods.money(allocated), Decimal("0.00"))
    return total


async def test_closing_for_finished_month_lands_in_its_own_month(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«Назад в будущее»: комплект за ИЮНЬ пришёл в июле — расход остаётся июньским.

    Услуга оказана, месяц закрыт: документ действует сразу, а признание идёт в свой период,
    а не в месяц получения бумаги. Иначе июньские 80 455 ₽ утяжелили бы прибыль июля.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ООО «Назад в будущее»", inn="7839494297")
        bill = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="80455.00",
            doc_kind="bill",
            number="0000-002629",
            invoice_date=date(2026, 6, 30),
            operational_scope="finance",
        )
        closing = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="80455.00",
            doc_kind="closing",
            number="УПД-002629",
            invoice_date=date(2026, 6, 30),
            operational_scope="finance",
        )
        await session.commit()

        # Документ за прошедший месяц действует сразу — ждать нечего.
        await prepayments.apply_closing_document(session, closing, as_of=date(2026, 7, 2))
        await session.commit()
        assert closing.activation_status == "active"
        assert await _payable(session, cp.id) == Decimal("80455.00")

        # Период услуги — июнь: признание попадает в июнь, хотя бумага пришла в июле.
        await periods.set_invoice_service_period(
            session,
            invoice=closing,
            start=date(2026, 6, 1),
            end=date(2026, 6, 30),
            actor_user_id=None,
        )
        await periods.recognize_due_expenses(session, as_of=date(2026, 7, 2), commit=True)
        accrual = await session.scalar(
            select(SupplierExpenseAccrual).where(SupplierExpenseAccrual.invoice_id == closing.id)
        )
        assert accrual.status == "recognized"
        assert accrual.recognition_month == date(2026, 6, 1)

        # Счёт расхода не признаёт: одна услуга — одно признание.
        bill_accrual = await session.scalar(
            select(SupplierExpenseAccrual).where(SupplierExpenseAccrual.invoice_id == bill.id)
        )
        assert bill_accrual is None


async def test_closing_for_running_month_waits_for_its_date(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«ЭкоЦентр»: комплект за ИЮЛЬ пришёл 15 июля — УПД ждёт 31-го, дебиторку не гасит.

    Это и есть правило 4 канона: пока месяц идёт, документ на руках долгом не является. Оплатили
    счёт 20 июля — деньги встали дебиторкой; 31 июля документ вступает в силу, гасит её и
    становится расходом июля.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name='ООО "ЭкоЦентр"', inn="3444177534")
        bill = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="3348.22",
            doc_kind="bill",
            number="ВД-46890",
            invoice_date=date(2026, 7, 31),
            operational_scope="finance",
        )
        closing = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="3348.22",
            doc_kind="closing",
            number="ВД-46890-УПД",
            invoice_date=date(2026, 7, 31),
            operational_scope="finance",
        )
        await session.commit()

        # 15 июля: документ на руках, но месяц идёт — в долг он не встаёт.
        await prepayments.apply_closing_document(session, closing, as_of=date(2026, 7, 15))
        await session.commit()
        assert closing.activation_status == "pending"
        assert await _payable(session, cp.id) == Decimal("0.00")

        # 20 июля оплатили счёт: деньги ушли, документа в силе нет — это дебиторка.
        await _pay_bill(session, bill=bill, on=date(2026, 7, 20), wallet_name="ЭкоЦентр Банк")
        await session.commit()
        receivable = await session.scalar(
            select(SupplierPrepayment).where(
                SupplierPrepayment.counterparty_id == cp.id,
                SupplierPrepayment.kind == "prepaid_bill",
            )
        )
        assert receivable is not None
        assert receivable.amount == Decimal("3348.22")
        assert receivable.amount_settled == Decimal("0.00")

        # 31 июля документ вступает в силу — его активирует ночная джоба.
        await prepayments.activate_due_closing_invoices(session, as_of=date(2026, 7, 31))
        await session.commit()
        await session.refresh(closing)
        assert closing.activation_status == "active"

        # Дебиторка закрыта тем самым УПД: деньги нашли своё основание.
        await session.refresh(receivable)
        assert receivable.amount_settled == Decimal("3348.22")
        assert receivable.status == "settled"
        assert await _payable(session, cp.id) == Decimal("0.00")

        # Расход — июльский: период услуги совпадает с месяцем документа.
        await periods.set_invoice_service_period(
            session,
            invoice=closing,
            start=date(2026, 7, 1),
            end=date(2026, 7, 31),
            actor_user_id=None,
        )
        await periods.recognize_due_expenses(session, as_of=date(2026, 8, 1), commit=True)
        accrual = await session.scalar(
            select(SupplierExpenseAccrual).where(SupplierExpenseAccrual.invoice_id == closing.id)
        )
        assert accrual.status == "recognized"
        assert accrual.recognition_month == date(2026, 7, 1)


async def test_paid_bill_and_its_closing_do_not_double_the_debt(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Счёт и УПД на одну услугу не удваивают долг: счёт долгом не является по канону.

    У ЭкоЦентра в системе лежат пары «ВД-46890» и «ВД-46890-УПД» на одну сумму. Если бы оба
    считались обязательством, кредиторка по контрагенту оказалась бы вдвое больше реальной.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name='ООО "ЭкоЦентр-долг"', inn="3444177535")
        await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="772.37",
            doc_kind="bill",
            number="ВД-44001",
            invoice_date=date(2026, 7, 31),
            operational_scope="finance",
        )
        closing = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="772.37",
            doc_kind="closing",
            number="ВД-44001-УПД",
            invoice_date=date(2026, 7, 31),
            operational_scope="finance",
        )
        await session.commit()
        await prepayments.apply_closing_document(session, closing, as_of=date(2026, 8, 1))
        await session.commit()

        assert await _payable(session, cp.id) == Decimal("772.37")

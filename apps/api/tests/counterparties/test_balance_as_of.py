"""Остатки расчётов НА ДАТУ — то, чего не хватало балансу.

Плитка «Остатки» отвечает на вопрос «сколько должны СЕЙЧАС»: берёт текущие статусы и текущие
гашения. Баланс собирается на конец месяца, и вопрос там другой — «сколько было должно
31 июля». Документ, оплаченный 5 августа, сегодня закрыт, а на 31 июля был живой кредиторкой,
и текущими статусами этого не увидеть.

Здесь закреплены два свойства: обязательство появляется своей датой и гасится только теми
платежами, которые к дате уже произошли.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from cp_helpers import make_counterparty, make_invoice, make_wallet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    CashflowTransaction,
    InvoicePaymentAllocation,
    SupplierInvoice,
    SupplierPrepayment,
)
from app.services import supplier_prepayments
from app.services.counterparty_balance_as_of import build_balance_as_of


async def _cash_payment(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    wallet_id: uuid.UUID,
    amount: str,
    on: date,
) -> CashflowTransaction:
    tx = CashflowTransaction(
        counterparty_id=counterparty_id,
        wallet_id=wallet_id,
        direction="out",
        amount=Decimal(amount),
        operation_date=on,
        source_kind="manual",
        quality_status="auto",
    )
    session.add(tx)
    await session.flush()
    return tx


async def test_obligation_appears_on_its_document_date(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """До даты документа долга нет, с неё — есть (правило 4 канона)."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Дата документа", inn="6155000800")
        await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="50000.00",
            number="УПД-АВГ",
            doc_kind="closing",
            operational_scope="finance",
            invoice_date=date(2026, 8, 31),
            payment_status="unpaid",
        )
        await session.commit()

        before = await build_balance_as_of(session, as_of=date(2026, 7, 31))
        assert before.payable_total == Decimal("0.00"), "долг возник раньше даты документа"

        on_date = await build_balance_as_of(session, as_of=date(2026, 8, 31))
        assert on_date.payable_total == Decimal("50000.00")


async def test_payment_closes_the_debt_only_from_its_own_date(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Оплата 5 августа не закрывает июльскую кредиторку задним числом.

    Ровно этого не умеет плитка «Остатки»: сегодня документ оплачен и в остатке его нет, а на
    31 июля он был живым долгом. Без такого расчёта баланс на конец месяца собрать нечем.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Оплата позже", inn="6155000801")
        wallet = await make_wallet(session, code="tbank-asof-1", name="Т-Банк")
        invoice = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="12000.00",
            number="УПД-ИЮЛЬ",
            doc_kind="closing",
            operational_scope="finance",
            invoice_date=date(2026, 7, 20),
            payment_status="paid",
        )
        tx = await _cash_payment(
            session,
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            amount="12000.00",
            on=date(2026, 8, 5),
        )
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id,
                cashflow_transaction_id=tx.id,
                amount=Decimal("12000.00"),
                source_kind="cash",
                # Строку разобрали ещё позже, чем заплатили: дата записи ≠ дата события.
                created_at=datetime(2026, 8, 12, tzinfo=UTC),
            )
        )
        await session.commit()

        july = await build_balance_as_of(session, as_of=date(2026, 7, 31))
        assert july.payable_total == Decimal("12000.00"), "оплата августа закрыла июльский долг"

        august = await build_balance_as_of(session, as_of=date(2026, 8, 31))
        assert august.payable_total == Decimal("0.00")


async def test_prepayment_becomes_receivable_from_its_money_date(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Дебиторка появляется датой платежа, а не датой записи предоплаты."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Аванс на дату", inn="6155000802")
        wallet = await make_wallet(session, code="tbank-asof-2", name="Т-Банк")
        tx = await _cash_payment(
            session,
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            amount="9000.00",
            on=date(2026, 7, 10),
        )
        session.add(
            SupplierPrepayment(
                counterparty_id=cp.id,
                kind="subscription",
                wallet_id=wallet.id,
                amount=Decimal("9000.00"),
                amount_settled=Decimal("0.00"),
                status="open",
                cashflow_transaction_id=tx.id,
            )
        )
        await session.commit()

        before = await build_balance_as_of(session, as_of=date(2026, 7, 9))
        assert before.receivable_total == Decimal("0.00")

        after = await build_balance_as_of(session, as_of=date(2026, 7, 31))
        assert after.receivable_total == Decimal("9000.00")
        assert [row.counterparty_name for row in after.rows] == ["Аванс на дату"]


async def test_informational_and_future_documents_stay_out(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Справочный документ долгом не становится ни на одну дату.

    Тот же предикат, что у плитки и у сверки: расход по нему уже начислен договором, а вторая
    кредиторка на ту же услугу — выдумка.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Справочный на дату", inn="6155000803")
        doc = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="3000.00",
            number="АКТ-СПР",
            doc_kind="closing",
            operational_scope="finance",
            invoice_date=date(2026, 6, 30),
            payment_status="unpaid",
        )
        doc.informational = True
        # Счёт на оплату — не долг по канону, тоже не должен попадать.
        await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="7000.00",
            number="СЧ-100",
            doc_kind="bill",
            invoice_date=date(2026, 6, 15),
            payment_status="unpaid",
        )
        await session.commit()

        report = await build_balance_as_of(session, as_of=date(2026, 12, 31))
        assert report.payable_total == Decimal("0.00")
        assert report.rows == []


async def test_prepayment_settlement_counts_from_the_money_date(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Гашение авансом не может произойти раньше, чем пришли деньги.

    ЭкоЦентр присылает УПД, датированный 31.07, а счёт по нему оплачивают 15.08. По одной лишь
    дате документа выходило, что на 31 июля долг уже закрыт, хотя предоплаты в тот день ещё не
    существовало: обе стороны показывали ноль там, где был живой долг на 12 000 ₽.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Аванс позже документа", inn="6155000804")
        wallet = await make_wallet(session, code="tbank-asof-3", name="Т-Банк")
        invoice = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="12000.00",
            number="УПД-3107",
            doc_kind="closing",
            operational_scope="finance",
            invoice_date=date(2026, 7, 31),
            payment_status="paid",
        )
        tx = await _cash_payment(
            session,
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            amount="12000.00",
            on=date(2026, 8, 15),
        )
        prepayment = SupplierPrepayment(
            counterparty_id=cp.id,
            kind="prepaid_bill",
            wallet_id=wallet.id,
            amount=Decimal("12000.00"),
            amount_settled=Decimal("12000.00"),
            status="settled",
            cashflow_transaction_id=tx.id,
        )
        session.add(prepayment)
        await session.flush()
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id,
                prepayment_id=prepayment.id,
                amount=Decimal("12000.00"),
                source_kind="prepayment",
            )
        )
        await session.commit()

        july = await build_balance_as_of(session, as_of=date(2026, 7, 31))
        assert july.payable_total == Decimal("12000.00"), "долг закрыт деньгами из будущего"
        assert july.receivable_total == Decimal("0.00"), "аванса 31 июля ещё не было"

        august = await build_balance_as_of(session, as_of=date(2026, 8, 31))
        assert august.payable_total == Decimal("0.00")
        assert august.receivable_total == Decimal("0.00")


async def _pay_bill(
    session: AsyncSession,
    bill: SupplierInvoice,
    *,
    wallet_id: uuid.UUID,
    amount: str,
    on: date,
) -> CashflowTransaction:
    """Оплатить счёт (целиком или частью) и провести штатный чокпоинт ДЗ по счёту."""
    tx = await _cash_payment(
        session, counterparty_id=bill.counterparty_id, wallet_id=wallet_id, amount=amount, on=on
    )
    session.add(
        InvoicePaymentAllocation(
            invoice_id=bill.id,
            source_kind="cash",
            cashflow_transaction_id=tx.id,
            amount=Decimal(amount),
        )
    )
    await session.flush()
    await supplier_prepayments.reconcile_bill_prepayment(session, bill)
    await session.flush()
    return tx


async def _bill_prepayment(session: AsyncSession, bill: SupplierInvoice) -> SupplierPrepayment:
    prepayment = await session.scalar(
        select(SupplierPrepayment).where(SupplierPrepayment.bill_invoice_id == bill.id)
    )
    assert prepayment is not None and prepayment.cashflow_transaction_id is None
    return prepayment


async def _two_part_bill(
    session: AsyncSession, *, name: str, inn: str
) -> tuple[uuid.UUID, uuid.UUID, SupplierInvoice]:
    cp = await make_counterparty(session, name=name, inn=inn)
    wallet = await make_wallet(session, code=f"tbank-asof-{inn[-2:]}", name="Т-Банк")
    bill = await make_invoice(
        session,
        counterparty_id=cp.id,
        amount="10000.00",
        number=f"СЧ-{inn[-4:]}",
        doc_kind="bill",
        operational_scope="finance",
        invoice_date=date(2026, 7, 15),
        payment_status="unpaid",
    )
    return cp.id, wallet.id, bill


async def _closing_act(session: AsyncSession, cp_id: uuid.UUID, on: date) -> SupplierInvoice:
    return await make_invoice(
        session,
        counterparty_id=cp_id,
        amount="10000.00",
        number=f"АКТ-{on:%d%m}",
        doc_kind="closing",
        operational_scope="finance",
        invoice_date=on,
        payment_status="unpaid",
    )


async def _balance(session: AsyncSession, on: date) -> tuple[Decimal, Decimal]:
    report = await build_balance_as_of(session, as_of=on)
    return report.receivable_total, report.payable_total


async def test_paid_bill_receivable_counts_from_the_bill_payments(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ДЗ по оплаченному счёту живёт с даты оплаты счёта и растёт с каждой её частью.

    Своей проводки у такой ДЗ нет по конструкции чокпоинта (``reconcile_bill_prepayment``):
    деньги несёт аллокация счёта. Пока баланс датировал её днём ЗАПИСИ, аванс за электричество,
    оплаченный 20.06, а заведённый позже конца месяца, на этот конец не существовал вовсе, и
    закрывающий акт висел кредиторкой целиком. Запись здесь намеренно заведена 20.09 — позже
    всех дат среза, как бывает при позднем разборе выписки.
    """
    async with async_session_factory() as session:
        cp_id, wallet_id, bill = await _two_part_bill(
            session, name="Счёт двумя частями", inn="6155000805"
        )
        await _pay_bill(session, bill, wallet_id=wallet_id, amount="4000.00", on=date(2026, 7, 20))
        await _pay_bill(session, bill, wallet_id=wallet_id, amount="6000.00", on=date(2026, 8, 5))
        prepayment = await _bill_prepayment(session, bill)
        assert prepayment.amount == Decimal("10000.00")
        prepayment.created_at = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
        act = await _closing_act(session, cp_id, date(2026, 8, 31))
        await supplier_prepayments.apply_closing_document(session, act, as_of=date(2026, 9, 1))
        await session.commit()

        assert await _balance(session, date(2026, 7, 19)) == (0, 0), "денег по счёту ещё не было"
        assert await _balance(session, date(2026, 7, 31)) == (4000, 0), "вторая часть — 05.08"
        assert await _balance(session, date(2026, 8, 30)) == (10000, 0), "акт ещё не в силе"
        assert await _balance(session, date(2026, 8, 31)) == (0, 0), "акт погашен авансом"


async def test_act_between_two_bill_payments_leaves_the_unpaid_part_payable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Акт, вступивший в силу между двумя частями оплаты, закрыт авансом только на первую.

    Зачёт авансом записан на всю сумму акта, но на 31.07 по счёту ушло 4 000 из 10 000:
    остальные 6 000 — живой долг, пока 05.08 не пришла доплата. Одна дата на весь зачёт
    (первой оплаты) закрывала акт задним числом деньгами из будущего — сторона ДЗ при этом
    уже честно показывала только 4 000, и обе стороны описывали одно гашение по-разному.
    """
    async with async_session_factory() as session:
        cp_id, wallet_id, bill = await _two_part_bill(
            session, name="Акт между оплатами", inn="6155000806"
        )
        await _pay_bill(session, bill, wallet_id=wallet_id, amount="4000.00", on=date(2026, 7, 20))
        act = await _closing_act(session, cp_id, date(2026, 7, 25))
        await supplier_prepayments.apply_closing_document(session, act, as_of=date(2026, 7, 26))
        await _pay_bill(session, bill, wallet_id=wallet_id, amount="6000.00", on=date(2026, 8, 5))
        await session.commit()
        await session.refresh(act)
        assert act.payment_status == "paid", "доплата дозакрыла акт обратным неттингом"

        assert await _balance(session, date(2026, 7, 24)) == (4000, 0)
        assert await _balance(session, date(2026, 7, 31)) == (0, 6000), "6 000 ещё не ушли"
        assert await _balance(session, date(2026, 8, 5)) == (0, 0)


async def test_bill_money_carried_by_rule1_receivable_is_not_counted_twice(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Деньги, уже ставшие авансом правила 1, в ДЗ по счёту второй раз не попадают.

    Classify-then-match: платёж 20.07 сначала стал авансом правила 1 (своя проводка) и только
    потом его привязали к счёту — чокпоинт завёл ДЗ счёта лишь на доплату 05.08. Баланс,
    датируя ДЗ счёта ПЕРВОЙ его оплатой, приписал бы ей и деньги аванса: 8 000 на 31.07 при
    4 000 ушедших.
    """
    async with async_session_factory() as session:
        cp_id, wallet_id, bill = await _two_part_bill(
            session, name="Аванс правила 1", inn="6155000807"
        )
        first = await _cash_payment(
            session,
            counterparty_id=cp_id,
            wallet_id=wallet_id,
            amount="4000.00",
            on=date(2026, 7, 20),
        )
        session.add(
            SupplierPrepayment(
                counterparty_id=cp_id,
                kind="subscription",
                wallet_id=wallet_id,
                amount=Decimal("4000.00"),
                amount_settled=Decimal("0.00"),
                status="open",
                cashflow_transaction_id=first.id,
            )
        )
        session.add(
            InvoicePaymentAllocation(
                invoice_id=bill.id,
                source_kind="cash",
                cashflow_transaction_id=first.id,
                amount=Decimal("4000.00"),
            )
        )
        await session.flush()
        await supplier_prepayments.reconcile_bill_prepayment(session, bill)
        await _pay_bill(session, bill, wallet_id=wallet_id, amount="6000.00", on=date(2026, 8, 5))
        assert (await _bill_prepayment(session, bill)).amount == Decimal("6000.00")
        await session.commit()

        assert await _balance(session, date(2026, 7, 31)) == (4000, 0)
        assert await _balance(session, date(2026, 8, 31)) == (10000, 0)


async def test_rule1_remainder_of_a_bigger_payment_leaves_the_bill_money_alone(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Аванс правила 1 на той же проводке не всегда несёт деньги счёта.

    Платёж 15 000 сначала погасил счёт на 10 000 (ДЗ по счёту — 10 000), и только остаток 5 000
    правило 1 сделало своим авансом. Вычти аванс из денег счёта — и акт, закрытый ДЗ счёта,
    получил бы 5 000 ложной кредиторки, навсегда.
    """
    async with async_session_factory() as session:
        cp_id, wallet_id, bill = await _two_part_bill(
            session, name="Платёж больше счёта", inn="6155000808"
        )
        payment = await _cash_payment(
            session,
            counterparty_id=cp_id,
            wallet_id=wallet_id,
            amount="15000.00",
            on=date(2026, 7, 20),
        )
        session.add(
            InvoicePaymentAllocation(
                invoice_id=bill.id,
                source_kind="cash",
                cashflow_transaction_id=payment.id,
                amount=Decimal("10000.00"),
            )
        )
        await session.flush()
        await supplier_prepayments.reconcile_bill_prepayment(session, bill)
        session.add(
            SupplierPrepayment(
                counterparty_id=cp_id,
                kind="subscription",
                wallet_id=wallet_id,
                amount=Decimal("5000.00"),
                amount_settled=Decimal("0.00"),
                status="open",
                cashflow_transaction_id=payment.id,
            )
        )
        await session.flush()
        assert (await _bill_prepayment(session, bill)).amount == Decimal("10000.00")
        act = await _closing_act(session, cp_id, date(2026, 7, 25))
        await supplier_prepayments.apply_closing_document(session, act, as_of=date(2026, 7, 26))
        await session.commit()

        assert await _balance(session, date(2026, 7, 24)) == (15000, 0)
        assert await _balance(session, date(2026, 7, 31)) == (5000, 0)

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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    BusinessOwner,
    CashflowTransaction,
    InvoicePaymentAllocation,
    SupplierPrepayment,
)
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


async def test_an_owner_loan_is_not_a_supplier_advance(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Заём собственнику живёт в той же таблице, но это не аванс поставщику.

    Найдено на копии прод-базы: 1 220 000 ₽ займов Григорию и Павлу стояли внутри «Выданных
    авансов поставщикам» и составляли там 77 % строки — то есть баланс сообщал, что бизнес
    щедро авансирует поставщиков, хотя деньги ушли собственникам. Деньги выданы не под товар,
    гасятся не накладной, и в отчёте им место в своей строке.
    """
    async with async_session_factory() as session:
        owner = await make_counterparty(session, name="Собственник")
        supplier = await make_counterparty(session, name="Поставщик")
        wallet = await make_wallet(session, code="tbank-owner-loan", name="Т-Банк")
        for cp, kind, amount in (
            (owner, "owner_loan", "1020000.00"),
            (supplier, "subscription", "9000.00"),
        ):
            tx = await _cash_payment(
                session,
                counterparty_id=cp.id,
                wallet_id=wallet.id,
                amount=amount,
                on=date(2026, 7, 10),
            )
            session.add(
                SupplierPrepayment(
                    counterparty_id=cp.id,
                    kind=kind,
                    wallet_id=wallet.id,
                    amount=Decimal(amount),
                    amount_settled=Decimal("0.00"),
                    status="open",
                    cashflow_transaction_id=tx.id,
                )
            )
        await session.commit()

        sheet = await build_balance_as_of(session, as_of=date(2026, 7, 31))
        assert sheet.owner_loans_total == Decimal("1020000.00")
        assert sheet.receivable_total == Decimal("9000.00")


async def test_an_opening_balance_exists_from_the_start_of_accounting(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Входящий остаток датируется началом учёта, а не днём, когда его успели завести.

    Второй дефект того же прод-случая: займы собственникам завели 2 августа, и датировка «по
    дате создания» вычёркивала их из июля целиком — 1,22 млн молча уходили в капитал, потому
    что баланс сводится всегда. Строка с ``opening=true`` описывает долг, который БЫЛ на
    01.07.2026; дата ввода к его существованию отношения не имеет.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Собственник с опенингом")
        session.add(
            SupplierPrepayment(
                counterparty_id=cp.id,
                kind="owner_loan",
                amount=Decimal("200000.00"),
                amount_settled=Decimal("0.00"),
                status="open",
                opening=True,
                # Денежной проводки у входящего остатка нет — иначе он был бы обычной оплатой.
                cashflow_transaction_id=None,
                created_at=datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
            )
        )
        await session.commit()

        july = await build_balance_as_of(session, as_of=date(2026, 7, 31))
        assert july.owner_loans_total == Decimal("200000.00")


async def test_a_prepayment_to_a_registered_owner_is_a_loan_whatever_its_kind(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Собственника узнаём по реестру, а не по виду предоплаты.

    Владелец 09.08.2026: «у Павла 230 000, а не 200 000». Разница в 30 000 — заём от 14.07,
    который завела автоматика разбора банковского списания с видом ``subscription``: она про
    предоплатную модель и о собственниках не знает. Строка легла в «Выданные авансы
    поставщикам», и долг собственника в балансе оказался занижен.

    Вид ставит тот путь, который создал запись; принадлежность к собственникам ведёт человек в
    ``business_owner``. Второе устойчиво, первое — нет, поэтому смотрим на второе.
    """
    async with async_session_factory() as session:
        owner = await make_counterparty(session, name="Собственник Павел")
        supplier = await make_counterparty(session, name="Обычный поставщик")
        wallet = await make_wallet(session, code="tbank-owner-kind", name="Т-Банк")
        session.add(
            BusinessOwner(
                counterparty_id=owner.id,
                share_percent=Decimal("50.00"),
                started_on=date(2026, 1, 1),
            )
        )
        for cp, kind, amount in (
            (owner, "owner_loan", "200000.00"),
            # Тот самый автоматический разбор: вид «подписка», а по сути заём собственнику.
            (owner, "subscription", "30000.00"),
            (supplier, "subscription", "9000.00"),
        ):
            tx = await _cash_payment(
                session,
                counterparty_id=cp.id,
                wallet_id=wallet.id,
                amount=amount,
                on=date(2026, 7, 14),
            )
            session.add(
                SupplierPrepayment(
                    counterparty_id=cp.id,
                    kind=kind,
                    wallet_id=wallet.id,
                    amount=Decimal(amount),
                    amount_settled=Decimal("0.00"),
                    status="open",
                    cashflow_transaction_id=tx.id,
                )
            )
        await session.commit()

        sheet = await build_balance_as_of(session, as_of=date(2026, 7, 31))
        assert sheet.owner_loans_total == Decimal("230000.00")
        # Подписка настоящего поставщика осталась там, где ей место.
        assert sheet.receivable_total == Decimal("9000.00")

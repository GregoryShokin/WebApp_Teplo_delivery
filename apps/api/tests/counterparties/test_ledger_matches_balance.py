"""Итог сверки с контрагентом = его остаток на сегодня в балансе на дату. Замок на равенство.

НАХОДКА 25.09.2026 НА КОПИИ ПРОДА, подтверждённая двумя независимыми проверками. Сверка в
карточке контрагента («деньги минус документы») обещала сходиться с «Остатками», но у 9 из 48
контрагентов расходилась на 204 772,53 ₽, и так было давно, ещё на коде 04cade1a. Разбор по
первичке дал две разные части.

Пробелы кода, 154 974,73 ₽. Баланс знает о деньгах то, чего сверка не видела:

* дивиденды — выплата, а не долг (решение владельца 25.09): Григорий и Павел по 50 000 ₽;
* возвраты от поставщиков гасят дебиторку без строк гашения: Лигай 2 822 ₽, Скачкова 10 112,13 ₽;
* предоплата, закрытая решением человека без аллокаций: «Поставка овощей», 38 479 ₽;
* наша проводка оплатила документ другого контрагента и жила в двух сверках сразу: ТОРА
  → Скачкова, 3 561,60 ₽.

Вопросы к данным, 49 797,80 ₽. Это платежи без предоплаты и без привязки к документу. Сверка
честно считает их авансом, а баланс их не видит. Кодом это НЕ чинится: решает человек, и
расхождение здесь — единственный сигнал, что строку надо разобрать.

Поэтому замок сформулирован как равенство с явной поправкой:

    итог сверки − неразобранные платежи = нетто баланса на сегодня

Уберёт код одно из четырёх правил — равенство сломается на своём сценарии. Спрячет
неразобранный платёж — сломается тест, который требует, чтобы он остался виден. На копии прода
то же равенство проверяется по всем контрагентам, если задана ``TEPLO_PRODCOPY_DATABASE_URL``.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from cp_helpers import make_bank_operation, make_counterparty, make_invoice, make_wallet
from sqlalchemy import and_, exists, or_, select, text, union
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.models import (
    BankOperation,
    CashflowTransaction,
    CounterpartyPayableProfile,
    DdsArticle,
    InvoicePaymentAllocation,
    SupplierInvoice,
    SupplierPrepayment,
)
from app.services import accounting_periods, clock, owner_analytics, supplier_prepayments
from app.services.counterparty_balance_as_of import build_balance_as_of
from app.services.counterparty_settlement_ledger import (
    ROW_CLOSURE,
    ROW_PAYMENT,
    ROW_PAYOUT,
    ROW_REFUND,
    ROW_TRANSFER,
    build_ledger,
)

TODAY = date(2026, 9, 25)


async def _article(session: AsyncSession, code: str) -> DdsArticle:
    """Статья из каталога миграций (0114): код у неё тот же, что на проде."""
    article = await session.scalar(select(DdsArticle).where(DdsArticle.code == code))
    assert article is not None, f"в каталоге нет статьи {code}"
    return article


async def _out(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    wallet_id: uuid.UUID,
    amount: str,
    on: date,
    source_kind: str = "manual",
    source_id: uuid.UUID | None = None,
    article_id: uuid.UUID | None = None,
    expense_month: date | None = None,
) -> CashflowTransaction:
    tx = CashflowTransaction(
        wallet_id=wallet_id,
        counterparty_id=counterparty_id,
        direction="out",
        amount=Decimal(amount),
        operation_date=on,
        source_kind=source_kind,
        source_id=source_id,
        article_id=article_id,
        expense_month=expense_month,
        quality_status="final",
    )
    session.add(tx)
    await session.flush()
    return tx


async def _prepayment(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    wallet_id: uuid.UUID,
    tx: CashflowTransaction | None,
    amount: str,
    kind: str = "goods",
) -> SupplierPrepayment:
    prepayment = SupplierPrepayment(
        counterparty_id=counterparty_id,
        kind=kind,
        wallet_id=wallet_id,
        amount=Decimal(amount),
        amount_settled=Decimal("0"),
        status="open",
        cashflow_transaction_id=tx.id if tx else None,
        service_period_status="missing",
    )
    session.add(prepayment)
    await session.flush()
    return prepayment


async def unplaced_payments(
    session: AsyncSession, counterparty_id: uuid.UUID
) -> dict[uuid.UUID, Decimal]:
    """Платежи без предоплаты и без привязки: сверка считает их авансом, баланс их не видит.

    «Без привязки» — ни одной аллокации ни по одному ключу денег: ни по самой проводке, ни по
    её банк-операции (с обоих концов моста), ни по чеку, в долю которого она входит. Оплата
    СЧЁТА — тоже привязка: у неё своя ДЗ ``prepaid_bill``, и баланс её видит. Платёж за период
    до начала учёта и выплата дивидендов сюда не входят: остаток они не двигают и в сверке.
    """
    bridged_allocation = (
        select(InvoicePaymentAllocation.id)
        .join(BankOperation, BankOperation.id == InvoicePaymentAllocation.bank_operation_id)
        .where(BankOperation.cashflow_transaction_id == CashflowTransaction.id)
    )
    placed = or_(
        exists().where(SupplierPrepayment.cashflow_transaction_id == CashflowTransaction.id),
        exists().where(InvoicePaymentAllocation.cashflow_transaction_id == CashflowTransaction.id),
        bridged_allocation.exists(),
        and_(
            CashflowTransaction.source_kind == "bank_operation",
            exists().where(
                InvoicePaymentAllocation.bank_operation_id == CashflowTransaction.source_id
            ),
        ),
        and_(
            CashflowTransaction.source_kind == "kassa_cheque",
            exists().where(InvoicePaymentAllocation.invoice_id == CashflowTransaction.source_id),
        ),
    )
    rows = (
        await session.execute(
            select(CashflowTransaction.id, CashflowTransaction.amount)
            .outerjoin(DdsArticle, DdsArticle.id == CashflowTransaction.article_id)
            .where(
                CashflowTransaction.counterparty_id == counterparty_id,
                CashflowTransaction.direction == "out",
                CashflowTransaction.quality_status != "excluded",
                ~placed,
                or_(
                    CashflowTransaction.expense_month.is_(None),
                    CashflowTransaction.expense_month >= accounting_periods.ACCOUNTING_START,
                ),
                or_(
                    DdsArticle.code.is_(None),
                    DdsArticle.code != owner_analytics.DIVIDENDS_ARTICLE_CODE,
                ),
            )
        )
    ).all()
    return {row[0]: Decimal(row[1]) for row in rows}


async def divergences(session: AsyncSession, today: date) -> dict[uuid.UUID, Decimal]:
    """Контрагенты, у которых «сверка − неразобранные платежи ≠ баланс на сегодня»."""
    balance = {
        row.counterparty_id: row.net
        for row in (await build_balance_as_of(session, as_of=today)).rows
    }
    ids = set(
        (
            await session.scalars(
                union(
                    select(SupplierInvoice.counterparty_id).where(
                        SupplierInvoice.counterparty_id.is_not(None),
                        SupplierInvoice.direction == "payable",
                        SupplierInvoice.doc_kind == "closing",
                    ),
                    select(SupplierPrepayment.counterparty_id),
                    select(CashflowTransaction.counterparty_id).where(
                        CashflowTransaction.counterparty_id.is_not(None),
                        CashflowTransaction.direction == "out",
                    ),
                )
            )
        ).all()
    )
    out: dict[uuid.UUID, Decimal] = {}
    for cp_id in ids:
        ledger = await build_ledger(session, cp_id, today=today)
        unplaced = sum((await unplaced_payments(session, cp_id)).values(), Decimal("0"))
        diff = ledger.closing_balance - unplaced - balance.get(cp_id, Decimal("0"))
        if diff:
            out[cp_id] = diff
    return out


async def test_dividends_are_a_payout_not_owner_debt(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Дивиденды из Сейфа видны в сверке, но остаток собственника не двигают.

    Решение владельца 25.09.2026: дивиденды — выплата, а не долг. Баланс их дебиторкой не
    считает (предоплаты у выдачи нет), а сверка считала: у Григория 1 070 000 ₽ против
    1 020 000 ₽ в «Остатках». Заём собственнику при этом — по-прежнему долг: его предоплата
    на месте, и остаток он двигает.
    """
    async with async_session_factory() as session:
        owner = await make_counterparty(
            session, name="Григорий", inn=None, cp_type="individual", relationship="informal"
        )
        safe = await make_wallet(session, code="safe-div", name="Сейф")
        opening = await supplier_prepayments.create_opening_prepayment(
            session,
            counterparty_id=owner.id,
            amount=Decimal("1020000.00"),
            kind=owner_analytics.OWNER_LOAN_KIND,
            note="Входящий остаток на 01.07.2026",
        )
        # Как на проде: остаток заведён 02.08, до выплаты. Дата его строки — день записи.
        opening.created_at = datetime(2026, 8, 2, 9, tzinfo=UTC)
        dividends = await _article(session, owner_analytics.DIVIDENDS_ARTICLE_CODE)
        payout = await _out(
            session,
            counterparty_id=owner.id,
            wallet_id=safe.id,
            amount="50000.00",
            on=date(2026, 8, 19),
            source_kind="safe_payout",
            article_id=dividends.id,
        )
        loans = await _article(session, "vydacha_kreditov_i_zaimov")
        loan = await _out(
            session,
            counterparty_id=owner.id,
            wallet_id=safe.id,
            amount="30000.00",
            on=date(2026, 8, 20),
            article_id=loans.id,
        )
        await _prepayment(
            session,
            counterparty_id=owner.id,
            wallet_id=safe.id,
            tx=loan,
            amount="30000.00",
            kind="subscription",
        )
        await session.commit()

        ledger = await build_ledger(session, owner.id, today=TODAY)

        assert ledger.closing_balance == Decimal("1050000.00")
        (row,) = [r for r in ledger.rows if r.id == payout.id]
        assert row.kind == ROW_PAYOUT
        assert row.title == "Выплата дивидендов"
        assert row.binds is False and row.owner_settlement and row.status == "ok"
        # Остаток на строке выплаты тот же, что до неё; в «заплачено» она не входит.
        assert row.balance_after == Decimal("1020000.00")
        assert ledger.total_paid == Decimal("1050000.00")
        assert await divergences(session, TODAY) == {}


async def test_supplier_refund_settles_receivable_and_excess_stays_income(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Возврат гасит дебиторку отдельной строкой; излишек сверх неё — обычный приход.

    Как у Лигая: аванс 156 800 ₽, поставок на 153 978 ₽, 20.07 вернули 2 822 ₽. Баланс вычитает
    возврат из дебиторки, а сверка его не видела и держала 2 822 ₽ «заплачено вперёд». Излишек
    возврата сверх открытой дебиторки баланс обрезает, и ``refund_counterparty_prepayments``
    оставляет его приходом. Гасить сверке тоже нечего — иначе остаток ушёл бы в «должны мы».
    """
    async with async_session_factory() as session:
        supplier = await make_counterparty(session, name="Лигай", inn="6143049401")
        bank = await make_wallet(session, code="tbank-refund", name="Т-Банк")
        refunds = await _article(session, supplier_prepayments.SUPPLIER_REFUND_ARTICLE_CODE)
        advance = await _out(
            session,
            counterparty_id=supplier.id,
            wallet_id=bank.id,
            amount="156800.00",
            on=date(2026, 7, 1),
            source_kind="supplier_prepayment",
        )
        prepayment = await _prepayment(
            session, counterparty_id=supplier.id, wallet_id=bank.id, tx=advance, amount="156800.00"
        )
        delivery = await make_invoice(
            session,
            counterparty_id=supplier.id,
            amount="153978.00",
            doc_kind="closing",
            invoice_date=date(2026, 7, 10),
            payment_status="paid",
        )
        session.add(
            InvoicePaymentAllocation(
                invoice_id=delivery.id,
                prepayment_id=prepayment.id,
                amount=Decimal("153978.00"),
                source_kind="prepayment",
            )
        )
        prepayment.amount_settled = Decimal("153978.00")
        prepayment.status = "partially_settled"
        refund = CashflowTransaction(
            wallet_id=bank.id,
            counterparty_id=supplier.id,
            direction="in",
            amount=Decimal("2822.00"),
            operation_date=date(2026, 7, 20),
            article_id=refunds.id,
            source_kind="new_payment_income",
            quality_status="final",
        )
        session.add(refund)
        await session.flush()
        await supplier_prepayments.refund_counterparty_prepayments(
            session, counterparty_id=supplier.id, amount=Decimal("2822.00")
        )
        await session.commit()

        ledger = await build_ledger(session, supplier.id, today=TODAY)
        assert ledger.closing_balance == Decimal("0.00")
        (row,) = [r for r in ledger.rows if r.kind == ROW_REFUND]
        assert row.id == refund.id and row.amount == Decimal("2822.00")
        assert row.uncovered == Decimal("0.00")
        advance_row = next(r for r in ledger.rows if r.id == advance.id)
        assert advance_row.closed_by == "refund"
        assert await divergences(session, TODAY) == {}

        # Вернули ещё 1 000 ₽, а открытой дебиторки уже нет: это приход, а не гашение.
        extra = CashflowTransaction(
            wallet_id=bank.id,
            counterparty_id=supplier.id,
            direction="in",
            amount=Decimal("1000.00"),
            operation_date=date(2026, 7, 25),
            article_id=refunds.id,
            source_kind="new_payment_income",
            quality_status="final",
        )
        session.add(extra)
        await session.commit()

        ledger = await build_ledger(session, supplier.id, today=TODAY)
        assert ledger.closing_balance == Decimal("0.00")
        (extra_row,) = [r for r in ledger.rows if r.id == extra.id]
        assert extra_row.kind == ROW_REFUND and extra_row.uncovered == Decimal("1000.00")
        assert await divergences(session, TODAY) == {}


async def test_prepayment_closed_by_decision_leaves_the_ledger_on_its_date(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Предоплата, закрытая человеком без аллокаций, уходит из остатка датой решения.

    Как у «Поставки овощей»: платёж 38 479 ₽ от 23.06, 20.07 закрыт историческим расчётом без
    строк гашения (``settled_on``). Баланс считал его закрытым, сверка — нет, и карточка писала
    «мы заплатили вперёд 22 397 ₽», хотя должны мы 17 398 ₽.
    """
    async with async_session_factory() as session:
        supplier = await make_counterparty(session, name="Поставка овощей", inn="6143049402")
        bank = await make_wallet(session, code="tbank-closure", name="Т-Банк")
        payment = await _out(
            session,
            counterparty_id=supplier.id,
            wallet_id=bank.id,
            amount="38479.00",
            on=date(2026, 6, 23),
            source_kind="supplier_prepayment",
        )
        prepayment = await _prepayment(
            session, counterparty_id=supplier.id, wallet_id=bank.id, tx=payment, amount="38479.00"
        )
        prepayment.amount_settled = Decimal("38479.00")
        prepayment.status = "settled"
        prepayment.settled_on = date(2026, 7, 20)
        prepayment.note = "Исторический расчёт за поставки до текущего контура ДЗ/КЗ"
        await make_invoice(
            session,
            counterparty_id=supplier.id,
            amount="17398.00",
            doc_kind="closing",
            invoice_date=date(2026, 9, 18),
        )
        await session.commit()

        ledger = await build_ledger(session, supplier.id, today=TODAY)

        assert ledger.closing_balance == Decimal("-17398.00")
        (closure,) = [r for r in ledger.rows if r.kind == ROW_CLOSURE]
        assert closure.row_date == date(2026, 7, 20)
        assert closure.amount == Decimal("38479.00")
        assert closure.subtitle is not None and "Исторический расчёт" in closure.subtitle
        assert closure.balance_after == Decimal("0.00")
        payment_row = next(r for r in ledger.rows if r.id == payment.id)
        assert payment_row.closed_by == "decision"
        assert await divergences(session, TODAY) == {}

        # Решение ещё не наступило — предоплата живая, как и в балансе на ту же дату.
        early = await build_ledger(session, supplier.id, today=date(2026, 7, 19))
        assert next(r for r in early.rows if r.kind == ROW_CLOSURE).binds is False


async def test_payment_for_another_counterpartys_document_lives_in_one_ledger(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Наша проводка закрыла чужой документ — деньги у того, чей документ, а у нас вычет.

    На проде 3 561,60 ₽ от 30.06 размечены на ТОРА, а оплатили УПД DX001312A ИП Скачковой.
    Баланс относит деньги по документу: у Скачковой 0, у ТОРА 0. Сверка Скачковой это давно
    показывает строкой «Оплата по проводке ТОРА», а сверка ТОРА держала ту же сумму авансом.
    """
    async with async_session_factory() as session:
        supplier = await make_counterparty(session, name="ИП Скачкова", inn="6143049403")
        payer = await make_counterparty(session, name='ООО "ТОРА"', inn="6143049404")
        bank = await make_wallet(session, code="tbank-transfer", name="Т-Банк")
        invoice = await make_invoice(
            session,
            counterparty_id=supplier.id,
            amount="3561.60",
            number="DX001312A",
            doc_kind="closing",
            invoice_date=date(2026, 6, 19),
            payment_status="paid",
        )
        payment = await _out(
            session,
            counterparty_id=payer.id,
            wallet_id=bank.id,
            amount="3561.60",
            on=date(2026, 6, 30),
            source_kind="counterparty_payment",
        )
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id,
                cashflow_transaction_id=payment.id,
                amount=Decimal("3561.60"),
                source_kind="cash",
            )
        )
        await session.commit()

        payer_ledger = await build_ledger(session, payer.id, today=TODAY)
        supplier_ledger = await build_ledger(session, supplier.id, today=TODAY)

        assert payer_ledger.closing_balance == Decimal("0.00")
        assert supplier_ledger.closing_balance == Decimal("0.00")
        (transfer,) = [r for r in payer_ledger.rows if r.kind == ROW_TRANSFER]
        assert transfer.amount == Decimal("3561.60")
        assert "ИП Скачкова" in transfer.title
        assert transfer.subtitle is not None and "DX001312A" in transfer.subtitle
        # Свежими сверху: вычет стоит над своим платежом того же дня, и «остаток после» в
        # соседних строках читается подряд (+3 561,60 → 3 561,60, затем −3 561,60 → 0).
        assert [(r.kind, r.balance_after) for r in payer_ledger.rows] == [
            (ROW_TRANSFER, Decimal("0.00")),
            (ROW_PAYMENT, Decimal("3561.60")),
        ]
        assert await divergences(session, TODAY) == {}


async def test_split_cheque_payment_is_covered_across_all_its_shares(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Оплата чека картой покрывает все доли чека, а не только первую.

    Чек «Местного закупа» разносится по статьям на несколько проводок ``kassa_cheque``, а его
    оплата привязана к банк-операции (``bank_operation_id``), которая ссылается только на первую
    долю. Сверка искала привязку лишь у проводок ``bank_operation``, и 149 оплатами на 342 974 ₽
    красила «без документов 191 521,69 ₽». Остаток от этого не страдал, страдал вид.
    """
    async with async_session_factory() as session:
        shop = await make_counterparty(session, name="Местный закуп", inn=None)
        card = await make_wallet(session, code="tbank-card", name="Т-Банк карта")
        cheque = await make_invoice(
            session,
            counterparty_id=shop.id,
            amount="408.00",
            number="Ч-1",
            doc_kind="closing",
            source="kassa_cheque",
            invoice_date=date(2026, 6, 22),
            payment_status="paid",
        )
        operation = await make_bank_operation(
            session, amount="408.00", operation_date=date(2026, 6, 22)
        )
        shares = [
            await _out(
                session,
                counterparty_id=shop.id,
                wallet_id=card.id,
                amount=amount,
                on=date(2026, 6, 22),
                source_kind="kassa_cheque",
                source_id=cheque.id,
            )
            for amount in ("401.00", "7.00")
        ]
        operation.cashflow_transaction_id = shares[0].id
        session.add(
            InvoicePaymentAllocation(
                invoice_id=cheque.id,
                bank_operation_id=operation.id,
                amount=Decimal("408.00"),
                source_kind="bank",
            )
        )
        await session.commit()

        ledger = await build_ledger(session, shop.id, today=TODAY)

        assert ledger.closing_balance == Decimal("0.00")
        payments = [r for r in ledger.rows if r.kind == ROW_PAYMENT]
        assert sorted(r.amount for r in payments) == [Decimal("7.00"), Decimal("401.00")]
        assert all(r.uncovered == Decimal("0") and r.status == "ok" for r in payments)
        assert ledger.overdue_amount == Decimal("0")
        assert await divergences(session, TODAY) == {}


async def test_split_bank_operation_spreads_its_settlement_over_the_shares(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«Ничьё» гашение банк-операции раскладывается по её долям, не задваиваясь.

    Операция в магазине разнесена по двум статьям, а оплата документа помечена операцией. Прежде
    всё гашение вешалось на первую долю, и вторая краснела «без документов». Покрытие по факту
    одно: раскладка не должна дать больше суммы гашения.
    """
    async with async_session_factory() as session:
        shop = await make_counterparty(session, name="Магазин у дома", inn=None)
        card = await make_wallet(session, code="tbank-split", name="Т-Банк")
        operation = await make_bank_operation(
            session, amount="5361.84", operation_date=date(2026, 6, 21)
        )
        receipt = await make_invoice(
            session,
            counterparty_id=shop.id,
            amount="5000.00",
            doc_kind="closing",
            invoice_date=date(2026, 6, 21),
            payment_status="partially_paid",
        )
        first, second = [
            await _out(
                session,
                counterparty_id=shop.id,
                wallet_id=card.id,
                amount=amount,
                on=date(2026, 6, 21),
                source_kind="bank_operation",
                source_id=operation.id,
            )
            for amount in ("5339.86", "21.98")
        ]
        session.add(
            InvoicePaymentAllocation(
                invoice_id=receipt.id,
                bank_operation_id=operation.id,
                amount=Decimal("5000.00"),
                source_kind="bank",
            )
        )
        await session.commit()

        ledger = await build_ledger(session, shop.id, today=TODAY)

        by_id = {r.id: r for r in ledger.rows}
        # 5 000 гашения: первая доля покрыта на 5 000, вторая не покрыта — не 5 000 дважды.
        assert by_id[first.id].uncovered == Decimal("339.86")
        assert by_id[second.id].uncovered == Decimal("21.98")

        session.add(
            InvoicePaymentAllocation(
                invoice_id=receipt.id,
                bank_operation_id=operation.id,
                amount=Decimal("361.84"),
                source_kind="bank",
            )
        )
        receipt.amount = Decimal("5361.84")
        receipt.payment_status = "paid"
        await session.commit()

        ledger = await build_ledger(session, shop.id, today=TODAY)
        by_id = {r.id: r for r in ledger.rows}
        assert by_id[first.id].uncovered == Decimal("0")
        assert by_id[second.id].uncovered == Decimal("0")
        assert ledger.closing_balance == Decimal("0.00")


async def test_unplaced_payment_stays_visible_as_a_question_for_a_human(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Платёж без предоплаты и без привязки остаётся расхождением — его решает человек.

    Так на проде лежат 45 242,36 ₽ и 485 ₽ ТОРА от 23.06: деньги ушли до начала учёта, месяц
    расхода не указан. Сверка считает их авансом, баланс — нет. Подгонять код под совпадение
    нельзя: другого сигнала, что строку надо разобрать, нет. Когда человек укажет месяц до
    начала учёта, строка перестанет двигать остаток сама, без правки кода.
    """
    async with async_session_factory() as session:
        supplier = await make_counterparty(session, name='ООО "ТОРА"', inn="6143049405")
        bank = await make_wallet(session, code="tbank-question", name="Т-Банк")
        payment = await _out(
            session,
            counterparty_id=supplier.id,
            wallet_id=bank.id,
            amount="45242.36",
            on=date(2026, 6, 23),
            source_kind="bank_operation",
        )
        await session.commit()

        ledger = await build_ledger(session, supplier.id, today=TODAY)
        balance = await build_balance_as_of(session, as_of=TODAY)

        assert ledger.closing_balance == Decimal("45242.36")
        assert all(row.counterparty_id != supplier.id for row in balance.rows)
        assert await unplaced_payments(session, supplier.id) == {payment.id: Decimal("45242.36")}
        assert await divergences(session, TODAY) == {}

        payment.expense_month = date(2026, 6, 1)
        await session.commit()

        ledger = await build_ledger(session, supplier.id, today=TODAY)
        assert ledger.closing_balance == Decimal("0.00")
        assert await unplaced_payments(session, supplier.id) == {}


# Платежи на копии прода 25.09.2026, которые кодом не чинятся: решение по каждому за владельцем.
OWNER_QUESTIONS: dict[uuid.UUID, str] = {
    uuid.UUID("af8f5233-c066-43f9-a1bf-8892aedb0ec7"): "ТОРА 45 242,36 от 23.06 — до начала учёта?",
    uuid.UUID("fc034e21-dc3f-48b2-9bb5-880db0d9c853"): "ТОРА 485 от 23.06 — до начала учёта?",
    uuid.UUID("2715f98a-8177-4b2a-84c0-2445c8b050ac"): "МЯСНОФФ-ДОН 912,44 от 23.06 — до учёта?",
    uuid.UUID("718eb71b-8711-4f95-a363-e6061b68f9fb"): "Алиев 550 от 13.07 — акт сверки №1005",
    uuid.UUID("76887871-d24d-4de4-82a2-592c13400829"): "Поставка овощей 1 316 от 15.07 — ДЗ?",
    uuid.UUID("1ec3c034-ef39-44dd-8666-08955d2d6133"): "Местный закуп 717 от 15.09 — расход?",
    uuid.UUID("a371651d-3f9e-4b59-9095-a98b61194b9c"): "Местный закуп 150 от 17.09 — расход?",
    uuid.UUID("40d674dc-5cfa-4f8b-b311-2d9924cfbc21"): "Местный закуп 425 от 22.09 — расход?",
}


@pytest.mark.skipif(
    not os.environ.get("TEPLO_PRODCOPY_DATABASE_URL"),
    reason="нужна копия прода: TEPLO_PRODCOPY_DATABASE_URL (pg_dump в свой Postgres)",
)
async def test_prod_copy_ledgers_match_balance_except_owner_questions() -> None:
    """На копии прода: сверка = баланс по ВСЕМ контрагентам, кроме платежей-вопросов владельцу.

    Только чтение: транзакция открывается READ ONLY, и копия после прогона та же. Новый
    неразобранный платёж роняет тест списком — это не ошибка кода, а новый вопрос к данным.
    """
    engine = create_async_engine(os.environ["TEPLO_PRODCOPY_DATABASE_URL"], poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            today = clock.moscow_today()
            assert await divergences(session, today) == {}

            profiles = (await session.scalars(select(CounterpartyPayableProfile))).all()
            ids = {p.counterparty_id for p in profiles} | set(
                (
                    await session.scalars(
                        select(CashflowTransaction.counterparty_id)
                        .where(CashflowTransaction.counterparty_id.is_not(None))
                        .distinct()
                    )
                ).all()
            )
            unplaced: dict[uuid.UUID, Decimal] = {}
            for cp_id in ids:
                unplaced.update(await unplaced_payments(session, cp_id))
            unknown = {
                tx_id: amount for tx_id, amount in unplaced.items() if tx_id not in OWNER_QUESTIONS
            }
            assert unknown == {}, f"новые неразобранные платежи — вопрос владельцу: {unknown}"
    finally:
        await engine.dispose()

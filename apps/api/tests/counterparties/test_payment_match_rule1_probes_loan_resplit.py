"""Пробы скептика (Fable, 25.09, раунд 2): заём на доле разбора и переразбор после двери.

1) Сверка бартерного займа при авансе правила 1 помечена проводкой доли — пересборка якоря
   не ужимает чужой аванс; исключение снимает её; на якорной доле двойного уменьшения нет;
   счета/баланс не задеты.
2) Переразбор операции снимает зачёты двери до проверки остатков: с той же накладной, с другой,
   с черновиком (отказ без записи), с долями на двух контрагентов.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from cp_helpers import (
    make_account,
    make_bank_operation,
    make_counterparty,
    make_draft,
    make_expense_article,
    make_invoice,
    make_wallet,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    BankOperation,
    IikoProduct,
    InvoicePaymentAllocation,
    SupplierInvoice,
    SupplierPrepayment,
)
from app.services.banking.classifier import (
    OperationSplitLine,
    apply_operation_action,
    apply_operation_split,
)
from app.services.counterparty_balance_as_of import build_balance_as_of
from app.services.supplier_prepayments import RULE1_PREPAYMENT_KIND

PAID_ON = date(2026, 7, 31)
DOC_ON = date(2026, 8, 1)
POSTED = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
ISSUED = datetime(2026, 8, 1, 7, 0, tzinfo=UTC)
ZERO = Decimal("0.00")
D = Decimal


async def _bank_fixture(session: AsyncSession, *, inn: str, amount: str) -> BankOperation:
    account = await make_account(session)
    await make_wallet(
        session,
        name=f"Т-Банк {inn}-{uuid.uuid4().hex[:4]}",
        wallet_type="bank",
        account_id=account.id,
    )
    return await make_bank_operation(
        session,
        amount=amount,
        inn=inn,
        operation_date=PAID_ON,
        posted_at=POSTED,
        account_id=account.id,
    )


async def _statement_payment(
    session: AsyncSession, *, counterparty_id: uuid.UUID, inn: str, amount: str = "12000.00"
) -> BankOperation:
    article = await make_expense_article(session)
    operation = await _bank_fixture(session, inn=inn, amount=amount)
    await apply_operation_action(
        session,
        operation,
        action="set_article",
        article_id=article.id,
        counterparty_id=counterparty_id,
    )
    return operation


async def _warehouse_invoice(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    amount: str = "12000.00",
    number: str = "ТН-0801",
) -> SupplierInvoice:
    return await make_invoice(
        session,
        counterparty_id=counterparty_id,
        amount=amount,
        number=number,
        operational_scope="warehouse",
        invoice_date=DOC_ON,
        issued_at=ISSUED,
    )


async def _advances(session: AsyncSession, counterparty_id: uuid.UUID) -> list[SupplierPrepayment]:
    return list(
        (
            await session.scalars(
                select(SupplierPrepayment)
                .where(
                    SupplierPrepayment.counterparty_id == counterparty_id,
                    SupplierPrepayment.kind == RULE1_PREPAYMENT_KIND,
                )
                .order_by(SupplierPrepayment.created_at)
            )
        ).all()
    )


async def _adv(session: AsyncSession, counterparty_id: uuid.UUID) -> tuple[Decimal, Decimal] | None:
    rows = await _advances(session, counterparty_id)
    assert len(rows) <= 1, [(r.amount, r.amount_settled, r.status) for r in rows]
    return (rows[0].amount, rows[0].amount_settled) if rows else None


async def _balance(
    session: AsyncSession, counterparty_id: uuid.UUID, as_of: date
) -> tuple[Decimal, Decimal]:
    sheet = await build_balance_as_of(session, as_of=as_of)
    row = next((r for r in sheet.rows if r.counterparty_id == counterparty_id), None)
    return (row.receivable, row.payable) if row is not None else (ZERO, ZERO)


async def _ledger_receivable(session: AsyncSession, counterparty_id: uuid.UUID) -> Decimal:
    rows = (
        await session.scalars(
            select(SupplierPrepayment).where(
                SupplierPrepayment.counterparty_id == counterparty_id,
                SupplierPrepayment.status.in_(("open", "partially_settled")),
            )
        )
    ).all()
    return sum((r.amount - r.amount_settled for r in rows), ZERO)


async def _allocations(
    session: AsyncSession, invoice_id: uuid.UUID
) -> list[InvoicePaymentAllocation]:
    return list(
        (
            await session.scalars(
                select(InvoicePaymentAllocation)
                .where(InvoicePaymentAllocation.invoice_id == invoice_id)
                .order_by(InvoicePaymentAllocation.created_at)
            )
        ).all()
    )


async def _confirm(
    factory: async_sessionmaker[AsyncSession], invoice_id: uuid.UUID, op_id: uuid.UUID
) -> None:
    from app.services.counterparty_bank_match import confirm_invoice_match

    async with factory() as session:
        await confirm_invoice_match(
            session,
            invoice_id=invoice_id,
            bank_operation_id=op_id,
            enrich=False,
            actor_user_id=None,
        )


async def _their_loan(
    session: AsyncSession, counterparty_id: uuid.UUID, *, amount: str
) -> SupplierInvoice:
    from app.services.warehouse_invoices import LineInput, create_warehouse_invoice

    product = IikoProduct(
        iiko_id=str(uuid.uuid4()),
        name="Моцарелла",
        type="GOODS",
        unit="кг",
        synced_at=datetime.now(UTC),
    )
    session.add(product)
    await session.commit()
    return await create_warehouse_invoice(
        session,
        counterparty_id=counterparty_id,
        issued_at=datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
        mode="loan",
        we_lend=False,
        lines=[
            LineInput(
                name="Моцарелла", quantity=D("10"), price=D(amount) / 10, iiko_product_id=product.id
            )
        ],
    )


async def _split_two(
    session: AsyncSession, *, inn: str, a_id: uuid.UUID, b_id: uuid.UUID
) -> BankOperation:
    """Операция 12 000: 8 000 → A (якорь), 4 000 → B."""
    article = await make_expense_article(session)
    operation = await _bank_fixture(session, inn=inn, amount="12000.00")
    await apply_operation_split(
        session,
        operation,
        splits=[
            OperationSplitLine(article_id=article.id, amount=D("8000.00"), counterparty_id=a_id),
            OperationSplitLine(article_id=article.id, amount=D("4000.00"), counterparty_id=b_id),
        ],
    )
    return operation


async def _pay_loan(
    factory: async_sessionmaker[AsyncSession], loan_id: uuid.UUID, op_id: uuid.UUID, amount: str
) -> None:
    from app.services.barter_loan_money import pay_payable_loan_with_money

    async with factory() as session:
        await pay_payable_loan_with_money(
            session,
            loan_id=loan_id,
            operation_date=PAID_ON,
            amount=D(amount),
            bank_operation_id=op_id,
        )


# --- находка 1: заём помечен проводкой доли ---------------------------------------------------


async def test_loan_on_non_anchor_share_then_exclude_reopens_the_loan(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A 8 000 (якорь) + B 4 000; заём B 3 000 этой операцией → A 8 000, B 1 000. Потом
    «Исключить операцию»: заём снова открыт, аллокации сняты, авансов нет, ДЗ 0."""
    async with async_session_factory() as session:
        a = await make_counterparty(session, name="A-r2-1", inn="7704000001")
        b = await make_counterparty(session, name="B-r2-1", inn="7704000002")
        operation = await _split_two(session, inn="7704000001", a_id=a.id, b_id=b.id)
        await session.commit()
        loan = await _their_loan(session, b.id, amount="3000.00")
        await session.commit()
        a_id, b_id, loan_id, op_id = a.id, b.id, loan.id, operation.id
    await _pay_loan(async_session_factory, loan_id, op_id, "3000.00")
    async with async_session_factory() as session:
        loan = await session.get(SupplierInvoice, loan_id)
        [alloc] = await _allocations(session, loan_id)
        share_b = await session.scalar(
            select(SupplierPrepayment.cashflow_transaction_id).where(
                SupplierPrepayment.counterparty_id == b_id
            )
        )
        got = (
            await _adv(session, a_id),
            await _adv(session, b_id),
            loan.barter_return_status,
            alloc.cashflow_transaction_id == share_b and alloc.bank_operation_id == op_id,
        )
        assert got == ((D("8000.00"), ZERO), (D("1000.00"), ZERO), "returned", True), got

        operation = await session.get(BankOperation, op_id)
        await apply_operation_action(session, operation, action="exclude")
        await session.commit()
        loan = await session.get(SupplierInvoice, loan_id)
        got = (
            loan.barter_return_status,
            loan.payment_status,
            await _allocations(session, loan_id),
            await _advances(session, a_id),
            await _advances(session, b_id),
            await _ledger_receivable(session, a_id) + await _ledger_receivable(session, b_id),
        )
        assert got == ("open", "unpaid", [], [], [], ZERO), got


async def test_loan_on_the_anchor_share_single_untouched_advance_no_double_reduction(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Один контрагент, одна проводка (якорь), аванс нетронут 12 000; заём 3 000 → аванс 9 000
    (release + пересборка якоря дают одно и то же, не 6 000). Исключение — всё снято."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Якорь-r2-2", inn="7704000003")
        operation = await _statement_payment(session, counterparty_id=cp.id, inn="7704000003")
        await session.commit()
        loan = await _their_loan(session, cp.id, amount="3000.00")
        await session.commit()
        cp_id, loan_id, op_id = cp.id, loan.id, operation.id
    await _pay_loan(async_session_factory, loan_id, op_id, "3000.00")
    async with async_session_factory() as session:
        [alloc] = await _allocations(session, loan_id)
        operation = await session.get(BankOperation, op_id)
        got = (
            await _adv(session, cp_id),
            await _ledger_receivable(session, cp_id),
            await _balance(session, cp_id, date(2026, 8, 31)),
            alloc.cashflow_transaction_id == operation.cashflow_transaction_id,
        )
        assert got == ((D("9000.00"), ZERO), D("9000.00"), (D("9000.00"), ZERO), True), got
        # Повторная классификация (та же статья, тот же контрагент) — пересборка якоря ещё раз.
        article = await make_expense_article(session)
        await apply_operation_action(
            session, operation, action="set_article", article_id=article.id, counterparty_id=cp_id
        )
        await session.commit()
        assert await _adv(session, cp_id) == (D("9000.00"), ZERO)
        loan = await session.get(SupplierInvoice, loan_id)
        assert loan.barter_return_status == "returned"

        await apply_operation_action(session, operation, action="exclude")
        await session.commit()
        loan = await session.get(SupplierInvoice, loan_id)
        got = (
            loan.barter_return_status,
            await _allocations(session, loan_id),
            await _advances(session, cp_id),
        )
        assert got == ("open", [], []), got


async def test_loan_on_the_anchor_share_of_a_two_counterparty_split(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A 8 000 (якорь) + B 4 000; заём A 3 000 → A 5 000 (один раз), B 4 000 не тронут."""
    async with async_session_factory() as session:
        a = await make_counterparty(session, name="A-r2-3", inn="7704000004")
        b = await make_counterparty(session, name="B-r2-3", inn="7704000005")
        operation = await _split_two(session, inn="7704000004", a_id=a.id, b_id=b.id)
        await session.commit()
        loan = await _their_loan(session, a.id, amount="3000.00")
        await session.commit()
        a_id, b_id, loan_id, op_id = a.id, b.id, loan.id, operation.id
    await _pay_loan(async_session_factory, loan_id, op_id, "3000.00")
    async with async_session_factory() as session:
        got = (
            await _adv(session, a_id),
            await _adv(session, b_id),
            await _balance(session, a_id, date(2026, 8, 31)),
            await _balance(session, b_id, date(2026, 8, 31)),
        )
        assert got == (
            (D("5000.00"), ZERO),
            (D("4000.00"), ZERO),
            (D("5000.00"), ZERO),
            (D("4000.00"), ZERO),
        ), got


async def test_open_bill_is_settled_by_the_rebuild_next_to_the_loan(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Аванс 12 000, открытый счёт 5 000 того же поставщика, заём 3 000 оплачен операцией.
    Пересборка якоря после займа гасит счёт правилом 1 (cash-зачёт проводкой), сверка займа
    помечена той же проводкой: бюджет = 3 000 + 5 000, аванс 9 000 = 5 000 денег счёта +
    4 000 свободных, своей ДЗ у счёта нет, баланс ДЗ 9 000. (Сверка/pay-split со счётом после
    займа отказывают «операция уже использована» — как на main.)"""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Заём+счёт-r2", inn="7704000006")
        operation = await _statement_payment(session, counterparty_id=cp.id, inn="7704000006")
        bill = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="5000.00",
            number="СЧ-r2",
            doc_kind="bill",
            operational_scope="finance",
            invoice_date=date(2026, 8, 1),
        )
        await session.commit()
        loan = await _their_loan(session, cp.id, amount="3000.00")
        await session.commit()
        cp_id, bill_id, op_id, loan_id = cp.id, bill.id, operation.id, loan.id
    await _pay_loan(async_session_factory, loan_id, op_id, "3000.00")
    async with async_session_factory() as session:
        bill_dz = await session.scalar(
            select(SupplierPrepayment).where(SupplierPrepayment.bill_invoice_id == bill_id)
        )
        bill = await session.get(SupplierInvoice, bill_id)
        bill_allocs = await _allocations(session, bill_id)
        got = (
            await _adv(session, cp_id),
            bill_dz is None,
            bill.payment_status,
            [(a.source_kind, a.amount) for a in bill_allocs],
            await _ledger_receivable(session, cp_id),
            await _balance(session, cp_id, date(2026, 8, 31)),
        )
        assert got == (
            (D("9000.00"), ZERO),
            True,
            "paid",
            [("cash", D("5000.00"))],
            D("9000.00"),
            (D("9000.00"), ZERO),
        ), got


# --- находка 2: переразбор снимает зачёты двери до проверки остатков ---------------------------


async def _seed_door_paid(
    factory: async_sessionmaker[AsyncSession], *, inn: str
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with factory() as session:
        cp = await make_counterparty(session, name=f"Дверь-{inn}", inn=inn)
        operation = await _statement_payment(session, counterparty_id=cp.id, inn=inn)
        invoice = await _warehouse_invoice(session, counterparty_id=cp.id)
        await session.commit()
        ids = (cp.id, operation.id, invoice.id)
    await _confirm(factory, ids[2], ids[1])
    return ids


async def test_resplit_with_the_same_invoice_pays_it_by_the_share(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    cp_id, op_id, invoice_id = await _seed_door_paid(async_session_factory, inn="7704000007")
    async with async_session_factory() as session:
        operation = await session.get(BankOperation, op_id)
        article = await make_expense_article(session)
        await apply_operation_split(
            session,
            operation,
            splits=[
                OperationSplitLine(
                    article_id=article.id, amount=D("12000.00"), invoice_id=invoice_id
                )
            ],
            counterparty_id=cp_id,
        )
        await session.commit()
        invoice = await session.get(SupplierInvoice, invoice_id)
        allocs = await _allocations(session, invoice_id)
        got = (
            invoice.payment_status,
            [(a.source_kind, a.amount, a.cashflow_transaction_id is not None) for a in allocs],
            await _advances(session, cp_id),
            await _balance(session, cp_id, date(2026, 8, 31)),
            await _balance(session, cp_id, date(2026, 7, 31)),
        )
        assert got == ("paid", [("bank", D("12000.00"), True)], [], (ZERO, ZERO), (ZERO, ZERO)), got


async def test_resplit_with_another_invoice_moves_the_payment(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Дверь оплатила X; переразбор называет Y (тот же поставщик). X — открытая КЗ 12 000,
    Y оплачена долей, аванса нет, ДЗ 0."""
    cp_id, op_id, x_id = await _seed_door_paid(async_session_factory, inn="7704000008")
    async with async_session_factory() as session:
        y = await _warehouse_invoice(session, counterparty_id=cp_id, number="ТН-Y")
        await session.commit()
        y_id = y.id
        operation = await session.get(BankOperation, op_id)
        article = await make_expense_article(session)
        await apply_operation_split(
            session,
            operation,
            splits=[
                OperationSplitLine(article_id=article.id, amount=D("12000.00"), invoice_id=y_id)
            ],
            counterparty_id=cp_id,
        )
        await session.commit()
        x = await session.get(SupplierInvoice, x_id)
        y = await session.get(SupplierInvoice, y_id)
        got = (
            x.payment_status,
            await _allocations(session, x_id),
            y.payment_status,
            await _advances(session, cp_id),
            await _ledger_receivable(session, cp_id),
            await _balance(session, cp_id, date(2026, 8, 31)),
        )
        assert got == ("unpaid", [], "paid", [], ZERO, (ZERO, D("12000.00"))), got


async def test_resplit_refuses_when_the_door_paid_invoice_is_in_a_draft_and_writes_nothing(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Акт в черновике закрыт зачётом двери (сверка с черновиком). Переразбор — отказ, после
    отката всё как было: акт оплачен, аванс тронут на 4 000."""
    from app.services.counterparty_matching import allocate_bank_operation_to_draft

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Черновик-r2", inn="7704000009")
        act = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="4000.00",
            number="АКТ-r2",
            operational_scope="finance",
            invoice_date=date(2026, 8, 1),
        )
        draft = await make_draft(session, counterparty_id=cp.id, amount="4000.00")
        act.draft_id = draft.id
        operation = await _statement_payment(
            session, counterparty_id=cp.id, inn="7704000009", amount="10000.00"
        )
        await session.commit()
        await allocate_bank_operation_to_draft(
            session, bank_operation_id=operation.id, draft_id=draft.id
        )
        cp_id, act_id, op_id = cp.id, act.id, operation.id
    async with async_session_factory() as session:
        assert await _adv(session, cp_id) == (D("10000.00"), D("4000.00"))
        operation = await session.get(BankOperation, op_id)
        article = await make_expense_article(session)
        with pytest.raises(ValueError, match="черновик"):
            await apply_operation_split(
                session,
                operation,
                splits=[OperationSplitLine(article_id=article.id, amount=D("10000.00"))],
                counterparty_id=cp_id,
            )
        await session.rollback()
    async with async_session_factory() as session:
        act = await session.get(SupplierInvoice, act_id)
        got = (
            act.payment_status,
            await _adv(session, cp_id),
            len(await _allocations(session, act_id)),
            await _ledger_receivable(session, cp_id),
        )
        assert got == ("paid", (D("10000.00"), D("4000.00")), 1, D("6000.00")), got


async def test_resplit_into_two_counterparties_after_the_door(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Дверь оплатила накладную X 12 000. Переразбор: 8 000 → X с её накладной, 4 000 → B
    свободно. X: накладная частично оплачена 8 000 долей, аванса нет; B: аванс 4 000."""
    cp_id, op_id, invoice_id = await _seed_door_paid(async_session_factory, inn="7704000010")
    async with async_session_factory() as session:
        b = await make_counterparty(session, name="B-r2-8", inn="7704000011")
        operation = await session.get(BankOperation, op_id)
        article = await make_expense_article(session)
        await apply_operation_split(
            session,
            operation,
            splits=[
                OperationSplitLine(
                    article_id=article.id,
                    amount=D("8000.00"),
                    invoice_id=invoice_id,
                    counterparty_id=cp_id,
                ),
                OperationSplitLine(
                    article_id=article.id, amount=D("4000.00"), counterparty_id=b.id
                ),
            ],
        )
        await session.commit()
        invoice = await session.get(SupplierInvoice, invoice_id)
        got = (
            invoice.payment_status,
            await _advances(session, cp_id),
            await _adv(session, b.id),
            await _balance(session, cp_id, date(2026, 8, 31)),
            await _balance(session, b.id, date(2026, 8, 31)),
        )
        assert got == (
            "partially_paid",
            [],
            (D("4000.00"), ZERO),
            (ZERO, D("4000.00")),
            (D("4000.00"), ZERO),
        ), got


async def test_resplit_free_lines_after_the_door_rebuilds_one_advance(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Дверь, потом переразбор без накладной (две статьи): накладная не оплачена, аванс один на
    долю, зачётов-сирот нет, ДЗ 12 000 = КЗ 12 000."""
    cp_id, op_id, invoice_id = await _seed_door_paid(async_session_factory, inn="7704000012")
    async with async_session_factory() as session:
        operation = await session.get(BankOperation, op_id)
        article = await make_expense_article(session)
        other = await make_expense_article(session, code="hoz_r2", name="Хоз-r2")
        await apply_operation_split(
            session,
            operation,
            splits=[
                OperationSplitLine(article_id=article.id, amount=D("9000.00")),
                OperationSplitLine(article_id=other.id, amount=D("3000.00")),
            ],
            counterparty_id=cp_id,
        )
        await session.commit()
        invoice = await session.get(SupplierInvoice, invoice_id)
        orphans = (
            await session.scalars(
                select(SupplierPrepayment).where(
                    SupplierPrepayment.cashflow_transaction_id.is_(None)
                )
            )
        ).all()
        settled_allocs = (
            await session.scalars(
                select(InvoicePaymentAllocation).where(
                    InvoicePaymentAllocation.source_kind == "prepayment"
                )
            )
        ).all()
        got = (
            invoice.payment_status,
            [a.amount for a in await _advances(session, cp_id)],
            len(orphans),
            len(settled_allocs),
            await _balance(session, cp_id, date(2026, 8, 31)),
        )
        assert got == (
            "unpaid",
            [D("9000.00"), D("3000.00")],
            0,
            0,
            (D("12000.00"), D("12000.00")),
        ), got

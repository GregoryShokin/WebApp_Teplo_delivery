"""Пробы скептика (Fable, 25.09): двери прямой оплаты и аванс правила 1 — края.

Каждая проба утверждает идеальные цифры (деньги числятся ровно один раз, ДЗ/КЗ на даты по
канону). На main красные 11 из 13. Две пробы (заём на неякорной доле мультисплита, переразбор
с той же накладной) были регрессиями первой версии ветки. Основной путь — в
``test_payment_match_rule1_advance``.

Не вошли три пробы, красные и после починки: две доли одного контрагента (диалог закрывает
накладную только авансом первой доли — цифры честные, остаток гасится «из предоплаты»),
перевес платежа, чей аванс тронут и УПД, и дверью (тронутый аванс остаётся у прежнего
контрагента — так и на main), исключение операции при закрытом месяце документа (замок
исключения смотрит на месяц операции — так и на main).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

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
    DdsArticle,
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
from app.services.supplier_prepayments import (
    RULE1_PREPAYMENT_KIND,
    SUPPLIER_REFUND_ARTICLE_CODE,
    apply_closing_document,
)

BASE = "/api/v1/warehouse"
PAID_ON = date(2026, 7, 31)
DOC_ON = date(2026, 8, 1)
POSTED = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
ISSUED = datetime(2026, 8, 1, 7, 0, tzinfo=UTC)
ZERO = Decimal("0.00")
D = Decimal


def _run(coro):
    return asyncio.run(coro)


async def _bank_fixture(
    session: AsyncSession,
    *,
    inn: str,
    amount: str,
    direction: str = "out",
    operation_date: date = PAID_ON,
    posted_at: datetime = POSTED,
) -> BankOperation:
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
        direction=direction,
        operation_date=operation_date,
        posted_at=posted_at,
        account_id=account.id,
    )


async def _statement_payment(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    inn: str,
    amount: str = "12000.00",
    operation_date: date = PAID_ON,
    posted_at: datetime = POSTED,
) -> BankOperation:
    article = await make_expense_article(session)
    operation = await _bank_fixture(
        session, inn=inn, amount=amount, operation_date=operation_date, posted_at=posted_at
    )
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
    invoice_date: date = DOC_ON,
    issued_at: datetime = ISSUED,
) -> SupplierInvoice:
    return await make_invoice(
        session,
        counterparty_id=counterparty_id,
        amount=amount,
        number=number,
        operational_scope="warehouse",
        invoice_date=invoice_date,
        issued_at=issued_at,
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


async def _advance(session: AsyncSession, counterparty_id: uuid.UUID) -> SupplierPrepayment | None:
    rows = await _advances(session, counterparty_id)
    assert len(rows) <= 1, [(r.amount, r.amount_settled, r.status) for r in rows]
    return rows[0] if rows else None


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


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    *,
    inn: str,
    invoice_amount: str = "12000.00",
    invoice_date: date = DOC_ON,
    issued_at: datetime = ISSUED,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with factory() as session:
        cp = await make_counterparty(session, name=f"Поставщик-{inn}", inn=inn)
        operation = await _statement_payment(session, counterparty_id=cp.id, inn=inn)
        invoice = await _warehouse_invoice(
            session,
            counterparty_id=cp.id,
            amount=invoice_amount,
            invoice_date=invoice_date,
            issued_at=issued_at,
        )
        await session.commit()
        advance = await _advance(session, cp.id)
        assert advance is not None and advance.amount == D("12000.00")
        return cp.id, operation.id, invoice.id


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


async def _product(session: AsyncSession) -> IikoProduct:
    product = IikoProduct(
        iiko_id=str(uuid.uuid4()),
        name="Моцарелла",
        type="GOODS",
        unit="кг",
        synced_at=datetime.now(UTC),
    )
    session.add(product)
    await session.flush()
    return product


async def _their_loan(
    session: AsyncSession, counterparty_id: uuid.UUID, *, amount: str
) -> SupplierInvoice:
    from app.services.warehouse_invoices import LineInput, create_warehouse_invoice

    product = await _product(session)
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


# --- P1. Мультисплит на двух контрагентов + дверь займа на НЕякорной доле ---------------------


async def test_probe_multisplit_loan_on_non_anchor_share_reduces_only_that_share(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Операция 12 000 разобрана: 8 000 → A (якорная доля), 4 000 → B. Их заём B на 3 000
    оплачен этой операцией. Деньги: 12 000 − 3 000 = 9 000 дебиторки: A 8 000, B 1 000."""
    from app.services.barter_loan_money import pay_payable_loan_with_money

    async with async_session_factory() as session:
        a = await make_counterparty(session, name="Якорь-A", inn="7703000001")
        b = await make_counterparty(session, name="Доля-B", inn="7703000002")
        article = await make_expense_article(session)
        operation = await _bank_fixture(session, inn="7703000001", amount="12000.00")
        await apply_operation_split(
            session,
            operation,
            splits=[
                OperationSplitLine(
                    article_id=article.id, amount=D("8000.00"), counterparty_id=a.id
                ),
                OperationSplitLine(
                    article_id=article.id, amount=D("4000.00"), counterparty_id=b.id
                ),
            ],
        )
        await session.commit()
        assert (await _advance(session, a.id)).amount == D("8000.00")
        assert (await _advance(session, b.id)).amount == D("4000.00")
        loan = await _their_loan(session, b.id, amount="3000.00")
        await session.commit()
        a_id, b_id, loan_id, op_id = a.id, b.id, loan.id, operation.id

    async with async_session_factory() as session:
        await pay_payable_loan_with_money(
            session,
            loan_id=loan_id,
            operation_date=PAID_ON,
            amount=D("3000.00"),
            bank_operation_id=op_id,
        )

    async with async_session_factory() as session:
        adv_a = await _advance(session, a_id)
        adv_b = await _advance(session, b_id)
        got = (
            adv_a.amount if adv_a else ZERO,
            adv_b.amount if adv_b else ZERO,
            await _ledger_receivable(session, a_id) + await _ledger_receivable(session, b_id),
        )
        assert got == (D("8000.00"), D("1000.00"), D("9000.00")), (
            f"(аванс A, аванс B, ДЗ всего) = {got}; заём 3 000 оплачен из доли B, "
            "доля A его денег не давала"
        )


# --- P2. Переразбор операции с указанием той же накладной, что оплачена дверью -----------------


async def test_probe_resplit_naming_the_invoice_already_paid_by_the_door(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Накладная оплачена в диалоге «Оплатить» этой операцией. Потом операцию разносят по
    строкам с привязкой той же накладной (invoice_id) — штатный путь «Разобрать операцию».
    Ожидание: переразбор проходит, накладная оплачена долей, второй дебиторки нет."""
    cp_id, op_id, invoice_id = await _seed(async_session_factory, inn="7703000003")
    await _confirm(async_session_factory, invoice_id, op_id)
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
        assert invoice.payment_status == "paid"
        assert await _ledger_receivable(session, cp_id) == ZERO
        assert await _balance(session, cp_id, date(2026, 8, 31)) == (ZERO, ZERO)


# --- P4. Черновик: счёт + закрывающий на одни деньги -----------------------------------------


async def test_probe_draft_with_bill_and_closing_shares_the_advance(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Платёж 10 000 стал авансом. Черновик: счёт 6 000 (01.08) + УПД 4 000 (02.08).
    Итог: УПД закрыт зачётом, счёт оплачен, ДЗ = 6 000 (деньги счёта ждут закрывающего)."""
    from app.services.counterparty_matching import allocate_bank_operation_to_draft

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Черновик-счёт-УПД", inn="7703000005")
        bill = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="6000.00",
            number="СЧ-1",
            doc_kind="bill",
            operational_scope="finance",
            invoice_date=date(2026, 8, 1),
        )
        upd = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="4000.00",
            number="УПД-1",
            operational_scope="finance",
            invoice_date=date(2026, 8, 2),
        )
        draft = await make_draft(session, counterparty_id=cp.id, amount="10000.00")
        bill.draft_id = draft.id
        upd.draft_id = draft.id
        operation = await _statement_payment(
            session, counterparty_id=cp.id, inn="7703000005", amount="10000.00"
        )
        await session.commit()
        await allocate_bank_operation_to_draft(
            session, bank_operation_id=operation.id, draft_id=draft.id
        )
        cp_id, bill_id, upd_id = cp.id, bill.id, upd.id

    async with async_session_factory() as session:
        bill = await session.get(SupplierInvoice, bill_id)
        upd = await session.get(SupplierInvoice, upd_id)
        got = (
            bill.payment_status,
            upd.payment_status,
            await _ledger_receivable(session, cp_id),
            await _balance(session, cp_id, date(2026, 7, 31)),
            await _balance(session, cp_id, date(2026, 8, 31)),
        )
        assert got == ("paid", "paid", D("6000.00"), (D("10000.00"), ZERO), (D("6000.00"), ZERO)), (
            got
        )


# --- P6. Правка ОПЛАЧЕННОЙ накладной вниз после двери -------------------------------------------


async def test_probe_adjusting_a_door_paid_invoice_down_reopens_the_advance(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Накладная 12 000 оплачена дверью, потом исправлена до 10 000: излишек 2 000 — дебиторка,
    ровно один раз (в том же авансе, без второй записи)."""
    from app.services.warehouse_invoices import LineInput, adjust_paid_invoice

    cp_id, op_id, invoice_id = await _seed(async_session_factory, inn="7703000008")
    await _confirm(async_session_factory, invoice_id, op_id)
    async with async_session_factory() as session:
        product = await _product(session)
        invoice = await session.get(SupplierInvoice, invoice_id)
        await adjust_paid_invoice(
            session,
            invoice,
            lines=[
                LineInput(
                    name="Товар", quantity=D("10"), price=D("1000.00"), iiko_product_id=product.id
                )
            ],
        )
        await session.commit()
        got = (
            invoice.payment_status,
            await _ledger_receivable(session, cp_id),
            await _balance(session, cp_id, date(2026, 7, 31)),
            await _balance(session, cp_id, date(2026, 8, 31)),
            len(
                (
                    await session.scalars(
                        select(SupplierPrepayment).where(
                            SupplierPrepayment.counterparty_id == cp_id
                        )
                    )
                ).all()
            ),
        )
        assert got == ("paid", D("2000.00"), (D("12000.00"), ZERO), (D("2000.00"), ZERO), 1), got


# --- P7. Документ ДО денег: накладная 25.07, платёж 31.07 -------------------------------------


async def test_probe_document_before_money_is_payable_until_the_money_date(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Поставка 25.07, оплата 31.07 (обычный порядок). Дверь: 28.07 — КЗ 12 000; с 31.07 — 0/0."""
    cp_id, op_id, invoice_id = await _seed(
        async_session_factory,
        inn="7703000009",
        invoice_date=date(2026, 7, 25),
        issued_at=datetime(2026, 7, 25, 9, 0, tzinfo=UTC),
    )
    await _confirm(async_session_factory, invoice_id, op_id)
    async with async_session_factory() as session:
        got = (
            await _balance(session, cp_id, date(2026, 7, 28)),
            await _balance(session, cp_id, date(2026, 7, 31)),
            await _balance(session, cp_id, date(2026, 8, 31)),
            await _ledger_receivable(session, cp_id),
        )
        assert got == ((ZERO, D("12000.00")), (ZERO, ZERO), (ZERO, ZERO), ZERO), got


# --- P8. Частично возвращённый аванс ---------------------------------------------------------


async def test_probe_partially_refunded_advance_pays_only_its_rest(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Поставщик вернул 4 000 из 12 000 (возвратная строка выписки). Накладная 12 000 этой
    операцией: оплачено 8 000, остаток 4 000 — честная КЗ; ДЗ 0."""
    from app.services.counterparty_bank_match import confirm_invoice_match

    cp_id, op_id, invoice_id = await _seed(async_session_factory, inn="7703000010")
    async with async_session_factory() as session:
        refund_article = await session.scalar(
            select(DdsArticle).where(DdsArticle.code == SUPPLIER_REFUND_ARTICLE_CODE)
        )
        if refund_article is None:
            refund_article = DdsArticle(
                code=SUPPLIER_REFUND_ARTICLE_CODE,
                name="Возврат переплаты",
                movement_type="inflow",
                activity_type="operating",
            )
            session.add(refund_article)
            await session.flush()
        refund = await _bank_fixture(
            session,
            inn="7703000010",
            amount="4000.00",
            direction="in",
            operation_date=date(2026, 8, 1),
            posted_at=datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
        )
        await apply_operation_action(
            session,
            refund,
            action="set_article",
            article_id=refund_article.id,
            counterparty_id=cp_id,
        )
        await session.commit()
        adv = await _advance(session, cp_id)
        assert (adv.amount_settled, adv.status) == (D("4000.00"), "partially_settled"), (
            adv.amount_settled,
            adv.status,
        )
        await confirm_invoice_match(
            session,
            invoice_id=invoice_id,
            bank_operation_id=op_id,
            enrich=False,
            actor_user_id=None,
        )
        invoice = await session.get(SupplierInvoice, invoice_id)
        got = (
            invoice.payment_status,
            sum(a.amount for a in await _allocations(session, invoice_id)),
            await _ledger_receivable(session, cp_id),
            await _balance(session, cp_id, date(2026, 8, 31)),
        )
        assert got == ("partially_paid", D("8000.00"), ZERO, (ZERO, D("4000.00"))), got


# --- P9. Одна операция, две двери: накладная зачётом + счёт аллокацией ------------------------


async def test_probe_one_operation_closing_by_door_then_bill_by_split(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Накладная 12 000 закрыта зачётом из аванса. Потом счёт 12 000 того же поставщика
    оплачивают той же операцией через pay-split (F2: счёту — незанятое). Потом приходит УПД
    12 000 по счёту. Деньги 12 000, поставок на 24 000 → КЗ 12 000, ДЗ 0."""
    from app.services.warehouse_payments import BankPart, pay_invoice_split

    cp_id, op_id, invoice_id = await _seed(async_session_factory, inn="7703000011")
    await _confirm(async_session_factory, invoice_id, op_id)
    async with async_session_factory() as session:
        bill = await make_invoice(
            session,
            counterparty_id=cp_id,
            amount="12000.00",
            number="СЧ-2",
            doc_kind="bill",
            operational_scope="finance",
            invoice_date=date(2026, 8, 3),
        )
        await session.commit()
        bill_id = bill.id
        await pay_invoice_split(
            session, invoice_id=bill_id, bank_parts=[BankPart(bank_operation_id=op_id, amount=None)]
        )
    async with async_session_factory() as session:
        upd = await make_invoice(
            session,
            counterparty_id=cp_id,
            amount="12000.00",
            number="УПД-2",
            operational_scope="finance",
            invoice_date=date(2026, 8, 10),
        )
        await apply_closing_document(session, upd, as_of=date(2026, 8, 10))
        await session.commit()
        bill = await session.get(SupplierInvoice, bill_id)
        got = (
            bill.payment_status,
            upd.payment_status,
            await _ledger_receivable(session, cp_id),
            await _balance(session, cp_id, date(2026, 8, 31)),
        )
        assert got[2] == ZERO and got[3] == (ZERO, D("12000.00")), got


# --- P11. Сплит: банковская часть (дверь) + наличная часть ------------------------------------


async def test_probe_pay_split_bank_door_plus_cash_part(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Накладная 15 000: 12 000 операцией (аванс) + 3 000 наличными из Сейфа. Деньги 15 000,
    документ 15 000 → 0/0, аванс не тронут сверх зачёта, второй дебиторки от наличных нет."""
    from app.services.warehouse_payments import BankPart, CashPart, pay_invoice_split

    cp_id, op_id, invoice_id = await _seed(
        async_session_factory, inn="7703000013", invoice_amount="15000.00"
    )
    async with async_session_factory() as session:
        safe = await make_wallet(session, name="Сейф", wallet_type="cash_safe")
        article = await make_expense_article(session)
        await session.commit()
        await pay_invoice_split(
            session,
            invoice_id=invoice_id,
            bank_parts=[BankPart(bank_operation_id=op_id, amount=None)],
            cash_parts=[
                CashPart(
                    wallet_id=safe.id,
                    amount=D("3000.00"),
                    operation_date=date(2026, 8, 2),
                    article_id=article.id,
                    comment=None,
                )
            ],
        )
        invoice = await session.get(SupplierInvoice, invoice_id)
        got = (
            invoice.payment_status,
            await _ledger_receivable(session, cp_id),
            await _balance(session, cp_id, date(2026, 7, 31)),
            await _balance(session, cp_id, date(2026, 8, 1)),
            await _balance(session, cp_id, date(2026, 8, 31)),
        )
        assert got == ("paid", ZERO, (D("12000.00"), ZERO), (ZERO, D("3000.00")), (ZERO, ZERO)), got


# --- P12. Дверь займа после частичного зачёта дверью (якорная доля) ---------------------------


async def test_probe_loan_after_partial_door_settlement_on_the_anchor(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Аванс 12 000: накладная 7 000 дверью, потом их заём 3 000 этой же операцией.
    ДЗ = 12 000 − 7 000 − 3 000 = 2 000."""
    from app.services.barter_loan_money import pay_payable_loan_with_money

    cp_id, op_id, invoice_id = await _seed(
        async_session_factory, inn="7703000014", invoice_amount="7000.00"
    )
    await _confirm(async_session_factory, invoice_id, op_id)
    async with async_session_factory() as session:
        loan = await _their_loan(session, cp_id, amount="3000.00")
        await session.commit()
        loan_id = loan.id
    async with async_session_factory() as session:
        await pay_payable_loan_with_money(
            session,
            loan_id=loan_id,
            operation_date=PAID_ON,
            amount=D("3000.00"),
            bank_operation_id=op_id,
        )
    async with async_session_factory() as session:
        adv = await _advance(session, cp_id)
        got = (
            (adv.amount, adv.amount_settled) if adv else None,
            await _ledger_receivable(session, cp_id),
            await _balance(session, cp_id, date(2026, 8, 31)),
        )
        assert got[1] == D("2000.00") and got[2] == (D("2000.00"), ZERO), got


# --- P13. Мультисплит на двух контрагентов: дверь на накладную НЕякорной доли ------------------


async def test_probe_multisplit_door_on_non_anchor_share(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Операция 12 000: 8 000 → A (якорь), 4 000 → B. Накладная B 4 000 оплачена этой операцией
    в диалоге. Идеал: аванс B закрыт, аванс A нетронут (8 000), ДЗ всего 8 000."""
    async with async_session_factory() as session:
        a = await make_counterparty(session, name="Якорь-A2", inn="7703000015")
        b = await make_counterparty(session, name="Доля-B2", inn="7703000016")
        article = await make_expense_article(session)
        operation = await _bank_fixture(session, inn="7703000015", amount="12000.00")
        await apply_operation_split(
            session,
            operation,
            splits=[
                OperationSplitLine(
                    article_id=article.id, amount=D("8000.00"), counterparty_id=a.id
                ),
                OperationSplitLine(
                    article_id=article.id, amount=D("4000.00"), counterparty_id=b.id
                ),
            ],
        )
        invoice = await _warehouse_invoice(session, counterparty_id=b.id, amount="4000.00")
        await session.commit()
        a_id, b_id, op_id, invoice_id = a.id, b.id, operation.id, invoice.id
    await _confirm(async_session_factory, invoice_id, op_id)
    async with async_session_factory() as session:
        # Повторная классификация якорной доли (та же статья, тот же контрагент) — пересборка A.
        operation = await session.get(BankOperation, op_id)
        article = await make_expense_article(session)
        anchor = await session.get(SupplierInvoice, invoice_id)
        assert anchor.payment_status == "paid"
        got = (
            await _ledger_receivable(session, a_id),
            await _ledger_receivable(session, b_id),
            await _balance(session, a_id, date(2026, 8, 31)),
            await _balance(session, b_id, date(2026, 8, 31)),
        )
        assert got == (D("8000.00"), ZERO, (D("8000.00"), ZERO), (ZERO, ZERO)), got


# --- P14. Исключение и возврат из исключения, потом дверь заново -------------------------------


async def test_probe_exclude_then_revive_then_pay_again(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Дверь → «Исключить» → снова разметить (та же статья/контрагент) → дверь. После возврата
    из исключения: аванс 12 000, накладная не оплачена; после второй двери: 0/0."""
    cp_id, op_id, invoice_id = await _seed(async_session_factory, inn="7703000017")
    await _confirm(async_session_factory, invoice_id, op_id)
    async with async_session_factory() as session:
        operation = await session.get(BankOperation, op_id)
        article = await make_expense_article(session)
        await apply_operation_action(session, operation, action="exclude")
        await session.commit()
        await apply_operation_action(
            session, operation, action="set_article", article_id=article.id, counterparty_id=cp_id
        )
        await session.commit()
        invoice = await session.get(SupplierInvoice, invoice_id)
        mid = (
            invoice.payment_status,
            await _ledger_receivable(session, cp_id),
            await _balance(session, cp_id, date(2026, 8, 31)),
            len(await _advances(session, cp_id)),
        )
        assert mid == ("unpaid", D("12000.00"), (D("12000.00"), D("12000.00")), 1), mid
    await _confirm(async_session_factory, invoice_id, op_id)
    async with async_session_factory() as session:
        invoice = await session.get(SupplierInvoice, invoice_id)
        got = (
            invoice.payment_status,
            await _ledger_receivable(session, cp_id),
            await _balance(session, cp_id, date(2026, 7, 31)),
            await _balance(session, cp_id, date(2026, 8, 31)),
        )
        assert got == ("paid", ZERO, (D("12000.00"), ZERO), (ZERO, ZERO)), got


# --- P15. Снятие контрагента с платежа после двери -------------------------------------------


async def test_probe_unsetting_the_counterparty_after_the_door(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Дверь, потом с операции сняли контрагента (статья без контрагента). Решение «этот платёж
    оплатил эту накладную» держится сверкой; аванса нет; ДЗ 0."""
    cp_id, op_id, invoice_id = await _seed(async_session_factory, inn="7703000018")
    await _confirm(async_session_factory, invoice_id, op_id)
    async with async_session_factory() as session:
        operation = await session.get(BankOperation, op_id)
        article = await make_expense_article(session)
        await apply_operation_action(
            session, operation, action="set_article", article_id=article.id, counterparty_id=None
        )
        await session.commit()
        invoice = await session.get(SupplierInvoice, invoice_id)
        allocs = await _allocations(session, invoice_id)
        got = (
            invoice.payment_status,
            [(a.source_kind, a.amount) for a in allocs],
            await _ledger_receivable(session, cp_id),
            len(await _advances(session, cp_id)),
            await _balance(session, cp_id, date(2026, 8, 31)),
        )
        assert got == ("paid", [("bank", D("12000.00"))], ZERO, 0, (ZERO, ZERO)), got


# --- P16. Смена статьи (тот же контрагент) после двери ---------------------------------------


async def test_probe_changing_the_article_after_the_door_keeps_the_numbers(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    cp_id, op_id, invoice_id = await _seed(async_session_factory, inn="7703000019")
    await _confirm(async_session_factory, invoice_id, op_id)
    async with async_session_factory() as session:
        operation = await session.get(BankOperation, op_id)
        other = await make_expense_article(session, code="hoz_rashody2", name="Хозрасходы-2")
        await apply_operation_action(
            session, operation, action="set_article", article_id=other.id, counterparty_id=cp_id
        )
        await session.commit()
        invoice = await session.get(SupplierInvoice, invoice_id)
        got = (
            invoice.payment_status,
            await _ledger_receivable(session, cp_id),
            await _balance(session, cp_id, date(2026, 7, 31)),
            await _balance(session, cp_id, date(2026, 8, 31)),
        )
        assert got == ("paid", ZERO, (D("12000.00"), ZERO), (ZERO, ZERO)), got

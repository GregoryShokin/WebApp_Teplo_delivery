"""Денежное гашение бартерных займов — целевой процесс владельца (18.07.2026).

Долг займа номинирован ТОВАРОМ: возврат товаром идёт по ИСХОДНОЙ цене выдачи (300, даже если
рынок 350), гашение деньгами НАШЕГО займа — по ТЕКУЩЕЙ цене (350), «их» займа — по сумме
накладной («как обычную поставку»). Зачёт долга всегда по исходной цене за кг.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from cp_helpers import (
    make_bank_operation,
    make_counterparty,
    make_expense_article,
    make_wallet,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    BarterReturnLine,
    CashflowTransaction,
    Counterparty,
    CounterpartyAlias,
    IikoProduct,
    InvoiceLineItem,
    SupplierInvoice,
)
from app.services.barter_loan_money import (
    MoneyReturnLine,
    pay_payable_loan_with_money,
    settle_receivable_loan_with_money,
)
from app.services.warehouse_invoices import (
    LineInput,
    ReturnLineInput,
    WarehouseInvoiceError,
    create_barter_return,
    create_warehouse_invoice,
    loan_settled_value,
)

ISSUED = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
OP_DATE = date(2026, 7, 15)


async def _receivable(session: AsyncSession, counterparty_id: uuid.UUID) -> Decimal:
    """Открытая дебиторка контрагента — как её считает плитка дашборда."""
    from app.models import SupplierPrepayment

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


async def _product(session: AsyncSession, name: str = "Моцарелла") -> IikoProduct:
    product = IikoProduct(
        iiko_id=str(uuid.uuid4()), name=name, type="GOODS", unit="кг",
        synced_at=datetime.now(UTC),
    )
    session.add(product)
    await session.flush()
    return product


async def _make_loan(
    session: AsyncSession,
    *,
    inn: str,
    we_lend: bool,
    qty: str = "10",
    price: str = "300",
) -> tuple[SupplierInvoice, InvoiceLineItem]:
    """Заём 10 кг × 300 = 3000: мы выдаём (we_lend) или нам выдают."""
    cp = await make_counterparty(session, name=f"Бартер-{inn}", inn=inn)
    await make_expense_article(session, code="payment_to_supplier", name="Оплата поставщикам")
    await make_expense_article(
        session, code="vozvrat_pereplaty_ot_postavschikov",
        name="Возврат переплаты от поставщиков",
    )
    product = await _product(session)
    await session.commit()
    loan = await create_warehouse_invoice(
        session,
        counterparty_id=cp.id,
        issued_at=ISSUED,
        mode="loan",
        we_lend=we_lend,
        lines=[
            LineInput(
                name="Моцарелла", quantity=Decimal(qty), price=Decimal(price),
                iiko_product_id=product.id,
            )
        ],
    )
    line = (
        await session.scalars(
            select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == loan.id)
        )
    ).one()
    return loan, line


async def test_goods_return_uses_issue_price_not_market(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Правило владельца: выдали по 300, вернули товаром при рынке 350 → приход по 300."""
    async with async_session_factory() as session:
        loan, line = await _make_loan(session, inn="6155995001", we_lend=True)
        ret = await create_barter_return(
            session,
            loan_id=loan.id,
            issued_at=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
            returns=[ReturnLineInput(amount=Decimal("1200"), loan_line_item_id=line.id,
                                     quantity=Decimal("4"))],
        )
        assert ret.amount == Decimal("1200.00")  # 4 кг × 300 (исходная), не 350
        await session.refresh(loan)
        assert loan.barter_return_status == "partially_returned"
        assert await loan_settled_value(session, loan) == Decimal("1200.00")


async def test_receivable_loan_money_settlement_current_price_cash(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Наш заём 10 кг × 300; контрагент гасит 4 кг ДЕНЬГАМИ по текущей 350: в кассу приходит
    1400, долг уменьшается на 1200 (4 × 300) — остаток 1800."""
    async with async_session_factory() as session:
        loan, line = await _make_loan(session, inn="6155995002", we_lend=True)
        wallet = await make_wallet(session, name="Сейф-бартер", wallet_type="cash_safe")
        await session.commit()

        result = await settle_receivable_loan_with_money(
            session,
            loan_id=loan.id,
            operation_date=OP_DATE,
            lines=[MoneyReturnLine(loan_line_item_id=line.id, quantity=Decimal("4"),
                                   unit_price=Decimal("350"))],
            wallet_id=wallet.id,
        )

        assert result["money_received"] == 1400.0
        assert result["credited"] == 1200.0
        assert result["remaining"] == 1800.0
        tx = await session.get(
            CashflowTransaction, uuid.UUID(result["cashflow_transaction_id"])
        )
        assert tx is not None and tx.direction == "in"
        assert tx.amount == Decimal("1400.00")
        assert tx.wallet_id == wallet.id
        await session.refresh(loan)
        assert loan.barter_return_status == "partially_returned"

        # Догашивает остальные 6 кг по 320 → деньги 1920, зачёт 1800 → заём закрыт.
        result = await settle_receivable_loan_with_money(
            session,
            loan_id=loan.id,
            operation_date=OP_DATE,
            lines=[MoneyReturnLine(loan_line_item_id=line.id, quantity=Decimal("6"),
                                   unit_price=Decimal("320"))],
            wallet_id=wallet.id,
        )
        assert result["remaining"] == 0.0
        await session.refresh(loan)
        assert loan.barter_return_status == "returned"
        assert loan.payment_status == "paid"


async def test_receivable_loan_money_settlement_bank_operation(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Банковский канал: входящая операция ровно на сумму гашения привязывается, проводка
    получает контрагента и статью возврата, операция становится classified."""
    from cp_helpers import make_account

    async with async_session_factory() as session:
        loan, line = await _make_loan(session, inn="6155995003", we_lend=True)
        account = await make_account(session)
        await make_wallet(
            session, name="Банк-бартер", wallet_type="bank", account_id=account.id
        )
        operation = await make_bank_operation(
            session, amount="3500.00", direction="in", inn="6155995003",
            operation_date=OP_DATE, account_id=account.id,
        )
        await session.commit()

        result = await settle_receivable_loan_with_money(
            session,
            loan_id=loan.id,
            operation_date=OP_DATE,
            lines=[MoneyReturnLine(loan_line_item_id=line.id, quantity=Decimal("10"),
                                   unit_price=Decimal("350"))],
            bank_operation_id=operation.id,
        )

        assert result["money_received"] == 3500.0
        assert result["credited"] == 3000.0
        assert result["remaining"] == 0.0
        await session.refresh(operation)
        assert operation.classification_status == "classified"
        assert operation.cashflow_transaction_id is not None
        tx = await session.get(CashflowTransaction, operation.cashflow_transaction_id)
        assert tx.counterparty_id == loan.counterparty_id
        await session.refresh(loan)
        assert loan.barter_return_status == "returned"


async def test_payable_loan_paid_from_wallet_like_regular_supply(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Их заём нам 3000; мы гасим деньгами «как обычную поставку»: 1000 с кошелька →
    частично, потом товаром 4 кг (1200) и деньгами 800 → закрыт. Гибрид сходится."""
    async with async_session_factory() as session:
        loan, line = await _make_loan(session, inn="6155995004", we_lend=False)
        wallet = await make_wallet(session, name="Касса-бартер", wallet_type="cash_safe")
        await session.commit()

        result = await pay_payable_loan_with_money(
            session, loan_id=loan.id, operation_date=OP_DATE,
            amount=Decimal("1000"), wallet_id=wallet.id,
        )
        assert result["remaining"] == 2000.0
        await session.refresh(loan)
        assert loan.barter_return_status == "partially_returned"

        # Возврат товаром 4 кг по исходной цене → зачёт 1200, остаток 800.
        await create_barter_return(
            session, loan_id=loan.id,
            issued_at=datetime(2026, 7, 21, 10, 0, tzinfo=UTC),
            returns=[ReturnLineInput(amount=Decimal("1200"), loan_line_item_id=line.id,
                                     quantity=Decimal("4"))],
        )
        assert await loan_settled_value(session, loan) == Decimal("2200.00")

        result = await pay_payable_loan_with_money(
            session, loan_id=loan.id, operation_date=OP_DATE,
            amount=Decimal("800"), wallet_id=wallet.id,
        )
        assert result["remaining"] == 0.0
        await session.refresh(loan)
        assert loan.barter_return_status == "returned"
        assert loan.payment_status == "paid"


async def test_payable_loan_money_cannot_exceed_goods_settled_remainder(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Лимит оплаты — остаток по ЗАЧЁТНОЙ стоимости: после возврата 4 кг (1200) деньгами можно
    не больше 1800, полная сумма займа — уже переплата."""
    import pytest

    async with async_session_factory() as session:
        loan, line = await _make_loan(session, inn="6155995005", we_lend=False)
        wallet = await make_wallet(session, name="Касса-лимит", wallet_type="cash_safe")
        await session.commit()
        await create_barter_return(
            session, loan_id=loan.id,
            issued_at=datetime(2026, 7, 21, 10, 0, tzinfo=UTC),
            returns=[ReturnLineInput(amount=Decimal("1200"), loan_line_item_id=line.id,
                                     quantity=Decimal("4"))],
        )

        with pytest.raises(WarehouseInvoiceError, match="вне остатка"):
            await pay_payable_loan_with_money(
                session, loan_id=loan.id, operation_date=OP_DATE,
                amount=Decimal("3000"), wallet_id=wallet.id,
            )


async def test_payable_loan_bank_settlement_and_unwind_resync(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Банковский канал их займа + откат: исключение оплатившей операции возвращает заём в
    partially/open (статус пересинхронизируется, фантомно-закрытого займа нет)."""
    from app.services.banking.classifier import apply_operation_action

    async with async_session_factory() as session:
        loan, _line = await _make_loan(session, inn="6155995006", we_lend=False)
        wallet = await make_wallet(session, name="Банк-их", wallet_type="bank")
        operation = await make_bank_operation(
            session, amount="3000.00", direction="out", inn="6155995006",
            operation_date=OP_DATE, classification_status="classified",
        )
        tx = CashflowTransaction(
            wallet_id=wallet.id, direction="out", amount=Decimal("3000.00"),
            operation_date=OP_DATE, counterparty_id=loan.counterparty_id,
            source_kind="bank_operation", source_id=operation.id,
            payment_purpose="оплата бартерного займа", quality_status="auto",
        )
        session.add(tx)
        await session.flush()
        operation.cashflow_transaction_id = tx.id
        await session.commit()

        result = await pay_payable_loan_with_money(
            session, loan_id=loan.id, operation_date=OP_DATE,
            amount=Decimal("3000"), bank_operation_id=operation.id,
        )
        assert result["remaining"] == 0.0
        await session.refresh(loan)
        assert loan.barter_return_status == "returned"
        assert loan.payment_status == "paid"

        # Оператор исключил операцию — оплата снимается, заём обязан снова открыться.
        await apply_operation_action(session, operation, action="exclude")
        await session.commit()
        await session.refresh(loan)
        assert loan.barter_return_status == "open", (
            f"Заём остался {loan.barter_return_status} после снятия единственной оплаты"
        )
        assert loan.payment_status != "paid"


async def test_rule1_still_ignores_barter_loans(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Случайный банк-платёж поставщику по-прежнему НЕ гасит его бартерный заём — гашение
    только явное. Платёж без открытой обычной КЗ целиком уходит в предоплату."""
    from app.services.supplier_prepayments import ensure_prepayment_from_bank_transaction

    async with async_session_factory() as session:
        loan, _line = await _make_loan(session, inn="6155995007", we_lend=False)
        wallet = await make_wallet(session, name="Банк-правило1", wallet_type="bank")
        tx = CashflowTransaction(
            wallet_id=wallet.id, direction="out", amount=Decimal("3000.00"),
            operation_date=OP_DATE, counterparty_id=loan.counterparty_id,
            source_kind="bank_operation", payment_purpose="оплата поставщику",
            quality_status="auto",
        )
        session.add(tx)
        await session.flush()

        prepayment = await ensure_prepayment_from_bank_transaction(session, tx)
        await session.commit()

        assert prepayment is not None and prepayment.amount == Decimal("3000.00")
        await session.refresh(loan)
        assert loan.payment_status == "unpaid"
        assert loan.barter_return_status == "open"
        allocs = (
            await session.scalars(
                select(BarterReturnLine).where(BarterReturnLine.loan_invoice_id == loan.id)
            )
        ).all()
        assert allocs == []


async def test_dashboard_tiles_show_barter_remainders(
    client,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Решение владельца 18.07: бартер в ОБЩИХ плитках по ОСТАТКУ. Наша выдача 3000 с
    возвратом 4 кг (1200) → ДЗ 1800; их заём 3000 с оплатой 1000 → КЗ 2000."""
    from cp_helpers import admin_headers

    async with async_session_factory() as session:
        # Наш заём (ДЗ): 10 кг × 300, вернули 4 кг товаром.
        lend_loan, lend_line = await _make_loan(session, inn="6155996001", we_lend=True)
        await create_barter_return(
            session, loan_id=lend_loan.id,
            issued_at=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
            returns=[ReturnLineInput(amount=Decimal("1200"), loan_line_item_id=lend_line.id,
                                     quantity=Decimal("4"))],
        )
        lend_cp = str(lend_loan.counterparty_id)

        # Их заём (КЗ): 10 кг × 300, оплатили 1000 деньгами.
        borrow_loan, _ = await _make_loan(session, inn="6155996002", we_lend=False)
        wallet = await make_wallet(session, name="Касса-плитки", wallet_type="cash_safe")
        await session.commit()
        await pay_payable_loan_with_money(
            session, loan_id=borrow_loan.id, operation_date=OP_DATE,
            amount=Decimal("1000"), wallet_id=wallet.id,
        )
        borrow_cp = str(borrow_loan.counterparty_id)

    headers = await admin_headers(async_session_factory)
    response = client.get(
        "/api/v1/accounting/suppliers/balances", headers=headers
    )
    assert response.status_code == 200, response.text
    items = {i["counterparty_id"]: i for i in response.json()["items"]}

    lend_row = items.get(lend_cp)
    assert lend_row is not None, "контрагент нашей выдачи не попал в остатки"
    assert Decimal(str(lend_row["receivable"])) == Decimal("1800"), (
        f"ДЗ по нашей выдаче {lend_row['receivable']}, ожидалось 1800 (3000 − возврат 1200)"
    )

    borrow_row = items.get(borrow_cp)
    assert borrow_row is not None, "контрагент их займа не попал в остатки"
    assert Decimal(str(borrow_row["payable"])) == Decimal("2000"), (
        f"КЗ по их займу {borrow_row['payable']}, ожидалось 2000 (3000 − оплата 1000)"
    )


async def test_barter_partners_balances(
    client,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Уровень партнёров вкладки «Бартер»: наша выдача → «нам должны» остатком; их заём с
    частичной оплатой → «мы должны» остатком; взаимные поставки → по направлению; рассчитанный
    партнёр остаётся в списке с нулями."""
    from cp_helpers import admin_headers, make_invoice

    async with async_session_factory() as session:
        # Наш заём 3000, возвращено 4 кг товаром → нам должны 1800.
        lend_loan, lend_line = await _make_loan(session, inn="6155997001", we_lend=True)
        await create_barter_return(
            session, loan_id=lend_loan.id,
            issued_at=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
            returns=[ReturnLineInput(amount=Decimal("1200"), loan_line_item_id=lend_line.id,
                                     quantity=Decimal("4"))],
        )
        lend_cp = str(lend_loan.counterparty_id)

        # Их заём 3000, оплачено 1000 → мы должны 2000.
        borrow_loan, _ = await _make_loan(session, inn="6155997002", we_lend=False)
        wallet = await make_wallet(session, name="Касса-партнёры", wallet_type="cash_safe")
        await session.commit()
        await pay_payable_loan_with_money(
            session, loan_id=borrow_loan.id, operation_date=OP_DATE,
            amount=Decimal("1000"), wallet_id=wallet.id,
        )
        borrow_cp = str(borrow_loan.counterparty_id)

        # Взаимные поставки (неявный бартер): приход 3430 и расход 1078.
        mutual = await make_counterparty(
            session, name="Взаимные-партнёр", inn="6155997003", relationship="barter"
        )
        await make_invoice(
            session, counterparty_id=mutual.id, amount="3430.00", doc_kind="closing",
            number="ВП-1", invoice_date=date(2026, 7, 14),
        )
        await make_invoice(
            session, counterparty_id=mutual.id, amount="1078.00", doc_kind="closing",
            direction="receivable", number="ВП-2", invoice_date=date(2026, 7, 10),
        )
        mutual_cp = str(mutual.id)

        # Рассчитанный: бартер-профиль без открытых позиций.
        settled_cp_row = await make_counterparty(
            session, name="Рассчитанный-партнёр", inn="6155997004", relationship="barter"
        )
        settled_cp = str(settled_cp_row.id)
        await session.commit()

    headers = await admin_headers(async_session_factory)
    response = client.get("/api/v1/warehouse/barter/partners", headers=headers)
    assert response.status_code == 200, response.text
    rows = {r["counterparty_id"]: r for r in response.json()}

    lend = rows[lend_cp]
    assert (lend["we_are_owed"], lend["we_owe"]) == (1800.0, 0.0)
    assert lend["open_loans"] == 1 and lend["has_loans"] is True

    borrow = rows[borrow_cp]
    assert (borrow["we_are_owed"], borrow["we_owe"]) == (0.0, 2000.0)
    assert borrow["net"] == -2000.0

    mut = rows[mutual_cp]
    assert (mut["we_are_owed"], mut["we_owe"]) == (1078.0, 3430.0)
    assert mut["has_mutual"] is True and mut["has_loans"] is False

    settled = rows[settled_cp]
    assert (settled["we_are_owed"], settled["we_owe"]) == (0.0, 0.0)


async def test_legacy_invoice_marked_as_loan_then_returned(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Мостик для унаследованных строк (сценарий владельца с Ёбидоёби): iiko-приходная без
    роли признаётся ЯВНЫМ займом → уходит из хроно-зачёта → гасится товаром по цене прихода.
    Строк InvoiceLineItem у совсем старых нет — материализуются из JSONB-зеркала."""
    from cp_helpers import make_invoice

    from app.services.counterparty_barter_match import suggest_barter_matches
    from app.services.warehouse_invoices import convert_invoice_to_barter_loan

    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Легаси-Ёбидоёби", inn="6155998001", relationship="barter"
        )
        # Унаследованная iiko-приходная: только JSONB-зеркало, без InvoiceLineItem.
        legacy = await make_invoice(
            session, counterparty_id=cp.id, amount="1223.24", doc_kind="closing",
            source="iiko", number="78787712", invoice_date=date(2026, 7, 10),
            line_items=[{"product_id": "guid-cheese", "name": "Сыр Гауда",
                         "quantity": "2.6", "amount": "1223.24"}],
        )
        await session.commit()

        converted = await convert_invoice_to_barter_loan(session, legacy.id)
        assert converted.barter_role == "loan"
        assert converted.barter_return_status == "open"
        # Из хроно-зачёта накладная ушла (там только строки без роли).
        assert await suggest_barter_matches(session, cp.id) == []
        line = (
            await session.scalars(
                select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == legacy.id)
            )
        ).one()
        assert float(line.quantity) == 2.6

        # «Предположим, я их вернул»: возврат товаром по цене прихода — заём закрыт.
        ret = await create_barter_return(
            session, loan_id=legacy.id,
            issued_at=datetime(2026, 7, 19, 10, 0, tzinfo=UTC),
            returns=[ReturnLineInput(amount=Decimal("1223.24"), loan_line_item_id=line.id,
                                     quantity=Decimal("2.6"))],
        )
        assert ret.direction == "receivable"  # возврат их займа = наша расходная
        await session.refresh(legacy)
        assert legacy.barter_return_status == "returned"
        assert legacy.payment_status == "paid"


async def test_bank_payment_of_loan_respects_goods_returns(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ПРЕДКОММИТ-АУДИТ, блокер: их заём 3000, товаром вернули 4 кг (1200) → долг 1800.
    Оплата банк-операцией на 1800 не должна аллоцировать весь остаток НАКЛАДНОЙ (3000):
    товарные возвраты живут в леджере, а не в аллокациях."""
    from app.models import InvoicePaymentAllocation

    async with async_session_factory() as session:
        loan, line = await _make_loan(session, inn="6155999001", we_lend=False)
        await create_barter_return(
            session, loan_id=loan.id,
            issued_at=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
            returns=[ReturnLineInput(amount=Decimal("1200"), loan_line_item_id=line.id,
                                     quantity=Decimal("4"))],
        )
        wallet = await make_wallet(session, name="Банк-аудит1", wallet_type="bank")
        operation = await make_bank_operation(
            session, amount="1800.00", direction="out", inn="6155999001",
            operation_date=OP_DATE, classification_status="classified",
        )
        tx = CashflowTransaction(
            wallet_id=wallet.id, direction="out", amount=Decimal("1800.00"),
            operation_date=OP_DATE, counterparty_id=loan.counterparty_id,
            source_kind="bank_operation", source_id=operation.id,
            payment_purpose="оплата займа", quality_status="auto",
        )
        session.add(tx)
        await session.flush()
        operation.cashflow_transaction_id = tx.id
        await session.commit()

        result = await pay_payable_loan_with_money(
            session, loan_id=loan.id, operation_date=OP_DATE,
            amount=Decimal("1800"), bank_operation_id=operation.id,
        )

        allocated = await session.scalar(
            select(func.coalesce(func.sum(InvoicePaymentAllocation.amount), 0)).where(
                InvoicePaymentAllocation.invoice_id == loan.id
            )
        )
        assert Decimal(str(allocated)) == Decimal("1800.00"), (
            f"Аллоцировано {allocated} вместо запрошенных 1800 — товарный возврат оплачен вторично"
        )
        assert result["remaining"] == 0.0
        await session.refresh(loan)
        assert loan.barter_return_status == "returned"


async def test_bank_payment_of_loan_clears_rule1_prepayment(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ПРЕДКОММИТ-АУДИТ: классификатор уже завёл ДЗ правила 1 на платёж бартерному партнёру
    (бартер исключён из FIFO-гашения КЗ). Явное гашение займа тратит те же деньги — ДЗ
    обязана схлопнуться, иначе двойной учёт."""
    from app.services.supplier_prepayments import ensure_prepayment_from_bank_transaction

    async with async_session_factory() as session:
        loan, _line = await _make_loan(session, inn="6155999002", we_lend=False)
        wallet = await make_wallet(session, name="Банк-аудит2", wallet_type="bank")
        operation = await make_bank_operation(
            session, amount="3000.00", direction="out", inn="6155999002",
            operation_date=OP_DATE, classification_status="classified",
        )
        tx = CashflowTransaction(
            wallet_id=wallet.id, direction="out", amount=Decimal("3000.00"),
            operation_date=OP_DATE, counterparty_id=loan.counterparty_id,
            source_kind="bank_operation", source_id=operation.id,
            payment_purpose="оплата поставщику", quality_status="auto",
        )
        session.add(tx)
        await session.flush()
        operation.cashflow_transaction_id = tx.id
        prepayment = await ensure_prepayment_from_bank_transaction(session, tx)
        await session.commit()
        assert prepayment is not None and prepayment.amount == Decimal("3000.00")

        await pay_payable_loan_with_money(
            session, loan_id=loan.id, operation_date=OP_DATE,
            amount=Decimal("3000"), bank_operation_id=operation.id,
        )

        dz = await _receivable(session, loan.counterparty_id)
        assert dz == Decimal("0.00"), (
            f"ДЗ {dz} осталась фантомом: те же 3000 и погасили заём, и числятся авансом"
        )


async def test_incoming_operation_cannot_settle_two_loans(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ПРЕДКОММИТ-АУДИТ: одна входящая операция не может закрыть два наших займа —
    гашение проверяет свободный остаток операции (леджер + аллокации)."""
    import pytest
    from cp_helpers import make_account

    async with async_session_factory() as session:
        loan_a, line_a = await _make_loan(session, inn="6155999003", we_lend=True)
        # Второй заём тому же партнёру.
        product = await _product(session, name="Пармезан")
        loan_b = await create_warehouse_invoice(
            session, counterparty_id=loan_a.counterparty_id,
            issued_at=datetime(2026, 7, 11, 12, 0, tzinfo=UTC), mode="loan", we_lend=True,
            lines=[LineInput(name="Пармезан", quantity=Decimal("10"), price=Decimal("300"),
                             iiko_product_id=product.id)],
        )
        line_b = (
            await session.scalars(
                select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == loan_b.id)
            )
        ).one()
        account = await make_account(session)
        await make_wallet(session, name="Банк-аудит3", wallet_type="bank", account_id=account.id)
        operation = await make_bank_operation(
            session, amount="3000.00", direction="in", inn="6155999003",
            operation_date=OP_DATE, account_id=account.id,
        )
        await session.commit()

        await settle_receivable_loan_with_money(
            session, loan_id=loan_a.id, operation_date=OP_DATE,
            lines=[MoneyReturnLine(loan_line_item_id=line_a.id, quantity=Decimal("10"),
                                   unit_price=Decimal("300"))],
            bank_operation_id=operation.id,
        )
        # Та же операция на второй заём — деньги уже потрачены.
        with pytest.raises(WarehouseInvoiceError, match="остаток операции"):
            await settle_receivable_loan_with_money(
                session, loan_id=loan_b.id, operation_date=OP_DATE,
                lines=[MoneyReturnLine(loan_line_item_id=line_b.id, quantity=Decimal("10"),
                                       unit_price=Decimal("300"))],
                bank_operation_id=operation.id,
            )


async def test_duplicate_lines_in_one_request_respect_kg_limit(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ПРЕДКОММИТ-АУДИТ: две строки запроса по ОДНОЙ позиции займа не обходят лимит по кг
    (снимок остатков копится внутри цикла)."""
    import pytest

    async with async_session_factory() as session:
        loan, line = await _make_loan(session, inn="6155999004", we_lend=True)
        await session.commit()
        with pytest.raises(WarehouseInvoiceError, match="превышает остаток"):
            await create_barter_return(
                session, loan_id=loan.id,
                issued_at=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
                returns=[
                    ReturnLineInput(amount=Decimal("1800"), loan_line_item_id=line.id,
                                    quantity=Decimal("6")),
                    ReturnLineInput(amount=Decimal("1800"), loan_line_item_id=line.id,
                                    quantity=Decimal("6")),
                ],
            )


async def test_iiko_resync_keeps_converted_loan_ledger(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ПРЕДКОММИТ-АУДИТ, блокер: конвертированный заём остаётся под реверс-синком iiko.
    Пере-синк НЕ должен затирать его строки — иначе леджер гашений осиротеет (FK SET NULL)
    и возвращённое можно вернуть повторно."""
    from cp_helpers import cloud_invoice_docs, suppliers_xml

    from app.services.counterparty_invoice_sync import ingest_iiko_payables
    from app.services.warehouse_invoices import convert_invoice_to_barter_loan

    guid = "guid-audit-resync"
    doc = {
        "id": "doc-audit-resync", "supplier": guid, "status": "PROCESSED",
        "document_number": "ПН-resync", "incoming_date": "2026-07-10",
        "items": [{"sum": "1223.24"}],
    }
    suppliers = suppliers_xml([{"id": guid, "name": "Резинк-партнёр", "inn": "6155999005"}])

    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Резинк-партнёр", inn="6155999005", iiko_guid=guid
        )
        await session.commit()
        await ingest_iiko_payables(
            session, suppliers_xml=suppliers, incoming_docs=cloud_invoice_docs([doc])
        )
        await session.commit()
        invoice = (
            await session.scalars(
                select(SupplierInvoice).where(SupplierInvoice.counterparty_id == cp.id)
            )
        ).one()

        await convert_invoice_to_barter_loan(session, invoice.id)
        line_id = (
            await session.scalars(
                select(InvoiceLineItem.id).where(InvoiceLineItem.invoice_id == invoice.id)
            )
        ).first()
        assert line_id is not None

        # Повторный синк того же документа — строки займа обязаны уцелеть.
        await ingest_iiko_payables(
            session, suppliers_xml=suppliers, incoming_docs=cloud_invoice_docs([doc])
        )
        await session.commit()
        still = (
            await session.scalars(
                select(InvoiceLineItem.id).where(InvoiceLineItem.invoice_id == invoice.id)
            )
        ).all()
        assert list(still) == [line_id], (
            "Реверс-синк пересоздал строки конвертированного займа — леджер гашений осиротел"
        )


async def test_iiko_resync_does_not_rewrite_loan_amount(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ПРЕДКОММИТ-АУДИТ: заём, ушедший в iiko (авто-пуш), попадает в ветку own_pushed синка.
    Она правит amount, но НЕ пересобирает строки — сумма займа разъехалась бы с Σ позиций и
    заём стало бы невозможно закрыть. Сумма долга неприкосновенна."""
    from cp_helpers import cloud_invoice_docs, suppliers_xml

    from app.services.counterparty_invoice_sync import ingest_iiko_payables

    guid = "guid-audit-amount"
    async with async_session_factory() as session:
        loan, line = await _make_loan(session, inn="6155999006", we_lend=False)
        cp = await session.get(Counterparty, loan.counterparty_id)
        session.add(CounterpartyAlias(counterparty_id=cp.id, alias=guid, source="iiko"))
        # Займ «ушёл» в iiko: получил external_id (авто-пуш при создании).
        loan.external_id = "doc-audit-amount"
        loan.iiko_push_status = "pushed"
        await session.commit()

        # iiko отдаёт документ с ДРУГОЙ суммой (округление/правка кладовщиком).
        await ingest_iiko_payables(
            session,
            suppliers_xml=suppliers_xml(
                [{"id": guid, "name": "Бартер-6155999006", "inn": "6155999006"}]
            ),
            incoming_docs=cloud_invoice_docs(
                [
                    {
                        "id": "doc-audit-amount", "supplier": guid, "status": "PROCESSED",
                        "document_number": loan.number, "incoming_date": "2026-07-10",
                        "items": [{"sum": "2999.99"}],
                    }
                ]
            ),
        )
        await session.commit()
        await session.refresh(loan)
        assert loan.amount == Decimal("3000.00"), (
            f"Сумма займа переписана синком в {loan.amount} — долг разъехался с позициями"
        )

        # Контроль: заём по-прежнему закрывается возвратом всех килограммов.
        await create_barter_return(
            session, loan_id=loan.id,
            issued_at=datetime(2026, 7, 22, 10, 0, tzinfo=UTC),
            returns=[ReturnLineInput(amount=Decimal("3000"), loan_line_item_id=line.id,
                                     quantity=Decimal("10"))],
        )
        await session.refresh(loan)
        assert loan.barter_return_status == "returned"


async def test_direct_wallet_payment_door_respects_goods_returns(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ПРЕДКОММИТ-АУДИТ: ПРЯМАЯ дверь оплаты накладной (POST /invoices/{id}/pay) в обход
    бартерного сервиса тоже обязана знать про товарные возвраты — иначе заплатим 3000 при
    реальном долге 1800."""
    import pytest

    from app.services.counterparty_payments import (
        CounterpartyPaymentError,
        pay_invoice_from_wallet,
    )

    async with async_session_factory() as session:
        loan, line = await _make_loan(session, inn="6155999007", we_lend=False)
        await create_barter_return(
            session, loan_id=loan.id,
            issued_at=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
            returns=[ReturnLineInput(amount=Decimal("1200"), loan_line_item_id=line.id,
                                     quantity=Decimal("4"))],
        )
        wallet = await make_wallet(session, name="Касса-прямая", wallet_type="cash_safe")
        await session.commit()

        with pytest.raises(CounterpartyPaymentError, match="вне допустимого остатка"):
            await pay_invoice_from_wallet(
                session, invoice_id=loan.id, wallet_id=wallet.id,
                amount=Decimal("3000"), operation_date=OP_DATE,
            )
        # Реальный остаток пройдёт.
        await pay_invoice_from_wallet(
            session, invoice_id=loan.id, wallet_id=wallet.id,
            amount=Decimal("1800"), operation_date=OP_DATE,
        )
        await session.refresh(loan)
        assert loan.barter_return_status == "returned"


async def test_barter_invoice_cannot_be_voided(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ПРЕДКОММИТ-АУДИТ: возвратная накладная создаётся 'paid' без аллокаций — общий гард
    void'а её пропускал бы, а строки леджера остались бы: заём числился бы погашенным
    несуществующим документом."""
    import pytest

    from app.services.counterparty_registry import CounterpartyRegistryError, void_invoice

    async with async_session_factory() as session:
        loan, line = await _make_loan(session, inn="6155999008", we_lend=True)
        ret = await create_barter_return(
            session, loan_id=loan.id,
            issued_at=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
            returns=[ReturnLineInput(amount=Decimal("1200"), loan_line_item_id=line.id,
                                     quantity=Decimal("4"))],
        )
        with pytest.raises(CounterpartyRegistryError, match="[Бб]артерная"):
            await void_invoice(session, ret.id)
        with pytest.raises(CounterpartyRegistryError, match="[Бб]артерная"):
            await void_invoice(session, loan.id)


async def test_bank_wallet_rejected_in_cash_channel(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ПРЕДКОММИТ-АУДИТ: приход на БАНКОВСКИЙ кошелёк «наличным» каналом задвоил бы ДДС
    (выписка заведёт вторую проводку — source_kind='barter_return' не склеивается)."""
    import pytest

    async with async_session_factory() as session:
        loan, line = await _make_loan(session, inn="6155999009", we_lend=True)
        bank_wallet = await make_wallet(session, name="Р/с-бартер", wallet_type="bank")
        await session.commit()

        with pytest.raises(WarehouseInvoiceError, match="Банк-операция"):
            await settle_receivable_loan_with_money(
                session, loan_id=loan.id, operation_date=OP_DATE,
                lines=[MoneyReturnLine(loan_line_item_id=line.id, quantity=Decimal("4"),
                                       unit_price=Decimal("350"))],
                wallet_id=bank_wallet.id,
            )

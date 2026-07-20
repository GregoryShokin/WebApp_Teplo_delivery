"""Бартер: устойчивость на ДЛИННЫХ сценариях (проверка перед деплоем).

Отдельные операции покрыты в test_barter_*.py — здесь проверяется, что долг не «едет» на
цепочках из нескольких действий: частичный возврат → деньги → списание, откаты, многострочные
займы, накопление допуска перевозврата.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from cp_helpers import make_counterparty, make_expense_article, make_wallet
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import BarterReturnLine, IikoProduct, InvoiceLineItem, SupplierInvoice
from app.services.barter_loan_money import pay_payable_loan_with_money
from app.services.warehouse_invoices import (
    LineInput,
    ReturnLineInput,
    WarehouseInvoiceError,
    create_barter_return,
    create_warehouse_invoice,
    loan_settled_value,
)

ISSUED = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
RET_AT = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)
OP_DATE = date(2026, 7, 20)


async def _product(session: AsyncSession, name: str) -> IikoProduct:
    product = IikoProduct(
        iiko_id=str(uuid.uuid4()), name=name, type="GOODS", unit="кг",
        synced_at=datetime.now(UTC),
    )
    session.add(product)
    await session.flush()
    return product


VERIFIED_REQUISITES = {
    "bankAcnt": "40702810400000012345",
    "bankBik": "044525225",
    "recipientCorrAccountNumber": "30101810400000000225",
}


async def _loan(
    session: AsyncSession,
    *,
    inn: str,
    we_lend: bool = False,
    lines: list[tuple[str, str, str]] | None = None,
    bankable: bool = False,
) -> tuple[SupplierInvoice, list[InvoiceLineItem]]:
    """Заём по списку (название, кг, цена). По умолчанию — их заём 10 кг × 300.

    ``bankable`` — с подтверждёнными реквизитами, чтобы заём можно было отправить в банк.
    """
    spec = lines or [("Моцарелла", "10", "300")]
    cp = await make_counterparty(
        session,
        name=f"Сценарий-{inn}",
        inn=inn,
        **(
            {"requisites": VERIFIED_REQUISITES, "requisites_verified": True}
            if bankable
            else {}
        ),
    )
    await make_expense_article(session, code="payment_to_supplier", name="Оплата поставщикам")
    inputs = []
    for name, qty, price in spec:
        product = await _product(session, name)
        inputs.append(
            LineInput(
                name=name, quantity=Decimal(qty), price=Decimal(price),
                iiko_product_id=product.id,
            )
        )
    await session.commit()
    loan = await create_warehouse_invoice(
        session, counterparty_id=cp.id, issued_at=ISSUED, mode="loan",
        we_lend=we_lend, lines=inputs,
    )
    items = (
        await session.scalars(
            select(InvoiceLineItem)
            .where(InvoiceLineItem.invoice_id == loan.id)
            .order_by(InvoiceLineItem.sort_order)
        )
    ).all()
    return loan, list(items)


async def _remaining(session: AsyncSession, loan_id: uuid.UUID) -> Decimal:
    loan = await session.get(SupplierInvoice, loan_id)
    await session.refresh(loan)
    return Decimal(str(loan.amount)) - await loan_settled_value(session, loan)


async def test_chain_goods_then_money_then_writeoff(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Цепочка: 3000 → товаром 1500 → деньгами 1000 → хвост 500 списан. Заём закрыт ровно."""
    async with async_session_factory() as session:
        loan, items = await _loan(session, inn="6177000001", lines=[("Моцарелла", "10", "300")])
        line, loan_id = items[0], loan.id
        wallet = await make_wallet(session, name="Касса-сц1", wallet_type="cash_safe")
        await session.commit()

        await create_barter_return(
            session, loan_id=loan_id, issued_at=RET_AT,
            returns=[
                ReturnLineInput(
                    amount=Decimal("1500"), loan_line_item_id=line.id, quantity=Decimal("5")
                )
            ],
        )
        assert await _remaining(session, loan_id) == Decimal("1500.00")

        await pay_payable_loan_with_money(
            session, loan_id=loan_id, operation_date=OP_DATE,
            amount=Decimal("1000"), wallet_id=wallet.id,
        )
        await session.commit()
        assert await _remaining(session, loan_id) == Decimal("500.00")

        # Товара «на складе займа» осталось 5 кг, но деньгами уже закрыта часть долга —
        # вернуть можно только на оставшиеся 500 ₽ (1,67 кг), не больше.
        with pytest.raises(WarehouseInvoiceError, match="превышает остаток долга"):
            await create_barter_return(
                session, loan_id=loan_id, issued_at=RET_AT,
                returns=[
                    ReturnLineInput(
                        amount=Decimal("900"), loan_line_item_id=line.id, quantity=Decimal("3")
                    )
                ],
            )

        # Возвращаем 1 кг (300 ₽) и прощаем хвост 200 ₽ — заём закрыт ровно.
        await create_barter_return(
            session, loan_id=loan_id, issued_at=RET_AT,
            returns=[
                ReturnLineInput(
                    amount=Decimal("300"), loan_line_item_id=line.id, quantity=Decimal("1")
                )
            ],
            write_off_remainder=True,
        )
        refreshed = await session.get(SupplierInvoice, loan_id)
        assert await _remaining(session, loan_id) == Decimal("0.00")
        assert refreshed.barter_return_status == "returned"
        assert Decimal(str(refreshed.barter_writeoff_amount)) == Decimal("200.00")


async def test_tolerance_does_not_accumulate_across_returns(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Допуск 100 г — на ПОЗИЦИЮ, а не на каждый возврат: тремя заходами лишнего не вернуть."""
    async with async_session_factory() as session:
        loan, items = await _loan(session, inn="6177000002", lines=[("Моцарелла", "10", "300")])
        line, loan_id = items[0], loan.id
        await session.commit()

        await create_barter_return(
            session, loan_id=loan_id, issued_at=RET_AT,
            returns=[
                ReturnLineInput(
                    amount=Decimal("3000"), loan_line_item_id=line.id, quantity=Decimal("10")
                )
            ],
        )
        # Позиция закрыта целиком — добрать «ещё 100 г довеска» уже нельзя (заём закрыт).
        with pytest.raises(WarehouseInvoiceError, match="полностью возвращён|превышает выданное"):
            await create_barter_return(
                session, loan_id=loan_id, issued_at=RET_AT,
                returns=[
                    ReturnLineInput(
                        amount=Decimal("30"), loan_line_item_id=line.id, quantity=Decimal("0.2")
                    )
                ],
            )


async def test_multiline_loan_closes_only_when_all_lines_returned(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Две позиции: закрыли одну целиком — заём ещё открыт; закрыли вторую — закрылся."""
    async with async_session_factory() as session:
        loan, items = await _loan(
            session, inn="6177000003",
            lines=[("Моцарелла", "10", "300"), ("Креветка", "2", "500")],
        )
        mozzarella, shrimp = items[0], items[1]
        loan_id = loan.id
        await session.commit()
        assert Decimal(str(loan.amount)) == Decimal("4000.00")

        await create_barter_return(
            session, loan_id=loan_id, issued_at=RET_AT,
            returns=[
                ReturnLineInput(
                    amount=Decimal("3000"), loan_line_item_id=mozzarella.id,
                    quantity=Decimal("10"),
                )
            ],
        )
        refreshed = await session.get(SupplierInvoice, loan_id)
        assert refreshed.barter_return_status == "partially_returned"
        assert await _remaining(session, loan_id) == Decimal("1000.00")

        await create_barter_return(
            session, loan_id=loan_id, issued_at=RET_AT,
            returns=[
                ReturnLineInput(
                    amount=Decimal("1000"), loan_line_item_id=shrimp.id, quantity=Decimal("2")
                )
            ],
        )
        refreshed = await session.get(SupplierInvoice, loan_id)
        assert refreshed.barter_return_status == "returned"
        assert await _remaining(session, loan_id) == Decimal("0.00")


async def test_write_off_on_full_return_writes_nothing(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Флаг списания при ПОЛНОМ возврате не должен списывать ничего (хвоста нет)."""
    async with async_session_factory() as session:
        loan, items = await _loan(session, inn="6177000004", lines=[("Моцарелла", "10", "300")])
        line, loan_id = items[0], loan.id
        await session.commit()

        await create_barter_return(
            session, loan_id=loan_id, issued_at=RET_AT,
            returns=[
                ReturnLineInput(
                    amount=Decimal("3000"), loan_line_item_id=line.id, quantity=Decimal("10")
                )
            ],
            write_off_remainder=True,
        )
        refreshed = await session.get(SupplierInvoice, loan_id)
        assert Decimal(str(refreshed.barter_writeoff_amount)) == Decimal("0.00")
        assert refreshed.barter_return_status == "returned"


async def test_line_from_another_loan_is_rejected(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Строка ЧУЖОГО займа в возврате не принимается — иначе можно списать долг с другого."""
    async with async_session_factory() as session:
        first, first_items = await _loan(session, inn="6177000005")
        second, _ = await _loan(session, inn="6177000006")
        await session.commit()

        with pytest.raises(WarehouseInvoiceError):
            await create_barter_return(
                session, loan_id=second.id, issued_at=RET_AT,
                returns=[
                    ReturnLineInput(
                        amount=Decimal("300"), loan_line_item_id=first_items[0].id,
                        quantity=Decimal("1"),
                    )
                ],
            )


async def test_money_payment_cannot_exceed_goods_adjusted_remainder(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Лимит денег считается от ЗАЧЁТНОЙ стоимости: после возврата товара платить можно меньше.

    Аллокационного остатка накладной для этого мало — товарные возвраты живут в леджере, а не
    в аллокациях, и без учёта зачётной стоимости деньгами закрыли бы уже закрытый товаром долг.
    """
    async with async_session_factory() as session:
        loan, items = await _loan(session, inn="6177000007", lines=[("Моцарелла", "10", "300")])
        line, loan_id = items[0], loan.id
        wallet = await make_wallet(session, name="Касса-сц7", wallet_type="cash_safe")
        await session.commit()

        await create_barter_return(
            session, loan_id=loan_id, issued_at=RET_AT,
            returns=[
                ReturnLineInput(
                    amount=Decimal("2400"), loan_line_item_id=line.id, quantity=Decimal("8")
                )
            ],
        )
        assert await _remaining(session, loan_id) == Decimal("600.00")

        with pytest.raises(WarehouseInvoiceError, match="вне остатка"):
            await pay_payable_loan_with_money(
                session, loan_id=loan_id, operation_date=OP_DATE,
                amount=Decimal("700"), wallet_id=wallet.id,
            )


async def test_zero_and_negative_quantities_rejected(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Нулевые и отрицательные килограммы не проходят — иначе в леджер попадёт мусор."""
    async with async_session_factory() as session:
        loan, items = await _loan(session, inn="6177000008")
        line, loan_id = items[0], loan.id
        await session.commit()

        for qty in (Decimal("0"), Decimal("-1")):
            with pytest.raises(WarehouseInvoiceError):
                await create_barter_return(
                    session, loan_id=loan_id, issued_at=RET_AT,
                    returns=[
                        ReturnLineInput(
                            amount=Decimal("300"), loan_line_item_id=line.id, quantity=qty
                        )
                    ],
                )


# --- находки аудита устойчивости (20.07) ----------------------------------------------------


async def test_bank_draft_for_partially_returned_loan_uses_goods_adjusted_remainder(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Черновик в банк по частично возвращённому займу — на ОСТАТОК, а не на всю сумму.

    Товарные возвраты живут в леджере, а не в аллокациях, поэтому «сумма − аллокации» для займа
    завышена. Соседняя дверь (оплата с кошелька) поправку имеет; без неё черновик уходит в банк
    на полную сумму и мы платим за уже возвращённый товар.
    """
    from app.services.counterparty_payments import create_payment_draft_for_invoices

    async with async_session_factory() as session:
        loan, items = await _loan(
            session, inn="6177000009", lines=[("Моцарелла", "10", "300")], bankable=True
        )
        line, loan_id = items[0], loan.id
        await session.commit()

        # Вернули 6 кг из 10 → долг 1200 из 3000.
        await create_barter_return(
            session, loan_id=loan_id, issued_at=RET_AT,
            returns=[
                ReturnLineInput(
                    amount=Decimal("1800"), loan_line_item_id=line.id, quantity=Decimal("6")
                )
            ],
        )
        assert await _remaining(session, loan_id) == Decimal("1200.00")

        draft = await create_payment_draft_for_invoices(
            session, invoice_ids=[loan_id], actor_user_id=None
        )
        assert Decimal(str(draft.amount)) == Decimal("1200.00")


async def test_money_first_then_goods_cannot_over_return(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Деньгами закрыли часть долга — товаром можно вернуть только ОСТАВШУЮСЯ часть.

    Килограммы деньги не расходуют (леджер их не видит), поэтому без рублёвого лимита оператор
    возвращал весь товар поверх уже уплаченных денег: товар отдан и деньги заплачены, а
    переплата не становится ни дебиторкой, ни предоплатой — просто теряется.
    """
    async with async_session_factory() as session:
        loan, items = await _loan(session, inn="6177000010", lines=[("Моцарелла", "10", "300")])
        line, loan_id = items[0], loan.id
        wallet = await make_wallet(session, name="Касса-сц10", wallet_type="cash_safe")
        await session.commit()

        # Заплатили 2400 из 3000 → долг 600 = 2 кг по цене выдачи.
        await pay_payable_loan_with_money(
            session, loan_id=loan_id, operation_date=OP_DATE,
            amount=Decimal("2400"), wallet_id=wallet.id,
        )
        await session.commit()
        assert await _remaining(session, loan_id) == Decimal("600.00")

        # Возврат всех 10 кг — это возврат сверх долга: должен быть отклонён.
        with pytest.raises(WarehouseInvoiceError):
            await create_barter_return(
                session, loan_id=loan_id, issued_at=RET_AT,
                returns=[
                    ReturnLineInput(
                        amount=Decimal("3000"), loan_line_item_id=line.id, quantity=Decimal("10")
                    )
                ],
            )

        # А 2 кг (ровно на остаток долга) проходят и закрывают заём.
        await create_barter_return(
            session, loan_id=loan_id, issued_at=RET_AT,
            returns=[
                ReturnLineInput(
                    amount=Decimal("600"), loan_line_item_id=line.id, quantity=Decimal("2")
                )
            ],
        )
        refreshed = await session.get(SupplierInvoice, loan_id)
        assert refreshed.barter_return_status == "returned"
        assert await _remaining(session, loan_id) == Decimal("0.00")


# --- банк-канал гашения НАШЕГО займа: гарды (P1 аудита 20.07) --------------------------------


async def _lend_loan_with_bank(
    session: AsyncSession, *, inn: str, op_amount: str, **op_kwargs
):
    """Наш заём 10 кг × 300 (нам должны 3000) + входящая банк-операция на op_amount."""
    from cp_helpers import make_account, make_bank_operation

    cp = await make_counterparty(session, name=f"Банк-{inn}", inn=inn)
    await make_expense_article(
        session, code="vozvrat_pereplaty_ot_postavschikov",
        name="Возврат переплаты от поставщиков",
    )
    product = await _product(session, "Моцарелла")
    account = await make_account(session)
    await make_wallet(session, name=f"Банк-{inn}", wallet_type="bank", account_id=account.id)
    await session.commit()
    loan = await create_warehouse_invoice(
        session, counterparty_id=cp.id, issued_at=ISSUED, mode="loan", we_lend=True,
        lines=[
            LineInput(
                name="Моцарелла", quantity=Decimal("10"), price=Decimal("300"),
                iiko_product_id=product.id,
            )
        ],
    )
    line = (
        await session.scalars(
            select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == loan.id)
        )
    ).one()
    operation = await make_bank_operation(
        session, amount=op_amount, direction="in", inn=inn,
        operation_date=OP_DATE, account_id=account.id, **op_kwargs,
    )
    await session.commit()
    return loan, line, operation


async def test_bank_operation_larger_than_settlement_is_rejected(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Операция крупнее суммы гашения не принимается — иначе её остаток выпадет из ДДС.

    Проводка создавалась на сумму ГАШЕНИЯ, а операция помечалась разобранной ЦЕЛИКОМ: разница
    (5000 − 3000) не попадала в ДДС никогда, потому что все контуры доразбора смотрят только на
    неразобранные операции.
    """
    from app.services.barter_loan_money import MoneyReturnLine, settle_receivable_loan_with_money

    async with async_session_factory() as session:
        loan, line, operation = await _lend_loan_with_bank(
            session, inn="6177001001", op_amount="5000.00"
        )

        with pytest.raises(WarehouseInvoiceError, match="не совпадает|разнесите"):
            await settle_receivable_loan_with_money(
                session, loan_id=loan.id, operation_date=OP_DATE,
                lines=[
                    MoneyReturnLine(
                        loan_line_item_id=line.id, quantity=Decimal("10"),
                        unit_price=Decimal("300"),
                    )
                ],
                bank_operation_id=operation.id,
            )


async def test_already_classified_operation_cannot_settle_loan(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Уже разобранная операция заём не гасит — её деньги учтены статьёй, это двойной учёт."""
    from app.services.barter_loan_money import MoneyReturnLine, settle_receivable_loan_with_money

    async with async_session_factory() as session:
        loan, line, operation = await _lend_loan_with_bank(
            session, inn="6177001002", op_amount="3000.00",
            classification_status="classified",
        )

        with pytest.raises(WarehouseInvoiceError, match="уже разобрана"):
            await settle_receivable_loan_with_money(
                session, loan_id=loan.id, operation_date=OP_DATE,
                lines=[
                    MoneyReturnLine(
                        loan_line_item_id=line.id, quantity=Decimal("10"),
                        unit_price=Decimal("300"),
                    )
                ],
                bank_operation_id=operation.id,
            )


async def test_internal_transfer_cannot_settle_loan(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Внутренний перевод между своими счетами — не деньги партнёра, заём им не гасится."""
    from app.services.barter_loan_money import MoneyReturnLine, settle_receivable_loan_with_money

    async with async_session_factory() as session:
        from app.models import TransferGroup

        group = TransferGroup(amount=Decimal("3000.00"), initiated_at=ISSUED)
        session.add(group)
        await session.flush()
        loan, line, operation = await _lend_loan_with_bank(
            session, inn="6177001003", op_amount="3000.00", transfer_group_id=group.id,
        )

        with pytest.raises(WarehouseInvoiceError, match="перевод"):
            await settle_receivable_loan_with_money(
                session, loan_id=loan.id, operation_date=OP_DATE,
                lines=[
                    MoneyReturnLine(
                        loan_line_item_id=line.id, quantity=Decimal("10"),
                        unit_price=Decimal("300"),
                    )
                ],
                bank_operation_id=operation.id,
            )


async def test_exact_unreviewed_operation_settles_and_books_full_amount(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Регресс-гард: точная неразобранная операция гасит заём, вся сумма попадает в ДДС."""
    from app.models import CashflowTransaction
    from app.services.barter_loan_money import MoneyReturnLine, settle_receivable_loan_with_money

    async with async_session_factory() as session:
        loan, line, operation = await _lend_loan_with_bank(
            session, inn="6177001004", op_amount="3500.00"
        )

        result = await settle_receivable_loan_with_money(
            session, loan_id=loan.id, operation_date=OP_DATE,
            lines=[
                MoneyReturnLine(
                    loan_line_item_id=line.id, quantity=Decimal("10"),
                    unit_price=Decimal("350"),
                )
            ],
            bank_operation_id=operation.id,
        )

        assert result["money_received"] == 3500.0
        assert result["credited"] == 3000.0
        await session.refresh(operation)
        tx = await session.get(CashflowTransaction, operation.cashflow_transaction_id)
        # В ДДС попала ВСЯ сумма операции, а не только зачтённая часть.
        assert Decimal(str(tx.amount)) == Decimal("3500.00")


# --- находки финального аудита (20.07) ------------------------------------------------------


async def test_invoice_remaining_accounts_goods_returns_for_loans(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Остаток займа считается с учётом ТОВАРНЫХ возвратов в ЕДИНОЙ точке `_invoice_remaining`.

    Её зовут все двери оплаты (paid-переход после исполнения платежа банком, FIFO по черновику,
    сверка, выдача с Сейфа, классификатор). Точечные поправки в отдельных дверях оставляли
    остальные считать долг по аллокациям — и платить за уже возвращённый товар второй раз.
    """
    from app.services.counterparty_matching import _invoice_remaining

    async with async_session_factory() as session:
        loan, items = await _loan(session, inn="6177002001", lines=[("Моцарелла", "10", "300")])
        line, loan_id = items[0], loan.id
        await session.commit()

        await create_barter_return(
            session, loan_id=loan_id, issued_at=RET_AT,
            returns=[
                ReturnLineInput(
                    amount=Decimal("1800"), loan_line_item_id=line.id, quantity=Decimal("6")
                )
            ],
        )
        refreshed = await session.get(SupplierInvoice, loan_id)
        # Аллокаций нет вовсе — по ним остаток был бы 3000. Товар вернули на 1800.
        assert await _invoice_remaining(session, refreshed) == Decimal("1200.00")


async def test_loan_in_bank_draft_rejects_goods_return(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Заём, отправленный в банк, нельзя гасить товаром до отзыва платежа.

    Иначе: черновик висит на подписи → возвращаем весь товар → заём закрыт без аллокации →
    банк исполняет платёж → paid-переход платит второй раз. И товар у нас, и деньги ушли.
    """
    from app.services.counterparty_payments import create_payment_draft_for_invoices

    async with async_session_factory() as session:
        loan, items = await _loan(
            session, inn="6177002002", lines=[("Моцарелла", "10", "300")], bankable=True
        )
        line, loan_id = items[0], loan.id
        await session.commit()

        await create_payment_draft_for_invoices(
            session, invoice_ids=[loan_id], actor_user_id=None
        )
        refreshed = await session.get(SupplierInvoice, loan_id)
        assert refreshed.draft_id is not None, "предусловие: заём в банке"

        with pytest.raises(WarehouseInvoiceError, match="отправлен в банк"):
            await create_barter_return(
                session, loan_id=loan_id, issued_at=RET_AT,
                returns=[
                    ReturnLineInput(
                        amount=Decimal("3000"), loan_line_item_id=line.id, quantity=Decimal("10")
                    )
                ],
            )


async def test_over_return_within_tolerance_keeps_document_consistent(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Довесок срезается и в СТРОКАХ, а не только в шапке: документ не расходится сам с собой.

    Иначе шапка возвратной накладной одна, сумма её позиций другая, а в iiko уходит третья —
    и баланс поставщика у нас и в iiko разъезжается.
    """
    async with async_session_factory() as session:
        # Заём 2.6 кг × 470.48 = 1223.25 (копеечный дрейф округления как у тилапии).
        loan, items = await _loan(
            session, inn="6177002003", lines=[("Тилапия", "2.6", "470.48")]
        )
        line, loan_id = items[0], loan.id
        await session.commit()

        ret = await create_barter_return(
            session, loan_id=loan_id, issued_at=RET_AT,
            returns=[
                ReturnLineInput(
                    amount=Decimal("1270"), loan_line_item_id=line.id, quantity=Decimal("2.7")
                )
            ],
        )
        lines_sum = await session.scalar(
            select(func.coalesce(func.sum(InvoiceLineItem.sum), 0)).where(
                InvoiceLineItem.invoice_id == ret.id
            )
        )
        ledger_sum = await session.scalar(
            select(func.coalesce(func.sum(BarterReturnLine.amount), 0)).where(
                BarterReturnLine.return_invoice_id == ret.id
            )
        )
        # Шапка = позиции = леджер: одна и та же цифра уйдёт и в iiko.
        assert Decimal(str(ret.amount)) == Decimal(str(lines_sum)) == Decimal(str(ledger_sum))

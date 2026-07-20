"""Контрагент — свойство ДОЛИ разбора, а не операции.

Один платёж покрывает расходы разных контрагентов («овощи + коробки + мусорщики» одним
переводом), поэтому каждая доля несёт своего контрагента: её проводка ДДС, её дебиторка
(правило 1) и гашение её накладной идут в карточку именно этого контрагента.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from cp_helpers import (
    make_account,
    make_bank_operation,
    make_counterparty,
    make_expense_article,
    make_invoice,
    make_wallet,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    CashflowTransaction,
    CounterpartyPayableProfile,
    InvoicePaymentAllocation,
    SupplierInvoice,
    SupplierPrepayment,
)
from app.services.banking.classifier import OperationSplitLine, apply_operation_split


async def _bank_fixture(session: AsyncSession, *, op_amount: str):
    account = await make_account(session)
    await make_wallet(session, wallet_type="bank", account_id=account.id)
    article = await make_expense_article(session)  # code=payment_to_supplier
    op = await make_bank_operation(
        session, amount=op_amount, direction="out", account_id=account.id
    )
    return account, article, op


async def test_split_books_each_line_to_own_counterparty(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Две доли одного платежа → две проводки, каждая в карточке своего контрагента."""
    async with async_session_factory() as session:
        _account, article, op = await _bank_fixture(session, op_amount="8000.00")
        veggies = await make_counterparty(session, name="Поставка овощей", inn="7701234567")
        boxes = await make_counterparty(session, name="Коробки", inn="7801234567")
        await session.commit()

        created_ids = await apply_operation_split(
            session,
            op,
            splits=[
                OperationSplitLine(article.id, Decimal("5000.00"), counterparty_id=veggies.id),
                OperationSplitLine(article.id, Decimal("3000.00"), counterparty_id=boxes.id),
            ],
        )
        await session.commit()

        first = await session.get(CashflowTransaction, created_ids[0])
        second = await session.get(CashflowTransaction, created_ids[1])
        assert first.counterparty_id == veggies.id
        assert second.counterparty_id == boxes.id


async def test_split_line_without_counterparty_falls_back_to_common(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Общий контрагент разбора остаётся дефолтом для долей без своего (прежние вызовы)."""
    async with async_session_factory() as session:
        _account, article, op = await _bank_fixture(session, op_amount="8000.00")
        common = await make_counterparty(session, name="Общий", inn="7701234567")
        own = await make_counterparty(session, name="Свой", inn="7801234567")
        await session.commit()

        created_ids = await apply_operation_split(
            session,
            op,
            splits=[
                OperationSplitLine(article.id, Decimal("5000.00")),
                OperationSplitLine(article.id, Decimal("3000.00"), counterparty_id=own.id),
            ],
            counterparty_id=common.id,
        )
        await session.commit()

        assert (await session.get(CashflowTransaction, created_ids[0])).counterparty_id == common.id
        assert (await session.get(CashflowTransaction, created_ids[1])).counterparty_id == own.id


async def test_split_pays_invoices_of_two_counterparties(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Один платёж гасит накладные РАЗНЫХ контрагентов — каждая долей своего контрагента."""
    async with async_session_factory() as session:
        _account, article, op = await _bank_fixture(session, op_amount="8000.00")
        veggies = await make_counterparty(session, name="Поставка овощей", inn="7701234567")
        boxes = await make_counterparty(session, name="Коробки", inn="7801234567")
        inv_veggies = await make_invoice(
            session, counterparty_id=veggies.id, amount="5000.00", number="О-1"
        )
        inv_boxes = await make_invoice(
            session, counterparty_id=boxes.id, amount="3000.00", number="К-1"
        )
        await session.commit()
        veggies_invoice_id, boxes_invoice_id = inv_veggies.id, inv_boxes.id

        created_ids = await apply_operation_split(
            session,
            op,
            splits=[
                OperationSplitLine(
                    article.id,
                    Decimal("5000.00"),
                    invoice_id=veggies_invoice_id,
                    counterparty_id=veggies.id,
                ),
                OperationSplitLine(
                    article.id,
                    Decimal("3000.00"),
                    invoice_id=boxes_invoice_id,
                    counterparty_id=boxes.id,
                ),
            ],
        )
        await session.commit()

        assert (await session.get(SupplierInvoice, veggies_invoice_id)).payment_status == "paid"
        assert (await session.get(SupplierInvoice, boxes_invoice_id)).payment_status == "paid"
        # Каждая аллокация помечена И операцией, И своей долей — иначе «бюджет платежа»
        # считался бы по всей операции и доли съедали бы деньги друг друга.
        allocations = (
            await session.scalars(
                select(InvoicePaymentAllocation).where(
                    InvoicePaymentAllocation.bank_operation_id == op.id
                )
            )
        ).all()
        assert {alloc.cashflow_transaction_id for alloc in allocations} == set(created_ids)


async def test_split_rejects_invoice_of_another_counterparty_in_line(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Гард переехал на строку: накладную гасит только доля её собственного контрагента."""
    async with async_session_factory() as session:
        _account, article, op = await _bank_fixture(session, op_amount="5000.00")
        supplier = await make_counterparty(session, name="Поставщик", inn="7701234567")
        other = await make_counterparty(session, name="Другой", inn="7801234567")
        invoice = await make_invoice(session, counterparty_id=supplier.id, amount="5000.00")
        await session.commit()

        with pytest.raises(ValueError, match="не совпадает с контрагентом накладной"):
            await apply_operation_split(
                session,
                op,
                splits=[
                    OperationSplitLine(
                        article.id,
                        Decimal("5000.00"),
                        invoice_id=invoice.id,
                        counterparty_id=other.id,
                    )
                ],
            )

        assert (await session.get(SupplierInvoice, invoice.id)).payment_status == "unpaid"


async def test_split_rejects_same_invoice_in_two_lines(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Одна накладная в двух долях ушла бы в переплату: проверка остатка видит лишь первую."""
    async with async_session_factory() as session:
        _account, article, op = await _bank_fixture(session, op_amount="8000.00")
        supplier = await make_counterparty(session, name="Поставщик", inn="7701234567")
        invoice = await make_invoice(session, counterparty_id=supplier.id, amount="5000.00")
        await session.commit()

        with pytest.raises(ValueError, match="двумя строками"):
            await apply_operation_split(
                session,
                op,
                splits=[
                    OperationSplitLine(
                        article.id,
                        Decimal("5000.00"),
                        invoice_id=invoice.id,
                        counterparty_id=supplier.id,
                    ),
                    OperationSplitLine(
                        article.id,
                        Decimal("3000.00"),
                        invoice_id=invoice.id,
                        counterparty_id=supplier.id,
                    ),
                ],
            )


async def test_supplier_payment_line_requires_counterparty(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«Оплата поставщикам» без контрагента — расход в никуда: не в карточку и не в ДЗ/КЗ."""
    async with async_session_factory() as session:
        _account, article, op = await _bank_fixture(session, op_amount="5000.00")
        await session.commit()

        with pytest.raises(ValueError, match="Оплата поставщикам"):
            await apply_operation_split(
                session,
                op,
                splits=[OperationSplitLine(article.id, Decimal("5000.00"))],
            )


async def test_supplier_payment_line_takes_counterparty_from_invoice(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Доля с накладной контрагента уже «знает» его — отдельно выбирать не заставляем."""
    async with async_session_factory() as session:
        _account, article, op = await _bank_fixture(session, op_amount="5000.00")
        supplier = await make_counterparty(session, name="Поставщик", inn="7701234567")
        invoice = await make_invoice(session, counterparty_id=supplier.id, amount="5000.00")
        await session.commit()

        created_ids = await apply_operation_split(
            session,
            op,
            splits=[OperationSplitLine(article.id, Decimal("5000.00"), invoice_id=invoice.id)],
        )
        await session.commit()

        assert (await session.get(CashflowTransaction, created_ids[0])).counterparty_id == (
            supplier.id
        )


async def test_advance_line_requires_own_counterparty(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Аванс поставщику рождает дебиторку — доля без контрагента не проходит даже с соседкой."""
    async with async_session_factory() as session:
        _account, article, op = await _bank_fixture(session, op_amount="8000.00")
        advance_article = await make_expense_article(
            session, code="advance_to_supplier", name="Авансы поставщикам"
        )
        supplier = await make_counterparty(session, name="Поставщик", inn="7701234567")
        await session.commit()

        with pytest.raises(ValueError, match="Авансы поставщикам"):
            await apply_operation_split(
                session,
                op,
                splits=[
                    OperationSplitLine(
                        article.id, Decimal("5000.00"), counterparty_id=supplier.id
                    ),
                    OperationSplitLine(advance_article.id, Decimal("3000.00")),
                ],
            )


async def test_advance_line_books_prepayment_on_line_counterparty(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Дебиторка аванса идёт на контрагента СВОЕЙ доли, а не на контрагента соседней."""
    async with async_session_factory() as session:
        _account, article, op = await _bank_fixture(session, op_amount="8000.00")
        advance_article = await make_expense_article(
            session, code="advance_to_supplier", name="Авансы поставщикам"
        )
        veggies = await make_counterparty(session, name="Поставка овощей", inn="7701234567")
        boxes = await make_counterparty(session, name="Коробки", inn="7801234567")
        await session.commit()

        created_ids = await apply_operation_split(
            session,
            op,
            splits=[
                OperationSplitLine(article.id, Decimal("5000.00"), counterparty_id=veggies.id),
                OperationSplitLine(
                    advance_article.id, Decimal("3000.00"), counterparty_id=boxes.id
                ),
            ],
        )
        await session.commit()

        prepayment = (
            await session.scalars(
                select(SupplierPrepayment).where(
                    SupplierPrepayment.cashflow_transaction_id == created_ids[1]
                )
            )
        ).one()
        assert prepayment.counterparty_id == boxes.id
        assert prepayment.amount == Decimal("3000.00")


async def test_line_budget_is_not_eaten_by_other_counterparty_line(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Доля с предоплатной моделью считает бюджет по СЕБЕ, а не по всей операции.

    Раньше «бюджет платежа» строки-якоря включал гашения ВСЕХ долей операции (мост через
    ``BankOperation.cashflow_transaction_id``), поэтому оплата накладной контрагента Б съедала
    деньги доли контрагента А и её дебиторка выходила заниженной.
    """
    async with async_session_factory() as session:
        _account, article, op = await _bank_fixture(session, op_amount="8000.00")
        # Доля-якорь: контрагент с предоплатной моделью по банк-фиду (кейс Манго) → правило 1
        # заводит на неё дебиторку на всю сумму доли.
        mango = await make_counterparty(session, name="Манго Телеком", inn="7709501144")
        profile = (
            await session.execute(
                select(CounterpartyPayableProfile).where(
                    CounterpartyPayableProfile.counterparty_id == mango.id
                )
            )
        ).scalar_one()
        profile.bank_payments_create_prepayment = True
        boxes = await make_counterparty(session, name="Коробки", inn="7801234567")
        inv_boxes = await make_invoice(
            session, counterparty_id=boxes.id, amount="3000.00", number="К-1"
        )
        await session.commit()
        boxes_invoice_id = inv_boxes.id

        created_ids = await apply_operation_split(
            session,
            op,
            splits=[
                OperationSplitLine(article.id, Decimal("5000.00"), counterparty_id=mango.id),
                OperationSplitLine(
                    article.id,
                    Decimal("3000.00"),
                    invoice_id=boxes_invoice_id,
                    counterparty_id=boxes.id,
                ),
            ],
        )
        await session.commit()

        prepayment = (
            await session.scalars(
                select(SupplierPrepayment).where(
                    SupplierPrepayment.cashflow_transaction_id == created_ids[0]
                )
            )
        ).one()
        assert prepayment.counterparty_id == mango.id
        assert prepayment.amount == Decimal("5000.00")  # не 2000 = 5000 − гашение чужой доли


async def test_exclude_after_split_drops_prepayments_of_all_lines(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Исключение операции снимает дебиторку ВСЕХ долей, а не только якорной."""
    from app.services.banking.classifier import apply_operation_action

    async with async_session_factory() as session:
        _account, article, op = await _bank_fixture(session, op_amount="8000.00")
        advance_article = await make_expense_article(
            session, code="advance_to_supplier", name="Авансы поставщикам"
        )
        veggies = await make_counterparty(session, name="Поставка овощей", inn="7701234567")
        boxes = await make_counterparty(session, name="Коробки", inn="7801234567")
        await session.commit()

        await apply_operation_split(
            session,
            op,
            splits=[
                OperationSplitLine(
                    advance_article.id, Decimal("5000.00"), counterparty_id=veggies.id
                ),
                OperationSplitLine(
                    advance_article.id, Decimal("3000.00"), counterparty_id=boxes.id
                ),
            ],
        )
        await session.commit()
        assert await session.scalar(select(func.count()).select_from(SupplierPrepayment)) == 2

        await apply_operation_action(session, op, action="exclude")
        await session.commit()

        assert await session.scalar(select(func.count()).select_from(SupplierPrepayment)) == 0

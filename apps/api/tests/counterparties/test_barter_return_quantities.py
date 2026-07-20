"""Возврат бартерного займа по КИЛОГРАММАМ: допуск перевозврата и судьба недовеса.

Правила владельца (2026-07-19). Долг займа номинирован ТОВАРОМ, отсюда вся арифметика:
1. вернуть можно на 100 г больше выданного (довесок при фасовке) — сверх этого ошибка;
2. перевозврат НЕ увеличивает сумму долга: лишние граммы уходят по более низкой цене за кг;
3. возврат НЕ создаёт дебиторку — вернули больше, значит рассчитались в ноль;
4. недовес либо остаётся долгом, либо списывается («про эти 60 грамм просто забываю»).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from cp_helpers import make_counterparty, make_expense_article
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import IikoProduct, InvoiceLineItem, SupplierInvoice
from app.services.warehouse_invoices import (
    LineInput,
    ReturnLineInput,
    WarehouseInvoiceError,
    create_barter_return,
    create_warehouse_invoice,
    loan_settled_value,
)

ISSUED = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
RETURNED = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)


async def _product(session: AsyncSession, name: str = "Тилапия") -> IikoProduct:
    product = IikoProduct(
        iiko_id=str(uuid.uuid4()), name=name, type="GOODS", unit="кг",
        synced_at=datetime.now(UTC),
    )
    session.add(product)
    await session.flush()
    return product


async def _loan(
    session: AsyncSession, *, inn: str, qty: str = "3.000", price: str = "400.00"
) -> tuple[SupplierInvoice, InvoiceLineItem]:
    """Их заём (мы должны): выдали нам qty кг по price."""
    cp = await make_counterparty(session, name=f"Партнёр-{inn}", inn=inn)
    await make_expense_article(session, code="payment_to_supplier", name="Оплата поставщикам")
    product = await _product(session)
    await session.commit()
    loan = await create_warehouse_invoice(
        session,
        counterparty_id=cp.id,
        issued_at=ISSUED,
        mode="loan",
        we_lend=False,
        lines=[
            LineInput(
                name="Тилапия", quantity=Decimal(qty), price=Decimal(price),
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


async def test_overreturn_beyond_tolerance_is_rejected(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Заняли 3 кг, вводят 3,5 — система тормозит (это ошибка ввода, а не довесок)."""
    async with async_session_factory() as session:
        loan, line = await _loan(session, inn="6166000001")
        await session.commit()

        with pytest.raises(WarehouseInvoiceError, match="превышает выданное"):
            await create_barter_return(
                session,
                loan_id=loan.id,
                issued_at=RETURNED,
                returns=[
                    ReturnLineInput(
                        amount=Decimal("1400"), loan_line_item_id=line.id,
                        quantity=Decimal("3.500"),
                    )
                ],
            )


async def test_overreturn_within_tolerance_closes_loan_without_receivable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Вернули 3,1 кг вместо 3 — принимаем, долг закрыт РОВНО суммой займа.

    Сумма не растёт (возврат идёт по более низкой цене за кг) и дебиторка не появляется:
    вернули больше — рассчитались в ноль, а не «нам должны».
    """
    async with async_session_factory() as session:
        loan, line = await _loan(session, inn="6166000002")
        await session.commit()
        loan_id = loan.id

        ret = await create_barter_return(
            session,
            loan_id=loan_id,
            issued_at=RETURNED,
            returns=[
                ReturnLineInput(
                    amount=Decimal("1240"), loan_line_item_id=line.id,
                    quantity=Decimal("3.100"),
                )
            ],
        )

        refreshed = await session.get(SupplierInvoice, loan_id)
        # Долг закрыт ровно суммой займа: ни рубля сверху.
        assert await loan_settled_value(session, refreshed) == Decimal("1200.00")
        assert Decimal(str(ret.amount)) == Decimal("1200.00")
        assert refreshed.barter_return_status == "returned"
        assert refreshed.payment_status == "paid"


async def test_under_return_keeps_remainder_by_default(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Вернули 2,94 из 3 кг и НЕ списываем — хвост остаётся долгом."""
    async with async_session_factory() as session:
        loan, line = await _loan(session, inn="6166000003")
        await session.commit()
        loan_id = loan.id

        await create_barter_return(
            session,
            loan_id=loan_id,
            issued_at=RETURNED,
            returns=[
                ReturnLineInput(
                    amount=Decimal("1176"), loan_line_item_id=line.id,
                    quantity=Decimal("2.940"),
                )
            ],
        )

        refreshed = await session.get(SupplierInvoice, loan_id)
        assert refreshed.barter_return_status == "partially_returned"
        # 0,06 кг × 400 = 24 ₽ остались висеть долгом.
        assert Decimal(str(refreshed.amount)) - await loan_settled_value(
            session, refreshed
        ) == Decimal("24.00")


async def test_under_return_with_write_off_closes_loan(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Тот же недовес, но владелец решил забыть про хвост — заём закрыт, КЗ обнулена."""
    async with async_session_factory() as session:
        loan, line = await _loan(session, inn="6166000004")
        await session.commit()
        loan_id = loan.id

        await create_barter_return(
            session,
            loan_id=loan_id,
            issued_at=RETURNED,
            returns=[
                ReturnLineInput(
                    amount=Decimal("1176"), loan_line_item_id=line.id,
                    quantity=Decimal("2.940"),
                )
            ],
            write_off_remainder=True,
        )

        refreshed = await session.get(SupplierInvoice, loan_id)
        assert Decimal(str(refreshed.barter_writeoff_amount)) == Decimal("24.00")
        assert refreshed.barter_return_status == "returned"
        assert refreshed.payment_status == "paid"
        assert await loan_settled_value(session, refreshed) == Decimal("1200.00")


async def test_write_off_accumulates_across_partial_returns(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Списание копится, а не перетирается: два недовоза подряд закрывают заём суммарно."""
    async with async_session_factory() as session:
        loan, line = await _loan(session, inn="6166000005")
        await session.commit()
        loan_id = loan.id

        # Первый возврат — часть товара, хвост НЕ списываем.
        await create_barter_return(
            session,
            loan_id=loan_id,
            issued_at=RETURNED,
            returns=[
                ReturnLineInput(
                    amount=Decimal("800"), loan_line_item_id=line.id, quantity=Decimal("2.000")
                )
            ],
        )
        mid = await session.get(SupplierInvoice, loan_id)
        assert mid.barter_return_status == "partially_returned"

        # Второй возврат меньше остатка, хвост прощаем.
        await create_barter_return(
            session,
            loan_id=loan_id,
            issued_at=RETURNED,
            returns=[
                ReturnLineInput(
                    amount=Decimal("360"), loan_line_item_id=line.id, quantity=Decimal("0.900")
                )
            ],
            write_off_remainder=True,
        )

        refreshed = await session.get(SupplierInvoice, loan_id)
        assert Decimal(str(refreshed.barter_writeoff_amount)) == Decimal("40.00")
        assert refreshed.barter_return_status == "returned"


async def test_split_lines_cannot_bypass_tolerance(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Две строки одной позиции в одном запросе не обходят лимит (лимит копится в снимке)."""
    async with async_session_factory() as session:
        loan, line = await _loan(session, inn="6166000006")
        await session.commit()

        with pytest.raises(WarehouseInvoiceError, match="превышает выданное"):
            await create_barter_return(
                session,
                loan_id=loan.id,
                issued_at=RETURNED,
                returns=[
                    ReturnLineInput(
                        amount=Decimal("800"), loan_line_item_id=line.id,
                        quantity=Decimal("2.000"),
                    ),
                    ReturnLineInput(
                        amount=Decimal("800"), loan_line_item_id=line.id,
                        quantity=Decimal("2.000"),
                    ),
                ],
            )

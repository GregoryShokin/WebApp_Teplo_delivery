"""Эталон суммы строки накладной.

Цена хранится в 2 знака, поэтому `сумма ÷ кол-во` не всегда даёт целые копейки
(80 ед. за 3298 ₽ = 41,225 ₽/ед → округляется до 41,23 → 80×41,23 = 3298,40).
Раньше бэк пересчитывал строку как кол-во×цена и итог накладной «уезжал»
(введённые 6179,87 сохранялись как 6180,19). Теперь при явной `LineInput.sum`
она хранится как есть; без неё — обратная совместимость (кол-во×цена).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from cp_helpers import make_counterparty, make_iiko_product
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.counterparty_payable import InvoiceLineItem
from app.services.warehouse_invoices import LineInput, create_warehouse_invoice


async def test_explicit_line_sum_is_kept_not_recomputed(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Сценарий со снимков: мука 80×41,23 (сумма 3298) + сахар 20×88,46 (сумма 1769,28).
    Кол-во×цена дали бы 3298,40 и 1769,20 (итог 5067,60), но эталон — введённые суммы."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ИП Карпов", inn="7700002001")
        muka = await make_iiko_product(session, name="мука донские дары")
        sahar = await make_iiko_product(session, name="Сахар")
        invoice = await create_warehouse_invoice(
            session,
            counterparty_id=cp.id,
            issued_at=datetime(2026, 7, 16, 19, 18, tzinfo=UTC),
            lines=[
                LineInput(
                    name="мука донские дары",
                    quantity=Decimal("80"),
                    price=Decimal("41.23"),
                    iiko_product_id=muka.id,
                    sum=Decimal("3298.00"),
                ),
                LineInput(
                    name="Сахар",
                    quantity=Decimal("20"),
                    price=Decimal("88.46"),
                    iiko_product_id=sahar.id,
                    sum=Decimal("1769.28"),
                ),
            ],
            number="32",
            source="kassa_invoice",
        )
        await session.commit()

        # Итог = сумма введённых сумм строк, а не Σ(кол-во×цена).
        assert invoice.amount == Decimal("5067.28")

        rows = (
            await session.scalars(
                select(InvoiceLineItem)
                .where(InvoiceLineItem.invoice_id == invoice.id)
                .order_by(InvoiceLineItem.sort_order)
            )
        ).all()
        # Суммы строк сохранены как эталон; цена — как ввёл пользователь (справочно).
        assert [r.sum for r in rows] == [Decimal("3298.00"), Decimal("1769.28")]
        assert [r.price for r in rows] == [Decimal("41.23"), Decimal("88.46")]


async def test_line_sum_absent_falls_back_to_qty_times_price(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Без явной суммы (старый клиент) — прежнее поведение: строка = кол-во×цена."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ИП Егиазарян", inn="7700002002")
        product = await make_iiko_product(session, name="мука")
        invoice = await create_warehouse_invoice(
            session,
            counterparty_id=cp.id,
            issued_at=datetime(2026, 7, 16, 19, 18, tzinfo=UTC),
            lines=[
                LineInput(
                    name="мука",
                    quantity=Decimal("80"),
                    price=Decimal("41.23"),
                    iiko_product_id=product.id,
                )
            ],
            number="33",
            source="kassa_invoice",
        )
        await session.commit()
        assert invoice.amount == Decimal("3298.40")

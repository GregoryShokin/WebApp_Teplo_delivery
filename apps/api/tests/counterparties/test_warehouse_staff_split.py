"""Накладная, блок «Траты на персонал»: персональные строки несут свою статью ДДС, а
staff-часть оплаты разносится по статьям этих строк (вместо одной staff-статьи). Старые
накладные без статей на строках падают на прежнюю единую staff-статью. Прогон на teplo_test.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from cp_helpers import make_counterparty, make_expense_article, make_wallet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import InvoiceLineItem
from app.services.warehouse_invoices import LineInput, create_warehouse_invoice
from app.services.warehouse_payments import (
    build_staff_split_cash_parts,
    pay_invoice_split,
    resolve_staff_article_id,
)

ISSUED = datetime(2026, 6, 17, 12, 0, tzinfo=UTC)
OP_DATE = date(2026, 6, 17)
FOOD = "Расходы на питание персонала"
PERS = "Расходы на персонал"


async def _articles(session: AsyncSession):
    food = await make_expense_article(session, code="pitanie", name=FOOD)
    pers = await make_expense_article(session, code="persrash", name=PERS)
    await make_expense_article(session, code="payment_to_supplier", name="Оплата поставщикам")
    await make_expense_article(
        session, code="supplier_staff_payment", name="Оплата поставщику (персонал)"
    )
    return food, pers


async def _lines(session: AsyncSession, invoice_id):
    return list(
        (
            await session.scalars(
                select(InvoiceLineItem)
                .where(InvoiceLineItem.invoice_id == invoice_id)
                .order_by(InvoiceLineItem.sort_order)
            )
        ).all()
    )


async def test_create_invoice_with_staff_lines(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        food, pers = await _articles(session)
        cp = await make_counterparty(session, name="Поставщик")
        await session.commit()

        invoice = await create_warehouse_invoice(
            session,
            counterparty_id=cp.id,
            issued_at=ISSUED,
            lines=[
                LineInput(name="Лосось", quantity=Decimal("5"), price=Decimal("1000")),
                LineInput(
                    name="Обеды поварам",
                    quantity=Decimal("1"),
                    price=Decimal("800"),
                    is_staff=True,
                    dds_article_id=food.id,
                ),
                LineInput(
                    name="Спецодежда",
                    quantity=Decimal("1"),
                    price=Decimal("1200"),
                    is_staff=True,
                    dds_article_id=pers.id,
                ),
            ],
        )

        assert invoice.amount == Decimal("7000.00")  # 5000 склад + 800 + 1200 персонал
        assert invoice.staff_amount == Decimal("2000.00")
        lines = await _lines(session, invoice.id)
        # Товарная строка — без статьи и без флага; персональные — каждая со своей статьёй.
        assert lines[0].is_staff is False and lines[0].dds_article_id is None
        assert lines[1].is_staff is True and lines[1].dds_article_id == food.id
        assert lines[2].is_staff is True and lines[2].dds_article_id == pers.id


async def test_staff_split_books_by_line_articles(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        food, pers = await _articles(session)
        cp = await make_counterparty(session, name="Поставщик")
        await session.commit()

        invoice = await create_warehouse_invoice(
            session,
            counterparty_id=cp.id,
            issued_at=ISSUED,
            lines=[
                LineInput(name="Лосось", quantity=Decimal("5"), price=Decimal("1000")),
                LineInput(
                    name="Обеды",
                    quantity=Decimal("1"),
                    price=Decimal("800"),
                    is_staff=True,
                    dds_article_id=food.id,
                ),
                LineInput(
                    name="Спецодежда",
                    quantity=Decimal("1"),
                    price=Decimal("1200"),
                    is_staff=True,
                    dds_article_id=pers.id,
                ),
            ],
        )
        wallet = await make_wallet(session, name="ТК Черникова", wallet_type="store_cash")
        await session.commit()

        parts = await build_staff_split_cash_parts(
            session, invoice, wallet_id=wallet.id, operation_date=OP_DATE
        )
        by_article = {p.article_id: p.amount for p in parts}
        # Staff-часть разнесена по статьям строк, а не свалена в одну staff-статью.
        assert by_article[food.id] == Decimal("800.00")
        assert by_article[pers.id] == Decimal("1200.00")
        # Производственная часть (склад) присутствует отдельно.
        assert Decimal("5000.00") in {p.amount for p in parts}

        result = await pay_invoice_split(session, invoice_id=invoice.id, cash_parts=parts)
        assert result.payment_status == "paid"


async def test_staff_split_fallback_when_lines_have_no_article(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Старые накладные: персоналка есть, но строки без статьи → прежняя единая staff-статья.
    async with async_session_factory() as session:
        await _articles(session)
        cp = await make_counterparty(session, name="Поставщик")
        await session.commit()

        invoice = await create_warehouse_invoice(
            session,
            counterparty_id=cp.id,
            issued_at=ISSUED,
            lines=[
                LineInput(name="Лосось", quantity=Decimal("5"), price=Decimal("1000")),
                LineInput(
                    name="Персоналка без статьи",
                    quantity=Decimal("1"),
                    price=Decimal("2000"),
                    is_staff=True,
                ),
            ],
        )
        wallet = await make_wallet(session, name="ТК Черникова", wallet_type="store_cash")
        staff_article_id = await resolve_staff_article_id(session)
        await session.commit()

        parts = await build_staff_split_cash_parts(
            session, invoice, wallet_id=wallet.id, operation_date=OP_DATE
        )
        by_article = {p.article_id: p.amount for p in parts}
        assert by_article[staff_article_id] == Decimal("2000.00")

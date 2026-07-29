"""Перепроводка ДДС по статьям позиций после правки ОПЛАЧЕННОГО документа.

Разнос оплаты по статьям считается из строк один раз — при создании чека / оплате накладной.
Правка оплаченной пересобирает строки, поэтому разрез надо пересчитать: иначе статьи позиций и
статьи проводок расходятся молча (чеки Ч-54/Ч-55, 27.07.2026). Проверяем: суммы/счета/даты и
привязки оплат при этом не двигаются, ручная разметка авто-вызовом не перебивается, а явная
кнопка перебивает и её. teplo_test.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from cp_helpers import (
    make_account,
    make_bank_operation,
    make_counterparty,
    make_expense_article,
    make_iiko_product,
    make_wallet,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import CashflowTransaction, InvoicePaymentAllocation
from app.services.kassa.cheque import ChequeBankPart, ChequeLineInput, create_cheque
from app.services.warehouse_dds_reproject import (
    dds_articles_mismatch,
    reproject_invoice_dds,
)
from app.services.warehouse_invoices import LineInput, adjust_paid_invoice

pytestmark = pytest.mark.usefixtures("migrated_db")

ISSUED = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
OP_DATE = date(2026, 7, 20)


async def _cheque_475(session: AsyncSession):
    """Чек на 475 ₽ картой: ВСЯ сумма заведена расходом «Содержание торговых точек».

    Так на кассе оформляют покупку мимо склада — ровно исходное состояние Ч-54.
    """
    supplier_article = await make_expense_article(
        session, code="payment_to_supplier", name="Оплата поставщикам"
    )
    upkeep_article = await make_expense_article(
        session, code="venue_upkeep", name="Содержание торговых точек"
    )
    cp = await make_counterparty(session, name="Местный закуп")
    saucer = await make_iiko_product(session, name="Соусник бутылка", unit="шт")
    lid = await make_iiko_product(session, name="Крышка для бутылок", unit="шт")
    account = await make_account(session)
    await make_wallet(session, name="Тинькофф карта", wallet_type="bank", account_id=account.id)
    op = await make_bank_operation(
        session,
        amount="475.00",
        operation_date=OP_DATE,
        posted_at=ISSUED,
        category="cardOperation",
        account_id=account.id,
    )
    await session.commit()

    cheque = await create_cheque(
        session,
        counterparty_id=cp.id,
        issued_at=ISSUED,
        bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
        track_nomenclature=True,
        lines=[
            ChequeLineInput(
                name="Соусник + крышки + пакет",
                quantity=Decimal("1"),
                price=Decimal("475.00"),
                amount=Decimal("475.00"),
                dds_article_id=upkeep_article.id,
            )
        ],
    )
    return cheque, supplier_article, upkeep_article, saucer, lid


async def _txn_by_article(session: AsyncSession, invoice_id) -> dict[str, Decimal]:
    rows = (
        await session.scalars(
            select(CashflowTransaction).where(CashflowTransaction.source_id == invoice_id)
        )
    ).all()
    from app.models import DdsArticle

    out: dict[str, Decimal] = {}
    for row in rows:
        name = "—"
        if row.article_id is not None:
            name = await session.scalar(
                select(DdsArticle.name).where(DdsArticle.id == row.article_id)
            )
        out[name] = out.get(name, Decimal("0.00")) + Decimal(row.amount)
    return out


def _corrected_lines(supplier_article, saucer, lid, upkeep_article) -> list[LineInput]:
    """Правка «как из окна»: 468 ₽ товара с номенклатурой + 7 ₽ расхода (пакет). Итог тот же 475."""
    return [
        LineInput(
            name="Соусник бутылка 0.097",
            quantity=Decimal("50"),
            price=Decimal("7.72"),
            sum=Decimal("386.00"),
            iiko_product_id=saucer.id,
            dds_article_id=supplier_article.id,
        ),
        LineInput(
            name="Крышка для бутылок",
            quantity=Decimal("50"),
            price=Decimal("1.64"),
            sum=Decimal("82.00"),
            iiko_product_id=lid.id,
            dds_article_id=supplier_article.id,
        ),
        LineInput(
            name="пакет",
            quantity=Decimal("1"),
            price=Decimal("7.00"),
            sum=Decimal("7.00"),
            dds_article_id=upkeep_article.id,
        ),
    ]


async def test_adjust_paid_reprojects_dds_by_new_articles(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Правка оплаченного чека переразносит проводки ДДС: 475 «Содержание» → 468 товар + 7 расход."""
    async with async_session_factory() as session:
        cheque, supplier, upkeep, saucer, lid = await _cheque_475(session)
        assert await _txn_by_article(session, cheque.id) == {"Содержание торговых точек": Decimal("475.00")}

        await adjust_paid_invoice(
            session, cheque, lines=_corrected_lines(supplier, saucer, lid, upkeep)
        )

        assert await _txn_by_article(session, cheque.id) == {
            "Оплата поставщикам": Decimal("468.00"),
            "Содержание торговых точек": Decimal("7.00"),
        }
        assert await dds_articles_mismatch(session, cheque) is False


async def test_reproject_keeps_payment_anchor_and_amounts(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Переразнос не двигает деньги: та же сумма, тот же кошелёк и дата, аллокация цела.

    Первая проводка части — якорь: на неё ссылаются аллокация оплаты и банковская операция.
    Удалить её значило бы оборвать связь «операция ↔ расход» и вернуть операцию в разбор.
    """
    async with async_session_factory() as session:
        cheque, supplier, upkeep, saucer, lid = await _cheque_475(session)
        before = (
            await session.scalars(
                select(CashflowTransaction).where(CashflowTransaction.source_id == cheque.id)
            )
        ).all()
        anchor_id = before[0].id
        wallet_id, op_date = before[0].wallet_id, before[0].operation_date

        await adjust_paid_invoice(
            session, cheque, lines=_corrected_lines(supplier, saucer, lid, upkeep)
        )

        after = list(
            (
                await session.scalars(
                    select(CashflowTransaction).where(CashflowTransaction.source_id == cheque.id)
                )
            ).all()
        )
        assert sum(Decimal(t.amount) for t in after) == Decimal("475.00")
        assert {t.wallet_id for t in after} == {wallet_id}
        assert {t.operation_date for t in after} == {op_date}
        assert anchor_id in {t.id for t in after}  # якорь пережил перепроводку
        allocation = await session.scalar(
            select(InvoicePaymentAllocation).where(
                InvoicePaymentAllocation.invoice_id == cheque.id
            )
        )
        assert allocation is not None and Decimal(allocation.amount) == Decimal("475.00")


async def test_auto_reproject_does_not_touch_manual_markup(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Проводку, размеченную вручную (manual_override), авто-перепроводка обходит.

    Человек ставил разметку осознанно в окне «Разобрать» — молча перебивать её правкой позиций
    нельзя (решение владельца 29.07.2026). Расхождение при этом остаётся видимым.
    """
    async with async_session_factory() as session:
        cheque, supplier, upkeep, saucer, lid = await _cheque_475(session)
        txns = (
            await session.scalars(
                select(CashflowTransaction).where(CashflowTransaction.source_id == cheque.id)
            )
        ).all()
        for txn in txns:
            txn.quality_status = "manual_override"
            txn.article_id = supplier.id
        await session.commit()

        await adjust_paid_invoice(
            session, cheque, lines=_corrected_lines(supplier, saucer, lid, upkeep)
        )

        assert await _txn_by_article(session, cheque.id) == {"Оплата поставщикам": Decimal("475.00")}
        assert await dds_articles_mismatch(session, cheque) is True


async def test_forced_reproject_overrides_manual_markup(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Явная кнопка «Перепровести ДДС по позициям» перебивает и ручную разметку — это уже
    осознанное действие человека; статус проводки возвращается в ``final``."""
    async with async_session_factory() as session:
        cheque, supplier, upkeep, saucer, lid = await _cheque_475(session)
        txns = (
            await session.scalars(
                select(CashflowTransaction).where(CashflowTransaction.source_id == cheque.id)
            )
        ).all()
        for txn in txns:
            txn.quality_status = "manual_override"
            txn.article_id = supplier.id
        await session.commit()
        await adjust_paid_invoice(
            session, cheque, lines=_corrected_lines(supplier, saucer, lid, upkeep)
        )

        report = await reproject_invoice_dds(session, cheque, force=True)
        await session.commit()

        assert report.skipped_manual == 0
        assert await _txn_by_article(session, cheque.id) == {
            "Оплата поставщикам": Decimal("468.00"),
            "Содержание торговых точек": Decimal("7.00"),
        }
        statuses = {
            t.quality_status
            for t in (
                await session.scalars(
                    select(CashflowTransaction).where(CashflowTransaction.source_id == cheque.id)
                )
            ).all()
        }
        assert statuses == {"final"}


async def test_overpayment_stays_on_supplier_article(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Правка уменьшила сумму: излишек ушёл в дебиторку, а в ДДС он обязан остаться на «Оплата
    поставщикам» — аванс поставщику это та же статья, размазывать его по расходным нельзя."""
    async with async_session_factory() as session:
        cheque, supplier, upkeep, saucer, lid = await _cheque_475(session)

        # Итог правки — 275 ₽ (200 товар + 75 расход); оплачено по-прежнему 475.
        await adjust_paid_invoice(
            session,
            cheque,
            lines=[
                LineInput(
                    name="Соусник бутылка 0.097",
                    quantity=Decimal("25"),
                    price=Decimal("8.00"),
                    sum=Decimal("200.00"),
                    iiko_product_id=saucer.id,
                    dds_article_id=supplier.id,
                ),
                LineInput(
                    name="пакет",
                    quantity=Decimal("1"),
                    price=Decimal("75.00"),
                    sum=Decimal("75.00"),
                    dds_article_id=upkeep.id,
                ),
            ],
        )

        by_article = await _txn_by_article(session, cheque.id)
        assert sum(by_article.values()) == Decimal("475.00")  # деньги не двигались
        # 200 товар + 200 переплаты (475 − 275) на «Оплата поставщикам», 75 — расход.
        assert by_article == {
            "Оплата поставщикам": Decimal("400.00"),
            "Содержание торговых точек": Decimal("75.00"),
        }


async def test_cheque_store_line_without_article_gets_supplier_article(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Складская строка чека без статьи получает «Оплата поставщикам».

    ``create_cheque`` требует статью у каждой позиции, а правка — нет: строка, перенесённая из
    блока расходов в складской, уезжала с ``dds_article_id=NULL`` (Ч-55). Товарность от этого не
    менялась, зато разнос по статьям рассыпался, а ``_pending_article_sums`` такие строки
    молча пропускает.
    """
    async with async_session_factory() as session:
        cheque, supplier, _upkeep, saucer, _lid = await _cheque_475(session)

        await adjust_paid_invoice(
            session,
            cheque,
            lines=[
                LineInput(
                    name="Мешок для мусора 120л",
                    quantity=Decimal("1"),
                    price=Decimal("475.00"),
                    sum=Decimal("475.00"),
                    iiko_product_id=saucer.id,
                    dds_article_id=None,  # фронт присылает null, если складских строк не было
                )
            ],
        )

        from app.models import InvoiceLineItem

        lines = (
            await session.scalars(
                select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == cheque.id)
            )
        ).all()
        assert [line.dds_article_id for line in lines] == [supplier.id]
        assert await _txn_by_article(session, cheque.id) == {"Оплата поставщикам": Decimal("475.00")}

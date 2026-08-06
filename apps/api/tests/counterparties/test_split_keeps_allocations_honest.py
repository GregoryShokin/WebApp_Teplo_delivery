"""Разбиение проводки не должно рождать расход из воздуха.

ДВЕ ДЫРЫ, НАЙДЕННЫЕ 06.08.2026, обе тихие — сходятся и баланс кошелька, и сумма долей.

1. ОПЛАЧЕННЫЕ ДОКУМЕНТЫ ПЕРЕЖИВАЮТ РАЗБИЕНИЕ, А ДЕНЬГИ ПОД НИМИ — НЕТ. Первая доля мутирует
   исходную строку и наследует её аллокации целиком, остальные рождаются чистыми.
   ``apply_cashflow_split`` снимает встречную ногу перевода, выплаты сотрудникам и привязки
   ОС, но ``InvoicePaymentAllocation`` не трогает. Платёж 80 455 ₽, разнесённый на
   72 455 + 8 000, оставлял накладную закрытой на все 80 455, а высвободившиеся 8 000 второй
   раз уходили в расход кассой.

2. ДОЛЯ ВЫХОДИТ ИЗ-ПОД ЧУЖОГО СЛОЯ. Принадлежность зарплате, депозиту, авансу ключуется
   МЕХАНИЗМОМ (``pnl_cash_origin``), а доля рождается с ``manual_split``, которого в
   справочнике нет никогда. Аванс сотруднику 10 000 ₽, разложенный на 6 000 + 4 000, дал бы
   4 000 ₽ расходом ОПиУ поверх зарплатного начисления.

На данных стенда ни одна дыра ещё не сработала (проводок ``manual_split`` нет вовсе) —
тесты закрывают их превентивно.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    CashflowTransaction,
    DdsArticle,
    InvoicePaymentAllocation,
    PnlArticleRule,
    PnlCashOrigin,
)
from app.services.banking.cashflow_classify import CashflowSplitLine, apply_cashflow_split
from app.services.pnl.sources import cashflow as cash_source
from app.services.pnl.types import Verdict
from cp_helpers import make_counterparty, make_invoice, make_wallet

JULY = (date(2026, 7, 1), date(2026, 7, 31))


async def _article(session: AsyncSession, *, code: str, line_code: str) -> DdsArticle:
    article = DdsArticle(
        code=code,
        name=f"Статья {code}",
        movement_type="outflow",
        activity_type="operating",
    )
    session.add(article)
    await session.flush()
    session.add(
        PnlArticleRule(
            article_id=article.id,
            line_code=line_code,
            in_pnl=True,
            owner_stream="cash",
            sign=1,
            applies_to="both",
            is_active=True,
        )
    )
    await session.flush()
    return article


def _manual_payment(wallet_id, article_id, amount: str, **extra) -> CashflowTransaction:
    return CashflowTransaction(
        wallet_id=wallet_id,
        direction="out",
        amount=Decimal(amount),
        operation_date=date(2026, 7, 8),
        article_id=article_id,
        source_kind=extra.pop("source_kind", "manual_bank_to_safe"),
        payment_purpose="Оплата",
        quality_status="manual_override",
        **extra,
    )


@pytest.mark.asyncio
async def test_split_below_allocated_amount_is_rejected(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Первая доля меньше уже оплаченного документами — отказ, а не тихое задвоение."""
    async with async_session_factory() as session:
        counterparty = await make_counterparty(session, name="Поставщик-сплит", inn="6155034001")
        article = await _article(
            session, code="test_split_alloc_guard", line_code="shop_maintenance"
        )
        wallet = await make_wallet(session, code="split-guard-w1", name="Банк")
        invoice = await make_invoice(
            session,
            counterparty_id=counterparty.id,
            amount="80455.00",
            doc_kind="closing",
            number="УПД-800",
            payment_status="paid",
            invoice_date=date(2026, 7, 8),
        )
        txn = _manual_payment(wallet.id, article.id, "80455.00", counterparty_id=counterparty.id)
        session.add(txn)
        await session.flush()
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id,
                source_kind="cash",
                cashflow_transaction_id=txn.id,
                amount=Decimal("80455.00"),
            )
        )
        await session.commit()

        with pytest.raises(ValueError, match="уже разложен по документам"):
            await apply_cashflow_split(
                session,
                txn,
                splits=[
                    CashflowSplitLine(article_id=article.id, amount=Decimal("72455.00")),
                    CashflowSplitLine(article_id=article.id, amount=Decimal("8000.00")),
                ],
                counterparty_id=counterparty.id,
            )


@pytest.mark.asyncio
async def test_split_above_allocated_amount_is_allowed(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Аллокации помещаются в первую долю — разбор законен, документ остаётся оплаченным."""
    async with async_session_factory() as session:
        counterparty = await make_counterparty(session, name="Поставщик-сплит-ок", inn="6155034002")
        article = await _article(
            session, code="test_split_alloc_ok", line_code="shop_maintenance"
        )
        wallet = await make_wallet(session, code="split-guard-w2", name="Банк")
        invoice = await make_invoice(
            session,
            counterparty_id=counterparty.id,
            amount="5000.00",
            doc_kind="closing",
            number="УПД-801",
            payment_status="paid",
            invoice_date=date(2026, 7, 8),
        )
        txn = _manual_payment(wallet.id, article.id, "10000.00", counterparty_id=counterparty.id)
        session.add(txn)
        await session.flush()
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id,
                source_kind="cash",
                cashflow_transaction_id=txn.id,
                amount=Decimal("5000.00"),
            )
        )
        await session.commit()

        created = await apply_cashflow_split(
            session,
            txn,
            splits=[
                CashflowSplitLine(article_id=article.id, amount=Decimal("6000.00")),
                CashflowSplitLine(article_id=article.id, amount=Decimal("4000.00")),
            ],
            counterparty_id=counterparty.id,
        )
        await session.commit()
        assert len(created) == 2


@pytest.mark.asyncio
async def test_split_stays_inside_its_owner_layer(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Доля аванса не выходит в расход ОПиУ: принадлежность слою берётся у родителя."""
    async with async_session_factory() as session:
        article = await _article(
            session, code="test_split_owner_layer", line_code="shop_maintenance"
        )
        wallet = await make_wallet(session, code="split-guard-w3", name="Сейф")
        # Механизм «выдача аванса» посчитан зарплатным модулем — вся его касса вне ОПиУ.
        # Запись в справочнике механизмов уже есть (сидируется миграцией) — проверяем это,
        # а не заводим свою: дубль нарушил бы уникальность пары «механизм × статья».
        assert await session.scalar(
            select(PnlCashOrigin.id).where(PnlCashOrigin.source_kind == "salary_advance")
        ), "механизм выдачи аванса обязан быть в справочнике исключений"
        txn = _manual_payment(wallet.id, article.id, "10000.00", source_kind="salary_advance")
        session.add(txn)
        await session.flush()
        parent_id = txn.id

        # Доля рождается с manual_split — своего места в справочнике механизмов у неё нет.
        child = _manual_payment(wallet.id, article.id, "4000.00", source_kind="manual_split")
        child.source_id = parent_id
        txn.amount = Decimal("6000.00")
        session.add(child)
        await session.commit()

        layer = await cash_source.build_cash_layer(session, *JULY)

        # Обе строки остаются в своём слое: расход по ним уже посчитан ведомостью.
        assert (
            layer.by_verdict[Verdict.EXCLUDED_OWNED_BY_LAYER.value] == Decimal("10000.00")
        ), "доля разбора вышла из-под зарплатного контура и легла в расход второй раз"
        assert "shop_maintenance" not in layer.buckets or layer.buckets[
            "shop_maintenance"
        ].amount == Decimal("0.00")

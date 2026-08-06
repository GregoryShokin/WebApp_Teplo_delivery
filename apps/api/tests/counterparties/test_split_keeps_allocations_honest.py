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
    SupplierExpenseAccrual,
)
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
async def test_split_of_fully_spent_payment_does_not_double_the_expense(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Платёж целиком ушёл на документ — обе доли остаются потраченными, а не расходом.

    Разбить платёж по статьям для аналитики законно, и запрещать это нельзя. Но доля-новичок
    выглядит свободными деньгами, хотя тех же денег давно нет: без наследования оплаченности
    8 000 ₽ легли бы расходом второй раз, поверх начисления по накладной.
    """
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
        session.add(
            SupplierExpenseAccrual(
                counterparty_id=counterparty.id,
                invoice_id=invoice.id,
                article_id=article.id,
                amount=Decimal("80455.00"),
                status="recognized",
                service_period_start=date(2026, 7, 1),
                service_period_end=date(2026, 7, 31),
                recognition_month=date(2026, 7, 1),
            )
        )
        parent = _manual_payment(
            wallet.id, article.id, "80455.00", counterparty_id=counterparty.id
        )
        session.add(parent)
        await session.flush()
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id,
                source_kind="cash",
                cashflow_transaction_id=parent.id,
                amount=Decimal("80455.00"),
            )
        )
        await session.flush()

        # Разбор: первая доля мутирует родителя и его аллокации сохраняет, вторая — чистая.
        child = _manual_payment(
            wallet.id,
            article.id,
            "8000.00",
            counterparty_id=counterparty.id,
            source_kind="manual_split",
        )
        child.source_id = parent.id
        parent.amount = Decimal("72455.00")
        session.add(child)
        await session.commit()

        layer = await cash_source.build_cash_layer(session, *JULY)

        assert layer.by_verdict[Verdict.EXCLUDED_ACCRUAL_SETTLEMENT.value] == Decimal(
            "80455.00"
        ), "доля разбора легла расходом поверх уже признанного документа"
        assert layer.buckets.get("shop_maintenance", cash_source.CashBucket()).amount == Decimal(
            "0.00"
        )


@pytest.mark.asyncio
async def test_partially_spent_payment_keeps_its_free_part(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Аллокаций меньше суммы платежа — свободная часть остаётся расходом, а не пропадает."""
    async with async_session_factory() as session:
        counterparty = await make_counterparty(
            session, name="Поставщик-частично", inn="6155034002"
        )
        article = await _article(session, code="test_split_alloc_ok", line_code="shop_maintenance")
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
        # Начисление обязательно: «оплаченность» считается по документам, которые НЕСУТ
        # расход. Без него родитель и сам не был бы settled, и тест проверял бы не то.
        session.add(
            SupplierExpenseAccrual(
                counterparty_id=counterparty.id,
                invoice_id=invoice.id,
                article_id=article.id,
                amount=Decimal("5000.00"),
                status="recognized",
                service_period_start=date(2026, 7, 1),
                service_period_end=date(2026, 7, 31),
                recognition_month=date(2026, 7, 1),
            )
        )
        parent = _manual_payment(wallet.id, article.id, "6000.00")
        session.add(parent)
        await session.flush()
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id,
                source_kind="cash",
                cashflow_transaction_id=parent.id,
                amount=Decimal("5000.00"),
            )
        )
        child = _manual_payment(wallet.id, article.id, "4000.00", source_kind="manual_split")
        child.source_id = parent.id
        session.add(child)
        await session.commit()

        layer = await cash_source.build_cash_layer(session, *JULY)

        # Родитель покрыт не полностью → доля наследования не получает и остаётся расходом.
        assert layer.buckets["shop_maintenance"].amount == Decimal("4000.00")


@pytest.mark.asyncio
async def test_coverage_is_measured_against_the_original_payment(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Покрытие меряется от ИСХОДНОЙ суммы платежа, а не от усохшего остатка родителя.

    Первая доля мутирует родителя, и его сумма падает. Сравнение с ней объявляло потраченным
    почти любой разобранный платёж: покрытый документами на 50 000 из 80 455 и разнесённый на
    10 000 + 70 455 отдавал бы «оплачено» все 70 455 при реально свободных 30 455.
    """
    async with async_session_factory() as session:
        counterparty = await make_counterparty(session, name="Частичное-покрытие", inn="6155034003")
        article = await _article(
            session, code="test_split_partial_cover", line_code="shop_maintenance"
        )
        wallet = await make_wallet(session, code="split-guard-w4", name="Банк")
        invoice = await make_invoice(
            session,
            counterparty_id=counterparty.id,
            amount="50000.00",
            doc_kind="closing",
            number="УПД-802",
            payment_status="paid",
            invoice_date=date(2026, 7, 8),
        )
        session.add(
            SupplierExpenseAccrual(
                counterparty_id=counterparty.id,
                invoice_id=invoice.id,
                article_id=article.id,
                amount=Decimal("50000.00"),
                status="recognized",
                service_period_start=date(2026, 7, 1),
                service_period_end=date(2026, 7, 31),
                recognition_month=date(2026, 7, 1),
            )
        )
        parent = _manual_payment(wallet.id, article.id, "80455.00")
        session.add(parent)
        await session.flush()
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id,
                source_kind="cash",
                cashflow_transaction_id=parent.id,
                amount=Decimal("50000.00"),
            )
        )
        # Разбор 10 000 + 70 455: родитель усыхает до 10 000, доля рождается на 70 455.
        child = _manual_payment(wallet.id, article.id, "70455.00", source_kind="manual_split")
        child.source_id = parent.id
        parent.amount = Decimal("10000.00")
        session.add(child)
        await session.commit()

        layer = await cash_source.build_cash_layer(session, *JULY)

        # Покрыто 50 000 из 80 455 — свободные 30 455 обязаны остаться расходом.
        # Наследования нет: доля 70 455 идёт в строку целиком, родитель 10 000 закрыт
        # документом. Итог строки — 70 455, а не ноль.
        assert layer.buckets["shop_maintenance"].amount == Decimal("70455.00")


@pytest.mark.asyncio
async def test_documents_without_accrual_do_not_count_as_coverage(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Оплата складской накладной расхода ОПиУ не несёт и покрытием не считается.

    Товар приходит фудкостом из iiko, поэтому такая аллокация в отчёте ничего не закрывает.
    На данных прода 93 привязки из 95 — именно такие: засчитывая их, доля теряла расход молча.
    """
    async with async_session_factory() as session:
        counterparty = await make_counterparty(session, name="Склад-накладная", inn="6155034004")
        article = await _article(
            session, code="test_split_no_accrual", line_code="shop_maintenance"
        )
        wallet = await make_wallet(session, code="split-guard-w5", name="Банк")
        # Накладная БЕЗ начисления — её расход придёт фудкостом, а не через ДЗ/КЗ.
        invoice = await make_invoice(
            session,
            counterparty_id=counterparty.id,
            amount="10000.00",
            doc_kind="closing",
            number="НАКЛ-900",
            payment_status="paid",
            operational_scope="warehouse",
            invoice_date=date(2026, 7, 8),
        )
        parent = _manual_payment(wallet.id, article.id, "10000.00")
        session.add(parent)
        await session.flush()
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id,
                source_kind="cash",
                cashflow_transaction_id=parent.id,
                amount=Decimal("10000.00"),
            )
        )
        child = _manual_payment(wallet.id, article.id, "4000.00", source_kind="manual_split")
        child.source_id = parent.id
        parent.amount = Decimal("6000.00")
        session.add(child)
        await session.commit()

        layer = await cash_source.build_cash_layer(session, *JULY)

        # Ни родитель, ни доля не считаются оплаченными «под документ»: начисления нет вовсе.
        assert layer.buckets["shop_maintenance"].amount == Decimal("10000.00")


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

"""Правило 1 канона не зависит от КАНАЛА денег: наличная выплата = банковское списание.

Кейс владельца 27.07.2026: две выплаты аренды по 50 000 ₽ из Сейфа были размечены на
арендодателей, у которых на 31.07 уже лежали отложенные (pending) УПД. Дебиторка не возникла —
правило 1 висело на условии ``source_kind == 'bank_operation'`` и наличный контур мимо него
проходил. В свою дату УПД активировался бы фантомной кредиторкой на уже уплаченные деньги.

Здесь закрыты обе двери ручного разбора (PATCH-разметка и мультисплит), обратный ход
(снятие контрагента, исключение проводки) и граница — самооплатный кассовый чек.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import Decimal

from cp_helpers import make_counterparty, make_invoice, make_wallet
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    CashflowTransaction,
    DdsArticle,
    SupplierInvoice,
    SupplierPrepayment,
)
from app.services.supplier_prepayments import activate_due_closing_invoices

HEADERS = {"X-User-Role": "finance_manager"}


def _run(coro):
    return asyncio.run(coro)


async def _article(session: AsyncSession, *, code: str, name: str) -> DdsArticle:
    article = DdsArticle(code=code, name=name, movement_type="outflow", activity_type="operating")
    session.add(article)
    await session.flush()
    return article


async def _cash_txn(
    session: AsyncSession,
    *,
    wallet_id: uuid.UUID,
    amount: str,
    source_kind: str = "template_import",
    counterparty_id: uuid.UUID | None = None,
    article_id: uuid.UUID | None = None,
    operation_date: date = date(2026, 7, 1),
) -> CashflowTransaction:
    """Наличная проводка Сейфа — та самая форма, что пришла заливкой шаблона."""
    txn = CashflowTransaction(
        wallet_id=wallet_id,
        direction="out",
        amount=Decimal(amount),
        operation_date=operation_date,
        counterparty_id=counterparty_id,
        article_id=article_id,
        source_kind=source_kind,
        payment_purpose="Выплата из Сейфа",
        quality_status="auto",
    )
    session.add(txn)
    await session.flush()
    return txn


async def _receivable(session: AsyncSession, counterparty_id: uuid.UUID) -> Decimal:
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


# --- Кейс владельца: аренда наличными вперёд, УПД приходит 31-го ------------------------------


def test_cash_rent_payment_creates_receivable_settled_by_future_upd(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Полный цикл: платёж 01.07 → ДЗ 50 000 → активация УПД 31.07 → 0/0, без фантомной КЗ."""

    async def seed() -> dict[str, str]:
        async with async_session_factory() as session:
            landlord = await make_counterparty(session, name="Арендодатель-нал", inn="6155020101")
            wallet = await make_wallet(session, name="Сейф-тест", wallet_type="cash_safe")
            article = await _article(session, code="cash_rent_1", name="Аренда торговых точек")
            # Отложенный УПД: дата 31.07 ещё не наступила — в кредиторку он не входит.
            invoice = await make_invoice(
                session,
                counterparty_id=landlord.id,
                amount="50000.00",
                invoice_date=date(2026, 7, 31),
                operational_scope="finance",
                activation_status="pending",
            )
            txn = await _cash_txn(session, wallet_id=wallet.id, amount="50000.00")
            await session.commit()
            return {
                "cp": str(landlord.id),
                "txn": str(txn.id),
                "article": str(article.id),
                "invoice": str(invoice.id),
            }

    ids = _run(seed())

    # Владелец размечает наличную проводку на арендодателя.
    r = client.patch(
        f"/api/v1/dds/transactions/{ids['txn']}",
        json={"article_id": ids["article"], "counterparty_id": ids["cp"]},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text

    async def after_marking() -> Decimal:
        async with async_session_factory() as session:
            return await _receivable(session, uuid.UUID(ids["cp"]))

    assert _run(after_marking()) == Decimal("50000.00")

    # Наступило 31.07: джоба активирует УПД, он гасит дебиторку — долга не возникает.
    async def activate() -> tuple[str, Decimal]:
        async with async_session_factory() as session:
            await activate_due_closing_invoices(session, as_of=date(2026, 7, 31))
            invoice = await session.get(SupplierInvoice, uuid.UUID(ids["invoice"]))
            return invoice.payment_status, await _receivable(session, uuid.UUID(ids["cp"]))

    payment_status, receivable = _run(activate())
    assert payment_status == "paid"
    assert receivable == Decimal("0.00")


def test_cash_payment_settles_open_kz_before_creating_receivable(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """FIFO работает и для наличных: открытая КЗ гасится, дебиторкой становится излишек."""

    async def seed() -> dict[str, str]:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Услуги-нал", inn="6155020102")
            wallet = await make_wallet(session, name="Сейф-КЗ", wallet_type="cash_safe")
            article = await _article(session, code="cash_rule1_kz", name="Услуги наличными")
            invoice = await make_invoice(
                session,
                counterparty_id=cp.id,
                amount="30000.00",
                invoice_date=date(2026, 6, 30),
                operational_scope="finance",
            )
            txn = await _cash_txn(session, wallet_id=wallet.id, amount="50000.00")
            await session.commit()
            return {
                "cp": str(cp.id),
                "txn": str(txn.id),
                "article": str(article.id),
                "invoice": str(invoice.id),
            }

    ids = _run(seed())
    r = client.patch(
        f"/api/v1/dds/transactions/{ids['txn']}",
        json={"article_id": ids["article"], "counterparty_id": ids["cp"]},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text

    async def check() -> tuple[str, Decimal]:
        async with async_session_factory() as session:
            invoice = await session.get(SupplierInvoice, uuid.UUID(ids["invoice"]))
            return invoice.payment_status, await _receivable(session, uuid.UUID(ids["cp"]))

    payment_status, receivable = _run(check())
    assert payment_status == "paid"
    assert receivable == Decimal("20000.00")


# --- Обратный ход: ДЗ следует за деньгами -----------------------------------------------------


def test_removing_counterparty_drops_receivable(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Сняли контрагента с проводки — дебиторка не должна остаться висеть на нём."""

    async def seed() -> dict[str, str]:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Ошибочный-нал", inn="6155020103")
            wallet = await make_wallet(session, name="Сейф-откат", wallet_type="cash_safe")
            article = await _article(session, code="cash_rollback", name="Прочее наличными")
            txn = await _cash_txn(session, wallet_id=wallet.id, amount="7000.00")
            await session.commit()
            return {"cp": str(cp.id), "txn": str(txn.id), "article": str(article.id)}

    ids = _run(seed())
    client.patch(
        f"/api/v1/dds/transactions/{ids['txn']}",
        json={"article_id": ids["article"], "counterparty_id": ids["cp"]},
        headers=HEADERS,
    )

    async def receivable() -> Decimal:
        async with async_session_factory() as session:
            return await _receivable(session, uuid.UUID(ids["cp"]))

    assert _run(receivable()) == Decimal("7000.00")

    r = client.patch(
        f"/api/v1/dds/transactions/{ids['txn']}",
        json={"article_id": ids["article"], "counterparty_id": None},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    assert _run(receivable()) == Decimal("0.00")


def test_excluding_transaction_drops_receivable(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Исключённая из ДДС проводка забирает свою дебиторку — денег за ней уже нет."""

    async def seed() -> dict[str, str]:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Исключаемый-нал", inn="6155020104")
            wallet = await make_wallet(session, name="Сейф-исключение", wallet_type="cash_safe")
            article = await _article(session, code="cash_excluded", name="Наличные к исключению")
            txn = await _cash_txn(session, wallet_id=wallet.id, amount="4000.00")
            await session.commit()
            return {"cp": str(cp.id), "txn": str(txn.id), "article": str(article.id)}

    ids = _run(seed())
    client.patch(
        f"/api/v1/dds/transactions/{ids['txn']}",
        json={"article_id": ids["article"], "counterparty_id": ids["cp"]},
        headers=HEADERS,
    )

    async def receivable() -> Decimal:
        async with async_session_factory() as session:
            return await _receivable(session, uuid.UUID(ids["cp"]))

    assert _run(receivable()) == Decimal("4000.00")

    r = client.post(
        f"/api/v1/dds/transactions/{ids['txn']}/classify",
        json={"action": "exclude", "splits": []},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    assert _run(receivable()) == Decimal("0.00")


# --- Вторая дверь: мультисплит ----------------------------------------------------------------


def test_split_gives_each_share_its_own_receivable(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Ровно кейс владельца: одна выплата разносится на двух арендодателей — ДЗ у обоих."""

    async def seed() -> dict[str, str]:
        async with async_session_factory() as session:
            first = await make_counterparty(session, name="Арендодатель-1", inn="6155020105")
            second = await make_counterparty(session, name="Арендодатель-2", inn="6155020106")
            wallet = await make_wallet(session, name="Сейф-сплит", wallet_type="cash_safe")
            article = await _article(session, code="cash_split_rent", name="Аренда (сплит)")
            txn = await _cash_txn(session, wallet_id=wallet.id, amount="101000.00")
            await session.commit()
            return {
                "first": str(first.id),
                "second": str(second.id),
                "txn": str(txn.id),
                "article": str(article.id),
            }

    ids = _run(seed())
    r = client.post(
        f"/api/v1/dds/transactions/{ids['txn']}/classify",
        json={
            "action": "split",
            "splits": [
                {
                    "article_id": ids["article"],
                    "amount": "50000.00",
                    "counterparty_id": ids["first"],
                },
                {
                    "article_id": ids["article"],
                    "amount": "50000.00",
                    "counterparty_id": ids["second"],
                },
                {"article_id": ids["article"], "amount": "1000.00"},
            ],
        },
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text

    async def check() -> tuple[Decimal, Decimal]:
        async with async_session_factory() as session:
            return (
                await _receivable(session, uuid.UUID(ids["first"])),
                await _receivable(session, uuid.UUID(ids["second"])),
            )

    first_receivable, second_receivable = _run(check())
    assert first_receivable == Decimal("50000.00")
    assert second_receivable == Decimal("50000.00")


# --- Граница: самооплатный контур в правило 1 не входит ---------------------------------------


def test_kassa_cheque_never_creates_receivable(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Чек кассы закрывает свою накладную сам — дебиторки на «Местный закуп» быть не должно."""

    async def seed() -> dict[str, str]:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Местный закуп-тест", inn="6155020107")
            wallet = await make_wallet(session, name="Касса-чек", wallet_type="cash")
            article = await _article(session, code="cash_cheque_art", name="Закуп по чеку")
            txn = await _cash_txn(
                session,
                wallet_id=wallet.id,
                amount="6782.03",
                source_kind="kassa_cheque",
            )
            await session.commit()
            return {"cp": str(cp.id), "txn": str(txn.id), "article": str(article.id)}

    ids = _run(seed())
    r = client.patch(
        f"/api/v1/dds/transactions/{ids['txn']}",
        json={"article_id": ids["article"], "counterparty_id": ids["cp"]},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text

    async def receivable() -> Decimal:
        async with async_session_factory() as session:
            return await _receivable(session, uuid.UUID(ids["cp"]))

    assert _run(receivable()) == Decimal("0.00")

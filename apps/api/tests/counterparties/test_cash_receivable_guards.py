"""Границы правила 1 в наличном контуре: чужими деньгами оно не распоряжается.

Предделойный аудит фикса «наличный платёж → дебиторка» (27.07.2026) нашёл шесть мест, где
расширенное правило 1 залезало в чужой огород. Каждый тест здесь — репро одного из них;
до фикса границ они падают.

Общий корень: банковский разбор зовёт правило 1 только для СВОБОДНЫХ строк
(``classifier.py``: не аванс, без явной привязки к накладной, не выплата), а наличный цикл
звал его для всего подряд — включая деньги, которые уже распределил другой контур.
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
    InvoicePaymentAllocation,
    SupplierInvoice,
    SupplierPrepayment,
)

HEADERS = {"X-User-Role": "finance_manager"}


def _run(coro):
    return asyncio.run(coro)


async def _article(session: AsyncSession, *, code: str, name: str) -> DdsArticle:
    article = DdsArticle(code=code, name=name, movement_type="outflow", activity_type="operating")
    session.add(article)
    await session.flush()
    return article


async def _txn(
    session: AsyncSession,
    *,
    wallet_id: uuid.UUID,
    amount: str,
    source_kind: str,
    counterparty_id: uuid.UUID | None = None,
    article_id: uuid.UUID | None = None,
) -> CashflowTransaction:
    txn = CashflowTransaction(
        wallet_id=wallet_id,
        direction="out",
        amount=Decimal(amount),
        operation_date=date(2026, 7, 1),
        counterparty_id=counterparty_id,
        article_id=article_id,
        source_kind=source_kind,
        payment_purpose="Наличная выплата",
        quality_status="auto",
    )
    session.add(txn)
    await session.flush()
    return txn


async def _open_receivable(session: AsyncSession, counterparty_id: uuid.UUID) -> Decimal:
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


async def _allocated(session: AsyncSession, invoice_id: uuid.UUID) -> Decimal:
    rows = (
        await session.scalars(
            select(InvoicePaymentAllocation).where(
                InvoicePaymentAllocation.invoice_id == invoice_id
            )
        )
    ).all()
    return sum((Decimal(str(r.amount)) for r in rows), Decimal("0.00"))


# --- Д1: целевой аванс под поставку — не добыча правила 1 -------------------------------------


def test_split_does_not_consume_earmarked_goods_prepayment(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Аванс kind='goods' ждёт СВОЮ поставку; разбор проводки не вправе скормить его чужому УПД."""

    async def seed() -> dict[str, str]:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Целевой аванс", inn="6155030101")
            wallet = await make_wallet(session, name="Сейф-аванс", wallet_type="cash_safe")
            article = await _article(session, code="guard_advance", name="Авансы поставщикам")
            # Посторонний финансовый УПД того же поставщика — приманка для FIFO.
            other = await make_invoice(
                session,
                counterparty_id=cp.id,
                amount="20000.00",
                invoice_date=date(2026, 6, 15),
                operational_scope="finance",
            )
            # source_kind намеренно нейтральный: проверяем защиту ЯДРА правила 1 (чужой вид
            # предоплаты), а не чёрный список наличных контуров.
            txn = await _txn(
                session,
                wallet_id=wallet.id,
                amount="20000.00",
                source_kind="template_import",
                counterparty_id=cp.id,
                article_id=article.id,
            )
            # Целевой аванс завёл кассовый контур — он гасится только своей поставкой.
            session.add(
                SupplierPrepayment(
                    counterparty_id=cp.id,
                    kind="goods",
                    wallet_id=wallet.id,
                    amount=Decimal("20000.00"),
                    amount_settled=Decimal("0.00"),
                    status="open",
                    cashflow_transaction_id=txn.id,
                    article_id=article.id,
                    note="Аванс под поставку",
                )
            )
            await session.commit()
            return {
                "cp": str(cp.id),
                "txn": str(txn.id),
                "article": str(article.id),
                "other": str(other.id),
            }

    ids = _run(seed())

    r = client.post(
        f"/api/v1/dds/transactions/{ids['txn']}/classify",
        json={
            "action": "split",
            "splits": [
                {
                    "article_id": ids["article"],
                    "amount": "20000.00",
                    "counterparty_id": ids["cp"],
                }
            ],
        },
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text

    async def check() -> tuple[Decimal, str, str | None]:
        async with async_session_factory() as session:
            invoice = await session.get(SupplierInvoice, uuid.UUID(ids["other"]))
            prepayment = await session.scalar(
                select(SupplierPrepayment).where(
                    SupplierPrepayment.cashflow_transaction_id == uuid.UUID(ids["txn"])
                )
            )
            return (
                await _open_receivable(session, uuid.UUID(ids["cp"])),
                invoice.payment_status,
                prepayment.kind if prepayment else None,
            )

    receivable, other_status, kind = _run(check())
    assert kind == "goods", "целевой аванс подменён записью правила 1"
    assert other_status == "unpaid", "посторонний УПД погашен целевым авансом"
    assert receivable == Decimal("20000.00"), "дебиторка целевого аванса испарилась"


# --- Д2: сплит проводки, уже оплатившей накладную --------------------------------------------


def test_split_of_allocated_payment_does_not_overspend(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """1000 ₽ не могут закрыть документов на 1400 ₽: доля-новичок не «свободные деньги»."""

    async def seed() -> dict[str, str]:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Оплата-сплит", inn="6155030102")
            wallet = await make_wallet(session, name="Сейф-сплит-гард", wallet_type="cash_safe")
            article = await _article(session, code="guard_split_pay", name="Оплата поставщику")
            paid = await make_invoice(
                session,
                counterparty_id=cp.id,
                amount="1000.00",
                invoice_date=date(2026, 6, 10),
                operational_scope="warehouse",
                payment_status="paid",
            )
            other = await make_invoice(
                session,
                counterparty_id=cp.id,
                amount="400.00",
                invoice_date=date(2026, 6, 20),
                operational_scope="finance",
            )
            txn = await _txn(
                session,
                wallet_id=wallet.id,
                amount="1000.00",
                source_kind="counterparty_payment",
                counterparty_id=cp.id,
                article_id=article.id,
            )
            # Деньги уже израсходованы: этой проводкой оплачена складская накладная.
            session.add(
                InvoicePaymentAllocation(
                    invoice_id=paid.id,
                    source_kind="cash",
                    cashflow_transaction_id=txn.id,
                    amount=Decimal("1000.00"),
                )
            )
            await session.commit()
            return {
                "cp": str(cp.id),
                "txn": str(txn.id),
                "article": str(article.id),
                "paid": str(paid.id),
                "other": str(other.id),
            }

    ids = _run(seed())

    r = client.post(
        f"/api/v1/dds/transactions/{ids['txn']}/classify",
        json={
            "action": "split",
            "splits": [
                {"article_id": ids["article"], "amount": "600.00", "counterparty_id": ids["cp"]},
                {"article_id": ids["article"], "amount": "400.00", "counterparty_id": ids["cp"]},
            ],
        },
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text

    async def check() -> tuple[Decimal, Decimal, Decimal]:
        async with async_session_factory() as session:
            return (
                await _allocated(session, uuid.UUID(ids["paid"])),
                await _allocated(session, uuid.UUID(ids["other"])),
                await _open_receivable(session, uuid.UUID(ids["cp"])),
            )

    paid_alloc, other_alloc, receivable = _run(check())
    # Всё, что могут профинансировать 1000 ₽, — уже профинансировано складской накладной.
    assert paid_alloc == Decimal("1000.00"), "прежняя оплата снята разбором"
    assert other_alloc == Decimal("0.00"), "тот же рубль погасил второй документ"
    assert receivable == Decimal("0.00"), "на израсходованные деньги заведена дебиторка"


# --- Д3: «Исключить» не должно снимать чужие оплаты -------------------------------------------


def test_exclude_keeps_foreign_allocations(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Исключение проводки возвращало складскую накладную в КЗ — и вернуть её обратно нечем."""

    async def seed() -> dict[str, str]:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Исключение-склад", inn="6155030103")
            wallet = await make_wallet(session, name="Сейф-исключ-гард", wallet_type="cash_safe")
            article = await _article(session, code="guard_exclude", name="Оплата склада")
            invoice = await make_invoice(
                session,
                counterparty_id=cp.id,
                amount="900.00",
                invoice_date=date(2026, 6, 5),
                operational_scope="warehouse",
                payment_status="paid",
            )
            txn = await _txn(
                session,
                wallet_id=wallet.id,
                amount="900.00",
                source_kind="counterparty_payment",
                counterparty_id=cp.id,
                article_id=article.id,
            )
            session.add(
                InvoicePaymentAllocation(
                    invoice_id=invoice.id,
                    source_kind="cash",
                    cashflow_transaction_id=txn.id,
                    amount=Decimal("900.00"),
                )
            )
            await session.commit()
            return {"cp": str(cp.id), "txn": str(txn.id), "invoice": str(invoice.id)}

    ids = _run(seed())

    r = client.post(
        f"/api/v1/dds/transactions/{ids['txn']}/classify",
        json={"action": "exclude", "splits": []},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text

    async def check() -> tuple[Decimal, str]:
        async with async_session_factory() as session:
            invoice = await session.get(SupplierInvoice, uuid.UUID(ids["invoice"]))
            return await _allocated(session, uuid.UUID(ids["invoice"])), invoice.payment_status

    allocated, status = _run(check())
    assert allocated == Decimal("900.00"), "исключение сняло оплату складской накладной"
    assert status == "paid", "накладная вернулась в кредиторку — риск повторной оплаты"


# --- Д4: адресное гашение аренды не перевешивается FIFO ---------------------------------------


def test_reclassify_keeps_addressed_lease_settlement(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Аренда гасится адресно по договору; переразметка не должна двигать деньги на чужой УПД."""

    async def seed() -> dict[str, str]:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Арендодатель-адрес", inn="6155030104")
            wallet = await make_wallet(session, name="Сейф-аренда-гард", wallet_type="cash_safe")
            article = await _article(session, code="guard_lease", name="Аренда адресная")
            # Более старый открытый УПД — именно на него FIFO перевесил бы деньги.
            older = await make_invoice(
                session,
                counterparty_id=cp.id,
                amount="50000.00",
                invoice_date=date(2026, 5, 31),
                operational_scope="finance",
            )
            addressed = await make_invoice(
                session,
                counterparty_id=cp.id,
                amount="50000.00",
                invoice_date=date(2026, 6, 30),
                operational_scope="finance",
                payment_status="paid",
            )
            txn = await _txn(
                session,
                wallet_id=wallet.id,
                amount="50000.00",
                source_kind="safe_payout",
                counterparty_id=cp.id,
                article_id=article.id,
            )
            session.add(
                InvoicePaymentAllocation(
                    invoice_id=addressed.id,
                    source_kind="cash",
                    cashflow_transaction_id=txn.id,
                    amount=Decimal("50000.00"),
                )
            )
            await session.commit()
            return {
                "cp": str(cp.id),
                "txn": str(txn.id),
                "article": str(article.id),
                "older": str(older.id),
                "addressed": str(addressed.id),
            }

    ids = _run(seed())

    r = client.patch(
        f"/api/v1/dds/transactions/{ids['txn']}",
        json={"article_id": ids["article"], "counterparty_id": ids["cp"]},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text

    async def check() -> tuple[Decimal, Decimal]:
        async with async_session_factory() as session:
            return (
                await _allocated(session, uuid.UUID(ids["addressed"])),
                await _allocated(session, uuid.UUID(ids["older"])),
            )

    addressed_alloc, older_alloc = _run(check())
    assert addressed_alloc == Decimal("50000.00"), "адресное гашение договора снято"
    assert older_alloc == Decimal("0.00"), "деньги перевешены FIFO на посторонний документ"


# --- Д5: гард кассового чека не обходится сплитом ---------------------------------------------


def test_cheque_split_inherits_self_settling_guard(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Доли чека получают source_kind='manual_split' — самооплатность обязана наследоваться."""

    async def seed() -> dict[str, str]:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Местный закуп-сплит", inn="6155030105")
            wallet = await make_wallet(session, name="Касса-чек-сплит", wallet_type="cash")
            first = await _article(session, code="guard_cheque_a", name="Продукты по чеку")
            second = await _article(session, code="guard_cheque_b", name="Хозтовары по чеку")
            txn = await _txn(
                session,
                wallet_id=wallet.id,
                amount="1000.00",
                source_kind="kassa_cheque",
                counterparty_id=cp.id,
                article_id=first.id,
            )
            await session.commit()
            return {
                "cp": str(cp.id),
                "txn": str(txn.id),
                "first": str(first.id),
                "second": str(second.id),
            }

    ids = _run(seed())

    r = client.post(
        f"/api/v1/dds/transactions/{ids['txn']}/classify",
        json={
            "action": "split",
            "splits": [
                {"article_id": ids["first"], "amount": "600.00", "counterparty_id": ids["cp"]},
                {"article_id": ids["second"], "amount": "400.00", "counterparty_id": ids["cp"]},
            ],
        },
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text

    async def receivable() -> Decimal:
        async with async_session_factory() as session:
            return await _open_receivable(session, uuid.UUID(ids["cp"]))

    assert _run(receivable()) == Decimal("0.00"), "доля чека завела фантомную дебиторку"


# --- Д6: транзитная доля не гасит кредиторку --------------------------------------------------


def test_transfer_share_does_not_settle_payable(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Перенос Сейф→Касса деньги компании не покидают: чужой УПД он оплачивать не может."""

    async def seed() -> dict[str, str]:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Транзит-контрагент", inn="6155030106")
            source = await make_wallet(session, name="Сейф-транзит", wallet_type="cash_safe")
            destination = await make_wallet(session, name="Касса-транзит", wallet_type="cash")
            # Статья обязана быть именно TRANSFER_OUT_ARTICLE_CODE — иначе сплит отвергнет
            # счёт-получатель, и транзитная доля вообще не появится. В базе она уже есть.
            out_article = await session.scalar(
                select(DdsArticle).where(DdsArticle.code == "internal_transfer")
            )
            if out_article is None:
                out_article = await _article(
                    session, code="internal_transfer", name="Перевод между счетами"
                )
            invoice = await make_invoice(
                session,
                counterparty_id=cp.id,
                amount="5000.00",
                invoice_date=date(2026, 6, 1),
                operational_scope="finance",
            )
            txn = await _txn(
                session,
                wallet_id=source.id,
                amount="5000.00",
                source_kind="template_import",
                counterparty_id=cp.id,
            )
            await session.commit()
            return {
                "cp": str(cp.id),
                "txn": str(txn.id),
                "article": str(out_article.id),
                "destination": str(destination.id),
                "invoice": str(invoice.id),
            }

    ids = _run(seed())

    r = client.post(
        f"/api/v1/dds/transactions/{ids['txn']}/classify",
        json={
            "action": "split",
            "splits": [
                {
                    "article_id": ids["article"],
                    "amount": "5000.00",
                    "counterparty_id": ids["cp"],
                    "transfer_wallet_id": ids["destination"],
                }
            ],
        },
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text

    async def check() -> tuple[str, Decimal]:
        async with async_session_factory() as session:
            invoice = await session.get(SupplierInvoice, uuid.UUID(ids["invoice"]))
            return invoice.payment_status, await _open_receivable(session, uuid.UUID(ids["cp"]))

    status, receivable = _run(check())
    assert status == "unpaid", "внутренний перевод фиктивно оплатил кредиторку"
    assert receivable == Decimal("0.00"), "внутренний перевод завёл дебиторку"

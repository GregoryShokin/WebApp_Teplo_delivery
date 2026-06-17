"""Чеки модуля «Касса»: создание (карта / сплит карта+нал / номенклатура), подбор
card-операций и анти-дубль. Прогоняется на ``teplo_test``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from cp_helpers import (
    headers_for,
    make_account,
    make_bank_operation,
    make_counterparty,
    make_expense_article,
    make_wallet,
)
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    BankOperation,
    CashflowTransaction,
    InvoiceLineItem,
    InvoicePaymentAllocation,
    Wallet,
)
from app.services.kassa.cheque import (
    ChequeBankPart,
    ChequeLineInput,
    KassaChequeError,
    create_cheque,
    list_card_transactions,
)

ISSUED = datetime(2026, 6, 17, 12, 0, tzinfo=UTC)
OP_DATE = date(2026, 6, 17)


async def _card_op(session: AsyncSession, *, amount: str, posted_at: datetime | None = ISSUED):
    """Account + card wallet hanging off it + a card-purchase bank op on that account."""
    account = await make_account(session)
    wallet = await make_wallet(
        session, name="Тинькофф карта", wallet_type="bank", account_id=account.id
    )
    op = await make_bank_operation(
        session,
        amount=amount,
        operation_date=OP_DATE,
        posted_at=posted_at,
        category="cardOperation",
        account_id=account.id,
    )
    return wallet, op


async def _allocs(session: AsyncSession, invoice_id) -> list[InvoicePaymentAllocation]:
    return list(
        (
            await session.scalars(
                select(InvoicePaymentAllocation).where(
                    InvoicePaymentAllocation.invoice_id == invoice_id
                )
            )
        ).all()
    )


async def _txns(session: AsyncSession, invoice_id) -> list[CashflowTransaction]:
    return list(
        (
            await session.scalars(
                select(CashflowTransaction).where(CashflowTransaction.source_id == invoice_id)
            )
        ).all()
    )


async def test_create_cheque_card_only_is_paid_and_booked(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        article = await make_expense_article(
            session, code="courier_payout", name="Курьерская служба -"
        )
        cp = await make_counterparty(session, name="Магазин")
        wallet, op = await _card_op(session, amount="1500.00")
        await session.commit()

        cheque = await create_cheque(
            session,
            counterparty_id=cp.id,
            article_id=article.id,
            issued_at=ISSUED,
            bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
        )

        assert cheque.source == "kassa_cheque"
        assert cheque.payment_status == "paid"
        assert cheque.amount == Decimal("1500.00")
        assert (cheque.number or "").startswith("Ч-")

        allocs = await _allocs(session, cheque.id)
        assert len(allocs) == 1
        assert allocs[0].source_kind == "bank"
        assert allocs[0].bank_operation_id == op.id

        txns = await _txns(session, cheque.id)
        assert len(txns) == 1
        assert txns[0].source_kind == "kassa_cheque"
        assert txns[0].wallet_id == wallet.id
        assert txns[0].direction == "out"
        assert txns[0].article_id == article.id

        # The card op is now linked to the DDS movement (classified — no double expense).
        op_after = await session.get(BankOperation, op.id)
        assert op_after.cashflow_transaction_id == txns[0].id


async def test_create_cheque_split_card_plus_cash(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="cleaning", name="Расходники")
        cp = await make_counterparty(session, name="Магазин")
        card_wallet, op = await _card_op(session, amount="600.00")
        # tk_chernikova засеян миграцией 0115 — сервис находит его по коду.
        cash_wallet = await session.scalar(select(Wallet).where(Wallet.code == "tk_chernikova"))
        assert cash_wallet is not None
        await session.commit()

        cheque = await create_cheque(
            session,
            counterparty_id=cp.id,
            article_id=article.id,
            issued_at=ISSUED,
            bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
            cash_amount=Decimal("400.00"),
        )

        assert cheque.payment_status == "paid"
        assert cheque.amount == Decimal("1000.00")

        allocs = await _allocs(session, cheque.id)
        assert sorted(a.source_kind for a in allocs) == ["bank", "cash"]

        txns = await _txns(session, cheque.id)
        assert len(txns) == 2
        assert {t.wallet_id for t in txns} == {card_wallet.id, cash_wallet.id}


async def test_create_cheque_with_nomenclature(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="goods", name="Сырьё")
        cp = await make_counterparty(session, name="Магазин")
        _, op = await _card_op(session, amount="500.00")
        await session.commit()

        cheque = await create_cheque(
            session,
            counterparty_id=cp.id,
            article_id=article.id,
            issued_at=ISSUED,
            bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
            track_nomenclature=True,
            lines=[ChequeLineInput(name="Лук", quantity=Decimal("5"), price=Decimal("100.00"))],
        )

        assert cheque.payment_status == "paid"
        lines = (
            await session.scalars(
                select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == cheque.id)
            )
        ).all()
        assert len(lines) == 1
        assert lines[0].name == "Лук"
        assert lines[0].sum == Decimal("500.00")


async def test_nomenclature_total_mismatch_rejected(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="goods2", name="Сырьё2")
        cp = await make_counterparty(session, name="Магазин")
        _, op = await _card_op(session, amount="500.00")
        await session.commit()

        with pytest.raises(KassaChequeError):
            await create_cheque(
                session,
                counterparty_id=cp.id,
                article_id=article.id,
                issued_at=ISSUED,
                bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
                track_nomenclature=True,
                lines=[ChequeLineInput(name="Лук", quantity=Decimal("1"), price=Decimal("100.00"))],
            )


async def test_non_card_operation_rejected(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="x", name="X")
        cp = await make_counterparty(session, name="Магазин")
        account = await make_account(session)
        await make_wallet(session, wallet_type="bank", account_id=account.id)
        # Supplier payment: has INN + account, no cardOperation → not a card purchase.
        op = await make_bank_operation(
            session,
            amount="500.00",
            operation_date=OP_DATE,
            inn="7712345678",
            account="40702810900000000001",
            account_id=account.id,
        )
        await session.commit()

        with pytest.raises(KassaChequeError):
            await create_cheque(
                session,
                counterparty_id=cp.id,
                article_id=article.id,
                issued_at=ISSUED,
                bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
            )


async def test_operation_reuse_rejected(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        article = await make_expense_article(session, code="y", name="Y")
        cp = await make_counterparty(session, name="Магазин")
        _, op = await _card_op(session, amount="700.00")
        await session.commit()

        await create_cheque(
            session,
            counterparty_id=cp.id,
            article_id=article.id,
            issued_at=ISSUED,
            bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
        )
        with pytest.raises(KassaChequeError):
            await create_cheque(
                session,
                counterparty_id=cp.id,
                article_id=article.id,
                issued_at=ISSUED,
                bank_parts=[ChequeBankPart(bank_operation_id=op.id)],
            )


async def test_list_card_transactions_excludes_non_card(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        account = await make_account(session)
        await make_wallet(session, wallet_type="bank", account_id=account.id)
        card = await make_bank_operation(
            session,
            amount="300.00",
            operation_date=OP_DATE,
            posted_at=ISSUED,
            category="cardOperation",
            account_id=account.id,
        )
        # Supplier payment (has requisites, not a card op) — must be excluded.
        await make_bank_operation(
            session,
            amount="900.00",
            operation_date=OP_DATE,
            posted_at=ISSUED,
            inn="7712345678",
            account="40702810900000000002",
            account_id=account.id,
        )
        await session.commit()

        candidates = await list_card_transactions(session, issued_at=ISSUED)
        assert [c.bank_operation_id for c in candidates] == [card.id]
        assert candidates[0].tier == 1


# --- HTTP-слой (FastAPI): право, маппинг тела, сериализация ChequeRead, 409 --------

KASSA_BASE = "/api/v1/kassa"


def _run(coro):
    return asyncio.run(coro)


async def _seed_route(factory: async_sessionmaker[AsyncSession]):
    async with factory() as session:
        article = await make_expense_article(
            session, code="courier_payout", name="Курьерская служба -"
        )
        cp = await make_counterparty(session, name="Магазин")
        account = await make_account(session)
        await make_wallet(session, wallet_type="bank", account_id=account.id)
        op = await make_bank_operation(
            session,
            amount="1500.00",
            operation_date=OP_DATE,
            posted_at=ISSUED,
            category="cardOperation",
            account_id=account.id,
        )
        await session.commit()
        return cp.id, article.id, op.id


def _cheque_body(cp_id, article_id, op_id) -> dict:
    return {
        "counterparty_id": str(cp_id),
        "article_id": str(article_id),
        "issued_at": ISSUED.isoformat(),
        "bank_parts": [{"bank_operation_id": str(op_id)}],
    }


def test_post_cheque_creates_paid_and_serializes(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    cp_id, article_id, op_id = _run(_seed_route(async_session_factory))
    headers = _run(headers_for(async_session_factory, "kassa-cashier@test.local", ["cashier"]))
    body = _cheque_body(cp_id, article_id, op_id)

    resp = client.post(f"{KASSA_BASE}/cheques", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["payment_status"] == "paid"
    assert data["amount"] == 1500.0
    assert data["number"].startswith("Ч-")
    assert data["article_name"] == "Курьерская служба -"
    assert len(data["allocations"]) == 1

    got = client.get(f"{KASSA_BASE}/cheques/{data['id']}", headers=headers)
    assert got.status_code == 200
    assert got.json()["id"] == data["id"]

    # Та же операция повторно → 409 (анти-дубль на HTTP-слое).
    repeat = client.post(f"{KASSA_BASE}/cheques", json=body, headers=headers)
    assert repeat.status_code == 409


def test_post_cheque_forbidden_without_permission(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    cp_id, article_id, op_id = _run(_seed_route(async_session_factory))
    headers = _run(
        headers_for(async_session_factory, "kassa-courier@test.local", ["senior_courier"])
    )
    resp = client.post(
        f"{KASSA_BASE}/cheques", json=_cheque_body(cp_id, article_id, op_id), headers=headers
    )
    assert resp.status_code == 403

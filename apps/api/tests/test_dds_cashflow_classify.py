"""Полный разбор РУЧНОЙ проводки ДДС без bank-операции: POST /dds/transactions/{id}/classify.

Ключевой инвариант — БАЛАНС наличного кошелька сохраняется (проводка сама двигает баланс, в
отличие от операции выписки): split раскладывает ту же сумму по статьям; внутренний перевод в
наличный счёт дорисовывает встречную ногу (в банковский — нет, её принесёт выписка); мягкое
исключение убирает проводку из баланса обратимо."""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import CashflowTransaction, DdsArticle, Wallet

HEADERS = {"X-User-Role": "finance_manager"}
WINDOW = {"from": "2026-06-01", "to": "2026-06-30"}


def _run(coro):
    return asyncio.run(coro)


def _make_article(code: str, name: str, movement_type: str = "outflow") -> DdsArticle:
    return DdsArticle(
        code=code, name=name, movement_type=movement_type, activity_type="operating"
    )


def _wallet_balance(client: TestClient, wallet_id: str) -> Decimal:
    r = client.get("/api/v1/dds/wallets", headers=HEADERS)
    assert r.status_code == 200, r.text
    for wallet in r.json():
        if wallet["id"] == wallet_id:
            return Decimal(wallet["balance"])
    raise AssertionError(f"wallet {wallet_id} not found")


def _rows_for(
    factory: async_sessionmaker[AsyncSession], wallet_id: str
) -> list[CashflowTransaction]:
    async def go() -> list[CashflowTransaction]:
        async with factory() as session:
            return list(
                (
                    await session.scalars(
                        select(CashflowTransaction).where(
                            CashflowTransaction.wallet_id == uuid.UUID(wallet_id)
                        )
                    )
                ).all()
            )

    return _run(go())


def _journal_status(client: TestClient, txn_id: str) -> str | None:
    r = client.get("/api/v1/dds/journal", params={"status": "all", **WINDOW}, headers=HEADERS)
    assert r.status_code == 200, r.text
    for item in r.json()["items"]:
        if item["id"] == txn_id:
            return item["status"]
    return None


def _seed_single(
    factory: async_sessionmaker[AsyncSession],
) -> dict[str, str]:
    """Один наличный кошелёк, две статьи, одна неразмеченная проводка −500."""

    async def go() -> dict[str, str]:
        async with factory() as session:
            wallet = Wallet(
                code="cf_cash", name="Касса", type="cash", opening_balance=Decimal("0.00")
            )
            food = _make_article("cf_food", "Продукты")
            wages = _make_article("cf_wages", "Зарплата")
            session.add_all([wallet, food, wages])
            await session.flush()
            txn = CashflowTransaction(
                wallet_id=wallet.id,
                direction="out",
                amount=Decimal("500.00"),
                operation_date=date(2026, 6, 15),
                article_id=None,
                source_kind="manual_adjustment",
                payment_purpose="Неразмеченный расход",
                quality_status="requires_review",
            )
            session.add(txn)
            await session.commit()
            return {
                "wallet_id": str(wallet.id),
                "txn_id": str(txn.id),
                "food_id": str(food.id),
                "wages_id": str(wages.id),
            }

    return _run(go())


def _seed_transfer(
    factory: async_sessionmaker[AsyncSession], *, dest_type: str
) -> dict[str, str]:
    """Исходный наличный кошелёк −500 и счёт-получатель (наличный/банковский).

    Единая транзитная статья ``internal_transfer`` уже есть в каталоге ДДС (миграция 0114,
    подтверждена активной в 0182), поэтому её не создаём — бэкенд резолвит по коду.
    """

    async def go() -> dict[str, str]:
        async with factory() as session:
            source = Wallet(
                code="cf_src", name="Сейф тест", type="cash", opening_balance=Decimal("0.00")
            )
            dest = Wallet(
                code="cf_dest",
                name="Получатель",
                type=dest_type,
                opening_balance=Decimal("0.00"),
            )
            session.add_all([source, dest])
            await session.flush()
            txn = CashflowTransaction(
                wallet_id=source.id,
                direction="out",
                amount=Decimal("500.00"),
                operation_date=date(2026, 6, 15),
                article_id=None,
                source_kind="manual_adjustment",
                payment_purpose="Перевод на другой счёт",
                quality_status="requires_review",
            )
            session.add(txn)
            await session.commit()
            return {
                "source_id": str(source.id),
                "dest_id": str(dest.id),
                "txn_id": str(txn.id),
            }

    return _run(go())


def test_split_preserves_balance(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ids = _seed_single(async_session_factory)
    assert _wallet_balance(client, ids["wallet_id"]) == Decimal("-500.00")

    r = client.post(
        f"/api/v1/dds/transactions/{ids['txn_id']}/classify",
        json={
            "action": "split",
            "splits": [
                {"article_id": ids["food_id"], "amount": "300.00"},
                {"article_id": ids["wages_id"], "amount": "200.00"},
            ],
        },
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["cashflow_transaction_ids"]) == 2

    # Баланс не изменился — те же 500 разложены по двум статьям.
    assert _wallet_balance(client, ids["wallet_id"]) == Decimal("-500.00")
    rows = _rows_for(async_session_factory, ids["wallet_id"])
    assert len(rows) == 2
    assert sum(r.amount for r in rows) == Decimal("500.00")
    assert all(r.article_id is not None for r in rows)


def test_split_amount_mismatch_rejected(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ids = _seed_single(async_session_factory)
    r = client.post(
        f"/api/v1/dds/transactions/{ids['txn_id']}/classify",
        json={
            "action": "split",
            "splits": [
                {"article_id": ids["food_id"], "amount": "300.00"},
                {"article_id": ids["wages_id"], "amount": "100.00"},
            ],
        },
        headers=HEADERS,
    )
    assert r.status_code == 400, r.text
    # Проводка не тронута — по-прежнему одна строка на 500.
    rows = _rows_for(async_session_factory, ids["wallet_id"])
    assert len(rows) == 1
    assert rows[0].amount == Decimal("500.00")


def _transfer_out_article_id(factory: async_sessionmaker[AsyncSession]) -> str:
    async def go() -> str:
        async with factory() as session:
            return str(
                await session.scalar(
                    select(DdsArticle.id).where(DdsArticle.code == "internal_transfer")
                )
            )

    return _run(go())


def test_transfer_row_to_cash_creates_counter_leg(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Строка «Выбытие — перевод между счетами» + наличный счёт-получатель → встречная нога."""
    ids = _seed_transfer(async_session_factory, dest_type="cash")
    transfer_article = _transfer_out_article_id(async_session_factory)

    r = client.post(
        f"/api/v1/dds/transactions/{ids['txn_id']}/classify",
        json={
            "action": "split",
            "splits": [
                {
                    "article_id": transfer_article,
                    "amount": "500.00",
                    "transfer_wallet_id": ids["dest_id"],
                }
            ],
        },
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text

    # Деньги не потерялись: −500 на исходном, +500 на получателе.
    assert _wallet_balance(client, ids["source_id"]) == Decimal("-500.00")
    assert _wallet_balance(client, ids["dest_id"]) == Decimal("500.00")
    source_rows = _rows_for(async_session_factory, ids["source_id"])
    assert len(source_rows) == 1
    assert source_rows[0].transfer_group_id is not None
    dest_rows = _rows_for(async_session_factory, ids["dest_id"])
    assert len(dest_rows) == 1
    assert dest_rows[0].direction == "in"
    assert dest_rows[0].transfer_group_id == source_rows[0].transfer_group_id


def test_transfer_row_to_bank_no_counter_leg(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ids = _seed_transfer(async_session_factory, dest_type="bank")
    transfer_article = _transfer_out_article_id(async_session_factory)

    r = client.post(
        f"/api/v1/dds/transactions/{ids['txn_id']}/classify",
        json={
            "action": "split",
            "splits": [
                {
                    "article_id": transfer_article,
                    "amount": "500.00",
                    "transfer_wallet_id": ids["dest_id"],
                }
            ],
        },
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    # Банковскому счёту встречная нога не нужна — её принесёт выписка.
    assert _wallet_balance(client, ids["source_id"]) == Decimal("-500.00")
    assert _rows_for(async_session_factory, ids["dest_id"]) == []


def test_transfer_wallet_on_non_transfer_article_rejected(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Счёт-получатель допустим только у строки с транзитной статьёй."""
    ids = _seed_single(async_session_factory)
    r = client.post(
        f"/api/v1/dds/transactions/{ids['txn_id']}/classify",
        json={
            "action": "split",
            "splits": [
                {
                    "article_id": ids["food_id"],
                    "amount": "500.00",
                    "transfer_wallet_id": str(uuid.uuid4()),
                }
            ],
        },
        headers=HEADERS,
    )
    assert r.status_code == 400, r.text


def test_exclude_removes_from_balance_reversibly(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ids = _seed_single(async_session_factory)
    assert _wallet_balance(client, ids["wallet_id"]) == Decimal("-500.00")

    r = client.post(
        f"/api/v1/dds/transactions/{ids['txn_id']}/classify",
        json={"action": "exclude"},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    assert _wallet_balance(client, ids["wallet_id"]) == Decimal("0.00")
    assert _journal_status(client, ids["txn_id"]) == "excluded"

    # Обратимо: повторный split возвращает проводку в баланс.
    back = client.post(
        f"/api/v1/dds/transactions/{ids['txn_id']}/classify",
        json={"action": "split", "splits": [{"article_id": ids["food_id"], "amount": "500.00"}]},
        headers=HEADERS,
    )
    assert back.status_code == 200, back.text
    assert _wallet_balance(client, ids["wallet_id"]) == Decimal("-500.00")
    assert _journal_status(client, ids["txn_id"]) == "classified"


def test_bank_operation_sourced_rejected(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Проводка из bank-операции разбирается через операцию выписки, не через этот эндпоинт."""

    async def seed() -> str:
        async with async_session_factory() as session:
            wallet = Wallet(
                code="cf_bankop", name="Банк", type="bank", opening_balance=Decimal("0.00")
            )
            session.add(wallet)
            await session.flush()
            txn = CashflowTransaction(
                wallet_id=wallet.id,
                direction="out",
                amount=Decimal("500.00"),
                operation_date=date(2026, 6, 15),
                source_kind="bank_operation",
                source_id=uuid.uuid4(),
                quality_status="auto",
            )
            session.add(txn)
            await session.commit()
            return str(txn.id)

    txn_id = _run(seed())
    r = client.post(
        f"/api/v1/dds/transactions/{txn_id}/classify",
        json={"action": "exclude"},
        headers=HEADERS,
    )
    assert r.status_code == 400, r.text


def test_classify_missing_transaction_404(client: TestClient) -> None:
    r = client.post(
        f"/api/v1/dds/transactions/{uuid.uuid4()}/classify",
        json={"action": "exclude"},
        headers=HEADERS,
    )
    assert r.status_code == 404, r.text

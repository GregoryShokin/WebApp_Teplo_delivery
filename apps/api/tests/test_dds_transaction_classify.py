"""Ручная разметка проводки ДДС без bank-операции: PATCH /dds/transactions/{id}.

Проводка без статьи в журнале показывается как ``needs_review`` (кликабельна для разметки);
после назначения статьи — ``classified``. Баланс не зависит от статьи."""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import CashflowTransaction, DdsArticle, Wallet

HEADERS = {"X-User-Role": "finance_manager"}
WINDOW = {"from": "2026-06-01", "to": "2026-06-30"}


def _run(coro):
    return asyncio.run(coro)


def _seed(factory: async_sessionmaker[AsyncSession]) -> dict[str, str]:
    async def go() -> dict[str, str]:
        async with factory() as session:
            wallet = Wallet(code="tx_classify_cash", name="Касса тест", type="cash")
            article = DdsArticle(
                code="tx_classify_food",
                name="Разметка продукты",
                movement_type="outflow",
                activity_type="operating",
            )
            session.add_all([wallet, article])
            await session.flush()
            txn = CashflowTransaction(
                wallet_id=wallet.id,
                direction="out",
                amount=Decimal("500.00"),
                operation_date=date(2026, 6, 15),
                article_id=None,
                source_kind="manual_adjustment",
                payment_purpose="Неразмеченный перевод",
                quality_status="requires_review",
            )
            session.add(txn)
            await session.commit()
            return {"txn_id": str(txn.id), "article_id": str(article.id)}

    return _run(go())


def _journal_row(client: TestClient, txn_id: str) -> dict:
    r = client.get("/api/v1/dds/journal", params={"status": "all", **WINDOW}, headers=HEADERS)
    assert r.status_code == 200, r.text
    for item in r.json()["items"]:
        if item["id"] == txn_id:
            return item
    return {}


def test_unmarked_transaction_needs_review_then_classified(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ids = _seed(async_session_factory)

    # до разметки — проводка без статьи показывается как «требует проверки»
    before = _journal_row(client, ids["txn_id"])
    assert before.get("status") == "needs_review"
    assert before.get("article_id") is None

    # ручная разметка через PATCH
    r = client.patch(
        f"/api/v1/dds/transactions/{ids['txn_id']}",
        json={"article_id": ids["article_id"], "counterparty_id": None},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    assert r.json()["article_id"] == ids["article_id"]

    # после — размечено, статья видна
    after = _journal_row(client, ids["txn_id"])
    assert after.get("status") == "classified"
    assert after.get("article_id") == ids["article_id"]


def test_transaction_classify_validation(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ids = _seed(async_session_factory)
    # несуществующая статья → 400
    bad = client.patch(
        f"/api/v1/dds/transactions/{ids['txn_id']}",
        json={"article_id": str(uuid.uuid4())},
        headers=HEADERS,
    )
    assert bad.status_code == 400, bad.text
    # несуществующая проводка → 404
    missing = client.patch(
        f"/api/v1/dds/transactions/{uuid.uuid4()}",
        json={"article_id": None},
        headers=HEADERS,
    )
    assert missing.status_code == 404, missing.text

"""Журнал ДДС показывает внутренние переводы отдельным статусом (движение счёт→счёт).
Регресс: раньше операции со статусом ``internal_transfer`` были видны в балансе, но
выпадали из журнала (он брал только ``needs_review`` + проводки-cashflow)."""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import BankOperation

HEADERS = {"X-User-Role": "finance_manager"}
WINDOW = {"from": "2026-06-01", "to": "2026-06-30"}


def _run(coro):
    return asyncio.run(coro)


def _seed(factory: async_sessionmaker[AsyncSession]) -> None:
    async def go() -> None:
        async with factory() as session:
            session.add_all(
                [
                    BankOperation(
                        provider="sber",
                        provider_operation_id="jr-transfer-1",
                        operation_date=date(2026, 6, 22),
                        direction="out",
                        amount=Decimal("630000.00"),
                        currency="RUB",
                        payment_purpose="Перевод собственных средств НДС не облагается.",
                        raw_payload={},
                        classification_status="internal_transfer",
                    ),
                    BankOperation(
                        provider="tbank",
                        provider_operation_id="jr-review-1",
                        operation_date=date(2026, 6, 22),
                        direction="out",
                        amount=Decimal("1000.00"),
                        currency="RUB",
                        payment_purpose="Оплата поставщику",
                        raw_payload={},
                        classification_status="needs_review",
                    ),
                ]
            )
            await session.commit()

    _run(go())


def _statuses(client: TestClient, status: str) -> list[str]:
    r = client.get("/api/v1/dds/journal", params={"status": status, **WINDOW}, headers=HEADERS)
    assert r.status_code == 200, r.text
    return [item["status"] for item in r.json()["items"]]


def test_journal_internal_transfer_visible(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _seed(async_session_factory)

    # Вкладка «Внутренние переводы»: только переводы, без needs_review.
    r = client.get(
        "/api/v1/dds/journal", params={"status": "transfers", **WINDOW}, headers=HEADERS
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["transfer_total"] >= 1
    assert "internal_transfer" in [it["status"] for it in data["items"]]
    assert "needs_review" not in [it["status"] for it in data["items"]]

    # «Требуют проверки» НЕ содержит перевод; «Все» содержит и то, и другое.
    unmarked = _statuses(client, "unmarked")
    assert "internal_transfer" not in unmarked
    assert "needs_review" in unmarked

    all_rows = _statuses(client, "all")
    assert "internal_transfer" in all_rows
    assert "needs_review" in all_rows

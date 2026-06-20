"""Phase 4 warehouse HTTP endpoints: time match-suggestions, match/confirm, pay-split.

Driven through the FastAPI app so the ``counterparties.*`` permission guards and the
domain-error → 409 mapping are exercised end to end on ``teplo_test``.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime

from cp_helpers import (
    admin_headers,
    headers_for,
    make_bank_operation,
    make_counterparty,
    make_expense_article,
    make_invoice,
    make_wallet,
)
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

BASE = "/api/v1/warehouse"
ISSUED = datetime(2026, 6, 10, 14, 30, tzinfo=UTC)
SAME_DAY = date(2026, 6, 10)


def _run(coro):
    return asyncio.run(coro)


def _admin(factory) -> dict[str, str]:
    return _run(admin_headers(factory))


def _cashier(factory) -> dict[str, str]:
    return _run(headers_for(factory, "wh-cashier@test.local", ["cashier"]))


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    *,
    staff_amount: str | None = None,
    direction: str = "payable",
    payment_status: str = "unpaid",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with factory() as session:
        await make_expense_article(session, code="payment_to_supplier", name="Оплата поставщикам")
        await make_expense_article(
            session, code="supplier_staff_payment", name="Оплата поставщику (персонал)"
        )
        cp = await make_counterparty(session, name="Поставщик")
        invoice = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="1000.00",
            issued_at=ISSUED,
            staff_amount=staff_amount,
            direction=direction,
            payment_status=payment_status,
        )
        op = await make_bank_operation(
            session, amount="1000.00", operation_date=SAME_DAY,
            posted_at=datetime(2026, 6, 10, 14, 32, tzinfo=UTC),
        )
        wallet = await make_wallet(session, name="ТК Черникова", wallet_type="store_cash")
        await session.commit()
        return invoice.id, op.id, wallet.id


def test_match_suggestions_lists_confident_candidate(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    invoice_id, op_id, _ = _run(_seed(async_session_factory))
    resp = client.get(
        f"{BASE}/invoices/{invoice_id}/match-suggestions", headers=_admin(async_session_factory)
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["confident"] is True
    assert data["remaining"] == 1000.0
    assert any(c["bank_operation_id"] == str(op_id) and c["tier"] == 1 for c in data["candidates"])


def test_match_suggestions_forbidden_for_cashier(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    invoice_id, _, _ = _run(_seed(async_session_factory))
    resp = client.get(
        f"{BASE}/invoices/{invoice_id}/match-suggestions", headers=_cashier(async_session_factory)
    )
    assert resp.status_code == 403


def test_match_confirm_pays_invoice(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    invoice_id, op_id, _ = _run(_seed(async_session_factory))
    body = {"invoice_id": str(invoice_id), "bank_operation_id": str(op_id), "enrich": False}
    resp = client.post(f"{BASE}/match/confirm", json=body, headers=_admin(async_session_factory))
    assert resp.status_code == 200, resp.text
    assert resp.json()["payment_status"] == "paid"


def test_pay_split_full_then_conflict_on_repeat(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    invoice_id, op_id, _ = _run(_seed(async_session_factory))
    body = {"bank_parts": [{"bank_operation_id": str(op_id)}], "cash_parts": []}
    headers = _admin(async_session_factory)

    first = client.post(f"{BASE}/invoices/{invoice_id}/pay-split", json=body, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["payment_status"] == "paid"

    # The operation is now occupied → re-pay is rejected with 409 (no double allocation).
    second = client.post(f"{BASE}/invoices/{invoice_id}/pay-split", json=body, headers=headers)
    assert second.status_code == 409


def test_match_confirm_rejects_receivable_invoice(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Guard: a crafted confirm against a receivable (AR) invoice is rejected (409), not
    silently allocated — the suggestion layer never offers it, but the endpoint must too."""
    invoice_id, op_id, _ = _run(_seed(async_session_factory, direction="receivable"))
    body = {"invoice_id": str(invoice_id), "bank_operation_id": str(op_id), "enrich": False}
    resp = client.post(f"{BASE}/match/confirm", json=body, headers=_admin(async_session_factory))
    assert resp.status_code == 409


def test_pay_split_staff_rejected_on_partially_paid(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """split_staff разносит полную сумму накладной → на частично оплаченной даёт понятный 409,
    а не загадочное «сумма превышает остаток»."""
    invoice_id, _, wallet_id = _run(
        _seed(async_session_factory, staff_amount="200.00", payment_status="partially_paid")
    )
    body = {
        "cash_parts": [
            {"wallet_id": str(wallet_id), "amount": "800.00", "operation_date": "2026-06-10"}
        ],
        "split_staff": True,
    }
    resp = client.post(
        f"{BASE}/invoices/{invoice_id}/pay-split", json=body, headers=_admin(async_session_factory)
    )
    assert resp.status_code == 409


def test_pay_split_staff_books_two_articles(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    invoice_id, _, wallet_id = _run(_seed(async_session_factory, staff_amount="200.00"))
    body = {
        "cash_parts": [
            {"wallet_id": str(wallet_id), "amount": "1000.00", "operation_date": "2026-06-10"}
        ],
        "split_staff": True,
    }
    resp = client.post(
        f"{BASE}/invoices/{invoice_id}/pay-split", json=body, headers=_admin(async_session_factory)
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["payment_status"] == "paid"
    assert data["production_amount"] == 800.0
    assert len(data["allocations"]) == 2  # production + персонал on separate DDS articles


def test_kassa_invoice_idempotent_on_duplicate_number(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Повторное «Создать» с тем же номером к тому же контрагенту из Кассы → 409, не дубль.

    Защита от каскада дублей: фронт держит номер после ошибки/двойного клика, а iiko дедупит
    документы по номеру → второй пуш ловил коллизию external_id и валился в 500 (накладная
    уже закоммичена). Здесь второй POST должен отбиться до создания строки."""
    headers = _admin(async_session_factory)

    async def _cp() -> uuid.UUID:
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="Поставщик-дубль")
            await session.commit()
            return cp.id

    cp_id = _run(_cp())
    payload = {
        "counterparty_id": str(cp_id),
        "issued_at": ISSUED.isoformat(),
        "number": "К-777",
        "via_kassa": True,
        "lines": [{"name": "Молоко", "quantity": 2, "price": 50}],
    }
    first = client.post(f"{BASE}/invoices", json=payload, headers=headers)
    assert first.status_code == 201, first.text
    second = client.post(f"{BASE}/invoices", json=payload, headers=headers)
    assert second.status_code == 409, second.text
    assert "уже создана" in second.json()["detail"]

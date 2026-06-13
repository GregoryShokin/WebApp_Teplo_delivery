"""RBAC for the «Контрагенты» API + domain-error HTTP mapping.

Permission tiers (migration 0093): read+operate → owner/admin/manager/office_manager,
admin → owner/admin, cashier → no access. Driven through the FastAPI app so the
``require_permission`` guards and the 409 conflict mapping are both exercised.
"""

from __future__ import annotations

import asyncio
import uuid

from cp_helpers import admin_headers, headers_for, make_counterparty, make_invoice
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

BASE = "/api/v1/counterparties"
VERIFIED_REQUISITES = {
    "bankAcnt": "40702810400000012345",
    "bankBik": "044525225",
    "recipientCorrAccountNumber": "30101810400000000225",
}


def _run(coro):
    return asyncio.run(coro)


def _admin(factory) -> dict[str, str]:
    return _run(admin_headers(factory))


def _manager(factory) -> dict[str, str]:
    return _run(headers_for(factory, "cp-manager@test.local", ["manager"]))


def _cashier(factory) -> dict[str, str]:
    return _run(headers_for(factory, "cp-cashier@test.local", ["cashier"]))


async def _seed_supplier_with_invoice(
    factory: async_sessionmaker[AsyncSession], *, verified: bool
) -> tuple[uuid.UUID, uuid.UUID]:
    async with factory() as session:
        cp = await make_counterparty(
            session,
            name="ООО Поставщик",
            inn="7701234567",
            requisites=VERIFIED_REQUISITES,
            requisites_verified=verified,
        )
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="100.00")
        await session.commit()
        return cp.id, invoice.id


def test_read_tier_registry(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    assert client.get(f"{BASE}/registry").status_code == 401  # unauthenticated
    assert client.get(f"{BASE}/registry", headers=_admin(async_session_factory)).status_code == 200
    assert (
        client.get(f"{BASE}/registry", headers=_manager(async_session_factory)).status_code == 200
    )
    assert (
        client.get(f"{BASE}/registry", headers=_cashier(async_session_factory)).status_code == 403
    )


def test_operate_tier_auto_match(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    assert (
        client.post(f"{BASE}/match/auto", headers=_admin(async_session_factory)).status_code == 200
    )
    assert (
        client.post(f"{BASE}/match/auto", headers=_manager(async_session_factory)).status_code
        == 200
    )
    assert (
        client.post(f"{BASE}/match/auto", headers=_cashier(async_session_factory)).status_code
        == 403
    )


def test_admin_tier_create_category(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    def payload() -> dict:
        return {"code": f"cat_{uuid.uuid4().hex[:8]}", "name": "Маркетинг", "sort_order": 1}

    assert (
        client.post(
            f"{BASE}/categories", json=payload(), headers=_cashier(async_session_factory)
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"{BASE}/categories", json=payload(), headers=_manager(async_session_factory)
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"{BASE}/categories", json=payload(), headers=_admin(async_session_factory)
        ).status_code
        == 201
    )


def test_post_draft_without_verified_requisites_returns_409(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _cp_id, invoice_id = _run(_seed_supplier_with_invoice(async_session_factory, verified=False))

    response = client.post(
        f"{BASE}/drafts",
        json={"invoice_ids": [str(invoice_id)]},
        headers=_manager(async_session_factory),
    )

    assert response.status_code == 409
    assert "одтвержден" in response.json()["detail"]  # «не подтверждены»


def test_post_draft_happy_path_as_manager_returns_201(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _cp_id, invoice_id = _run(_seed_supplier_with_invoice(async_session_factory, verified=True))

    response = client.post(
        f"{BASE}/drafts",
        json={"invoice_ids": [str(invoice_id)]},
        headers=_manager(async_session_factory),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "created"
    assert body["amount"] == 100.0

"""Гранулярные права на накладные — invoices.normal.* / invoices.barter.* (миграция 0117).

Проверяет РАЗДЕЛЕНИЕ доступа на странице «Накладные»: право на обычные накладные не
даёт доступ к бартерным и наоборот; бывшего общего ``counterparties.operate`` для
создания/оплаты теперь недостаточно. Прогон через FastAPI — реальные guard'ы.

Пользователь с произвольным набором прав собирается на лету (временная роль с нужными
permission-кодами), т.к. готовых ролей с гранулярным набором нет.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence

from cp_helpers import make_counterparty, make_invoice, token_headers
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Organization, Permission, Role, RolePermission, User, UserRole

BASE = "/api/v1/warehouse"
CP_BASE = "/api/v1/counterparties"


def _run(coro):
    return asyncio.run(coro)


async def _user_with_perms(
    factory: async_sessionmaker[AsyncSession], email: str, codes: Sequence[str]
) -> uuid.UUID:
    async with factory() as session:
        existing = await session.scalar(select(User).where(User.email == email))
        if existing is not None:
            return existing.id
        org_id = await session.scalar(select(Organization.id).limit(1))
        assert org_id is not None
        role = Role(code=f"t-{uuid.uuid4().hex[:10]}", name="test-role")
        session.add(role)
        await session.flush()
        perms = (
            await session.scalars(select(Permission).where(Permission.code.in_(tuple(codes))))
        ).all()
        assert {p.code for p in perms} == set(codes), "permission codes must be seeded"
        for perm in perms:
            session.add(RolePermission(role_id=role.id, permission_id=perm.id))
        user = User(
            email=email, full_name=email, hashed_password="sha256$unused", is_active=True
        )
        session.add(user)
        await session.flush()
        session.add(UserRole(user_id=user.id, role_id=role.id, organization_id=org_id))
        await session.commit()
        return user.id


def _headers(
    factory: async_sessionmaker[AsyncSession], email: str, codes: Sequence[str]
) -> dict[str, str]:
    return token_headers(_run(_user_with_perms(factory, email, codes)))


def _seed_counterparty(
    factory: async_sessionmaker[AsyncSession], *, relationship: str = "official"
) -> uuid.UUID:
    async def _seed() -> uuid.UUID:
        async with factory() as session:
            cp = await make_counterparty(
                session, name=f"CP {uuid.uuid4().hex[:6]}", relationship=relationship
            )
            await session.commit()
            return cp.id

    return _run(_seed())


def _seed_invoice(
    factory: async_sessionmaker[AsyncSession], *, relationship: str, barter_role: str | None
) -> uuid.UUID:
    async def _seed() -> uuid.UUID:
        async with factory() as session:
            cp = await make_counterparty(
                session, name=f"CP {uuid.uuid4().hex[:6]}", relationship=relationship
            )
            inv = await make_invoice(
                session, counterparty_id=cp.id, amount="100.00", barter_role=barter_role
            )
            await session.commit()
            return inv.id

    return _run(_seed())


def _invoice_payload(cp_id: uuid.UUID, mode: str) -> dict:
    return {
        "counterparty_id": str(cp_id),
        "issued_at": "2026-06-17T12:00:00+00:00",
        "mode": mode,
        "we_lend": False,
        "lines": [{"name": "Товар", "quantity": "1", "price": "100"}],
    }


def test_operate_alone_cannot_create(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Бывшего общего operate теперь НЕ хватает для создания — нужно явное invoices.*.create."""
    cp = _seed_counterparty(async_session_factory)
    headers = _headers(
        async_session_factory,
        "wh-operate-only@test.local",
        ["counterparties.read", "counterparties.operate"],
    )
    resp = client.post(f"{BASE}/invoices", json=_invoice_payload(cp, "normal"), headers=headers)
    assert resp.status_code == 403, resp.text


def test_normal_create_scope(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """invoices.normal.create: обычную создать можно, бартер-займ — нет."""
    headers = _headers(
        async_session_factory, "wh-normal-create@test.local", ["invoices.normal.create"]
    )
    normal_cp = _seed_counterparty(async_session_factory)
    ok = client.post(
        f"{BASE}/invoices", json=_invoice_payload(normal_cp, "normal"), headers=headers
    )
    assert ok.status_code == 201, ok.text

    barter_cp = _seed_counterparty(async_session_factory)
    forbidden = client.post(
        f"{BASE}/invoices", json=_invoice_payload(barter_cp, "loan"), headers=headers
    )
    assert forbidden.status_code == 403, forbidden.text


def test_barter_create_scope(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """invoices.barter.create: бартер-займ создать можно, обычную — нет."""
    headers = _headers(
        async_session_factory, "wh-barter-create@test.local", ["invoices.barter.create"]
    )
    barter_cp = _seed_counterparty(async_session_factory)
    ok = client.post(f"{BASE}/invoices", json=_invoice_payload(barter_cp, "loan"), headers=headers)
    assert ok.status_code == 201, ok.text

    normal_cp = _seed_counterparty(async_session_factory)
    forbidden = client.post(
        f"{BASE}/invoices", json=_invoice_payload(normal_cp, "normal"), headers=headers
    )
    assert forbidden.status_code == 403, forbidden.text


def test_pay_split_scope_by_invoice_kind(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """invoices.normal.pay: оплатить обычную можно (не 403), бартерную — нельзя (403)."""
    headers = _headers(async_session_factory, "wh-normal-pay@test.local", ["invoices.normal.pay"])
    body = {"bank_parts": [], "cash_parts": []}

    normal_id = _seed_invoice(async_session_factory, relationship="official", barter_role=None)
    normal_resp = client.post(
        f"{BASE}/invoices/{normal_id}/pay-split", json=body, headers=headers
    )
    assert normal_resp.status_code != 403, normal_resp.text

    barter_id = _seed_invoice(async_session_factory, relationship="barter", barter_role="loan")
    barter_resp = client.post(
        f"{BASE}/invoices/{barter_id}/pay-split", json=body, headers=headers
    )
    assert barter_resp.status_code == 403, barter_resp.text


def test_barter_pay_scope_on_counterparty_pay(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """invoices.barter.pay: оплата бартерной можно, обычной — нельзя (counterparties pay)."""
    headers = _headers(async_session_factory, "wh-barter-pay@test.local", ["invoices.barter.pay"])
    body = {"wallet_id": str(uuid.uuid4()), "amount": "10.00", "operation_date": "2026-06-17"}

    barter_id = _seed_invoice(async_session_factory, relationship="barter", barter_role="loan")
    barter_resp = client.post(
        f"{CP_BASE}/invoices/{barter_id}/pay", json=body, headers=headers
    )
    assert barter_resp.status_code != 403, barter_resp.text

    normal_id = _seed_invoice(async_session_factory, relationship="official", barter_role=None)
    normal_resp = client.post(
        f"{CP_BASE}/invoices/{normal_id}/pay", json=body, headers=headers
    )
    assert normal_resp.status_code == 403, normal_resp.text

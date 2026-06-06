from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import create_access_token
from app.models import (
    Organization,
    Permission,
    Role,
    RolePermission,
    RolePermissionEvent,
    User,
    UserRole,
    UserRoleEvent,
)

READ_GUARD_CASES = (
    "/api/v1/dds/wallets",
    "/api/v1/payroll/runs",
    "/api/v1/schedule",
    "/api/v1/shifts/ledger?date=2026-06-01",
    "/api/v1/inventory/positions",
    "/api/v1/employees",
    "/api/v1/couriers/list",
    "/api/v1/deposits",
    "/api/v1/vacations",
    "/api/v1/payroll/fund/tiers",
)


@pytest.mark.parametrize("path", READ_GUARD_CASES)
def test_migrated_read_guards_require_permissions(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
    path: str,
) -> None:
    admin_headers = _headers_for_admin(async_session_factory)
    cashier_headers = _headers_for_user(
        async_session_factory,
        "cashier-read@test.local",
        ["cashier"],
    )

    denied = client.get(path, headers=cashier_headers)
    allowed = client.get(path, headers=admin_headers)

    assert denied.status_code == 403
    assert allowed.status_code == 200


def test_migrated_write_guard_allows_permission_and_denies_missing(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    cashier_headers = _headers_for_user(
        async_session_factory,
        "cashier-write@test.local",
        ["cashier"],
    )
    payload = {
        "code": f"test_{uuid.uuid4().hex[:8]}",
        "display_name": "Test inventory item",
        "allocation_group": "common",
    }

    denied = client.post("/api/v1/inventory/positions", json=payload, headers=cashier_headers)
    allowed = client.post(
        "/api/v1/inventory/positions",
        json={**payload, "code": f"test_{uuid.uuid4().hex[:8]}"},
        headers=_headers_for_admin(async_session_factory),
    )

    assert denied.status_code == 403
    assert allowed.status_code == 200


def test_put_role_permissions_updates_set_and_writes_events(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_headers = _headers_for_admin(async_session_factory)
    manager_id = _run(_role_id(async_session_factory, "manager"))
    owner_id = _run(_role_id(async_session_factory, "owner"))

    response = client.put(
        f"/api/v1/access-control/roles/{manager_id}/permissions",
        json={"permission_codes": ["dds.read", "inventory.write"]},
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["permission_codes"] == ["dds.read", "inventory.write"]
    assert _run(_role_permission_codes(async_session_factory, "manager")) == {
        "dds.read",
        "inventory.write",
    }

    response = client.put(
        f"/api/v1/access-control/roles/{manager_id}/permissions",
        json={"permission_codes": ["dds.read"]},
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert _run(_role_permission_codes(async_session_factory, "manager")) == {"dds.read"}
    assert sorted(_run(_role_permission_events(async_session_factory, "manager"))) == [
        ("added", "dds.read"),
        ("added", "inventory.write"),
        ("removed", "inventory.write"),
    ]

    fixed = client.put(
        f"/api/v1/access-control/roles/{owner_id}/permissions",
        json={"permission_codes": []},
        headers=admin_headers,
    )
    assert fixed.status_code == 400


def test_assign_and_revoke_user_role_write_audit_events(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_headers = _headers_for_admin(async_session_factory)
    user_id = _run(_create_user(async_session_factory, "target-role@test.local", []))

    assigned = client.post(
        f"/api/v1/access-control/users/{user_id}/roles",
        json={"role_code": "manager"},
        headers=admin_headers,
    )
    revoked = client.delete(
        f"/api/v1/access-control/users/{user_id}/roles/manager",
        headers=admin_headers,
    )
    audit = client.get("/api/v1/access-control/audit", headers=admin_headers)

    assert assigned.status_code == 200
    assert "manager" in assigned.json()["role_codes"]
    assert revoked.status_code == 200
    assert "manager" not in revoked.json()["role_codes"]
    assert _run(_user_role_events(async_session_factory, user_id)) == [
        ("assigned", "manager"),
        ("revoked", "manager"),
    ]
    assert audit.status_code == 200
    assert any(event["type"] == "user_role" for event in audit.json())


def test_self_lockout_guards(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_id, admin_headers = _run(_admin_identity(async_session_factory))
    owner_user_id = _run(_create_user(async_session_factory, "only-owner@test.local", ["owner"]))

    revoke_last_owner = client.delete(
        f"/api/v1/access-control/users/{owner_user_id}/roles/owner",
        headers=admin_headers,
    )
    deactivate_self = client.patch(
        f"/api/v1/access-control/users/{admin_id}",
        json={"is_active": False},
        headers=admin_headers,
    )

    assert revoke_last_owner.status_code == 400
    assert deactivate_self.status_code == 400


def _headers_for_admin(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, str]:
    _admin_id, headers = _run(_admin_identity(session_factory))
    return headers


def _headers_for_user(
    session_factory: async_sessionmaker[AsyncSession],
    email: str,
    role_codes: Sequence[str],
) -> dict[str, str]:
    user_id = _run(_create_user(session_factory, email, role_codes))
    return {"Authorization": f"Bearer {create_access_token(str(user_id))}"}


async def _admin_identity(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, dict[str, str]]:
    async with session_factory() as session:
        admin_id = await session.scalar(select(User.id).where(User.email == "admin@teplo.local"))
        assert admin_id is not None
    return admin_id, {"Authorization": f"Bearer {create_access_token(str(admin_id))}"}


async def _create_user(
    session_factory: async_sessionmaker[AsyncSession],
    email: str,
    role_codes: Sequence[str],
) -> uuid.UUID:
    async with session_factory() as session:
        existing = await session.scalar(select(User).where(User.email == email))
        if existing is not None:
            return existing.id
        organization_id = await session.scalar(select(Organization.id).limit(1))
        assert organization_id is not None
        roles = (
            await session.scalars(select(Role).where(Role.code.in_(tuple(role_codes))))
        ).all()
        assert {role.code for role in roles} == set(role_codes)
        user = User(
            email=email,
            full_name=email,
            hashed_password="sha256$unused",
            is_active=True,
        )
        session.add(user)
        await session.flush()
        for role in roles:
            session.add(
                UserRole(
                    user_id=user.id,
                    role_id=role.id,
                    organization_id=organization_id,
                )
            )
        await session.commit()
        return user.id


async def _role_id(
    session_factory: async_sessionmaker[AsyncSession],
    role_code: str,
) -> uuid.UUID:
    async with session_factory() as session:
        role_id = await session.scalar(select(Role.id).where(Role.code == role_code))
        assert role_id is not None
        return role_id


async def _role_permission_codes(
    session_factory: async_sessionmaker[AsyncSession],
    role_code: str,
) -> set[str]:
    async with session_factory() as session:
        return set(
            await session.scalars(
                select(Permission.code)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .join(Role, Role.id == RolePermission.role_id)
                .where(Role.code == role_code)
            )
        )


async def _role_permission_events(
    session_factory: async_sessionmaker[AsyncSession],
    role_code: str,
) -> list[tuple[str, str]]:
    async with session_factory() as session:
        rows = await session.execute(
            select(RolePermissionEvent.action, Permission.code)
            .join(Role, Role.id == RolePermissionEvent.role_id)
            .join(Permission, Permission.id == RolePermissionEvent.permission_id)
            .where(Role.code == role_code)
            .order_by(RolePermissionEvent.created_at, RolePermissionEvent.id)
        )
        return [(action, permission_code) for action, permission_code in rows]


async def _user_role_events(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
) -> list[tuple[str, str]]:
    async with session_factory() as session:
        rows = await session.execute(
            select(UserRoleEvent.action, Role.code)
            .join(Role, Role.id == UserRoleEvent.role_id)
            .where(UserRoleEvent.user_id == user_id)
            .order_by(UserRoleEvent.created_at, UserRoleEvent.id)
        )
        return [(action, role_code) for action, role_code in rows]


def _run(value):
    return asyncio.run(value)

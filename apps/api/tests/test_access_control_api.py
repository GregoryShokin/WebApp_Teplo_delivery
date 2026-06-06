from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.permissions import (
    DEFAULT_ROLE_PERMISSIONS,
    LEGACY_PERMISSION_CODES,
    MODULE_ORDER,
    VISIBLE_PERMISSION_CODES,
)
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
    ("/api/v1/dds/wallets", False),
    ("/api/v1/payroll/runs", False),
    ("/api/v1/schedule", True),
    ("/api/v1/shifts/ledger?date=2026-06-01", True),
    ("/api/v1/inventory/positions", True),
    ("/api/v1/employees", False),
    ("/api/v1/couriers/list", True),
    ("/api/v1/deposits", False),
    ("/api/v1/vacations", False),
    ("/api/v1/payroll/fund/tiers", False),
)


@pytest.mark.parametrize(("path", "cashier_allowed"), READ_GUARD_CASES)
def test_migrated_read_guards_follow_passport_defaults(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
    path: str,
    cashier_allowed: bool,
) -> None:
    admin_headers = _headers_for_admin(async_session_factory)
    cashier_headers = _headers_for_user(
        async_session_factory,
        "cashier-read@test.local",
        ["cashier"],
    )

    denied = client.get(path, headers=cashier_headers)
    allowed = client.get(path, headers=admin_headers)

    assert denied.status_code == (200 if cashier_allowed else 403)
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
        json={"permission_codes": ["finance.cashflow.read", "accounting.inventory_directory.edit"]},
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert set(response.json()["permission_codes"]) == {
        "finance.cashflow.read",
        "accounting.inventory_directory.edit",
    }
    assert _run(_role_permission_codes(async_session_factory, "manager")) == {
        "finance.cashflow.read",
        "accounting.inventory_directory.edit",
    }

    response = client.put(
        f"/api/v1/access-control/roles/{manager_id}/permissions",
        json={"permission_codes": ["finance.cashflow.read"]},
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert _run(_role_permission_codes(async_session_factory, "manager")) == {
        "finance.cashflow.read"
    }
    events = set(_run(_role_permission_events(async_session_factory, "manager")))
    assert ("added", "finance.cashflow.read") in events
    assert ("added", "accounting.inventory_directory.edit") in events
    assert ("removed", "accounting.inventory_directory.edit") in events

    fixed = client.put(
        f"/api/v1/access-control/roles/{owner_id}/permissions",
        json={"permission_codes": []},
        headers=admin_headers,
    )
    assert fixed.status_code == 400


def test_access_control_permissions_api_returns_visible_passport_catalog(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    response = client.get(
        "/api/v1/access-control/permissions",
        headers=_headers_for_admin(async_session_factory),
    )

    assert response.status_code == 200
    payload = response.json()
    assert [group["module"] for group in payload] == list(MODULE_ORDER)
    returned_codes = {
        permission["code"]
        for group in payload
        for permission in group["permissions"]
    }
    assert returned_codes == VISIBLE_PERMISSION_CODES
    assert returned_codes.isdisjoint(LEGACY_PERMISSION_CODES)
    assert any(
        permission["description"] == "Смотреть администрацию"
        for group in payload
        for permission in group["permissions"]
    )


def test_access_control_roles_api_returns_passport_defaults(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    response = client.get(
        "/api/v1/access-control/roles",
        headers=_headers_for_admin(async_session_factory),
    )

    assert response.status_code == 200
    roles = {role["code"]: role for role in response.json()}
    assert set(roles) == {"owner", "manager", "office_manager", "cashier"}
    assert roles["owner"]["is_editable"] is False
    assert set(roles["owner"]["permission_codes"]) == VISIBLE_PERMISSION_CODES
    assert set(roles["manager"]["permission_codes"]) == DEFAULT_ROLE_PERMISSIONS["manager"]
    assert set(roles["office_manager"]["permission_codes"]) == DEFAULT_ROLE_PERMISSIONS[
        "office_manager"
    ]
    assert set(roles["cashier"]["permission_codes"]) == DEFAULT_ROLE_PERMISSIONS["cashier"]
    assert "staff.administration.read" not in roles["manager"]["permission_codes"]
    assert "staff.administration.edit" not in roles["manager"]["permission_codes"]


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

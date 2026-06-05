from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentActor, require_permission
from app.auth.permissions import ALL_PERMISSION_CODES
from app.models import Permission, Role, RolePermission


def test_rbac_seed_grants_only_owner_and_admin(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    role_permissions, permission_codes = asyncio.run(
        _load_seeded_permissions(async_session_factory)
    )

    assert permission_codes == ALL_PERMISSION_CODES
    assert role_permissions["owner"] == ALL_PERMISSION_CODES
    assert role_permissions["admin"] == ALL_PERMISSION_CODES
    assert role_permissions["manager"] == frozenset()
    assert role_permissions["cashier"] == frozenset()


def test_require_permission_allows_matching_permission_and_denies_missing() -> None:
    dependency = require_permission("dds.classify")

    asyncio.run(
        dependency(
            CurrentActor(
                roles=frozenset({"cashier"}),
                permissions=frozenset({"dds.classify"}),
            )
        )
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(dependency(CurrentActor(roles=frozenset({"cashier"}))))

    assert exc_info.value.status_code == 403


async def _load_seeded_permissions(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[dict[str, frozenset[str]], frozenset[str]]:
    role_codes = ("owner", "admin", "manager", "cashier")
    role_permissions: dict[str, set[str]] = {code: set() for code in role_codes}

    async with session_factory() as session:
        permission_codes = frozenset(await session.scalars(select(Permission.code)))
        seeded_roles = set(
            await session.scalars(select(Role.code).where(Role.code.in_(role_codes)))
        )
        assert seeded_roles == set(role_codes)

        rows = await session.execute(
            select(Role.code, Permission.code)
            .join(RolePermission, RolePermission.role_id == Role.id)
            .join(Permission, Permission.id == RolePermission.permission_id)
            .where(Role.code.in_(role_codes))
        )
        for role_code, permission_code in rows:
            role_permissions[role_code].add(permission_code)

    return (
        {role_code: frozenset(codes) for role_code, codes in role_permissions.items()},
        permission_codes,
    )

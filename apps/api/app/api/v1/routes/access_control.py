from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.api.deps import CurrentActor, get_current_actor, require_permission
from app.core.security import hash_password
from app.db.session import get_session
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
from app.schemas.access_control import (
    AccessAuditEventRead,
    AccessUserCreate,
    AccessUserPatch,
    AccessUserRead,
    PermissionModuleRead,
    RoleAccessRead,
    RolePermissionsUpdate,
    UserRoleUpdate,
)

router = APIRouter()

ACCESS_ROLE_CODES = ("owner", "manager", "office_manager", "cashier")
EDITABLE_ROLE_CODES = frozenset({"manager", "office_manager", "cashier"})
ROLES_WRITE_ACCESS = (Depends(require_permission("access_control.roles.write")),)
USERS_WRITE_ACCESS = (Depends(require_permission("access_control.users.write")),)


@router.get(
    "/permissions",
    response_model=list[PermissionModuleRead],
    dependencies=ROLES_WRITE_ACCESS,
)
async def list_permissions(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, object]]:
    rows = (
        await session.scalars(select(Permission).order_by(Permission.module, Permission.code))
    ).all()
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for permission in rows:
        grouped[permission.module].append(
            {"code": permission.code, "description": permission.description}
        )
    return [
        {"module": module, "permissions": permissions}
        for module, permissions in grouped.items()
    ]


@router.get("/roles", response_model=list[RoleAccessRead], dependencies=ROLES_WRITE_ACCESS)
async def list_roles(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, object]]:
    roles = await _access_roles(session)
    permissions_by_role = await _role_permission_codes(session, [role.id for role in roles])
    return [_role_payload(role, permissions_by_role.get(role.id, [])) for role in roles]


@router.put(
    "/roles/{role_id}/permissions",
    response_model=RoleAccessRead,
    dependencies=ROLES_WRITE_ACCESS,
)
async def put_role_permissions(
    role_id: uuid.UUID,
    payload: RolePermissionsUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, object]:
    role = await session.get(Role, role_id)
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    if role.code not in EDITABLE_ROLE_CODES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Role is fixed")

    requested_codes = set(payload.permission_codes)
    permissions = (
        await session.scalars(
            select(Permission).where(Permission.code.in_(requested_codes)).order_by(Permission.code)
        )
    ).all()
    permissions_by_code = {permission.code: permission for permission in permissions}
    missing_codes = requested_codes - set(permissions_by_code)
    if missing_codes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown permissions: {', '.join(sorted(missing_codes))}",
        )

    current_rows = (
        await session.scalars(
            select(RolePermission).where(RolePermission.role_id == role.id)
        )
    ).all()
    current_permission_ids = {row.permission_id for row in current_rows}
    requested_permission_ids = {permission.id for permission in permissions}
    added_ids = requested_permission_ids - current_permission_ids
    removed_ids = current_permission_ids - requested_permission_ids

    for permission_id in added_ids:
        session.add(RolePermission(role_id=role.id, permission_id=permission_id))
        session.add(
            RolePermissionEvent(
                role_id=role.id,
                permission_id=permission_id,
                action="added",
                actor_user_id=actor.user_id,
            )
        )
    if removed_ids:
        await session.execute(
            delete(RolePermission).where(
                RolePermission.role_id == role.id,
                RolePermission.permission_id.in_(removed_ids),
            )
        )
        for permission_id in removed_ids:
            session.add(
                RolePermissionEvent(
                    role_id=role.id,
                    permission_id=permission_id,
                    action="removed",
                    actor_user_id=actor.user_id,
                )
            )

    await session.commit()
    return _role_payload(role, sorted(requested_codes))


@router.get("/users", response_model=list[AccessUserRead], dependencies=USERS_WRITE_ACCESS)
async def list_users(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, object]]:
    users = (await session.scalars(select(User).order_by(User.email))).all()
    roles_by_user = await _user_role_codes(session, [user.id for user in users])
    return [_user_payload(user, roles_by_user.get(user.id, [])) for user in users]


@router.post(
    "/users",
    response_model=AccessUserRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=USERS_WRITE_ACCESS,
)
async def create_user(
    payload: AccessUserCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    email = payload.email.strip().lower()
    existing = await session.scalar(select(User).where(func.lower(User.email) == email))
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already exists")

    user = User(
        email=email,
        full_name=payload.full_name.strip(),
        hashed_password=hash_password(payload.password),
        is_active=payload.is_active,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return _user_payload(user, [])


@router.patch(
    "/users/{user_id}",
    response_model=AccessUserRead,
    dependencies=USERS_WRITE_ACCESS,
)
async def patch_user(
    user_id: uuid.UUID,
    payload: AccessUserPatch,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, object]:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if (
        user_id == actor.user_id
        and "is_active" in payload.model_fields_set
        and payload.is_active is False
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot deactivate your own account",
        )

    if "full_name" in payload.model_fields_set and payload.full_name is not None:
        user.full_name = payload.full_name.strip()
    if "is_active" in payload.model_fields_set and payload.is_active is not None:
        user.is_active = payload.is_active

    await session.commit()
    await session.refresh(user)
    roles_by_user = await _user_role_codes(session, [user.id])
    return _user_payload(user, roles_by_user.get(user.id, []))


@router.post(
    "/users/{user_id}/roles",
    response_model=AccessUserRead,
    dependencies=USERS_WRITE_ACCESS,
)
async def assign_user_role(
    user_id: uuid.UUID,
    payload: UserRoleUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, object]:
    user = await _user_or_404(session, user_id)
    role = await _assignable_role_or_400(session, payload.role_code)
    existing = await session.scalar(
        select(UserRole).where(UserRole.user_id == user.id, UserRole.role_id == role.id)
    )
    if existing is None:
        organization_id = await _organization_id_for_user_role(session, user.id)
        session.add(UserRole(user_id=user.id, role_id=role.id, organization_id=organization_id))
        session.add(
            UserRoleEvent(
                user_id=user.id,
                role_id=role.id,
                action="assigned",
                actor_user_id=actor.user_id,
            )
        )
        await session.commit()

    roles_by_user = await _user_role_codes(session, [user.id])
    return _user_payload(user, roles_by_user.get(user.id, []))


@router.delete(
    "/users/{user_id}/roles/{role_code}",
    response_model=AccessUserRead,
    dependencies=USERS_WRITE_ACCESS,
)
async def revoke_user_role(
    user_id: uuid.UUID,
    role_code: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, object]:
    user = await _user_or_404(session, user_id)
    role = await _assignable_role_or_400(session, role_code)
    user_role = await session.scalar(
        select(UserRole).where(UserRole.user_id == user.id, UserRole.role_id == role.id)
    )
    if user_role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User role not found")
    if role.code == "owner" and await _owner_holder_count(session) <= 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot revoke the last owner role",
        )

    await session.delete(user_role)
    session.add(
        UserRoleEvent(
            user_id=user.id,
            role_id=role.id,
            action="revoked",
            actor_user_id=actor.user_id,
        )
    )
    await session.commit()
    roles_by_user = await _user_role_codes(session, [user.id])
    return _user_payload(user, roles_by_user.get(user.id, []))


@router.get("/audit", response_model=list[AccessAuditEventRead], dependencies=ROLES_WRITE_ACCESS)
async def list_audit_events(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AccessAuditEventRead]:
    actor_user = aliased(User)
    role_permission_rows = (
        await session.execute(
            select(
                RolePermissionEvent,
                Role.code,
                Permission.code,
                actor_user.email,
            )
            .join(Role, Role.id == RolePermissionEvent.role_id)
            .join(Permission, Permission.id == RolePermissionEvent.permission_id)
            .outerjoin(actor_user, actor_user.id == RolePermissionEvent.actor_user_id)
        )
    ).all()

    target_user = aliased(User)
    user_role_actor = aliased(User)
    user_role_rows = (
        await session.execute(
            select(
                UserRoleEvent,
                target_user.email,
                Role.code,
                user_role_actor.email,
            )
            .join(target_user, target_user.id == UserRoleEvent.user_id)
            .join(Role, Role.id == UserRoleEvent.role_id)
            .outerjoin(user_role_actor, user_role_actor.id == UserRoleEvent.actor_user_id)
        )
    ).all()

    events: list[AccessAuditEventRead] = [
        AccessAuditEventRead(
            type="role_permission",
            actor_email=actor_email,
            role_code=role_code,
            permission_code=permission_code,
            action=event.action,
            created_at=event.created_at,
        )
        for event, role_code, permission_code, actor_email in role_permission_rows
    ]
    events.extend(
        AccessAuditEventRead(
            type="user_role",
            actor_email=actor_email,
            user_email=user_email,
            role_code=role_code,
            permission_code=None,
            action=event.action,
            created_at=event.created_at,
        )
        for event, user_email, role_code, actor_email in user_role_rows
    )
    events.sort(key=lambda item: item.created_at, reverse=True)
    return events[offset : offset + limit]


async def _access_roles(session: AsyncSession) -> list[Role]:
    roles = (
        await session.scalars(select(Role).where(Role.code.in_(ACCESS_ROLE_CODES)))
    ).all()
    by_code = {role.code: role for role in roles}
    return [by_code[code] for code in ACCESS_ROLE_CODES if code in by_code]


async def _role_permission_codes(
    session: AsyncSession,
    role_ids: list[uuid.UUID],
) -> dict[uuid.UUID, list[str]]:
    if not role_ids:
        return {}
    rows = await session.execute(
        select(RolePermission.role_id, Permission.code)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .where(RolePermission.role_id.in_(role_ids))
        .order_by(Permission.code)
    )
    grouped: dict[uuid.UUID, list[str]] = defaultdict(list)
    for role_id, permission_code in rows:
        grouped[role_id].append(permission_code)
    return grouped


async def _user_role_codes(
    session: AsyncSession,
    user_ids: list[uuid.UUID],
) -> dict[uuid.UUID, list[str]]:
    if not user_ids:
        return {}
    rows = await session.execute(
        select(UserRole.user_id, Role.code)
        .join(Role, Role.id == UserRole.role_id)
        .where(UserRole.user_id.in_(user_ids))
        .order_by(Role.code)
    )
    grouped: dict[uuid.UUID, list[str]] = defaultdict(list)
    for user_id, role_code in rows:
        grouped[user_id].append(role_code)
    return grouped


def _role_payload(role: Role, permission_codes: list[str]) -> dict[str, object]:
    return {
        "id": role.id,
        "code": role.code,
        "name": role.name,
        "is_editable": role.code in EDITABLE_ROLE_CODES,
        "permission_codes": sorted(permission_codes),
    }


def _user_payload(user: User, role_codes: list[str]) -> dict[str, object]:
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "is_active": user.is_active,
        "role_codes": sorted(role_codes),
    }


async def _user_or_404(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


async def _assignable_role_or_400(session: AsyncSession, role_code: str) -> Role:
    if role_code not in ACCESS_ROLE_CODES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Role is not assignable")
    role = await session.scalar(select(Role).where(Role.code == role_code))
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    return role


async def _organization_id_for_user_role(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> uuid.UUID:
    organization_id = await session.scalar(
        select(UserRole.organization_id).where(UserRole.user_id == user_id).limit(1)
    )
    if organization_id is not None:
        return organization_id
    organization_id = await session.scalar(select(Organization.id).order_by(Organization.name).limit(1))
    if organization_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Organization not found")
    return organization_id


async def _owner_holder_count(session: AsyncSession) -> int:
    return int(
        await session.scalar(
            select(func.count(distinct(UserRole.user_id)))
            .join(Role, Role.id == UserRole.role_id)
            .where(Role.code == "owner")
        )
        or 0
    )

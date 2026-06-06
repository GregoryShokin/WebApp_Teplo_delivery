from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import ALL_PERMISSION_CODES, permission_is_granted
from app.auth.service import get_permission_codes_for_roles, get_user_by_id, get_user_role_codes
from app.core.config import get_settings
from app.core.security import decode_access_token
from app.db.session import get_session


@dataclass(frozen=True)
class CurrentActor:
    roles: frozenset[str]
    user_id: uuid.UUID | None = None
    permissions: frozenset[str] = frozenset()
    permissions_loaded: bool = False


ROLE_HIERARCHY = ("manager", "accountant", "finance_manager", "owner", "admin")
MANAGER_PLUS = frozenset(ROLE_HIERARCHY)
FINANCE_MANAGER_PLUS = frozenset({"finance_manager", "owner", "admin"})


def _split_roles(value: str | None) -> set[str]:
    if not value:
        return set()
    return {part.strip() for part in value.replace(";", ",").split(",") if part.strip()}


async def get_current_actor(
    session: Annotated[AsyncSession, Depends(get_session)],
    authorization: Annotated[str | None, Header()] = None,
    x_user_role: Annotated[str | None, Header()] = None,
    x_user_roles: Annotated[str | None, Header()] = None,
) -> CurrentActor:
    header_roles = _split_roles(x_user_roles) | _split_roles(x_user_role)
    roles = set(header_roles)
    token_roles: set[str] = set()
    user_id: uuid.UUID | None = None

    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        try:
            claims = decode_access_token(token)
        except Exception as exc:  # noqa: BLE001 - keep auth errors opaque
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid access token"
            ) from exc
        claim_roles = claims.get("roles") or claims.get("role") or []
        if isinstance(claim_roles, str):
            token_roles |= _split_roles(claim_roles)
        else:
            token_roles |= {str(role) for role in claim_roles}
        subject = claims.get("sub")
        if subject:
            try:
                user_id = uuid.UUID(str(subject))
            except ValueError:
                user_id = None

    if user_id is not None:
        db_user = await get_user_by_id(session, user_id)
        if db_user is not None:
            if not db_user.is_active:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Inactive user",
                )
            roles |= set(await get_user_role_codes(session, user_id))
        else:
            roles |= token_roles
    else:
        roles |= token_roles

    permissions: frozenset[str] = frozenset()
    has_explicit_actor = bool(authorization or header_roles)
    if not roles and get_settings().environment == "local" and not has_explicit_actor:
        roles.add("finance_manager")
        permissions = ALL_PERMISSION_CODES
    elif roles:
        permissions = frozenset(await get_permission_codes_for_roles(session, roles))

    return CurrentActor(
        roles=frozenset(roles),
        user_id=user_id,
        permissions=permissions,
        permissions_loaded=True,
    )


def require_permission(code: str):
    async def _dep(actor: Annotated[CurrentActor, Depends(get_current_actor)]) -> None:
        ensure_permission(actor, code)

    return _dep


def ensure_permission(actor: CurrentActor, code: str) -> None:
    if permission_is_granted(code, actor.permissions):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Insufficient permission",
    )


def ensure_any_permission(actor: CurrentActor, codes: Iterable[str]) -> None:
    if any(permission_is_granted(code, actor.permissions) for code in codes):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Insufficient permission",
    )


def require_finance_manager_plus(actor: CurrentActor) -> None:
    if actor.roles & FINANCE_MANAGER_PLUS:
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")


def require_manager_plus(actor: CurrentActor) -> None:
    if actor.roles & MANAGER_PLUS:
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")

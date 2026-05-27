from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Header, HTTPException, status

from app.core.config import get_settings
from app.core.security import decode_access_token


@dataclass(frozen=True)
class CurrentActor:
    roles: frozenset[str]


ROLE_HIERARCHY = ("manager", "accountant", "finance_manager", "owner", "admin")
FINANCE_MANAGER_PLUS = frozenset({"finance_manager", "owner", "admin"})


def _split_roles(value: str | None) -> set[str]:
    if not value:
        return set()
    return {part.strip() for part in value.replace(";", ",").split(",") if part.strip()}


async def get_current_actor(
    authorization: Annotated[str | None, Header()] = None,
    x_user_role: Annotated[str | None, Header()] = None,
    x_user_roles: Annotated[str | None, Header()] = None,
) -> CurrentActor:
    roles = _split_roles(x_user_roles) | _split_roles(x_user_role)

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
            roles |= _split_roles(claim_roles)
        else:
            roles |= {str(role) for role in claim_roles}

    if not roles and get_settings().environment == "local":
        roles.add("finance_manager")

    return CurrentActor(roles=frozenset(roles))


def require_finance_manager_plus(actor: CurrentActor) -> None:
    if actor.roles & FINANCE_MANAGER_PLUS:
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")

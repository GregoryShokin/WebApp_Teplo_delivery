from __future__ import annotations

import uuid
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import AuthenticatedUser, build_authenticated_user, get_user_by_id
from app.core.security import decode_access_token
from app.db.session import get_session

CurrentUser = AuthenticatedUser

bearer_scheme = HTTPBearer(auto_error=False)


async def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CurrentUser:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing access token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise unauthorized

    try:
        payload = decode_access_token(credentials.credentials)
        if payload.get("type") != "access":
            raise unauthorized
        user_id = uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError, jwt.InvalidTokenError):
        raise unauthorized from None

    user = await get_user_by_id(session, user_id)
    if user is None:
        raise unauthorized

    authenticated = await build_authenticated_user(session, user)
    if authenticated is None:
        raise unauthorized

    return authenticated


def require_role(*roles: str):
    async def checker(user: Annotated[CurrentUser, Depends(current_user)]) -> CurrentUser:
        # `admin` — суперроль: имеет неявный доступ ко всем эндпоинтам, требующим
        # любую конкретную роль (owner / finance_manager / manager и т.д.).
        allowed = set(roles) | {"admin"}
        if not set(user.roles).intersection(allowed):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient role",
            )
        return user

    return checker

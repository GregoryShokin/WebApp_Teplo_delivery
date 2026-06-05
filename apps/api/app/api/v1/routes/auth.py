from __future__ import annotations

import uuid
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import authenticate_user, build_authenticated_user, get_user_by_id
from app.core.config import get_settings
from app.core.security import create_access_token, create_refresh_token, decode_token
from app.db.session import get_session
from app.schemas.auth import AuthUserRead, LoginRequest, TokenResponse

router = APIRouter()


def _read_user(user) -> AuthUserRead:
    return AuthUserRead(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        roles=list(user.roles),
    )


def _set_refresh_cookie(response: Response, refresh_token: str) -> None:
    settings = get_settings()
    max_age = settings.jwt_refresh_token_expire_days * 24 * 60 * 60
    response.set_cookie(
        key=settings.auth_refresh_cookie_name,
        value=refresh_token,
        max_age=max_age,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/api/v1/auth",
    )


@router.post("/login", response_model=TokenResponse)
async def login(
    credentials: LoginRequest,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TokenResponse:
    user = await authenticate_user(session, credentials.email, credentials.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    claims = {
        "email": user.email,
        "roles": list(user.roles),
        "permissions": list(user.permissions),
    }
    access_token = create_access_token(str(user.id), claims)
    refresh_token = create_refresh_token(str(user.id), {"email": user.email})
    _set_refresh_cookie(response, refresh_token)

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=_read_user(user),
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TokenResponse:
    settings = get_settings()
    refresh_cookie = request.cookies.get(settings.auth_refresh_cookie_name)
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing refresh token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if refresh_cookie is None:
        raise unauthorized

    try:
        payload = decode_token(refresh_cookie)
        if payload.get("type") != "refresh":
            raise unauthorized
        user_id = uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError, jwt.InvalidTokenError):
        raise unauthorized from None

    db_user = await get_user_by_id(session, user_id)
    if db_user is None:
        raise unauthorized

    user = await build_authenticated_user(session, db_user)
    if user is None:
        raise unauthorized

    access_token = create_access_token(
        str(user.id),
        {
            "email": user.email,
            "roles": list(user.roles),
            "permissions": list(user.permissions),
        },
    )
    refresh_token = create_refresh_token(str(user.id), {"email": user.email})
    _set_refresh_cookie(response, refresh_token)
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=_read_user(user),
    )


@router.post("/logout")
async def logout(response: Response) -> dict[str, str]:
    settings = get_settings()
    response.delete_cookie(key=settings.auth_refresh_cookie_name, path="/api/v1/auth")
    return {"status": "ok"}

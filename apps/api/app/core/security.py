from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt

from app.core.config import get_settings


def hash_password(password: str) -> str:
    password_bytes = password.encode("utf-8")
    return bcrypt.hashpw(password_bytes, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    if password_hash.startswith("sha256$"):
        digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return password_hash == f"sha256${digest}"
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def create_token(
    subject: str,
    expires_delta: timedelta,
    token_type: str,
    claims: dict[str, Any] | None = None,
) -> str:
    settings = get_settings()
    expires_at = datetime.now(UTC) + expires_delta
    payload: dict[str, Any] = {"sub": subject, "exp": expires_at, "type": token_type}
    if claims:
        payload.update(claims)
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_access_token(
    subject: str,
    claims: dict[str, Any] | None = None,
    expires_delta: timedelta | None = None,
) -> str:
    settings = get_settings()
    return create_token(
        subject,
        expires_delta or timedelta(minutes=settings.jwt_access_token_expire_minutes),
        "access",
        claims,
    )


def create_refresh_token(
    subject: str,
    claims: dict[str, Any] | None = None,
    expires_delta: timedelta | None = None,
) -> str:
    settings = get_settings()
    return create_token(
        subject,
        expires_delta or timedelta(days=settings.jwt_refresh_token_expire_days),
        "refresh",
        claims,
    )


def decode_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])


def decode_access_token(token: str) -> dict[str, Any]:
    payload = decode_token(token)
    if payload.get("type", "access") != "access":
        raise jwt.InvalidTokenError("Expected access token")
    return payload

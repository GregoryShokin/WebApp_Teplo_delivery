from __future__ import annotations

import uuid

from pydantic import BaseModel


class LoginRequest(BaseModel):
    email: str
    password: str


class AuthUserRead(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    roles: list[str]


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: AuthUserRead

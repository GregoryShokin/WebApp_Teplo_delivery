from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import verify_password
from app.models import Role, User, UserRole


@dataclass(frozen=True)
class AuthenticatedUser:
    id: uuid.UUID
    email: str
    full_name: str
    roles: tuple[str, ...]


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(
        select(User).where(func.lower(User.email) == email.strip().lower())
    )
    return result.scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)


async def get_user_role_codes(session: AsyncSession, user_id: uuid.UUID) -> tuple[str, ...]:
    result = await session.execute(
        select(Role.code)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id)
        .order_by(Role.code)
    )
    return tuple(result.scalars().all())


async def build_authenticated_user(
    session: AsyncSession, user: User
) -> AuthenticatedUser | None:
    if not user.is_active:
        return None

    roles = await get_user_role_codes(session, user.id)
    return AuthenticatedUser(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        roles=roles,
    )


async def authenticate_user(
    session: AsyncSession, email: str, password: str
) -> AuthenticatedUser | None:
    user = await get_user_by_email(session, email)
    if user is None or not verify_password(password, user.hashed_password):
        return None

    return await build_authenticated_user(session, user)

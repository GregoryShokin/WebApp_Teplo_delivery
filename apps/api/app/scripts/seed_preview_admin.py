"""Завести единую учётку администратора для превью-стенда.

Все `docker-compose.*.yml` объявляют одну и ту же связку (`TEPLO_ADMIN_EMAIL` /
`TEPLO_ADMIN_PASSWORD`), но приложение эти переменные само не читает: при поднятии стенда
на дампе прод-БД такого пользователя в базе просто нет, и войти в превью нечем. Скрипт
идемпотентно создаёт его (или чинит пароль существующему) и выдаёт роль администратора.

Пароль здесь заведомо публичный, поэтому на боевом окружении скрипт отказывается работать:
``ENVIRONMENT`` из ``PRODUCTION_ENVIRONMENTS`` — это стоп.

    docker exec -w /app/apps/api <api-контейнер> python -m app.scripts.seed_preview_admin
"""

from __future__ import annotations

import argparse
import asyncio
import os

from sqlalchemy import select

from app.core.config import PRODUCTION_ENVIRONMENTS, get_settings
from app.core.security import hash_password
from app.db.session import AsyncSessionLocal
from app.models import Organization, Role, User, UserRole

DEFAULT_EMAIL = "admin1@teplo.local"
DEFAULT_PASSWORD = "admin-password-for-smoke"
DEFAULT_FULL_NAME = "Администратор превью"
ADMIN_ROLE_CODE = "admin"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed the shared preview admin account.")
    parser.add_argument("--email", default=os.environ.get("TEPLO_ADMIN_EMAIL") or DEFAULT_EMAIL)
    parser.add_argument(
        "--password",
        default=os.environ.get("TEPLO_ADMIN_PASSWORD") or DEFAULT_PASSWORD,
    )
    parser.add_argument("--full-name", dest="full_name", default=DEFAULT_FULL_NAME)
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    environment = get_settings().environment.casefold()
    if environment in PRODUCTION_ENVIRONMENTS:
        raise SystemExit(
            f"seed_preview_admin отказан: ENVIRONMENT={environment}. "
            "Учётка с публичным паролем допустима только на локальных превью."
        )

    async with AsyncSessionLocal() as session:
        role = await session.scalar(select(Role).where(Role.code == ADMIN_ROLE_CODE))
        if role is None:
            raise SystemExit(f"роль {ADMIN_ROLE_CODE!r} не найдена — накати миграции")

        user = await session.scalar(select(User).where(User.email == args.email))
        if user is None:
            user = User(
                email=args.email,
                hashed_password=hash_password(args.password),
                full_name=args.full_name,
                is_active=True,
            )
            session.add(user)
            await session.flush()
            action = "created"
        else:
            user.hashed_password = hash_password(args.password)
            user.is_active = True
            action = "updated"

        existing_role = await session.scalar(
            select(UserRole).where(UserRole.user_id == user.id, UserRole.role_id == role.id)
        )
        if existing_role is None:
            organization_id = await session.scalar(
                select(UserRole.organization_id).where(UserRole.user_id == user.id).limit(1)
            ) or await session.scalar(select(Organization.id).order_by(Organization.name).limit(1))
            if organization_id is None:
                raise SystemExit("организация не найдена — база пустая?")
            session.add(
                UserRole(user_id=user.id, role_id=role.id, organization_id=organization_id)
            )

        await session.commit()

    print(f"preview_admin {action} email={args.email} role={ADMIN_ROLE_CODE}")


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()

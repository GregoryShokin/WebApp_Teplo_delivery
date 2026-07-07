"""Поглощение старых карточек «Внештат №1/№2» пулом плейсхолдеров.

До этой фичи внештатники были двумя вечными сотрудниками «Внештат №1/№2» в Штате,
привязанными к одноимённым iiko-карточкам. Теперь эти строки становятся системными
ПЛЕЙСХОЛДЕРАМИ пула: помечаются ``is_freelancer_placeholder=True`` и исчезают из
штатных списков. История их смен/ведомостей/выплат остаётся привязанной к строке —
прошлые расчёты неприкосновенны. Новые внештатники заводятся отдельными временными
карточками, а явка на «Внештат №N» перенаправляется на привязанного внештатника.

ВАЖНО: запускать ВРУЧНУЮ и только ПОСЛЕ финализации текущей ведомости (07.07) —
пока идёт живой расчёт по старой схеме, карточки трогать нельзя. Сама миграция 0162
со старыми карточками ничего не делает.

Запуск (после деплоя кода + ``alembic upgrade head`` + финализации ведомости):

    # предпросмотр без записи:
    docker exec -w /app/apps/api teplo-api python -m app.scripts.absorb_legacy_freelancers --dry-run
    # применить:
    docker exec -w /app/apps/api teplo-api python -m app.scripts.absorb_legacy_freelancers --commit
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import or_, select

from app.db.session import AsyncSessionLocal
from app.models import Employee
from app.services.freelancer.pool import parse_placeholder_index


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Пометить старые карточки «Внештат №N» плейсхолдерами пула внештатников.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--commit",
        action="store_true",
        help="применить изменения (по умолчанию — предпросмотр без записи)",
    )
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="предпросмотр без записи (поведение по умолчанию)",
    )
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    commit = args.commit and not args.dry_run

    async with AsyncSessionLocal() as session:
        candidates = (
            await session.scalars(
                select(Employee)
                .where(
                    Employee.is_freelancer_placeholder.is_(False),
                    or_(
                        Employee.full_name.ilike("Внештат %"),
                        Employee.full_name.ilike("Внештат№%"),
                    ),
                )
                .order_by(Employee.full_name)
            )
        ).all()

        marked: list[str] = []
        for employee in candidates:
            if parse_placeholder_index(employee.full_name) is None:
                # Имя не в формате «Внештат №N» — пропускаем (не наш плейсхолдер).
                continue
            employee.is_freelancer_placeholder = True
            employee.is_freelancer_temp = False
            marked.append(employee.full_name)

        if commit:
            await session.commit()
        else:
            await session.rollback()

    for name in marked:
        print(f"  плейсхолдер: {name}")
    suffix = "" if commit else " (dry-run, изменения откатаны)"
    print(f"Найдено кандидатов: {len(marked)}, помечено плейсхолдерами: {len(marked)}{suffix}.")
    if not commit:
        print("Запусти с --commit, чтобы применить.")


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()

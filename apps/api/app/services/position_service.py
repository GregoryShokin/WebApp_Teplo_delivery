"""CRUD реестра должностей.

Семантика операций с iiko (сами вызовы iiko делает роут — их мокают тесты):
  - Добавить/Изменить: всегда пушим роль в iiko (PUT byCode);
  - Исключить/Вернуть: только статус в реестре, iiko не трогаем;
  - Удалить: DELETE роли в iiko + удаление из реестра; блокируется, пока есть
    сотрудники с этой должностью.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Employee, EmployeePositionAssignment, Position, PositionChangeEvent, Role
from app.services.iiko_sync import IikoEmployeeRole
from app.services.position_registry import normalize_position_name


class PositionServiceError(ValueError):
    def __init__(self, message: str, *, status_code: int = 422) -> None:
        self.status_code = status_code
        super().__init__(message)


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
    "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch",
    "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _translit_slug(name: str) -> str:
    transliterated = "".join(
        _TRANSLIT.get(char, char) for char in name.replace("ё", "е").casefold()
    )
    return re.sub(r"[^a-z0-9]+", "_", transliterated).strip("_")


def generate_iiko_role_code(name: str, taken_codes: set[str]) -> str:
    """Код роли iiko из названия должности: translit, верхний регистр, до 12 символов."""
    base = (_translit_slug(name).upper())[:12] or "POS"
    code = base
    suffix = 2
    while code in taken_codes:
        code = f"{base[:10]}_{suffix}"
        suffix += 1
    return code


def generate_access_role_code(name: str, taken_codes: set[str]) -> str:
    """Код роли доступа (движка прав) для должности: pos_<slug>, уникальный."""
    base = f"pos_{_translit_slug(name) or 'role'}"[:60]
    code = base
    suffix = 2
    while code in taken_codes:
        code = f"{base[:56]}_{suffix}"
        suffix += 1
    return code


async def ensure_access_role(session: AsyncSession, position: Position) -> Role:
    """Создать/привязать роль-движок прав для должности, участвующей в доступах.

    Если у должности уже есть access_role_code — синхронизируем имя роли.
    Иначе заводим новую роль pos_<slug> с пустым набором прав.
    """
    if position.access_role_code:
        role = await session.scalar(select(Role).where(Role.code == position.access_role_code))
        if role is not None:
            if role.name != position.name:
                role.name = position.name
            return role
    taken = set((await session.scalars(select(Role.code))).all())
    code = generate_access_role_code(position.name, taken)
    role = Role(code=code, name=position.name)
    session.add(role)
    await session.flush()
    position.access_role_code = code
    return role


async def list_positions(session: AsyncSession) -> list[Position]:
    return list((await session.scalars(select(Position).order_by(Position.name))).all())


async def get_position(session: AsyncSession, position_id: uuid.UUID) -> Position:
    position = await session.get(Position, position_id)
    if position is None:
        raise PositionServiceError("Должность не найдена", status_code=404)
    return position


async def employee_counts_by_position(session: AsyncSession) -> dict[str, int]:
    rows = (
        await session.execute(
            select(Employee.position, func.count())
            .where(Employee.status != "inactive")
            .group_by(Employee.position)
        )
    ).all()
    counts: dict[str, int] = {}
    for name, count in rows:
        if name:
            counts[normalize_position_name(name)] = (
                counts.get(normalize_position_name(name), 0) + count
            )
    return counts


async def find_by_name(session: AsyncSession, name: str) -> Position | None:
    normalized = normalize_position_name(name)
    for position in await list_positions(session):
        if normalize_position_name(position.name) == normalized:
            return position
    return None


async def create_position(
    session: AsyncSession,
    *,
    name: str,
    archetype: str,
    permission_group: str,
    schedule_type: str,
    participates_in_access: bool = False,
    access_role_code: str | None = None,
    iiko_role_id: str | None = None,
    iiko_role_code: str | None = None,
    status: str = "active",
    actor_user_id: uuid.UUID | None = None,
) -> Position:
    clean_name = name.strip()
    if not clean_name:
        raise PositionServiceError("Название должности обязательно")
    if await find_by_name(session, clean_name) is not None:
        raise PositionServiceError(
            "Должность с таким названием уже есть в реестре", status_code=409
        )

    position = Position(
        name=clean_name,
        archetype=archetype,
        permission_group=permission_group,
        schedule_type=schedule_type,
        participates_in_access=participates_in_access,
        access_role_code=access_role_code,
        iiko_role_id=iiko_role_id,
        iiko_role_code=iiko_role_code,
        status=status,
    )
    session.add(position)
    await session.flush()
    if participates_in_access:
        await ensure_access_role(session, position)
    _add_change_event(session, position, "create", before=None, actor_user_id=actor_user_id)
    return position


async def update_position(
    session: AsyncSession,
    position: Position,
    *,
    changes: dict[str, Any],
    actor_user_id: uuid.UUID | None = None,
) -> Position:
    before = _position_snapshot(position)
    new_name = changes.get("name")
    if new_name is not None:
        new_name = new_name.strip()
        if not new_name:
            raise PositionServiceError("Название должности обязательно")
        existing = await find_by_name(session, new_name)
        if existing is not None and existing.id != position.id:
            raise PositionServiceError(
                "Должность с таким названием уже есть в реестре", status_code=409
            )
        old_name = position.name
        if new_name != old_name:
            # Должность сотрудника хранится строкой в истории назначений
            # (Employee.position — column_property над ней) — переименование тянем за собой.
            await session.execute(
                update(EmployeePositionAssignment)
                .where(EmployeePositionAssignment.position == old_name)
                .values(position=new_name)
            )
        position.name = new_name

    for field in (
        "archetype",
        "permission_group",
        "participates_in_access",
        "schedule_type",
        "iiko_role_id",
        "iiko_role_code",
    ):
        if field in changes and changes[field] is not None:
            setattr(position, field, changes[field])

    # Включили участие в доступах → завести/привязать роль-движок прав.
    # Переименование должности подтягивает имя привязанной роли.
    if position.participates_in_access:
        await ensure_access_role(session, position)

    await session.flush()
    _add_change_event(session, position, "update", before=before, actor_user_id=actor_user_id)
    return position


async def set_position_status(
    session: AsyncSession,
    position: Position,
    *,
    status: str,
    actor_user_id: uuid.UUID | None = None,
) -> Position:
    if status not in ("active", "excluded"):
        raise PositionServiceError("Недопустимый статус должности")
    if position.status == status:
        return position
    before = _position_snapshot(position)
    position.status = status
    await session.flush()
    _add_change_event(
        session,
        position,
        "exclude" if status == "excluded" else "restore",
        before=before,
        actor_user_id=actor_user_id,
    )
    return position


async def employees_with_position(session: AsyncSession, name: str) -> int:
    count = await session.scalar(
        select(func.count()).select_from(Employee).where(Employee.position == name)
    )
    return int(count or 0)


async def delete_position(
    session: AsyncSession,
    position: Position,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> None:
    occupied = await employees_with_position(session, position.name)
    if occupied:
        raise PositionServiceError(
            f"Нельзя удалить должность «{position.name}»: "
            f"она назначена сотрудникам ({occupied}). Используйте «Исключить».",
            status_code=409,
        )
    before = _position_snapshot(position)
    event = PositionChangeEvent(
        position_id=None,
        position_name=position.name,
        action="delete",
        before_value=before,
        after_value=None,
        actor_user_id=actor_user_id,
    )
    session.add(event)
    await session.delete(position)
    await session.flush()


async def link_iiko_roles(
    session: AsyncSession,
    roles: list[IikoEmployeeRole],
    *,
    actor_user_id: uuid.UUID | None = None,
) -> tuple[int, int]:
    """Первая загрузка/сверка ролей iiko.

    Существующие должности линкуются по названию (заполняем iiko_role_id/code);
    роли iiko, которых нет в реестре, заводятся со status=excluded, чтобы
    не мусорить в выборе (Бармен, Официант и т.п.).
    """
    positions = await list_positions(session)
    by_normalized = {normalize_position_name(p.name): p for p in positions}
    linked = 0
    imported = 0
    for role in roles:
        if role.deleted:
            continue
        position = by_normalized.get(normalize_position_name(role.name))
        if position is not None:
            if position.iiko_role_id != role.id or position.iiko_role_code != role.code:
                before = _position_snapshot(position)
                position.iiko_role_id = role.id
                position.iiko_role_code = role.code
                _add_change_event(
                    session, position, "iiko_link", before=before, actor_user_id=actor_user_id
                )
                linked += 1
            continue
        position = await create_position(
            session,
            name=role.name,
            archetype="none",
            permission_group="none",
            schedule_type="SESSION",
            iiko_role_id=role.id,
            iiko_role_code=role.code,
            status="excluded",
            actor_user_id=actor_user_id,
        )
        by_normalized[normalize_position_name(position.name)] = position
        imported += 1
    await session.flush()
    return linked, imported


def _position_snapshot(position: Position) -> dict[str, Any]:
    return {
        "name": position.name,
        "iiko_role_id": position.iiko_role_id,
        "iiko_role_code": position.iiko_role_code,
        "archetype": position.archetype,
        "permission_group": position.permission_group,
        "participates_in_access": position.participates_in_access,
        "access_role_code": position.access_role_code,
        "status": position.status,
        "schedule_type": position.schedule_type,
    }


def _add_change_event(
    session: AsyncSession,
    position: Position,
    action: str,
    *,
    before: dict[str, Any] | None,
    actor_user_id: uuid.UUID | None,
) -> None:
    session.add(
        PositionChangeEvent(
            position_id=position.id,
            position_name=position.name,
            action=action,
            before_value=before,
            after_value=_position_snapshot(position),
            actor_user_id=actor_user_id,
        )
    )

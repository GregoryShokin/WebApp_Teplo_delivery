"""Ingest смены курьера из вебхука iikoCloud (realtime открытие/закрытие).

Дополняет поллинг ``iiko_attendance_sync``: вебхук отражает открытие/закрытие смены сразу,
а выгрузка ``/employees/attendance`` публикует явку с задержкой (инцидент 2026-06-28).

РЕАЛЬНАЯ структура события iikoCloud ``PersonalShift`` (откалибрована 2026-06-28 по логам
прод-вебхука): ``eventType="PersonalShift"``, ``eventInfo={"id": <employeeId>,
"roleId": <roleId>, "opened": true|false, "terminalGroupId": ...}``. Времени в ``eventInfo``
НЕТ — только флаг ``opened`` (true=открытие, false=закрытие). Поэтому для opened_at/closed_at
берём время ПОЛУЧЕНИЯ (``now``): вебхук realtime, это надёжнее, чем гадать формат/таймзону
``eventTime``; точные времена для KPI всё равно даёт поллинг (источник истины).

Сохраняем ТОЛЬКО смены курьеров (резолв ``Employee`` по ``iiko_id`` + позиция в
``courier_positions``). Логика:
 * открытие — создаём смену, если открытой ещё нет (иначе не плодим);
 * закрытие — закрываем последнюю открытую смену курьера (обновляем СУЩЕСТВУЮЩУЮ запись,
   в т.ч. созданную поллингом → без дублей).
Вебхук-открытие помечается ``raw_payload["_source"]=cloud_webhook`` — защита от prune; позже
поллинг «усыновляет» такую смену, проставляя точное время открытия (см. ``upsert_attendance``).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CourierIikoShift, Employee
from app.services.couriers.iiko_attendance_sync import first_text
from app.services.position_registry import courier_positions, ensure_position_registry_fresh

logger = logging.getLogger(__name__)

CLOUD_WEBHOOK_SOURCE = "cloud_webhook"
# employeeId в PersonalShift лежит в поле "id" (НЕ "employeeId"); запасные ключи — на вариации.
_EMPLOYEE_KEYS = ("id", "employeeId", "employee_id", "userId", "courierId")


async def ingest_cloud_shift_event(
    session: AsyncSession, event: dict[str, Any]
) -> uuid.UUID | None:
    """Применить событие смены вебхука iikoCloud. Возвращает employee_id курьера или None.

    None — событие не курьера (другой сотрудник / не нашли) или закрывать нечего. Не курьера
    молча пропускаем; отсутствие id сотрудника логируем (сигнал к проверке структуры).
    """
    info = event.get("eventInfo") if isinstance(event, dict) else None
    if not isinstance(info, dict):
        return None

    emp_iiko_id = first_text(info, *_EMPLOYEE_KEYS)
    if not emp_iiko_id:
        logger.warning("cloud shift webhook: нет id сотрудника в eventInfo=%s", info)
        return None

    opened = info.get("opened")
    role_id = first_text(info, "roleId", "role_id")
    now = datetime.now(UTC)

    # Только курьеры: событие приходит на всех сотрудников, courier_iiko_shift — про курьеров.
    await ensure_position_registry_fresh(session)
    employee = await session.scalar(select(Employee).where(Employee.iiko_id == emp_iiko_id))
    if employee is None or employee.position not in courier_positions():
        return None

    open_shift = await session.scalar(
        select(CourierIikoShift)
        .where(
            CourierIikoShift.iiko_employee_id == emp_iiko_id,
            CourierIikoShift.closed_at.is_(None),
        )
        .order_by(CourierIikoShift.opened_at.desc())
    )

    if opened is True:
        if open_shift is not None:
            return employee.id  # уже открыта — не плодим дубль
        shift = CourierIikoShift(
            iiko_employee_id=emp_iiko_id,
            opened_at=now,
            employee_id=employee.id,
            iiko_role_id=role_id or "",
            attendance_type="",
            raw_payload={
                **info,
                "_source": CLOUD_WEBHOOK_SOURCE,
                "_eventTime": event.get("eventTime"),
            },
        )
        session.add(shift)
        await session.flush()
        return employee.id

    # opened=False (или отсутствует) — закрытие: закрываем последнюю открытую смену курьера.
    if open_shift is None:
        logger.info(
            "cloud shift webhook: закрытие %s, но открытой смены нет (доберёт поллинг)",
            emp_iiko_id,
        )
        return None
    open_shift.closed_at = now
    open_shift.employee_id = employee.id
    if role_id:
        open_shift.iiko_role_id = role_id
    open_shift.imported_at = now
    payload = dict(open_shift.raw_payload or {})
    payload["_closed_via"] = CLOUD_WEBHOOK_SOURCE
    payload["_closeEventTime"] = event.get("eventTime")
    open_shift.raw_payload = payload
    await session.flush()
    return employee.id

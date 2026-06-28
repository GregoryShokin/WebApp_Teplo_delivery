"""Ingest смены курьера из вебхука iikoCloud (realtime открытие/закрытие).

Дополняет поллинг ``iiko_attendance_sync``: вебхук заводит запись смены сразу при открытии,
тогда как выгрузка iiko ``/employees/attendance`` публикует явку с задержкой (см. инцидент
2026-06-28 — смена открыта в 12:35, в выгрузке появилась к 13:09). Дедуп с поллингом по
UNIQUE ``(iiko_employee_id, opened_at)``: позже тот же поллинг обогащает/закрывает запись.

Сохраняем ТОЛЬКО смены курьеров (резолв ``Employee`` по ``iiko_id`` + позиция в
``courier_positions``); события других сотрудников игнорируем. Парсер полей гибкий — точная
структура ``eventInfo`` PersonalShift калибруется по реальному payload (endpoint логирует
сырое событие), поэтому имена полей берём из набора кандидатов.

Запись помечается ``raw_payload["_source"]="cloud_webhook"`` — ``prune_missing_attendance``
по этому признаку НЕ удаляет свежую открытую вебхук-смену, которой ещё нет в выгрузке iiko.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CourierIikoShift, Employee
from app.services.couriers.iiko_attendance_sync import first_text, parse_iiko_datetime
from app.services.position_registry import courier_positions, ensure_position_registry_fresh

logger = logging.getLogger(__name__)

CLOUD_WEBHOOK_SOURCE = "cloud_webhook"

# Кандидаты имён полей eventInfo (структуру PersonalShift калибруем по реальному payload).
# Без общего "id" — у событий-заказов это id заказа, дало бы ложный матч.
_EMPLOYEE_KEYS = ("employeeId", "employee_id", "userId", "user_id", "courierId")
_OPEN_KEYS = (
    "openDateTime",
    "startDateTime",
    "openDate",
    "startDate",
    "startTime",
    "dateFrom",
    "openedAt",
    "shiftOpenDateTime",
)
_CLOSE_KEYS = (
    "closeDateTime",
    "endDateTime",
    "closeDate",
    "endDate",
    "endTime",
    "dateTo",
    "closedAt",
    "shiftCloseDateTime",
)


async def ingest_cloud_shift_event(
    session: AsyncSession, event: dict[str, Any]
) -> uuid.UUID | None:
    """Сохранить смену курьера из одного события вебхука. Возвращает employee_id или None.

    None — событие не относится к курьеру (другой сотрудник / не нашли) или нет ключевых
    полей (employee/openDateTime). Не курьера молча пропускаем, отсутствие полей логируем
    (сигнал к калибровке парсера).
    """
    info = event.get("eventInfo") if isinstance(event, dict) else None
    if not isinstance(info, dict):
        return None

    emp_iiko_id = first_text(info, *_EMPLOYEE_KEYS)
    open_raw = first_text(info, *_OPEN_KEYS)
    if not emp_iiko_id or not open_raw:
        logger.warning(
            "cloud shift webhook: не найдены employeeId/openDateTime в eventInfo=%s", info
        )
        return None

    try:
        opened_at = parse_iiko_datetime(open_raw)
    except Exception:  # noqa: BLE001 — кривая дата = пропуск, не валим вебхук
        logger.warning("cloud shift webhook: не распарсил дату открытия %r", open_raw)
        return None
    close_raw = first_text(info, *_CLOSE_KEYS)
    closed_at = None
    if close_raw:
        try:
            closed_at = parse_iiko_datetime(close_raw)
        except Exception:  # noqa: BLE001
            closed_at = None

    # Только курьеры: событие приходит на всех сотрудников, courier_iiko_shift — про курьеров.
    await ensure_position_registry_fresh(session)
    employee = await session.scalar(select(Employee).where(Employee.iiko_id == emp_iiko_id))
    if employee is None or employee.position not in courier_positions():
        return None

    existing = await session.scalar(
        select(CourierIikoShift).where(
            CourierIikoShift.iiko_employee_id == emp_iiko_id,
            CourierIikoShift.opened_at == opened_at,
        )
    )
    if existing is None:
        existing = CourierIikoShift(iiko_employee_id=emp_iiko_id, opened_at=opened_at)
        session.add(existing)

    existing.employee_id = employee.id
    # role/attendance_type вебхук может не нести — поллинг их обогатит при синке выгрузки.
    existing.iiko_role_id = existing.iiko_role_id or ""
    existing.attendance_type = existing.attendance_type or ""
    # Закрытие из вебхука применяем; «открытие» не затирает уже проставленное закрытие.
    if closed_at is not None:
        existing.closed_at = closed_at
    existing.imported_at = datetime.now(UTC)
    payload = dict(info)
    payload["_source"] = CLOUD_WEBHOOK_SOURCE
    existing.raw_payload = payload

    await session.flush()
    return employee.id

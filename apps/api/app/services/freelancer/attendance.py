"""Перенаправление явки с плейсхолдера на временного внештатника по дате.

Явка из iiko приходит на карту плейсхолдера «Внештат №N». При построении леджера
и загрузке явок мы подменяем сотрудника на того временного внештатника, чья
карточка (``freelancer_temp_card``) покрывает дату явки — включая архивные
карточки (расчёту прошлых периодов они нужны). Если на дату к плейсхолдеру никто
не привязан — заводим кейс разбора менеджеру, явку не теряем.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Employee, FreelancerAttendanceCase, FreelancerTempCard


@dataclass(frozen=True, slots=True)
class _Binding:
    period_from: date
    period_to: date
    employee_id: uuid.UUID


class BindingIndex:
    """Индекс привязок плейсхолдер → [(период, внештатник)] для быстрого ремапа."""

    def __init__(self, bindings: dict[uuid.UUID, list[_Binding]]) -> None:
        self._bindings = bindings

    @property
    def is_empty(self) -> bool:
        return not self._bindings

    def resolve(self, placeholder_id: uuid.UUID, work_date: date) -> uuid.UUID | None:
        for binding in self._bindings.get(placeholder_id, ()):  # type: ignore[arg-type]
            if binding.period_from <= work_date <= binding.period_to:
                return binding.employee_id
        return None


async def load_binding_index(session: AsyncSession) -> BindingIndex:
    """Грузит ВСЕ карточки (в т.ч. архивные) — архивная в своём периоде всё ещё
    маппит явку. Перекрытий на одном плейсхолдере нет по построению."""
    bindings: dict[uuid.UUID, list[_Binding]] = defaultdict(list)
    rows = await session.scalars(select(FreelancerTempCard))
    for card in rows.all():
        bindings[card.placeholder_employee_id].append(
            _Binding(card.period_from, card.period_to, card.employee_id)
        )
    return BindingIndex(bindings)


async def upsert_attendance_case(
    session: AsyncSession,
    *,
    placeholder_id: uuid.UUID,
    work_date: date,
    minutes: int = 0,
    opened_at: datetime | None = None,
    now: datetime | None = None,
) -> FreelancerAttendanceCase:
    """Идемпотентно по (placeholder, work_date). Разрешённый ранее кейс, по которому
    снова нет привязки, снова открывается."""
    now = now or datetime.now(UTC)
    case = await session.scalar(
        select(FreelancerAttendanceCase).where(
            FreelancerAttendanceCase.placeholder_employee_id == placeholder_id,
            FreelancerAttendanceCase.work_date == work_date,
        )
    )
    if case is None:
        case = FreelancerAttendanceCase(
            placeholder_employee_id=placeholder_id,
            work_date=work_date,
            minutes=minutes,
            opened_at=opened_at,
            status="open",
        )
        session.add(case)
    else:
        case.minutes = minutes
        case.opened_at = opened_at
        if case.status != "dismissed":
            case.status = "open"
            case.resolved_employee_id = None
        case.updated_at = now
    return case


async def resolve_cases_for_card(
    session: AsyncSession,
    card: FreelancerTempCard,
    *,
    now: datetime | None = None,
) -> int:
    """Закрывает открытые кейсы плейсхолдера, попадающие в период карточки."""
    now = now or datetime.now(UTC)
    cases = await session.scalars(
        select(FreelancerAttendanceCase).where(
            FreelancerAttendanceCase.placeholder_employee_id == card.placeholder_employee_id,
            FreelancerAttendanceCase.status == "open",
            FreelancerAttendanceCase.work_date >= card.period_from,
            FreelancerAttendanceCase.work_date <= card.period_to,
        )
    )
    resolved = 0
    for case in cases.all():
        case.status = "resolved"
        case.resolved_employee_id = card.employee_id
        case.updated_at = now
        resolved += 1
    return resolved


async def remap_attendance_employee(
    session: AsyncSession,
    employee: Employee,
    work_date: date,
    *,
    index: BindingIndex,
    minutes: int = 0,
    opened_at: datetime | None = None,
    record_case: bool = True,
) -> Employee | None:
    """Возвращает сотрудника, которому принадлежит явка на ``work_date``.

    - Обычный сотрудник → он сам.
    - Плейсхолдер пула с привязкой на дату → временный внештатник.
    - Плейсхолдер без привязки → ``None`` (явку на плейсхолдер не относим),
      при ``record_case`` заводим/обновляем кейс разбора.
    """
    if not getattr(employee, "is_freelancer_placeholder", False):
        return employee
    resolved_id = index.resolve(employee.id, work_date)
    if resolved_id is not None:
        resolved = await session.get(Employee, resolved_id)
        if resolved is not None:
            return resolved
    if record_case:
        await upsert_attendance_case(
            session,
            placeholder_id=employee.id,
            work_date=work_date,
            minutes=minutes,
            opened_at=opened_at,
        )
    return None

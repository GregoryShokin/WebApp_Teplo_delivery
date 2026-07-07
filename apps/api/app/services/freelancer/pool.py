"""Пул iiko-плейсхолдеров «Внештат №N» и жизненный цикл временных карточек.

Ключевые инварианты:
- Плейсхолдер — постоянная системная строка ``employee`` (``is_freelancer_placeholder``),
  держит реальную iiko-карту. Архивировать нельзя: iiko-синк реактивирует активную
  карту. Скрыт из штатных списков по флагу.
- Временный внештатник — отдельная строка ``employee`` (``is_freelancer_temp``,
  реальное имя, категория freelancer, синтетический iiko_id), привязан к плейсхолдеру
  на период через ``freelancer_temp_card``.
- Перекрытие периодов на одном плейсхолдере невозможно по построению: аллокатор
  берёт первый №N без пересечения периодов.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Employee, FreelancerTempCard
from app.services.employee_status import (
    compute_status,
    position_group_for_position,
)
from app.services.iiko_sync import (
    IikoEmployeeOperationError,
    create_iiko_employee,
    update_iiko_employee,
)

# Максимальная длина карточки внештатника — 30 календарных дней включительно.
MAX_CARD_DAYS = 30

PLACEHOLDER_NAME_PREFIX = "Внештат №"
_PLACEHOLDER_NAME_RE = re.compile(r"^\s*Внештат\s*№\s*(\d+)\s*$", re.IGNORECASE)

# Сообщение отказа при попытке периода длиннее 30 дней.
TOO_LONG_MESSAGE = "Период больше 30 дней — заводите сотрудника в штат, а не внештат."


class FreelancerError(ValueError):
    """Доменная ошибка контура внештатников (маппится в 422 на уровне роутера)."""


class DuplicateActiveNameError(FreelancerError):
    """Среди активных сотрудников уже есть такое имя."""


@dataclass(frozen=True, slots=True)
class PlaceholderAllocation:
    placeholder: Employee
    created_new: bool


def synthetic_freelancer_iiko_id() -> str:
    """Синтетический iiko_id для временного внештатника (своей iiko-карты нет)."""
    return f"freelancer:{uuid.uuid4().hex}"


def placeholder_display_name(index: int) -> str:
    return f"{PLACEHOLDER_NAME_PREFIX}{index}"


def parse_placeholder_index(full_name: str | None) -> int | None:
    if not full_name:
        return None
    match = _PLACEHOLDER_NAME_RE.match(full_name)
    return int(match.group(1)) if match else None


def normalize_name(full_name: str) -> str:
    """Схлопывает пробелы и приводит к нижнему регистру для сравнения имён."""
    return re.sub(r"\s+", " ", full_name or "").strip().casefold()


def validate_period(period_from: date, period_to: date) -> None:
    if period_to < period_from:
        raise FreelancerError("Дата окончания раньше даты начала.")
    # Включительно: 30 дней ⇒ разница ≤ 29.
    if (period_to - period_from).days > MAX_CARD_DAYS - 1:
        raise FreelancerError(TOO_LONG_MESSAGE)


async def ensure_unique_active_name(
    session: AsyncSession,
    full_name: str,
    *,
    exclude_employee_id: uuid.UUID | None = None,
) -> None:
    """Блокирует дубль имени среди АКТИВНЫХ сотрудников (архивные — не в счёт).

    Плейсхолдеры пула («Внештат №N») в сравнении не участвуют.
    """
    normalized = normalize_name(full_name)
    if not normalized:
        return
    query = select(Employee).where(
        Employee.status == "active",
        Employee.is_freelancer_placeholder.is_(False),
        func.lower(func.regexp_replace(Employee.full_name, r"\s+", " ", "g")) == normalized,
    )
    if exclude_employee_id is not None:
        query = query.where(Employee.id != exclude_employee_id)
    existing = await session.scalar(query.limit(1))
    if existing is not None:
        raise DuplicateActiveNameError(
            "Сотрудник с таким именем уже есть. Добавьте отличие, например «Иван Иванов №2»."
        )


async def _cards_on_placeholder(
    session: AsyncSession,
    placeholder_id: uuid.UUID,
) -> list[FreelancerTempCard]:
    result = await session.scalars(
        select(FreelancerTempCard).where(
            FreelancerTempCard.placeholder_employee_id == placeholder_id
        )
    )
    return list(result.all())


def _periods_overlap(a_from: date, a_to: date, b_from: date, b_to: date) -> bool:
    return a_from <= b_to and b_from <= a_to


async def _placeholder_is_free(
    session: AsyncSession,
    placeholder_id: uuid.UUID,
    period_from: date,
    period_to: date,
) -> bool:
    """Плейсхолдер свободен, если ни одна его карточка (в т.ч. архивная) не
    пересекается по периоду с запрошенным окном."""
    for card in await _cards_on_placeholder(session, placeholder_id):
        if _periods_overlap(period_from, period_to, card.period_from, card.period_to):
            return False
    return True


async def _next_placeholder_index(session: AsyncSession) -> int:
    """Следующий свободный №N — max по ВСЕМ карточкам с именем «Внештат №N»
    (не только помеченным флагом), чтобы не столкнуться с чужой картой."""
    names = await session.scalars(
        select(Employee.full_name).where(
            or_(
                Employee.is_freelancer_placeholder.is_(True),
                Employee.full_name.ilike("Внештат %"),
            )
        )
    )
    max_index = 0
    for name in names.all():
        index = parse_placeholder_index(name)
        if index is not None and index > max_index:
            max_index = index
    return max_index + 1


async def allocate_placeholder(
    session: AsyncSession,
    *,
    iiko_position: str,
    iiko_role_id: str,
    period_from: date,
    period_to: date,
    pin_code: str,
    now: datetime | None = None,
) -> PlaceholderAllocation:
    """Находит свободный плейсхолдер под период или создаёт новый «Внештат №N».

    Побочные эффекты в iiko: у нового плейсхолдера ПИН = ``pin_code`` (create);
    у переиспользуемого — ПИН обновляется на ``pin_code`` (update). Вызывать под
    защитой обработки iiko-ошибок роутера.
    """
    now = now or datetime.now(UTC)
    today = now.date()

    placeholders = list(
        (
            await session.scalars(
                select(Employee).where(Employee.is_freelancer_placeholder.is_(True))
            )
        ).all()
    )
    placeholders.sort(key=lambda emp: parse_placeholder_index(emp.full_name) or 10**9)

    for placeholder in placeholders:
        if await _placeholder_is_free(session, placeholder.id, period_from, period_to):
            await update_iiko_employee(
                session,
                iiko_id=placeholder.iiko_id,
                pin_code=pin_code,
            )
            placeholder.updated_at = now
            return PlaceholderAllocation(placeholder=placeholder, created_new=False)

    # Все заняты — новый плейсхолдер «Внештат №(max+1)» в iiko + локальная строка.
    index = await _next_placeholder_index(session)
    display_name = placeholder_display_name(index)
    iiko_employee = await create_iiko_employee(
        session,
        full_name=display_name,
        role_id=iiko_role_id,
        pin_code=pin_code,
    )

    placeholder = await session.scalar(
        select(Employee).where(Employee.iiko_id == iiko_employee.iiko_id)
    )
    if placeholder is None:
        placeholder = Employee(
            full_name=display_name,
            iiko_id=iiko_employee.iiko_id,
            position=iiko_position,
            category=None,
            is_freelancer_placeholder=True,
            hire_date=iiko_employee.hire_date or today,
            tenure_started_at=iiko_employee.hire_date or today,
            pin_assumed_from_iiko=True,
            iiko_sync_at=now,
        )
        session.add(placeholder)
    else:
        placeholder.full_name = display_name
        placeholder.position = iiko_position
        placeholder.is_freelancer_placeholder = True
        placeholder.pin_assumed_from_iiko = True
        placeholder.pin_hash = None
        placeholder.fire_date = None
        placeholder.fire_reason = None
        placeholder.iiko_sync_at = now
    placeholder.status = compute_status(
        placeholder,
        is_iiko_deleted=False,
        position_group=position_group_for_position(placeholder.position),
    )
    await session.flush()
    return PlaceholderAllocation(placeholder=placeholder, created_new=True)


async def create_temp_card(
    session: AsyncSession,
    *,
    employee_id: uuid.UUID,
    placeholder_id: uuid.UUID,
    period_from: date,
    period_to: date,
    created_by: uuid.UUID | None,
    pin_code: str | None = None,
) -> FreelancerTempCard:
    card = FreelancerTempCard(
        employee_id=employee_id,
        placeholder_employee_id=placeholder_id,
        period_from=period_from,
        period_to=period_to,
        created_by=created_by,
        pin_code=pin_code,
    )
    session.add(card)
    await session.flush()
    return card


async def get_card_for_employee(
    session: AsyncSession,
    employee_id: uuid.UUID,
) -> FreelancerTempCard | None:
    return await session.scalar(
        select(FreelancerTempCard).where(FreelancerTempCard.employee_id == employee_id)
    )


async def update_card_period(
    session: AsyncSession,
    card: FreelancerTempCard,
    *,
    period_from: date | None = None,
    period_to: date | None = None,
) -> FreelancerTempCard:
    """Правка периода активной карточки — в пределах 30 дней от НАЧАЛА карточки.

    Начало не двигаем за пределы исходного окна: якорь — ``card.period_from``.
    """
    new_from = period_from if period_from is not None else card.period_from
    new_to = period_to if period_to is not None else card.period_to
    validate_period(new_from, new_to)
    anchor = card.period_from
    if (new_to - anchor).days > MAX_CARD_DAYS - 1:
        raise FreelancerError(TOO_LONG_MESSAGE)
    # Нет пересечения с другими карточками того же плейсхолдера.
    for other in await _cards_on_placeholder(session, card.placeholder_employee_id):
        if other.id == card.id:
            continue
        if _periods_overlap(new_from, new_to, other.period_from, other.period_to):
            raise FreelancerError("Период пересекается с другой карточкой на этом плейсхолдере.")
    card.period_from = new_from
    card.period_to = new_to
    await session.flush()
    return card


async def reset_placeholder_pin(
    session: AsyncSession,
    placeholder: Employee,
) -> None:
    """Сброс ПИН iiko-карты плейсхолдера (пустой pinCode) при освобождении."""
    await update_iiko_employee(
        session,
        iiko_id=placeholder.iiko_id,
        pin_code="",
    )


async def archive_card(
    session: AsyncSession,
    card: FreelancerTempCard,
    *,
    now: datetime | None = None,
    reset_pin: bool = True,
) -> None:
    """Архивирует карточку: помечает archived_at, уводит сотрудника в inactive,
    освобождает плейсхолдер (сброс ПИН, если на нём больше нет активной привязки).

    Физического удаления нет: история смен/ведомостей/выплат остаётся на
    архивной строке сотрудника.
    """
    now = now or datetime.now(UTC)
    if card.archived_at is not None:
        return
    card.archived_at = now

    employee = await session.get(Employee, card.employee_id)
    if employee is not None:
        employee.status = "inactive"
        # fire_date = конец периода: строка уходит из активных списков, статус
        # считается inactive (см. compute_status). Дальнейшие расчёты — по истории.
        employee.fire_date = card.period_to
        employee.updated_at = now

    if reset_pin:
        placeholder = await session.get(Employee, card.placeholder_employee_id)
        if placeholder is not None and not await placeholder_has_active_binding(
            session,
            card.placeholder_employee_id,
            now.date(),
            exclude_card_id=card.id,
        ):
            await reset_placeholder_pin(session, placeholder)


async def archive_expired_cards(
    session: AsyncSession,
    *,
    today: date | None = None,
    now: datetime | None = None,
) -> int:
    """Тело ежедневного джоба: архивирует карточки с истёкшим периодом.

    iiko-ошибка сброса ПИН по одной карточке не валит весь батч — локальная
    архивация выполняется в любом случае.
    """
    import logging

    logger = logging.getLogger("app.services.freelancer.pool")
    now = now or datetime.now(UTC)
    today = today or now.date()
    cards = list(
        (
            await session.scalars(
                select(FreelancerTempCard).where(
                    FreelancerTempCard.archived_at.is_(None),
                    FreelancerTempCard.period_to < today,
                )
            )
        ).all()
    )
    archived = 0
    for card in cards:
        try:
            await archive_card(session, card, now=now, reset_pin=True)
        except (IikoEmployeeOperationError, OSError) as exc:
            # ПИН не сбросился — архивируем без сброса, кейс переживёт следующий тик.
            logger.warning(
                "freelancer archive: сброс ПИН плейсхолдера не удался card=%s: %s",
                card.id,
                exc,
            )
            await archive_card(session, card, now=now, reset_pin=False)
        archived += 1
    return archived


async def placeholder_has_active_binding(
    session: AsyncSession,
    placeholder_id: uuid.UUID,
    on_date: date,
    *,
    exclude_card_id: uuid.UUID | None = None,
) -> bool:
    """Есть ли на плейсхолдере не архивная карточка, покрывающая дату."""
    query = select(FreelancerTempCard).where(
        FreelancerTempCard.placeholder_employee_id == placeholder_id,
        FreelancerTempCard.archived_at.is_(None),
        FreelancerTempCard.period_from <= on_date,
        FreelancerTempCard.period_to >= on_date,
    )
    if exclude_card_id is not None:
        query = query.where(FreelancerTempCard.id != exclude_card_id)
    return (await session.scalar(query.limit(1))) is not None

"""Реестр должностей: кэш-снапшот таблицы `position` + поведенческие выборки.

Должность определяет архетип оплаты, группу прав и участие в выдаче доступов.
Бывшие захардкоженные списки имён должностей (OKLADNIK_POSITIONS,
PAYROLL_TARGET_POSITIONS, VACATION_EMPLOYEE_POSITIONS и т.д.) читаются отсюда.

Допуски (отпуска / график / надбавка старшинства / подмена) больше не хранятся
отдельными флагами — они выводятся из архетипа: production_percent → включены,
иначе выключены (исторически совпадало с {Повар, Кассир}).

Снапшот обновляется при старте приложения, после CRUD реестра и по TTL в
middleware; до первой загрузки из БД действует встроенный канон (идентичный
сиду миграции), поэтому поведение без БД не отличается от прежнего.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Position

ARCHETYPE_OKLADNIK = "okladnik"
ARCHETYPE_PRODUCTION_PERCENT = "production_percent"
ARCHETYPE_SHIFT_POOL = "shift_pool"
ARCHETYPE_COURIER = "courier"
ARCHETYPE_NONE = "none"

STATUS_ACTIVE = "active"
STATUS_EXCLUDED = "excluded"

REGISTRY_TTL_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class PositionInfo:
    name: str
    archetype: str
    permission_group: str
    status: str = STATUS_ACTIVE
    schedule_type: str = "SESSION"
    participates_in_access: bool = False
    access_role_code: str | None = None
    iiko_role_id: str | None = None
    iiko_role_code: str | None = None

    # --- допуски, выводимые из архетипа --- #
    @property
    def gets_vacation(self) -> bool:
        return self.archetype == ARCHETYPE_PRODUCTION_PERCENT

    @property
    def in_schedule(self) -> bool:
        return self.archetype == ARCHETYPE_PRODUCTION_PERCENT

    @property
    def gets_seniority_premium(self) -> bool:
        return self.archetype == ARCHETYPE_PRODUCTION_PERCENT

    @property
    def can_substitute(self) -> bool:
        return self.archetype == ARCHETYPE_PRODUCTION_PERCENT


# Встроенный канон — идентичен сиду миграций 0100/0102. Поведение оплаты/прав
# обязано совпадать с прежними захардкоженными списками.
DEFAULT_POSITIONS: tuple[PositionInfo, ...] = (
    PositionInfo(
        "Кассир", "production_percent", "cashiers",
        participates_in_access=True, access_role_code="cashier",
    ),
    PositionInfo("Повар", "production_percent", "cooks"),
    PositionInfo(
        "Управляющий", "okladnik", "administration", schedule_type="FIXED",
        participates_in_access=True, access_role_code="manager",
    ),
    PositionInfo(
        "Системный администратор", "okladnik", "administration", schedule_type="FIXED",
        participates_in_access=True, access_role_code="admin",
    ),
    PositionInfo("Курьер", "courier", "couriers"),
    PositionInfo(
        "Старший курьер", "courier", "couriers",
        participates_in_access=True, access_role_code="senior_courier",
    ),
    PositionInfo(
        "Менеджер", "okladnik", "administration",
        participates_in_access=True, access_role_code="office_manager",
    ),
    PositionInfo("Уборщица", "okladnik", "auxiliary", schedule_type="FIXED"),
    PositionInfo("Посудомойка", "shift_pool", "auxiliary"),
)


def normalize_position_name(position: str | None) -> str:
    text = (position or "").replace("\xa0", " ").strip()
    text = text.replace("ё", "е").replace("Ё", "Е").casefold()
    text = re.sub(r"\s*[-–—]\s*", "-", text)
    return re.sub(r"\s+", " ", text).strip()


class PositionSnapshot:
    def __init__(self, positions: tuple[PositionInfo, ...]) -> None:
        self.positions = positions
        self.by_normalized = {
            normalize_position_name(position.name): position for position in positions
        }

    def get(self, position: str | None) -> PositionInfo | None:
        return self.by_normalized.get(normalize_position_name(position))

    def canonical_name(self, position: str | None) -> str | None:
        info = self.get(position)
        return info.name if info is not None else None

    def active(self) -> tuple[PositionInfo, ...]:
        return tuple(p for p in self.positions if p.status == STATUS_ACTIVE)

    def names(self, *, active_only: bool = False) -> tuple[str, ...]:
        source = self.active() if active_only else self.positions
        return tuple(p.name for p in source)

    def names_by_archetype(self, *archetypes: str) -> tuple[str, ...]:
        return tuple(p.name for p in self.positions if p.archetype in archetypes)

    def names_by_permission_group(self, group: str) -> tuple[str, ...]:
        return tuple(p.name for p in self.positions if p.permission_group == group)


_DEFAULT_SNAPSHOT = PositionSnapshot(DEFAULT_POSITIONS)
_snapshot: PositionSnapshot = _DEFAULT_SNAPSHOT
_loaded_at: float | None = None


def snapshot() -> PositionSnapshot:
    return _snapshot


def registry_is_stale(max_age_seconds: float = REGISTRY_TTL_SECONDS) -> bool:
    return _loaded_at is None or (time.monotonic() - _loaded_at) > max_age_seconds


def invalidate_position_registry() -> None:
    global _loaded_at
    _loaded_at = None


def reset_position_registry_for_tests() -> None:
    global _snapshot, _loaded_at
    _snapshot = _DEFAULT_SNAPSHOT
    _loaded_at = None


async def refresh_position_registry(session: AsyncSession) -> PositionSnapshot:
    global _snapshot, _loaded_at
    rows = (await session.scalars(select(Position).order_by(Position.name))).all()
    if rows:
        _snapshot = PositionSnapshot(
            tuple(
                PositionInfo(
                    name=row.name,
                    archetype=row.archetype,
                    permission_group=row.permission_group,
                    status=row.status,
                    schedule_type=row.schedule_type,
                    participates_in_access=row.participates_in_access,
                    access_role_code=row.access_role_code,
                    iiko_role_id=row.iiko_role_id,
                    iiko_role_code=row.iiko_role_code,
                )
                for row in rows
            )
        )
    else:
        # Пустая таблица (до сида) — работаем по встроенному канону.
        _snapshot = _DEFAULT_SNAPSHOT
    _loaded_at = time.monotonic()
    return _snapshot


async def ensure_position_registry_fresh(
    session: AsyncSession,
    max_age_seconds: float = REGISTRY_TTL_SECONDS,
) -> PositionSnapshot:
    if registry_is_stale(max_age_seconds):
        return await refresh_position_registry(session)
    return _snapshot


# --------------------------------------------------------------------------- #
# Поведенческие выборки (замена бывших захардкоженных списков)
# --------------------------------------------------------------------------- #
def known_position_names() -> tuple[str, ...]:
    """Все должности реестра (включая исключённые) — для нормализации/отображения."""
    return _snapshot.names()


def active_position_names() -> tuple[str, ...]:
    """Должности, доступные для выбора (status=active)."""
    return _snapshot.names(active_only=True)


def position_info(position: str | None) -> PositionInfo | None:
    return _snapshot.get(position)


def canonical_position_name(position: str | None) -> str | None:
    return _snapshot.canonical_name(position)


def is_known_position(position: str | None) -> bool:
    return _snapshot.get(position) is not None


def is_active_position(position: str | None) -> bool:
    info = _snapshot.get(position)
    return info is not None and info.status == STATUS_ACTIVE


def okladnik_positions() -> tuple[str, ...]:
    """Бывший OKLADNIK_POSITIONS: фиксированный оклад, полумесячная ведомость."""
    return _snapshot.names_by_archetype(ARCHETYPE_OKLADNIK)


def dishwasher_positions() -> tuple[str, ...]:
    """Бывший DISHWASHER_POSITIONS: посменная оплата из месячного пула."""
    return _snapshot.names_by_archetype(ARCHETYPE_SHIFT_POOL)


def production_payroll_positions() -> tuple[str, ...]:
    """Бывший PAYROLL_TARGET_POSITIONS: недельная производственная ЗП (% с выручки)."""
    return _snapshot.names_by_archetype(ARCHETYPE_PRODUCTION_PERCENT)


def admin_payroll_positions() -> tuple[str, ...]:
    """Бывший ADMIN_PAYROLL_POSITIONS: админский каденс ведомости (оклад + посменный пул)."""
    return _snapshot.names_by_archetype(ARCHETYPE_OKLADNIK, ARCHETYPE_SHIFT_POOL)


def courier_positions() -> tuple[str, ...]:
    return _snapshot.names_by_archetype(ARCHETYPE_COURIER)


def vacation_positions() -> tuple[str, ...]:
    """Бывший VACATION_EMPLOYEE_POSITIONS (выводится из архетипа production_percent)."""
    return _snapshot.names_by_archetype(ARCHETYPE_PRODUCTION_PERCENT)


def schedule_positions() -> tuple[str, ...]:
    """Бывший SCHEDULE_EMPLOYEE_POSITIONS (выводится из архетипа production_percent)."""
    return _snapshot.names_by_archetype(ARCHETYPE_PRODUCTION_PERCENT)


def seniority_premium_positions() -> tuple[str, ...]:
    """Бывший VALID_SENIORITY_PREMIUM_POSITIONS (архетип production_percent)."""
    return _snapshot.names_by_archetype(ARCHETYPE_PRODUCTION_PERCENT)


def substitute_target_positions() -> tuple[str, ...]:
    """Бывший SUBSTITUTE_TARGET_POSITIONS — кого можно подменять (архетип production_percent)."""
    return _snapshot.names_by_archetype(ARCHETYPE_PRODUCTION_PERCENT)


def auxiliary_positions() -> tuple[str, ...]:
    """Бывший AUXILIARY_POSITIONS: вспомогательный персонал (без ПИН-кода)."""
    return _snapshot.names_by_permission_group("auxiliary")


def positions_for_permission_group(group: str) -> tuple[str, ...]:
    return _snapshot.names_by_permission_group(group)


def permission_group_for_position(position: str | None) -> str | None:
    info = _snapshot.get(position)
    return info.permission_group if info is not None else None


def access_role_code_for_position(position: str | None) -> str | None:
    """Код роли-движка прав, привязанной к должности (или None, если не участвует)."""
    info = _snapshot.get(position)
    if info is None or not info.participates_in_access:
        return None
    return info.access_role_code


def default_position_info(name: str) -> PositionInfo | None:
    """Канонический сид для должности (используется миграцией и тестами)."""
    for info in DEFAULT_POSITIONS:
        if info.name == name:
            return replace(info)
    return None

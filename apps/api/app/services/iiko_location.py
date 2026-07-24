"""Резолвер iiko-идентификаторов точки из реестра помещений — вместо зашитых констант.

id Черниковой были прописаны в ~9 местах: организация облака (``organizationId``), подразделение
RMS (``departmentId``) и имя департамента в OLAP выручки. Реестр помещений (``Location``) хранит
их как ДАННЫЕ, поэтому код должен спрашивать реестр, а не носить строку в исходниках. Пока
работающая точка одна — резолвер возвращает её id; когда появится вторая, точку выберет контекст
операции (Э3), а места чтения id уже будут ходить сюда.

**Безопасность.** Это критичные денежные контуры (выручка, накладные, выплаты), и они непроверяемы
на стенде (iiko замокан). Поэтому у резолвера есть fallback на прежние зашитые значения: если
реестр ещё не заполнен, поведение прода при единственной точке остаётся ровно прежним — снятие
хардкода не должно ничего сломать.

``revenue_department_name`` — отдельный случай: OLAP выручки фильтрует по НАЗВАНИЮ департамента
(``Foodmarket Тепло Черникова``), которого в реестре нет (там ``location.name`` = «Черникова» и
``iiko_department_id``). Пока отдаём прежнее имя как fallback; связать имя с реестром — отдельный
шаг (хранить OLAP-имя или фильтровать OLAP по ``departmentId``).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Location

# Прежние зашитые значения Черниковой. Резолвер возвращает их ТОЛЬКО как fallback (реестр пуст):
# на действующем проде точка одна и её id в реестре совпадают с этими.
FALLBACK_ORGANIZATION_ID = "5c7e51f9-93c6-450c-9299-440ac1c889e8"
FALLBACK_DEPARTMENT_ID = "d8d4a22e-3abd-4f02-b82d-7d4712f32729"
FALLBACK_REVENUE_DEPARTMENT_NAME = "Foodmarket Тепло Черникова"


async def primary_location(session: AsyncSession) -> Location | None:
    """Основная точка для операций без явного контекста помещения: единственная ДЕЙСТВУЮЩАЯ точка
    с заполненным ``iiko_department_id``. Несколько таких — берём первую по имени (до Э3, где точку
    выбирает контекст операции; до появления второго филиала таких точек одна)."""
    rows = (
        await session.scalars(
            select(Location)
            .where(
                Location.status == "active",
                Location.iiko_department_id.is_not(None),
            )
            .order_by(Location.name)
        )
    ).all()
    return rows[0] if rows else None


async def resolve_organization_id(session: AsyncSession) -> str:
    """``organizationId`` (облако) основной точки; fallback — прежняя константа."""
    location = await primary_location(session)
    if location is not None and location.iiko_organization_id:
        return location.iiko_organization_id
    return FALLBACK_ORGANIZATION_ID


async def resolve_department_id(session: AsyncSession) -> str:
    """``departmentId`` (RMS) основной точки; fallback — прежняя константа."""
    location = await primary_location(session)
    if location is not None and location.iiko_department_id:
        return location.iiko_department_id
    return FALLBACK_DEPARTMENT_ID


# --- Кэш для синхронных мест ------------------------------------------------------------------
# Многие iiko-контуры (выплаты в кассу, накладные) синхронны и вызываются из глубины без session,
# поэтому вместо прокидывания id через десятки слоёв прогреваем значения из реестра ОДИН раз при
# старте приложения и раздаём синхронными геттерами. Пока точка одна — это её id; при изменении
# реестра значения подхватятся после рестарта. Кэш пуст → геттер отдаёт прежнюю константу, так что
# и без прогрева поведение прода остаётся прежним.
_cache: dict[str, str] = {}


async def warm_iiko_location_cache(session: AsyncSession) -> None:
    """Прогреть кэш id основной точки из реестра. Зовётся при старте; ошибку не поднимает."""
    location = await primary_location(session)
    if location is None:
        return
    if location.iiko_organization_id:
        _cache["organization_id"] = location.iiko_organization_id
    if location.iiko_department_id:
        _cache["department_id"] = location.iiko_department_id


def get_organization_id() -> str:
    """``organizationId`` основной точки для синхронных мест (кэш → fallback-константа)."""
    return _cache.get("organization_id", FALLBACK_ORGANIZATION_ID)


def get_department_id() -> str:
    """``departmentId`` основной точки для синхронных мест (кэш → fallback-константа)."""
    return _cache.get("department_id", FALLBACK_DEPARTMENT_ID)


def get_revenue_department_name() -> str:
    """Имя департамента для OLAP выручки. Реестр OLAP-имя пока не хранит — отдаём fallback; связать
    с реестром (хранить имя или фильтровать по ``departmentId``) — отдельный шаг."""
    return _cache.get("revenue_department_name", FALLBACK_REVENUE_DEPARTMENT_NAME)

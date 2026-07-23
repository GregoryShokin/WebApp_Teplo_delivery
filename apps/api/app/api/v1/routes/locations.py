"""Реестр помещений: «Настройки → Помещения».

Помещение отвечает на вопрос «где» и держит связь с iiko. Идентификаторов три, и они из
разных подсистем iiko: организация облачного API (накладные и платежи поставщикам),
подразделение RMS (выручка, кассовые смены, выплаты) и склады (остатки, инвентаризации).
Помещение без идентификаторов — нормальный случай: арендованный склад или офис в iiko не
заведены, но аренду и расходы на них учитывать нужно.

Удаления нет: помещение с историей расходов удалять нельзя, закрытая точка переводится в
статус ``inactive`` с датой закрытия.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.db.session import get_session
from app.models import Location, Organization

router = APIRouter()
LOCATIONS_READ_ACCESS = (Depends(require_permission("source.locations.read")),)
LOCATIONS_EDIT_ACCESS = (Depends(require_permission("source.locations.edit")),)

LocationKind = Literal["point", "warehouse", "office"]
LocationStatus = Literal["active", "inactive"]


class LocationRead(BaseModel):
    id: uuid.UUID
    name: str
    kind: LocationKind
    status: LocationStatus
    address: str | None
    iiko_organization_id: str | None
    iiko_department_id: str | None
    iiko_store_ids: list[str]
    opened_on: date | None
    closed_on: date | None
    note: str | None
    # Подключение к iiko неполное, если задана только часть идентификаторов: выручка придёт,
    # а накладные — нет. Считаем на бэкенде, чтобы список и карточка судили одинаково.
    iiko_linked: bool


class LocationListRead(BaseModel):
    items: list[LocationRead]


class LocationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=160)
    kind: LocationKind = "point"
    address: str | None = Field(default=None, max_length=512)
    iiko_organization_id: str | None = Field(default=None, max_length=64)
    iiko_department_id: str | None = Field(default=None, max_length=64)
    iiko_store_ids: list[str] = Field(default_factory=list)
    opened_on: date | None = None
    note: str | None = None

    @field_validator("iiko_organization_id", "iiko_department_id", "note", mode="after")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed or None

    @field_validator("iiko_store_ids", mode="after")
    @classmethod
    def _clean_stores(cls, value: list[str]) -> list[str]:
        seen: list[str] = []
        for item in value:
            trimmed = item.strip()
            if trimmed and trimmed not in seen:
                seen.append(trimmed)
        return seen


class LocationUpdateRequest(LocationCreateRequest):
    status: LocationStatus | None = None
    closed_on: date | None = None


def _payload(location: Location) -> LocationRead:
    return LocationRead(
        id=location.id,
        name=location.name,
        kind=location.kind,  # type: ignore[arg-type]
        status=location.status,  # type: ignore[arg-type]
        address=location.address,
        iiko_organization_id=location.iiko_organization_id,
        iiko_department_id=location.iiko_department_id,
        iiko_store_ids=list(location.iiko_store_ids or []),
        opened_on=location.opened_on,
        closed_on=location.closed_on,
        note=location.note,
        iiko_linked=bool(location.iiko_organization_id and location.iiko_department_id),
    )


async def _location_or_404(session: AsyncSession, location_id: uuid.UUID) -> Location:
    location = await session.get(Location, location_id)
    if location is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Помещение не найдено")
    return location


async def _ensure_name_free(
    session: AsyncSession,
    name: str,
    *,
    exclude_id: uuid.UUID | None = None,
) -> None:
    query = select(Location.id).where(Location.name == name)
    if exclude_id is not None:
        query = query.where(Location.id != exclude_id)
    if await session.scalar(query) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Помещение с таким названием уже есть",
        )


@router.get("", response_model=LocationListRead, dependencies=LOCATIONS_READ_ACCESS)
async def list_locations(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LocationListRead:
    locations = (
        await session.scalars(select(Location).order_by(Location.status, Location.name))
    ).all()
    return LocationListRead(items=[_payload(item) for item in locations])


@router.get("/{location_id}", response_model=LocationRead, dependencies=LOCATIONS_READ_ACCESS)
async def get_location(
    location_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LocationRead:
    return _payload(await _location_or_404(session, location_id))


@router.post(
    "",
    response_model=LocationRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=LOCATIONS_EDIT_ACCESS,
)
async def create_location(
    payload: LocationCreateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LocationRead:
    name = payload.name.strip()
    await _ensure_name_free(session, name)
    organization_id = await session.scalar(select(Organization.id).order_by(Organization.name))
    if organization_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Организация не найдена — заведите её до помещений",
        )
    location = Location(
        organization_id=organization_id,
        name=name,
        kind=payload.kind,
        address=payload.address,
        status="active",
        iiko_organization_id=payload.iiko_organization_id,
        iiko_department_id=payload.iiko_department_id,
        iiko_store_ids=payload.iiko_store_ids,
        opened_on=payload.opened_on,
        note=payload.note,
    )
    session.add(location)
    await session.commit()
    await session.refresh(location)
    return _payload(location)


@router.patch("/{location_id}", response_model=LocationRead, dependencies=LOCATIONS_EDIT_ACCESS)
async def update_location(
    location_id: uuid.UUID,
    payload: LocationUpdateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LocationRead:
    location = await _location_or_404(session, location_id)
    name = payload.name.strip()
    await _ensure_name_free(session, name, exclude_id=location.id)

    location.name = name
    location.kind = payload.kind
    location.address = payload.address
    location.iiko_organization_id = payload.iiko_organization_id
    location.iiko_department_id = payload.iiko_department_id
    location.iiko_store_ids = payload.iiko_store_ids
    location.opened_on = payload.opened_on
    location.note = payload.note
    if payload.status is not None:
        location.status = payload.status
    # Дату закрытия держим согласованной со статусом: у работающей точки её быть не должно,
    # иначе в отчётах появится «закрыто, но работает».
    location.closed_on = payload.closed_on if location.status == "inactive" else None

    await session.commit()
    await session.refresh(location)
    return _payload(location)

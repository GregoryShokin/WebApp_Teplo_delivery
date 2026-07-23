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
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.db.session import get_session
from app.models import Counterparty, CounterpartyRole, Location, LocationLease, Organization
from app.services import counterparty_registry as registry

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


# --- аренда -------------------------------------------------------------------
#
# Арендодатель — контрагент с ролью landlord: от него по факту нужно только название, а
# реквизиты добавляются по желанию в обычной карточке контрагента. Роль проставляется
# автоматически при первой же аренде, чтобы владельцу не приходилось помнить про справочник.


class LeaseRead(BaseModel):
    id: uuid.UUID
    location_id: uuid.UUID
    location_name: str
    counterparty_id: uuid.UUID
    counterparty_name: str
    monthly_amount: float
    payment_day: int | None
    payment_mode: Literal["prepaid", "postpaid"]
    documents_mode: Literal["official", "informal"]
    deposit_amount: float
    started_on: date
    ended_on: date | None
    note: str | None
    is_active: bool


class LeaseListRead(BaseModel):
    items: list[LeaseRead]


class LeaseWriteRequest(BaseModel):
    """Арендодатель заводится прямо здесь: в списке контрагентов его обычно ещё нет.

    От собственника по факту нужно только название, ИНН — по желанию. Существующего
    контрагента переиспользуем (ищем по ИНН, затем по названию), иначе плодились бы дубли,
    когда один и тот же собственник сдаёт и точку, и склад. ``counterparty_id`` остаётся для
    случая, когда арендодатель уже есть в системе и его выбирают явно.
    """

    model_config = ConfigDict(extra="forbid")

    counterparty_id: uuid.UUID | None = None
    landlord_name: str | None = Field(default=None, max_length=255)
    landlord_inn: str | None = Field(default=None, max_length=12)
    monthly_amount: Decimal = Field(ge=0)
    payment_day: int | None = Field(default=None, ge=1, le=31)
    payment_mode: Literal["prepaid", "postpaid"] = "prepaid"
    documents_mode: Literal["official", "informal"] = "informal"
    deposit_amount: Decimal = Field(default=Decimal("0"), ge=0)
    started_on: date
    ended_on: date | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _landlord_is_set(self) -> LeaseWriteRequest:
        if self.counterparty_id is None and not (self.landlord_name or "").strip():
            raise ValueError("Укажите название арендодателя")
        return self


class LeaseCloseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ended_on: date


def _lease_payload(lease: LocationLease, location_name: str, counterparty_name: str) -> LeaseRead:
    return LeaseRead(
        id=lease.id,
        location_id=lease.location_id,
        location_name=location_name,
        counterparty_id=lease.counterparty_id,
        counterparty_name=counterparty_name,
        monthly_amount=float(lease.monthly_amount),
        payment_day=lease.payment_day,
        payment_mode=lease.payment_mode,  # type: ignore[arg-type]
        documents_mode=lease.documents_mode,  # type: ignore[arg-type]
        deposit_amount=float(lease.deposit_amount),
        started_on=lease.started_on,
        ended_on=lease.ended_on,
        note=lease.note,
        is_active=lease.ended_on is None,
    )


async def _leases_with_names(
    session: AsyncSession,
    *,
    location_id: uuid.UUID | None = None,
    counterparty_id: uuid.UUID | None = None,
) -> list[LeaseRead]:
    query = (
        select(LocationLease, Location.name, Counterparty.name)
        .join(Location, Location.id == LocationLease.location_id)
        .join(Counterparty, Counterparty.id == LocationLease.counterparty_id)
        .order_by(LocationLease.ended_on.is_not(None), LocationLease.started_on.desc())
    )
    if location_id is not None:
        query = query.where(LocationLease.location_id == location_id)
    if counterparty_id is not None:
        query = query.where(LocationLease.counterparty_id == counterparty_id)
    rows = (await session.execute(query)).all()
    return [_lease_payload(lease, location_name, cp_name) for lease, location_name, cp_name in rows]


async def _counterparty_or_404(session: AsyncSession, counterparty_id: uuid.UUID) -> Counterparty:
    counterparty = await session.get(Counterparty, counterparty_id)
    if counterparty is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Контрагент не найден")
    return counterparty


async def _resolve_landlord(
    session: AsyncSession,
    payload: LeaseWriteRequest,
) -> Counterparty:
    """Найти арендодателя или завести его по названию из формы аренды.

    Порядок поиска — ИНН, затем название без учёта регистра: собственник, сдающий и точку, и
    склад, должен остаться одной карточкой, иначе долг и залог разъедутся по дублям.
    """
    if payload.counterparty_id is not None:
        counterparty = await _counterparty_or_404(session, payload.counterparty_id)
        await _ensure_landlord_role(session, counterparty.id)
        return counterparty

    name = (payload.landlord_name or "").strip()
    inn = (payload.landlord_inn or "").strip() or None

    if inn:
        found = await session.scalar(select(Counterparty).where(Counterparty.inn == inn))
        if found is not None:
            await _ensure_landlord_role(session, found.id)
            return found

    found = await session.scalar(
        select(Counterparty).where(func.lower(Counterparty.name) == name.lower())
    )
    if found is not None:
        await _ensure_landlord_role(session, found.id)
        return found

    try:
        counterparty = await registry.create_counterparty(
            session,
            name=name,
            inn=inn,
            # Физлицо — обычный случай для аренды: собственники часто сдают как частные лица.
            cp_type="individual" if not inn or len(inn) == 12 else "legal_entity",
            # Отношения берём из порядка документов аренды: без УПД это informal-канал,
            # и требовать банковские реквизиты в этот момент не нужно.
            relationship="official" if payload.documents_mode == "official" else "informal",
            confirm_no_dds_article=True,
            role="landlord",
        )
    except registry.CounterpartyRegistryError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    return counterparty


async def _ensure_landlord_role(session: AsyncSession, counterparty_id: uuid.UUID) -> None:
    existing = await session.scalar(
        select(CounterpartyRole).where(
            CounterpartyRole.counterparty_id == counterparty_id,
            CounterpartyRole.role == "landlord",
        )
    )
    if existing is None:
        session.add(CounterpartyRole(counterparty_id=counterparty_id, role="landlord"))


async def _lease_or_404(session: AsyncSession, lease_id: uuid.UUID) -> LocationLease:
    lease = await session.get(LocationLease, lease_id)
    if lease is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Аренда не найдена")
    return lease


@router.get(
    "/{location_id}/leases",
    response_model=LeaseListRead,
    dependencies=LOCATIONS_READ_ACCESS,
)
async def list_location_leases(
    location_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LeaseListRead:
    await _location_or_404(session, location_id)
    return LeaseListRead(items=await _leases_with_names(session, location_id=location_id))


@router.post(
    "/{location_id}/leases",
    response_model=LeaseRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=LOCATIONS_EDIT_ACCESS,
)
async def create_location_lease(
    location_id: uuid.UUID,
    payload: LeaseWriteRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LeaseRead:
    location = await _location_or_404(session, location_id)
    if payload.ended_on is not None and payload.ended_on < payload.started_on:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Дата окончания раньше начала аренды",
        )
    counterparty = await _resolve_landlord(session, payload)

    lease = LocationLease(
        location_id=location.id,
        counterparty_id=counterparty.id,
        monthly_amount=payload.monthly_amount,
        payment_day=payload.payment_day,
        payment_mode=payload.payment_mode,
        documents_mode=payload.documents_mode,
        deposit_amount=payload.deposit_amount,
        started_on=payload.started_on,
        ended_on=payload.ended_on,
        note=(payload.note or "").strip() or None,
    )
    session.add(lease)
    await session.commit()
    await session.refresh(lease)
    return _lease_payload(lease, location.name, counterparty.name)


@router.patch(
    "/{location_id}/leases/{lease_id}",
    response_model=LeaseRead,
    dependencies=LOCATIONS_EDIT_ACCESS,
)
async def update_location_lease(
    location_id: uuid.UUID,
    lease_id: uuid.UUID,
    payload: LeaseWriteRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LeaseRead:
    location = await _location_or_404(session, location_id)
    lease = await _lease_or_404(session, lease_id)
    if lease.location_id != location.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Аренда не относится к этому помещению"
        )
    if payload.ended_on is not None and payload.ended_on < payload.started_on:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Дата окончания раньше начала аренды",
        )
    counterparty = await _resolve_landlord(session, payload)

    lease.counterparty_id = counterparty.id
    lease.monthly_amount = payload.monthly_amount
    lease.payment_day = payload.payment_day
    lease.payment_mode = payload.payment_mode
    lease.documents_mode = payload.documents_mode
    lease.deposit_amount = payload.deposit_amount
    lease.started_on = payload.started_on
    lease.ended_on = payload.ended_on
    lease.note = (payload.note or "").strip() or None
    await session.commit()
    await session.refresh(lease)
    return _lease_payload(lease, location.name, counterparty.name)


@router.post(
    "/{location_id}/leases/{lease_id}/close",
    response_model=LeaseRead,
    dependencies=LOCATIONS_EDIT_ACCESS,
)
async def close_location_lease(
    location_id: uuid.UUID,
    lease_id: uuid.UUID,
    payload: LeaseCloseRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LeaseRead:
    """Закрыть аренду — так оформляется смена арендодателя.

    Прошлые месяцы остаются за прежним собственником: новая аренда заводится отдельной
    строкой, а не правкой этой.
    """
    location = await _location_or_404(session, location_id)
    lease = await _lease_or_404(session, lease_id)
    if lease.location_id != location.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Аренда не относится к этому помещению"
        )
    if payload.ended_on < lease.started_on:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Дата окончания раньше начала аренды",
        )
    lease.ended_on = payload.ended_on
    await session.commit()
    await session.refresh(lease)
    counterparty = await _counterparty_or_404(session, lease.counterparty_id)
    return _lease_payload(lease, location.name, counterparty.name)


@router.get(
    "/leases/by-counterparty/{counterparty_id}",
    response_model=LeaseListRead,
    dependencies=LOCATIONS_READ_ACCESS,
)
async def list_counterparty_leases(
    counterparty_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LeaseListRead:
    """Что сдаёт этот контрагент — блок «Аренда» в его карточке."""
    return LeaseListRead(items=await _leases_with_names(session, counterparty_id=counterparty_id))

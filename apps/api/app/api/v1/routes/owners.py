"""Реестр собственников: кто владеет бизнесом, какой долей и кому он должен.

СОБСТВЕННИК — ОБЫЧНЫЙ КОНТРАГЕНТ С ЗАПИСЬЮ В РЕЕСТРЕ. Личность и расчёты живут в карточке
контрагента: он вносит деньги, получает возвраты, ему начисляют дивиденды — механика ровно та
же, что у любого контрагента, и второй истории долга нам не нужно. Доля живёт в
``business_owner``: у поставщика её не бывает, и колонка в общей таблице стояла бы пустой у всех,
кроме двоих.

ЗАЧЕМ РЕЕСТР ОТДЕЛЬНЫМ ЭКРАНОМ. Собственники не ищутся в списке поставщиков, а три статьи —
«поступление от собственников», «возврат» и «дивиденды» — требуют назвать, чьё движение
(``owner_required``). Пока реестр пуст, эти статьи провести нельзя вовсе, и это правильный отказ:
деньги каждого учитываются отдельно.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.db.session import get_session
from app.models import BusinessOwner, Counterparty
from app.services import clock, owner_analytics
from app.services import counterparty_registry as registry

router = APIRouter()

READ = (Depends(require_permission("counterparties.read")),)
OPERATE = (Depends(require_permission("counterparties.operate")),)


class OwnerRead(BaseModel):
    id: uuid.UUID
    counterparty_id: uuid.UUID
    name: str
    inn: str | None
    share_percent: Decimal
    started_on: date
    ended_on: date | None


class OwnerListRead(BaseModel):
    items: list[OwnerRead]
    # Сумма долей. Отдаём считанной сервером, чтобы экран не собирал её по-своему: 100 % —
    # признак того, что реестр полон, и увидеть это человек должен одним числом.
    shares_total: Decimal


class OwnerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    inn: str | None = Field(default=None, max_length=12)
    # Собственник — человек; юрлицо оставляем возможным на случай, когда доля оформлена на
    # компанию, но по умолчанию физлицо.
    type: Literal["individual", "legal_entity"] = "individual"
    share_percent: Decimal = Field(gt=0, le=100)
    started_on: date | None = None
    # Уже заведённый контрагент: человек мог попасть в базу раньше — арендодателем или
    # подрядчиком. Заводить его вторым лицом значило бы расколоть расчёты надвое.
    counterparty_id: uuid.UUID | None = None


class OwnerUpdate(BaseModel):
    share_percent: Decimal | None = Field(default=None, gt=0, le=100)
    # Дата выхода из состава. Запись остаётся: прошлые начисления никуда не деваются.
    ended_on: date | None = None


def _to_read(row: owner_analytics.OwnerRow) -> OwnerRead:
    return OwnerRead(
        id=row.registration.id,
        counterparty_id=row.counterparty.id,
        name=row.counterparty.name,
        inn=row.counterparty.inn,
        share_percent=row.share_percent,
        started_on=row.registration.started_on,
        ended_on=row.registration.ended_on,
    )


async def _listing(session: AsyncSession, *, include_former: bool = False) -> OwnerListRead:
    rows = await owner_analytics.list_owners(session, include_former=include_former)
    active = [row for row in rows if row.registration.ended_on is None]
    return OwnerListRead(
        items=[_to_read(row) for row in rows],
        shares_total=owner_analytics.shares_total(active),
    )


@router.get("", response_model=OwnerListRead, dependencies=READ)
async def list_owners(
    session: Annotated[AsyncSession, Depends(get_session)],
    include_former: bool = False,
) -> OwnerListRead:
    return await _listing(session, include_former=include_former)


@router.post(
    "", response_model=OwnerListRead, status_code=status.HTTP_201_CREATED, dependencies=OPERATE
)
async def create_owner(
    payload: OwnerCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> OwnerListRead:
    """Завести собственника: карточку контрагента (если её ещё нет) и запись в реестре."""
    counterparty_id = payload.counterparty_id
    if counterparty_id is not None:
        counterparty = await session.get(Counterparty, counterparty_id)
        if counterparty is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Контрагент не найден"
            )
    else:
        try:
            counterparty = await registry.create_counterparty(
                session,
                name=payload.name,
                inn=payload.inn,
                cp_type=payload.type,
                # Расчёты с собственником неофициальные: договора и первички между ним и
                # бизнесом нет, деньги ходят переводами на карту. Как у арендодателя-физлица.
                relationship="informal",
                role=owner_analytics.OWNER_ROLE,
                # Статьи по умолчанию у собственника нет и быть не может: движений по нему три
                # (взнос, возврат, дивиденды), и какое происходит — решает человек при платеже.
                confirm_no_dds_article=True,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        counterparty_id = counterparty.id

    if await owner_analytics.is_owner(session, counterparty_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"«{counterparty.name}» уже в реестре собственников — правьте его долю",
        )

    session.add(
        BusinessOwner(
            counterparty_id=counterparty_id,
            share_percent=payload.share_percent,
            started_on=payload.started_on or clock.moscow_today(),
        )
    )
    await owner_analytics.ensure_role_marker(session, counterparty_id)
    await session.commit()
    return await _listing(session)


@router.patch("/{owner_id}", response_model=OwnerListRead, dependencies=OPERATE)
async def update_owner(
    owner_id: uuid.UUID,
    payload: OwnerUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> OwnerListRead:
    """Изменить долю или закрыть участие.

    Запись не удаляется никогда: по ней считались прошлые начисления, и без неё они повисли бы
    без объяснения, откуда взялись.
    """
    row = await session.get(BusinessOwner, owner_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Запись реестра не найдена"
        )
    if payload.share_percent is not None:
        row.share_percent = payload.share_percent
    if payload.ended_on is not None:
        if payload.ended_on < row.started_on:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Дата выхода раньше даты входа",
            )
        row.ended_on = payload.ended_on
    await session.commit()
    return await _listing(session, include_former=True)

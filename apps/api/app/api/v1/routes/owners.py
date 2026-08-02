"""Реестр собственников: кто вкладывает в бизнес и кому он должен.

СОБСТВЕННИК — ОБЫЧНЫЙ КОНТРАГЕНТ С РОЛЬЮ ``owner``, и своей таблицы у него нет намеренно.
Ему нужны ровно те же расчёты, что и любому контрагенту: он вносит деньги, получает возвраты,
ему начисляют дивиденды. Заведи второй реестр — и у одного человека появятся две несовместимые
истории долга, а собрать их в одну строку баланса будет нечем.

ЗАЧЕМ РЕЕСТР ОТДЕЛЬНЫМ ЭКРАНОМ, если это те же контрагенты. Затем же, зачем помещения отдельно
от подрядчиков: собственников двое, они не ищутся в списке поставщиков, а статьи «поступление
от собственников», «возврат» и «дивиденды» требуют назвать, чьё движение (``owner_required``).
Пока в реестре пусто, эти три статьи провести нельзя вовсе — и это правильный отказ: деньги
каждого собственника учитываются отдельно.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.db.session import get_session
from app.models import Counterparty, CounterpartyRole
from app.services import counterparty_registry as registry
from app.services import owner_analytics

router = APIRouter()

READ = (Depends(require_permission("counterparties.read")),)
OPERATE = (Depends(require_permission("counterparties.operate")),)


class OwnerRead(BaseModel):
    id: uuid.UUID
    name: str
    inn: str | None
    type: str
    status: str


class OwnerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    inn: str | None = Field(default=None, max_length=12)
    # Собственник — человек; юрлицо оставляем возможным на случай, когда доля оформлена на
    # компанию, но по умолчанию физлицо.
    type: Literal["individual", "legal_entity"] = "individual"


def _to_read(counterparty: Counterparty) -> OwnerRead:
    return OwnerRead(
        id=counterparty.id,
        name=counterparty.name,
        inn=counterparty.inn,
        type=counterparty.type,
        status=counterparty.status,
    )


@router.get("", response_model=list[OwnerRead], dependencies=READ)
async def list_owners(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[OwnerRead]:
    return [_to_read(item) for item in await owner_analytics.list_owners(session)]


@router.post(
    "", response_model=OwnerRead, status_code=status.HTTP_201_CREATED, dependencies=OPERATE
)
async def create_owner(
    payload: OwnerCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> OwnerRead:
    """Завести собственника. Он же — карточка контрагента со всеми расчётами."""
    try:
        counterparty = await registry.create_counterparty(
            session,
            name=payload.name,
            inn=payload.inn,
            cp_type=payload.type,
            # Расчёты с собственником неофициальные: договора и первички между ним и бизнесом
            # нет, деньги ходят переводами на карту. Тот же режим, что у арендодателя-физлица.
            relationship="informal",
            role=owner_analytics.OWNER_ROLE,
            # Статьи по умолчанию у собственника нет и быть не может: движений по нему три
            # (взнос, возврат, дивиденды), и какое из них происходит — решает человек в момент
            # платежа. Подтверждаем отсутствие явно, а не оставляем реестр требовать выбор.
            confirm_no_dds_article=True,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    await session.commit()
    return _to_read(counterparty)


@router.post("/{counterparty_id}", response_model=OwnerRead, dependencies=OPERATE)
async def mark_as_owner(
    counterparty_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> OwnerRead:
    """Отметить собственником уже заведённого контрагента.

    Нужен, потому что человек мог попасть в базу раньше и другим путём — например, как
    арендодатель или подрядчик. Заводить его вторым лицом значило бы расколоть расчёты надвое.
    """
    counterparty = await session.get(Counterparty, counterparty_id)
    if counterparty is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Контрагент не найден")
    if not await owner_analytics.is_owner(session, counterparty_id):
        session.add(
            CounterpartyRole(counterparty_id=counterparty_id, role=owner_analytics.OWNER_ROLE)
        )
        await session.commit()
    return _to_read(counterparty)

"""Коммунальные потоки помещения и календарь ожиданий.

ЧТО ТАКОЕ ПОТОК. Пара «помещение × ресурс» (вода / газ / электричество) с ответами на вопросы,
которых в самой квитанции нет: КОМУ платим и на КАКУЮ статью относить расход. Оба ответа
неочевидны и различаются в пределах одной точки — по решению владельца от 02.08.2026 вода и газ
возмещаются одному арендодателю, электричество другому. Из потока же берётся помещение: без
него расход осядет «без помещения» и прибыль точки посчитается без коммуналки.

ПРИЁМКИ ЗДЕСЬ БОЛЬШЕ НЕТ. Платёжка приходит на «Страницу на оплату» третьим источником рядом с
почтой и ЭДО — там же, где живут все остальные основания платежей. Отдельный экран для неё был
ошибкой: документ становился видимым только тому, кто знает про специальную страницу, а
заплатить по нему из очереди оплат было нечем. Поток остался настройкой, приёмка ушла в общий
контур.

ПРАВА берём существующие — ``accounting.suppliers.read/edit``. Коммуналка это взаиморасчёты с
контрагентом (арендодателем), и заводить под неё отдельную пару прав значило бы получить право,
которое никому не выдано: у контура доступов единица выдачи — должность, а не экран.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.db.session import get_session
from app.models import (
    UTILITY_KIND_LABELS,
    UTILITY_KINDS,
    Counterparty,
    DdsArticle,
    Location,
    UtilityAccount,
)
from app.services import utility_charges

router = APIRouter()

READ_ACCESS = (Depends(require_permission("accounting.suppliers.read")),)
EDIT_ACCESS = (Depends(require_permission("accounting.suppliers.edit")),)


class AccountWrite(BaseModel):
    location_id: uuid.UUID
    kind: Literal["water", "gas", "electricity"]
    counterparty_id: uuid.UUID
    dds_article_id: uuid.UUID
    expected_day: int | None = Field(default=None, ge=1, le=28)
    started_on: date
    ended_on: date | None = None
    is_active: bool = True
    note: str | None = None


class AccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    location_id: uuid.UUID
    location_name: str
    kind: str
    kind_label: str
    counterparty_id: uuid.UUID
    counterparty_name: str
    dds_article_id: uuid.UUID
    dds_article_name: str
    expected_day: int | None
    started_on: date
    ended_on: date | None
    is_active: bool
    note: str | None


class AccountListRead(BaseModel):
    items: list[AccountRead]


class CalendarRow(BaseModel):
    account_id: uuid.UUID
    account_title: str
    location_name: str
    kind: str
    kind_label: str
    month: date
    state: str
    invoice_id: uuid.UUID | None
    amount: Decimal | None


class CalendarRead(BaseModel):
    items: list[CalendarRow]


def _account_title(kind: str, location_name: str) -> str:
    return f"{UTILITY_KIND_LABELS.get(kind, kind)} · {location_name}"


async def _account_or_404(session: AsyncSession, account_id: uuid.UUID) -> UtilityAccount:
    account = await session.get(UtilityAccount, account_id)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Коммунальный поток не найден"
        )
    return account


async def _assert_article_fits(session: AsyncSession, article_id: uuid.UUID) -> DdsArticle:
    """Статья должна быть расходной и привязанной к помещению — и точно не арендной.

    Арендная статья здесь запрещена зеркально запрету коммунальной в договоре аренды: два
    потока одного арендодателя обязаны различаться статьёй, иначе гарды «месяц уже закрыт»
    принимают один за другой, и один из расходов пропадает.
    """
    article = await session.get(DdsArticle, article_id)
    if article is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Статья ДДС не найдена"
        )
    if article.movement_type != "outflow":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Статья должна быть расходной",
        )
    if article.lease_bound:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Статья «{article.name}» отведена под аренду. Для коммунальных расходов нужна "
                "своя статья — иначе аренда и коммуналка перекрывают друг друга"
            ),
        )
    return article


async def _accounts_with_names(
    session: AsyncSession, *, only_active: bool = False
) -> list[AccountRead]:
    query = (
        select(UtilityAccount, Location.name, Counterparty.name, DdsArticle.name)
        .join(Location, Location.id == UtilityAccount.location_id)
        .join(Counterparty, Counterparty.id == UtilityAccount.counterparty_id)
        .join(DdsArticle, DdsArticle.id == UtilityAccount.dds_article_id)
        .order_by(Location.name, UtilityAccount.kind)
    )
    if only_active:
        query = query.where(UtilityAccount.is_active.is_(True))
    rows = (await session.execute(query)).all()
    return [
        AccountRead(
            id=account.id,
            location_id=account.location_id,
            location_name=location_name,
            kind=account.kind,
            kind_label=UTILITY_KIND_LABELS.get(account.kind, account.kind),
            counterparty_id=account.counterparty_id,
            counterparty_name=counterparty_name,
            dds_article_id=account.dds_article_id,
            dds_article_name=article_name,
            expected_day=account.expected_day,
            started_on=account.started_on,
            ended_on=account.ended_on,
            is_active=account.is_active,
            note=account.note,
        )
        for account, location_name, counterparty_name, article_name in rows
    ]


@router.get("/accounts", response_model=AccountListRead, dependencies=READ_ACCESS)
async def list_accounts(
    session: Annotated[AsyncSession, Depends(get_session)],
    only_active: bool = False,
) -> AccountListRead:
    return AccountListRead(items=await _accounts_with_names(session, only_active=only_active))


@router.post(
    "/accounts",
    response_model=AccountRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=EDIT_ACCESS,
)
async def create_account(
    payload: AccountWrite,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AccountRead:
    if payload.ended_on is not None and payload.ended_on < payload.started_on:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Дата окончания раньше начала",
        )
    if await session.get(Location, payload.location_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Помещение не найдено")
    if await session.get(Counterparty, payload.counterparty_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Контрагент не найден")
    await _assert_article_fits(session, payload.dds_article_id)

    duplicate = await session.scalar(
        select(UtilityAccount.id).where(
            UtilityAccount.location_id == payload.location_id,
            UtilityAccount.kind == payload.kind,
        )
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"По этому помещению поток «{UTILITY_KIND_LABELS[payload.kind]}» уже заведён — "
                "правьте его, а не создавайте второй"
            ),
        )

    account = UtilityAccount(**payload.model_dump())
    session.add(account)
    await session.commit()
    items = await _accounts_with_names(session)
    return next(item for item in items if item.id == account.id)


@router.patch("/accounts/{account_id}", response_model=AccountRead, dependencies=EDIT_ACCESS)
async def update_account(
    account_id: uuid.UUID,
    payload: AccountWrite,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AccountRead:
    account = await _account_or_404(session, account_id)
    if payload.ended_on is not None and payload.ended_on < payload.started_on:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Дата окончания раньше начала",
        )
    # Те же проверки ссылок, что при создании: PATCH — полная замена, и без них правка могла
    # перевести поток на несуществующее помещение или контрагента.
    if await session.get(Location, payload.location_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Помещение не найдено")
    if await session.get(Counterparty, payload.counterparty_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Контрагент не найден")
    await _assert_article_fits(session, payload.dds_article_id)
    if (payload.location_id, payload.kind) != (account.location_id, account.kind):
        duplicate = await session.scalar(
            select(UtilityAccount.id).where(
                UtilityAccount.location_id == payload.location_id,
                UtilityAccount.kind == payload.kind,
                UtilityAccount.id != account.id,
            )
        )
        if duplicate is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Такой поток по этому помещению уже есть",
            )

    for field, value in payload.model_dump().items():
        setattr(account, field, value)
    await session.commit()
    items = await _accounts_with_names(session)
    return next(item for item in items if item.id == account.id)


@router.get("/calendar", response_model=CalendarRead, dependencies=READ_ACCESS)
async def utilities_calendar(
    session: Annotated[AsyncSession, Depends(get_session)],
    months_back: int = 6,
) -> CalendarRead:
    """Месяц за месяцем по каждому потоку — в том числе месяцы без документа.

    Это единственное место, где видно ПРОПАЖУ. Остальные витрины показывают то, что в системе
    есть; месяц, за который платёжку не принесли, следа не оставляет нигде.
    """
    months_back = max(1, min(months_back, 24))
    accounts = (
        await session.scalars(select(UtilityAccount).where(UtilityAccount.is_active.is_(True)))
    ).all()
    names = {row.id: row.location_name for row in await _accounts_with_names(session)}
    items: list[CalendarRow] = []
    for account in accounts:
        location_name = names.get(account.id, "")
        periods = await utility_charges.expected_periods(
            session, account=account, months_back=months_back
        )
        for row in periods:
            items.append(
                CalendarRow(
                    account_id=account.id,
                    account_title=_account_title(account.kind, location_name),
                    location_name=location_name,
                    kind=account.kind,
                    kind_label=UTILITY_KIND_LABELS.get(account.kind, account.kind),
                    month=row["month"],
                    state=row["state"],
                    invoice_id=row["invoice_id"],
                    amount=row["amount"],
                )
            )
    items.sort(key=lambda r: (r.month, r.account_title), reverse=True)
    return CalendarRead(items=items)


@router.get("/kinds", dependencies=READ_ACCESS)
async def utility_kinds() -> dict[str, list[dict[str, str]]]:
    return {
        "items": [{"value": kind, "label": UTILITY_KIND_LABELS[kind]} for kind in UTILITY_KINDS]
    }

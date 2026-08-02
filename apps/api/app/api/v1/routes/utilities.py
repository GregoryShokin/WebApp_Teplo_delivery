"""Коммунальные услуги: приёмка платёжек и календарь ожиданий.

ПРАВА берём существующие — ``accounting.suppliers.read/edit``. Коммуналка это взаиморасчёты с
контрагентом (арендодателем), и заводить под неё отдельную пару прав значило бы получить право,
которое никому не выдано: у контура доступов единица выдачи — должность, а не экран.

ПОЧЕМУ ЗАГРУЗКА ФАЙЛА ЖИВЁТ ЗДЕСЬ, А НЕ В ОБЩЕМ «ПРИЁМЕ ДОКУМЕНТОВ». Приложение до этого файлов
не принимало вовсе: единственным входом была почта. Общий канал загрузки — отдельная задача с
своими вопросами (кто, куда, какие типы, как показывать). Коммунальной платёжке нужен вход
сегодня, и он намеренно узкий: одна форма, один список, понятный набор полей.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, get_current_actor, require_permission
from app.db.session import get_session
from app.models import (
    UTILITY_KIND_LABELS,
    UTILITY_KINDS,
    Counterparty,
    DdsArticle,
    Location,
    UtilityAccount,
    UtilityIntake,
)
from app.services import utility_charges
from app.services.utility_charges import UtilityChargeError

router = APIRouter()

READ_ACCESS = (Depends(require_permission("accounting.suppliers.read")),)
EDIT_ACCESS = (Depends(require_permission("accounting.suppliers.edit")),)

# Больше 25 МБ фотография квитанции не бывает даже с современного телефона, а лежит файл в
# строке таблицы — раздувать её нечем.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
ALLOWED_MIME_PREFIXES = ("image/", "application/pdf")


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


class IntakeRead(BaseModel):
    id: uuid.UUID
    status: str
    account_id: uuid.UUID | None
    account_title: str | None
    filename: str | None
    mime: str | None
    has_document: bool
    period_start: date | None
    period_end: date | None
    amount: Decimal | None
    document_number: str | None
    document_date: date | None
    recognition: dict | None
    invoice_id: uuid.UUID | None
    note: str | None
    created_at: date


class IntakeListRead(BaseModel):
    items: list[IntakeRead]


class IntakePatch(BaseModel):
    account_id: uuid.UUID | None = None
    period_start: date | None = None
    period_end: date | None = None
    amount: Decimal | None = Field(default=None, gt=0)
    document_number: str | None = None
    document_date: date | None = None
    note: str | None = None
    status: Literal["new", "needs_review", "ready", "rejected"] | None = None


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


async def _intake_or_404(session: AsyncSession, intake_id: uuid.UUID) -> UtilityIntake:
    intake = await session.get(UtilityIntake, intake_id)
    if intake is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Платёжка не найдена")
    return intake


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
        await session.scalars(
            select(UtilityAccount).where(UtilityAccount.is_active.is_(True))
        )
    ).all()
    names = {
        row.id: row.location_name
        for row in await _accounts_with_names(session)
    }
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


async def _intake_payload(session: AsyncSession, intake: UtilityIntake) -> IntakeRead:
    title = None
    if intake.account_id is not None:
        account = await session.get(UtilityAccount, intake.account_id)
        if account is not None:
            location = await session.get(Location, account.location_id)
            title = _account_title(account.kind, location.name if location else "")
    return IntakeRead(
        id=intake.id,
        status=intake.status,
        account_id=intake.account_id,
        account_title=title,
        filename=intake.filename,
        mime=intake.mime,
        has_document=intake.content is not None,
        period_start=intake.period_start,
        period_end=intake.period_end,
        amount=intake.amount,
        document_number=intake.document_number,
        document_date=intake.document_date,
        recognition=intake.recognition,
        invoice_id=intake.invoice_id,
        note=intake.note,
        created_at=intake.created_at.date(),
    )


@router.get("/intakes", response_model=IntakeListRead, dependencies=READ_ACCESS)
async def list_intakes(
    session: Annotated[AsyncSession, Depends(get_session)],
    status_filter: str | None = None,
) -> IntakeListRead:
    query = select(UtilityIntake).order_by(UtilityIntake.created_at.desc()).limit(200)
    if status_filter:
        query = query.where(UtilityIntake.status == status_filter)
    intakes = (await session.scalars(query)).all()
    return IntakeListRead(items=[await _intake_payload(session, i) for i in intakes])


@router.post(
    "/intakes",
    response_model=IntakeRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=EDIT_ACCESS,
)
async def create_intake(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    file: Annotated[UploadFile | None, File()] = None,
    account_id: Annotated[uuid.UUID | None, Form()] = None,
    period_start: Annotated[date | None, Form()] = None,
    period_end: Annotated[date | None, Form()] = None,
    amount: Annotated[Decimal | None, Form()] = None,
    document_number: Annotated[str | None, Form()] = None,
    document_date: Annotated[date | None, Form()] = None,
    note: Annotated[str | None, Form()] = None,
) -> IntakeRead:
    """Принять платёжку: файлом или руками.

    Без файла — законный случай, а не обход: по газу документа не приносят вовсе, сумму
    называет арендодатель. Такая строка отличима от подтверждённой бумагой (``has_document``),
    и в этом вся разница — учёт она двигает одинаково.
    """
    content: bytes | None = None
    sha: str | None = None
    if file is not None:
        content = await file.read()
        if not content:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Файл пустой"
            )
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Файл больше {MAX_UPLOAD_BYTES // 1024 // 1024} МБ",
            )
        mime = file.content_type or ""
        if not mime.startswith(ALLOWED_MIME_PREFIXES):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Принимаем фотографию или PDF",
            )
        sha = hashlib.sha256(content).hexdigest()
        existing = await session.scalar(
            select(UtilityIntake).where(UtilityIntake.attachment_sha256 == sha)
        )
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Этот файл уже загружен — найдите его в списке платёжек",
            )

    if account_id is not None:
        await _account_or_404(session, account_id)

    intake = UtilityIntake(
        account_id=account_id,
        # Пока распознавания нет, любая строка идёт человеку: сумму и период он видит на фото
        # и вводит сам. Появится разбор — сюда встанет его вердикт.
        status="needs_review",
        filename=file.filename if file is not None else None,
        mime=file.content_type if file is not None else None,
        attachment_sha256=sha,
        attachment_size=len(content) if content is not None else None,
        content=content,
        period_start=period_start,
        period_end=period_end,
        amount=amount,
        document_number=(document_number or "").strip() or None,
        document_date=document_date,
        note=(note or "").strip() or None,
        uploaded_by_user_id=actor.user_id,
    )
    session.add(intake)
    await session.commit()
    await session.refresh(intake)
    return await _intake_payload(session, intake)


@router.get("/intakes/{intake_id}/file", dependencies=READ_ACCESS)
async def download_intake_file(
    intake_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    intake = await _intake_or_404(session, intake_id)
    if intake.content is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Файла нет")
    raw = intake.filename or "document"
    # HTTP-заголовки только latin-1: русское имя файла обязано ехать вторым, RFC 5987-полем,
    # иначе Starlette роняет ответ и браузер показывает «Network Error».
    ascii_name = raw.encode("ascii", "ignore").decode() or "document"
    disposition = f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(raw)}"
    return Response(
        content=bytes(intake.content),
        media_type=intake.mime or "application/octet-stream",
        headers={"Content-Disposition": disposition},
    )


@router.patch("/intakes/{intake_id}", response_model=IntakeRead, dependencies=EDIT_ACCESS)
async def update_intake(
    intake_id: uuid.UUID,
    payload: IntakePatch,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> IntakeRead:
    intake = await _intake_or_404(session, intake_id)
    if intake.status == "promoted":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Платёжка проведена — сначала отзовите долг, потом правьте",
        )
    data = payload.model_dump(exclude_unset=True)
    if "account_id" in data and data["account_id"] is not None:
        await _account_or_404(session, data["account_id"])
    for field, value in data.items():
        setattr(intake, field, value)
    await session.commit()
    await session.refresh(intake)
    return await _intake_payload(session, intake)


@router.post("/intakes/{intake_id}/promote", response_model=IntakeRead, dependencies=EDIT_ACCESS)
async def promote_intake(
    intake_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> IntakeRead:
    """Провести: создать долг перед арендодателем и признать расход месяца потребления."""
    intake = await _intake_or_404(session, intake_id)
    try:
        await utility_charges.promote_intake(session, intake, actor_user_id=actor.user_id)
    except UtilityChargeError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    await session.commit()
    await session.refresh(intake)
    return await _intake_payload(session, intake)


@router.post("/intakes/{intake_id}/revoke", response_model=IntakeRead, dependencies=EDIT_ACCESS)
async def revoke_intake(
    intake_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> IntakeRead:
    """Отозвать проведённый долг — когда в сумме ошиблись, а месяц надо провести заново."""
    intake = await _intake_or_404(session, intake_id)
    try:
        await utility_charges.revoke_intake(session, intake, actor_user_id=actor.user_id)
    except UtilityChargeError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    await session.commit()
    await session.refresh(intake)
    return await _intake_payload(session, intake)


@router.get("/kinds", dependencies=READ_ACCESS)
async def utility_kinds() -> dict[str, list[dict[str, str]]]:
    return {
        "items": [
            {"value": kind, "label": UTILITY_KIND_LABELS[kind]} for kind in UTILITY_KINDS
        ]
    }

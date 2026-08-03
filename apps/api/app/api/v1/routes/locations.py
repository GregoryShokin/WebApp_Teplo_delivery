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
from app.models import (
    Counterparty,
    CounterpartyPayableProfile,
    CounterpartyRole,
    DdsArticle,
    Location,
    LocationLease,
    Organization,
    SupplierInvoice,
    SupplierPrepayment,
)
from app.services import counterparty_matching, iiko_directory, lease_accruals
from app.services import counterparty_registry as registry
from app.services.location_analytics import leases_for_location

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


class IikoDirectoryItemRead(BaseModel):
    id: str
    name: str


class IikoDirectoryRead(BaseModel):
    # 'live' — данные из iiko; 'mock' — демо для стенда без iiko; 'unavailable' — iiko не ответил
    # (на проде), фронт покажет ручной ввод.
    source: Literal["live", "mock", "unavailable"]
    organizations: list[IikoDirectoryItemRead]
    departments: list[IikoDirectoryItemRead]
    stores: list[IikoDirectoryItemRead]


@router.get("/iiko-directory", response_model=IikoDirectoryRead, dependencies=LOCATIONS_READ_ACCESS)
async def get_iiko_directory_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> IikoDirectoryRead:
    """Организации/подразделения/склады из iiko для выбора привязки помещения — вместо ручного
    ввода UUID. Маршрут стоит ДО ``/{location_id}``, иначе слаг перехватится как id."""
    directory = await iiko_directory.get_iiko_directory(session)
    return IikoDirectoryRead(
        source=directory.source,  # type: ignore[arg-type]
        organizations=[IikoDirectoryItemRead(**item) for item in directory.organizations],
        departments=[IikoDirectoryItemRead(**item) for item in directory.departments],
        stores=[IikoDirectoryItemRead(**item) for item in directory.stores],
    )


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
    dds_article_id: uuid.UUID | None
    is_active: bool


class LeaseListRead(BaseModel):
    items: list[LeaseRead]


class LeaseTermsRequest(BaseModel):
    """Условия аренды без арендодателя.

    Правка условий и смена собственника — разные события. Здесь меняется только то, о чём
    договорились с ТЕМ ЖЕ арендодателем: сумма, день платежа, порядок документов, залог.
    Смена фамилии, ИНН или реквизитов — это правка карточки контрагента, а не смена
    арендодателя: помещение продолжает сдавать то же лицо.
    """

    model_config = ConfigDict(extra="forbid")

    monthly_amount: Decimal = Field(ge=0)
    payment_day: int | None = Field(default=None, ge=1, le=31)
    payment_mode: Literal["prepaid", "postpaid"] = "prepaid"
    documents_mode: Literal["official", "informal"] = "informal"
    deposit_amount: Decimal = Field(default=Decimal("0"), ge=0)
    started_on: date
    ended_on: date | None = None
    note: str | None = None
    # Статья ДДС, по которой платят эту аренду: по ней платёж и найдёт этот договор.
    dds_article_id: uuid.UUID | None = None


class LandlordInput(BaseModel):
    """Данные арендодателя. Для официальной аренды нужны реквизиты — иначе платить нечем."""

    model_config = ConfigDict(extra="forbid")

    counterparty_id: uuid.UUID | None = None
    name: str | None = Field(default=None, max_length=255)
    inn: str | None = Field(default=None, max_length=12)
    bank_bik: str | None = Field(default=None, max_length=16)
    bank_account: str | None = Field(default=None, max_length=32)
    corr_account: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def _identified(self) -> LandlordInput:
        if self.counterparty_id is None and not (self.name or "").strip():
            raise ValueError("Укажите название арендодателя")
        return self


class LeaseWriteRequest(LeaseTermsRequest):
    """Первая аренда помещения: арендодатель заводится здесь же.

    В списке контрагентов его обычно ещё нет — там поставщики. Существующего
    переиспользуем (ищем по ИНН, затем по названию), иначе собственник, сдающий и точку,
    и склад, разъехался бы на два дубля вместе с долгом и залогом.
    """

    landlord: LandlordInput


class LeaseReplaceLandlordRequest(BaseModel):
    """Смена собственника — явное действие, а не правка строки.

    Прежняя аренда закрывается своей датой, новая заводится отдельной строкой: только так
    платежи за прошлые месяцы остаются за прежним владельцем. Прежний арендодатель уходит
    в архив, если больше ничего не сдаёт.
    """

    model_config = ConfigDict(extra="forbid")

    landlord: LandlordInput
    terms: LeaseTermsRequest
    previous_ended_on: date
    archive_previous: bool = True


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
        dds_article_id=lease.dds_article_id,
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


def _landlord_requisites(landlord: LandlordInput, documents_mode: str) -> dict[str, str]:
    """Реквизиты официального арендодателя: без них платёж по УПД не отправить.

    Набор тот же, что у официального поставщика (``OFFICIAL_SUPPLIER_REQUIRED_REQUISITES``),
    поэтому проверяем его здесь явно: реестр требует реквизиты только с ролью ``supplier``,
    а у арендодателя роль своя.
    """
    if documents_mode != "official":
        return {}

    requisites = {
        "inn": (landlord.inn or "").strip(),
        "bankBik": (landlord.bank_bik or "").strip(),
        "bankAcnt": (landlord.bank_account or "").strip(),
        "recipientCorrAccountNumber": (landlord.corr_account or "").strip(),
    }
    missing = [
        label
        for key, label in registry.OFFICIAL_SUPPLIER_REQUIRED_REQUISITES.items()
        if not requisites.get(key)
    ]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Для официальной аренды заполните реквизиты арендодателя: " + ", ".join(missing),
        )
    return requisites


async def _resolve_landlord(
    session: AsyncSession,
    landlord: LandlordInput,
    documents_mode: str,
) -> Counterparty:
    """Найти арендодателя или завести его по данным из формы аренды.

    Порядок поиска — ИНН, затем название без учёта регистра: собственник, сдающий и точку, и
    склад, должен остаться одной карточкой, иначе долг и залог разъедутся по дублям.
    """
    requisites = _landlord_requisites(landlord, documents_mode)

    if landlord.counterparty_id is not None:
        counterparty = await _counterparty_or_404(session, landlord.counterparty_id)
        await _ensure_landlord_role(session, counterparty.id)
        return counterparty

    name = (landlord.name or "").strip()
    inn = (landlord.inn or "").strip() or None

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
            # и банковских реквизитов там не бывает.
            relationship="official" if documents_mode == "official" else "informal",
            requisites=requisites or None,
            confirm_no_dds_article=True,
            role="landlord",
        )
    except registry.CounterpartyRegistryError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return counterparty


async def _archive_landlord_if_idle(session: AsyncSession, counterparty_id: uuid.UUID) -> bool:
    """Убрать прежнего собственника из активных, если он больше ничего не сдаёт.

    Проверяем именно оставшиеся аренды: тот же человек может сдавать вторую точку, и тогда
    архивировать его нельзя — он останется в подсказках и отчётах как действующий.
    """
    still_renting = await session.scalar(
        select(func.count())
        .select_from(LocationLease)
        .where(
            LocationLease.counterparty_id == counterparty_id,
            LocationLease.ended_on.is_(None),
        )
    )
    if still_renting:
        return False
    counterparty = await session.get(Counterparty, counterparty_id)
    if counterparty is None or counterparty.status == registry.ARCHIVED_STATUS:
        return False
    counterparty.status = registry.ARCHIVED_STATUS
    return True


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


async def _assert_lease_article(session: AsyncSession, article_id: uuid.UUID | None) -> None:
    """Статья договора аренды, если она выбрана, должна быть именно арендной (``lease_bound``).

    Помещению принадлежат ещё коммуналка и охрана — они тоже ``location_required``, и раньше
    список в диалоге предлагал их наравне с арендой. Выбор коммунальной статьи в договоре стоит
    дорого: арендное начисление встаёт под неё, а гард ``_month_closed_by_landlord`` считает
    месяц коммуналки закрытым чужим документом — коммунальный расход месяца молча пропадает.
    Проверка серверная, чтобы фильтр в интерфейсе не был единственной защитой.

    Пустую статью здесь НЕ отклоняем, хотя соблазн есть: ``ensure_lease_invoice`` без неё
    возвращает ``None``, то есть договор заведён, а начисления по нему не будет никогда и
    никакой ошибки при этом никто не увидит. Но запрет пустой статьи — смена контракта
    (в схеме поле объявлено необязательным) и он заодно закрывает правку уже существующих
    договоров без статьи. Это решение владельца, а не побочный эффект коммунальной ветки;
    поведение зафиксировано тестом ``test_lease_without_article_never_accrues``.
    """
    if article_id is None:
        return
    article = await session.get(DdsArticle, article_id)
    if article is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Статья ДДС не найдена"
        )
    if not article.lease_bound:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Статья «{article.name}» не арендная. В договоре аренды можно выбрать только "
                "статью аренды — коммунальные расходы учитываются отдельно от неё"
            ),
        )


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
    await _assert_lease_article(session, payload.dds_article_id)
    counterparty = await _resolve_landlord(session, payload.landlord, payload.documents_mode)

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
        dds_article_id=payload.dds_article_id,
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
    payload: LeaseTermsRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LeaseRead:
    """Правка условий с ТЕМ ЖЕ арендодателем.

    Собственника здесь сменить нельзя — для этого есть ``/replace-landlord``. Иначе правка
    суммы задним числом переписала бы прошлые месяцы на другое лицо, а фамилия и ИНН
    правятся в карточке контрагента: помещение по-прежнему сдаёт тот же человек.
    """
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
    await _assert_lease_article(session, payload.dds_article_id)
    counterparty = await _counterparty_or_404(session, lease.counterparty_id)

    lease.monthly_amount = payload.monthly_amount
    lease.payment_day = payload.payment_day
    lease.payment_mode = payload.payment_mode
    lease.documents_mode = payload.documents_mode
    lease.deposit_amount = payload.deposit_amount
    lease.started_on = payload.started_on
    lease.ended_on = payload.ended_on
    lease.note = (payload.note or "").strip() or None
    lease.dds_article_id = payload.dds_article_id
    # Смена ставки/условий доходит до ДЗ/КЗ: пересобираем открытое обязательство текущего месяца
    # (прошлые — в силе, их не трогаем). Новое здесь не заводим — начисление стартует джобой или
    # кнопкой пересбора, а правка договора не должна начислять там, где начисления ещё не было.
    await lease_accruals.rebuild_lease_invoice(
        session, lease, date.today(), create_if_missing=False
    )
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


class LandlordReplacementRead(BaseModel):
    previous: LeaseRead
    current: LeaseRead
    previous_archived: bool


@router.post(
    "/{location_id}/leases/{lease_id}/replace-landlord",
    response_model=LandlordReplacementRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=LOCATIONS_EDIT_ACCESS,
)
async def replace_lease_landlord(
    location_id: uuid.UUID,
    lease_id: uuid.UUID,
    payload: LeaseReplaceLandlordRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LandlordReplacementRead:
    """Сменить собственника: закрыть прежнюю аренду и завести новую отдельной строкой.

    Это единственный способ поменять арендодателя. Правка имени в карточке контрагента
    сменой НЕ считается — там человек меняет фамилию или ИНН, оставаясь тем же лицом, и
    платежи прошлых месяцев должны остаться при нём.
    """
    location = await _location_or_404(session, location_id)
    previous = await _lease_or_404(session, lease_id)
    if previous.location_id != location.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Аренда не относится к этому помещению"
        )
    if previous.ended_on is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Эта аренда уже закрыта — заведите нового арендодателя новой строкой",
        )
    if payload.previous_ended_on < previous.started_on:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Прежняя аренда не может закончиться раньше своего начала",
        )
    if payload.terms.started_on <= payload.previous_ended_on:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Новая аренда должна начинаться после последнего дня прежней",
        )

    await _assert_lease_article(session, payload.terms.dds_article_id)
    new_landlord = await _resolve_landlord(session, payload.landlord, payload.terms.documents_mode)
    if new_landlord.id == previous.counterparty_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Это тот же арендодатель — правьте условия аренды или его карточку",
        )

    previous.ended_on = payload.previous_ended_on
    previous_counterparty_id = previous.counterparty_id

    current = LocationLease(
        location_id=location.id,
        counterparty_id=new_landlord.id,
        monthly_amount=payload.terms.monthly_amount,
        payment_day=payload.terms.payment_day,
        payment_mode=payload.terms.payment_mode,
        documents_mode=payload.terms.documents_mode,
        deposit_amount=payload.terms.deposit_amount,
        started_on=payload.terms.started_on,
        ended_on=payload.terms.ended_on,
        note=(payload.terms.note or "").strip() or None,
        dds_article_id=payload.terms.dds_article_id,
    )
    session.add(current)
    await session.flush()

    archived = False
    if payload.archive_previous:
        archived = await _archive_landlord_if_idle(session, previous_counterparty_id)

    await session.commit()
    await session.refresh(previous)
    await session.refresh(current)

    previous_counterparty = await _counterparty_or_404(session, previous_counterparty_id)
    return LandlordReplacementRead(
        previous=_lease_payload(previous, location.name, previous_counterparty.name),
        current=_lease_payload(current, location.name, new_landlord.name),
        previous_archived=archived,
    )


class LocationLeaseOptionRead(BaseModel):
    lease_id: uuid.UUID
    counterparty_id: uuid.UUID
    counterparty_name: str
    monthly_amount: float
    payment_day: int | None
    documents_mode: Literal["official", "informal"]
    # Реквизитный контур арендодателя — чтобы окно платежа выбрало канал само (банк по
    # реквизитам / карта ИП → Сейф / наличные), той же логикой, что и для контрагента.
    relationship: str
    has_requisites: bool
    requisites_verified: bool


class LocationOptionRead(BaseModel):
    location_id: uuid.UUID
    location_name: str
    kind: LocationKind
    status: LocationStatus
    leases: list[LocationLeaseOptionRead]


class LocationOptionListRead(BaseModel):
    items: list[LocationOptionRead]


@router.get(
    "/options/for-article/{article_id}",
    response_model=LocationOptionListRead,
    dependencies=LOCATIONS_READ_ACCESS,
)
async def list_location_options(
    article_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    on_date: date | None = None,
) -> LocationOptionListRead:
    """Помещения и их арендодатели для платежа по статье с аналитикой по помещению.

    Отдаём ВСЕ помещения, включая закрытые: по закрытой точке приходят исторические выписки,
    и разобрать их должно быть можно. Аренды — только действовавшие на дату платежа, иначе
    оператору предложили бы собственника, которому в этом месяце уже не платят.
    """
    effective_date = on_date or date.today()
    locations = (
        await session.scalars(select(Location).order_by(Location.status, Location.name))
    ).all()

    items: list[LocationOptionRead] = []
    for location in locations:
        leases = await leases_for_location(
            session, location.id, article_id=article_id, on_date=effective_date
        )
        options: list[LocationLeaseOptionRead] = []
        for lease in leases:
            counterparty = await session.get(Counterparty, lease.counterparty_id)
            profile = await session.scalar(
                select(CounterpartyPayableProfile).where(
                    CounterpartyPayableProfile.counterparty_id == lease.counterparty_id
                )
            )
            options.append(
                LocationLeaseOptionRead(
                    lease_id=lease.id,
                    counterparty_id=lease.counterparty_id,
                    counterparty_name=counterparty.name if counterparty else "—",
                    monthly_amount=float(lease.monthly_amount),
                    payment_day=lease.payment_day,
                    documents_mode=lease.documents_mode,  # type: ignore[arg-type]
                    # Профиля может не быть у только что заведённого арендодателя — тогда без
                    # реквизитов (informal): платёж пойдёт через Сейф/наличные, а не в банк.
                    relationship=profile.relationship if profile else "informal",
                    has_requisites=bool(profile.requisites) if profile else False,
                    requisites_verified=bool(profile.requisites_verified) if profile else False,
                )
            )
        items.append(
            LocationOptionRead(
                location_id=location.id,
                location_name=location.name,
                kind=location.kind,  # type: ignore[arg-type]
                status=location.status,  # type: ignore[arg-type]
                leases=options,
            )
        )
    return LocationOptionListRead(items=items)


class LeaseAccrualRead(BaseModel):
    invoice_id: uuid.UUID
    number: str | None
    invoice_date: date | None
    amount: float
    paid_amount: float
    payment_status: str
    activation_status: str
    period_start: date | None
    period_end: date | None


class LeaseLedgerRead(BaseModel):
    accruals: list[LeaseAccrualRead]
    accrued_total: float
    paid_total: float
    outstanding_total: float
    deposit_outstanding: float


@router.post(
    "/{location_id}/leases/{lease_id}/accruals/rebuild",
    response_model=LeaseLedgerRead,
    dependencies=LOCATIONS_EDIT_ACCESS,
)
async def rebuild_lease_accrual(
    location_id: uuid.UUID,
    lease_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    month: date | None = None,
) -> LeaseLedgerRead:
    """Начислить аренду за месяц вручную, не дожидаясь ночной джобы.

    Идемпотентно: повторный вызов за тот же месяц ничего не задваивает — ключ
    ``lease:{id}:{YYYY-MM}`` лежит под уникальным индексом.
    """
    location = await _location_or_404(session, location_id)
    lease = await _lease_or_404(session, lease_id)
    if lease.location_id != location.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Аренда не относится к этому помещению"
        )
    await lease_accruals.rebuild_lease_invoice(session, lease, month or date.today())
    await session.commit()
    return await _lease_ledger(session, lease)


@router.get(
    "/{location_id}/leases/{lease_id}/ledger",
    response_model=LeaseLedgerRead,
    dependencies=LOCATIONS_READ_ACCESS,
)
async def get_lease_ledger(
    location_id: uuid.UUID,
    lease_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LeaseLedgerRead:
    """Начисления и оплаты по договору: что начислено, что оплачено, сколько висит залога."""
    location = await _location_or_404(session, location_id)
    lease = await _lease_or_404(session, lease_id)
    if lease.location_id != location.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Аренда не относится к этому помещению"
        )
    return await _lease_ledger(session, lease)


async def _lease_ledger(session: AsyncSession, lease: LocationLease) -> LeaseLedgerRead:
    invoices = (
        await session.scalars(
            select(SupplierInvoice)
            .where(
                SupplierInvoice.source == lease_accruals.LEASE_INVOICE_SOURCE,
                SupplierInvoice.external_id.like(f"lease:{lease.id}:%"),
            )
            .order_by(SupplierInvoice.invoice_date)
        )
    ).all()

    rows: list[LeaseAccrualRead] = []
    accrued_total = Decimal("0")
    paid_total = Decimal("0")
    for invoice in invoices:
        paid = await counterparty_matching._allocated_amount(session, invoice.id)
        accrued_total += Decimal(invoice.amount)
        paid_total += paid
        rows.append(
            LeaseAccrualRead(
                invoice_id=invoice.id,
                number=invoice.number,
                invoice_date=invoice.invoice_date,
                amount=float(invoice.amount),
                paid_amount=float(paid),
                payment_status=invoice.payment_status,
                activation_status=invoice.activation_status,
                period_start=invoice.service_period_start,
                period_end=invoice.service_period_end,
            )
        )

    deposit = await session.scalar(
        select(
            func.coalesce(
                func.sum(SupplierPrepayment.amount - SupplierPrepayment.amount_settled), 0
            )
        ).where(
            SupplierPrepayment.lease_id == lease.id,
            SupplierPrepayment.status.in_(("open", "partially_settled")),
        )
    )
    return LeaseLedgerRead(
        accruals=rows,
        accrued_total=float(accrued_total),
        paid_total=float(paid_total),
        outstanding_total=float(accrued_total - paid_total),
        deposit_outstanding=float(deposit or 0),
    )


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

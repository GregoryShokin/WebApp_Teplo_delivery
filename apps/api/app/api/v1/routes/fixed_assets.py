"""Учёт основных средств: реестр карточек, амортизация и закрытие месяца.

Карточка = ОДНА физическая единица. Количества в карточке нет сознательно: инвентарный номер
на группу из трёх ларей бессмыслен, а списать один из трёх было бы нечем.

Удаления карточки нет. Объект с историей начислений удалять нельзя — выбывший переводится в
статус (``disposed``/``sold``), и амортизация по нему прекращается.

Остаточная стоимость и накопленная амортизация НЕ хранятся в карточке: они выводятся из строк
начисления. Единственный хранимый снимок — ``residual_after`` в строке месяца, и его
пересчитывает сервис при любой правке.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, get_current_actor, require_permission
from app.db.session import get_session
from app.models import (
    AssetCategory,
    AssetConditionReport,
    DepreciationEntry,
    FixedAsset,
    Location,
)
from app.services import asset_analytics, asset_revaluation
from app.services import fixed_assets as service
from app.services.anthropic_client import LlmCallError

router = APIRouter()
ASSETS_READ_ACCESS = (Depends(require_permission("accounting.fixed_assets.read")),)
ASSETS_EDIT_ACCESS = (Depends(require_permission("accounting.fixed_assets.edit")),)
# Закрытие месяца — это закрытие учётного периода, а не правка карточки: право отдельное,
# чтобы «редактировать ОС» не давало права двигать отчётность.
PERIOD_CLOSE_ACCESS = (Depends(require_permission("accounting.periods.close")),)

AssetStatus = Literal["in_use", "in_storage", "not_working", "disposed", "sold"]
ValuationBasis = Literal["market", "payment"]
ReviewStatus = Literal["ok", "requires_owner_review"]

STATUS_TITLES: dict[str, str] = {
    "in_use": "В работе",
    "in_storage": "На складе",
    "not_working": "Не работает",
    "disposed": "Списан",
    "sold": "Продан",
}


class AssetCategoryRead(BaseModel):
    id: uuid.UUID
    name: str
    useful_life_months: int
    note: str | None


class CategoryListRead(BaseModel):
    items: list[AssetCategoryRead]


class FixedAssetRead(BaseModel):
    id: uuid.UUID
    name: str
    inventory_number: str | None
    brand_model: str | None
    category_id: uuid.UUID | None
    category_name: str | None
    initial_cost: Decimal
    accumulated: Decimal
    residual: Decimal
    monthly_amount: Decimal
    useful_life_months: int | None
    valuation_basis: ValuationBasis
    valued_on: date | None
    commissioned_on: date | None
    status: AssetStatus
    status_title: str
    location: str | None
    location_id: uuid.UUID | None
    location_name: str | None
    source_ref: str | None
    review_status: ReviewStatus
    review_reason: str | None
    note: str | None
    # Амортизация по объекту не идёт: выбыл, не введён в эксплуатацию, без срока или
    # самортизирован полностью. Считаем на бэкенде, чтобы список и карточка судили одинаково.
    depreciating: bool


class FixedAssetListRead(BaseModel):
    items: list[FixedAssetRead]
    total: int


class CategoryTotalRead(BaseModel):
    category_id: uuid.UUID | None
    category_name: str
    count: int
    initial_cost: Decimal
    accumulated: Decimal
    residual: Decimal
    monthly_amount: Decimal


class SummaryRead(BaseModel):
    count: int
    initial_cost: Decimal
    accumulated: Decimal
    residual: Decimal
    monthly_amount: Decimal
    # Последний месяц, за который вообще есть начисления: по нему видно, закрыт ли период.
    last_closed_month: date | None
    by_category: list[CategoryTotalRead]


class DepreciationEntryRead(BaseModel):
    period_month: date
    amount: Decimal
    residual_after: Decimal
    is_manual: bool
    corrected_at: date | None
    note: str | None


class ConditionReportRead(BaseModel):
    """Сообщение менеджера о состоянии объекта и предложение модели по нему."""

    id: uuid.UUID
    message: str
    status: Literal["pending", "proposed", "applied", "dismissed", "failed"]
    cost_before: Decimal
    proposed_cost: Decimal | None
    proposed_reason: str | None
    # Уверенность модели показывается, но НЕ управляет применением: стоимость актива меняет
    # человек, какой бы уверенной модель ни была.
    confidence: Decimal | None
    model: str | None
    error: str | None
    created_at: date


class ConditionReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=3, max_length=4000)


class ConditionDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accept: bool


class FixedAssetDetailRead(FixedAssetRead):
    entries: list[DepreciationEntryRead]
    # История сообщений о состоянии с предложениями модели — свежие первыми.
    condition_reports: list[ConditionReportRead] = Field(default_factory=list)


class FixedAssetCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    initial_cost: Decimal = Field(ge=0)
    category_id: uuid.UUID | None = None
    brand_model: str | None = Field(default=None, max_length=160)
    inventory_number: str | None = Field(default=None, max_length=64)
    valuation_basis: ValuationBasis = "payment"
    valued_on: date | None = None
    commissioned_on: date | None = None
    useful_life_months: int | None = Field(default=None, gt=0)
    status: AssetStatus = "in_use"
    location_id: uuid.UUID | None = None
    source_ref: str | None = Field(default=None, max_length=64)
    note: str | None = None

    @field_validator("name", "brand_model", "inventory_number", "source_ref", "note", mode="after")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed or None


class FixedAssetUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    initial_cost: Decimal | None = Field(default=None, ge=0)
    category_id: uuid.UUID | None = None
    brand_model: str | None = Field(default=None, max_length=160)
    valuation_basis: ValuationBasis | None = None
    valued_on: date | None = None
    commissioned_on: date | None = None
    useful_life_months: int | None = Field(default=None, gt=0)
    status: AssetStatus | None = None
    location_id: uuid.UUID | None = None
    review_status: ReviewStatus | None = None
    review_reason: str | None = None
    note: str | None = None


class DepreciationCorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period_month: date
    amount: Decimal = Field(ge=0)
    note: str | None = None


class MonthCloseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period_month: date


class MonthCloseRead(BaseModel):
    period_month: date
    entries: int
    amount: Decimal


async def _asset_or_404(session: AsyncSession, asset_id: uuid.UUID) -> FixedAsset:
    asset = await session.get(FixedAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Объект не найден")
    return asset


async def _accrued_by_asset(
    session: AsyncSession, asset_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Decimal]:
    """Накопленная амортизация пачкой.

    Одним запросом на весь список, а не по объекту на строку: в реестре 149 карточек, и
    поштучный расчёт превратил бы открытие страницы в 149 запросов.
    """
    if not asset_ids:
        return {}
    rows = (
        await session.execute(
            select(DepreciationEntry.asset_id, func.sum(DepreciationEntry.amount))
            .where(DepreciationEntry.asset_id.in_(asset_ids))
            .group_by(DepreciationEntry.asset_id)
        )
    ).all()
    return {asset_id: Decimal(str(total or 0)) for asset_id, total in rows}


def _monthly_amount(asset: FixedAsset, life: int | None, residual: Decimal) -> Decimal:
    """Плановое начисление за месяц. Ноль, если объект не амортизируется."""
    if not life or residual <= 0:
        return Decimal("0.00")
    if asset.status in service.INACTIVE_STATUSES or asset.commissioned_on is None:
        return Decimal("0.00")
    planned = Decimal(str(asset.initial_cost)) / Decimal(life)
    return min(planned.quantize(Decimal("0.01")), residual)


def _payload(
    asset: FixedAsset,
    *,
    category: AssetCategory | None,
    location_name: str | None,
    accrued: Decimal,
) -> FixedAssetRead:
    initial = Decimal(str(asset.initial_cost))
    residual = max(initial - accrued, Decimal("0.00"))
    life = asset.useful_life_months or (category.useful_life_months if category else None)
    monthly = _monthly_amount(asset, life, residual)
    return FixedAssetRead(
        id=asset.id,
        name=asset.name,
        inventory_number=asset.inventory_number,
        brand_model=asset.brand_model,
        category_id=asset.category_id,
        category_name=category.name if category else None,
        initial_cost=initial,
        accumulated=accrued,
        residual=residual,
        monthly_amount=monthly,
        useful_life_months=life,
        valuation_basis=asset.valuation_basis,  # type: ignore[arg-type]
        valued_on=asset.valued_on,
        commissioned_on=asset.commissioned_on,
        status=asset.status,  # type: ignore[arg-type]
        status_title=STATUS_TITLES.get(asset.status, asset.status),
        location=asset.location,
        location_id=asset.location_id,
        location_name=location_name,
        source_ref=asset.source_ref,
        review_status=asset.review_status,  # type: ignore[arg-type]
        review_reason=asset.review_reason,
        note=asset.note,
        depreciating=monthly > 0,
    )


@router.get("/categories", response_model=CategoryListRead, dependencies=ASSETS_READ_ACCESS)
async def list_categories(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CategoryListRead:
    """Справочник категорий со сроками. Маршрут стоит ДО ``/{asset_id}``, иначе слаг
    перехватится как идентификатор."""
    rows = (await session.scalars(select(AssetCategory).order_by(AssetCategory.name))).all()
    return CategoryListRead(
        items=[
            AssetCategoryRead(
                id=row.id,
                name=row.name,
                useful_life_months=row.useful_life_months,
                note=row.note,
            )
            for row in rows
        ]
    )


@router.get("/summary", response_model=SummaryRead, dependencies=ASSETS_READ_ACCESS)
async def get_summary(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SummaryRead:
    """Свод по строкам баланса: сколько стоило, сколько износилось, сколько осталось.

    Выбывшие объекты (списанные и проданные) в свод не входят — их стоимость уже ушла из
    внеоборотных активов, и показывать её рядом с живыми значило бы завысить баланс.
    """
    assets = (
        await session.scalars(
            select(FixedAsset).where(FixedAsset.status.not_in(("disposed", "sold")))
        )
    ).all()
    categories = {row.id: row for row in (await session.scalars(select(AssetCategory))).all()}
    accrued_map = await _accrued_by_asset(session, [asset.id for asset in assets])

    buckets: dict[uuid.UUID | None, CategoryTotalRead] = {}
    totals = {
        "initial": Decimal("0.00"),
        "accrued": Decimal("0.00"),
        "residual": Decimal("0.00"),
        "monthly": Decimal("0.00"),
    }
    for asset in assets:
        category = categories.get(asset.category_id) if asset.category_id else None
        accrued = accrued_map.get(asset.id, Decimal("0.00"))
        row = _payload(asset, category=category, location_name=None, accrued=accrued)
        bucket = buckets.get(asset.category_id)
        if bucket is None:
            bucket = CategoryTotalRead(
                category_id=asset.category_id,
                category_name=category.name if category else "Без категории",
                count=0,
                initial_cost=Decimal("0.00"),
                accumulated=Decimal("0.00"),
                residual=Decimal("0.00"),
                monthly_amount=Decimal("0.00"),
            )
            buckets[asset.category_id] = bucket
        bucket.count += 1
        bucket.initial_cost += row.initial_cost
        bucket.accumulated += row.accumulated
        bucket.residual += row.residual
        bucket.monthly_amount += row.monthly_amount
        totals["initial"] += row.initial_cost
        totals["accrued"] += row.accumulated
        totals["residual"] += row.residual
        totals["monthly"] += row.monthly_amount

    last_month = await session.scalar(select(func.max(DepreciationEntry.period_month)))
    return SummaryRead(
        count=len(assets),
        initial_cost=totals["initial"],
        accumulated=totals["accrued"],
        residual=totals["residual"],
        monthly_amount=totals["monthly"],
        last_closed_month=last_month,
        by_category=sorted(buckets.values(), key=lambda item: -item.residual),
    )


class UnlinkedPaymentRead(BaseModel):
    transaction_id: uuid.UUID
    operation_date: date
    amount: Decimal
    article_name: str
    article_link_kind: str
    payment_purpose: str | None


class UnlinkedPaymentListRead(BaseModel):
    items: list[UnlinkedPaymentRead]
    total: int


@router.get(
    "/unlinked-payments",
    response_model=UnlinkedPaymentListRead,
    dependencies=ASSETS_READ_ACCESS,
)
async def list_unlinked_payments(
    session: Annotated[AsyncSession, Depends(get_session)],
    since: date | None = None,
) -> UnlinkedPaymentListRead:
    """Платежи по статьям основных средств, за которыми не стоит ни один объект.

    Сеть-ловушка, а не отчёт. Статья попадает на проводку из шести десятков мест — разбор
    выписки, касса, склад, оплата счетов, предоплаты, правила по мерчанту, подтверждение банка,
    ночные джобы, скрипты. Жёсткий гард стоит там, где человек может указать объект; всё
    остальное ловится здесь, включая автоматические контуры, где спрашивать некого, и любую
    точку, которую добавят через полгода.

    Каждая строка списка — покупка или ремонт, ушедшие мимо баланса. Пустой список означает,
    что за деньгами на статьях ОС стоят настоящие объекты.
    """
    rows = await asset_analytics.find_unlinked_asset_payments(session, since=since)
    return UnlinkedPaymentListRead(
        items=[
            UnlinkedPaymentRead(
                transaction_id=row.transaction_id,
                operation_date=row.operation_date,
                amount=row.amount,
                article_name=row.article_name,
                article_link_kind=row.article_link_kind,
                payment_purpose=row.payment_purpose,
            )
            for row in rows
        ],
        total=len(rows),
    )


@router.get("", response_model=FixedAssetListRead, dependencies=ASSETS_READ_ACCESS)
async def list_assets(
    session: Annotated[AsyncSession, Depends(get_session)],
    search: Annotated[str | None, Query(max_length=160)] = None,
    status_filter: Annotated[AssetStatus | None, Query(alias="status")] = None,
    category_id: uuid.UUID | None = None,
    location_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> FixedAssetListRead:
    query = select(FixedAsset)
    if search:
        pattern = f"%{search.strip()}%"
        query = query.where(
            or_(
                FixedAsset.name.ilike(pattern),
                FixedAsset.brand_model.ilike(pattern),
                FixedAsset.inventory_number.ilike(pattern),
                FixedAsset.source_ref.ilike(pattern),
            )
        )
    if status_filter is not None:
        query = query.where(FixedAsset.status == status_filter)
    if category_id is not None:
        query = query.where(FixedAsset.category_id == category_id)
    if location_id is not None:
        query = query.where(FixedAsset.location_id == location_id)

    total = await session.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = (
        await session.scalars(
            query.order_by(FixedAsset.inventory_number, FixedAsset.name).limit(limit).offset(offset)
        )
    ).all()

    categories = {row.id: row for row in (await session.scalars(select(AssetCategory))).all()}
    locations = {row.id: row.name for row in (await session.scalars(select(Location))).all()}
    accrued_map = await _accrued_by_asset(session, [row.id for row in rows])

    return FixedAssetListRead(
        items=[
            _payload(
                row,
                category=categories.get(row.category_id) if row.category_id else None,
                location_name=locations.get(row.location_id) if row.location_id else None,
                accrued=accrued_map.get(row.id, Decimal("0.00")),
            )
            for row in rows
        ],
        total=total,
    )


@router.get("/{asset_id}", response_model=FixedAssetDetailRead, dependencies=ASSETS_READ_ACCESS)
async def get_asset(
    asset_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> FixedAssetDetailRead:
    asset = await _asset_or_404(session, asset_id)
    category = await session.get(AssetCategory, asset.category_id) if asset.category_id else None
    location = await session.get(Location, asset.location_id) if asset.location_id else None
    accrued = await service.accumulated_depreciation(session, asset.id)
    entries = (
        await session.scalars(
            select(DepreciationEntry)
            .where(DepreciationEntry.asset_id == asset.id)
            .order_by(DepreciationEntry.period_month.desc())
        )
    ).all()
    reports = (
        await session.scalars(
            select(AssetConditionReport)
            .where(AssetConditionReport.asset_id == asset.id)
            .order_by(AssetConditionReport.created_at.desc())
        )
    ).all()
    base = _payload(
        asset,
        category=category,
        location_name=location.name if location else None,
        accrued=accrued,
    )
    return FixedAssetDetailRead(
        **base.model_dump(),
        entries=[
            DepreciationEntryRead(
                period_month=entry.period_month,
                amount=Decimal(str(entry.amount)),
                residual_after=Decimal(str(entry.residual_after)),
                is_manual=entry.is_manual,
                corrected_at=entry.corrected_at.date() if entry.corrected_at else None,
                note=entry.note,
            )
            for entry in entries
        ],
        condition_reports=[_report_payload(report) for report in reports],
    )


@router.post(
    "",
    response_model=FixedAssetRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=ASSETS_EDIT_ACCESS,
)
async def create_asset_endpoint(
    payload: FixedAssetCreateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> FixedAssetRead:
    """Завести карточку. Инвентарный номер присваивается сам, если не задан явно."""
    try:
        asset = await service.create_asset(
            session,
            name=payload.name,
            initial_cost=payload.initial_cost,
            category_id=payload.category_id,
            valuation_basis=payload.valuation_basis,
            valued_on=payload.valued_on,
            commissioned_on=payload.commissioned_on,
            useful_life_months=payload.useful_life_months,
            status=payload.status,
            location_id=payload.location_id,
            brand_model=payload.brand_model,
            source_ref=payload.source_ref,
            inventory_number=payload.inventory_number,
            note=payload.note,
        )
    except service.FixedAssetError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    await session.commit()
    await session.refresh(asset)
    category = await session.get(AssetCategory, asset.category_id) if asset.category_id else None
    location = await session.get(Location, asset.location_id) if asset.location_id else None
    return _payload(
        asset,
        category=category,
        location_name=location.name if location else None,
        accrued=Decimal("0.00"),
    )


@router.patch("/{asset_id}", response_model=FixedAssetRead, dependencies=ASSETS_EDIT_ACCESS)
async def update_asset(
    asset_id: uuid.UUID,
    payload: FixedAssetUpdateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> FixedAssetRead:
    """Правка карточки. Инвентарный номер не меняется: он наклеен на объекте."""
    asset = await _asset_or_404(session, asset_id)
    fields = payload.model_dump(exclude_unset=True)
    for field, value in fields.items():
        setattr(asset, field, value)

    accrued = await service.accumulated_depreciation(session, asset.id)
    # Стоимость нельзя опустить ниже уже начисленного: остаточная ушла бы в минус, а баланс
    # показал бы отрицательный актив.
    if Decimal(str(asset.initial_cost)) < accrued:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"По объекту уже начислено {accrued:.2f} ₽ — первоначальная стоимость "
                f"не может быть меньше"
            ),
        )
    if "initial_cost" in fields:
        # База амортизации сменилась: хранимые остатки прошлых месяцев считались от старой.
        await service.recompute_residuals(session, asset.id)
    await session.commit()
    await session.refresh(asset)

    category = await session.get(AssetCategory, asset.category_id) if asset.category_id else None
    location = await session.get(Location, asset.location_id) if asset.location_id else None
    return _payload(
        asset,
        category=category,
        location_name=location.name if location else None,
        accrued=accrued,
    )


@router.patch(
    "/{asset_id}/depreciation",
    response_model=DepreciationEntryRead,
    dependencies=ASSETS_EDIT_ACCESS,
)
async def correct_depreciation_endpoint(
    asset_id: uuid.UUID,
    payload: DepreciationCorrectionRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> DepreciationEntryRead:
    """Поправить начисление за месяц вручную.

    Правка помечается ручной и не затирается повторным закрытием месяца — иначе ночная джоба
    отменяла бы решение человека при первом же перезапуске.
    """
    await _asset_or_404(session, asset_id)
    try:
        entry = await service.correct_depreciation(
            session,
            asset_id=asset_id,
            period_month=payload.period_month,
            amount=payload.amount,
            user_id=actor.user_id,
            note=payload.note,
        )
    except service.FixedAssetError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    await session.commit()
    await session.refresh(entry)
    return DepreciationEntryRead(
        period_month=entry.period_month,
        amount=Decimal(str(entry.amount)),
        residual_after=Decimal(str(entry.residual_after)),
        is_manual=entry.is_manual,
        corrected_at=entry.corrected_at.date() if entry.corrected_at else None,
        note=entry.note,
    )


def _report_payload(report: AssetConditionReport) -> ConditionReportRead:
    return ConditionReportRead(
        id=report.id,
        message=report.message,
        status=report.status,  # type: ignore[arg-type]
        cost_before=Decimal(str(report.cost_before)),
        proposed_cost=(
            Decimal(str(report.proposed_cost)) if report.proposed_cost is not None else None
        ),
        proposed_reason=report.proposed_reason,
        confidence=Decimal(str(report.confidence)) if report.confidence is not None else None,
        model=report.model,
        error=report.error,
        created_at=report.created_at.date(),
    )


@router.post(
    "/{asset_id}/condition",
    response_model=ConditionReportRead,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=ASSETS_EDIT_ACCESS,
)
async def report_condition(
    asset_id: uuid.UUID,
    payload: ConditionReportRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> ConditionReportRead:
    """Менеджер пишет, что случилось с объектом. Оценку даст модель — в фоне.

    Отвечаем 202 и записью в статусе «ожидает»: вызов модели идёт до двух минут, держать на
    нём HTTP-запрос нельзя. Стоимость сама не изменится ни при каком ответе — предложение
    ждёт решения владельца.
    """
    await _asset_or_404(session, asset_id)
    try:
        report = await asset_revaluation.submit_report(
            session, asset_id=asset_id, message=payload.message, user_id=actor.user_id
        )
    except LlmCallError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except IntegrityError as exc:
        # Частичный уникальный индекс: одна необработанная запись на объект. Двойное
        # «Сохранить» не должно дать два платных вызова модели по одному поводу.
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="По этому объекту уже есть сообщение в работе — дождитесь оценки",
        ) from exc
    await session.commit()
    await session.refresh(report)
    return _report_payload(report)


@router.post(
    "/{asset_id}/condition/{report_id}/decision",
    response_model=ConditionReportRead,
    dependencies=ASSETS_EDIT_ACCESS,
)
async def decide_condition(
    asset_id: uuid.UUID,
    report_id: uuid.UUID,
    payload: ConditionDecisionRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> ConditionReportRead:
    """Решение владельца по предложению модели: применить или отклонить.

    Автоприменения нет ни при какой уверенности модели — стоимость актива меняет человек.
    """
    report = await session.get(AssetConditionReport, report_id)
    if report is None or report.asset_id != asset_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Обращение не найдено")
    try:
        await asset_revaluation.decide_report(
            session, report=report, accept=payload.accept, user_id=actor.user_id
        )
    except LlmCallError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    await session.commit()
    await session.refresh(report)
    return _report_payload(report)


@router.post(
    "/close-month",
    response_model=MonthCloseRead,
    dependencies=PERIOD_CLOSE_ACCESS,
)
async def close_month_endpoint(
    payload: MonthCloseRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MonthCloseRead:
    """Закрыть месяц вручную — на случай, когда ночная джоба не отработала.

    Безопасно повторять: начисление идемпотентно по паре «объект и месяц», уже посчитанные
    строки (в том числе поправленные вручную) не трогаются. Прогон пишется в журнал с
    пометкой ``manual``, чтобы ручное закрытие отличалось от ночного.
    """
    run = await service.close_month(session, period_month=payload.period_month, reason="manual")
    return MonthCloseRead(
        period_month=service.month_start(payload.period_month),
        entries=int(run.result.get("entries", 0)),
        amount=Decimal(str(run.result.get("amount", "0.00"))),
    )

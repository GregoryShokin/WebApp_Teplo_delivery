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

import logging
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    CurrentActor,
    get_current_actor,
    require_any_permission,
    require_permission,
)
from app.db.session import get_session
from app.models import (
    AssetBalanceSnapshot,
    AssetCategory,
    AssetConditionReport,
    DepreciationEntry,
    FixedAsset,
    Location,
)
from app.services import asset_analytics, asset_balance, asset_intake, asset_revaluation
from app.services import fixed_assets as service
from app.services.anthropic_client import LlmCallError

logger = logging.getLogger(__name__)

router = APIRouter()
ASSETS_READ_ACCESS = (Depends(require_permission("accounting.fixed_assets.read")),)
ASSETS_EDIT_ACCESS = (Depends(require_permission("accounting.fixed_assets.edit")),)
# Выбор объекта в разборе платежа: пускаем и того, кто разбирает ДДС. Подробности — в докстринге
# ``list_asset_options``; коротко: без этого право «разбирать выписку» упирается в 403 на статье,
# которая карточку требует, и платёж становится неразносимым вообще.
ASSETS_PICK_ACCESS = (
    Depends(require_any_permission(("accounting.fixed_assets.read", "finance.cashflow.classify"))),
)
# Закрытие месяца — это закрытие учётного периода, а не правка карточки: право отдельное,
# чтобы «редактировать ОС» не давало права двигать отчётность.
PERIOD_CLOSE_ACCESS = (Depends(require_permission("accounting.periods.close")),)

AssetStatus = Literal["in_use", "in_storage", "not_working", "disposed", "sold"]
ValuationBasis = Literal["market", "payment"]
ReviewStatus = Literal["ok", "requires_owner_review"]
SpecProfile = Literal["equipment", "furniture", "other"]
AssetCondition = Literal["new", "used"]
ConditionReportKind = Literal["purchase", "incident"]
AcquisitionSource = Literal["purchase", "owner_contribution", "owner_loan", "donation"]

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
    # Какие поля спрашивать при заведении: у техники марка и модель, у мебели материал и
    # размеры. Поле ОБЯЗАНО быть объявлено здесь: ``response_model`` молча выбрасывает всё, чего
    # нет в схеме, и форма получила бы профиль пустым при живом значении в базе — этот дефект в
    # модуле повторялся уже трижды.
    spec_profile: SpecProfile
    note: str | None


class CategoryListRead(BaseModel):
    items: list[AssetCategoryRead]


class FixedAssetRead(BaseModel):
    id: uuid.UUID
    name: str
    inventory_number: str | None
    brand_model: str | None
    # Новым куплен объект или с рук; NULL у карточек описи 2026 — «неизвестно».
    condition: AssetCondition | None
    # Откуда объект взялся у бизнеса — правая сторона баланса. NULL = «не указано».
    acquisition_source: AcquisitionSource | None
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


class LocationTotalRead(BaseModel):
    location_id: uuid.UUID | None
    location_name: str
    # 'point' — торговая точка, 'warehouse' — склад, 'office' — офис.
    kind: str | None
    count: int
    initial_cost: Decimal
    residual: Decimal
    monthly_amount: Decimal
    # Не приносит выручку: резерв, поломка или имущество вне торговой точки. Амортизация на
    # нём при этом идёт — исправная техника в запасе стареет так же, как в работе.
    idle_count: int
    idle_residual: Decimal


class SummaryRead(BaseModel):
    count: int
    initial_cost: Decimal
    accumulated: Decimal
    residual: Decimal
    monthly_amount: Decimal
    # Последний месяц, за который вообще есть начисления: по нему видно, закрыт ли период.
    last_closed_month: date | None
    by_category: list[CategoryTotalRead]
    by_location: list[LocationTotalRead]


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
    # 'incident' — поломка у работающего объекта: предмет разговора стоимость.
    # 'purchase'  — купили б/у: предмет разговора остаток срока службы, цену не трогаем.
    kind: ConditionReportKind
    status: Literal["pending", "proposed", "applied", "dismissed", "failed"]
    cost_before: Decimal
    proposed_cost: Decimal | None
    # Сколько объекту осталось работать по мнению модели. Только у покупок б/у.
    proposed_useful_life_months: int | None
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
    acquisition_source: AcquisitionSource | None = None

    @model_validator(mode="after")
    def _market_asset_needs_an_origin(self) -> FixedAssetCreateRequest:
        """Оценка «рыночная» — значит фирма за объект не платила, и происхождение обязательно.

        Актив вырос, деньги не тратились: в балансе обязана появиться встречная запись —
        вклад собственника, долг перед ним или безвозмездное поступление. Спросить об этом
        можно только сейчас, пока человек помнит; через год про мангал не вспомнит никто, и
        правая сторона баланса будет восстанавливаться догадками.

        У оценки «по платежу» вопроса нет: заплатила фирма, и происхождение подставляется само.
        """
        if self.valuation_basis == "market" and self.acquisition_source is None:
            raise ValueError(
                "Укажите, откуда объект взялся: вклад собственника, долг перед ним или "
                "безвозмездное поступление — без этого в балансе не будет встречной записи"
            )
        return self

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
    # Правится задним числом сознательно: 149 карточек описи 2026 стоят без происхождения, и
    # разметить их может только владелец — поштучно, по мере того как вспоминает.
    acquisition_source: AcquisitionSource | None = None


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
        condition=asset.condition,  # type: ignore[arg-type]
        acquisition_source=asset.acquisition_source,  # type: ignore[arg-type]
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


@router.get("/categories", response_model=CategoryListRead, dependencies=ASSETS_PICK_ACCESS)
async def list_categories(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CategoryListRead:
    """Справочник категорий со сроками. Маршрут стоит ДО ``/{asset_id}``, иначе слаг
    перехватится как идентификатор.

    Доступен и тому, кто разбирает ДДС: при заведении карточки из платежа категорию выбирают
    здесь, а без неё объект остался бы без срока и молча выпал из начисления амортизации.
    Десять названий со сроками — не денежные данные."""
    rows = (await session.scalars(select(AssetCategory).order_by(AssetCategory.name))).all()
    return CategoryListRead(
        items=[
            AssetCategoryRead(
                id=row.id,
                name=row.name,
                useful_life_months=row.useful_life_months,
                spec_profile=row.spec_profile,  # type: ignore[arg-type]
                note=row.note,
            )
            for row in rows
        ]
    )


class AssetOptionRead(BaseModel):
    asset_id: uuid.UUID
    inventory_number: str | None
    name: str
    brand_model: str | None
    location_name: str | None
    status: AssetStatus
    status_title: str
    initial_cost: Decimal


class AssetOptionListRead(BaseModel):
    items: list[AssetOptionRead]


@router.get("/options", response_model=AssetOptionListRead, dependencies=ASSETS_PICK_ACCESS)
async def list_asset_options(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AssetOptionListRead:
    """Справочник объектов для выбора в разборе платежа.

    ОТДЕЛЬНЫЙ ОТ РЕЕСТРА МАРШРУТ, и по двум причинам.

    Первая — доступ. Выписку разбирает финансист, а не бухгалтер по основным средствам. Повесь
    сюда только ``accounting.fixed_assets.read`` — и человек с правом разбирать ДДС упрётся в
    403 на статье, которая объект ТРЕБУЕТ: разнести платёж станет нельзя вообще. Ровно это уже
    случилось с помещениями (``source.locations.read`` менеджеру не выдан). Поэтому пускаем по
    любому из двух прав: видеть объекты в списке ≠ видеть их стоимость, историю и начисления.

    Вторая — вес. Карточка реестра тянет накопленную амортизацию, месячную сумму и признак
    «амортизируется» — это запрос начислений по каждому объекту. Для выпадающего списка нужны
    номер, имя и где стоит.

    ВЫБЫВШИЕ НЕ ОТДАЮТСЯ: расход на проданный или списанный объект гейт всё равно отклонит
    (``resolve_asset_context``), и показывать его в списке значило бы вести оператора в тупик.
    Неработающие остаются — их чинят, и покупка запчасти к ним законна.

    Маршрут стоит ДО ``/{asset_id}``, иначе слаг перехватится как идентификатор.
    """
    rows = (
        await session.execute(
            select(FixedAsset, Location.name)
            .outerjoin(Location, Location.id == FixedAsset.location_id)
            .where(FixedAsset.status.not_in(("disposed", "sold")))
            .order_by(FixedAsset.inventory_number, FixedAsset.name)
        )
    ).all()
    return AssetOptionListRead(
        items=[
            AssetOptionRead(
                asset_id=asset.id,
                inventory_number=asset.inventory_number,
                name=asset.name,
                brand_model=asset.brand_model,
                location_name=location_name,
                status=asset.status,  # type: ignore[arg-type]
                status_title=STATUS_TITLES.get(asset.status, asset.status),
                initial_cost=Decimal(str(asset.initial_cost)),
            )
            for asset, location_name in rows
        ]
    )


class AssetFromPaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    # Сумма ПЛАТЕЖА, а не оценка: по методологии для покупки первоначальная стоимость и есть
    # уплаченные деньги (``valuation_basis='payment'``). Поэтому поле не «стоимость объекта»,
    # а «сколько заплатили», и фронт подставляет его из строки разбора.
    initial_cost: Decimal = Field(gt=0)
    category_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    brand_model: str | None = Field(default=None, max_length=255)
    # День покупки. Амортизация пойдёт с месяца ввода — по умолчанию это дата платежа.
    commissioned_on: date | None = None
    # Материал, размеры, объём — то, по чему объект узнают на следующей инвентаризации.
    # Отдельного поля под характеристики в карточке нет, поэтому кладём их в заметку.
    note: str | None = Field(default=None, max_length=2000)
    condition: AssetCondition | None = None
    # Описание состояния б/у объекта: уходит в ``asset_condition_report``, оттуда его заберёт
    # контур оценки моделью. Для нового объекта смысла не имеет и игнорируется.
    condition_note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _used_needs_description(self) -> AssetFromPaymentRequest:
        """Б/У без описания состояния не принимаем.

        Проверка на СЕРВЕРЕ, а не только в форме: признак «б/у» без описания — это карточка,
        про которую известно ровно то, что износ у неё уже есть, и неизвестно какой. Оценить
        такую нечем, и она молча амортизируется по сроку нового объекта — то есть завышает
        баланс несколько лет подряд. Пусть лучше запрос откажет.
        """
        if self.condition == "used" and not (self.condition_note or "").strip():
            raise ValueError("Для б/у объекта опишите состояние — по нему считается оценка")
        return self


class IntakeTurnIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str
    answer: str


class IntakeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Что написал сотрудник в единственном поле: «купили стол», «рисоварка».
    purchase: str = Field(min_length=1, max_length=500)
    # Вся переписка целиком: состояние диалога держит клиент, на сервере его нет.
    history: list[IntakeTurnIn] = Field(default_factory=list, max_length=10)


class IntakeRead(BaseModel):
    status: Literal["need_more", "ready"]
    question: str | None = None
    why: str | None = None
    suggestions: list[str] = Field(default_factory=list)
    name: str | None = None
    brand: str | None = None
    model: str | None = None
    category_id: uuid.UUID | None = None
    category_name: str | None = None
    material: str | None = None
    dimensions: str | None = None
    specs: str | None = None
    condition: AssetCondition | None = None
    condition_note: str | None = None
    reason: str | None = None


@router.post("/intake", response_model=IntakeRead, dependencies=ASSETS_PICK_ACCESS)
async def asset_intake_step(
    payload: IntakeRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> IntakeRead:
    """Ход диалога заведения карточки: уточняющий вопрос либо готовая карточка.

    Сотрудник пишет «купили стол» — и дальше отвечает на короткие вопросы, вместо того чтобы
    самому решать, офисная это мебель или кухонное оборудование. От этого решения зависит срок
    амортизации, а покупку заводит тот, кто платил, а не бухгалтер по ОС.

    Ошибку модели отдаём как 422 с человеческой причиной: интерфейс по ней возвращается к
    ручной форме. Недоступность модели не должна мешать записать покупку — иначе контур,
    который защищает баланс, сам же его и ломает.
    """
    try:
        result = await asset_intake.next_step(
            session,
            purchase=payload.purchase,
            history=[
                asset_intake.IntakeTurn(question=turn.question, answer=turn.answer)
                for turn in payload.history
            ],
        )
    except asset_intake.AssetIntakeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return IntakeRead(
        status=result.status,  # type: ignore[arg-type]
        question=result.question,
        why=result.why,
        suggestions=list(result.suggestions),
        name=result.name,
        brand=result.brand,
        model=result.model,
        category_id=result.category_id,  # type: ignore[arg-type]
        category_name=result.category_name,
        material=result.material,
        dimensions=result.dimensions,
        specs=result.specs,
        condition=result.condition,  # type: ignore[arg-type]
        condition_note=result.condition_note,
        reason=result.reason,
    )


@router.post(
    "/from-payment",
    response_model=AssetOptionRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=ASSETS_PICK_ACCESS,
)
async def create_asset_from_payment(
    payload: AssetFromPaymentRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> AssetOptionRead:
    """Завести карточку прямо из разбора платежа — купили новое, карточки ещё нет.

    ЗАЧЕМ ОТДЕЛЬНЫЙ МАРШРУТ. Выбор объекта в разборе показывает только УЖЕ заведённые карточки,
    и на главном случае это тупик: купили рисоварку — привязывать не к чему. Отправлять человека
    заводить карточку в другой раздел и возвращаться значит гарантировать, что покупка уйдёт в
    расход мимо баланса: гейт откажет, оператор выберет статью попроще, и всё.

    ПОЧЕМУ ПРАВО ТО ЖЕ, ЧТО У ВЫБОРА. Полный ``POST ""`` даёт завести карточку с любой
    стоимостью, оценкой и датой — это работа бухгалтера по ОС. Здесь же стоимость НЕ
    произвольная: она равна сумме платежа, а ``valuation_basis`` жёстко ``payment``. То есть
    человек не оценивает имущество, а фиксирует то, что уже видно в выписке. Отдавать это право
    отдельно бессмысленно: кто разбирает покупку, тот и знает, что купили.

    Статус и СПИ не спрашиваем: объект куплен и работает (``in_use``), а срок берётся из
    категории — так карточка сразу попадает в начисление, а не зависает без амортизации.

    ДАТА ВВОДА ПОДСТАВЛЯЕТСЯ ОБЯЗАТЕЛЬНО. ``accrue_depreciation`` начисляет с месяца ввода, и
    карточка с пустой датой молча выпадает из начисления: ошибки нет, амортизации нет, а
    заметить это можно только по расхождению баланса через полгода. Поэтому пустое значение
    заменяем сегодняшним днём, а не оставляем как есть.

    Б/У ОБЪЕКТ СРАЗУ ВСТАЁТ В ОЧЕРЕДЬ НА ОЦЕНКУ. Описание состояния уходит в
    ``asset_condition_report`` — ту же таблицу, куда менеджер пишет о поломке, — и его
    подхватывает существующий контур оценки моделью. Отдельного пути не заводим: он отличался
    бы только точкой входа, а владельцу пришлось бы смотреть предложения в двух местах.

    Вид обращения — ``purchase``, и это меняет сам вопрос к модели: не «сколько объект теперь
    стоит», а «сколько ему осталось работать». Цена б/У объекта износ уже содержит (продавец
    его учёл), а срок приходит из категории и считает объект новым — семь лет амортизации у
    пароконвектомата 2018 года. Стоимость при этом не меняется ни на копейку ни сейчас, ни
    после решения владельца: применяется только срок.
    """
    commissioned_on = payload.commissioned_on or datetime.now(UTC).date()
    condition_note = (payload.condition_note or "").strip()
    # Описание состояния дублируем в заметку карточки СОЗНАТЕЛЬНО. Запись в очереди на оценку
    # живёт своей жизнью: её можно отклонить, а вызов модели — провалить. Свидетельство о том,
    # что объект куплен изношенным, должно остаться в карточке в любом из этих случаев.
    note_parts = [
        (payload.note or "").strip(),
        f"Куплен б/у. Состояние при покупке: {condition_note}" if condition_note else "",
    ]
    note = "\n".join(part for part in note_parts if part) or None
    try:
        asset = await service.create_asset(
            session,
            name=payload.name.strip(),
            initial_cost=payload.initial_cost,
            category_id=payload.category_id,
            valuation_basis="payment",
            valued_on=commissioned_on,
            commissioned_on=commissioned_on,
            status="in_use",
            location_id=payload.location_id,
            brand_model=(payload.brand_model or "").strip() or None,
            condition=payload.condition,
            # Заплатила фирма — правую сторону баланса эта карточка не двигает, и вопроса
            # «откуда объект» здесь не существует.
            acquisition_source="purchase",
            note=note,
        )
    except service.FixedAssetError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    if payload.condition == "used" and condition_note:
        # Неудача постановки в очередь не должна отменять карточку: покупка уже состоялась, а
        # оценка — надстройка над ней. Свидетельство при этом не теряется: описание состояния
        # остаётся в заметке карточки.
        try:
            await asset_revaluation.submit_report(
                session,
                asset_id=asset.id,
                message=f"Куплен б/у. Состояние со слов покупателя: {condition_note}",
                user_id=actor.user_id,
                kind="purchase",
            )
        except LlmCallError:
            logger.warning("Не удалось поставить б/у объект %s в очередь на оценку", asset.id)
    await session.commit()
    await session.refresh(asset)
    location = await session.get(Location, asset.location_id) if asset.location_id else None
    return AssetOptionRead(
        asset_id=asset.id,
        inventory_number=asset.inventory_number,
        name=asset.name,
        brand_model=asset.brand_model,
        location_name=location.name if location else None,
        status=asset.status,  # type: ignore[arg-type]
        status_title=STATUS_TITLES.get(asset.status, asset.status),
        initial_cost=Decimal(str(asset.initial_cost)),
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
    locations = {row.id: row for row in (await session.scalars(select(Location))).all()}
    accrued_map = await _accrued_by_asset(session, [asset.id for asset in assets])

    buckets: dict[uuid.UUID | None, CategoryTotalRead] = {}
    places: dict[uuid.UUID | None, LocationTotalRead] = {}
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

        # Разрез «где стоит»: ось помещения отвечает на вопрос, который по категориям не
        # виден — сколько имущества работает на точке, а сколько лежит на складе.
        location = locations.get(asset.location_id) if asset.location_id else None
        place = places.get(asset.location_id)
        if place is None:
            place = LocationTotalRead(
                location_id=asset.location_id,
                location_name=location.name if location else "Помещение не указано",
                kind=location.kind if location else None,
                count=0,
                initial_cost=Decimal("0.00"),
                residual=Decimal("0.00"),
                monthly_amount=Decimal("0.00"),
                idle_count=0,
                idle_residual=Decimal("0.00"),
            )
            places[asset.location_id] = place
        place.count += 1
        place.initial_cost += row.initial_cost
        place.residual += row.residual
        place.monthly_amount += row.monthly_amount
        # Выручку делает только исправное оборудование НА ТОРГОВОЙ ТОЧКЕ. Всё остальное —
        # резерв, поломка или имущество вне точки — стоит денег и амортизируется, а выручки
        # не приносит. Считаем обе причины сразу: владельцу важна сумма, а не её разбор.
        if asset.status != "in_use" or location is None or location.kind != "point":
            place.idle_count += 1
            place.idle_residual += row.residual

    last_month = await session.scalar(select(func.max(DepreciationEntry.period_month)))
    return SummaryRead(
        count=len(assets),
        initial_cost=totals["initial"],
        accumulated=totals["accrued"],
        residual=totals["residual"],
        monthly_amount=totals["monthly"],
        last_closed_month=last_month,
        by_category=sorted(buckets.values(), key=lambda item: -item.residual),
        by_location=sorted(places.values(), key=lambda item: -item.residual),
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


class BalanceLineRead(BaseModel):
    line_name: str
    asset_count: int
    initial_cost: Decimal
    accumulated: Decimal
    residual: Decimal
    depreciation: Decimal


class LineDriftRead(BaseModel):
    line_name: str
    field: str
    snapshot_value: Decimal
    current_value: Decimal


class MonthlyDepreciationRead(BaseModel):
    period_month: date
    amount: Decimal


class ReportingRead(BaseModel):
    period_month: date
    # Одиннадцать строк внеоборотных активов: десять категорий плюс «Не работающее
    # оборудование», которое собирается по статусу.
    lines: list[BalanceLineRead]
    residual_total: Decimal
    depreciation_total: Decimal
    # Месяц заморожен снимком — цифру можно переносить, она не поедет.
    is_frozen: bool
    # Прошлое сдвинули после заморозки: переоценка, коррекция или правка карточки.
    drift: list[LineDriftRead]
    series: list[MonthlyDepreciationRead]


@router.get("/reporting", response_model=ReportingRead, dependencies=ASSETS_READ_ACCESS)
async def get_reporting(
    session: Annotated[AsyncSession, Depends(get_session)],
    period_month: date | None = None,
) -> ReportingRead:
    """Готовые к переносу числа для баланса и ОПиУ.

    Балансового модуля и ОПиУ в приложении нет — они ведутся в таблицах, и по методологии
    реестра «эта цифра переносится вручную пару раз в год». Здесь модуль ОС отдаёт ровно то,
    что переносят: остаточную стоимость по одиннадцати строкам внеоборотных активов на конец
    месяца и помесячный ряд амортизации для строки «УчОС Амортизация».

    Без ``period_month`` берётся последний закрытый месяц: именно его цифры окончательны.
    """
    period = period_month
    if period is None:
        period = await session.scalar(select(func.max(DepreciationEntry.period_month)))
    if period is None:
        # Ни один месяц не закрыт — отдаём пустую форму, а не 404: страница должна открыться
        # и объяснить, что переносить пока нечего.
        return ReportingRead(
            period_month=service.month_start(date.today()),
            lines=[],
            residual_total=Decimal("0.00"),
            depreciation_total=Decimal("0.00"),
            is_frozen=False,
            drift=[],
            series=[],
        )

    period = service.month_start(period)
    frozen = await session.scalar(
        select(func.count())
        .select_from(AssetBalanceSnapshot)
        .where(AssetBalanceSnapshot.period_month == period)
    )
    lines = await asset_balance.balance_lines(session, as_of=period)
    drift = await asset_balance.compare_with_snapshot(session, period_month=period)
    series = await asset_balance.depreciation_series(session)

    return ReportingRead(
        period_month=period,
        lines=[
            BalanceLineRead(
                line_name=line.line_name,
                asset_count=line.asset_count,
                initial_cost=line.initial_cost,
                accumulated=line.accumulated,
                residual=line.residual,
                depreciation=line.depreciation,
            )
            for line in lines
        ],
        residual_total=sum((line.residual for line in lines), Decimal("0.00")),
        depreciation_total=sum((line.depreciation for line in lines), Decimal("0.00")),
        is_frozen=bool(frozen),
        drift=[
            LineDriftRead(
                line_name=item.line_name,
                field=item.field,
                snapshot_value=item.snapshot_value,
                current_value=item.current_value,
            )
            for item in drift
        ],
        series=[
            MonthlyDepreciationRead(period_month=month, amount=amount) for month, amount in series
        ],
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
            acquisition_source=payload.acquisition_source,
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
        kind=report.kind,  # type: ignore[arg-type]
        status=report.status,  # type: ignore[arg-type]
        cost_before=Decimal(str(report.cost_before)),
        proposed_cost=(
            Decimal(str(report.proposed_cost)) if report.proposed_cost is not None else None
        ),
        proposed_useful_life_months=report.proposed_useful_life_months,
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

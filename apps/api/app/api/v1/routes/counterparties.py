"""API модуля «Контрагенты».

Inbox счетов к оплате, отправка пачки накладных одним черновиком в банк, мэчинг
банковских операций с накладными, реестр контрагентов с леджер-категориями и
карточкой (реквизиты + верификация). Права: чтение — counterparties.read,
операции — counterparties.operate, администрирование — counterparties.admin.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    CurrentActor,
    ensure_permission,
    get_current_actor,
    require_any_permission,
    require_permission,
)
from app.core.config import get_settings
from app.db.session import get_session
from app.models import CounterpartyPaymentDraft, SupplierInvoice, SupplierPrepayment, Wallet
from app.services import counterparty_bank_match as bank_match
from app.services import counterparty_barter_match as barter
from app.services import counterparty_matching as matching
from app.services import counterparty_payments as payments
from app.services import counterparty_registry as registry
from app.services import supplier_prepayments as prepayments
from app.services.banking.exceptions import BankCredentialsError, BankFetchError
from app.services.counterparty_invoice_sync import (
    list_unlinked_iiko_suppliers,
    sync_counterparty_invoices,
)
from app.services.warehouse_invoices import invoice_permission_kind

router = APIRouter()

READ = (Depends(require_permission("counterparties.read")),)
OPERATE = (Depends(require_permission("counterparties.operate")),)
ADMIN = (Depends(require_permission("counterparties.admin")),)
# Справочники для формы накладной (контрагенты, статьи) — доступны и из Кассы.
INVOICE_REFS = (
    Depends(require_any_permission(("counterparties.read", "kassa.invoices.create"))),
)

_DOMAIN_ERRORS = (
    payments.CounterpartyPaymentError,
    matching.CounterpartyMatchError,
    registry.CounterpartyRegistryError,
)


def _conflict(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


def _bank_rejected(exc: BankFetchError) -> HTTPException:
    """Отказ банка при создании черновика → осмысленный HTTP вместо голой 500.

    Отказ по данным (4xx кроме авторизации, напр. неверный контрольный разряд счёта) →
    422 с причиной от банка, которую фронт покажет тостом. Проблема доступа/недоступность
    банка → 502 (это на нашей стороне/у банка, а не ошибка пользователя).
    """
    if isinstance(exc, BankCredentialsError):
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Ошибка авторизации в банке — обратитесь к администратору",
        )
    if exc.status_code is not None and 400 <= exc.status_code < 500:
        message = "Банк отклонил платёж по реквизитам получателя"
        if exc.detail:
            message = f"{message}: {exc.detail}"
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=message)
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="Банк временно недоступен — попробуйте позже",
    )


# --- schemas ------------------------------------------------------------------


class CategoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    sort_order: int
    is_active: bool


class CategoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=160)
    sort_order: int = 0


class CategoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=160)
    sort_order: int | None = None
    is_active: bool | None = None


class InvoiceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    counterparty_id: uuid.UUID
    counterparty_name: str
    ledger_category_id: uuid.UUID | None
    source: str
    direction: str
    number: str | None
    invoice_date: date | None
    due_date: date | None
    amount: float
    vat_total: float
    vat_breakdown: dict[str, Any]
    allocated: float
    remaining: float
    payment_status: str
    draft_id: uuid.UUID | None
    barter_settlement_id: uuid.UUID | None = None
    barter_role: str | None = None
    iiko_push_status: str = "not_pushed"
    iiko_push_error: str | None = None
    external_id: str | None = None
    draft_status: str | None = None
    draft_pays_via_safe: bool = False


class RegistryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    counterparty_id: uuid.UUID
    name: str
    inn: str | None
    status: str
    relationship: str
    ledger_category_id: uuid.UUID | None
    brand_group: str | None
    internal_name: str | None
    payment_delay_days: int | None
    requisites_verified: bool
    kassa_enabled: bool
    has_iiko_guid: bool
    unpaid_count: int
    unpaid_remaining: float
    receivable_remaining: float
    prepayment_balance: float


class CardRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    counterparty_id: uuid.UUID
    name: str
    inn: str | None
    type: str
    status: str
    relationship: str
    barter_balance: float
    profile: dict[str, Any] | None
    aliases: list[dict[str, Any]]
    collection_sources: list[dict[str, Any]]
    routing_rules: list[dict[str, Any]]
    invoices: list[InvoiceRead]
    drafts: list[dict[str, Any]]


class DraftRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    # None — черновик «просто траты» без получателя (окно «Новый платёж», 0161).
    counterparty_id: uuid.UUID | None = None
    document_id: str
    amount: float
    status: str
    provider_ref: str | None
    last_error: str | None
    # Выплата через Сейф (неофициальный поставщик): черновик выписан на карту ИП.
    pays_via_safe: bool = False
    created_at: Any


class InvoiceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    counterparty_id: uuid.UUID
    amount: Decimal = Field(gt=0)
    number: str | None = Field(default=None, max_length=128)
    invoice_date: date | None = None
    due_date: date | None = None
    note: str | None = None
    # Optional VAT breakdown {rate: amount}, e.g. {"10": 90.91, "22": 180.33}.
    vat_breakdown: dict[str, Decimal] | None = None


class DraftCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_ids: list[uuid.UUID] = Field(min_length=1)


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ledger_category_id: uuid.UUID | None = None
    relationship: str | None = None
    brand_group: str | None = Field(default=None, max_length=160)
    internal_name: str | None = Field(default=None, max_length=255)
    payment_delay_days: int | None = Field(default=None, ge=0)
    payment_due_day_of_month: int | None = Field(default=None, ge=1, le=31)
    manager_name: str | None = Field(default=None, max_length=160)
    manager_phone: str | None = Field(default=None, max_length=64)
    default_dds_article_id: uuid.UUID | None = None
    status: str | None = None


class RequisitesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requisites: dict[str, Any]
    verified: bool = False


class KassaEnabledUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class CashAllocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(gt=0)
    cashflow_transaction_id: uuid.UUID | None = None


class ManualPaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(gt=0)
    wallet_id: uuid.UUID
    operation_date: date
    article_id: uuid.UUID | None = None
    comment: str | None = None


class PrepaymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    counterparty_id: uuid.UUID
    wallet_id: uuid.UUID
    amount: Decimal = Field(gt=0)
    operation_date: date
    article_id: uuid.UUID | None = None
    kind: str = "goods"
    note: str | None = None


class PrepaymentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    counterparty_id: uuid.UUID
    kind: str
    wallet_id: uuid.UUID | None
    amount: float
    amount_settled: float
    status: str
    article_id: uuid.UUID | None
    note: str | None
    created_at: datetime


class SettleFromPrepaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prepayment_id: uuid.UUID
    amount: Decimal | None = Field(default=None, gt=0)


class BankPrepaymentDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    counterparty_id: uuid.UUID
    amount: Decimal = Field(gt=0)
    article_id: uuid.UUID | None = None


class WalletRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    type: str


class ExpenseArticleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str


class AllocateOperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_id: uuid.UUID


class CounterpartyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    inn: str | None = Field(default=None, max_length=12)
    type: str = "legal_entity"
    # Канал оплаты: official (банк) / informal (карта-нал) / barter (зачёт по сальдо).
    relationship: Literal["official", "informal", "barter"] = "official"
    internal_name: str | None = Field(default=None, max_length=255)
    ledger_category_id: uuid.UUID | None = None
    brand_group: str | None = Field(default=None, max_length=160)
    payment_delay_days: int | None = Field(default=None, ge=0)
    payment_due_day_of_month: int | None = Field(default=None, ge=1, le=31)
    manager_name: str | None = Field(default=None, max_length=160)
    manager_phone: str | None = Field(default=None, max_length=64)
    # Привязка к поставщику из справочника iiko (GUID) — заводит alias source='iiko', чтобы
    # синк накладных узнавал контрагента, а не плодил дубль.
    iiko_supplier_guid: str | None = Field(default=None, max_length=64)


class IikoSupplierOption(BaseModel):
    guid: str
    name: str
    inn: str | None = None


class CollectionSourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    value: str | None = Field(default=None, max_length=255)
    note: str | None = None


class RoutingRuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prefix: str = Field(min_length=1, max_length=64)
    target_counterparty_id: uuid.UUID


class MatchCandidateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    bank_operation_id: uuid.UUID
    operation_date: date
    amount: float
    official_name: str | None
    inn: str | None
    requisites: dict[str, Any]


class MatchSuggestionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    invoice_id: uuid.UUID
    invoice_number: str | None
    invoice_amount: float
    counterparty_id: uuid.UUID
    counterparty_name: str
    counterparty_has_inn: bool
    candidates: list[MatchCandidateRead]
    confident: bool


class BarterSettleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payable_ids: list[uuid.UUID] = Field(min_length=1)
    receivable_ids: list[uuid.UUID] = Field(min_length=1)


class ConfirmMatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_id: uuid.UUID
    bank_operation_id: uuid.UUID
    enrich: bool = True


# --- categories ---------------------------------------------------------------


@router.get("/categories", response_model=list[CategoryRead], dependencies=READ)
async def get_categories(
    session: Annotated[AsyncSession, Depends(get_session)],
    include_inactive: bool = False,
) -> list[CategoryRead]:
    rows = await registry.list_categories(session, include_inactive=include_inactive)
    return [CategoryRead.model_validate(row) for row in rows]


@router.post(
    "/categories",
    response_model=CategoryRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=ADMIN,
)
async def post_category(
    payload: CategoryCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CategoryRead:
    try:
        category = await registry.create_category(
            session, code=payload.code, name=payload.name, sort_order=payload.sort_order
        )
    except registry.CounterpartyRegistryError as exc:
        raise _conflict(exc) from exc
    return CategoryRead.model_validate(category)


@router.put("/categories/{category_id}", response_model=CategoryRead, dependencies=ADMIN)
async def put_category(
    category_id: uuid.UUID,
    payload: CategoryUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CategoryRead:
    try:
        category = await registry.update_category(
            session,
            category_id,
            name=payload.name,
            sort_order=payload.sort_order,
            is_active=payload.is_active,
        )
    except registry.CounterpartyRegistryError as exc:
        raise _conflict(exc) from exc
    return CategoryRead.model_validate(category)


# --- invoices inbox -----------------------------------------------------------


@router.get("/invoices", response_model=list[InvoiceRead], dependencies=INVOICE_REFS)
async def get_invoices(
    session: Annotated[AsyncSession, Depends(get_session)],
    status_filter: Annotated[str | None, Query(alias="status")] = "unpaid,partially_paid",
    counterparty_id: uuid.UUID | None = None,
    category_id: uuid.UUID | None = None,
    in_draft: bool | None = None,
    direction: str = "payable",
    relationship: str | None = None,
    source: str | None = None,
    # Мультивыбор поставщиков передаётся CSV-строкой UUID (как status) — надёжнее, чем
    # массив в query, независимо от сериализации на клиенте.
    counterparty_ids: Annotated[str | None, Query()] = None,
    date_from: date | None = None,
    date_to: date | None = None,
    not_in_iiko: bool | None = None,
) -> list[InvoiceRead]:
    statuses = (
        tuple(part.strip() for part in status_filter.split(",") if part.strip())
        if status_filter
        else None
    )
    cp_ids: list[uuid.UUID] | None = None
    if counterparty_ids:
        try:
            cp_ids = [
                uuid.UUID(part.strip()) for part in counterparty_ids.split(",") if part.strip()
            ] or None
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Некорректный counterparty_ids",
            ) from exc
    items = await registry.list_invoices(
        session,
        statuses=statuses,
        counterparty_id=counterparty_id,
        counterparty_ids=cp_ids,
        category_id=category_id,
        in_draft=in_draft,
        direction=direction or None,
        relationship=relationship,
        source=source,
        date_from=date_from,
        date_to=date_to,
        not_in_iiko=not_in_iiko,
        # «Накладные» — производственный контур; почтовые счета (услуги) показываются только
        # на «Странице на оплату». Если явно просят source='email', не исключаем.
        exclude_sources=("email",) if source is None else None,
    )
    return [InvoiceRead.model_validate(item) for item in items]


@router.post(
    "/invoices",
    response_model=InvoiceRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=OPERATE,
)
async def post_manual_invoice(
    payload: InvoiceCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> InvoiceRead:
    try:
        invoice = await registry.create_manual_invoice(
            session,
            counterparty_id=payload.counterparty_id,
            amount=payload.amount,
            number=payload.number,
            invoice_date=payload.invoice_date,
            due_date=payload.due_date,
            note=payload.note,
            vat_breakdown=payload.vat_breakdown,
            actor_user_id=actor.user_id,
        )
    except registry.CounterpartyRegistryError as exc:
        raise _conflict(exc) from exc
    item = await registry.get_invoice_item(session, invoice.id)
    return InvoiceRead.model_validate(item)


@router.post("/invoices/{invoice_id}/void", response_model=InvoiceRead, dependencies=OPERATE)
async def post_void_invoice(
    invoice_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> InvoiceRead:
    try:
        await registry.void_invoice(session, invoice_id)
    except registry.CounterpartyRegistryError as exc:
        raise _conflict(exc) from exc
    item = await registry.get_invoice_item(session, invoice_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Накладная не найдена")
    return InvoiceRead.model_validate(item)


@router.post(
    "/invoices/{invoice_id}/allocate-cash",
    response_model=InvoiceRead,
    dependencies=OPERATE,
)
async def post_allocate_cash(
    invoice_id: uuid.UUID,
    payload: CashAllocationRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> InvoiceRead:
    try:
        await matching.allocate_cash_to_invoice(
            session,
            invoice_id=invoice_id,
            amount=payload.amount,
            cashflow_transaction_id=payload.cashflow_transaction_id,
            actor_user_id=actor.user_id,
        )
    except matching.CounterpartyMatchError as exc:
        raise _conflict(exc) from exc
    item = await registry.get_invoice_item(session, invoice_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Накладная не найдена")
    return InvoiceRead.model_validate(item)


@router.post("/invoices/{invoice_id}/pay", response_model=InvoiceRead)
async def post_pay_invoice(
    invoice_id: uuid.UUID,
    payload: ManualPaymentRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> InvoiceRead:
    """Manually pay (part of) an invoice from a DDS wallet — creates a DDS expense.
    Право invoices.{normal|barter}.pay по типу накладной (бартер платится из бартер-инбокса)."""
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Накладная не найдена")
    ensure_permission(actor, f"invoices.{await invoice_permission_kind(session, invoice)}.pay")
    try:
        await payments.pay_invoice_from_wallet(
            session,
            invoice_id=invoice_id,
            wallet_id=payload.wallet_id,
            amount=payload.amount,
            operation_date=payload.operation_date,
            article_id=payload.article_id,
            comment=payload.comment,
            actor_user_id=actor.user_id,
        )
    except payments.CounterpartyPaymentError as exc:
        raise _conflict(exc) from exc
    item = await registry.get_invoice_item(session, invoice_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Накладная не найдена")
    return InvoiceRead.model_validate(item)


# --- предоплаты поставщикам (дебиторка) ----------------------------------------


@router.post(
    "/prepayments", response_model=PrepaymentRead, status_code=status.HTTP_201_CREATED
)
async def post_create_prepayment(
    payload: PrepaymentCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> PrepaymentRead:
    """Завести предоплату поставщику (дебиторка): реальный расход с кошелька + запись долга.

    Кошелёк — только наличный (Сейф/Касса): проводка создаётся сразу, а прямые ручные
    проводки по банку запрещены (баланс банка ведётся от выписки) — банковская
    предоплата идёт черновиком через ``/prepayments/bank-draft``. Для Сейфа сумма
    ограничена свободным остатком (не съедаем чужие резервы).
    """
    ensure_permission(actor, "invoices.normal.pay")
    wallet = await session.get(Wallet, payload.wallet_id)
    if wallet is None or wallet.status != "active" or wallet.type not in (
        "cash_safe",
        "store_cash",
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Предоплата проводится только с наличного счёта (Сейф/Касса); "
                "банковская — черновиком «в банк»"
            ),
        )
    if wallet.type == "cash_safe":
        from app.api.v1.routes.dds import _safe_free_amount

        free = await _safe_free_amount(session, wallet)
        if payload.amount > free:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Недостаточно свободных средств на Сейфе: "
                    f"свободно {free}, запрошено {payload.amount}"
                ),
            )
    try:
        prepayment = await prepayments.create_supplier_prepayment(
            session,
            counterparty_id=payload.counterparty_id,
            wallet_id=payload.wallet_id,
            amount=payload.amount,
            operation_date=payload.operation_date,
            article_id=payload.article_id,
            kind=payload.kind,
            note=payload.note,
            actor_user_id=actor.user_id,
        )
    except payments.CounterpartyPaymentError as exc:
        raise _conflict(exc) from exc
    return PrepaymentRead.model_validate(prepayment)


@router.post(
    "/prepayments/bank-draft",
    response_model=DraftRead,
    status_code=status.HTTP_201_CREATED,
)
async def post_prepayment_bank_draft(
    payload: BankPrepaymentDraftRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> DraftRead:
    """«Банк по реквизитам»: standalone-черновик в банк по реквизитам контрагента (без
    накладной). При статусе «исполнен» создаётся предоплата (дебиторка)."""
    ensure_permission(actor, "invoices.normal.pay")
    try:
        draft = await payments.create_standalone_payment_draft(
            session,
            counterparty_id=payload.counterparty_id,
            amount=payload.amount,
            prepayment_article_id=payload.article_id,
            actor_user_id=actor.user_id,
        )
    except payments.CounterpartyPaymentError as exc:
        raise _conflict(exc) from exc
    except BankFetchError as exc:
        raise _bank_rejected(exc) from exc
    return DraftRead.model_validate(draft)


@router.get("/prepayments", response_model=list[PrepaymentRead], dependencies=READ)
async def get_prepayments(
    session: Annotated[AsyncSession, Depends(get_session)],
    counterparty_id: uuid.UUID | None = None,
    only_open: bool = True,
) -> list[PrepaymentRead]:
    query = select(SupplierPrepayment)
    if counterparty_id is not None:
        query = query.where(SupplierPrepayment.counterparty_id == counterparty_id)
    if only_open:
        query = query.where(SupplierPrepayment.status.in_(("open", "partially_settled")))
    query = query.order_by(SupplierPrepayment.created_at.desc())
    rows = (await session.scalars(query)).all()
    return [PrepaymentRead.model_validate(row) for row in rows]


@router.post("/invoices/{invoice_id}/settle-from-prepayment", response_model=InvoiceRead)
async def post_settle_from_prepayment(
    invoice_id: uuid.UUID,
    payload: SettleFromPrepaymentRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> InvoiceRead:
    """Погасить (часть) накладной против выданной предоплаты — без движения денег."""
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Накладная не найдена")
    ensure_permission(actor, f"invoices.{await invoice_permission_kind(session, invoice)}.pay")
    try:
        await prepayments.settle_invoice_from_prepayment(
            session,
            invoice_id=invoice_id,
            prepayment_id=payload.prepayment_id,
            amount=payload.amount,
            actor_user_id=actor.user_id,
        )
    except payments.CounterpartyPaymentError as exc:
        raise _conflict(exc) from exc
    item = await registry.get_invoice_item(session, invoice_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Накладная не найдена")
    return InvoiceRead.model_validate(item)


# --- registry & card ----------------------------------------------------------


@router.get("/registry", response_model=list[RegistryRead], dependencies=INVOICE_REFS)
async def get_registry(
    session: Annotated[AsyncSession, Depends(get_session)],
    category_id: uuid.UUID | None = None,
    include_archived: bool = False,
    kassa_only: bool = False,
) -> list[RegistryRead]:
    items = await registry.list_registry(
        session,
        category_id=category_id,
        include_archived=include_archived,
        kassa_only=kassa_only,
    )
    return [RegistryRead.model_validate(item) for item in items]


@router.get("/needs-setup", dependencies=READ)
async def get_needs_setup(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    items = await registry.list_needs_setup(session)
    return {"count": len(items), "items": items}


@router.get("/iiko-suppliers", response_model=list[IikoSupplierOption], dependencies=ADMIN)
async def get_iiko_suppliers(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[IikoSupplierOption]:
    """Поставщики из справочника iiko, ещё не привязанные к контрагенту — кандидаты на онбординг
    (живой запрос к iiko, поэтому только под admin и не в общем реестре)."""
    try:
        rows = await list_unlinked_iiko_suppliers(session)
    except Exception as exc:  # noqa: BLE001 — сетевая/iiko-ошибка → 502, фронт покажет тост
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Не удалось получить поставщиков iiko: {exc}",
        ) from exc
    return [IikoSupplierOption(**row) for row in rows]


@router.get("/wallets", response_model=list[WalletRead], dependencies=READ)
async def get_wallets(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[WalletRead]:
    return [WalletRead.model_validate(wallet) for wallet in await registry.list_wallets(session)]


@router.get("/expense-articles", response_model=list[ExpenseArticleRead], dependencies=INVOICE_REFS)
async def get_expense_articles(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[ExpenseArticleRead]:
    rows = await registry.list_expense_articles(session)
    return [ExpenseArticleRead.model_validate(row) for row in rows]


@router.post(
    "",
    response_model=CardRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=ADMIN,
)
async def post_counterparty(
    payload: CounterpartyCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CardRead:
    try:
        counterparty = await registry.create_counterparty(
            session,
            name=payload.name,
            inn=payload.inn,
            cp_type=payload.type,
            relationship=payload.relationship,
            internal_name=payload.internal_name,
            ledger_category_id=payload.ledger_category_id,
            brand_group=payload.brand_group,
            payment_delay_days=payload.payment_delay_days,
            payment_due_day_of_month=payload.payment_due_day_of_month,
            manager_name=payload.manager_name,
            manager_phone=payload.manager_phone,
            iiko_supplier_guid=payload.iiko_supplier_guid,
        )
    except registry.CounterpartyRegistryError as exc:
        raise _conflict(exc) from exc
    card = await registry.get_counterparty_card(session, counterparty.id)
    return CardRead.model_validate(card)


@router.get("/{counterparty_id}", response_model=CardRead, dependencies=READ)
async def get_card(
    counterparty_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CardRead:
    card = await registry.get_counterparty_card(session, counterparty_id)
    if card is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Контрагент не найден")
    return CardRead.model_validate(card)


@router.put("/{counterparty_id}/profile", response_model=CardRead, dependencies=ADMIN)
async def put_profile(
    counterparty_id: uuid.UUID,
    payload: ProfileUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CardRead:
    try:
        await registry.update_profile(
            session,
            counterparty_id,
            ledger_category_id=payload.ledger_category_id,
            relationship=payload.relationship,
            brand_group=payload.brand_group,
            internal_name=payload.internal_name,
            payment_delay_days=payload.payment_delay_days,
            payment_due_day_of_month=payload.payment_due_day_of_month,
            manager_name=payload.manager_name,
            manager_phone=payload.manager_phone,
            default_dds_article_id=payload.default_dds_article_id,
            status=payload.status,
        )
    except registry.CounterpartyRegistryError as exc:
        raise _conflict(exc) from exc
    card = await registry.get_counterparty_card(session, counterparty_id)
    return CardRead.model_validate(card)


@router.post("/{counterparty_id}/kassa-enabled", response_model=CardRead, dependencies=OPERATE)
async def post_kassa_enabled(
    counterparty_id: uuid.UUID,
    payload: KassaEnabledUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CardRead:
    """Переключить «Активен в Кассе» — видимость поставщика в дропдауне накладной Кассы."""
    try:
        await registry.set_kassa_enabled(session, counterparty_id, enabled=payload.enabled)
    except registry.CounterpartyRegistryError as exc:
        raise _conflict(exc) from exc
    card = await registry.get_counterparty_card(session, counterparty_id)
    return CardRead.model_validate(card)


@router.get("/{counterparty_id}/requisites/suggestion", dependencies=ADMIN)
async def get_requisites_suggestion(
    counterparty_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    return await registry.autofill_requisites_from_bank(session, counterparty_id)


@router.put("/{counterparty_id}/requisites", response_model=CardRead, dependencies=ADMIN)
async def put_requisites(
    counterparty_id: uuid.UUID,
    payload: RequisitesUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> CardRead:
    try:
        await registry.set_requisites(
            session,
            counterparty_id,
            requisites=payload.requisites,
            verified=payload.verified,
            actor_user_id=actor.user_id,
        )
    except registry.CounterpartyRegistryError as exc:
        raise _conflict(exc) from exc
    card = await registry.get_counterparty_card(session, counterparty_id)
    return CardRead.model_validate(card)


@router.post("/{counterparty_id}/archive", response_model=CardRead, dependencies=ADMIN)
async def post_archive(
    counterparty_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CardRead:
    try:
        await registry.set_counterparty_archived(session, counterparty_id, archived=True)
    except registry.CounterpartyRegistryError as exc:
        raise _conflict(exc) from exc
    card = await registry.get_counterparty_card(session, counterparty_id)
    return CardRead.model_validate(card)


@router.post("/{counterparty_id}/unarchive", response_model=CardRead, dependencies=ADMIN)
async def post_unarchive(
    counterparty_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CardRead:
    try:
        await registry.set_counterparty_archived(session, counterparty_id, archived=False)
    except registry.CounterpartyRegistryError as exc:
        raise _conflict(exc) from exc
    card = await registry.get_counterparty_card(session, counterparty_id)
    return CardRead.model_validate(card)


@router.post(
    "/{counterparty_id}/sources",
    response_model=CardRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=ADMIN,
)
async def post_collection_source(
    counterparty_id: uuid.UUID,
    payload: CollectionSourceCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CardRead:
    try:
        await registry.add_collection_source(
            session,
            counterparty_id=counterparty_id,
            kind=payload.kind,
            value=payload.value,
            note=payload.note,
        )
    except registry.CounterpartyRegistryError as exc:
        raise _conflict(exc) from exc
    card = await registry.get_counterparty_card(session, counterparty_id)
    return CardRead.model_validate(card)


@router.delete(
    "/{counterparty_id}/sources/{source_id}",
    response_model=CardRead,
    dependencies=ADMIN,
)
async def delete_collection_source(
    counterparty_id: uuid.UUID,
    source_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CardRead:
    try:
        await registry.remove_collection_source(session, source_id)
    except registry.CounterpartyRegistryError as exc:
        raise _conflict(exc) from exc
    card = await registry.get_counterparty_card(session, counterparty_id)
    return CardRead.model_validate(card)


@router.post(
    "/{counterparty_id}/routing",
    response_model=CardRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=ADMIN,
)
async def post_routing_rule(
    counterparty_id: uuid.UUID,
    payload: RoutingRuleCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CardRead:
    try:
        await registry.add_routing_rule(
            session,
            counterparty_id=counterparty_id,
            prefix=payload.prefix,
            target_counterparty_id=payload.target_counterparty_id,
        )
    except registry.CounterpartyRegistryError as exc:
        raise _conflict(exc) from exc
    card = await registry.get_counterparty_card(session, counterparty_id)
    return CardRead.model_validate(card)


@router.delete(
    "/{counterparty_id}/routing/{rule_id}",
    response_model=CardRead,
    dependencies=ADMIN,
)
async def delete_routing_rule(
    counterparty_id: uuid.UUID,
    rule_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CardRead:
    try:
        await registry.remove_routing_rule(session, rule_id)
    except registry.CounterpartyRegistryError as exc:
        raise _conflict(exc) from exc
    card = await registry.get_counterparty_card(session, counterparty_id)
    return CardRead.model_validate(card)


# --- barter loan settlement ---------------------------------------------------


def _barter_invoice(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(item["id"]),
        "number": item["number"],
        "invoice_date": item["invoice_date"].isoformat() if item["invoice_date"] else None,
        "amount": float(item["amount"]),
        "products": item["products"],
    }


@router.get("/{counterparty_id}/barter", dependencies=READ)
async def get_barter(
    counterparty_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    detail = await barter.barter_detail(session, counterparty_id)
    return {
        "relationship_balance": float(detail.relationship_balance),
        "open_payables": [_barter_invoice(item) for item in detail.open_payables],
        "open_receivables": [_barter_invoice(item) for item in detail.open_receivables],
        "settlements": [
            {
                "id": str(view.id),
                "amount": float(view.amount),
                "is_auto": view.is_auto,
                "we_lent": view.we_lent,
                "loan_numbers": view.loan_numbers,
                "return_numbers": view.return_numbers,
            }
            for view in detail.settlements
        ],
    }


@router.get("/{counterparty_id}/barter/suggestions", dependencies=READ)
async def get_barter_suggestions(
    counterparty_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, Any]]:
    suggestions = await barter.suggest_barter_matches(session, counterparty_id)
    return [
        {
            "payable_ids": [str(i) for i in suggestion.payable_ids],
            "receivable_ids": [str(i) for i in suggestion.receivable_ids],
            "amount": float(suggestion.amount),
            "confident": suggestion.confident,
            "we_lent": suggestion.we_lent,
        }
        for suggestion in suggestions
    ]


@router.post("/{counterparty_id}/barter/auto-settle", dependencies=OPERATE)
async def post_barter_auto_settle(
    counterparty_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, int]:
    settled = await barter.auto_settle_barter(session, counterparty_id, actor_user_id=actor.user_id)
    return {"settled": settled}


@router.post("/{counterparty_id}/barter/settle", dependencies=OPERATE)
async def post_barter_settle(
    counterparty_id: uuid.UUID,
    payload: BarterSettleRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    try:
        settlement = await barter.confirm_barter_settlement(
            session,
            counterparty_id,
            payable_ids=payload.payable_ids,
            receivable_ids=payload.receivable_ids,
            actor_user_id=actor.user_id,
        )
    except barter.BarterMatchError as exc:
        raise _conflict(exc) from exc
    return {"id": str(settlement.id), "amount": float(settlement.amount)}


# --- drafts -------------------------------------------------------------------


@router.get("/drafts/list", response_model=list[DraftRead], dependencies=READ)
async def get_drafts(
    session: Annotated[AsyncSession, Depends(get_session)],
    counterparty_id: uuid.UUID | None = None,
) -> list[DraftRead]:
    query = select(CounterpartyPaymentDraft).order_by(CounterpartyPaymentDraft.created_at.desc())
    if counterparty_id is not None:
        query = query.where(CounterpartyPaymentDraft.counterparty_id == counterparty_id)
    rows = (await session.execute(query)).scalars().all()
    return [DraftRead.model_validate(row) for row in rows]


@router.post(
    "/drafts",
    response_model=DraftRead,
    status_code=status.HTTP_201_CREATED,
)
async def post_draft(
    payload: DraftCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> DraftRead:
    # «Отправить в банк» = оплата обычных накладных (бартер в банк не отправляется).
    ensure_permission(actor, "invoices.normal.pay")
    try:
        draft = await payments.create_payment_draft_for_invoices(
            session, invoice_ids=payload.invoice_ids, actor_user_id=actor.user_id
        )
    except payments.CounterpartyPaymentError as exc:
        raise _conflict(exc) from exc
    except BankFetchError as exc:
        raise _bank_rejected(exc) from exc
    return DraftRead.model_validate(draft)


@router.post(
    "/drafts/{draft_id}/cancel", status_code=status.HTTP_204_NO_CONTENT, dependencies=OPERATE
)
async def post_cancel_draft(
    draft_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> None:
    # Черновик «просто траты» (без контрагента) — из финансового контура: отменять
    # его правом контрагентского оператора нельзя, нужен уровень создателя.
    draft = await session.get(CounterpartyPaymentDraft, draft_id)
    if draft is not None and draft.counterparty_id is None:
        ensure_permission(actor, "finance.safe.allocate")
    try:
        await payments.cancel_payment_draft(session, draft_id=draft_id)
    except payments.CounterpartyPaymentError as exc:
        raise _conflict(exc) from exc


# --- matching -----------------------------------------------------------------


@router.post("/match/auto", dependencies=OPERATE)
async def post_auto_match(
    session: Annotated[AsyncSession, Depends(get_session)],
    window_days: int | None = None,
) -> dict[str, Any]:
    result = await matching.auto_match_bank_operations(session, window_days=window_days)
    return {
        "matched": len(result["matched"]),
        "needs_review": result["needs_review"],
    }


@router.get("/match/suggestions", response_model=list[MatchSuggestionRead], dependencies=OPERATE)
async def get_match_suggestions(
    session: Annotated[AsyncSession, Depends(get_session)],
    counterparty_id: uuid.UUID | None = None,
) -> list[MatchSuggestionRead]:
    items = await bank_match.suggest_invoice_matches(session, counterparty_id=counterparty_id)
    return [MatchSuggestionRead.model_validate(item) for item in items]


@router.post("/match/confirm", dependencies=OPERATE)
async def post_confirm_match(
    payload: ConfirmMatchRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    try:
        return await bank_match.confirm_invoice_match(
            session,
            invoice_id=payload.invoice_id,
            bank_operation_id=payload.bank_operation_id,
            enrich=payload.enrich,
            actor_user_id=actor.user_id,
        )
    except matching.CounterpartyMatchError as exc:
        raise _conflict(exc) from exc


@router.post(
    "/match/operations/{bank_operation_id}/allocate",
    response_model=DraftRead,
    dependencies=OPERATE,
)
async def post_allocate_operation(
    bank_operation_id: uuid.UUID,
    payload: AllocateOperationRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> DraftRead:
    try:
        draft = await matching.allocate_bank_operation_to_draft(
            session,
            bank_operation_id=bank_operation_id,
            draft_id=payload.draft_id,
            actor_user_id=actor.user_id,
        )
    except matching.CounterpartyMatchError as exc:
        raise _conflict(exc) from exc
    return DraftRead.model_validate(draft)


# --- sync ---------------------------------------------------------------------


@router.post("/sync", dependencies=OPERATE)
async def post_sync(
    session: Annotated[AsyncSession, Depends(get_session)],
    days: int | None = None,
) -> dict[str, Any]:
    window = days or get_settings().counterparty_invoice_sync_days
    try:
        result = await sync_counterparty_invoices(session, days=window, run_reason="manual")
    except Exception as exc:  # noqa: BLE001 - surface upstream failure to the caller
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Синхронизация с iiko не удалась: {exc}",
        ) from exc
    return result.as_dict()

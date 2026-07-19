"""«Управление складом» → накладные. Phase 1: nomenclature picker + manual cache sync.

Reuses the counterparties permission scopes on MVP (a dedicated warehouse.* scope can
be split out later). The picker defaults to GOODS — purchasable raw goods — so the
line-item product search isn't drowned in dishes/modifiers.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    CurrentActor,
    ensure_any_permission,
    ensure_permission,
    get_current_actor,
    require_any_permission,
    require_permission,
)
from app.db.session import get_session
from app.models import IikoProduct, SupplierInvoice, SupplierInvoiceTombstone, Wallet
from app.services.counterparty_bank_match import (
    TimeMatchSuggestion,
    confirm_invoice_match,
    suggest_invoice_matches_by_time,
)
from app.services.counterparty_iiko_payment import (
    IikoPaymentError,
    mark_iiko_payment_settled_manually,
    mirror_single_paid_iiko_invoice,
)
from app.services.counterparty_matching import CounterpartyMatchError, _allocated_amount
from app.services.iiko_product_sync import sync_iiko_products
from app.services.invoice_price_control import (
    PriceControlError,
    assert_price_cleared,
    confirm_prices,
    price_stats_for_product,
)
from app.services.kassa.cheque_payout_push import post_kassa_payment_to_iiko
from app.services.kassa.invoice_paid_push import counterparty_iiko_guid
from app.services.kassa.payouts import kassa_today
from app.services.warehouse_invoice_push import (
    WarehousePushError,
    book_correction_in_iiko,
    delete_invoice_in_iiko,
    propagate_invoice_edit_to_iiko,
    push_invoice_to_iiko,
)
from app.services.warehouse_invoices import (
    LineInput,
    ReturnLineInput,
    WarehouseInvoiceError,
    adjust_paid_invoice,
    create_barter_return,
    create_warehouse_invoice,
    delete_warehouse_invoice,
    ensure_invoice_deletable,
    get_loan_returnable,
    get_warehouse_invoice,
    invoice_permission_kind,
    list_open_loans,
    list_staff_articles,
    list_warehouse_invoices,
    next_invoice_number,
    update_warehouse_invoice,
)
from app.services.warehouse_payments import (
    BankPart,
    CashPart,
    WarehousePaymentError,
    build_auto_split_cash_parts,
    build_staff_split_cash_parts,
    pay_invoice_split,
    resolve_match_params,
)

router = APIRouter()

READ = (Depends(require_permission("counterparties.read")),)
OPERATE = (Depends(require_permission("counterparties.operate")),)
# Справочники для формы накладной — доступны и из контура Кассы:
# право «Создавать накладные из Кассы» само открывает номенклатуру/статьи.
INVOICE_REFS = (
    Depends(require_any_permission(("counterparties.read", "kassa.invoices.create"))),
)
# Отправка накладной в iiko: управляющий/менеджер (counterparties.operate) либо
# кассир через узкое право (накладные из Кассы тоже должны попадать на склад).
PUSH = (
    Depends(require_any_permission(("counterparties.operate", "kassa.invoices.push"))),
)


class LineCreate(BaseModel):
    name: str
    quantity: Decimal
    price: Decimal
    iiko_product_id: uuid.UUID | None = None
    vat_percent: Decimal | None = None
    is_staff: bool = False
    # Статья ДДС персональной строки (питание / прочие затраты на персонал) — задаёт,
    # на какую статью ляжет «персонал»-часть при оплате накладной. Только для is_staff.
    dds_article_id: uuid.UUID | None = None


class InvoiceCreate(BaseModel):
    counterparty_id: uuid.UUID
    issued_at: datetime
    # "normal" — обычная приходная; "loan" — бартер-займ (we_lend: мы выдаём / нам выдают).
    mode: str = "normal"
    we_lend: bool = True
    number: str | None = None
    due_date: date | None = None
    store_guid: str | None = None
    lines: list[LineCreate] = Field(min_length=1)
    # Касса: создать сразу оплаченной — списание с ТК Черникова + проводка оплаты в iiko.
    # paid_amount=None → полная сумма накладной (можно указать меньше для частичной оплаты).
    mark_paid: bool = False
    paid_amount: Decimal | None = Field(default=None, gt=0)
    # Накладная создана через страницу Касса → помечается source="kassa_invoice"
    # (вкладка «Накладные» Кассы показывает только такие; на Складе видны все).
    via_kassa: bool = False


class InvoiceUpdate(BaseModel):
    # Правка позиций неоплаченной накладной (товар + персонал). Контрагент/режим не меняем.
    lines: list[LineCreate] = Field(min_length=1)
    issued_at: datetime | None = None
    number: str | None = None


class ReturnLineCreate(BaseModel):
    amount: Decimal
    loan_line_item_id: uuid.UUID | None = None
    quantity: Decimal | None = None


class ReturnCreate(BaseModel):
    loan_id: uuid.UUID
    issued_at: datetime
    number: str | None = None
    returns: list[ReturnLineCreate] = Field(min_length=1)


class MoneySettleLine(BaseModel):
    """Гашение нашего займа деньгами: кг строки займа × ТЕКУЩАЯ цена (ввод оператора)."""

    loan_line_item_id: uuid.UUID
    quantity: Decimal
    unit_price: Decimal


class MoneySettleRequest(BaseModel):
    """Денежное гашение бартерного займа. Ровно один канал: кошелёк (нал) или банк-операция.

    Наш заём (мы выдали): ``lines`` обязательны — контрагент платит кг × текущая цена.
    Их заём (нам выдали): ``amount`` обязателен — платим «как обычную поставку»."""

    operation_date: date
    wallet_id: uuid.UUID | None = None
    bank_operation_id: uuid.UUID | None = None
    lines: list[MoneySettleLine] | None = None
    amount: Decimal | None = None


class MatchConfirmRequest(BaseModel):
    invoice_id: uuid.UUID
    bank_operation_id: uuid.UUID
    enrich: bool = True
    # Оператор явно подтвердил ручную привязку карт-оплаты к накладной (получатель — эквайер).
    # Пропускает карт-шум в guard; контрагент реквизитами эквайера всё равно не обогащается.
    allow_card: bool = False


class BankPartReq(BaseModel):
    bank_operation_id: uuid.UUID
    amount: Decimal | None = None


class CashPartReq(BaseModel):
    wallet_id: uuid.UUID
    amount: Decimal
    operation_date: date
    article_id: uuid.UUID | None = None
    comment: str | None = None


class KassaPayRequest(BaseModel):
    # Доплата накладной из Кассы. amount=None → весь остаток.
    amount: Decimal | None = Field(default=None, gt=0)


class PaySplitRequest(BaseModel):
    bank_parts: list[BankPartReq] = Field(default_factory=list)
    cash_parts: list[CashPartReq] = Field(default_factory=list)
    # split_staff: pay the whole invoice in cash from ONE wallet, booking the «персонал»
    # part to its own DDS article. Bank parts are not allowed with it (bank lines are
    # classified as one sum), so the staff split stays cash-only on MVP.
    split_staff: bool = False
    # split_by_lines: статью ДДС менеджер не выбирает — каждая наличная строка (Сейф/ТК)
    # разносится по статьям из ПОЗИЦИЙ накладной (build_auto_split_cash_parts). cash-only,
    # поддерживает несколько кошельков и частичную оплату.
    split_by_lines: bool = False


def _serialize_time_suggestion(sug: TimeMatchSuggestion) -> dict[str, Any]:
    return {
        "invoice_id": str(sug.invoice_id),
        "invoice_number": sug.invoice_number,
        "invoice_amount": float(sug.invoice_amount),
        "remaining": float(sug.remaining),
        "issued_at": sug.issued_at.isoformat() if sug.issued_at else None,
        "counterparty_id": str(sug.counterparty_id),
        "counterparty_name": sug.counterparty_name,
        "counterparty_has_inn": sug.counterparty_has_inn,
        "confident": sug.confident,
        "candidates": [
            {
                "bank_operation_id": str(c.bank_operation_id),
                "operation_date": c.operation_date.isoformat(),
                "posted_at": c.posted_at.isoformat() if c.posted_at else None,
                "amount": float(c.amount),
                "official_name": c.official_name,
                "inn": c.inn,
                "requisites": c.requisites,
                "tier": c.tier,
                "minutes_delta": c.minutes_delta,
            }
            for c in sug.candidates
        ],
    }


@router.get("/products", dependencies=INVOICE_REFS)
async def list_products(
    session: Annotated[AsyncSession, Depends(get_session)],
    q: str | None = None,
    type: str = "GOODS",
    include_deleted: bool = False,
    limit: int = Query(default=500, le=2000),
) -> list[dict[str, Any]]:
    """Nomenclature picker. ``type=''`` shows every type; defaults to GOODS."""
    query = select(IikoProduct)
    if type:
        query = query.where(IikoProduct.type == type.upper())
    if not include_deleted:
        query = query.where(IikoProduct.deleted.is_(False))
    if q:
        pattern = f"%{q.strip().lower()}%"
        query = query.where(
            func.lower(IikoProduct.name).like(pattern)
            | func.lower(func.coalesce(IikoProduct.code, "")).like(pattern)
        )
    query = query.order_by(IikoProduct.name).limit(limit)
    products = (await session.scalars(query)).all()
    return [
        {
            "id": str(p.id),
            "iiko_id": p.iiko_id,
            "name": p.name,
            "code": p.code,
            "unit": p.unit,
            "type": p.type,
        }
        for p in products
    ]


@router.get("/products/{product_id}/price-stats", dependencies=INVOICE_REFS)
async def get_product_price_stats(
    product_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Скользящее среднее цены товара + пороги — для живой подсветки отклонения при вводе строки."""
    return await price_stats_for_product(session, product_id=product_id)


@router.post("/products/sync", dependencies=OPERATE)
async def post_products_sync(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Refresh the nomenclature cache from iiko `/v2/entities/products/list`."""
    result = await sync_iiko_products(session)
    return {
        "seen": result.seen,
        "created": result.created,
        "updated": result.updated,
        "goods_count": result.goods_count,
        "needs_unit_mapping": result.needs_unit_mapping,
    }


@router.get("/invoices/next-number", dependencies=INVOICE_REFS)
async def invoice_next_number(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    return {"number": await next_invoice_number(session)}


@router.get("/staff-articles", dependencies=INVOICE_REFS)
async def staff_articles(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, str]]:
    """Статьи ДДС для блока «Траты на персонал» накладной (питание / прочие затраты)."""
    return [{"id": str(a.id), "name": a.name} for a in await list_staff_articles(session)]


@router.get("/loans", dependencies=READ)
async def list_loans(
    session: Annotated[AsyncSession, Depends(get_session)],
    counterparty_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Open / partially-returned barter loans — для режима «Возврат»."""
    return await list_open_loans(session, counterparty_id)


@router.get("/loans/{loan_id}/returnable", dependencies=READ)
async def loan_returnable(
    loan_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    loan = await get_loan_returnable(session, loan_id)
    if loan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Заём не найден")
    return loan


@router.post("/invoices/return", status_code=status.HTTP_201_CREATED)
async def post_return(
    payload: ReturnCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    ensure_permission(actor, "invoices.barter.create")
    try:
        ret = await create_barter_return(
            session,
            loan_id=payload.loan_id,
            issued_at=payload.issued_at,
            number=payload.number,
            returns=[
                ReturnLineInput(
                    amount=line.amount,
                    loan_line_item_id=line.loan_line_item_id,
                    quantity=line.quantity,
                )
                for line in payload.returns
            ],
            actor_user_id=actor.user_id,
        )
    except WarehouseInvoiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    # Возврат — тоже складское движение: уходит в iiko автоматом (модель владельца 18.07 —
    # наша система узнаёт о возврате первой, iiko только зеркалит). Сбой не валит возврат.
    with contextlib.suppress(WarehousePushError):
        await push_invoice_to_iiko(session, ret.id)
    result = await get_warehouse_invoice(session, ret.id)
    assert result is not None
    return result


@router.post("/invoices/{invoice_id}/mark-loan")
async def post_mark_invoice_as_loan(
    invoice_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    """Признать унаследованную бартерную накладную явным займом — открывает штатное гашение."""
    ensure_permission(actor, "invoices.barter.create")
    from app.services.warehouse_invoices import convert_invoice_to_barter_loan

    try:
        invoice = await convert_invoice_to_barter_loan(session, invoice_id)
    except WarehouseInvoiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return {"id": str(invoice.id), "barter_role": invoice.barter_role}


@router.get("/barter/partners", dependencies=READ)
async def list_barter_partners(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, Any]]:
    """Вкладка «Бартер»: партнёры одной строкой — нам должны / мы должны / нетто."""
    from app.services.counterparty_barter_match import barter_partner_balances

    return await barter_partner_balances(session)


@router.post("/loans/{loan_id}/settle-money", status_code=status.HTTP_201_CREATED)
async def post_loan_money_settlement(
    loan_id: uuid.UUID,
    payload: MoneySettleRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    """Денежное гашение бартерного займа: наш заём — контрагент платит кг × текущая цена;
    их заём — мы платим по накладной. Канал: нал (кошелёк) или банк-операция."""
    ensure_permission(actor, "invoices.barter.create")
    from app.services.barter_loan_money import (
        MoneyReturnLine,
        pay_payable_loan_with_money,
        settle_receivable_loan_with_money,
    )

    loan = await session.get(SupplierInvoice, loan_id)
    if loan is None or loan.barter_role != "loan":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Заём не найден")
    try:
        if loan.direction == "receivable":
            if not payload.lines:
                raise WarehouseInvoiceError("Укажите позиции: кг × текущая цена")
            return await settle_receivable_loan_with_money(
                session,
                loan_id=loan_id,
                operation_date=payload.operation_date,
                lines=[
                    MoneyReturnLine(
                        loan_line_item_id=line.loan_line_item_id,
                        quantity=line.quantity,
                        unit_price=line.unit_price,
                    )
                    for line in payload.lines
                ],
                wallet_id=payload.wallet_id,
                bank_operation_id=payload.bank_operation_id,
                actor_user_id=actor.user_id,
            )
        if payload.amount is None:
            raise WarehouseInvoiceError("Укажите сумму оплаты займа")
        return await pay_payable_loan_with_money(
            session,
            loan_id=loan_id,
            operation_date=payload.operation_date,
            amount=payload.amount,
            wallet_id=payload.wallet_id,
            bank_operation_id=payload.bank_operation_id,
            actor_user_id=actor.user_id,
        )
    except WarehouseInvoiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


@router.get("/invoices", dependencies=READ)
async def list_invoices(
    session: Annotated[AsyncSession, Depends(get_session)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    counterparty_id: uuid.UUID | None = None,
    has_staff: bool | None = None,
) -> list[dict[str, Any]]:
    statuses = [s for s in (status_filter or "").split(",") if s] or None
    return await list_warehouse_invoices(
        session, statuses=statuses, counterparty_id=counterparty_id, has_staff=has_staff
    )


@router.post("/invoices/{invoice_id}/push", dependencies=PUSH)
async def post_push(
    invoice_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Отправить (или повторить отправку) накладную в iiko. Создаёт реальный документ."""
    try:
        await push_invoice_to_iiko(session, invoice_id)
    except WarehousePushError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    result = await get_warehouse_invoice(session, invoice_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Накладная не найдена")
    return result


@router.get("/invoices/{invoice_id}", dependencies=READ)
async def get_invoice(
    invoice_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    invoice = await get_warehouse_invoice(session, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Накладная не найдена")
    return invoice


@router.get("/invoices/{invoice_id}/match-suggestions", dependencies=READ)
async def get_match_suggestions(
    invoice_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    window_hours: int | None = None,
    tolerance_pct: float | None = None,
    tight_minutes: int | None = None,
) -> dict[str, Any]:
    """Кандидаты банк-операций по дате+времени чека (issued_at ↔ posted_at)."""
    window, tol, tight = await resolve_match_params(
        session,
        window_hours=window_hours,
        amount_tolerance_pct=tolerance_pct,
        tight_window_minutes=tight_minutes,
    )
    sug = await suggest_invoice_matches_by_time(
        session,
        invoice_id=invoice_id,
        window_hours=window,
        amount_tolerance_pct=tol,
        tight_window_minutes=tight,
    )
    if sug is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Накладная не поддерживает сверку с банком",
        )
    return _serialize_time_suggestion(sug)


@router.post("/match/confirm")
async def post_match_confirm(
    payload: MatchConfirmRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    """Подтвердить мэтч накладной с банк-операцией (переиспользует общий банк-мэтч).
    Сверка с выпиской = часть оплаты → право invoices.{normal|barter}.pay по накладной."""
    invoice = await session.get(SupplierInvoice, payload.invoice_id)
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Накладная не найдена")
    ensure_permission(actor, f"invoices.{await invoice_permission_kind(session, invoice)}.pay")
    try:
        return await confirm_invoice_match(
            session,
            invoice_id=payload.invoice_id,
            bank_operation_id=payload.bank_operation_id,
            enrich=payload.enrich,
            actor_user_id=actor.user_id,
            allow_card=payload.allow_card,
        )
    except CounterpartyMatchError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/invoices/{invoice_id}/pay-split")
async def post_pay_split(
    invoice_id: uuid.UUID,
    payload: PaySplitRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    """Оплатить накладную из нескольких источников (банк + касса) одной транзакцией.
    При ``split_staff`` оплата налом из одного кошелька разносится на производство/персонал."""
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Накладная не найдена")
    ensure_permission(actor, f"invoices.{await invoice_permission_kind(session, invoice)}.pay")
    # Контроль цен: подозрительную (неподтверждённую) накладную оплачивать нельзя.
    try:
        assert_price_cleared(invoice)
    except PriceControlError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    bank_parts = [
        BankPart(bank_operation_id=b.bank_operation_id, amount=b.amount) for b in payload.bank_parts
    ]
    cash_parts = [
        CashPart(
            wallet_id=c.wallet_id,
            amount=c.amount,
            operation_date=c.operation_date,
            article_id=c.article_id,
            comment=c.comment,
        )
        for c in payload.cash_parts
    ]
    try:
        if payload.split_by_lines:
            # Статью ДДС менеджер не выбирает — каждая наличная строка разносится по статьям
            # из позиций накладной. Поддерживает несколько кошельков и частичную оплату.
            if payload.bank_parts:
                raise WarehousePaymentError(
                    "Разнесение по позициям доступно только при оплате наличными/со счёта"
                )
            if not payload.cash_parts:
                raise WarehousePaymentError("Укажите хотя бы один источник оплаты")
            cash_parts = await build_auto_split_cash_parts(
                session,
                invoice,
                rows=[(c.wallet_id, c.amount, c.operation_date) for c in payload.cash_parts],
            )
            bank_parts = []
        elif payload.split_staff:
            if payload.bank_parts:
                raise WarehousePaymentError(
                    "Разнесение персонала доступно только при оплате наличными"
                )
            if len(payload.cash_parts) != 1:
                raise WarehousePaymentError(
                    "Для разнесения персонала укажите один наличный источник"
                )
            # build_staff_split_cash_parts разносит ПОЛНУЮ сумму накладной — на частично
            # оплаченной Σ частей всегда > остаток → 409. Поэтому только для неоплаченной.
            if invoice.payment_status != "unpaid":
                raise WarehousePaymentError(
                    "Разнести персонал можно только для полностью неоплаченной накладной"
                )
            c0 = payload.cash_parts[0]
            cash_parts = await build_staff_split_cash_parts(
                session,
                invoice,
                wallet_id=c0.wallet_id,
                operation_date=c0.operation_date,
                comment=c0.comment,
            )
        await pay_invoice_split(
            session,
            invoice_id=invoice_id,
            bank_parts=bank_parts,
            cash_parts=cash_parts,
            actor_user_id=actor.user_id,
        )
    except (WarehousePaymentError, CounterpartyMatchError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    result = await get_warehouse_invoice(session, invoice_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Накладная не найдена")
    return result


async def _settle_paid_from_kassa(
    session: AsyncSession,
    invoice: SupplierInvoice,
    paid_amount: Decimal | None,
    actor_user_id: uuid.UUID | None,
    *,
    do_push: bool = True,
) -> None:
    """Оплатить накладную из контура Кассы: (опц.) документ в iiko, списание наличных
    с ТК Черникова (ДДС) и проводка оплаты поставщику в iiko (изъятие). ``do_push=False`` —
    доплата уже существующей накладной (она была отправлена в iiko ранее)."""
    amount = paid_amount if paid_amount is not None else invoice.amount
    # 1) Документ в iiko (incomingInvoice → товар + долг). prepare_push исключает строки
    #    «персонал» → в iiko уходит только товарная часть. Сбой push накладную не валит.
    if do_push:
        with contextlib.suppress(WarehousePushError):
            await push_invoice_to_iiko(session, invoice.id)
    # 2) ДДС: оплата наличными со счёта ТК Черникова → статус накладной paid/partially_paid.
    wallet = await session.scalar(select(Wallet).where(Wallet.code == "tk_chernikova"))
    if wallet is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Кошелёк ТК Черникова не найден",
        )
    # Дата расхода — КАЛЕНДАРНЫЙ ДЕНЬ МСК (kassa_today), не date.today(): контейнер API живёт
    # в UTC, и с 00:00 до 03:00 по Москве расход падал во ВЧЕРАШНИЙ, уже сверенный с iiko день.
    # «Траты на персонал» — отдельная статья ДДС: полную оплату разносим на производство/
    # персонал по статьям строк (build_staff_split_cash_parts). Без персонала (или доплата
    # уже частично оплаченной) — одна часть.
    staff_total = invoice.staff_amount or Decimal("0.00")
    if staff_total > 0 and invoice.payment_status == "unpaid":
        cash_parts = await build_staff_split_cash_parts(
            session, invoice, wallet_id=wallet.id, operation_date=kassa_today()
        )
    else:
        cash_parts = [
            CashPart(wallet_id=wallet.id, amount=amount, operation_date=kassa_today())
        ]
    await pay_invoice_split(
        session,
        invoice_id=invoice.id,
        cash_parts=cash_parts,
        actor_user_id=actor_user_id,
    )
    # 3) iiko: изъятия оплаты ПЕРСОНАЛЬНЫХ/прочих статей (персонал → свои статьи), счёт по способу
    #    оплаты (нал → Главная касса). ТОВАРНУЮ часть («Оплата поставщикам») здесь НЕ проводим
    #    (skip_supplier=True) — её гасит правильная «оплата накладной» add_payment сверочным джобом
    #    mirror_paid_kassa_invoices, чтобы товар не задваивался. Побочно — ошибка iiko не валит
    #    оплату (статусы в kassa_cheque_iiko_payout).
    await post_kassa_payment_to_iiko(session, invoice.id, skip_supplier=True)
    await session.commit()


@router.post("/invoices/{invoice_id}/pay-kassa")
async def post_pay_kassa(
    invoice_id: uuid.UUID,
    payload: KassaPayRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    """Доплатить уже созданную накладную из Кассы: списание с ТК Черникова + проводка
    оплаты в iiko. Накладная отправлена в iiko ранее — push не повторяем (защита от дубля)."""
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Накладная не найдена")
    ensure_any_permission(actor, ("kassa.invoices.create", "invoices.normal.pay"))
    if invoice.payment_status == "paid":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Накладная уже оплачена")
    # Контроль цен: подозрительную (неподтверждённую) накладную оплачивать нельзя.
    try:
        assert_price_cleared(invoice)
    except PriceControlError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if not await counterparty_iiko_guid(session, invoice.counterparty_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Контрагент не сматчен с iiko — оплату провести нельзя",
        )
    try:
        await _settle_paid_from_kassa(
            session, invoice, payload.amount, actor.user_id, do_push=False
        )
    except (WarehousePaymentError, CounterpartyMatchError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    result = await get_warehouse_invoice(session, invoice_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Накладная не найдена")
    return result


@router.post("/invoices", status_code=status.HTTP_201_CREATED)
async def post_invoice(
    payload: InvoiceCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    if payload.mode == "loan":
        ensure_permission(actor, "invoices.barter.create")
    else:
        # Обычную накладную может создать и кассир из контура Кассы.
        ensure_any_permission(actor, ("invoices.normal.create", "kassa.invoices.create"))
    # «Оплачено» из Кассы требует сматченного с iiko контрагента — иначе оплату не провести.
    if (
        payload.mark_paid
        and payload.mode != "loan"
        and not await counterparty_iiko_guid(session, payload.counterparty_id)
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Контрагент не сматчен с iiko — оплату провести нельзя",
        )
    # Идемпотентность Кассы: тот же номер к тому же контрагенту = повторное «Создать» (после
    # ошибки/двойного клика/двух вкладок). Не плодим дубль и не списываем наличные дважды.
    if payload.via_kassa and payload.number and payload.mode != "loan":
        # Двойной клик/две вкладки = тот же номер тому же контрагенту в ту же дату. Скоуп по
        # дате (ключ iiko = номер+дата): тот же печатный номер в РАЗНЫЙ день — это другая
        # накладная, не дубль, блокировать её нельзя. Авто-суффикс «N-2» здесь не ловим —
        # это узкий edge (кросс-контрагентная коллизия + повторный клик), фронт гасит кнопку.
        dup_id = await session.scalar(
            select(SupplierInvoice.id).where(
                SupplierInvoice.source == "kassa_invoice",
                SupplierInvoice.counterparty_id == payload.counterparty_id,
                SupplierInvoice.invoice_date == payload.issued_at.date(),
                SupplierInvoice.number == payload.number,
            )
        )
        if dup_id is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Накладная №{payload.number} уже создана — обновите список",
            )
    try:
        invoice = await create_warehouse_invoice(
            session,
            counterparty_id=payload.counterparty_id,
            issued_at=payload.issued_at,
            mode=payload.mode,
            we_lend=payload.we_lend,
            number=payload.number,
            due_date=payload.due_date,
            store_guid=payload.store_guid,
            lines=[
                LineInput(
                    name=line.name,
                    quantity=line.quantity,
                    price=line.price,
                    iiko_product_id=line.iiko_product_id,
                    vat_percent=line.vat_percent,
                    is_staff=line.is_staff,
                    dds_article_id=line.dds_article_id,
                )
                for line in payload.lines
            ],
            actor_user_id=actor.user_id,
            source="kassa_invoice" if payload.via_kassa else "manual",
        )
    except WarehouseInvoiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    # Наша накладная (Касса ИЛИ Склад) уходит в iiko СРАЗУ при создании — ручное «Отправить в
    # iiko» больше не нужно ни на одном контуре: правка и удаление теперь пробрасываются в iiko
    # (Cloud update/delete), «доводить документ до официального» вручную незачем. БАРТЕР ТОЖЕ
    # (модель владельца 18.07): в iiko накладные не создаются вообще — все документы, включая
    # займы и возвраты, рождаются у нас и уходят туда зеркалом (наша система узнаёт о движении
    # ПЕРВОЙ). Пушим товарную часть; prepare_push сам пропустит накладную без товара
    # (→ skipped). Сбой push не валит создание — статус фиксируется в iiko_push_status,
    # кнопка «Переотправить в iiko» в деталях остаётся для ретрая.
    with contextlib.suppress(WarehousePushError):
        await push_invoice_to_iiko(session, invoice.id)
    if payload.mark_paid and payload.mode != "loan":
        # Документ уже отправлен выше → оплату проводим без повторного push (do_push=False).
        await _settle_paid_from_kassa(
            session, invoice, payload.paid_amount, actor.user_id, do_push=False
        )
    result = await get_warehouse_invoice(session, invoice.id)
    assert result is not None
    return result


@router.put("/invoices/{invoice_id}")
async def put_invoice(
    invoice_id: uuid.UUID,
    payload: InvoiceUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    """Правка позиций неоплаченной (не бартерной) накладной — «переделать и отправить в iiko».
    Контрагент и режим не меняются; пересчитываются суммы/персонал и сбрасывается статус
    отправки в iiko, чтобы исправленную накладную можно было запушить."""
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Накладная не найдена")
    kind = await invoice_permission_kind(session, invoice)
    ensure_any_permission(actor, (f"invoices.{kind}.edit", "kassa.invoices.create"))
    try:
        await update_warehouse_invoice(
            session,
            invoice,
            lines=[
                LineInput(
                    name=line.name,
                    quantity=line.quantity,
                    price=line.price,
                    iiko_product_id=line.iiko_product_id,
                    vat_percent=line.vat_percent,
                    is_staff=line.is_staff,
                    dds_article_id=line.dds_article_id,
                )
                for line in payload.lines
            ],
            issued_at=payload.issued_at,
            number=payload.number,
        )
    except WarehouseInvoiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    # Уже выгруженную в iiko накладную сразу синхронизируем правкой (Cloud update→post); не-фатально
    # (ошибка iiko не роняет правку — фиксируется в iiko_push_status/iiko_push_error).
    if invoice.external_id:
        await propagate_invoice_edit_to_iiko(session, invoice_id)
    result = await get_warehouse_invoice(session, invoice_id)
    assert result is not None
    return result


@router.post("/invoices/{invoice_id}/adjust-paid")
async def post_adjust_paid_invoice(
    invoice_id: uuid.UUID,
    payload: InvoiceUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    """Исправить УЖЕ ОПЛАЧЕННУЮ накладную (поставщик прислал не ту, а её успели провести и
    оплатить): заменить позиции/сумму; излишек оплаты уходит в дебиторку поставщику. Отдельное
    точечное право ``invoices.normal.edit_paid`` (owner/admin). iiko-документ здесь НЕ трогаем —
    оплаченный iiko не даёт распровести/править; коррекция в iiko отдельным шагом (возвратная
    накладная), см. project_edit_paid_invoice_feasibility."""
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Накладная не найдена")
    if (await invoice_permission_kind(session, invoice)) == "barter":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Бартерную накладную так исправлять нельзя",
        )
    ensure_permission(actor, "invoices.normal.edit_paid")
    allocated_before = await _allocated_amount(session, invoice_id)
    try:
        await adjust_paid_invoice(
            session,
            invoice,
            lines=[
                LineInput(
                    name=line.name,
                    quantity=line.quantity,
                    price=line.price,
                    iiko_product_id=line.iiko_product_id,
                    vat_percent=line.vat_percent,
                    is_staff=line.is_staff,
                    dds_article_id=line.dds_article_id,
                )
                for line in payload.lines
            ],
            issued_at=payload.issued_at,
            number=payload.number,
            actor_user_id=actor.user_id,
        )
    except WarehouseInvoiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    # Отразить коррекцию в iiko — полный разворот: возврат старого прихода + новая приходная + зачёт
    # (Фаза 2). Не-фатально: ошибка iiko не роняет коррекцию, статус садится в накладную, ждёт ретрая.
    if invoice.external_id and invoice.iiko_return_status == "pending":
        await book_correction_in_iiko(session, invoice_id)
    result = await get_warehouse_invoice(session, invoice_id)
    assert result is not None
    # Сколько излишка оплаты ушло в дебиторку — фронту для тоста «… ₽ в дебиторку».
    result["moved_to_receivable"] = float(
        max(Decimal("0"), allocated_before - Decimal(str(result["amount"])))
    )
    return result


@router.post("/invoices/{invoice_id}/confirm-prices")
async def post_confirm_prices(
    invoice_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    """«ОК, всё верно» — подтвердить подозрительные цены накладной и разблокировать оплату.

    Отдельное точечное право ``invoices.confirm_price`` (owner/admin/менеджер): разделяет «кто
    платит» и «кто одобряет ценовую аномалию». Пока не подтверждено — оплата и отправка в банк
    заблокированы. Правка позиций сбрасывает подтверждение (цены нужно сверить заново)."""
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Накладная не найдена")
    ensure_permission(actor, "invoices.confirm_price")
    try:
        await confirm_prices(session, invoice, actor_user_id=actor.user_id)
    except PriceControlError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    result = await get_warehouse_invoice(session, invoice_id)
    assert result is not None
    return result


@router.post("/invoices/{invoice_id}/retry-iiko-return")
async def post_retry_iiko_return(
    invoice_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    """Повторить проводку коррекции в iiko после сбоя (сага продолжится с последнего успешного
    шага: возврат → новая приходная → зачёт; чек-пойнты хранятся в накладной)."""
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Накладная не найдена")
    ensure_permission(actor, "invoices.normal.edit_paid")
    await book_correction_in_iiko(session, invoice_id)
    result = await get_warehouse_invoice(session, invoice_id)
    assert result is not None
    return result


@router.post("/invoices/{invoice_id}/send-iiko-payment")
async def post_send_iiko_payment(
    invoice_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    """«Отправить оплату в iiko сейчас»: провести ``add_payment`` для оплаченной iiko-накладной, не
    дожидаясь 5-минутного сверочного джоба. Нужно перед правкой оплаченной, если оплата оригинала
    ещё не отражена в iiko. Идемпотентно: если платёж уже в iiko (в т.ч. проведён вручную в
    бэк-офисе) — iiko вернёт «already paid», зафиксируем. Право ``invoices.normal.edit_paid``."""
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Накладная не найдена")
    ensure_permission(actor, "invoices.normal.edit_paid")
    try:
        res = await mirror_single_paid_iiko_invoice(
            session, invoice_id, actor_user_id=actor.user_id
        )
    except IikoPaymentError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    result = await get_warehouse_invoice(session, invoice_id)
    assert result is not None
    # Не удалось (iiko отклонил / сеть) — фронту текст, чтобы предложить ручное подтверждение.
    if not res.ok:
        result["iiko_payment_push_error"] = res.error or "Оплата в iiko не прошла — повторите позже"
    return result


@router.post("/invoices/{invoice_id}/confirm-iiko-payment")
async def post_confirm_iiko_payment(
    invoice_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict[str, Any]:
    """«Оплата подтверждена вручную»: человек утверждает, что платёж уже проведён в бэк-офисе iiko.
    Пишем ok-маркер зеркала — гард правки проходит, а сверочный джоб больше не шлёт ``add_payment``
    (без риска задвоить уже проведённый вручную платёж). Право ``invoices.normal.edit_paid``."""
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Накладная не найдена")
    ensure_permission(actor, "invoices.normal.edit_paid")
    try:
        await mark_iiko_payment_settled_manually(session, invoice, actor_user_id=actor.user_id)
    except IikoPaymentError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    result = await get_warehouse_invoice(session, invoice_id)
    assert result is not None
    return result


@router.delete("/invoices/{invoice_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_invoice(
    invoice_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> None:
    """Удалить неоплаченную накладную с обеих сторон. Если она есть в iiko (``external_id``) —
    сначала удаляем ТАМ (unpost→cancel): иначе реверс-синк воскресил бы её из живого
    PROCESSED-документа. Отказ iiko → 409, локально ничего не трогаем. Тумбстон закрывает
    гонку с синком, читающим старый снапшот."""
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Накладная не найдена")
    kind = await invoice_permission_kind(session, invoice)
    ensure_any_permission(actor, (f"invoices.{kind}.edit", "kassa.invoices.create"))
    try:
        ensure_invoice_deletable(invoice)
    except WarehouseInvoiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    if invoice.external_id:
        from app.services.iiko_sync import _load_source_credential_env

        await _load_source_credential_env(session)
        error = await delete_invoice_in_iiko(invoice)
        if error is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"iiko не удалил документ: {error}",
            )
        session.add(
            SupplierInvoiceTombstone(
                source=invoice.source,
                external_id=invoice.external_id,
                reason="удалена пользователем из приложения (двустороннее удаление)",
            )
        )

    try:
        await delete_warehouse_invoice(session, invoice)
    except WarehouseInvoiceError as exc:  # pragma: no cover — гейты проверены выше
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

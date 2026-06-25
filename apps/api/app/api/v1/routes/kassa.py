from __future__ import annotations

import contextlib
import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, ensure_permission, get_current_actor, require_permission
from app.db.session import get_session
from app.models import Counterparty, DdsArticle, IikoCashShift, Wallet
from app.schemas.kassa import (
    CardTransactionRead,
    ChequeCreate,
    ChequeRead,
    KassaAccountRead,
    KassaConfigRead,
    KassaCounterpartyRead,
    KassaDdsArticleRead,
    KassaShiftDetailRead,
    KassaShiftRead,
    KassaShiftSyncReport,
)
from app.services.kassa.cheque import (
    ChequeBankPart,
    ChequeLineInput,
    KassaChequeError,
    create_cheque,
    get_cheque,
    list_card_transactions,
    list_cheque_articles,
    list_cheques,
)
from app.services.kassa.cheque_payout_push import post_kassa_payment_to_iiko
from app.services.kassa.iiko_cashshift_sync import (
    get_shift,
    list_shifts,
    post_shift_adjustment,
    post_shift_cash_circuit,
    sync_iiko_cashshifts,
    waive_shift_penalty,
)
from app.services.settings_service import SettingNotFoundError, get_setting
from app.services.warehouse_invoice_push import WarehousePushError, push_invoice_to_iiko

router = APIRouter()
logger = logging.getLogger(__name__)

# Фиче-флаг: блок ручного ввода суммы чека («ожидает подтверждения банком»). По умолчанию
# выключен (карт-операции приходят вебхуком) — включается тумблером в Настройках → Касса.
MANUAL_PENDING_SETTING_KEY = "kassa.manual_pending_cheque_enabled"


async def _manual_pending_enabled(session: AsyncSession) -> bool:
    """Прочитать флаг ручного pending. Отсутствие настройки = ВЫКЛ (безопасный дефолт)."""
    try:
        setting = await get_setting(session, MANUAL_PENDING_SETTING_KEY)
    except SettingNotFoundError:
        return False
    return bool(setting.get("value"))

# Служебный контрагент-«корзина» местных закупок: жёстко зашит получателем чека по
# бизнес-карте. Он синтетический (без ИНН/реквизитов), поэтому может оказаться в статусе
# requires_setup — но в списке для чека должен быть ВСЕГДА, иначе кнопка «Создать чек»
# молча гаснет (получатель не находится). Поэтому отдаём его независимо от статуса.
LOCAL_PURCHASE_NAME = "Местный закуп"

KASSA_REFS_READ = (Depends(require_permission("kassa.refs.read")),)
KASSA_CHEQUES_READ = (Depends(require_permission("kassa.cheques.read")),)
KASSA_SHIFTS_READ = (Depends(require_permission("kassa.shifts.read")),)
KASSA_SHIFTS_SYNC = (Depends(require_permission("kassa.shifts.sync")),)
KASSA_SHIFTS_POST = (Depends(require_permission("kassa.shifts.post")),)
KASSA_ADJUSTMENTS_CREATE = (Depends(require_permission("kassa.adjustments.create")),)
KASSA_PENALTY_WAIVE = (Depends(require_permission("kassa.penalty.waive")),)


@router.get("/config", response_model=KassaConfigRead, dependencies=KASSA_REFS_READ)
async def get_kassa_config(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """Фиче-флаги Кассы для UI (например, показывать ли ручной ввод суммы чека)."""
    return {"manual_pending_cheque_enabled": await _manual_pending_enabled(session)}


@router.get(
    "/dds-articles",
    response_model=list[KassaDdsArticleRead],
    dependencies=KASSA_REFS_READ,
)
async def list_dds_articles(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[DdsArticle]:
    """Статьи ДДС для позиций чека — только белый список (4 статьи местного закупа)."""
    return await list_cheque_articles(session)


@router.get(
    "/accounts",
    response_model=list[KassaAccountRead],
    dependencies=KASSA_REFS_READ,
)
async def list_accounts(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[Wallet]:
    """Активные счета-кошельки (для отображения счёта оплаты, без остатков)."""
    result = await session.scalars(
        select(Wallet).where(Wallet.status == "active").order_by(Wallet.name)
    )
    return list(result.all())


@router.get(
    "/counterparties",
    response_model=list[KassaCounterpartyRead],
    dependencies=KASSA_REFS_READ,
)
async def list_counterparties(
    session: Annotated[AsyncSession, Depends(get_session)],
    search: str | None = None,
) -> list[Counterparty]:
    """Контрагенты чека (магазин/поставщик): активные + всегда служебный «Местный закуп».

    «Местный закуп» отдаём независимо от статуса (он синтетический, может быть
    requires_setup) — иначе кнопка «Создать чек» гаснет без объяснения."""
    available = or_(
        Counterparty.status == "active", Counterparty.name == LOCAL_PURCHASE_NAME
    )
    conditions = [available]
    if search:
        pattern = f"%{search.strip()}%"
        conditions.append(or_(Counterparty.name.ilike(pattern), Counterparty.inn.ilike(pattern)))
    result = await session.scalars(
        select(Counterparty).where(*conditions).order_by(Counterparty.name)
    )
    return list(result.all())


@router.get(
    "/card-transactions",
    response_model=list[CardTransactionRead],
    dependencies=KASSA_CHEQUES_READ,
)
async def list_card_transactions_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
    issued_at: datetime | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    window_hours: int = Query(48, ge=1, le=720),
):
    """Card-покупки T-Bank для выбора в чеке (около даты чека либо за диапазон)."""
    try:
        return await list_card_transactions(
            session,
            issued_at=issued_at,
            date_from=date_from,
            date_to=date_to,
            window_hours=window_hours,
        )
    except KassaChequeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/cheques", response_model=list[ChequeRead], dependencies=KASSA_CHEQUES_READ)
async def list_cheques_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    return await list_cheques(session, limit=limit, offset=offset)


@router.get("/cheques/{cheque_id}", response_model=ChequeRead, dependencies=KASSA_CHEQUES_READ)
async def get_cheque_endpoint(
    cheque_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    payload = await get_cheque(session, cheque_id)
    if payload is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Чек не найден")
    return payload


@router.post("/cheques", response_model=ChequeRead, status_code=status.HTTP_201_CREATED)
async def create_cheque_endpoint(
    payload: ChequeCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    ensure_permission(actor, "kassa.cheques.create")
    # Серверный гард kill-switch: ручной pending принимаем, только если включён флаг в
    # Настройках. Иначе блокируем даже при устаревшем клиенте / прямом вызове API.
    if payload.pending_card_amount is not None and not await _manual_pending_enabled(session):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ручной ввод суммы чека отключён в настройках Кассы",
        )
    try:
        invoice = await create_cheque(
            session,
            counterparty_id=payload.counterparty_id,
            article_id=payload.article_id,
            issued_at=payload.issued_at,
            bank_parts=[
                ChequeBankPart(bank_operation_id=part.bank_operation_id, amount=part.amount)
                for part in payload.bank_parts
            ],
            cash_amount=payload.cash_amount,
            pending_card_amount=payload.pending_card_amount,
            track_nomenclature=payload.track_nomenclature,
            lines=[
                ChequeLineInput(
                    name=line.name,
                    quantity=line.quantity,
                    price=line.price,
                    unit=line.unit,
                    dds_article_id=line.dds_article_id,
                    iiko_product_id=line.iiko_product_id,
                    vat_percent=line.vat_percent,
                    amount=line.amount,
                )
                for line in payload.lines
            ],
            number=payload.number,
            store_guid=payload.store_guid,
            comment=payload.comment,
            actor_user_id=actor.user_id,
        )
    except KassaChequeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    # Складские позиции (с товаром) уходят в iiko приходной накладной от «Местного закупа»;
    # прочие расходы без товара push пропускает сам. Ошибка/skip не отменяет созданный чек.
    with contextlib.suppress(WarehousePushError):
        await push_invoice_to_iiko(session, invoice.id)
    # ПЕРСОНАЛЬНЫЕ/прочие статьи чека (питание/содержание) дублируем в iiko изъятиями addPayOut
    # по статьям, счёт по способу оплаты (наличные→Главная касса, карта→эквайринг). ТОВАРНУЮ часть
    # («Оплата поставщикам») здесь НЕ проводим (skip_supplier=True) — её гасит правильная «оплата
    # накладной» add_payment сверочным джобом mirror_paid_kassa_invoices (≤5 мин), чтобы товар не
    # задваивался. Идемпотентно; сбой проводки фиксируется в kassa_cheque_iiko_payout.
    try:
        await post_kassa_payment_to_iiko(session, invoice.id, skip_supplier=True)
        await session.commit()
    except Exception:  # noqa: BLE001 — iiko-проводка побочна, чек уже создан и закоммичен
        await session.rollback()
        logger.exception("Чек %s: не удалось провести прочие расходы в iiko", invoice.id)
    payload_out = await get_cheque(session, invoice.id)
    assert payload_out is not None  # noqa: S101 - just created it
    return payload_out


@router.get("/shifts", response_model=list[KassaShiftRead], dependencies=KASSA_SHIFTS_READ)
async def list_shifts_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
    date_from: date | None = None,
    date_to: date | None = None,
) -> list:
    return await list_shifts(session, date_from=date_from, date_to=date_to)


@router.get(
    "/shifts/{shift_id}", response_model=KassaShiftDetailRead, dependencies=KASSA_SHIFTS_READ
)
async def get_shift_endpoint(
    shift_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    payload = await get_shift(session, shift_id)
    if payload is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Смена не найдена")
    return payload


@router.post("/shifts/sync", response_model=KassaShiftSyncReport, dependencies=KASSA_SHIFTS_SYNC)
async def sync_shifts_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict:
    """Подтянуть закрытые смены iiko за период (по умолчанию now−2д..now+1д)."""
    today = date.today()
    range_from = date_from or today - timedelta(days=2)
    range_to = date_to or today + timedelta(days=1)
    try:
        report = await sync_iiko_cashshifts(session, date_from=range_from, date_to=range_to)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Ошибка iiko: {exc}"
        ) from exc
    await session.commit()
    return report.as_dict()


@router.post(
    "/shifts/{shift_id}/post", response_model=KassaShiftDetailRead, dependencies=KASSA_SHIFTS_POST
)
async def post_shift_endpoint(
    shift_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """Вручную провести наличный контур смены в ДДС (идемпотентно)."""
    shift = await session.get(IikoCashShift, shift_id)
    if shift is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Смена не найдена")
    try:
        await post_shift_cash_circuit(session, shift)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    payload = await get_shift(session, shift_id)
    assert payload is not None  # noqa: S101 - shift exists
    return payload


@router.post(
    "/shifts/{shift_id}/post-adjustment",
    response_model=KassaShiftDetailRead,
    dependencies=KASSA_ADJUSTMENTS_CREATE,
)
async def post_shift_adjustment_endpoint(
    shift_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """Провести расхождение кассы (cash_diff) корректировкой ДДС (право выдаётся точечно)."""
    shift = await session.get(IikoCashShift, shift_id)
    if shift is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Смена не найдена")
    try:
        await post_shift_adjustment(session, shift)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    payload = await get_shift(session, shift_id)
    assert payload is not None  # noqa: S101 - shift exists
    return payload


@router.post(
    "/shifts/{shift_id}/waive-penalty",
    response_model=KassaShiftDetailRead,
    dependencies=KASSA_PENALTY_WAIVE,
)
async def waive_shift_penalty_endpoint(
    shift_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    """Отменить авто-штрафы кассирам за недостачу смены (право выдаётся точечно).

    Удаляет связанные ``PayrollAdjustment`` (штраф уходит из расчёта зарплаты) и помечает
    штрафы ``waived``. Работает даже при недостаче > порога."""
    shift = await session.get(IikoCashShift, shift_id)
    if shift is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Смена не найдена")
    try:
        await waive_shift_penalty(session, shift, actor_user_id=actor.user_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    payload = await get_shift(session, shift_id)
    assert payload is not None  # noqa: S101 - shift exists
    return payload

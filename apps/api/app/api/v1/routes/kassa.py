from __future__ import annotations

import contextlib
import logging
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
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
    KassaFreelancerShiftPayoutRequest,
    KassaFreelancerShiftsRead,
    KassaFreelancerShiftVoidRequest,
    KassaFreelancerSyncReport,
    KassaJournalRead,
    KassaPayinCreate,
    KassaPayinPresetArticleRead,
    KassaPayinPresetCounterpartyRead,
    KassaPayinPresetOptionRead,
    KassaPayinPresetRead,
    KassaPayinPresetWrite,
    KassaPayinResultRead,
    KassaPayinUpdate,
    KassaPayoutArticleRead,
    KassaPayoutContextRead,
    KassaPayoutCreate,
    KassaPayoutEmployeeRead,
    KassaPayoutResultRead,
    KassaPayrollPayoutRequest,
    KassaPendingRead,
    KassaShiftDetailRead,
    KassaShiftRead,
    KassaShiftSyncReport,
    KassaTargetPayoutRequest,
)
from app.services.advance_iiko_payout import post_advance_payout_to_iiko
from app.services.freelancer.shift_settlement import (
    FreelancerShiftSettlementError,
    freelancer_shift_details,
    pay_freelancer_shifts,
    sync_freelancer_shifts,
    void_freelancer_shift_payout,
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
from app.services.kassa.payins import (
    KassaPayinError,
    create_payin,
    create_preset,
    delete_payin,
    delete_preset,
    list_active_presets,
    list_all_presets,
    list_preset_articles,
    list_preset_counterparties,
    update_payin,
    update_preset,
)
from app.services.kassa.payouts import (
    MOSCOW_TZ,
    KassaPayoutError,
    KassaPayoutResult,
    create_payout,
    delete_payout,
    get_kassa_wallet,
    kassa_balance,
    kassa_journal,
    kassa_pending_payload,
    kassa_today,
    list_payout_articles,
    list_payout_employees,
    pay_kassa_target,
    update_payout,
)
from app.services.payroll_advance_service import (
    cancel_kassa_advance,
    disburse_kassa_advance,
)
from app.services.payroll_reserves import pay_run_from_pool
from app.services.payroll_runner import PayrollConflictError, PayrollNotFoundError
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
# Кассовый журнал и «Выплата из кассы» — свои права, НЕ переиспользуют общий ДДС
# (finance.cashflow.* администратору не положены).
KASSA_JOURNAL_READ = (Depends(require_permission("kassa.journal.read")),)
KASSA_PAYOUTS_CREATE = (Depends(require_permission("kassa.payouts.create")),)
# Отмена ошибочной выдачи внештатнику — ОТДЕЛЬНОЕ право (owner/admin): выдаёт кассовый
# администратор, а откатывает уже отданные наличные — тот, кто отвечает за деньги.
KASSA_FREELANCER_SHIFT_VOID = (Depends(require_permission("kassa.freelancer_shift.void")),)
# Внесение по пресету — то же право, что и выплата (кассовые движения ТК Черникова).
# Каталог пресетов курирует владелец — под общими правами Настроек.
KASSA_SETTINGS_READ = (Depends(require_permission("settings.general.read")),)
KASSA_SETTINGS_EDIT = (Depends(require_permission("settings.general.edit")),)


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
    available = or_(Counterparty.status == "active", Counterparty.name == LOCAL_PURCHASE_NAME)
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
                    is_return=line.is_return,
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
    # Явная граница транзакции перед побочными iiko-действиями: сам чек уже зафиксирован
    # (``create_cheque`` коммитит последней строкой), а этот commit закрывает всё, что могло
    # открыться после него, чтобы rollback ниже откатывал ТОЛЬКО несостоявшуюся проводку.
    await session.commit()
    # id забираем сейчас же: rollback ниже делает ORM-объект expired, и любое обращение к его
    # атрибутам полезло бы в БД вне greenlet (MissingGreenlet) — кассир получал бы 500 на уже
    # записанный чек и создавал бы его повторно.
    invoice_id = invoice.id
    # Складские позиции (с товаром) уходят в iiko приходной накладной от «Местного закупа»;
    # прочие расходы без товара push пропускает сам. Ошибка/skip не отменяет созданный чек.
    with contextlib.suppress(WarehousePushError):
        await push_invoice_to_iiko(session, invoice_id)
    # ПЕРСОНАЛЬНЫЕ/прочие статьи чека (питание/содержание) дублируем в iiko изъятиями addPayOut
    # по статьям, счёт по способу оплаты (наличные→Главная касса, карта→эквайринг). ТОВАРНУЮ часть
    # («Оплата поставщикам») здесь НЕ проводим (skip_supplier=True) — её гасит правильная «оплата
    # накладной» add_payment сверочным джобом mirror_paid_kassa_invoices (≤5 мин), чтобы товар не
    # задваивался. Идемпотентно; сбой проводки фиксируется в kassa_cheque_iiko_payout.
    try:
        await post_kassa_payment_to_iiko(session, invoice_id, skip_supplier=True)
        await session.commit()
    except Exception:  # noqa: BLE001 — iiko-проводка побочна, чек уже создан и закоммичен
        await session.rollback()
        logger.exception("Чек %s: не удалось провести прочие расходы в iiko", invoice_id)
    payload_out = await get_cheque(session, invoice_id)
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


# --- Кассовый журнал и «Выплата из кассы» (ТК Черникова) -------------------------


@router.get("/journal", response_model=KassaJournalRead, dependencies=KASSA_JOURNAL_READ)
async def kassa_journal_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
    date_from: date | None = None,
    date_to: date | None = None,
    direction: str | None = Query(None, pattern="^(in|out)$"),
    search: str | None = None,
) -> dict:
    """Журнал кассы: зеркало ДДС строго по ТК Черникова, других счетов не видно.

    По умолчанию — текущий месяц. Остаток «в кассе сейчас» считается по всем
    движениям, приход/расход — за выбранный период."""
    today = kassa_today()
    range_from = date_from or today.replace(day=1)
    range_to = date_to or today
    try:
        return await kassa_journal(
            session,
            date_from=range_from,
            date_to=range_to,
            direction=direction,
            search=search,
            actor_user_id=actor.user_id,
        )
    except KassaPayoutError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get(
    "/payout-articles",
    response_model=list[KassaPayoutArticleRead],
    dependencies=KASSA_PAYOUTS_CREATE,
)
async def list_payout_articles_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict]:
    """Статьи, разрешённые владельцем для выплат из кассы (флаг «касса», только расход)."""
    return await list_payout_articles(session)


@router.get(
    "/payout-employees",
    response_model=list[KassaPayoutEmployeeRead],
    dependencies=KASSA_PAYOUTS_CREATE,
)
async def list_payout_employees_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """Активные сотрудники — получатели аванса/займа из кассы."""
    return await list_payout_employees(session)


@router.get(
    "/payout-context",
    response_model=KassaPayoutContextRead,
    dependencies=KASSA_PAYOUTS_CREATE,
)
async def payout_context_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """Счёт и учётный остаток для строки контекста формы выплаты."""
    try:
        wallet = await get_kassa_wallet(session)
        balance = await kassa_balance(session, wallet)
    except KassaPayoutError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {"wallet_name": wallet.name, "balance": float(balance)}


@router.post(
    "/payouts",
    response_model=KassaPayoutResultRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_payout_endpoint(
    payload: KassaPayoutCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    """Выдать наличные из кассы: статья решает маршрут (леджер/дебиторка/проводка).

    Дата операции — всегда сегодня. Сумма больше учётного остатка не блокируется
    (предупреждение показывает фронт)."""
    ensure_permission(actor, "kassa.payouts.create")
    try:
        result = await create_payout(
            session,
            article_id=payload.article_id,
            amount=payload.amount,
            comment=payload.comment,
            employee_id=payload.employee_id,
            counterparty_id=payload.counterparty_id,
            location_id=payload.location_id,
            lease_id=payload.lease_id,
            actor_user_id=actor.user_id,
        )
    except KassaPayoutError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await _mirror_advance_payout_to_iiko(session, result)
    return result.as_dict()


@router.patch("/payouts/{transaction_id}", response_model=KassaPayoutResultRead)
async def update_payout_endpoint(
    transaction_id: uuid.UUID,
    payload: KassaPayoutCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    """Исправить СВОЮ кассовую запись в день создания.

    Голая проводка правится на месте; аванс/предоплата пересоздаются (отмена +
    новая выдача), у записи появляется новый id."""
    ensure_permission(actor, "kassa.payouts.create")
    try:
        result = await update_payout(
            session,
            transaction_id=transaction_id,
            article_id=payload.article_id,
            amount=payload.amount,
            comment=payload.comment,
            employee_id=payload.employee_id,
            counterparty_id=payload.counterparty_id,
            actor_user_id=actor.user_id,
        )
    except KassaPayoutError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await _mirror_advance_payout_to_iiko(session, result)
    return result.as_dict()


@router.delete("/payouts/{transaction_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_payout_endpoint(
    transaction_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> None:
    """Удалить СВОЮ кассовую запись в день создания (аванс — отмена, предоплата — снятие)."""
    ensure_permission(actor, "kassa.payouts.create")
    try:
        await delete_payout(session, transaction_id=transaction_id, actor_user_id=actor.user_id)
    except KassaPayoutError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


# --- «Внесение в кассу» по пресетам (ТК Черникова) ------------------------------


@router.get(
    "/payin-presets",
    response_model=list[KassaPayinPresetOptionRead],
    dependencies=KASSA_PAYOUTS_CREATE,
)
async def list_payin_presets_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict]:
    """Активные пресеты внесения для формы кассира (имя + шаблон комментария)."""
    return await list_active_presets(session)


@router.post(
    "/payins",
    response_model=KassaPayinResultRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_payin_endpoint(
    payload: KassaPayinCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    """Провести приход на ТК Черникова по пресету (дата — сегодня; iiko не трогаем)."""
    ensure_permission(actor, "kassa.payouts.create")
    try:
        return await create_payin(
            session,
            preset_id=payload.preset_id,
            amount=payload.amount,
            comment=payload.comment,
            actor_user_id=actor.user_id,
        )
    except KassaPayinError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.patch("/payins/{transaction_id}", response_model=KassaPayinResultRead)
async def update_payin_endpoint(
    transaction_id: uuid.UUID,
    payload: KassaPayinUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    """Исправить сумму/комментарий СВОЕГО сегодняшнего внесения (статья пресета неизменна)."""
    ensure_permission(actor, "kassa.payouts.create")
    try:
        return await update_payin(
            session,
            transaction_id=transaction_id,
            amount=payload.amount,
            comment=payload.comment,
            actor_user_id=actor.user_id,
        )
    except KassaPayinError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.delete("/payins/{transaction_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_payin_endpoint(
    transaction_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> None:
    """Удалить СВОЁ сегодняшнее внесение (приход снимается с кассы)."""
    ensure_permission(actor, "kassa.payouts.create")
    try:
        await delete_payin(session, transaction_id=transaction_id, actor_user_id=actor.user_id)
    except KassaPayinError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


# --- Каталог пресетов внесения (Настройки → Касса, курирует владелец) ------------


@router.get(
    "/payin-presets/all",
    response_model=list[KassaPayinPresetRead],
    dependencies=KASSA_SETTINGS_READ,
)
async def list_payin_presets_all_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict]:
    """Все пресеты каталога (вкл. выключенные) с именами статьи/контрагента — для Настроек."""
    return await list_all_presets(session)


@router.get(
    "/payin-preset-articles",
    response_model=list[KassaPayinPresetArticleRead],
    dependencies=KASSA_SETTINGS_READ,
)
async def list_payin_preset_articles_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict]:
    """Приходные статьи ДДС для привязки к пресету (выпадашка в Настройках)."""
    return await list_preset_articles(session)


@router.get(
    "/payin-preset-counterparties",
    response_model=list[KassaPayinPresetCounterpartyRead],
    dependencies=KASSA_SETTINGS_READ,
)
async def list_payin_preset_counterparties_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict]:
    """Активные контрагенты для привязки к пресету (выпадашка в Настройках)."""
    return await list_preset_counterparties(session)


@router.post(
    "/payin-presets",
    response_model=KassaPayinPresetRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=KASSA_SETTINGS_EDIT,
)
async def create_payin_preset_endpoint(
    payload: KassaPayinPresetWrite,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    """Завести пресет внесения (владелец)."""
    try:
        preset = await create_preset(
            session,
            name=payload.name,
            article_id=payload.article_id,
            counterparty_id=payload.counterparty_id,
            comment_template=payload.comment_template,
            is_active=payload.is_active,
            sort_order=payload.sort_order,
            actor_user_id=actor.user_id,
        )
    except KassaPayinError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return await _preset_read(session, preset.id)


@router.patch(
    "/payin-presets/{preset_id}",
    response_model=KassaPayinPresetRead,
    dependencies=KASSA_SETTINGS_EDIT,
)
async def update_payin_preset_endpoint(
    preset_id: uuid.UUID,
    payload: KassaPayinPresetWrite,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """Изменить пресет внесения (владелец)."""
    try:
        await update_preset(
            session,
            preset_id=preset_id,
            name=payload.name,
            article_id=payload.article_id,
            counterparty_id=payload.counterparty_id,
            comment_template=payload.comment_template,
            is_active=payload.is_active,
            sort_order=payload.sort_order,
        )
    except KassaPayinError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return await _preset_read(session, preset_id)


@router.delete(
    "/payin-presets/{preset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=KASSA_SETTINGS_EDIT,
)
async def delete_payin_preset_endpoint(
    preset_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Удалить пресет внесения (владелец). Проведённые по нему внесения остаются в журнале."""
    try:
        await delete_preset(session, preset_id=preset_id)
    except KassaPayinError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


async def _preset_read(session: AsyncSession, preset_id: uuid.UUID) -> dict:
    """Одна строка каталога после создания/правки (из общего list, отфильтрованного по id)."""
    for item in await list_all_presets(session):
        if item["id"] == preset_id:
            return item
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Пресет не найден")


# --- Вкладка «К выдаче»: целёвки в кассе и разрешения на авансы/займы ------------


@router.get("/pending", response_model=KassaPendingRead, dependencies=KASSA_PAYOUTS_CREATE)
async def kassa_pending_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """Позиции «К выдаче»: целёвки, переданные в кассу, + ожидающие разрешения.

    Целёвке доступна частичная выдача, разрешению — только вся сумма."""
    try:
        return await kassa_pending_payload(session)
    except KassaPayoutError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post(
    "/targets/{allocation_id}/payout",
    response_model=KassaPendingRead,
    dependencies=KASSA_PAYOUTS_CREATE,
)
async def pay_kassa_target_endpoint(
    allocation_id: uuid.UUID,
    payload: KassaTargetPayoutRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    """«Выдано»: расход целёвки наличными из кассы (частично можно, сверх остатка — нет).

    Проводка по статье целёвки с её контрагентом, датой сегодня — строка сразу видна
    в кассовом журнале. Сумма больше учётного остатка кассы не блокируется
    (предупреждение показывает фронт)."""
    try:
        await pay_kassa_target(
            session,
            allocation_id=allocation_id,
            amount=payload.amount,
            actor_user_id=actor.user_id,
        )
        return await kassa_pending_payload(session)
    except KassaPayoutError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post(
    "/targets/{allocation_id}/payroll-payout",
    response_model=KassaPendingRead,
    dependencies=KASSA_PAYOUTS_CREATE,
)
async def pay_kassa_payroll_target_endpoint(
    allocation_id: uuid.UUID,
    payload: KassaPayrollPayoutRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    """Выдать полные остатки выбранным сотрудникам из зарплатного резерва кассы.

    Кассиру не нужны права на payroll: endpoint узко принимает только резерв торговой
    кассы, а проводку выполняет общий зарплатный движок (ведомость + резерв + ДДС).
    """
    try:
        await pay_run_from_pool(
            session,
            reserve_id=allocation_id,
            selected_ids=set(payload.employee_ids),
            boundary_override=payload.boundary_id,
            allow_overflow=False,
            expected_location="kassa",
            paid_at=datetime.now(MOSCOW_TZ).date(),
            actor_user_id=actor.user_id,
        )
        return await kassa_pending_payload(session)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post(
    "/advance-permissions/{advance_id}/disburse",
    response_model=KassaPendingRead,
    dependencies=KASSA_PAYOUTS_CREATE,
)
async def disburse_kassa_advance_endpoint(
    advance_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """«Выплачено»: исполнить разрешение на аванс/заём (только вся сумма).

    Наличная проводка из ТК Черникова + активация долга датой подтверждения
    (удержания от неё) + изъятие в iiko после commit (ошибка iiko выдачу не валит).
    Повторное исполнение и исполнение отменённого — 409."""
    try:
        advance = await disburse_kassa_advance(session, advance_id=advance_id)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    # Тот же контур, что у наличной выдачи с ТК: после commit, ошибка не откатывает.
    await post_advance_payout_to_iiko(
        session,
        amount=advance.amount,
        payout_date=advance.issued_on,
        source_id=str(advance.id),
        is_loan=advance.kind == "loan",
        source="cash",
    )
    return await kassa_pending_payload(session)


@router.post(
    "/advance-permissions/{advance_id}/cancel",
    response_model=KassaPendingRead,
    dependencies=KASSA_PAYOUTS_CREATE,
)
async def cancel_kassa_advance_endpoint(
    advance_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """Отменить разрешение со стороны кассы: создатель увидит «отменено кассой».

    Проводок нет (разрешение денег не двигало). Уже исполненное — 409."""
    try:
        await cancel_kassa_advance(session, advance_id=advance_id, by_kassa_admin=True)
    except PayrollNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PayrollConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return await kassa_pending_payload(session)


@router.post(
    "/freelancer-shifts/sync",
    response_model=KassaFreelancerSyncReport,
    dependencies=KASSA_PAYOUTS_CREATE,
)
async def sync_freelancer_shifts_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """«Синхронизировать смены»: перечитать явки текущей недели из iiko + вернуть список.

    Дёргает ночное окно авансов (``refresh_current_week_advance_window``): гарантирует
    недельный период и грузит явки iiko за отработанные дни, чтобы свежие смены появились
    в «К выдаче». Ошибка iiko — 502 (как у прочих ручных синков)."""
    try:
        return await sync_freelancer_shifts(session)
    except FreelancerShiftSettlementError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — сбой выгрузки iiko отдаём как 502
        logger.exception("freelancer shift sync failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Не удалось синхронизировать смены из iiko — попробуйте позже",
        ) from exc


@router.get(
    "/freelancer-shifts/{employee_id}",
    response_model=KassaFreelancerShiftsRead,
    dependencies=KASSA_PAYOUTS_CREATE,
)
async def freelancer_shift_details_endpoint(
    employee_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """Смены внештатника открытого периода (для модалки): дата · часы · сумма · оплачена ли."""
    return {"shifts": await freelancer_shift_details(session, employee_id)}


@router.post(
    "/freelancer-shifts/payout",
    response_model=KassaPendingRead,
    dependencies=KASSA_PAYOUTS_CREATE,
)
async def pay_freelancer_shifts_endpoint(
    payload: KassaFreelancerShiftPayoutRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    """«Выплатить»: выдать наличные из ТК Черникова за выбранные смены (каждую целиком).

    Движок пишет статью «Зарплата производственного персонала» сам (админ не вводит ни
    сумму, ни статью). Запрос атомарен: уже выданная налом смена — 409. Сумма больше
    остатка кассы не блокируется (предупреждение показывает фронт)."""
    try:
        await pay_freelancer_shifts(
            session,
            attendance_entry_ids=payload.attendance_entry_ids,
            actor_user_id=actor.user_id,
        )
    except FreelancerShiftSettlementError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return await kassa_pending_payload(session)


@router.post(
    "/freelancer-shifts/void",
    response_model=KassaPendingRead,
    dependencies=KASSA_FREELANCER_SHIFT_VOID,
)
async def void_freelancer_shift_endpoint(
    payload: KassaFreelancerShiftVoidRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> dict:
    """«Отменить выдачу»: откат ошибочной наличной выдачи внештатнику за смену.

    Отменяется операция целиком (сумму выданного контур не пересчитывает никогда): деньги
    снимаются — в день выдачи удалением проводки, за прошлую дату сторно-приходом, — а смена
    возвращается в «К выдаче». Уже посчитанная ведомость периода после этого требует
    пересчёта перед финализацией (staleness-гейт). Финализированный период — 409."""
    try:
        await void_freelancer_shift_payout(
            session,
            attendance_entry_id=payload.attendance_entry_id,
            actor_user_id=actor.user_id,
        )
    except FreelancerShiftSettlementError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return await kassa_pending_payload(session)


async def _mirror_advance_payout_to_iiko(session: AsyncSession, result: KassaPayoutResult) -> None:
    """Наличный аванс/заём с ТК Черникова зеркалится изъятием в iiko «Главная касса».

    Тот же контур, что при выдаче со страницы авансов (payroll_advances): вызывается
    ПОСЛЕ commit, ошибка iiko выдачу не откатывает (логируется внутри)."""
    advance = result.advance
    if advance is None:
        return
    await post_advance_payout_to_iiko(
        session,
        amount=Decimal(str(advance.amount)),
        payout_date=advance.issued_on,
        source_id=str(advance.id),
        is_loan=advance.kind == "loan",
    )

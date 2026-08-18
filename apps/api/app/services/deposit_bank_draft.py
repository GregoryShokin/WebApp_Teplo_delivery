"""Банк-черновик выдачи депозита (курьеры + производственный персонал) — этап 3.

Выдача депозита безналичным каналом идёт «как зарплата»: деньги переводятся с
расчётного счёта на карту «Сейф» (ИП Шокина), откуда раздаются сотрудникам. У самих
сотрудников банковских реквизитов в системе НЕТ, поэтому черновик платежа в Т-Банк
всегда выписывается на ИП Шокину (те же реквизиты, что у зарплатного черновика,
``payroll.bank_payout_requisites``).

Модуль отвечает за ДВЕ вещи:
1. Пара ДДС-проводок перевода банк→Сейф (как ``payroll_payouts.book_bank_to_safe_transfer``):
   банк-нога (out, «Выбытие — перевод между счетами») — prebooked-цель для исходящей
   операции выписки; Сейф-нога (in, «Поступление — перевод между счетами») — двигает баланс
   Сейфа, откуда депозит раздаётся. Сам расход «Выдача депозита» списывается с Сейфа
   отдельной проводкой в ``deposit_service``/``couriers.deposit_service`` (wallet=Сейф для
   банк-черновика) — здесь только транзит.
2. Черновик платежа в Т-Банк на ИП Шокину (``build_payment_draft_api_payload`` +
   ``TbankClient.create_payment_draft``). Сетевой вызов — ПОСЛЕ commit, устойчиво: ошибка
   банка логируется и НЕ откатывает уже проведённую выдачу (как iiko-изъятие). iiko здесь
   НЕ вызывается (деньги идут не через «Главную кассу»).

Идемпотентность транзита — по ``(source_kind, source_id)``. У производственной выдачи
``source_id = DepositTransaction.id`` (UUID); у курьерской ``CourierDepositTransaction.id``
целочисленный и в UUID-поле ``source_id`` не помещается → ``source_id=None`` (операция
создаётся один раз, как в ``_book_deposit_return_cashflow``).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import (
    Account,
    CashflowTransaction,
    DdsArticle,
    DepositBankDraft,
    Employee,
    ReconciliationCase,
    SafeAllocation,
    Wallet,
)
from app.services import deposit_service
from app.services.bank_payment_status import classify_payment_status
from app.services.banking import BankClient
from app.services.banking.exceptions import BankFetchError
from app.services.banking.ip_card_requisites import (
    load_owner_approved_ip_card_requisites,
)
from app.services.banking.payout import payer_account_for, payout_client_for
from app.services.banking.safe_allocations import create_allocation, safe_reserved_total
from app.services.banking.tbank import build_payment_draft_api_payload
from app.services.deposit_schedule import assert_no_payroll_deposit_payout
from app.services.wallets import (
    DDS_ARTICLE_TRANSFER_IN_CODE,
    DDS_ARTICLE_TRANSFER_OUT_CODE,
    SAFE_WALLET_CODE,
)

# Статья резерва Сейфа под выдачу депозита (та же, что у наличного расхода).
DEPOSIT_PAYOUT_ARTICLE_CODE = "vydacha_depozita_sotrudniku"

logger = logging.getLogger(__name__)

MOCK_PAYER_ACCOUNT = "00000000000000000000"

# source_kind транзитной пары банк→Сейф для выдачи депозита.
COURIER_DEPOSIT_RETURN_DRAFT_SOURCE_KIND = "courier_deposit_return_draft"
PRODUCTION_DEPOSIT_PAYOUT_DRAFT_SOURCE_KIND = "production_deposit_payout_draft"

MONEY = Decimal("0.01")


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(MONEY)


async def book_deposit_bank_to_safe_transfer(
    session: AsyncSession,
    *,
    source_kind: str,
    source_id: uuid.UUID | None,
    amount: Decimal,
    operation_date: date,
    purpose: str,
    provider: str = "tbank",
) -> bool:
    """Завести транзит банк→Сейф под безналичную выдачу депозита.

    ``provider`` задаёт банк-плательщика (``tbank`` / ``sber``): банк-нога перевода садится на
    кошелёк расчётного счёта этого банка. Идемпотентно по ``(source_kind, source_id)`` (когда
    ``source_id`` задан). Устойчиво: нет расчётного/Сейф-кошелька или статей переводов — no-op
    (возврат False), выдачу не валим. Возвращает True, если пара проводок создана.
    """
    amount = _money(amount)
    if amount <= 0:
        return False
    if source_id is not None:
        existing = await session.scalar(
            select(CashflowTransaction.id).where(
                CashflowTransaction.source_kind == source_kind,
                CashflowTransaction.source_id == source_id,
            )
        )
        if existing is not None:
            return False

    settings = get_settings()
    payer_account = payer_account_for(settings, provider)
    if not payer_account:
        return False
    bank_wallet = await session.scalar(
        select(Wallet)
        .join(Account, Account.id == Wallet.account_id)
        .where(Account.account_number == payer_account, Wallet.status == "active")
    )
    safe_wallet = await session.scalar(
        select(Wallet).where(Wallet.code == SAFE_WALLET_CODE, Wallet.status == "active")
    )
    if bank_wallet is None or safe_wallet is None:
        return False
    transfer_out_article = await session.scalar(
        select(DdsArticle.id).where(DdsArticle.code == DDS_ARTICLE_TRANSFER_OUT_CODE)
    )
    transfer_in_article = await session.scalar(
        select(DdsArticle.id).where(DdsArticle.code == DDS_ARTICLE_TRANSFER_IN_CODE)
    )
    session.add(
        CashflowTransaction(
            wallet_id=bank_wallet.id,
            direction="out",
            amount=amount,
            operation_date=operation_date,
            article_id=transfer_out_article,
            source_kind=source_kind,
            source_id=source_id,
            payment_purpose=purpose,
            quality_status="final",
        )
    )
    session.add(
        CashflowTransaction(
            wallet_id=safe_wallet.id,
            direction="in",
            amount=amount,
            operation_date=operation_date,
            article_id=transfer_in_article,
            source_kind=source_kind,
            source_id=source_id,
            payment_purpose=purpose,
            quality_status="final",
        )
    )
    await session.flush()
    return True


async def send_deposit_payout_bank_draft(
    session: AsyncSession,
    *,
    document_id: str,
    amount: Decimal,
    purpose: str,
    bank_client: BankClient | None = None,
    provider: str = "tbank",
) -> bool:
    """Выписать банк-черновик на Сейф (Шокину) под выдачу депозита.

    ``provider`` (``tbank`` / ``sber``) выбирает банк-плательщика: Т-Банк — черновик через
    Open API, Сбер — рублёвый РПП без подписи (черновик, подписывается в СберБизнес).
    Получатель-Сейф общий и зафиксирован в коде; одноимённая настройка БД используется
    только для контроля дрейфа. Вызывать ПОСЛЕ commit (БД — источник истины). Устойчиво:
    нет расчётного счёта или ошибка банка — исключение НЕ поднимается, уже проведённая
    выдача не откатывается. Но молчать о провале нельзя: депозит списан, сотрудник считается
    рассчитанным, а платёж в банк не ушёл — поэтому заводим кейс владельцу на разбор.
    Возвращает True, если черновик отправлен (или сэмулирован в mock-режиме).
    """
    try:
        amount = _money(amount)
        if amount <= 0:
            return False
        requisites = await _bank_payout_requisites(session)
        settings = get_settings()
        payer_account = payer_account_for(settings, provider)
        if not payer_account:
            logger.warning(
                "Банк-черновик выдачи депозита %s: не настроен расчётный счёт плательщика (%s)",
                document_id,
                provider,
            )
            await _open_draft_failure_case(
                session,
                document_id=document_id,
                amount=amount,
                purpose=purpose,
                provider=provider,
                reason="Не настроен расчётный счёт плательщика",
            )
            return False
        # Валидация полей платежа (выкинет ValueError при нехватке реквизитов) — до сетевого вызова.
        build_payment_draft_api_payload(
            document_id=document_id,
            amount=amount,
            purpose=purpose,
            requisites=requisites,
            payer_account=payer_account,
        )
        client = bank_client or payout_client_for(provider, session)
        result = await client.create_payment_draft(
            document_id=document_id,
            amount=amount,
            purpose=purpose,
            requisites=dict(requisites),
            payer_account=payer_account,
        )
        logger.info(
            "Банк-черновик выдачи депозита %s отправлен: status=%s ref=%s",
            document_id,
            getattr(result, "status", None),
            getattr(result, "provider_ref", None),
        )
        return True
    except Exception as exc:  # noqa: BLE001 — банк не должен валить уже проведённую выдачу
        logger.warning(
            "Банк-черновик выдачи депозита %s не отправлен: %s", document_id, exc, exc_info=True
        )
        await _open_draft_failure_case(
            session,
            document_id=document_id,
            amount=amount,
            purpose=purpose,
            provider=provider,
            reason=str(exc)[:500],
        )
        return False


async def _open_draft_failure_case(
    session: AsyncSession,
    *,
    document_id: str,
    amount: Decimal,
    purpose: str,
    provider: str,
    reason: str,
) -> None:
    """Кейс владельцу: выдача проведена, а черновик в банк не ушёл — платить нечем.

    Своим коммитом: функцию зовут ПОСЛЕ commit роута, кейс должен пережить возврат ответа.
    Уникального индекса на этот kind нет — каждый провал заводит свой кейс (document_id
    у выдач разный). Падение самого кейса не валит ответ: выдача уже проведена и
    откатывать её из-за журнала нельзя.
    """
    try:
        session.add(
            ReconciliationCase(
                kind="deposit_bank_draft_failed",
                status="pending",
                provider=provider,
                payload={
                    "document_id": document_id,
                    "amount": str(amount),
                    "purpose": purpose,
                    "reason": reason,
                },
            )
        )
        await session.commit()
    except Exception:  # noqa: BLE001 — журнал не важнее уже выданного депозита
        logger.exception("Не удалось завести кейс о непрошедшем черновике %s", document_id)
        await session.rollback()


async def _bank_payout_requisites(session: AsyncSession) -> dict[str, Any]:
    return await load_owner_approved_ip_card_requisites(session)


def _payer_account(settings: Any) -> str | None:
    if settings.tbank_api_account_number:
        return settings.tbank_api_account_number
    if settings.teplo_bank_client_mode == "mock":
        return MOCK_PAYER_ACCOUNT
    return None


# --- полный цикл: черновик → активные платежи → оплата → резерв → выплата ---------


async def create_deposit_payout_draft(
    session: AsyncSession,
    *,
    recipient_kind: str,
    amount: Decimal,
    purpose: str,
    provider: str,
    employee_id: uuid.UUID | None = None,
    courier_deposit_transaction_id: int | None = None,
    created_by_user_id: uuid.UUID | None = None,
    bank_client: BankClient | None = None,
) -> DepositBankDraft:
    """Завести банк-черновик под выдачу депозита (синхронно, зеркало create_advance_bank_draft).

    Депозит-счёт НЕ трогается и расход НЕ книжится — деньги двигаются только после оплаты
    черновика (транзит+резерв) и фактической выдачи. ``document_id`` — по id черновика, чтобы
    вебхук/поллинг нашли платёж по ``provider_ref``. При сетевой/банк-ошибке черновик
    сохраняется со статусом ``failed`` (выдачу не валим — можно отправить заново).

    ``created_by_user_id`` — автор выдачи; хранится в payload и нужен курьерскому возврату:
    ``CourierDepositTransaction.created_by`` NOT NULL, а транзакция создаётся лишь при выдаче.
    """
    amount = _money(amount)
    if recipient_kind == "production" and employee_id is not None:
        await assert_no_payroll_deposit_payout(session, employee_id)
    draft_id = uuid.uuid4()
    document_id = f"teplo-deposit-{draft_id}"
    settings = get_settings()
    payer_account = payer_account_for(settings, provider)
    requisites = await _bank_payout_requisites(session)
    api_payload = build_payment_draft_api_payload(
        document_id=document_id,
        amount=amount,
        purpose=purpose,
        requisites=requisites,
        payer_account=payer_account,
    )
    stored_payload: dict[str, Any] = {"accountNumber": payer_account, "request": api_payload}
    if created_by_user_id is not None:
        stored_payload["created_by"] = str(created_by_user_id)

    def _make(status: str, provider_ref: str | None, last_error: str | None) -> DepositBankDraft:
        return DepositBankDraft(
            id=draft_id,
            recipient_kind=recipient_kind,
            employee_id=employee_id,
            courier_deposit_transaction_id=courier_deposit_transaction_id,
            deposit_transaction_id=None,  # заполнится при выдаче
            document_id=document_id[:64],
            amount=amount,
            status=status,
            bank_provider=provider,
            provider_ref=provider_ref,
            payload=stored_payload,
            last_error=last_error,
            synced_at=datetime.now(UTC),
        )

    client = bank_client or payout_client_for(provider, session)
    try:
        result = await client.create_payment_draft(
            document_id=document_id,
            amount=amount,
            purpose=purpose,
            requisites=dict(requisites),
            payer_account=payer_account,
        )
    except BankFetchError as exc:
        draft = _make("failed", None, str(exc)[:500])
        session.add(draft)
        await session.flush()
        return draft

    status = result.status if result.status in ("created", "updated") else "created"
    draft = _make(status, result.provider_ref, None)
    session.add(draft)
    await session.flush()
    return draft


# Наличные каналы «резерв» (этап 4): деньги уже наличными, банк не участвует.
CASH_RESERVE_CHANNELS = {"cash_safe": "safe", "cash_tk": "kassa"}


async def create_deposit_cash_reserve(
    session: AsyncSession,
    *,
    employee_id: uuid.UUID,
    amount: Decimal,
    purpose: str,
    channel: str,
    recipient_kind: str = "production",
) -> DepositBankDraft:
    """Наличный депозит-резерв (этап 4): «Создать резерв» (Сейф) / «Передать в кассу» (Касса).

    Деньги уже наличными (на карте Сейфа или в ящике кассы) — движения перемещения НЕ книжим,
    только заводим резерв ``SafeAllocation(reserved)`` + ``DepositBankDraft`` сразу в статусе
    ``paid`` (банк-шага нет, поэтому provider_ref=None). Депозит-счёт спишется при фактической
    выдаче резерва (``sync_deposit_after_allocation_change``) — как в банк-цикле, тем же кодом.

    🔴 R1: резерв идёт с ``employee_id=None`` (получатель — в ``DepositBankDraft``), иначе
    ``pay_allocation`` завёл бы EmployeePayout вида salary. Для Сейфа проверяем свободный
    остаток (перерезервирование → ValueError); для кассы — нет (деньги физически в ящике).
    """
    amount = _money(amount)
    if recipient_kind == "production":
        await assert_no_payroll_deposit_payout(session, employee_id)
    location = CASH_RESERVE_CHANNELS[channel]
    article_id = await session.scalar(
        select(DdsArticle.id).where(DdsArticle.code == DEPOSIT_PAYOUT_ARTICLE_CODE)
    )
    if location == "kassa":
        # Локальный импорт: kassa.payouts локально импортирует этот модуль (pay_kassa_target),
        # top-import здесь замкнул бы цикл.
        from app.services.kassa.payouts import get_kassa_wallet, kassa_balance

        wallet = await get_kassa_wallet(session)
        free_amount = None  # деньги уже в ящике кассы — перемещения нет
    else:
        from app.services.kassa.payouts import kassa_balance

        wallet = await session.scalar(
            select(Wallet).where(Wallet.code == SAFE_WALLET_CODE, Wallet.status == "active")
        )
        if wallet is None:
            raise ValueError("Кошелёк «Сейф» не найден")
        # Свободно на Сейфе = наличный баланс − уже зарезервировано (как create_safe_allocation).
        free_amount = await kassa_balance(session, wallet) - await safe_reserved_total(
            session, wallet.id
        )

    allocation = await create_allocation(
        session,
        wallet_id=wallet.id,
        amount=amount,
        free_amount=free_amount,
        article_id=article_id,
        employee_id=None,  # R1
        purpose=purpose,
        location=location,
    )
    draft = DepositBankDraft(
        id=uuid.uuid4(),
        recipient_kind=recipient_kind,
        employee_id=employee_id,
        courier_deposit_transaction_id=None,
        deposit_transaction_id=None,  # спишется при выдаче резерва
        document_id=f"teplo-deposit-cash-{uuid.uuid4()}"[:64],
        amount=amount,
        status="paid",  # деньги уже наличными — сразу «резерв», без банк-шага
        bank_provider=channel,  # маркер наличного канала (не банк-провайдер)
        provider_ref=None,
        safe_allocation_id=allocation.id,
        payload={},
        last_error=None,
        synced_at=datetime.now(UTC),
    )
    session.add(draft)
    await session.flush()
    return draft


async def apply_deposit_draft_status(
    session: AsyncSession,
    *,
    draft: DepositBankDraft,
    raw_status: str | None,
    operation_date: date | None = None,
    commit: bool = True,
) -> str:
    """Продвинуть депозитный черновик по статусу платежа (webhook Т-Банк / поллинг Сбер).

    При ``paid`` заводит транзит р/с→Сейф и резерв Сейфа под выдачу (см.
    ``_book_deposit_transit_and_reserve``). Депозит-счёт сотрудника НЕ списывается — это
    случится при фактической выдаче (оплата резерва). Идемпотентно: row-lock сериализует
    гонку, переход только из created/updated. В ``paid`` переходим ТОЛЬКО если транзит+резерв
    реально заведены — иначе (кошельки не настроены) черновик остаётся created, поллинг
    повторит, и «Выплатить» не окажется навсегда заблокированным без резерва.
    """
    outcome = classify_payment_status(raw_status)
    draft = await session.get(DepositBankDraft, draft.id, with_for_update=True)
    if draft is None:
        return "created"

    if outcome == "paid" and draft.status in ("created", "updated"):
        if await _book_deposit_transit_and_reserve(
            session, draft=draft, operation_date=operation_date
        ):
            draft.status = "paid"
            draft.synced_at = datetime.now(UTC)
    elif outcome == "failed" and draft.status in ("created", "updated"):
        draft.status = "failed"
        draft.last_error = f"Платёж отклонён банком: {raw_status}"[:500]
        draft.synced_at = datetime.now(UTC)

    if commit:
        await session.commit()
    return draft.status


async def _book_deposit_transit_and_reserve(
    session: AsyncSession,
    *,
    draft: DepositBankDraft,
    operation_date: date | None = None,
) -> bool:
    """Транзит р/с→Сейф на сумму выдачи + резерв Сейфа под выдачу депозита.

    Транзит идёт через общую ``book_deposit_bank_to_safe_transfer`` (source_id = id черновика,
    универсальный для производственника и курьера). Затем ``SafeAllocation(reserved)`` на ту же
    сумму со статьёй «Выдача депозита» — деньги висят под выдачу, видны в «Активных платежах»,
    гасятся кнопкой «Выплатить депозит».

    🔴 R1: резерв создаётся с ``employee_id=None`` — иначе ``pay_allocation`` завёл бы
    ``EmployeePayout`` вида salary, и выдача депозита срезала бы зарплату сотрудника из
    ближайшей ведомости. Получатель живёт в ``DepositBankDraft`` (employee_id/courier_tx).

    Возвращает True, если транзит+резерв заведены (или уже были); False — кошельки не настроены
    (вызывающий не переводит черновик в paid, поллинг повторит).
    """
    if draft.safe_allocation_id is not None:
        return True

    operation_date = operation_date or datetime.now(UTC).date()
    reserve_purpose = _draft_reserve_purpose(await _draft_recipient_name(session, draft))
    booked = await book_deposit_bank_to_safe_transfer(
        session,
        source_kind=PRODUCTION_DEPOSIT_PAYOUT_DRAFT_SOURCE_KIND,
        source_id=draft.id,
        amount=draft.amount,
        operation_date=operation_date,
        purpose=reserve_purpose,
        provider=draft.bank_provider,
    )
    # booked=False может значить «уже было» ИЛИ «нет кошельков». Отличаем по факту наличия
    # проводки: если транзит есть — идём дальше к резерву; если нет — кошельки не настроены.
    if not booked:
        existing_transit = await session.scalar(
            select(CashflowTransaction.id).where(
                CashflowTransaction.source_kind == PRODUCTION_DEPOSIT_PAYOUT_DRAFT_SOURCE_KIND,
                CashflowTransaction.source_id == draft.id,
            )
        )
        if existing_transit is None:
            return False

    safe_wallet = await session.scalar(
        select(Wallet).where(Wallet.code == SAFE_WALLET_CODE, Wallet.status == "active")
    )
    if safe_wallet is None:
        return False
    article_id = await session.scalar(
        select(DdsArticle.id).where(DdsArticle.code == DEPOSIT_PAYOUT_ARTICLE_CODE)
    )
    allocation = SafeAllocation(
        wallet_id=safe_wallet.id,
        amount=draft.amount,
        amount_paid=Decimal("0"),
        article_id=article_id,
        counterparty_id=None,
        employee_id=None,  # R1: НЕ привязывать сотрудника — иначе съест ЗП
        purpose=reserve_purpose,
        status="reserved",
    )
    session.add(allocation)
    await session.flush()
    draft.safe_allocation_id = allocation.id
    return True


async def _draft_recipient_name(session: AsyncSession, draft: DepositBankDraft) -> str | None:
    if draft.employee_id is not None:
        return await session.scalar(
            select(Employee.full_name).where(Employee.id == draft.employee_id)
        )
    return None


def _draft_reserve_purpose(recipient_name: str | None) -> str:
    base = "Выдача депозита"
    return f"{base} — {recipient_name}" if recipient_name else base


# --- гард «в пути» + фактическая выдача резерва (этапы 2–3) -------------------

# Статусы черновика, при которых деньги ещё «в пути» — заняты под выдачу этому сотруднику,
# хотя депозит-счёт ещё не списан (created/updated — в банке, paid — резервом на Сейфе).
IN_FLIGHT_DRAFT_STATUSES = ("created", "updated", "paid")


async def deposit_in_flight_amount(
    session: AsyncSession, employee_id: uuid.UUID
) -> Decimal:
    """Σ сумм активных банк-черновиков выдачи депозита сотрудника (created/updated/paid).

    Пока черновик висит, депозит-счёт не списан — но эти деньги уже заняты под выдачу.
    Гард роута вычитает это из баланса: нельзя выписать новый черновик или выдать наличными
    сверх свободного остатка (balance − in_flight), иначе одна сумма уйдёт дважды (R2/R3).
    """
    total = await session.scalar(
        select(func.coalesce(func.sum(DepositBankDraft.amount), 0)).where(
            DepositBankDraft.employee_id == employee_id,
            DepositBankDraft.status.in_(IN_FLIGHT_DRAFT_STATUSES),
        )
    )
    return _money(total)


async def allocation_deposit_draft(
    session: AsyncSession, allocation_id: uuid.UUID
) -> DepositBankDraft | None:
    """Депозитный черновик, привязанный к резерву Сейфа (такому запрещены отмена/перенос)."""
    return await session.scalar(
        select(DepositBankDraft).where(DepositBankDraft.safe_allocation_id == allocation_id)
    )


@dataclass(frozen=True)
class DepositDisbursement:
    """Факт состоявшейся выдачи депозита (резерв оплачен) — для пост-commit шагов (iiko)."""

    draft_id: uuid.UUID
    recipient_kind: str
    employee_id: uuid.UUID | None
    courier_deposit_transaction_id: int | None
    amount: Decimal


async def sync_deposit_after_allocation_change(
    session: AsyncSession, *, allocation_id: uuid.UUID, now: datetime | None = None
) -> DepositDisbursement | None:
    """Свести депозитный черновик со статусом его резерва Сейфа/Кассы.

    Оплата резерва («Выплатить депозит» с Сейфа или «Выдать» из кассы) = фактическая выдача
    денег сотруднику. ТОЛЬКО здесь списывается депозит-счёт и пишется ``DepositTransaction``
    типа ``payout`` — раньше, пока черновик висел, депозит был цел (решение владельца). Отмена
    резерва (гард этапа 3 её блокирует, но на всякий) → черновик ``cancelled``, депозит цел.

    Зеркало ``sync_advance_after_allocation_change``, но у депозита транзакции-леджера до выдачи
    НЕ было — она создаётся здесь. Возвращает ``DepositDisbursement``, если выдача состоялась
    (вызывающий проводит iiko-изъятие ПОСЛЕ commit для кассового пути); иначе None. Идемпотентно:
    повторный вызов на ``disbursed`` — no-op (переход только из paid).
    """
    now = now or datetime.now(UTC)
    draft = await session.scalar(
        select(DepositBankDraft)
        .where(DepositBankDraft.safe_allocation_id == allocation_id)
        .with_for_update()
    )
    if draft is None:
        return None
    allocation = await session.get(SafeAllocation, allocation_id)
    if allocation is None:
        return None

    if allocation.status == "paid" and draft.status == "paid":
        if draft.recipient_kind == "production" and draft.employee_id is not None:
            # Депозит-леджер: OUT-проводка выдачи + списание баланса на ту же сумму
            # (расход в ДДС уже провёл pay_allocation/pay_kassa_target по статье выдачи).
            transaction = deposit_service.add_transaction(
                session,
                employee_id=draft.employee_id,
                transaction_type="payout",
                amount=draft.amount,
                now=now,
            )
            await session.flush()
            draft.deposit_transaction_id = transaction.id
            account = await deposit_service.get_deposit_account(
                session, draft.employee_id, for_update=True
            )
            if account is not None:
                account.balance = _money(account.balance) - draft.amount
                account.last_updated = now
        elif draft.recipient_kind == "courier" and draft.employee_id is not None:
            # Курьерский леджер (своя таблица, целочисленный id): строка возврата создаётся
            # ТОЛЬКО здесь (уменьшает баланс депозита курьера) — раньше депозит был цел. Расход
            # в ДДС уже провёл pay_allocation. Локальный импорт: курьерский сервис импортирует
            # этот модуль на верхнем уровне (транзит), top-import замкнул бы цикл.
            from app.services.couriers.deposit_service import (
                add_return_transaction_ledger_only,
            )

            created_by = (draft.payload or {}).get("created_by")
            if not created_by:
                # Автора нет в payload (created_by — user.id, NOT NULL) — не выдаём вслепую,
                # оставляем резерв/черновик paid для разбора (штатно так не бывает: роут его пишет).
                logger.warning(
                    "Курьерский черновик %s без created_by в payload — выдача пропущена", draft.id
                )
                return None
            courier_tx = await add_return_transaction_ledger_only(
                session,
                employee_id=draft.employee_id,
                amount_cents=int((draft.amount * 100).to_integral_value()),
                transaction_date=now.date(),
                comment=None,
                created_by_user_id=uuid.UUID(created_by),
                payout_method="bank_draft_sber" if draft.bank_provider == "sber" else "bank_draft",
            )
            draft.courier_deposit_transaction_id = courier_tx.id
        else:
            return None
        draft.status = "disbursed"
        draft.synced_at = now
        return DepositDisbursement(
            draft_id=draft.id,
            recipient_kind=draft.recipient_kind,
            employee_id=draft.employee_id,
            courier_deposit_transaction_id=draft.courier_deposit_transaction_id,
            amount=draft.amount,
        )
    if allocation.status == "cancelled" and draft.status in ("created", "updated", "paid"):
        draft.status = "cancelled"
        draft.synced_at = now
    return None

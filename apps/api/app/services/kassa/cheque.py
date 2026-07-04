"""Чеки модуля «Касса» — уже оплаченная покупка (курьер купил в магазине по карте).

Чек хранится в общей ``supplier_invoice`` с ``source='kassa_cheque'`` и при создании
сразу гасится: одна/несколько card-операций из банка (безнал) + опционально наличная
часть. Каждая часть порождает движение ДДС по выбранной статье; ``Σ частей == сумма
чека`` → статус ``paid`` (чек не висит в кредиторке).

Card-операции классификатор выписки НЕ книжит сам (для него это «шум»), поэтому движение
ДДС для безналичной части создаём здесь же и привязываем банк-операцию к нему
(``op.cashflow_transaction_id``) — это и есть «классификация» card-покупки. ``kassa_cheque``
входит в ``PREBOOKABLE_SOURCE_KINDS``, поэтому обратный порядок (чек раньше импорта) тоже
не задвоит расход. Наличная часть — расход с кошелька «Торговая касса Черникова».
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AppSetting,
    BankOperation,
    CashflowTransaction,
    Counterparty,
    DdsArticle,
    IikoProduct,
    InvoiceLineItem,
    InvoicePaymentAllocation,
    ReconciliationCase,
    SupplierInvoice,
    Wallet,
)
from app.services.banking.classifier import (
    AWAITING_BANK_QUALITY,
    _wallet_for_operation,
    close_reconciliation_case,
)
from app.services.counterparty_bank_match import (
    BANK_NOISE_INNS,
    BUSINESS_TZ,
    _as_utc,
    _receiver_block,
)
from app.services.counterparty_matching import _op_already_allocated, _recompute_status

CASH_WALLET_CODE = "tk_chernikova"
# Бизнес-карта — всегда Т-Банк рублёвый счёт (других карточных счетов нет). На него садится
# ручной пендинг-чек (полная сумма) до прихода выписки.
CARD_WALLET_CODE = "tbank_main"
CARD_TX_WINDOW_HOURS = 48
CARD_TX_TIGHT_MINUTES = 120

# Матч пендинг-чека с пришедшей card-операцией: окно по ДАТЕ операции (операция несёт
# реальную дату транзакции, поэтому окно мало даже при понедельничной пачке пт–вс).
PENDING_CHEQUE_MATCH_WINDOW_DAYS = 3
# «Через сколько дней без подтверждения банком поднимать чек в Требует разбора» — настройка
# (Настройки приложения). Сейчас 4 дня (до webhooks), после webhooks владелец ставит 1–2.
PENDING_CHEQUE_ALERT_SETTING_KEY = "kassa.pending_cheque_alert_days"
PENDING_CHEQUE_ALERT_DEFAULT_DAYS = 4
UNCONFIRMED_CHEQUE_CASE_KIND = "unconfirmed_cheque"
# Аллокация пендинг-карты: покрывает сумму чека (чтобы он не висел в кредиторке), но это
# не подтверждённая банком оплата. При матче заменяется на обычную 'bank'-аллокацию.
PENDING_CARD_ALLOCATION_SOURCE = "card_pending"

# Возврат пришёл по чеку, который его не ждал (проведён полностью) либо больше остатка
# ожидания — кейс в «Требует разбора» с действием «Учесть возврат».
CARD_REFUND_CASE_KIND = "card_refund_after_cheque"
# Чек ждёт возврат (позиции исключены), а банк его не прислал дольше порога.
REFUND_MISSING_CASE_KIND = "cheque_refund_missing"
REFUND_WAIT_ALERT_SETTING_KEY = "kassa.refund_wait_alert_days"
REFUND_WAIT_ALERT_DEFAULT_DAYS = 7
# Служебная входящая статья для «Учесть возврат» (get-or-create по code).
EXPENSE_REFUND_ARTICLE_CODE = "expense_refund"
EXPENSE_REFUND_ARTICLE_NAME = "Возврат расходов"

# Чеку местного закупа доступны только эти статьи ДДС (по ИМЕНИ — code/uuid различны
# dev/prod). Кассир выбирает статью каждой позиции только из этого белого списка.
CHEQUE_ARTICLE_NAMES = (
    "Расходы на питание персонала",
    "Расходы на персонал",
    "Содержание торговых точек",
    "Оплата поставщикам",
)


class KassaChequeError(RuntimeError):
    """Доменная ошибка создания чека (маппится на HTTP 409/422)."""


def _money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _qty(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def _line_sum(line: ChequeLineInput) -> Decimal:
    """Сумма строки: введённая на кассе приоритетна над quantity*price (см. ChequeLineInput)."""
    if line.amount is not None:
        return _money(line.amount)
    return _money(_qty(line.quantity) * _money(line.price))


@dataclass
class ChequeLineInput:
    name: str
    quantity: Decimal
    price: Decimal
    unit: str | None = None
    dds_article_id: uuid.UUID | None = None
    iiko_product_id: uuid.UUID | None = None
    vat_percent: Decimal | None = None
    amount: Decimal | None = None  # сумма строки с кассы; приоритетна над quantity*price
    # Позиция возвращена в магазин: участвует в сверке gross-суммы с бумажным чеком и
    # карт-операцией (копейка в копейку), но не проводится — ДДС/iiko/аллокации без неё.
    is_return: bool = False


@dataclass
class ChequeBankPart:
    """Одна card-операция как источник оплаты чека. ``amount=None`` → вся операция."""

    bank_operation_id: uuid.UUID
    amount: Decimal | None = None


@dataclass
class CardTxnCandidate:
    bank_operation_id: uuid.UUID
    operation_date: date
    posted_at: datetime | None
    purchased_at: datetime | None  # момент покупки (authorizationDate), не проводки
    amount: Decimal
    counterparty_name_raw: str | None
    purpose: str | None
    tier: int | None
    minutes_delta: int | None
    # Возврат(ы) по этой покупке, уже пришедшие в выписку (refundIn с тем же rrn,
    # непривязанные). UI подсвечивает: «по покупке есть возврат — отметьте позиции».
    refund_amount: Decimal | None = None
    refund_count: int = 0


def _is_card_purchase(operation: BankOperation) -> bool:
    """True, если операция — покупка по бизнес-карте (а не платёж контрагенту/перевод).

    Инверсия логики складского мэтча: для накладных card-операции — «шум», для чека это
    как раз цель. Первичный признак — ``raw_payload['category'] == 'cardOperation'``; резерв
    (если на боевом токене признака нет) — расход без реквизитов контрагента. Эквайринговый
    «шум» ТБанка (зачисления/комиссия) исключаем — это не покупка.
    """
    if operation.direction != "out" or operation.transfer_group_id is not None:
        return False
    raw = operation.raw_payload or {}
    # Карточная покупка по бизнес-карте — самый надёжный признак ``category``. У неё
    # реквизиты получателя = процессинг эмитента (АО «ТБанк», ИНН 7710140679 из
    # BANK_NOISE_INNS), а реальный мерчант лежит в ``merch``/``description``. Поэтому
    # проверяем cardOperation ДО BANK_NOISE-фильтра — иначе любая карт-покупка ложно
    # отсекается как эквайринговый «шум» (подтверждено боевой операцией «Магнит»).
    if str(raw.get("category") or "").strip() == "cardOperation":
        return True
    receiver = _receiver_block(raw)
    inn = str(receiver.get("inn") or operation.counterparty_inn_raw or "")
    name = str(receiver.get("name") or operation.counterparty_name_raw or "")
    if inn in BANK_NOISE_INNS or "ТБАНК" in name.upper():
        return False
    account = str(receiver.get("acct") or operation.counterparty_account_raw or "")
    return not inn and not account


# Категория T-Банка у возврата карт-покупки (деньги вернулись на счёт). Возврат несёт
# ТЕ ЖЕ rrn/authCode, что исходная покупка (подтверждено боевой выпиской), поэтому связь
# «покупка ↔ возврат» детерминированная, без фаззи-матча по суммам/мерчанту.
TBANK_REFUND_CATEGORY = "refundIn"


def _is_card_refund(operation: BankOperation) -> bool:
    """True, если операция — возврат карт-покупки (``refundIn``)."""
    if operation.direction != "in" or operation.transfer_group_id is not None:
        return False
    raw = operation.raw_payload or {}
    return str(raw.get("category") or "").strip() == TBANK_REFUND_CATEGORY


def _op_rrn_auth(operation: BankOperation) -> tuple[str, str] | None:
    """``(rrn, authCode)`` карт-операции — общий ключ покупки и её возврата."""
    raw = operation.raw_payload or {}
    rrn = str(raw.get("rrn") or "").strip()
    auth = str(raw.get("authCode") or "").strip()
    if not rrn or not auth:
        return None
    return rrn, auth


def _card_merchant_label(operation: BankOperation) -> str | None:
    """Человекочитаемое имя мерчанта card-операции для дропдауна (а не «АО ТБанк»).

    В выписке T-Bank получатель card-покупки = процессинг банка; реальный магазин —
    в ``merch.name`` (+ город) или в ``description``. Фолбэк — сырой контрагент.
    """
    raw = operation.raw_payload or {}
    merch = raw.get("merch") if isinstance(raw.get("merch"), dict) else None
    name = str((merch or {}).get("name") or "").strip()
    if name:
        city = str((merch or {}).get("city") or "").strip()
        return f"{name} ({city})" if city else name
    description = str(raw.get("description") or "").strip()
    return description or operation.counterparty_name_raw


def _parse_iso_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _purchase_dt(operation: BankOperation) -> datetime | None:
    """Момент реальной покупки по карте (``authorizationDate`` из выписки T-Bank).

    Банк списывает card-покупки пачкой через 1–3 дня (settlement), поэтому
    ``operation_date``/``posted_at`` = день ПРОВОДКИ, а не покупки. Для «операций за
    день» нужен момент авторизации в магазине. Фолбэк — ``chargeDate``/``posted_at``.
    """
    raw = operation.raw_payload or {}
    for key in ("authorizationDate", "chargeDate"):
        parsed = _parse_iso_dt(raw.get(key))
        if parsed is not None:
            return parsed
    return _as_utc(operation.posted_at) if operation.posted_at is not None else None


def _assert_card_purchase(operation: BankOperation) -> None:
    if not _is_card_purchase(operation):
        raise KassaChequeError("Операция не является покупкой по бизнес-карте")


async def next_cheque_number(session: AsyncSession) -> str:
    """Следующий номер чека в отдельной серии ``Ч-N`` (не пересекается с накладными)."""
    numbers = (
        await session.scalars(
            select(SupplierInvoice.number).where(
                SupplierInvoice.source == "kassa_cheque",
                SupplierInvoice.number.isnot(None),
            )
        )
    ).all()
    top = 0
    for raw in numbers:
        digits = "".join(ch for ch in (raw or "") if ch.isdigit())
        if digits:
            top = max(top, int(digits))
    return f"Ч-{top + 1}"


async def list_card_transactions(
    session: AsyncSession,
    *,
    issued_at: datetime | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    window_hours: int = CARD_TX_WINDOW_HOURS,
    tight_window_minutes: int = CARD_TX_TIGHT_MINUTES,
    limit: int = 200,
) -> list[CardTxnCandidate]:
    """Card-покупки T-Bank для дропдауна чека, без уже использованных/проведённых.

    При заданном ``issued_at`` ранжируем по близости ко времени чека (tier 1-4), иначе —
    по дате диапазона (сначала свежие). Сумму не фильтруем: пользователь сам набирает
    нужные операции под сумму чека (возможен сплит на несколько карт + наличные).
    """
    if issued_at is not None:
        # UI шлёт время чека из <input type="datetime-local"> как naive локальное (МСК).
        # Трактуем naive как BUSINESS_TZ; иначе сравнение по времени уедет на смещение пояса.
        issued_local = (
            issued_at if issued_at.tzinfo is not None else issued_at.replace(tzinfo=BUSINESS_TZ)
        )
        issued_utc = issued_local.astimezone(UTC)
        issued_date = issued_local.astimezone(BUSINESS_TZ).date()
        # Показываем операции строго за ДЕНЬ ПОКУПКИ. Банк проводит покупку позже
        # (settlement-лаг), operation_date всегда ≥ дня покупки — поэтому окно по
        # operation_date асимметрично вперёд; точный фильтр по дню покупки — в цикле.
        day_lo = issued_date - timedelta(days=1)
        day_hi = issued_date + timedelta(days=10)
    elif date_from is not None and date_to is not None:
        issued_utc = None
        issued_date = None
        day_lo, day_hi = date_from, date_to
    else:
        raise KassaChequeError("Укажите дату чека или диапазон дат")

    operations = (
        await session.scalars(
            select(BankOperation).where(
                BankOperation.provider == "tbank",
                BankOperation.direction == "out",
                BankOperation.transfer_group_id.is_(None),
                BankOperation.cashflow_transaction_id.is_(None),
                BankOperation.operation_date >= day_lo,
                BankOperation.operation_date <= day_hi,
            )
        )
    ).all()

    # Один bulk-запрос вместо N: какие из операций окна уже привязаны к чеку/накладной
    # (раньше ``_op_already_allocated`` дёргался в цикле — это и был лишний пинг).
    op_ids = [operation.id for operation in operations]
    allocated_ids: set[uuid.UUID] = set()
    if op_ids:
        allocated_ids = set(
            (
                await session.scalars(
                    select(InvoicePaymentAllocation.bank_operation_id).where(
                        InvoicePaymentAllocation.bank_operation_id.in_(op_ids)
                    )
                )
            ).all()
        )

    candidates: list[CardTxnCandidate] = []
    refund_key_by_op: dict[uuid.UUID, tuple | None] = {}
    for operation in operations:
        if not _is_card_purchase(operation):
            continue
        if operation.id in allocated_ids:
            continue
        rrn_auth = _op_rrn_auth(operation)
        refund_key_by_op[operation.id] = (
            (operation.account_id, *rrn_auth) if rrn_auth is not None else None
        )

        purchased_at = _purchase_dt(operation)
        tier: int | None = None
        minutes_delta: int | None = None
        if issued_date is not None:
            # Строго за ДЕНЬ ПОКУПКИ (authorizationDate в МСК), а не за день проводки.
            purchase_date = (
                purchased_at.astimezone(BUSINESS_TZ).date()
                if purchased_at is not None
                else operation.operation_date
            )
            if purchase_date != issued_date:
                continue
            if purchased_at is not None and issued_utc is not None:
                minutes_delta = int(abs((purchased_at - issued_utc).total_seconds()) // 60)
                tier = 1 if minutes_delta <= tight_window_minutes else 2
            else:
                tier = 2

        candidates.append(
            CardTxnCandidate(
                bank_operation_id=operation.id,
                operation_date=operation.operation_date,
                posted_at=operation.posted_at,
                purchased_at=purchased_at,
                amount=_money(abs(operation.amount)),
                counterparty_name_raw=_card_merchant_label(operation),
                purpose=operation.payment_purpose,
                tier=tier,
                minutes_delta=minutes_delta,
            )
        )

    # Возвраты по показанным покупкам, уже пришедшие в выписку: непривязанный refundIn с
    # тем же rrn/authCode на том же счёте. UI подсветит «по покупке есть возврат — отметьте
    # позиции», и чек сразу создастся net.
    keys = {key for key in refund_key_by_op.values() if key is not None}
    if keys:
        rrns = {rrn for _, rrn, _ in keys}
        refund_rows = (
            await session.scalars(
                select(BankOperation).where(
                    BankOperation.provider == "tbank",
                    BankOperation.direction == "in",
                    BankOperation.cashflow_transaction_id.is_(None),
                    BankOperation.raw_payload["category"].astext == TBANK_REFUND_CATEGORY,
                    BankOperation.raw_payload["rrn"].astext.in_(rrns),
                )
            )
        ).all()
        refunds_by_key: dict[tuple, list[BankOperation]] = {}
        for refund in refund_rows:
            rrn_auth = _op_rrn_auth(refund)
            if rrn_auth is None:
                continue
            refunds_by_key.setdefault((refund.account_id, *rrn_auth), []).append(refund)
        for candidate in candidates:
            matched = refunds_by_key.get(refund_key_by_op.get(candidate.bank_operation_id) or ())
            if matched:
                candidate.refund_amount = _money(sum(abs(r.amount) for r in matched))
                candidate.refund_count = len(matched)

    if issued_utc is not None:
        candidates.sort(
            key=lambda c: (c.tier or 9, c.minutes_delta if c.minutes_delta is not None else 10**9)
        )
    else:
        candidates.sort(key=lambda c: c.posted_at or datetime.min.replace(tzinfo=UTC), reverse=True)
    return candidates[:limit]


def _allocate_by_articles(
    amount: Decimal, article_sums: list[tuple[uuid.UUID, Decimal]], total: Decimal
) -> list[tuple[uuid.UUID, Decimal]]:
    """Разнести сумму оплаты по статьям пропорционально их долям в чеке.

    Оплата (карта/наличные) и статьи (позиции) — независимые разбиения суммы чека,
    поэтому каждую оплату делим между статьями пропорционально ``доля статьи / итог``.
    Остаток от округления отдаём последней статье, чтобы Σ долей == ``amount``.
    """
    shares: list[tuple[uuid.UUID, Decimal]] = []
    allocated = Decimal("0.00")
    for index, (article_id, article_sum) in enumerate(article_sums):
        if index == len(article_sums) - 1:
            share = amount - allocated
        else:
            share = _money(amount * article_sum / total)
            allocated += share
        if share > 0:
            shares.append((article_id, share))
    return shares


async def create_cheque(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    article_id: uuid.UUID | None = None,
    issued_at: datetime,
    bank_parts: list[ChequeBankPart] | None = None,
    cash_amount: Decimal | None = None,
    pending_card_amount: Decimal | None = None,
    track_nomenclature: bool = False,
    lines: list[ChequeLineInput] | None = None,
    number: str | None = None,
    store_guid: str | None = None,
    comment: str | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> SupplierInvoice:
    """Создать чек (оплата картой ± наличными) и сразу провести его в ДДС.

    Всё валидируется ДО записи; занятая операция / неверная сумма прерывают создание,
    ничего не сохраняя. Возвращает оплаченный (``paid``) ``SupplierInvoice``.
    """
    bank_parts = bank_parts or []
    line_inputs = list(lines or [])
    pending_card = _money(pending_card_amount) if pending_card_amount is not None else None
    if issued_at is None:
        raise KassaChequeError("Укажите дату и время чека")
    if pending_card is not None:
        # Ручной ввод суммы чека (банк ещё не передал операцию). Нельзя смешивать с выбранной
        # картой — это два взаимоисключающих способа указать карточную оплату.
        if bank_parts:
            raise KassaChequeError(
                "Нельзя одновременно выбрать оплату по карте и ввести сумму вручную"
            )
        if pending_card <= 0:
            raise KassaChequeError("Сумма чека вручную должна быть больше нуля")
    if not bank_parts and pending_card is None and not cash_amount:
        raise KassaChequeError("Укажите источник оплаты: карта, ручная сумма или наличные")

    # Возвращённые позиции: чек вводится gross (как на бумаге, сверка копейка в копейку),
    # проводится net. Ожидаемый от банка возврат = сумма помеченных строк.
    returned_total = _money(
        sum((_line_sum(line) for line in line_inputs if line.is_return), Decimal("0.00"))
    )
    if returned_total > 0:
        # MVP-контур возврата: ровно одна карт-операция, без наличной части и без ручного
        # пендинга. Наличный возврат курьеру отдают на месте — такой чек вносится сразу net,
        # без пометок.
        if pending_card is not None:
            raise KassaChequeError("Возврат нельзя сочетать с ручным вводом суммы чека")
        if len(bank_parts) != 1:
            raise KassaChequeError("Возврат поддерживается только при одной карт-операции")
        if cash_amount:
            raise KassaChequeError("Возврат поддерживается только при оплате картой без наличных")
        if bank_parts[0].amount is not None:
            raise KassaChequeError("При возврате сумма карт-части определяется автоматически")
        if not track_nomenclature:
            raise KassaChequeError("Возврат отмечается на позициях — включите позиции чека")

    counterparty = await session.get(Counterparty, counterparty_id)
    if counterparty is None:
        raise KassaChequeError("Контрагент не найден")

    # --- валидируем все источники ДО записи -----------------------------------
    resolved_bank: list[tuple[BankOperation, Wallet, Decimal]] = []
    seen_ops: set[uuid.UUID] = set()
    bank_total = Decimal("0.00")
    for part in bank_parts:
        if part.bank_operation_id in seen_ops:
            raise KassaChequeError("Одна банковская операция указана дважды")
        seen_ops.add(part.bank_operation_id)
        operation = await session.get(BankOperation, part.bank_operation_id)
        if operation is None:
            raise KassaChequeError("Банковская операция не найдена")
        _assert_card_purchase(operation)
        if operation.cashflow_transaction_id is not None:
            raise KassaChequeError("Банковская операция уже проведена в ДДС")
        if await _op_already_allocated(session, operation.id):
            raise KassaChequeError("Банковская операция уже использована")
        wallet = await _wallet_for_operation(session, operation)
        if wallet is None:
            raise KassaChequeError("Не удалось определить счёт по банковской операции")
        op_amount = _money(abs(operation.amount))
        amount = _money(part.amount) if part.amount is not None else op_amount
        if returned_total > 0:
            # Проводим net (покупка − возвраты). Недоиспользованный остаток операции
            # (op − аллокация) — это и есть «ждём возврат от банка»: по нему уборщик
            # match_card_refund_operations привяжет пришедший refundIn.
            if returned_total >= op_amount:
                raise KassaChequeError("Сумма возвратов не может быть не меньше суммы операции")
            amount = _money(op_amount - returned_total)
        if amount <= 0:
            raise KassaChequeError("Сумма банковской части должна быть больше нуля")
        if amount > op_amount:
            raise KassaChequeError("Сумма части превышает сумму операции")
        resolved_bank.append((operation, wallet, amount))
        bank_total += amount

    cash_total = _money(cash_amount) if cash_amount is not None else Decimal("0.00")
    if cash_total < 0:
        raise KassaChequeError("Сумма наличной части не может быть отрицательной")

    # Карточная часть: либо разобранная по операциям (bank_total), либо ручной пендинг.
    card_total = bank_total + (pending_card or Decimal("0.00"))
    paid_total = _money(card_total + cash_total)
    if paid_total <= 0:
        raise KassaChequeError("Сумма оплаты должна быть больше нуля")

    cash_wallet: Wallet | None = None
    if cash_total > 0:
        cash_wallet = await session.scalar(
            select(Wallet).where(Wallet.code == CASH_WALLET_CODE, Wallet.status == "active")
        )
        if cash_wallet is None:
            raise KassaChequeError("Счёт «Торговая касса Черникова» не найден")

    # --- создаём чек (накладную) -----------------------------------------------
    invoice = SupplierInvoice(
        counterparty_id=counterparty_id,
        source="kassa_cheque",
        direction="payable",
        number=number or await next_cheque_number(session),
        invoice_date=issued_at.date(),
        issued_at=issued_at,
        amount=Decimal("0.00"),
        payment_status="unpaid",
        store_guid=store_guid,
        created_by_user_id=actor_user_id,
    )
    session.add(invoice)
    await session.flush()

    vat_total = Decimal("0.00")
    mirror: list[dict[str, Any]] = []
    # Накопление сумм по статьям ДДС (для построчной проводки чека местного закупа).
    article_totals: dict[uuid.UUID, Decimal] = {}
    if track_nomenclature:
        if not line_inputs:
            raise KassaChequeError("Включён складской учёт — добавьте позиции")
        product_ids = [li.iiko_product_id for li in line_inputs if li.iiko_product_id]
        products: dict[uuid.UUID, IikoProduct] = {}
        if product_ids:
            rows = (
                await session.scalars(select(IikoProduct).where(IikoProduct.id.in_(product_ids)))
            ).all()
            products = {product.id: product for product in rows}
        lines_total = Decimal("0.00")  # gross: ВСЕ строки, включая возвращённые
        for index, line in enumerate(line_inputs):
            product = products.get(line.iiko_product_id) if line.iiko_product_id else None
            quantity = _qty(line.quantity)
            price = _money(line.price)
            # Сумма строки = введённая на кассе (если задана), иначе кол-во×цена. Иначе
            # построчное округление qty*price расходится с оплатой: на фронте цена
            # пересчитывается из суммы как round(сумма/кол-во), и qty*round(сумма/кол-во) ≠ сумма.
            line_sum = _line_sum(line)
            line_article_id = line.dds_article_id or article_id
            if line_article_id is None:
                raise KassaChequeError(f"У позиции «{line.name}» не указана статья ДДС")
            vat_sum: Decimal | None = None
            if line.vat_percent:
                rate = Decimal(str(line.vat_percent))
                vat_sum = _money(line_sum * rate / (Decimal("100") + rate))  # gross-inclusive
                if not line.is_return:
                    vat_total += vat_sum
            item_name = line.name or (product.name if product else "Позиция")
            session.add(
                InvoiceLineItem(
                    invoice_id=invoice.id,
                    iiko_product_id=line.iiko_product_id,
                    product_guid=product.iiko_id if product else None,
                    name=item_name,
                    article=product.code if product else None,
                    unit=line.unit or (product.unit if product else None),
                    quantity=quantity,
                    price=price,
                    sum=line_sum,
                    vat_percent=Decimal(str(line.vat_percent)) if line.vat_percent else None,
                    vat_sum=vat_sum,
                    dds_article_id=line_article_id,
                    is_staff=False,
                    is_return=line.is_return,
                    sort_order=index,
                )
            )
            lines_total += line_sum
            if not line.is_return:
                # Возвращённая позиция не проводится: ни в статьи ДДС, ни в iiko.
                article_totals[line_article_id] = (
                    article_totals.get(line_article_id, Decimal("0.00")) + line_sum
                )
            mirror.append(
                {
                    "product_id": product.iiko_id if product else None,
                    "article": product.code if product else None,
                    "name": item_name,
                    "quantity": str(quantity),
                    "amount": str(line_sum),
                    "is_return": line.is_return,
                }
            )
        if returned_total > 0:
            # Gross-сверка «как на бумаге»: все позиции (включая возвращённые) должны
            # сойтись с суммой карт-операции копейка в копейку. Net при этом равен оплате
            # автоматически (paid_total = операция − возвраты).
            op_amount = _money(abs(resolved_bank[0][0].amount))
            if _money(lines_total) != op_amount:
                raise KassaChequeError(
                    f"Сумма позиций {_money(lines_total)} (с возвратами) не совпадает "
                    f"с суммой операции {op_amount}"
                )
        elif _money(lines_total) != paid_total:
            raise KassaChequeError(
                f"Сумма позиций {_money(lines_total)} не совпадает с оплатой {paid_total}"
            )
    elif article_id is not None:
        # Чек без позиций — вся сумма на единую статью чека (фолбэк-режим).
        article_totals[article_id] = paid_total
    else:
        raise KassaChequeError("Укажите статью ДДС (в позициях или на чеке)")

    # Статьи проводки в порядке появления; проверяем, что все расходные и активные.
    article_sums = list(article_totals.items())
    for line_article_id, _sum in article_sums:
        article = await session.get(DdsArticle, line_article_id)
        if article is None or not article.is_active:
            raise KassaChequeError("Статья ДДС не найдена")
        if article.movement_type != "outflow":
            raise KassaChequeError("Статья ДДС должна быть расходной")

    invoice.amount = paid_total
    invoice.staff_amount = Decimal("0.00")
    invoice.vat_total = _money(vat_total)
    invoice.line_items = mirror
    await session.flush()

    # --- безналичные части: движения ДДС по статьям + привязка операции + аллокация --
    for operation, wallet, amount in resolved_bank:
        first_txn_id: uuid.UUID | None = None
        for art_id, share in _allocate_by_articles(amount, article_sums, paid_total):
            transaction = CashflowTransaction(
                wallet_id=wallet.id,
                direction="out",
                amount=share,
                operation_date=operation.operation_date,
                article_id=art_id,
                counterparty_id=counterparty_id,
                source_kind="kassa_cheque",
                source_id=invoice.id,
                payment_purpose=f"Чек {invoice.number}",
                comment=comment,
                quality_status="final",
            )
            session.add(transaction)
            await session.flush()
            if first_txn_id is None:
                first_txn_id = transaction.id
        operation.cashflow_transaction_id = first_txn_id
        # Операция выбрана и привязана к проводке чека — это и есть её классификация.
        # Снимаем «требует разбора» (статус + карточку), иначе она задвоится в журнале
        # с проводкой чека (как делает _settle_pending_cheque для пендинг-ветки).
        operation.classification_status = "classified"
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id,
                source_kind="bank",
                bank_operation_id=operation.id,
                amount=amount,
                created_by_user_id=actor_user_id,
            )
        )
        await session.flush()
        await _resolve_cases(session, bank_operation_id=operation.id, invoice_id=invoice.id)
    await session.flush()

    # Возврат мог уже прийти в выписку (чек вносится на следующий день) — привязываем
    # сразу, не дожидаясь следующего синка банка.
    if returned_total > 0 and resolved_bank:
        await _attach_refunds_for_purchase(session, resolved_bank[0][0])
        await session.flush()

    # --- пендинг-карта: одна проводка «Ожидает подтверждения банком» на Т-Банк р/с ----
    # Полную сумму держим ОДНОЙ проводкой (article_id=None) и не двигаем баланс банка
    # (он идёт от выписки). Когда придёт реальная card-операция, чек-реконсилер заменит
    # эту проводку разнесёнными по статьям строками и привяжет операцию (без задвоения).
    if pending_card is not None:
        card_wallet = await session.scalar(
            select(Wallet).where(Wallet.code == CARD_WALLET_CODE, Wallet.status == "active")
        )
        if card_wallet is None:
            raise KassaChequeError("Счёт бизнес-карты (Т-Банк рублёвый) не найден")
        placeholder = CashflowTransaction(
            wallet_id=card_wallet.id,
            direction="out",
            amount=pending_card,
            operation_date=issued_at.date(),
            # С позициями статьи берём из них при матче; без позиций (фолбэк) — единая статья чека.
            article_id=None if track_nomenclature else article_id,
            counterparty_id=counterparty_id,
            source_kind="kassa_cheque",
            source_id=invoice.id,
            payment_purpose=f"Чек {invoice.number} (ожидает подтверждения банком)",
            comment=comment,
            quality_status=AWAITING_BANK_QUALITY,
        )
        session.add(placeholder)
        await session.flush()
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id,
                source_kind=PENDING_CARD_ALLOCATION_SOURCE,
                cashflow_transaction_id=placeholder.id,
                amount=pending_card,
                created_by_user_id=actor_user_id,
            )
        )
        await session.flush()

    # --- наличная часть: движения ДДС по статьям с кассы Черниковой + аллокация --
    if cash_total > 0 and cash_wallet is not None:
        first_cash_txn_id: uuid.UUID | None = None
        for art_id, share in _allocate_by_articles(cash_total, article_sums, paid_total):
            transaction = CashflowTransaction(
                wallet_id=cash_wallet.id,
                direction="out",
                amount=share,
                operation_date=issued_at.date(),
                article_id=art_id,
                counterparty_id=counterparty_id,
                source_kind="kassa_cheque",
                source_id=invoice.id,
                payment_purpose=f"Чек {invoice.number} (наличные)",
                comment=comment,
                quality_status="final",
            )
            session.add(transaction)
            await session.flush()
            if first_cash_txn_id is None:
                first_cash_txn_id = transaction.id
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id,
                source_kind="cash",
                cashflow_transaction_id=first_cash_txn_id,
                amount=cash_total,
                created_by_user_id=actor_user_id,
            )
        )
        await session.flush()

    await _recompute_status(session, invoice)
    await session.commit()
    await session.refresh(invoice)
    return invoice


# --- Пендинг-чек: матч с пришедшей выпиской и эскалация «банк не передал» --------------
#
# Ручной чек (полная сумма, ``quality_status='awaiting_bank'``) сводим с реальной card-
# операцией Т-Банка ПОСЛЕ её импорта (вызывается из синка банка ДО общей классификации,
# см. ``scheduler.sync_bank_provider``). При матче плейсхолдер заменяется разнесёнными по
# статьям проводками и операция привязывается к чеку — повторного расхода не возникает.


async def _pending_article_sums(
    session: AsyncSession, invoice: SupplierInvoice, placeholder: CashflowTransaction
) -> list[tuple[uuid.UUID, Decimal]]:
    """Разбивка чека по статьям ДДС для разнесения при матче (как при создании)."""
    lines = (
        await session.scalars(
            select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == invoice.id)
        )
    ).all()
    sums: dict[uuid.UUID, Decimal] = {}
    for line in lines:
        if line.dds_article_id is None or line.is_return:
            continue
        sums[line.dds_article_id] = sums.get(line.dds_article_id, Decimal("0.00")) + _money(
            line.sum
        )
    if sums:
        return list(sums.items())
    if placeholder.article_id is not None:
        return [(placeholder.article_id, _money(invoice.amount))]
    raise KassaChequeError("Не удалось определить статьи чека для разнесения")


def _operation_purchase_date(operation: BankOperation) -> date:
    """День ПОКУПКИ по карте (authorizationDate), а не день проводки (settlement-лаг 1–3 дня)."""
    purchased = _purchase_dt(operation)
    if purchased is not None:
        return purchased.astimezone(BUSINESS_TZ).date()
    return operation.operation_date


async def _find_matching_card_op(
    session: AsyncSession, placeholder: CashflowTransaction, window_days: int
) -> BankOperation | None:
    """Непривязанная card-операция Т-Банка под пендинг-чек: тот же счёт, точная сумма, день покупки.

    Матчим по ДНЮ ПОКУПКИ (placeholder.operation_date = дата чека): банк проводит покупку
    позже (settlement), поэтому SQL-окно по дате проводки берём с запасом вперёд, а точную
    близость считаем по authorizationDate — устойчиво к понедельничной пачке пт–вс.
    """
    low = placeholder.operation_date - timedelta(days=window_days)
    high = placeholder.operation_date + timedelta(days=window_days + 14)
    candidates = (
        await session.scalars(
            select(BankOperation)
            .where(
                BankOperation.provider == "tbank",
                BankOperation.direction == "out",
                BankOperation.amount == placeholder.amount,
                BankOperation.transfer_group_id.is_(None),
                BankOperation.cashflow_transaction_id.is_(None),
                BankOperation.operation_date >= low,
                BankOperation.operation_date <= high,
            )
            .order_by(BankOperation.operation_date)
        )
    ).all()
    for operation in candidates:
        if not _is_card_purchase(operation):
            continue
        purchase_date = _operation_purchase_date(operation)
        if abs((purchase_date - placeholder.operation_date).days) > window_days:
            continue
        if await _op_already_allocated(session, operation.id):
            continue
        wallet = await _wallet_for_operation(session, operation)
        if wallet is None or wallet.id != placeholder.wallet_id:
            continue
        return operation
    return None


async def _settle_pending_cheque(
    session: AsyncSession, placeholder: CashflowTransaction, operation: BankOperation
) -> None:
    """Свести пендинг-чек с операцией: разнести по статьям, привязать операцию, закрыть кейсы."""
    invoice = await session.get(SupplierInvoice, placeholder.source_id)
    if invoice is None:
        return
    wallet_id = placeholder.wallet_id
    comment = placeholder.comment
    paid_total = _money(invoice.amount)
    card_amount = _money(operation.amount)
    article_sums = await _pending_article_sums(session, invoice, placeholder)

    # Снять пендинг-проводку и её аллокацию — взамен заведём разнесённые по статьям.
    pending_allocs = (
        await session.scalars(
            select(InvoicePaymentAllocation).where(
                InvoicePaymentAllocation.invoice_id == invoice.id,
                InvoicePaymentAllocation.source_kind == PENDING_CARD_ALLOCATION_SOURCE,
            )
        )
    ).all()
    for alloc in pending_allocs:
        await session.delete(alloc)
    await session.delete(placeholder)
    await session.flush()

    first_txn_id: uuid.UUID | None = None
    for art_id, share in _allocate_by_articles(card_amount, article_sums, paid_total):
        transaction = CashflowTransaction(
            wallet_id=wallet_id,
            direction="out",
            amount=share,
            operation_date=operation.operation_date,
            article_id=art_id,
            counterparty_id=invoice.counterparty_id,
            source_kind="kassa_cheque",
            source_id=invoice.id,
            payment_purpose=f"Чек {invoice.number}",
            comment=comment,
            quality_status="final",
        )
        session.add(transaction)
        await session.flush()
        if first_txn_id is None:
            first_txn_id = transaction.id
    operation.cashflow_transaction_id = first_txn_id
    operation.classification_status = "classified"
    session.add(
        InvoicePaymentAllocation(
            invoice_id=invoice.id,
            source_kind="bank",
            bank_operation_id=operation.id,
            amount=card_amount,
        )
    )
    await session.flush()
    await _resolve_cases(session, bank_operation_id=operation.id, invoice_id=invoice.id)
    await _recompute_status(session, invoice)


async def _resolve_cases(
    session: AsyncSession, *, bank_operation_id: uuid.UUID, invoice_id: uuid.UUID
) -> None:
    """Закрыть pending-кейсы по этой операции и «банк не передал» по этому чеку."""
    cases = (
        await session.scalars(
            select(ReconciliationCase).where(ReconciliationCase.status == "pending")
        )
    ).all()
    for case in cases:
        is_op_case = case.bank_operation_id == bank_operation_id
        is_cheque_case = case.kind == UNCONFIRMED_CHEQUE_CASE_KIND and str(
            (case.payload or {}).get("invoice_id")
        ) == str(invoice_id)
        if is_op_case or is_cheque_case:
            case.status = "resolved"
            case.resolved_at = datetime.now(UTC)
            case.resolution_payload = {"reason": "matched_pending_cheque"}


async def match_pending_cheque_operations(
    session: AsyncSession, *, window_days: int = PENDING_CHEQUE_MATCH_WINDOW_DAYS
) -> int:
    """Свести все ожидающие подтверждения чеки с пришедшими card-операциями. Без commit."""
    placeholders = (
        await session.scalars(
            select(CashflowTransaction).where(
                CashflowTransaction.source_kind == "kassa_cheque",
                CashflowTransaction.quality_status == AWAITING_BANK_QUALITY,
            )
        )
    ).all()
    matched = 0
    for placeholder in placeholders:
        operation = await _find_matching_card_op(session, placeholder, window_days)
        if operation is None:
            continue
        await _settle_pending_cheque(session, placeholder, operation)
        matched += 1
    return matched


# --- Возвраты карт-покупок (refundIn): молчаливая привязка к чеку, ждущему возврат -----
#
# Чек с возвращёнными позициями проведён net, а его карт-операция «недоиспользована»:
# |операция| − bank-аллокация = ожидаемый от банка возврат. Когда refundIn приезжает в
# выписку (поллинг/вебхук — оба через ingest_operations), он опознаётся по rrn/authCode
# исходной покупки и привязывается к ДДС-проводке чека БЕЗ участия человека — строка не
# попадает в «Требует разбора». Всё, что не сошлось, остаётся needs_review (фаза 2 — кейс).


async def _cheque_expectation(
    session: AsyncSession, purchase: BankOperation
) -> tuple[SupplierInvoice, Decimal] | None:
    """(чек Кассы, ожидание возврата = |операция| − Σ bank-аллокаций) по карт-покупке.

    ``None`` — операция разнесена не чеком (ручная классификация, оплата накладной):
    такие операции возвраты не приманивают. Ожидание может быть 0 — чек проведён
    полностью, возврата не ждали (кейс фазы 2).
    """
    if purchase.cashflow_transaction_id is None:
        return None
    alloc_rows = (
        await session.execute(
            select(InvoicePaymentAllocation.amount, SupplierInvoice)
            .join(SupplierInvoice, SupplierInvoice.id == InvoicePaymentAllocation.invoice_id)
            .where(InvoicePaymentAllocation.bank_operation_id == purchase.id)
        )
    ).all()
    if not alloc_rows or any(invoice.source != "kassa_cheque" for _, invoice in alloc_rows):
        return None
    alloc_total = sum((_money(amount) for amount, _ in alloc_rows), Decimal("0.00"))
    expected = _money(abs(purchase.amount)) - alloc_total
    return alloc_rows[0][1], expected


async def _refunds_of_purchase(
    session: AsyncSession, purchase: BankOperation
) -> list[BankOperation]:
    """Все refundIn того же rrn/authCode на том же счёте (привязанные и нет)."""
    rrn_auth = _op_rrn_auth(purchase)
    if rrn_auth is None:
        return []
    rrn, auth = rrn_auth
    return list(
        (
            await session.scalars(
                select(BankOperation)
                .where(
                    BankOperation.provider == purchase.provider,
                    BankOperation.direction == "in",
                    BankOperation.account_id == purchase.account_id,
                    BankOperation.transfer_group_id.is_(None),
                    BankOperation.raw_payload["category"].astext == TBANK_REFUND_CATEGORY,
                    BankOperation.raw_payload["rrn"].astext == rrn,
                    BankOperation.raw_payload["authCode"].astext == auth,
                )
                .order_by(BankOperation.operation_date)
            )
        ).all()
    )


async def _resolve_refund_missing_cases(
    session: AsyncSession, purchase_operation_id: uuid.UUID
) -> None:
    """Закрыть pending-кейсы «возврат не пришёл» по покупке — ожидание выбрано."""
    cases = (
        await session.scalars(
            select(ReconciliationCase).where(
                ReconciliationCase.kind == REFUND_MISSING_CASE_KIND,
                ReconciliationCase.bank_operation_id == purchase_operation_id,
                ReconciliationCase.status == "pending",
            )
        )
    ).all()
    for case in cases:
        await close_reconciliation_case(
            session, case, status="resolved", resolution_payload={"reason": "refund_attached"}
        )


async def _attach_refunds_for_purchase(session: AsyncSession, purchase: BankOperation) -> int:
    """Привязать пришедшие refundIn к карт-покупке, по которой чек ждёт возврат.

    Гейты: покупка классифицирована чеком Кассы, ожидание > 0. Возвраты того же
    rrn/authCode привязываются по очереди, пока не выберут ожидание; каждый получает
    ссылку на ДДС-проводку чека и статус ``classified``. Возврат больше остатка
    ожидания не привязывается (кейс поднимет матчер). Идемпотентно, без commit.
    """
    expectation = await _cheque_expectation(session, purchase)
    if expectation is None:
        return 0
    _invoice, expected = expectation
    if expected <= 0:
        return 0

    refunds = await _refunds_of_purchase(session, purchase)
    outstanding = expected - sum(
        (_money(abs(r.amount)) for r in refunds if r.cashflow_transaction_id is not None),
        Decimal("0.00"),
    )
    attached = 0
    for refund in refunds:
        if refund.cashflow_transaction_id is not None:
            continue
        amount = _money(abs(refund.amount))
        if amount > outstanding:
            continue
        refund.cashflow_transaction_id = purchase.cashflow_transaction_id
        refund.classification_status = "classified"
        outstanding = _money(outstanding - amount)
        attached += 1
    if attached and outstanding <= 0:
        await _resolve_refund_missing_cases(session, purchase.id)
    return attached


async def _ensure_late_refund_case(
    session: AsyncSession,
    *,
    refund: BankOperation,
    purchase: BankOperation,
    invoice: SupplierInvoice,
    expected: Decimal,
) -> bool:
    """Поднять кейс «возврат по проведённому чеку» (дедуп по pending kind+операция)."""
    existing = await session.scalar(
        select(ReconciliationCase.id).where(
            ReconciliationCase.kind == CARD_REFUND_CASE_KIND,
            ReconciliationCase.bank_operation_id == refund.id,
            ReconciliationCase.status == "pending",
        )
    )
    if existing is not None:
        return False
    session.add(
        ReconciliationCase(
            kind=CARD_REFUND_CASE_KIND,
            status="pending",
            provider=refund.provider,
            bank_operation_id=refund.id,
            payload={
                "invoice_id": str(invoice.id),
                "cheque_number": invoice.number,
                "refund_amount": str(_money(abs(refund.amount))),
                "purchase_operation_id": str(purchase.id),
                "purchase_amount": str(_money(abs(purchase.amount))),
                "rrn": (_op_rrn_auth(refund) or ("", ""))[0],
                # Чек не ждал возврат вовсе или возврат больше остатка ожидания.
                "reason": "cheque_did_not_expect" if expected <= 0 else "exceeds_expected",
            },
        )
    )
    return True


async def match_card_refund_operations(session: AsyncSession) -> int:
    """Свести непривязанные возвраты (refundIn) с чеками, ждущими возврат. Без commit.

    Вызывается из ``ingest_operations`` ДО общей классификации (как матч пендинг-чеков),
    поэтому покрывает оба пути выписки — поллинг и вебхук. Точный матч привязывается
    молча; возврат по чеку, который его НЕ ждал (или больше остатка ожидания), поднимает
    кейс «Требует разбора» с действием «Учесть возврат». Возвращает число привязанных.
    """
    refund_rows = (
        await session.scalars(
            select(BankOperation).where(
                BankOperation.provider == "tbank",
                BankOperation.direction == "in",
                BankOperation.cashflow_transaction_id.is_(None),
                BankOperation.transfer_group_id.is_(None),
                BankOperation.raw_payload["category"].astext == TBANK_REFUND_CATEGORY,
            )
        )
    ).all()
    matched = 0
    seen_purchases: set[uuid.UUID] = set()
    for refund in refund_rows:
        rrn_auth = _op_rrn_auth(refund)
        if rrn_auth is None:
            continue
        rrn, auth = rrn_auth
        purchase = await session.scalar(
            select(BankOperation).where(
                BankOperation.provider == "tbank",
                BankOperation.direction == "out",
                BankOperation.account_id == refund.account_id,
                BankOperation.cashflow_transaction_id.is_not(None),
                BankOperation.raw_payload["category"].astext == "cardOperation",
                BankOperation.raw_payload["rrn"].astext == rrn,
                BankOperation.raw_payload["authCode"].astext == auth,
            )
        )
        if purchase is None:
            continue
        expectation = await _cheque_expectation(session, purchase)
        if expectation is None:
            # Покупка разнесена не чеком — обычный разбор выписки, не наш кейс.
            continue
        invoice, expected = expectation
        if purchase.id not in seen_purchases:
            # Один вызов закрывает ВСЕ подходящие возвраты этой покупки.
            seen_purchases.add(purchase.id)
            matched += await _attach_refunds_for_purchase(session, purchase)
        if refund.cashflow_transaction_id is None:
            # Чек есть, но возврат не влез в ожидание (или его не ждали) — фаза 2: кейс.
            await _ensure_late_refund_case(
                session, refund=refund, purchase=purchase, invoice=invoice, expected=expected
            )
    return matched


async def apply_card_refund_case(
    session: AsyncSession, case: ReconciliationCase
) -> dict[str, Any]:
    """Действие «Учесть возврат» по кейсу «возврат по проведённому чеку». Без commit.

    Книжит входящую ДДС-проводку (кошелёк операции возврата, служебная статья «Возврат
    расходов») — история чека НЕ мутируется, iiko остаётся как проведено (изъятия
    необратимы). Возврат привязывается к проводке (classified), кейс закрывается.
    Повтор по уже привязанному возврату просто закрывает кейс.
    """
    if case.kind != CARD_REFUND_CASE_KIND:
        raise KassaChequeError("Кейс не является возвратом по чеку")
    if case.bank_operation_id is None:
        raise KassaChequeError("Кейс не привязан к операции возврата")
    refund = await session.get(BankOperation, case.bank_operation_id)
    if refund is None:
        raise KassaChequeError("Операция возврата не найдена")

    invoice: SupplierInvoice | None = None
    raw_invoice_id = (case.payload or {}).get("invoice_id")
    if raw_invoice_id:
        invoice = await session.get(SupplierInvoice, uuid.UUID(str(raw_invoice_id)))

    if refund.cashflow_transaction_id is not None:
        # Уже учтён (гонка/повторный клик) — просто закрываем кейс.
        await close_reconciliation_case(
            session, case, status="resolved", resolution_payload={"reason": "already_linked"}
        )
        return {"transaction_id": refund.cashflow_transaction_id, "already_linked": True}

    wallet = await _wallet_for_operation(session, refund)
    if wallet is None:
        raise KassaChequeError("Не удалось определить счёт по операции возврата")

    article = await session.scalar(
        select(DdsArticle).where(DdsArticle.code == EXPENSE_REFUND_ARTICLE_CODE)
    )
    if article is None:
        article = DdsArticle(
            code=EXPENSE_REFUND_ARTICLE_CODE,
            name=EXPENSE_REFUND_ARTICLE_NAME,
            movement_type="inflow",
            activity_type="operating",
            is_active=True,
            description=(
                "Служебная: возвраты карт-покупок по проведённым чекам Кассы "
                "(кнопка «Учесть возврат» в «Требует разбора»)"
            ),
        )
        session.add(article)
        await session.flush()

    transaction = CashflowTransaction(
        wallet_id=wallet.id,
        direction="in",
        amount=_money(abs(refund.amount)),
        operation_date=refund.operation_date,
        article_id=article.id,
        counterparty_id=invoice.counterparty_id if invoice else None,
        source_kind="kassa_cheque_refund",
        source_id=invoice.id if invoice else None,
        payment_purpose=(
            f"Возврат по чеку {invoice.number}" if invoice else "Возврат карт-покупки"
        ),
        quality_status="final",
    )
    session.add(transaction)
    await session.flush()
    refund.cashflow_transaction_id = transaction.id
    refund.classification_status = "classified"
    await close_reconciliation_case(
        session,
        case,
        status="resolved",
        resolution_payload={"cashflow_transaction_id": str(transaction.id)},
    )
    return {"transaction_id": transaction.id, "already_linked": False}


async def get_refund_wait_alert_days(session: AsyncSession) -> int:
    """Порог «возврат не пришёл» (дней) из Настроек; по умолчанию 7 (постинг банка 1–3 дня)."""
    setting = await session.scalar(
        select(AppSetting).where(AppSetting.key == REFUND_WAIT_ALERT_SETTING_KEY)
    )
    if setting is None:
        return REFUND_WAIT_ALERT_DEFAULT_DAYS
    try:
        days = int(setting.value)
    except (TypeError, ValueError):
        return REFUND_WAIT_ALERT_DEFAULT_DAYS
    return days if days > 0 else REFUND_WAIT_ALERT_DEFAULT_DAYS


async def escalate_missing_cheque_refunds(
    session: AsyncSession, *, now: datetime | None = None
) -> int:
    """Поднять в «Требует разбора» чеки, ждущие возврат дольше порога. Без commit.

    Ожидание = |операция| − аллокация − уже привязанные возвраты. Кейс с дедупом по
    pending (kind + покупка); закрывается автоматически при привязке возврата
    (``_attach_refunds_for_purchase``) либо руками («Отложить»).
    """
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(days=await get_refund_wait_alert_days(session))
    rows = (
        await session.execute(
            select(BankOperation, InvoicePaymentAllocation.amount, SupplierInvoice)
            .join(
                InvoicePaymentAllocation,
                InvoicePaymentAllocation.bank_operation_id == BankOperation.id,
            )
            .join(SupplierInvoice, SupplierInvoice.id == InvoicePaymentAllocation.invoice_id)
            .where(SupplierInvoice.source == "kassa_cheque")
        )
    ).all()
    created = 0
    for purchase, alloc_amount, invoice in rows:
        expected = _money(abs(purchase.amount)) - _money(alloc_amount)
        if expected <= 0:
            continue
        if invoice.created_at is not None and invoice.created_at > cutoff:
            continue
        refunds = await _refunds_of_purchase(session, purchase)
        attached_total = sum(
            (_money(abs(r.amount)) for r in refunds if r.cashflow_transaction_id is not None),
            Decimal("0.00"),
        )
        outstanding = expected - attached_total
        if outstanding <= 0:
            continue
        existing = await session.scalar(
            select(ReconciliationCase.id).where(
                ReconciliationCase.kind == REFUND_MISSING_CASE_KIND,
                ReconciliationCase.bank_operation_id == purchase.id,
                ReconciliationCase.status == "pending",
            )
        )
        if existing is not None:
            continue
        session.add(
            ReconciliationCase(
                kind=REFUND_MISSING_CASE_KIND,
                status="pending",
                provider=purchase.provider,
                bank_operation_id=purchase.id,
                payload={
                    "invoice_id": str(invoice.id),
                    "cheque_number": invoice.number,
                    "expected_refund": str(outstanding),
                    "waiting_since": (
                        invoice.created_at.isoformat() if invoice.created_at else None
                    ),
                    "reason": "refund_not_received",
                },
            )
        )
        created += 1
    await session.flush()
    return created


async def get_pending_cheque_alert_days(session: AsyncSession) -> int:
    """Порог «банк не передал» (дней) из Настроек; по умолчанию 4."""
    setting = await session.scalar(
        select(AppSetting).where(AppSetting.key == PENDING_CHEQUE_ALERT_SETTING_KEY)
    )
    if setting is None:
        return PENDING_CHEQUE_ALERT_DEFAULT_DAYS
    try:
        days = int(setting.value)
    except (TypeError, ValueError):
        return PENDING_CHEQUE_ALERT_DEFAULT_DAYS
    return days if days > 0 else PENDING_CHEQUE_ALERT_DEFAULT_DAYS


async def _has_open_unconfirmed_case(session: AsyncSession, invoice_id: uuid.UUID) -> bool:
    cases = (
        await session.scalars(
            select(ReconciliationCase).where(
                ReconciliationCase.kind == UNCONFIRMED_CHEQUE_CASE_KIND,
                ReconciliationCase.status == "pending",
            )
        )
    ).all()
    return any(str((case.payload or {}).get("invoice_id")) == str(invoice_id) for case in cases)


async def escalate_overdue_pending_cheques(
    session: AsyncSession, *, now: datetime | None = None
) -> int:
    """Поднять в «Требует разбора» чеки, которые ждут подтверждения банком дольше порога.

    Создаёт ``ReconciliationCase(kind='unconfirmed_cheque')`` — он попадает в счётчик
    «Требует разбора: N», который видит менеджер. Без commit (коммитит вызывающий джоб).
    """
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(days=await get_pending_cheque_alert_days(session))
    placeholders = (
        await session.scalars(
            select(CashflowTransaction).where(
                CashflowTransaction.source_kind == "kassa_cheque",
                CashflowTransaction.quality_status == AWAITING_BANK_QUALITY,
                CashflowTransaction.created_at <= cutoff,
            )
        )
    ).all()
    created = 0
    for placeholder in placeholders:
        invoice = await session.get(SupplierInvoice, placeholder.source_id)
        if invoice is None:
            continue
        if await _has_open_unconfirmed_case(session, invoice.id):
            continue
        session.add(
            ReconciliationCase(
                kind=UNCONFIRMED_CHEQUE_CASE_KIND,
                status="pending",
                provider="tbank",
                bank_operation_id=None,
                payload={
                    "invoice_id": str(invoice.id),
                    "cheque_number": invoice.number,
                    "amount": str(_money(placeholder.amount)),
                    "issued_at": invoice.issued_at.isoformat() if invoice.issued_at else None,
                    "waiting_since": placeholder.created_at.isoformat()
                    if placeholder.created_at
                    else None,
                    "reason": "bank_operation_missing",
                },
            )
        )
        created += 1
    await session.flush()
    return created


async def list_cheque_articles(session: AsyncSession) -> list[DdsArticle]:
    """Статьи ДДС, доступные для позиций чека (белый список ``CHEQUE_ARTICLE_NAMES``)."""
    result = await session.scalars(
        select(DdsArticle)
        .where(
            DdsArticle.name.in_(CHEQUE_ARTICLE_NAMES),
            DdsArticle.is_active.is_(True),
            DdsArticle.movement_type == "outflow",
        )
        .order_by(DdsArticle.name)
    )
    return list(result.all())


async def list_cheques(
    session: AsyncSession, *, limit: int = 100, offset: int = 0
) -> list[dict[str, Any]]:
    invoices = (
        await session.scalars(
            select(SupplierInvoice)
            .where(SupplierInvoice.source == "kassa_cheque")
            .order_by(SupplierInvoice.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [await _cheque_payload(session, invoice) for invoice in invoices]


async def get_cheque(session: AsyncSession, cheque_id: uuid.UUID) -> dict[str, Any] | None:
    invoice = await session.get(SupplierInvoice, cheque_id)
    if invoice is None or invoice.source != "kassa_cheque":
        return None
    return await _cheque_payload(session, invoice)


async def _cheque_payload(session: AsyncSession, invoice: SupplierInvoice) -> dict[str, Any]:
    counterparty = await session.get(Counterparty, invoice.counterparty_id)
    allocations = (
        await session.scalars(
            select(InvoicePaymentAllocation).where(
                InvoicePaymentAllocation.invoice_id == invoice.id
            )
        )
    ).all()
    lines = (
        await session.scalars(
            select(InvoiceLineItem)
            .where(InvoiceLineItem.invoice_id == invoice.id)
            .order_by(InvoiceLineItem.sort_order)
        )
    ).all()
    # Статья чека — общая у всех его движений ДДС (берём первую).
    article_row = (
        await session.execute(
            select(DdsArticle.id, DdsArticle.name)
            .join(CashflowTransaction, CashflowTransaction.article_id == DdsArticle.id)
            .where(
                CashflowTransaction.source_kind == "kassa_cheque",
                CashflowTransaction.source_id == invoice.id,
            )
            .limit(1)
        )
    ).first()
    # Имена статей ДДС позиций (для отображения построчно).
    article_ids = {line.dds_article_id for line in lines if line.dds_article_id is not None}
    article_names: dict[uuid.UUID, str] = {}
    if article_ids:
        rows = await session.execute(
            select(DdsArticle.id, DdsArticle.name).where(DdsArticle.id.in_(article_ids))
        )
        article_names = {row[0]: row[1] for row in rows.all()}
    returned_total = sum(
        (_money(line.sum) for line in lines if line.is_return), Decimal("0.00")
    )
    return {
        "id": invoice.id,
        "number": invoice.number,
        "counterparty_id": invoice.counterparty_id,
        "counterparty_name": counterparty.name if counterparty else "—",
        "issued_at": invoice.issued_at.isoformat() if invoice.issued_at else None,
        "amount": float(_money(invoice.amount)),
        "returned_total": float(returned_total),
        "payment_status": invoice.payment_status,
        "article_id": article_row[0] if article_row else None,
        "article_name": article_row[1] if article_row else None,
        "allocations": [
            {
                "id": allocation.id,
                "source_kind": allocation.source_kind,
                "bank_operation_id": allocation.bank_operation_id,
                "cashflow_transaction_id": allocation.cashflow_transaction_id,
                "amount": float(_money(allocation.amount)),
            }
            for allocation in allocations
        ],
        "lines": [
            {
                "id": line.id,
                "name": line.name,
                "article": line.article,
                "unit": line.unit,
                "quantity": float(_qty(line.quantity)),
                "price": float(_money(line.price)),
                "sum": float(_money(line.sum)),
                "vat_percent": float(line.vat_percent) if line.vat_percent is not None else None,
                "dds_article_id": line.dds_article_id,
                "dds_article_name": article_names.get(line.dds_article_id),
                "is_return": line.is_return,
            }
            for line in lines
        ],
    }

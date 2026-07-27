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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
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
from app.services.warehouse_invoices import supplier_payment_article_ids

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


def _assert_goods_lines_have_product(
    lines: Sequence[ChequeLineInput],
    cheque_article_id: uuid.UUID | None,
    products: dict[uuid.UUID, IikoProduct],
    supplier_article_ids: set[uuid.UUID],
) -> None:
    """Товарная строка чека обязана быть сопоставлена с номенклатурой iiko.

    Тот же гард, что у накладной (``warehouse_invoices._assert_goods_have_product``), которого
    чекам не хватало: строку без ``product_guid`` выгрузка в iiko МОЛЧА отбрасывает
    (``warehouse_invoice_push.prepare_push``: ``if not line.product_guid: continue``) — приход
    уходит неполным, сумма в iiko меньше суммы чека, а статус остаётся «отправлена в iiko», так
    что расхождение незаметно. Так в чеке Ч-44 (23.07.2026) потерялся «кефир», набранный
    текстом без выбора товара.

    Признак товарной строки у чека — статья «Оплата поставщикам» (у чека ``is_staff`` всегда
    false, а статья есть у каждой строки), поэтому критерий по СТАТЬЕ, как в ``prepare_push`` и
    ``line_is_goods``, а не по ``dds_article_id is None``, как у накладной. Строки расходных
    статей (питание персонала, содержание точек) в iiko приходом не идут — номенклатура им не
    нужна, это и есть путь для покупки без номенклатуры: блок «Прочие расходы».

    Возвращённые (``is_return``) строки тоже проверяем: в iiko они не уходят, но правило «в
    складском блоке — только номенклатура» должно быть одним для всех строк, иначе снятая
    пометка возврата тихо вернёт дырку.
    """
    for line in lines:
        article_id = line.dds_article_id or cheque_article_id
        if article_id is None or article_id not in supplier_article_ids:
            continue
        if line.iiko_product_id is None or products.get(line.iiko_product_id) is None:
            raise KassaChequeError(
                f"Позиция «{line.name or '—'}»: выберите конкретный товар из номенклатуры iiko. "
                "Товарную позицию нельзя сохранить без сопоставления — иначе она потеряется "
                "при выгрузке чека в iiko. Нет такого товара в номенклатуре — внесите позицию "
                "в «Прочие расходы»."
            )


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
    # Непривязанные возвраты (refundIn) на ТОЙ ЖЕ КАРТЕ, уже пришедшие в выписку (надёжного
    # ключа к конкретной покупке нет). UI подсвечивает: «на карте есть возврат — отметьте позиции».
    refund_amount: Decimal | None = None
    refund_count: int = 0


def _is_card_purchase(operation: BankOperation) -> bool:
    """True, если операция — покупка по бизнес-карте (а не платёж контрагенту/перевод).

    ``category`` у боевого токена T-Банка есть у КАЖДОЙ операции и является решающим:
    ``cardOperation`` — покупка по карте (наша цель), любая другая (``contragentOutcome`` —
    оплата поставщику, ``fee``/``tax``/``budget`` и т.п.) — НЕ покупка, даже если у неё
    пустой блок контрагента и есть ``merch``/``cardNumber`` (боевой кейс: оплаты поставщикам
    ошибочно попадали в пикер чеков). Фолбэк по реквизитам — только для легаси-операций без
    ``category`` (неизвестный токен).
    """
    if operation.direction != "out" or operation.transfer_group_id is not None:
        return False
    raw = operation.raw_payload or {}
    category = str(raw.get("category") or "").strip()
    if category == "cardOperation":
        return True
    if category:
        # Категория есть, но не карт-операция → это не покупка по карте.
        return False
    # Легаси-операция без category — эвристика: расход без реквизитов контрагента.
    receiver = _receiver_block(raw)
    inn = str(receiver.get("inn") or operation.counterparty_inn_raw or "")
    name = str(receiver.get("name") or operation.counterparty_name_raw or "")
    if inn in BANK_NOISE_INNS or "ТБАНК" in name.upper():
        return False
    account = str(receiver.get("acct") or operation.counterparty_account_raw or "")
    return not inn and not account


# Категория T-Банка у возврата карт-покупки (деньги вернулись на счёт).
#
# ⚠️ Надёжного ПЕР-ТРАНЗАКЦИОННОГО ключа «покупка ↔ возврат» банк НЕ даёт (проверено боевой
# выпиской 2026-07-06, чек Ч-22): у настоящего возврата (``refundIn``) свой ``rrn``
# (≠ покупке), ``authCode`` отсутствует, а ``ucid`` — идентификатор ПАЧКИ (десятки операций
# под одним ``ucid``). Совпадают только ``cardNumber``/мерчант/``ucid`` — все НЕ уникальны.
# Поэтому возврат матчим не к покупке, а к ЧЕКУ по декларации кассира: та же карта +
# сумма возврата == ожидаемому возврату чека (Σ помеченных строк ``is_return``). Для
# reversal-«отмены» rrn совпадал бы, но это частный случай — не полагаемся на него.
TBANK_REFUND_CATEGORY = "refundIn"


def _is_card_refund(operation: BankOperation) -> bool:
    """True, если операция — возврат карт-покупки (``refundIn``)."""
    if operation.direction != "in" or operation.transfer_group_id is not None:
        return False
    raw = operation.raw_payload or {}
    return str(raw.get("category") or "").strip() == TBANK_REFUND_CATEGORY


def _op_card(operation: BankOperation) -> str | None:
    """Маскированный номер карты (``cardNumber``) — общий у покупки и её возврата на одной
    карте. Единственный устойчивый признак для card-scoped сопоставления возврата с чеком."""
    raw = operation.raw_payload or {}
    card = str(raw.get("cardNumber") or "").strip()
    return card or None


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
    card_by_op: dict[uuid.UUID, str | None] = {}
    for operation in operations:
        if not _is_card_purchase(operation):
            continue
        if operation.id in allocated_ids:
            continue
        card_by_op[operation.id] = _op_card(operation)

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

    # Card-scoped подсказка о возвратах: у показанных покупок ищем непривязанные refundIn
    # НА ТОЙ ЖЕ КАРТЕ (надёжного ключа к конкретной покупке банк не даёт — см. _op_card).
    # Показываем «на карте есть непривязанный возврат N ₽» — кассир решает, к этой ли покупке,
    # и отмечает позиции. Точную привязку по (карта + сумма ожидаемого возврата) делает матчер.
    cards = {card for card in card_by_op.values() if card}
    if cards:
        refund_rows = (
            await session.scalars(
                select(BankOperation).where(
                    BankOperation.provider == "tbank",
                    BankOperation.direction == "in",
                    BankOperation.cashflow_transaction_id.is_(None),
                    BankOperation.transfer_group_id.is_(None),
                    BankOperation.raw_payload["category"].astext == TBANK_REFUND_CATEGORY,
                    BankOperation.raw_payload["cardNumber"].astext.in_(cards),
                )
            )
        ).all()
        refunds_by_card: dict[str, list[BankOperation]] = {}
        for refund in refund_rows:
            card = _op_card(refund)
            if card is not None:
                refunds_by_card.setdefault(card, []).append(refund)
        for candidate in candidates:
            card = card_by_op.get(candidate.bank_operation_id)
            # Возврат не может превышать покупку по сумме И не может быть раньше самой покупки
            # (более ранний refundIn — за другую, прошлую покупку). Показываем только такие.
            matched = [
                r
                for r in refunds_by_card.get(card or "", [])
                if _money(abs(r.amount)) <= candidate.amount
                and _refund_not_before_purchase(
                    r, candidate.purchased_at, candidate.operation_date
                )
            ]
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

    # --- номенклатура товарных строк: гард ДО записи ---------------------------
    # Товар из кэша номенклатуры (по нему же ниже заполняем product_guid/article/unit) и
    # UUID-ы статей «Оплата поставщикам» — признак ТОВАРНОЙ строки чека.
    products: dict[uuid.UUID, IikoProduct] = {}
    if track_nomenclature and line_inputs:
        product_ids = [li.iiko_product_id for li in line_inputs if li.iiko_product_id]
        if product_ids:
            rows = (
                await session.scalars(select(IikoProduct).where(IikoProduct.id.in_(product_ids)))
            ).all()
            products = {product.id: product for product in rows}
        _assert_goods_lines_have_product(
            line_inputs, article_id, products, await supplier_payment_article_ids(session)
        )

    # --- создаём чек (накладную) -----------------------------------------------
    invoice = SupplierInvoice(
        counterparty_id=counterparty_id,
        source="kassa_cheque",
        direction="payable",
        doc_kind="closing",  # чек Кассы — оплаченная покупка = приход = закрывающий
        operational_scope="warehouse",
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
    # сразу, не дожидаясь следующего синка банка (только если ЭТОТ чек — единственный кандидат).
    if returned_total > 0 and resolved_bank:
        await _attach_matching_refunds_for_cheque(session, invoice, resolved_bank[0][0])
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
# Чек с возвращёнными позициями проведён net, ожидаемый от банка возврат = Σ строк is_return.
# Надёжного пер-транзакционного ключа «покупка↔возврат» банк НЕ даёт (см. _op_card), поэтому
# refundIn сопоставляется с ЧЕКОМ по декларации кассира: та же карта покупки + остаток
# ожидания == сумме возврата. Когда refundIn приезжает в выписку (поллинг/вебхук — оба через
# ingest_operations), однозначное совпадение привязывается к ДДС-проводке чека БЕЗ участия
# человека — строка не попадает в «Требует разбора». Несопоставленное/неоднозначное с
# задержкой поднимает escalate_missing_cheque_refunds (даёт время создать чек).


# Окно матчинга возврата к чеку: возврат НЕ РАНЬШЕ самой покупки (refundIn физически
# происходит позже покупки — более ранний возврат заведомо за ДРУГУЮ, прошлую покупку на
# той же карте) и не позже +60 дней от неё (постинг банка 1–3 дня, берём с запасом). Якорь —
# момент ПОКУПКИ по ``authorizationDate``, а не дата ввода чека кассиром.
REFUND_MATCH_WINDOW_DAYS = 60


async def _cheque_expected_refund(session: AsyncSession, invoice_id: uuid.UUID) -> Decimal:
    """Ожидаемый возврат чека = Σ сумм помеченных строк ``is_return`` (декларация кассира)."""
    total = await session.scalar(
        select(func.coalesce(func.sum(InvoiceLineItem.sum), 0)).where(
            InvoiceLineItem.invoice_id == invoice_id,
            InvoiceLineItem.is_return.is_(True),
        )
    )
    return _money(total or 0)


async def _attached_refund_total(
    session: AsyncSession, cheque_txn_id: uuid.UUID | None
) -> Decimal:
    """Σ уже привязанных к проводке чека возвратов refundIn (для остатка ожидания)."""
    if cheque_txn_id is None:
        return Decimal("0.00")
    total = await session.scalar(
        select(func.coalesce(func.sum(func.abs(BankOperation.amount)), 0)).where(
            BankOperation.cashflow_transaction_id == cheque_txn_id,
            BankOperation.direction == "in",
            BankOperation.raw_payload["category"].astext == TBANK_REFUND_CATEGORY,
        )
    )
    return _money(total or 0)


def _refund_not_before_purchase(
    refund: BankOperation, purchase_dt: datetime | None, purchase_day: date | None
) -> bool:
    """Возврат физически не может произойти РАНЬШЕ покупки: ``refundIn`` в прошлом (напр.,
    за другую покупку месяцем ранее, всё ещё висящий непривязанным на карте) — со 100%
    вероятностью не относится к этой покупке. Сравниваем реальный момент операции
    (``authorizationDate``/``posted_at`` через ``_purchase_dt``); если точного момента с
    какой-то стороны нет — падаем на день проводки (``operation_date``)."""
    refund_dt = _purchase_dt(refund)
    if purchase_dt is not None and refund_dt is not None:
        return refund_dt >= purchase_dt
    purchase_d = purchase_dt.date() if purchase_dt is not None else purchase_day
    refund_d = refund_dt.date() if refund_dt is not None else refund.operation_date
    return purchase_d is None or refund_d is None or refund_d >= purchase_d


def _refund_within_window(refund: BankOperation, purchase: BankOperation) -> bool:
    """Возврат относится к покупке по времени: не раньше самой покупки (см.
    ``_refund_not_before_purchase``) и не позже +``REFUND_MATCH_WINDOW_DAYS`` от неё
    (защита от древних совпадений вперёд). Якорь — момент ПОКУПКИ, а не дата ввода чека."""
    purchase_dt = _purchase_dt(purchase)
    if not _refund_not_before_purchase(refund, purchase_dt, purchase.operation_date):
        return False
    refund_dt = _purchase_dt(refund)
    if purchase_dt is not None and refund_dt is not None:
        return refund_dt <= purchase_dt + timedelta(days=REFUND_MATCH_WINDOW_DAYS)
    purchase_d = purchase_dt.date() if purchase_dt is not None else purchase.operation_date
    refund_d = refund_dt.date() if refund_dt is not None else refund.operation_date
    return (
        purchase_d is None
        or refund_d is None
        or refund_d <= purchase_d + timedelta(days=REFUND_MATCH_WINDOW_DAYS)
    )


async def _cheque_purchase_op(
    session: AsyncSession, invoice_id: uuid.UUID
) -> BankOperation | None:
    """Карт-покупка чека (bank-аллокация с привязанной ДДС-проводкой)."""
    return await session.scalar(
        select(BankOperation)
        .join(
            InvoicePaymentAllocation, InvoicePaymentAllocation.bank_operation_id == BankOperation.id
        )
        .where(
            InvoicePaymentAllocation.invoice_id == invoice_id,
            InvoicePaymentAllocation.source_kind == "bank",
            BankOperation.cashflow_transaction_id.is_not(None),
        )
        .limit(1)
    )


async def _candidate_cheques_for_refund(
    session: AsyncSession, refund: BankOperation
) -> list[tuple[SupplierInvoice, BankOperation, Decimal]]:
    """Чеки Кассы с НЕзакрытым ожиданием возврата по ТОЙ ЖЕ карте, в окне.

    Надёжного пер-транзакционного ключа «покупка↔возврат» банк не даёт, поэтому кандидатов
    отбираем по карте + положительному остатку ожидания (Σ is_return − привязанные возвраты).
    Возвращает (чек, покупка, остаток). Разбор exact/partial/ambiguous — в ``_match_refund``."""
    card = _op_card(refund)
    if card is None:
        return []
    rows = (
        await session.execute(
            select(SupplierInvoice, BankOperation)
            .join(
                InvoicePaymentAllocation,
                InvoicePaymentAllocation.invoice_id == SupplierInvoice.id,
            )
            .join(BankOperation, BankOperation.id == InvoicePaymentAllocation.bank_operation_id)
            .where(
                SupplierInvoice.source == "kassa_cheque",
                InvoicePaymentAllocation.source_kind == "bank",
                BankOperation.account_id == refund.account_id,
                BankOperation.raw_payload["cardNumber"].astext == card,
                BankOperation.raw_payload["category"].astext == "cardOperation",
                BankOperation.cashflow_transaction_id.is_not(None),
            )
        )
    ).all()
    result: list[tuple[SupplierInvoice, BankOperation, Decimal]] = []
    seen: set[uuid.UUID] = set()
    for invoice, purchase in rows:
        if invoice.id in seen:
            continue
        seen.add(invoice.id)
        expected = await _cheque_expected_refund(session, invoice.id)
        if expected <= 0:
            continue
        outstanding = expected - await _attached_refund_total(
            session, purchase.cashflow_transaction_id
        )
        if outstanding <= 0:
            continue
        if not _refund_within_window(refund, purchase):
            continue
        result.append((invoice, purchase, outstanding))
    return result


async def _match_refund(
    session: AsyncSession, refund: BankOperation
) -> tuple[str, list[tuple[SupplierInvoice, BankOperation, Decimal]]]:
    """Разобрать возврат по кандидатам-чекам. Возвращает (status, targets):

    - ``'unique'`` + [единственный (чек, покупка, остаток)] — привязывать автоматически.
      Приоритет точному совпадению суммы (outstanding == сумма); если точного нет, но
      возврат ВПИСЫВАЕТСЯ (сумма < остаток) ровно в один чек — тоже уникально (частичный).
    - ``'ambiguous'`` + [список подходящих] — несколько чеков подходят, нужен ручной выбор.
    - ``'none'`` + [] — ни один чек не ждёт возврат такого размера (сирота: кассир не
      отметил возврат / возврат не по чеку)."""
    amount = _money(abs(refund.amount))
    if amount <= 0:
        return ("none", [])
    cands = await _candidate_cheques_for_refund(session, refund)
    fit = [c for c in cands if amount <= c[2]]  # возврат вписывается в остаток
    if not fit:
        return ("none", [])
    exact = [c for c in fit if c[2] == amount]
    if len(exact) == 1:
        return ("unique", [exact[0]])
    if not exact and len(fit) == 1:
        return ("unique", [fit[0]])
    return ("ambiguous", fit)


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


async def _resolve_refund_op_cases(session: AsyncSession, refund_id: uuid.UUID) -> None:
    """Закрыть pending-кейсы «возврат по чеку» по этой операции возврата — она привязана."""
    cases = (
        await session.scalars(
            select(ReconciliationCase).where(
                ReconciliationCase.kind == CARD_REFUND_CASE_KIND,
                ReconciliationCase.bank_operation_id == refund_id,
                ReconciliationCase.status == "pending",
            )
        )
    ).all()
    for case in cases:
        await close_reconciliation_case(
            session, case, status="resolved", resolution_payload={"reason": "refund_attached"}
        )


async def _attach_refund_to_cheque(
    session: AsyncSession,
    refund: BankOperation,
    invoice: SupplierInvoice,
    purchase: BankOperation,
) -> None:
    """Привязать возврат к проводке чека: classified + закрыть кейсы по нему; missing-кейс
    чека гасим ТОЛЬКО когда ожидание закрыто полностью (частичный возврат — ещё ждём остаток)."""
    refund.cashflow_transaction_id = purchase.cashflow_transaction_id
    refund.classification_status = "classified"
    await session.flush()  # чтобы _attached_refund_total увидел эту привязку
    await _resolve_refund_op_cases(session, refund.id)
    expected = await _cheque_expected_refund(session, invoice.id)
    outstanding = expected - await _attached_refund_total(session, purchase.cashflow_transaction_id)
    if outstanding <= 0:
        await _resolve_refund_missing_cases(session, purchase.id)


async def _attach_matching_refunds_for_cheque(
    session: AsyncSession, invoice: SupplierInvoice, purchase: BankOperation
) -> int:
    """При создании чека привязать уже пришедшие возвраты по его карте (если однозначно).

    Возврат привязывается, только если ЭТОТ чек — единственный кандидат для него
    (``_candidate_cheques_for_refund``): исключает захват возврата чужого чека той же суммы.
    Без commit."""
    card = _op_card(purchase)
    if card is None or purchase.cashflow_transaction_id is None:
        return 0
    if await _cheque_expected_refund(session, invoice.id) <= 0:
        return 0
    refunds = (
        await session.scalars(
            select(BankOperation)
            .where(
                BankOperation.provider == "tbank",
                BankOperation.direction == "in",
                BankOperation.account_id == purchase.account_id,
                BankOperation.cashflow_transaction_id.is_(None),
                BankOperation.transfer_group_id.is_(None),
                BankOperation.raw_payload["category"].astext == TBANK_REFUND_CATEGORY,
                BankOperation.raw_payload["cardNumber"].astext == card,
            )
            .order_by(BankOperation.operation_date)
        )
    ).all()
    attached = 0
    for refund in refunds:
        status, targets = await _match_refund(session, refund)
        if status == "unique" and targets[0][0].id == invoice.id:
            await _attach_refund_to_cheque(session, refund, invoice, targets[0][1])
            attached += 1
    return attached


async def _ensure_unmatched_refund_case(
    session: AsyncSession,
    refund: BankOperation,
    candidates: list[tuple[SupplierInvoice, BankOperation, Decimal]],
) -> bool:
    """Поднять кейс «возврат по чеку» (не сматчен либо неоднозначен). Дедуп по операции.

    В payload кладём кандидатов (чек+покупка+остаток), чтобы «Учесть возврат» мог привязать
    возврат к ВЫБРАННОМУ чеку (гасит его ожидание), а не завести оторванную проводку."""
    existing = await session.scalar(
        select(ReconciliationCase.id).where(
            ReconciliationCase.kind == CARD_REFUND_CASE_KIND,
            ReconciliationCase.bank_operation_id == refund.id,
            ReconciliationCase.status == "pending",
        )
    )
    if existing is not None:
        return False
    raw = refund.raw_payload or {}
    merch = raw.get("merch") if isinstance(raw.get("merch"), dict) else {}
    candidate_payload = [
        {
            "invoice_id": str(inv.id),
            "purchase_operation_id": str(pur.id),
            "cheque_number": inv.number,
            "outstanding": str(out),
        }
        for inv, pur, out in candidates
    ]
    session.add(
        ReconciliationCase(
            kind=CARD_REFUND_CASE_KIND,
            status="pending",
            provider=refund.provider,
            bank_operation_id=refund.id,
            payload={
                "refund_amount": str(_money(abs(refund.amount))),
                "card": _op_card(refund),
                "merchant": str((merch or {}).get("name") or "") or None,
                # 0 кандидатов — ни один чек не ждёт возврат такого размера (кассир не отметил
                # возврат / возврат не по чеку); >1 — несколько чеков подходят, нужен выбор.
                "reason": "no_matching_cheque" if not candidates else "ambiguous",
                "candidates": candidate_payload or None,
                "candidate_cheques": [inv.number for inv, _, _ in candidates] or None,
            },
        )
    )
    return True


async def match_card_refund_operations(session: AsyncSession) -> int:
    """Привязать непривязанные возвраты (refundIn) к чекам, ждущим ровно эту сумму. Без commit.

    Вызывается из ``ingest_operations`` ДО общей классификации (как матч пендинг-чеков),
    поэтому покрывает оба пути выписки — поллинг и вебхук. Привязывает ТОЛЬКО однозначные
    точные совпадения (единственный чек той же карты с остатком ожидания == сумме возврата).
    Несопоставленные/неоднозначные возвраты НЕ трогаем — их (с задержкой, дав время создать
    чек) поднимает в «Требует разбора» ``escalate_missing_cheque_refunds``. Возвращает число
    привязанных."""
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
    for refund_row in refund_rows:
        # Блокируем строку и перечитываем под локом: закрывает гонку с apply_card_refund_case
        # (ручной «Учесть возврат») — иначе оба могли бы учесть один возврат дважды.
        refund = await session.get(BankOperation, refund_row.id, with_for_update=True)
        if refund is None or refund.cashflow_transaction_id is not None:
            continue
        status, targets = await _match_refund(session, refund)
        if status == "unique":
            invoice, purchase, _out = targets[0]
            await _attach_refund_to_cheque(session, refund, invoice, purchase)
            matched += 1
    return matched


async def _resolve_target_cheque(
    session: AsyncSession, case: ReconciliationCase, invoice_id: uuid.UUID | None, amount: Decimal
) -> tuple[SupplierInvoice, BankOperation] | None:
    """Определить чек, к которому «Учесть возврат» привяжет возврат: явный выбор оператора
    (invoice_id) либо единственный кандидат из payload. None — сирота (проводим отдельно).

    Проверяет АКТУАЛЬНОЕ состояние (payload кейса — снимок и мог устареть): выбранный чек
    обязан быть среди кандидатов кейса И иметь остаток ожидания ≥ суммы возврата (иначе
    «Учесть» переполнил бы ожидание чека / потерял бы возврат другого)."""
    chosen: str | None = str(invoice_id) if invoice_id else None
    if chosen is None:
        candidates = (case.payload or {}).get("candidates") or []
        if len(candidates) == 1:
            chosen = str(candidates[0].get("invoice_id"))
    if not chosen:
        return None
    # Выбрать можно ТОЛЬКО чек из кандидатов кейса (в т.ч. запрещает выбор при 0 кандидатов).
    allowed = {str(c.get("invoice_id")) for c in ((case.payload or {}).get("candidates") or [])}
    if chosen not in allowed:
        raise KassaChequeError("Выбранный чек не относится к этому возврату")
    invoice = await session.get(SupplierInvoice, uuid.UUID(chosen))
    if invoice is None or invoice.source != "kassa_cheque":
        return None
    purchase = await _cheque_purchase_op(session, invoice.id)
    if purchase is None or purchase.cashflow_transaction_id is None:
        return None
    # Пере-проверяем остаток по актуальным данным (кандидат мог быть погашен другим возвратом).
    expected = await _cheque_expected_refund(session, invoice.id)
    outstanding = expected - await _attached_refund_total(session, purchase.cashflow_transaction_id)
    if amount > outstanding:
        raise KassaChequeError(
            "Выбранный чек уже получил ожидаемый возврат — выберите другой чек"
        )
    return invoice, purchase


async def apply_card_refund_case(
    session: AsyncSession,
    case: ReconciliationCase,
    *,
    invoice_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Действие «Учесть возврат» по кейсу «возврат по проведённому чеку». Без commit.

    Если возврат относится к конкретному чеку (единственный кандидат или явный выбор
    оператора ``invoice_id``) — ПРИВЯЗЫВАЕМ его к ДДС-проводке чека (гасит ожидание чека,
    закрывает кейсы «возврат не пришёл» — иначе они висели бы вечно). Иначе (сирота: кассир
    не отметил возврат / возврат не по чеку) — заводим отдельную входящую проводку «Возврат
    расходов». История чека и iiko не мутируются. Повтор по привязанному возврату — no-op.
    """
    if case.kind != CARD_REFUND_CASE_KIND:
        raise KassaChequeError("Кейс не является возвратом по чеку")
    if case.bank_operation_id is None:
        raise KassaChequeError("Кейс не привязан к операции возврата")
    # Блокируем строку возврата: закрывает гонку с realtime-матчером (двойной учёт).
    refund = await session.get(BankOperation, case.bank_operation_id, with_for_update=True)
    if refund is None:
        raise KassaChequeError("Операция возврата не найдена")

    if refund.cashflow_transaction_id is not None:
        # Уже учтён (гонка/повторный клик) — просто закрываем кейс.
        await close_reconciliation_case(
            session, case, status="resolved", resolution_payload={"reason": "already_linked"}
        )
        return {"transaction_id": refund.cashflow_transaction_id, "already_linked": True}

    target = await _resolve_target_cheque(session, case, invoice_id, _money(abs(refund.amount)))
    if target is not None:
        # Привязка к чеку: гасит ожидание, чек уже проведён net — новую проводку НЕ заводим.
        invoice, purchase = target
        await _attach_refund_to_cheque(session, refund, invoice, purchase)
        return {"linked_invoice_id": str(invoice.id), "already_linked": False}

    # Неоднозначность (несколько кандидатов), но чек не выбран — НЕ уходим в сироту молча
    # (иначе ожидания чеков не погасятся, а возврат ляжет как чужой доход). Требуем выбор.
    if (case.payload or {}).get("candidates"):
        raise KassaChequeError("Выберите чек, к которому относится возврат")

    # Сирота (кандидатов нет): отдельная входящая проводка «Возврат расходов».
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
                "Служебная: возвраты карт-покупок, не сопоставленные с чеком "
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
        source_kind="kassa_cheque_refund",
        payment_purpose="Возврат карт-покупки",
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
    """Поднять в «Требует разбора» отложенные кейсы возвратов. Без commit.

    Две ветки (обе с задержкой ``kassa.refund_wait_alert_days``, чтобы realtime-матчер и
    создание чека успели сработать):
    (а) ЧЕК ЖДЁТ ВОЗВРАТ (позиции исключены), а он не пришёл — кейс ``cheque_refund_missing``
        (дедуп по покупке; авто-резолв при привязке возврата).
    (б) ВОЗВРАТ НЕ СМАТЧЕН ни с одним чеком (сумму никто не ждёт) либо неоднозначен — кейс
        ``card_refund_after_cheque`` с кнопкой «Учесть возврат» (дедуп по операции возврата).
    """
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(days=await get_refund_wait_alert_days(session))
    created = 0

    # (а) Чеки, ждущие возврат дольше порога.
    rows = (
        await session.execute(
            select(SupplierInvoice, BankOperation)
            .join(
                InvoicePaymentAllocation,
                InvoicePaymentAllocation.invoice_id == SupplierInvoice.id,
            )
            .join(BankOperation, BankOperation.id == InvoicePaymentAllocation.bank_operation_id)
            .where(
                SupplierInvoice.source == "kassa_cheque",
                InvoicePaymentAllocation.source_kind == "bank",
            )
        )
    ).all()
    for invoice, purchase in rows:
        expected = await _cheque_expected_refund(session, invoice.id)
        if expected <= 0:
            continue
        if invoice.created_at is not None and invoice.created_at > cutoff:
            continue
        outstanding = expected - await _attached_refund_total(
            session, purchase.cashflow_transaction_id
        )
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

    # (б) Возвраты, пришедшие раньше порога и всё ещё не сматченные однозначно с чеком.
    # operation_date — только дата (без времени), поэтому берём порог на день СТРОЖЕ
    # (moment − (N+1) дней), чтобы точно прошло ≥ N суток и не эскалировать раньше срока.
    stale_before = (moment - timedelta(days=await get_refund_wait_alert_days(session) + 1)).date()
    stale_refunds = (
        await session.scalars(
            select(BankOperation).where(
                BankOperation.provider == "tbank",
                BankOperation.direction == "in",
                BankOperation.cashflow_transaction_id.is_(None),
                BankOperation.transfer_group_id.is_(None),
                BankOperation.raw_payload["category"].astext == TBANK_REFUND_CATEGORY,
                BankOperation.operation_date <= stale_before,
            )
        )
    ).all()
    for refund in stale_refunds:
        status, targets = await _match_refund(session, refund)
        if status == "unique":
            # Однозначный матч добьёт realtime-матчер на следующем синке — не наш кейс.
            continue
        # 'ambiguous' → targets = подходящие чеки (для выбора оператором); 'none' → [].
        if await _ensure_unmatched_refund_case(session, refund, targets):
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

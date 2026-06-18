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
    BankOperation,
    CashflowTransaction,
    Counterparty,
    DdsArticle,
    IikoProduct,
    InvoiceLineItem,
    InvoicePaymentAllocation,
    SupplierInvoice,
    Wallet,
)
from app.services.banking.classifier import _wallet_for_operation
from app.services.counterparty_bank_match import (
    BANK_NOISE_INNS,
    BUSINESS_TZ,
    _as_utc,
    _receiver_block,
)
from app.services.counterparty_matching import _op_already_allocated, _recompute_status

CASH_WALLET_CODE = "tk_chernikova"
CARD_TX_WINDOW_HOURS = 48
CARD_TX_TIGHT_MINUTES = 120

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


@dataclass
class ChequeLineInput:
    name: str
    quantity: Decimal
    price: Decimal
    unit: str | None = None
    dds_article_id: uuid.UUID | None = None
    iiko_product_id: uuid.UUID | None = None
    vat_percent: Decimal | None = None


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
    for operation in operations:
        if not _is_card_purchase(operation):
            continue
        if operation.id in allocated_ids:
            continue

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
    if issued_at is None:
        raise KassaChequeError("Укажите дату и время чека")
    if not bank_parts and not cash_amount:
        raise KassaChequeError("Укажите хотя бы один источник оплаты: карта или наличные")

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
        if amount <= 0:
            raise KassaChequeError("Сумма банковской части должна быть больше нуля")
        if amount > op_amount:
            raise KassaChequeError("Сумма части превышает сумму операции")
        resolved_bank.append((operation, wallet, amount))
        bank_total += amount

    cash_total = _money(cash_amount) if cash_amount is not None else Decimal("0.00")
    if cash_total < 0:
        raise KassaChequeError("Сумма наличной части не может быть отрицательной")

    paid_total = _money(bank_total + cash_total)
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
        lines_total = Decimal("0.00")
        for index, line in enumerate(line_inputs):
            product = products.get(line.iiko_product_id) if line.iiko_product_id else None
            quantity = _qty(line.quantity)
            price = _money(line.price)
            line_sum = _money(quantity * price)
            line_article_id = line.dds_article_id or article_id
            if line_article_id is None:
                raise KassaChequeError(f"У позиции «{line.name}» не указана статья ДДС")
            vat_sum: Decimal | None = None
            if line.vat_percent:
                rate = Decimal(str(line.vat_percent))
                vat_sum = _money(line_sum * rate / (Decimal("100") + rate))  # gross-inclusive
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
                    sort_order=index,
                )
            )
            lines_total += line_sum
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
                }
            )
        if _money(lines_total) != paid_total:
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
    return {
        "id": invoice.id,
        "number": invoice.number,
        "counterparty_id": invoice.counterparty_id,
        "counterparty_name": counterparty.name if counterparty else "—",
        "issued_at": invoice.issued_at.isoformat() if invoice.issued_at else None,
        "amount": float(_money(invoice.amount)),
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
            }
            for line in lines
        ],
    }

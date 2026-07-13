"""Дублирование оплаты накладной в iiko (Cloud API ``add_payment``).

Когда у нас iiko-накладная стала «оплачено» через банковский черновик, шлём такой же платёж в
iiko, чтобы расчёты с поставщиками и денежные счета в iiko отражали реальность без ручного ввода.

Контракт ``add_payment`` подтверждён на реальной проводке (HTTP 201):
тело ``{organizationId, documentId, paymentDate, accountId, amount}``; ``paymentDate`` — ISO 8601
с ДЕСЯТИЧНОЙ ЗАПЯТОЙ перед мс и offset (``YYYY-MM-DDThh:mm:ss,sss+03:00``); ``accountId`` —
счёт-ИСТОЧНИК денег (для оплат через банк = «Денежные средства, эквайринг»). Метод НЕ идемпотентен —
повтор задвоит платёж, поэтому факт отправки фиксируем в :class:`IikoInvoicePaymentPush` по
``idempotency_key`` (``invoice:<id>``) и не шлём дважды.

Платить можно только накладные, существующие в iiko (есть ``external_id`` = iiko ``documentId``).
Эндпоинт триггера — сверочный джоб ``mirror_paid_iiko_invoices`` (не трогает путь гашения).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_DOWN, Decimal
from zoneinfo import ZoneInfo

import anyio
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import (
    CashflowTransaction,
    Counterparty,
    CounterpartyPayableProfile,
    CounterpartyPaymentDraft,
    IikoInvoicePaymentPush,
    InvoicePaymentAllocation,
    ReconciliationCase,
    SupplierInvoice,
)

logger = logging.getLogger(__name__)
_MSK = ZoneInfo("Europe/Moscow")

_IIKO_BASE = "https://api-ru.iiko.services"
_ACCESS_TOKEN_PATH = "/api/v2/access_token"
_ADD_PAYMENT_PATH = "/api/inventory/v1/incoming_invoice/modify/add_payment"

# Организация Cloud — «Foodmarket Тепло Черникова» (получено из /api/1/organizations).
IIKO_ORGANIZATION_ID = "5c7e51f9-93c6-450c-9299-440ac1c889e8"
# Счёт-источник денег для зеркала банковской оплаты — «Денежные средства, эквайринг» (решение
# владельца: расчёты по банку iiko ведёт через эквайринг, НЕ через «Денежные средства, банк»).
IIKO_ACQUIRING_ACCOUNT = "3f261590-f208-2970-1300-95d2493a3c28"
# Маппинг наших кошельков на денежный счёт-источник iiko (для ручного push по конкретному кошельку).
WALLET_TO_IIKO_ACCOUNT: dict[str, str] = {
    "cash_safe": "1a731cc0-df27-4fce-8a07-4fb692e24fc2",      # Сейф монеты
    "tk_chernikova": "8ccc8f0f-24f6-64d2-5eea-04f829ba381f",  # Главная касса
    "tbank_main": IIKO_ACQUIRING_ACCOUNT,                      # банк → эквайринг
    "sber_main": IIKO_ACQUIRING_ACCOUNT,                       # банк → эквайринг
}

# Кап ретраев сверочного джоба — после стольких неудачных попыток платёж в iiko больше не шлём
# автоматически (нужен ручной разбор), чтобы не долбить iiko бесконечно.
MAX_PUSH_ATTEMPTS = 6


def _amount_iiko_representable(amount: Decimal) -> bool:
    """iiko парсит ``amount`` JSON-числом в double и валидирует целость копеек (``amount*100`` —
    целое). Десятичные суммы с неточным double (напр. ``4213.44`` → ``421343.99999999994``) iiko
    отклоняет ``"invalid amount in JSON"`` (проверено на боевом API). Эмулируем ту же проверку,
    чтобы не слать обречённое (метод НЕ идемпотентен) и сразу звать ручной разбор. Целые суммы и
    «удачные» дроби (напр. ``959.88`` → ``95988.0``) → True."""
    return (float(amount) * 100).is_integer()


def representable_split(amount: Decimal) -> list[Decimal]:
    """Разбить сумму на 1–3 «представимых» для iiko части (точная сумма), чтобы провести её
    несколькими ``add_payment``. Целые рубли всегда представимы; копейки — обычно тоже, а «неудачные»
    (напр. 0.29 → 28.9999…) бьём на две представимые (0.20 + 0.09). Примеры: 33982.80 →
    [33982.00, 0.80]; 4213.44 → [4213.00, 0.44]; 0.29 → [0.20, 0.09]. Представимая сумма → [сама]."""
    amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    if _amount_iiko_representable(amount):
        return [amount]
    whole = amount.to_integral_value(rounding=ROUND_DOWN)  # целые рубли — всегда представимы
    cents = amount - whole  # 0.xx
    parts: list[Decimal] = []
    if whole > 0:
        parts.append(whole)
    if _amount_iiko_representable(cents):
        parts.append(cents)
    else:
        step = Decimal("0.01")
        a = step
        while a < cents:
            b = cents - a
            if _amount_iiko_representable(a) and _amount_iiko_representable(b):
                parts.extend([a, b])
                break
            a += step
        else:  # для 2-значных копеек недостижимо; фолбэк — вернуть как есть (уйдёт в ошибку/кейс)
            parts.append(cents)
    return parts


# Окно ожидания появления документа в iiko Cloud. add_payment (Cloud) видит документ не сразу после
# его создания в iikoServer — между Server и Cloud есть задержка/рассинхрон репликации. «incoming
# invoice not found» в пределах этого окна — НЕ перманентный отказ (документ доедет и оплата
# пройдёт), поэтому сверочный джоб продолжает ретраить. За окном считаем, что документ в Cloud так и
# не появился, и заводим ручной кейс (реальный сбой синхронизации на стороне iiko).
IIKO_SYNC_GRACE = timedelta(days=5)

# Окно, после которого pending-пуш считаем ЗАСТРЯВШИМ (осиротевшим): pending пишется до HTTP
# add_payment и финализируется за секунды; строка старше окна = процесс упал между ними. Свип
# заводит по ней видимый кейс (ветка mirror для этого мертва — pending исключает накладную из
# выборки). С запасом, чтобы не трогать свежий in-flight pending конкурентного запроса.
IIKO_STALE_PENDING_WINDOW = timedelta(minutes=15)


def _is_incoming_invoice_not_found(response: dict | None, error: str | None) -> bool:
    """iiko Cloud add_payment вернул «incoming invoice not found»: документ есть в iikoServer,
    но ещё не виден в Cloud (рассинхрон Server↔Cloud). Отличаем ВРЕМЕННУЮ ошибку от настоящих
    перманентных 4xx (invalid amount и т.п.), ретраить которые бессмысленно."""
    blob = ""
    if isinstance(response, dict):
        blob += json.dumps(response, ensure_ascii=False)
    if error:
        blob += " " + error
    blob = blob.lower()
    return "not found" in blob and "invoic" in blob


def _is_already_paid(response: dict | None) -> bool:
    """iiko отклонил add_payment, потому что накладная УЖЕ оплачена. Для зеркала это идемпотентный
    успех (платёж в iiko есть), а не ошибка — иначе оно бесконечно пыталось бы оплатить оплаченную."""
    if not isinstance(response, dict):
        return False
    return "already paid" in json.dumps(response, ensure_ascii=False).lower()


def _within_iiko_sync_grace(invoice: SupplierInvoice) -> bool:
    """Документ создан в iiko недавно (grace-окно) → ждём его появления в Cloud, ретраим."""
    anchor = invoice.iiko_pushed_at or invoice.created_at
    if anchor is None:
        return True
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=UTC)
    return (datetime.now(UTC) - anchor) < IIKO_SYNC_GRACE


async def _cap_push_attempts(session: AsyncSession, *, idempotency_key: str) -> None:
    """Заблокировать авто-ретраи пуша (attempts=кап) — терминальный провал, который сам не
    рассосётся (перманентный отказ iiko / документ не появился в Cloud за grace-окно)."""
    record = await session.scalar(
        select(IikoInvoicePaymentPush).where(
            IikoInvoicePaymentPush.idempotency_key == idempotency_key
        )
    )
    if record is not None and record.attempts < MAX_PUSH_ATTEMPTS:
        record.attempts = MAX_PUSH_ATTEMPTS
        await session.commit()


# Машиночитаемые коды причин «оплата не дошла до iiko» (payload['reason_code']). Определяют, можно
# ли авто-повторить отправку (retriable) или остаётся только ручное подтверждение. Человекочитаемый
# текст — в payload['reason']. Единый видимый список owner-review (kind='iiko_payment_unsettled').
IIKO_UNSETTLED_REASON_CODES = frozenset(
    {
        "orphaned_pending",  # процесс упал между pending и финализацией — ретраить можно
        "not_in_cloud_grace",  # документ ещё не доехал в Cloud (рассинхрон) — дойдёт сам
        "not_in_cloud_terminal",  # не появился за grace-окно — ретраить/ручной
        "iiko_rejected",  # перманентный отказ iiko (invalid amount и т.п.)
        "retry_cap_exhausted",  # исчерпан кап авто-ретраев транзиента — тихо выпадал из выборки
        "multi_invoice",  # мультиплатёж без честной банковской доли — только ручное
        "multi_invoice_residual",  # сумма честных долей не совпала с суммой банк-платежа
        "barter_counterparty",  # взаимозачёт: баланс iiko требует ручной сверки
        "paid_outside_draft",  # оплата мимо банк-черновика (аванс/наличные) — авто нельзя
        "correction_unsettled",  # накладная скорректирована, а оплата оригинала так и не дошла
    }
)
# Причины, где авто-повтор add_payment имеет смысл (кнопка «Повторить отправку»). Остальные —
# только ручное подтверждение (мультиплатёж/аванс/коррекция: реального add_payment нет/нельзя).
# orphaned_pending СЮДА НЕ входит: осиротевший pending мог УЖЕ пройти в iiko (pending пишется до
# HTTP add_payment), авто-повтор неидемпотентного add_payment задвоил бы платёж. Разрешение —
# только ручное подтверждение после сверки в iiko (auto_sendable для pending возвращает False).
IIKO_UNSETTLED_RETRIABLE = frozenset(
    {"not_in_cloud_grace", "not_in_cloud_terminal", "iiko_rejected", "retry_cap_exhausted"}
)


async def _open_iiko_payment_case(
    session: AsyncSession,
    *,
    invoice_id: uuid.UUID,
    external_id: str,
    amount: Decimal,
    reason: str,
    reason_code: str,
) -> bool:
    """Завести видимый кейс owner-review «оплата в iiko не проведена» (idempotent по накладной).
    Возвращает True, если кейс СОЗДАН (False — уже был pending-кейс по этой накладной).

    Единая точка единого видимого списка: сюда сводятся ВСЕ причины «у нас оплачено — в iiko нет»
    (непредставимая сумма / кап / перманентный отказ / документ не в Cloud / мультиплатёж / оплата
    мимо черновика / скорректированная с недоехавшей оплатой). ``reason_code`` — машиночитаемый код
    (см. IIKO_UNSETTLED_REASON_CODES) для группировки/действий; ``reason`` — человекочитаемый текст.
    Обогащаем payload поставщиком/номером/датой — карточка показывает суть без доп. запросов."""
    existing = await session.scalar(
        select(ReconciliationCase.id)
        .where(
            ReconciliationCase.kind == "iiko_payment_unsettled",
            ReconciliationCase.status == "pending",
            ReconciliationCase.payload["invoice_id"].astext == str(invoice_id),
        )
        .limit(1)
    )
    if existing is not None:
        return False
    # Обогащение (только при создании — фоновый запрос, не в горячем пути): поставщик/номер/
    # дата для карточки. session.get — свежий запрос по id (не lazy-load отцепленного инстанса).
    supplier_name: str | None = None
    invoice_number: str | None = None
    issued_at: str | None = None
    inv = await session.get(SupplierInvoice, invoice_id)
    if inv is not None:
        invoice_number = inv.number
        when = inv.issued_at or (
            datetime.combine(inv.invoice_date, time()) if inv.invoice_date else None
        )
        issued_at = when.isoformat() if when is not None else None
        cp = await session.get(Counterparty, inv.counterparty_id)
        supplier_name = cp.name if cp is not None else None
    session.add(
        ReconciliationCase(
            kind="iiko_payment_unsettled",
            status="pending",
            provider="iiko",
            payload={
                "invoice_id": str(invoice_id),
                "external_id": external_id or "",
                "amount": str(amount),
                "reason": reason,
                "reason_code": reason_code,
                "supplier_name": supplier_name,
                "invoice_number": invoice_number,
                "issued_at": issued_at,
                "retriable": reason_code in IIKO_UNSETTLED_RETRIABLE,
            },
        )
    )
    # Самостоятельный commit: кейс должен пережить даже последующий per-item rollback и НЕ зависеть
    # от того, коммитит ли вызывающий после нас (банковский mirror — не коммитит).
    await session.commit()
    logger.warning(
        "iiko mirror: оплата накладной %s НЕ проведена в iiko (%s/%s) — кейс owner-review",
        invoice_id,
        reason_code,
        reason,
    )
    return True


async def _resolve_iiko_payment_case(session: AsyncSession, invoice_id: uuid.UUID) -> int:
    """Закрыть (resolved) висящие pending-кейсы «оплата не дошла до iiko» по накладной — оплата
    восстановилась (успешный пуш зеркала / подтверждено вручную). Возвращает число закрытых.
    Без этого кейс висел бы вечно даже после того, как оплата в iiko появилась."""
    cases = (
        await session.scalars(
            select(ReconciliationCase).where(
                ReconciliationCase.kind == "iiko_payment_unsettled",
                ReconciliationCase.status == "pending",
                ReconciliationCase.payload["invoice_id"].astext == str(invoice_id),
            )
        )
    ).all()
    for case in cases:
        case.status = "resolved"
        case.resolved_at = datetime.now(UTC)
        case.resolution_payload = {"reason": "mirrored_ok"}
    if cases:
        await session.commit()
    return len(cases)


async def _multi_draft_bank_shares(
    session: AsyncSession, draft_ids: set[uuid.UUID]
) -> tuple[dict[tuple[uuid.UUID, uuid.UUID], Decimal], dict[uuid.UUID, Decimal]]:
    """Вернуть честные банковские доли по накладным мультичерновиков и их суммы по черновику.

    Источник истины — не ``draft.amount`` и не пропорция gross-сумм, а аллокация, привязанная к
    ДДС-проводке именно этого черновика (``counterparty_payment/source_id=draft.id``). В текущем
    пути банковская проводка создаёт ``source_kind='cash'`` (потому что источник — cashflow), а в
    старых/ручных данных встречается ``bank``; связь с ДДС-проводкой однозначно подтверждает, что
    это банковская доля. Аллокацию чужой накладной к проводке черновика не принимаем.
    """
    if not draft_ids:
        return {}, {}
    rows = (
        await session.execute(
            select(
                CashflowTransaction.source_id,
                InvoicePaymentAllocation.invoice_id,
                func.sum(InvoicePaymentAllocation.amount),
            )
            .join(
                CashflowTransaction,
                CashflowTransaction.id == InvoicePaymentAllocation.cashflow_transaction_id,
            )
            .join(SupplierInvoice, SupplierInvoice.id == InvoicePaymentAllocation.invoice_id)
            .where(
                InvoicePaymentAllocation.source_kind.in_(("bank", "cash")),
                CashflowTransaction.source_kind == "counterparty_payment",
                CashflowTransaction.source_id.in_(draft_ids),
                SupplierInvoice.draft_id == CashflowTransaction.source_id,
            )
            .group_by(CashflowTransaction.source_id, InvoicePaymentAllocation.invoice_id)
        )
    ).all()
    shares: dict[tuple[uuid.UUID, uuid.UUID], Decimal] = {}
    totals = {draft_id: Decimal("0.00") for draft_id in draft_ids}
    for draft_id, invoice_id, amount in rows:
        if draft_id is None:
            continue
        share = Decimal(str(amount or 0)).quantize(Decimal("0.01"))
        shares[(draft_id, invoice_id)] = share
        totals[draft_id] += share
    return shares, totals


class IikoPaymentError(RuntimeError):
    """Доменная ошибка пуша оплаты в iiko (нет маппинга, нет external_id и т.п.)."""


@dataclass
class IikoPushResult:
    ok: bool
    skipped: bool  # True — уже успешно отправляли по этому ключу, повторно не слали
    status_code: int | None
    payload: dict
    response: dict
    error: str | None = None


def account_id_for_wallet(wallet_code: str) -> str:
    account = WALLET_TO_IIKO_ACCOUNT.get(wallet_code)
    if not account:
        raise IikoPaymentError(
            f"Нет маппинга кошелька «{wallet_code}» на счёт iiko — оплата в iiko не отправлена"
        )
    return account


def format_iiko_payment_date(dt: datetime) -> str:
    """ISO 8601 с ДЕСЯТИЧНОЙ ЗАПЯТОЙ перед мс и offset — формат, который принимает iiko.

    Пример: ``2026-06-26T14:05:09,000+03:00``. Точка (``.000``) iiko НЕ принимает — нужна запятая.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_MSK)
    base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    millis = f"{dt.microsecond // 1000:03d}"
    offset = dt.strftime("%z")  # +0300
    offset = f"{offset[:3]}:{offset[3:]}" if offset else "+03:00"
    return f"{base},{millis}{offset}"


def build_add_payment_payload(
    *, external_id: str, amount: Decimal, account_id: str, payment_dt: datetime
) -> dict:
    return {
        "organizationId": IIKO_ORGANIZATION_ID,
        "documentId": external_id,
        "paymentDate": format_iiko_payment_date(payment_dt),
        "accountId": account_id,
        "amount": float(amount),
    }


def _iiko_opener() -> urllib.request.OpenerDirector:
    # В обход прокси: банк-туннель (HTTPS_PROXY) перехватывает api-ru.iiko.services и рубит
    # соединение. Проверенный рабочий путь (HTTP 201) — собственный opener без прокси.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _iiko_auth_token(opener: urllib.request.OpenerDirector) -> str:
    """Cloud OAuth: POST /api/v2/access_token {appId, apiKey, clientSecret} → token.

    КРИТИЧНО: clientSecret хранится С завершающим ``=`` (иначе 401 Invalid client secret)."""
    body = json.dumps(
        {
            "appId": os.environ["IIKO_CLOUD_APP_ID"],
            "apiKey": os.environ["IIKO_CLOUD_API_LOGIN"],
            "clientSecret": os.environ["IIKO_CLOUD_CLIENT_SECRET"],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        _IIKO_BASE + _ACCESS_TOKEN_PATH,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(req, timeout=30) as response:
        return json.loads(response.read())["token"]


def _call_add_payment(payload: dict) -> tuple[int, dict]:
    """Синхронный вызов iiko ``add_payment`` (исполняется в треде): auth + POST в обход прокси.

    НИКОГДА не бросает: транспорт/таймаут/прокси/протухший токен/нет креды (KeyError) возвращаются
    как код 0 с телом-ошибкой, чтобы вызывающий зафиксировал их как ``error`` (++attempts, кап
    ретраев), а не зациклил джоб исключениями. Терпим к пустому/не-JSON телу при 2xx.
    """
    opener = _iiko_opener()
    try:
        token = _iiko_auth_token(opener)
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(_IIKO_BASE + _ADD_PAYMENT_PATH, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {token}")
        with opener.open(req, timeout=40) as response:
            body = response.read().decode("utf-8", "replace")
            parsed = json.loads(body) if body.strip().startswith(("{", "[")) else {"raw": body}
            return response.status, parsed
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except Exception:  # noqa: BLE001 — тело ошибки не JSON
            return exc.code, {"error": raw[:400]}
    except Exception as exc:  # noqa: BLE001 — сеть/прокси/токен/нет креды: ошибка, но джоб не валим
        return 0, {"error": f"{type(exc).__name__}: {exc}"[:400]}


async def _mark_split_done(
    session: AsyncSession,
    *,
    idempotency_key: str,
    invoice_id: uuid.UUID | None,
    external_id: str,
    amount: Decimal,
    account_id: str,
    n_parts: int,
    actor_user_id: uuid.UUID | None = None,
) -> None:
    """Пометить дроблёную оплату «проведена» summary-строкой по БАЗОВОМУ ключу. ``invoice_id`` задан
    → ``blocked_invoice_ids`` исключит накладную из авто-выборки джоба (все части уже прошли).
    Обновляет существующую строку по ключу (напр. прежний ``error`` непредставимой суммы) в ``ok``."""
    record = await session.scalar(
        select(IikoInvoicePaymentPush).where(
            IikoInvoicePaymentPush.idempotency_key == idempotency_key
        )
    )
    if record is None:
        record = IikoInvoicePaymentPush(idempotency_key=idempotency_key)
        session.add(record)
    record.invoice_id = invoice_id
    record.external_id = external_id
    record.amount = amount
    record.account_to = account_id
    record.status = "ok"
    record.attempts = 0
    record.iiko_document_id = None
    record.error = None
    record.request_payload = {"split_total": str(amount), "parts": n_parts}
    record.response_payload = {"split_done": n_parts}
    record.created_by_user_id = actor_user_id
    await session.commit()


async def push_invoice_payment_to_iiko(
    session: AsyncSession,
    *,
    external_id: str,
    amount: Decimal,
    account_id: str,
    idempotency_key: str,
    payment_dt: datetime | None = None,
    invoice_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    dry_run: bool = False,
) -> IikoPushResult:
    """Отправить оплату накладной в iiko (идемпотентно по ``idempotency_key``).

    Паттерн pending-first против двойной оплаты при НЕидемпотентном ``add_payment``: ДО HTTP пишем
    durable-строку ``pending`` (commit), потом зовём iiko, потом переводим в ``ok``/``error``. Если
    процесс упадёт между HTTP 201 и финальным commit — строка останется ``pending`` и навсегда
    исключена из авто-выборки (нужен ручной разбор), но платёж НЕ уйдёт повторно. ``pending``/``ok``
    по существующему ключу → не шлём (``ok`` — уже отправлено; ``pending`` — in-flight/осиротевший).
    ``dry_run=True`` — только собрать payload, без вызова и записи.
    """
    if not external_id:
        raise IikoPaymentError("У накладной нет external_id (её нет в iiko) — оплата не отправлена")
    amount = Decimal(str(amount))
    if amount <= 0:
        raise IikoPaymentError("Сумма оплаты должна быть больше нуля")

    # Креды Cloud живут в DB-vault (SourceCredential), а НЕ в env контейнера — как и все iiko-пути,
    # грузим IIKO_CLOUD_* в os.environ перед вызовом. До записи pending: сбой загрузки не оставит
    # orphan-строку. Локальный импорт — против циклов на загрузке модуля.
    from app.services.iiko_sync import _load_source_credential_env

    await _load_source_credential_env(session)

    # iiko отвергает суммы, чей float(amount)*100 не целое («invalid amount in JSON»). Такие суммы НЕ
    # шлём одним платежом, а ДРОБИМ на представимые части (целые рубли + копейки, при нужде в две) и
    # проводим несколькими add_payment — сумма точная, каждая часть проходит. Части идут суб-ключами
    # с invoice_id=None; итог помечает summary-строка по БАЗОВОМУ ключу (invoice_id задан → джоб
    # видит накладную «оплаченной»), недооплаченная НЕ блокируется в blocked_invoice_ids.
    parts = representable_split(amount)
    if len(parts) > 1:
        summary = await session.scalar(
            select(IikoInvoicePaymentPush).where(
                IikoInvoicePaymentPush.idempotency_key == idempotency_key
            )
        )
        if summary is not None and summary.status == "ok":
            return IikoPushResult(
                ok=True, skipped=True, status_code=None,
                payload=summary.request_payload, response=summary.response_payload,
            )
        if dry_run:
            return IikoPushResult(
                ok=True, skipped=False, status_code=None,
                payload={"split": [str(p) for p in parts]},
                response={"dry_run": True, "parts": len(parts)},
            )
        for i, part in enumerate(parts):
            # Части летят подряд, iiko троттлит серию (429). На ТРАНЗИЕНТНОМ отказе (429/5xx/сеть)
            # ретраим часть с бэкоффом, чтобы дробление завершилось за один проход. Бэкофф только на
            # ретрае (attempt>0) — happy-path без задержек. Перманентный 4xx не ретраим. Даже если
            # часть так и не пройдёт — уже проведённые части останутся ok (суб-ключи), а сверочный
            # джоб дожмёт остаток следующим проходом (summary не записан → накладная не заблокирована).
            res: IikoPushResult | None = None
            for attempt in range(4):
                if attempt > 0:
                    await anyio.sleep(3.0 * attempt)
                res = await push_invoice_payment_to_iiko(
                    session,
                    external_id=external_id,
                    amount=part,
                    account_id=account_id,
                    idempotency_key=f"{idempotency_key}#{i}",
                    payment_dt=payment_dt,
                    invoice_id=None,  # части не блокируют накладную; итог — summary-строка
                    actor_user_id=actor_user_id,
                )
                sc = res.status_code or 0
                if res.ok or not (sc in (0, 429) or sc >= 500):
                    break  # успех или перманентный отказ — ретраить нечего
            assert res is not None
            if not res.ok:
                return IikoPushResult(
                    ok=False, skipped=False, status_code=res.status_code,
                    payload=res.payload, response=res.response,
                    error=f"дробление части {i + 1}/{len(parts)} ({part}₽): {res.error}",
                )
        await _mark_split_done(
            session, idempotency_key=idempotency_key, invoice_id=invoice_id,
            external_id=external_id, amount=amount, account_id=account_id,
            n_parts=len(parts), actor_user_id=actor_user_id,
        )
        return IikoPushResult(
            ok=True, skipped=False, status_code=201,
            payload={"split_total": str(amount), "parts": [str(p) for p in parts]},
            response={"split_done": len(parts)},
        )

    existing = await session.scalar(
        select(IikoInvoicePaymentPush).where(
            IikoInvoicePaymentPush.idempotency_key == idempotency_key
        )
    )
    if existing is not None and existing.status in ("ok", "pending"):
        return IikoPushResult(
            ok=existing.status == "ok", skipped=True, status_code=None,
            payload=existing.request_payload, response=existing.response_payload,
            error=existing.error,
        )

    payload = build_add_payment_payload(
        external_id=external_id,
        amount=amount,
        account_id=account_id,
        payment_dt=payment_dt or datetime.now(_MSK),
    )
    representable = _amount_iiko_representable(amount)
    if dry_run:
        return IikoPushResult(
            ok=True, skipped=False, status_code=None, payload=payload,
            response={"dry_run": True, "amount_representable": representable},
        )
    if not representable:
        # iiko парсит amount в double и валидирует целость копеек; суммы с неточным double
        # (напр. 4213.44 → 421343.9999) он отклоняет 'invalid amount in JSON'. Не шлём — обречено
        # и НЕ идемпотентно; помечаем терминально (кап), чтобы джоб не долбил, а вызывающий завёл
        # ручной кейс. existing-счётчик не понижаем.
        record = existing or IikoInvoicePaymentPush(idempotency_key=idempotency_key)
        record.invoice_id = invoice_id
        record.external_id = external_id
        record.amount = amount
        record.account_to = account_id
        record.status = "error"
        record.attempts = MAX_PUSH_ATTEMPTS
        record.iiko_document_id = None
        record.error = "amount_not_representable: сумма не представима для iiko (копейки в double)"
        record.request_payload = payload
        record.response_payload = {}
        record.created_by_user_id = actor_user_id
        if existing is None:
            session.add(record)
        await session.commit()
        return IikoPushResult(
            ok=False, skipped=False, status_code=422, payload=payload, response={},
            error=record.error,
        )

    # 1) Durable in-flight маркер ДО необратимого HTTP-вызова.
    record = existing or IikoInvoicePaymentPush(idempotency_key=idempotency_key)
    record.invoice_id = invoice_id
    record.external_id = external_id
    record.amount = amount
    record.account_to = account_id
    record.status = "pending"
    record.attempts = (existing.attempts if existing is not None else 0) + 1
    record.iiko_document_id = None
    record.error = None
    record.request_payload = payload
    record.response_payload = {}
    record.created_by_user_id = actor_user_id
    if existing is None:
        session.add(record)
    try:
        await session.commit()
    except IntegrityError:
        # Гонка по уникальному idempotency_key — другой процесс уже взял. Возвращаем его, не шлём.
        await session.rollback()
        winner = await session.scalar(
            select(IikoInvoicePaymentPush).where(
                IikoInvoicePaymentPush.idempotency_key == idempotency_key
            )
        )
        if winner is not None:
            return IikoPushResult(
                ok=winner.status == "ok", skipped=True, status_code=None,
                payload=winner.request_payload, response=winner.response_payload,
                error=winner.error,
            )
        raise

    # 2) Необратимый вызов iiko (``_call_add_payment`` не бросает — ошибки приходят кодом 0).
    status_code, response = await anyio.to_thread.run_sync(lambda: _call_add_payment(payload))
    ok = status_code in (200, 201)
    # «already paid» — накладная в iiko УЖЕ оплачена (иным путём/ранее): для зеркала это
    # идемпотентный УСПЕХ (платёж есть), а не ошибка — иначе оно долбило бы её вечно (при дроблении
    # части с invoice_id=None не капаются). Помечаем ok, чтобы записать done и остановиться.
    if not ok and _is_already_paid(response):
        ok = True
    iiko_doc = None
    if isinstance(response, dict):
        iiko_doc = (
            response.get("documentId")
            or response.get("id")
            or response.get("accountingTransactionId")
        )
    error = None if ok else json.dumps(response, ensure_ascii=False)[:500]

    # 3) Финализация маркера. Если ЭТОТ commit упадёт после 201 — строка остаётся ``pending`` и не
    # перепошлётся (исключена из выборки), что безопаснее двойной оплаты.
    record.status = "ok" if ok else "error"
    record.iiko_document_id = iiko_doc
    record.error = error
    record.response_payload = response if isinstance(response, dict) else {"raw": response}
    # «Документ не найден в Cloud» — не перманентный отказ, а рассинхрон iikoServer↔Cloud: документ
    # есть в Server, add_payment пройдёт, когда он доедет в Cloud. НЕ засчитываем попыткой в кап
    # MAX_PUSH_ATTEMPTS — сверочный джоб продолжает ретраить; предохранитель по возрасту документа
    # на стороне джоба (заведёт ручной кейс, если документ не появится за grace-окно).
    if not ok and _is_incoming_invoice_not_found(record.response_payload, error):
        record.attempts = max(0, record.attempts - 1)
    await session.commit()

    return IikoPushResult(
        ok=ok, skipped=False, status_code=status_code, payload=payload,
        response=record.response_payload, error=error,
    )


async def mirror_paid_iiko_invoices(
    session: AsyncSession, *, limit: int = 50
) -> dict[str, int]:
    """Сверочный джоб: зеркалировать в iiko оплаты iiko-накладных, оплаченных через банк.

    Берём накладные ``source='iiko'`` (есть ``external_id`` = iiko documentId),
    ``direction='payable'``, ``payment_status='paid'``, привязанные к ОПЛАЧЕННОМУ банк-черновику
    (``draft.status='paid'`` ⇒ платёж прошёл через банк, а не наличными/бартером), по которым ещё
    НЕТ пуша ok/pending и не исчерпан кап попыток.

    Для однонакладного черновика сумма по-прежнему = ``draft.amount`` (оплаченный банком остаток,
    не gross накладной). Для мультинакладного берём по каждой накладной сумму её аллокаций,
    привязанных к банковской ДДС-проводке этого черновика: это честная доля, без пропорций и
    догадок. Нулевую долю не шлём; расхождение суммы долей с ``draft.amount`` оставляем видимым
    owner-review кейсом. Контрагентов с ``relationship='barter'`` или прямой бартерной активностью
    (receivable/явная barter-role накладная) автоматически не зеркалим: встречные продажи/возвраты
    смешаны с долгом поставщика и требуют ручной сверки баланса.

    Шлём ``add_payment`` на эквайринг-счёт (idempotent по ``invoice:<id>``). Ошибка по одной не
    валит остальные. Путь гашения НЕ трогаем.
    """
    blocked_invoice_ids = select(IikoInvoicePaymentPush.invoice_id).where(
        IikoInvoicePaymentPush.invoice_id.is_not(None),
        or_(
            IikoInvoicePaymentPush.status.in_(("ok", "pending")),
            IikoInvoicePaymentPush.attempts >= MAX_PUSH_ATTEMPTS,
        ),
    )
    # Число накладных у черновика: для одной draft.amount и есть банковская доля, для нескольких
    # доли ниже читаются из InvoicePaymentAllocation, привязанных к ДДС-проводке черновика.
    sibling_count = (
        select(func.count(SupplierInvoice.id))
        .where(SupplierInvoice.draft_id == CounterpartyPaymentDraft.id)
        .correlate(CounterpartyPaymentDraft)
        .scalar_subquery()
    )
    barter_invoice = aliased(SupplierInvoice)
    has_barter_activity = (
        select(barter_invoice.id)
        .where(
            barter_invoice.counterparty_id == SupplierInvoice.counterparty_id,
            or_(
                barter_invoice.direction == "receivable",
                barter_invoice.barter_role.is_not(None),
            ),
        )
        .correlate(SupplierInvoice)
        .exists()
    )
    rows = (
        await session.execute(
            select(
                SupplierInvoice,
                CounterpartyPaymentDraft.id,
                CounterpartyPaymentDraft.amount,
                sibling_count,
                CounterpartyPayableProfile.relationship,
                has_barter_activity,
            )
            .join(CounterpartyPaymentDraft, CounterpartyPaymentDraft.id == SupplierInvoice.draft_id)
            .outerjoin(
                CounterpartyPayableProfile,
                CounterpartyPayableProfile.counterparty_id == SupplierInvoice.counterparty_id,
            )
            .where(
                SupplierInvoice.source == "iiko",
                SupplierInvoice.external_id.is_not(None),
                SupplierInvoice.direction == "payable",
                SupplierInvoice.payment_status == "paid",
                CounterpartyPaymentDraft.status == "paid",
                SupplierInvoice.id.not_in(blocked_invoice_ids),
                # Скорректированную оплаченную накладную НЕ зеркалим: её external_id уже указывает
                # на НОВУЮ приходную (Y), которую платить нельзя — зачёт завышает баланс поставщика
                # (Y покрыта возвратом старого прихода). См. book_correction_in_iiko / edit_paid.
                SupplierInvoice.iiko_correction_new_external_id.is_(None),
            )
            .order_by(SupplierInvoice.created_at)
            .limit(limit)
        )
    ).all()

    result = {"eligible": len(rows), "ok": 0, "skipped": 0, "error": 0, "skipped_multi": 0}
    multi_draft_ids = {draft_id for _, draft_id, _, siblings, _, _ in rows if siblings > 1}
    multi_shares, multi_totals = await _multi_draft_bank_shares(session, multi_draft_ids)
    multi_draft_meta: dict[uuid.UUID, tuple[Decimal, bool, tuple[uuid.UUID, str] | None]] = {}
    multi_success_anchors: dict[uuid.UUID, tuple[uuid.UUID, str]] = {}

    for invoice, draft_id, draft_amount, siblings, relationship, barter_activity in rows:
        # Захватываем поля в локали ДО пуша: после возможного session.rollback() обращение к
        # ORM-инстансу подняло бы MissingGreenlet (lazy-IO в async) и оборвало бы весь батч.
        inv_id = invoice.id
        external_id = invoice.external_id or ""
        requires_barter_review = relationship == "barter" or barter_activity
        payment_amount = Decimal(str(draft_amount)).quantize(Decimal("0.01"))
        if siblings > 1:
            payment_amount = multi_shares.get((draft_id, inv_id), Decimal("0.00"))
            meta = multi_draft_meta.get(draft_id)
            anchor = meta[2] if meta is not None else None
            multi_draft_meta[draft_id] = (
                Decimal(str(draft_amount)).quantize(Decimal("0.01")),
                requires_barter_review,
                anchor or (inv_id, external_id),
            )
        if siblings > 1 and requires_barter_review:
            logger.info(
                "iiko mirror: накладная %s у взаимозачётного контрагента — ручная сверка",
                inv_id,
            )
            result["skipped_multi"] += 1
            await _open_iiko_payment_case(
                session, invoice_id=inv_id, external_id=external_id, amount=payment_amount,
                reason=(
                    "контрагент работает по взаимозачёту: к его долгу в iiko примешаны встречные "
                    "продажи и возвраты — автоматическая оплата отключена, сверьте итоговый баланс"
                ),
                reason_code="barter_counterparty",
            )
            continue
        if siblings > 1 and payment_amount <= 0:
            logger.info(
                "iiko mirror: у накладной %s нет подтверждённой банковской доли — ручная сверка",
                inv_id,
            )
            result["skipped_multi"] += 1
            await _open_iiko_payment_case(
                session, invoice_id=inv_id, external_id=external_id, amount=payment_amount,
                reason=(
                    "для этой накладной в банковском платеже нет подтверждённой доли — "
                    "автоматическая оплата не отправлена, проверьте способ её погашения"
                ),
                reason_code="multi_invoice",
            )
            continue
        try:
            res = await push_invoice_payment_to_iiko(
                session,
                external_id=external_id,
                amount=payment_amount,
                account_id=IIKO_ACQUIRING_ACCOUNT,
                idempotency_key=f"invoice:{inv_id}",
                invoice_id=inv_id,
            )
        except Exception:  # noqa: BLE001 — ошибка по одной накладной не валит весь проход
            await session.rollback()
            logger.warning("iiko mirror: ошибка пуша по накладной %s", inv_id, exc_info=True)
            result["error"] += 1
            continue
        if res.skipped:
            if res.ok:
                result["skipped"] += 1
                await _resolve_iiko_payment_case(session, inv_id)
                if siblings > 1:
                    multi_success_anchors.setdefault(draft_id, (inv_id, external_id))
            else:
                # осиротевший pending (процесс упал между commit pending и финализацией) — сам
                # не переотправится, нужен ручной разбор, иначе неоплата в iiko потерялась бы тихо.
                result["error"] += 1
                await _open_iiko_payment_case(
                    session, invoice_id=inv_id, external_id=external_id, amount=payment_amount,
                    reason="осиротевший pending-пуш add_payment — нужен ручной разбор",
                    reason_code="orphaned_pending",
                )
        elif res.ok:
            result["ok"] += 1
            await _resolve_iiko_payment_case(session, inv_id)  # оплата дошла — снять висящий кейс
            if siblings > 1:
                multi_success_anchors.setdefault(draft_id, (inv_id, external_id))
            logger.info(
                "iiko mirror: оплата накладной %s (%s) отправлена в iiko", inv_id, external_id
            )
        elif (
            res.status_code is not None
            and 400 <= res.status_code < 500
            and res.status_code != 429
        ):
            result["error"] += 1
            not_found = _is_incoming_invoice_not_found(res.response, res.error)
            if not_found and _within_iiko_sync_grace(invoice):
                # Документ ещё не виден в iiko Cloud (рассинхрон iikoServer↔Cloud) — НЕ терминал:
                # add_payment пройдёт, когда документ доедет в Cloud. Кейс — для видимости; ретрай
                # следующим проходом (attempts для not-found не капится).
                await _open_iiko_payment_case(
                    session, invoice_id=inv_id, external_id=external_id, amount=payment_amount,
                    reason=(
                        "документ ещё не виден в iiko Cloud (рассинхрон iikoServer↔Cloud) — "
                        "оплата дойдёт автоматически после синхронизации"
                    ),
                    reason_code="not_in_cloud_grace",
                )
                logger.warning(
                    "iiko mirror: накладная %s ещё не в iiko Cloud — ждём синхронизации", inv_id
                )
            elif not_found:
                # Документ не появился в Cloud за grace-окно → терминал: attempts для not-found
                # компенсируется и сам до капа не дойдёт — блокируем ретраи явно.
                await _cap_push_attempts(session, idempotency_key=f"invoice:{inv_id}")
                await _open_iiko_payment_case(
                    session, invoice_id=inv_id, external_id=external_id, amount=payment_amount,
                    reason="документ не появился в iiko Cloud за отведённое окно — ручной разбор",
                    reason_code="not_in_cloud_terminal",
                )
                logger.warning(
                    "iiko mirror: накладная %s не появилась в iiko Cloud — терминал", inv_id
                )
            else:
                # Настоящий перманентный отказ iiko (invalid amount и т.п.) — кейс; attempts растёт
                # естественно до капа (blocked_invoice_ids), авто-ретраи прекращаются сами.
                logger.warning(
                    "iiko mirror: iiko перманентно отклонил накладную %s: %s", inv_id, res.error
                )
                await _open_iiko_payment_case(
                    session, invoice_id=inv_id, external_id=external_id, amount=payment_amount,
                    reason=res.error or f"HTTP {res.status_code}",
                    reason_code="iiko_rejected",
                )
        else:
            # временный сбой (429 / 5xx / сеть) — кап доберётся на следующих проходах
            result["error"] += 1
            logger.warning("iiko mirror: временный сбой накладной %s: %s", inv_id, res.error)

    # «Не угадывай»: даже если честные доли удалось отправить, их сумма может не совпасть с
    # draft.amount (остаток закрыт авансом/наличными/зачётом либо данные неполны). После успешных
    # пушей старые multi-кейсы уже закрыты, поэтому заводим ОДИН отдельный кейс на расхождение.
    # Свип не должен автозакрывать его только по ok-пушу накладной: само расхождение ещё не
    # разобрано.
    for draft_id, (
        draft_amount,
        requires_barter_review,
        fallback_anchor,
    ) in multi_draft_meta.items():
        if requires_barter_review:
            continue  # по каждой накладной уже открыт более сильный barter-кейс
        attributed = multi_totals.get(draft_id, Decimal("0.00")).quantize(Decimal("0.01"))
        discrepancy = (draft_amount - attributed).quantize(Decimal("0.01"))
        if discrepancy == 0:
            continue
        anchor = multi_success_anchors.get(draft_id) or fallback_anchor
        if anchor is None:
            continue
        anchor_id, anchor_external_id = anchor
        await _open_iiko_payment_case(
            session,
            invoice_id=anchor_id,
            external_id=anchor_external_id,
            amount=abs(discrepancy),
            reason=(
                f"сумма подтверждённых банковских долей по накладным ({attributed} ₽) "
                f"не совпадает с суммой платежа ({draft_amount} ₽); расхождение "
                f"{abs(discrepancy)} ₽ не распределено автоматически — сверьте его вручную"
            ),
            reason_code="multi_invoice_residual",
        )
    return result


async def original_payment_settled_in_iiko(
    session: AsyncSession, invoice: SupplierInvoice
) -> bool:
    """Отражена ли оплата ЭТОЙ накладной в iiko (для гарда правки оплаченной).

    Признак зависит от источника — у контуров iiko и Кассы РАЗНЫЕ зеркала add_payment:
    - ``source='iiko'`` (банк-зеркало ``mirror_paid_iiko_invoices``): ok-строка по ключу
      ``invoice:<id>``. ``ok`` = успешный ``add_payment`` (201) / идемпотентный «already paid» /
      summary-строка дробления / ручное подтверждение. ``pending``/``error``/нет = НЕ дошла.
    - ``source in (kassa_cheque, kassa_invoice)`` (товарное зеркало ``mirror_paid_kassa_invoices``,
      namespace ``kassa_goods:*``): успешный пуш доли ``kassa_goods:<id>:*`` со ``status='ok'`` ИЛИ
      ручное подтверждение (маркер ``kassa_goods_done:<id>`` с ``manual_confirmation``). Просто
      done-маркер БЕЗ ручного флага и без ok-доли (терминальный сбой / no-goods / backlog) settled
      НЕ считаем — консервативно держим коррекцию (лучше переблок, чем перекос баланса).

    Гард держит ЛЮБУЮ накладную этих источников с ``external_id`` без подтверждённой оплаты (в т.ч.
    оплаченную наличными/зачётом аванса iiko-накладную — у неё банк-зеркала нет, эскейп = ручное
    подтверждение). Прозрачен (True) только для источников без add_payment-зеркала (``manual``,
    бартер) и для накладных без ``external_id`` — там ждать в iiko нечего."""
    if not invoice.external_id:
        return True
    if invoice.source == "iiko":
        row = await session.scalar(
            select(IikoInvoicePaymentPush.id)
            .where(
                IikoInvoicePaymentPush.idempotency_key == f"invoice:{invoice.id}",
                IikoInvoicePaymentPush.status == "ok",
            )
            .limit(1)
        )
        return row is not None
    if invoice.source in ("kassa_cheque", "kassa_invoice"):
        share_ok = await session.scalar(
            select(IikoInvoicePaymentPush.id)
            .where(
                IikoInvoicePaymentPush.idempotency_key.like(
                    f"kassa\\_goods:{invoice.id}:%", escape="\\"
                ),
                IikoInvoicePaymentPush.status == "ok",
            )
            .limit(1)
        )
        if share_ok is not None:
            return True
        manual = await session.scalar(
            select(IikoInvoicePaymentPush.id)
            .where(
                IikoInvoicePaymentPush.idempotency_key == f"kassa_goods_done:{invoice.id}",
                IikoInvoicePaymentPush.response_payload["manual_confirmation"].astext == "true",
            )
            .limit(1)
        )
        return manual is not None
    # Прочие источники (manual/бартер) add_payment-зеркала в iiko не имеют — гард прозрачен.
    return True


async def _iiko_payment_fully_settled(
    session: AsyncSession, invoice: SupplierInvoice
) -> bool:
    """СТРОЖЕ, чем ``original_payment_settled_in_iiko`` — для АВТО-ЗАКРЫТИЯ кейса свипом. Отличие
    только для Кассы: у смешанного чека (карта+наличные) две доли ``kassa_goods:<id>:card/cash``;
    ``original_payment_settled_in_iiko`` считает settled по ЛЮБОЙ ok-доле, но если ВТОРАЯ доля
    провалилась — оплата отражена не полностью, кейс закрывать НЕЛЬЗЯ. Здесь: есть хоть одна
    доля со ``status!='ok'`` (провал/висит) → НЕ settled. Иначе — как обычный предикат."""
    if invoice.source in ("kassa_cheque", "kassa_invoice"):
        bad_share = await session.scalar(
            select(IikoInvoicePaymentPush.id)
            .where(
                IikoInvoicePaymentPush.idempotency_key.like(
                    f"kassa\\_goods:{invoice.id}:%", escape="\\"
                ),
                IikoInvoicePaymentPush.status != "ok",
            )
            .limit(1)
        )
        if bad_share is not None:
            return False
    return await original_payment_settled_in_iiko(session, invoice)


async def iiko_invoice_payment_auto_sendable(
    session: AsyncSession, invoice: SupplierInvoice
) -> bool:
    """Можно ли отправить оплату в iiko автоматически кнопкой «Отправить оплату в iiko сейчас».

    Зеркало умеет только ОДНОНАКЛАДНЫЙ банк-черновик (у него ``draft.amount`` = честная сумма add_
    payment). Мультиплатёж/зачёт аванса/наличные/Сейф долю по одной накладной не выводят — там
    только ручное подтверждение. Уже скорректированную накладную (external_id = новая приходная Y)
    слать нельзя."""
    if (
        invoice.source != "iiko"
        or not invoice.external_id
        or invoice.draft_id is None
        or invoice.iiko_correction_new_external_id is not None
    ):
        return False
    draft = await session.get(CounterpartyPaymentDraft, invoice.draft_id)
    if draft is None or draft.status != "paid" or draft.pays_via_safe:
        return False
    # Застрявший pending-пуш (осиротевший): авто-отправка его пропустит (push видит pending и
    # возвращает skipped) — реально не сработает, поэтому НЕ авто (остаётся ручное подтверждение
    # после сверки в iiko; иначе кейс orphaned_pending был бы неразрешим ни одной кнопкой).
    pending = await session.scalar(
        select(IikoInvoicePaymentPush.id)
        .where(
            IikoInvoicePaymentPush.idempotency_key == f"invoice:{invoice.id}",
            IikoInvoicePaymentPush.status == "pending",
        )
        .limit(1)
    )
    if pending is not None:
        return False
    siblings = await session.scalar(
        select(func.count(SupplierInvoice.id)).where(
            SupplierInvoice.draft_id == invoice.draft_id
        )
    )
    return siblings == 1


async def mirror_single_paid_iiko_invoice(
    session: AsyncSession,
    invoice_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> IikoPushResult:
    """Отправить оплату ОДНОЙ оплаченной iiko-накладной в iiko сейчас (ручное действие владельца:
    кнопка «Отправить оплату в iiko сейчас» в гарде правки / панели owner-review). Тот же путь, что
    у сверочного джоба ``mirror_paid_iiko_invoices``, но по одной накладной и синхронно.

    Бросает :class:`IikoPaymentError` с человекочитаемым текстом, когда авто-отправка невозможна
    (не iiko / без external_id / не финализирован банк / мультиплатёж / уже скорректирована) — тогда
    остаётся только «Оплата подтверждена вручную». Идемпотентно (ключ ``invoice:<id>``): «already
    paid» вернётся как ok, повторный вызов после ok — skipped."""
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        raise IikoPaymentError("Накладная не найдена")
    if invoice.source != "iiko" or not invoice.external_id:
        raise IikoPaymentError("Накладная не выгружена в iiko — отправлять оплату нечего")
    if invoice.payment_status not in ("paid", "partially_paid"):
        raise IikoPaymentError("Накладная ещё не оплачена")
    if invoice.iiko_correction_new_external_id is not None:
        raise IikoPaymentError(
            "Накладная уже скорректирована — оплату новой приходной в iiko слать нельзя"
        )
    if invoice.draft_id is None:
        raise IikoPaymentError(
            "Оплата прошла не через банк (аванс/наличные) — автоматически отправить в iiko нельзя, "
            "подтвердите оплату вручную"
        )
    draft = await session.get(CounterpartyPaymentDraft, invoice.draft_id)
    if draft is None or draft.status != "paid" or draft.pays_via_safe:
        raise IikoPaymentError(
            "Банковский платёж ещё не финализирован — автоматически отправить в iiko нельзя"
        )
    siblings = await session.scalar(
        select(func.count(SupplierInvoice.id)).where(
            SupplierInvoice.draft_id == invoice.draft_id
        )
    )
    if siblings != 1:
        raise IikoPaymentError(
            "Платёж закрывает несколько накладных — долю по одной автоматически отправить нельзя, "
            "подтвердите оплату вручную"
        )
    res = await push_invoice_payment_to_iiko(
        session,
        external_id=invoice.external_id,
        amount=draft.amount,
        account_id=IIKO_ACQUIRING_ACCOUNT,
        idempotency_key=f"invoice:{invoice_id}",
        invoice_id=invoice_id,
        actor_user_id=actor_user_id,
    )
    if res.ok:
        await _resolve_iiko_payment_case(session, invoice_id)  # оплата дошла — снять кейс
    return res


async def mark_iiko_payment_settled_manually(
    session: AsyncSession,
    invoice: SupplierInvoice,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> None:
    """Пометить оплату накладной «подтверждена вручную в iiko»: человек утверждает, что платёж уже
    проведён в бэк-офисе iiko (гибридный контур — бухгалтер иногда платит там руками).

    Пишем ok-маркер зеркала → гард правки оплаченной проходит, а сверочный джоб больше НЕ пытается
    ``add_payment``. Ключ — по контуру источника: iiko → ``invoice:<id>``, Касса →
    ``kassa_goods_done:<id>`` (с ``manual_confirmation``). Фиксируем, кто/когда, для аудита.

    Отказ, если оплату можно отправить АВТОМАТИЧЕСКИ (одиночный банк-черновик): вслепую подтверждать
    её нельзя — это заглушило бы реальное зеркало и оплата в iiko не появилась бы (тот самый перекос
    АЛЬЯНС ЮГ). Для таких — «Отправить оплату в iiko сейчас» (``mirror_single_paid_iiko_invoice``).
    """
    if await iiko_invoice_payment_auto_sendable(session, invoice):
        raise IikoPaymentError(
            "Оплату можно отправить автоматически — используйте «Отправить оплату в iiko сейчас»"
        )
    key = (
        f"kassa_goods_done:{invoice.id}"
        if invoice.source in ("kassa_cheque", "kassa_invoice")
        else f"invoice:{invoice.id}"
    )
    record = await session.scalar(
        select(IikoInvoicePaymentPush).where(IikoInvoicePaymentPush.idempotency_key == key)
    )
    # Не затираем реальный успешный пуш (с iiko_document_id) — иначе теряем аудиторскую ссылку на
    # проводку iiko; оплата и так отражена, подтверждать нечего (но кейс, если висит, закрываем).
    if record is not None and record.status == "ok" and record.iiko_document_id:
        await _resolve_iiko_payment_case(session, invoice.id)
        return
    if record is None:
        record = IikoInvoicePaymentPush(idempotency_key=key)
        session.add(record)
    record.invoice_id = invoice.id
    record.external_id = invoice.external_id or "-"
    record.amount = Decimal(str(invoice.amount or 0))
    record.account_to = "manual"
    record.status = "ok"
    record.attempts = 0
    record.iiko_document_id = None
    record.error = "Оплата подтверждена вручную в iiko"
    record.request_payload = {"manual_confirmation": True}
    record.response_payload = {
        "manual_confirmation": True,
        "by": str(actor_user_id) if actor_user_id else None,
    }
    record.created_by_user_id = actor_user_id
    await session.commit()
    await _resolve_iiko_payment_case(session, invoice.id)  # подтверждено вручную — снять кейс


# Счёт-источник денег iiko по источнику оплаты доли (зеркало Кассы): карта/банк/pending →
# эквайринг, наличные → Главная касса. Оба счёта подтверждены реальными 1₽-проводками (201).
KASSA_MIRROR_ACCOUNT = {
    "card": IIKO_ACQUIRING_ACCOUNT,
    "cash": WALLET_TO_IIKO_ACCOUNT["tk_chernikova"],
}


async def _mark_kassa_goods_done(
    session: AsyncSession, *, invoice_id: uuid.UUID, external_id: str, note: str
) -> None:
    """Пометить товарную часть документа Кассы «зеркалирование завершено» — исключить его из
    будущих проходов джоба (иначе LIMIT-окно из свежих забилось бы ``ok``-документами и хвост-
    straggler'ы голодали бы). Маркер — спец-строка ``kassa_goods_done:<id>`` (idempotent)."""
    key = f"kassa_goods_done:{invoice_id}"
    existing = await session.scalar(
        select(IikoInvoicePaymentPush).where(IikoInvoicePaymentPush.idempotency_key == key)
    )
    if existing is not None:
        return
    session.add(
        IikoInvoicePaymentPush(
            idempotency_key=key,
            invoice_id=invoice_id,
            external_id=external_id or "-",
            amount=Decimal("0"),
            account_to="-",
            status="ok",
            attempts=0,
            error=note,
            request_payload={},
            response_payload={},
        )
    )
    await session.commit()


async def mirror_paid_kassa_invoices(
    session: AsyncSession, *, limit: int = 100
) -> dict[str, int]:
    """Сверочный джоб: зеркалировать в iiko ТОВАРНУЮ оплату оплаченных чеков/накладных Кассы.

    Берём ``source IN (kassa_cheque, kassa_invoice)``, ``payment_status='paid'`` с ``external_id``
    (товар уже в iiko incomingInvoice) и БЕЗ маркера завершения. Товарную сумму (= сумму документа,
    тот же фильтр, что у ``prepare_push``) разносим по источникам и шлём ``add_payment`` (НОПЛ) —
    карта→эквайринг, наличные→Главная касса — ВМЕСТО изъятия «Оплата поставщикам» (его теперь
    пропускает ``post_kassa_payment_to_iiko(skip_supplier=True)``). Идемпотентно по
    ``kassa_goods:<id>:<src>`` (отдельный namespace, не пересекается с банковским ``invoice:<id>``);
    ошибка по доле не валит проход; кап ``MAX_PUSH_ATTEMPTS``. Зеркалим только ПОЛНУЮ оплату —
    частичные доплаты pay-kassa пока нет (решение владельца).

    BACKLOG: документы, чей товар УЖЕ погашён старым addPayOut (созданы до перехода), НЕ
    add_payment'им — иначе долг поставщика в iiko гасится дважды; их сразу помечаем завершёнными.
    """
    from app.services.kassa.cheque_payout_push import (
        compute_kassa_goods_split,
        supplier_goods_already_paid_in_iiko,
    )

    # Исключаем уже завершённые (маркер kassa_goods_done) — иначе LIMIT-окно забилось бы ok-
    # документами и хвост голодал бы (банковский mirror исключает done через blocked_invoice_ids).
    # escape='\\': подчёркивания в литерале — буквальные, не LIKE-wildcard.
    done_ids = select(IikoInvoicePaymentPush.invoice_id).where(
        IikoInvoicePaymentPush.idempotency_key.like("kassa\\_goods\\_done:%", escape="\\"),
        IikoInvoicePaymentPush.status == "ok",
        IikoInvoicePaymentPush.invoice_id.is_not(None),
    )
    rows = (
        await session.scalars(
            select(SupplierInvoice)
            .where(
                SupplierInvoice.source.in_(("kassa_cheque", "kassa_invoice")),
                SupplierInvoice.external_id.is_not(None),
                SupplierInvoice.payment_status == "paid",
                SupplierInvoice.id.not_in(done_ids),
                # Скорректированную накладную НЕ зеркалим: её external_id уже указывает на новую
                # приходную Y, платить которую зачётом нельзя (завышает баланс поставщика).
                # Симметрично банковскому mirror; иначе коррекция Кассы задваивала бы приход на Y.
                SupplierInvoice.iiko_correction_new_external_id.is_(None),
            )
            .order_by(SupplierInvoice.created_at)
            .limit(limit)
        )
    ).all()

    result = {
        "eligible": len(rows), "ok": 0, "skipped": 0, "error": 0,
        "no_goods": 0, "backlog": 0, "manual": 0,
    }
    for invoice in rows:
        # Поля в локали ДО пуша: после commit/rollback доступ к ORM-инстансу мог бы поднять
        # MissingGreenlet (lazy-IO в async) и оборвать батч.
        inv_id = invoice.id
        external_id = invoice.external_id or ""
        if invoice.issued_at is not None:
            payment_dt = invoice.issued_at
        elif invoice.invoice_date is not None:
            payment_dt = datetime.combine(invoice.invoice_date, time(12, 0), _MSK)
        else:
            payment_dt = datetime.now(_MSK)

        # BACKLOG: товар уже погашён старым addPayOut → не add_payment'им (задвоило бы) → done.
        if await supplier_goods_already_paid_in_iiko(session, inv_id):
            await _mark_kassa_goods_done(
                session, invoice_id=inv_id, external_id=external_id, note="backlog: addPayOut"
            )
            result["backlog"] += 1
            continue

        split = await compute_kassa_goods_split(session, inv_id)
        if split is None:
            await _mark_kassa_goods_done(
                session, invoice_id=inv_id, external_id=external_id, note="нет товарной части"
            )
            result["no_goods"] += 1
            continue
        card_share, cash_share = split

        # Доли терминальны (ok / skipped / необратимый провал) → закрываем накладную done; при
        # временном сбое возвращаемся следующим проходом. НЕОБРАТИМЫЙ провал (непредставимая сумма /
        # кап / перманентный отказ iiko) НЕ прячем под done — заводим видимый кейс owner-review
        # (раньше кап тихо помечался «зеркалировано», и неоплата в iiko терялась).
        terminal_fail: list[str] = []
        transient = False
        goods_total = card_share + cash_share
        for src, share in (("card", card_share), ("cash", cash_share)):
            if share <= 0:
                continue
            key = f"kassa_goods:{inv_id}:{src}"
            existing = await session.scalar(
                select(IikoInvoicePaymentPush).where(
                    IikoInvoicePaymentPush.idempotency_key == key
                )
            )
            if existing is not None and existing.status == "ok":
                result["skipped"] += 1
                continue
            if existing is not None and existing.attempts >= MAX_PUSH_ATTEMPTS:
                terminal_fail.append(f"{src}: {existing.error or 'исчерпан кап попыток'}")
                continue
            try:
                res = await push_invoice_payment_to_iiko(
                    session,
                    external_id=external_id,
                    amount=share,
                    account_id=KASSA_MIRROR_ACCOUNT[src],
                    idempotency_key=key,
                    payment_dt=payment_dt,
                    invoice_id=inv_id,
                )
            except Exception:  # noqa: BLE001 — ошибка по одной доле не валит весь проход
                await session.rollback()
                logger.warning("kassa mirror: ошибка пуша %s", key, exc_info=True)
                transient = True
                result["error"] += 1
                continue
            if res.skipped:
                if res.ok:
                    result["skipped"] += 1
                else:
                    # осиротевший pending — сам не переотправится, нужен ручной разбор
                    terminal_fail.append(f"{src}: осиротевший pending-пуш — ручной разбор")
                    result["error"] += 1
            elif res.ok:
                result["ok"] += 1
                logger.info("kassa mirror: товарная оплата %s отправлена в iiko", key)
            elif (
                res.status_code is not None
                and 400 <= res.status_code < 500
                and res.status_code != 429
            ):
                if _is_incoming_invoice_not_found(res.response, res.error) and (
                    _within_iiko_sync_grace(invoice)
                ):
                    # Документ ещё не в iiko Cloud (рассинхрон Server↔Cloud) — НЕ терминал: ретраим
                    # следующим проходом (накладную НЕ закрываем done), кейс — для видимости.
                    transient = True
                    result["error"] += 1
                    await _open_iiko_payment_case(
                        session, invoice_id=inv_id, external_id=external_id, amount=goods_total,
                        reason=(
                            "документ ещё не виден в iiko Cloud (рассинхрон iikoServer↔Cloud) — "
                            "оплата дойдёт автоматически после синхронизации"
                        ),
                        reason_code="not_in_cloud_grace",
                    )
                    logger.warning("kassa mirror: %s ещё не в iiko Cloud — ждём синхронизации", key)
                else:
                    # перманентный отказ iiko (invalid amount и т.п.) ИЛИ документ не появился за
                    # grace-окно — ручной разбор.
                    terminal_fail.append(f"{src}: {res.error or f'HTTP {res.status_code}'}")
                    result["error"] += 1
                    logger.warning("kassa mirror: iiko перманентно отклонил %s: %s", key, res.error)
            else:
                # временный сбой (429 / 5xx / сеть) — добор на следующем проходе
                transient = True
                result["error"] += 1
                logger.warning("kassa mirror: временный сбой %s: %s", key, res.error)

        # Кейс заводим СРАЗУ при любом терминальном провале — даже если рядом флапает transient-доля
        # (иначе терминальный провал прятался бы за ней до её собственного капа). Idempotent по
        # накладной, так что повторные проходы не плодят дубли.
        if terminal_fail:
            reason = "; ".join(terminal_fail)
            await _open_iiko_payment_case(
                session,
                invoice_id=inv_id,
                external_id=external_id,
                amount=goods_total,
                reason=reason,
                reason_code="iiko_rejected",
            )
        if transient:
            continue  # есть transient-доля → накладную НЕ закрываем, доберём следующим проходом
        if terminal_fail:
            await _mark_kassa_goods_done(
                session,
                invoice_id=inv_id,
                external_id=external_id,
                note=f"iiko отклонил оплату (ручной разбор): {'; '.join(terminal_fail)}",
            )
            result["manual"] += 1
        else:
            await _mark_kassa_goods_done(
                session, invoice_id=inv_id, external_id=external_id, note="зеркалировано"
            )
            await _resolve_iiko_payment_case(session, inv_id)  # товар зеркалирован — снять кейс
    return result


async def sweep_unsettled_iiko_payments(
    session: AsyncSession, *, limit: int = 500
) -> dict[str, int]:
    """Дозор единого видимого списка «у нас оплачено — в iiko нет»: ловит причины, которые сверочные
    джобы ``mirror_*`` НЕ видят (молча пропущены их выборкой), и закрывает восстановившиеся кейсы.

    Категории (каждую — в owner-review через ``_open_iiko_payment_case``, idempotent по накладной):
    - ``paid_outside_draft`` — iiko-накладная оплачена НЕ через банк-черновик (аванс/наличные/прямое
      сопоставление): банк-зеркало её вовсе не берёт (нет черновика в статусе ``paid``);
    - ``correction_unsettled`` — накладная скорректирована (``external_id`` уже = приходная Y),
      а оплата ОРИГИНАЛА так и не была зеркалирована в iiko (гард Этапа 1 закрывает это вперёд,
      здесь ловим наследие);
    - ``retry_cap_exhausted`` — транзиент исчерпал кап (``attempts>=MAX``) и накладная выпала из
      выборки джоба БЕЗ кейса (ветка временного сбоя кейс не заводит);
    - ``orphaned_pending`` — pending-пуш застрял (краш между commit pending и финализацией add_
      payment); ветка mirror для этого мертва (pending исключает накладную из выборки).

    Закрытие кейсов — по СТРОГОМУ предикату ``_iiko_payment_fully_settled`` (для смешанного чека
    Кассы «любая ok-доля» недостаточно — вторая доля могла провалиться). Гоняется тем же scheduler-
    джобом после ``mirror_*``. NB: частичная (partially_paid) банковская оплата С черновиком —
    вне контура (в процессе, дозакроется и отзеркалится); ловим только БЕЗ черновика (застрявший
    ручной мэтч). Возвращает счётчики."""
    result = {
        "resolved": 0,
        "paid_outside_draft": 0,
        "correction_unsettled": 0,
        "retry_cap_exhausted": 0,
        "orphaned_pending": 0,
    }

    # --- 1. Закрыть восстановившиеся кейсы (накладная снова отражена в iiko) ---
    pending = (
        await session.scalars(
            select(ReconciliationCase)
            .where(
                ReconciliationCase.kind == "iiko_payment_unsettled",
                ReconciliationCase.status == "pending",
            )
            .limit(limit)
        )
    ).all()
    resolved_any = False
    for case in pending:
        # Ок-пуши честных долей не доказывают, что разобрано расхождение всего банк-платежа.
        # Такой кейс закрывает только человек после сверки; иначе свип молча спрятал бы остаток.
        if (case.payload or {}).get("reason_code") == "multi_invoice_residual":
            continue
        inv_id_raw = (case.payload or {}).get("invoice_id")
        if not inv_id_raw:
            continue
        try:
            inv = await session.get(SupplierInvoice, uuid.UUID(str(inv_id_raw)))
        except (ValueError, TypeError):
            continue
        # СТРОГИЙ предикат: для Кассы «полностью settled» (все доли), иначе частично-провалившийся
        # смешанный чек (одна ok-доля) ложно закрыл бы кейс, а вторая доля осталась бы не в iiko.
        if inv is not None and await _iiko_payment_fully_settled(session, inv):
            case.status = "resolved"
            case.resolved_at = datetime.now(UTC)
            case.resolution_payload = {"reason": "settled"}
            result["resolved"] += 1
            resolved_any = True
    if resolved_any:
        await session.commit()

    # --- 2. paid_outside_draft: paid iiko без покрытия банк-зеркалом (нет paid-черновика) ---
    outside = (
        await session.scalars(
            select(SupplierInvoice)
            .outerjoin(
                CounterpartyPaymentDraft,
                CounterpartyPaymentDraft.id == SupplierInvoice.draft_id,
            )
            .where(
                SupplierInvoice.source == "iiko",
                SupplierInvoice.external_id.is_not(None),
                SupplierInvoice.direction == "payable",
                # paid — полностью оплачена; partially_paid БЕЗ черновика — застрявший ручной банк-
                # мэтч (деньги ушли частично, но контур не завершится сам). Частичную оплату С
                # черновиком НЕ трогаем — она в процессе (дозакроется → mirror отзеркалит).
                or_(
                    SupplierInvoice.payment_status == "paid",
                    and_(
                        SupplierInvoice.payment_status == "partially_paid",
                        SupplierInvoice.draft_id.is_(None),
                    ),
                ),
                SupplierInvoice.iiko_correction_new_external_id.is_(None),
                # НЕ покрыта банк-зеркалом: нет черновика ЛИБО он не финализирован банком (created/
                # updated/failed) ЛИБО деньги на Сейфе. Мультиплатёж (siblings>1) сюда НЕ попадает —
                # его ловит сам джоб (reason_code=multi_invoice).
                or_(
                    SupplierInvoice.draft_id.is_(None),
                    CounterpartyPaymentDraft.status != "paid",
                    CounterpartyPaymentDraft.pays_via_safe.is_(True),
                ),
            )
            .limit(limit)
        )
    ).all()
    for inv in outside:
        if await original_payment_settled_in_iiko(session, inv):
            continue
        if await _open_iiko_payment_case(
            session, invoice_id=inv.id, external_id=inv.external_id or "",
            amount=Decimal(str(inv.amount or 0)),
            reason=(
                "оплата не отражена в iiko автоматически (не одиночный банк-черновик: аванс / "
                "наличные / частичный мультиплатёж) — проведите оплату вручную или подтвердите"
            ),
            reason_code="paid_outside_draft",
        ):
            result["paid_outside_draft"] += 1

    # --- 3. correction_unsettled: накладная скорректирована, а оплата оригинала не дошла ---
    corrected = (
        await session.scalars(
            select(SupplierInvoice)
            .where(
                # Кассу тоже: mirror_paid_kassa исключает скорректированные, а корректировать можно
                # накладную любого источника — оплата оригинала Кассы могла не доехать (наследие).
                SupplierInvoice.source.in_(("iiko", "kassa_cheque", "kassa_invoice")),
                SupplierInvoice.iiko_correction_new_external_id.is_not(None),
            )
            .limit(limit)
        )
    ).all()
    for inv in corrected:
        if await original_payment_settled_in_iiko(session, inv):
            continue
        if await _open_iiko_payment_case(
            session, invoice_id=inv.id, external_id=inv.external_id or "",
            amount=Decimal(str(inv.amount or 0)),
            reason=(
                "накладная скорректирована, но оплата оригинала так и не отражена в iiko — "
                "сверьте баланс поставщика в бэк-офисе iiko вручную"
            ),
            reason_code="correction_unsettled",
        ):
            result["correction_unsettled"] += 1

    # --- 4. retry_cap_exhausted: транзиент добрался до капа БЕЗ кейса (тихо выпал из выборки) ---
    capped_ids = (
        await session.scalars(
            select(IikoInvoicePaymentPush.invoice_id)
            .where(
                IikoInvoicePaymentPush.invoice_id.is_not(None),
                IikoInvoicePaymentPush.status == "error",
                IikoInvoicePaymentPush.attempts >= MAX_PUSH_ATTEMPTS,
            )
            .distinct()
            .limit(limit)
        )
    ).all()
    for inv_id in capped_ids:
        inv = await session.get(SupplierInvoice, inv_id)
        # скорректированные ловит категория выше; уже settled — пропускаем.
        if inv is None or inv.iiko_correction_new_external_id is not None:
            continue
        if await original_payment_settled_in_iiko(session, inv):
            continue
        # _open idempotent: если кейс уже заведён джобом (перманентный отказ) — no-op; создаётся
        # только для «тихого» транзиент-капа, где ветка временного сбоя кейс не заводила.
        if await _open_iiko_payment_case(
            session, invoice_id=inv.id, external_id=inv.external_id or "",
            amount=Decimal(str(inv.amount or 0)),
            reason=(
                "исчерпан лимит авто-повторов отправки в iiko — повторите отправку или проведите "
                "оплату вручную"
            ),
            reason_code="retry_cap_exhausted",
        ):
            result["retry_cap_exhausted"] += 1

    # --- 5. orphaned_pending: застрявший pending-пуш (краш между commit pending и финализацией —
    #        платёж МОГ и не уйти). Ветка mirror для этого мертва (pending исключает из выборки).
    stale_before = datetime.now(UTC) - IIKO_STALE_PENDING_WINDOW
    stale_ids = (
        await session.scalars(
            select(IikoInvoicePaymentPush.invoice_id)
            .where(
                IikoInvoicePaymentPush.invoice_id.is_not(None),
                IikoInvoicePaymentPush.status == "pending",
                IikoInvoicePaymentPush.updated_at < stale_before,
            )
            .distinct()
            .limit(limit)
        )
    ).all()
    for inv_id in stale_ids:
        inv = await session.get(SupplierInvoice, inv_id)
        if inv is None or await original_payment_settled_in_iiko(session, inv):
            continue
        if await _open_iiko_payment_case(
            session, invoice_id=inv.id, external_id=inv.external_id or "",
            amount=Decimal(str(inv.amount or 0)),
            reason=(
                "отправка оплаты в iiko зависла (процесс прервался) — проверьте в бэк-офисе iiko, "
                "прошёл ли платёж, и подтвердите вручную"
            ),
            reason_code="orphaned_pending",
        ):
            result["orphaned_pending"] += 1

    return result


async def retry_iiko_payment(
    session: AsyncSession,
    invoice_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None = None,
) -> IikoPushResult | None:
    """«Повторить отправку» по кейсу owner-review: снять кап/блокировку и переотправить в iiko.

    Снимаем ``attempts``-кап и ошибку у пушей (kassa done-маркер удаляем — джоб переобрабо-
    тает). Для авто-отправляемой (iiko одиночный банк-черновик) сразу проводим ``add_payment``
    синхронно (успех закрывает кейс). Для остального (Касса и т.п.) снятие капа
    даёт сверочному джобу переотправить, а восстановление закроет кейс (свип/джоб).
    Возвращает :class:`IikoPushResult` синхронного пуша или ``None`` (поставлено в очередь)."""
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        raise IikoPaymentError("Накладная не найдена")
    records = (
        await session.scalars(
            select(IikoInvoicePaymentPush).where(
                IikoInvoicePaymentPush.invoice_id == invoice_id
            )
        )
    ).all()
    changed = False
    for record in records:
        if record.idempotency_key == f"kassa_goods_done:{invoice_id}":
            # done-маркер исключает документ Кассы из выборки джоба — удаляем, чтобы переобработал.
            await session.delete(record)
            changed = True
        elif record.status == "error" or record.attempts >= MAX_PUSH_ATTEMPTS:
            record.attempts = 0
            record.error = None
            changed = True
    if changed:
        await session.commit()
    if await iiko_invoice_payment_auto_sendable(session, invoice):
        return await mirror_single_paid_iiko_invoice(
            session, invoice_id, actor_user_id=actor_user_id
        )
    return None

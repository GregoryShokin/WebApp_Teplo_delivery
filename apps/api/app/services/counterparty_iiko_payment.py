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
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CounterpartyPaymentDraft,
    IikoInvoicePaymentPush,
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


async def _open_iiko_payment_case(
    session: AsyncSession,
    *,
    invoice_id: uuid.UUID,
    external_id: str,
    amount: Decimal,
    reason: str,
) -> None:
    """Завести видимый кейс owner-review «оплата в iiko не проведена» (idempotent по накладной).

    Зовём, когда ``add_payment`` провалился НЕОБРАТИМО (непредставимая сумма / исчерпан кап /
    перманентный отказ iiko): зеркало надо провести в iiko вручную. Без кейса сбой тонул бы в
    маркере ``kassa_goods_done`` («зеркалировано»), а у нас накладная числилась бы оплаченной."""
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
        return
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
            },
        )
    )
    # Самостоятельный commit: кейс должен пережить даже последующий per-item rollback и НЕ зависеть
    # от того, коммитит ли вызывающий после нас (банковский mirror — не коммитит).
    await session.commit()
    logger.warning(
        "iiko mirror: оплата накладной %s НЕ проведена в iiko (%s) — заведён кейс owner-review",
        invoice_id,
        reason,
    )


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

    Сумма = ``draft.amount`` (оплаченный банком остаток, не gross накладной — это исключает
    переплату при частично предоплаченной/наличной накладной). Зеркалим только ОДНОНАКЛАДНЫЕ
    черновики: для мультинакладного честная пер-накладная доля из draft.amount не выводится →
    оставляем на ручную сверку. Шлём ``add_payment`` на эквайринг-счёт (idempotent по
    ``invoice:<id>``). Ошибка по одной не валит остальные. Путь гашения НЕ трогаем.
    """
    blocked_invoice_ids = select(IikoInvoicePaymentPush.invoice_id).where(
        IikoInvoicePaymentPush.invoice_id.is_not(None),
        or_(
            IikoInvoicePaymentPush.status.in_(("ok", "pending")),
            IikoInvoicePaymentPush.attempts >= MAX_PUSH_ATTEMPTS,
        ),
    )
    # Число накладных у черновика — чтобы зеркалить только однонакладные (draft.amount = банк-доля).
    sibling_count = (
        select(func.count(SupplierInvoice.id))
        .where(SupplierInvoice.draft_id == CounterpartyPaymentDraft.id)
        .correlate(CounterpartyPaymentDraft)
        .scalar_subquery()
    )
    rows = (
        await session.execute(
            select(SupplierInvoice, CounterpartyPaymentDraft.amount, sibling_count)
            .join(CounterpartyPaymentDraft, CounterpartyPaymentDraft.id == SupplierInvoice.draft_id)
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
    for invoice, draft_amount, siblings in rows:
        # Захватываем поля в локали ДО пуша: после возможного session.rollback() обращение к
        # ORM-инстансу подняло бы MissingGreenlet (lazy-IO в async) и оборвало бы весь батч.
        inv_id = invoice.id
        external_id = invoice.external_id or ""
        if siblings != 1:
            logger.info(
                "iiko mirror: накладная %s в мультинакладном черновике — пропуск (ручная сверка)",
                inv_id,
            )
            result["skipped_multi"] += 1
            continue
        try:
            res = await push_invoice_payment_to_iiko(
                session,
                external_id=external_id,
                amount=draft_amount,
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
            else:
                # осиротевший pending (процесс упал между commit pending и финализацией) — сам
                # не переотправится, нужен ручной разбор, иначе неоплата в iiko потерялась бы тихо.
                result["error"] += 1
                await _open_iiko_payment_case(
                    session, invoice_id=inv_id, external_id=external_id, amount=draft_amount,
                    reason="осиротевший pending-пуш add_payment — нужен ручной разбор",
                )
        elif res.ok:
            result["ok"] += 1
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
                    session, invoice_id=inv_id, external_id=external_id, amount=draft_amount,
                    reason=(
                        "документ ещё не виден в iiko Cloud (рассинхрон iikoServer↔Cloud) — "
                        "оплата дойдёт автоматически после синхронизации"
                    ),
                )
                logger.warning(
                    "iiko mirror: накладная %s ещё не в iiko Cloud — ждём синхронизации", inv_id
                )
            elif not_found:
                # Документ не появился в Cloud за grace-окно → терминал: attempts для not-found
                # компенсируется и сам до капа не дойдёт — блокируем ретраи явно.
                await _cap_push_attempts(session, idempotency_key=f"invoice:{inv_id}")
                await _open_iiko_payment_case(
                    session, invoice_id=inv_id, external_id=external_id, amount=draft_amount,
                    reason="документ не появился в iiko Cloud за отведённое окно — ручной разбор",
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
                    session, invoice_id=inv_id, external_id=external_id, amount=draft_amount,
                    reason=res.error or f"HTTP {res.status_code}",
                )
        else:
            # временный сбой (429 / 5xx / сеть) — кап доберётся на следующих проходах
            result["error"] += 1
            logger.warning("iiko mirror: временный сбой накладной %s: %s", inv_id, res.error)
    return result


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
    return result

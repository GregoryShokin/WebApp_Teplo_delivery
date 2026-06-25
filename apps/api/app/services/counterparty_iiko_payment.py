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
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import anyio
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CounterpartyPaymentDraft, IikoInvoicePaymentPush, SupplierInvoice

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
    if dry_run:
        return IikoPushResult(
            ok=True, skipped=False, status_code=None, payload=payload, response={"dry_run": True}
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
            result["skipped"] += 1
        elif res.ok:
            result["ok"] += 1
            logger.info(
                "iiko mirror: оплата накладной %s (%s) отправлена в iiko", inv_id, external_id
            )
        else:
            result["error"] += 1
            logger.warning("iiko mirror: iiko отклонил оплату накладной %s: %s", inv_id, res.error)
    return result

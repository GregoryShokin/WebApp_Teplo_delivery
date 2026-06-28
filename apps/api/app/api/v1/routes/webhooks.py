"""Входящие вебхуки банка (T-Банк).

Банк шлёт POST на ``/api/v1/webhooks/tbank/payment-status``. По факту это
realtime-уведомления об ОПЕРАЦИЯХ ПО СЧЁТУ (формат выписки: ``operationId``,
``operationStatus``, ``typeOfOperation``, суммы, ``payer``/``receiver``), а не статус
платёжного документа. Авторизация — токеном ``tbank_webhook_token`` в заголовке
``Authorization``; банк может прислать его как ``Bearer <token>``, так и «голым»
``<token>`` — принимаем оба варианта. Дополнительно — опциональный IP-whitelist
(6 IP банка). Тело с ``operationId`` (операция по счёту) → realtime-вливание в журнал
ДДС тем же стоком, что и поллинг (``ingest_operations``, дедуп по ``operationId``);
поллинг выписки остаётся сверкой. Тело без ``operationId`` (статус платёжного
документа) → гашение черновика накладной/выплаты по ``provider_ref``. Алиас
``/tbank/account-operation`` ведёт в тот же вливающий сток. Входящий контур (банк→мы),
без JWT; подключается заявкой на ``openapi@tbank.ru``.
"""

from __future__ import annotations

import hmac
import logging
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.session import get_session
from app.models import CounterpartyPaymentDraft, PayrollBankDraft, SalaryAdvanceBankDraft
from app.services.bank_payment_status import apply_payment_status
from app.services.banking.base import NormalizedBankOperation, clean_digits
from app.services.banking.tbank import (
    _document_number,
    is_tbank_operation_hold,
    normalize_tbank_statement_row,
)
from app.services.couriers.cloud_shift_ingest import ingest_cloud_shift_event
from app.services.couriers.shift_matching import recalculate_matches
from app.services.payroll_advance_service import apply_advance_draft_status
from app.services.payroll_payouts import apply_payroll_draft_status

router = APIRouter()
logger = logging.getLogger(__name__)
_MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# Идентификатор: сначала платёжные (статус документа), затем операционные (выписка).
_ID_FIELDS = (
    "paymentId",
    "documentId",
    "payment_id",
    "document_id",
    "operationId",
    "documentNumber",
    "id",
)
_STATUS_FIELDS = ("paymentStatus", "documentStatus", "payment_status", "operationStatus", "status")


def _extract(payload: dict[str, Any], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = payload.get(name)
        if value not in (None, ""):
            return str(value)
    return None


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # За одним доверенным прокси (Caddy дописывает реальный peer СПРАВА) последний элемент —
        # настоящий источник. Левые элементы клиент может подделать, поэтому НЕ берём [0].
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else None


def _extract_token(authorization: str | None) -> str:
    """Достать токен из заголовка ``Authorization``.

    Т-Банк может прислать его как ``Bearer <token>``, так и «голым» ``<token>`` —
    принимаем оба варианта (схема, если есть, не секрет — сравниваем обычным ==).
    """
    raw = (authorization or "").strip()
    scheme, sep, rest = raw.partition(" ")
    if sep and scheme.lower() == "bearer":
        return rest.strip()
    return raw


def _authorize_tbank_webhook(
    request: Request, settings: Settings, authorization: str | None
) -> None:
    """Проверить входящий вебхук T-Банка: bearer-токен + опциональный IP-allowlist.

    Один контур авторизации для всех вебхуков банка (``payment-status`` и
    ``account-operation``) — токен и список IP общие (один кабинет T-Банка). Подписи
    тела у банковских вебхуков нет, поэтому это вся верификация. Бросает 401/403.
    """
    if settings.tbank_webhook_token:
        token = _extract_token(authorization)
        # constant-time сравнение секрета.
        if not hmac.compare_digest(token, settings.tbank_webhook_token):
            # Безопасная диагностика: сам токен НЕ логируем, только метрики совпадения.
            logger.warning(
                "tbank webhook 401: ip=%s has_auth=%s len_got=%d len_exp=%d",
                _client_ip(request),
                bool((authorization or "").strip()),
                len(token),
                len(settings.tbank_webhook_token),
            )
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный токен вебхука")
    allowed = [
        ip.strip() for ip in (settings.tbank_webhook_allowed_ips or "").split(",") if ip.strip()
    ]
    if allowed and _client_ip(request) not in allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "IP не в списке разрешённых")


async def _settle_invoice_draft_from_operation(
    session: AsyncSession, operation: NormalizedBankOperation
) -> uuid.UUID | None:
    """Realtime-гашение накладной прямо из вебхука «операция по счёту».

    Раньше черновик доводил до ``paid`` только поллинг (``payment/status`` по ``documentId``);
    теперь это делает входящая расходная операция. Связь точная: при создании платёжки мы
    кладём в тело ``documentNumber`` (детерминированно из нашего ``document_id``), а банк
    возвращает его же в строке выписки — матчим по нему.

    Гасим ДО ``ingest_operations``: ``apply_payment_status`` заводит prebooked-проводку, которую
    эта же операция заберёт при вливании (порядок: гашение раньше claim). Берём черновик только
    при ОДНОЗНАЧНОМ совпадении (номер + сумма) — иначе оставляем сверочному поллингу, чтобы не
    погасить чужой платёж. Идемпотентность и гонку webhook↔polling держит сам
    ``apply_payment_status`` (row-lock + переход только из created/updated)."""
    if operation.direction != "out" or not operation.document_number:
        return None
    doc_number = str(operation.document_number).strip()
    if not doc_number:
        return None
    drafts = (
        await session.scalars(
            select(CounterpartyPaymentDraft).where(
                CounterpartyPaymentDraft.status.in_(("created", "updated")),
                CounterpartyPaymentDraft.payload["documentNumber"].astext == doc_number,
            )
        )
    ).all()
    if len(drafts) != 1:
        if len(drafts) > 1:
            logger.warning(
                "tbank webhook: %d открытых черновиков по documentNumber=%s — оставляю поллингу",
                len(drafts),
                doc_number,
            )
        return None
    draft = drafts[0]
    if (draft.amount - operation.amount).copy_abs() > Decimal("0.01"):
        logger.warning(
            "tbank webhook: documentNumber=%s, сумма %s != %s — оставляю поллингу",
            doc_number,
            draft.amount,
            operation.amount,
        )
        return None
    await apply_payment_status(
        session,
        draft=draft,
        raw_status="executed",
        operation_date=operation.operation_date,
        commit=False,
    )
    logger.info(
        "tbank webhook: накладная погашена из операции по счёту — draft=%s documentNumber=%s",
        draft.id,
        doc_number,
    )
    return draft.id


async def _settle_payroll_draft_from_operation(
    session: AsyncSession, operation: NormalizedBankOperation
) -> uuid.UUID | None:
    """Realtime-проведение транзита банк→Сейф по выплате ЗП прямо из операции по счёту.

    Раньше payroll-черновик доводил до paid только часовой поллинг — поэтому перевод банк→Сейф
    появлялся с задержкой и датой обнаружения (мог встать позже выплат с Сейфа). Теперь входящая
    расходная операция со «своим» ``documentNumber`` (детерминированно из ``document_id``
    черновика) доводит черновик сразу и датирует перевод реальной датой операции. Берём только при
    ОДНОЗНАЧНОМ совпадении (номер + сумма), иначе оставляем сверочному поллингу.
    """
    if operation.direction != "out" or not operation.document_number:
        return None
    doc = str(operation.document_number).strip()
    if not doc:
        return None
    drafts = (
        await session.scalars(
            select(PayrollBankDraft).where(PayrollBankDraft.status.in_(("created", "updated")))
        )
    ).all()
    matches = [
        d
        for d in drafts
        if _document_number(d.document_id) == doc
        and (d.amount - operation.amount).copy_abs() <= Decimal("0.01")
    ]
    if len(matches) != 1:
        if len(matches) > 1:
            logger.warning(
                "tbank webhook: %d payroll-черновиков по documentNumber=%s — оставляю поллингу",
                len(matches),
                doc,
            )
        return None
    draft = matches[0]
    await apply_payroll_draft_status(
        session,
        draft=draft,
        raw_status="executed",
        operation_date=operation.operation_date,
        commit=False,
    )
    logger.info(
        "tbank webhook: транзит ЗП банк→Сейф проведён из операции — draft=%s documentNumber=%s",
        draft.id,
        doc,
    )
    return draft.id


async def _settle_advance_draft_from_operation(
    session: AsyncSession, operation: NormalizedBankOperation
) -> uuid.UUID | None:
    """Realtime-проведение транзита банк→Сейф по банк-выдаче аванса/займа из операции по счёту.

    Аналогично ЗП: входящая расходная операция со «своим» ``documentNumber`` доводит
    advance-черновик до paid сразу и датирует транзит+резерв реальной датой операции (вместо
    отложенного часового поллинга). Только при однозначном совпадении номера и суммы.
    """
    if operation.direction != "out" or not operation.document_number:
        return None
    doc = str(operation.document_number).strip()
    if not doc:
        return None
    drafts = (
        await session.scalars(
            select(SalaryAdvanceBankDraft).where(
                SalaryAdvanceBankDraft.status.in_(("created", "updated"))
            )
        )
    ).all()
    matches = [
        d
        for d in drafts
        if _document_number(d.document_id) == doc
        and (d.amount - operation.amount).copy_abs() <= Decimal("0.01")
    ]
    if len(matches) != 1:
        if len(matches) > 1:
            logger.warning(
                "tbank webhook: %d advance-черновиков по documentNumber=%s — оставляю поллингу",
                len(matches),
                doc,
            )
        return None
    draft = matches[0]
    await apply_advance_draft_status(
        session,
        draft=draft,
        raw_status="executed",
        operation_date=operation.operation_date,
        commit=False,
    )
    logger.info(
        "tbank webhook: транзит выдачи банк→Сейф проведён из операции — draft=%s documentNumber=%s",
        draft.id,
        doc,
    )
    return draft.id


async def _ingest_tbank_account_operation(
    session: AsyncSession, payload: dict[str, Any]
) -> dict[str, Any]:
    """Влить операцию по счёту (тело = строка выписки T-Банка) в журнал ДДС.

    Общая логика для обоих входов: основного ``/tbank/payment-status`` (банк по заявке шлёт
    операции именно туда) и алиаса ``/tbank/account-operation``. Холд (``authorization``) не
    пускаем в баланс — ждём ``transaction``. Дедуп по ``operationId`` в ``ingest_operations``
    делает дубль-фаер идемпотентным. Возврат — тело ответа вебхука.
    """
    operation_id = _extract(payload, ("operationId", "id"))
    if not operation_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Нет идентификатора операции")

    account_number = clean_digits(payload.get("accountNumber"))
    if not account_number:
        # Без номера счёта операция не привяжется к счёту (account_id=NULL) и выпадет из
        # баланса (JOIN по счёту) — молча терять деньги нельзя. 422; сверочный поллинг
        # подберёт операцию со счётом из контекста.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Нет номера счёта в операции")

    if is_tbank_operation_hold(payload):
        logger.info("tbank account-operation холд (authorization): op=%s — пропуск", operation_id)
        return {
            "ok": True,
            "operation_id": operation_id,
            "stage": "authorization",
            "ingested": False,
        }

    # Импорт здесь, а не на уровне модуля: ingest_operations тянет тяжёлый scheduler.
    from app.scheduler import ingest_operations

    operation = normalize_tbank_statement_row(
        payload, account_number, datetime.now(_MOSCOW_TZ).date()
    )
    # Вебхук — основной путь проведения: расходная операция со «своим» documentNumber доводит
    # черновик до paid ДО вливания (prebooked-проводку заберёт эта же операция). Один платёж —
    # один черновик (накладная / ЗП / выдача аванса), short-circuit по documentNumber.
    settled_draft_id = (
        await _settle_invoice_draft_from_operation(session, operation)
        or await _settle_payroll_draft_from_operation(session, operation)
        or await _settle_advance_draft_from_operation(session, operation)
    )
    result = await ingest_operations(session, provider="tbank", operations=[operation])
    await session.commit()
    logger.info(
        "tbank account-operation принят: op=%s dir=%s amount=%s ins=%s upd=%s settled=%s",
        operation_id,
        operation.direction,
        operation.amount,
        result.get("inserted"),
        result.get("updated"),
        settled_draft_id,
    )
    return {
        "ok": True,
        "operation_id": operation_id,
        "stage": "transaction",
        "ingested": True,
        "inserted": result.get("inserted"),
        "updated": result.get("updated"),
        "settled_draft": str(settled_draft_id) if settled_draft_id else None,
    }


@router.post("/tbank/payment-status")
async def tbank_payment_status(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _authorize_tbank_webhook(request, settings, authorization)

    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001 - any malformed body is a 400
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Некорректный JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ожидался объект JSON")

    # Банк по заявке шлёт на ЭТОТ URL операции по счёту (формат выписки). Если тело — операция
    # (есть operationId) → realtime-вливание в журнал ДДС; иначе это статус платёжного
    # документа → гашение черновика накладной/выплаты по provider_ref (ниже).
    if _extract(payload, ("operationId",)):
        return await _ingest_tbank_account_operation(session, payload)

    payment_id = _extract(payload, _ID_FIELDS)
    raw_status = _extract(payload, _STATUS_FIELDS)
    if not payment_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Нет идентификатора платежа")
    logger.info("tbank webhook принят: payment_id=%s status=%s", payment_id, raw_status)

    draft = await session.scalar(
        select(CounterpartyPaymentDraft).where(CounterpartyPaymentDraft.provider_ref == payment_id)
    )
    if draft is not None:
        new_status = await apply_payment_status(session, draft=draft, raw_status=raw_status)
        return {"ok": True, "matched": True, "draft_status": new_status}

    # Платёж не от накладной — возможно, это payroll-черновик (выплата администрации):
    # при «исполнен» заводим внутренний перевод банк→Сейф.
    payroll_draft = await session.scalar(
        select(PayrollBankDraft).where(PayrollBankDraft.provider_ref == payment_id)
    )
    if payroll_draft is not None:
        new_status = await apply_payroll_draft_status(
            session, draft=payroll_draft, raw_status=raw_status
        )
        return {"ok": True, "matched": True, "draft_status": new_status}

    # Неизвестный платёж — отвечаем 200, чтобы банк не ретраил (это не наша ошибка).
    return {"ok": True, "matched": False}


@router.post("/tbank/account-operation")
async def tbank_account_operation(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Вебхук T-Банка «Операция по счёту» — realtime-наполнение журнала ДДС из выписки.

    Первичный путь наполнения: операцию пишем сразу при получении, поллинг выписки
    остаётся сверкой (покрывает потерянные события и выходные). Дедуп по
    ``(provider, provider_operation_id=operationId)`` делает дубль-фаер идемпотентным
    (UPDATE, а не вторая вставка → баланс не задваивается). Авторизационные холды
    (``operationStatus=authorization``) подтверждаем 200, но в журнал/баланс НЕ
    пускаем — деньги считаем только по проведённой операции (``transaction``);
    финальное событие с тем же ``operationId`` или сверочный поллинг доберут её.
    Всегда отвечаем 2xx на корректное тело, чтобы банк не ретраил (лимит 5 попыток,
    дальше дропает — добор за поллингом). Тело — один объект-операция (не массив).
    """
    _authorize_tbank_webhook(request, settings, authorization)

    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001 - любой кривой body = 400
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Некорректный JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ожидался объект JSON")

    return await _ingest_tbank_account_operation(session, payload)


def _verify_bearer(authorization: str | None, expected: str | None) -> bool:
    """Bearer-токен вебхука iikoCloud. expected пуст (dev) → проверка отключена."""
    if not expected:
        return True
    scheme, _, token = (authorization or "").partition(" ")
    return scheme == "Bearer" and hmac.compare_digest(token, expected)


# Типы событий iikoCloud, которые точно НЕ про смены — в ingest не отдаём (если iiko шлёт
# все типы на один URL). Остальное пробуем как смену; ingest сам отсеет не-курьерские.
_NON_SHIFT_EVENT_TYPES = frozenset(
    {
        "DeliveryOrderUpdate",
        "DeliveryOrderError",
        "TableOrderUpdate",
        "TableOrderError",
        "ReserveUpdate",
        "ReserveError",
        "StopListUpdate",
        "StopListError",
    }
)


@router.post("/iiko/employee-shift")
async def iiko_employee_shift(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Вебхук iikoCloud об открытии/закрытии смены сотрудника — realtime для курьеров.

    Тело — массив событий (или один объект). События смен курьеров вливаем в
    ``courier_iiko_shift`` сразу (поллинг ``/employees/attendance`` публикует явку с
    задержкой), события других сотрудников/заказов игнорируем. После вливания пересчитываем
    матчинг затронутых курьеров — смена видна в приложении почти мгновенно. Всегда 200
    (потерянное добирает поллинг). Каждое событие логируем сырым — структуру PersonalShift
    калибруем по факту.
    """
    if not _verify_bearer(authorization, settings.iiko_webhook_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный токен вебхука")
    allowed = [
        ip.strip() for ip in (settings.iiko_webhook_allowed_ips or "").split(",") if ip.strip()
    ]
    if allowed and _client_ip(request) not in allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "IP не в списке разрешённых")

    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001 - кривой body = 400
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Некорректный JSON") from exc

    events = payload if isinstance(payload, list) else [payload]
    affected: set[uuid.UUID] = set()
    processed = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("eventType") or "")
        logger.info(
            "iiko employee-shift webhook: type=%s info=%s",
            event_type,
            event.get("eventInfo"),
        )
        if event_type in _NON_SHIFT_EVENT_TYPES:
            continue
        employee_id = await ingest_cloud_shift_event(session, event)
        if employee_id is not None:
            affected.add(employee_id)
            processed += 1

    if affected:
        now = datetime.now(_MOSCOW_TZ)
        await recalculate_matches(
            session,
            now.date() - timedelta(days=2),
            now.date() + timedelta(days=1),
            employee_ids=affected,
        )
        await session.commit()
    return {"ok": True, "processed": processed}

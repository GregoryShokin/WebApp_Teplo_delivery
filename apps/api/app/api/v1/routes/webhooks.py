"""Входящие вебхуки банка (T-Банк).

Статус платёжного документа (тело без ``operationId``) — единственный вход, который гасит
банковский черновик и создаёт ДДС по ``provider_ref``. События с ``operationId`` (операции по
счёту/строки выписки) в ДДС-ингест НЕ пропускаются: подтверждаем 200 и игнорируем, чтобы
выписка не создавала вторую проводку поверх оплаты, уже зафиксированной статусом черновика.
Оба входа (``/tbank/payment-status`` и алиас ``/tbank/account-operation``) авторизуются токеном
``tbank_webhook_token`` в заголовке ``Authorization`` (``Bearer <token>`` или «голым») +
опциональный IP-whitelist. Входящий контур (банк→мы), без JWT; заявка на ``openapi@tbank.ru``.
"""

from __future__ import annotations

import hmac
import logging
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.session import get_session
from app.models import CounterpartyPaymentDraft, PayrollBankDraft, SalaryAdvanceBankDraft
from app.services.bank_payment_status import apply_payment_status
from app.services.banking.base import clean_digits
from app.services.banking.tbank import is_tbank_operation_hold, normalize_tbank_statement_row
from app.services.couriers.cloud_shift_ingest import ingest_cloud_shift_event
from app.services.couriers.shift_matching import recalculate_matches
from app.services.payroll_advance_service import apply_advance_draft_status
from app.services.payroll_payouts import apply_payroll_draft_status

router = APIRouter()
logger = logging.getLogger(__name__)
_MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# Идентификатор статусного webhook-а. ``operationId``/``documentNumber`` намеренно НЕ здесь:
# их несёт операция по счёту (выписка), а её этот контур в ДДС не пускает.
_ID_FIELDS = (
    "paymentId",
    "documentId",
    "payment_id",
    "document_id",
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


def _is_account_operation_payload(payload: dict[str, Any]) -> bool:
    """Тело — операция по счёту (строка выписки), а не статус платёжного документа."""
    if _extract(payload, ("operationId",)):
        return True
    return any(
        field in payload
        for field in ("typeOfOperation", "operationAmount", "accountAmount", "rubleAmount")
    )


async def _ingest_tbank_account_operation(
    session: AsyncSession, payload: dict[str, Any]
) -> dict[str, Any]:
    """Влить операцию по счёту (строку выписки T-Банка) в журнал ``bank_operations``.

    Realtime-выписка приходит на тот же URL, что и статус черновика. Черновики этим путём
    НЕ гасим — их доводит статусный webhook по ``provider_ref``; здесь только пишем операцию
    в выписку (баланс банка + пикер карт-оплат Кассы) и прогоняем классификацию. Анти-дубль
    с оплатой черновика — общий prebooked-механизм классификатора (операция «забирает» уже
    заведённую статусом проводку, а не создаёт вторую; поздние prebooked добирает
    ``reconcile_needs_review_prebooked``). Холд (``authorization``) в баланс не пускаем.
    Дедуп по ``operationId`` + стабильному ключу выписки в ``ingest_operations`` делает
    повторную доставку идемпотентной.
    """
    operation_id = _extract(payload, ("operationId", "id"))
    if not operation_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Нет идентификатора операции")

    account_number = clean_digits(payload.get("accountNumber"))
    if not account_number:
        # Без номера счёта операция не привяжется к счёту (account_id=NULL) и выпадет из
        # баланса (JOIN по счёту). 422; сверочный поллинг подберёт её со счётом из контекста.
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
    result = await ingest_operations(session, provider="tbank", operations=[operation])
    await session.commit()
    logger.info(
        "tbank account-operation влит в выписку: op=%s dir=%s amount=%s ins=%s upd=%s",
        operation_id,
        operation.direction,
        operation.amount,
        result.get("inserted"),
        result.get("updated"),
    )
    return {
        "ok": True,
        "operation_id": operation_id,
        "stage": "transaction",
        "ingested": True,
        "inserted": result.get("inserted"),
        "updated": result.get("updated"),
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

    # Операции по счёту приходят на тот же URL (формат выписки) — вливаем в выписку (баланс
    # банка + пикер карт-оплат Кассы), но черновики отсюда НЕ гасим: их доводит статус
    # платёжного документа (по provider_ref, ниже). Анти-дубль — prebooked-механизм классификатора.
    if _is_account_operation_payload(payload):
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

    # Банк-выдача аванса/займа (Фаза 2): доводим по статусу платёжного документа так же, как
    # накладные и ЗП. Раньше advance доводился из операции по счёту, которую этот контур больше
    # не обрабатывает, поэтому статусный webhook — единственный путь довести его до paid.
    advance_draft = await session.scalar(
        select(SalaryAdvanceBankDraft).where(SalaryAdvanceBankDraft.provider_ref == payment_id)
    )
    if advance_draft is not None:
        new_status = await apply_advance_draft_status(
            session, draft=advance_draft, raw_status=raw_status
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
    """Вебхук T-Банка «Операция по счёту» (алиас; банк шлёт выписку и на /payment-status).

    Вливаем строку выписки в ``bank_operations`` (баланс банка + пикер карт-оплат Кассы),
    но черновики отсюда НЕ гасим — их доводит статус платёжного документа. Анти-дубль с
    оплатой черновика — prebooked-механизм классификатора. Тело — один объект-операция.
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


@router.post("/iiko")
async def iiko_webhook(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Единый приёмник вебхуков iikoCloud (один ``webHooksUri`` на организацию, один токен).

    iikoCloud шлёт ВСЕ подписанные события на один URL, поэтому здесь — диспетчер по
    ``eventType``:
      * события смен сотрудников → ``ingest_cloud_shift_event`` (realtime открытие/закрытие
        смены курьера; поллинг ``/employees/attendance`` публикует явку с задержкой);
      * заказы/прочее (``DeliveryOrderUpdate`` и т.п.) — пока только логируем; их обработчик
        (KDS) подключается сюда же, когда вольётся из ветки dds-webhook-ingestion.
    Тело — массив событий (или один объект). Bearer-токен ``iiko_webhook_token`` + опц.
    IP-whitelist. Всегда 200 (потерянное добирает поллинг). Каждое событие логируем сырым —
    структуры событий калибруем по факту.
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
    skipped = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("eventType") or "")
        # TODO(калибровка вебхуков): пока на WARNING, чтобы видеть сырьё в проде (app-логи
        # уровня INFO в api не настроены). Вернуть на logger.info после калибровки структур.
        logger.warning("iiko webhook: type=%s info=%s", event_type, event.get("eventInfo"))
        # Не-сменные типы (заказы и пр.) — пока не наша область, пропускаем (обработчики
        # добавятся в этот же диспетчер). Остальное пробуем как смену; ingest сам отсеет
        # не-курьерские/неполные события (вернёт None).
        if event_type in _NON_SHIFT_EVENT_TYPES:
            skipped += 1
            continue
        employee_id = await ingest_cloud_shift_event(session, event)
        if employee_id is not None:
            affected.add(employee_id)
            processed += 1
        else:
            skipped += 1

    if affected:
        now = datetime.now(_MOSCOW_TZ)
        await recalculate_matches(
            session,
            now.date() - timedelta(days=2),
            now.date() + timedelta(days=1),
            employee_ids=affected,
        )
        await session.commit()
    # TODO(калибровка): WARNING временно для наблюдения итога батча в проде.
    logger.warning(
        "iiko webhook итог: events=%d processed=%d skipped=%d", len(events), processed, skipped
    )
    return {"ok": True, "processed": processed, "skipped": skipped}

"""Входящие вебхуки банка (T-Банк «Статус платежа»).

Банк шлёт POST на ``/api/v1/webhooks/tbank/payment-status`` при смене статуса платежа,
созданного через API. Авторизация — токеном ``tbank_webhook_token`` в заголовке
``Authorization``; банк может прислать его как ``Bearer <token>``, так и «голым»
``<token>`` — принимаем оба варианта. Дополнительно — опциональный
IP-whitelist (6 IP банка). Сопоставление платежа с черновиком — по ``provider_ref`` (id
платежа у банка), затем авто-гашение накладных (та же логика, что у фонового добора статуса).
Это входящий контур (банк→мы), без JWT; подключается заявкой на ``openapi@tbank.ru``.
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.session import get_session
from app.models import CounterpartyPaymentDraft, PayrollBankDraft
from app.services.bank_payment_status import apply_payment_status
from app.services.payroll_payouts import apply_payroll_draft_status

router = APIRouter()
logger = logging.getLogger(__name__)

_ID_FIELDS = ("paymentId", "documentId", "id", "payment_id", "document_id")
_STATUS_FIELDS = ("status", "paymentStatus", "documentStatus", "payment_status")


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


@router.post("/tbank/payment-status")
async def tbank_payment_status(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
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

    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001 - any malformed body is a 400
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Некорректный JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ожидался объект JSON")

    # ВРЕМЕННАЯ ДИАГНОСТИКА (удалить после снятия структуры тела вебхука T-Банка):
    logger.warning(
        "tbank webhook DIAG keys=%s body=%s",
        list(payload.keys()),
        json.dumps(payload, ensure_ascii=False)[:3000],
    )

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

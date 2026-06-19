"""Проводка оплаты приходной накладной в iiko изъятием «Оплата поставок».

Когда накладная создаётся через Кассу с флагом «Оплачено», её оплата дублируется в
iiko финансовой проводкой-изъятием ``POST /v2/payInOuts/addPayOut`` со счёта «Главная
касса» (ТК Черникова) на корсчёт «Задолженность перед поставщиками» с привязкой к
конкретному поставщику (поле ``counteragent`` = iiko-GUID). Тип изъятия
``INVOICE_PAYMENT_PAYOUT_TYPE_ID`` уже инкапсулирует счёт-источник + корсчёт + статью ДДС.

NB: iiko проводит это как «ИС» (изъятие, transactionType PAYOUT), НЕ как «НОПЛ»
(INVOICE_PAYMENT) — payInOuts физически создаёт только PAYOUT/PAYIN. Финансово гасит
сальдо долга поставщика (баланс по поставщикам корректен), без привязки к конкретной
накладной — owner подтвердил, что этого достаточно (см. project_kassa_module_decisions).

Проводки iiko НЕОБРАТИМЫ; идемпотентность держим в ``supplier_invoice.raw_payload``
(``iiko_payment.status == 'posted'`` → повтор пропускаем). Сбой проводки НЕ валит
накладную (она уже создана и оплачена в ДДС): пишем ``failed`` + причину.

iiko-доступ — синхронные ``http.client``-хелперы из ``iiko_cashshift_sync`` (в обход
прокси/WAF). iiko Server периодически таймаутит на TLS — обработка лёгкая (1 проводка).
"""

from __future__ import annotations

import datetime
import logging
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models import CounterpartyAlias, SupplierInvoice
from app.services.kassa.cheque_payout_push import ADD_PAYOUT_PATH, CHERNIKOVA_DEPARTMENT_ID
from app.services.kassa.iiko_cashshift_sync import _auth_token, _iiko_post

logger = logging.getLogger(__name__)

IIKO_SOURCE = "iiko"

# Тип изъятия iiko «Приложение/Оплата поставок»: chiefAccount=Главная касса,
# корсчёт «Задолженность перед поставщиками», counteragentType=SUPPLIER. id одинаков
# на dev/prod (общий iiko-сервер). Подтверждён живым тестом 2026-06-19.
INVOICE_PAYMENT_PAYOUT_TYPE_ID = "713d0ef1-cf10-4924-b719-888198925ccd"


class InvoicePaymentPushError(RuntimeError):
    """Доменная ошибка проведения оплаты накладной в iiko."""


async def counterparty_iiko_guid(session: AsyncSession, counterparty_id: uuid.UUID) -> str | None:
    """iiko-GUID поставщика для проводки оплаты (первый alias source='iiko')."""
    return await session.scalar(
        select(CounterpartyAlias.alias)
        .where(
            CounterpartyAlias.counterparty_id == counterparty_id,
            CounterpartyAlias.source == IIKO_SOURCE,
        )
        .limit(1)
    )


def _add_invoice_payout(
    token: str, *, counteragent_guid: str, amount: Decimal, comment: str
) -> None:
    body = {
        "payOutTypeId": INVOICE_PAYMENT_PAYOUT_TYPE_ID,
        "payOutDate": datetime.date.today().isoformat(),
        "counteragent": counteragent_guid,
        "departmentSumMap": {CHERNIKOVA_DEPARTMENT_ID: float(amount)},
        "comment": comment,
    }
    data = _iiko_post(token, ADD_PAYOUT_PATH, body)
    result = data.get("result") if isinstance(data, dict) else None
    if result != "SUCCESS":
        raise InvoicePaymentPushError(f"addPayOut вернул не SUCCESS: {data}")


async def post_invoice_payment_to_iiko(
    session: AsyncSession, invoice: SupplierInvoice, *, amount: Decimal
) -> None:
    """Провести оплату накладной в iiko (изъятие из Главной кассы на долг поставщика).

    Идемпотентно по ``raw_payload['iiko_payment']``. НЕ коммитит — вызывающий решает.
    Сбой фиксируется в ``raw_payload`` (status=failed), исключение наружу не пробрасывается:
    оплата в iiko побочна, накладная уже создана и оплачена в ДДС.
    """
    raw = dict(invoice.raw_payload or {})
    tracked = raw.get("iiko_payment")
    if isinstance(tracked, dict) and tracked.get("status") == "posted":
        return  # уже проведено — не задваиваем

    guid = await counterparty_iiko_guid(session, invoice.counterparty_id)
    comment = f"Оплата накладной №{invoice.number or invoice.id}"
    entry: dict = {
        "amount": str(amount),
        "counteragent": guid,
        "type_id": INVOICE_PAYMENT_PAYOUT_TYPE_ID,
    }
    if not guid:
        entry.update(status="failed", error="Нет iiko-GUID контрагента")
    else:
        try:
            token = _auth_token()
            _add_invoice_payout(token, counteragent_guid=guid, amount=amount, comment=comment)
            entry["status"] = "posted"
        except Exception as exc:  # noqa: BLE001 — iiko-проводка побочна, накладную не валим
            entry.update(status="failed", error=str(exc)[:500])
            logger.warning("Накладная %s: оплата в iiko не прошла: %s", invoice.id, exc)

    raw["iiko_payment"] = entry
    invoice.raw_payload = raw
    flag_modified(invoice, "raw_payload")

"""iiko Cloud API для ВОЗВРАТНОЙ накладной (``returned_invoice``).

Отражает коррекцию уже ОПЛАЧЕННОЙ приходной накладной: оплаченный оригинал iiko править/удалять
не даёт (``unpost`` проведённой с оплатой → 500, ``add_payment`` необратим — проверено на живом
API), поэтому лишние/не те товары возвращаем ОТДЕЛЬНЫМ документом со ссылкой на оригинал
(``incomingInvoiceId``). В iiko это списывает товары со склада и создаёт долг поставщика
(дебиторку), зеркалящую нашу ``SupplierPrepayment``. Контракт проверен на живом API 10.07:
create со ссылкой → post → get подтверждает связь; ``expenseAccount`` iiko проставляет сам;
документ НЕ заморожен (нет оплаты) → свободно ``unpost``→``cancel``. См.
project_edit_paid_invoice_feasibility.

Здесь — только тонкие сетевые обёртки и билдер тела; оркестрация с БД живёт в
:mod:`warehouse_invoice_push`.
"""

from __future__ import annotations

import urllib.request
from datetime import datetime

from app.services.iiko_cloud_client import iiko_cloud_call
from app.services.iiko_invoice_cloud import format_iiko_invoice_datetime
from app.services.iiko_location import get_organization_id

_RETURNED = "/api/inventory/v1/returned_invoice"


def build_returned_invoice_body(
    *,
    counteragent: str,
    date: datetime,
    items: list[dict],
    incoming_invoice_id: str | None = None,
    default_store: str | None = None,
    number: str | None = None,
    comment: str | None = None,
    organization_id: str | None = None,
) -> dict:
    """JSON-тело ``returned_invoice/create``. ``items`` — позиции возврата
    ({num, product, store, amount, price[, sum]}). Дата — ISO с ТОЧКОЙ (как у накладных).
    ``expenseAccount`` не шлём: iiko проставляет счёт расхода сам."""
    organization_id = organization_id or get_organization_id()
    body: dict = {
        "organizationId": organization_id,
        "counteragent": counteragent,
        "date": format_iiko_invoice_datetime(date),
        "items": items,
    }
    if incoming_invoice_id is not None:
        body["incomingInvoiceId"] = incoming_invoice_id
    if default_store is not None:
        body["defaultStore"] = default_store
    if number is not None:
        body["number"] = number
    if comment is not None:
        body["comment"] = comment
    return body


def _ref(organization_id: str, document_id: str) -> dict:
    return {"organizationId": organization_id, "documentId": document_id}


def create_returned_invoice(
    body: dict, *, token: str | None = None, opener: urllib.request.OpenerDirector | None = None
) -> tuple[int, dict | list]:
    return iiko_cloud_call(_RETURNED + "/create", body, token=token, opener=opener)


def post_returned_invoice(
    organization_id: str,
    document_id: str,
    *,
    token: str | None = None,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[int, dict | list]:
    return iiko_cloud_call(
        _RETURNED + "/post", _ref(organization_id, document_id), token=token, opener=opener
    )


def unpost_returned_invoice(
    organization_id: str,
    document_id: str,
    *,
    token: str | None = None,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[int, dict | list]:
    return iiko_cloud_call(
        _RETURNED + "/unpost", _ref(organization_id, document_id), token=token, opener=opener
    )


def cancel_returned_invoice(
    organization_id: str,
    document_id: str,
    *,
    token: str | None = None,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[int, dict | list]:
    return iiko_cloud_call(
        _RETURNED + "/cancel", _ref(organization_id, document_id), token=token, opener=opener
    )


def get_returned_invoice(
    organization_id: str,
    document_id: str,
    *,
    token: str | None = None,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[int, dict | list]:
    return iiko_cloud_call(
        _RETURNED + "/get", _ref(organization_id, document_id), token=token, opener=opener
    )

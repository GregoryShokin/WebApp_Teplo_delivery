"""iiko Cloud API для накладных: приходные (incoming_invoice) и расходные (outgoing_invoice).

Заменяет легаси RMS-XML-импорт (``/documents/import/incomingInvoice``). Здесь — ЧИСТЫЕ билдеры тел
(без сети, unit-тестируемые) и тонкие сетевые операции поверх :mod:`iiko_cloud_client`. Оркестрация
c БД (резолвинг строк, гейт идемпотентности, статусы) живёт в :mod:`warehouse_invoice_push`.

Контракт (проверен на живом API 09.07, спека ``/api-docs/docs``):
- эндпоинты ``.../{incoming,outgoing}_invoice/{get,create,update,post,unpost,cancel,list}``;
- даты ISO-8601 С ТОЧКОЙ (2026-06-25T16:36:00.000+03:00), НЕ запятая (не как в add_payment);
- read-only поля (``status, productArticle, priceWithoutVat, sumWithoutVat, producer``) НЕ слать —
  ``create``/``update`` отвергают их 400; статусом рулят ``post``/``unpost``/``cancel``;
- ``create`` возвращает ``documentId`` в теле ответа (легаси export-lookup больше не нужен);
- ``create``/``update`` НЕ идемпотентны — идемпотентность на нашей стороне (гейт по external_id);
- ``update`` проведённой накладной АВТО-распроводит её в ``NEW`` и НЕ проводит обратно — после
  правки нужен явный ``post``; распроводка блокируется, если есть проводки оплаты (HTTP 500 с
  бизнес-``message``).
"""

from __future__ import annotations

import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from app.services.iiko_cloud_client import IIKO_ORGANIZATION_ID, iiko_cloud_call

_MSK = ZoneInfo("Europe/Moscow")
ZERO_GUID = "00000000-0000-0000-0000-000000000000"

# Направления накладной (SupplierInvoice.direction) → Cloud-ресурс.
_PAYABLE = "payable"       # приходная (нам поставили) → incoming_invoice
_RECEIVABLE = "receivable"  # расходная (мы отгрузили) → outgoing_invoice


def endpoint(direction: str, op: str) -> str:
    """``('payable','create') -> /api/inventory/v1/incoming_invoice/create``."""
    kind = "incoming_invoice" if direction == _PAYABLE else "outgoing_invoice"
    return f"/api/inventory/v1/{kind}/{op}"


def format_iiko_invoice_datetime(dt: datetime) -> str:
    """ISO-8601 с ТОЧКОЙ перед мс и offset — формат, который принимают эндпоинты накладных.

    Пример: ``2026-06-25T16:36:00.000+03:00``. Naive-время трактуем как МСК.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_MSK)
    base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    millis = f"{dt.microsecond // 1000:03d}"
    offset = dt.strftime("%z")  # +0300
    offset = f"{offset[:3]}:{offset[3:]}" if offset else "+03:00"
    return f"{base}.{millis}{offset}"


@dataclass
class CloudInvoiceLine:
    """Позиция накладной для Cloud-тела. Только поля, принимаемые ``create``/``update`` (read-only
    поля ответа ``get`` сюда не входят). ``actual_amount`` по умолчанию = ``amount``."""

    num: int
    product: str            # GUID номенклатуры
    store: str              # GUID склада
    amount: float
    price: float | None = None
    sum: float | None = None
    amount_unit: str | None = None   # GUID единицы измерения
    vat_percent: float | None = None
    actual_amount: float | None = None
    container_id: str = ZERO_GUID
    is_additional_expense: bool = False
    # только для расходной (outgoing):
    discount_sum: float | None = None
    product_size: str | None = None

    def to_incoming(self) -> dict:
        item: dict = {
            "num": self.num,
            "product": self.product,
            "store": self.store,
            "amount": self.amount,
            "actualAmount": self.actual_amount if self.actual_amount is not None else self.amount,
            "containerId": self.container_id,
            "isAdditionalExpense": self.is_additional_expense,
        }
        if self.amount_unit is not None:
            item["amountUnit"] = self.amount_unit
        if self.price is not None:
            item["price"] = self.price
        if self.sum is not None:
            item["sum"] = self.sum
        if self.vat_percent is not None:
            item["vatPercent"] = self.vat_percent
        return item

    def to_outgoing(self) -> dict:
        item: dict = {
            "num": self.num,
            "product": self.product,
            "store": self.store,
            "amount": self.amount,
            "containerId": self.container_id,
        }
        if self.amount_unit is not None:
            item["amountUnit"] = self.amount_unit
        if self.price is not None:
            item["price"] = self.price
        if self.sum is not None:
            item["sum"] = self.sum
        if self.vat_percent is not None:
            item["vatPercent"] = self.vat_percent
        if self.discount_sum is not None:
            item["discountSum"] = self.discount_sum
        if self.product_size is not None:
            item["productSize"] = self.product_size
        return item


@dataclass
class CloudInvoiceDoc:
    """Доменно-нейтральное описание накладной для сборки Cloud-тела."""

    direction: str                       # payable | receivable
    counteragent: str                    # GUID контрагента
    date: datetime                       # дата/время документа
    lines: list[CloudInvoiceLine]
    number: str | None = None            # documentNumber
    default_store: str | None = None
    incoming_date: datetime | None = None  # только incoming (дата поступления)
    document_id: str | None = None       # обязателен для update
    organization_id: str = IIKO_ORGANIZATION_ID
    extra: dict = field(default_factory=dict)  # необязательные поля (dueDate, comment, ...)


def build_invoice_body(doc: CloudInvoiceDoc) -> dict:
    """Собрать JSON-тело для ``create``/``update`` по направлению. Read-only поля не эмитятся."""
    body: dict = {
        "organizationId": doc.organization_id,
        "counteragent": doc.counteragent,
        "date": format_iiko_invoice_datetime(doc.date),
    }
    if doc.document_id is not None:
        body["documentId"] = doc.document_id
    if doc.number is not None:
        body["number"] = doc.number
    if doc.default_store is not None:
        body["defaultStore"] = doc.default_store
    if doc.direction == _PAYABLE:
        if doc.incoming_date is not None:
            body["incomingDate"] = format_iiko_invoice_datetime(doc.incoming_date)
        body["items"] = [line.to_incoming() for line in doc.lines]
    else:
        body["items"] = [line.to_outgoing() for line in doc.lines]
    body.update(doc.extra)
    return body


# ── Сетевые операции (тонкие обёртки; не бросают — см. iiko_cloud_call) ──────────────────────────


def _ref(organization_id: str, document_id: str) -> dict:
    return {"organizationId": organization_id, "documentId": document_id}


def get_invoice(
    direction: str,
    organization_id: str,
    document_id: str,
    *,
    token: str | None = None,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[int, dict | list]:
    return iiko_cloud_call(endpoint(direction, "get"), _ref(organization_id, document_id),
                           token=token, opener=opener)


def list_invoices(
    direction: str,
    organization_id: str,
    *,
    date_from: str,
    date_to: str,
    token: str | None = None,
    opener: urllib.request.OpenerDirector | None = None,
) -> tuple[int, dict | list]:
    """Сводки документов за период (``from``/``to`` — простые даты ``YYYY-MM-DD``).

    ВАЖНО: вопреки спеке, ``list`` возвращает НЕ полные документы, а сводки
    (``documentId, date, number, processed, deleted, sum, counteragent, ...``) БЕЗ ``items`` и
    строкового ``status`` (проверено на живом API 09.07). За полным документом — ``get``.
    """
    return iiko_cloud_call(
        endpoint(direction, "list"),
        {"organizationId": organization_id, "from": date_from, "to": date_to},
        token=token,
        opener=opener,
    )


def create_invoice(
    direction: str, body: dict, *, token: str | None = None, opener=None
) -> tuple[int, dict | list]:
    return iiko_cloud_call(endpoint(direction, "create"), body, token=token, opener=opener)


def update_invoice(
    direction: str, body: dict, *, token: str | None = None, opener=None
) -> tuple[int, dict | list]:
    return iiko_cloud_call(endpoint(direction, "update"), body, token=token, opener=opener)


def post_invoice(
    direction: str, organization_id: str, document_id: str, *, token: str | None = None, opener=None
) -> tuple[int, dict | list]:
    return iiko_cloud_call(endpoint(direction, "post"), _ref(organization_id, document_id),
                           token=token, opener=opener)


def unpost_invoice(
    direction: str, organization_id: str, document_id: str, *, token: str | None = None, opener=None
) -> tuple[int, dict | list]:
    return iiko_cloud_call(endpoint(direction, "unpost"), _ref(organization_id, document_id),
                           token=token, opener=opener)


def cancel_invoice(
    direction: str, organization_id: str, document_id: str, *, token: str | None = None, opener=None
) -> tuple[int, dict | list]:
    return iiko_cloud_call(endpoint(direction, "cancel"), _ref(organization_id, document_id),
                           token=token, opener=opener)


def extract_document_id(response: dict | list | None) -> str | None:
    """``documentId`` из ответа ``create``/``get`` (create кладёт его прямо в тело)."""
    if isinstance(response, dict):
        return response.get("documentId") or response.get("id")
    return None


def extract_document_status(response: dict | list | None) -> str | None:
    """``status`` документа из ответа ``get`` (``NEW`` / ``PROCESSED`` / ...). Тело ПЛОСКОЕ —
    поля документа лежат на верхнем уровне (проверено на живом API 22.07)."""
    if isinstance(response, dict):
        status = response.get("status")
        return str(status).strip() if status else None
    return None


def business_error_message(response: dict | list | None) -> str | None:
    """Текст бизнес-ошибки iiko. Cloud отдаёт бизнес-отказ КОДОМ 500/409 с ``message`` —
    парсить его, а не полагаться на HTTP-статус."""
    if isinstance(response, dict):
        msg = response.get("message") or response.get("error") or response.get("errorMessage")
        return str(msg).strip() if msg else None
    return None

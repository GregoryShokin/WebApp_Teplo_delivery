"""Unit-тесты чистых билдеров Cloud-накладных (без сети/БД)."""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.services.iiko_invoice_cloud import (
    ZERO_GUID,
    CloudInvoiceDoc,
    CloudInvoiceLine,
    build_invoice_body,
    business_error_message,
    endpoint,
    extract_document_id,
    format_iiko_invoice_datetime,
)

MSK = ZoneInfo("Europe/Moscow")

# Поля ответа get, которые create/update ОТВЕРГАЮТ (400) — не должны попадать в тело.
READ_ONLY_FIELDS = {"status", "productArticle", "priceWithoutVat", "sumWithoutVat", "producer"}


def test_datetime_compensates_cloud_shift_and_keeps_msk_offset() -> None:
    dt = datetime(2026, 6, 25, 16, 36, 0, tzinfo=MSK)
    # iiko Cloud вычитает +03:00 из самих цифр. 19:36+03 после его обработки станет
    # требуемыми 16:36+03 в бэк-офисе.
    assert format_iiko_invoice_datetime(dt) == "2026-06-25T19:36:00.000+03:00"


def test_datetime_naive_treated_as_msk() -> None:
    # Наивное время трактуется как МСК; компенсация сохраняет московский offset.
    assert format_iiko_invoice_datetime(datetime(2026, 7, 9, 0, 0, 0)) == (
        "2026-07-09T03:00:00.000+03:00"
    )


def test_endpoint_mapping() -> None:
    assert endpoint("payable", "create") == "/api/inventory/v1/incoming_invoice/create"
    assert endpoint("payable", "post") == "/api/inventory/v1/incoming_invoice/post"
    assert endpoint("receivable", "update") == "/api/inventory/v1/outgoing_invoice/update"
    assert endpoint("receivable", "unpost") == "/api/inventory/v1/outgoing_invoice/unpost"


def _incoming_doc(**over) -> CloudInvoiceDoc:
    base = dict(
        direction="payable",
        counteragent="7a867d7d-75c8-446a-83b2-04591efe7def",
        date=datetime(2026, 6, 25, 16, 36, tzinfo=MSK),
        incoming_date=datetime(2026, 6, 25, 0, 0, tzinfo=MSK),
        default_store="8a55c561-2598-4150-ac28-1b03cf8a1835",
        number="4-2",
        lines=[
            CloudInvoiceLine(
                num=1,
                product="0eef42b7-61d9-4d57-a1de-b8c5016b248e",
                store="8a55c561-2598-4150-ac28-1b03cf8a1835",
                amount=100,
                amount_unit="7ba81c3a-8de5-8f9d-fb9f-e39efcbc57cc",
                price=41.23,
                sum=4122.5,
                vat_percent=0,
            )
        ],
    )
    base.update(over)
    return CloudInvoiceDoc(**base)


def test_incoming_body_shape_and_dot_dates() -> None:
    body = build_invoice_body(_incoming_doc())
    assert body["organizationId"]
    assert body["counteragent"] == "7a867d7d-75c8-446a-83b2-04591efe7def"
    assert body["date"] == "2026-06-25T19:36:00.000+03:00"
    assert body["incomingDate"] == "2026-06-25T03:00:00.000+03:00"
    assert body["defaultStore"] == "8a55c561-2598-4150-ac28-1b03cf8a1835"
    assert body["number"] == "4-2"
    assert "documentId" not in body  # для create documentId не задаём

    item = body["items"][0]
    assert item["num"] == 1
    assert item["product"] == "0eef42b7-61d9-4d57-a1de-b8c5016b248e"
    assert item["store"] == "8a55c561-2598-4150-ac28-1b03cf8a1835"
    assert item["amount"] == 100
    assert item["actualAmount"] == 100  # дефолт = amount
    assert item["containerId"] == ZERO_GUID
    assert item["amountUnit"] == "7ba81c3a-8de5-8f9d-fb9f-e39efcbc57cc"
    assert item["price"] == 41.23
    assert item["sum"] == 4122.5
    assert item["vatPercent"] == 0
    assert item["isAdditionalExpense"] is False


def test_no_read_only_fields_are_emitted() -> None:
    body = build_invoice_body(_incoming_doc())
    assert READ_ONLY_FIELDS.isdisjoint(body.keys())
    assert READ_ONLY_FIELDS.isdisjoint(body["items"][0].keys())


def test_update_sets_document_id() -> None:
    body = build_invoice_body(_incoming_doc(document_id="79723842-8bd3-c540-019f-037eebe54b6e"))
    assert body["documentId"] == "79723842-8bd3-c540-019f-037eebe54b6e"


def test_actual_amount_override_kept() -> None:
    doc = _incoming_doc(
        lines=[CloudInvoiceLine(num=1, product="p", store="s", amount=10, actual_amount=9)]
    )
    item = build_invoice_body(doc)["items"][0]
    assert item["amount"] == 10
    assert item["actualAmount"] == 9


def test_optional_line_fields_omitted_when_none() -> None:
    doc = _incoming_doc(lines=[CloudInvoiceLine(num=1, product="p", store="s", amount=5)])
    item = build_invoice_body(doc)["items"][0]
    for absent in ("amountUnit", "price", "sum", "vatPercent"):
        assert absent not in item
    # обязательные всё равно на месте
    assert item["amount"] == 5 and item["containerId"] == ZERO_GUID


def test_outgoing_body_uses_outgoing_item_shape() -> None:
    doc = CloudInvoiceDoc(
        direction="receivable",
        counteragent="cptr",
        date=datetime(2026, 7, 1, 12, 0, tzinfo=MSK),
        default_store="store",
        number="R-1",
        lines=[
            CloudInvoiceLine(
                num=1, product="p", store="store", amount=3, price=100, sum=300,
                vat_percent=0, discount_sum=10, product_size="size1",
            )
        ],
    )
    body = build_invoice_body(doc)
    assert "incomingDate" not in body  # только у incoming
    item = body["items"][0]
    # outgoing-специфика присутствует…
    assert item["discountSum"] == 10
    assert item["productSize"] == "size1"
    # …а incoming-специфики нет
    assert "actualAmount" not in item
    assert "isAdditionalExpense" not in item


def test_extract_document_id_and_business_error() -> None:
    assert extract_document_id({"documentId": "abc"}) == "abc"
    assert extract_document_id({"id": "xyz"}) == "xyz"
    assert extract_document_id(None) is None
    assert business_error_message(
        {"message": "Нельзя распровести документ т.к. по документу уже есть проводки оплаты"}
    ).startswith("Нельзя распровести")
    assert business_error_message({"ok": True}) is None

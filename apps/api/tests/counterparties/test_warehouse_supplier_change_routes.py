"""HTTP-гейты смены поставщика при правке накладной (PUT /warehouse/invoices/{id}).

Через FastAPI-приложение: (1) уже выгруженную в iiko накладную нельзя перевести на контрагента без
iiko-GUID (409 — иначе документ повис бы на старом поставщике); (2) ещё-не-выгруженную можно
свободно (200). Ядро смены (сервис + iiko-тело) — в test_warehouse_supplier_change.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime

from cp_helpers import admin_headers, make_counterparty, make_iiko_product, make_invoice
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import InvoiceLineItem

BASE = "/api/v1/warehouse"
ISSUED = datetime(2026, 6, 12, 14, 30, tzinfo=UTC)
SAME_DAY = date(2026, 6, 12)


def _run(coro):
    return asyncio.run(coro)


def _admin(factory) -> dict[str, str]:
    return _run(admin_headers(factory))


async def _seed(
    factory: async_sessionmaker[AsyncSession], *, external_id: str | None, new_has_guid: bool
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    # Route-тесты коммитят в общую БД без отката — ИНН/alias уникализируем, иначе второй тест
    # упрётся в uq_counterparty_inn_not_null / уникальность alias.
    tag = uuid.uuid4().hex[:8]
    async with factory() as session:
        old = await make_counterparty(
            session, name="Старый", inn=f"77{uuid.uuid4().int % 10**9:09d}", iiko_guid=f"SUP-OLD-{tag}"
        )
        new = await make_counterparty(
            session,
            name="Новый",
            inn=f"77{uuid.uuid4().int % 10**9:09d}",
            iiko_guid=f"SUP-NEW-{tag}" if new_has_guid else None,
        )
        product = await make_iiko_product(session, name="Лосось")
        invoice = await make_invoice(
            session,
            counterparty_id=old.id,
            amount="1000.00",
            number="W-70",
            external_id=external_id,
            issued_at=ISSUED,
            invoice_date=SAME_DAY,
        )
        session.add(
            InvoiceLineItem(
                invoice_id=invoice.id,
                iiko_product_id=product.id,
                product_guid=product.iiko_id,
                name="Лосось",
                quantity=2,
                price=500,
                sum=1000,
                is_staff=False,
                sort_order=0,
            )
        )
        await session.commit()
        return invoice.id, new.id, product.id


def _payload(counterparty_id: uuid.UUID, product_id: uuid.UUID) -> dict:
    return {
        "counterparty_id": str(counterparty_id),
        "lines": [
            {
                "name": "Лосось",
                "quantity": 2,
                "price": 500,
                "iiko_product_id": str(product_id),
                "sum": 1000,
                "is_staff": False,
            }
        ],
    }


def test_change_supplier_without_iiko_guid_conflicts_when_pushed(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    invoice_id, new_id, product_id = _run(
        _seed(async_session_factory, external_id="IIKO-DOC-X", new_has_guid=False)
    )
    resp = client.put(
        f"{BASE}/invoices/{invoice_id}",
        json=_payload(new_id, product_id),
        headers=_admin(async_session_factory),
    )
    assert resp.status_code == 409, resp.text
    assert "iiko" in resp.json()["detail"].lower()


def test_change_supplier_ok_when_not_yet_in_iiko(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    invoice_id, new_id, product_id = _run(
        _seed(async_session_factory, external_id=None, new_has_guid=True)
    )
    resp = client.put(
        f"{BASE}/invoices/{invoice_id}",
        json=_payload(new_id, product_id),
        headers=_admin(async_session_factory),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["counterparty_id"] == str(new_id)

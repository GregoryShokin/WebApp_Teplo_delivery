"""Смена поставщика при редактировании неоплаченной складской накладной.

Проверяем ядро: ``update_warehouse_invoice`` меняет ``counterparty_id``, отвергает несуществующего
контрагента, а после смены зеркало в iiko (``prepare_push``) несёт GUID НОВОГО поставщика. Гейт
роута «у нового контрагента нет iiko-GUID для уже выгруженной накладной» — в test_warehouse_routes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from cp_helpers import make_counterparty, make_iiko_product
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.warehouse_invoice_push import prepare_push
from app.services.warehouse_invoices import (
    LineInput,
    WarehouseInvoiceError,
    create_warehouse_invoice,
    update_warehouse_invoice,
)

ISSUED = datetime(2026, 6, 15, 14, 30, tzinfo=UTC)


async def _goods_invoice(session: AsyncSession, counterparty_id: uuid.UUID):
    """Неоплаченная накладная с одной товарной строкой (сматченной с номенклатурой iiko).
    Возвращает ``(invoice, product_id)`` — ``product_id`` нужен, чтобы повторно передать ту же
    товарную строку в ``update_warehouse_invoice`` (иначе номенклатурный гард отвергнет)."""
    product = await make_iiko_product(session, name="Лосось")
    invoice = await create_warehouse_invoice(
        session,
        counterparty_id=counterparty_id,
        issued_at=ISSUED,
        store_guid="ST-1",
        lines=[LineInput(name="Лосось", quantity=2, price=500, iiko_product_id=product.id, sum=1000)],
    )
    return invoice, product.id


def _goods_line(product_id: uuid.UUID) -> LineInput:
    return LineInput(name="Лосось", quantity=2, price=500, iiko_product_id=product_id, sum=1000)


async def test_update_changes_supplier(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        old = await make_counterparty(session, name="Старый", inn="7720000010", iiko_guid="SUP-OLD")
        new = await make_counterparty(session, name="Новый", inn="7720000011", iiko_guid="SUP-NEW")
        await session.commit()
        invoice, product_id = await _goods_invoice(session, old.id)

        updated = await update_warehouse_invoice(
            session, invoice, lines=[_goods_line(product_id)], counterparty_id=new.id
        )
        assert updated.counterparty_id == new.id


async def test_update_rejects_unknown_supplier(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        old = await make_counterparty(session, name="Старый", inn="7720000020", iiko_guid="SUP-OLD")
        await session.commit()
        invoice, product_id = await _goods_invoice(session, old.id)

        with pytest.raises(WarehouseInvoiceError, match="Контрагент не найден"):
            await update_warehouse_invoice(
                session, invoice, lines=[_goods_line(product_id)], counterparty_id=uuid.uuid4()
            )


async def test_update_none_keeps_supplier(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        old = await make_counterparty(session, name="Старый", inn="7720000030", iiko_guid="SUP-OLD")
        await session.commit()
        invoice, product_id = await _goods_invoice(session, old.id)

        updated = await update_warehouse_invoice(
            session, invoice, lines=[_goods_line(product_id)], counterparty_id=None
        )
        assert updated.counterparty_id == old.id


async def test_changed_supplier_flows_new_counteragent_to_iiko(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """После смены поставщика тело для iiko (``prepare_push``) несёт GUID нового контрагента —
    именно он уйдёт в ``counteragent`` при Cloud update→post."""
    async with async_session_factory() as session:
        old = await make_counterparty(session, name="Старый", inn="7720000040", iiko_guid="SUP-OLD")
        new = await make_counterparty(session, name="Новый", inn="7720000041", iiko_guid="SUP-NEW")
        await session.commit()
        invoice, product_id = await _goods_invoice(session, old.id)

        before = await prepare_push(session, invoice)
        assert before.doc is not None and before.doc.counteragent == "SUP-OLD"

        await update_warehouse_invoice(
            session, invoice, lines=[_goods_line(product_id)], counterparty_id=new.id
        )

        after = await prepare_push(session, invoice)
        assert after.doc is not None
        assert after.doc.counteragent == "SUP-NEW"
        assert [line.product for line in after.doc.lines]  # товарная строка сохранена

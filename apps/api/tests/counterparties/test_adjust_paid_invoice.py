"""Правка УЖЕ ОПЛАЧЕННОЙ накладной (invoices.normal.edit_paid): излишек оплаты → дебиторка.

Сценарий: поставщик прислал не ту накладную, её провели и оплатили; теперь исправляем сумму
вниз, а излишек не пропадает и не сторнирует деньги — уходит в SupplierPrepayment («поставщик
нам должен»). iiko-документ сервис не трогает (это отдельный шаг), поэтому тесты — чисто про
нашу сторону.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from cp_helpers import (
    admin_headers,
    allocated_total,
    headers_for,
    make_counterparty,
    make_iiko_product,
    make_invoice,
)
from sqlalchemy import select

import app.services.warehouse_invoice_push as wip
from app.models import (
    InvoiceLineItem,
    InvoicePaymentAllocation,
    SupplierInvoice,
    SupplierInvoiceTombstone,
    SupplierPrepayment,
)
from app.services.supplier_prepayments import counterparty_prepayment_balance
from app.services.warehouse_invoice_push import _CloudPushOutcome, book_correction_in_iiko
from app.services.warehouse_invoices import (
    LineInput,
    WarehouseInvoiceError,
    adjust_paid_invoice,
)

pytestmark = pytest.mark.usefixtures("migrated_db")


def _line(product_id, price, qty="1"):
    return LineInput(name="Мешок", quantity=Decimal(qty), price=Decimal(price), iiko_product_id=product_id)


async def test_overpayment_spills_to_prepayment(async_session_factory):
    """Оплаченная 100 → исправлена на 60: аллокации 60, дебиторка 40 (реальные деньги)."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик Х", inn="7700000001")
        product = await make_iiko_product(session, name="Мешок")
        invoice = await make_invoice(
            session, counterparty_id=cp.id, amount="100.00", payment_status="paid", number="500"
        )
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id, source_kind="cash", amount=Decimal("100.00")
            )
        )
        await session.commit()

        await adjust_paid_invoice(session, invoice, lines=[_line(product.id, "60")])
        await session.refresh(invoice)

        assert invoice.amount == Decimal("60.00")
        assert invoice.payment_status == "paid"
        assert await allocated_total(session, invoice.id) == Decimal("60.00")

        prepayments = (
            await session.scalars(
                select(SupplierPrepayment).where(SupplierPrepayment.counterparty_id == cp.id)
            )
        ).all()
        assert len(prepayments) == 1
        assert prepayments[0].amount == Decimal("40.00")
        assert prepayments[0].amount_settled == Decimal("0.00")
        assert prepayments[0].status == "open"
        # Новой денежной проводки не создаём — деньги уже ушли при оплате.
        assert prepayments[0].cashflow_transaction_id is None
        assert await counterparty_prepayment_balance(session, cp.id) == Decimal("40.00")


async def test_exact_correction_makes_no_prepayment(async_session_factory):
    """Сумма не изменилась (только состав позиций) → дебиторки нет, статус остаётся paid."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик Y")
        product = await make_iiko_product(session, name="Мешок")
        invoice = await make_invoice(
            session, counterparty_id=cp.id, amount="100.00", payment_status="paid", number="501"
        )
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id, source_kind="cash", amount=Decimal("100.00")
            )
        )
        await session.commit()

        await adjust_paid_invoice(session, invoice, lines=[_line(product.id, "100")])
        await session.refresh(invoice)

        assert invoice.amount == Decimal("100.00")
        assert invoice.payment_status == "paid"
        assert await allocated_total(session, invoice.id) == Decimal("100.00")
        prepayments = (
            await session.scalars(
                select(SupplierPrepayment).where(SupplierPrepayment.counterparty_id == cp.id)
            )
        ).all()
        assert prepayments == []


async def test_overpayment_from_existing_prepayment_returns_to_it(async_session_factory):
    """Если оплата шла из предоплаты — излишек ВОЗВРАЩАЕМ в неё, а не плодим новую дебиторку."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик Z")
        product = await make_iiko_product(session, name="Мешок")
        prepayment = SupplierPrepayment(
            counterparty_id=cp.id, kind="goods", amount=Decimal("100.00"),
            amount_settled=Decimal("100.00"), status="settled",
        )
        session.add(prepayment)
        invoice = await make_invoice(
            session, counterparty_id=cp.id, amount="100.00", payment_status="paid", number="502"
        )
        await session.flush()
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id, source_kind="prepayment",
                prepayment_id=prepayment.id, amount=Decimal("100.00"),
            )
        )
        await session.commit()

        await adjust_paid_invoice(session, invoice, lines=[_line(product.id, "60")])
        await session.refresh(invoice)
        await session.refresh(prepayment)

        assert invoice.amount == Decimal("60.00")
        assert await allocated_total(session, invoice.id) == Decimal("60.00")
        # Новой предоплаты не появилось — вернули в исходную.
        prepayments = (
            await session.scalars(
                select(SupplierPrepayment).where(SupplierPrepayment.counterparty_id == cp.id)
            )
        ).all()
        assert len(prepayments) == 1
        assert prepayment.amount_settled == Decimal("60.00")
        assert prepayment.status == "partially_settled"


async def test_gates_reject_unpaid_and_barter(async_session_factory):
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик G")
        product = await make_iiko_product(session, name="Мешок")

        unpaid = await make_invoice(
            session, counterparty_id=cp.id, amount="100.00", payment_status="unpaid", number="503"
        )
        await session.commit()
        with pytest.raises(WarehouseInvoiceError, match="ОПЛАЧЕННОЙ"):
            await adjust_paid_invoice(session, unpaid, lines=[_line(product.id, "60")])

        barter = await make_invoice(
            session, counterparty_id=cp.id, amount="100.00", payment_status="paid",
            number="504", barter_role="loan",
        )
        await session.commit()
        with pytest.raises(WarehouseInvoiceError, match="Бартер"):
            await adjust_paid_invoice(session, barter, lines=[_line(product.id, "60")])


async def test_adjust_paid_endpoint_permission_and_receivable(client, async_session_factory):
    """API: owner/admin может, менеджер (без edit_paid) — 403; ответ отдаёт moved_to_receivable."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик API")
        product = await make_iiko_product(session, name="Мешок")
        invoice = await make_invoice(
            session, counterparty_id=cp.id, amount="100.00", payment_status="paid", number="600"
        )
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id, source_kind="cash", amount=Decimal("100.00")
            )
        )
        await session.commit()
        invoice_id = str(invoice.id)
        cp_id = cp.id
        product_id = str(product.id)

    body = {"lines": [{"name": "Мешок", "quantity": "1", "price": "60", "iiko_product_id": product_id}]}

    manager_h = await headers_for(async_session_factory, "mgr@teplo.local", ["manager"])
    forbidden = client.post(f"/api/v1/warehouse/invoices/{invoice_id}/adjust-paid", json=body, headers=manager_h)
    assert forbidden.status_code == 403

    admin_h = await admin_headers(async_session_factory)
    ok = client.post(f"/api/v1/warehouse/invoices/{invoice_id}/adjust-paid", json=body, headers=admin_h)
    assert ok.status_code == 200, ok.text
    data = ok.json()
    assert data["amount"] == 60.0
    assert data["payment_status"] == "paid"
    assert data["moved_to_receivable"] == 40.0

    async with async_session_factory() as session:
        assert await counterparty_prepayment_balance(session, cp_id) == Decimal("40.00")


# --- Фаза 2: полный разворот коррекции в iiko (возврат + новая приходная + зачёт) ---------------


def _old_line(invoice_id, product, qty, price):
    return InvoiceLineItem(
        invoice_id=invoice_id,
        iiko_product_id=product.id,
        product_guid=product.iiko_id,
        name=product.name,
        quantity=Decimal(qty),
        price=Decimal(price),
        sum=Decimal(str(float(qty) * float(price))),
        sort_order=0,
    )


async def test_correction_snapshots_all_old_goods(async_session_factory):
    """Накладная в iiko: правка вниз копит СНИМОК ВСЕХ старых товаров (не дельту), status pending."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик R", iiko_guid="SUP-R")
        product = await make_iiko_product(session, name="Мешок")
        invoice = await make_invoice(
            session, counterparty_id=cp.id, amount="100.00", payment_status="paid",
            number="700", external_id="IIKO-DOC-R",
        )
        session.add(_old_line(invoice.id, product, "2", "50"))
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id, source_kind="cash", amount=Decimal("100.00")
            )
        )
        await session.commit()

        await adjust_paid_invoice(session, invoice, lines=[_line(product.id, "50", qty="1")])
        await session.refresh(invoice)

        assert invoice.amount == Decimal("50.00")
        assert invoice.iiko_return_status == "pending"
        # ВСЕ старые товары (2 шт), а не дельта «убыло» (1 шт) — возврат снимает весь старый приход.
        assert invoice.iiko_return_lines == [
            {"product": product.iiko_id, "quantity": 2.0, "price": 50.0, "name": "Мешок"}
        ]
        assert await counterparty_prepayment_balance(session, cp.id) == Decimal("50.00")


async def test_correction_price_only_change_triggers_contour(async_session_factory):
    """Изменилась только ЦЕНА (кол-во то же) → контур всё равно нужен (дебиторка на разницу)."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик P", iiko_guid="SUP-P")
        product = await make_iiko_product(session, name="Мешок")
        invoice = await make_invoice(
            session, counterparty_id=cp.id, amount="100.00", payment_status="paid",
            number="705", external_id="IIKO-DOC-P",
        )
        session.add(_old_line(invoice.id, product, "1", "100"))
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id, source_kind="cash", amount=Decimal("100.00")
            )
        )
        await session.commit()

        await adjust_paid_invoice(session, invoice, lines=[_line(product.id, "60", qty="1")])
        await session.refresh(invoice)

        assert invoice.iiko_return_status == "pending"
        assert invoice.iiko_return_lines == [
            {"product": product.iiko_id, "quantity": 1.0, "price": 100.0, "name": "Мешок"}
        ]
        assert await counterparty_prepayment_balance(session, cp.id) == Decimal("40.00")


async def test_correction_no_contour_when_unchanged(async_session_factory):
    """external_id есть, но правка ничего не изменила (те же товары и сумма) → контур не запускаем."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик U", iiko_guid="SUP-U")
        product = await make_iiko_product(session, name="Мешок")
        invoice = await make_invoice(
            session, counterparty_id=cp.id, amount="100.00", payment_status="paid",
            number="706", external_id="IIKO-DOC-U",
        )
        session.add(_old_line(invoice.id, product, "2", "50"))
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id, source_kind="cash", amount=Decimal("100.00")
            )
        )
        await session.commit()

        await adjust_paid_invoice(session, invoice, lines=[_line(product.id, "50", qty="2")])
        await session.refresh(invoice)
        assert invoice.iiko_return_status == "none"
        assert invoice.iiko_return_lines == []


async def test_correction_no_contour_when_not_in_iiko(async_session_factory):
    """Накладная НЕ в iiko (нет external_id) — контур не запускаем."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик NR")
        product = await make_iiko_product(session, name="Мешок")
        invoice = await make_invoice(
            session, counterparty_id=cp.id, amount="100.00", payment_status="paid", number="701"
        )
        session.add(_old_line(invoice.id, product, "2", "50"))
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id, source_kind="cash", amount=Decimal("100.00")
            )
        )
        await session.commit()

        await adjust_paid_invoice(session, invoice, lines=[_line(product.id, "50", qty="1")])
        await session.refresh(invoice)
        assert invoice.iiko_return_status == "none"
        assert invoice.iiko_return_lines == []


def _wire_contour_mocks(monkeypatch, calls):
    """Замокать сетевые шаги контура: возврат и новая приходная (зачёта в контуре нет)."""

    def fake_return(org, body):
        calls["return"] = body
        return _CloudPushOutcome("RET-DOC-1", posted=True)

    def fake_incoming(direction, org, body, *, existing_document_id):
        calls["incoming"] = {"body": body, "existing": existing_document_id}
        return _CloudPushOutcome("NEW-DOC-Y", posted=True, created=True)

    monkeypatch.setattr(wip, "_cloud_create_and_post_return", fake_return)
    monkeypatch.setattr(wip, "_cloud_create_and_post", fake_incoming)


async def _setup_pending_contour_invoice(session, *, number, external_id="IIKO-DOC-X"):
    cp = await make_counterparty(session, name=f"Поставщик {number}", iiko_guid=f"SUP-{number}")
    product = await make_iiko_product(session, name="Мешок")
    invoice = await make_invoice(
        session, counterparty_id=cp.id, amount="50.00", payment_status="paid",
        number=number, external_id=external_id,
        issued_at=datetime(2026, 7, 11, 12, 0, tzinfo=UTC),
    )
    invoice.store_guid = "ST-1"
    invoice.iiko_return_status = "pending"
    invoice.iiko_return_lines = [
        {"product": product.iiko_id, "quantity": 2.0, "price": 50.0, "name": "Мешок"}
    ]
    # Текущие (правильные) позиции — из них соберётся новая приходная Y (1×50).
    session.add(_old_line(invoice.id, product, "1", "50"))
    session.add(
        InvoicePaymentAllocation(
            invoice_id=invoice.id, source_kind="cash", amount=Decimal("50.00")
        )
    )
    await session.commit()
    return invoice.id


async def test_book_correction_full_contour_success(async_session_factory, monkeypatch):
    """Контур: возврат всех старых + новая приходная (БЕЗ оплаты) → booked, external_id X→Y,
    тумбстон на X, снимок очищен."""
    calls: dict = {}
    async with async_session_factory() as session:
        inv_id = await _setup_pending_contour_invoice(session, number="710")
        _wire_contour_mocks(monkeypatch, calls)
        await book_correction_in_iiko(session, inv_id)

    async with async_session_factory() as session:
        inv = await session.get(SupplierInvoice, inv_id)
        assert inv.iiko_return_status == "booked"
        assert inv.iiko_return_external_id == "RET-DOC-1"
        assert inv.iiko_correction_new_external_id == "NEW-DOC-Y"
        assert inv.external_id == "NEW-DOC-Y"  # перецеп X→Y
        assert inv.iiko_return_lines == []
        tomb = await session.scalar(
            select(SupplierInvoiceTombstone).where(
                SupplierInvoiceTombstone.external_id == "IIKO-DOC-X"
            )
        )
        assert tomb is not None and tomb.source == "manual"

    # Возврат — на ВСЕ старые товары (2 шт) со ссылкой на оригинал X.
    assert calls["return"]["incomingInvoiceId"] == "IIKO-DOC-X"
    assert calls["return"]["items"][0]["amount"] == 2.0
    # Новая приходная — форс create (existing=None). Зачёта в контуре нет — Y остаётся неоплаченной.
    assert calls["incoming"]["existing"] is None
    assert "zachet" not in calls


async def test_book_correction_return_step_fails_keeps_state(async_session_factory, monkeypatch):
    """Сбой на шаге возврата → failed; external_id не перецеплен, снимок и Y не тронуты."""
    async with async_session_factory() as session:
        inv_id = await _setup_pending_contour_invoice(session, number="711")
        monkeypatch.setattr(
            wip,
            "_cloud_create_and_post_return",
            lambda org, body: _CloudPushOutcome(None, posted=False, error="create HTTP 500"),
        )
        await book_correction_in_iiko(session, inv_id)

    async with async_session_factory() as session:
        inv = await session.get(SupplierInvoice, inv_id)
        assert inv.iiko_return_status == "failed"
        assert "500" in (inv.iiko_return_error or "")
        assert inv.iiko_return_external_id is None
        assert inv.iiko_correction_new_external_id is None
        assert inv.external_id == "IIKO-DOC-X"  # не перецеплен
        assert inv.iiko_return_lines != []  # снимок сохранён для ретрая


async def test_book_correction_resumes_after_return(async_session_factory, monkeypatch):
    """Ретрай, когда возврат (шаг 1) уже сделан: его НЕ пересоздаём, идёт только новая приходная."""
    calls: dict = {}
    async with async_session_factory() as session:
        inv_id = await _setup_pending_contour_invoice(session, number="712")
        # Симулируем уже пройденный шаг 1 (возврат проведён), шаг 2 ещё нет.
        inv = await session.get(SupplierInvoice, inv_id)
        inv.iiko_return_external_id = "RET-DOC-1"
        inv.iiko_return_status = "failed"
        await session.commit()

        _wire_contour_mocks(monkeypatch, calls)
        await book_correction_in_iiko(session, inv_id)

    async with async_session_factory() as session:
        inv = await session.get(SupplierInvoice, inv_id)
        assert inv.iiko_return_status == "booked"
        assert inv.iiko_correction_new_external_id == "NEW-DOC-Y"
        assert inv.external_id == "NEW-DOC-Y"
        assert inv.iiko_return_lines == []
    # Шаг 1 пропущен (возврат не пересоздан), шаг 2 отработал.
    assert "return" not in calls
    assert calls["incoming"]["existing"] is None


async def test_book_correction_new_invoice_step_fails(async_session_factory, monkeypatch):
    """Сбой на шаге новой приходной → failed; возврат (шаг 1) закреплён, external_id не перецеплен."""
    async with async_session_factory() as session:
        inv_id = await _setup_pending_contour_invoice(session, number="713")
        monkeypatch.setattr(
            wip, "_cloud_create_and_post_return",
            lambda org, body: _CloudPushOutcome("RET-DOC-1", posted=True),
        )
        monkeypatch.setattr(
            wip, "_cloud_create_and_post",
            lambda direction, org, body, *, existing_document_id: _CloudPushOutcome(
                None, posted=False, error="create HTTP 500"
            ),
        )
        await book_correction_in_iiko(session, inv_id)

    async with async_session_factory() as session:
        inv = await session.get(SupplierInvoice, inv_id)
        assert inv.iiko_return_status == "failed"
        assert inv.iiko_return_external_id == "RET-DOC-1"  # шаг 1 закреплён
        assert inv.iiko_correction_new_external_id is None
        assert inv.external_id == "IIKO-DOC-X"  # не перецеплен

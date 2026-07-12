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
    make_draft,
    make_iiko_product,
    make_invoice,
)
from sqlalchemy import select

import app.services.counterparty_iiko_payment as cip
import app.services.warehouse_invoice_push as wip
from app.models import (
    IikoInvoicePaymentPush,
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
    get_warehouse_invoice,
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


async def test_gate_bank_finalized_draft_allowed(async_session_factory):
    """Оплаченная ЧЕРЕЗ БАНК (черновик paid, не через Сейф) — править МОЖНО: главный кейс фичи
    (счёт ЭДО ушёл в банк и оплачен). Излишек так же уходит в дебиторку."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик BF")
        product = await make_iiko_product(session, name="Мешок")
        draft = await make_draft(session, counterparty_id=cp.id, amount="100.00", status="paid")
        invoice = await make_invoice(
            session, counterparty_id=cp.id, amount="100.00", payment_status="paid",
            number="800", draft_id=draft.id,
        )
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id, source_kind="bank", amount=Decimal("100.00")
            )
        )
        await session.commit()

        await adjust_paid_invoice(session, invoice, lines=[_line(product.id, "60")])
        await session.refresh(invoice)
        assert invoice.amount == Decimal("60.00")
        assert invoice.payment_status == "paid"
        assert await counterparty_prepayment_balance(session, cp.id) == Decimal("40.00")


async def test_gate_pending_or_safe_draft_blocked(async_session_factory):
    """Черновик ещё в банке (created) ИЛИ деньги зарезервированы на Сейфе (pays_via_safe) —
    править НЕЛЬЗЯ: платёж не финализирован, сначала отзыв."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик PB")
        product = await make_iiko_product(session, name="Мешок")
        # 1) черновик created — висит в банке, деньги не ушли
        d1 = await make_draft(session, counterparty_id=cp.id, amount="100.00", status="created")
        inv1 = await make_invoice(
            session, counterparty_id=cp.id, amount="100.00", payment_status="paid",
            number="801", draft_id=d1.id,
        )
        await session.commit()
        with pytest.raises(WarehouseInvoiceError, match="банк"):
            await adjust_paid_invoice(session, inv1, lines=[_line(product.id, "60")])

        # 2) черновик paid, но через Сейф — резерв ещё не выплачен
        d2 = await make_draft(session, counterparty_id=cp.id, amount="100.00", status="paid")
        d2.pays_via_safe = True
        inv2 = await make_invoice(
            session, counterparty_id=cp.id, amount="100.00", payment_status="paid",
            number="802", draft_id=d2.id,
        )
        await session.commit()
        with pytest.raises(WarehouseInvoiceError, match="банк"):
            await adjust_paid_invoice(session, inv2, lines=[_line(product.id, "60")])


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


# ============================================================================
# Этап 1 — гард «правка оплаченной ↔ оплата отражена в iiko»
# Не пускаем коррекцию, пока оплата ОРИГИНАЛА не отражена в iiko (иначе book_correction
# перецепит external_id X→Y и затумбстонит X навсегда — возврат уйдёт не на ту сумму,
# корень перекоса АЛЬЯНС ЮГ / DX001323A).
# ============================================================================


def _fake_ok(calls):
    """Мок add_payment: HTTP 201 (проводка создана)."""

    def fake(payload):
        calls.append(payload)
        return 201, {"accountingTransactionId": "TX-1", "sum": payload["amount"]}

    return fake


def _fake_already_paid(calls):
    """Мок add_payment: iiko отвечает «already paid» (оплата уже есть) — идемпотентный успех."""

    def fake(payload):
        calls.append(payload)
        return 400, {"message": "Invoice already paid"}

    return fake


def _ok_push(invoice_id, external_id, amount="100.00"):
    """ok-строка зеркала = оплата оригинала отражена в iiko (снимает гард)."""
    return IikoInvoicePaymentPush(
        idempotency_key=f"invoice:{invoice_id}",
        invoice_id=invoice_id,
        external_id=external_id,
        amount=Decimal(amount),
        account_to=cip.IIKO_ACQUIRING_ACCOUNT,
        status="ok",
        attempts=0,
    )


async def _iiko_bank_paid_invoice(
    session, *, number, external_id, amount="100.00", siblings=1
):
    """iiko-накладная, оплаченная через банк (черновик paid, аллокация bank). ``siblings>1`` —
    несколько накладных на одном черновике (мультиплатёж — зеркало долю не выводит)."""
    cp = await make_counterparty(session, name=f"Поставщик {number}", iiko_guid=f"SUP-{number}")
    product = await make_iiko_product(session, name="Мешок")
    draft = await make_draft(session, counterparty_id=cp.id, amount=amount, status="paid")
    invoice = await make_invoice(
        session, counterparty_id=cp.id, amount=amount, payment_status="paid",
        number=number, source="iiko", external_id=external_id, draft_id=draft.id,
    )
    session.add(
        InvoicePaymentAllocation(
            invoice_id=invoice.id, source_kind="bank", amount=Decimal(amount)
        )
    )
    for i in range(1, siblings):
        await make_invoice(
            session, counterparty_id=cp.id, amount=amount, payment_status="paid",
            number=f"{number}-{i}", source="iiko", external_id=f"{external_id}-{i}",
            draft_id=draft.id,
        )
    await session.commit()
    return cp, product, draft, invoice


async def _setup_iiko_pending_contour(session, *, number, external_id=None):
    """Как _setup_pending_contour_invoice, но source='iiko' — для гарда book_correction."""
    external_id = external_id or f"IIKO-{number}"
    cp = await make_counterparty(session, name=f"Поставщик {number}", iiko_guid=f"SUP-{number}")
    product = await make_iiko_product(session, name="Мешок")
    invoice = await make_invoice(
        session, counterparty_id=cp.id, amount="50.00", payment_status="paid",
        number=number, source="iiko", external_id=external_id,
        issued_at=datetime(2026, 7, 11, 12, 0, tzinfo=UTC),
    )
    invoice.store_guid = "ST-1"
    invoice.iiko_return_status = "pending"
    invoice.iiko_return_lines = [
        {"product": product.iiko_id, "quantity": 2.0, "price": 50.0, "name": "Мешок"}
    ]
    session.add(_old_line(invoice.id, product, "1", "50"))
    session.add(
        InvoicePaymentAllocation(
            invoice_id=invoice.id, source_kind="cash", amount=Decimal("50.00")
        )
    )
    await session.commit()
    return invoice.id


async def _iiko_cash_paid_invoice(session, *, number, external_id, amount="100.00"):
    """iiko-накладная, оплаченная наличными (без банк-черновика) → auto_sendable=False (банк-
    зеркала нет). Гард держит её до ручного подтверждения."""
    cp = await make_counterparty(session, name=f"Поставщик {number}", iiko_guid=f"SUP-{number}")
    product = await make_iiko_product(session, name="Мешок")
    invoice = await make_invoice(
        session, counterparty_id=cp.id, amount=amount, payment_status="paid",
        number=number, source="iiko", external_id=external_id,
    )
    session.add(
        InvoicePaymentAllocation(
            invoice_id=invoice.id, source_kind="cash", amount=Decimal(amount)
        )
    )
    await session.commit()
    return cp, product, invoice


async def _kassa_paid_invoice(session, *, number, external_id, amount="100.00"):
    """Оплаченная накладная Кассы (source='kassa_invoice') с external_id — у неё своё товарное
    зеркало (namespace kassa_goods:*), гард смотрит именно его."""
    cp = await make_counterparty(session, name=f"Поставщик {number}", iiko_guid=f"SUP-{number}")
    product = await make_iiko_product(session, name="Мешок")
    invoice = await make_invoice(
        session, counterparty_id=cp.id, amount=amount, payment_status="paid",
        number=number, source="kassa_invoice", external_id=external_id,
    )
    session.add(
        InvoicePaymentAllocation(
            invoice_id=invoice.id, source_kind="cash", amount=Decimal(amount)
        )
    )
    await session.commit()
    return cp, product, invoice


async def test_guard_blocks_edit_when_payment_not_in_iiko(async_session_factory):
    """Репро АЛЬЯНС ЮГ: iiko-накладная оплачена через банк, но оплата НЕ зеркалирована в iiko →
    правку оплаченной НЕ пускаем; накладная остаётся нетронутой."""
    async with async_session_factory() as session:
        _, product, _, invoice = await _iiko_bank_paid_invoice(
            session, number="900", external_id="IIKO-900"
        )
        inv_id = invoice.id
        with pytest.raises(WarehouseInvoiceError, match="не отражена в iiko"):
            await adjust_paid_invoice(session, invoice, lines=[_line(product.id, "60")])
    async with async_session_factory() as session:
        fresh = await session.get(SupplierInvoice, inv_id)
        assert fresh.amount == Decimal("100.00")  # не тронута
        assert fresh.iiko_return_status == "none"  # сага не взведена


async def test_guard_allows_edit_when_payment_mirrored(async_session_factory):
    """Есть ok-строка зеркала (оплата в iiko) → правка проходит, излишек в дебиторку."""
    async with async_session_factory() as session:
        cp, product, _, invoice = await _iiko_bank_paid_invoice(
            session, number="901", external_id="IIKO-901"
        )
        session.add(_ok_push(invoice.id, "IIKO-901"))
        await session.commit()
        await adjust_paid_invoice(session, invoice, lines=[_line(product.id, "60")])
        await session.refresh(invoice)
        assert invoice.amount == Decimal("60.00")
        assert await counterparty_prepayment_balance(session, cp.id) == Decimal("40.00")


async def test_send_now_pushes_and_unblocks(async_session_factory, monkeypatch):
    """«Отправить оплату сейчас»: одиночный банк-черновик → add_payment, ok-строка, гард снят."""
    calls: list = []
    monkeypatch.setattr(cip, "_call_add_payment", _fake_ok(calls))
    async with async_session_factory() as session:
        _, product, _, invoice = await _iiko_bank_paid_invoice(
            session, number="902", external_id="IIKO-902"
        )
        res = await cip.mirror_single_paid_iiko_invoice(session, invoice.id)
        assert res.ok
        assert len(calls) == 1 and calls[0]["documentId"] == "IIKO-902"
        assert await cip.original_payment_settled_in_iiko(session, invoice) is True
        # теперь правка проходит
        await adjust_paid_invoice(session, invoice, lines=[_line(product.id, "60")])
        await session.refresh(invoice)
        assert invoice.amount == Decimal("60.00")


async def test_send_now_rejects_multi_invoice(async_session_factory):
    """Мультиплатёж (несколько накладных на одном черновике) — авто-отправить долю нельзя."""
    async with async_session_factory() as session:
        _, _, _, invoice = await _iiko_bank_paid_invoice(
            session, number="903", external_id="IIKO-903", siblings=2
        )
        with pytest.raises(cip.IikoPaymentError, match="несколько накладных"):
            await cip.mirror_single_paid_iiko_invoice(session, invoice.id)


async def test_send_now_already_paid_counts_as_ok(async_session_factory, monkeypatch):
    """Гибридный контур: платёж уже проведён в бэк-офисе iiko → «already paid» = ok, гард снят."""
    calls: list = []
    monkeypatch.setattr(cip, "_call_add_payment", _fake_already_paid(calls))
    async with async_session_factory() as session:
        _, _, _, invoice = await _iiko_bank_paid_invoice(
            session, number="904", external_id="IIKO-904"
        )
        res = await cip.mirror_single_paid_iiko_invoice(session, invoice.id)
        assert res.ok
        assert await cip.original_payment_settled_in_iiko(session, invoice) is True


async def test_confirm_manual_marks_settled(async_session_factory):
    """«Оплата подтверждена вручную» для НЕ-авто (наличная iiko): ok-маркер, гард снят."""
    async with async_session_factory() as session:
        _, product, invoice = await _iiko_cash_paid_invoice(
            session, number="905", external_id="IIKO-905"
        )
        assert await cip.iiko_invoice_payment_auto_sendable(session, invoice) is False
        assert await cip.original_payment_settled_in_iiko(session, invoice) is False  # гард держит
        await cip.mark_iiko_payment_settled_manually(session, invoice, actor_user_id=None)
        row = await session.scalar(
            select(IikoInvoicePaymentPush).where(
                IikoInvoicePaymentPush.idempotency_key == f"invoice:{invoice.id}"
            )
        )
        assert row.status == "ok"
        assert row.response_payload.get("manual_confirmation") is True
        assert await cip.original_payment_settled_in_iiko(session, invoice) is True
        await adjust_paid_invoice(session, invoice, lines=[_line(product.id, "60")])
        await session.refresh(invoice)
        assert invoice.amount == Decimal("60.00")


async def test_confirm_manual_refuses_when_auto_sendable(async_session_factory):
    """Авто-отправляемую (одиночный банк-черновик) вслепую подтверждать НЕЛЬЗЯ — это заглушило бы
    реальное зеркало add_payment (воспроизвело бы перекос АЛЬЯНС ЮГ). Требуем «Отправить сейчас»."""
    async with async_session_factory() as session:
        _, _, _, invoice = await _iiko_bank_paid_invoice(
            session, number="905b", external_id="IIKO-905B"
        )
        with pytest.raises(cip.IikoPaymentError, match="автоматически"):
            await cip.mark_iiko_payment_settled_manually(session, invoice, actor_user_id=None)
        # ok-строка НЕ создана — накладная осталась под гардом
        row = await session.scalar(
            select(IikoInvoicePaymentPush).where(
                IikoInvoicePaymentPush.idempotency_key == f"invoice:{invoice.id}"
            )
        )
        assert row is None


async def test_confirm_manual_keeps_real_push_row(async_session_factory):
    """mark не затирает реальную ok-строку зеркала (сохраняет accountingTransactionId и сумму)."""
    async with async_session_factory() as session:
        # мульти → auto_sendable=False, поэтому mark не откажет; сеем «реальный» ok от зеркала
        _, _, _, invoice = await _iiko_bank_paid_invoice(
            session, number="905c", external_id="IIKO-905C", siblings=2
        )
        real = _ok_push(invoice.id, "IIKO-905C")
        real.iiko_document_id = "TX-real"
        session.add(real)
        await session.commit()
        await cip.mark_iiko_payment_settled_manually(session, invoice, actor_user_id=None)
        row = await session.scalar(
            select(IikoInvoicePaymentPush).where(
                IikoInvoicePaymentPush.idempotency_key == f"invoice:{invoice.id}"
            )
        )
        assert row.iiko_document_id == "TX-real"  # не затёрто
        assert row.account_to != "manual"


async def test_guard_blocks_kassa_edit_when_goods_not_mirrored(async_session_factory):
    """Kassa-накладная: пока оплата (kassa_goods:*) не зеркалирована — правку не пускаем."""
    async with async_session_factory() as session:
        _, product, invoice = await _kassa_paid_invoice(
            session, number="920", external_id="K-920"
        )
        assert await cip.original_payment_settled_in_iiko(session, invoice) is False
        with pytest.raises(WarehouseInvoiceError, match="не отражена в iiko"):
            await adjust_paid_invoice(session, invoice, lines=[_line(product.id, "60")])


async def test_guard_allows_kassa_edit_when_goods_share_mirrored(async_session_factory):
    """Успешная доля kassa_goods:<id>:* (товар в iiko) → правка Кассы проходит."""
    async with async_session_factory() as session:
        _, product, invoice = await _kassa_paid_invoice(
            session, number="921", external_id="K-921"
        )
        session.add(
            IikoInvoicePaymentPush(
                idempotency_key=f"kassa_goods:{invoice.id}:card", invoice_id=invoice.id,
                external_id="K-921", amount=Decimal("100.00"),
                account_to=cip.IIKO_ACQUIRING_ACCOUNT, status="ok",
            )
        )
        await session.commit()
        assert await cip.original_payment_settled_in_iiko(session, invoice) is True
        await adjust_paid_invoice(session, invoice, lines=[_line(product.id, "60")])
        await session.refresh(invoice)
        assert invoice.amount == Decimal("60.00")


async def test_guard_blocks_kassa_when_only_plain_done_marker(async_session_factory):
    """Просто kassa_goods_done без manual-флага и без ok-доли (терминальный сбой зеркала) — НЕ
    считаем settled: коррекцию держим (иначе возврат поверх неоплаченного в iiko прихода)."""
    async with async_session_factory() as session:
        _, product, invoice = await _kassa_paid_invoice(
            session, number="923", external_id="K-923"
        )
        session.add(
            IikoInvoicePaymentPush(
                idempotency_key=f"kassa_goods_done:{invoice.id}", invoice_id=invoice.id,
                external_id="K-923", amount=Decimal("0"), account_to="-", status="ok",
                error="ручной разбор",
            )
        )
        await session.commit()
        assert await cip.original_payment_settled_in_iiko(session, invoice) is False


async def test_confirm_manual_kassa_writes_done_marker(async_session_factory):
    """Ручное подтверждение Кассы пишет kassa_goods_done:<id> с manual-флагом → гард снят, джоб
    пропустит (маркер done)."""
    async with async_session_factory() as session:
        _, product, invoice = await _kassa_paid_invoice(
            session, number="922", external_id="K-922"
        )
        await cip.mark_iiko_payment_settled_manually(session, invoice, actor_user_id=None)
        row = await session.scalar(
            select(IikoInvoicePaymentPush).where(
                IikoInvoicePaymentPush.idempotency_key == f"kassa_goods_done:{invoice.id}"
            )
        )
        assert row is not None and row.status == "ok"
        assert row.response_payload.get("manual_confirmation") is True
        # ключ invoice:<id> НЕ создан (у Кассы другой namespace)
        wrong = await session.scalar(
            select(IikoInvoicePaymentPush).where(
                IikoInvoicePaymentPush.idempotency_key == f"invoice:{invoice.id}"
            )
        )
        assert wrong is None
        assert await cip.original_payment_settled_in_iiko(session, invoice) is True


async def test_book_correction_blocked_when_payment_not_in_iiko(
    async_session_factory, monkeypatch
):
    """Прямой вход retry-iiko-return: без ok-пуша разворот не идёт (failed), X не перецеплен."""
    calls: dict = {}
    _wire_contour_mocks(monkeypatch, calls)
    async with async_session_factory() as session:
        inv_id = await _setup_iiko_pending_contour(session, number="906")
        await book_correction_in_iiko(session, inv_id)
    async with async_session_factory() as session:
        inv = await session.get(SupplierInvoice, inv_id)
        assert inv.iiko_return_status == "failed"
        assert "не отражена в iiko" in (inv.iiko_return_error or "")
        assert inv.external_id == "IIKO-906"  # не перецеплен
        assert inv.iiko_correction_new_external_id is None
    assert calls == {}  # сетевые шаги не вызывались


async def test_book_correction_proceeds_when_payment_mirrored(
    async_session_factory, monkeypatch
):
    """С ok-пушем разворот проходит штатно (booked, external_id X→Y)."""
    calls: dict = {}
    _wire_contour_mocks(monkeypatch, calls)
    async with async_session_factory() as session:
        inv_id = await _setup_iiko_pending_contour(session, number="907")
        session.add(_ok_push(inv_id, "IIKO-907", "50.00"))
        await session.commit()
        await book_correction_in_iiko(session, inv_id)
    async with async_session_factory() as session:
        inv = await session.get(SupplierInvoice, inv_id)
        assert inv.iiko_return_status == "booked"
        assert inv.external_id == "NEW-DOC-Y"


async def test_detail_exposes_iiko_payment_gate_fields(async_session_factory):
    """Деталь накладной отдаёт фронту iiko_payment_settled / auto_sendable для гарда-UX."""
    async with async_session_factory() as session:
        _, _, _, invoice = await _iiko_bank_paid_invoice(
            session, number="908", external_id="IIKO-908"
        )
        detail = await get_warehouse_invoice(session, invoice.id)
        assert detail["iiko_payment_settled"] is False
        assert detail["iiko_payment_auto_sendable"] is True  # одиночный банк-черновик
        session.add(_ok_push(invoice.id, "IIKO-908"))
        await session.commit()
        detail2 = await get_warehouse_invoice(session, invoice.id)
        assert detail2["iiko_payment_settled"] is True


async def test_detail_auto_sendable_false_for_multi(async_session_factory):
    """Мультиплатёж → auto_sendable False (авто-отправить долю нельзя)."""
    async with async_session_factory() as session:
        _, _, _, invoice = await _iiko_bank_paid_invoice(
            session, number="909", external_id="IIKO-909", siblings=2
        )
        detail = await get_warehouse_invoice(session, invoice.id)
        assert detail["iiko_payment_auto_sendable"] is False


async def test_send_iiko_payment_endpoint(client, async_session_factory, monkeypatch):
    """API send-iiko-payment: менеджеру 403; owner → 200 и оплата отражена в iiko."""
    calls: list = []
    monkeypatch.setattr(cip, "_call_add_payment", _fake_ok(calls))
    async with async_session_factory() as session:
        _, _, _, invoice = await _iiko_bank_paid_invoice(
            session, number="910", external_id="IIKO-910"
        )
        inv_id = str(invoice.id)
    mgr_h = await headers_for(async_session_factory, "mgr2@teplo.local", ["manager"])
    assert (
        client.post(
            f"/api/v1/warehouse/invoices/{inv_id}/send-iiko-payment", headers=mgr_h
        ).status_code
        == 403
    )
    admin_h = await admin_headers(async_session_factory)
    r = client.post(f"/api/v1/warehouse/invoices/{inv_id}/send-iiko-payment", headers=admin_h)
    assert r.status_code == 200, r.text
    assert r.json()["iiko_payment_settled"] is True
    assert len(calls) == 1


async def test_confirm_iiko_payment_endpoint(client, async_session_factory):
    """API confirm-iiko-payment: не-авто (наличная iiko) → 200 и оплата отражена; авто (одиночный
    банк) → 422 (вслепую подтверждать нельзя, надо «Отправить оплату в iiko сейчас»)."""
    async with async_session_factory() as session:
        _, _, cash_inv = await _iiko_cash_paid_invoice(
            session, number="911", external_id="IIKO-911"
        )
        cash_id = str(cash_inv.id)
        _, _, _, auto_inv = await _iiko_bank_paid_invoice(
            session, number="911b", external_id="IIKO-911B"
        )
        auto_id = str(auto_inv.id)
    admin_h = await admin_headers(async_session_factory)
    r = client.post(f"/api/v1/warehouse/invoices/{cash_id}/confirm-iiko-payment", headers=admin_h)
    assert r.status_code == 200, r.text
    assert r.json()["iiko_payment_settled"] is True
    r2 = client.post(f"/api/v1/warehouse/invoices/{auto_id}/confirm-iiko-payment", headers=admin_h)
    assert r2.status_code == 422  # авто → предлагаем «Отправить сейчас»

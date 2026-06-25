"""Зеркалирование ТОВАРНОЙ оплаты чеков/накладных Кассы в iiko (Cloud add_payment).

HTTP к iiko замокан (``_call_add_payment``). Проверяем: товарную сумму (= incomingInvoice)
разносим по источникам (карта→эквайринг, наличные→Главная касса) двумя add_payment; backlog
(товар уже погашён старым addPayOut) НЕ зеркалим; идемпотентность через done-маркер. teplo_test.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from cp_helpers import make_counterparty, make_expense_article, make_invoice
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.services.counterparty_iiko_payment as mod
from app.models import (
    ChequeIikoPayout,
    IikoInvoicePaymentPush,
    InvoiceLineItem,
    InvoicePaymentAllocation,
)

ACQUIRING = "3f261590-f208-2970-1300-95d2493a3c28"
GLAVNAYA_KASSA = "8ccc8f0f-24f6-64d2-5eea-04f829ba381f"
ISSUED = datetime(2026, 6, 17, 12, 0, tzinfo=UTC)


def _fake_ok(calls: list):
    def _f(payload: dict):
        calls.append(payload)
        return 201, {
            "accountingTransactionId": f"t-{len(calls)}",
            "documentId": payload["documentId"],
        }

    return _f


async def _seed_kassa(
    factory: async_sessionmaker[AsyncSession],
    *,
    source: str = "kassa_cheque",
    external_id: str | None = None,
    goods: str = "600.00",
    card: str = "700.00",
    cash: str = "300.00",
    with_supplier_addpayout: bool = False,
    with_old_is: bool = False,
) -> uuid.UUID:
    """Оплаченный чек/накладная Кассы: товарная строка (с product_guid, статья «Оплата
    поставщикам») + персональная (is_staff, без product_guid) + аллокации карта/наличные."""
    external_id = external_id or f"doc-{uuid.uuid4()}"
    card_d, cash_d, goods_d = Decimal(card), Decimal(cash), Decimal(goods)
    total = card_d + cash_d
    async with factory() as session:
        cp = await make_counterparty(session, name="Местный закуп", inn="7712345678")
        supplier_article = await make_expense_article(session)  # «Оплата поставщикам»
        inv = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount=total,
            source=source,
            external_id=external_id,
            payment_status="paid",
            issued_at=ISSUED,
        )
        session.add(
            InvoiceLineItem(
                invoice_id=inv.id, name="Курица", quantity=Decimal("1"),
                price=goods_d, sum=goods_d, product_guid="prod-1",
                dds_article_id=supplier_article.id, is_staff=False,
            )
        )
        # Персональная строка (питание): is_staff → в товар/incomingInvoice не входит.
        session.add(
            InvoiceLineItem(
                invoice_id=inv.id, name="Питание", quantity=Decimal("1"),
                price=total - goods_d, sum=total - goods_d, product_guid=None,
                dds_article_id=None, is_staff=True,
            )
        )
        if card_d > 0:
            session.add(
                InvoicePaymentAllocation(invoice_id=inv.id, source_kind="bank", amount=card_d)
            )
        if cash_d > 0:
            session.add(
                InvoicePaymentAllocation(invoice_id=inv.id, source_kind="cash", amount=cash_d)
            )
        if with_supplier_addpayout:
            # Старое изъятие товара уже прошло (backlog) → зеркало должно ПРОПУСТИТЬ.
            session.add(
                ChequeIikoPayout(
                    invoice_id=inv.id, dds_article_id=supplier_article.id, source="card",
                    pay_out_type_id="T_SUP", amount=goods_d, comment="x", status="posted",
                )
            )
        if with_old_is:
            # Старое «ИС»-зеркало (invoice_paid_push) уже погасило товар — отметка в raw_payload.
            inv.raw_payload = {"iiko_payment": {"status": "posted"}}
        await session.commit()
        return inv.id


async def _push_rows(factory, invoice_id) -> list[IikoInvoicePaymentPush]:
    async with factory() as session:
        return list(
            (
                await session.scalars(
                    select(IikoInvoicePaymentPush).where(
                        IikoInvoicePaymentPush.invoice_id == invoice_id
                    )
                )
            ).all()
        )


@pytest.mark.asyncio
async def test_mirror_splits_goods_card_and_cash(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Товар 600 при оплате карта 700 / нал 300 → card_share=420 (эквайринг), cash_share=180
    (Главная касса); два add_payment; done-маркер."""
    inv_id = await _seed_kassa(async_session_factory, goods="600.00", card="700.00", cash="300.00")
    calls: list[dict] = []
    monkeypatch.setattr(mod, "_call_add_payment", _fake_ok(calls))

    async with async_session_factory() as session:
        result = await mod.mirror_paid_kassa_invoices(session)

    assert result["ok"] == 2, result
    by_account = {c["accountId"]: c for c in calls}
    assert by_account[ACQUIRING]["amount"] == 420.0
    assert by_account[GLAVNAYA_KASSA]["amount"] == 180.0
    assert all(c["documentId"] for c in calls)

    rows = await _push_rows(async_session_factory, inv_id)
    keys = {r.idempotency_key for r in rows}
    assert f"kassa_goods:{inv_id}:card" in keys
    assert f"kassa_goods:{inv_id}:cash" in keys
    assert f"kassa_goods_done:{inv_id}" in keys  # маркер завершения


@pytest.mark.asyncio
async def test_mirror_cash_only_single_payment(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Чисто наличная накладная Кассы → один add_payment на Главную кассу (card-доля = 0)."""
    await _seed_kassa(
        async_session_factory, source="kassa_invoice", goods="500.00", card="0.00", cash="500.00"
    )
    calls: list[dict] = []
    monkeypatch.setattr(mod, "_call_add_payment", _fake_ok(calls))
    async with async_session_factory() as session:
        result = await mod.mirror_paid_kassa_invoices(session)
    assert result["ok"] == 1, result
    assert len(calls) == 1
    assert calls[0]["accountId"] == GLAVNAYA_KASSA
    assert calls[0]["amount"] == 500.0


@pytest.mark.asyncio
async def test_mirror_skips_backlog_addpayout(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Товар уже погашён старым addPayOut (posted ChequeIikoPayout по supplier-статье) → НЕ
    add_payment'им (задвоило бы), помечаем backlog/done."""
    inv_id = await _seed_kassa(async_session_factory, with_supplier_addpayout=True)
    calls: list[dict] = []
    monkeypatch.setattr(mod, "_call_add_payment", _fake_ok(calls))
    async with async_session_factory() as session:
        result = await mod.mirror_paid_kassa_invoices(session)
    assert calls == []  # ни одной реальной оплаты в iiko
    assert result["backlog"] == 1 and result["ok"] == 0
    rows = await _push_rows(async_session_factory, inv_id)
    assert any(r.idempotency_key == f"kassa_goods_done:{inv_id}" for r in rows)


@pytest.mark.asyncio
async def test_mirror_skips_backlog_old_is(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Товар погашён СТАРЫМ «ИС»-зеркалом (raw_payload['iiko_payment'].status='posted', напр.
    накладные Кассы 4-2/4-3 на проде) → НЕ add_payment'им, помечаем backlog/done."""
    inv_id = await _seed_kassa(async_session_factory, source="kassa_invoice", with_old_is=True)
    calls: list[dict] = []
    monkeypatch.setattr(mod, "_call_add_payment", _fake_ok(calls))
    async with async_session_factory() as session:
        result = await mod.mirror_paid_kassa_invoices(session)
    assert calls == []
    assert result["backlog"] == 1 and result["ok"] == 0
    rows = await _push_rows(async_session_factory, inv_id)
    assert any(r.idempotency_key == f"kassa_goods_done:{inv_id}" for r in rows)


@pytest.mark.asyncio
async def test_mirror_idempotent_second_run_excluded(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Второй прогон: документ исключён done-маркером (eligible=0), повторных оплат нет."""
    await _seed_kassa(async_session_factory)
    calls: list[dict] = []
    monkeypatch.setattr(mod, "_call_add_payment", _fake_ok(calls))
    async with async_session_factory() as session:
        await mod.mirror_paid_kassa_invoices(session)
    assert len(calls) == 2
    async with async_session_factory() as session:
        second = await mod.mirror_paid_kassa_invoices(session)
    assert second["eligible"] == 0
    assert len(calls) == 2  # вторых вызовов нет

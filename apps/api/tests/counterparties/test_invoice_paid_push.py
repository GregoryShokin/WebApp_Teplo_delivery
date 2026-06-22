"""iiko-проводка оплаты накладной (``invoice_paid_push``). iiko замокан (monkeypatch
имён в модуле). Прогон на ``teplo_test``."""

from __future__ import annotations

import asyncio
from decimal import Decimal

from cp_helpers import (
    admin_headers,
    make_counterparty,
    make_expense_article,
    make_invoice,
)
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.counterparty_registry import list_invoices
from app.services.kassa import invoice_paid_push as pp

WH = "/api/v1/warehouse"


def _run(coro):
    return asyncio.run(coro)


def _mock_iiko(monkeypatch, calls: list, *, success: bool = True) -> None:
    monkeypatch.setattr(pp, "_auth_token", lambda: "TOKEN")

    def fake_post(token, path, body):  # noqa: ANN001, ANN202
        calls.append((path, body))
        return {"result": "SUCCESS" if success else "ERROR"}

    monkeypatch.setattr(pp, "_iiko_post", fake_post)


def test_invoice_payment_posts_with_counteragent(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    calls: list = []
    _mock_iiko(monkeypatch, calls)

    async def scenario():
        async with async_session_factory() as session:
            cp = await make_counterparty(
                session, name="ООО Поставщик", inn="7701234567", iiko_guid="GUID-123"
            )
            inv = await make_invoice(
                session, counterparty_id=cp.id, amount="500.00", number="Н-1"
            )
            await session.commit()
            await pp.post_invoice_payment_to_iiko(session, inv, amount=Decimal("500.00"))
            await session.commit()
            await session.refresh(inv)
            return dict(inv.raw_payload or {})

    raw = _run(scenario())
    assert len(calls) == 1
    _path, body = calls[0]
    assert body["payOutTypeId"] == pp.INVOICE_PAYMENT_PAYOUT_TYPE_ID
    assert body["counteragent"] == "GUID-123"
    assert body["departmentSumMap"] == {pp.CHERNIKOVA_DEPARTMENT_ID: 500.0}
    assert raw["iiko_payment"]["status"] == "posted"


def test_invoice_payment_idempotent(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    calls: list = []
    _mock_iiko(monkeypatch, calls)

    async def scenario():
        async with async_session_factory() as session:
            cp = await make_counterparty(
                session, name="ООО Поставщик2", inn="7701234568", iiko_guid="GUID-2"
            )
            inv = await make_invoice(session, counterparty_id=cp.id, amount="300.00")
            await session.commit()
            await pp.post_invoice_payment_to_iiko(session, inv, amount=Decimal("300.00"))
            await session.commit()
            # повтор: уже posted — iiko не дёргаем повторно
            await pp.post_invoice_payment_to_iiko(session, inv, amount=Decimal("300.00"))
            await session.commit()

    _run(scenario())
    assert len(calls) == 1


def test_invoice_payment_no_guid_marks_failed(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    calls: list = []
    _mock_iiko(monkeypatch, calls)

    async def scenario():
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="ООО БезГуид", inn="7701234569")
            inv = await make_invoice(session, counterparty_id=cp.id, amount="200.00")
            await session.commit()
            await pp.post_invoice_payment_to_iiko(session, inv, amount=Decimal("200.00"))
            await session.commit()
            await session.refresh(inv)
            return dict(inv.raw_payload or {})

    raw = _run(scenario())
    assert len(calls) == 0  # iiko не дёрнут — нет GUID
    assert raw["iiko_payment"]["status"] == "failed"


def test_pay_kassa_endpoint_blocks_without_iiko_guid(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async def seed():
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="ООО НетГуид-Касса", inn="7705551111")
            inv = await make_invoice(session, counterparty_id=cp.id, amount="100.00")
            await session.commit()
            return inv.id

    inv_id = _run(seed())
    headers = _run(admin_headers(async_session_factory))
    r = client.post(f"{WH}/invoices/{inv_id}/pay-kassa", json={"amount": None}, headers=headers)
    assert r.status_code == 409
    assert "iiko" in r.json()["detail"].lower()


def test_pay_kassa_endpoint_blocks_already_paid(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async def seed():
        async with async_session_factory() as session:
            cp = await make_counterparty(
                session, name="ООО Оплач-Касса", inn="7705552222", iiko_guid="G-PAID"
            )
            inv = await make_invoice(
                session, counterparty_id=cp.id, amount="100.00", payment_status="paid"
            )
            await session.commit()
            return inv.id

    inv_id = _run(seed())
    headers = _run(admin_headers(async_session_factory))
    r = client.post(f"{WH}/invoices/{inv_id}/pay-kassa", json={"amount": None}, headers=headers)
    assert r.status_code == 409
    assert "оплачена" in r.json()["detail"].lower()


def test_pay_kassa_splits_staff_to_own_article(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    """Касса-оплата накладной с «тратами на персонал»: ДДС получает ДВЕ статьи (товар +
    персонал), а изъятие поставщику в iiko — ТОЛЬКО за товарную часть (персонал «не в iiko»).
    Регресс: раньше весь итог шёл одной статьёй «Оплата поставщикам» и весь в iiko-изъятие."""
    calls: list = []
    _mock_iiko(monkeypatch, calls)

    async def seed():
        async with async_session_factory() as session:
            await make_expense_article(
                session, code="payment_to_supplier", name="Оплата поставщикам"
            )
            await make_expense_article(
                session, code="supplier_staff_payment", name="Оплата поставщику (персонал)"
            )
            # Кошелёк ТК Черникова засеян миграцией 0115 — используем существующий.
            cp = await make_counterparty(
                session, name="ООО Сплит", inn="7706660000", iiko_guid="G-SPLIT"
            )
            inv = await make_invoice(
                session,
                counterparty_id=cp.id,
                amount="399.00",
                staff_amount="199.00",
                number="SP-1",
            )
            await session.commit()
            return inv.id

    inv_id = _run(seed())
    headers = _run(admin_headers(async_session_factory))
    r = client.post(f"{WH}/invoices/{inv_id}/pay-kassa", json={"amount": None}, headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["payment_status"] == "paid"
    assert data["production_amount"] == 200.0
    assert len(data["allocations"]) == 2  # товар + персонал на РАЗНЫХ статьях ДДС
    # iiko-изъятия теперь идут ПО СТАТЬЯМ из нормализованных строк накладной
    # (post_kassa_payment_to_iiko) — покрыто в test_kassa_cheque_payout. Здесь накладная без
    # invoice_line_item, поэтому iiko-проводок нет: проверяем только ДДС-сплит товар/персонал.


def test_kassa_invoice_source_filter(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """source='kassa_invoice' принимается enum-ом, фильтр list_invoices его выделяет."""

    async def scenario():
        async with async_session_factory() as session:
            cp = await make_counterparty(session, name="ООО Кассо-Ист", inn="7705559999")
            await make_invoice(
                session,
                counterparty_id=cp.id,
                amount="100.00",
                source="kassa_invoice",
                number="K-1",
            )
            await make_invoice(
                session, counterparty_id=cp.id, amount="200.00", source="manual", number="M-1"
            )
            await session.commit()
            kassa = await list_invoices(
                session, source="kassa_invoice", direction="payable", counterparty_id=cp.id
            )
            both = await list_invoices(session, direction="payable", counterparty_id=cp.id)
            return sorted(i.number for i in kassa), sorted(i.number for i in both)

    kassa_nums, both_nums = _run(scenario())
    assert kassa_nums == ["K-1"]  # вкладка Кассы — только kassa_invoice
    assert both_nums == ["K-1", "M-1"]  # Склад — обе

"""iiko-проводка оплаты накладной (``invoice_paid_push``). iiko замокан (monkeypatch
имён в модуле). Прогон на ``teplo_test``."""

from __future__ import annotations

import asyncio

from cp_helpers import (
    admin_headers,
    make_counterparty,
    make_expense_article,
    make_invoice,
)
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.counterparty_registry import list_invoices
from app.services.kassa import cheque_payout_push as cpp

WH = "/api/v1/warehouse"


def _run(coro):
    return asyncio.run(coro)


def _mock_iiko(monkeypatch, calls: list, *, success: bool = True) -> None:
    """Защитный мок iiko-проводок pay-kassa: реальные изъятия идёт ``post_kassa_payment_to_iiko``
    (модуль ``cheque_payout_push``). Товарную часть теперь гасит add_payment джобом — здесь не
    проверяется."""
    monkeypatch.setattr(cpp, "_auth_token", lambda: "TOKEN")

    def fake_post(token, path, body):  # noqa: ANN001, ANN202
        calls.append((path, body))
        return {"result": "SUCCESS" if success else "ERROR"}

    monkeypatch.setattr(cpp, "_iiko_post", fake_post)


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

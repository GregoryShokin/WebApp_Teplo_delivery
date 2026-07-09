"""Пуш складской накладной в iiko через Cloud API: сборка тела (без сети) + оркестрация
``push_invoice_to_iiko`` с замоканным сетевым слоем.

Живой ``create``/``post`` тут не дёргаем (создаёт реальный документ). Проверяем детерминированное:
форму Cloud-тела по направлению, исключение «персонал»-строк, причины пропуска, машину статусов
(pushed/failed/skipped), гейт идемпотентности и re-post созданного-но-не-проведённого документа.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from cp_helpers import make_counterparty
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import InvoiceLineItem, SupplierInvoice
from app.services import warehouse_invoice_push as wip
from app.services.iiko_invoice_cloud import build_invoice_body
from app.services.warehouse_invoice_push import (
    _CloudPushOutcome,
    prepare_push,
    propagate_invoice_edit_to_iiko,
    push_invoice_to_iiko,
)

ISSUED = datetime(2026, 6, 15, 14, 30, tzinfo=UTC)

# Поля ответа get, которые create/update отвергают — не должны попадать в тело.
READ_ONLY_FIELDS = {"status", "productArticle", "priceWithoutVat", "sumWithoutVat", "producer"}


async def _invoice_with_lines(
    session: AsyncSession, cp_id, *, store_guid: str | None, staff_second: bool
) -> SupplierInvoice:
    invoice = SupplierInvoice(
        counterparty_id=cp_id,
        source="manual",
        direction="payable",
        number="W-10",
        amount=Decimal("1500.00"),
        payment_status="unpaid",
        issued_at=ISSUED,
        store_guid=store_guid,
    )
    session.add(invoice)
    await session.flush()
    session.add(
        InvoiceLineItem(
            invoice_id=invoice.id, product_guid="PROD-A", name="Лосось",
            quantity=Decimal("2"), price=Decimal("500"), sum=Decimal("1000"),
            is_staff=False, sort_order=0,
        )
    )
    session.add(
        InvoiceLineItem(
            invoice_id=invoice.id, product_guid="PROD-B", name="Печенье",
            quantity=Decimal("1"), price=Decimal("500"), sum=Decimal("500"),
            is_staff=staff_second, sort_order=1,
        )
    )
    await session.commit()
    return invoice


# ── prepare_push → Cloud-тело (без сети) ─────────────────────────────────────────────────────────


async def test_prepare_push_builds_incoming_cloud_body(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Поставщик", inn="7710000050", iiko_guid="SUP-GUID-1"
        )
        invoice = await _invoice_with_lines(session, cp.id, store_guid="ST-1", staff_second=True)

        prepared = await prepare_push(session, invoice)
        assert prepared.skip_reason is None
        assert prepared.doc is not None
        assert prepared.doc.direction == "payable"
        assert prepared.doc.counteragent == "SUP-GUID-1"
        # только товарная строка; персонал-строка исключена
        assert [line.product for line in prepared.doc.lines] == ["PROD-A"]
        line = prepared.doc.lines[0]
        assert line.amount == 2.0 and line.price == 500.0 and line.sum == 1000.0

        body = build_invoice_body(prepared.doc)
        assert body["counteragent"] == "SUP-GUID-1"      # Cloud-поле партнёра (не <supplier>)
        assert body["defaultStore"] == "ST-1"
        assert body["date"].endswith("+03:00") and "." in body["date"]   # ISO с точкой
        assert body["incomingDate"].endswith("+03:00")
        assert [it["product"] for it in body["items"]] == ["PROD-A"]
        # read-only полей нет
        assert READ_ONLY_FIELDS.isdisjoint(body.keys())
        assert READ_ONLY_FIELDS.isdisjoint(body["items"][0].keys())


async def test_prepare_push_skips_without_iiko_guid(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Без GUID", inn="7710000051")
        invoice = await _invoice_with_lines(session, cp.id, store_guid="ST-1", staff_second=False)

        prepared = await prepare_push(session, invoice)
        assert prepared.doc is None
        assert prepared.skip_reason is not None and "GUID" in prepared.skip_reason


async def test_prepare_push_skips_without_store(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Поставщик2", inn="7710000052", iiko_guid="SUP-GUID-2"
        )
        invoice = await _invoice_with_lines(session, cp.id, store_guid=None, staff_second=False)

        prepared = await prepare_push(session, invoice)
        assert prepared.doc is None
        assert prepared.skip_reason is not None and "склад" in prepared.skip_reason


async def test_prepare_push_skips_all_staff(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Накладная целиком из персонал-строк → в iiko уходить нечему → skip."""
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Поставщик3", inn="7710000053", iiko_guid="SUP-GUID-3"
        )
        invoice = SupplierInvoice(
            counterparty_id=cp.id, source="manual", direction="payable", number="W-11",
            amount=Decimal("500.00"), payment_status="unpaid", issued_at=ISSUED, store_guid="ST-1",
        )
        session.add(invoice)
        await session.flush()
        session.add(
            InvoiceLineItem(
                invoice_id=invoice.id, product_guid="PROD-S", name="Печенье",
                quantity=Decimal("1"), price=Decimal("500"), sum=Decimal("500"),
                is_staff=True, sort_order=0,
            )
        )
        await session.commit()

        prepared = await prepare_push(session, invoice)
        assert prepared.doc is None
        assert prepared.skip_reason is not None


# ── push_invoice_to_iiko: оркестрация (сетевой слой замокан) ─────────────────────────────────────


def _patch_cloud(monkeypatch, outcome: _CloudPushOutcome) -> list[dict]:
    """Замокать сетевой ``_cloud_create_and_post``; вернуть список зафиксированных вызовов."""
    calls: list[dict] = []

    def fake(direction, organization_id, body, *, existing_document_id):
        calls.append(
            {"direction": direction, "org": organization_id, "body": body,
             "existing_document_id": existing_document_id}
        )
        return outcome

    monkeypatch.setattr(wip, "_cloud_create_and_post", fake)
    return calls


async def test_push_success_sets_external_id_and_pushed(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Поставщик", inn="7710000060", iiko_guid="SUP-GUID-1"
        )
        invoice = await _invoice_with_lines(session, cp.id, store_guid="ST-1", staff_second=False)
        calls = _patch_cloud(
            monkeypatch, _CloudPushOutcome("IIKO-DOC-1", posted=True, created=True)
        )

        result = await push_invoice_to_iiko(session, invoice.id)
        assert result.iiko_push_status == "pushed"
        assert result.external_id == "IIKO-DOC-1"
        assert result.iiko_pushed_at is not None
        assert result.iiko_push_error is None
        # create-путь: без существующего documentId
        assert len(calls) == 1 and calls[0]["existing_document_id"] is None


async def test_push_idempotent_when_already_pushed(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Поставщик", inn="7710000061", iiko_guid="SUP-GUID-1"
        )
        invoice = await _invoice_with_lines(session, cp.id, store_guid="ST-1", staff_second=False)
        invoice.external_id = "IIKO-DOC-9"
        invoice.iiko_push_status = "pushed"
        await session.commit()
        calls = _patch_cloud(monkeypatch, _CloudPushOutcome("X", posted=True))

        result = await push_invoice_to_iiko(session, invoice.id)
        assert result.iiko_push_status == "pushed"
        assert result.external_id == "IIKO-DOC-9"
        assert calls == []  # сеть не дёргали


async def test_push_reposts_created_but_not_posted(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Документ создан, но прошлый post упал (external_id есть, failed) → повторный пуш только
    ПРОВОДИТ его (create не повторяем)."""
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Поставщик", inn="7710000062", iiko_guid="SUP-GUID-1"
        )
        invoice = await _invoice_with_lines(session, cp.id, store_guid="ST-1", staff_second=False)
        invoice.external_id = "IIKO-DOC-7"
        invoice.iiko_push_status = "failed"
        await session.commit()
        calls = _patch_cloud(
            monkeypatch, _CloudPushOutcome("IIKO-DOC-7", posted=True, created=False)
        )

        result = await push_invoice_to_iiko(session, invoice.id)
        assert result.iiko_push_status == "pushed"
        assert len(calls) == 1
        assert calls[0]["existing_document_id"] == "IIKO-DOC-7"  # create пропущен


async def test_push_records_business_error_and_keeps_external_id(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Поставщик", inn="7710000063", iiko_guid="SUP-GUID-1"
        )
        invoice = await _invoice_with_lines(session, cp.id, store_guid="ST-1", staff_second=False)
        _patch_cloud(
            monkeypatch,
            _CloudPushOutcome(
                "IIKO-DOC-2", posted=False, created=True,
                error="Нельзя распровести документ т.к. по документу уже есть проводки оплаты",
            ),
        )

        result = await push_invoice_to_iiko(session, invoice.id)
        assert result.iiko_push_status == "failed"
        assert "проводки оплаты" in result.iiko_push_error
        # документ создан → id сохранён, чтобы повторно не создавать (только re-post)
        assert result.external_id == "IIKO-DOC-2"


async def test_push_skips_without_iiko_guid(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Без GUID", inn="7710000064")
        invoice = await _invoice_with_lines(session, cp.id, store_guid="ST-1", staff_second=False)
        calls = _patch_cloud(monkeypatch, _CloudPushOutcome("X", posted=True))

        result = await push_invoice_to_iiko(session, invoice.id)
        assert result.iiko_push_status == "skipped"
        assert calls == []


# ── propagate_invoice_edit_to_iiko: проброс правки (сетевой слой замокан) ─────────────────────────


def _patch_cloud_update(monkeypatch, outcome: _CloudPushOutcome) -> list[dict]:
    calls: list[dict] = []

    def fake(direction, organization_id, body, *, document_id):
        calls.append(
            {"direction": direction, "org": organization_id, "body": body,
             "document_id": document_id}
        )
        return outcome

    monkeypatch.setattr(wip, "_cloud_update_and_post", fake)
    return calls


async def test_propagate_edit_success(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Поставщик", inn="7710000070", iiko_guid="SUP-GUID-1"
        )
        invoice = await _invoice_with_lines(session, cp.id, store_guid="ST-1", staff_second=False)
        invoice.external_id = "IIKO-DOC-5"
        invoice.iiko_push_status = "pushed"
        await session.commit()
        calls = _patch_cloud_update(monkeypatch, _CloudPushOutcome("IIKO-DOC-5", posted=True))

        result = await propagate_invoice_edit_to_iiko(session, invoice.id)
        assert result.iiko_push_status == "pushed"
        assert result.iiko_push_error is None
        assert len(calls) == 1
        assert calls[0]["document_id"] == "IIKO-DOC-5"
        assert calls[0]["body"]["documentId"] == "IIKO-DOC-5"  # тело update несёт documentId


async def test_propagate_noop_without_external_id(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Поставщик", inn="7710000071", iiko_guid="SUP-GUID-1"
        )
        invoice = await _invoice_with_lines(session, cp.id, store_guid="ST-1", staff_second=False)
        calls = _patch_cloud_update(monkeypatch, _CloudPushOutcome("X", posted=True))

        result = await propagate_invoice_edit_to_iiko(session, invoice.id)
        assert calls == []  # ещё не в iiko → сеть не дёргаем
        assert result.external_id is None


async def test_propagate_records_business_error(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Поставщик", inn="7710000072", iiko_guid="SUP-GUID-1"
        )
        invoice = await _invoice_with_lines(session, cp.id, store_guid="ST-1", staff_second=False)
        invoice.external_id = "IIKO-DOC-6"
        invoice.iiko_push_status = "pushed"
        await session.commit()
        _patch_cloud_update(
            monkeypatch, _CloudPushOutcome("IIKO-DOC-6", posted=False, error="post HTTP 409")
        )

        result = await propagate_invoice_edit_to_iiko(session, invoice.id)
        assert result.iiko_push_status == "failed"
        assert "409" in result.iiko_push_error

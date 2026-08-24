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
    _line_amounts_for_iiko,
    delete_invoice_in_iiko,
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
        # 14:30 UTC = 17:30 МСК; шлём 20:30+03, потому что Cloud сам отнимет три часа.
        assert body["date"] == "2026-06-15T20:30:00.000+03:00"
        assert body["incomingDate"] == "2026-06-15T15:00:00.000+03:00"
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


# ── цена строки под инвариант iiko «sum == price * amount» ───────────────────────────────────────


@pytest.mark.parametrize(
    ("quantity", "price", "line_sum", "expected_price", "expected_sum"),
    [
        # Ввод через СУММУ: 840 ÷ 2,9 = 289,655172…, в базе цена округлена до 289,66 —
        # реальная строка «Шампиньоны» накладной №515256.
        ("2.9", "289.66", "840.00", 289.655172, 839.9999988),
        ("3", "33.33", "100.00", 33.333333, 99.999999),
        ("20", "88.46", "1769.28", 88.464, 1769.28),
        # Хвост двоичного умножения — его и ждёт iiko (строка «Ванилин» чека Ч-72).
        ("0.051", "4393.33", "224.06", 4393.333333, 224.05999998299995),
        # В Decimal равенство точное, но во float произведение отличается — «Курица» чека Ч-80.
        ("9.56", "499.00", "4770.44", 499.0, 4770.4400000000005),
        # Округляется в ту же копейку, но точного равенства нет — iiko отвергнет, правим.
        ("2.9", "289.66", "840.01", 289.658621, 840.0100008999999),
        # Равенство строгое — строка уходит нетронутой (подавляющее большинство).
        ("10", "120.00", "1200.00", 120.0, 1200.0),
        ("0.7", "400.00", "280.00", 400.0, 280.0),
        # Делить не на что — отдаём как есть.
        ("0", "120.00", "0.00", 120.0, 0.0),
    ],
)
def test_line_amounts_for_iiko(
    quantity: str, price: str, line_sum: str, expected_price: float, expected_sum: float
) -> None:
    got_price, got_sum = _line_amounts_for_iiko(
        Decimal(quantity), Decimal(price), Decimal(line_sum)
    )
    assert got_price == expected_price
    assert got_sum == expected_sum
    if Decimal(quantity) > 0:
        assert got_price is not None
        assert got_sum is not None
        # Каждая строка должна сходиться в ТОЙ ЖЕ арифметике, что у iiko — даже когда исходные
        # Decimal сходились и цена осталась прежней (9,56 × 499,00 из чека Ч-80).
        assert got_price * float(Decimal(quantity)) == got_sum
    if got_price != float(Decimal(price)):
        # От эталона кассира уходим меньше чем на полкопейки.
        assert abs(Decimal(str(got_sum)) - Decimal(line_sum)) < Decimal("0.005")


def test_line_amounts_for_iiko_keeps_line_without_sum() -> None:
    """Строка без эталонной суммы (старые данные) — прежнее поведение, цена из базы."""
    assert _line_amounts_for_iiko(Decimal("2.9"), Decimal("289.66"), None) == (289.66, None)


async def test_prepare_push_price_matches_line_sum(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Каждая строка тела удовлетворяет ``sum == price * amount`` СТРОГО.

    Без этого iiko отвергает ВЕСЬ документ («sum must be equal to price * amount in item» —
    прод 06.08.2026, накладная №515256).
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Поставка овощей", inn="7710000054", iiko_guid="SUP-GUID-4"
        )
        invoice = SupplierInvoice(
            counterparty_id=cp.id, source="manual", direction="payable", number="515256",
            amount=Decimal("2040.00"), payment_status="unpaid", issued_at=ISSUED, store_guid="ST-1",
        )
        session.add(invoice)
        await session.flush()
        session.add(
            InvoiceLineItem(
                invoice_id=invoice.id, product_guid="PROD-CHAMP", name="Шампиньоны",
                quantity=Decimal("2.9"), price=Decimal("289.66"), sum=Decimal("840.00"),
                is_staff=False, sort_order=0,
            )
        )
        session.add(
            InvoiceLineItem(
                invoice_id=invoice.id, product_guid="PROD-CUC", name="Огурцы",
                quantity=Decimal("10"), price=Decimal("120.00"), sum=Decimal("1200.00"),
                is_staff=False, sort_order=1,
            )
        )
        await session.commit()

        prepared = await prepare_push(session, invoice)
        assert prepared.doc is not None
        champignons, cucumbers = prepared.doc.lines
        # цена восстановлена из эталонной суммы, сумма — точное произведение
        assert champignons.price == 289.655172 and champignons.sum == 839.9999988
        # обычная строка уходит как была
        assert cucumbers.price == 120.0 and cucumbers.sum == 1200.0

        items = build_invoice_body(prepared.doc)["items"]
        for item in items:
            assert item["price"] * item["amount"] == item["sum"]
        # итог документа отходит от нашего меньше чем на копейку
        assert abs(sum(Decimal(str(it["sum"])) for it in items) - Decimal("2040.00")) < Decimal(
            "0.01"
        )


# ── push_invoice_to_iiko: оркестрация (сетевой слой замокан) ─────────────────────────────────────


def _patch_cloud(monkeypatch, outcome: _CloudPushOutcome) -> list[dict]:
    """Замокать сетевой ``_cloud_create_and_post``; вернуть список зафиксированных вызовов."""
    calls: list[dict] = []

    def fake(direction, organization_id, body):
        calls.append({"direction": direction, "org": organization_id, "body": body})
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
        # create-путь: документа в iiko ещё нет
        assert len(calls) == 1


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


async def test_push_syncs_existing_document_via_update(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Документ в iiko уже есть (external_id, failed) → повтор идёт update→post: create не
    повторяем (дубль), а голый post не донёс бы локальную правку — например смену поставщика."""
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Поставщик", inn="7710000062", iiko_guid="SUP-GUID-NEW"
        )
        invoice = await _invoice_with_lines(session, cp.id, store_guid="ST-1", staff_second=False)
        invoice.external_id = "IIKO-DOC-7"
        invoice.iiko_push_status = "failed"
        await session.commit()
        create_calls = _patch_cloud(
            monkeypatch, _CloudPushOutcome("IIKO-DOC-7", posted=True, created=True)
        )
        update_calls = _patch_cloud_update(
            monkeypatch, _CloudPushOutcome("IIKO-DOC-7", posted=True)
        )

        result = await push_invoice_to_iiko(session, invoice.id)
        assert result.iiko_push_status == "pushed"
        assert create_calls == []
        assert len(update_calls) == 1
        assert update_calls[0]["document_id"] == "IIKO-DOC-7"
        # тело несёт актуального поставщика — ровно то, чего не делал прежний голый post
        assert update_calls[0]["body"]["counteragent"] == "SUP-GUID-NEW"


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


def test_post_document_accepts_already_processed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Прошлый post дошёл до iiko, а ответ до нас — нет: повтор ловит «status mismatch», но
    документ уже PROCESSED → это успех, иначе накладная залипла бы в failed навсегда."""
    monkeypatch.setattr(
        wip, "post_invoice",
        lambda *a, **kw: (500, {"message": "document status mismatch: expected NEW, got PROCESSED"}),
    )
    monkeypatch.setattr(wip, "get_invoice", lambda *a, **kw: (200, {"status": "PROCESSED"}))

    assert wip._post_document("payable", "ORG", "DOC", token="t", opener=None) is None


def test_post_document_keeps_mismatch_when_not_processed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Тот же mismatch, но документ НЕ проведён (например, отменён) → это настоящая ошибка."""
    monkeypatch.setattr(
        wip, "post_invoice",
        lambda *a, **kw: (500, {"message": "document status mismatch: expected NEW, got DELETED"}),
    )
    monkeypatch.setattr(wip, "get_invoice", lambda *a, **kw: (200, {"status": "DELETED"}))

    error = wip._post_document("payable", "ORG", "DOC", token="t", opener=None)
    assert error is not None and "status mismatch" in error


# ── лимит частоты Cloud (TOO_MANY_REQUESTS / 429) ───────────────────────────────────────────────


def _no_pauses(monkeypatch: pytest.MonkeyPatch, attempts: int = 3) -> None:
    """Паузы бэкоффа в ноль: число попыток то же, ждать в тесте нечего."""
    monkeypatch.setattr(wip, "_RATE_LIMIT_RETRY_DELAYS", (0.0,) * attempts)


def _patch_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wip, "iiko_opener", lambda: None)
    monkeypatch.setattr(wip, "iiko_auth_token", lambda opener: "TOKEN")


_RATE_LIMITED = (429, {"error": "TOO_MANY_REQUESTS"})
_CREATE_BODY = {"number": "W-10", "date": "2026-06-15T14:30:00.000+03:00"}


def test_probe_key_uses_business_day_when_timezone_compensation_crosses_midnight() -> None:
    # Желаемые 23:30 МСК кодируются как 02:30+03 следующих суток; iiko после своего −03
    # положит документ обратно на 24-е, и list должен искать именно там.
    assert wip._probe_key(
        {"number": "W-11", "date": "2026-08-25T02:30:00.000+03:00"}
    ) == ("W-11", "2026-08-24")


def test_post_document_accepts_processed_after_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429 на post не значит «не проведён»: iiko могла применить проводку и всё равно ответить
    отказом (прод 27.07). Спрашиваем факт — PROCESSED → успех, а не вечный failed."""
    _no_pauses(monkeypatch, attempts=1)
    monkeypatch.setattr(wip, "post_invoice", lambda *a, **kw: _RATE_LIMITED)
    monkeypatch.setattr(wip, "get_invoice", lambda *a, **kw: (200, {"status": "PROCESSED"}))

    assert wip._post_document("payable", "ORG", "DOC", token="t", opener=None) is None


def test_post_document_retries_rate_limit_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_pauses(monkeypatch)
    answers = iter([_RATE_LIMITED, _RATE_LIMITED, (200, {})])
    monkeypatch.setattr(wip, "post_invoice", lambda *a, **kw: next(answers))
    monkeypatch.setattr(wip, "get_invoice", lambda *a, **kw: (200, {"status": "NEW"}))

    assert wip._post_document("payable", "ORG", "DOC", token="t", opener=None) is None


def test_post_document_reports_rate_limit_in_human_words(monkeypatch: pytest.MonkeyPatch) -> None:
    """Попытки исчерпаны, документ так и не проведён → в накладную садится понятный текст,
    а не сырое TOO_MANY_REQUESTS."""
    _no_pauses(monkeypatch)
    monkeypatch.setattr(wip, "post_invoice", lambda *a, **kw: _RATE_LIMITED)
    monkeypatch.setattr(wip, "get_invoice", lambda *a, **kw: (200, {"status": "NEW"}))

    error = wip._post_document("payable", "ORG", "DOC", token="t", opener=None)
    assert error is not None and "частот" in error


def test_create_after_rate_limit_adopts_existing_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Отказ по лимиту НЕ доказывает, что документ не создан. Прежде чем повторять create, ищем
    его в iiko по номеру: нашли непроведённый — берём тот же id и просто проводим, второй
    реальный документ не создаём."""
    _no_pauses(monkeypatch)
    _patch_transport(monkeypatch)
    creates: list[dict] = []

    def fake_create(direction, body, *, token, opener):
        creates.append(body)
        return _RATE_LIMITED

    monkeypatch.setattr(wip, "create_invoice", fake_create)
    monkeypatch.setattr(
        wip, "list_invoices",
        lambda *a, **kw: (200, [{"number": "W-10", "documentId": "DOC-1", "processed": False}]),
    )
    monkeypatch.setattr(wip, "post_invoice", lambda *a, **kw: (200, {}))

    outcome = wip._cloud_create_and_post("payable", "ORG", _CREATE_BODY)
    assert outcome.document_id == "DOC-1"
    assert outcome.posted is True
    assert len(creates) == 1  # повтора create не было — иначе дубль документа в iiko


def test_create_after_rate_limit_accepts_already_processed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Тот же поиск нашёл документ УЖЕ проведённым — цель достигнута, post не нужен."""
    _no_pauses(monkeypatch)
    _patch_transport(monkeypatch)
    monkeypatch.setattr(wip, "create_invoice", lambda *a, **kw: _RATE_LIMITED)
    monkeypatch.setattr(
        wip, "list_invoices",
        lambda *a, **kw: (200, [{"number": "W-10", "documentId": "DOC-2", "processed": True}]),
    )
    posts: list[str] = []
    monkeypatch.setattr(wip, "post_invoice", lambda *a, **kw: posts.append("x") or (200, {}))

    outcome = wip._cloud_create_and_post("payable", "ORG", _CREATE_BODY)
    assert (outcome.document_id, outcome.posted, outcome.error) == ("DOC-2", True, None)
    assert posts == []


def test_create_retries_rate_limit_when_document_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Документа с нашим номером в iiko нет → create не дошёл, повтор безопасен."""
    _no_pauses(monkeypatch)
    _patch_transport(monkeypatch)
    answers = iter([_RATE_LIMITED, (200, {"documentId": "DOC-3"})])
    monkeypatch.setattr(wip, "create_invoice", lambda *a, **kw: next(answers))
    monkeypatch.setattr(wip, "list_invoices", lambda *a, **kw: (200, []))
    monkeypatch.setattr(wip, "post_invoice", lambda *a, **kw: (200, {}))

    outcome = wip._cloud_create_and_post("payable", "ORG", _CREATE_BODY)
    assert (outcome.document_id, outcome.posted, outcome.created) == ("DOC-3", True, True)


def test_create_gives_up_on_rate_limit_with_human_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_pauses(monkeypatch)
    _patch_transport(monkeypatch)
    monkeypatch.setattr(wip, "create_invoice", lambda *a, **kw: _RATE_LIMITED)
    monkeypatch.setattr(wip, "list_invoices", lambda *a, **kw: (200, []))

    outcome = wip._cloud_create_and_post("payable", "ORG", _CREATE_BODY)
    assert outcome.document_id is None
    assert outcome.error is not None and "частот" in outcome.error


def test_create_does_not_retry_without_number(monkeypatch: pytest.MonkeyPatch) -> None:
    """Номера нет (iiko нумерует сама) → найти свой документ нечем, значит и повторять create
    нельзя: рискуем задвоить реальный документ."""
    _no_pauses(monkeypatch)
    _patch_transport(monkeypatch)
    creates: list[int] = []
    monkeypatch.setattr(
        wip, "create_invoice", lambda *a, **kw: creates.append(1) or _RATE_LIMITED
    )
    listed: list[int] = []
    monkeypatch.setattr(wip, "list_invoices", lambda *a, **kw: listed.append(1) or (200, []))

    outcome = wip._cloud_create_and_post(
        "payable", "ORG", {"date": "2026-06-15T14:30:00.000+03:00"}
    )
    assert outcome.document_id is None and outcome.error is not None
    assert len(creates) == 1 and listed == []


def test_create_reports_business_error_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Отказ по существу (не лимит) повторять бессмысленно — отдаём текст iiko как есть."""
    _no_pauses(monkeypatch)
    _patch_transport(monkeypatch)
    creates: list[int] = []
    monkeypatch.setattr(
        wip, "create_invoice",
        lambda *a, **kw: creates.append(1) or (500, {"message": "Склад не найден"}),
    )

    outcome = wip._cloud_create_and_post("payable", "ORG", _CREATE_BODY)
    assert outcome.error == "Склад не найден"
    assert len(creates) == 1


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


# ── delete_invoice_in_iiko: двустороннее удаление (сетевой слой замокан) ─────────────────────────


async def test_delete_in_iiko_noop_without_external_id(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Поставщик", inn="7710000080", iiko_guid="SUP-GUID-1"
        )
        invoice = await _invoice_with_lines(session, cp.id, store_guid="ST-1", staff_second=False)
        called: list[str] = []
        monkeypatch.setattr(
            wip, "_cloud_delete_document", lambda *a, **k: called.append("x") or None
        )

        assert await delete_invoice_in_iiko(invoice) is None
        assert called == []  # не в iiko → сеть не дёргаем


async def test_delete_in_iiko_returns_error_text(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Поставщик", inn="7710000081", iiko_guid="SUP-GUID-1"
        )
        invoice = await _invoice_with_lines(session, cp.id, store_guid="ST-1", staff_second=False)
        invoice.external_id = "IIKO-DOC-D"
        await session.commit()
        monkeypatch.setattr(
            wip, "_cloud_delete_document",
            lambda *a, **k: "Нельзя распровести: по документу уже есть проводки оплаты",
        )

        error = await delete_invoice_in_iiko(invoice)
        assert error is not None and "проводки оплаты" in error


async def test_prepare_push_sends_moscow_wall_clock_and_midday_incoming(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """В iiko уходят московские цифры, а дата прихода — полднем, а не полуночью.

    Прод-баг 20.08.2026: iiko кладёт наши документы на три часа раньше присланного (его
    собственный ``dateCreated`` совпадает с нашим ``created_at`` до секунды, а присланный нами
    ``date`` возвращается на три часа раньше). Полночь минус три часа = предыдущий день —
    так документы и оказывались в iiko вчерашним числом. Полдень тот же сдвиг переживает.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Поставщик", inn="7710000051", iiko_guid="SUP-GUID-2"
        )
        invoice = await _invoice_with_lines(session, cp.id, store_guid="ST-1", staff_second=False)
        # 15:16 МСК = 12:16 UTC: в базе инстант, в iiko должны уйти московские цифры.
        invoice.issued_at = datetime(2026, 8, 20, 12, 16, tzinfo=UTC)
        await session.commit()

        prepared = await prepare_push(session, invoice)

        assert prepared.doc is not None
        assert prepared.doc.date == datetime(2026, 8, 20, 15, 16)  # МСК, без зоны
        assert prepared.doc.incoming_date == datetime(2026, 8, 20, 12, 0)  # полдень тех же суток

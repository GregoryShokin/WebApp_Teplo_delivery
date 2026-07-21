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
from cp_helpers import make_counterparty, make_expense_article, make_invoice, make_wallet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.services.counterparty_iiko_payment as mod
from app.models import (
    CashflowTransaction,
    ChequeIikoPayout,
    IikoInvoicePaymentPush,
    InvoiceLineItem,
    InvoicePaymentAllocation,
    ReconciliationCase,
    SupplierInvoice,
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


@pytest.mark.asyncio
async def test_mirror_bank_paid_kassa_goes_to_acquiring(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Оплата kassa-накладной банк-черновиком: cash-аллокация с БАНКОВСКОГО кошелька зеркалится
    на эквайринг, а НЕ на Главную кассу (баг накладной Карпова №4-2 — оплата с р/с ушла на кассу).
    Различие — по реальному типу кошелька cashflow, а не по source_kind аллокации."""
    ext = f"doc-{uuid.uuid4()}"
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик-банк", inn="7712345699")
        art = await make_expense_article(session)  # «Оплата поставщикам»
        bank_wallet = await make_wallet(
            session, name="Тинькофф · Рублёвый счёт", wallet_type="bank"
        )
        inv = await make_invoice(
            session, counterparty_id=cp.id, amount=Decimal("500.00"),
            source="kassa_invoice", external_id=ext, payment_status="paid", issued_at=ISSUED,
        )
        session.add(
            InvoiceLineItem(
                invoice_id=inv.id, name="Товар", quantity=Decimal("1"), price=Decimal("500"),
                sum=Decimal("500"), product_guid="prod-bank", dds_article_id=art.id, is_staff=False,
            )
        )
        cf = CashflowTransaction(
            wallet_id=bank_wallet.id, direction="out", amount=Decimal("500.00"),
            operation_date=ISSUED.date(), article_id=art.id,
            source_kind="counterparty_payment", source_id=inv.id, quality_status="final",
        )
        session.add(cf)
        await session.flush()
        session.add(
            InvoicePaymentAllocation(
                invoice_id=inv.id, source_kind="cash",
                cashflow_transaction_id=cf.id, amount=Decimal("500.00"),
            )
        )
        await session.commit()

    calls: list[dict] = []
    monkeypatch.setattr(mod, "_call_add_payment", _fake_ok(calls))
    async with async_session_factory() as session:
        result = await mod.mirror_paid_kassa_invoices(session)

    assert result["ok"] == 1, result
    assert len(calls) == 1
    assert calls[0]["accountId"] == ACQUIRING  # эквайринг, НЕ Главная касса (8ccc8f0f)
    assert calls[0]["amount"] == 500.0


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


@pytest.mark.asyncio
async def test_mirror_excludes_corrected_kassa_invoice(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Скорректированную kassa-накладную (iiko_correction_new_external_id задан → external_id уже =
    новая приходная Y) джоб НЕ зеркалит: зачёт add_payment на Y завышает баланс поставщика. Иначе
    правка оплаченной kassa-накладной привела бы к тому же перекосу, что АЛЬЯНС ЮГ."""
    inv_id = await _seed_kassa(async_session_factory)
    async with async_session_factory() as session:
        inv = await session.get(SupplierInvoice, inv_id)
        inv.iiko_correction_new_external_id = "NEW-Y"
        await session.commit()
    calls: list[dict] = []
    monkeypatch.setattr(mod, "_call_add_payment", _fake_ok(calls))
    async with async_session_factory() as session:
        result = await mod.mirror_paid_kassa_invoices(session)
    assert calls == []  # исключена фильтром — ни одной оплаты на Y
    assert result["eligible"] == 0
    rows = await _push_rows(async_session_factory, inv_id)
    assert not any(r.idempotency_key == f"kassa_goods_done:{inv_id}" for r in rows)


def test_amount_iiko_representable_matches_iiko_validator() -> None:
    """iiko бракует ('invalid amount in JSON') суммы, чьи копейки не целые в double — напр.
    4213.44 → 421343.99999999994 (реальный прод-кейс). Целые и «удачные» дроби проходят."""
    f = mod._amount_iiko_representable
    assert f(Decimal("4213.44")) is False  # упавшая на проде накладная №9
    assert f(Decimal("0.07")) is False
    for ok in ("959.88", "1778.04", "1642.65", "2705.96", "4213", "100.10", "500.00"):
        assert f(Decimal(ok)) is True, ok


@pytest.mark.asyncio
async def test_mirror_splits_unrepresentable_amount_without_manual_case(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """«Несчастливая» сумма 4213.44 дробится на представимые 4213.00 + 0.44 и полностью
    проводится в iiko без owner-review; итоговая summary-строка закрывает накладную как done."""
    inv_id = await _seed_kassa(
        async_session_factory, source="kassa_invoice", goods="4213.44", card="0.00", cash="4213.44"
    )
    calls: list[dict] = []
    monkeypatch.setattr(mod, "_call_add_payment", _fake_ok(calls))

    async with async_session_factory() as session:
        result = await mod.mirror_paid_kassa_invoices(session)

    assert result["ok"] == 1 and result["manual"] == 0, result
    amounts = sorted(call["amount"] for call in calls)
    assert amounts == [0.44, 4213.0]
    assert all((amount * 100).is_integer() for amount in amounts)

    rows = await _push_rows(async_session_factory, inv_id)
    by_key = {row.idempotency_key: row for row in rows}
    cash_key = f"kassa_goods:{inv_id}:cash"
    assert by_key[cash_key].status == "ok"
    assert by_key[cash_key].invoice_id == inv_id
    assert f"kassa_goods_done:{inv_id}" in {r.idempotency_key for r in rows}

    async with async_session_factory() as session:
        split_rows = (
            await session.scalars(
                select(IikoInvoicePaymentPush).where(
                    IikoInvoicePaymentPush.idempotency_key.like(f"{cash_key}#%")
                )
            )
        ).all()
    split_by_key = {row.idempotency_key: row for row in split_rows}
    assert split_by_key[f"{cash_key}#0"].status == "ok"
    assert split_by_key[f"{cash_key}#0"].invoice_id is None
    assert split_by_key[f"{cash_key}#1"].status == "ok"
    assert split_by_key[f"{cash_key}#1"].invoice_id is None

    async with async_session_factory() as session:
        cases = (
            await session.scalars(
                select(ReconciliationCase).where(
                    ReconciliationCase.kind == "iiko_payment_unsettled"
                )
            )
        ).all()
    assert cases == []


@pytest.mark.asyncio
async def test_mirror_permanent_reject_opens_case(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Перманентный отказ iiko (4xx) по представимой сумме → сразу кейс owner-review + done,
    без ожидания капа (раньше копился бы кап и тихо помечался «зеркалировано»)."""
    inv_id = await _seed_kassa(
        async_session_factory, source="kassa_invoice", goods="500.00", card="0.00", cash="500.00"
    )

    def _reject(payload: dict):
        return 400, {"message": "invalid amount in JSON"}

    monkeypatch.setattr(mod, "_call_add_payment", _reject)
    async with async_session_factory() as session:
        result = await mod.mirror_paid_kassa_invoices(session)

    assert result["manual"] == 1, result
    async with async_session_factory() as session:
        cases = (
            await session.scalars(
                select(ReconciliationCase).where(
                    ReconciliationCase.kind == "iiko_payment_unsettled"
                )
            )
        ).all()
    assert len(cases) == 1 and cases[0].payload["invoice_id"] == str(inv_id)
    rows = await _push_rows(async_session_factory, inv_id)
    assert f"kassa_goods_done:{inv_id}" in {r.idempotency_key for r in rows}


@pytest.mark.asyncio
async def test_mirror_transient_error_keeps_open_for_retry(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Временный сбой (5xx) → накладную НЕ закрываем и кейс НЕ заводим: вернёмся следующим
    проходом (отличие от перманентного отказа)."""
    inv_id = await _seed_kassa(
        async_session_factory, source="kassa_invoice", goods="500.00", card="0.00", cash="500.00"
    )

    def _flaky(payload: dict):
        return 503, {"message": "service unavailable"}

    monkeypatch.setattr(mod, "_call_add_payment", _flaky)
    async with async_session_factory() as session:
        result = await mod.mirror_paid_kassa_invoices(session)

    assert result["manual"] == 0
    rows = await _push_rows(async_session_factory, inv_id)
    assert f"kassa_goods_done:{inv_id}" not in {r.idempotency_key for r in rows}
    async with async_session_factory() as session:
        cases = (
            await session.scalars(
                select(ReconciliationCase).where(
                    ReconciliationCase.kind == "iiko_payment_unsettled"
                )
            )
        ).all()
    assert cases == []
    async with async_session_factory() as session:
        second = await mod.mirror_paid_kassa_invoices(session)
    assert second["eligible"] == 1  # снова в выборке (не исключена done-маркером)


@pytest.mark.asyncio
async def test_mirror_mixed_terminal_and_transient_opens_case_keeps_open(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Смешанная накладная: card-доля отклонена перманентно (4xx), cash-доля — временно (5xx).
    Кейс owner-review заводится СРАЗУ (не прячется за transient-долей), но накладную НЕ закрываем
    — вернёмся за cash-долей следующим проходом."""
    inv_id = await _seed_kassa(
        async_session_factory, source="kassa_invoice", goods="600.00", card="700.00", cash="300.00"
    )

    def _mixed(payload: dict):
        if payload["amount"] == 420.0:  # card_share — перманентный отказ
            return 400, {"message": "invalid amount in JSON"}
        return 503, {"message": "unavailable"}  # cash_share — временный сбой

    monkeypatch.setattr(mod, "_call_add_payment", _mixed)
    async with async_session_factory() as session:
        result = await mod.mirror_paid_kassa_invoices(session)

    async with async_session_factory() as session:
        cases = (
            await session.scalars(
                select(ReconciliationCase).where(
                    ReconciliationCase.kind == "iiko_payment_unsettled"
                )
            )
        ).all()
    assert len(cases) == 1 and cases[0].payload["invoice_id"] == str(inv_id)
    rows = await _push_rows(async_session_factory, inv_id)
    assert f"kassa_goods_done:{inv_id}" not in {r.idempotency_key for r in rows}  # НЕ закрыта
    assert result["manual"] == 0  # manual растёт только при закрытии done


@pytest.mark.asyncio
async def test_mirror_orphaned_pending_share_opens_case(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Осиротевшая pending-строка доли (in-flight упал между commit и финализацией): push её НЕ
    переотправляет (skipped/ok=False), но это не «улажено» — заводим кейс ручного разбора."""
    inv_id = await _seed_kassa(
        async_session_factory, source="kassa_invoice", goods="500.00", card="0.00", cash="500.00"
    )
    async with async_session_factory() as session:
        session.add(
            IikoInvoicePaymentPush(
                idempotency_key=f"kassa_goods:{inv_id}:cash",
                invoice_id=inv_id,
                external_id="x",
                amount=Decimal("500.00"),
                account_to=GLAVNAYA_KASSA,
                status="pending",
                attempts=1,
                request_payload={},
                response_payload={},
            )
        )
        await session.commit()

    calls: list[dict] = []
    monkeypatch.setattr(mod, "_call_add_payment", _fake_ok(calls))
    async with async_session_factory() as session:
        result = await mod.mirror_paid_kassa_invoices(session)

    assert calls == []  # pending не переотправляется реальным вызовом
    assert result["manual"] == 1
    async with async_session_factory() as session:
        cases = (
            await session.scalars(
                select(ReconciliationCase).where(
                    ReconciliationCase.kind == "iiko_payment_unsettled"
                )
            )
        ).all()
    assert len(cases) == 1 and cases[0].payload["invoice_id"] == str(inv_id)

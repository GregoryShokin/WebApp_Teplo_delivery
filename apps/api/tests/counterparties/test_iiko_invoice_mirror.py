"""Зеркалирование оплаты iiko-накладной в iiko (Cloud add_payment) — сверочный джоб.

HTTP к iiko замокан (``_call_add_payment``). Проверяем: пуш только для оплаченных через
банк-черновик iiko-накладных, верное тело (accountId=эквайринг, paymentDate с запятой,
amount), идемпотентность, пропуск не-iiko/без-черновика, запись ошибки + ретрай с капом.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from cp_helpers import make_counterparty, make_draft, make_invoice
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.services.counterparty_iiko_payment as mod
from app.models import IikoInvoicePaymentPush, ReconciliationCase

ACQUIRING = "3f261590-f208-2970-1300-95d2493a3c28"


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    *,
    source: str = "iiko",
    external_id: str | None = None,
    with_draft: bool = True,
    amount: str = "29168.99",
    draft_amount: str | None = None,
) -> uuid.UUID:
    external_id = external_id or f"doc-{uuid.uuid4()}"
    async with factory() as session:
        cp = await make_counterparty(session, name="Поставщик", inn="7700000000")
        draft_id = None
        if with_draft:
            draft = await make_draft(
                session, counterparty_id=cp.id, amount=draft_amount or amount, status="paid"
            )
            draft_id = draft.id
        inv = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount=amount,
            source=source,
            external_id=external_id,
            payment_status="paid",
            draft_id=draft_id,
        )
        await session.commit()
        return inv.id


async def _seed_multi_invoice_draft(
    factory: async_sessionmaker[AsyncSession],
) -> uuid.UUID:
    """Один банк-черновик на ДВЕ iiko-накладные — пер-накладная банк-доля не выводится из
    draft.amount, поэтому зеркало такие пропускает (ручная сверка)."""
    async with factory() as session:
        cp = await make_counterparty(session, name="Поставщик", inn="7700000000")
        draft = await make_draft(session, counterparty_id=cp.id, amount="3000.00", status="paid")
        for _ in range(2):
            await make_invoice(
                session,
                counterparty_id=cp.id,
                amount="1500.00",
                source="iiko",
                external_id=f"doc-{uuid.uuid4()}",
                payment_status="paid",
                draft_id=draft.id,
            )
        await session.commit()
        return draft.id


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


def _fake_ok(calls: list):
    def _f(payload: dict):
        calls.append(payload)
        return 201, {"accountingTransactionId": "t-1", "documentId": payload["documentId"]}

    return _f


def _fake_400(calls: list):
    def _f(payload: dict):
        calls.append(payload)
        return 400, {"message": "invalid"}

    return _f


async def test_mirror_pushes_bank_paid_iiko_invoice(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    invoice_id = await _seed(async_session_factory)
    calls: list = []
    monkeypatch.setattr(mod, "_call_add_payment", _fake_ok(calls))

    async with async_session_factory() as session:
        result = await mod.mirror_paid_iiko_invoices(session)

    assert result["ok"] == 1 and result["eligible"] == 1
    assert len(calls) == 1
    body = calls[0]
    assert body["accountId"] == ACQUIRING
    assert body["amount"] == 29168.99
    assert "," in body["paymentDate"] and body["paymentDate"].endswith("+03:00")

    rows = await _push_rows(async_session_factory, invoice_id)
    assert len(rows) == 1 and rows[0].status == "ok" and rows[0].attempts == 1


async def test_mirror_idempotent_second_run_skips(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    invoice_id = await _seed(async_session_factory)
    calls: list = []
    monkeypatch.setattr(mod, "_call_add_payment", _fake_ok(calls))

    async with async_session_factory() as session:
        await mod.mirror_paid_iiko_invoices(session)
    async with async_session_factory() as session:
        second = await mod.mirror_paid_iiko_invoices(session)

    # После ok-пуша накладная исключается из выборки — второй проход её не видит и не шлёт.
    assert second["eligible"] == 0
    assert len(calls) == 1
    assert len(await _push_rows(async_session_factory, invoice_id)) == 1


async def test_mirror_skips_non_iiko_invoice(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed(async_session_factory, source="manual")
    calls: list = []
    monkeypatch.setattr(mod, "_call_add_payment", _fake_ok(calls))
    async with async_session_factory() as session:
        result = await mod.mirror_paid_iiko_invoices(session)
    assert result["eligible"] == 0 and not calls


async def test_mirror_skips_invoice_without_bank_draft(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    # iiko-накладная «оплачено», но без банк-черновика (напр. наличными) → не зеркалим.
    await _seed(async_session_factory, with_draft=False)
    calls: list = []
    monkeypatch.setattr(mod, "_call_add_payment", _fake_ok(calls))
    async with async_session_factory() as session:
        result = await mod.mirror_paid_iiko_invoices(session)
    assert result["eligible"] == 0 and not calls


async def test_mirror_records_error_retries_then_caps(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    invoice_id = await _seed(async_session_factory)
    calls: list = []
    monkeypatch.setattr(mod, "_call_add_payment", _fake_400(calls))
    monkeypatch.setattr(mod, "MAX_PUSH_ATTEMPTS", 2)

    # 1-я попытка → error, attempts=1, накладная ещё eligible.
    async with async_session_factory() as session:
        r1 = await mod.mirror_paid_iiko_invoices(session)
    assert r1["error"] == 1
    # 2-я попытка → attempts=2 (= кап).
    async with async_session_factory() as session:
        r2 = await mod.mirror_paid_iiko_invoices(session)
    assert r2["error"] == 1
    # 3-я → кап исчерпан, накладная больше не берётся.
    async with async_session_factory() as session:
        r3 = await mod.mirror_paid_iiko_invoices(session)
    assert r3["eligible"] == 0

    assert len(calls) == 2
    rows = await _push_rows(async_session_factory, invoice_id)
    assert len(rows) == 1 and rows[0].status == "error" and rows[0].attempts == 2


async def test_mirror_sends_bank_amount_not_gross(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Накладная gross=1000, банк-черновик закрыл остаток 600 (частично пред-оплачена) —
    в iiko уходит 600 (draft.amount), НЕ 1000. Защита от переплаты (add_payment неидемпотентен)."""
    await _seed(async_session_factory, amount="1000.00", draft_amount="600.00")
    calls: list = []
    monkeypatch.setattr(mod, "_call_add_payment", _fake_ok(calls))
    async with async_session_factory() as session:
        result = await mod.mirror_paid_iiko_invoices(session)
    assert result["ok"] == 1
    assert calls[0]["amount"] == 600.0


async def test_mirror_skips_multi_invoice_draft(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_multi_invoice_draft(async_session_factory)
    calls: list = []
    monkeypatch.setattr(mod, "_call_add_payment", _fake_ok(calls))
    async with async_session_factory() as session:
        result = await mod.mirror_paid_iiko_invoices(session)
    assert result["skipped_multi"] == 2 and result["ok"] == 0
    assert not calls


async def test_mirror_continues_when_push_raises(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Исключение в push (напр. сбой commit) не должно ронять весь батч — except ловит, логирует
    по локальному id (без обращения к ORM-инстансу после rollback) и идёт дальше."""
    await _seed(async_session_factory)

    async def _boom(*args: object, **kwargs: object) -> object:
        raise OSError("transport down")

    monkeypatch.setattr(mod, "push_invoice_payment_to_iiko", _boom)
    async with async_session_factory() as session:
        result = await mod.mirror_paid_iiko_invoices(session)
    assert result["error"] == 1  # обработано, не упало наружу


async def test_mirror_splits_bank_unrepresentable_amount(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Банковское зеркало: «несчастливая» сумма (draft.amount=4213.44) теперь ДРОБИТСЯ на
    представимые части (4213.00 + 0.44) и проводится несколькими add_payment — БЕЗ ручного кейса
    (раньше такой платёж в iiko не уходил и заводился owner-review)."""
    await _seed(async_session_factory, amount="4213.44", draft_amount="4213.44")
    calls: list = []
    monkeypatch.setattr(mod, "_call_add_payment", _fake_ok(calls))

    async with async_session_factory() as session:
        result = await mod.mirror_paid_iiko_invoices(session)

    assert result["ok"] == 1
    amounts = sorted(c["amount"] for c in calls)
    assert amounts == [0.44, 4213.0]
    assert all((a * 100).is_integer() for a in amounts)  # обе части представимы
    # никакого owner-review кейса — сумма проведена дроблением
    async with async_session_factory() as session:
        cases = (
            await session.scalars(
                select(ReconciliationCase).where(
                    ReconciliationCase.kind == "iiko_payment_unsettled"
                )
            )
        ).all()
    assert cases == []


def test_format_payment_date_uses_decimal_comma() -> None:
    dt = datetime(2026, 6, 26, 14, 5, 9, 123000, tzinfo=ZoneInfo("Europe/Moscow"))
    s = mod.format_iiko_payment_date(dt)
    assert s == "2026-06-26T14:05:09,123+03:00"


def test_account_id_for_wallet_maps_bank_to_acquiring() -> None:
    assert mod.account_id_for_wallet("tbank_main") == ACQUIRING
    assert mod.account_id_for_wallet("sber_main") == ACQUIRING
    with pytest.raises(mod.IikoPaymentError):
        mod.account_id_for_wallet("unknown_wallet")

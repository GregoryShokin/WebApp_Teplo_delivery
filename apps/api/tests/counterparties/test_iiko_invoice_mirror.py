"""Зеркалирование оплаты iiko-накладной в iiko (Cloud add_payment) — сверочный джоб.

HTTP к iiko замокан (``_call_add_payment``). Проверяем: пуш только для оплаченных через
банк-черновик iiko-накладных, верное тело (accountId=эквайринг, paymentDate с запятой,
amount), идемпотентность, пропуск не-iiko/без-черновика, запись ошибки + ретрай с капом.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from cp_helpers import make_counterparty, make_draft, make_invoice, make_wallet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.services.counterparty_iiko_payment as mod
from app.models import (
    CashflowTransaction,
    IikoInvoicePaymentPush,
    InvoicePaymentAllocation,
    ReconciliationCase,
)

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
    *,
    relationship: str = "official",
    draft_amount: str = "3000.00",
    shares: tuple[str, str] = ("1000.00", "2000.00"),
    invoice_amounts: tuple[str, str] = ("1500.00", "2500.00"),
    allocation_source_kinds: tuple[str, str] = ("cash", "bank"),
    barter_activity: bool = False,
    with_existing_cases: bool = False,
) -> list[uuid.UUID]:
    """Один банк-черновик на две накладные с честными долями из реальных аллокаций.

    Продовый путь помечает такую аллокацию ``source_kind='cash'``, но связывает её с банковской
    ДДС-проводкой ``counterparty_payment/source_id=draft.id`` — именно эта связь является
    надёжным признаком банковской доли и защищает от пропорционального угадывания.
    """
    async with factory() as session:
        cp = await make_counterparty(
            session, name="Поставщик", inn="7700000000", relationship=relationship
        )
        draft = await make_draft(
            session, counterparty_id=cp.id, amount=draft_amount, status="paid"
        )
        wallet = await make_wallet(session, name="Тестовый банк", wallet_type="bank")
        txn = CashflowTransaction(
            wallet_id=wallet.id,
            direction="out",
            amount=Decimal(draft_amount),
            operation_date=date(2026, 7, 13),
            counterparty_id=cp.id,
            source_kind="counterparty_payment",
            source_id=draft.id,
            payment_purpose="Оплата двух накладных",
            quality_status="final",
        )
        session.add(txn)
        await session.flush()
        if barter_activity:
            await make_invoice(
                session,
                counterparty_id=cp.id,
                amount="1.00",
                source="iiko",
                direction="receivable",
                external_id=f"receivable-{uuid.uuid4()}",
            )
        invoice_ids: list[uuid.UUID] = []
        for invoice_amount, share, source_kind in zip(
            invoice_amounts, shares, allocation_source_kinds, strict=True
        ):
            inv = await make_invoice(
                session,
                counterparty_id=cp.id,
                amount=invoice_amount,
                source="iiko",
                external_id=f"doc-{uuid.uuid4()}",
                payment_status="paid",
                draft_id=draft.id,
            )
            invoice_ids.append(inv.id)
            if Decimal(share) > 0:
                session.add(
                    InvoicePaymentAllocation(
                        invoice_id=inv.id,
                        source_kind=source_kind,
                        cashflow_transaction_id=txn.id,
                        amount=Decimal(share),
                    )
                )
            if with_existing_cases:
                session.add(
                    ReconciliationCase(
                        kind="iiko_payment_unsettled",
                        status="pending",
                        provider="iiko",
                        payload={
                            "invoice_id": str(inv.id),
                            "reason": "старый кейс мультиплатежа",
                            "reason_code": "multi_invoice",
                        },
                    )
                )
        await session.commit()
        return invoice_ids


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


async def _cases(factory, *, status: str | None = None) -> list[ReconciliationCase]:
    async with factory() as session:
        stmt = select(ReconciliationCase).where(
            ReconciliationCase.kind == "iiko_payment_unsettled"
        )
        if status is not None:
            stmt = stmt.where(ReconciliationCase.status == status)
        return list((await session.scalars(stmt)).all())


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


async def test_mirror_pushes_multi_invoice_draft_by_honest_allocations(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    invoice_ids = await _seed_multi_invoice_draft(
        async_session_factory, with_existing_cases=True
    )
    calls: list = []
    monkeypatch.setattr(mod, "_call_add_payment", _fake_ok(calls))
    async with async_session_factory() as session:
        result = await mod.mirror_paid_iiko_invoices(session)

    assert result["ok"] == 2 and result["skipped_multi"] == 0
    # Gross накладных = 1500/2500, но честные банковские доли = 1000/2000: никакой пропорции.
    assert sorted(call["amount"] for call in calls) == [1000.0, 2000.0]
    assert all(call["accountId"] == ACQUIRING for call in calls)
    assert await _cases(async_session_factory, status="pending") == []
    assert {case.status for case in await _cases(async_session_factory)} == {"resolved"}
    for inv_id in invoice_ids:
        assert len(await _push_rows(async_session_factory, inv_id)) == 1

    # Перезапуск не дублирует необратимый add_payment по уже записанным invoice:<id>.
    async with async_session_factory() as session:
        second = await mod.mirror_paid_iiko_invoices(session)
    assert second["eligible"] == 0
    assert len(calls) == 2


async def test_mirror_leaves_barter_multi_invoice_for_manual_review(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Канонический сигнал взаимозачёта — relationship='barter' в payable-профиле.

    Его выставляет синхронизация встречных receivable-накладных и может закрепить владелец; даже
    при точных банковских долях автоматический add_payment опасен для смешанного баланса iiko.
    """
    await _seed_multi_invoice_draft(async_session_factory, relationship="barter")
    calls: list = []
    monkeypatch.setattr(mod, "_call_add_payment", _fake_ok(calls))

    async with async_session_factory() as session:
        result = await mod.mirror_paid_iiko_invoices(session)

    assert result["skipped_multi"] == 2 and result["ok"] == 0
    assert calls == []
    cases = await _cases(async_session_factory, status="pending")
    assert len(cases) == 2
    assert all(case.payload.get("reason_code") == "barter_counterparty" for case in cases)
    assert all(case.payload.get("supplier_name") == "Поставщик" for case in cases)
    assert all(case.payload.get("retriable") is False for case in cases)


async def test_mirror_detects_barter_activity_even_when_profile_is_official(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Прямая receivable-накладная страхует устаревший профиль relationship='official'."""
    await _seed_multi_invoice_draft(async_session_factory, barter_activity=True)
    calls: list = []
    monkeypatch.setattr(mod, "_call_add_payment", _fake_ok(calls))

    async with async_session_factory() as session:
        result = await mod.mirror_paid_iiko_invoices(session)

    assert result["skipped_multi"] == 2 and result["ok"] == 0
    assert calls == []
    cases = await _cases(async_session_factory, status="pending")
    assert len(cases) == 2
    assert all(case.payload.get("reason_code") == "barter_counterparty" for case in cases)


async def test_mirror_sends_honest_multi_shares_and_keeps_residual_visible(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_multi_invoice_draft(
        async_session_factory,
        draft_amount="3000.00",
        shares=("1000.00", "1500.00"),
        invoice_amounts=("1500.00", "1500.00"),
    )
    calls: list = []
    monkeypatch.setattr(mod, "_call_add_payment", _fake_ok(calls))

    async with async_session_factory() as session:
        result = await mod.mirror_paid_iiko_invoices(session)

    assert result["ok"] == 2
    assert sorted(call["amount"] for call in calls) == [1000.0, 1500.0]
    cases = await _cases(async_session_factory, status="pending")
    assert len(cases) == 1
    assert cases[0].payload.get("reason_code") == "multi_invoice_residual"
    assert cases[0].payload.get("amount") == "500.00"
    assert cases[0].payload.get("retriable") is False

    # Даже при ok по обеим накладным свип не прячет неразобранное расхождение 500 ₽.
    async with async_session_factory() as session:
        await mod.sweep_unsettled_iiko_payments(session)
    still_pending = await _cases(async_session_factory, status="pending")
    assert len(still_pending) == 1
    assert still_pending[0].payload.get("reason_code") == "multi_invoice_residual"


async def test_mirror_does_not_send_multi_invoice_without_bank_share(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_multi_invoice_draft(
        async_session_factory,
        draft_amount="1500.00",
        shares=("1500.00", "0.00"),
        invoice_amounts=("1500.00", "1500.00"),
    )
    calls: list = []
    monkeypatch.setattr(mod, "_call_add_payment", _fake_ok(calls))

    async with async_session_factory() as session:
        result = await mod.mirror_paid_iiko_invoices(session)

    assert result["ok"] == 1 and result["skipped_multi"] == 1
    assert [call["amount"] for call in calls] == [1500.0]
    cases = await _cases(async_session_factory, status="pending")
    assert len(cases) == 1
    assert cases[0].payload.get("reason_code") == "multi_invoice"
    assert cases[0].payload.get("amount") == "0.00"


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

"""Лимит частоты iiko (429) на ``add_payment``: платёж не теряем, но и не задваиваем.

``add_payment`` НЕ идемпотентен, а отказ по лимиту не доказывает, что платёж не прошёл (на
накладных 27.07 iiko вернула 429 на ``post``, а документ оказался проведён). Поэтому такой пуш
не повторяется ни в вызове, ни авто-ретраем джоба: он глушится капом и уходит под сверку проводок
(``iiko_payment_verify``), а если сверка его за сутки не разобрала — в ручной разбор.

HTTP к iiko замокан (``_call_add_payment``).
"""

from __future__ import annotations

import urllib.error
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cp_helpers import make_counterparty, make_draft, make_invoice
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.services.counterparty_iiko_payment as mod
from app.models import IikoInvoicePaymentPush, ReconciliationCase, SupplierInvoice

RATE_LIMITED = (429, {"error": "TOO_MANY_REQUESTS"})
RATE_LIMITED_AS_500 = (500, {"message": "TOO_MANY_REQUESTS"})
OK_201 = (201, {"documentId": "x", "accountingTransactionId": "t-1"})


def _fake(calls: list, status: int, body: dict):
    def _f(payload: dict):
        calls.append(payload)
        return status, body

    return _f


async def _seed_paid_iiko_invoice(
    factory: async_sessionmaker[AsyncSession], *, amount: str = "1000.00"
) -> uuid.UUID:
    async with factory() as session:
        cp = await make_counterparty(session, name="Поставщик RL", inn="7700000456")
        draft = await make_draft(session, counterparty_id=cp.id, amount=amount, status="paid")
        inv = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount=amount,
            source="iiko",
            external_id=f"doc-{uuid.uuid4()}",
            payment_status="paid",
            draft_id=draft.id,
        )
        await session.commit()
        return inv.id


async def _push_row(
    factory: async_sessionmaker[AsyncSession], invoice_id: uuid.UUID
) -> IikoInvoicePaymentPush | None:
    async with factory() as session:
        return await session.scalar(
            select(IikoInvoicePaymentPush).where(
                IikoInvoicePaymentPush.idempotency_key == f"invoice:{invoice_id}"
            )
        )


async def _cases(
    factory: async_sessionmaker[AsyncSession], invoice_id: uuid.UUID
) -> list[ReconciliationCase]:
    async with factory() as session:
        return list(
            (
                await session.scalars(
                    select(ReconciliationCase).where(
                        ReconciliationCase.kind == "iiko_payment_unsettled",
                        ReconciliationCase.payload["invoice_id"].astext == str(invoice_id),
                    )
                )
            ).all()
        )


def test_auth_token_retries_rate_limit(monkeypatch) -> None:
    """Выдача токена по 429 повторяется: платёж при этом ещё не отправлялся, дублировать нечего."""
    monkeypatch.setattr(mod, "RATE_LIMIT_RETRY_DELAYS", (0.0, 0.0))
    answers = iter([429, 429, None])

    def _token(opener: object) -> str:
        code = next(answers)
        if code is not None:
            raise urllib.error.HTTPError("url", code, "Too Many Requests", {}, None)  # type: ignore[arg-type]
        return "TOKEN"

    monkeypatch.setattr(mod, "_iiko_auth_token", _token)
    assert mod._iiko_auth_token_with_retry(None) == "TOKEN"


def test_auth_token_does_not_swallow_other_errors(monkeypatch) -> None:
    """401 (протухший секрет) — не лимит частоты: повторять бессмысленно, ошибка идёт наверх."""
    monkeypatch.setattr(mod, "RATE_LIMIT_RETRY_DELAYS", (0.0,))

    def _token(opener: object) -> str:
        raise urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(mod, "_iiko_auth_token", _token)
    with pytest.raises(urllib.error.HTTPError):
        mod._iiko_auth_token_with_retry(None)


async def test_rate_limit_blocks_auto_retry(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    """429 → пуш помечен машиночитаемо и заглушен капом; следующий проход в iiko НЕ ходит.
    Слепой повтор задвоил бы платёж, если 429 всё-таки его провела."""
    inv_id = await _seed_paid_iiko_invoice(async_session_factory)

    monkeypatch.setattr(mod, "_call_add_payment", _fake([], *RATE_LIMITED))
    async with async_session_factory() as session:
        await mod.mirror_paid_iiko_invoices(session)

    row = await _push_row(async_session_factory, inv_id)
    assert row is not None
    assert row.status == "error"
    assert (row.error or "").startswith(mod.RATE_LIMITED_ERROR_PREFIX)
    assert row.attempts >= mod.MAX_PUSH_ATTEMPTS

    calls: list = []
    monkeypatch.setattr(mod, "_call_add_payment", _fake(calls, *OK_201))
    async with async_session_factory() as session:
        await mod.mirror_paid_iiko_invoices(session)
    assert calls == []


async def test_rate_limit_recognized_in_body_not_only_by_code(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    """Cloud отдаёт отказы и кодом 500 с текстом в теле — опираться только на HTTP 429 нельзя."""
    inv_id = await _seed_paid_iiko_invoice(async_session_factory)

    monkeypatch.setattr(mod, "_call_add_payment", _fake([], *RATE_LIMITED_AS_500))
    async with async_session_factory() as session:
        await mod.mirror_paid_iiko_invoices(session)

    row = await _push_row(async_session_factory, inv_id)
    assert row is not None
    assert (row.error or "").startswith(mod.RATE_LIMITED_ERROR_PREFIX)


async def test_rate_limited_push_is_not_counted_as_settled(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    """Заглушенный пуш не выдаётся за отражённую оплату — иначе коррекция оплаченной накладной
    пошла бы дальше на непроведённом платеже."""
    inv_id = await _seed_paid_iiko_invoice(async_session_factory)
    monkeypatch.setattr(mod, "_call_add_payment", _fake([], *RATE_LIMITED))
    async with async_session_factory() as session:
        await mod.mirror_paid_iiko_invoices(session)

    async with async_session_factory() as session:
        invoice = await session.get(SupplierInvoice, inv_id)
        assert invoice is not None
        assert await mod.original_payment_settled_in_iiko(session, invoice) is False


async def test_fresh_rate_limited_push_gets_no_auto_retry_case(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    """Свежий 429 разбирает сверка проводок, а не человек: кейса с кнопкой авто-повтора быть не
    должно — она отправила бы платёж второй раз."""
    inv_id = await _seed_paid_iiko_invoice(async_session_factory)
    monkeypatch.setattr(mod, "_call_add_payment", _fake([], *RATE_LIMITED))
    async with async_session_factory() as session:
        await mod.mirror_paid_iiko_invoices(session)
        await mod.sweep_unsettled_iiko_payments(session)

    codes = {(case.payload or {}).get("reason_code") for case in await _cases(
        async_session_factory, inv_id
    )}
    assert "retry_cap_exhausted" not in codes
    assert "rate_limited_unverified" not in codes


async def test_stale_rate_limited_push_goes_to_manual_review(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    """Сутки провисел неразобранным (сверка не добралась) → видимый кейс, но БЕЗ авто-повтора."""
    inv_id = await _seed_paid_iiko_invoice(async_session_factory)
    monkeypatch.setattr(mod, "_call_add_payment", _fake([], *RATE_LIMITED))
    async with async_session_factory() as session:
        await mod.mirror_paid_iiko_invoices(session)

    async with async_session_factory() as session:
        row = await session.scalar(
            select(IikoInvoicePaymentPush).where(
                IikoInvoicePaymentPush.idempotency_key == f"invoice:{inv_id}"
            )
        )
        assert row is not None
        row.created_at = datetime.now(UTC) - mod.RATE_LIMITED_REVIEW_AFTER - timedelta(hours=1)
        await session.commit()

    async with async_session_factory() as session:
        result = await mod.sweep_unsettled_iiko_payments(session)

    assert result["rate_limited_unverified"] == 1
    codes = {(case.payload or {}).get("reason_code") for case in await _cases(
        async_session_factory, inv_id
    )}
    assert "rate_limited_unverified" in codes
    assert "rate_limited_unverified" not in mod.IIKO_UNSETTLED_RETRIABLE

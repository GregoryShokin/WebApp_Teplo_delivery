"""Пост-проверка зеркала оплат: подтверждение проводки в iiko и переотправка потерянных.

Сетевой слой (OLAP-отчёт iikoServer) замокан — проверяем машину состояний: подтверждение,
терпение до порога, переотправка со снятием done-маркера и переход в ручной разбор.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from cp_helpers import make_counterparty
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import IikoInvoicePaymentPush, ReconciliationCase, SupplierInvoice
from app.services import iiko_payment_verify as verify_mod
from app.services.iiko_payment_verify import (
    MAX_RESENDS,
    VERIFY_ATTEMPTS_BEFORE_RESEND,
    verify_mirrored_payments,
)

SENT_AT = datetime(2026, 7, 20, 19, 30, tzinfo=UTC)


async def _paid_invoice(session: AsyncSession, *, number: str, amount: str) -> SupplierInvoice:
    cp = await make_counterparty(
        session, name=f"Поставщик {number}", inn=f"77100005{number[-2:].rjust(2, '0')}",
        iiko_guid=f"SUP-{number}",
    )
    invoice = SupplierInvoice(
        counterparty_id=cp.id,
        source="kassa_invoice",
        direction="payable",
        number=number,
        amount=Decimal(amount),
        payment_status="paid",
        external_id=f"IIKO-DOC-{number}",
    )
    session.add(invoice)
    await session.flush()
    return invoice


async def _push_row(
    session: AsyncSession,
    invoice: SupplierInvoice,
    *,
    amount: str,
    verify_attempts: int = 0,
    resend_count: int = 0,
) -> IikoInvoicePaymentPush:
    row = IikoInvoicePaymentPush(
        idempotency_key=f"kassa_goods:{invoice.id}:card",
        invoice_id=invoice.id,
        external_id=invoice.external_id or "",
        amount=Decimal(amount),
        account_to="ACC-1",
        status="ok",
        attempts=1,
        verify_attempts=verify_attempts,
        resend_count=resend_count,
    )
    session.add(row)
    await session.flush()
    # created_at проставляет БД — сдвигаем в прошлое, иначе строка не выйдет из grace-окна.
    row.created_at = SENT_AT
    await session.commit()
    return row


def _patch_olap(monkeypatch: pytest.MonkeyPatch, payments: list[tuple[str, Decimal]]) -> None:
    monkeypatch.setattr(
        verify_mod, "fetch_invoice_payment_transactions", lambda date_from, date_to: payments
    )


@pytest.mark.asyncio
async def test_verify_marks_payment_confirmed(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with async_session_factory() as session:
        invoice = await _paid_invoice(session, number="41", amount="3443.00")
        row = await _push_row(session, invoice, amount="3443.00")
        _patch_olap(monkeypatch, [("41", Decimal("3443.00"))])

        result = await verify_mirrored_payments(session)

        await session.refresh(row)
        assert result["verified"] == 1
        assert row.verified_at is not None
        assert row.status == "ok"


@pytest.mark.asyncio
async def test_verify_tolerates_kopeck_difference(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проводка на копейку разошлась с нашей суммой — это та же оплата, а не потеря."""
    async with async_session_factory() as session:
        invoice = await _paid_invoice(session, number="42", amount="3320.00")
        row = await _push_row(session, invoice, amount="3320.00")
        _patch_olap(monkeypatch, [("42", Decimal("3320.02"))])

        result = await verify_mirrored_payments(session)

        await session.refresh(row)
        assert result["verified"] == 1
        assert row.verified_at is not None


@pytest.mark.asyncio
async def test_verify_waits_before_resending(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Одной неудачной проверки мало: проводка могла ещё не доехать — просто считаем попытки."""
    async with async_session_factory() as session:
        invoice = await _paid_invoice(session, number="43", amount="1000.00")
        row = await _push_row(session, invoice, amount="1000.00")
        _patch_olap(monkeypatch, [("999", Decimal("1000.00"))])

        result = await verify_mirrored_payments(session)

        await session.refresh(row)
        assert result["pending"] == 1
        assert result["resent"] == 0
        assert row.verify_attempts == 1
        assert row.status == "ok"


@pytest.mark.asyncio
async def test_verify_resends_after_threshold_and_clears_done_marker(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Порог достигнут → снимаем ok и done-маркер, чтобы зеркалящий джоб отправил платёж снова."""
    async with async_session_factory() as session:
        invoice = await _paid_invoice(session, number="44", amount="2000.00")
        row = await _push_row(
            session, invoice, amount="2000.00", verify_attempts=VERIFY_ATTEMPTS_BEFORE_RESEND - 1
        )
        session.add(
            IikoInvoicePaymentPush(
                idempotency_key=f"kassa_goods_done:{invoice.id}",
                invoice_id=invoice.id,
                external_id=invoice.external_id or "",
                amount=Decimal("0"),
                account_to="-",
                status="ok",
            )
        )
        await session.commit()
        _patch_olap(monkeypatch, [])

        result = await verify_mirrored_payments(session)

        await session.refresh(row)
        assert result["resent"] == 1
        assert row.status == "error"
        assert row.attempts == 0  # кап сброшен, иначе переотправка не состоится
        assert row.verify_attempts == 0
        assert row.resend_count == 1
        done = await session.scalar(
            select(IikoInvoicePaymentPush).where(
                IikoInvoicePaymentPush.idempotency_key == f"kassa_goods_done:{invoice.id}"
            )
        )
        assert done is None  # накладная снова попадёт в выборку зеркалящего джоба


@pytest.mark.asyncio
async def test_verify_opens_case_after_last_resend(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Последняя допустимая переотправка → кейс в owner-review, дальше только руками."""
    async with async_session_factory() as session:
        invoice = await _paid_invoice(session, number="45", amount="1500.00")
        row = await _push_row(
            session,
            invoice,
            amount="1500.00",
            verify_attempts=VERIFY_ATTEMPTS_BEFORE_RESEND - 1,
            resend_count=MAX_RESENDS - 1,
        )
        _patch_olap(monkeypatch, [])

        result = await verify_mirrored_payments(session)

        await session.refresh(row)
        assert result["manual"] == 1
        assert row.resend_count == MAX_RESENDS
        case = await session.scalar(
            select(ReconciliationCase).where(ReconciliationCase.kind == "iiko_payment_unsettled")
        )
        assert case is not None


@pytest.mark.asyncio
async def test_verify_skips_fresh_and_exhausted_rows(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Свежую отправку не судим (проводка ещё в пути), исчерпавшую переотправки — не трогаем."""
    async with async_session_factory() as session:
        fresh_invoice = await _paid_invoice(session, number="46", amount="700.00")
        fresh = IikoInvoicePaymentPush(
            idempotency_key=f"kassa_goods:{fresh_invoice.id}:card",
            invoice_id=fresh_invoice.id,
            external_id=fresh_invoice.external_id or "",
            amount=Decimal("700.00"),
            account_to="ACC-1",
            status="ok",
        )
        session.add(fresh)
        exhausted_invoice = await _paid_invoice(session, number="47", amount="800.00")
        exhausted = await _push_row(
            session, exhausted_invoice, amount="800.00", resend_count=MAX_RESENDS
        )
        _patch_olap(monkeypatch, [])

        result = await verify_mirrored_payments(session)

        assert result["checked"] == 0
        await session.refresh(fresh)
        await session.refresh(exhausted)
        assert fresh.verify_attempts == 0
        assert exhausted.verify_attempts == 0


@pytest.mark.asyncio
async def test_verify_no_rows_does_not_call_iiko(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Пустая выборка — без похода в iiko (джоб крутится каждые полчаса)."""
    def _boom(date_from: object, date_to: object) -> list[tuple[str, Decimal]]:
        raise AssertionError("OLAP не должен вызываться на пустой выборке")

    monkeypatch.setattr(verify_mod, "fetch_invoice_payment_transactions", _boom)
    async with async_session_factory() as session:
        result = await verify_mirrored_payments(session)
    assert result == {"checked": 0, "verified": 0, "pending": 0, "resent": 0, "manual": 0}

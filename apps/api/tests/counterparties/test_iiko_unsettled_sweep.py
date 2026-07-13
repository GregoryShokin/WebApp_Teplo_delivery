"""Этап 2 — единый видимый список «оплата не дошла до iiko».

Дозор ``sweep_unsettled_iiko_payments`` ловит причины, которые сверочные джобы не видят
(paid_outside_draft / correction_unsettled / retry_cap_exhausted) и закрывает восстановившиеся
кейсы; действия на кейсе — «повторить отправку» и «оплата проведена вручную».
"""

from __future__ import annotations

from decimal import Decimal

from cp_helpers import (
    admin_headers,
    make_counterparty,
    make_draft,
    make_invoice,
)
from sqlalchemy import select

import app.services.counterparty_iiko_payment as cip
from app.models import (
    IikoInvoicePaymentPush,
    InvoicePaymentAllocation,
    ReconciliationCase,
)


async def _paid_iiko(
    session,
    *,
    number,
    external_id,
    amount="100.00",
    draft_status=None,
    pays_via_safe=False,
    corrected=False,
):
    """Оплаченная iiko-накладная в разных состояниях: с/без банк-черновика, скорректированная."""
    cp = await make_counterparty(session, name=f"Пост {number}", iiko_guid=f"G-{number}")
    draft_id = None
    if draft_status is not None:
        draft = await make_draft(session, counterparty_id=cp.id, amount=amount, status=draft_status)
        draft.pays_via_safe = pays_via_safe
        draft_id = draft.id
    inv = await make_invoice(
        session, counterparty_id=cp.id, amount=amount, payment_status="paid",
        number=number, source="iiko", external_id=external_id, draft_id=draft_id,
    )
    if corrected:
        inv.iiko_correction_new_external_id = f"NEW-{external_id}"
    await session.flush()
    return cp, inv


def _ok_push(inv_id, external_id, amount="100.00"):
    return IikoInvoicePaymentPush(
        idempotency_key=f"invoice:{inv_id}", invoice_id=inv_id, external_id=external_id,
        amount=Decimal(amount), account_to=cip.IIKO_ACQUIRING_ACCOUNT, status="ok",
    )


async def _cases(session_factory):
    async with session_factory() as session:
        return (
            await session.scalars(
                select(ReconciliationCase).where(
                    ReconciliationCase.kind == "iiko_payment_unsettled"
                )
            )
        ).all()


async def test_sweep_flags_paid_outside_draft(async_session_factory):
    """iiko-накладная оплачена НЕ через банк-черновик (наличные) → банк-зеркало её не берёт;
    свип заводит видимый кейс reason_code=paid_outside_draft (обогащённый). Idempotent."""
    async with async_session_factory() as session:
        _, inv = await _paid_iiko(session, number="A1", external_id="A1")  # без черновика
        session.add(
            InvoicePaymentAllocation(
                invoice_id=inv.id, source_kind="cash", amount=Decimal("100.00")
            )
        )
        await session.commit()
        inv_id = inv.id
        res = await cip.sweep_unsettled_iiko_payments(session)
    assert res["paid_outside_draft"] == 1
    cases = await _cases(async_session_factory)
    assert len(cases) == 1
    p = cases[0].payload
    assert p["reason_code"] == "paid_outside_draft" and p["invoice_id"] == str(inv_id)
    assert p["supplier_name"] == "Пост A1" and p["invoice_number"] == "A1"
    assert p["retriable"] is False
    # idempotent — второй проход не плодит дубль
    async with async_session_factory() as session:
        res2 = await cip.sweep_unsettled_iiko_payments(session)
    assert res2["paid_outside_draft"] == 0
    assert len(await _cases(async_session_factory)) == 1


async def test_sweep_skips_bank_eligible_and_settled(async_session_factory):
    """Однонакладный банк-черновик (его берёт mirror) и уже settled (ok-пуш) свип НЕ флагует."""
    async with async_session_factory() as session:
        # bank-eligible (однонакладный paid-черновик) — его берёт mirror, свип не флагует
        await _paid_iiko(session, number="B1", external_id="B1", draft_status="paid")
        _, done = await _paid_iiko(session, number="B2", external_id="B2")
        session.add(_ok_push(done.id, "B2"))
        await session.commit()
        res = await cip.sweep_unsettled_iiko_payments(session)
    assert res["paid_outside_draft"] == 0 and res["retry_cap_exhausted"] == 0
    assert await _cases(async_session_factory) == []


async def test_sweep_flags_correction_unsettled(async_session_factory):
    """Скорректированная накладная (external_id уже = Y) без ok-пуша оригинала → кейс
    reason_code=correction_unsettled; с ok-пушем — НЕ флагуем."""
    async with async_session_factory() as session:
        await _paid_iiko(session, number="C1", external_id="Y1", corrected=True)  # без ok-пуша
        _, ok_inv = await _paid_iiko(session, number="C2", external_id="Y2", corrected=True)
        session.add(_ok_push(ok_inv.id, "Y2"))
        await session.commit()
        res = await cip.sweep_unsettled_iiko_payments(session)
    assert res["correction_unsettled"] == 1
    cases = await _cases(async_session_factory)
    assert len(cases) == 1 and cases[0].payload["reason_code"] == "correction_unsettled"


async def test_sweep_flags_retry_cap_exhausted(async_session_factory):
    """Пуш исчерпал кап (attempts>=MAX, status=error) БЕЗ кейса → свип заводит
    reason_code=retry_cap_exhausted (retriable)."""
    async with async_session_factory() as session:
        _, inv = await _paid_iiko(session, number="D1", external_id="D1", draft_status="paid")
        session.add(
            IikoInvoicePaymentPush(
                idempotency_key=f"invoice:{inv.id}", invoice_id=inv.id, external_id="D1",
                amount=Decimal("100.00"), account_to="x", status="error",
                attempts=cip.MAX_PUSH_ATTEMPTS, error="boom",
            )
        )
        await session.commit()
        res = await cip.sweep_unsettled_iiko_payments(session)
    assert res["retry_cap_exhausted"] == 1
    cases = await _cases(async_session_factory)
    assert len(cases) == 1 and cases[0].payload["reason_code"] == "retry_cap_exhausted"
    assert cases[0].payload["retriable"] is True


async def test_sweep_resolves_recovered_case(async_session_factory):
    """Висящий pending-кейс, чья накладная снова settled (ok-пуш) → свип закрывает (resolved)."""
    async with async_session_factory() as session:
        _, inv = await _paid_iiko(session, number="E1", external_id="E1")
        await cip._open_iiko_payment_case(
            session, invoice_id=inv.id, external_id="E1", amount=Decimal("100.00"),
            reason="test", reason_code="iiko_rejected",
        )
        session.add(_ok_push(inv.id, "E1"))  # оплата восстановилась
        await session.commit()
        res = await cip.sweep_unsettled_iiko_payments(session)
    assert res["resolved"] == 1
    cases = await _cases(async_session_factory)
    assert len(cases) == 1 and cases[0].status == "resolved"


async def test_retry_iiko_payment_uncaps_and_sends(async_session_factory, monkeypatch):
    """retry снимает кап и (для одиночного банка) синхронно проводит add_payment; успех закрывает
    кейс, строка пуша становится ok."""
    monkeypatch.setattr(
        cip, "_call_add_payment",
        lambda p: (201, {"accountingTransactionId": "t", "documentId": p["documentId"]}),
    )
    async with async_session_factory() as session:
        _, inv = await _paid_iiko(session, number="F1", external_id="F1", draft_status="paid")
        session.add(
            IikoInvoicePaymentPush(
                idempotency_key=f"invoice:{inv.id}", invoice_id=inv.id, external_id="F1",
                amount=Decimal("100.00"), account_to="x", status="error",
                attempts=cip.MAX_PUSH_ATTEMPTS,
            )
        )
        await cip._open_iiko_payment_case(
            session, invoice_id=inv.id, external_id="F1", amount=Decimal("100.00"),
            reason="capped", reason_code="retry_cap_exhausted",
        )
        await session.commit()
        res = await cip.retry_iiko_payment(session, inv.id)
        assert res is not None and res.ok
        row = await session.scalar(
            select(IikoInvoicePaymentPush).where(
                IikoInvoicePaymentPush.idempotency_key == f"invoice:{inv.id}"
            )
        )
        assert row.status == "ok"
    cases = await _cases(async_session_factory)
    assert len(cases) == 1 and cases[0].status == "resolved"


async def test_confirm_manual_closes_case(async_session_factory):
    """«Оплата проведена вручную» для не-авто накладной пишет ok-маркер и закрывает кейс."""
    async with async_session_factory() as session:
        _, inv = await _paid_iiko(session, number="G1", external_id="G1")  # без черновика → не авто
        await cip._open_iiko_payment_case(
            session, invoice_id=inv.id, external_id="G1", amount=Decimal("100.00"),
            reason="outside", reason_code="paid_outside_draft",
        )
        await session.commit()
        await cip.mark_iiko_payment_settled_manually(session, inv, actor_user_id=None)
    cases = await _cases(async_session_factory)
    assert len(cases) == 1 and cases[0].status == "resolved"


async def _make_case(session_factory, *, number, external_id, reason_code, draft_status=None):
    async with session_factory() as session:
        _, inv = await _paid_iiko(
            session, number=number, external_id=external_id, draft_status=draft_status
        )
        await cip._open_iiko_payment_case(
            session, invoice_id=inv.id, external_id=external_id, amount=Decimal("100.00"),
            reason="test", reason_code=reason_code,
        )
        await session.commit()
        case = await session.scalar(
            select(ReconciliationCase).where(ReconciliationCase.kind == "iiko_payment_unsettled")
        )
        return str(case.id)


async def test_retry_endpoint_resolves(client, async_session_factory, monkeypatch):
    """API retry-iiko-payment: retriable + одиночный банк → 200, кейс resolved."""
    monkeypatch.setattr(
        cip, "_call_add_payment",
        lambda p: (201, {"accountingTransactionId": "t", "documentId": p["documentId"]}),
    )
    case_id = await _make_case(
        async_session_factory, number="H1", external_id="H1",
        reason_code="iiko_rejected", draft_status="paid",
    )
    admin_h = await admin_headers(async_session_factory)
    r = client.post(f"/api/v1/dds/owner-review/{case_id}/retry-iiko-payment", headers=admin_h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "resolved"


async def test_retry_endpoint_rejects_non_retriable(client, async_session_factory):
    """API retry: для мультиплатежа (не авто-повторяемо) → 422 (нужно ручное подтверждение)."""
    case_id = await _make_case(
        async_session_factory, number="H2", external_id="H2",
        reason_code="multi_invoice", draft_status="paid",
    )
    admin_h = await admin_headers(async_session_factory)
    r = client.post(f"/api/v1/dds/owner-review/{case_id}/retry-iiko-payment", headers=admin_h)
    assert r.status_code == 422


async def test_confirm_manual_endpoint_resolves(client, async_session_factory):
    """API confirm-iiko-manual: не-авто накладная → 200, кейс resolved."""
    case_id = await _make_case(
        async_session_factory, number="H3", external_id="H3", reason_code="paid_outside_draft"
    )
    admin_h = await admin_headers(async_session_factory)
    r = client.post(f"/api/v1/dds/owner-review/{case_id}/confirm-iiko-manual", headers=admin_h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "resolved"


async def test_sweep_flags_issued_at_enrichment(async_session_factory):
    """Обогащение payload включает дату накладной (issued_at) для карточки."""
    from datetime import UTC, datetime

    async with async_session_factory() as session:
        _, inv = await _paid_iiko(session, number="I1", external_id="I1")
        inv.issued_at = datetime(2026, 7, 11, 9, 30, tzinfo=UTC)
        await session.commit()
        await cip.sweep_unsettled_iiko_payments(session)
    cases = await _cases(async_session_factory)
    assert len(cases) == 1 and cases[0].payload["issued_at"] is not None
    assert cases[0].payload["issued_at"].startswith("2026-07-11")

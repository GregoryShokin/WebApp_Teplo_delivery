"""Дробление «непредставимых» для iiko сумм (float(amount)*100 не целое) на представимые части
и проведение несколькими add_payment. iiko отвергает такие суммы «invalid amount in JSON»
(проверено на боевом API), а целые рубли + «удачные» копейки принимает."""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from cp_helpers import make_counterparty, make_draft, make_invoice
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.services.counterparty_iiko_payment as mod
from app.models import IikoInvoicePaymentPush

pytestmark = pytest.mark.usefixtures("migrated_db")


def _rep(x: Decimal) -> bool:
    return (float(x) * 100).is_integer()


@pytest.mark.parametrize(
    "amount", ["33982.80", "4213.44", "0.29", "0.07", "0.58", "2.03", "5.29", "999999.57"]
)
def test_representable_split_valid(amount: str) -> None:
    a = Decimal(amount)
    parts = mod.representable_split(a)
    assert sum(parts) == a  # точная сумма
    assert all(_rep(p) for p in parts)  # каждая часть представима для iiko
    assert 1 <= len(parts) <= 3
    assert all(p > 0 for p in parts)


@pytest.mark.parametrize("amount", ["100.00", "0.25", "0.50", "1500.75", "959.88"])
def test_representable_split_passthrough(amount: str) -> None:
    a = Decimal(amount)
    assert mod.representable_split(a) == [a]  # представимая сумма → одна часть, без дробления


def test_split_two_representable_cents() -> None:
    # «неудачные» копейки без целой части (0.29 → 28.9999…) → две представимые части, сумма точная
    parts = mod.representable_split(Decimal("0.29"))
    assert len(parts) == 2 and sum(parts) == Decimal("0.29")
    assert all(_rep(p) for p in parts)


def _fake_ok(calls: list):
    def _f(payload: dict):
        calls.append(payload)
        return 201, {"accountingTransactionId": "t", "documentId": payload["documentId"]}

    return _f


async def _seed(factory: async_sessionmaker[AsyncSession], *, amount: str) -> uuid.UUID:
    async with factory() as session:
        cp = await make_counterparty(session, name="Поставщик S", inn="7700000042")
        draft = await make_draft(session, counterparty_id=cp.id, amount=amount, status="paid")
        inv = await make_invoice(
            session, counterparty_id=cp.id, amount=amount, source="iiko",
            external_id=f"doc-{uuid.uuid4()}", payment_status="paid", draft_id=draft.id,
        )
        await session.commit()
        return inv.id


async def test_mirror_splits_unrepresentable_amount(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """33982.80 (непредставима) → два add_payment (33982.00 + 0.80), сумма точная, накладная ok."""
    inv_id = await _seed(async_session_factory, amount="33982.80")
    calls: list = []
    monkeypatch.setattr(mod, "_call_add_payment", _fake_ok(calls))

    async with async_session_factory() as session:
        result = await mod.mirror_paid_iiko_invoices(session)

    assert result["ok"] == 1
    amounts = sorted(c["amount"] for c in calls)
    assert amounts == [0.8, 33982.0]
    assert all((a * 100).is_integer() for a in amounts)  # обе части представимы

    async with async_session_factory() as session:
        rows = (
            await session.scalars(
                select(IikoInvoicePaymentPush).where(
                    IikoInvoicePaymentPush.idempotency_key.like(f"invoice:{inv_id}%")
                )
            )
        ).all()
    by_key = {r.idempotency_key: r for r in rows}
    # summary по базовому ключу: invoice_id задан (для blocked_invoice_ids), статус ok
    assert by_key[f"invoice:{inv_id}"].status == "ok"
    assert by_key[f"invoice:{inv_id}"].invoice_id == inv_id
    # части: суб-ключи, invoice_id=None (недооплаченная не блокируется)
    assert by_key[f"invoice:{inv_id}#0"].invoice_id is None and by_key[f"invoice:{inv_id}#0"].status == "ok"
    assert by_key[f"invoice:{inv_id}#1"].invoice_id is None


async def test_mirror_split_idempotent_second_run(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Повторный прогон дроблёной оплаты не шлёт add_payment заново (summary ok → накладная done)."""
    await _seed(async_session_factory, amount="33982.80")
    calls: list = []
    monkeypatch.setattr(mod, "_call_add_payment", _fake_ok(calls))

    async with async_session_factory() as session:
        await mod.mirror_paid_iiko_invoices(session)
    first = len(calls)
    assert first == 2

    async with async_session_factory() as session:
        await mod.mirror_paid_iiko_invoices(session)
    assert len(calls) == first  # ничего не отправлено повторно

"""Matching bank operations to drafts + allocating cash facts to invoices.

Auto-match ties an outgoing bank op to a draft by exact INN + amount within a window;
lower-level allocators spread a payment across a draft's invoices (FIFO) or apply a
manual cash split. Invoice status is derived from allocation coverage.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from cp_helpers import (
    allocated_total,
    make_bank_operation,
    make_counterparty,
    make_draft,
    make_invoice,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.counterparty_matching import (
    CounterpartyMatchError,
    allocate_bank_operation_to_draft,
    allocate_cash_to_invoice,
    auto_match_bank_operations,
)

INN = "7704444444"


async def test_auto_match_exact_single_candidate_marks_paid(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="X", inn=INN)
        draft = await make_draft(session, counterparty_id=cp.id, amount="1000.00")
        invoice = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", draft_id=draft.id
        )
        await session.commit()
        await session.refresh(draft)
        await make_bank_operation(
            session, amount="1000.00", inn=INN, operation_date=draft.created_at.date()
        )
        await session.commit()

        result = await auto_match_bank_operations(session)

        assert len(result["matched"]) == 1
        assert result["needs_review"] == []
        await session.refresh(invoice)
        await session.refresh(draft)
        assert invoice.payment_status == "paid"
        assert draft.status == "paid"


async def test_auto_match_amount_mismatch_goes_to_review(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="X", inn=INN)
        draft = await make_draft(session, counterparty_id=cp.id, amount="1000.00")
        invoice = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", draft_id=draft.id
        )
        await session.commit()
        await session.refresh(draft)
        await make_bank_operation(
            session, amount="1234.00", inn=INN, operation_date=draft.created_at.date()
        )
        await session.commit()

        result = await auto_match_bank_operations(session)

        assert result["matched"] == []
        assert len(result["needs_review"]) == 1
        assert result["needs_review"][0]["candidate_draft_ids"] == [draft.id]
        await session.refresh(invoice)
        assert invoice.payment_status == "unpaid"


async def test_auto_match_ambiguous_candidates_goes_to_review(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="X", inn=INN)
        draft_a = await make_draft(session, counterparty_id=cp.id, amount="1000.00")
        draft_b = await make_draft(session, counterparty_id=cp.id, amount="1000.00")
        await session.commit()
        await session.refresh(draft_a)
        await make_bank_operation(
            session, amount="1000.00", inn=INN, operation_date=draft_a.created_at.date()
        )
        await session.commit()

        result = await auto_match_bank_operations(session)

        assert result["matched"] == []
        assert len(result["needs_review"]) == 1
        assert set(result["needs_review"][0]["candidate_draft_ids"]) == {draft_a.id, draft_b.id}


async def test_auto_match_ignores_incoming_operations(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="X", inn=INN)
        draft = await make_draft(session, counterparty_id=cp.id, amount="1000.00")
        await make_invoice(session, counterparty_id=cp.id, amount="1000.00", draft_id=draft.id)
        await session.commit()
        await session.refresh(draft)
        await make_bank_operation(
            session,
            amount="1000.00",
            direction="in",  # incoming — never an outgoing payment
            inn=INN,
            operation_date=draft.created_at.date(),
        )
        await session.commit()

        result = await auto_match_bank_operations(session)

        assert result["matched"] == []
        assert result["needs_review"] == []


async def test_allocate_bank_operation_spreads_fifo_across_invoices(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="X", inn=INN)
        draft = await make_draft(session, counterparty_id=cp.id, amount="500.00")
        inv1 = await make_invoice(
            session, counterparty_id=cp.id, amount="300.00", draft_id=draft.id
        )
        inv2 = await make_invoice(
            session, counterparty_id=cp.id, amount="200.00", draft_id=draft.id
        )
        op = await make_bank_operation(session, amount="500.00", inn=INN)
        await session.commit()

        await allocate_bank_operation_to_draft(session, bank_operation_id=op.id, draft_id=draft.id)

        await session.refresh(inv1)
        await session.refresh(inv2)
        await session.refresh(draft)
        assert await allocated_total(session, inv1.id) == Decimal("300.00")
        assert await allocated_total(session, inv2.id) == Decimal("200.00")
        assert inv1.payment_status == "paid"
        assert inv2.payment_status == "paid"
        assert draft.status == "paid"


async def test_allocate_bank_operation_partial_keeps_draft_open(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="X", inn=INN)
        draft = await make_draft(session, counterparty_id=cp.id, amount="1000.00")
        invoice = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", draft_id=draft.id
        )
        op = await make_bank_operation(session, amount="600.00", inn=INN)
        await session.commit()

        await allocate_bank_operation_to_draft(session, bank_operation_id=op.id, draft_id=draft.id)

        await session.refresh(invoice)
        await session.refresh(draft)
        assert invoice.payment_status == "partially_paid"
        assert draft.status == "created"  # not fully covered → stays open


async def test_cash_split_then_partial_then_paid(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="X", inn=INN)
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="1000.00")
        await session.commit()

        await allocate_cash_to_invoice(session, invoice_id=invoice.id, amount=Decimal("400.00"))
        await session.refresh(invoice)
        assert invoice.payment_status == "partially_paid"

        await allocate_cash_to_invoice(session, invoice_id=invoice.id, amount=Decimal("600.00"))
        await session.refresh(invoice)
        assert invoice.payment_status == "paid"
        assert await allocated_total(session, invoice.id) == Decimal("1000.00")


async def test_split_bank_then_cash_covers_invoice(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Безнал-часть через черновик + остаток наличными → накладная закрыта."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="X", inn=INN)
        draft = await make_draft(session, counterparty_id=cp.id, amount="700.00")
        invoice = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", draft_id=draft.id
        )
        op = await make_bank_operation(session, amount="700.00", inn=INN)
        await session.commit()

        await allocate_bank_operation_to_draft(session, bank_operation_id=op.id, draft_id=draft.id)
        await session.refresh(invoice)
        assert invoice.payment_status == "partially_paid"

        await allocate_cash_to_invoice(session, invoice_id=invoice.id, amount=Decimal("300.00"))
        await session.refresh(invoice)
        assert invoice.payment_status == "paid"


async def test_allocate_cash_rejects_amount_over_remaining(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="X", inn=INN)
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="100.00")
        await session.commit()

        with pytest.raises(CounterpartyMatchError, match="остатка"):
            await allocate_cash_to_invoice(session, invoice_id=invoice.id, amount=Decimal("150.00"))


async def test_allocate_cash_rejects_void_invoice(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="X", inn=INN)
        invoice = await make_invoice(
            session, counterparty_id=cp.id, amount="100.00", payment_status="void"
        )
        await session.commit()

        with pytest.raises(CounterpartyMatchError, match="аннулирована"):
            await allocate_cash_to_invoice(session, invoice_id=invoice.id, amount=Decimal("50.00"))

"""Warehouse invoice ↔ T-Bank time-match (``suggest_invoice_matches_by_time``).

Ranks outgoing operations by receipt-time (``issued_at`` ↔ ``posted_at``) closeness and
amount, per the owner's rule: ±10% amount is allowed but only "confident" with an exact
amount inside the tight time window. Exercised on the real ``teplo_test`` schema.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from cp_helpers import make_bank_operation, make_counterparty, make_invoice
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import InvoicePaymentAllocation, TransferGroup
from app.services.counterparty_bank_match import suggest_invoice_matches_by_time

ISSUED = datetime(2026, 6, 10, 14, 30, tzinfo=UTC)
SAME_DAY = date(2026, 6, 10)


async def test_time_match_ranks_tight_then_fallback(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик")
        invoice = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", issued_at=ISSUED
        )
        # tight time (Δ2min) + exact amount → tier 1
        op_a = await make_bank_operation(
            session, amount="1000.00", operation_date=SAME_DAY,
            posted_at=datetime(2026, 6, 10, 14, 32, tzinfo=UTC),
        )
        # posted_at unknown, same business date, amount within ±10% → tier 4 fallback
        op_b = await make_bank_operation(
            session, amount="1050.00", operation_date=SAME_DAY, posted_at=None,
        )
        await session.commit()

        sug = await suggest_invoice_matches_by_time(session, invoice_id=invoice.id)
        assert sug is not None
        assert [c.bank_operation_id for c in sug.candidates] == [op_a.id, op_b.id]
        assert sug.candidates[0].tier == 1 and sug.candidates[0].minutes_delta == 2
        assert sug.candidates[1].tier == 4 and sug.candidates[1].minutes_delta is None
        assert sug.confident is True  # exactly one strong (tier ≤ 2) candidate
        assert sug.remaining == Decimal("1000.00")


async def test_time_match_excludes_already_allocated_operation(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="P")
        invoice = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", issued_at=ISSUED
        )
        op = await make_bank_operation(
            session, amount="1000.00", operation_date=SAME_DAY,
            posted_at=datetime(2026, 6, 10, 14, 32, tzinfo=UTC),
        )
        other = await make_invoice(session, counterparty_id=cp.id, amount="1000.00", number="X2")
        session.add(
            InvoicePaymentAllocation(
                invoice_id=other.id, source_kind="bank",
                bank_operation_id=op.id, amount=Decimal("1000.00"),
            )
        )
        await session.commit()

        sug = await suggest_invoice_matches_by_time(session, invoice_id=invoice.id)
        assert sug is not None
        assert sug.candidates == []
        assert sug.confident is False


async def test_time_match_excludes_internal_transfer(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="P")
        invoice = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", issued_at=ISSUED
        )
        tg = TransferGroup(amount=Decimal("1000.00"), initiated_at=SAME_DAY)
        session.add(tg)
        await session.flush()
        await make_bank_operation(
            session, amount="1000.00", operation_date=SAME_DAY,
            posted_at=datetime(2026, 6, 10, 14, 32, tzinfo=UTC), transfer_group_id=tg.id,
        )
        await session.commit()

        sug = await suggest_invoice_matches_by_time(session, invoice_id=invoice.id)
        assert sug is not None and sug.candidates == []


async def test_time_match_excludes_incoming_and_over_tolerance(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="P")
        invoice = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", issued_at=ISSUED
        )
        await make_bank_operation(  # incoming → not an outgoing payment
            session, amount="1000.00", direction="in", operation_date=SAME_DAY,
            posted_at=datetime(2026, 6, 10, 14, 32, tzinfo=UTC),
        )
        await make_bank_operation(  # +20% → outside ±10%
            session, amount="1200.00", operation_date=SAME_DAY,
            posted_at=datetime(2026, 6, 10, 14, 32, tzinfo=UTC),
        )
        await session.commit()

        sug = await suggest_invoice_matches_by_time(session, invoice_id=invoice.id)
        assert sug is not None and sug.candidates == []


async def test_time_match_none_for_barter_and_receivable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="P")
        loan = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", issued_at=ISSUED, barter_role="loan"
        )
        ar = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", issued_at=ISSUED,
            direction="receivable",
        )
        await session.commit()

        assert await suggest_invoice_matches_by_time(session, invoice_id=loan.id) is None
        assert await suggest_invoice_matches_by_time(session, invoice_id=ar.id) is None


async def test_time_match_tight_window_rejects_far_same_day_op(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A same-calendar-day op that is actually ~12h off must NOT sneak past a tight window —
    the window applies whenever posted_at is known (else it could become 'confident')."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="P")
        invoice = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", issued_at=ISSUED
        )
        await make_bank_operation(  # same date, but 02:00 vs issued 14:30 → ~12.5h apart
            session, amount="1000.00", operation_date=SAME_DAY,
            posted_at=datetime(2026, 6, 10, 2, 0, tzinfo=UTC),
        )
        await session.commit()

        sug = await suggest_invoice_matches_by_time(
            session, invoice_id=invoice.id, window_hours=2
        )
        assert sug is not None and sug.candidates == []


async def test_time_match_fuzzy_amount_is_tier3_not_confident(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """±10% amount is allowed (tier 3) but a fuzzy amount alone is never confident — the
    owner's rule is that fuzzy amount only counts when paired with an exact match."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="P")
        invoice = await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", issued_at=ISSUED
        )
        await make_bank_operation(
            session, amount="1050.00", operation_date=SAME_DAY,
            posted_at=datetime(2026, 6, 10, 14, 35, tzinfo=UTC),
        )
        await session.commit()

        sug = await suggest_invoice_matches_by_time(session, invoice_id=invoice.id)
        assert sug is not None
        assert len(sug.candidates) == 1
        assert sug.candidates[0].tier == 3
        assert sug.confident is False

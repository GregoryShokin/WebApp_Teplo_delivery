"""Barter netting suggestions the UI highlights so the manager doesn't pick blind.

Confident = same rule as auto-settle (exact sum + exact product set, unique). Sum-only
matches (e.g. one loan ↔ a batched return whose products differ) are proposed too, but
flagged non-confident for a human to verify the номенклатура.
"""

from __future__ import annotations

from decimal import Decimal

from cp_helpers import make_counterparty, make_invoice
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.counterparty_barter_match import suggest_barter_matches


def items(*product_ids: str) -> list[dict[str, str]]:
    return [{"product_id": pid, "name": pid, "article": pid} for pid in product_ids]


async def _barter_cp(session: AsyncSession, inn: str):
    return await make_counterparty(session, name="Барт", inn=inn, relationship="barter")


async def test_suggest_confident_1to1(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await _barter_cp(session, "7710000010")
        p = await make_invoice(
            session, counterparty_id=cp.id, amount="2030.00", direction="payable",
            line_items=items("A"),
        )
        r = await make_invoice(
            session, counterparty_id=cp.id, amount="2030.00", direction="receivable",
            line_items=items("A"),
        )
        await session.commit()

        suggestions = await suggest_barter_matches(session, cp.id)
        assert len(suggestions) == 1
        suggestion = suggestions[0]
        assert suggestion.confident is True
        assert set(suggestion.payable_ids) == {p.id}
        assert set(suggestion.receivable_ids) == {r.id}
        assert suggestion.amount == Decimal("2030.00")


async def test_suggest_ambiguous_sum_only(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ёбидоёби-case: 1 payable ↔ 2 receivables by sum, products differ → not confident."""
    async with async_session_factory() as session:
        cp = await _barter_cp(session, "7710000011")
        p = await make_invoice(
            session, counterparty_id=cp.id, amount="465.00", direction="payable",
            line_items=items("X"),
        )
        r1 = await make_invoice(
            session, counterparty_id=cp.id, amount="200.00", direction="receivable",
            line_items=items("Y"),
        )
        r2 = await make_invoice(
            session, counterparty_id=cp.id, amount="265.00", direction="receivable",
            line_items=items("Z"),
        )
        await session.commit()

        suggestions = await suggest_barter_matches(session, cp.id)
        assert len(suggestions) == 1
        suggestion = suggestions[0]
        assert suggestion.confident is False
        assert set(suggestion.payable_ids) == {p.id}
        assert set(suggestion.receivable_ids) == {r1.id, r2.id}
        assert suggestion.amount == Decimal("465.00")


async def test_suggest_none_when_sums_differ(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await _barter_cp(session, "7710000012")
        await make_invoice(
            session, counterparty_id=cp.id, amount="100.00", direction="payable",
            line_items=items("A"),
        )
        await make_invoice(
            session, counterparty_id=cp.id, amount="250.00", direction="receivable",
            line_items=items("B"),
        )
        await session.commit()

        assert await suggest_barter_matches(session, cp.id) == []


async def test_suggest_non_overlapping(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two independent confident pairs → two suggestions, no shared invoice."""
    async with async_session_factory() as session:
        cp = await _barter_cp(session, "7710000013")
        p1 = await make_invoice(
            session, counterparty_id=cp.id, amount="100.00", direction="payable",
            line_items=items("A"),
        )
        r1 = await make_invoice(
            session, counterparty_id=cp.id, amount="100.00", direction="receivable",
            line_items=items("A"),
        )
        p2 = await make_invoice(
            session, counterparty_id=cp.id, amount="300.00", direction="payable",
            line_items=items("B"),
        )
        r2 = await make_invoice(
            session, counterparty_id=cp.id, amount="300.00", direction="receivable",
            line_items=items("B"),
        )
        await session.commit()

        suggestions = await suggest_barter_matches(session, cp.id)
        assert len(suggestions) == 2
        all_payable = {pid for s in suggestions for pid in s.payable_ids}
        all_receivable = {rid for s in suggestions for rid in s.receivable_ids}
        assert all_payable == {p1.id, p2.id}
        assert all_receivable == {r1.id, r2.id}
        assert all(s.confident for s in suggestions)

"""Barter partners: payable + receivable invoices, net balance, direction split.

Net barter balance is signed: positive = we owe them, negative = they owe us.
The inbox («К оплате») lists only payables; receivables are AR, not a bill.
"""

from __future__ import annotations

from decimal import Decimal

from cp_helpers import make_counterparty, make_invoice
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.counterparty_registry import (
    get_counterparty_card,
    list_invoices,
    list_registry,
)


async def test_card_barter_balance_nets_payable_minus_receivable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Ёбидоёби", inn="7706666666", relationship="barter"
        )
        await make_invoice(session, counterparty_id=cp.id, amount="1000.00", direction="payable")
        await make_invoice(session, counterparty_id=cp.id, amount="300.00", direction="receivable")
        await session.commit()

        card = await get_counterparty_card(session, cp.id)
        assert card.relationship == "barter"
        # We owe 1000, they owe us 300 → net 700 in their favour.
        assert card.barter_balance == Decimal("700.00")


async def test_card_barter_balance_negative_when_they_owe_more(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Пури-Пури", inn="7706660000", relationship="barter"
        )
        await make_invoice(session, counterparty_id=cp.id, amount="200.00", direction="payable")
        await make_invoice(session, counterparty_id=cp.id, amount="900.00", direction="receivable")
        await session.commit()

        card = await get_counterparty_card(session, cp.id)
        assert card.barter_balance == Decimal("-700.00")  # they owe us more


async def test_registry_splits_payable_and_receivable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Батутный парк", inn="7707777777", relationship="barter"
        )
        await make_invoice(session, counterparty_id=cp.id, amount="1000.00", direction="payable")
        await make_invoice(session, counterparty_id=cp.id, amount="300.00", direction="receivable")
        await session.commit()

        item = {row.counterparty_id: row for row in await list_registry(session)}[cp.id]
        assert item.relationship == "barter"
        assert item.unpaid_remaining == Decimal("1000.00")  # payable only
        assert item.receivable_remaining == Decimal("300.00")


async def test_inbox_lists_only_payables(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="X", inn="7708888888", relationship="barter")
        await make_invoice(
            session, counterparty_id=cp.id, amount="1000.00", direction="payable", number="AP-1"
        )
        await make_invoice(
            session, counterparty_id=cp.id, amount="300.00", direction="receivable", number="AR-1"
        )
        await session.commit()

        payables = await list_invoices(session, direction="payable")
        assert [item.number for item in payables] == ["AP-1"]
        receivables = await list_invoices(session, direction="receivable")
        assert [item.number for item in receivables] == ["AR-1"]


async def test_inbox_filters_by_relationship(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Inbox segment (Поставщики/Неофициальные/Бартер) filters by counterparty type."""
    async with async_session_factory() as session:
        official = await make_counterparty(
            session, name="Офиц", inn="7710000001", relationship="official"
        )
        informal = await make_counterparty(
            session, name="Нал", inn="7710000002", relationship="informal"
        )
        await make_invoice(session, counterparty_id=official.id, amount="100.00", number="OF-1")
        await make_invoice(session, counterparty_id=informal.id, amount="200.00", number="IN-1")
        await session.commit()

        official_only = await list_invoices(session, relationship="official", direction="payable")
        assert [item.number for item in official_only] == ["OF-1"]
        informal_only = await list_invoices(session, relationship="informal", direction="payable")
        assert [item.number for item in informal_only] == ["IN-1"]

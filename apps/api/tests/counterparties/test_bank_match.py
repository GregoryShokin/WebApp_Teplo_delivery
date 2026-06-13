"""Backlog reconciliation by amount + counterparty enrichment.

For each unpaid invoice, suggest T-Bank outgoing ops with the exact amount (excluding
card/acquirer noise); on manager confirm, allocate the op and optionally enrich the
counterparty with the payee's official name/INN/requisites from the receiver block.
"""

from __future__ import annotations

import pytest
from cp_helpers import make_bank_operation, make_counterparty, make_invoice
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Counterparty, CounterpartyPayableProfile
from app.services.counterparty_bank_match import (
    confirm_invoice_match,
    suggest_invoice_matches,
)
from app.services.counterparty_matching import CounterpartyMatchError

ACQUIRER_INN = "7710140679"
REAL_RECEIVER = {
    "name": "ООО Реальный Поставщик",
    "inn": "7705555555",
    "kpp": "770501001",
    "acct": "40702810400000099999",
    "bicRu": "044525225",
    "corAcct": "30101810400000000225",
}


async def test_suggest_matches_unpaid_invoice_by_exact_amount(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="iiko Поставщик", inn=None, status="requires_setup"
        )
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="5000.00", number="С-1")
        await make_bank_operation(
            session, amount="5000.00", inn="7705555555", receiver=REAL_RECEIVER
        )
        await make_bank_operation(
            session, amount="4999.00", inn="7705555555", receiver=REAL_RECEIVER
        )
        await session.commit()

        suggestions = await suggest_invoice_matches(session)

        assert len(suggestions) == 1
        suggestion = suggestions[0]
        assert suggestion.invoice_id == invoice.id
        assert suggestion.counterparty_has_inn is False
        assert len(suggestion.candidates) == 1  # only the exact-amount op
        assert suggestion.candidates[0].official_name == "ООО Реальный Поставщик"
        assert suggestion.candidates[0].inn == "7705555555"
        assert suggestion.confident is True


async def test_suggest_excludes_card_and_acquirer_noise(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="X", inn=None, status="requires_setup")
        await make_invoice(session, counterparty_id=cp.id, amount="5000.00")
        # category cardOperation
        await make_bank_operation(session, amount="5000.00", category="cardOperation")
        # acquirer INN
        await make_bank_operation(
            session, amount="5000.00", inn=ACQUIRER_INN, receiver={"inn": ACQUIRER_INN}
        )
        # name carries ТБанк
        await make_bank_operation(
            session, amount="5000.00", name="АО ТБанк", receiver={"name": "АО ТБанк"}
        )
        await session.commit()

        suggestions = await suggest_invoice_matches(session)

        assert suggestions == []  # all candidates filtered as noise


async def test_suggest_filters_payees_by_existing_counterparty_inn(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Известный", inn="7705555555")
        await make_invoice(session, counterparty_id=cp.id, amount="5000.00")
        await make_bank_operation(
            session, amount="5000.00", inn="7705555555", receiver=REAL_RECEIVER
        )
        # Same amount but a different payee INN → must be excluded.
        await make_bank_operation(
            session,
            amount="5000.00",
            inn="7700000000",
            receiver={"name": "Чужой", "inn": "7700000000"},
        )
        await session.commit()

        suggestions = await suggest_invoice_matches(session)

        assert len(suggestions) == 1
        assert suggestion_inns(suggestions[0]) == {"7705555555"}
        assert suggestions[0].counterparty_has_inn is True


async def test_suggest_not_confident_with_multiple_distinct_payees(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="X", inn=None, status="requires_setup")
        await make_invoice(session, counterparty_id=cp.id, amount="5000.00")
        await make_bank_operation(
            session, amount="5000.00", inn="7700000001", receiver={"name": "A", "inn": "7700000001"}
        )
        await make_bank_operation(
            session, amount="5000.00", inn="7700000002", receiver={"name": "B", "inn": "7700000002"}
        )
        await session.commit()

        suggestions = await suggest_invoice_matches(session)

        assert len(suggestions) == 1
        assert len(suggestions[0].candidates) == 2
        assert suggestions[0].confident is False


async def test_confirm_match_allocates_and_enriches_counterparty(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="iiko Поставщик", inn=None, status="requires_setup"
        )
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="5000.00")
        op = await make_bank_operation(
            session, amount="5000.00", inn="7705555555", receiver=REAL_RECEIVER
        )
        await session.commit()

        result = await confirm_invoice_match(
            session,
            invoice_id=invoice.id,
            bank_operation_id=op.id,
            enrich=True,
            actor_user_id=None,
        )

        assert result["payment_status"] == "paid"
        assert result["enriched"] is True

        refreshed = await session.get(Counterparty, cp.id)
        assert refreshed.name == "ООО Реальный Поставщик"
        assert refreshed.inn == "7705555555"
        assert refreshed.status == "active"  # promoted from requires_setup

        profile = await session.scalar(
            select(CounterpartyPayableProfile).where(
                CounterpartyPayableProfile.counterparty_id == cp.id
            )
        )
        assert profile.requisites_verified is True
        assert profile.requisites["bankBik"] == "044525225"


async def test_confirm_match_without_enrich_leaves_counterparty_untouched(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="iiko Поставщик", inn=None, status="requires_setup"
        )
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="5000.00")
        op = await make_bank_operation(
            session, amount="5000.00", inn="7705555555", receiver=REAL_RECEIVER
        )
        await session.commit()

        result = await confirm_invoice_match(
            session,
            invoice_id=invoice.id,
            bank_operation_id=op.id,
            enrich=False,
            actor_user_id=None,
        )

        assert result["payment_status"] == "paid"
        assert result["enriched"] is False
        refreshed = await session.get(Counterparty, cp.id)
        assert refreshed.name == "iiko Поставщик"
        assert refreshed.inn is None
        assert refreshed.status == "requires_setup"


async def test_confirm_match_rejects_inn_conflict_on_enrich(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Известный", inn="7701111111")
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="5000.00")
        op = await make_bank_operation(
            session, amount="5000.00", inn="7705555555", receiver=REAL_RECEIVER
        )
        await session.commit()

        with pytest.raises(CounterpartyMatchError, match="другой ИНН"):
            await confirm_invoice_match(
                session,
                invoice_id=invoice.id,
                bank_operation_id=op.id,
                enrich=True,
                actor_user_id=None,
            )


async def test_confirm_match_rejects_reused_operation(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="X", inn=None, status="requires_setup")
        inv1 = await make_invoice(session, counterparty_id=cp.id, amount="5000.00")
        inv2 = await make_invoice(session, counterparty_id=cp.id, amount="5000.00")
        op = await make_bank_operation(
            session, amount="5000.00", inn="7705555555", receiver=REAL_RECEIVER
        )
        await session.commit()

        await confirm_invoice_match(
            session,
            invoice_id=inv1.id,
            bank_operation_id=op.id,
            enrich=False,
            actor_user_id=None,
        )
        with pytest.raises(CounterpartyMatchError, match="уже использована"):
            await confirm_invoice_match(
                session,
                invoice_id=inv2.id,
                bank_operation_id=op.id,
                enrich=False,
                actor_user_id=None,
            )


async def test_suggest_ignores_receivable_invoices(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Receivables (AR) are money owed to us — never matched to outgoing payments."""
    async with async_session_factory() as session:
        cp = await make_counterparty(
            session, name="Барт", inn=None, status="requires_setup", relationship="barter"
        )
        await make_invoice(session, counterparty_id=cp.id, amount="5000.00", direction="receivable")
        await make_bank_operation(
            session, amount="5000.00", inn="7705555555", receiver=REAL_RECEIVER
        )
        await session.commit()

        assert await suggest_invoice_matches(session) == []


def suggestion_inns(suggestion) -> set[str]:
    return {candidate.inn for candidate in suggestion.candidates if candidate.inn}

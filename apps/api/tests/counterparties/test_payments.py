"""Payment drafts: pack invoices of one legal entity into a single T-Bank draft.

A draft is not a payment — invoices stay unpaid; sending requires verified requisites.
Runs against the mock bank client (``TEPLO_BANK_CLIENT_MODE=mock`` from conftest).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from cp_helpers import make_counterparty, make_draft, make_invoice
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.banking.tbank import build_payment_draft_api_payload
from app.services.counterparty_payments import (
    CounterpartyPaymentError,
    RequisitesNotVerifiedError,
    cancel_payment_draft,
    create_payment_draft_for_invoices,
    create_standalone_payment_draft,
)

VERIFIED_REQUISITES = {
    "bankAcnt": "40702810400000012345",
    "bankBik": "044525225",
    "recipientCorrAccountNumber": "30101810400000000225",
}


async def _verified_supplier(session: AsyncSession, **extra):
    return await make_counterparty(
        session,
        name="ООО Поставщик",
        inn="7701234567",
        requisites=VERIFIED_REQUISITES,
        requisites_verified=True,
        **extra,
    )


def test_build_payment_draft_rejects_invalid_inn_length() -> None:
    requisites = dict(
        VERIFIED_REQUISITES,
        recipientName="ООО Суши Принт",
        inn="773444316",
    )

    with pytest.raises(ValueError, match="10 или 12"):
        build_payment_draft_api_payload(
            document_id="teplo-cp-test",
            amount=Decimal("100.00"),
            purpose="Оплата поставщику",
            requisites=requisites,
            payer_account="40702810999999999999",
        )


def test_build_payment_draft_accepts_legal_entity_inn() -> None:
    requisites = dict(
        VERIFIED_REQUISITES,
        recipientName="ООО Поставщик",
        inn="7701234567",
    )

    payload = build_payment_draft_api_payload(
        document_id="teplo-cp-test",
        amount=Decimal("100.00"),
        purpose="Оплата поставщику",
        requisites=requisites,
        payer_account="40702810999999999999",
    )

    assert payload["inn"] == "7701234567"


async def test_create_draft_packs_invoices_and_links_them(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        supplier = await _verified_supplier(session)
        inv1 = await make_invoice(
            session,
            counterparty_id=supplier.id,
            amount="110.00",
            number="A-1",
            vat_total="10.00",
            vat_breakdown={"10": "10.00"},
        )
        inv2 = await make_invoice(
            session,
            counterparty_id=supplier.id,
            amount="122.00",
            number="A-2",
            vat_total="22.00",
            vat_breakdown={"22": "22.00"},
        )
        await session.commit()

        draft = await create_payment_draft_for_invoices(
            session, invoice_ids=[inv1.id, inv2.id], actor_user_id=None
        )

        assert draft.status == "created"
        assert draft.provider_ref == f"mock-{draft.document_id}"
        assert draft.amount == Decimal("232.00")

        await session.refresh(inv1)
        await session.refresh(inv2)
        assert inv1.draft_id == draft.id
        assert inv2.draft_id == draft.id
        # Drafting does not pay: invoices stay unpaid.
        assert inv1.payment_status == "unpaid"

        # VAT is stated in the payment purpose and the whole purpose fits the limit.
        purpose = draft.payload["paymentPurpose"]
        assert "НДС" in purpose
        assert len(purpose) <= 210


async def test_create_draft_blocked_without_verified_requisites(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        supplier = await make_counterparty(
            session,
            name="ООО Без реквизитов",
            inn="7709999999",
            requisites=VERIFIED_REQUISITES,
            requisites_verified=False,  # the gate
        )
        invoice = await make_invoice(session, counterparty_id=supplier.id, amount="500.00")
        await session.commit()

        with pytest.raises(RequisitesNotVerifiedError):
            await create_payment_draft_for_invoices(
                session, invoice_ids=[invoice.id], actor_user_id=None
            )


async def test_create_standalone_draft_rejects_invalid_inn_before_bank(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        supplier = await make_counterparty(
            session,
            name="ООО Суши Принт",
            inn="773444316",
            requisites=VERIFIED_REQUISITES,
            requisites_verified=True,
        )
        await session.commit()

        with pytest.raises(CounterpartyPaymentError, match="10 или 12"):
            await create_standalone_payment_draft(
                session,
                counterparty_id=supplier.id,
                amount=Decimal("1000.00"),
                actor_user_id=None,
            )


async def test_create_draft_rejects_mixed_counterparties(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        a = await _verified_supplier(session)
        b = await make_counterparty(session, name="Другое юрлицо", inn="7702222222")
        inv_a = await make_invoice(session, counterparty_id=a.id, amount="100.00")
        inv_b = await make_invoice(session, counterparty_id=b.id, amount="200.00")
        await session.commit()

        with pytest.raises(CounterpartyPaymentError, match="одного юрлица"):
            await create_payment_draft_for_invoices(
                session, invoice_ids=[inv_a.id, inv_b.id], actor_user_id=None
            )


async def test_create_draft_rejects_already_queued_invoice(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        supplier = await _verified_supplier(session)
        existing_draft = await make_draft(session, counterparty_id=supplier.id, amount="100.00")
        invoice = await make_invoice(
            session, counterparty_id=supplier.id, amount="100.00", draft_id=existing_draft.id
        )
        await session.commit()

        with pytest.raises(CounterpartyPaymentError, match="уже отправлены"):
            await create_payment_draft_for_invoices(
                session, invoice_ids=[invoice.id], actor_user_id=None
            )


async def test_create_draft_rejects_paid_invoice(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        supplier = await _verified_supplier(session)
        invoice = await make_invoice(
            session, counterparty_id=supplier.id, amount="100.00", payment_status="paid"
        )
        await session.commit()

        with pytest.raises(CounterpartyPaymentError):
            await create_payment_draft_for_invoices(
                session, invoice_ids=[invoice.id], actor_user_id=None
            )


async def test_cancel_draft_unlinks_invoices(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        supplier = await _verified_supplier(session)
        invoice = await make_invoice(session, counterparty_id=supplier.id, amount="150.00")
        await session.commit()
        draft = await create_payment_draft_for_invoices(
            session, invoice_ids=[invoice.id], actor_user_id=None
        )
        await session.refresh(invoice)
        assert invoice.draft_id == draft.id

        await cancel_payment_draft(session, draft_id=draft.id)

        await session.refresh(invoice)
        assert invoice.draft_id is None


async def test_cancel_paid_draft_is_rejected(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        supplier = await _verified_supplier(session)
        draft = await make_draft(
            session, counterparty_id=supplier.id, amount="100.00", status="paid"
        )
        await session.commit()

        with pytest.raises(CounterpartyPaymentError, match="оплачен"):
            await cancel_payment_draft(session, draft_id=draft.id)


async def test_create_draft_blocked_for_informal_counterparty(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        supplier = await make_counterparty(
            session,
            name="Местный закуп",
            inn="7709990000",
            requisites=VERIFIED_REQUISITES,
            requisites_verified=True,
            relationship="informal",  # paid by card/cash, never bank
        )
        invoice = await make_invoice(session, counterparty_id=supplier.id, amount="500.00")
        await session.commit()

        with pytest.raises(CounterpartyPaymentError, match="картой/наличными"):
            await create_payment_draft_for_invoices(
                session, invoice_ids=[invoice.id], actor_user_id=None
            )


async def test_create_draft_rejects_receivable_invoice(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        supplier = await _verified_supplier(session)
        invoice = await make_invoice(
            session, counterparty_id=supplier.id, amount="500.00", direction="receivable"
        )
        await session.commit()

        with pytest.raises(CounterpartyPaymentError, match="Доходные"):
            await create_payment_draft_for_invoices(
                session, invoice_ids=[invoice.id], actor_user_id=None
            )

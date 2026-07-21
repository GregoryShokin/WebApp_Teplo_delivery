"""Приоритет сопоставления контрагента для счетов из почты."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from cp_helpers import make_counterparty, make_invoice
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import CounterpartyCollectionSource, EmailInvoiceIntake
from app.services.email_invoice_ingest import (
    _match_or_create_counterparty,
    confirm_intake_with_review,
)
from app.services.invoice_recognition import RecognizedInvoice


async def test_invoice_inn_overrides_shared_sender_email(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Один почтовый ящик может выставлять счета за несколько юрлиц.

    ИНН в PDF однозначно указывает на получателя и не должен быть перебит старой
    привязкой адреса отправителя к другому поставщику.
    """
    sender = "anton.luchinskis@synapse-studio.ru"
    async with async_session_factory() as session:
        synapsis = await make_counterparty(session, name="Синапсис, ООО", inn="3525357535")
        oo = await make_counterparty(session, name="О. О, ООО", inn="3525346702")
        session.add(
            CounterpartyCollectionSource(
                counterparty_id=synapsis.id,
                kind="email",
                value=sender,
            )
        )
        await session.commit()

        counterparty_id = await _match_or_create_counterparty(
            session,
            RecognizedInvoice(recipient_name='ООО "о.О"', inn="3525346702"),
            sender,
        )

        assert counterparty_id == oo.id
        # Общий адрес остаётся привязанным к исходному поставщику для писем без ИНН.
        source = await session.scalar(
            select(CounterpartyCollectionSource).where(CounterpartyCollectionSource.value == sender)
        )
        assert source is not None
        assert source.counterparty_id == synapsis.id


async def test_review_reassigns_unpaid_linked_invoice_with_its_intake(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ручная правка контрагента должна переносить и активный платёж, не только intake."""
    async with async_session_factory() as session:
        wrong = await make_counterparty(session, name="Синапсис, ООО", inn="3525357535")
        right = await make_counterparty(session, name="О. О, ООО", inn="3525346702")
        invoice = await make_invoice(
            session,
            counterparty_id=wrong.id,
            amount="68000.00",
            source="email",
            doc_kind="bill",
            invoice_date=date(2026, 7, 16),
        )
        intake = EmailInvoiceIntake(
            mailbox="personal",
            from_addr="anton.luchinskis@synapse-studio.ru",
            attachment_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            status="linked",
            counterparty_id=wrong.id,
            invoice_id=invoice.id,
            recognition={
                "amount": "68000.00",
                "invoice_date": "2026-07-16",
                "document_kind": "invoice",
            },
        )
        session.add(intake)
        await session.commit()

        await confirm_intake_with_review(
            session,
            intake,
            actor_user_id=None,
            counterparty_id=right.id,
        )
        await session.commit()

        await session.refresh(intake)
        await session.refresh(invoice)
        assert intake.counterparty_id == right.id
        assert invoice.counterparty_id == right.id
        assert invoice.amount == Decimal("68000.00")

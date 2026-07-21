"""Приоритет сопоставления контрагента для счетов из почты."""

from __future__ import annotations

from cp_helpers import make_counterparty
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import CounterpartyCollectionSource
from app.services.email_invoice_ingest import _match_or_create_counterparty
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

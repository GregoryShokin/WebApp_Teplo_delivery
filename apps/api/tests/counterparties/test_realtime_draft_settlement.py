"""Near-realtime доводка ОПЛАТЫ черновика по входящей операции выписки.

``scheduler.settle_counterparty_draft_from_operation``: входящую подтверждённую Transaction
матчит к created/updated-черновику по точной сумме, назначению, счёту и documentNumber.
Статус payment/status не нужен: Transaction сама подтверждает фактическое списание.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from cp_helpers import make_counterparty, make_draft, make_invoice
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.scheduler import settle_counterparty_draft_from_operation
from app.services.banking.base import NormalizedBankOperation
from app.services.banking.tbank import _document_number


def _op(
    *,
    amount: str,
    document_number: str,
    purpose: str = "Оплата поставщику по счёту 1",
    account_number: str = "00000000000000000000",
    direction: str = "out",
    operation_status: str = "Transaction",
) -> NormalizedBankOperation:
    return NormalizedBankOperation(
        provider="tbank",
        provider_operation_id=f"op-{uuid.uuid4()}",
        account_number=account_number,
        operation_date=date.today(),
        direction=direction,
        amount=Decimal(amount),
        payment_purpose=purpose,
        document_number=str(document_number),
        raw_payload={"operationStatus": operation_status},
    )


async def _sent_draft(
    session: AsyncSession, cp_id, *, amount: str = "1000.00", provider_ref="pay-1"
):
    draft = await make_draft(session, counterparty_id=cp_id, amount=amount)
    draft.provider_ref = provider_ref
    draft.payload = {
        "paymentPurpose": "Оплата поставщику по счёту 1",
        "accountNumber": "00000000000000000000",
    }
    await session.flush()
    inv = await make_invoice(session, counterparty_id=cp_id, amount=amount, draft_id=draft.id)
    return draft, inv


async def test_matches_by_docnumber_and_settles_executed(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик")
        draft, inv = await _sent_draft(session, cp.id, amount="1000.00")
        await session.commit()

        op = _op(amount="1000.00", document_number=_document_number(draft.document_id))
        status = await settle_counterparty_draft_from_operation(session, operation=op)
        await session.commit()

        assert status == "paid"
        await session.refresh(draft)
        await session.refresh(inv)
        assert draft.status == "paid"
        assert inv.payment_status == "paid"


async def test_transaction_settles_without_waiting_for_payment_status(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Transaction + точный многофакторный матч достаточно для гашения черновика."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик")
        draft, _ = await _sent_draft(session, cp.id, amount="1000.00")
        await session.commit()

        op = _op(amount="1000.00", document_number=_document_number(draft.document_id))
        status = await settle_counterparty_draft_from_operation(session, operation=op)
        assert status == "paid"
        await session.refresh(draft)
        assert draft.status == "paid"


async def test_ambiguous_documentnumber_is_skipped(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Два created-черновика с ОДНИМ documentNumber и суммой → неоднозначно, не трогаем."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик")
        shared_doc_id = f"teplo-cp-{uuid.uuid4()}"
        d1 = await make_draft(
            session, counterparty_id=cp.id, amount="1000.00", document_id=shared_doc_id
        )
        d1.provider_ref = "pay-a"
        d1.payload = {
            "paymentPurpose": "Оплата поставщику по счёту 1",
            "accountNumber": "00000000000000000000",
        }
        d2 = await make_draft(
            session, counterparty_id=cp.id, amount="1000.00", document_id=shared_doc_id
        )
        d2.provider_ref = "pay-b"
        d2.payload = dict(d1.payload)
        await session.commit()

        op = _op(amount="1000.00", document_number=_document_number(shared_doc_id))
        status = await settle_counterparty_draft_from_operation(session, operation=op)
        assert status is None
        await session.refresh(d1)
        await session.refresh(d2)
        assert d1.status == "created" and d2.status == "created"


async def test_amount_mismatch_is_skipped(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик")
        draft, _ = await _sent_draft(session, cp.id, amount="1000.00")
        await session.commit()

        # documentNumber совпадает, а сумма другая → не наш платёж.
        op = _op(amount="999.00", document_number=_document_number(draft.document_id))
        status = await settle_counterparty_draft_from_operation(session, operation=op)
        assert status is None


async def test_purpose_mismatch_is_skipped(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик")
        draft, _ = await _sent_draft(session, cp.id, amount="1000.00")
        await session.commit()

        op = _op(
            amount="1000.00",
            document_number=_document_number(draft.document_id),
            purpose="Другое назначение",
        )
        assert await settle_counterparty_draft_from_operation(session, operation=op) is None


async def test_payer_account_mismatch_is_skipped(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик")
        draft, _ = await _sent_draft(session, cp.id, amount="1000.00")
        await session.commit()

        op = _op(
            amount="1000.00",
            document_number=_document_number(draft.document_id),
            account_number="11111111111111111111",
        )
        assert await settle_counterparty_draft_from_operation(session, operation=op) is None


async def test_marker_allows_match_without_document_number(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик")
        draft, _ = await _sent_draft(session, cp.id, amount="1000.00")
        marker = f"[TPL-{draft.id.hex[:12].upper()}]"
        draft.payload = {
            **draft.payload,
            "paymentPurpose": f"Оплата поставщику по счёту 1 {marker}",
        }
        await session.commit()

        op = _op(
            amount="1000.00",
            document_number="",
            purpose=draft.payload["paymentPurpose"],
        )
        assert await settle_counterparty_draft_from_operation(session, operation=op) == "paid"


async def test_incoming_operation_is_ignored(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик")
        draft, _ = await _sent_draft(session, cp.id, amount="1000.00")
        await session.commit()

        op = _op(
            amount="1000.00", document_number=_document_number(draft.document_id), direction="in"
        )
        status = await settle_counterparty_draft_from_operation(session, operation=op)
        assert status is None


async def test_authorization_is_not_treated_as_payment(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Authorization — только холд, автогашение разрешено исключительно для Transaction."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик")
        draft, _ = await _sent_draft(session, cp.id, amount="1000.00")
        await session.commit()

        op = _op(amount="1000.00", document_number=_document_number(draft.document_id))
        op.raw_payload["operationStatus"] = "Authorization"
        status = await settle_counterparty_draft_from_operation(session, operation=op)
        assert status is None
        await session.refresh(draft)
        assert draft.status == "created"


async def test_no_matching_draft_is_noop(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик")
        await _sent_draft(session, cp.id, amount="1000.00")
        await session.commit()

        op = _op(amount="1000.00", document_number="1")  # заведомо не совпадёт
        status = await settle_counterparty_draft_from_operation(session, operation=op)
        assert status is None

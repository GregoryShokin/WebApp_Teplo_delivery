"""Near-realtime доводка ОПЛАТЫ черновика по входящей операции выписки.

``scheduler.settle_counterparty_draft_from_operation``: входящую исходящую проведённую
операцию матчит к created/updated-черновику по documentNumber (детерминирован из
``draft.document_id``) + точной сумме и доводит АВТОРИТЕТНЫМ статусом банка
(get_payment_status), без доверия к телу операции. Замена 15-мин поллинга для оплаты;
удаление банк вебхуком не шлёт — тут не покрывается.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from cp_helpers import make_counterparty, make_draft, make_invoice
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.scheduler import settle_counterparty_draft_from_operation
from app.services.banking.base import NormalizedBankOperation
from app.services.banking.exceptions import BankFetchError
from app.services.banking.tbank import _document_number


class _FakeClient:
    def __init__(self, status: str | None) -> None:
        self._status = status
        self.calls: list[str] = []

    async def get_payment_status(self, payment_id: str) -> str | None:
        self.calls.append(payment_id)
        return self._status


class _RaisingClient:
    async def get_payment_status(self, payment_id: str) -> str | None:
        raise BankFetchError("tbank", "429")


def _op(
    *,
    amount: str,
    document_number: str,
    direction: str = "out",
) -> NormalizedBankOperation:
    return NormalizedBankOperation(
        provider="tbank",
        provider_operation_id=f"op-{uuid.uuid4()}",
        account_number="00000000000000000000",
        operation_date=date(2026, 7, 7),
        direction=direction,
        amount=Decimal(amount),
        document_number=str(document_number),
        raw_payload={},
    )


async def _sent_draft(session: AsyncSession, cp_id, *, amount: str = "1000.00", provider_ref="pay-1"):
    draft = await make_draft(session, counterparty_id=cp_id, amount=amount)
    draft.provider_ref = provider_ref
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

        client = _FakeClient("EXECUTED")
        op = _op(amount="1000.00", document_number=_document_number(draft.document_id))
        status = await settle_counterparty_draft_from_operation(session, operation=op, client=client)
        await session.commit()

        assert status == "paid"
        assert client.calls == ["pay-1"]  # спросили АВТОРИТЕТНЫЙ статус по provider_ref
        await session.refresh(draft)
        await session.refresh(inv)
        assert draft.status == "paid"
        assert inv.payment_status == "paid"


async def test_trusts_bank_status_not_operation_body(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Совпал по documentNumber+сумме, но банк говорит SUBMITTED (не исполнен) → не гасим."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик")
        draft, _ = await _sent_draft(session, cp.id, amount="1000.00")
        await session.commit()

        op = _op(amount="1000.00", document_number=_document_number(draft.document_id))
        status = await settle_counterparty_draft_from_operation(
            session, operation=op, client=_FakeClient("SUBMITTED")
        )
        assert status == "created"
        await session.refresh(draft)
        assert draft.status == "created"


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
        d2 = await make_draft(
            session, counterparty_id=cp.id, amount="1000.00", document_id=shared_doc_id
        )
        d2.provider_ref = "pay-b"
        await session.commit()

        client = _FakeClient("EXECUTED")
        op = _op(amount="1000.00", document_number=_document_number(shared_doc_id))
        status = await settle_counterparty_draft_from_operation(session, operation=op, client=client)
        assert status is None
        assert client.calls == []  # к банку не ходили — отдали поллингу
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

        client = _FakeClient("EXECUTED")
        # documentNumber совпадает, а сумма другая → не наш платёж.
        op = _op(amount="999.00", document_number=_document_number(draft.document_id))
        status = await settle_counterparty_draft_from_operation(session, operation=op, client=client)
        assert status is None and client.calls == []


async def test_incoming_operation_is_ignored(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик")
        draft, _ = await _sent_draft(session, cp.id, amount="1000.00")
        await session.commit()

        client = _FakeClient("EXECUTED")
        op = _op(amount="1000.00", document_number=_document_number(draft.document_id), direction="in")
        status = await settle_counterparty_draft_from_operation(session, operation=op, client=client)
        assert status is None and client.calls == []


async def test_bank_error_is_swallowed(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """429/сеть при опросе статуса не валит доводку и не трогает черновик (доберёт поллинг)."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик")
        draft, _ = await _sent_draft(session, cp.id, amount="1000.00")
        await session.commit()

        op = _op(amount="1000.00", document_number=_document_number(draft.document_id))
        status = await settle_counterparty_draft_from_operation(
            session, operation=op, client=_RaisingClient()
        )
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

        client = _FakeClient("EXECUTED")
        op = _op(amount="1000.00", document_number="1")  # заведомо не совпадёт
        status = await settle_counterparty_draft_from_operation(session, operation=op, client=client)
        assert status is None and client.calls == []

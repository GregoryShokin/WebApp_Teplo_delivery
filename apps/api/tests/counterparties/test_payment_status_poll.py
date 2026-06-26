"""Фоновый добор статуса платежей (замена ручного мэчинга): scheduler.run_payment_status_poll
опрашивает банк по «отправленным в банк» черновикам и гасит исполненные."""

from __future__ import annotations

from cp_helpers import make_counterparty, make_draft, make_invoice
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.scheduler import run_payment_status_poll
from app.services.banking.tbank import TbankClient


class _FakeClient:
    def __init__(self, status: str | None) -> None:
        self._status = status
        self.calls: list[str] = []

    async def get_payment_status(self, payment_id: str) -> str | None:
        self.calls.append(payment_id)
        return self._status


async def _sent_draft(session: AsyncSession, cp_id, *, provider_ref: str = "pay-1"):
    draft = await make_draft(session, counterparty_id=cp_id, amount="1000.00")
    draft.provider_ref = provider_ref
    await session.flush()
    inv = await make_invoice(session, counterparty_id=cp_id, amount="1000.00", draft_id=draft.id)
    return draft, inv


async def test_poll_settles_executed_draft(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="P")
        draft, inv = await _sent_draft(session, cp.id)
        await session.commit()

        client = _FakeClient("executed")
        result = await run_payment_status_poll(session, client=client)
        assert result == {"checked": 1, "paid": 1, "failed": 0, "errors": 0, "reconciled": 0}
        assert client.calls == ["pay-1"]
        await session.refresh(inv)
        assert inv.payment_status == "paid"


async def test_poll_leaves_pending_draft(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="P")
        draft, inv = await _sent_draft(session, cp.id)
        await session.commit()

        result = await run_payment_status_poll(session, client=_FakeClient("processing"))
        assert result["checked"] == 1 and result["paid"] == 0
        await session.refresh(draft)
        assert draft.status == "created"


async def test_poll_ignores_draft_without_provider_ref(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="P")
        # Черновик без provider_ref (банк ещё не вернул id) — в опрос не попадает.
        draft = await make_draft(session, counterparty_id=cp.id, amount="500.00")
        await make_invoice(session, counterparty_id=cp.id, amount="500.00", draft_id=draft.id)
        await session.commit()

        client = _FakeClient("executed")
        result = await run_payment_status_poll(session, client=client)
        assert result["checked"] == 0 and client.calls == []


class _FlakyClient:
    """get_payment_status: бросает по одним provider_ref, отдаёт статус по другим."""

    def __init__(self, mapping: dict[str, object]) -> None:
        self.mapping = mapping

    async def get_payment_status(self, payment_id: str) -> str | None:
        value = self.mapping.get(payment_id)
        if isinstance(value, Exception):
            raise value
        return value  # type: ignore[return-value]


async def test_poll_isolates_one_payment_error(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Одна банк-ошибка по платежу не валит весь проход и не откатывает уже погашенные."""
    from app.services.banking.exceptions import BankFetchError

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="P")
        _, bad_inv = await _sent_draft(session, cp.id, provider_ref="bad")
        _, good_inv = await _sent_draft(session, cp.id, provider_ref="good")
        await session.commit()

        client = _FlakyClient({"bad": BankFetchError("tbank", "500"), "good": "executed"})
        result = await run_payment_status_poll(session, client=client)
        assert result["paid"] == 1 and result["errors"] == 1
        await session.refresh(good_inv)
        await session.refresh(bad_inv)
        assert good_inv.payment_status == "paid"
        assert bad_inv.payment_status == "unpaid"  # не тронут, но и не потерян


async def test_get_payment_status_mock_returns_none(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        # mock-режим: платежи в банк не уходят → статуса нет.
        assert await TbankClient(session).get_payment_status("pay-1") is None

"""Фоновая доводка платежей: ``run_payment_status_poll`` опрашивает статусы черновиков
и добирает застрявшие ``SUBMITTED`` по подтверждённым операциям выписки."""

from __future__ import annotations

import uuid
from decimal import Decimal

from cp_helpers import make_account, make_counterparty, make_draft, make_invoice, make_wallet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_payroll_payouts import create_payroll_run

from app.models import BankOperation, PayrollBankDraft
from app.scheduler import run_payment_status_poll
from app.services.banking.tbank import TbankClient, _payment_status_from_payload


def test_tbank_status_payload_maps_deleted_and_regular_statuses() -> None:
    assert (
        _payment_status_from_payload(
            {"result": [{"documentId": "doc-1", "status": "EXECUTED"}]}, "doc-1"
        )
        == "EXECUTED"
    )
    assert (
        _payment_status_from_payload(
            {
                "result": [],
                "resultError": [{"documentId": "doc-1", "errorCode": "PAYMENT_NOT_FOUND"}],
            },
            "doc-1",
        )
        == "deleted"
    )


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
    draft.payload = {
        "paymentPurpose": "Оплата поставщику по счёту 1",
        "accountNumber": "00000000000000000000",
    }
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
        assert result == {
            "checked": 1,
            "paid": 1,
            "failed": 0,
            "deleted": 0,
            "errors": 0,
            "matched_by_operation": 0,
            "reconciled": 0,
            "absorbed": 0,
        }
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


async def test_poll_settles_submitted_draft_from_recorded_transaction(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Уже сохранённая Transaction закрывает SUBMITTED по сумме и назначению."""
    from app.services.banking.tbank import _document_number

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="P")
        draft, inv = await _sent_draft(session, cp.id)
        account = await make_account(
            session,
            account_number="40702819999999999999",
            legal_entity="ИП Шокина Е.А.",
        )
        await make_wallet(session, wallet_type="bank", account_id=account.id)
        draft.payload = {**draft.payload, "accountNumber": account.account_number}
        operation = BankOperation(
            id=uuid.uuid4(),
            provider="tbank",
            provider_operation_id="eco-center-operation",
            account_id=account.id,
            operation_date=draft.created_at.date(),
            direction="out",
            amount=Decimal("1000.00"),
            currency="RUB",
            payment_purpose=draft.payload["paymentPurpose"],
            document_number=_document_number(draft.document_id),
            raw_payload={"operationStatus": "Transaction"},
            classification_status="needs_review",
        )
        session.add(operation)
        await session.commit()

        result = await run_payment_status_poll(session, client=_FakeClient("SUBMITTED"))

        assert result["matched_by_operation"] == 1
        assert result["paid"] == 1
        await session.refresh(draft)
        await session.refresh(inv)
        await session.refresh(operation)
        assert draft.status == "paid"
        assert inv.payment_status == "paid"
        assert operation.classification_status == "classified"
        assert operation.cashflow_transaction_id is not None


async def test_poll_removes_deleted_draft_from_active_state(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Удалённый черновик")
        draft, inv = await _sent_draft(session, cp.id)
        await session.commit()

        result = await run_payment_status_poll(session, client=_FakeClient("deleted"))

        assert result["checked"] == 1 and result["deleted"] == 1
        await session.refresh(draft)
        await session.refresh(inv)
        assert draft.status == "deleted"
        assert draft.last_error == "Черновик удалён в банке"
        assert inv.draft_id is None


async def test_poll_marks_deleted_payroll_draft_ready_for_retry(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        _period, run, _employees = await create_payroll_run(session)
        draft = PayrollBankDraft(
            id=uuid.uuid4(),
            run_id=run.id,
            document_id=f"teplo-payroll-{run.id}",
            amount=Decimal("1000.00"),
            status="created",
            bank_provider="tbank",
            provider_ref="payroll-doc-1",
        )
        session.add(draft)
        await session.commit()

        client = _FakeClient("DELETED")
        result = await run_payment_status_poll(session, client=client)

        assert result["checked"] == 1 and result["deleted"] == 1
        assert client.calls == ["payroll-doc-1"]
        await session.refresh(draft)
        assert draft.status == "deleted"
        assert draft.last_error == "Черновик удалён в банке"


async def test_poll_uses_bank_provider_from_each_draft(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    async with async_session_factory() as session:
        tbank_cp = await make_counterparty(session, name="Т-Банк платёж")
        sber_cp = await make_counterparty(session, name="Сбер платёж")
        tbank_draft, _ = await _sent_draft(session, tbank_cp.id, provider_ref="t-doc")
        sber_draft, sber_invoice = await _sent_draft(session, sber_cp.id, provider_ref="s-doc")
        tbank_draft.bank_provider = "tbank"
        sber_draft.bank_provider = "sber"
        await session.commit()

        clients = {
            "tbank": _FakeClient("processing"),
            "sber": _FakeClient("deleted"),
        }
        monkeypatch.setattr(
            "app.scheduler.payout_client_for",
            lambda provider, _session: clients[provider],
        )

        result = await run_payment_status_poll(session)

        assert result["checked"] == 2 and result["deleted"] == 1
        assert clients["tbank"].calls == ["t-doc"]
        assert clients["sber"].calls == ["s-doc"]
        await session.refresh(sber_invoice)
        assert sber_invoice.draft_id is None


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

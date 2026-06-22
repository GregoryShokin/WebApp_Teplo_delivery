from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_payroll_payouts import (
    RecordingBankClient,
    create_actor_user,
    create_payroll_run,
)

from app.api.deps import CurrentActor
from app.api.v1.routes import payroll as payroll_routes
from app.models import AppSetting, PayrollBankDraft, PayrollLine, PayrollRunEvent
from app.services.payroll_payouts import (
    PAYOUT_REQUISITES_KEY,
    apply_run_payout_delta,
    create_or_update_run_draft,
    get_run_payout_delta,
    set_run_payout_cash,
)


async def test_run_delta_routes_call_run_level_services(monkeypatch) -> None:
    run_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    session = object()
    actor = CurrentActor(roles=frozenset({"manager"}), user_id=actor_id)
    calls: list[tuple[str, uuid.UUID, object]] = []

    async def fake_get_run_payout_delta(fake_session, fake_run_id):
        calls.append(("get", fake_run_id, fake_session))
        return {
            "run_id": fake_run_id,
            "document_id": "teplo-payroll-test",
            "previous_amount": Decimal("1000.00"),
            "new_amount": Decimal("1250.00"),
            "delta": Decimal("250.00"),
            "classification": "topup",
        }

    async def fake_apply_run_payout_delta(fake_session, fake_run_id, *, actor_user_id):
        calls.append(("apply", fake_run_id, fake_session))
        assert actor_user_id == actor_id
        return 1

    monkeypatch.setattr(payroll_routes, "get_run_payout_delta", fake_get_run_payout_delta)
    monkeypatch.setattr(payroll_routes, "apply_run_payout_delta", fake_apply_run_payout_delta)

    delta = await payroll_routes.get_run_bank_draft_delta(run_id, session, actor)
    applied = await payroll_routes.post_apply_run_bank_draft_delta(run_id, session, actor)

    assert delta["classification"] == "topup"
    assert applied.applied_count == 1
    assert calls == [("get", run_id, session), ("apply", run_id, session)]


async def test_create_run_draft_uses_run_account_part_for_one_bank_draft(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, _employees = await create_payroll_run(
            session,
            employee_line_totals=[
                [Decimal("1000")],
                [Decimal("2000")],
                [Decimal("3000")],
            ],
        )
        # Total payable is 6000; owner pays 2250 in cash, so 3750 goes to the account.
        # Сейф-модель требует наличный счёт при cash > 0 (введено унификацией ЗП, фаза 4.1).
        await set_run_payout_cash(
            session,
            run.id,
            amount_cash=Decimal("2250"),
            cash_wallet_code="tk_chernikova",
            actor_user_id=actor.id,
        )

        draft = await create_or_update_run_draft(session, run.id, actor_user_id=actor.id)
        rows = (
            await session.scalars(select(PayrollBankDraft).where(PayrollBankDraft.run_id == run.id))
        ).all()
        event = await _event(session, run.id, "bank_draft_created")

        assert rows == [draft]
        assert draft.document_id == f"teplo-payroll-{run.id}"
        assert draft.amount == Decimal("3750.00")
        assert event.payload["amount_account"] == "3750.00"


async def test_run_draft_is_idempotent_by_document_id(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, _employees = await create_payroll_run(session)
        await set_run_payout_cash(session, run.id, amount_cash=Decimal("0"), actor_user_id=actor.id)

        first = await create_or_update_run_draft(session, run.id, actor_user_id=actor.id)
        second = await create_or_update_run_draft(session, run.id, actor_user_id=actor.id)
        rows = (
            await session.scalars(select(PayrollBankDraft).where(PayrollBankDraft.run_id == run.id))
        ).all()

        assert len(rows) == 1
        assert second.id == first.id
        assert second.document_id == f"teplo-payroll-{run.id}"
        assert second.status == "updated"


async def test_run_draft_uses_requisites_from_setting(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, _employees = await create_payroll_run(session)
        await set_run_payout_cash(session, run.id, amount_cash=Decimal("0"), actor_user_id=actor.id)
        setting = await session.scalar(
            select(AppSetting).where(AppSetting.key == PAYOUT_REQUISITES_KEY)
        )
        assert setting is not None
        setting.value = {
            "recipientName": "TEST RECIPIENT",
            "inn": "123456789012",
            "kpp": "0",
            "bankAcnt": "40817810800023540968",
            "bankBik": "044525974",
            "corrAccount": "30101810145250000974",
            "executionOrder": 5,
            "paymentPurpose": "Payroll {start} {end}",
        }
        await session.commit()
        bank_client = RecordingBankClient()

        draft = await create_or_update_run_draft(
            session,
            run.id,
            actor_user_id=actor.id,
            bank_client=bank_client,
        )

        assert draft.payload["recipientName"] == "TEST RECIPIENT"
        assert draft.payload["paymentPurpose"].startswith("Payroll 2026-")
        assert bank_client.drafts[0]["requisites"]["recipientName"] == "TEST RECIPIENT"


async def test_run_delta_topup_creates_separate_draft_and_down_records_overpaid(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, _employees = await create_payroll_run(session)
        await set_run_payout_cash(session, run.id, amount_cash=Decimal("0"), actor_user_id=actor.id)
        await create_or_update_run_draft(session, run.id, actor_user_id=actor.id)
        line = await session.scalar(select(PayrollLine).where(PayrollLine.run_id == run.id))
        assert line is not None

        line.total_payable = Decimal("1250.00")
        await session.commit()
        delta = await get_run_payout_delta(session, run.id)
        assert delta["classification"] == "topup"
        assert delta["delta"] == Decimal("250.00")

        bank_client = RecordingBankClient()
        applied = await apply_run_payout_delta(
            session,
            run.id,
            actor_user_id=actor.id,
            bank_client=bank_client,
        )
        topup_event = await _event(session, run.id, "bank_draft_topup")
        assert applied == 1
        assert bank_client.drafts[0]["document_id"] == f"teplo-payroll-{run.id}-topup-1"
        assert topup_event.payload["delta"] == "250.00"

        line.total_payable = Decimal("900.00")
        await session.commit()
        applied = await apply_run_payout_delta(session, run.id, actor_user_id=actor.id)
        overpaid_event = await _event(session, run.id, "payout_overpaid")
        draft = await session.scalar(
            select(PayrollBankDraft).where(PayrollBankDraft.run_id == run.id)
        )

        assert applied == 1
        assert draft is not None
        assert draft.amount == Decimal("900.00")
        assert overpaid_event.payload["overpaid_amount"] == "350.00"


async def _event(
    session: AsyncSession,
    run_id,
    action: str,
) -> PayrollRunEvent:
    event = await session.scalar(
        select(PayrollRunEvent).where(
            PayrollRunEvent.run_id == run_id,
            PayrollRunEvent.action == action,
        )
    )
    assert event is not None
    return event

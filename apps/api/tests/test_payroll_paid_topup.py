"""Доплата после перевода на карту ИП не повторяет первоначальные деньги."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin_payout_split import _payer_wallet, _safe_wallet
from test_payroll_payouts import (
    RecordingBankClient,
    create_actor_user,
    create_payroll_run,
    patch_runner_recompute,
)

from app.models import CashflowTransaction, PayrollBankDraft, PayrollPayment, SafeAllocation
from app.schemas.payroll import PayrollBankDraftRead
from app.services import payroll_payouts
from app.services.payments_aggregator import _payroll_bank_draft_items
from app.services.payroll_runner import (
    PayrollConflictError,
    finalize_payroll_run,
    run_payroll,
    unfinalize_payroll_run,
)


@pytest.mark.parametrize(
    ("original", "recalculated", "delta"),
    [
        (Decimal("1000"), Decimal("1250"), Decimal("250")),
        (Decimal("128850"), Decimal("130360"), Decimal("1510")),
    ],
)
async def test_paid_run_topup_credits_only_delta_and_keeps_original_transfer(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    original: Decimal,
    recalculated: Decimal,
    delta: Decimal,
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        await _payer_wallet(session)
        safe = await _safe_wallet(session)
        period, run, employees = await create_payroll_run(
            session, employee_line_totals=[[original]]
        )
        client = RecordingBankClient()
        draft = await payroll_payouts.create_or_update_run_draft(
            session,
            run.id,
            actor_user_id=actor.id,
            bank_client=client,
        )
        assert draft is not None
        await payroll_payouts.apply_payroll_draft_status(
            session,
            draft=draft,
            raw_status="executed",
            operation_date=date(2026, 6, 1),
        )
        before = list(
            (
                await session.scalars(
                    select(CashflowTransaction).where(
                        CashflowTransaction.source_kind == payroll_payouts.BANK_TO_SAFE_SOURCE_KIND,
                        CashflowTransaction.source_id == run.id,
                    )
                )
            ).all()
        )
        assert len(before) == 2 and {entry.amount for entry in before} == {original}
        with pytest.raises(PayrollConflictError, match="уже оплачен"):
            await payroll_payouts.create_or_update_run_draft(
                session,
                run.id,
                actor_user_id=actor.id,
                bank_client=client,
            )
        assert len(client.drafts) == 1

        await unfinalize_payroll_run(
            session, run.id, reason="Добавлена пропущенная смена", actor_user_id=actor.id
        )
        await patch_runner_recompute(monkeypatch, {employees[0].id: recalculated})
        run = await run_payroll(session, period.id, force_refresh=True)
        await finalize_payroll_run(session, run.id, finalized_by_user_id=actor.id)
        assert (await payroll_payouts.get_run_payout_delta(session, run.id))["delta"] == delta
        await payroll_payouts.apply_run_payout_delta(
            session,
            run.id,
            actor_user_id=actor.id,
            bank_client=client,
        )
        assert client.drafts[-1]["amount"] == delta
        assert client.drafts[-1]["document_id"].endswith("-topup-1")
        await session.refresh(draft)
        read = PayrollBankDraftRead.model_validate(draft)
        assert read.amount == recalculated  # cumulative baseline must not change
        assert read.payment_amount == delta  # current request is a separate payment
        assert read.document_id == client.drafts[-1]["document_id"]
        items = await _payroll_bank_draft_items(session)
        item = next(item for item in items if item.extra.get("run_id") == str(run.id))
        assert item.amount == delta and item.state == "in_bank"
        assert item.title.startswith("Доплата · ")
        assert Decimal(item.extra["cumulative_amount"]) == recalculated
        with pytest.raises(PayrollConflictError, match="доплата уже создана"):
            await payroll_payouts.create_or_update_run_draft(
                session, run.id, actor_user_id=actor.id, bank_client=client
            )
        assert (
            await payroll_payouts.apply_run_payout_delta(
                session,
                run.id,
                actor_user_id=actor.id,
                bank_client=client,
            )
            == 0
        )
        assert len(client.drafts) == 2

        draft = await session.scalar(
            select(PayrollBankDraft).where(PayrollBankDraft.run_id == run.id)
        )
        await payroll_payouts.apply_payroll_draft_status(
            session,
            draft=draft,
            raw_status="executed",
            operation_date=date(2026, 6, 2),
        )
        await payroll_payouts.apply_payroll_draft_status(
            session, draft=draft, raw_status="executed"
        )
        transfers = list(
            (
                await session.scalars(
                    select(CashflowTransaction).where(
                        CashflowTransaction.source_kind == payroll_payouts.BANK_TO_SAFE_SOURCE_KIND,
                        CashflowTransaction.source_id == run.id,
                    )
                )
            ).all()
        )
        assert len(transfers) == 4
        assert {entry.id for entry in before}.issubset({entry.id for entry in transfers})
        assert sum(entry.amount for entry in transfers if entry.direction == "in") == recalculated
        assert len([entry for entry in transfers if entry.operation_date == date(2026, 6, 2)]) == 2
        reserve = await session.scalar(
            select(SafeAllocation).where(
                SafeAllocation.source_run_id == run.id,
                SafeAllocation.wallet_id == safe.id,
                SafeAllocation.status.in_(("reserved", "partially_paid")),
            )
        )
        assert reserve is not None and reserve.amount == recalculated


async def test_pending_topup_stays_active_even_if_employees_are_already_paid(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        _period, run, employees = await create_payroll_run(
            session, employee_line_totals=[[Decimal("130360")]]
        )
        session.add(
            PayrollBankDraft(
                run_id=run.id,
                document_id=f"teplo-payroll-{run.id}-topup-1",
                amount=Decimal("130360"),
                status="updated",
                payload={"last_action": "topup", "payload": {"amount": 1510}},
            )
        )
        session.add(
            PayrollPayment(
                run_id=run.id,
                employee_id=employees[0].id,
                amount=Decimal("130360"),
                status="paid",
            )
        )
        await session.commit()
        item = next(
            item
            for item in await _payroll_bank_draft_items(session)
            if item.extra.get("run_id") == str(run.id)
        )
        assert item.state == "in_bank" and item.amount == Decimal("1510")


@pytest.mark.parametrize("bank_request", [None, {}, {"amount": "invalid"}, {"amount": "NaN"}])
def test_incomplete_topup_never_shows_cumulative_amount(bank_request) -> None:
    draft = PayrollBankDraft(
        amount=Decimal("130360"), payload={"last_action": "topup", "payload": bank_request}
    )
    assert draft.payment_amount == Decimal("0.00")

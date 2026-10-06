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

from app.models import CashflowTransaction, PayrollBankDraft, SafeAllocation
from app.services import payroll_payouts
from app.services.payroll_runner import (
    PayrollConflictError,
    finalize_payroll_run,
    run_payroll,
    unfinalize_payroll_run,
)


async def test_paid_run_topup_credits_only_delta_and_keeps_original_transfer(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        await _payer_wallet(session)
        safe = await _safe_wallet(session)
        period, run, employees = await create_payroll_run(session)
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
        assert len(before) == 2 and {entry.amount for entry in before} == {Decimal("1000")}
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
        await patch_runner_recompute(monkeypatch, {employees[0].id: Decimal("1250")})
        run = await run_payroll(session, period.id, force_refresh=True)
        await finalize_payroll_run(session, run.id, finalized_by_user_id=actor.id)
        assert (await payroll_payouts.get_run_payout_delta(session, run.id))["delta"] == Decimal(
            "250"
        )
        await payroll_payouts.apply_run_payout_delta(
            session,
            run.id,
            actor_user_id=actor.id,
            bank_client=client,
        )
        assert client.drafts[-1]["amount"] == Decimal("250")
        assert client.drafts[-1]["document_id"].endswith("-topup-1")
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
        assert sum(entry.amount for entry in transfers if entry.direction == "in") == Decimal(
            "1250"
        )
        assert len([entry for entry in transfers if entry.operation_date == date(2026, 6, 2)]) == 2
        reserve = await session.scalar(
            select(SafeAllocation).where(
                SafeAllocation.source_run_id == run.id,
                SafeAllocation.wallet_id == safe.id,
                SafeAllocation.status.in_(("reserved", "partially_paid")),
            )
        )
        assert reserve is not None and reserve.amount == Decimal("1250")

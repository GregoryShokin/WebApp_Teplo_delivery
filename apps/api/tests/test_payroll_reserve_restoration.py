"""Refinalization restores funded payroll pools without repeating money movements."""

import asyncio
from decimal import Decimal

from sqlalchemy import select
from test_payroll_pool_payout import OP_DATE, PAID_AT, THREE, _setup_run_with_reserves

from app.models import (
    CashflowTransaction,
    PayrollBankDraft,
    PayrollLine,
    PayrollRun,
    SafeAllocation,
)
from app.services.banking.payment_purpose import owner_card_payment_purpose
from app.services.payroll_payments import mark_partial_payment
from app.services.payroll_payouts import (
    apply_payroll_draft_status,
    apply_run_payout_delta,
    book_bank_to_safe_transfer,
)
from app.services.payroll_reserves import (
    cancel_run_reserves,
    pay_run_from_pool,
    restore_run_reserves,
    transfer_run_reserve,
)
from app.services.payroll_runner import finalize_payroll_run, unfinalize_payroll_run


async def _postings(session):
    return (
        await session.execute(
            select(
                CashflowTransaction.id,
                CashflowTransaction.wallet_id,
                CashflowTransaction.direction,
                CashflowTransaction.amount,
                CashflowTransaction.source_id,
                CashflowTransaction.quality_status,
            ).order_by(CashflowTransaction.id)
        )
    ).all()


async def _active(session, run_id):
    return {
        r.location: r
        for r in (
            await session.scalars(
                select(SafeAllocation).where(
                    SafeAllocation.source_run_id == run_id,
                    SafeAllocation.status.in_(("reserved", "partially_paid")),
                )
            )
        ).all()
    }


async def test_refinalize_restores_real_case_without_reserving_unpaid_topup(async_session_factory):
    async with async_session_factory() as session:
        run_id, _employees, actor = await _setup_run_with_reserves(
            session, totals=[[Decimal("164275")]], cash=Decimal("35425")
        )
        old = await _active(session, run_id)
        before = await _postings(session)
        await unfinalize_payroll_run(session, run_id, reason="Пропущена смена", actor_user_id=actor)
        line = await session.scalar(select(PayrollLine).where(PayrollLine.run_id == run_id))
        line.total_payable = Decimal("165785")
        session.add(
            PayrollBankDraft(
                run_id=run_id,
                document_id=f"teplo-payroll-{run_id}-topup-1",
                amount=Decimal("130360"),
                status="updated",
                provider_ref="pending-topup",
                payload={
                    "last_action": "topup",
                    "payload": {
                        "amount": 1510,
                        "paymentPurpose": owner_card_payment_purpose(
                            f"teplo-payroll-{run_id}-topup-1"
                        ),
                    },
                },
            )
        )
        await session.commit()
        await finalize_payroll_run(session, run_id, finalized_by_user_id=actor)
        pools = await _active(session, run_id)
        assert pools["kassa"].amount == Decimal("35425")
        assert pools["safe"].amount == Decimal("128850")  # not the draft baseline 130360
        assert all(pool.amount_paid == 0 for pool in pools.values())
        assert all(pool.status == "cancelled" for pool in old.values())
        assert await _postings(session) == before
        draft = await session.scalar(
            select(PayrollBankDraft).where(PayrollBankDraft.run_id == run_id)
        )
        await apply_payroll_draft_status(session, draft=draft, raw_status="executed")
        funded_pools = await _active(session, run_id)
        assert funded_pools["safe"].amount == Decimal("130360")
        assert funded_pools["kassa"].amount == Decimal("35425")
        funded_postings = await _postings(session)
        assert len(funded_postings) == len(before) + 2
        await apply_payroll_draft_status(session, draft=draft, raw_status="executed")
        assert await _postings(session) == funded_postings
        run = await session.get(PayrollRun, run_id)
        assert await restore_run_reserves(session, run) == 0
        await session.commit()
        assert {k: r.id for k, r in (await _active(session, run_id)).items()} == {
            k: r.id for k, r in pools.items()
        }
        assert await _postings(session) == funded_postings


async def test_refinalize_keeps_partial_paid_amount_and_does_not_repeat_expense(
    async_session_factory,
):
    async with async_session_factory() as session:
        run_id, employees, actor = await _setup_run_with_reserves(
            session, totals=[[Decimal("1000")]], cash=Decimal("0")
        )
        await mark_partial_payment(
            session,
            run_id,
            employees[0],
            amount=Decimal("600"),
            paid_at=PAID_AT,
            actor_user_id=actor,
        )
        before = await _postings(session)
        await unfinalize_payroll_run(session, run_id, reason="Проверка", actor_user_id=actor)
        await finalize_payroll_run(session, run_id, finalized_by_user_id=actor)
        pools = await _active(session, run_id)
        assert pools["safe"].amount == Decimal("1000")
        assert pools["safe"].amount_paid == Decimal("600")
        assert pools["safe"].status == "partially_paid"
        assert await _postings(session) == before


async def test_refinalize_fully_paid_run_does_not_recreate_spent_pool(async_session_factory):
    async with async_session_factory() as session:
        run_id, _employees, actor = await _setup_run_with_reserves(
            session, totals=[[Decimal("1000")]], cash=Decimal("0")
        )
        pool = (await _active(session, run_id))["safe"]
        await pay_run_from_pool(session, reserve_id=pool.id, paid_at=PAID_AT, actor_user_id=actor)
        before = await _postings(session)
        reserve_ids = list((await session.scalars(select(SafeAllocation.id))).all())
        await unfinalize_payroll_run(session, run_id, reason="Проверка", actor_user_id=actor)
        await finalize_payroll_run(session, run_id, finalized_by_user_id=actor)
        assert not await _active(session, run_id)
        assert list((await session.scalars(select(SafeAllocation.id))).all()) == reserve_ids
        assert await _postings(session) == before


async def test_refinalize_preserves_recorded_safe_to_kassa_transfer(async_session_factory):
    async with async_session_factory() as session:
        run_id, employees, actor = await _setup_run_with_reserves(
            session, totals=THREE, cash=Decimal("2000")
        )
        pool = (await _active(session, run_id))["safe"]
        await transfer_run_reserve(
            session,
            reserve_id=pool.id,
            selected_ids={employees[0]},
            operation_date=OP_DATE,
            actor_user_id=actor,
        )
        before = await _postings(session)
        await unfinalize_payroll_run(session, run_id, reason="Пересчёт", actor_user_id=actor)
        await finalize_payroll_run(session, run_id, finalized_by_user_id=actor)
        pools = await _active(session, run_id)
        assert pools["safe"].amount == Decimal("3000")
        assert pools["kassa"].amount == Decimal("3000")
        assert await _postings(session) == before


async def test_repeat_transit_repairs_old_missing_pools_without_new_transfer(async_session_factory):
    async with async_session_factory() as session:
        run_id, _employees, _actor = await _setup_run_with_reserves(
            session, totals=THREE, cash=Decimal("2000")
        )
        await cancel_run_reserves(session, run_id)
        await session.commit()
        before = await _postings(session)
        run = await session.get(PayrollRun, run_id)
        assert not await book_bank_to_safe_transfer(session, run, operation_date=OP_DATE)
        await session.commit()
        assert {k: p.amount for k, p in (await _active(session, run_id)).items()} == {
            "kassa": Decimal("2000"),
            "safe": Decimal("4000"),
        }
        assert await _postings(session) == before


async def test_unchanged_delta_repairs_missing_pools_without_bank_call(async_session_factory):
    async with async_session_factory() as session:
        run_id, _employees, actor = await _setup_run_with_reserves(
            session, totals=THREE, cash=Decimal("2000")
        )
        session.add(
            PayrollBankDraft(
                run_id=run_id,
                document_id=f"teplo-payroll-{run_id}",
                amount=Decimal("4000"),
                status="paid",
                payload={"amount": 4000},
            )
        )
        await cancel_run_reserves(session, run_id)
        await session.commit()
        before = await _postings(session)
        assert await apply_run_payout_delta(session, run_id, actor_user_id=actor) == 0
        assert len(await _active(session, run_id)) == 2
        assert await _postings(session) == before


async def test_open_run_receipt_waits_for_refinalization_before_restoring_pools(
    async_session_factory,
):
    async with async_session_factory() as session:
        run_id, _employees, actor = await _setup_run_with_reserves(
            session, totals=THREE, cash=Decimal("2000")
        )
        await unfinalize_payroll_run(session, run_id, reason="Пересчёт", actor_user_id=actor)
        run = await session.get(PayrollRun, run_id)
        assert await restore_run_reserves(session, run) == 0
        assert not await _active(session, run_id)
        before = await _postings(session)
        assert not await book_bank_to_safe_transfer(session, run)
        assert not await _active(session, run_id)
        await finalize_payroll_run(session, run_id, finalized_by_user_id=actor)
        assert len(await _active(session, run_id)) == 2
        assert await _postings(session) == before


async def test_concurrent_repairs_create_only_one_pool_per_location(async_session_factory):
    async with async_session_factory() as session:
        run_id, _employees, _actor = await _setup_run_with_reserves(
            session, totals=THREE, cash=Decimal("2000")
        )
        await cancel_run_reserves(session, run_id)
        await session.commit()
        before = await _postings(session)

    async def repair():
        async with async_session_factory() as session:
            run = await session.get(PayrollRun, run_id)
            changed = await restore_run_reserves(session, run)
            await session.commit()
            return changed

    assert sorted(await asyncio.gather(repair(), repair())) == [0, 2]
    async with async_session_factory() as session:
        assert len(await _active(session, run_id)) == 2
        assert await _postings(session) == before


async def test_excluded_bank_receipt_cannot_fund_a_restored_safe_pool(async_session_factory):
    async with async_session_factory() as session:
        run_id, _employees, _actor = await _setup_run_with_reserves(
            session, totals=THREE, cash=Decimal("0")
        )
        await cancel_run_reserves(session, run_id)
        for posting in (
            await session.scalars(
                select(CashflowTransaction).where(
                    CashflowTransaction.source_id == run_id, CashflowTransaction.direction == "in"
                )
            )
        ).all():
            posting.quality_status = "excluded"
        await session.commit()
        before = await _postings(session)
        run = await session.get(PayrollRun, run_id)
        assert await restore_run_reserves(session, run) == 0
        assert not await _active(session, run_id)
        assert await _postings(session) == before

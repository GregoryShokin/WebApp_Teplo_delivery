"""Active payments issue every obligation; DDS never treats a deposit as salary."""

import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from test_deposit_payout_expense import _seed_deposit_article
from test_payroll_payments import create_actor_user, create_payroll_run
from test_payroll_payouts import fund_wallet
from test_payroll_pool_payout import OP_DATE, PAID_AT, _payment, _reserve, _seed_bank_payer
from test_payroll_reserve_plan import edit, item

from app.api.deps import CurrentActor
from app.api.v1.routes.payroll import get_lines
from app.models import (
    CashflowTransaction,
    DdsArticle,
    PayrollLine,
    PayrollPayment,
    PayrollPayoutBooking,
    PayrollRun,
)
from app.services.kassa.payouts import kassa_pending_payload
from app.services.payroll_obligations import DDS_ARTICLE_DEPOSIT_PAYOUT, run_obligations
from app.services.payroll_payments import mark_payment, unmark_payment
from app.services.payroll_payout_allocation import (
    DDS_ARTICLE_ADMIN_PAYROLL,
    DDS_ARTICLE_AUX_PAYROLL,
    DDS_ARTICLE_PRODUCTION_PAYROLL,
)
from app.services.payroll_payouts import (
    book_bank_to_safe_transfer,
    book_payout_expense_for_employees,
    set_run_payout_cash,
)
from app.services.payroll_reserve_plan import get_reserve_plan, save_reserve_plan
from app.services.payroll_reserves import (
    pay_run_from_pool,
    reconcile_run_reserves,
    run_payment_settlement,
    run_solvency,
)
from app.services.payroll_runner import PayrollConflictError, _run_payment_metrics

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def iiko_calls(monkeypatch):
    calls = []

    async def fake_post(session, *, amount, payout_date, source_id):
        # The payroll transaction has already committed before the external mirror.
        assert not session.in_transaction()
        calls.append((amount, payout_date, source_id))

    monkeypatch.setattr(
        "app.services.deposit_iiko_payout_production.post_production_deposit_payout_to_iiko",
        fake_post,
    )
    return calls


async def setup(session, *, location="safe", salary="10205", deposit="20000", mixed=False):
    await _seed_bank_payer(session)
    await fund_wallet(session, "tk_chernikova")
    await _seed_deposit_article(session)
    actor = await create_actor_user(session)
    _period, run, employees = await create_payroll_run(
        session,
        employee_line_totals=[
            [Decimal("1000"), Decimal("2000"), Decimal("3000")] if mixed else [Decimal(salary)]
        ],
    )
    lines = list(
        (
            await session.scalars(
                select(PayrollLine).where(PayrollLine.run_id == run.id).order_by(PayrollLine.role)
            )
        ).all()
    )
    for line, role in zip(lines, ["Сушист", "Менеджер", "Уборщица"], strict=False):
        line.role = role
    lines[0].deposit_payout_scheduled = Decimal(deposit)
    await session.flush()
    total = sum((line.total_payable for line in lines), Decimal(0)) + Decimal(deposit)
    await set_run_payout_cash(
        session,
        run.id,
        amount_cash=total if location == "kassa" else Decimal(0),
        cash_wallet_code="tk_chernikova",
        actor_user_id=actor.id,
    )
    if location == "safe":
        await book_bank_to_safe_transfer(session, run, operation_date=OP_DATE)
    await session.commit()
    return run.id, employees[0].id, actor.id, await _reserve(session, run.id, location)


async def pay(session, reserve, actor):
    plan = await get_reserve_plan(session, reserve.id)
    return await pay_run_from_pool(
        session,
        reserve_id=reserve.id,
        plan_version=plan["version"],
        allow_overflow=False,
        paid_at=PAID_AT,
        actor_user_id=actor,
    )


async def articles(session, run):
    return dict(
        (
            await session.execute(
                select(
                    DdsArticle.code,
                    func.sum(CashflowTransaction.amount),
                )
                .join(CashflowTransaction, CashflowTransaction.article_id == DdsArticle.id)
                .where(
                    CashflowTransaction.source_kind == "payroll_payout",
                    CashflowTransaction.source_id == run,
                    CashflowTransaction.direction == "out",
                )
                .group_by(DdsArticle.code)
            )
        ).all()
    )


@pytest.mark.parametrize("location", ["safe", "kassa"])
async def test_full_salary_and_deposit_from_active_pool(
    async_session_factory, iiko_calls, location
):
    async with async_session_factory() as session:
        run, employee, actor, reserve = await setup(session, location=location)
        plan = await get_reserve_plan(session, reserve.id)
        allocation = item(plan, employee)
        assert allocation["amount"] == allocation["remaining"] == Decimal("30205")
        assert allocation["salary_remaining"] == Decimal("10205")
        assert allocation["deposit_remaining"] == Decimal("20000")
        result = await pay(session, reserve, actor)
        assert result.primary_booked == Decimal("30205") and result.employees_paid == 1
        payment = await _payment(session, run, employee)
        assert payment.amount == payment.booked_amount == Decimal("10205")
        assert payment.status == "paid"
        assert await articles(session, run) == {
            DDS_ARTICLE_PRODUCTION_PAYROLL: Decimal("10205"),
            DDS_ARTICLE_DEPOSIT_PAYOUT: Decimal("20000"),
        }
        assert reserve.amount_paid == Decimal("30205") and reserve.status == "paid"
        assert (await run_payment_settlement(session, run)).settled
        assert (await run_obligations(session, run))[employee].remaining == 0
        count = await session.scalar(select(func.count()).select_from(CashflowTransaction))
        with pytest.raises(PayrollConflictError):
            await pay(session, reserve, actor)
        assert await session.scalar(select(func.count()).select_from(CashflowTransaction)) == count
    assert [c[0] for c in iiko_calls] == ([Decimal("20000")] if location == "kassa" else [])
    if iiko_calls:
        assert iiko_calls[0][1] == PAID_AT and iiko_calls[0][2] != str(run)


async def test_partial_deposit_keeps_reserve_and_gross_debt(async_session_factory, iiko_calls):
    async with async_session_factory() as session:
        run, employee, actor, reserve = await setup(
            session, location="kassa", salary="1000", deposit="500"
        )
        for amount, salary_paid, deposit_paid, remaining in [
            ("900", "900", "0", "600"),
            ("200", "1000", "100", "400"),
            ("400", "1000", "500", "0"),
        ]:
            plan = await edit(session, reserve, employee, amount, actor)
            assert item(plan, employee)["amount"] == Decimal(amount)
            await pay(session, reserve, actor)
            payment = await _payment(session, run, employee)
            assert payment.amount == payment.booked_amount == Decimal(salary_paid)
            assert payment.status == ("paid" if remaining == "0" else "partially_paid")
            obligation = (await run_obligations(session, run))[employee]
            assert obligation.deposit_paid == Decimal(deposit_paid)
            assert obligation.remaining == Decimal(remaining)
            assert reserve.amount - reserve.amount_paid == Decimal(remaining)
            settlement = await run_payment_settlement(session, run)
            assert settlement.required == Decimal("1500")
            assert (
                settlement.paid == settlement.booked == Decimal(salary_paid) + Decimal(deposit_paid)
            )
            assert settlement.settled == (remaining == "0")
            metrics = (await _run_payment_metrics(session, [run]))[run]
            assert metrics["paid_total"] == float(Decimal(salary_paid) + Decimal(deposit_paid))
            assert metrics["remaining_shortfall"] == float(remaining)
            solvency = await run_solvency(session, await session.get(PayrollRun, run))
            assert solvency.remaining == Decimal(remaining)
            serialized = await get_lines(
                run, session, CurrentActor(roles=frozenset({"owner"}), user_id=actor)
            )
            assert serialized[0].deposit_paid_amount == float(deposit_paid)
        assert await articles(session, run) == {
            DDS_ARTICLE_PRODUCTION_PAYROLL: Decimal("1000"),
            DDS_ARTICLE_DEPOSIT_PAYOUT: Decimal("500"),
        }
    assert [c[0] for c in iiko_calls] == [Decimal("100"), Decimal("400")]
    assert len({c[2] for c in iiko_calls}) == 2


async def test_deposit_only_and_cashier_preview(async_session_factory):
    async with async_session_factory() as session:
        run, employee, actor, reserve = await setup(
            session, location="kassa", salary="0", deposit="500"
        )
        payload = await kassa_pending_payload(session)
        target = next(t for t in payload["targets"] if t["id"] == reserve.id)
        row = target["payroll_employees"][0]
        assert row["payable"] and row["remaining"] == row["planned_amount"] == 500
        assert (await pay(session, reserve, actor)).primary_booked == Decimal("500")
        assert await articles(session, run) == {DDS_ARTICLE_DEPOSIT_PAYOUT: Decimal("500")}
        assert (await _payment(session, run, employee)).amount == 0


async def test_mixed_roles_split_dds_and_do_not_duplicate_deposit(async_session_factory):
    async with async_session_factory() as session:
        run, employee, actor, reserve = await setup(session, deposit="500", mixed=True)
        assert (await pay(session, reserve, actor)).primary_booked == Decimal("6500")
        assert await articles(session, run) == {
            DDS_ARTICLE_PRODUCTION_PAYROLL: Decimal("1000"),
            DDS_ARTICLE_ADMIN_PAYROLL: Decimal("2000"),
            DDS_ARTICLE_AUX_PAYROLL: Decimal("3000"),
            DDS_ARTICLE_DEPOSIT_PAYOUT: Decimal("500"),
        }
        assert (await run_obligations(session, run))[employee].remaining == 0


async def test_old_plan_missing_deposit_employee_auto_fills_but_explicit_zero_does_not(
    async_session_factory,
):
    async with async_session_factory() as session:
        run, employee, actor, reserve = await setup(session)
        await save_reserve_plan(session, reserve, {}, actor)
        await session.commit()
        plan = await get_reserve_plan(session, reserve.id)
        assert item(plan, employee)["amount"] == Decimal("30205")
        plan = await edit(session, reserve, employee, "0", actor)
        assert item(plan, employee)["amount"] == 0
        assert item(plan, employee)["deferred"] == Decimal("30205")
        with pytest.raises(PayrollConflictError, match="нет выбранных"):
            await pay(session, reserve, actor)


async def test_manual_full_after_partial_pool_books_only_deposit_delta(async_session_factory):
    async with async_session_factory() as session:
        run, employee, actor, reserve = await setup(session, salary="1000", deposit="500")
        await edit(session, reserve, employee, "1100", actor)
        await pay(session, reserve, actor)
        await mark_payment(
            session,
            run,
            employee,
            paid_at=PAID_AT,
            method="cash",
            cash_wallet_code="cash_safe",
            actor_user_id=actor,
        )
        assert await articles(session, run) == {
            DDS_ARTICLE_PRODUCTION_PAYROLL: Decimal("1000"),
            DDS_ARTICLE_DEPOSIT_PAYOUT: Decimal("500"),
        }
        assert (await run_payment_settlement(session, run)).settled


async def test_full_rollback_reopens_both_obligations(async_session_factory):
    async with async_session_factory() as session:
        run, employee, actor, reserve = await setup(session)
        await pay(session, reserve, actor)
        await unmark_payment(session, run, employee, actor_user_id=actor)
        obligation = (await run_obligations(session, run))[employee]
        assert obligation.deposit_paid == 0 and obligation.remaining == Decimal("30205")
        active = await session.scalar(
            select(func.count())
            .select_from(PayrollPayoutBooking)
            .where(PayrollPayoutBooking.reversal_transaction_id.is_(None))
        )
        assert active == 0
        assert reserve.amount_paid == 0 and reserve.status == "reserved"
        assert (await pay(session, reserve, actor)).primary_booked == Decimal("30205")
        assert (await run_payment_settlement(session, run)).settled


async def test_deposit_remainder_can_move_to_other_account(async_session_factory, iiko_calls):
    async with async_session_factory() as session:
        run, employee, actor, reserve = await setup(session, salary="1000", deposit="500")
        await edit(session, reserve, employee, "1100", actor)
        await pay(session, reserve, actor)
        # Restore the deferred amount to the unpaid plan, then explicitly move it.
        await edit(session, reserve, employee, "400", actor)
        moved = await edit(session, reserve, employee, "350", actor, destination="kassa")
        assert moved["transferred"] == Decimal("50")
        cash = await _reserve(session, run, "kassa")
        cash_plan = await get_reserve_plan(session, cash.id)
        assert item(cash_plan, employee)["amount"] == Decimal("50")
        assert item(cash_plan, employee)["salary_remaining"] == 0
        assert item(cash_plan, employee)["deposit_remaining"] == Decimal("400")
        await pay(session, cash, actor)
        assert (await run_obligations(session, run))[employee].remaining == Decimal("350")
        assert (await get_reserve_plan(session, reserve.id))["outstanding"] == Decimal("350")
        await pay(session, reserve, actor)
        assert (await run_payment_settlement(session, run)).settled
    assert [c[0] for c in iiko_calls] == [Decimal("50")]


async def test_legacy_aggregate_deposit_is_not_paid_again(async_session_factory):
    async with async_session_factory() as session:
        run_id, employee, actor, reserve = await setup(session, salary="1000", deposit="500")
        run = await session.get(PayrollRun, run_id)
        await book_payout_expense_for_employees(session, run, [employee])
        session.add(
            PayrollPayment(
                run_id=run_id,
                employee_id=employee,
                amount=Decimal("1000"),
                booked_amount=Decimal("1000"),
                paid_at=PAID_AT,
                status="paid",
                method="cash",
            )
        )
        await session.flush()
        await reconcile_run_reserves(session, run_id)
        await session.commit()
        assert (await run_obligations(session, run_id))[employee].remaining == 0
        await mark_payment(
            session,
            run_id,
            employee,
            paid_at=PAID_AT,
            method="cash",
            cash_wallet_code="cash_safe",
            actor_user_id=actor,
        )
        assert await articles(session, run_id) == {
            DDS_ARTICLE_PRODUCTION_PAYROLL: Decimal("1000"),
            DDS_ARTICLE_DEPOSIT_PAYOUT: Decimal("500"),
        }


async def test_missing_dds_article_rejects_without_paying(async_session_factory):
    async with async_session_factory() as session:
        run, employee, actor, reserve = await setup(session, salary="1000", deposit="500")
        article = await session.scalar(
            select(DdsArticle).where(DdsArticle.code == DDS_ARTICLE_DEPOSIT_PAYOUT)
        )
        article.code = "missing_deposit_article_test"
        await session.commit()
        with pytest.raises(PayrollConflictError, match="статья ДДС"):
            await pay(session, reserve, actor)
        await session.rollback()
        assert await _payment(session, run, employee) is None
        assert await articles(session, run) == {}


async def test_concurrent_manual_and_active_payments_do_not_double_dds(async_session_factory):
    async with async_session_factory() as session:
        run, employee, actor, reserve = await setup(session)
        reserve_id = reserve.id
        plan = await get_reserve_plan(session, reserve_id)

    async def from_pool():
        async with async_session_factory() as session:
            return await pay_run_from_pool(
                session,
                reserve_id=reserve_id,
                plan_version=plan["version"],
                allow_overflow=False,
                paid_at=PAID_AT,
                actor_user_id=actor,
            )

    async def manual():
        async with async_session_factory() as session:
            return await mark_payment(
                session,
                run,
                employee,
                paid_at=PAID_AT,
                method="cash",
                cash_wallet_code="cash_safe",
                actor_user_id=actor,
            )

    results = await asyncio.gather(from_pool(), manual(), return_exceptions=True)
    assert all(not isinstance(r, Exception) or isinstance(r, PayrollConflictError) for r in results)
    async with async_session_factory() as session:
        assert await articles(session, run) == {
            DDS_ARTICLE_PRODUCTION_PAYROLL: Decimal("10205"),
            DDS_ARTICLE_DEPOSIT_PAYOUT: Decimal("20000"),
        }
        assert (await run_payment_settlement(session, run)).settled

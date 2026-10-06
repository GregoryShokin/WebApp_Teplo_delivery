"""A pencil saves a persistent unpaid plan, never an employee payout."""

from decimal import Decimal

import pytest
from sqlalchemy import func, select
from test_payroll_pool_payout import PAID_AT, _payment, _reserve, _setup_run_with_reserves

from app.models import CashflowTransaction, PayrollPayment, PayrollRunEvent
from app.services.payroll_reserve_plan import (
    edit_reserve_plan,
    get_reserve_plan,
    transfer_planned_reserve,
)
from app.services.payroll_reserves import pay_run_from_pool
from app.services.payroll_runner import PayrollConflictError

pytestmark = pytest.mark.asyncio


def item(plan, employee):
    return next(i for i in plan["allocations"] if i["employee_id"] == employee)


async def setup(session):
    return await _setup_run_with_reserves(
        session, totals=[[Decimal("7500")], [Decimal("9000")]], cash=Decimal("7500")
    )


async def edit(session, reserve, employee, amount, actor, destination=None, version=None):
    plan = await get_reserve_plan(session, reserve.id)
    return await edit_reserve_plan(
        session,
        reserve_id=reserve.id,
        employee_id=employee,
        amount=Decimal(amount),
        expected_version=version or plan["version"],
        remainder_destination=destination,
        operation_date=PAID_AT,
        actor_user_id=actor,
    )


async def test_edit_keeps_remainder_without_paying_or_moving_money(async_session_factory):
    async with async_session_factory() as session:
        run, employees, actor = await setup(session)
        reserve = await _reserve(session, run, "kassa")
        txn_count = await session.scalar(select(func.count()).select_from(CashflowTransaction))
        result = await edit(session, reserve, employees[0], "7000", actor)
        assert item(result, employees[0])["amount"] == Decimal("7000")
        assert item(result, employees[0])["deferred"] == Decimal("500")
        assert result["outstanding"] == Decimal("7500")
        assert await session.scalar(select(func.count()).select_from(PayrollPayment)) == 0
        assert (
            await session.scalar(select(func.count()).select_from(CashflowTransaction)) == txn_count
        )
        assert reserve.amount_paid == 0 and reserve.status == "reserved"
        from app.schemas.payroll import PayrollReservePlanRead

        serialized = PayrollReservePlanRead.model_validate(result).model_dump(mode="json")
        assert isinstance(serialized["outstanding"], float)
        assert all(isinstance(i["amount"], float) for i in serialized["allocations"])
    # A new session/window sees the saved plan, not the default 7500 again.
    async with async_session_factory() as session:
        result = await get_reserve_plan(session, reserve.id)
        assert item(result, employees[0])["amount"] == Decimal("7000")
        assert item(result, employees[0])["deferred"] == Decimal("500")


async def test_pay_only_saved_amount_leaves_deferred_reserved(async_session_factory):
    async with async_session_factory() as session:
        run, employees, actor = await setup(session)
        reserve = await _reserve(session, run, "kassa")
        plan = await edit(session, reserve, employees[0], "7000", actor)
        result = await pay_run_from_pool(
            session,
            reserve_id=reserve.id,
            selected_ids=set(employees),
            plan_version=plan["version"],
            allow_overflow=False,
            paid_at=PAID_AT,
            actor_user_id=actor,
        )
        assert result.primary_booked == Decimal("7000")
        payment = await _payment(session, run, employees[0])
        assert payment.amount == Decimal("7000") and payment.status == "partially_paid"
        plan = await get_reserve_plan(session, reserve.id)
        assert plan["outstanding"] == Decimal("500")
        assert item(plan, employees[0])["amount"] == 0
        assert item(plan, employees[0])["deferred"] == Decimal("500")
        plan = await edit(session, reserve, employees[0], "500", actor)
        assert item(plan, employees[0])["deferred"] == 0
        result = await pay_run_from_pool(
            session,
            reserve_id=reserve.id,
            plan_version=plan["version"],
            allow_overflow=False,
            paid_at=PAID_AT,
            actor_user_id=actor,
        )
        assert result.primary_booked == Decimal("500")
        assert (await _payment(session, run, employees[0])).status == "paid"


async def test_move_only_remainder_to_safe_keeps_employee_unpaid(async_session_factory):
    async with async_session_factory() as session:
        run, employees, actor = await setup(session)
        source = await _reserve(session, run, "kassa")
        destination = await _reserve(session, run, "safe")
        result = await edit(session, source, employees[0], "7000", actor, "safe")
        assert result["transferred"] == Decimal("500")
        assert item(result, employees[0])["amount"] == Decimal("7000")
        assert item(result, employees[0])["deferred"] == 0
        assert source.amount == Decimal("7000") and source.amount_paid == 0
        assert destination.amount == Decimal("9500") and destination.amount_paid == 0
        assert await session.scalar(select(func.count()).select_from(PayrollPayment)) == 0
        event = await session.scalar(
            select(PayrollRunEvent).where(PayrollRunEvent.action == "reserve_transferred")
        )
        legs = (
            await session.scalars(
                select(CashflowTransaction).where(
                    CashflowTransaction.source_id == event.payload["transfer_id"]
                )
            )
        ).all()
        assert sorted((t.direction, t.amount) for t in legs) == [
            ("in", Decimal("500")),
            ("out", Decimal("500")),
        ]
        dest_plan = await get_reserve_plan(session, destination.id)
        assert item(dest_plan, employees[0])["amount"] == Decimal("500")
        assert item(dest_plan, employees[1])["amount"] == Decimal("9000")
        # The source pays 7000, not the original 7500; the other account pays 500.
        await pay_run_from_pool(
            session,
            reserve_id=source.id,
            plan_version=result["version"],
            allow_overflow=False,
            paid_at=PAID_AT,
            actor_user_id=actor,
        )
        dest_plan = await get_reserve_plan(session, destination.id)
        paid = await pay_run_from_pool(
            session,
            reserve_id=destination.id,
            selected_ids={employees[0]},
            plan_version=dest_plan["version"],
            allow_overflow=False,
            paid_at=PAID_AT,
            actor_user_id=actor,
        )
        assert paid.primary_booked == Decimal("500")
        assert (await _payment(session, run, employees[0])).amount == Decimal("7500")


async def test_zero_amount_keeps_whole_wage_reserved(async_session_factory):
    async with async_session_factory() as session:
        run, employees, actor = await setup(session)
        reserve = await _reserve(session, run, "kassa")
        plan = await edit(session, reserve, employees[0], "0", actor)
        assert item(plan, employees[0])["deferred"] == Decimal("7500")
        with pytest.raises(PayrollConflictError, match="нет выбранных сумм"):
            await pay_run_from_pool(
                session,
                reserve_id=reserve.id,
                plan_version=plan["version"],
                allow_overflow=False,
                paid_at=PAID_AT,
                actor_user_id=actor,
            )
        await session.rollback()


async def test_stale_edit_and_payout_rejected(async_session_factory):
    async with async_session_factory() as session:
        run, employees, actor = await setup(session)
        reserve = await _reserve(session, run, "kassa")
        original = await get_reserve_plan(session, reserve.id)
        await edit(session, reserve, employees[0], "7000", actor)
        with pytest.raises(PayrollConflictError, match="План или остаток изменился"):
            await edit(session, reserve, employees[0], "6500", actor, version=original["version"])
        await session.rollback()
        await session.refresh(reserve)
        with pytest.raises(PayrollConflictError, match="План или остаток изменился"):
            await pay_run_from_pool(
                session,
                reserve_id=reserve.id,
                plan_version=original["version"],
                allow_overflow=False,
                paid_at=PAID_AT,
                actor_user_id=actor,
            )
        await session.rollback()


@pytest.mark.parametrize(
    "amount,destination", [("7500.01", None), ("-1", None), ("7000", "kassa"), ("7500", "safe")]
)
async def test_invalid_edit_rolls_back_without_paying(async_session_factory, amount, destination):
    async with async_session_factory() as session:
        run, employees, actor = await setup(session)
        reserve = await _reserve(session, run, "kassa")
        with pytest.raises(PayrollConflictError):
            await edit(session, reserve, employees[0], amount, actor, destination)
        await session.rollback()
        assert await session.scalar(select(func.count()).select_from(PayrollPayment)) == 0
        assert (
            await session.scalar(
                select(func.count())
                .select_from(PayrollRunEvent)
                .where(PayrollRunEvent.action == "reserve_plan_updated")
            )
            == 0
        )


async def test_transfer_button_honors_plan_and_leaves_deferred(async_session_factory):
    async with async_session_factory() as session:
        run, employees, actor = await setup(session)
        reserve = await _reserve(session, run, "kassa")
        plan = await edit(session, reserve, employees[0], "7000", actor)
        result = await transfer_planned_reserve(
            session,
            reserve_id=reserve.id,
            selected_ids={employees[0]},
            expected_version=plan["version"],
            operation_date=PAID_AT,
            actor_user_id=actor,
        )
        assert result.amount == Decimal("7000")
        plan = await get_reserve_plan(session, reserve.id)
        assert plan["outstanding"] == Decimal("500") and item(plan, employees[0])[
            "deferred"
        ] == Decimal("500")
        assert item(plan, employees[0])["amount"] == 0
        assert await session.scalar(select(func.count()).select_from(PayrollPayment)) == 0


async def test_cashier_sees_same_plan_and_deferred_amount(async_session_factory):
    from app.services.kassa.payouts import kassa_pending_payload

    async with async_session_factory() as session:
        run, employees, actor = await setup(session)
        reserve = await _reserve(session, run, "kassa")
        plan = await edit(session, reserve, employees[0], "7000", actor)
        payload = await kassa_pending_payload(session)
        target = next(t for t in payload["targets"] if t["id"] == reserve.id)
        assert target["payroll_plan_version"] == plan["version"]
        employee = next(e for e in target["payroll_employees"] if e["employee_id"] == employees[0])
        assert employee["planned_amount"] == 7000
        assert employee["deferred_amount"] == 500
        assert employee["remaining"] == 7500
        assert employee["payment_status"] == "pending"


async def test_old_pencil_endpoint_cannot_pay_without_confirmation(async_session_factory):
    from fastapi import HTTPException

    from app.api.v1.routes.payroll import post_pay_employee_from_reserve
    from app.schemas.payroll import PayrollReserveEmployeePayRequest

    async with async_session_factory() as session:
        run, employees, _actor = await setup(session)
        reserve = await _reserve(session, run, "kassa")
        request = PayrollReserveEmployeePayRequest(
            employee_id=employees[0], amount=7000, paid_at=PAID_AT
        )
        with pytest.raises(HTTPException) as error:
            await post_pay_employee_from_reserve(reserve.id, request, session, None)
        assert error.value.status_code == 409
        assert await session.scalar(select(func.count()).select_from(PayrollPayment)) == 0


async def test_remainder_move_is_symmetric_safe_to_cash(async_session_factory):
    async with async_session_factory() as session:
        run, employees, actor = await _setup_run_with_reserves(
            session, totals=[[Decimal("7500")], [Decimal("9000")]], cash=Decimal("9000")
        )
        source = await _reserve(session, run, "safe")
        result = await edit(session, source, employees[1], "7000", actor, "kassa")
        assert result["transferred"] == Decimal("500")
        assert result["outstanding"] == Decimal("7000")
        assert item(result, employees[1])["amount"] == Decimal("7000")
        assert item(result, employees[1])["other_amount"] == Decimal("2000")
        assert await session.scalar(select(func.count()).select_from(PayrollPayment)) == 0

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentActor
from app.api.v1.routes import payroll as payroll_routes
from app.models import (
    Employee,
    PayrollLine,
    PayrollPayment,
    PayrollPeriod,
    PayrollRun,
    PayrollRunEvent,
    User,
)
from app.schemas.payroll import (
    PayrollPaymentMarkRequest,
    PayrollPaymentsBulkMarkRequest,
    PayrollPaymentsMarkAllRequest,
)
from app.services import payroll_runner
from app.services.payroll_payments import mark_payment, mark_payments_selected
from app.services.payroll_runner import finalize_payroll_run, run_payroll, unfinalize_payroll_run


async def test_mark_payment_on_finalized_run_creates_snapshot_and_serializes_line(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, employees = await create_payroll_run(
            session,
            status="finalized",
            period_status="finalized",
            employee_line_totals=[[Decimal("1000"), Decimal("250.50")]],
        )

        await mark_payment(
            session,
            run.id,
            employees[0].id,
            paid_at=date(2026, 5, 27),
            method="business_card",
            actor_user_id=actor.id,
        )

        payment = await session.scalar(
            select(PayrollPayment).where(PayrollPayment.run_id == run.id)
        )
        lines = await payroll_routes.get_lines(
            run.id,
            session,
            CurrentActor(roles=frozenset({"manager"}), user_id=actor.id),
        )

        assert payment is not None
        assert payment.employee_id == employees[0].id
        assert payment.amount == Decimal("1250.50")
        assert payment.method == "business_card"
        assert {line.payment_status for line in lines} == {"paid"}
        assert {line.paid_amount for line in lines} == {1250.5}
        assert {line.paid_at for line in lines} == {date(2026, 5, 27)}
        assert {line.paid_method for line in lines} == {"business_card"}


async def test_mark_payment_uses_total_payable_after_ndfl(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, employees = await create_payroll_run(
            session,
            status="finalized",
            period_status="finalized",
            employee_line_totals=[[Decimal("870")]],
        )
        line = await session.scalar(select(PayrollLine).where(PayrollLine.run_id == run.id))
        assert line is not None
        line.base_pay = Decimal("1000.00")
        line.ndfl_withheld = Decimal("130.00")
        line.total_payable = Decimal("870.00")
        await session.commit()

        payment = await mark_payment(
            session,
            run.id,
            employees[0].id,
            paid_at=date(2026, 5, 27),
            method="business_card",
            actor_user_id=actor.id,
        )

        assert payment.amount == Decimal("870.00")
        assert payment.amount_account == Decimal("870.00")


async def test_mark_payment_on_not_finalized_run_returns_409(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, employees = await create_payroll_run(session, status="completed")

        with pytest.raises(HTTPException) as exc_info:
            await payroll_routes.post_mark_payment(
                run.id,
                PayrollPaymentMarkRequest(
                    employee_id=employees[0].id,
                    paid_at=date(2026, 5, 27),
                    method="cash",
                ),
                session,
                CurrentActor(roles=frozenset({"manager"}), user_id=actor.id),
            )

        assert exc_info.value.status_code == 409
        assert exc_info.value.detail == ("Сначала финализируйте ведомость")


async def test_mark_payment_on_legacy_run_returns_409(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, employees = await create_payroll_run(
            session,
            status="finalized",
            period_status="finalized",
            is_legacy=True,
        )

        with pytest.raises(HTTPException) as exc_info:
            await payroll_routes.post_mark_payment(
                run.id,
                PayrollPaymentMarkRequest(
                    employee_id=employees[0].id,
                    paid_at=date(2026, 5, 27),
                    method="cash",
                ),
                session,
                CurrentActor(roles=frozenset({"manager"}), user_id=actor.id),
            )

        assert exc_info.value.status_code == 409
        assert exc_info.value.detail == ("Импортированная ведомость — выплаты не отмечаются")


async def test_mark_payment_with_invalid_method_returns_409(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, employees = await create_payroll_run(
            session,
            status="finalized",
            period_status="finalized",
        )

        with pytest.raises(HTTPException) as exc_info:
            await payroll_routes.post_mark_payment(
                run.id,
                PayrollPaymentMarkRequest(
                    employee_id=employees[0].id,
                    paid_at=date(2026, 5, 27),
                    method="crypto",
                ),
                session,
                CurrentActor(roles=frozenset({"manager"}), user_id=actor.id),
            )

        assert exc_info.value.status_code == 409
        assert exc_info.value.detail == "Некорректный способ выплаты"


async def test_unmark_payment_deletes_row_and_line_returns_pending(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, employees = await create_payroll_run(
            session,
            status="finalized",
            period_status="finalized",
        )
        await mark_payment(
            session,
            run.id,
            employees[0].id,
            paid_at=date(2026, 5, 27),
            method="transfer",
            actor_user_id=actor.id,
        )

        await payroll_routes.delete_mark_payment(
            run.id,
            employees[0].id,
            session,
            CurrentActor(roles=frozenset({"manager"}), user_id=actor.id),
        )
        payment_count = await session.scalar(select(func.count()).select_from(PayrollPayment))
        lines = await payroll_routes.get_lines(
            run.id,
            session,
            CurrentActor(roles=frozenset({"manager"}), user_id=actor.id),
        )

        assert payment_count == 0
        assert {line.payment_status for line in lines} == {"pending"}
        assert {line.paid_amount for line in lines} == {None}


async def test_unmark_booked_payment_is_rejected(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Нельзя удалить PayrollPayment, если по нему уже проведён расход ДДС."""
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, employees = await create_payroll_run(
            session,
            status="finalized",
            period_status="finalized",
        )
        payment = await mark_payment(
            session,
            run.id,
            employees[0].id,
            paid_at=date(2026, 5, 27),
            method="transfer",
            actor_user_id=actor.id,
        )
        payment.booked_amount = payment.amount
        await session.commit()

        with pytest.raises(HTTPException) as exc_info:
            await payroll_routes.delete_mark_payment(
                run.id,
                employees[0].id,
                session,
                CurrentActor(roles=frozenset({"manager"}), user_id=actor.id),
            )

        assert exc_info.value.status_code == 409
        assert exc_info.value.detail == "Проведённую выплату нельзя снять без финансового отката"
        saved = await session.get(PayrollPayment, payment.id)
        assert saved is not None and saved.booked_amount == payment.amount


async def test_mark_all_payments_marks_only_unpaid_employees(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, employees = await create_payroll_run(
            session,
            status="finalized",
            period_status="finalized",
            employee_line_totals=[[Decimal("1000")], [Decimal("2000")]],
        )
        await mark_payment(
            session,
            run.id,
            employees[0].id,
            paid_at=date(2026, 5, 27),
            method="cash",
            actor_user_id=actor.id,
        )

        response = await payroll_routes.post_mark_all_payments(
            run.id,
            PayrollPaymentsMarkAllRequest(paid_at=date(2026, 5, 28), method="transfer"),
            session,
            CurrentActor(roles=frozenset({"manager"}), user_id=actor.id),
        )
        payments = (
            await session.scalars(
                select(PayrollPayment)
                .where(PayrollPayment.run_id == run.id)
                .order_by(PayrollPayment.amount)
            )
        ).all()

        assert response.marked_count == 1
        assert len(payments) == 2
        assert [(payment.amount, payment.method, payment.paid_at) for payment in payments] == [
            (Decimal("1000.00"), "cash", date(2026, 5, 27)),
            (Decimal("2000.00"), "transfer", date(2026, 5, 28)),
        ]


async def test_repeat_mark_payment_updates_existing_row_without_duplicate(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, employees = await create_payroll_run(
            session,
            status="finalized",
            period_status="finalized",
        )

        first = await mark_payment(
            session,
            run.id,
            employees[0].id,
            paid_at=date(2026, 5, 27),
            method="cash",
            actor_user_id=actor.id,
        )
        second = await mark_payment(
            session,
            run.id,
            employees[0].id,
            paid_at=date(2026, 5, 28),
            method="other",
            actor_user_id=actor.id,
        )
        payment_count = await session.scalar(select(func.count()).select_from(PayrollPayment))

        assert second.id == first.id
        assert payment_count == 1
        assert second.paid_at == date(2026, 5, 28)
        assert second.method == "other"


async def test_payment_survives_unfinalize_recompute_and_keeps_original_amount(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        period, run, employees = await create_payroll_run(
            session,
            status="completed",
            period_status="open",
            employee_line_totals=[[Decimal("1000")]],
        )
        await finalize_payroll_run(session, run.id, finalized_by_user_id=actor.id)
        payment = await mark_payment(
            session,
            run.id,
            employees[0].id,
            paid_at=date(2026, 5, 27),
            method="business_card",
            actor_user_id=actor.id,
        )
        await unfinalize_payroll_run(
            session,
            run.id,
            reason="Исправить начисление",
            actor_user_id=actor.id,
        )
        await patch_runner_recompute(monkeypatch, employees[0].id, total_payable=Decimal("1500"))

        recomputed = await run_payroll(session, period.id, force_refresh=True)
        saved_payment = await session.scalar(
            select(PayrollPayment).where(PayrollPayment.run_id == recomputed.id)
        )
        lines = (
            await session.scalars(select(PayrollLine).where(PayrollLine.run_id == recomputed.id))
        ).all()

        assert recomputed.id == run.id
        assert saved_payment is not None
        assert saved_payment.id == payment.id
        assert saved_payment.amount == Decimal("1000.00")
        assert [line.total_payable for line in lines] == [Decimal("1500.00")]


async def test_mark_and_unmark_create_payroll_run_events(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, employees = await create_payroll_run(
            session,
            status="finalized",
            period_status="finalized",
        )

        await mark_payment(
            session,
            run.id,
            employees[0].id,
            paid_at=date(2026, 5, 27),
            method="cash",
            actor_user_id=actor.id,
        )
        await payroll_routes.delete_mark_payment(
            run.id,
            employees[0].id,
            session,
            CurrentActor(roles=frozenset({"manager"}), user_id=actor.id),
        )
        events = (
            await session.scalars(
                select(PayrollRunEvent)
                .where(PayrollRunEvent.run_id == run.id)
                .order_by(PayrollRunEvent.created_at, PayrollRunEvent.action)
            )
        ).all()

        assert [event.action for event in events] == ["payment_marked", "payment_unmarked"]
        assert {event.actor_user_id for event in events} == {actor.id}
        assert events[0].payload["employee_id"] == str(employees[0].id)
        assert events[0].payload["amount"] == "1000.00"
        assert events[1].payload["employee_id"] == str(employees[0].id)


async def test_bulk_mark_selected_marks_only_chosen_without_method(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, employees = await create_payroll_run(
            session,
            status="finalized",
            period_status="finalized",
            employee_line_totals=[[Decimal("1000")], [Decimal("2000")], [Decimal("3000")]],
        )

        marked = await mark_payments_selected(
            session,
            run.id,
            [employees[0].id, employees[2].id],
            paid_at=date(2026, 5, 27),
            actor_user_id=actor.id,
        )
        assert marked == 2

        payments = {
            payment.employee_id: payment
            for payment in (
                await session.scalars(select(PayrollPayment).where(PayrollPayment.run_id == run.id))
            ).all()
        }
        assert set(payments) == {employees[0].id, employees[2].id}
        assert all(payment.status == "paid" for payment in payments.values())
        assert all(payment.method is None for payment in payments.values())
        assert payments[employees[0].id].amount == Decimal("1000.00")
        assert payments[employees[2].id].amount == Decimal("3000.00")

        # Route wiring marks the remaining unpaid employee.
        result = await payroll_routes.post_bulk_mark_payments(
            run.id,
            PayrollPaymentsBulkMarkRequest(
                employee_ids=[employees[1].id], paid_at=date(2026, 5, 27)
            ),
            session,
            CurrentActor(roles=frozenset({"manager"}), user_id=actor.id),
        )
        assert result.marked_count == 1

        # Re-marking an already-paid employee is a no-op.
        again = await mark_payments_selected(
            session,
            run.id,
            [employees[0].id],
            paid_at=date(2026, 5, 27),
            actor_user_id=actor.id,
        )
        assert again == 0


async def create_actor_user(session: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"payroll-payment-actor-{uuid.uuid4()}@test.local",
        hashed_password="hashed",
        full_name="Payroll Payment Actor",
        is_active=True,
    )
    session.add(user)
    await session.commit()
    return user


async def create_payroll_run(
    session: AsyncSession,
    *,
    status: str = "finalized",
    period_status: str = "finalized",
    is_legacy: bool = False,
    employee_line_totals: list[list[Decimal]] | None = None,
) -> tuple[PayrollPeriod, PayrollRun, list[Employee]]:
    line_totals = employee_line_totals or [[Decimal("1000")]]
    period = PayrollPeriod(
        id=uuid.uuid4(),
        period_type="week",
        start_date=date(2026, 5, 19),
        end_date=date(2026, 5, 25),
        payroll_date=date(2026, 5, 26),
        status=period_status,
        finalized_at=datetime(2026, 5, 27, tzinfo=UTC) if period_status == "finalized" else None,
    )
    run = PayrollRun(
        id=uuid.uuid4(),
        period_id=period.id,
        started_at=datetime(2026, 5, 26, tzinfo=UTC),
        finished_at=datetime(2026, 5, 26, 1, tzinfo=UTC),
        status=status,
        blocking_issues=[],
        summary={},
        is_imported_legacy=is_legacy,
    )
    employees = [
        Employee(
            id=uuid.uuid4(),
            full_name=f"Payroll Employee {index + 1}",
            iiko_id=f"iiko-{uuid.uuid4()}",
            category="category_2",
            status="active",
            is_senior=False,
            is_deputy_senior=False,
            pin_hash="hashed-pin",
            pin_set_at=datetime(2026, 5, 1, tzinfo=UTC),
            created_at=datetime(2026, 5, 1, tzinfo=UTC),
            updated_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        for index, _employee_totals in enumerate(line_totals)
    ]
    session.add_all([period, run, *employees])
    await session.flush()
    for employee, employee_totals in zip(employees, line_totals, strict=True):
        for role_index, total_payable in enumerate(employee_totals):
            session.add(make_payroll_line(run.id, employee.id, role_index, total_payable))
    await session.commit()
    return period, run, employees


def make_payroll_line(
    run_id: uuid.UUID,
    employee_id: uuid.UUID,
    role_index: int,
    total_payable: Decimal,
) -> PayrollLine:
    return PayrollLine(
        id=uuid.uuid4(),
        run_id=run_id,
        employee_id=employee_id,
        role=f"role-{role_index}",
        base_pay=total_payable,
        premium=Decimal("0"),
        percent_pay=Decimal("0"),
        vacation_pay=Decimal("0"),
        ndfl_withheld=Decimal("0"),
        fund_accrual=Decimal("0"),
        deduction=Decimal("0"),
        total_payable=total_payable,
        deposit_excluded_for_run=False,
        deposit_exclusion_reason=None,
        components={"days": [], "adjustments": {}},
    )


async def patch_runner_recompute(
    monkeypatch: pytest.MonkeyPatch,
    employee_id: uuid.UUID,
    *,
    total_payable: Decimal,
) -> None:
    async def fake_load_attendance_entries(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    async def fake_collect_blocking_issues(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return []

    async def fake_ensure_daily_revenue_cached(*_args: Any, **_kwargs: Any) -> dict[date, Decimal]:
        return {}

    async def fake_calculate_payroll_lines(
        _session: AsyncSession,
        _period: PayrollPeriod,
        run_id: uuid.UUID,
        _entries: list[Any],
        **_kwargs: Any,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            lines=[make_payroll_line(run_id, employee_id, 0, total_payable)],
            blocking_issues=[],
            summary={"total_payable": float(total_payable)},
        )

    async def fake_update_deposits_and_fund(
        _session: AsyncSession,
        _period: PayrollPeriod,
        _run: PayrollRun,
        _lines: list[PayrollLine],
    ) -> dict[str, Any]:
        return {}

    async def fake_mark_vacations_paid(*_args: Any, **_kwargs: Any) -> int:
        return 0

    monkeypatch.setattr(payroll_runner, "load_attendance_entries", fake_load_attendance_entries)
    monkeypatch.setattr(payroll_runner, "collect_blocking_issues", fake_collect_blocking_issues)
    monkeypatch.setattr(
        payroll_runner,
        "ensure_daily_revenue_cached",
        fake_ensure_daily_revenue_cached,
    )
    monkeypatch.setattr(payroll_runner, "calculate_payroll_lines", fake_calculate_payroll_lines)
    monkeypatch.setattr(payroll_runner, "update_deposits_and_fund", fake_update_deposits_and_fund)
    monkeypatch.setattr(
        payroll_runner.vacation_service,
        "mark_vacations_paid_for_payroll_period",
        fake_mark_vacations_paid,
    )

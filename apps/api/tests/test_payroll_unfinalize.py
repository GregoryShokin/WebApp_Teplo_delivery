from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentActor
from app.api.v1.routes import payroll as payroll_routes
from app.models import (
    DepositAccount,
    DepositTransaction,
    Employee,
    PayrollLine,
    PayrollPeriod,
    PayrollRun,
    PayrollRunEvent,
    User,
)
from app.schemas.payroll import PayrollRunUnfinalize
from app.services import payroll_runner
from app.services.payroll_runner import (
    finalize_payroll_run,
    run_payroll,
    unfinalize_payroll_run,
)


async def test_finalize_and_unfinalize_round_trip_deposit_balance(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        period, run, _employee, account = await create_payroll_run_with_deposit(
            session,
            account_balance=Decimal("5000"),
            transaction_amount=Decimal("1000"),
        )

        await finalize_payroll_run(session, run.id, finalized_by_user_id=actor.id)
        await session.refresh(account)

        assert account.balance == Decimal("6000.00")
        assert period.status == "finalized"

        await unfinalize_payroll_run(
            session,
            run.id,
            reason="Забыли премию",
            actor_user_id=actor.id,
        )
        await session.refresh(account)
        await session.refresh(period)
        await session.refresh(run)

        assert account.balance == Decimal("5000.00")
        assert run.status == "completed"
        assert period.status == "open"
        assert period.finalized_at is None
        assert period.finalized_by_user_id is None


async def test_unfinalize_allows_recompute_for_period(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        period, run, employee, _account = await create_payroll_run_with_deposit(session)
        await finalize_payroll_run(session, run.id, finalized_by_user_id=actor.id)
        await unfinalize_payroll_run(
            session,
            run.id,
            reason="Исправить категорию",
            actor_user_id=actor.id,
        )
        await patch_runner_recompute(monkeypatch, employee.id)

        recomputed = await run_payroll(session, period.id, force_refresh=True)
        await session.refresh(period)

        assert recomputed.status == "completed"
        assert period.status == "open"


@pytest.mark.parametrize(
    ("run_status", "period_status", "reason", "is_legacy", "expected_detail"),
    [
        ("completed", "open", "Нужно исправить", False, "Ведомость не финализирована"),
        ("finalized", "finalized", "   ", False, "Нужна причина отката"),
        (
            "finalized",
            "finalized",
            "Нужно исправить",
            True,
            "Импортированную ведомость нельзя откатывать",
        ),
    ],
)
async def test_unfinalize_conflicts_return_409(
    async_session_factory: async_sessionmaker[AsyncSession],
    run_status: str,
    period_status: str,
    reason: str,
    is_legacy: bool,
    expected_detail: str,
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, _employee, _account = await create_payroll_run_with_deposit(
            session,
            run_status=run_status,
            period_status=period_status,
            is_legacy=is_legacy,
        )

        with pytest.raises(HTTPException) as exc_info:
            await payroll_routes.post_unfinalize(
                run.id,
                PayrollRunUnfinalize(reason=reason),
                session,
                CurrentActor(roles=frozenset({"manager"}), user_id=actor.id),
            )

        assert exc_info.value.status_code == 409
        assert exc_info.value.detail == expected_detail


async def test_unfinalize_creates_reasoned_event_with_actor(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, _employee, _account = await create_payroll_run_with_deposit(session)

        await finalize_payroll_run(session, run.id, finalized_by_user_id=actor.id)
        await unfinalize_payroll_run(
            session,
            run.id,
            reason="Штраф внесли после закрытия",
            actor_user_id=actor.id,
        )

        events = (
            await session.scalars(
                select(PayrollRunEvent)
                .where(PayrollRunEvent.run_id == run.id)
                .order_by(PayrollRunEvent.created_at, PayrollRunEvent.action)
            )
        ).all()
        finalize_event = next(event for event in events if event.action == "finalize")
        unfinalize_event = next(event for event in events if event.action == "unfinalize")

        assert finalize_event.actor_user_id == actor.id
        assert finalize_event.payload["run_status"] == {
            "before": "completed",
            "after": "finalized",
        }
        assert unfinalize_event.reason == "Штраф внесли после закрытия"
        assert unfinalize_event.actor_user_id == actor.id
        assert unfinalize_event.payload["run_status"] == {
            "before": "finalized",
            "after": "completed",
        }
        assert unfinalize_event.payload["period_status"] == {
            "before": "finalized",
            "after": "open",
        }
        assert unfinalize_event.payload["deposit_balance_delta_total"] == "-1000.00"


async def test_finalize_unfinalize_recompute_refinalize_has_no_double_deposit_count(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        period, run, employee, account = await create_payroll_run_with_deposit(
            session,
            account_balance=Decimal("5000"),
            transaction_amount=Decimal("1000"),
        )
        await finalize_payroll_run(session, run.id, finalized_by_user_id=actor.id)
        await unfinalize_payroll_run(
            session,
            run.id,
            reason="Пересчитать с новой премией",
            actor_user_id=actor.id,
        )
        await patch_runner_recompute(
            monkeypatch,
            employee.id,
            deposit_transaction_amount=Decimal("1200"),
        )

        recomputed = await run_payroll(session, period.id, force_refresh=True)
        transactions = (
            await session.scalars(
                select(DepositTransaction).where(DepositTransaction.run_id == recomputed.id)
            )
        ).all()
        await finalize_payroll_run(session, recomputed.id, finalized_by_user_id=actor.id)
        await session.refresh(account)

        assert [transaction.amount for transaction in transactions] == [Decimal("1200.00")]
        assert account.balance == Decimal("6200.00")


async def create_actor_user(session: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"payroll-actor-{uuid.uuid4()}@test.local",
        hashed_password="hashed",
        full_name="Payroll Actor",
        is_active=True,
    )
    session.add(user)
    await session.commit()
    return user


async def create_payroll_run_with_deposit(
    session: AsyncSession,
    *,
    run_status: str = "completed",
    period_status: str = "open",
    is_legacy: bool = False,
    account_balance: Decimal = Decimal("5000"),
    transaction_type: str = "accrual",
    transaction_amount: Decimal = Decimal("1000"),
) -> tuple[PayrollPeriod, PayrollRun, Employee, DepositAccount]:
    employee = Employee(
        id=uuid.uuid4(),
        full_name="Payroll Employee",
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
    finalized_at = datetime(2026, 5, 27, tzinfo=UTC) if period_status == "finalized" else None
    period = PayrollPeriod(
        id=uuid.uuid4(),
        period_type="week",
        start_date=date(2026, 5, 19),
        end_date=date(2026, 5, 25),
        payroll_date=date(2026, 5, 26),
        status=period_status,
        finalized_at=finalized_at,
    )
    run = PayrollRun(
        id=uuid.uuid4(),
        period_id=period.id,
        started_at=datetime(2026, 5, 26, tzinfo=UTC),
        finished_at=datetime(2026, 5, 26, 1, tzinfo=UTC),
        status=run_status,
        blocking_issues=[],
        summary={},
        is_imported_legacy=is_legacy,
    )
    account = DepositAccount(
        id=uuid.uuid4(),
        employee_id=employee.id,
        balance=account_balance,
        initial_balance=account_balance,
        last_updated=datetime(2026, 5, 26, tzinfo=UTC),
    )
    session.add_all([employee, period, run, account])
    await session.flush()
    session.add(
        DepositTransaction(
            id=uuid.uuid4(),
            employee_id=employee.id,
            run_id=run.id,
            transaction_type=transaction_type,
            amount=transaction_amount,
            created_at=datetime(2026, 5, 26, tzinfo=UTC),
        )
    )
    await session.commit()
    return period, run, employee, account


async def patch_runner_recompute(
    monkeypatch: pytest.MonkeyPatch,
    employee_id: uuid.UUID,
    *,
    deposit_transaction_amount: Decimal | None = None,
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
            lines=[
                PayrollLine(
                    id=uuid.uuid4(),
                    run_id=run_id,
                    employee_id=employee_id,
                    role="pizza",
                    base_pay=Decimal("1000"),
                    premium=Decimal("0"),
                    percent_pay=Decimal("0"),
                    vacation_pay=Decimal("0"),
                    ndfl_withheld=Decimal("0"),
                    fund_accrual=Decimal("0"),
                    deduction=Decimal("0"),
                    total_payable=Decimal("1000"),
                    components={"days": [], "adjustments": {}},
                )
            ],
            blocking_issues=[],
            summary={"total_payable": 1000},
        )

    async def fake_update_deposits_and_fund(
        session: AsyncSession,
        _period: PayrollPeriod,
        run: PayrollRun,
        _lines: list[PayrollLine],
    ) -> dict[str, Any]:
        if deposit_transaction_amount is None:
            return {}
        session.add(
            DepositTransaction(
                id=uuid.uuid4(),
                employee_id=employee_id,
                run_id=run.id,
                transaction_type="accrual",
                amount=deposit_transaction_amount,
                created_at=datetime(2026, 5, 26, tzinfo=UTC),
            )
        )
        await session.flush()
        return {"deposit_transaction_count": 1}

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

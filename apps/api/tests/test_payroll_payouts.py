from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentActor
from app.api.v1.routes import payroll as payroll_routes
from app.core.config import Settings
from app.models import (
    Employee,
    PayrollBankDraft,
    PayrollLine,
    PayrollPayment,
    PayrollPeriod,
    PayrollRun,
    PayrollRunEvent,
    SourceCredential,
    User,
)
from app.schemas.payroll import PayrollPayoutSplitPatch
from app.services import payroll_runner
from app.services.banking import PaymentDraftResult
from app.services.banking.tbank import TbankClient
from app.services.payroll_payments import mark_payment
from app.services.payroll_payouts import (
    apply_payout_deltas,
    create_or_update_drafts,
    create_or_update_run_draft,
    get_payout_deltas,
    set_payout_split,
)
from app.services.payroll_runner import finalize_payroll_run, run_payroll, unfinalize_payroll_run


async def test_split_validates_bounds_status_and_legacy(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, employees = await create_payroll_run(
            session,
            employee_line_totals=[[Decimal("1000"), Decimal("250")]],
        )

        payment = await set_payout_split(
            session,
            run.id,
            employees[0].id,
            amount_cash=Decimal("300"),
            actor_user_id=actor.id,
        )

        assert payment.amount == Decimal("1250.00")
        assert payment.amount_cash == Decimal("300.00")
        assert payment.amount_account == Decimal("950.00")
        assert payment.status == "planned"

        with pytest.raises(HTTPException) as too_large:
            await payroll_routes.patch_payout_split(
                run.id,
                employees[0].id,
                PayrollPayoutSplitPatch(amount_cash=Decimal("1300")),
                session,
                CurrentActor(roles=frozenset({"manager"}), user_id=actor.id),
            )
        assert too_large.value.status_code == 409

        _period_nf, run_nf, employees_nf = await create_payroll_run(
            session,
            status="completed",
            period_status="open",
        )
        with pytest.raises(HTTPException) as not_finalized:
            await payroll_routes.patch_payout_split(
                run_nf.id,
                employees_nf[0].id,
                PayrollPayoutSplitPatch(amount_cash=Decimal("1")),
                session,
                CurrentActor(roles=frozenset({"manager"}), user_id=actor.id),
            )
        assert not_finalized.value.status_code == 409

        _period_legacy, run_legacy, employees_legacy = await create_payroll_run(
            session,
            is_legacy=True,
        )
        with pytest.raises(HTTPException) as legacy:
            await payroll_routes.patch_payout_split(
                run_legacy.id,
                employees_legacy[0].id,
                PayrollPayoutSplitPatch(amount_cash=Decimal("1")),
                session,
                CurrentActor(roles=frozenset({"manager"}), user_id=actor.id),
            )
        assert legacy.value.status_code == 409


async def test_payout_split_uses_total_payable_after_ndfl(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, employees = await create_payroll_run(
            session,
            employee_line_totals=[[Decimal("870")]],
        )
        line = await session.scalar(select(PayrollLine).where(PayrollLine.run_id == run.id))
        assert line is not None
        line.base_pay = Decimal("1000.00")
        line.ndfl_withheld = Decimal("130.00")
        line.total_payable = Decimal("870.00")
        await session.commit()

        payment = await set_payout_split(
            session,
            run.id,
            employees[0].id,
            amount_cash=Decimal("100"),
            actor_user_id=actor.id,
        )

        assert payment.amount == Decimal("870.00")
        assert payment.amount_cash == Decimal("100.00")
        assert payment.amount_account == Decimal("770.00")


async def test_split_on_paid_payment_returns_409(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, employees = await create_payroll_run(session)
        await mark_payment(
            session,
            run.id,
            employees[0].id,
            paid_at=date(2026, 5, 27),
            method="transfer",
            actor_user_id=actor.id,
        )

        with pytest.raises(HTTPException) as exc_info:
            await payroll_routes.patch_payout_split(
                run.id,
                employees[0].id,
                PayrollPayoutSplitPatch(amount_cash=Decimal("100")),
                session,
                CurrentActor(roles=frozenset({"manager"}), user_id=actor.id),
            )

        assert exc_info.value.status_code == 409
        assert exc_info.value.detail == "Выплата уже отмечена, сначала откатите"


async def test_create_or_update_drafts_uses_only_account_part_and_writes_events(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, employees = await create_payroll_run(
            session,
            employee_line_totals=[
                [Decimal("1000")],
                [Decimal("2000")],
                [Decimal("3000")],
            ],
        )
        await set_payout_split(
            session,
            run.id,
            employees[0].id,
            amount_cash=Decimal("250"),
            actor_user_id=actor.id,
        )
        await set_payout_split(
            session,
            run.id,
            employees[1].id,
            amount_cash=Decimal("2000"),
            actor_user_id=actor.id,
        )
        await set_payout_split(
            session,
            run.id,
            employees[2].id,
            amount_cash=Decimal("0"),
            actor_user_id=actor.id,
        )

        draft = await create_or_update_run_draft(
            session,
            run.id,
            actor_user_id=actor.id,
        )
        payments = (
            await session.scalars(
                select(PayrollPayment)
                .where(PayrollPayment.run_id == run.id)
                .order_by(PayrollPayment.amount)
            )
        ).all()
        draft_count = await session.scalar(
            select(func.count())
            .select_from(PayrollBankDraft)
            .where(PayrollBankDraft.run_id == run.id)
        )
        events = await events_for_run(session, run.id, action="bank_draft_created")

        assert draft_count == 1
        assert draft.document_id == f"teplo-payroll-{run.id}"
        assert draft.amount == Decimal("3750.00")
        assert draft.status == "created"
        assert payments[0].status == "draft_created"
        assert payments[0].draft_document_id is None
        assert payments[1].status == "planned"
        assert payments[2].status == "draft_created"
        assert [event.action for event in events] == ["bank_draft_created"]
        assert events[0].payload["amount_account"] == "3750.00"


async def test_create_or_update_drafts_is_idempotent_for_same_document_id(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _period, run, employees = await create_payroll_run(session)
        await set_payout_split(
            session,
            run.id,
            employees[0].id,
            amount_cash=Decimal("0"),
            actor_user_id=actor.id,
        )

        first_draft = await create_or_update_run_draft(session, run.id, actor_user_id=actor.id)
        first_document_id = first_draft.document_id

        second_draft = await create_or_update_run_draft(session, run.id, actor_user_id=actor.id)
        draft_count = await session.scalar(
            select(func.count())
            .select_from(PayrollBankDraft)
            .where(PayrollBankDraft.run_id == run.id)
        )
        compatibility_count = await create_or_update_drafts(session, run.id, actor_user_id=actor.id)

        assert draft_count == 1
        assert compatibility_count == 1
        assert second_draft.id == first_draft.id
        assert second_draft.document_id == first_document_id
        assert second_draft.status == "updated"


async def test_get_payout_deltas_after_recompute_classifies_all_directions(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        period, run, employees = await create_payroll_run(
            session,
            status="completed",
            period_status="open",
            employee_line_totals=[
                [Decimal("1000")],
                [Decimal("1000")],
                [Decimal("1000")],
            ],
        )
        await finalize_payroll_run(session, run.id, finalized_by_user_id=actor.id)
        for employee in employees:
            await mark_payment(
                session,
                run.id,
                employee.id,
                paid_at=date(2026, 5, 27),
                method="transfer",
                actor_user_id=actor.id,
            )
        await create_or_update_run_draft(session, run.id, actor_user_id=actor.id)
        await unfinalize_payroll_run(
            session,
            run.id,
            reason="Исправить начисления",
            actor_user_id=actor.id,
        )
        await patch_runner_recompute(
            monkeypatch,
            {
                employees[0].id: Decimal("1500"),
                employees[1].id: Decimal("800"),
                employees[2].id: Decimal("1000"),
            },
        )

        recomputed = await run_payroll(session, period.id, force_refresh=True)
        await finalize_payroll_run(session, recomputed.id, finalized_by_user_id=actor.id)
        deltas = await get_payout_deltas(session, recomputed.id)

        assert len(deltas) == 1
        assert deltas[0]["run_id"] == recomputed.id
        assert deltas[0]["classification"] == "topup"
        assert deltas[0]["previous_amount"] == Decimal("3000.00")
        assert deltas[0]["new_amount"] == Decimal("3300.00")
        assert deltas[0]["delta"] == Decimal("300.00")


async def test_apply_deltas_topup_creates_separate_mock_draft_and_event(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        period, run, employees = await create_payroll_run(
            session,
            status="completed",
            period_status="open",
        )
        await finalize_payroll_run(session, run.id, finalized_by_user_id=actor.id)
        await mark_payment(
            session,
            run.id,
            employees[0].id,
            paid_at=date(2026, 5, 27),
            method="transfer",
            actor_user_id=actor.id,
        )
        await create_or_update_run_draft(session, run.id, actor_user_id=actor.id)
        await unfinalize_payroll_run(
            session,
            run.id,
            reason="Добавили премию",
            actor_user_id=actor.id,
        )
        await patch_runner_recompute(monkeypatch, {employees[0].id: Decimal("1250")})
        recomputed = await run_payroll(session, period.id, force_refresh=True)
        await finalize_payroll_run(session, recomputed.id, finalized_by_user_id=actor.id)

        applied_count = await apply_payout_deltas(
            session,
            recomputed.id,
            actor_user_id=actor.id,
        )
        payment = await session.scalar(
            select(PayrollPayment).where(PayrollPayment.run_id == recomputed.id)
        )
        draft = await session.scalar(
            select(PayrollBankDraft).where(PayrollBankDraft.run_id == recomputed.id)
        )
        events = await events_for_run(session, recomputed.id, action="bank_draft_topup")

        assert payment is not None
        assert draft is not None
        assert applied_count == 1
        assert payment.amount == Decimal("1250.00")
        assert payment.amount_account == Decimal("1250.00")
        assert draft.amount == Decimal("1250.00")
        assert len(events) == 1
        assert events[0].payload["delta"] == "250.00"
        assert events[0].payload["document_id"] == (f"teplo-payroll-{recomputed.id}-topup-1")


async def test_apply_deltas_overpay_records_excess_without_bank_draft(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        period, run, employees = await create_payroll_run(
            session,
            status="completed",
            period_status="open",
        )
        await finalize_payroll_run(session, run.id, finalized_by_user_id=actor.id)
        await mark_payment(
            session,
            run.id,
            employees[0].id,
            paid_at=date(2026, 5, 27),
            method="transfer",
            actor_user_id=actor.id,
        )
        await create_or_update_run_draft(session, run.id, actor_user_id=actor.id)
        await unfinalize_payroll_run(
            session,
            run.id,
            reason="Сняли лишнюю корректировку",
            actor_user_id=actor.id,
        )
        await patch_runner_recompute(monkeypatch, {employees[0].id: Decimal("750")})
        recomputed = await run_payroll(session, period.id, force_refresh=True)
        await finalize_payroll_run(session, recomputed.id, finalized_by_user_id=actor.id)
        bank_client = RecordingBankClient()

        applied_count = await apply_payout_deltas(
            session,
            recomputed.id,
            actor_user_id=actor.id,
            bank_client=bank_client,
        )
        payment = await session.scalar(
            select(PayrollPayment).where(PayrollPayment.run_id == recomputed.id)
        )
        draft = await session.scalar(
            select(PayrollBankDraft).where(PayrollBankDraft.run_id == recomputed.id)
        )
        events = await events_for_run(session, recomputed.id, action="payout_overpaid")

        assert payment is not None
        assert draft is not None
        assert applied_count == 1
        assert payment.amount == Decimal("750.00")
        assert draft.amount == Decimal("750.00")
        assert bank_client.drafts == []
        assert len(events) == 1
        assert events[0].payload["overpaid_amount"] == "250.00"


async def test_tbank_live_payment_draft_posts_payload_and_bearer_header(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    class FakeResponse:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {"documentId": "bank-doc-1", "status": "created"}

    class FakeAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            calls["client_kwargs"] = kwargs

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def post(
            self,
            path: str,
            *,
            json: dict[str, Any],
            headers: dict[str, str],
        ) -> FakeResponse:
            calls["post"] = {"path": path, "json": json, "headers": headers}
            return FakeResponse()

    monkeypatch.setattr("app.services.banking.tbank.httpx.AsyncClient", FakeAsyncClient)

    async with async_session_factory() as session:
        session.add(
            SourceCredential(
                id=uuid.uuid4(),
                provider="tbank",
                credential_kind="bearer_token",
                value_encrypted="test-token",
                is_active=True,
            )
        )
        await session.commit()
        client = TbankClient(
            session,
            settings=Settings(
                teplo_bank_client_mode="live",
                tbank_payment_base_url="https://secured-openapi.tbank.ru",
            ),
        )

        result = await client.create_payment_draft(
            document_id="teplo-payroll-live-test",
            amount=Decimal("123.45"),
            purpose="Выплата заработной платы",
            requisites={
                "recipientName": "ШОКИНА КРИСТИНА",
                "inn": "890307589201",
                "kpp": "0",
                "bankAcnt": "40817810800023540968",
                "bankBik": "044525974",
                "corrAccount": "30101810145250000974",
                "executionOrder": 5,
            },
            payer_account="40702810900000000001",
        )

    assert result.document_id == "teplo-payroll-live-test"
    assert result.provider_ref == "bank-doc-1"
    assert calls["client_kwargs"]["base_url"] == "https://secured-openapi.tbank.ru"
    assert calls["client_kwargs"]["headers"]["Authorization"] == "Bearer test-token"
    assert calls["client_kwargs"]["headers"]["Content-Type"] == "application/json"
    assert calls["post"]["path"] == "/api/v1/payment/create"
    assert calls["post"]["headers"]["X-Request-Id"]
    assert calls["post"]["json"] == {
        "documentNumber": calls["post"]["json"]["documentNumber"],
        "amount": 123.45,
        "recipientName": "ШОКИНА КРИСТИНА",
        "inn": "890307589201",
        "kpp": "0",
        "bankAcnt": "40817810800023540968",
        "bankBik": "044525974",
        "accountNumber": "40702810900000000001",
        "paymentPurpose": "Выплата заработной платы",
        "executionOrder": 5,
        "recipientCorrAccountNumber": "30101810145250000974",
        "taxPayerStatus": "0",
        "kbk": "0",
        "oktmo": "0",
        "taxEvidence": "0",
        "taxPeriod": "0",
        "uin": "0",
        "taxDocNumber": "0",
        "taxDocDate": "0",
    }
    assert calls["post"]["json"]["documentNumber"].isdigit()
    assert 1 <= len(calls["post"]["json"]["documentNumber"]) <= 6


class RecordingBankClient:
    provider = "tbank"

    def __init__(self) -> None:
        self.drafts: list[dict[str, Any]] = []

    async def create_payment_draft(
        self,
        *,
        document_id: str,
        amount: Decimal,
        purpose: str,
        requisites: dict[str, Any],
        payer_account: str,
    ) -> PaymentDraftResult:
        self.drafts.append(
            {
                "document_id": document_id,
                "amount": amount,
                "purpose": purpose,
                "requisites": requisites,
                "payer_account": payer_account,
            }
        )
        return PaymentDraftResult(
            document_id=document_id,
            status="created",
            provider_ref=f"mock-{document_id}",
        )


async def create_actor_user(session: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"payroll-payout-actor-{uuid.uuid4()}@test.local",
        hashed_password="hashed",
        full_name="Payroll Payout Actor",
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
    period_index = await session.scalar(select(func.count()).select_from(PayrollPeriod))
    start_date = date(2026, 5, 19) + timedelta(days=int(period_index or 0) * 7)
    end_date = start_date + timedelta(days=6)
    payroll_date = end_date + timedelta(days=1)
    period = PayrollPeriod(
        id=uuid.uuid4(),
        period_type="week",
        start_date=start_date,
        end_date=end_date,
        payroll_date=payroll_date,
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
            full_name=f"Payroll Payout Employee {index + 1}",
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


async def events_for_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    action: str | None = None,
) -> list[PayrollRunEvent]:
    stmt = select(PayrollRunEvent).where(PayrollRunEvent.run_id == run_id)
    if action is not None:
        stmt = stmt.where(PayrollRunEvent.action == action)
    return list(
        (
            await session.scalars(stmt.order_by(PayrollRunEvent.created_at, PayrollRunEvent.action))
        ).all()
    )


async def patch_runner_recompute(
    monkeypatch: pytest.MonkeyPatch,
    employee_totals: dict[uuid.UUID, Decimal],
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
        lines = [
            make_payroll_line(run_id, employee_id, index, total_payable)
            for index, (employee_id, total_payable) in enumerate(employee_totals.items())
        ]
        return SimpleNamespace(
            lines=lines,
            blocking_issues=[],
            summary={"total_payable": float(sum(employee_totals.values(), Decimal("0")))},
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

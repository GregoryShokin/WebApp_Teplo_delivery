"""Регрессии ошибки получателя: один расход, идентифицируемый автор, хозяйственная дата."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin_payout_split import _safe_wallet
from test_deposit_bank_draft import _seed_article, _seed_employee
from test_payroll_payouts import create_actor_user

from app.api.deps import CurrentActor
from app.api.v1.routes import deposits as deposit_routes
from app.models import (
    AgentAction,
    AgentRun,
    CashflowTransaction,
    DepositAccount,
    DepositTransaction,
)
from app.services.deposit_dates import effective_deposit_date
from app.services.deposit_integrity import expected_balances
from app.services.deposit_service import (
    PRODUCTION_DEPOSIT_PAYOUT_ARTICLE_CODE,
    PRODUCTION_DEPOSIT_PAYOUT_SOURCE_KIND,
    add_transaction,
    book_production_deposit_payout_cashflow,
    transaction_payload,
)


async def test_cash_payout_keeps_recipient_author_and_single_expense(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 8 сентября в UTC уже является 9 сентября по Москве.
    recorded = datetime(2026, 9, 8, 21, 30, tzinfo=UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return recorded if tz is not None else recorded.replace(tzinfo=None)

    monkeypatch.setattr(deposit_routes, "datetime", FrozenDateTime)
    async with async_session_factory() as session:
        author = await create_actor_user(session)
        recipient = await _seed_employee(session)
        recipient.full_name = "Авакумов Александр"
        other = await _seed_employee(session)
        other.full_name = "Абдурахманов Сергей"
        await _safe_wallet(session)
        await _seed_article(session, PRODUCTION_DEPOSIT_PAYOUT_ARTICLE_CODE, "Выдача депозита")
        for employee, amount in [(recipient, Decimal("2000")), (other, Decimal("6000"))]:
            session.add(
                DepositAccount(
                    employee_id=employee.id,
                    balance=amount,
                    initial_balance=Decimal("0"),
                    last_updated=recorded,
                )
            )
            add_transaction(
                session,
                employee_id=employee.id,
                transaction_type="accrual",
                amount=amount,
                now=recorded,
                happened_on=date(2026, 9, 7),
            )
        await session.commit()
        result = await deposit_routes.payout_deposit(
            recipient.id,
            session,
            CurrentActor(
                user_id=author.id,
                roles=frozenset({"admin"}),
                permissions=frozenset({"finance.payout_channel.safe"}),
            ),
            deposit_routes.DepositPayoutRequest(amount=Decimal("2000"), payout_method="cash_safe"),
        )
        assert result["balance"] == "0.00"
        assert result["transaction"]["effective_date"] == "2026-09-09"
        transaction = await session.get(DepositTransaction, uuid.UUID(result["transaction"]["id"]))
        assert transaction.employee_id == recipient.id
        expenses = (
            await session.scalars(
                select(CashflowTransaction).where(
                    CashflowTransaction.source_kind == PRODUCTION_DEPOSIT_PAYOUT_SOURCE_KIND,
                    CashflowTransaction.source_id == transaction.id,
                )
            )
        ).all()
        assert len(expenses) == 1
        expense = expenses[0]
        assert expense.amount == Decimal("2000") and expense.direction == "out"
        assert expense.operation_date == transaction.happened_on == date(2026, 9, 9)
        assert expense.created_by_user_id == author.id
        assert recipient.full_name in expense.payment_purpose
        assert other.full_name not in expense.payment_purpose
        audit = await session.scalar(
            select(AgentRun).where(
                AgentRun.agent_name == "deposit_manual_change",
                AgentRun.params["employee_id"].astext == str(recipient.id),
            )
        )
        assert audit.params["actor_user_id"] == str(author.id)
        action = await session.scalar(
            select(AgentAction).where(AgentAction.agent_run_id == audit.id)
        )
        assert action.after_value["transaction"]["employee_id"] == str(recipient.id)
        balances = await expected_balances(session)
        assert balances[recipient.id] == Decimal("0")
        assert balances[other.id] == Decimal("6000")
        # Повторная бухгалтерская обработка той же выдачи не создаёт новый расход.
        await book_production_deposit_payout_cashflow(
            session,
            transaction=transaction,
            payout_method="cash_safe",
            transaction_date=date(2026, 9, 9),
            comment=None,
            employee_full_name=recipient.full_name,
            created_by_user_id=author.id,
        )
        await session.flush()
        expenses_after = (
            await session.scalars(
                select(CashflowTransaction).where(
                    CashflowTransaction.source_kind == PRODUCTION_DEPOSIT_PAYOUT_SOURCE_KIND,
                    CashflowTransaction.source_id == transaction.id,
                )
            )
        ).all()
        assert len(expenses_after) == 1 and expenses_after[0].id == expense.id


def test_history_payload_keeps_actual_day_and_original_registration() -> None:
    recorded = datetime(2026, 9, 7, 8, 3, tzinfo=UTC)
    transaction = DepositTransaction(
        transaction_type="dismissal_payout",
        amount=Decimal("2000"),
        happened_on=date(2026, 9, 8),
        created_at=recorded,
    )
    payload = transaction_payload(transaction)
    assert payload["happened_on"] == payload["effective_date"] == "2026-09-08"
    assert payload["created_at"] == recorded.isoformat()


@pytest.mark.parametrize(
    "recorded",
    [
        datetime(2026, 9, 7, 21, 30, tzinfo=UTC),
        datetime(2026, 9, 7, 21, 30),
    ],
)
def test_legacy_deposit_date_falls_back_to_moscow_day(recorded: datetime) -> None:
    transaction = DepositTransaction(created_at=recorded, happened_on=None)
    assert effective_deposit_date(transaction) == date(2026, 9, 8)

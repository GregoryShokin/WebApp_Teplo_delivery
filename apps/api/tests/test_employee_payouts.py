from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import CashflowTransaction, DdsArticle, Employee, EmployeePayout, Wallet
from app.services.employee_payouts import (
    EMPLOYEE_PAYOUT_SOURCE_KIND,
    create_cash_employee_payout,
)
from app.services.payroll_runner import PayrollConflictError, PayrollNotFoundError


async def _make_employee(session: AsyncSession) -> Employee:
    employee = Employee(
        id=uuid.uuid4(),
        full_name="Собственник Тест",
        iiko_id=f"iiko-{uuid.uuid4()}",
        status="active",
        is_senior=False,
        is_deputy_senior=False,
        pin_hash="hashed-pin",
        pin_set_at=datetime(2026, 5, 1, tzinfo=UTC),
        created_at=datetime(2026, 5, 1, tzinfo=UTC),
        updated_at=datetime(2026, 5, 1, tzinfo=UTC),
    )
    session.add(employee)
    await session.flush()
    return employee


async def _make_wallet(session: AsyncSession, *, wallet_type: str) -> Wallet:
    wallet = Wallet(
        id=uuid.uuid4(),
        code=f"w-{uuid.uuid4().hex[:8]}",
        name=f"Тест счёт ({wallet_type})",
        type=wallet_type,
        status="active",
        opening_balance=Decimal("0"),
    )
    session.add(wallet)
    await session.flush()
    return wallet


async def test_cash_payout_books_provodka_and_links(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        employee = await _make_employee(session)
        wallet = await _make_wallet(session, wallet_type="store_cash")
        await session.commit()

        payout = await create_cash_employee_payout(
            session,
            employee_id=employee.id,
            amount=Decimal("12000"),
            wallet_id=wallet.id,
            payout_date=date(2026, 5, 20),
            kind="owner_salary",
            note="  тест  ",
        )
        await session.commit()

        assert payout.status == "paid"
        assert payout.amount == Decimal("12000.00")
        assert payout.note == "тест"
        assert payout.cashflow_transaction_id is not None

        # Статья по умолчанию — «Зарплата собственника».
        article_code = await session.scalar(
            select(DdsArticle.code).where(DdsArticle.id == payout.article_id)
        )
        assert article_code == "zarplata_sobstvennika"

        # Ровно одна out-проводка с source_kind=employee_payout, source_id=payout.id.
        txn = await session.scalar(
            select(CashflowTransaction).where(
                CashflowTransaction.source_kind == EMPLOYEE_PAYOUT_SOURCE_KIND,
                CashflowTransaction.source_id == payout.id,
            )
        )
        assert txn is not None
        assert txn.direction == "out"
        assert txn.amount == Decimal("12000.00")
        assert txn.wallet_id == wallet.id
        assert txn.operation_date == date(2026, 5, 20)
        assert payout.cashflow_transaction_id == txn.id


async def test_bank_wallet_rejected(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        employee = await _make_employee(session)
        wallet = await _make_wallet(session, wallet_type="bank")
        await session.commit()

        with pytest.raises(PayrollConflictError):
            await create_cash_employee_payout(
                session,
                employee_id=employee.id,
                amount=Decimal("5000"),
                wallet_id=wallet.id,
                payout_date=date(2026, 5, 20),
            )
        # Ничего не создано.
        count = await session.scalar(select(EmployeePayout.id).where(EmployeePayout.employee_id == employee.id))
        assert count is None


async def test_non_positive_amount_rejected(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        employee = await _make_employee(session)
        wallet = await _make_wallet(session, wallet_type="cash_safe")
        await session.commit()

        with pytest.raises(PayrollConflictError):
            await create_cash_employee_payout(
                session,
                employee_id=employee.id,
                amount=Decimal("0"),
                wallet_id=wallet.id,
                payout_date=date(2026, 5, 20),
            )


async def test_explicit_article_id_used(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Явный article_id (выбор в диалоге плавающей кнопки) применяется вместо дефолта."""
    async with async_session_factory() as session:
        employee = await _make_employee(session)
        wallet = await _make_wallet(session, wallet_type="store_cash")
        article = DdsArticle(
            id=uuid.uuid4(),
            code=f"test-art-{uuid.uuid4().hex[:8]}",
            name="Тест выплата",
            movement_type="outflow",
            activity_type="operating",
            is_active=True,
        )
        session.add(article)
        await session.commit()

        payout = await create_cash_employee_payout(
            session,
            employee_id=employee.id,
            amount=Decimal("3000"),
            wallet_id=wallet.id,
            payout_date=date(2026, 5, 20),
            kind="salary",
            article_id=article.id,
        )
        await session.commit()
        assert payout.article_id == article.id
        txn = await session.scalar(
            select(CashflowTransaction).where(
                CashflowTransaction.source_kind == EMPLOYEE_PAYOUT_SOURCE_KIND,
                CashflowTransaction.source_id == payout.id,
            )
        )
        assert txn is not None and txn.article_id == article.id


async def test_unknown_article_id_rejected(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        employee = await _make_employee(session)
        wallet = await _make_wallet(session, wallet_type="store_cash")
        await session.commit()

        with pytest.raises(PayrollNotFoundError):
            await create_cash_employee_payout(
                session,
                employee_id=employee.id,
                amount=Decimal("3000"),
                wallet_id=wallet.id,
                payout_date=date(2026, 5, 20),
                article_id=uuid.uuid4(),
            )

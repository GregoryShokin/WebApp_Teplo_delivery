from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    Account,
    BankOperation,
    CashflowTransaction,
    DdsArticle,
    Employee,
    EmployeePayout,
    SafeAllocation,
    Wallet,
)
from app.services.banking.classifier import SAFE_WALLET_CODE
from app.services.employee_payouts import (
    EMPLOYEE_PAYOUT_BANK_TO_SAFE_SOURCE_KIND,
    EMPLOYEE_PAYOUT_SOURCE_KIND,
    confirm_employee_payout_by_operation,
    create_cash_employee_payout,
)
from app.services.payroll_payouts import MOCK_PAYER_ACCOUNT
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

        # Статья по умолчанию — «Зарплата административного персонала».
        article_code = await session.scalar(
            select(DdsArticle.code).where(DdsArticle.id == payout.article_id)
        )
        assert article_code == "zarplata_administrativnogo_personala"

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


async def test_bank_payout_confirm_books_transit_and_reserve(
    async_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Привязка банковской выплаты к исходящей операции → транзит банк→Сейф + резерв, paid."""
    monkeypatch.setattr(
        "app.services.employee_payouts.get_settings",
        lambda: SimpleNamespace(
            tbank_api_account_number=MOCK_PAYER_ACCOUNT,
            teplo_bank_client_mode="mock",
        ),
    )
    async with async_session_factory() as session:
        employee = await _make_employee(session)
        account = Account(
            id=uuid.uuid4(),
            bank_code="tbank",
            account_number=MOCK_PAYER_ACCOUNT,
            bic="044525974",
            legal_entity="ИП Тест",
            status="active",
        )
        session.add(account)
        await session.flush()
        bank_wallet = Wallet(
            id=uuid.uuid4(),
            code=f"bank-{uuid.uuid4().hex[:6]}",
            name="Банк (плательщик)",
            type="bank",
            status="active",
            account_id=account.id,
            opening_balance=Decimal("0"),
        )
        session.add(bank_wallet)
        await session.flush()
        # Сейф-кошелёк уже засеян миграциями; создаём только если его нет.
        safe_wallet = await session.scalar(
            select(Wallet).where(Wallet.code == SAFE_WALLET_CODE)
        )
        if safe_wallet is None:
            safe_wallet = Wallet(
                id=uuid.uuid4(),
                code=SAFE_WALLET_CODE,
                name="Сейф",
                type="cash_safe",
                status="active",
                opening_balance=Decimal("0"),
            )
            session.add(safe_wallet)
            await session.flush()
        elif safe_wallet.status != "active":
            safe_wallet.status = "active"
            await session.flush()
        payout = EmployeePayout(
            id=uuid.uuid4(),
            employee_id=employee.id,
            kind="owner_salary",
            amount=Decimal("40000"),
            payout_date=date(2026, 5, 20),
            wallet_id=bank_wallet.id,
            status="pending",
        )
        session.add(payout)
        operation = BankOperation(
            id=uuid.uuid4(),
            provider="tbank",
            provider_operation_id=f"op-{uuid.uuid4().hex[:8]}",
            operation_date=date(2026, 5, 21),
            direction="out",
            amount=Decimal("40000"),
            currency="RUB",
            raw_payload={},
            classification_status="pending",
        )
        session.add(operation)
        await session.commit()

        confirmed = await confirm_employee_payout_by_operation(
            session, payout_id=payout.id, bank_operation_id=operation.id
        )
        await session.commit()

        assert confirmed.status == "paid"
        assert confirmed.bank_operation_id == operation.id
        assert confirmed.safe_allocation_id is not None

        legs = (
            await session.scalars(
                select(CashflowTransaction).where(
                    CashflowTransaction.source_kind == EMPLOYEE_PAYOUT_BANK_TO_SAFE_SOURCE_KIND,
                    CashflowTransaction.source_id == payout.id,
                )
            )
        ).all()
        assert len(legs) == 2
        assert sorted(leg.direction for leg in legs) == ["in", "out"]
        # Транзит на дату операции, не выплаты.
        assert all(leg.operation_date == date(2026, 5, 21) for leg in legs)

        alloc = await session.get(SafeAllocation, confirmed.safe_allocation_id)
        assert alloc is not None
        assert alloc.status == "reserved"
        assert alloc.amount == Decimal("40000.00")

        # Идемпотентность: повторная привязка не двоит транзит.
        again = await confirm_employee_payout_by_operation(
            session, payout_id=payout.id, bank_operation_id=operation.id
        )
        assert again.status == "paid"
        legs_after = (
            await session.scalars(
                select(CashflowTransaction.id).where(
                    CashflowTransaction.source_kind == EMPLOYEE_PAYOUT_BANK_TO_SAFE_SOURCE_KIND,
                    CashflowTransaction.source_id == payout.id,
                )
            )
        ).all()
        assert len(legs_after) == 2

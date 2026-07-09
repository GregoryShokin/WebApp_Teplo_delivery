"""Привязка операции журнала ДДС к сотруднику → учёт «уже выплачено» в расчёте ЗП.

Покрывает: атрибуция создаёт EmployeePayout на зарплатной статье; гейт employee_id вне
зарплатной статьи; ре-разбор снимает прежнюю выплату; offset-шаг режет net с переносом
остатка; равенство кодов зарплатных статей между классификатором и окном «Новый платёж».
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from cp_helpers import make_account, make_bank_operation, make_expense_article, make_wallet
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    CashflowTransaction,
    Employee,
    EmployeePayout,
    EmployeePayoutOffset,
    PayrollLine,
    PayrollPeriod,
    PayrollRun,
    Wallet,
)
from app.services.banking.cashflow_classify import apply_cashflow_split
from app.services.banking.classifier import (
    EMPLOYEE_PAYOUT_ARTICLE_CODES,
    apply_operation_split,
)
from app.services.banking.safe_allocations import book_salary_via_safe, pay_allocation
from app.services.payroll_employee_payout_offset import (
    apply_employee_payout_offsets,
    apply_employee_payout_offsets_to_balances,
)

SALARY_CODE = "zarplata_administrativnogo_personala"


def test_article_codes_match_new_payment() -> None:
    """Дубль кодов зарплатных статей в классификаторе == окно «Новый платёж»."""
    from app.services.new_payment import EMPLOYEE_PAYOUT_ARTICLE_CODES as NP_CODES

    assert EMPLOYEE_PAYOUT_ARTICLE_CODES == NP_CODES


async def _employee(session: AsyncSession, name: str = "Тест Сотрудник") -> Employee:
    employee = Employee(full_name=name, iiko_id=f"iiko-{uuid.uuid4().hex[:12]}")
    session.add(employee)
    await session.flush()
    return employee


async def _salary_op(session: AsyncSession, *, amount: str = "100000.00"):
    account = await make_account(session)
    await make_wallet(session, wallet_type="bank", account_id=account.id)
    salary = await make_expense_article(
        session, code=SALARY_CODE, name="Зарплата административного персонала"
    )
    op = await make_bank_operation(session, amount=amount, direction="out", account_id=account.id)
    return salary, op


async def test_split_salary_article_creates_employee_payout(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        salary, op = await _salary_op(session)
        employee = await _employee(session)
        await session.commit()

        created = await apply_operation_split(
            session,
            op,
            splits=[(salary.id, Decimal("100000.00"), None, None, employee.id)],
        )
        await session.commit()

        payout = await session.scalar(
            select(EmployeePayout).where(EmployeePayout.bank_operation_id == op.id)
        )
        assert payout is not None
        assert payout.employee_id == employee.id
        assert payout.kind == "salary"  # без назначения должности → обычный сотрудник
        assert payout.status == "paid"
        assert payout.amount == Decimal("100000.00")
        assert payout.payout_date == op.operation_date
        assert payout.cashflow_transaction_id == created[0]
        # Второй проводки НЕ создаём — только одна из split.
        cf_count = await session.scalar(
            select(func.count())
            .select_from(CashflowTransaction)
            .where(CashflowTransaction.source_id == op.id)
        )
        assert cf_count == 1


async def test_cashflow_split_salary_article_creates_employee_payout(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Единая логика: разбор РУЧНОЙ проводки на зарплатную статью + сотрудник → EmployeePayout."""
    async with async_session_factory() as session:
        wallet = await make_wallet(session, wallet_type="cash")
        salary = await make_expense_article(
            session, code=SALARY_CODE, name="Зарплата административного персонала"
        )
        employee = await _employee(session)
        txn = CashflowTransaction(
            wallet_id=wallet.id,
            direction="out",
            amount=Decimal("50000.00"),
            operation_date=date(2026, 7, 3),
            source_kind="manual",
            quality_status="manual_override",
        )
        session.add(txn)
        await session.flush()
        await session.commit()
        txn_id = txn.id

        await apply_cashflow_split(
            session, txn, splits=[(salary.id, Decimal("50000.00"), None, None, employee.id)]
        )
        await session.commit()

        payout = await session.scalar(
            select(EmployeePayout).where(EmployeePayout.employee_id == employee.id)
        )
        assert payout is not None
        assert payout.kind == "salary"
        assert payout.status == "paid"
        assert payout.amount == Decimal("50000.00")
        # Первая доля = сама проводка (её id сохраняется); операции выписки нет.
        assert payout.cashflow_transaction_id == txn_id
        assert payout.bank_operation_id is None


async def test_salary_via_safe_reserve_then_payout_creates_employee_payout(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«Через Сейф»: топ-ап + резерв под сотрудника; EmployeePayout — только по «Выплачено»."""
    async with async_session_factory() as session:
        account = await make_account(session)
        await make_wallet(session, wallet_type="bank", account_id=account.id)
        # Сейф-кошелёк и транзитные статьи есть в справочнике (сиды 0048/0114) — берём готовые.
        safe = await session.scalar(select(Wallet).where(Wallet.code == "cash_safe"))
        assert safe is not None
        salary = await make_expense_article(
            session, code=SALARY_CODE, name="Зарплата административного персонала"
        )
        op = await make_bank_operation(
            session, amount="100000.00", direction="out", account_id=account.id
        )
        employee = await _employee(session)
        await session.commit()

        allocation = await book_salary_via_safe(
            session, op, article_id=salary.id, employee_id=employee.id
        )
        await session.commit()

        assert allocation.wallet_id == safe.id
        assert allocation.employee_id == employee.id
        assert allocation.amount == Decimal("100000.00")
        assert allocation.status == "reserved"
        assert allocation.source_operation_id == op.id
        # До оплаты резерва выплаты в ЗП НЕТ.
        assert await session.scalar(select(func.count()).select_from(EmployeePayout)) == 0

        leg_id = await pay_allocation(
            session, allocation, amount=Decimal("100000.00"), operation_date=date(2026, 7, 3)
        )
        await session.commit()

        payout = await session.scalar(
            select(EmployeePayout).where(EmployeePayout.employee_id == employee.id)
        )
        assert payout is not None
        assert payout.status == "paid"
        assert payout.amount == Decimal("100000.00")
        assert payout.safe_allocation_id == allocation.id
        assert payout.cashflow_transaction_id == leg_id


async def test_employee_id_on_non_salary_article_rejected(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        account = await make_account(session)
        await make_wallet(session, wallet_type="bank", account_id=account.id)
        expense = await make_expense_article(session)  # payment_to_supplier — не зарплатная
        op = await make_bank_operation(
            session, amount="5000.00", direction="out", account_id=account.id
        )
        employee = await _employee(session)
        await session.commit()

        with pytest.raises(ValueError, match="только для зарплатной статьи"):
            await apply_operation_split(
                session,
                op,
                splits=[(expense.id, Decimal("5000.00"), None, None, employee.id)],
            )


async def test_resplit_clears_prior_payout(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        salary, op = await _salary_op(session)
        expense = await make_expense_article(session)  # payment_to_supplier
        employee = await _employee(session)
        await session.commit()

        await apply_operation_split(
            session, op, splits=[(salary.id, Decimal("100000.00"), None, None, employee.id)]
        )
        await session.commit()
        assert await session.scalar(
            select(func.count()).select_from(EmployeePayout)
        ) == 1

        # Пере-разбор на не-зарплатную статью — прежняя выплата должна исчезнуть.
        await apply_operation_split(
            session, op, splits=[(expense.id, Decimal("100000.00"), None, None, None)]
        )
        await session.commit()
        assert await session.scalar(
            select(func.count()).select_from(EmployeePayout)
        ) == 0


async def test_offset_reduces_net_and_carries_remainder(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        employee = await _employee(session)
        period = PayrollPeriod(
            period_type="half_month",
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 15),
            payroll_date=date(2026, 7, 15),
            status="open",
        )
        session.add(period)
        await session.flush()
        run = PayrollRun(
            period_id=period.id,
            started_at=datetime.now(UTC),
            status="running",
            blocking_issues=[],
            summary={},
        )
        session.add(run)
        await session.flush()
        payout = EmployeePayout(
            employee_id=employee.id,
            kind="salary",
            amount=Decimal("100000.00"),
            payout_date=date(2026, 7, 3),
            status="paid",
        )
        session.add(payout)
        line = PayrollLine(
            run_id=run.id,
            employee_id=employee.id,
            role="Администратор",
            total_payable=Decimal("40000.00"),
            components={},
        )
        session.add(line)
        await session.flush()

        summary = await apply_employee_payout_offsets(session, period, run, [line])
        assert summary["employee_payout_offset_count"] == 1
        # net зажат до 0, зачтено 40 000 — остаток 60 000 переносится.
        assert line.total_payable == Decimal("0.00")
        assert line.employee_payout_offset == Decimal("40000.00")
        offset_amount = await session.scalar(
            select(EmployeePayoutOffset.amount).where(EmployeePayoutOffset.run_id == run.id)
        )
        assert offset_amount == Decimal("40000.00")

        # Финализация двигает баланс потребления; остаток выплаты остаётся непогашенным.
        await apply_employee_payout_offsets_to_balances(
            session, run, datetime.now(UTC), reverse=False
        )
        refreshed = await session.get(EmployeePayout, payout.id)
        assert refreshed.offset_amount == Decimal("40000.00")

        # Дефинализация — точный реверс (значение в сессии; refresh сбросил бы неслитый реверс).
        await apply_employee_payout_offsets_to_balances(
            session, run, datetime.now(UTC), reverse=True
        )
        assert payout.offset_amount == Decimal("0.00")

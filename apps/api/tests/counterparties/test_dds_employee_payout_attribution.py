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
from app.services.banking.safe_allocations import book_safe_topup_reserves, pay_allocation
from app.services.payroll_advance_service import book_operation_advance
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

        allocations = await book_safe_topup_reserves(
            session, op, reserves=[(salary.id, Decimal("100000.00"), employee.id)]
        )
        await session.commit()
        allocation = allocations[0]

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


async def test_operation_advance_creates_salary_advance(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Разбор операции как аванс → SalaryAdvance(issued, вся сумма, source_operation_id)."""
    async with async_session_factory() as session:
        account = await make_account(session)
        await make_wallet(session, wallet_type="bank", account_id=account.id)
        op = await make_bank_operation(
            session, amount="10000.00", direction="out", account_id=account.id
        )
        employee = await _employee(session)
        await session.commit()

        advance = await book_operation_advance(
            session, op, employee_id=employee.id, kind="advance"
        )
        await session.commit()

        assert advance.kind == "advance"
        assert advance.amount == Decimal("10000.00")
        # Аванс на всю сумму: гасится min(amount, net) → остаток переносится (installments_count=1).
        assert advance.per_installment_amount == Decimal("10000.00")
        assert advance.installments_count == 1
        assert advance.status == "issued"
        assert advance.source_operation_id == op.id
        assert advance.issued_on == op.operation_date


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
        # Не-зарплатная статья БЕЗ обязательного контрагента: тест про снятие выплаты, а
        # «Оплата поставщикам» теперь требует контрагента и увела бы его в сторону.
        expense = await make_expense_article(session, code="prochie_rashody", name="Прочие расходы")
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


async def test_exclude_cancels_the_payout_of_the_operation(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«Исключить» операцию снимает и «выплачено» — иначе ЗП недоплачена на эту сумму.

    Хвост фикса 30.07.2026: исключение убрало проводку из статьи, но ``EmployeePayout``
    оставался, и расчёт продолжал вычитать из «к выдаче» выплату, за которой в учёте больше
    нет денег. Отмена мягкая (``status='cancelled'``) — этот статус уже выведен из расчётов.
    """
    from app.services.banking.classifier import apply_operation_action

    async with async_session_factory() as session:
        salary, op = await _salary_op(session)
        employee = await _employee(session)
        await session.commit()

        await apply_operation_split(
            session, op, splits=[(salary.id, Decimal("100000.00"), None, None, employee.id)]
        )
        await session.commit()
        payout = (await session.scalars(select(EmployeePayout))).one()
        assert payout.status == "paid"

        await apply_operation_action(session, op, action="exclude")
        await session.commit()

        await session.refresh(payout)
        assert payout.status == "cancelled"
        assert await session.scalar(select(func.count()).select_from(EmployeePayout)) == 1, (
            "отмена мягкая — запись выплаты остаётся видна"
        )


async def test_exclude_refuses_when_the_payout_is_already_offset(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Зачтённую в проведённой ведомости выплату исключение не отменяет, а отказывает.

    Тот же гард, что у переразбора: снять зачёт вправе только дефинализация ведомости.
    Молча разошедшийся баланс «к выдаче» дороже отказа.
    """
    from app.services.banking.classifier import apply_operation_action

    async with async_session_factory() as session:
        salary, op = await _salary_op(session)
        employee = await _employee(session)
        await session.commit()

        await apply_operation_split(
            session, op, splits=[(salary.id, Decimal("100000.00"), None, None, employee.id)]
        )
        await session.commit()
        payout = (await session.scalars(select(EmployeePayout))).one()
        payout.offset_amount = Decimal("40000.00")
        await session.commit()
        # id держим отдельно: после rollback объекты сессии истекают, и обращение к
        # ``op.id`` полезло бы за ленивой загрузкой уже вне async-контекста.
        payout_id, op_id = payout.id, op.id

        with pytest.raises(ValueError) as error:
            await apply_operation_action(session, op, action="exclude")
        assert "дефинализация" in str(error.value)

        await session.rollback()
        refreshed = await session.get(EmployeePayout, payout_id)
        assert refreshed.status == "paid", "отказ не меняет ни выплату, ни разметку"
        rows = (
            await session.scalars(
                select(CashflowTransaction).where(
                    CashflowTransaction.source_kind == "bank_operation",
                    CashflowTransaction.source_id == op_id,
                )
            )
        ).all()
        assert [row.quality_status for row in rows] != ["excluded"]


async def test_manual_cashflow_exclude_cancels_its_payout(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Та же дыра в ручном контуре: исключение проводки снимает её «выплачено».

    ``apply_cashflow_split`` заводит выплату так же, как разбор выписки, поэтому и отменять
    её обязаны оба входа — иначе исключение ручной проводки тихо занижало бы «к выдаче».
    """
    from app.services.banking.cashflow_classify import apply_cashflow_exclude

    async with async_session_factory() as session:
        wallet = await make_wallet(session, name="Сейф выплат", wallet_type="cash_safe")
        salary = await make_expense_article(
            session, code=SALARY_CODE, name="Зарплата административного персонала"
        )
        employee = await _employee(session)
        txn = CashflowTransaction(
            wallet_id=wallet.id,
            direction="out",
            amount=Decimal("50000.00"),
            operation_date=date(2026, 7, 20),
            source_kind="kassa_payout",
            payment_purpose="Выплата сотруднику",
            quality_status="manual_override",
        )
        session.add(txn)
        await session.flush()
        await session.commit()

        await apply_cashflow_split(
            session, txn, splits=[(salary.id, Decimal("50000.00"), None, None, employee.id)]
        )
        await session.commit()
        payout = (await session.scalars(select(EmployeePayout))).one()
        assert payout.status == "paid"

        await apply_cashflow_exclude(session, txn)
        await session.commit()

        await session.refresh(payout)
        assert payout.status == "cancelled"

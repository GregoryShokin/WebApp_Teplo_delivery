"""Интеграция: новый порядок выплаты администрации (проводки по «Выплатить»).

Порядок:
1. «Сформировать черновик» — только заявка в банк, НИКАКИХ проводок ДДС.
2. Платёж исполнен (статус T-Bank) — внутренний перевод банк→Сейф на безналичную часть.
3. «Выплатить (N)» — расход ЗП из Сейфа по статьям только по выбранным сотрудникам,
   инкрементально и без дублей.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_payroll_payouts import RecordingBankClient, create_actor_user

from app.core.config import get_settings
from app.models import (
    Account,
    CashflowTransaction,
    DdsArticle,
    Employee,
    PayrollLine,
    PayrollPeriod,
    PayrollRun,
    Wallet,
)
from app.services.payroll_payments import mark_payments_selected
from app.services.payroll_payout_allocation import (
    DDS_ARTICLE_ADMIN_PAYROLL,
    DDS_ARTICLE_AUX_PAYROLL,
)
from app.services.payroll_payouts import (
    BANK_TO_SAFE_SOURCE_KIND,
    MOCK_PAYER_ACCOUNT,
    PAYROLL_PAYOUT_SOURCE_KIND,
    SAFE_WALLET_CODE,
    apply_payroll_draft_status,
    create_or_update_run_draft,
    set_run_payout_cash,
)
from app.services.position_registry import reset_position_registry_for_tests


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_position_registry_for_tests()
    yield
    reset_position_registry_for_tests()


async def _payer_wallet(session: AsyncSession) -> tuple[Account, Wallet]:
    settings = get_settings()
    payer_account_num = settings.tbank_api_account_number or MOCK_PAYER_ACCOUNT
    wallet = await session.scalar(
        select(Wallet)
        .join(Account, Account.id == Wallet.account_id)
        .where(Account.account_number == payer_account_num, Wallet.status == "active")
    )
    if wallet is not None:
        account = await session.get(Account, wallet.account_id)
        return account, wallet
    account = Account(
        id=uuid.uuid4(),
        bank_code="tbank",
        account_number=payer_account_num,
        bic="044525974",
        legal_entity="ИП Шокина Кристина Юрьевна",
        currency="RUB",
        status="active",
    )
    session.add(account)
    await session.flush()
    wallet = Wallet(
        id=uuid.uuid4(),
        code=f"test-payer-{uuid.uuid4().hex[:8]}",
        name="Test Payer",
        type="bank",
        currency="RUB",
        status="active",
        account_id=account.id,
    )
    session.add(wallet)
    await session.commit()
    return account, wallet


async def _safe_wallet(session: AsyncSession) -> Wallet:
    wallet = await session.scalar(select(Wallet).where(Wallet.code == SAFE_WALLET_CODE))
    assert wallet is not None, "Сейф (cash_safe) должен быть засеян миграцией"
    return wallet


async def _make_admin_run(
    session: AsyncSession, line_specs: list[tuple[str, Decimal]]
) -> tuple[PayrollRun, list[tuple[uuid.UUID, str]]]:
    period = PayrollPeriod(
        id=uuid.uuid4(),
        period_type="half_month",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 15),
        payroll_date=date(2026, 6, 16),
        status="finalized",
        finalized_at=datetime(2026, 6, 16, tzinfo=UTC),
    )
    run = PayrollRun(
        id=uuid.uuid4(),
        period_id=period.id,
        started_at=datetime(2026, 6, 16, tzinfo=UTC),
        finished_at=datetime(2026, 6, 16, 1, tzinfo=UTC),
        status="finalized",
        blocking_issues=[],
        summary={"kind": "admin"},
        is_imported_legacy=False,
    )
    session.add_all([period, run])
    await session.flush()
    employees: list[tuple[uuid.UUID, str]] = []
    for role, total in line_specs:
        employee = Employee(
            id=uuid.uuid4(),
            full_name=f"Admin {role} {uuid.uuid4().hex[:6]}",
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
        session.add(employee)
        await session.flush()
        session.add(
            PayrollLine(
                id=uuid.uuid4(),
                run_id=run.id,
                employee_id=employee.id,
                role=role,
                base_pay=total,
                premium=Decimal("0"),
                percent_pay=Decimal("0"),
                vacation_pay=Decimal("0"),
                ndfl_withheld=Decimal("0"),
                fund_accrual=Decimal("0"),
                deduction=Decimal("0"),
                total_payable=total,
                deposit_excluded_for_run=False,
                components={},
            )
        )
        employees.append((employee.id, role))
    await session.commit()
    return run, employees


async def _txns(session: AsyncSession, run_id: uuid.UUID, source_kind: str) -> list[CashflowTransaction]:
    return list(
        (
            await session.scalars(
                select(CashflowTransaction).where(
                    CashflowTransaction.source_kind == source_kind,
                    CashflowTransaction.source_id == run_id,
                )
            )
        ).all()
    )


async def _article_id(session: AsyncSession, code: str) -> uuid.UUID:
    return await session.scalar(select(DdsArticle.id).where(DdsArticle.code == code))


async def test_admin_draft_books_no_cashflow(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Шаг 1: «Сформировать черновик» создаёт только заявку в банк, без проводок ДДС."""
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        await _payer_wallet(session)
        run, _employees = await _make_admin_run(
            session,
            [("Менеджер", Decimal("30000")), ("Уборщица", Decimal("15000"))],
        )
        await set_run_payout_cash(
            session, run.id, amount_cash=Decimal("15000"), cash_wallet_code="cash_safe",
            actor_user_id=actor.id,
        )
        draft = await create_or_update_run_draft(
            session, run.id, actor_user_id=actor.id, bank_client=RecordingBankClient()
        )
        assert draft is not None
        assert draft.amount == Decimal("30000.00")  # банковская часть = ФОТ 45000 − наличные 15000

        # Никаких проводок ДДС на черновике (ни выплаты, ни перевода).
        assert await _txns(session, run.id, PAYROLL_PAYOUT_SOURCE_KIND) == []
        assert await _txns(session, run.id, BANK_TO_SAFE_SOURCE_KIND) == []


async def test_paid_books_bank_to_safe_transfer(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Шаг 2: при статусе «исполнен» создаётся внутренний перевод банк→Сейф, идемпотентно."""
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _account, payer_wallet = await _payer_wallet(session)
        safe_wallet = await _safe_wallet(session)
        run, _employees = await _make_admin_run(
            session,
            [("Менеджер", Decimal("30000")), ("Уборщица", Decimal("15000"))],
        )
        await set_run_payout_cash(
            session, run.id, amount_cash=Decimal("15000"), cash_wallet_code="cash_safe",
            actor_user_id=actor.id,
        )
        draft = await create_or_update_run_draft(
            session, run.id, actor_user_id=actor.id, bank_client=RecordingBankClient()
        )

        status = await apply_payroll_draft_status(session, draft=draft, raw_status="executed")
        assert status == "paid"

        transfer = await _txns(session, run.id, BANK_TO_SAFE_SOURCE_KIND)
        assert len(transfer) == 2
        bank_leg = next(t for t in transfer if t.wallet_id == payer_wallet.id)
        safe_leg = next(t for t in transfer if t.wallet_id == safe_wallet.id)
        # банк-нога: списание со счёта (для матчинга выписки), Сейф-нога: приток (двигает баланс Сейфа)
        assert bank_leg.direction == "out" and bank_leg.amount == Decimal("30000.00")
        assert safe_leg.direction == "in" and safe_leg.amount == Decimal("30000.00")

        # Идемпотентность: повторный «исполнен» не дублирует перевод.
        await apply_payroll_draft_status(session, draft=draft, raw_status="executed")
        assert len(await _txns(session, run.id, BANK_TO_SAFE_SOURCE_KIND)) == 2


async def test_payout_books_expense_from_safe_incrementally(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Шаг 3: «Выплатить» заводит расход из Сейфа по статьям только по выбранным, без дублей."""
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        await _payer_wallet(session)
        safe_wallet = await _safe_wallet(session)
        run, employees = await _make_admin_run(
            session,
            [("Менеджер", Decimal("30000")), ("Уборщица", Decimal("15000"))],
        )
        by_role = {role: emp_id for emp_id, role in employees}
        admin_id = await _article_id(session, DDS_ARTICLE_ADMIN_PAYROLL)
        aux_id = await _article_id(session, DDS_ARTICLE_AUX_PAYROLL)

        # Выплачиваем только менеджера (30000 → «Зарплата админ»).
        marked = await mark_payments_selected(
            session, run.id, [by_role["Менеджер"]], paid_at=date(2026, 6, 16), actor_user_id=actor.id
        )
        assert marked == 1
        expense = await _txns(session, run.id, PAYROLL_PAYOUT_SOURCE_KIND)
        assert all(t.wallet_id == safe_wallet.id and t.direction == "out" for t in expense)
        by_article = {t.article_id: t.amount for t in expense}
        assert by_article == {admin_id: Decimal("30000.00")}

        # Выплачиваем уборщицу (15000 → «Содержание торговых точек»): добавляется расход.
        marked = await mark_payments_selected(
            session, run.id, [by_role["Уборщица"]], paid_at=date(2026, 6, 16), actor_user_id=actor.id
        )
        assert marked == 1
        expense = await _txns(session, run.id, PAYROLL_PAYOUT_SOURCE_KIND)
        totals: dict[uuid.UUID, Decimal] = {}
        for t in expense:
            totals[t.article_id] = totals.get(t.article_id, Decimal("0")) + t.amount
        assert totals == {admin_id: Decimal("30000.00"), aux_id: Decimal("15000.00")}

        # Повторное «Выплатить» уже оплаченных — без дублей (rows пустой → расхода нет).
        marked = await mark_payments_selected(
            session, run.id, [by_role["Менеджер"], by_role["Уборщица"]],
            paid_at=date(2026, 6, 16), actor_user_id=actor.id,
        )
        assert marked == 0
        totals_after: dict[uuid.UUID, Decimal] = {}
        for t in await _txns(session, run.id, PAYROLL_PAYOUT_SOURCE_KIND):
            totals_after[t.article_id] = totals_after.get(t.article_id, Decimal("0")) + t.amount
        assert totals_after == {admin_id: Decimal("30000.00"), aux_id: Decimal("15000.00")}

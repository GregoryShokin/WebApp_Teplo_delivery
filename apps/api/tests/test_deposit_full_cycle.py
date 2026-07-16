"""Полный цикл банк-выдачи депозита: черновик → оплата → резерв → фактическая выдача.

Закрепляет ключевые инварианты этапов 2–3 (см. docs/deposit-full-cycle-plan.md):

* 🔴 **R1** — резерв Сейфа под выдачу депозита создаётся с ``employee_id=None``. Иначе
  ``pay_allocation`` завёл бы ``EmployeePayout`` вида salary, и выдача депозита срезала бы
  зарплату сотрудника из ближайшей ведомости. Ссылка на получателя живёт в ``DepositBankDraft``.
* Депозит-счёт НЕ трогается, пока черновик висит — списывается только при фактической выдаче
  (оплате резерва). Пока черновик активен, сотрудник не считается рассчитанным при увольнении.
* Без Сейф-кошелька черновик остаётся ``created`` (поллинг повторит), «Выплатить» не залипает.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin_payout_split import _payer_wallet, _safe_wallet
from test_payroll_payouts import RecordingBankClient

from app.models import (
    DdsArticle,
    DepositAccount,
    DepositBankDraft,
    DepositTransaction,
    Employee,
    EmployeePayout,
    SafeAllocation,
)
from app.services.banking.safe_allocations import pay_allocation
from app.services.deposit_bank_draft import (
    DEPOSIT_PAYOUT_ARTICLE_CODE,
    apply_deposit_draft_status,
    create_deposit_payout_draft,
    deposit_in_flight_amount,
    sync_deposit_after_allocation_change,
)
from app.services.dismissal_reconciliation_service import _deposit_settled
from app.services.wallets import (
    DDS_ARTICLE_TRANSFER_IN_CODE,
    DDS_ARTICLE_TRANSFER_OUT_CODE,
)


async def _seed_article(session: AsyncSession, code: str, name: str) -> uuid.UUID:
    existing = await session.scalar(select(DdsArticle).where(DdsArticle.code == code))
    if existing is not None:
        return existing.id
    article = DdsArticle(
        id=uuid.uuid4(),
        code=code,
        name=name,
        movement_type="outflow",
        activity_type="operating",
    )
    session.add(article)
    await session.flush()
    return article.id


async def _seed_employee_with_deposit(
    session: AsyncSession, balance: Decimal
) -> Employee:
    employee = Employee(
        id=uuid.uuid4(),
        full_name=f"Повар {uuid.uuid4().hex[:6]}",
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
        DepositAccount(
            id=uuid.uuid4(),
            employee_id=employee.id,
            balance=balance,
            initial_balance=balance,
            last_updated=datetime(2026, 6, 1, tzinfo=UTC),
        )
    )
    await session.flush()
    return employee


async def _seed_bank_and_articles(session: AsyncSession) -> None:
    await _payer_wallet(session)
    await _safe_wallet(session)
    await _seed_article(session, DDS_ARTICLE_TRANSFER_OUT_CODE, "Выбытие — перевод")
    await _seed_article(session, DDS_ARTICLE_TRANSFER_IN_CODE, "Поступление — перевод")
    await _seed_article(session, DEPOSIT_PAYOUT_ARTICLE_CODE, "Выдача депозита")


async def _employee_payouts(session: AsyncSession, employee_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            select(func.count(EmployeePayout.id)).where(
                EmployeePayout.employee_id == employee_id
            )
        )
        or 0
    )


async def test_full_cycle_r1_reserve_without_employee_and_disburse(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Черновик → paid → резерв (R1: employee_id=None, без EmployeePayout) → выдача резерва."""
    async with async_session_factory() as session:
        await _seed_bank_and_articles(session)
        employee = await _seed_employee_with_deposit(session, Decimal("5000"))

        # 1. Черновик заведён, депозит НЕ тронут, транзакции-леджера нет.
        draft = await create_deposit_payout_draft(
            session,
            recipient_kind="production",
            amount=Decimal("5000"),
            purpose="Выдача депозита — тест",
            provider="tbank",
            employee_id=employee.id,
            bank_client=RecordingBankClient(),
        )
        assert draft.status in ("created", "updated")
        assert draft.provider_ref is not None
        assert draft.deposit_transaction_id is None
        assert draft.safe_allocation_id is None
        account = await session.scalar(
            select(DepositAccount).where(DepositAccount.employee_id == employee.id)
        )
        assert account.balance == Decimal("5000")
        ledger_before = await session.scalar(
            select(func.count(DepositTransaction.id)).where(
                DepositTransaction.employee_id == employee.id
            )
        )
        assert ledger_before == 0

        # 2. Оплата пришла (webhook/поллинг) → транзит + резерв Сейфа.
        status_after = await apply_deposit_draft_status(
            session, draft=draft, raw_status="executed", commit=True
        )
        assert status_after == "paid"
        assert draft.safe_allocation_id is not None

        allocation = await session.get(SafeAllocation, draft.safe_allocation_id)
        # 🔴 R1: резерв БЕЗ сотрудника — иначе pay_allocation срежет зарплату.
        assert allocation.employee_id is None
        assert allocation.status == "reserved"
        assert allocation.amount == Decimal("5000")
        # Депозит всё ещё цел (списание только при фактической выдаче).
        assert account.balance == Decimal("5000")
        assert await _employee_payouts(session, employee.id) == 0

        # 3. Фактическая выдача: оплата резерва + свод черновика.
        await pay_allocation(
            session,
            allocation,
            amount=Decimal("5000"),
            operation_date=date(2026, 7, 16),
        )
        # 🔴 R1 через pay_allocation: EmployeePayout не создан (резерв без сотрудника).
        assert await _employee_payouts(session, employee.id) == 0

        disbursement = await sync_deposit_after_allocation_change(
            session, allocation_id=allocation.id
        )
        assert disbursement is not None
        assert disbursement.amount == Decimal("5000")
        assert disbursement.employee_id == employee.id
        await session.commit()

        assert draft.status == "disbursed"
        assert draft.deposit_transaction_id is not None

        # Депозит-леджер: ровно одна out-проводка выдачи, баланс обнулён.
        payout = await session.scalar(
            select(DepositTransaction).where(
                DepositTransaction.employee_id == employee.id,
                DepositTransaction.transaction_type == "payout",
            )
        )
        assert payout is not None
        assert payout.amount == Decimal("5000")
        assert account.balance == Decimal("0")


async def test_paid_without_safe_wallet_keeps_draft_created(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """R6: нет активного Сейф-кошелька → черновик остаётся created (поллинг повторит)."""
    async with async_session_factory() as session:
        await _payer_wallet(session)
        safe_wallet = await _safe_wallet(session)
        safe_wallet.status = "inactive"  # Сейф недоступен
        await session.flush()
        await _seed_article(session, DDS_ARTICLE_TRANSFER_OUT_CODE, "Выбытие — перевод")
        await _seed_article(session, DDS_ARTICLE_TRANSFER_IN_CODE, "Поступление — перевод")
        await _seed_article(session, DEPOSIT_PAYOUT_ARTICLE_CODE, "Выдача депозита")
        employee = await _seed_employee_with_deposit(session, Decimal("5000"))

        draft = await create_deposit_payout_draft(
            session,
            recipient_kind="production",
            amount=Decimal("5000"),
            purpose="Выдача депозита — тест",
            provider="tbank",
            employee_id=employee.id,
            bank_client=RecordingBankClient(),
        )
        status_after = await apply_deposit_draft_status(
            session, draft=draft, raw_status="executed", commit=False
        )
        # В paid НЕ перешли — резерва нет, «Выплатить» не залипло.
        assert status_after == "created"
        await session.refresh(draft)
        assert draft.safe_allocation_id is None


async def test_deposit_not_settled_while_bank_draft_active(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Гейт увольнения: висящий банк-черновик (даже paid) держит сотрудника нерассчитанным."""
    async with async_session_factory() as session:
        await _seed_bank_and_articles(session)
        # Баланс 0, но активный черновик выдачи висит → расчёт не закрыт.
        employee = await _seed_employee_with_deposit(session, Decimal("0"))
        session.add(
            DepositBankDraft(
                id=uuid.uuid4(),
                recipient_kind="production",
                employee_id=employee.id,
                document_id="teplo-deposit-test",
                amount=Decimal("5000"),
                status="paid",
                bank_provider="tbank",
            )
        )
        await session.flush()

        assert await deposit_in_flight_amount(session, employee.id) == Decimal("5000")
        assert await _deposit_settled(session, employee.id) is False

"""Интеграция: выплата административной ведомости с разнесением по статьям ДДС.

Сквозной сценарий по примеру владельца: ФОТ 70 000 (вспомогательный 25 000 →
«Содержание торговых точек», администрация 45 000 → «Зарплата административного
персонала»), сплит наличными 20 000 (Сейф) / банком 50 000. Проверяем: каскад наличных,
проводки ДДС по статьям и кошелькам, идемпотентность и групповой матчинг одной банковской
операции 50 000 с группой prebooked-проводок.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_payroll_payouts import RecordingBankClient, create_actor_user

from app.models import (
    Account,
    BankOperation,
    CashflowTransaction,
    DdsArticle,
    Employee,
    PayrollLine,
    PayrollPeriod,
    PayrollRun,
    Wallet,
)
from app.services.banking.classifier import run_classification_rules
from app.services.payroll_payout_allocation import (
    DDS_ARTICLE_ADMIN_PAYROLL,
    DDS_ARTICLE_AUX_PAYROLL,
)
from app.core.config import get_settings
from app.services.payroll_payouts import (
    MOCK_PAYER_ACCOUNT,
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
    """Банковский кошелёк плательщика (счёт ИП) — существующий из сида или созданный.

    В тестовой среде ``tbank_api_account_number`` указывает на засеянный счёт, поэтому
    берём именно его, иначе ``_upsert_payout_cashflow`` спишет на другой кошелёк.
    """
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


async def _make_admin_run(
    session: AsyncSession, line_specs: list[tuple[str, Decimal]]
) -> PayrollRun:
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
    await session.commit()
    return run


async def _payout_txns(session: AsyncSession, run_id: uuid.UUID) -> list[CashflowTransaction]:
    return list(
        (
            await session.scalars(
                select(CashflowTransaction).where(
                    CashflowTransaction.source_kind == "payroll_payout",
                    CashflowTransaction.source_id == run_id,
                )
            )
        ).all()
    )


async def test_admin_payout_splits_articles_and_group_matches(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        payer_account, payer_wallet = await _payer_wallet(session)
        run = await _make_admin_run(
            session,
            [
                ("Менеджер", Decimal("30000")),
                ("Управляющий", Decimal("15000")),
                ("Уборщица", Decimal("15000")),
                ("Посудомойка", Decimal("10000")),
            ],
        )

        # Сплит: 20 000 наличными с Сейфа, остальное (50 000) банком на счёт ИП.
        await set_run_payout_cash(
            session,
            run.id,
            amount_cash=Decimal("20000"),
            cash_wallet_code="cash_safe",
            actor_user_id=actor.id,
        )
        draft = await create_or_update_run_draft(
            session, run.id, actor_user_id=actor.id, bank_client=RecordingBankClient()
        )
        # В банк уходит один черновик на всю банковскую часть.
        assert draft.amount == Decimal("50000.00")

        aux_id = await session.scalar(
            select(DdsArticle.id).where(DdsArticle.code == DDS_ARTICLE_AUX_PAYROLL)
        )
        admin_id = await session.scalar(
            select(DdsArticle.id).where(DdsArticle.code == DDS_ARTICLE_ADMIN_PAYROLL)
        )
        cash_wallet = await session.scalar(select(Wallet).where(Wallet.code == "cash_safe"))

        txns = await _payout_txns(session, run.id)
        by_key: dict[tuple, Decimal] = {}
        for txn in txns:
            key = (txn.wallet_id, txn.article_id)
            by_key[key] = by_key.get(key, Decimal("0")) + txn.amount

        # Банк: содержание 5 000 (недобор после налички) + зарплата админ 45 000.
        assert by_key[(payer_wallet.id, aux_id)] == Decimal("5000.00")
        assert by_key[(payer_wallet.id, admin_id)] == Decimal("45000.00")
        # Наличные с Сейфа: вся наличка ушла на содержание (приоритет вспомогательной корзины).
        assert by_key[(cash_wallet.id, aux_id)] == Decimal("20000.00")
        # Наличной части по администрации нет.
        assert (cash_wallet.id, admin_id) not in by_key
        # Контроль сумм по кошелькам.
        assert sum(t.amount for t in txns if t.wallet_id == payer_wallet.id) == Decimal("50000.00")
        assert sum(t.amount for t in txns if t.wallet_id == cash_wallet.id) == Decimal("20000.00")

        # Идемпотентность: повторный черновик пересоздаёт группу, не плодит проводки.
        await create_or_update_run_draft(
            session, run.id, actor_user_id=actor.id, bank_client=RecordingBankClient()
        )
        txns_again = await _payout_txns(session, run.id)
        assert len(txns_again) == len(txns) == 3

        # Групповой матчинг: одна банковская операция 50 000 матчится с группой банковских
        # проводок выплаты (5 000 + 45 000) и привязывается к одной из них.
        operation = BankOperation(
            id=uuid.uuid4(),
            provider="tbank",
            provider_operation_id=f"op-{uuid.uuid4()}",
            account_id=payer_account.id,
            operation_date=datetime.now(UTC).date(),
            direction="out",
            amount=Decimal("50000.00"),
            currency="RUB",
            raw_payload={},
            classification_status="pending",
        )
        session.add(operation)
        await session.commit()

        await run_classification_rules(session, [operation])
        await session.refresh(operation)

        assert operation.classification_status == "classified"
        assert operation.cashflow_transaction_id is not None
        linked = await session.get(CashflowTransaction, operation.cashflow_transaction_id)
        assert linked is not None
        assert linked.wallet_id == payer_wallet.id  # привязка к банковской проводке группы
        assert linked.source_id == run.id


async def test_admin_payout_all_cash_books_only_cash_side(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        actor = await create_actor_user(session)
        _account, payer_wallet = await _payer_wallet(session)
        run = await _make_admin_run(
            session,
            [
                ("Менеджер", Decimal("30000")),
                ("Уборщица", Decimal("15000")),
            ],
        )
        # Вся выплата наличными с ТК Черникова — банковской части (и черновика) нет,
        # но наличные всё равно заводятся проводками ДДС по статьям.
        await set_run_payout_cash(
            session,
            run.id,
            amount_cash=Decimal("45000"),
            cash_wallet_code="tk_chernikova",
            actor_user_id=actor.id,
        )
        draft = await create_or_update_run_draft(
            session, run.id, actor_user_id=actor.id, bank_client=RecordingBankClient()
        )
        assert draft is None  # банковского черновика нет

        cash_wallet = await session.scalar(select(Wallet).where(Wallet.code == "tk_chernikova"))
        aux_id = await session.scalar(
            select(DdsArticle.id).where(DdsArticle.code == DDS_ARTICLE_AUX_PAYROLL)
        )
        admin_id = await session.scalar(
            select(DdsArticle.id).where(DdsArticle.code == DDS_ARTICLE_ADMIN_PAYROLL)
        )
        txns = await _payout_txns(session, run.id)
        # Только наличные проводки на ТК Черникова, банковских нет.
        assert txns and all(t.wallet_id == cash_wallet.id for t in txns)
        by_article = {t.article_id: t.amount for t in txns}
        assert by_article[aux_id] == Decimal("15000.00")
        assert by_article[admin_id] == Decimal("30000.00")
        assert sum(t.amount for t in txns) == Decimal("45000.00")

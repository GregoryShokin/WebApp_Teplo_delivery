"""Этап 5: выплатной поток отложенной выдачи депозита («Выплатить»).

Выдача депозита (PayrollLine.deposit_payout_scheduled) разносится ОТДЕЛЬНОЙ корзиной по
статье «Выдача депозита» (не под зарплатной); «на руки» = ФОТ + выдача (растит банк-черновик
и наличный сплит); наличная часть выдачи с ТК Черникова → iiko-сумма для изъятия.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin_payout_split import _payer_wallet, _safe_wallet

from app.models import (
    CashflowTransaction,
    DdsArticle,
    Employee,
    PayrollLine,
    PayrollPeriod,
    PayrollRun,
    Wallet,
)
from app.services.payroll_payout_allocation import DDS_ARTICLE_PRODUCTION_PAYROLL
from app.services.payroll_payouts import (
    DDS_ARTICLE_DEPOSIT_PAYOUT,
    PAYROLL_PAYOUT_SOURCE_KIND,
    SAFE_WALLET_CODE,
    book_payout_expense_for_employees,
    set_run_payout_cash,
)
from app.services.position_registry import reset_position_registry_for_tests


async def _seed_deposit_article(session: AsyncSession) -> None:
    existing = await session.scalar(
        select(DdsArticle).where(DdsArticle.code == DDS_ARTICLE_DEPOSIT_PAYOUT)
    )
    if existing is None:
        session.add(
            DdsArticle(
                id=uuid.uuid4(),
                code=DDS_ARTICLE_DEPOSIT_PAYOUT,
                name="Выдача депозита",
                movement_type="outflow",
                activity_type="operating",
            )
        )
        await session.flush()


async def _make_production_run_with_deposit(
    session: AsyncSession,
    *,
    total_payable: Decimal,
    deposit_payout: Decimal,
) -> tuple[PayrollRun, uuid.UUID]:
    period = PayrollPeriod(
        id=uuid.uuid4(),
        period_type="half_month",
        start_date=date(2026, 6, 16),
        end_date=date(2026, 6, 22),
        payroll_date=date(2026, 6, 23),
        status="finalized",
        finalized_at=datetime(2026, 6, 23, tzinfo=UTC),
    )
    run = PayrollRun(
        id=uuid.uuid4(),
        period_id=period.id,
        started_at=datetime(2026, 6, 23, tzinfo=UTC),
        finished_at=datetime(2026, 6, 23, 1, tzinfo=UTC),
        status="finalized",
        blocking_issues=[],
        summary={},  # без kind=admin → производственная (статья ЗП производственного)
        is_imported_legacy=False,
    )
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
    session.add_all([period, run, employee])
    await session.flush()
    session.add(
        PayrollLine(
            id=uuid.uuid4(),
            run_id=run.id,
            employee_id=employee.id,
            role="Повар",
            base_pay=total_payable,
            premium=Decimal("0"),
            percent_pay=Decimal("0"),
            vacation_pay=Decimal("0"),
            ndfl_withheld=Decimal("0"),
            fund_accrual=Decimal("0"),
            deduction=Decimal("0"),
            total_payable=total_payable,
            deposit_excluded_for_run=False,
            deposit_payout_scheduled=deposit_payout,
            components={},
        )
    )
    await session.commit()
    return run, employee.id


async def _txns(
    session: AsyncSession, run_id: uuid.UUID
) -> list[CashflowTransaction]:
    return list(
        (
            await session.scalars(
                select(CashflowTransaction).where(
                    CashflowTransaction.source_kind == PAYROLL_PAYOUT_SOURCE_KIND,
                    CashflowTransaction.source_id == run_id,
                )
            )
        ).all()
    )


async def _article_code_by_id(session: AsyncSession) -> dict[uuid.UUID, str]:
    rows = (await session.execute(select(DdsArticle.id, DdsArticle.code))).all()
    return {article_id: code for article_id, code in rows}


async def test_deposit_payout_books_separate_bucket_from_safe(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Выдача депозита (банк-часть) — отдельной проводкой с Сейфа по статье «Выдача депозита»."""
    reset_position_registry_for_tests()
    try:
        async with async_session_factory() as session:
            await _payer_wallet(session)
            safe_wallet = await _safe_wallet(session)
            await _seed_deposit_article(session)
            run, employee_id = await _make_production_run_with_deposit(
                session, total_payable=Decimal("10000"), deposit_payout=Decimal("5000")
            )

            result = await book_payout_expense_for_employees(session, run, [employee_id])
            assert result.booked is True
            assert result.deposit_iiko_amount == Decimal("0")  # всё безналом → iiko не нужен

            codes = await _article_code_by_id(session)
            rows = await _txns(session, run.id)
            by_article: dict[str, Decimal] = {}
            for tx in rows:
                assert tx.wallet_id == safe_wallet.id  # cash=0 → всё с Сейфа
                assert tx.direction == "out"
                by_article[codes.get(tx.article_id)] = by_article.get(
                    codes.get(tx.article_id), Decimal("0")
                ) + tx.amount
            # ЗП и выдача депозита — РАЗНЫЕ статьи (не смешаны).
            assert by_article[DDS_ARTICLE_PRODUCTION_PAYROLL] == Decimal("10000.00")
            assert by_article[DDS_ARTICLE_DEPOSIT_PAYOUT] == Decimal("5000.00")
    finally:
        reset_position_registry_for_tests()


async def test_deposit_payout_cash_from_tk_returns_iiko_amount(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Наличная выдача депозита с ТК Черникова → отдельная проводка с ТК + iiko-сумма."""
    reset_position_registry_for_tests()
    try:
        async with async_session_factory() as session:
            actor_wallet = await _payer_wallet(session)
            assert actor_wallet is not None
            await _safe_wallet(session)
            await _seed_deposit_article(session)
            tk_wallet = await session.scalar(
                select(Wallet).where(Wallet.code == "tk_chernikova")
            )
            assert tk_wallet is not None, "ТК Черникова засеяна миграцией 0115"
            run, employee_id = await _make_production_run_with_deposit(
                session, total_payable=Decimal("10000"), deposit_payout=Decimal("5000")
            )
            # Вся выплата наличными из ТК Черникова: ЗП 10000 + депозит 5000 = 15000.
            await set_run_payout_cash(
                session,
                run.id,
                amount_cash=Decimal("15000"),
                cash_wallet_code="tk_chernikova",
                actor_user_id=None,
            )

            result = await book_payout_expense_for_employees(session, run, [employee_id])
            assert result.booked is True
            # Наличная часть выдачи депозита с ТК → в iiko.
            assert result.deposit_iiko_amount == Decimal("5000.00")

            codes = await _article_code_by_id(session)
            rows = await _txns(session, run.id)
            deposit_rows = [
                tx for tx in rows if codes.get(tx.article_id) == DDS_ARTICLE_DEPOSIT_PAYOUT
            ]
            assert len(deposit_rows) == 1
            assert deposit_rows[0].wallet_id == tk_wallet.id
            assert deposit_rows[0].amount == Decimal("5000.00")
    finally:
        reset_position_registry_for_tests()

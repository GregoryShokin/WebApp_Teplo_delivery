"""Закрытый месяц: цифры признанного расхода в нём не меняются.

У признанного расхода появился потребитель — отчёт «расход по месяцам». С этого момента цифра
закрытого месяца перестала быть внутренним делом витрины: её увидят в P&L и сверят с банком.
А изменить её до сих пор можно было молча — правкой периода, откатом, ночным признанием.

Замок, а не снимок: в расчётах с контрагентами пересчёта задним числом нет, начисление меняется
только осознанным действием человека. Открыть месяц обратно можно — замок нужен не ради
неприкосновенности, а чтобы закрытый отчёт нельзя было изменить НЕЗАМЕТНО.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from cp_helpers import make_counterparty, make_expense_article, make_invoice
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import SupplierExpenseAccrual
from app.services import accounting_periods
from app.services import supplier_service_periods as periods
from app.services.expense_reversal import ReversalRefused, reverse_expense
from app.services.subscription_accruals import SELF_BILLED_SOURCE


async def _recognized_accrual(
    session: AsyncSession, *, name: str, inn: str, month: date, amount: str = "5000.00"
) -> SupplierExpenseAccrual:
    cp = await make_counterparty(session, name=name, inn=inn)
    article = await make_expense_article(session, code=f"CLS-{inn}", name="Услуги")
    last = date(month.year, month.month, 28)
    invoice = await make_invoice(
        session,
        counterparty_id=cp.id,
        amount=amount,
        doc_kind="closing",
        source=SELF_BILLED_SOURCE,
        external_id=f"self:close-{inn}:{month:%Y-%m}",
        invoice_date=last,
    )
    accrual = SupplierExpenseAccrual(
        counterparty_id=cp.id,
        invoice_id=invoice.id,
        article_id=article.id,
        amount=Decimal(amount),
        service_period_start=month,
        service_period_end=last,
        status="recognized",
        recognition_month=month,
    )
    session.add(accrual)
    await session.flush()
    return accrual


async def test_closing_a_running_month_is_refused(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Незакончившийся месяц закрывать нечего — в нём ещё будут документы и платежи."""
    async with async_session_factory() as session:
        from app.services import clock

        current = clock.moscow_today().replace(day=1)
        with pytest.raises(accounting_periods.PeriodClosed) as exc:
            await accounting_periods.close_month(
                session, period_month=current, actor_user_id=None
            )
        assert "ещё не закончился" in str(exc.value)


async def test_period_change_is_blocked_in_a_closed_month(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Перенос периода не выносит расход из закрытого месяца и не вносит в него."""
    async with async_session_factory() as session:
        accrual = await _recognized_accrual(
            session, name="Замок Перенос", inn="6155000900", month=date(2026, 6, 1)
        )
        await accounting_periods.close_month(
            session, period_month=date(2026, 6, 1), actor_user_id=None
        )

        with pytest.raises(accounting_periods.PeriodClosed) as exc:
            await periods.change_accrual_period(
                session,
                accrual=accrual,
                start=date(2026, 7, 1),
                end=date(2026, 7, 31),
                actor_user_id=None,
                reason="перенос",
            )
        assert "06.2026 закрыт" in str(exc.value)


async def test_reversal_is_blocked_in_a_closed_month(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Откат расхода закрытого месяца отклоняется — цифра уже в отчётности."""
    async with async_session_factory() as session:
        accrual = await _recognized_accrual(
            session, name="Замок Откат", inn="6155000901", month=date(2026, 6, 1)
        )
        await accounting_periods.close_month(
            session, period_month=date(2026, 6, 1), actor_user_id=None
        )

        with pytest.raises(ReversalRefused) as exc:
            await reverse_expense(
                session,
                accrual,
                amount=Decimal("1000.00"),
                reason="ошибка",
                actor_user_id=None,
            )
        assert "06.2026 закрыт" in str(exc.value)


async def test_nightly_recognition_skips_closed_month_and_resumes_after_reopen(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ночное признание не дописывает расход в закрытый месяц, но возвращается после открытия.

    Молча дописать расход в закрытый месяц — худшее из возможного: отчёт изменится сам, и
    никто не узнает. Поэтому начисление остаётся scheduled и ждёт решения человека.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Замок Джоба", inn="6155000902")
        article = await make_expense_article(session, code="CLS-JOB", name="Услуги")
        invoice = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="4000.00",
            doc_kind="closing",
            invoice_date=date(2026, 6, 30),
        )
        accrual = SupplierExpenseAccrual(
            counterparty_id=cp.id,
            invoice_id=invoice.id,
            article_id=article.id,
            amount=Decimal("4000.00"),
            service_period_start=date(2026, 6, 1),
            service_period_end=date(2026, 6, 30),
            status="scheduled",
        )
        session.add(accrual)
        await accounting_periods.close_month(
            session, period_month=date(2026, 6, 1), actor_user_id=None
        )

        count = await periods.recognize_due_expenses(session, as_of=date(2026, 7, 5))
        assert count == 0, "расход дописан в закрытый месяц"
        await session.refresh(accrual)
        assert accrual.status == "scheduled"

        # Человек открыл период — признание идёт своим ходом.
        await accounting_periods.reopen_month(session, period_month=date(2026, 6, 1))
        count = await periods.recognize_due_expenses(session, as_of=date(2026, 7, 5))
        assert count == 1
        await session.refresh(accrual)
        assert accrual.status == "recognized"
        assert accrual.recognition_month == date(2026, 6, 1)


async def test_incoming_document_does_not_erase_expense_of_a_closed_month(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Настоящий УПД, пришедший позже, не снимает признанный расход закрытого месяца.

    Это единственный путь, которым признанный расход уменьшается АВТОМАТИЧЕСКИ, без участия
    человека: supersede_self_billed аннулирует самоакт, а sync_invoice_accrual снимает его
    начисление. Признать взамен расход по настоящему документу уже нельзя — джоба в закрытый
    месяц не пишет. Месяц остался бы вообще без расхода, молча и в фоновом пути.
    """
    from app.services.subscription_accruals import supersede_self_billed

    async with async_session_factory() as session:
        accrual = await _recognized_accrual(
            session, name="Замок УПД", inn="6155000903", month=date(2026, 6, 1), amount="13000.00"
        )
        self_billed = await session.get(SupplierExpenseAccrual, accrual.id)
        assert self_billed is not None
        await accounting_periods.close_month(
            session, period_month=date(2026, 6, 1), actor_user_id=None
        )

        real = await make_invoice(
            session,
            counterparty_id=accrual.counterparty_id,
            amount="13000.00",
            number="УПД-ПОЗЖЕ",
            doc_kind="closing",
            operational_scope="finance",
            invoice_date=date(2026, 6, 30),
        )
        real.dds_article_id = accrual.article_id
        real.service_period_start = date(2026, 6, 1)
        real.service_period_end = date(2026, 6, 30)
        real.service_period_status = "ready"
        await session.flush()

        victims = await supersede_self_billed(session, real)
        await periods.sync_invoice_accrual(session, real)
        await session.commit()

        assert victims == [], "самоакт закрытого месяца замещён — расход исчез бы"
        await session.refresh(accrual)
        assert accrual.status == "recognized"
        assert accrual.amount == Decimal("13000.00")

"""Сигнал застрявших начислений — то, что молча не доедет до отчёта.

Ночное признание расходов пропускает начисление, чей период услуги задевает закрытый месяц, и
делает это через голый ``continue``: расхода нет, документ есть, кредиторка есть, а сигнала нет
ни одного. Начисление остаётся в ожидании навсегда. Дефект спящий ровно до первого закрытия
месяца — и просыпается в тот же миг, поэтому сигнал обязан существовать раньше, чем владелец
первый раз закроет месяц ради балансового снимка.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from cp_helpers import make_counterparty
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AccountingPeriodClose, SupplierExpenseAccrual
from app.services.accounting_readiness import build_month_readiness

TODAY = date(2026, 9, 15)


async def _accrual(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    amount: str,
    start: date,
    end: date,
    status: str = "scheduled",
) -> SupplierExpenseAccrual:
    accrual = SupplierExpenseAccrual(
        id=uuid.uuid4(),
        counterparty_id=counterparty_id,
        amount=Decimal(amount),
        service_period_start=start,
        service_period_end=end,
        status=status,
    )
    session.add(accrual)
    await session.flush()
    return accrual


async def test_accrual_stuck_behind_a_closed_month_is_reported(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Период задевает закрытый месяц — признание пропускает его молча и навсегда."""
    async with async_session_factory() as session:
        supplier = await make_counterparty(session, name="ЭкоЦентр")
        session.add(AccountingPeriodClose(period_month=date(2026, 8, 1)))
        await _accrual(
            session,
            counterparty_id=supplier.id,
            amount="15580.00",
            start=date(2026, 8, 1),
            end=date(2026, 8, 31),
        )
        await session.commit()

        readiness = await build_month_readiness(session, month=date(2026, 8, 1), as_of=TODAY)

        blocked = next(
            item for item in readiness.items if item.code == "accruals_blocked_by_closed_period"
        )
        assert blocked.count == 1
        assert blocked.amount == Decimal("15580.00")
        assert blocked.rows[0]["counterparty"] == "ЭкоЦентр"
        assert readiness.ready is False, "закрывать месяц с потерянным расходом нельзя"


async def test_overdue_accrual_without_a_closed_month_means_the_job_did_not_run(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Закрытых месяцев нет, а расход не признан — значит до него не дошла ночная джоба.

    Причины разные и лечатся по-разному, поэтому и сигналы разные: здесь месяц открывать не
    нужно, нужно понять, почему джоба молчала.
    """
    async with async_session_factory() as session:
        supplier = await make_counterparty(session, name="ДоксИнБокс")
        await _accrual(
            session,
            counterparty_id=supplier.id,
            amount="3700.00",
            start=date(2026, 8, 1),
            end=date(2026, 8, 31),
        )
        await session.commit()

        readiness = await build_month_readiness(session, month=date(2026, 8, 1), as_of=TODAY)

        missed = next(
            item for item in readiness.items if item.code == "accruals_overdue_not_recognized"
        )
        assert missed.count == 1
        assert missed.amount == Decimal("3700.00")
        assert not [
            item for item in readiness.items if item.code == "accruals_blocked_by_closed_period"
        ]


async def test_future_period_and_recognized_expense_are_not_signals(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Будущий период ждать признания и должен, а признанный расход уже доехал.

    Сигнал, кричащий на здоровые строки, перестают читать через неделю.
    """
    async with async_session_factory() as session:
        supplier = await make_counterparty(session, name="Лема")
        await _accrual(
            session,
            counterparty_id=supplier.id,
            amount="1000.00",
            start=date(2026, 10, 1),
            end=date(2026, 10, 31),
        )
        await _accrual(
            session,
            counterparty_id=supplier.id,
            amount="2000.00",
            start=date(2026, 7, 1),
            end=date(2026, 7, 31),
            status="recognized",
        )
        await session.commit()

        readiness = await build_month_readiness(session, month=date(2026, 8, 1), as_of=TODAY)

        assert readiness.items == []
        assert readiness.ready is True

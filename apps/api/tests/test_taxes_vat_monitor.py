"""Монитор зарплатного критерия НДС-льготы (подп. 38 п. 3 ст. 149 НК).

Проверяем: действующий сотрудник берётся из последнего месяца (ушедший в декрет — нет),
показатель по полным месяцам, вердикт против введённого порога.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.tax import TaxPayrollLedger
from app.services.taxes.vat_monitor import evaluate_vat_wage_criterion


def _ledger(
    month: int, tab: str, employee: str, *, accrued: str, oklad: str = "50000.00"
) -> TaxPayrollLedger:
    return TaxPayrollLedger(
        id=uuid.uuid4(),
        year=2026,
        month=month,
        tab_number=tab,
        employee=employee,
        accrued=Decimal(accrued),
        oklad=Decimal(oklad),
    )


async def test_active_employee_is_from_latest_month(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Действующий сотрудник — из последнего месяца; ушедшая в декрет не берётся."""
    async with async_session_factory() as session:
        session.add(_ledger(5, "205", "ДОЛГОПОЛОВА К.Д.", accrued="13158.00"))
        session.add(_ledger(5, "206", "ВОДОЛАЗОВА В.С.", accrued="26316.00"))
        session.add(_ledger(6, "206", "ВОДОЛАЗОВА В.С.", accrued="28571.00"))
        session.add(_ledger(7, "206", "ВОДОЛАЗОВА В.С.", accrued="50000.00"))
        await session.flush()

        result = await evaluate_vat_wage_criterion(session, year=2026)

        assert result.active_tab == "206"
        assert result.active_employee == "ВОДОЛАЗОВА В.С."
        # Долгополова (декрет) исключена — в показателе только месяцы Водолазовой.
        assert [m.month for m in result.months] == [5, 6, 7]
        # Полный месяц — только июль (начислено 50 000 = оклад).
        assert result.indicator_full == Decimal("50000.00")
        # По всем месяцам: (26316 + 28571 + 50000) / 3.
        assert result.indicator_all == Decimal("34962.33")


async def test_criterion_passes_and_fails_against_threshold(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        session.add(_ledger(7, "206", "ВОДОЛАЗОВА В.С.", accrued="50000.00"))
        await session.flush()

        fail = await evaluate_vat_wage_criterion(
            session, year=2026, threshold=Decimal("57000")
        )
        assert fail.passes is False
        assert fail.margin == Decimal("-7000.00")
        assert any("НЕ проходит" in m for m in fail.messages)

        ok = await evaluate_vat_wage_criterion(
            session, year=2026, threshold=Decimal("45000")
        )
        assert ok.passes is True
        assert ok.margin == Decimal("5000.00")


async def test_no_threshold_reports_indicator_only(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        session.add(_ledger(7, "206", "ВОДОЛАЗОВА В.С.", accrued="50000.00"))
        await session.flush()

        result = await evaluate_vat_wage_criterion(session, year=2026)

        assert result.passes is None
        assert result.indicator_full == Decimal("50000.00")
        assert any("порог не введён" in m for m in result.messages)


async def test_no_ledger_is_empty(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        result = await evaluate_vat_wage_criterion(session, year=2026)

    assert result.active_employee is None
    assert result.indicator_full is None
    assert result.months == []

"""Сводка «начислено / уплачено / осталось» (решение владельца 27.07.2026).

Отвечает на вопрос «сколько начислено за период, сколько уплатили, сколько осталось».
Два края, ради которых сводка и переписывалась: уплаченное не должно задваиваться при
сосуществовании банковского факта и кассовой конвенции за один месяц, а «осталось» не
должно показывать ноль там, где начисления известны не за весь период.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.tax import TaxPayment, TaxPayrollLedger
from app.services.taxes.engine import YEAR_CONFIGS, compute_tax_state
from app.services.taxes.ledger_summary import build_ledger_summary
from app.services.taxes.repository import load_tax_inputs

CFG = YEAR_CONFIGS[2026]


def _ledger(month: int, *, contributions: str, ndfl: str, injury: str = "0") -> TaxPayrollLedger:
    return TaxPayrollLedger(
        id=uuid.uuid4(),
        year=2026,
        month=month,
        tab_number="206",
        employee="ВОДОЛАЗОВА В.С.",
        contributions=Decimal(contributions),
        ndfl=Decimal(ndfl),
        injury=Decimal(injury),
    )


def _paid(
    kind: str,
    amount: str,
    *,
    period: str | None,
    source: str = "bank_statement",
    paid_on: date = date(2026, 3, 27),
) -> TaxPayment:
    return TaxPayment(
        id=uuid.uuid4(),
        bundle_id=uuid.uuid4(),
        paid_on=paid_on,
        kind=kind,
        amount=Decimal(amount),
        recipient="sfr" if kind == "contrib_injury" else "fns",
        for_year=2026,
        for_period=period,
        status="paid",
        source_kind=source,
        quality_status="confirmed",
    )


async def _build(session: AsyncSession, as_of: date):
    inputs = await load_tax_inputs(session, as_of=as_of)
    state = compute_tax_state(inputs)
    return await build_ledger_summary(
        session, state=state, inputs=inputs, cfg=CFG, as_of=as_of
    )


async def test_rows_cover_all_kinds(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """В сводке — все виды платежей контура, включая НДФЛ и травматизм."""
    async with async_session_factory() as session:
        summary = await _build(session, date(2026, 7, 26))

    kinds = [row.kind for row in summary.rows]
    assert kinds == [
        "usn_advance",
        "contrib_employees",
        "ndfl",
        "contrib_fixed",
        "contrib_extra_1pct",
        "contrib_injury",
    ]
    ndfl = next(row for row in summary.rows if row.kind == "ndfl")
    assert ndfl.reduces_tax is False  # НДФЛ налог ИП не уменьшает — это налог работника
    injury = next(row for row in summary.rows if row.kind == "contrib_injury")
    assert injury.recipient == "sfr"


async def test_paid_not_doubled_between_bank_and_convention(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Банковский факт и разнос оборотки за ОДИН месяц — это один платёж, не два."""
    async with async_session_factory() as session:
        session.add(_ledger(6, contributions="8571.30", ndfl="3532.00"))
        session.add(_paid("contrib_employees", "8571.30", period="2026-06"))
        session.add(
            _paid("contrib_employees", "8571.30", period="2026-06", source="tax_notice")
        )
        await session.commit()

        summary = await _build(session, date(2026, 7, 26))

    contrib = next(row for row in summary.rows if row.kind == "contrib_employees")
    assert contrib.paid == Decimal("8571.30")  # не 17 142,60


async def test_left_is_none_when_turnover_incomplete(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Оборотка не за все месяцы — «осталось» прочерк, а не обманчивый ноль."""
    async with async_session_factory() as session:
        session.add(_ledger(6, contributions="8571.30", ndfl="3532.00"))
        await session.commit()

        summary = await _build(session, date(2026, 7, 26))  # ждём 6 месяцев, есть 1

    contrib = next(row for row in summary.rows if row.kind == "contrib_employees")
    assert contrib.accrued == Decimal("8571.30")
    assert contrib.left is None
    assert contrib.note is not None and "1 мес. из 6" in contrib.note


async def test_left_computed_when_turnover_complete(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Оборотка за все закрывшиеся месяцы — «осталось» считается честной разницей."""
    async with async_session_factory() as session:
        for month in range(1, 7):
            session.add(_ledger(month, contributions="1000.00", ndfl="500.00"))
        session.add(_paid("contrib_employees", "4000.00", period="2026-01"))
        await session.commit()

        summary = await _build(session, date(2026, 7, 26))

    contrib = next(row for row in summary.rows if row.kind == "contrib_employees")
    assert contrib.accrued == Decimal("6000.00")
    assert contrib.paid == Decimal("4000.00")
    assert contrib.left == Decimal("2000.00")


async def test_self_contributions_split_into_two_rows(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Взносы ИП «за себя» — ДВЕ строки: фиксированные и 1% (вопрос владельца 27.07)."""
    async with async_session_factory() as session:
        session.add(_paid("contrib_fixed", "14347.50", period="q1"))
        await session.commit()

        summary = await _build(session, date(2026, 7, 26))

    fixed = next(row for row in summary.rows if row.kind == "contrib_fixed")
    assert fixed.accrued == CFG.fixed_contribution
    assert fixed.paid == Decimal("14347.50")
    assert fixed.left == CFG.fixed_contribution - Decimal("14347.50")
    extra = next(row for row in summary.rows if row.kind == "contrib_extra_1pct")
    assert extra.title.startswith("Взносы ИП «за себя»")


async def test_left_never_negative_on_overpayment(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Переплата — это не отрицательный долг: «осталось» не уходит ниже нуля."""
    async with async_session_factory() as session:
        session.add(_paid("contrib_fixed", "99999.00", period="q1"))
        await session.commit()

        summary = await _build(session, date(2026, 7, 26))

    fixed = next(row for row in summary.rows if row.kind == "contrib_fixed")
    assert fixed.left == Decimal("0")

"""Обязательства «к уплате» для окна «Активные платежи».

Решение владельца 26.07.2026: окно само показывает всё, что надо платить. Документный
слой — плановые строки из платёжек (не закрытые фактом), расчётный — строки сверки
с ``payable_amount``. Травматизм — виден, но не отправляем контуром ЕНП (реквизиты СФР).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.tax import TaxPayment, TaxPayrollLedger
from app.services.taxes.obligations import list_payable_obligations


def _planned(
    kind: str, amount: str, due: date, period: str | None, recipient: str = "fns"
) -> TaxPayment:
    return TaxPayment(
        id=uuid.uuid4(),
        bundle_id=uuid.uuid4(),
        paid_on=due,
        kind=kind,
        amount=Decimal(amount),
        recipient=recipient,
        for_year=2026,
        for_period=period,
        status="planned",
        source_kind="tax_notice",
        quality_status="confirmed",
    )


def _paid(
    kind: str, amount: str, paid_on: date, period: str | None = None
) -> TaxPayment:
    return TaxPayment(
        id=uuid.uuid4(),
        bundle_id=uuid.uuid4(),
        paid_on=paid_on,
        kind=kind,
        amount=Decimal(amount),
        recipient="fns",
        for_year=2026,
        for_period=period,
        status="paid",
        source_kind="bank_statement",
        quality_status="confirmed",
    )


async def test_planned_included_settled_excluded(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Неоплаченная платёжка — в списке; закрытая фактом того же периода — нет."""
    async with async_session_factory() as session:
        session.add(_planned("usn_advance", "478376", date(2026, 7, 28), "h1"))
        session.add(_planned("usn_advance", "674624", date(2026, 4, 28), "q1"))
        session.add(_paid("usn_advance", "674624", date(2026, 4, 20), "q1"))
        await session.commit()

        obligations = await list_payable_obligations(session, today=date(2026, 7, 15))

    usn = [o for o in obligations if o.kind == "usn_advance"]
    assert [str(o.amount) for o in usn] == ["478376.00"]  # q1 закрыт фактом
    assert usn[0].due_date == date(2026, 7, 28)
    assert usn[0].sendable_via_enp is True


async def test_enp_due_comes_from_reconciliation(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Зарплатный ЕНП не продвигается платёжкой — «к уплате» приходит из сверки."""
    async with async_session_factory() as session:
        session.add(
            TaxPayrollLedger(
                id=uuid.uuid4(),
                year=2026,
                month=6,
                tab_number="206",
                employee="ИВАНОВА И.И.",
                contributions=Decimal("8571.30"),
                ndfl=Decimal("3532.00"),
            )
        )
        await session.commit()

        obligations = await list_payable_obligations(session, today=date(2026, 7, 15))

    enp = [o for o in obligations if o.kind == "enp_payroll"]
    assert len(enp) == 1
    assert enp[0].for_period == "2026-06"
    assert str(enp[0].amount) == "12103.30"  # взносы + НДФЛ июня, срок 28.07
    assert enp[0].sendable_via_enp is True


async def test_injury_is_visible_but_not_sendable_via_enp(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Травматизм платится в СФР по своим реквизитам — строка есть, кнопки ЕНП нет."""
    async with async_session_factory() as session:
        session.add(
            _planned("contrib_injury", "100", date(2026, 8, 15), "2026-07", recipient="sfr")
        )
        await session.commit()

        obligations = await list_payable_obligations(session, today=date(2026, 7, 15))

    injury = [o for o in obligations if o.kind == "contrib_injury"]
    assert len(injury) == 1
    assert injury[0].sendable_via_enp is False

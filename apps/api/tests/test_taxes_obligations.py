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
        # Травматизм уходит в СФР, остальное — в ФНС (CHECK ck_tax_payment_recipient_kind).
        recipient="sfr" if kind == "contrib_injury" else "fns",
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


async def test_injury_obligation_uses_sfr_requisites(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Травматизм — в списке «к уплате», а контур отправки даёт ему реквизиты СФР (не ЕНП)."""
    from app.services.taxes.bank_draft import requisites_for_kind

    async with async_session_factory() as session:
        session.add(
            _planned("contrib_injury", "100", date(2026, 8, 15), "2026-07", recipient="sfr")
        )
        await session.commit()

        obligations = await list_payable_obligations(session, today=date(2026, 7, 15))

    injury = [o for o in obligations if o.kind == "contrib_injury"]
    assert len(injury) == 1
    reqs = requisites_for_kind("contrib_injury")
    assert reqs["kbk"] == "79710212000061000160"  # КБК травматизма, не ЕНП
    assert reqs["taxPayerStatus"] == "08"
    assert "ОСФР" in reqs["recipientName"]
    # Остальные виды по-прежнему уходят ЕНПом в ФНС.
    assert requisites_for_kind("usn_advance")["kbk"] == "18201061201010000510"


async def test_injury_settled_only_by_payment_of_the_same_month(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Январский травматизм НЕ гасит июльскую платёжку: период у всех строк один — 'year'.

    Из-за этого обязательство «травматизм 100 ₽ за июль» молча исчезало из «Активных
    платежей», хотя по банку последний взнос в СФР был 23.06 (расхождение со сводкой
    «Осталось», найдено владельцем 27.07.2026).
    """
    async with async_session_factory() as session:
        # Срок июльского взноса — 15 августа (125-ФЗ, ст. 22).
        session.add(_planned("contrib_injury", "100", date(2026, 8, 15), "2026-07",
                             recipient="sfr"))
        session.add(_paid("contrib_injury", "100", date(2026, 1, 26), "year"))
        session.add(_paid("contrib_injury", "57.14", date(2026, 6, 23), "year"))
        await session.commit()

        obligations = await list_payable_obligations(session, today=date(2026, 7, 27))

    injury = [o for o in obligations if o.kind == "contrib_injury"]
    assert [o.amount for o in injury] == [Decimal("100")]


async def test_injury_settled_by_payment_inside_its_window(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Платёж внутри месяца начисления (до срока 15 числа следующего) обязательство гасит."""
    async with async_session_factory() as session:
        session.add(_planned("contrib_injury", "100", date(2026, 8, 15), "2026-07",
                             recipient="sfr"))
        # Бухгалтер выписывает платёжку в июле, платят её тогда же — задолго до срока.
        session.add(_paid("contrib_injury", "100", date(2026, 7, 27), "year"))
        await session.commit()

        obligations = await list_payable_obligations(session, today=date(2026, 8, 1))

    assert not [o for o in obligations if o.kind == "contrib_injury"]

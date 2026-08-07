"""Остаток денег на произвольную дату — фундамент денежного блока баланса.

До этого расчёта в проекте не было: остаток кошелька считался только «на сейчас», причём
четырьмя разными копиями формулы. Балансу нужен ответ на вопрос «сколько было денег 31 августа»,
и он не должен меняться от того, что случилось в сентябре.
"""

from __future__ import annotations

import sys
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

sys.path.append(str(Path(__file__).parent / "counterparties"))

from cp_helpers import make_account, make_bank_operation, make_wallet  # noqa: E402

from app.models import CashflowTransaction  # noqa: E402
from app.services.wallet_balance_as_of import (  # noqa: E402
    build_money_balance_as_of,
    wallet_balance_as_of,
    wallet_movement_deltas,
)

JULY_END = date(2026, 7, 31)


def _sum_of(money, wallet_ids: set[uuid.UUID]) -> Decimal:
    """Итог по своим кошелькам: в тестовой базе живут ещё и сид-кошельки миграций."""
    return sum((row.balance for row in money.rows if row.wallet_id in wallet_ids), Decimal("0.00"))


async def _bank_wallet(
    session: AsyncSession, *, opening: str = "0.00", opening_date: date | None = None
):
    account = await make_account(session)
    wallet = await make_wallet(
        session, name="Расчётный счёт", wallet_type="bank", account_id=account.id
    )
    wallet.opening_balance = Decimal(opening)
    wallet.opening_balance_date = opening_date
    await session.flush()
    return wallet, account


async def _cash_row(
    session: AsyncSession,
    *,
    wallet_id: uuid.UUID,
    amount: str,
    on: date,
    direction: str = "in",
    quality_status: str = "ok",
) -> CashflowTransaction:
    row = CashflowTransaction(
        wallet_id=wallet_id,
        direction=direction,
        amount=Decimal(amount),
        operation_date=on,
        source_kind="manual",
        quality_status=quality_status,
    )
    session.add(row)
    await session.flush()
    return row


async def test_a_later_operation_does_not_change_an_earlier_balance(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Главное свойство: остаток на 31.07 не двигается от того, что произошло в августе."""
    async with async_session_factory() as session:
        wallet, account = await _bank_wallet(session)
        await make_bank_operation(
            session,
            amount="100000.00",
            direction="in",
            operation_date=date(2026, 7, 20),
            account_id=account.id,
        )
        await make_bank_operation(
            session,
            amount="30000.00",
            direction="out",
            operation_date=date(2026, 8, 5),
            account_id=account.id,
        )
        await session.commit()

        assert await wallet_balance_as_of(session, wallet, as_of=JULY_END) == Decimal("100000.00")
        assert await wallet_balance_as_of(session, wallet, as_of=date(2026, 8, 31)) == Decimal(
            "70000.00"
        )
        # Без даты — «весь учёт», прежнее поведение витрины.
        assert await wallet_balance_as_of(session, wallet) == Decimal("70000.00")


async def test_the_opening_day_itself_is_already_inside_the_opening_balance(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Нижняя граница строгая: движение В ДЕНЬ опорной даты уже свёрнуто во входящий остаток.

    Посчитать его ещё раз значило бы удвоить ровно тот день, ради которого снимок и делался.
    """
    async with async_session_factory() as session:
        wallet, account = await _bank_wallet(
            session, opening="50000.00", opening_date=date(2026, 6, 16)
        )
        await make_bank_operation(
            session,
            amount="7000.00",
            direction="in",
            operation_date=date(2026, 6, 16),
            account_id=account.id,
        )
        await make_bank_operation(
            session,
            amount="3000.00",
            direction="in",
            operation_date=date(2026, 6, 17),
            account_id=account.id,
        )
        await session.commit()

        assert await wallet_balance_as_of(session, wallet, as_of=JULY_END) == Decimal("53000.00")


async def test_bank_balance_follows_the_statement_even_without_any_markup(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Неразнесённая операция двигает остаток так же, как разнесённая.

    Это не мелочь: у заметной части потока деньги несёт prebooked-проводка чужого контура, и
    собственной строки ДДС у операции нет вовсе. Если бы остаток шёл за разметкой, такие деньги
    в балансе просто отсутствовали бы.
    """
    async with async_session_factory() as session:
        wallet, account = await _bank_wallet(session)
        await make_bank_operation(
            session,
            amount="12500.00",
            direction="out",
            operation_date=date(2026, 7, 10),
            account_id=account.id,
            classification_status="needs_review",
        )
        await session.commit()

        assert await wallet_balance_as_of(session, wallet, as_of=JULY_END) == Decimal("-12500.00")


async def test_excluded_operation_leaves_the_balance(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Явно исключённая операция из остатка выпадает — единственное исключение из «за выпиской»."""
    async with async_session_factory() as session:
        wallet, account = await _bank_wallet(session)
        await make_bank_operation(
            session,
            amount="4000.00",
            direction="out",
            operation_date=date(2026, 7, 12),
            account_id=account.id,
            classification_status="excluded",
        )
        await session.commit()

        assert await wallet_balance_as_of(session, wallet, as_of=JULY_END) == Decimal("0.00")


async def test_cash_wallet_counts_transactions_and_skips_the_excluded_one(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """У наличных выписки нет — остаток собирается из проводок, мягко исключённые не в счёт."""
    async with async_session_factory() as session:
        safe = await make_wallet(session, name="Сейф", wallet_type="cash_safe")
        await _cash_row(session, wallet_id=safe.id, amount="20000.00", on=date(2026, 7, 5))
        await _cash_row(
            session, wallet_id=safe.id, amount="5000.00", on=date(2026, 7, 9), direction="out"
        )
        await _cash_row(
            session,
            wallet_id=safe.id,
            amount="9999.00",
            on=date(2026, 7, 9),
            quality_status="excluded",
        )
        await _cash_row(session, wallet_id=safe.id, amount="1000.00", on=date(2026, 8, 2))
        await session.commit()

        assert await wallet_balance_as_of(session, safe, as_of=JULY_END) == Decimal("15000.00")


async def test_total_covers_closed_wallets_but_solvency_does_not(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Баланс считает все кошельки, платёжеспособность — только активные.

    Счёт, закрытый в сентябре, 31 августа деньги держал: в описи он обязан быть. Платить с него
    уже нельзя — поэтому расчёт «хватит ли денег» его исключает. Раньше это была одна формула
    на два разных вопроса.
    """
    async with async_session_factory() as session:
        live = await make_wallet(session, name="Живой сейф", wallet_type="cash_safe")
        closed = await make_wallet(
            session, name="Закрытый сейф", wallet_type="cash_safe", status="closed"
        )
        await _cash_row(session, wallet_id=live.id, amount="10000.00", on=date(2026, 7, 3))
        await _cash_row(session, wallet_id=closed.id, amount="2500.00", on=date(2026, 7, 4))
        await session.commit()

        everything = await build_money_balance_as_of(session, as_of=JULY_END)
        active_only = await build_money_balance_as_of(
            session, as_of=JULY_END, include_inactive=False
        )

        # Сравниваем по СВОИМ кошелькам: в базе живут сид-кошельки миграций, и их остаток
        # к вопросу теста отношения не имеет.
        mine = {live.id, closed.id}
        assert _sum_of(everything, mine) == Decimal("12500.00")
        assert _sum_of(active_only, mine) == Decimal("10000.00")
        assert closed.id not in {row.wallet_id for row in active_only.rows}


async def test_a_date_before_the_opening_point_is_flagged_as_unusable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """До опорной точки движений в системе нет — остаток там не «входящий», а неизвестный."""
    async with async_session_factory() as session:
        wallet, _account = await _bank_wallet(
            session, opening="50000.00", opening_date=date(2026, 6, 16)
        )
        await session.commit()

        early = await build_money_balance_as_of(session, as_of=date(2026, 5, 31))
        late = await build_money_balance_as_of(session, as_of=JULY_END)

        assert next(row for row in early.rows if row.wallet_id == wallet.id).before_opening is True
        assert next(row for row in late.rows if row.wallet_id == wallet.id).before_opening is False


async def test_money_on_an_account_without_a_wallet_is_reported_not_hidden(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Счёт без кошелька — дыра в справочнике: деньги в банке есть, в балансе их нет.

    Молча потерянная сумма уехала бы в капитал и там растворилась, поэтому она считается
    отдельным числом.
    """
    async with async_session_factory() as session:
        before = (await build_money_balance_as_of(session, as_of=JULY_END)).total

        orphan_account = await make_account(session, account_number="40802810900000009999")
        await make_bank_operation(
            session,
            amount="8000.00",
            direction="in",
            operation_date=date(2026, 7, 8),
            account_id=orphan_account.id,
        )
        await session.commit()

        money = await build_money_balance_as_of(session, as_of=JULY_END)
        assert money.unmapped_bank_movement == Decimal("8000.00")
        # И при этом деньги не приписаны никакому кошельку: итог не шелохнулся.
        assert money.total == before


async def test_bank_wallet_without_an_account_is_refused(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Банковский кошелёк без счёта — не ноль, а вопрос без ответа: считать остаток не из чего."""
    async with async_session_factory() as session:
        broken = await make_wallet(session, name="Счёт без реквизитов", wallet_type="bank")
        await session.commit()

        with pytest.raises(ValueError, match="не закреплён счёт"):
            await wallet_balance_as_of(session, broken)


async def test_deltas_without_a_date_match_the_old_behaviour(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``as_of=None`` — «без верхней границы», а не «сегодня».

    Разница видна на проводке, датированной вперёд: «сегодня» её отрежет и тихо сдвинет цифру,
    которую витрина показывает сейчас.
    """
    async with async_session_factory() as session:
        safe = await make_wallet(session, name="Сейф вперёд", wallet_type="cash_safe")
        await _cash_row(session, wallet_id=safe.id, amount="700.00", on=date(2099, 1, 1))
        await session.commit()

        deltas = await wallet_movement_deltas(session)
        assert deltas[safe.id] == Decimal("700.00")
        bounded = await wallet_movement_deltas(session, as_of=JULY_END)
        assert bounded.get(safe.id, Decimal("0")) == Decimal("0")

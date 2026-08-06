"""Замок закрытого месяца держит ВСЕ три двери разметки «месяц расхода».

Проверка перед выкаткой 06.08.2026 воспроизвела три способа молча изменить закрытый месяц —
на 100 000, 77 000 и 55 000 ₽. Общее у всех одно: замок смотрел на значения ПОЛЯ и не смотрел
на месяц ДЕНЕГ, а он и есть тот месяц, который теряет расход.

1. Разбор проводки: пока поле пусто, расход стоит в месяце платежа. Любая разметка вынимает
   его оттуда — и если тот месяц закрыт, закрытый отчёт меняется без единого возражения.
2. Привязка контрагента (``PATCH /dds/transactions/{id}``) снимает разметку как неприменимую,
   то есть возвращает расход в месяц денег. Здесь замка не было вовсе.
3. Исключение проводки вынимает расход отовсюду мгновенно: проверка качества стоит в разборе
   отчёта первой. Замка тоже не было.

Ни один месяц на стенде не закрыт, поэтому весь этот код в живых данных не исполнялся ни разу —
только здесь.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).parent / "counterparties"))

from app.models import AccountingPeriodClose, CashflowTransaction, DdsArticle, PnlArticleRule
from app.services import accounting_periods
from app.services.banking.cashflow_classify import (
    CashflowSplitLine,
    apply_cashflow_exclude,
    apply_cashflow_split,
)

JULY = date(2026, 7, 1)
AUGUST = date(2026, 8, 1)


async def _article(session, *, code: str) -> DdsArticle:
    article = DdsArticle(
        code=code, name=f"Статья {code}", movement_type="outflow", activity_type="operating"
    )
    session.add(article)
    await session.flush()
    session.add(
        PnlArticleRule(
            article_id=article.id,
            line_code="office_maintenance",
            in_pnl=True,
            owner_stream="cash",
            sign=1,
            applies_to="both",
            is_active=True,
        )
    )
    await session.flush()
    return article


async def _close_july(session) -> None:
    session.add(AccountingPeriodClose(period_month=JULY))
    await session.flush()


def _payment(wallet_id, article_id, *, day: date, amount: str, **extra) -> CashflowTransaction:
    return CashflowTransaction(
        wallet_id=wallet_id,
        direction="out",
        amount=Decimal(amount),
        operation_date=day,
        article_id=article_id,
        source_kind="manual_bank_to_safe",
        payment_purpose="Оплата",
        quality_status="manual_override",
        **extra,
    )


def test_split_cannot_take_expense_out_of_a_closed_month(async_session_factory) -> None:
    """Деньги июля, июль закрыт: разметка расхода августом обязана быть отвергнута."""
    from cp_helpers import make_wallet

    async def scenario() -> None:
        async with async_session_factory() as session:
            article = await _article(session, code="test_lock_split")
            wallet = await make_wallet(session, code="lock-w1", name="Сейф")
            txn = _payment(wallet.id, article.id, day=date(2026, 7, 20), amount="100000.00")
            session.add(txn)
            await _close_july(session)
            await session.commit()

            with pytest.raises(accounting_periods.PeriodClosed):
                await apply_cashflow_split(
                    session,
                    txn,
                    splits=[
                        CashflowSplitLine(
                            article_id=article.id,
                            amount=Decimal("100000.00"),
                            expense_month=AUGUST,
                        )
                    ],
                )

    asyncio.run(scenario())


def test_clearing_the_mark_cannot_change_a_closed_month(async_session_factory) -> None:
    """Снятие разметки возвращает расход в месяц денег — это тоже изменение закрытого месяца."""
    from cp_helpers import make_wallet

    async def scenario() -> None:
        async with async_session_factory() as session:
            article = await _article(session, code="test_lock_clear")
            wallet = await make_wallet(session, code="lock-w2", name="Сейф")
            # Деньги августа, расход размечен июлем; июль закрывается ПОСЛЕ разметки.
            txn = _payment(
                wallet.id,
                article.id,
                day=date(2026, 8, 5),
                amount="77000.00",
                expense_month=JULY,
            )
            session.add(txn)
            await _close_july(session)
            await session.commit()

            with pytest.raises(accounting_periods.PeriodClosed):
                await apply_cashflow_split(
                    session,
                    txn,
                    splits=[
                        CashflowSplitLine(article_id=article.id, amount=Decimal("77000.00"))
                    ],
                )

    asyncio.run(scenario())


def test_exclude_cannot_empty_a_closed_month(async_session_factory) -> None:
    """Исключение проводки вынимает расход из месяца — закрытый месяц так не трогают."""
    from cp_helpers import make_wallet

    async def scenario() -> None:
        async with async_session_factory() as session:
            article = await _article(session, code="test_lock_exclude")
            wallet = await make_wallet(session, code="lock-w3", name="Сейф")
            txn = _payment(
                wallet.id,
                article.id,
                day=date(2026, 8, 5),
                amount="55000.00",
                expense_month=JULY,
            )
            session.add(txn)
            await _close_july(session)
            await session.commit()

            with pytest.raises(accounting_periods.PeriodClosed):
                await apply_cashflow_exclude(session, txn)

    asyncio.run(scenario())


def test_second_split_line_cannot_take_expense_out_of_a_closed_month(
    async_session_factory,
) -> None:
    """Месяц уносит ВТОРАЯ доля разбора — замок обязан сработать так же, как на первой.

    Прежняя проверка считала раскладку только для ``index == 0``: первая доля оставалась в
    месяце денег, вторая забирала свою часть в другой месяц, и закрытый месяц худел молча.
    """
    from cp_helpers import make_wallet

    async def scenario() -> None:
        async with async_session_factory() as session:
            article = await _article(session, code="test_lock_second_line")
            wallet = await make_wallet(session, code="lock-w5", name="Сейф")
            txn = _payment(wallet.id, article.id, day=date(2026, 7, 20), amount="100000.00")
            session.add(txn)
            await _close_july(session)
            await session.commit()

            with pytest.raises(accounting_periods.PeriodClosed):
                await apply_cashflow_split(
                    session,
                    txn,
                    splits=[
                        CashflowSplitLine(article_id=article.id, amount=Decimal("40000.00")),
                        CashflowSplitLine(
                            article_id=article.id,
                            amount=Decimal("60000.00"),
                            expense_month=AUGUST,
                        ),
                    ],
                )

    asyncio.run(scenario())


def test_open_months_are_not_blocked(async_session_factory) -> None:
    """Замок держит только закрытое: в открытых месяцах разметка работает как прежде."""
    from cp_helpers import make_wallet

    async def scenario() -> None:
        async with async_session_factory() as session:
            article = await _article(session, code="test_lock_open")
            wallet = await make_wallet(session, code="lock-w4", name="Сейф")
            txn = _payment(wallet.id, article.id, day=date(2026, 7, 20), amount="12000.00")
            session.add(txn)
            await session.commit()

            await apply_cashflow_split(
                session,
                txn,
                splits=[
                    CashflowSplitLine(
                        article_id=article.id, amount=Decimal("12000.00"), expense_month=AUGUST
                    )
                ],
            )
            await session.commit()
            assert txn.expense_month == AUGUST

    asyncio.run(scenario())

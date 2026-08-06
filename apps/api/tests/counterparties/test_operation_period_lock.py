"""Замок закрытого месяца на банковских дверях: исключение, возврат, переразметка.

Ручной разбор проводки замок получил 06.08.2026, а операции банковского фида — нет.
Между тем именно через них проходит основной поток: за июль на стенде это 239 проводок на
3 556 683,75 ₽. Три двери меняли цифры закрытого месяца молча:

* «Исключить» вынимает расход мгновенно — фильтр по качеству стоит в разборе отчёта первым;
* повторная разметка исключённой операции возвращает расход обратно;
* переразметка учтённой операции меняет статью и контрагента, а с ними строку отчёта и весь
  контур ДЗ/КЗ ниже по коду (проверка перед выкаткой воспроизвела это на 88 000 ₽).

Ни один месяц на стенде не закрыт, поэтому в живых данных этот код не исполнялся ни разу —
только здесь.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from cp_helpers import make_account, make_bank_operation, make_expense_article, make_wallet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AccountingPeriodClose, CashflowTransaction
from app.services import accounting_periods
from app.services.banking.classifier import apply_operation_action

JULY = date(2026, 7, 1)
OP_DATE = date(2026, 7, 20)


async def _fixture(session: AsyncSession, *, code: str, amount: str = "88000.00"):
    account = await make_account(session)
    wallet = await make_wallet(
        session, name="Р/с", wallet_type="bank", code=code, account_id=account.id
    )
    article = await make_expense_article(session, code=f"{code}_art", name="Услуги связи")
    operation = await make_bank_operation(
        session,
        amount=amount,
        direction="out",
        account_id=account.id,
        operation_date=OP_DATE,
    )
    return wallet, article, operation


async def _rows(session: AsyncSession, operation_id) -> list[CashflowTransaction]:
    return list(
        (
            await session.scalars(
                select(CashflowTransaction).where(
                    CashflowTransaction.source_kind == "bank_operation",
                    CashflowTransaction.source_id == operation_id,
                )
            )
        ).all()
    )


async def _close_july(session: AsyncSession) -> None:
    session.add(AccountingPeriodClose(period_month=JULY))
    await session.flush()


async def test_exclude_cannot_empty_a_closed_month(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«Исключить» на операции закрытого месяца обязано быть отвергнуто."""
    async with async_session_factory() as session:
        _wallet, article, operation = await _fixture(session, code="lock-op-1")
        await session.commit()
        operation_id = operation.id

        await apply_operation_action(
            session, operation, action="set_article", article_id=article.id
        )
        await session.commit()

        await _close_july(session)
        await session.commit()

        with pytest.raises(accounting_periods.PeriodClosed):
            await apply_operation_action(session, operation, action="exclude")
        await session.rollback()

        rows = await _rows(session, operation_id)
        assert len(rows) == 1
        assert rows[0].quality_status != "excluded", "расход всё-таки ушёл из закрытого месяца"


async def test_reviving_an_excluded_operation_cannot_change_a_closed_month(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Симметрия: возврат исключённой операции добавляет расход — тоже правка закрытых цифр."""
    async with async_session_factory() as session:
        _wallet, article, operation = await _fixture(session, code="lock-op-2")
        await session.commit()
        operation_id = operation.id

        await apply_operation_action(
            session, operation, action="set_article", article_id=article.id
        )
        await session.commit()
        await apply_operation_action(session, operation, action="exclude")
        await session.commit()

        await _close_july(session)
        await session.commit()

        with pytest.raises(accounting_periods.PeriodClosed):
            await apply_operation_action(
                session, operation, action="set_article", article_id=article.id
            )
        await session.rollback()

        rows = await _rows(session, operation_id)
        assert rows[0].quality_status == "excluded", "расход вернулся в закрытый месяц"


async def test_reclassifying_a_counted_operation_cannot_change_a_closed_month(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Переразметка учтённой операции меняет строку отчёта — в закрытом месяце нельзя."""
    async with async_session_factory() as session:
        _wallet, article, operation = await _fixture(session, code="lock-op-3")
        other = await make_expense_article(session, code="lock_op_3_other", name="Аренда")
        await session.commit()
        operation_id, article_id = operation.id, article.id

        await apply_operation_action(
            session, operation, action="set_article", article_id=article.id
        )
        await session.commit()

        await _close_july(session)
        await session.commit()

        with pytest.raises(accounting_periods.PeriodClosed):
            await apply_operation_action(
                session, operation, action="set_article", article_id=other.id
            )
        await session.rollback()

        rows = await _rows(session, operation_id)
        assert rows[0].article_id == article_id, "статья в закрытом месяце всё-таки сменилась"


async def test_open_month_operations_are_not_blocked(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Замок держит только закрытое: в открытом месяце все три двери работают как прежде."""
    async with async_session_factory() as session:
        _wallet, article, operation = await _fixture(
            session, code="lock-op-4", amount="12000.00"
        )
        other = await make_expense_article(session, code="lock_op_4_other", name="Аренда")
        await session.commit()

        await apply_operation_action(
            session, operation, action="set_article", article_id=article.id
        )
        await apply_operation_action(
            session, operation, action="set_article", article_id=other.id
        )
        await apply_operation_action(session, operation, action="exclude")
        await apply_operation_action(
            session, operation, action="set_article", article_id=other.id
        )
        await session.commit()

        rows = await _rows(session, operation.id)
        assert len(rows) == 1
        assert rows[0].article_id == other.id
        assert rows[0].quality_status != "excluded"
        assert rows[0].amount == Decimal("12000.00")

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

import uuid
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


async def test_splitting_a_counted_operation_cannot_change_a_closed_month(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Разбор операции по строкам — основной поток, и замка на нём не было вовсе.

    Проверка перед выкаткой намерила под этой дверью 144 исходящих операции на 2,77 млн ₽ и
    206 входящих на 2,85 млн ₽ за один июль. Первый разбор (проводок ещё нет) не блокируем:
    выписка вправе приезжать задним числом.
    """
    from app.services.banking.classifier import OperationSplitLine, apply_operation_split

    async with async_session_factory() as session:
        _wallet, article, operation = await _fixture(session, code="lock-op-5")
        other = await make_expense_article(session, code="lock_op_5_other", name="Аренда")
        await session.commit()
        operation_id, article_id = operation.id, article.id

        await apply_operation_action(
            session, operation, action="set_article", article_id=article.id
        )
        await session.commit()

        await _close_july(session)
        await session.commit()

        with pytest.raises(accounting_periods.PeriodClosed):
            await apply_operation_split(
                session,
                operation,
                splits=[
                    OperationSplitLine(article_id=other.id, amount=Decimal("88000.00")),
                ],
            )
        await session.rollback()

        rows = await _rows(session, operation_id)
        assert len(rows) == 1
        assert rows[0].article_id == article_id, "разбор всё-таки переписал закрытый месяц"


async def test_first_split_of_a_fresh_operation_is_allowed(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Выписка задним числом в закрытый месяц: разбирать НОВУЮ операцию замок не мешает."""
    from app.services.banking.classifier import OperationSplitLine, apply_operation_split

    async with async_session_factory() as session:
        _wallet, article, operation = await _fixture(session, code="lock-op-6")
        await session.commit()
        operation_id = operation.id

        await _close_july(session)
        await session.commit()

        await apply_operation_split(
            session,
            operation,
            splits=[OperationSplitLine(article_id=article.id, amount=Decimal("88000.00"))],
        )
        await session.commit()

        rows = await _rows(session, operation_id)
        assert len(rows) == 1, "первый разбор новой операции обязан проходить"


async def _prebook(session: AsyncSession, operation, *, wallet, article) -> CashflowTransaction:
    """Деньги операции учтены проводкой ЧУЖОГО контура, своей строки у операции нет.

    Ровно так выглядит треть банковских операций: оплата контрагенту, чек кассы, транзит в
    Сейф. Операция связана с проводкой якорем ``cashflow_transaction_id``, а ``source_kind`` у
    той — доменный, не ``bank_operation``.
    """
    prebooked = CashflowTransaction(
        wallet_id=wallet.id,
        direction="out",
        amount=operation.amount,
        operation_date=operation.operation_date,
        article_id=article.id,
        source_kind="counterparty_payment",
        source_id=uuid.uuid4(),
        payment_purpose="Оплата поставщику",
        quality_status="final",
    )
    session.add(prebooked)
    await session.flush()
    operation.cashflow_transaction_id = prebooked.id
    operation.classification_status = "classified"
    await session.flush()
    return prebooked


async def test_split_is_blocked_when_the_money_sits_on_a_prebooked_row(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Своих строк нет — но операция УЧТЕНА, и переразбор правит закрытый месяц.

    Замок отбирал проводки строго по ``source_kind='bank_operation'``, а у 112 июльских операций
    на 2 063 686,91 ₽ деньги несёт проводка чужого контура. Цикл замка по ним не выполнялся ни
    разу: «нет своих строк» читалось как «операция ещё не учтена», и переразбор проходил молча.
    """
    from app.services.banking.classifier import OperationSplitLine, apply_operation_split

    async with async_session_factory() as session:
        wallet, article, operation = await _fixture(session, code="lock-op-7")
        other = await make_expense_article(session, code="lock_op_7_other", name="Аренда")
        prebooked = await _prebook(session, operation, wallet=wallet, article=article)
        await session.commit()
        operation_id, prebooked_id, article_id = operation.id, prebooked.id, article.id

        await _close_july(session)
        await session.commit()

        with pytest.raises(accounting_periods.PeriodClosed):
            await apply_operation_split(
                session,
                operation,
                splits=[OperationSplitLine(article_id=other.id, amount=Decimal("88000.00"))],
            )
        await session.rollback()

        assert not await _rows(session, operation_id), "разбор всё-таки завёл свои строки"
        survivor = await session.get(CashflowTransaction, prebooked_id)
        assert survivor is not None and survivor.article_id == article_id


async def test_exclude_is_blocked_when_the_money_sits_on_a_prebooked_row(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Та же дыра у «Исключить»: зачёты снимались ДО проверки, а проверять было нечего.

    ``_soft_exclude_operation_cashflow`` выходит на «своих строк нет» раньше своего замка, а
    снятие банк-аллокаций накладных и предоплат идёт ещё выше по коду — то есть до него.
    """
    async with async_session_factory() as session:
        wallet, article, operation = await _fixture(session, code="lock-op-8")
        await _prebook(session, operation, wallet=wallet, article=article)
        await session.commit()
        anchor_id = operation.cashflow_transaction_id

        await _close_july(session)
        await session.commit()

        with pytest.raises(accounting_periods.PeriodClosed):
            await apply_operation_action(session, operation, action="exclude")
        await session.rollback()

        await session.refresh(operation)
        assert operation.cashflow_transaction_id == anchor_id, "якорь оборвали в закрытом месяце"
        assert operation.classification_status != "excluded"


async def test_safe_topup_cannot_erase_a_closed_month(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«Пополнение Сейфа» сносит прежний разбор целиком, а замка у этой двери не было вовсе.

    Новые ноги идут статьёй перевода (``in_pnl=false``), поэтому расход закрытого месяца из
    ОПиУ исчезает, а замена в отчёт не приходит. За июль под дверью 35 проводок на 713 262,42 ₽.
    """
    from app.services.banking.classifier import book_safe_topup

    async with async_session_factory() as session:
        _wallet, article, operation = await _fixture(session, code="lock-op-9")
        await session.commit()
        operation_id, article_id = operation.id, article.id

        await apply_operation_action(
            session, operation, action="set_article", article_id=article.id
        )
        await session.commit()

        await _close_july(session)
        await session.commit()

        with pytest.raises(accounting_periods.PeriodClosed):
            await book_safe_topup(session, operation)
        await session.rollback()

        rows = await _rows(session, operation_id)
        assert len(rows) == 1, "прежний разбор снесли в закрытом месяце"
        assert rows[0].article_id == article_id


async def test_prebooked_operation_is_not_blocked_by_the_lock_in_an_open_month(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Обратная сторона: в ОТКРЫТОМ месяце замок не должен отказывать ни на одной двери.

    ИСТОРИЯ ЭТОГО ТЕСТА. Раньше он проверял, что в открытом месяце проходит и разбор по
    статьям, — и проходил, заводя строку расхода РЯДОМ с живой prebooked-проводкой. Задвоения
    он не замечал по построению: ``_rows`` отбирает только ``source_kind='bank_operation'``,
    то есть чужую строку не видит вовсе. Разбор поверх prebooked теперь отвергается, но
    ЗАМКОМ ЭТО НЕ ЯВЛЯЕТСЯ — отказ приходит обычной ошибкой и по другой причине, что здесь и
    проверяется: месяц открыт, и ``PeriodClosed`` не поднимается.
    """
    from app.services.banking.classifier import (
        OperationSplitLine,
        apply_operation_split,
        book_safe_topup,
    )

    async with async_session_factory() as session:
        wallet, article, operation = await _fixture(session, code="lock-op-10", amount="12000.00")
        other = await make_expense_article(session, code="lock_op_10_other", name="Аренда")
        await _prebook(session, operation, wallet=wallet, article=article)
        await session.commit()
        operation_id = operation.id

        with pytest.raises(ValueError, match="уже проведены другим контуром") as refused:
            await apply_operation_split(
                session,
                operation,
                splits=[OperationSplitLine(article_id=other.id, amount=Decimal("12000.00"))],
            )
        assert not isinstance(refused.value, accounting_periods.PeriodClosed)
        assert not await _rows(session, operation_id), "второй строки расхода появиться не должно"

        await book_safe_topup(session, operation)
        await session.commit()
        assert operation.classification_status == "classified"

        await apply_operation_action(session, operation, action="exclude")
        await session.commit()
        assert operation.classification_status == "excluded"


async def test_open_month_operations_are_not_blocked(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Замок держит только закрытое: в открытом месяце все три двери работают как прежде."""
    async with async_session_factory() as session:
        _wallet, article, operation = await _fixture(session, code="lock-op-4", amount="12000.00")
        other = await make_expense_article(session, code="lock_op_4_other", name="Аренда")
        await session.commit()

        await apply_operation_action(
            session, operation, action="set_article", article_id=article.id
        )
        await apply_operation_action(session, operation, action="set_article", article_id=other.id)
        await apply_operation_action(session, operation, action="exclude")
        await apply_operation_action(session, operation, action="set_article", article_id=other.id)
        await session.commit()

        rows = await _rows(session, operation.id)
        assert len(rows) == 1
        assert rows[0].article_id == other.id
        assert rows[0].quality_status != "excluded"
        assert rows[0].amount == Decimal("12000.00")

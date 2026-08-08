"""Сверка приращения капитала — единственная непустая проверка модуля «Баланс».

ПОЧЕМУ ЕЁ НАДО ЗАЩИЩАТЬ ТЕСТАМИ ОТДЕЛЬНО. Капитал выводится как «актив минус обязательства»,
поэтому равенство «актив = пассив» выполняется по построению — при любых ошибках в данных.
Настоящая проверка появляется со второго снимка: приращение капитала считается из балансов, а
ожидаемое приращение — из прибыли и движений по статьям капитала. Пути не пересекаются, и
расхождение между ними означает дыру.

Отсюда два свойства, которые ломаются незаметно и потому проверяются здесь по отдельности:
ожидаемая величина не имеет права читать поля снимка, а полоса допуска — это ИЗМЕНЕНИЕ
неточности между снимками, а не её объём.
"""

from __future__ import annotations

import sys
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

sys.path.append(str(Path(__file__).parent / "counterparties"))

from cp_helpers import make_wallet  # noqa: E402

from app.models import BalanceSnapshot, CashflowTransaction, DdsArticle  # noqa: E402
from app.services.balance.reconcile import (  # noqa: E402
    equity_movements,
    explain,
    reconcile,
)

JULY_END = date(2026, 7, 31)
AUGUST_END = date(2026, 8, 31)


def _previous(equity: str, as_of: date = JULY_END) -> BalanceSnapshot:
    """Предыдущий снимок как ЗНАЧЕНИЕ: сверка читает у него только капитал и дату."""
    return BalanceSnapshot(
        as_of=as_of,
        period_month=as_of.replace(day=1),
        status="final",
        method="inventory",
        asset_total=Decimal(equity),
        liability_total=Decimal("0.00"),
        equity_plug=Decimal(equity),
    )


async def _article(session: AsyncSession, code: str, movement: str) -> DdsArticle:
    existing = await session.scalar(select(DdsArticle).where(DdsArticle.code == code))
    if existing is not None:
        return existing
    article = DdsArticle(code=code, name=code, movement_type=movement, activity_type="financing")
    session.add(article)
    await session.flush()
    return article


async def _movement(
    session: AsyncSession,
    *,
    code: str,
    amount: str,
    on: date,
    direction: str = "out",
    quality_status: str = "ok",
) -> None:
    article = await _article(session, code, "outflow" if direction == "out" else "inflow")
    wallet = await make_wallet(session, name=f"Счёт {uuid.uuid4().hex[:6]}")
    session.add(
        CashflowTransaction(
            wallet_id=wallet.id,
            article_id=article.id,
            direction=direction,
            amount=Decimal(amount),
            operation_date=on,
            source_kind="manual",
            quality_status=quality_status,
        )
    )
    await session.flush()


async def test_dividends_explain_a_fall_of_capital(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Капитал упал ровно на выплаченные дивиденды — это не дыра, а изъятие собственника."""
    async with async_session_factory() as session:
        await _movement(session, code="dividendy", amount="100000.00", on=date(2026, 8, 10))
        await session.commit()

        result = await reconcile(
            session,
            previous=_previous("1000000.00"),
            current_equity=Decimal("900000.00"),
            current_as_of=AUGUST_END,
            current_approximate=Decimal("0.00"),
            previous_approximate=Decimal("0.00"),
            net_profit=Decimal("0.00"),
            net_profit_status="ok",
        )

        assert result.movements.dividends == Decimal("100000.00")
        assert result.expected == Decimal("-100000.00")
        assert result.residual == Decimal("0.00")
        assert result.explained is True


async def test_capital_growing_without_profit_is_an_unexplained_hole(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Капитал вырос, а прибыли и вкладов под ним нет — ровно то, ради чего сверка существует.

    Такой рост означает одно из двух: в прибыль не попал расход или в балансе есть движение
    мимо прибыли. Обе беды невидимы в самом балансе, потому что он сходится всегда.
    """
    async with async_session_factory() as session:
        result = await reconcile(
            session,
            previous=_previous("1000000.00"),
            current_equity=Decimal("1050000.00"),
            current_as_of=AUGUST_END,
            current_approximate=Decimal("0.00"),
            previous_approximate=Decimal("0.00"),
            net_profit=Decimal("0.00"),
            net_profit_status="ok",
        )

        assert result.residual == Decimal("50000.00")
        assert result.explained is False
        assert "не попал расход" in explain(result)


async def test_an_unknown_profit_is_not_an_agreement(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Нет прибыли — нет и ответа: это не «сошлось».

    Соблазн подставить ноль велик, и он опаснее прочих: расхождение обнулилось бы вместе с
    прибылью, и месяц с неполным отчётом выглядел бы самым благополучным.
    """
    async with async_session_factory() as session:
        result = await reconcile(
            session,
            previous=_previous("1000000.00"),
            current_equity=Decimal("1000000.00"),
            current_as_of=AUGUST_END,
            current_approximate=Decimal("0.00"),
            previous_approximate=Decimal("0.00"),
            net_profit=None,
            net_profit_status="недоступен",
        )

        assert result.residual == Decimal("0.00")
        assert result.within_tolerance is True
        # Ноль расхождения при неизвестной прибыли — это отсутствие ответа, а не согласие.
        assert result.explained is False
        assert "неизвестна" in explain(result)


async def test_the_tolerance_is_the_change_of_approximation_not_its_volume(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Полоса допуска отвечает «насколько могла сдвинуться датировка ЗА ЭТОТ МЕСЯЦ».

    Возьми она объём приблизительных величин — получилось бы «чем хуже данные, тем шире
    сошлось»: один бартерный зачёт на 400 000 ₽ давал бы право на расхождение в 400 000 ₽.
    """
    async with async_session_factory() as session:
        unchanged = await reconcile(
            session,
            previous=_previous("1000000.00"),
            current_equity=Decimal("1000000.00"),
            current_as_of=AUGUST_END,
            current_approximate=Decimal("400000.00"),
            previous_approximate=Decimal("400000.00"),
            net_profit=Decimal("0.00"),
            net_profit_status="ok",
        )
        # Неточного много, но за месяц оно не сдвинулось — допуск только на округление.
        assert unchanged.tolerance == Decimal("1.00")

        grown = await reconcile(
            session,
            previous=_previous("1000000.00"),
            current_equity=Decimal("1000000.00"),
            current_as_of=AUGUST_END,
            current_approximate=Decimal("405000.00"),
            previous_approximate=Decimal("400000.00"),
            net_profit=Decimal("0.00"),
            net_profit_status="ok",
        )
        assert grown.tolerance == Decimal("5001.00")


async def test_the_expected_side_ignores_everything_stored_in_the_snapshot(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ожидаемое приращение считается мимо снимка — иначе сверка стала бы нулём навсегда.

    Проверяется прямо: у предыдущего снимка переписываются ВСЕ поля, кроме капитала и даты —
    прибыль, дивиденды, вклады, прошлое расхождение. Ожидаемая величина не должна шелохнуться.
    Стоит однажды взять прибыль из шапки «для консистентности» — и этот тест покраснеет.
    """
    async with async_session_factory() as session:
        await _movement(session, code="dividendy", amount="100000.00", on=date(2026, 8, 10))
        await session.commit()

        clean = _previous("1000000.00")
        poisoned = _previous("1000000.00")
        poisoned.net_profit = Decimal("777777.00")
        poisoned.equity_dividends = Decimal("555555.00")
        poisoned.equity_contributions = Decimal("444444.00")
        poisoned.equity_returns = Decimal("333333.00")
        poisoned.recon_expected = Decimal("222222.00")
        poisoned.recon_residual = Decimal("111111.00")

        args = dict(
            current_equity=Decimal("900000.00"),
            current_as_of=AUGUST_END,
            current_approximate=Decimal("0.00"),
            previous_approximate=Decimal("0.00"),
            net_profit=Decimal("0.00"),
            net_profit_status="ok",
        )
        assert (
            (await reconcile(session, previous=clean, **args)).expected
            == (await reconcile(session, previous=poisoned, **args)).expected
            == Decimal("-100000.00")
        )


async def test_movements_belong_to_the_month_they_happened_in(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Границы периода несимметричны, и это не придирка.

    Нижняя строгая, верхняя нет: движение В ДЕНЬ предыдущего среза уже учтено в его капитале, а
    движение в день текущего — ещё нет. Симметричные границы задвоили бы один день или потеряли
    другой, причём ровно на стыке месяцев, где такие ошибки и живут.
    """
    async with async_session_factory() as session:
        await _movement(session, code="dividendy", amount="10000.00", on=JULY_END)
        await _movement(session, code="dividendy", amount="20000.00", on=date(2026, 8, 1))
        await _movement(session, code="dividendy", amount="30000.00", on=AUGUST_END)
        await _movement(session, code="dividendy", amount="40000.00", on=date(2026, 9, 1))
        await session.commit()

        movements = await equity_movements(session, date_from=JULY_END, date_to=AUGUST_END)
        assert movements.dividends == Decimal("50000.00")


async def test_an_excluded_transaction_is_not_a_movement_of_capital(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Исключённая проводка не двигает деньги, значит и капитал ею не объясняется.

    Считай её — и снятая с учёта операция «объяснила» бы расхождение, которого на самом деле
    никто не объяснял.
    """
    async with async_session_factory() as session:
        await _movement(
            session,
            code="dividendy",
            amount="90000.00",
            on=date(2026, 8, 10),
            quality_status="excluded",
        )
        await session.commit()

        movements = await equity_movements(session, date_from=JULY_END, date_to=AUGUST_END)
        assert movements.dividends == Decimal("0.00")


async def test_contributions_and_returns_pull_capital_in_opposite_directions(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Внесённое собственником капитал поднимает, возвращённое — опускает.

    Перепутанный знак здесь не падает и не кричит: он просто удваивает расхождение, и владелец
    ищет дыру вдвое больше настоящей.
    """
    async with async_session_factory() as session:
        await _movement(
            session,
            code="postuplenie_deneg_ot_sobstvennikov",
            amount="200000.00",
            on=date(2026, 8, 4),
            direction="in",
        )
        await _movement(
            session, code="vozvrat_deneg_sobstvennikam", amount="50000.00", on=date(2026, 8, 20)
        )
        await session.commit()

        result = await reconcile(
            session,
            previous=_previous("1000000.00"),
            current_equity=Decimal("1150000.00"),
            current_as_of=AUGUST_END,
            current_approximate=Decimal("0.00"),
            previous_approximate=Decimal("0.00"),
            net_profit=Decimal("0.00"),
            net_profit_status="ok",
        )

        assert result.movements.contributions == Decimal("200000.00")
        assert result.movements.returns == Decimal("50000.00")
        assert result.expected == Decimal("150000.00")
        assert result.residual == Decimal("0.00")
        assert result.explained is True

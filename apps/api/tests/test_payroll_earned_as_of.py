"""Заработанное сотрудником НА ДАТУ — исторический срез, а не «доступно к авансу сейчас».

Дефект, ради которого это написано, не задваивал деньги, а ТЕРЯЛ их, и потому был невидим.
Витрина долгов перед сотрудниками обнуляет заработок периода, если тот финализирован, —
сравнивая границы периода со множеством ВСЕХ финализированных периодов, без даты финализации.
На историческую дату это означает: неделя 25–31.08, закрытая 3 сентября, на срезе 31.08
обнулялась как «уже в ведомости», а хвост ведомости её не подхватывал — он появляется только
3 сентября. Зарплата за неделю исчезала из среза целиком.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import PayrollPeriod
from app.services.payroll_advance_availability import _finalized_by, _period_containing

WEEK_START = date(2026, 8, 25)
WEEK_END = date(2026, 8, 31)


def _week(status: str, finalized_at: datetime | None) -> PayrollPeriod:
    return PayrollPeriod(
        period_type="week",
        start_date=WEEK_START,
        end_date=WEEK_END,
        payroll_date=date(2026, 9, 2),
        status=status,
        finalized_at=finalized_at,
    )


def test_period_closed_later_is_open_on_the_snapshot_date() -> None:
    """Главный случай: закрыли 3 сентября — значит 31 августа период был ОТКРЫТ.

    Смотреть надо на дату финализации, а не на статус: статус отвечает «как сейчас».
    """
    period = _week("finalized", datetime(2026, 9, 3, 12, 0, tzinfo=UTC))

    assert _finalized_by(period, date(2026, 8, 31)) is False
    assert _finalized_by(period, date(2026, 9, 3)) is True
    assert _finalized_by(period, date(2026, 9, 30)) is True


def test_open_period_is_never_finalized_on_any_date() -> None:
    assert _finalized_by(_week("open", None), date(2026, 12, 31)) is False
    assert _finalized_by(None, date(2026, 8, 31)) is False


async def test_containing_period_is_found_even_after_it_was_closed(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Резолвер исторического периода не фильтрует по статусу — и в этом всё дело.

    Тот, что обслуживает выдачу аванса, ищет ОТКРЫТЫЙ период: для 31.08 он вернул бы уже
    следующую неделю и заработанное не за тот период.
    """
    async with async_session_factory() as session:
        closed = _week("finalized", datetime(2026, 9, 3, 12, 0, tzinfo=UTC))
        session.add(closed)
        session.add(
            PayrollPeriod(
                period_type="week",
                start_date=date(2026, 9, 1),
                end_date=date(2026, 9, 7),
                payroll_date=date(2026, 9, 9),
                status="open",
            )
        )
        await session.commit()

        found = await _period_containing(session, date(2026, 8, 31))
        assert found is not None
        assert (found.start_date, found.end_date) == (WEEK_START, WEEK_END)
        assert found.status == "finalized", "статус не должен влиять на выбор периода"

        # Дата внутри следующей недели по-прежнему попадает в свою.
        september = await _period_containing(session, date(2026, 9, 3))
        assert september is not None
        assert september.start_date == date(2026, 9, 1)

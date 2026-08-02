"""Признанный расход по месяцам и статьям — мост из начислений в P&L.

До этого отчёта у начислений не было НИ ОДНОГО потребителя, кроме витрины, которая их же и
заводит: признание было замкнуто само на себя, и ошибка признания (двойной расход, пропавший
месяц) не проявлялась нигде — деньги при этом сходились всегда. Аудит 02.08.2026 нашёл десяток
таких мест только потому, что искал глазами по коду.

Здесь закреплены два свойства, без которых отчёт врал бы: длинный период раскладывается по
календарным месяцам, а расход без статьи виден отдельной цифрой, а не растворяется в итоге.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from cp_helpers import make_counterparty, make_expense_article, make_invoice
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import SupplierExpenseAccrual
from app.services.expense_recognition_report import build_expense_report, spread_over_months


def test_long_period_is_spread_over_calendar_months() -> None:
    """Квартальный документ даёт три месяца по трети, а не один месяц целиком.

    Начисление хранит ОДНУ дату признания, и для акта за июль-сентябрь это сентябрь: 36 000 ₽
    падали в один месяц, а июль и август стояли пустыми. Услуга оказывалась все три.
    """
    assert spread_over_months(Decimal("36000"), date(2026, 7, 1), date(2026, 9, 30)) == [
        (date(2026, 7, 1), Decimal("12000.00")),
        (date(2026, 8, 1), Decimal("12000.00")),
        (date(2026, 9, 1), Decimal("12000.00")),
    ]
    # Остаток от округления достаётся последнему месяцу — как у самоактов и договора услуги.
    assert spread_over_months(Decimal("10000"), date(2026, 4, 1), date(2026, 6, 30)) == [
        (date(2026, 4, 1), Decimal("3333.33")),
        (date(2026, 5, 1), Decimal("3333.33")),
        (date(2026, 6, 1), Decimal("3333.34")),
    ]
    # Период внутри одного месяца целиком в него и попадает.
    assert spread_over_months(Decimal("500"), date(2026, 7, 10), date(2026, 7, 20)) == [
        (date(2026, 7, 1), Decimal("500.00"))
    ]


async def _recognized(
    session: AsyncSession,
    *,
    counterparty_id,
    article_id,
    amount: str,
    start: date,
    end: date,
) -> SupplierExpenseAccrual:
    invoice = await make_invoice(
        session,
        counterparty_id=counterparty_id,
        amount=amount,
        doc_kind="closing",
        invoice_date=end,
    )
    accrual = SupplierExpenseAccrual(
        counterparty_id=counterparty_id,
        invoice_id=invoice.id,
        article_id=article_id,
        amount=Decimal(amount),
        service_period_start=start,
        service_period_end=end,
        status="recognized",
        recognition_month=end.replace(day=1),
    )
    session.add(accrual)
    await session.flush()
    return accrual


async def test_report_splits_quarter_across_months_and_keeps_articles_apart(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Расход раскладывается по месяцам и не смешивает статьи."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Отчёт Квартал", inn="6155000700")
        licence = await make_expense_article(session, code="REP-LIC", name="Лицензии")
        rent = await make_expense_article(session, code="REP-RENT", name="Аренда")
        await _recognized(
            session,
            counterparty_id=cp.id,
            article_id=licence.id,
            amount="36000.00",
            start=date(2026, 7, 1),
            end=date(2026, 9, 30),
        )
        await _recognized(
            session,
            counterparty_id=cp.id,
            article_id=rent.id,
            amount="50000.00",
            start=date(2026, 8, 1),
            end=date(2026, 8, 31),
        )
        await session.commit()

        report = await build_expense_report(
            session, date_from=date(2026, 7, 1), date_to=date(2026, 9, 30)
        )

        assert report.months == [date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1)]
        by_key = {(cell.month, cell.article_name): cell.amount for cell in report.cells}
        assert by_key[(date(2026, 7, 1), "Лицензии")] == Decimal("12000.00")
        assert by_key[(date(2026, 8, 1), "Лицензии")] == Decimal("12000.00")
        assert by_key[(date(2026, 9, 1), "Лицензии")] == Decimal("12000.00")
        # Аренда стоит только в своём месяце и с лицензиями не смешалась.
        assert by_key[(date(2026, 8, 1), "Аренда")] == Decimal("50000.00")
        assert report.total == Decimal("86000.00")


async def test_report_clips_to_the_requested_window(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Документ, начавшийся до окна, отдаёт только месяцы внутри него."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Отчёт Окно", inn="6155000701")
        article = await make_expense_article(session, code="REP-WIN", name="Услуги")
        await _recognized(
            session,
            counterparty_id=cp.id,
            article_id=article.id,
            amount="36000.00",
            start=date(2026, 7, 1),
            end=date(2026, 9, 30),
        )
        await session.commit()

        report = await build_expense_report(
            session, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30)
        )
        assert report.total == Decimal("12000.00"), "в окно попал чужой месяц"


async def test_expense_without_article_is_reported_separately(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Расход без статьи виден отдельной цифрой, а не растворяется в итоге.

    В P&L его отнести некуда: пока ``unattributed`` не ноль, отчёт о прибыли неполон ровно
    на эту сумму, и человек обязан это видеть.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Отчёт Без статьи", inn="6155000702")
        await _recognized(
            session,
            counterparty_id=cp.id,
            article_id=None,
            amount="7000.00",
            start=date(2026, 7, 1),
            end=date(2026, 7, 31),
        )
        await session.commit()

        report = await build_expense_report(
            session, date_from=date(2026, 7, 1), date_to=date(2026, 7, 31)
        )
        assert report.unattributed == Decimal("7000.00")
        assert report.total == Decimal("7000.00")
        assert [cell.article_name for cell in report.cells] == ["Без статьи"]


async def test_cancelled_and_scheduled_accruals_are_not_expense_yet(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """В расход идёт только признанное: ``scheduled`` ещё не расход, ``cancelled`` уже не."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Отчёт Статусы", inn="6155000703")
        article = await make_expense_article(session, code="REP-ST", name="Услуги")
        recognized = await _recognized(
            session,
            counterparty_id=cp.id,
            article_id=article.id,
            amount="1000.00",
            start=date(2026, 7, 1),
            end=date(2026, 7, 31),
        )
        scheduled = await _recognized(
            session,
            counterparty_id=cp.id,
            article_id=article.id,
            amount="2000.00",
            start=date(2026, 7, 1),
            end=date(2026, 7, 31),
        )
        scheduled.status = "scheduled"
        cancelled = await _recognized(
            session,
            counterparty_id=cp.id,
            article_id=article.id,
            amount="4000.00",
            start=date(2026, 7, 1),
            end=date(2026, 7, 31),
        )
        cancelled.status = "cancelled"
        await session.commit()
        assert recognized.status == "recognized"

        report = await build_expense_report(
            session, date_from=date(2026, 7, 1), date_to=date(2026, 7, 31)
        )
        assert report.total == Decimal("1000.00")


async def test_self_billed_expense_is_flagged_as_without_primary(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Расход без первички считается отдельно от расхода по документу контрагента.

    Самоакт мы выписали себе сами — в управленческом P&L он полноправен (деньги ушли, услуга
    оказана), но в налоговую базу УСН идти не может: инспекция снимет расход, у которого нет
    документа поставщика. Две цифры отвечают на разные вопросы, и складывать их нельзя.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Отчёт Первичка", inn="6155000704")
        article = await make_expense_article(session, code="REP-PRIM", name="Услуги")
        # Настоящий документ контрагента.
        await _recognized(
            session,
            counterparty_id=cp.id,
            article_id=article.id,
            amount="5000.00",
            start=date(2026, 7, 1),
            end=date(2026, 7, 31),
        )
        # Самоакт: первички нет.
        self_billed = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="3000.00",
            doc_kind="closing",
            source="self_billed",
            external_id="self:report-test:2026-07",
            invoice_date=date(2026, 7, 31),
        )
        session.add(
            SupplierExpenseAccrual(
                counterparty_id=cp.id,
                invoice_id=self_billed.id,
                article_id=article.id,
                amount=Decimal("3000.00"),
                service_period_start=date(2026, 7, 1),
                service_period_end=date(2026, 7, 31),
                status="recognized",
                recognition_month=date(2026, 7, 1),
            )
        )
        await session.commit()

        report = await build_expense_report(
            session, date_from=date(2026, 7, 1), date_to=date(2026, 7, 31)
        )
        assert report.total == Decimal("8000.00")
        assert report.without_primary == Decimal("3000.00")


async def test_location_slices_expense_and_missing_location_is_visible(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Разрез по помещению работает, а расход без помещения виден отдельной цифрой.

    Отчёт отвечал на вопрос «сколько потратили», но не на «сколько стоит эта точка» — а он и
    есть первый вопрос к P&L, когда точек больше одной. У документа поставщика помещения нет
    вовсе, и подставлять его наугад нельзя: выдуманная цифра выглядит достоверно и расходится
    с реальностью молча.
    """
    from sqlalchemy import select

    from app.models import Location, Organization

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Отчёт Точки", inn="6155000705")
        article = await make_expense_article(session, code="REP-LOC", name="Аренда")
        organization_id = await session.scalar(select(Organization.id).limit(1))
        assert organization_id is not None, "в сидах должна быть организация"
        location = Location(organization_id=organization_id, name="Черникова (отчёт)")
        session.add(location)
        await session.flush()

        with_location = await _recognized(
            session,
            counterparty_id=cp.id,
            article_id=article.id,
            amount="50000.00",
            start=date(2026, 7, 1),
            end=date(2026, 7, 31),
        )
        with_location.location_id = location.id
        # Второй расход — без помещения (документ поставщика его не несёт).
        await _recognized(
            session,
            counterparty_id=cp.id,
            article_id=article.id,
            amount="8000.00",
            start=date(2026, 7, 1),
            end=date(2026, 7, 31),
        )
        await session.commit()

        full = await build_expense_report(
            session, date_from=date(2026, 7, 1), date_to=date(2026, 7, 31)
        )
        assert full.total == Decimal("58000.00")
        assert full.without_location == Decimal("8000.00")

        by_point = await build_expense_report(
            session,
            date_from=date(2026, 7, 1),
            date_to=date(2026, 7, 31),
            location_id=location.id,
        )
        assert by_point.total == Decimal("50000.00"), "в срез точки попал расход без помещения"

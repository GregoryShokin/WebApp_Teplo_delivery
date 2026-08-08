"""Долг партнёра: взято наличными − его комиссия − возвращено.

ТРИ ИСТОЧНИКА, И КАЖДЫЙ ЛОМАЕТСЯ ПО-СВОЕМУ. Взятое — журнал смен, комиссия — регистр партнёрских
комиссий ОПиУ, возвращённое — проводки с контрагентом. Здесь закреплены не столько сами суммы,
сколько границы: с какой даты считаем, что делать с незавершённым месяцем и почему гашение нельзя
искать по статье.
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

sys.path.append(str(Path(__file__).parent / "counterparties"))

from cp_helpers import make_counterparty, make_expense_article, make_wallet  # noqa: E402

from app.models import (  # noqa: E402
    AppSetting,
    CashflowTransaction,
    IikoCashShift,
    IikoCashShiftPayout,
    PnlPartnerCommissionFact,
    PnlPartnerCommissionRule,
)
from app.services.partner_cash_balance import (  # noqa: E402
    PARTNER_COMMISSION_KEY,
    PARTNER_COUNTERPARTY_SETTING,
    build_partner_cash_balance,
)

JULY_END = date(2026, 7, 31)
AUGUST_END = date(2026, 8, 31)


async def _shift_payout(
    session: AsyncSession, *, amount: str, closed_on: datetime, category: str = "alisa"
) -> None:
    shift = IikoCashShift(
        iiko_session_id=str(uuid.uuid4()),
        open_date=closed_on,
        close_date=closed_on,
        raw={},
    )
    session.add(shift)
    await session.flush()
    session.add(
        IikoCashShiftPayout(
            shift_id=shift.id,
            account_id_iiko=str(uuid.uuid4()),
            category=category,
            amount=Decimal(amount),
        )
    )
    await session.flush()


async def _commission(session: AsyncSession, *, month: date, revenue: str) -> None:
    """Факт комиссии за месяц. Ставка 20 % — как в прод-правиле партнёра."""
    rule = await session.scalar(
        select(PnlPartnerCommissionRule).where(
            PnlPartnerCommissionRule.partner_key == PARTNER_COMMISSION_KEY
        )
    )
    if rule is None:
        rule = PnlPartnerCommissionRule(
            partner_key=PARTNER_COMMISSION_KEY,
            partner_name="В гостях у Алисы",
            commission_rate=Decimal("0.200000"),
            source_preset_id="test-preset",
        )
        session.add(rule)
        await session.flush()
    amount = (Decimal(revenue) * Decimal("0.2")).quantize(Decimal("0.01"))
    session.add(
        PnlPartnerCommissionFact(
            period_month=month,
            rule_id=rule.id,
            partner_key=PARTNER_COMMISSION_KEY,
            partner_name="В гостях у Алисы",
            revenue_amount=Decimal(revenue),
            commission_rate=Decimal("0.200000"),
            commission_amount=amount,
            source_ref="test",
        )
    )
    await session.flush()


async def _declare_partner(session: AsyncSession, counterparty_id: uuid.UUID | None) -> None:
    await session.execute(
        update(AppSetting)
        .where(AppSetting.key == PARTNER_COUNTERPARTY_SETTING)
        .values(value=str(counterparty_id) if counterparty_id else "")
    )
    await session.flush()


async def _repayment(
    session: AsyncSession, *, counterparty_id: uuid.UUID, amount: str, on: date
) -> None:
    wallet = await make_wallet(session, name="ТК Черникова", wallet_type="store_cash")
    article = await make_expense_article(
        session, code="postuplenie_deneg_s_torg_tochek", name="Поступление с торг. точек"
    )
    session.add(
        CashflowTransaction(
            wallet_id=wallet.id,
            counterparty_id=counterparty_id,
            article_id=article.id,
            direction="in",
            amount=Decimal(amount),
            operation_date=on,
            source_kind="kassa_payin",
            quality_status="final",
        )
    )
    await session.flush()


async def test_the_debt_is_cash_taken_minus_commission_minus_what_came_back(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Цифры прод-июля целиком: 124 644,00 − 40 890,60 − 77 238,00 = 6 515,40.

    Комиссия — не наша арифметика «выручка × 20 %», а готовый факт регистра ОПиУ: там та же
    величина уже сидит в расходах, и вторая формула однажды разошлась бы с первой.
    """
    async with async_session_factory() as session:
        partner = await make_counterparty(session, name="В гостях у Алисы")
        await _declare_partner(session, partner.id)

        await _shift_payout(
            session, amount="124644.00", closed_on=datetime(2026, 7, 15, 23, tzinfo=UTC)
        )
        await _commission(session, month=date(2026, 7, 1), revenue="204453.00")
        await _repayment(
            session, counterparty_id=partner.id, amount="77238.00", on=date(2026, 7, 7)
        )
        await session.commit()

        balance = await build_partner_cash_balance(session, as_of=JULY_END)
        assert balance.taken == Decimal("124644.00")
        assert balance.commission == Decimal("40890.60")
        assert balance.returned == Decimal("77238.00")
        assert balance.outstanding == Decimal("6515.40")
        assert balance.complete is True


async def test_cash_taken_before_the_start_of_accounting_is_not_a_debt(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Июньские изъятия в первый баланс не входят — решение владельца 08.08.2026.

    Они относятся к периоду, которого в учёте нет вовсе; втянуть их значило бы смешать две
    эпохи в одной цифре и объяснять потом разницу с ОПиУ, который начинается 01.07.
    """
    async with async_session_factory() as session:
        partner = await make_counterparty(session, name="В гостях у Алисы")
        await _declare_partner(session, partner.id)

        await _shift_payout(
            session, amount="45947.00", closed_on=datetime(2026, 6, 20, 23, tzinfo=UTC)
        )
        await _shift_payout(
            session, amount="10000.00", closed_on=datetime(2026, 7, 5, 23, tzinfo=UTC)
        )
        await _commission(session, month=date(2026, 7, 1), revenue="0.00")
        await session.commit()

        balance = await build_partner_cash_balance(session, as_of=JULY_END)
        assert balance.taken == Decimal("10000.00")


async def test_inside_an_open_month_the_commission_is_not_deducted_and_it_is_said_aloud(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Выручка партнёра приходит помесячно — внутри месяца вычитать нечего.

    Размазать месячную выручку по дням было бы выдумкой: выручка по дням неровная. Поэтому на
    срезе 15.08 комиссия августа не вычитается, долг завышен на неё, и строка это признаёт.
    """
    async with async_session_factory() as session:
        partner = await make_counterparty(session, name="В гостях у Алисы")
        await _declare_partner(session, partner.id)

        await _commission(session, month=date(2026, 7, 1), revenue="204453.00")
        await _shift_payout(
            session, amount="124644.00", closed_on=datetime(2026, 7, 15, 23, tzinfo=UTC)
        )
        await _shift_payout(
            session, amount="17113.00", closed_on=datetime(2026, 8, 6, 23, tzinfo=UTC)
        )
        await session.commit()

        mid_august = await build_partner_cash_balance(session, as_of=date(2026, 8, 15))
        assert mid_august.taken == Decimal("141757.00")
        # Июль закрыт — его комиссия вычтена; август ещё идёт — его комиссии нет.
        assert mid_august.commission == Decimal("40890.60")
        assert mid_august.open_month is True
        assert mid_august.complete is False


async def test_a_finished_month_without_commission_is_reported_not_silently_skipped(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Месяц кончился, партнёр наличные брал, а комиссии нет — долг завышен на неизвестное.

    Пропустить молча значило бы показать завышенную цифру со статусом «точно».
    """
    async with async_session_factory() as session:
        partner = await make_counterparty(session, name="В гостях у Алисы")
        await _declare_partner(session, partner.id)

        await _shift_payout(
            session, amount="50000.00", closed_on=datetime(2026, 7, 5, 23, tzinfo=UTC)
        )
        await session.commit()

        balance = await build_partner_cash_balance(session, as_of=JULY_END)
        assert balance.months_without_commission == [date(2026, 7, 1)]
        assert balance.commission == Decimal("0.00")
        assert balance.outstanding == Decimal("50000.00")
        assert balance.complete is False


async def test_ordinary_collections_under_the_same_article_do_not_repay_the_partner(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Инкассация с точки идёт той же статьёй — и долг партнёра она не гасит.

    Правило «минус всё по статье» выглядит правдоподобно ровно до первого дня, когда выручку
    сдали в кассу: на копии прода такой проводкой ушло бы в вычет несколько миллионов, и долг
    партнёра обнулился бы навсегда. Гашение ищем строго по контрагенту.
    """
    async with async_session_factory() as session:
        partner = await make_counterparty(session, name="В гостях у Алисы")
        wallet = await make_wallet(session, name="ТК Черникова", wallet_type="store_cash")
        article = await make_expense_article(
            session, code="postuplenie_deneg_s_torg_tochek", name="Поступление с торг. точек"
        )
        await _declare_partner(session, partner.id)

        await _shift_payout(
            session, amount="50000.00", closed_on=datetime(2026, 7, 5, 23, tzinfo=UTC)
        )
        await _commission(session, month=date(2026, 7, 1), revenue="0.00")
        # Обычная инкассация: та же статья, тот же кошелёк, БЕЗ контрагента.
        session.add(
            CashflowTransaction(
                wallet_id=wallet.id,
                article_id=article.id,
                direction="in",
                amount=Decimal("900000.00"),
                operation_date=date(2026, 7, 6),
                source_kind="kassa_cashshift",
                quality_status="final",
            )
        )
        await session.commit()

        balance = await build_partner_cash_balance(session, as_of=JULY_END)
        assert balance.returned == Decimal("0.00")
        assert balance.outstanding == Decimal("50000.00")


async def test_without_a_declared_partner_the_debt_is_shown_as_overstated(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Партнёр не объявлен — считаем взятое и честно говорим, что вычесть возвраты нечем.

    Молча показать ноль или молча вычесть «похожие» проводки — два разных способа соврать;
    здесь выбран третий: показать величину и назвать её неполноту.
    """
    async with async_session_factory() as session:
        await _declare_partner(session, None)
        await _shift_payout(
            session, amount="50000.00", closed_on=datetime(2026, 7, 5, 23, tzinfo=UTC)
        )
        await _commission(session, month=date(2026, 7, 1), revenue="0.00")
        await session.commit()

        balance = await build_partner_cash_balance(session, as_of=JULY_END)
        assert balance.outstanding == Decimal("50000.00")
        assert balance.repayments_linked is False
        assert balance.complete is False


async def test_only_the_partner_category_of_shift_payouts_counts(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Изъятия на зарплату курьеров и в главную кассу — не долг партнёра.

    Все три категории лежат в одной таблице и различаются только счётом-назначением iiko.
    """
    async with async_session_factory() as session:
        partner = await make_counterparty(session, name="В гостях у Алисы")
        await _declare_partner(session, partner.id)

        await _shift_payout(
            session, amount="10000.00", closed_on=datetime(2026, 7, 5, 23, tzinfo=UTC)
        )
        await _shift_payout(
            session,
            amount="70000.00",
            closed_on=datetime(2026, 7, 6, 23, tzinfo=UTC),
            category="courier_salary",
        )
        await _shift_payout(
            session,
            amount="80000.00",
            closed_on=datetime(2026, 7, 7, 23, tzinfo=UTC),
            category="main_cash",
        )
        await _commission(session, month=date(2026, 7, 1), revenue="0.00")
        await session.commit()

        balance = await build_partner_cash_balance(session, as_of=JULY_END)
        assert balance.taken == Decimal("10000.00")


async def test_a_partner_who_returned_more_than_owed_is_not_a_negative_receivable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Переплата партнёра — уже не наша дебиторка, и минусом в активе ей не место."""
    async with async_session_factory() as session:
        partner = await make_counterparty(session, name="В гостях у Алисы")
        await _declare_partner(session, partner.id)

        await _shift_payout(
            session, amount="10000.00", closed_on=datetime(2026, 7, 5, 23, tzinfo=UTC)
        )
        await _commission(session, month=date(2026, 7, 1), revenue="0.00")
        await _repayment(
            session, counterparty_id=partner.id, amount="25000.00", on=date(2026, 7, 9)
        )
        await session.commit()

        balance = await build_partner_cash_balance(session, as_of=JULY_END)
        assert balance.outstanding == Decimal("0.00")


async def test_the_setting_exists_so_the_owner_can_declare_the_partner(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Настройка заведена миграцией — иначе объявить партнёра было бы негде, кроме SQL."""
    async with async_session_factory() as session:
        row = await session.scalar(
            select(AppSetting).where(AppSetting.key == PARTNER_COUNTERPARTY_SETTING)
        )
        assert row is not None
        assert row.category == "kassa"
        assert row.widget_type == "text"

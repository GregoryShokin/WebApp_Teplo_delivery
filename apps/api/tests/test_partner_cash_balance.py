"""Долг партнёра по наличным из смены — начисление из iiko, гашение по контрагенту.

ПОЧЕМУ ГАШЕНИЕ ИМЕННО ПО КОНТРАГЕНТУ. Соблазн считать его по статье «Поступление денег с торг.
точек» велик: на копии прода единственное внесение партнёра шло именно ей. Но той же статьёй
идёт ВЕСЬ поток инкассаций с точек — 452 проводки, — и правило «минус всё по статье» вычло бы из
долга партнёра обычную выручку, обнулив его при первой же инкассации. Здесь это закреплено
тестом, чтобы соблазн не вернулся.
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
)
from app.services.partner_cash_balance import (  # noqa: E402
    PARTNER_COUNTERPARTY_SETTING,
    build_partner_cash_balance,
)

JULY_END = date(2026, 7, 31)


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


async def _declare_partner(session: AsyncSession, counterparty_id: uuid.UUID) -> None:
    await session.execute(
        update(AppSetting)
        .where(AppSetting.key == PARTNER_COUNTERPARTY_SETTING)
        .values(value=str(counterparty_id))
    )
    await session.flush()


async def test_the_partner_debt_is_what_was_taken_minus_what_came_back(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Изъятия смены минус внесения партнёра — и то, и другое до даты среза."""
    async with async_session_factory() as session:
        partner = await make_counterparty(session, name="В гостях у Алисы")
        wallet = await make_wallet(session, name="ТК Черникова", wallet_type="store_cash")
        article = await make_expense_article(
            session, code="postuplenie_deneg_s_torg_tochek", name="Поступление с торг. точек"
        )
        await _declare_partner(session, partner.id)

        await _shift_payout(
            session, amount="60000.00", closed_on=datetime(2026, 7, 5, 23, tzinfo=UTC)
        )
        await _shift_payout(
            session, amount="40000.00", closed_on=datetime(2026, 7, 20, 23, tzinfo=UTC)
        )
        # Изъятие августа — после среза, в июльский долг не входит.
        await _shift_payout(
            session, amount="15000.00", closed_on=datetime(2026, 8, 3, 23, tzinfo=UTC)
        )
        session.add(
            CashflowTransaction(
                wallet_id=wallet.id,
                counterparty_id=partner.id,
                article_id=article.id,
                direction="in",
                amount=Decimal("30000.00"),
                operation_date=date(2026, 7, 10),
                source_kind="kassa_payin",
                quality_status="final",
            )
        )
        await session.commit()

        balance = await build_partner_cash_balance(session, as_of=JULY_END)
        assert balance.taken == Decimal("100000.00")
        assert balance.returned == Decimal("30000.00")
        assert balance.outstanding == Decimal("70000.00")
        assert balance.repayments_linked is True


async def test_ordinary_collections_under_the_same_article_do_not_repay_the_partner(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Инкассация с точки идёт той же статьёй — и долг партнёра она не гасит.

    Это главный тест файла. Правило «минус всё по статье» выглядит правдоподобно ровно до
    первого дня, когда выручку сдали в кассу: на копии прода такой проводкой ушло бы в вычет
    несколько миллионов, и долг партнёра обнулился бы навсегда.
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
    """Партнёр не объявлен — считаем начисление и честно говорим, что вычесть возвраты нечем.

    Молча показать ноль или молча вычесть «похожие» проводки — два разных способа соврать;
    здесь выбран третий: показать величину и назвать её неполноту.
    """
    async with async_session_factory() as session:
        await session.execute(
            update(AppSetting)
            .where(AppSetting.key == PARTNER_COUNTERPARTY_SETTING)
            .values(value="")
        )
        await _shift_payout(
            session, amount="50000.00", closed_on=datetime(2026, 7, 5, 23, tzinfo=UTC)
        )
        await session.commit()

        balance = await build_partner_cash_balance(session, as_of=JULY_END)
        assert balance.outstanding == Decimal("50000.00")
        assert balance.repayments_linked is False


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
        await session.commit()

        balance = await build_partner_cash_balance(session, as_of=JULY_END)
        assert balance.taken == Decimal("10000.00")


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

"""Синк кассовых смен iiko (витрина): категоризация изъятий по счёту-назначению,
фильтр по ``closeDate`` (не по статусу) и идемпотентность повторного синка.

iiko-фетчи замоканы — проверяется чистая логика upsert + разноса. Прогон на ``teplo_test``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.services.kassa.iiko_cashshift_sync as sync_mod
from app.models import CashflowTransaction, DdsArticle, IikoCashShift, IikoCashShiftPayout, Wallet
from app.services.kassa.iiko_cashshift_sync import (
    ALISA_ACCOUNT_ID,
    COURIER_SALARY_ACCOUNT_ID,
    MAIN_CASH_ACCOUNT_ID,
    SHIFT_SOURCE_KIND,
    get_shift,
    post_shift_adjustment,
    sync_iiko_cashshifts,
)

DF = date(2026, 6, 15)
DT = date(2026, 6, 18)

ACCOUNTS = {
    MAIN_CASH_ACCOUNT_ID: "Главная касса",
    COURIER_SALARY_ACCOUNT_ID: "Зарплата курьеров",
    ALISA_ACCOUNT_ID: "Алиса наличные",
}
PAYMENTS = {
    "sess-1": {
        "payOutsRecords": [
            {"info": {"id": "p1", "accountId": MAIN_CASH_ACCOUNT_ID, "sum": 9786, "comment": ""}},
            {
                "info": {
                    "id": "p2",
                    "accountId": COURIER_SALARY_ACCOUNT_ID,
                    "sum": 6630,
                    "comment": "зп",
                }
            },
            {"info": {"id": "p3", "accountId": ALISA_ACCOUNT_ID, "sum": 7070, "comment": "алиса"}},
            {"info": {"id": "p4", "accountId": "acc-unknown", "sum": 100, "comment": "?"}},
        ]
    }
}


def _shift(session_id: str, *, close: str | None = "2026-06-17T22:00:00") -> dict:
    return {
        "id": session_id,
        "sessionNumber": 1065,
        "pointOfSaleId": "pos-1",
        "managerId": "mgr-1",
        "openDate": "2026-06-17T08:30:00",
        "closeDate": close,
        "sessionStatus": "UNACCEPTED",
        "sessionStartCash": 5000.08,
        "salesCash": 16416,
        "salesCard": 86948,
        "payIn": 0,
        "payOut": 16416,
        "cashRemain": 5000.08,
        "cashDiff": 10000.16,
    }


def _patch(monkeypatch, shifts: list[dict], payments: dict | None = None) -> None:
    payments = payments if payments is not None else PAYMENTS
    monkeypatch.setattr(sync_mod, "_auth_token", lambda: "tok")
    monkeypatch.setattr(sync_mod, "_fetch_cashshifts_list", lambda token, df, dt: shifts)
    monkeypatch.setattr(sync_mod, "_fetch_shift_payments", lambda token, sid: payments.get(sid, {}))
    monkeypatch.setattr(sync_mod, "_fetch_accounts_map", lambda token: ACCOUNTS)


async def test_sync_imports_and_categorizes_payouts(
    monkeypatch, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _patch(monkeypatch, [_shift("sess-1")])
    async with async_session_factory() as session:
        report = await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()

        assert report.created == 1
        assert report.payouts == 4
        shift = await session.scalar(
            select(IikoCashShift).where(IikoCashShift.iiko_session_id == "sess-1")
        )
        assert shift is not None
        assert shift.sales_cash == Decimal("16416")
        assert shift.cash_remain == Decimal("5000.08")
        assert shift.session_status == "UNACCEPTED"  # импортирована несмотря на статус
        assert shift.posted is True  # синк сразу проводит наличный контур

        payouts = (
            await session.scalars(
                select(IikoCashShiftPayout).where(IikoCashShiftPayout.shift_id == shift.id)
            )
        ).all()
        by_cat = {payout.category: payout for payout in payouts}
        assert set(by_cat) == {"main_cash", "courier_salary", "alisa", "unknown"}
        assert by_cat["main_cash"].amount == Decimal("9786")
        assert by_cat["main_cash"].account_name == "Главная касса"
        assert by_cat["courier_salary"].amount == Decimal("6630")
        assert by_cat["alisa"].amount == Decimal("7070")
        assert by_cat["unknown"].account_name is None  # счёта нет в справочнике


async def test_sync_skips_shift_without_close_date(
    monkeypatch, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _patch(monkeypatch, [_shift("sess-open", close=None)])
    async with async_session_factory() as session:
        report = await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()
        assert report.created == 0
        assert report.skipped == 1
        count = await session.scalar(select(func.count()).select_from(IikoCashShift))
        assert count == 0


async def test_sync_is_idempotent(
    monkeypatch, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _patch(monkeypatch, [_shift("sess-1")])
    async with async_session_factory() as session:
        await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()
        report2 = await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()

        assert report2.created == 0
        assert report2.updated == 1
        shift_count = await session.scalar(
            select(func.count())
            .select_from(IikoCashShift)
            .where(IikoCashShift.iiko_session_id == "sess-1")
        )
        assert shift_count == 1
        payout_count = await session.scalar(
            select(func.count())
            .select_from(IikoCashShiftPayout)
            .join(IikoCashShift, IikoCashShiftPayout.shift_id == IikoCashShift.id)
            .where(IikoCashShift.iiko_session_id == "sess-1")
        )
        assert payout_count == 4  # пересобраны, не задвоены


async def test_get_shift_detail_payload(
    monkeypatch, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _patch(monkeypatch, [_shift("sess-1")])
    async with async_session_factory() as session:
        await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()
        shift = await session.scalar(
            select(IikoCashShift).where(IikoCashShift.iiko_session_id == "sess-1")
        )
        payload = await get_shift(session, shift.id)
        assert payload is not None
        assert payload["posted"] is True
        assert payload["sales_cash"] == Decimal("16416")
        assert len(payload["payouts"]) == 4


# --- Фаза 6: авто-проводка наличного контура в ДДС (Модель A) ------------------


async def _txns_for_shift(session: AsyncSession, shift_id) -> list[CashflowTransaction]:
    return list(
        (
            await session.scalars(
                select(CashflowTransaction).where(
                    CashflowTransaction.source_kind == SHIFT_SOURCE_KIND,
                    CashflowTransaction.source_id == shift_id,
                )
            )
        ).all()
    )


async def test_sync_auto_posts_cash_circuit(
    monkeypatch, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    # Авто-проводка требует засеянные статьи (0114) и кошелёк tk_chernikova (0115).
    _patch(monkeypatch, [_shift("sess-1")])
    async with async_session_factory() as session:
        report = await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()
        assert report.posted == 1

        shift = await session.scalar(
            select(IikoCashShift).where(IikoCashShift.iiko_session_id == "sess-1")
        )
        assert shift.posted is True

        txns = await _txns_for_shift(session, shift.id)
        by_dir = {t.direction: t for t in txns}
        # Приход = main_cash (9786) + courier (6630) = 16416; расход курьеров = 6630.
        assert by_dir["in"].amount == Decimal("16416")
        assert by_dir["out"].amount == Decimal("6630")

        cash_wallet = await session.get(Wallet, by_dir["in"].wallet_id)
        assert cash_wallet.code == "tk_chernikova"
        assert by_dir["out"].wallet_id == cash_wallet.id  # курьеры с той же кассы

        inflow_article = await session.get(DdsArticle, by_dir["in"].article_id)
        courier_article = await session.get(DdsArticle, by_dir["out"].article_id)
        assert inflow_article.name == "Поступление денег с торг. точек"
        assert courier_article.name == "Курьерская служба -"

        # Алиса (7070) и unknown (100) — без движений ДДС.
        assert len(txns) == 2
        # Чистый остаток на кассе = инкассация (main_cash) = 9786.
        assert by_dir["in"].amount - by_dir["out"].amount == Decimal("9786")


async def test_post_is_idempotent_on_resync(
    monkeypatch, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _patch(monkeypatch, [_shift("sess-1")])
    async with async_session_factory() as session:
        await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()
        await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()
        shift = await session.scalar(
            select(IikoCashShift).where(IikoCashShift.iiko_session_id == "sess-1")
        )
        txns = await _txns_for_shift(session, shift.id)
        assert len(txns) == 2  # повторный синк не задвоил движения


async def test_post_adjustment_books_cash_diff_once(
    monkeypatch, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _patch(monkeypatch, [_shift("sess-1")])
    async with async_session_factory() as session:
        await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()
        shift = await session.scalar(
            select(IikoCashShift).where(IikoCashShift.iiko_session_id == "sess-1")
        )
        # cash_diff = 10000.16 > 0 → приход «Корретировки кассы +».
        txn = await post_shift_adjustment(session, shift)
        await session.commit()
        assert txn.direction == "in"
        assert txn.amount == Decimal("10000.16")
        article = await session.get(DdsArticle, txn.article_id)
        assert article.name == "Корретировки кассы +"

        with pytest.raises(RuntimeError):
            await post_shift_adjustment(session, shift)

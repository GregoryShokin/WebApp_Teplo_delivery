"""Витрина ТЕКУЩЕЙ смены и сигнал «наличка зависла в ящике».

Две вещи, которые синк не покрывает by design: он тянет только закрытые смены
(``status=CLOSED``), поэтому инкассация становится видна лишь вечером, а забытая
инкассация не ловится вовсе — недостачи она не даёт (см. ``compute_real_cash_diff``:
невынутые деньги одновременно уменьшают ``pay_out`` и поднимают ``cash_remain``).

iiko-фетчи замоканы. Прогон на ``teplo_test``.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from decimal import Decimal

import pytest
from cp_helpers import headers_for
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.services.kassa.iiko_cashshift_sync as sync_mod
from app.models import AppSetting, IikoCashShift
from app.services.kassa.iiko_cashshift_sync import (
    ALISA_ACCOUNT_ID,
    CASH_FLOAT_MIN_KEY,
    CASH_FLOAT_NORM_KEY,
    COURIER_SALARY_ACCOUNT_ID,
    FLOAT_MISSING,
    FLOAT_OK,
    FLOAT_PARTIAL,
    FLOAT_SHORT,
    MAIN_CASH_ACCOUNT_ID,
    STUCK_CASH_THRESHOLD_KEY,
    fetch_open_shift,
    get_shift,
    list_shifts,
    sync_iiko_cashshifts,
)

DF = date(2026, 8, 18)
DT = date(2026, 8, 21)
SHIFT_DAY = date(2026, 8, 19)

# Смена 1206 прода (19.08.2026): курьерам и Алисе выдали, инкассацию забыли —
# 2 821,83 ₽ остались в ящике сверх флоута 5 000,50.
OPEN_ROW = {
    "id": "sess-open",
    "sessionNumber": 1207,
    "pointOfSaleId": "pos-1",
    "openDate": "2026-08-19T09:34:40",
    "closeDate": None,
    "sessionStatus": "OPEN",
    "sessionStartCash": 7822.33,
    "salesCash": 320,
    "payIn": 0,
    "payOut": 2822,
    "cashRemain": None,
}
OPEN_PAYMENTS = {
    "payOutsRecords": [
        {
            "info": {
                "id": "po-1",
                "accountId": MAIN_CASH_ACCOUNT_ID,
                "sum": 2822,
                "comment": "за 19.08",
            },
            "actualSum": 2822,
        }
    ]
}


def _raise_iiko_down() -> str:
    raise RuntimeError("iiko auth failed: 503")


def _patch_open(monkeypatch, rows: list[dict], payments: dict | None = None) -> None:
    payments = payments if payments is not None else OPEN_PAYMENTS
    monkeypatch.setattr(sync_mod, "_auth_token", lambda: "tok")
    monkeypatch.setattr(sync_mod, "_fetch_open_cashshifts", lambda token, df, dt: rows)
    monkeypatch.setattr(sync_mod, "_fetch_shift_payments", lambda token, sid: payments)
    monkeypatch.setattr(
        sync_mod,
        "_fetch_cash_sales_by_day",
        lambda token, df, dt: ({SHIFT_DAY: Decimal("320")}, {SHIFT_DAY: Decimal("1500")}),
    )
    # Справочник счетов не должен дёргаться, пока все изъятия — из трёх известных.
    monkeypatch.setattr(
        sync_mod,
        "_fetch_accounts_map",
        lambda token: pytest.fail("справочник счетов запрошен без незнакомого счёта"),
    )


async def test_fetch_open_shift_builds_showcase(monkeypatch, async_session_factory) -> None:
    _patch_open(monkeypatch, [OPEN_ROW])
    async with async_session_factory() as session:
        payload = await fetch_open_shift(session)

    assert payload is not None
    assert payload["session_number"] == 1207
    assert payload["session_status"] == "OPEN"
    # Остаток ящика считаем сами: 7822.33 + 320 + 0 − 2822 (iiko у открытой смены не отдаёт).
    assert payload["cash_in_drawer"] == Decimal("5320.33")
    assert payload["collected_cash"] == Decimal("2822")
    # Сверх нормы размена (5000) — 320,33: столько ещё предстоит инкассировать.
    assert payload["cash_float_norm"] == Decimal("5000")
    assert payload["cash_float_min"] == Decimal("3000")
    assert payload["cash_over_norm"] == Decimal("320.33")
    assert payload["float_is_short"] is False
    assert [item["category"] for item in payload["payouts"]] == ["main_cash"]
    assert payload["payouts"][0]["account_name"] == "Главная касса"
    assert payload["payouts"][0]["comment"] == "за 19.08"


async def test_fetch_open_shift_returns_none_without_open_shift(
    monkeypatch, async_session_factory
) -> None:
    _patch_open(monkeypatch, [])
    async with async_session_factory() as session:
        assert await fetch_open_shift(session) is None


async def test_fetch_open_shift_ignores_closed_row(monkeypatch, async_session_factory) -> None:
    """Смена с closeDate — уже забота синка; витрина «идёт» её не показывает."""
    _patch_open(monkeypatch, [{**OPEN_ROW, "closeDate": "2026-08-19T22:00:00"}])
    async with async_session_factory() as session:
        assert await fetch_open_shift(session) is None


async def test_fetch_open_shift_without_olap_hides_drawer(
    monkeypatch, async_session_factory
) -> None:
    """Нет выручки из OLAP — остаток не показываем, а не показываем приблизительный."""
    _patch_open(monkeypatch, [OPEN_ROW])
    monkeypatch.setattr(sync_mod, "_fetch_cash_sales_by_day", lambda token, df, dt: ({}, {}))
    async with async_session_factory() as session:
        payload = await fetch_open_shift(session)

    assert payload is not None
    assert payload["cash_sales"] is None
    assert payload["cash_in_drawer"] is None
    assert payload["cash_over_norm"] is None
    # Изъятия при этом видны — ради них витрина и нужна.
    assert payload["collected_cash"] == Decimal("2822")


async def test_fetch_open_shift_resolves_unknown_account(
    monkeypatch, async_session_factory
) -> None:
    """Незнакомый счёт назначения — единственный повод сходить за справочником счетов."""
    _patch_open(
        monkeypatch,
        [OPEN_ROW],
        payments={
            "payOutsRecords": [
                {"info": {"id": "po-x", "accountId": "acc-unknown", "sum": 100}, "actualSum": 100}
            ]
        },
    )
    monkeypatch.setattr(sync_mod, "_fetch_accounts_map", lambda token: {"acc-unknown": "Прочее"})
    async with async_session_factory() as session:
        payload = await fetch_open_shift(session)

    assert payload is not None
    assert payload["payouts"][0]["category"] == "unknown"
    assert payload["payouts"][0]["account_name"] == "Прочее"
    assert payload["collected_cash"] == Decimal("0.00")


# --- сигнал «наличка зависла в ящике» ------------------------------------------
#
# Меряем от НОРМЫ размена (5000 ₽ по умолчанию), а не от остатка на открытие смены.
# Кейсы взяты с прода: 19.08 — настоящий пропуск, 08.08 — восстановление просевшего
# размена при честной инкассации (разностное правило дало бы здесь ложную тревогу).


def _closed_shift(
    *,
    pay_out: float,
    cash_remain: float,
    start_cash: float = 5000.50,
    cash_sales: float = 15444.83,
) -> dict:
    return {
        "id": "sess-1206",
        "sessionNumber": 1206,
        "pointOfSaleId": "pos-1",
        "managerId": "mgr-1",
        "openDate": "2026-08-19T09:32:15",
        "closeDate": "2026-08-19T22:00:33",
        "sessionStatus": "UNACCEPTED",
        "sessionStartCash": start_cash,
        "salesCash": cash_sales,
        "salesCard": 40000,
        "payIn": 0,
        "payOut": pay_out,
        "cashRemain": cash_remain,
        "cashDiff": cash_remain * 2,
    }


def _patch_closed(monkeypatch, shifts: list[dict], payments: dict) -> None:
    monkeypatch.setattr(sync_mod, "_auth_token", lambda: "tok")
    monkeypatch.setattr(sync_mod, "_fetch_cashshifts_list", lambda token, df, dt: shifts)
    monkeypatch.setattr(sync_mod, "_fetch_shift_payments", lambda token, sid: payments)
    monkeypatch.setattr(
        sync_mod,
        "_fetch_accounts_map",
        lambda token: {
            MAIN_CASH_ACCOUNT_ID: "Главная касса",
            COURIER_SALARY_ACCOUNT_ID: "Зарплата курьеров",
            ALISA_ACCOUNT_ID: "Алиса наличные",
        },
    )
    monkeypatch.setattr(
        sync_mod,
        "_fetch_cash_sales_by_day",
        lambda token, df, dt: (
            {SHIFT_DAY: Decimal("15444.83")},
            {SHIFT_DAY: Decimal("55444.83")},
        ),
    )


def _payouts(*records: tuple[str, str, float]) -> dict:
    return {
        "payOutsRecords": [
            {"info": {"id": pid, "accountId": account, "sum": amount, "comment": ""}}
            for pid, account, amount in records
        ]
    }


# Разнос изъятий смены 1206: курьеры + Алиса, инкассации нет.
PAYOUTS_NO_COLLECTION = _payouts(
    ("p1", COURIER_SALARY_ACCOUNT_ID, 8074),
    ("p2", ALISA_ACCOUNT_ID, 4549),
)


async def _row(session: AsyncSession) -> dict:
    rows = await list_shifts(session, date_from=DF, date_to=DT)
    return next(item for item in rows if item["session_number"] == 1206)


async def test_forgotten_collection_is_flagged_missing(
    monkeypatch, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Инцидент 19.08.2026: инкассацию забыли, в ящике осталось 7 822,33 при норме 5 000."""
    _patch_closed(
        monkeypatch,
        [_closed_shift(pay_out=12623, cash_remain=7822.33)],
        PAYOUTS_NO_COLLECTION,
    )
    async with async_session_factory() as session:
        await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()

        row = await _row(session)
        assert row["cash_over_norm"] == Decimal("2822.33")  # 7822.33 − норма 5000
        assert row["float_status"] == FLOAT_MISSING
        assert row["collected_cash"] == Decimal("0.00")
        # Недостачи при этом НЕТ: формула сверки сокращает невынутые деньги.
        assert row["real_cash_diff"] == Decimal("0.00")
        assert row["penalty_status"] == "none"


async def test_partial_collection_is_flagged_partial(
    monkeypatch, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Инкассация была, но сверх нормы всё равно осталось больше порога."""
    payments = _payouts(
        ("p1", COURIER_SALARY_ACCOUNT_ID, 8074),
        ("p2", ALISA_ACCOUNT_ID, 4549),
        ("p3", MAIN_CASH_ACCOUNT_ID, 1000),
    )
    _patch_closed(monkeypatch, [_closed_shift(pay_out=13623, cash_remain=6822.33)], payments)
    async with async_session_factory() as session:
        await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()

        row = await _row(session)
        assert row["collected_cash"] == Decimal("1000")
        assert row["cash_over_norm"] == Decimal("1822.33")
        assert row["float_status"] == FLOAT_PARTIAL


async def test_closing_at_norm_gives_no_signal(
    monkeypatch, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Ящик закрыт на норме размена — инкассировали всё, что следовало."""
    payments = _payouts(
        ("p1", COURIER_SALARY_ACCOUNT_ID, 8074),
        ("p2", ALISA_ACCOUNT_ID, 4549),
        ("p3", MAIN_CASH_ACCOUNT_ID, 2821.83),
    )
    _patch_closed(monkeypatch, [_closed_shift(pay_out=15444.83, cash_remain=5000.50)], payments)
    async with async_session_factory() as session:
        await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()

        row = await _row(session)
        assert row["cash_over_norm"] == Decimal("0.50")
        assert row["float_status"] == FLOAT_OK


async def test_restoring_drained_float_is_not_a_signal(
    monkeypatch, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Смена 1195 (08.08.2026): ящик просел до 1 709 ₽ и был поднят обратно до нормы.

    Прибавка за смену — 3 291,50 ₽, и разностное правило подняло бы тревогу, хотя
    инкассацию провели честно (13 390 ₽). От нормы кейс читается верно: сигнала нет.
    """
    payments = _payouts(("p1", MAIN_CASH_ACCOUNT_ID, 13390))
    _patch_closed(
        monkeypatch,
        [_closed_shift(start_cash=1709, pay_out=13390, cash_remain=5000.50)],
        payments,
    )
    async with async_session_factory() as session:
        await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()

        row = await _row(session)
        assert row["collected_cash"] == Decimal("13390")
        assert row["cash_over_norm"] == Decimal("0.50")
        assert row["float_status"] == FLOAT_OK


async def test_slow_drift_above_norm_is_caught(
    monkeypatch, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Ящик распух понемногу: за смену прибавилось всего 300 ₽, но норму он уже перерос.

    Разностное правило такую смену пропускало (300 < порога), от нормы она видна.
    """
    _patch_closed(
        monkeypatch,
        [_closed_shift(start_cash=6500, pay_out=12623, cash_remain=6800)],
        PAYOUTS_NO_COLLECTION,
    )
    async with async_session_factory() as session:
        await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()

        row = await _row(session)
        assert row["cash_over_norm"] == Decimal("1800")
        assert row["float_status"] == FLOAT_MISSING


async def test_small_leftover_is_below_threshold(
    monkeypatch, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Хвост размена в пределах порога сигналом не считается (обычные 265–914 ₽)."""
    _patch_closed(
        monkeypatch,
        [_closed_shift(pay_out=15179.83, cash_remain=5265.50)],
        PAYOUTS_NO_COLLECTION,
    )
    async with async_session_factory() as session:
        await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()

        row = await _row(session)
        assert row["cash_over_norm"] == Decimal("265.50")
        assert row["float_status"] == FLOAT_OK


async def test_drained_float_is_flagged_short(
    monkeypatch, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Смена 1193 (06.08.2026): инкассировали всё, в ящике осталось 795 ₽.

    Утром касса откроется с этой суммой, и сдачу давать будет нечем — обратный сигнал.
    """
    payments = _payouts(("p1", MAIN_CASH_ACCOUNT_ID, 19649.83))
    _patch_closed(monkeypatch, [_closed_shift(pay_out=19649.83, cash_remain=795)], payments)
    async with async_session_factory() as session:
        await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()

        row = await _row(session)
        assert row["cash_over_norm"] == Decimal("-4205.00")
        assert row["float_status"] == FLOAT_SHORT


async def test_below_norm_but_above_minimum_is_silent(
    monkeypatch, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Смена 1198 (11.08.2026): 3 426,50 ₽ — ниже нормы, но сдачу давать есть чем.

    Ровно тот случай, из-за которого минимум задан отдельной суммой, а не симметричным
    допуском от нормы: при допуске 1000 эта смена звенела бы впустую.
    """
    payments = _payouts(("p1", MAIN_CASH_ACCOUNT_ID, 17018.33))
    _patch_closed(monkeypatch, [_closed_shift(pay_out=17018.33, cash_remain=3426.50)], payments)
    async with async_session_factory() as session:
        await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()

        row = await _row(session)
        assert row["cash_over_norm"] == Decimal("-1573.50")
        assert row["float_status"] == FLOAT_OK


async def test_exactly_at_minimum_is_silent(
    monkeypatch, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Ровно минимум — ещё не сигнал: границу считаем допустимой."""
    payments = _payouts(("p1", MAIN_CASH_ACCOUNT_ID, 17444.83))
    _patch_closed(monkeypatch, [_closed_shift(pay_out=17444.83, cash_remain=3000)], payments)
    async with async_session_factory() as session:
        await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()

        assert (await _row(session))["float_status"] == FLOAT_OK


async def test_minimum_setting_overrides_default(
    monkeypatch, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Подняли минимум до 4000 — смена с 3 426,50 ₽ начинает считаться бедной разменом."""
    payments = _payouts(("p1", MAIN_CASH_ACCOUNT_ID, 17018.33))
    _patch_closed(monkeypatch, [_closed_shift(pay_out=17018.33, cash_remain=3426.50)], payments)
    async with async_session_factory() as session:
        setting = await session.scalar(
            select(AppSetting).where(AppSetting.key == CASH_FLOAT_MIN_KEY)
        )
        assert setting is not None, "миграция 0276 должна засеять минимальный размен"
        assert setting.value == 3000  # дефолт из миграции
        setting.value = 4000
        await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()

        assert (await _row(session))["float_status"] == FLOAT_SHORT


async def test_stuck_cash_wins_over_short(
    monkeypatch, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Состояния взаимоисключающие: остаток не бывает разом выше нормы и ниже минимума."""
    _patch_closed(
        monkeypatch,
        [_closed_shift(pay_out=12623, cash_remain=7822.33)],
        PAYOUTS_NO_COLLECTION,
    )
    async with async_session_factory() as session:
        setting = await session.scalar(
            select(AppSetting).where(AppSetting.key == CASH_FLOAT_MIN_KEY)
        )
        setting.value = 9000  # заведомо выше остатка — но сверху беда важнее
        await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()

        assert (await _row(session))["float_status"] == FLOAT_MISSING


async def test_norm_setting_overrides_default(
    monkeypatch, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Подняли норму размена до 10 000 — та же смена перестаёт быть сигналом."""
    _patch_closed(
        monkeypatch,
        [_closed_shift(pay_out=12623, cash_remain=7822.33)],
        PAYOUTS_NO_COLLECTION,
    )
    async with async_session_factory() as session:
        setting = await session.scalar(
            select(AppSetting).where(AppSetting.key == CASH_FLOAT_NORM_KEY)
        )
        assert setting is not None, "миграция 0276 должна засеять норму размена"
        assert setting.value == 5000  # дефолт из миграции
        setting.value = 10000
        await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()

        row = await _row(session)
        assert row["cash_over_norm"] == Decimal("-2177.67")
        assert row["float_status"] == FLOAT_OK


async def test_threshold_setting_overrides_default(
    monkeypatch, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Порог берётся из настройки: подняли до 5000 — сигнал по 2 822,33 гаснет."""
    _patch_closed(
        monkeypatch,
        [_closed_shift(pay_out=12623, cash_remain=7822.33)],
        PAYOUTS_NO_COLLECTION,
    )
    async with async_session_factory() as session:
        setting = await session.scalar(
            select(AppSetting).where(AppSetting.key == STUCK_CASH_THRESHOLD_KEY)
        )
        assert setting is not None, "миграция 0276 должна засеять порог"
        assert setting.value == 1000  # дефолт из миграции
        setting.value = 5000
        await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()

        row = await _row(session)
        assert row["float_status"] == FLOAT_OK


async def test_shift_detail_carries_norm_and_threshold(
    monkeypatch, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Деталь смены отдаёт обе величины — витрина объясняет, откуда взялся сигнал."""
    _patch_closed(
        monkeypatch,
        [_closed_shift(pay_out=12623, cash_remain=7822.33)],
        PAYOUTS_NO_COLLECTION,
    )
    async with async_session_factory() as session:
        await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        await session.commit()
        shift = await session.scalar(
            select(IikoCashShift).where(IikoCashShift.iiko_session_id == "sess-1206")
        )
        payload = await get_shift(session, shift.id)

        assert payload is not None
        assert payload["float_status"] == FLOAT_MISSING
        assert payload["cash_over_norm"] == Decimal("2822.33")
        assert payload["cash_float_norm"] == Decimal("5000")
        assert payload["cash_float_threshold"] == Decimal("1000")
        assert payload["cash_float_min"] == Decimal("3000")


async def test_missing_remainder_gives_no_signal(
    monkeypatch, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Нет остатка — сигнал не выдумываем (тот же принцип, что у недостачи)."""
    _patch_closed(
        monkeypatch,
        [_closed_shift(pay_out=12623, cash_remain=7822.33)],
        PAYOUTS_NO_COLLECTION,
    )
    async with async_session_factory() as session:
        await sync_iiko_cashshifts(session, date_from=DF, date_to=DT)
        shift = await session.scalar(
            select(IikoCashShift).where(IikoCashShift.iiko_session_id == "sess-1206")
        )
        shift.cash_remain = None
        await session.commit()

        row = await _row(session)
        assert row["cash_over_norm"] is None
        assert row["float_status"] == FLOAT_OK


async def test_open_shift_reports_short_float_live(monkeypatch, async_session_factory) -> None:
    """Разменом бедно прямо сейчас — видно, пока смена идёт и можно довнести наличных."""
    _patch_open(monkeypatch, [{**OPEN_ROW, "sessionStartCash": 795, "payOut": 0}])
    monkeypatch.setattr(
        sync_mod, "_fetch_shift_payments", lambda token, sid: {"payOutsRecords": []}
    )
    async with async_session_factory() as session:
        payload = await fetch_open_shift(session)

    assert payload is not None
    assert payload["cash_in_drawer"] == Decimal("1115")  # 795 + 320 выручки
    assert payload["float_is_short"] is True


async def test_open_shift_fetched_at_is_moscow_aware(monkeypatch, async_session_factory) -> None:
    """Метка времени витрины — с таймзоной: фронт печатает её локальным форматтером."""
    _patch_open(monkeypatch, [OPEN_ROW])
    async with async_session_factory() as session:
        payload = await fetch_open_shift(session)

    assert payload is not None
    fetched_at = payload["fetched_at"]
    assert isinstance(fetched_at, datetime)
    assert fetched_at.tzinfo is not None


# --- HTTP-слой: порядок роутов и сериализация ----------------------------------

KASSA_BASE = "/api/v1/kassa"


def _run(coro):
    return asyncio.run(coro)


def test_open_shift_endpoint_serializes(
    monkeypatch, client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """`/shifts/open` обязан разбираться РАНЬШЕ `/shifts/{shift_id}`, иначе 422 на «open»."""
    _patch_open(monkeypatch, [OPEN_ROW])
    headers = _run(headers_for(async_session_factory, "kassa-open@test.local", ["cashier"]))

    resp = client.get(f"{KASSA_BASE}/shifts/open", headers=headers)

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["session_number"] == 1207
    assert data["cash_in_drawer"] == 5320.33
    assert data["collected_cash"] == 2822.0
    assert data["payouts"][0]["category"] == "main_cash"


def test_open_shift_endpoint_returns_null_without_shift(
    monkeypatch, client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _patch_open(monkeypatch, [])
    headers = _run(headers_for(async_session_factory, "kassa-open2@test.local", ["cashier"]))

    resp = client.get(f"{KASSA_BASE}/shifts/open", headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json() is None


def test_open_shift_endpoint_reports_iiko_failure(
    monkeypatch, client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """iiko недоступен — 502 с человеческим текстом, как у синка, а не 500."""
    monkeypatch.setattr(sync_mod, "_auth_token", _raise_iiko_down)
    headers = _run(headers_for(async_session_factory, "kassa-open3@test.local", ["cashier"]))

    resp = client.get(f"{KASSA_BASE}/shifts/open", headers=headers)

    assert resp.status_code == 502
    assert "Ошибка iiko" in resp.json()["detail"]

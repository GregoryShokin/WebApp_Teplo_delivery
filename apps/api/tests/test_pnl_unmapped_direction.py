"""Выручка без цеха iiko не теряется из ОПиУ молча.

До 23.10.2026 строка пресета выручки, чьё место приготовления не отнесено ни к одному
направлению, отбрасывалась целиком: её не было ни в направлениях, ни в «итого», и ни лог, ни
отчёт об этом не говорили. Налоговая база (подневный OLAP по подразделению) такие продажи
видела — выручка ОПиУ и налоговая расходились бы без единого сигнала. Решение владельца:
в «итого» и с предупреждением.
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.models.pnl import PnlIikoFact
from app.services.pnl import iiko_sync, projector
from app.services.pnl.iiko_sync import (
    PNL_PRESET_ID,
    REVENUE_PRESET_ID,
    GoodsSyncResult,
    aggregate_revenue_rows,
    sync_month,
)

JULY = date(2026, 7, 1)


def _row(place: str | None, gross: str, net: str, cost: str) -> dict:
    return {
        "CookingPlace": place,
        "DishSumInt": gross,
        "DishDiscountSumInt": net,
        "ProductCostBase.ProductCost": cost,
    }


MAPPED_ROWS = [_row("Суши", "100", "80", "30"), _row("Пицца", "50", "45", "15")]
UNMAPPED_ROWS = [_row("Сервис", "10", "12", "2"), _row(None, "5", "5", "1")]


def test_unmapped_rows_go_to_total_and_their_own_direction() -> None:
    result = aggregate_revenue_rows(MAPPED_ROWS + UNMAPPED_ROWS)

    assert result.facts[("revenue_net", "total")] == Decimal("142")  # 80 + 45 + 12 + 5
    assert result.facts[("revenue_net", "unmapped")] == Decimal("17")
    assert result.facts[("revenue_gross", "unmapped")] == Decimal("15")
    assert result.facts[("food_cost", "unmapped")] == Decimal("3")
    assert result.facts[("revenue_net", "rolls")] == Decimal("80")
    assert result.unmapped_directions == {"Сервис", "(пусто)"}


def _patch_iiko(monkeypatch, revenue_rows: list[dict]) -> None:
    async def no_credentials(_session) -> None:
        return None

    async def fetch_preset(preset_id, _start, _end):
        if preset_id == REVENUE_PRESET_ID:
            return revenue_rows
        assert preset_id == PNL_PRESET_ID
        return []

    async def no_partners(_session, _start, _end):
        return [], set()

    async def no_goods(_session, _month):
        return GoodsSyncResult()

    monkeypatch.setattr(iiko_sync, "_load_source_credential_env", no_credentials)
    monkeypatch.setattr(iiko_sync, "_fetch_preset", fetch_preset)
    monkeypatch.setattr(iiko_sync, "fetch_partner_commissions", no_partners)
    monkeypatch.setattr(iiko_sync, "fetch_goods_metrics", no_goods)


def test_sync_keeps_unmapped_revenue_and_report_warns(async_session_factory, monkeypatch) -> None:
    async def scenario() -> None:
        _patch_iiko(monkeypatch, MAPPED_ROWS + UNMAPPED_ROWS)
        async with async_session_factory() as session:
            result = await sync_month(session, JULY)
            await session.commit()
            assert any("без цеха" in note for note in result.changed)

            unmapped = await session.scalar(
                select(PnlIikoFact).where(
                    PnlIikoFact.period_month == JULY,
                    PnlIikoFact.metric_code == "revenue_net",
                    PnlIikoFact.direction == "unmapped",
                )
            )
            assert unmapped is not None and unmapped.amount == Decimal("17.00")

            report = await projector.build_report(session, JULY)
            revenue = next(line for line in report.lines if line.code == "revenue_net_chernikova")
            assert revenue.amount == Decimal("142.00")  # без цеха — в выручке, а не мимо неё
            warning = next(
                item for item in report.warnings if item.code == "iiko_revenue_without_direction"
            )
            assert warning.amount == Decimal("17.00")
            assert warning.line_code == "revenue_net_chernikova"
            assert "«(пусто)», «Сервис»" in warning.message

    asyncio.run(scenario())


def test_direction_disappears_after_place_is_mapped(async_session_factory, monkeypatch) -> None:
    """Позицию привязали к цеху в iiko — «без цеха» и предупреждение уходят, а не залипают."""

    async def scenario() -> None:
        _patch_iiko(monkeypatch, MAPPED_ROWS + UNMAPPED_ROWS)
        async with async_session_factory() as session:
            await sync_month(session, JULY)
            await session.commit()

        _patch_iiko(monkeypatch, MAPPED_ROWS)
        async with async_session_factory() as session:
            result = await sync_month(session, JULY)
            await session.commit()
            assert any("направления больше нет" in note for note in result.changed)

            directions = set(
                (
                    await session.scalars(
                        select(PnlIikoFact.direction).where(
                            PnlIikoFact.period_month == JULY,
                            PnlIikoFact.metric_code == "revenue_net",
                        )
                    )
                ).all()
            )
            assert directions == {"total", "rolls", "pizza"}

            report = await projector.build_report(session, JULY)
            assert not [w for w in report.warnings if w.code == "iiko_revenue_without_direction"]

    asyncio.run(scenario())


def test_empty_preset_does_not_wipe_direction_split(async_session_factory, monkeypatch) -> None:
    """Пустой ответ пресета — не повод стирать разрез, записанный прошлой ночью."""

    async def scenario() -> None:
        _patch_iiko(monkeypatch, MAPPED_ROWS + UNMAPPED_ROWS)
        async with async_session_factory() as session:
            await sync_month(session, JULY)
            await session.commit()

        _patch_iiko(monkeypatch, [])
        async with async_session_factory() as session:
            await sync_month(session, JULY)
            await session.commit()
            kept = (
                await session.scalars(
                    select(PnlIikoFact.direction).where(
                        PnlIikoFact.period_month == JULY,
                        PnlIikoFact.metric_code == "revenue_net",
                    )
                )
            ).all()
            assert set(kept) == {"total", "rolls", "pizza", "unmapped"}

    asyncio.run(scenario())

"""Выбытие основного средства: объекта нет, остаток уходит в убыток.

Контур появился 2026-08-02 по живому случаю: менеджер написал про уличную скамью «украли»,
модель верно ответила «остаточная стоимость полностью утрачена» — и нажать было не на что.
Списание отвечает на вопрос, на который переоценка не отвечает в принципе: объект не подешевел,
его НЕТ.

Сеть здесь не трогается: ответ модели инъектируется параметром ``call``.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

sys.path.append(str(Path(__file__).parent / "counterparties"))

from cp_helpers import admin_headers  # noqa: E402

from app.models import AssetCategory, AssetConditionReport, AssetMovement, FixedAsset  # noqa: E402
from app.services.asset_balance import balance_lines, snapshot_month  # noqa: E402
from app.services.asset_disposal import (  # noqa: E402
    cancel_disposal,
    disposal_series,
    dispose_asset,
    sell_asset,
)
from app.services.asset_revaluation import (  # noqa: E402
    decide_report,
    process_pending,
    submit_report,
)
from app.services.fixed_assets import (  # noqa: E402
    FixedAssetError,
    accrue_depreciation,
)

BASE = "/api/v1/fixed-assets"


def _admin(factory) -> dict[str, str]:
    return asyncio.run(admin_headers(factory))


def _today() -> date:
    return datetime.now(UTC).date()


def _month_start(value: date) -> date:
    return value.replace(day=1)


async def _asset(
    session: AsyncSession,
    *,
    cost: str = "24000.00",
    commissioned_on: date | None = None,
    status: str = "in_use",
) -> FixedAsset:
    asset = FixedAsset(
        name="Скамья уличная",
        initial_cost=Decimal(cost),
        useful_life_months=60,
        commissioned_on=commissioned_on or _month_start(_today()),
        status=status,
        valuation_basis="market",
    )
    session.add(asset)
    await session.flush()
    return asset


def _loss_answer(**overrides: Any):
    """Ответ модели про УТРАТУ — третий исход обращения о состоянии."""
    payload = {
        "impact_kind": "loss",
        "value_loss_share": "1",
        "reasoning": "Менеджер сообщил, что скамью украли — объекта физически нет.",
        "confidence": 0.75,
        "needs_human": False,
    }
    payload.update(overrides)

    async def call(_settings, **_kwargs) -> dict[str, Any]:
        return payload

    return call


async def test_stolen_object_becomes_a_writeoff_proposal_not_a_revaluation(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«Украли» — предложение СПИСАТЬ, а не переоценить в ноль.

    До появления вида ``loss`` такой ответ выбрасывался целиком: кражи не было в словаре, а
    любой вид вне словаря код считал негодным ответом. Владелец видел верное обоснование
    модели и одну кнопку «Оставить как есть».
    """
    async with async_session_factory() as session:
        asset = await _asset(session)
        await submit_report(session, asset_id=asset.id, message="украли", user_id=None)
        await session.commit()

        counts = await process_pending(session, call=_loss_answer())
        assert counts == {"processed": 1, "proposed": 1, "failed": 0}

        report = await session.scalar(select(AssetConditionReport))
        assert report is not None
        assert report.status == "proposed"
        assert report.proposed_disposal is True
        # Суммы у предложения нет и быть не должно: убытком станет вся остаточная стоимость.
        assert report.proposed_cost is None
        assert Decimal(str(report.cost_before)) == Decimal("24000.00")
        # Карточка не тронута — решение за владельцем.
        assert Decimal(str(asset.initial_cost)) == Decimal("24000.00")
        assert asset.status == "in_use"


async def test_unsure_model_does_not_propose_a_writeoff(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``needs_human`` перевешивает вид: списание — необратимое действие, гадать нельзя."""
    async with async_session_factory() as session:
        asset = await _asset(session)
        await submit_report(session, asset_id=asset.id, message="скамьи нет?", user_id=None)
        await session.commit()

        await process_pending(session, call=_loss_answer(needs_human=True))

        report = await session.scalar(select(AssetConditionReport))
        assert report is not None
        assert report.proposed_disposal is False
        assert report.proposed_cost is None


async def test_accepting_the_writeoff_takes_the_asset_off_the_books(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Владелец согласился: объект выбыл, остаток стал убытком, карточка осталась историей.

    Первоначальная стоимость НЕ переписывается — именно этим выбытие отличается от переоценки
    в ноль, которая подменила бы цену покупки размером накопленного износа.
    """
    async with async_session_factory() as session:
        asset = await _asset(session)
        report = await submit_report(session, asset_id=asset.id, message="украли", user_id=None)
        await session.commit()
        await process_pending(session, call=_loss_answer())

        await decide_report(session, report=report, accept=True, user_id=None)
        await session.commit()

        assert report.status == "applied"
        assert asset.status == "disposed"
        assert Decimal(str(asset.initial_cost)) == Decimal("24000.00")

        movement = await session.scalar(select(AssetMovement))
        assert movement is not None
        assert movement.movement_type == "writeoff"
        assert Decimal(str(movement.amount)) == Decimal("24000.00")
        assert movement.previous_status == "in_use"
        assert movement.condition_report_id == report.id
        assert movement.note == "украли"


async def test_loss_equals_residual_not_initial_cost(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Убыток — ОСТАТОЧНАЯ стоимость: часть цены объекта уже ушла в расход амортизацией.

    Списать в убыток всю первоначальную значило бы посчитать её дважды.
    """
    async with async_session_factory() as session:
        started = _month_start(_today() - timedelta(days=90))
        asset = await _asset(session, cost="60000.00", commissioned_on=started)
        await session.flush()
        await accrue_depreciation(session, period_month=started)
        await session.commit()

        movement = await dispose_asset(session, asset=asset, reason="сгорела")
        await session.commit()

        # 60 000 / 60 мес = 1 000 ₽ за месяц; начислен один месяц.
        assert Decimal(str(movement.amount)) == Decimal("59000.00")


async def test_disposal_leaves_closed_months_alone(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Выбытие в августе не должно стирать объект из июльского баланса.

    Раньше строки баланса собирались по ТЕКУЩЕМУ статусу карточки: списанный сегодня объект
    задним числом исчезал из всех прошлых месяцев, включая уже замороженные и перенесённые в
    отчётность. Теперь объект покидает баланс с месяца выбытия.
    """
    async with async_session_factory() as session:
        category = await session.scalar(select(AssetCategory).where(AssetCategory.name == "Мебель"))
        if category is None:
            category = AssetCategory(name="Мебель", useful_life_months=60)
            session.add(category)
            await session.flush()

        past = _month_start(_today() - timedelta(days=60))
        asset = await _asset(session, cost="24000.00", commissioned_on=past)
        asset.category_id = category.id
        await session.flush()
        await accrue_depreciation(session, period_month=past)
        await session.commit()

        before = {
            line.line_name: line.residual for line in await balance_lines(session, as_of=past)
        }
        assert before.get(category.name) is not None

        await dispose_asset(session, asset=asset, reason="украли")
        await session.commit()

        after_past = {
            line.line_name: line.residual for line in await balance_lines(session, as_of=past)
        }
        after_now = {
            line.line_name: line.residual for line in await balance_lines(session, as_of=_today())
        }
        # Прошлый месяц не поехал…
        assert after_past.get(category.name) == before[category.name]
        # …а из текущего объект ушёл.
        assert category.name not in after_now


async def test_writeoff_in_a_frozen_month_is_refused(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """В закрытый месяц не списываем: его цифры владелец уже перенёс в отчётность."""
    async with async_session_factory() as session:
        past = _month_start(_today() - timedelta(days=60))
        asset = await _asset(session, cost="24000.00", commissioned_on=past)
        await session.flush()
        await accrue_depreciation(session, period_month=past)
        await snapshot_month(session, period_month=past)
        await session.commit()

        with pytest.raises(FixedAssetError, match="перенесён в отчётность"):
            await dispose_asset(session, asset=asset, reason="украли", disposed_on=past)


async def test_future_date_and_double_writeoff_are_refused(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Списать вперёд нельзя, дважды — тоже: убыток удвоился бы в ОПиУ."""
    async with async_session_factory() as session:
        asset = await _asset(session)
        await session.commit()

        with pytest.raises(FixedAssetError, match="в будущем"):
            await dispose_asset(
                session, asset=asset, reason="украли", disposed_on=_today() + timedelta(days=1)
            )

        await dispose_asset(session, asset=asset, reason="украли")
        await session.commit()

        with pytest.raises(FixedAssetError, match="уже списан"):
            await dispose_asset(session, asset=asset, reason="ещё раз")


async def test_cancel_returns_the_asset_to_its_previous_status(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Отмена возвращает объект туда, где он был, и убирает убыток из ряда ОПиУ.

    Именно в тот статус, а не «в работу»: у неработающего объекта своя строка баланса.
    """
    async with async_session_factory() as session:
        asset = await _asset(session, status="not_working")
        report = await submit_report(session, asset_id=asset.id, message="украли", user_id=None)
        await session.commit()
        await process_pending(session, call=_loss_answer())
        await decide_report(session, report=report, accept=True, user_id=None)
        await session.commit()

        assert asset.status == "disposed"

        await cancel_disposal(session, asset=asset)
        await session.commit()

        assert asset.status == "not_working"
        assert await session.scalar(select(AssetMovement)) is None
        assert await disposal_series(session) == []
        # Сообщение о состоянии снова ждёт решения — иначе в истории объекта осталось бы
        # «применено» рядом с отменённым списанием.
        assert report.status == "proposed"


async def test_disposal_series_feeds_the_pnl_line(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Помесячный ряд убытка — строка ОПиУ. Считать её по разнице реестров нельзя: списанный
    объект из реестра исчезает вместе со своей стоимостью."""
    async with async_session_factory() as session:
        first = await _asset(session, cost="24000.00")
        second = await _asset(session, cost="6000.00")
        await session.commit()

        await dispose_asset(session, asset=first, reason="украли")
        await dispose_asset(session, asset=second, reason="разбита")
        await session.commit()

        series = await disposal_series(session)
        assert series == [(_month_start(_today()), Decimal("30000.00"), 2)]


def test_api_writeoff_and_status_guard(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Маршрут списания и защита от возврата объекта в обход отмены.

    Вернуть статус патчем значило бы оставить объект в балансе и убыток в ОПиУ одновременно —
    одна и та же стоимость учлась бы дважды.
    """
    headers = _admin(async_session_factory)
    created = client.post(
        BASE,
        headers=headers,
        json={"name": "Скамья уличная", "initial_cost": "24000.00", "useful_life_months": 60},
    )
    asset_id = created.json()["id"]

    empty = client.post(f"{BASE}/{asset_id}/disposal", headers=headers, json={"reason": "  "})
    assert empty.status_code == 422

    residual_before = Decimal(client.get(f"{BASE}/summary", headers=headers).json()["residual"])

    disposed = client.post(
        f"{BASE}/{asset_id}/disposal", headers=headers, json={"reason": "украли"}
    )
    assert disposed.status_code == 200, disposed.text
    assert disposed.json()["loss_amount"] == "24000.00"

    detail = client.get(f"{BASE}/{asset_id}", headers=headers).json()
    assert detail["status"] == "disposed"
    assert detail["disposal"]["reason"] == "украли"
    assert detail["disposal"]["previous_status"] == "in_use"
    assert detail["disposal"]["period_frozen"] is False

    # В реестре карточка остаётся — со статусом «Списан»: это история объекта, и фильтр по
    # статусу её показывает намеренно. А вот из свода её стоимость уходит.
    listed = client.get(BASE, headers=headers).json()
    row = next(item for item in listed["items"] if item["id"] == asset_id)
    assert row["status"] == "disposed"
    assert row["monthly_amount"] == "0.00"
    residual_after = Decimal(client.get(f"{BASE}/summary", headers=headers).json()["residual"])
    assert residual_before - residual_after == Decimal("24000.00")

    revive = client.patch(f"{BASE}/{asset_id}", headers=headers, json={"status": "in_use"})
    assert revive.status_code == 422
    assert "отмените выбытие" in revive.json()["detail"]

    cancelled = client.delete(f"{BASE}/{asset_id}/disposal", headers=headers)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "in_use"

    back = client.get(f"{BASE}/{asset_id}", headers=headers).json()
    assert back["disposal"] is None


def test_reporting_carries_the_disposal_loss(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Строка «УчОС Убыток от выбытия» приходит из модуля готовой — её переносят руками."""
    headers = _admin(async_session_factory)
    created = client.post(
        BASE,
        headers=headers,
        json={"name": "Скамья уличная", "initial_cost": "24000.00", "useful_life_months": 60},
    )
    asset_id = created.json()["id"]
    client.post(f"{BASE}/{asset_id}/disposal", headers=headers, json={"reason": "украли"})

    reporting = client.get(f"{BASE}/reporting", headers=headers).json()
    series = reporting["disposal_series"]
    assert len(series) == 1
    assert series[0]["amount"] == "24000.00"
    assert series[0]["asset_count"] == 1


async def test_sale_leaves_the_asset_in_the_months_before_it(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Продажа — второй способ покинуть баланс, и месяц у неё считается так же, как у списания.

    Пока продажа оформлялась правкой статуса, движения не возникало вовсе: объект исчезал из
    ВСЕХ прошлых месяцев разом, потому что ``_has_left`` без строки движения не знает даты.
    """
    async with async_session_factory() as session:
        category = await session.scalar(select(AssetCategory).where(AssetCategory.name == "Мебель"))
        if category is None:
            category = AssetCategory(name="Мебель", useful_life_months=60)
            session.add(category)
            await session.flush()

        past = _month_start(_today() - timedelta(days=60))
        asset = await _asset(session, cost="24000.00", commissioned_on=past)
        asset.category_id = category.id
        await session.flush()
        await accrue_depreciation(session, period_month=past)
        await session.commit()

        before = {
            line.line_name: line.residual for line in await balance_lines(session, as_of=past)
        }
        assert before.get(category.name) is not None

        await sell_asset(session, asset=asset, amount=Decimal("5000.00"), note="продали соседям")
        await session.commit()

        after_past = {
            line.line_name: line.residual for line in await balance_lines(session, as_of=past)
        }
        after_now = {
            line.line_name: line.residual for line in await balance_lines(session, as_of=_today())
        }
        assert after_past.get(category.name) == before[category.name]
        assert category.name not in after_now

        movement = await session.scalar(
            select(AssetMovement).where(AssetMovement.movement_type == "sale")
        )
        assert movement is not None
        assert movement.occurred_on == _today()
        # Убытка у продажи нет: в ОПиУ она не идёт, а сумма — справочная цена сделки.
        assert Decimal(str(movement.amount)) == Decimal("5000.00")
        assert (await disposal_series(session)) == []


async def test_sale_in_a_frozen_month_and_a_second_sale_are_refused(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Замороженный месяц продажа не переписывает, а дважды продать один объект нельзя."""
    async with async_session_factory() as session:
        past = _month_start(_today() - timedelta(days=60))
        asset = await _asset(session, commissioned_on=past)
        await accrue_depreciation(session, period_month=past)
        await snapshot_month(session, period_month=past)
        await session.commit()

        with pytest.raises(FixedAssetError, match="закрыт"):
            await sell_asset(session, asset=asset, sold_on=past)

        await sell_asset(session, asset=asset)
        await session.commit()

        with pytest.raises(FixedAssetError, match="уже выбыл"):
            await sell_asset(session, asset=asset)


def test_status_patch_cannot_smuggle_an_asset_out_of_the_balance(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Правка статуса на выбывший отклоняется и подсказывает законный маршрут.

    Обходной путь снимал сразу три защиты: строку движения (по ней баланс узнаёт месяц),
    проверку закрытого месяца и расчёт убытка.
    """
    headers = _admin(async_session_factory)
    created = client.post(
        BASE,
        headers=headers,
        json={"name": "Печь на продажу", "initial_cost": "120000.00", "useful_life_months": 120},
    )
    assert created.status_code == 201, created.text
    asset_id = created.json()["id"]

    for bad_status, hint in (("sold", "/sale"), ("disposed", "/disposal")):
        refused = client.patch(f"{BASE}/{asset_id}", headers=headers, json={"status": bad_status})
        assert refused.status_code == 422, refused.text
        assert hint in refused.json()["detail"]

    # Законная дверь работает и ставит статус сама.
    sold = client.post(f"{BASE}/{asset_id}/sale", headers=headers, json={"amount": "50000.00"})
    assert sold.status_code == 200, sold.text
    assert sold.json()["status"] == "sold"

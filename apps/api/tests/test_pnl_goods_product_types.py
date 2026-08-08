"""В товарном учёте ОПиУ участвуют только товары iiko.

Складские остатки iiko отдают всю номенклатуру подряд: 08.08.2026 владелец увидел в выборе
блюда, заготовки, модификаторы и комбо-наборы — 170 позиций из 429. Размечать их нельзя ни
руками, ни автоматикой: блюдо на складе — это уже приготовленные товары, и своей стоимостью
оно считает то же сырьё вторым разом.

Комбо-наборам в ``iiko_product`` отдельного типа НЕ соответствует: наборы приходят обычными
позициями меню (``DISH``/``PREPARED``/``MODIFIER``), поэтому проверяются здесь тем же тестом,
что и блюда, — отдельного признака у них нет и ждать его неоткуда.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import Settings
from app.models import IikoProduct
from app.models.pnl import (
    PnlIikoFact,
    PnlIikoProductObservation,
    PnlIikoStockFact,
    PnlProductWhitelist,
)
from app.services.pnl import goods_classifier, iiko_sync
from app.services.pnl.ledgers import (
    build_goods_classifications,
    rebuild_goods_from_observations,
    remove_goods_classification,
    save_goods_classification,
)

HEADERS = {"X-User-Role": "admin"}
MONTH = date(2026, 8, 1)


def _catalogue_row(guid: str, name: str, product_type: str, code: str) -> IikoProduct:
    return IikoProduct(iiko_id=guid, name=name, code=code, type=product_type)


def _observation(
    guid: str,
    name: str,
    *,
    source_kind: str = "inventory",
) -> PnlIikoProductObservation:
    return PnlIikoProductObservation(
        period_month=MONTH,
        source_kind=source_kind,
        iiko_product_guid=guid,
        product_name=name,
        product_code=None,
        amount=Decimal("100.00"),
        rows_count=1,
    )


def test_picker_offers_goods_and_hides_dishes_preparations_and_combos(
    async_session_factory,
) -> None:
    async def scenario() -> None:
        async with async_session_factory() as session:
            session.add_all(
                [
                    _catalogue_row("guid-goods", "Лосось с/м", "GOODS", "118"),
                    _catalogue_row("guid-dish", "Палочки", "DISH", "1053"),
                    _catalogue_row("guid-prepared", "Рис для роллов пф", "PREPARED", "700"),
                    _catalogue_row("guid-modifier", "Бекон", "MODIFIER", "220"),
                    _catalogue_row("guid-combo", "Акционный набор №1", "DISH", "900"),
                ]
            )
            session.add_all(
                [
                    _observation("guid-goods", "Лосось с/м"),
                    _observation("guid-dish", "Палочки"),
                    _observation("guid-prepared", "Рис для роллов пф"),
                    _observation("guid-modifier", "Бекон"),
                    _observation("guid-combo", "Акционный набор №1"),
                    # GUID, которого справочник ещё не знает: синхронизация номенклатуры и
                    # выгрузка накладных приходят разными задачами. Такую строку прячут только
                    # вместе с суммой, поэтому она обязана остаться вопросом владельцу.
                    _observation("guid-unknown", "Новый товар", source_kind="incoming_invoice"),
                ]
            )
            await session.commit()

            ledger = await build_goods_classifications(session, MONTH)
            # Своих GUID в выдаче ровно два: товар и ещё неизвестная справочнику позиция.
            # Остальные строки — сид whitelist из миграции 0253, к этому тесту отношения
            # не имеющий.
            shown = {row.product_guid for row in ledger.rows} & {
                "guid-goods",
                "guid-dish",
                "guid-prepared",
                "guid-modifier",
                "guid-combo",
                "guid-unknown",
            }
            assert shown == {"guid-goods", "guid-unknown"}

    asyncio.run(scenario())


def test_api_refuses_to_classify_a_dish(client: TestClient, async_session_factory) -> None:
    async def prepare() -> None:
        async with async_session_factory() as session:
            session.add(_catalogue_row("guid-dish", "Палочки", "DISH", "1053"))
            session.add(_observation("guid-dish", "Палочки"))
            await session.commit()

    async def saved_rule() -> PnlProductWhitelist | None:
        async with async_session_factory() as session:
            return await session.scalar(
                select(PnlProductWhitelist).where(
                    PnlProductWhitelist.iiko_product_guid == "guid-dish"
                )
            )

    asyncio.run(prepare())
    response = client.patch(
        "/api/v1/reports/pnl/ledgers/goods/classifications/guid-dish",
        params={"month": "2026-08"},
        json={"source_kind": "inventory", "status": "stocked", "line_code": None},
        headers=HEADERS,
    )
    assert response.status_code == 422
    assert "блюдо" in response.json()["detail"]
    assert asyncio.run(saved_rule()) is None


def test_api_still_classifies_a_real_product(client: TestClient, async_session_factory) -> None:
    async def prepare() -> None:
        async with async_session_factory() as session:
            session.add(_catalogue_row("guid-goods", "Коробка для пиццы", "GOODS", "118"))
            session.add(_observation("guid-goods", "Коробка для пиццы"))
            await session.commit()

    async def stored_rule() -> PnlProductWhitelist | None:
        async with async_session_factory() as session:
            return await session.scalar(
                select(PnlProductWhitelist).where(
                    PnlProductWhitelist.iiko_product_guid == "guid-goods"
                )
            )

    asyncio.run(prepare())
    response = client.patch(
        "/api/v1/reports/pnl/ledgers/goods/classifications/guid-goods",
        params={"month": "2026-08"},
        json={
            "source_kind": "inventory",
            "status": "stocked",
            "line_code": "pizza_box_inventory",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    body = response.json()
    row = next(item for item in body["rows"] if item["product_guid"] == "guid-goods")
    assert row["status"] == "stocked"
    assert row["needs_removal"] is False
    assert row["product_type"] == "GOODS"

    rule = asyncio.run(stored_rule())
    assert rule is not None
    assert rule.include_status == "stocked"
    assert rule.line_code == "pizza_box_inventory"


def test_saved_dish_waits_for_the_owner_and_is_dropped_only_by_hand(
    client: TestClient,
    async_session_factory,
) -> None:
    """Сценарий «Палочек» с прода: разметка досталась от сида, тип — блюдо."""

    async def prepare() -> None:
        async with async_session_factory() as session:
            session.add(_catalogue_row("guid-dish", "Палочки", "DISH", "1053"))
            session.add(_observation("guid-dish", "Палочки"))
            session.add(
                PnlProductWhitelist(
                    iiko_product_guid="guid-dish",
                    source_kind="inventory",
                    line_code="packaging_inventory",
                    include_status="stocked",
                    product_name="Палочки",
                )
            )
            await session.commit()

    async def rule_left() -> PnlProductWhitelist | None:
        async with async_session_factory() as session:
            return await session.scalar(
                select(PnlProductWhitelist).where(
                    PnlProductWhitelist.iiko_product_guid == "guid-dish"
                )
            )

    baseline = client.get(
        "/api/v1/reports/pnl/ledgers/goods/classifications",
        params={"month": "2026-08"},
        headers=HEADERS,
    ).json()
    asyncio.run(prepare())
    listing = client.get(
        "/api/v1/reports/pnl/ledgers/goods/classifications",
        params={"month": "2026-08"},
        headers=HEADERS,
    )
    assert listing.status_code == 200
    body = listing.json()
    row = next(item for item in body["rows"] if item["product_guid"] == "guid-dish")
    # Строка не исчезла молча: за ней могут стоять уже посчитанные месяцы.
    assert row["needs_removal"] is True
    assert row["product_type"] == "DISH"
    assert body["removal_count"] == 1
    # И при этом не считается ни размеченным товаром, ни вопросом без ответа: оба счётчика
    # остались ровно такими, какими были до появления строки.
    assert body["rules_count"] == baseline["rules_count"]
    assert body["attention_count"] == baseline["attention_count"]

    removed = client.delete(
        "/api/v1/reports/pnl/ledgers/goods/classifications/guid-dish",
        params={"month": "2026-08"},
        headers=HEADERS,
    )
    assert removed.status_code == 200
    assert removed.json()["removal_count"] == 0
    assert all(item["product_guid"] != "guid-dish" for item in removed.json()["rows"])
    assert asyncio.run(rule_left()) is None


def test_removal_is_not_a_shortcut_for_products(client: TestClient, async_session_factory) -> None:
    async def prepare() -> None:
        async with async_session_factory() as session:
            session.add(_catalogue_row("guid-goods", "Лосось с/м", "GOODS", "118"))
            session.add(_observation("guid-goods", "Лосось с/м"))
            await session.commit()

    asyncio.run(prepare())
    response = client.delete(
        "/api/v1/reports/pnl/ledgers/goods/classifications/guid-goods",
        params={"month": "2026-08"},
        headers=HEADERS,
    )
    assert response.status_code == 422
    assert "не-товарного типа" in response.json()["detail"]


def test_stock_metrics_count_only_goods(async_session_factory) -> None:
    """Складской контур — источник строки «Запасы на складах» будущего Баланса."""

    async def scenario() -> None:
        async with async_session_factory() as session:
            session.add_all(
                [
                    _catalogue_row("guid-goods", "Лосось с/м", "GOODS", "118"),
                    _catalogue_row("guid-prepared", "Рис для роллов пф", "PREPARED", "700"),
                ]
            )
            session.add_all(
                [
                    _observation("guid-goods", "Лосось с/м"),
                    _observation("guid-prepared", "Рис для роллов пф"),
                ]
            )
            session.add_all(
                [
                    PnlIikoStockFact(
                        period_month=MONTH,
                        iiko_product_guid="guid-goods",
                        product_name="Лосось с/м",
                        opening_quantity=Decimal("1.000"),
                        opening_amount=Decimal("1000.00"),
                        receipts_amount=Decimal("0.00"),
                        closing_quantity=Decimal("1.000"),
                        closing_amount=Decimal("900.00"),
                        consumption_amount=Decimal("100.00"),
                        stores_count=1,
                    ),
                    PnlIikoStockFact(
                        period_month=MONTH,
                        iiko_product_guid="guid-prepared",
                        product_name="Рис для роллов пф",
                        opening_quantity=Decimal("2.000"),
                        opening_amount=Decimal("500.00"),
                        receipts_amount=Decimal("0.00"),
                        closing_quantity=Decimal("2.000"),
                        closing_amount=Decimal("500.00"),
                        consumption_amount=Decimal("0.00"),
                        stores_count=1,
                    ),
                ]
            )
            await session.commit()

            await rebuild_goods_from_observations(session, MONTH, "inventory")
            await session.commit()

            closing = await session.scalar(
                select(PnlIikoFact).where(
                    PnlIikoFact.period_month == MONTH,
                    PnlIikoFact.metric_code == iiko_sync.METRIC_STOCK_CLOSING,
                    PnlIikoFact.direction == "total",
                )
            )
            assert closing is not None
            # Заготовка на 500,00 ₽ в запас не попала — иначе её сырьё встало бы дважды.
            assert closing.amount == Decimal("900.00")

    asyncio.run(scenario())


def test_auto_classification_never_marks_a_dish_as_stocked(
    async_session_factory,
    monkeypatch,
) -> None:
    """Иначе снятое владельцем возвращалось бы молча при каждой синхронизации."""
    calls: list[str] = []

    async def fake_call_tool(_settings, *, prompt, **_kwargs):
        calls.append(prompt)
        return {"decisions": []}

    monkeypatch.setattr(goods_classifier, "call_tool", fake_call_tool)

    async def scenario() -> None:
        async with async_session_factory() as session:
            session.add_all(
                [
                    _catalogue_row("guid-dish", "Палочки", "DISH", "1053"),
                    _catalogue_row("guid-goods", "Лосось с/м", "GOODS", "118"),
                ]
            )
            await session.commit()

            result = await goods_classifier.auto_classify_new_goods(
                session,
                month_start=MONTH,
                month_end=date(2026, 8, 31),
                observations=[
                    iiko_sync.GoodsProductObservation(
                        source_kind="inventory",
                        iiko_product_guid="guid-dish",
                        product_name="Палочки",
                        product_code="1053",
                        amount=Decimal("-10.00"),
                        rows_count=1,
                    ),
                    iiko_sync.GoodsProductObservation(
                        source_kind="inventory",
                        iiko_product_guid="guid-goods",
                        product_name="Лосось с/м",
                        product_code="118",
                        amount=Decimal("-50.00"),
                        rows_count=1,
                    ),
                ],
                products_payload=json.dumps({"products": []}),
                charts_payload=json.dumps({"assemblyCharts": [], "preparedCharts": []}),
                settings=Settings(anthropic_api_key="test-key"),
            )
            await session.commit()

            rules = {
                rule.iiko_product_guid: rule
                for rule in (await session.execute(select(PnlProductWhitelist))).scalars()
            }
            assert "guid-dish" not in rules
            assert rules["guid-goods"].include_status == "stocked"
            assert result.classified == 1
            # Блюдо даже не поехало во внешнюю модель: гейт стоит до сбора кандидатов.
            assert calls == []

    asyncio.run(scenario())


def test_service_layer_refuses_a_dish_directly(async_session_factory) -> None:
    """Тот же запрет на слое сервиса: ручка — не единственный вход в разметку."""

    async def scenario() -> None:
        async with async_session_factory() as session:
            session.add(_catalogue_row("guid-modifier", "Бекон", "MODIFIER", "220"))
            session.add(_observation("guid-modifier", "Бекон"))
            await session.commit()

            try:
                await save_goods_classification(
                    session,
                    display_month=MONTH,
                    product_guid="guid-modifier",
                    source_kind="inventory",
                    status="stocked",
                    line_code=None,
                    note=None,
                    user_id=None,
                )
            except ValueError as error:
                assert "модификатор" in str(error)
            else:  # pragma: no cover — ветка означает провал теста
                raise AssertionError("модификатор не должен проходить в товарную разметку")

            ledger = await build_goods_classifications(session, MONTH)
            assert all(row.product_guid != "guid-modifier" for row in ledger.rows)

            try:
                await remove_goods_classification(
                    session,
                    display_month=MONTH,
                    product_guid="guid-modifier",
                )
            except ValueError as error:
                assert "нет разметки" in str(error)
            else:  # pragma: no cover — ветка означает провал теста
                raise AssertionError("снимать нечего, а вызов прошёл")

    asyncio.run(scenario())

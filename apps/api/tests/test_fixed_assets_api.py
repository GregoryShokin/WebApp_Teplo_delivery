"""HTTP-контур учёта ОС: реестр, карточка, свод и закрытие месяца.

Расчётная часть покрыта отдельно (``tests/counterparties/test_fixed_assets.py``), здесь —
только то, что добавляет слой API: права, фильтры, форма ответа и гарды на правку.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

sys.path.append(str(Path(__file__).parent / "counterparties"))

from cp_helpers import admin_headers, headers_for  # noqa: E402

from app.models import AssetConditionReport  # noqa: E402

BASE = "/api/v1/fixed-assets"


def _admin(factory) -> dict[str, str]:
    return asyncio.run(admin_headers(factory))


def _manager(factory) -> dict[str, str]:
    return asyncio.run(headers_for(factory, "manager-os@teplo.local", ["manager"]))


def _create(client: TestClient, headers: dict[str, str], **overrides) -> dict:
    payload = {
        "name": "Печь для пиццы",
        "initial_cost": "120000.00",
        "useful_life_months": 120,
        "commissioned_on": "2026-01-01",
        "valuation_basis": "market",
        "valued_on": "2026-01-01",
        # Рыночная оценка означает, что фирма за объект не платила, и происхождение с
        # 0232 обязательно: иначе в балансе не появится встречной записи.
        "acquisition_source": "owner_contribution",
    }
    payload.update(overrides)
    response = client.post(BASE, headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_categories_are_seeded_with_owner_useful_lives(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Справочник приходит непустым: без СПИ амортизация молча не начисляется."""
    response = client.get(f"{BASE}/categories", headers=_admin(async_session_factory))
    assert response.status_code == 200, response.text
    items = {item["name"]: item["useful_life_months"] for item in response.json()["items"]}
    assert items["Тепловое оборудование"] == 84
    assert items["Вспомогательное оборудование"] == 120
    # «Не работающее оборудование» — статус карточки, а не категория: заведи её со сроком,
    # и заведомо мёртвые объекты начнут амортизироваться.
    assert "Не работающее оборудование" not in items


def test_manager_without_permission_gets_403(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Реестр ОС — не общедоступная витрина: без права модуль закрыт целиком."""
    headers = _manager(async_session_factory)
    assert client.get(BASE, headers=headers).status_code == 403
    assert client.get(f"{BASE}/summary", headers=headers).status_code == 403
    assert (
        client.post(BASE, headers=headers, json={"name": "X", "initial_cost": "1"}).status_code
        == 403
    )


def test_inventory_number_is_assigned_and_card_reads_back(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Номер присваивается сам, а карточка отдаёт остаточную и плановое начисление."""
    headers = _admin(async_session_factory)
    created = _create(client, headers)
    assert created["inventory_number"] == "ОС-0001"
    assert created["residual"] == "120000.00"
    assert created["monthly_amount"] == "1000.00"
    assert created["depreciating"] is True

    detail = client.get(f"{BASE}/{created['id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["entries"] == []


def test_list_filters_by_search_and_status(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Поиск смотрит и в название, и в модель, и в инвентарный номер, и в ссылку на опись."""
    headers = _admin(async_session_factory)
    _create(client, headers, name="Печь для пиццы", brand_model="ItPizza ML44")
    _create(client, headers, name="Ларь морозильный", brand_model="POLAIR", status="not_working")

    by_brand = client.get(BASE, headers=headers, params={"search": "polair"})
    assert [item["name"] for item in by_brand.json()["items"]] == ["Ларь морозильный"]

    by_status = client.get(BASE, headers=headers, params={"status": "not_working"})
    assert by_status.json()["total"] == 1
    # Неработающее не амортизируется — методология инвентаризации 2026.
    assert by_status.json()["items"][0]["depreciating"] is False


def test_summary_excludes_disposed_and_groups_by_category(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Списанный объект ушёл из внеоборотных активов — в своде его быть не должно."""
    headers = _admin(async_session_factory)
    _create(client, headers, name="Живая печь")
    sold = _create(client, headers, name="Проданная печь")
    patched = client.patch(f"{BASE}/{sold['id']}", headers=headers, json={"status": "sold"})
    assert patched.status_code == 200, patched.text

    summary = client.get(f"{BASE}/summary", headers=headers).json()
    assert summary["count"] == 1
    assert summary["initial_cost"] == "120000.00"
    assert summary["monthly_amount"] == "1000.00"


def test_cost_cannot_drop_below_what_is_already_accrued(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Иначе остаточная уходит в минус, и баланс показывает отрицательный актив."""
    headers = _admin(async_session_factory)
    asset = _create(client, headers)
    closed = client.post(
        f"{BASE}/close-month", headers=headers, json={"period_month": "2026-02-01"}
    )
    assert closed.status_code == 200, closed.text
    assert closed.json() == {"period_month": "2026-02-01", "entries": 1, "amount": "1000.00"}

    response = client.patch(
        f"{BASE}/{asset['id']}", headers=headers, json={"initial_cost": "500.00"}
    )
    assert response.status_code == 422
    assert "1000.00" in response.json()["detail"]


def test_manual_correction_survives_repeated_month_close(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Ночная джоба не имеет права отменять решение человека при первом же перезапуске."""
    headers = _admin(async_session_factory)
    asset = _create(client, headers)
    client.post(f"{BASE}/close-month", headers=headers, json={"period_month": "2026-02-01"})

    corrected = client.patch(
        f"{BASE}/{asset['id']}/depreciation",
        headers=headers,
        json={
            "period_month": "2026-02-01",
            "amount": "400.00",
            "note": "Печь запущена только 20 августа",
        },
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["is_manual"] is True
    assert corrected.json()["residual_after"] == "119600.00"

    repeat = client.post(
        f"{BASE}/close-month", headers=headers, json={"period_month": "2026-02-01"}
    )
    assert repeat.json()["entries"] == 0

    detail = client.get(f"{BASE}/{asset['id']}", headers=headers).json()
    assert detail["entries"][0]["amount"] == "400.00"
    assert detail["entries"][0]["is_manual"] is True
    assert detail["residual"] == "119600.00"


def test_correction_beyond_initial_cost_is_rejected(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Потолок — первоначальная стоимость: сверх неё амортизировать нечего."""
    headers = _admin(async_session_factory)
    asset = _create(client, headers)
    client.post(f"{BASE}/close-month", headers=headers, json={"period_month": "2026-02-01"})

    response = client.patch(
        f"{BASE}/{asset['id']}/depreciation",
        headers=headers,
        json={"period_month": "2026-02-01", "amount": "999999.00"},
    )
    assert response.status_code == 422
    assert "первоначальной стоимости" in response.json()["detail"]


def test_correction_of_month_without_accrual_is_rejected(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Править нечего, пока месяц не закрыт — молча создавать строку задним числом нельзя."""
    headers = _admin(async_session_factory)
    asset = _create(client, headers)
    response = client.patch(
        f"{BASE}/{asset['id']}/depreciation",
        headers=headers,
        json={"period_month": str(date(2026, 2, 1)), "amount": "100.00"},
    )
    assert response.status_code == 422


def test_options_open_to_whoever_classifies_dds(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Разбирает выписку финансист, а не бухгалтер по ОС — и объект ему выбрать нужно.

    Роль ``manager`` имеет ``finance.cashflow.classify``, но не имеет
    ``accounting.fixed_assets.read``: реестр для неё закрыт (проверено выше), а справочник для
    выбора — открыт. Иначе статья, которая объект ТРЕБУЕТ, сделала бы платёж неразносимым
    вообще. Ровно это уже случилось с помещениями.
    """
    admin = _admin(async_session_factory)
    _create(client, admin, name="Печь для пиццы", inventory_number="ОС-0001")

    manager = _manager(async_session_factory)
    assert client.get(BASE, headers=manager).status_code == 403

    response = client.get(f"{BASE}/options", headers=manager)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["name"] for item in items] == ["Печь для пиццы"]
    # Списку нужны номер, где стоит и статус — по ним один «Стол производственный» отличают от
    # четырёх других. Начислений здесь нет сознательно: это запрос на каждую карточку.
    assert set(items[0]) == {
        "asset_id",
        "inventory_number",
        "name",
        "brand_model",
        "location_name",
        "status",
        "status_title",
        "initial_cost",
    }


def test_options_hide_assets_that_can_no_longer_take_money(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Выбывший объект гейт всё равно отклонит — показывать его значит вести в тупик.

    Неработающий при этом остаётся: его чинят, и покупка запчасти к нему законна.
    """
    headers = _admin(async_session_factory)
    _create(client, headers, name="Живая печь")
    sold = _create(client, headers, name="Проданная печь")
    broken = _create(client, headers, name="Сломанная печь")
    assert (
        client.patch(f"{BASE}/{sold['id']}", headers=headers, json={"status": "sold"}).status_code
        == 200
    )
    assert (
        client.patch(
            f"{BASE}/{broken['id']}", headers=headers, json={"status": "not_working"}
        ).status_code
        == 200
    )

    names = {
        item["name"] for item in client.get(f"{BASE}/options", headers=headers).json()["items"]
    }
    assert names == {"Живая печь", "Сломанная печь"}


def test_asset_created_from_payment_is_ready_to_depreciate(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Карточка из платежа обязана быть ГОТОВОЙ к начислению, а не заготовкой.

    Купили рисоварку — карточки ещё нет, и привязывать в разборе не к чему. Поэтому карточка
    заводится прямо из платежа. Но заведённая наполовину она хуже отсутствующей: объект без
    даты ввода ``accrue_depreciation`` молча ПРОПУСКАЕТ — ошибки нет, амортизации нет, а
    расхождение всплывает через полгода в балансе.

    Стоимость при этом равна сумме платежа и оценкой не является: ``valuation_basis='payment'``.
    """
    manager = _manager(async_session_factory)
    categories = client.get(f"{BASE}/categories", headers=manager).json()["items"]
    heat = next(item for item in categories if item["name"] == "Тепловое оборудование")

    created = client.post(
        f"{BASE}/from-payment",
        headers=manager,
        json={
            "name": "  Рисоварка промышленная  ",
            "initial_cost": "17422.00",
            "category_id": heat["id"],
            "brand_model": "Gastrorag DH-RC-2",
            "commissioned_on": "2026-07-30",
        },
    )
    assert created.status_code == 201, created.text
    option = created.json()
    assert option["name"] == "Рисоварка промышленная"
    assert option["inventory_number"], "номер присваивается сам"

    card = client.get(f"{BASE}/{option['asset_id']}", headers=_admin(async_session_factory)).json()
    assert card["commissioned_on"] == "2026-07-30"
    assert card["valuation_basis"] == "payment"
    assert card["status"] == "in_use"
    # Срок в карточке не задан — он разрешается из категории, и именно это делает объект
    # амортизируемым сразу, без визита в реестр.
    assert card["useful_life_months"] == heat["useful_life_months"]
    assert card["depreciating"] is True, "иначе объект молча выпал бы из начисления"
    assert card["monthly_amount"] != "0.00"


def test_asset_from_payment_without_date_still_depreciates(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Дату не прислали — подставляем сегодня, а не оставляем пустой.

    Пустая дата ввода не ошибка на входе и не видна глазами: она просто выключает начисление
    по объекту навсегда.
    """
    headers = _manager(async_session_factory)
    created = client.post(
        f"{BASE}/from-payment",
        headers=headers,
        json={"name": "Ларь морозильный", "initial_cost": "31000.00"},
    )
    assert created.status_code == 201, created.text
    card = client.get(
        f"{BASE}/{created.json()['asset_id']}", headers=_admin(async_session_factory)
    ).json()
    assert card["commissioned_on"] is not None


def test_categories_carry_field_profile_over_http(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Профиль полей обязан ДОЕХАТЬ ДО ФРОНТА, а не остаться в базе.

    Форма заведения начинается с категории и по её профилю решает, что спрашивать: у техники
    марку и модель, у мебели материал и размеры. Значение живёт в колонке, но между базой и
    формой стоит ``response_model``, который молча выбрасывает поля, не объявленные в схеме.
    Именно так в этом модуле трижды пропадал ``asset_link_kind``: в базе флаг стоял, на экране
    контур не появлялся, а тесты сервиса были зелёными — они читают модель, а не HTTP.
    """
    items = {
        item["name"]: item["spec_profile"]
        for item in client.get(f"{BASE}/categories", headers=_admin(async_session_factory)).json()[
            "items"
        ]
    }
    assert items["Тепловое оборудование"] == "equipment"
    assert items["Электроника и оргтехника"] == "equipment"
    # Стеллажи и столы из нержавейки: марки у них нет, а материал и размеры есть.
    assert items["Вспомогательное оборудование"] == "furniture"
    assert items["Мебель и предметы интерьера"] == "furniture"
    assert items["Прочий кухонный инвентарь"] == "other"


def test_used_asset_without_condition_description_is_rejected(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Б/У без описания состояния не принимаем — оценивать было бы нечем.

    Такая карточка знает ровно одно: износ у объекта уже есть. Какой — неизвестно, и объект
    начинает амортизироваться по сроку НОВОГО, то есть завышает баланс несколько лет подряд.
    Проверка стоит на сервере, а не только в форме: платежи приходят и не из формы.
    """
    response = client.post(
        f"{BASE}/from-payment",
        headers=_manager(async_session_factory),
        json={
            "name": "Пароконвектомат",
            "initial_cost": "90000.00",
            "condition": "used",
            "condition_note": "   ",
        },
    )
    assert response.status_code == 422, response.text


def test_used_asset_goes_into_the_valuation_queue(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Описание состояния б/У объекта встаёт в очередь на оценку и остаётся в карточке.

    Очередь — та же таблица, куда менеджер пишет о поломке: у владельца одно место, где он
    видит предложения модели, а не два. Стоимость при этом не меняется ни на копейку —
    предложение ждёт решения человека.

    Дубль описания в заметке не избыточность: запись в очереди можно отклонить, а вызов модели
    может провалиться. Свидетельство о том, что объект куплен изношенным, обязано пережить оба
    случая.
    """
    created = client.post(
        f"{BASE}/from-payment",
        headers=_manager(async_session_factory),
        json={
            "name": "Шкаф холодильный",
            "initial_cost": "45000.00",
            "condition": "used",
            "condition_note": "2019 года, дверь провисла, компрессор менялся",
        },
    )
    assert created.status_code == 201, created.text

    admin = _admin(async_session_factory)
    card = client.get(f"{BASE}/{created.json()['asset_id']}", headers=admin).json()
    assert card["condition"] == "used"
    assert "компрессор менялся" in (card["note"] or "")
    assert card["initial_cost"] == "45000.00", "оценка ничего не меняет сама"

    reports = card["condition_reports"]
    assert len(reports) == 1, "б/у объект обязан встать в очередь на оценку"
    assert reports[0]["status"] == "pending"
    assert "компрессор менялся" in reports[0]["message"]
    # Вид обращения задаёт САМ ВОПРОС к модели: у покупки это остаток срока, у поломки —
    # стоимость. Перепутанный вид отдал бы карточке скидку с цены, в которой износ уже сидит.
    assert reports[0]["kind"] == "purchase"


def test_new_asset_does_not_call_the_model(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Новый объект в очередь на оценку не встаёт: у него нечего оценивать.

    Иначе каждая покупка стоила бы вызова модели, а владелец получал бы поток предложений
    «износа нет» — и перестал бы их читать ровно к тому моменту, когда появится настоящее.
    """
    created = client.post(
        f"{BASE}/from-payment",
        headers=_manager(async_session_factory),
        json={
            "name": "Рисоварка промышленная",
            "initial_cost": "17422.00",
            "condition": "new",
            # Описание для нового объекта смысла не имеет и должно игнорироваться.
            "condition_note": "коробка не вскрыта",
        },
    )
    assert created.status_code == 201, created.text
    card = client.get(
        f"{BASE}/{created.json()['asset_id']}", headers=_admin(async_session_factory)
    ).json()
    assert card["condition"] == "new"
    assert card["condition_reports"] == []


def test_manager_message_about_a_breakdown_is_not_a_purchase(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Сообщение из карточки — поломка, даже если объект куплен б/у.

    Вид обращения задаётся точкой входа, а не выводится из карточки: «объект помечен б/у» не
    делает каждое следующее сообщение о нём разговором про покупку. Иначе через полгода
    сломавшийся компрессор попросил бы у модели остаток срока вместо оценки ущерба.
    """
    admin = _admin(async_session_factory)
    created = client.post(
        f"{BASE}/from-payment",
        headers=admin,
        json={
            "name": "Шкаф холодильный",
            "initial_cost": "45000.00",
            "condition": "used",
            "condition_note": "2019 года, работает",
        },
    )
    assert created.status_code == 201, created.text
    asset_id = created.json()["asset_id"]

    # Первое обращение — покупка; ждём его оценки, иначе частичный уникальный индекс не пустит
    # второе. Проще снять его руками, чем гонять фоновую джобу в HTTP-тесте.
    async def _release() -> None:
        async with async_session_factory() as session:
            report = await session.scalar(select(AssetConditionReport))
            assert report is not None
            report.status = "proposed"
            await session.commit()

    asyncio.run(_release())

    reported = client.post(
        f"{BASE}/{asset_id}/condition",
        headers=admin,
        json={"message": "Отказал компрессор, холод не держит"},
    )
    assert reported.status_code == 202, reported.text
    assert reported.json()["kind"] == "incident"


def test_market_asset_must_say_where_it_came_from(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Оценка «рыночная» — значит фирма не платила, и происхождение обязательно.

    Баланс — две стороны одного имущества. Покупка правую сторону не двигает: деньги ушли,
    объект пришёл. А объект, за который фирма не платила, увеличивает актив, ничего не
    уменьшая, — и без встречной записи в пассиве баланс просто не сойдётся. Спросить об этом
    можно только в момент заведения: через год никто не вспомнит, чей это был мангал.
    """
    headers = _admin(async_session_factory)
    refused = client.post(
        BASE,
        headers=headers,
        json={
            "name": "Ноутбук собственника",
            "initial_cost": "50000.00",
            "valuation_basis": "market",
            "valued_on": "2026-08-01",
        },
    )
    assert refused.status_code == 422, refused.text

    accepted = client.post(
        BASE,
        headers=headers,
        json={
            "name": "Ноутбук собственника",
            "initial_cost": "50000.00",
            "valuation_basis": "market",
            "valued_on": "2026-08-01",
            "acquisition_source": "owner_contribution",
        },
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["acquisition_source"] == "owner_contribution"


def test_asset_bought_by_the_company_needs_no_question(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """У покупки происхождение подставляется само — спрашивать нечего.

    Оценка «по платежу» и означает, что заплатила фирма. Лишний вопрос на самом частом пути
    стоил бы дороже пользы: чем длиннее форма, тем выше шанс, что покупку проведут статьёй
    попроще и она уйдёт мимо баланса.
    """
    headers = _admin(async_session_factory)
    from_payment = client.post(
        f"{BASE}/from-payment",
        headers=headers,
        json={"name": "Рисоварка", "initial_cost": "17422.00"},
    )
    assert from_payment.status_code == 201, from_payment.text
    card = client.get(f"{BASE}/{from_payment.json()['asset_id']}", headers=headers).json()
    assert card["acquisition_source"] == "purchase"

    # И полная форма с оценкой «по платежу» проходит вообще без поля: заплатила фирма.
    manual = client.post(
        BASE,
        headers=headers,
        json={"name": "Ларь", "initial_cost": "31000.00", "valuation_basis": "payment"},
    )
    assert manual.status_code == 201, manual.text
    assert manual.json()["acquisition_source"] is None, "по платежу происхождение не выдумываем"


def test_origin_can_be_filled_in_later(
    client: TestClient, async_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Происхождение правится задним числом: 149 карточек описи стоят без него.

    Разметить их может только владелец и только поштучно, по мере того как вспоминает. Ставить
    им «вклад собственника» скопом было бы удобно и неправильно — часть могла быть куплена
    фирмой, и такая разметка создала бы в балансе обязательство, которого нет.
    """
    headers = _admin(async_session_factory)
    asset = _create(client, headers)

    # Владелец вспомнил, что это был не подарок, а заём — правка проходит.
    patched = client.patch(
        f"{BASE}/{asset['id']}", headers=headers, json={"acquisition_source": "owner_loan"}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["acquisition_source"] == "owner_loan"

from __future__ import annotations

from fastapi.testclient import TestClient


def test_dds_articles_counterparties_and_rules_crud(client: TestClient) -> None:
    headers = {"X-User-Role": "finance_manager"}

    article_response = client.post(
        "/api/v1/dds/articles",
        headers=headers,
        json={
            "code": "stage3_test_article",
            "name": "Тестовая статья",
            "movement_type": "outflow",
            "activity_type": "operating",
            "description": "created by test",
        },
    )
    assert article_response.status_code == 201
    article = article_response.json()

    patched_article = client.patch(
        f"/api/v1/dds/articles/{article['id']}",
        headers=headers,
        json={"name": "Тестовая статья обновлена"},
    )
    assert patched_article.status_code == 200
    assert patched_article.json()["name"] == "Тестовая статья обновлена"

    article_alias = client.post(
        f"/api/v1/dds/articles/{article['id']}/aliases",
        headers=headers,
        json={"alias": "stage3 article alias"},
    )
    assert article_alias.status_code == 201
    assert (
        client.delete(
            f"/api/v1/dds/articles/aliases/{article_alias.json()['id']}", headers=headers
        ).status_code
        == 204
    )

    counterparty_response = client.post(
        "/api/v1/dds/counterparties",
        headers=headers,
        json={
            "name": "ООО Stage3",
            "inn": "770000030003",
            "type": "legal_entity",
            "default_dds_article_id": article["id"],
            "requisites": {
                "inn": "770000030003",
                "bankBik": "044525225",
                "bankAcnt": "40702810200000012345",
                "recipientCorrAccountNumber": "30101810400000000225",
            },
        },
    )
    assert counterparty_response.status_code == 201
    counterparty = counterparty_response.json()

    # Старый DDS endpoint теперь создаёт полноценную карточку единого реестра.
    registry_response = client.get("/api/v1/counterparties/registry", headers=headers)
    assert registry_response.status_code == 200
    assert counterparty["id"] in {item["counterparty_id"] for item in registry_response.json()}

    patched_counterparty = client.patch(
        f"/api/v1/dds/counterparties/{counterparty['id']}",
        headers=headers,
        json={"name": "ООО Stage3 Updated"},
    )
    assert patched_counterparty.status_code == 200
    assert patched_counterparty.json()["name"] == "ООО Stage3 Updated"

    counterparty_alias = client.post(
        f"/api/v1/dds/counterparties/{counterparty['id']}/aliases",
        headers=headers,
        json={"alias": "stage3 counterparty alias"},
    )
    assert counterparty_alias.status_code == 201
    assert (
        client.delete(
            f"/api/v1/dds/counterparties/aliases/{counterparty_alias.json()['id']}",
            headers=headers,
        ).status_code
        == 204
    )

    rule_response = client.post(
        "/api/v1/dds/classification-rules",
        headers=headers,
        json={
            "name": "Stage3 rule",
            "priority": 90,
            "provider": "tbank",
            "direction": "out",
            "counterparty_inn_match": "770000030003",
            "action": "set_article",
            "article_id": article["id"],
        },
    )
    assert rule_response.status_code == 201
    rule = rule_response.json()

    patched_rule = client.patch(
        f"/api/v1/dds/classification-rules/{rule['id']}",
        headers=headers,
        json={"priority": 80},
    )
    assert patched_rule.status_code == 200
    assert patched_rule.json()["priority"] == 80

    toggled_rule = client.post(
        f"/api/v1/dds/classification-rules/{rule['id']}/toggle",
        headers=headers,
    )
    assert toggled_rule.status_code == 200
    assert toggled_rule.json()["is_active"] is False

    assert (
        client.delete(f"/api/v1/dds/classification-rules/{rule['id']}", headers=headers).status_code
        == 204
    )
    assert (
        client.delete(
            f"/api/v1/dds/counterparties/{counterparty['id']}", headers=headers
        ).status_code
        == 204
    )
    archived = client.get(
        "/api/v1/dds/counterparties",
        headers=headers,
        params={"search": "Stage3 Updated"},
    )
    assert archived.status_code == 200
    assert archived.json()[0]["status"] == "archived"
    assert (
        client.delete(f"/api/v1/dds/articles/{article['id']}", headers=headers).status_code == 204
    )


def test_articles_list_carries_asset_link_kind(client: TestClient) -> None:
    """Признак «что статья делает с ОС» обязан доезжать до фронта.

    РЕГРЕССИЯ, найденная вживую 30.07.2026. Поле было объявлено в схеме и заполнялось
    миграцией, но ``_article_payloads`` собирает ответ ПОЛЕМ ЗА ПОЛЕМ — и про него забыли.
    Схема подставила своё умолчание ``None``, ответ остался валидным, тесты бэкенда прошли
    (гейт читает статью из базы, а не из HTTP), а фронт молча решил, что ни одна статья к
    основным средствам не относится: выбор объекта в разборе не показывался нигде.

    Ошибка такого рода не падает — она тихо выключает функцию. Поэтому проверяем именно
    HTTP-ответ, а не модель.
    """
    headers = {"X-User-Role": "finance_manager"}
    created = client.post(
        "/api/v1/dds/articles",
        headers=headers,
        json={
            "code": "asset_kind_roundtrip",
            "name": "Покупка ОС (тест)",
            "movement_type": "outflow",
            "activity_type": "investing",
            "asset_link_kind": "purchase",
        },
    )
    assert created.status_code == 201, created.text

    listed = client.get("/api/v1/dds/articles", headers=headers)
    assert listed.status_code == 200, listed.text
    article = next(
        item for item in listed.json() if item["code"] == "asset_kind_roundtrip"
    )
    assert article["asset_link_kind"] == "purchase"

    # И обратная сторона: обычная статья приходит с пустым признаком, а не с чужим.
    plain = next(item for item in listed.json() if item["code"] != "asset_kind_roundtrip")
    assert plain["asset_link_kind"] is None

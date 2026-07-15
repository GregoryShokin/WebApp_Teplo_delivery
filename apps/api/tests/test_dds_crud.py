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

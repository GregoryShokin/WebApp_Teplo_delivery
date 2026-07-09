"""API контекста окна «Новый платёж»: права на эндпоинты (header-роли).

Отдельный модуль только с client-тестами: truncate-фикстура ловит teardown-дедлок,
когда client-тесты соседствуют с async-ORM-тестами или делают несколько запросов
(инфраструктурная особенность — воспроизводится и на /dds/wallets дважды).
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient


def test_context_endpoint_denied_without_window_permissions(client: TestClient) -> None:
    # Кассир: ни одного права маршрутов окна — 403.
    response = client.get(
        "/api/v1/dds/new-payment/context", headers={"X-User-Role": "cashier"}
    )
    assert response.status_code == 403


def test_context_endpoint_owner_sees_all_flows(client: TestClient) -> None:
    # Владелец видит всё: и займ-статью, и свободные, и сотрудников (маршруты выплат).
    response = client.get("/api/v1/dds/new-payment/context", headers={"X-User-Role": "owner"})
    assert response.status_code == 200
    body = response.json()
    flows = {item["flow"] for item in body["articles"]}
    assert "employee_loan" in flows and "expense" in flows
    assert body["wallets"] is not None


def test_context_endpoint_manager_has_payout_but_no_loan(client: TestClient) -> None:
    # Менеджер ведёт ЗП (payroll.employee_payouts.create выдано миграцией 0153) → маршрут
    # employee_payout виден; права займов у него нет → employee_loan скрыт.
    response = client.get(
        "/api/v1/dds/new-payment/context", headers={"X-User-Role": "manager"}
    )
    assert response.status_code == 200
    manager_flows = {item["flow"] for item in response.json()["articles"]}
    assert "employee_loan" not in manager_flows
    assert "employee_payout" in manager_flows


def test_expense_draft_endpoint_requires_safe_allocate(client: TestClient) -> None:
    response = client.post(
        "/api/v1/dds/new-payment/expense-draft",
        headers={"X-User-Role": "cashier"},
        json={"article_id": str(uuid.uuid4()), "amount": 100, "purpose": "x"},
    )
    assert response.status_code == 403



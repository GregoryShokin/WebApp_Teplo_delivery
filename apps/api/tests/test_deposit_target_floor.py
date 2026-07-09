"""«Пол» индивидуального депозита: цель ниже дефолта категории — только с отдельным правом.

payroll.production_deposits.target_below_category (миграция 0173). Без права PATCH конфига
с целью ниже категорийного дефолта → 403 с пояснением; с правом — проходит. Цель, равная
дефолту или выше, и сброс override (null) права не требуют. Плюс поле surplus в списке
депозитов — «долг» перед сотрудником, когда собрано больше текущей цели.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from test_deposits import DepositFakeSession, app_with_session, make_account, make_employee

from app.api.deps import CurrentActor, get_current_actor

CATEGORY_RULES = {
    "payroll.category_rules": {
        "2": {"coeff": 7.5, "deposit_target": 15000, "deposit_withholding": 1000},
    }
}

FLOOR_PERMISSION = "payroll.production_deposits.target_below_category"


@pytest.fixture()
def settings_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_load_payroll_settings(_session: object) -> dict[str, object]:
        return dict(CATEGORY_RULES)

    monkeypatch.setattr(
        "app.api.v1.routes.deposits.load_payroll_settings",
        fake_load_payroll_settings,
    )


def app_with_floor_permission(session: DepositFakeSession):
    app = app_with_session(session)

    async def override_actor():
        return CurrentActor(
            roles=frozenset({"test"}),
            permissions=frozenset(
                {
                    "payroll.production_deposits.edit",
                    FLOOR_PERMISSION,
                }
            ),
        )

    app.dependency_overrides[get_current_actor] = override_actor
    return app


def test_target_below_category_blocked_without_permission(settings_patch: None) -> None:
    employee = make_employee()  # category_2 → дефолт цели 15000
    session = DepositFakeSession(employee)

    with TestClient(app_with_session(session)) as client:
        response = client.patch(
            f"/api/v1/deposits/{employee.id}/config",
            headers={"X-User-Role": "finance_manager"},
            json={"deposit_target_override": "10000"},
        )

    assert response.status_code == 403
    assert "ниже" in response.json()["detail"]
    assert "15000" in response.json()["detail"]
    assert employee.deposit_target_override is None


def test_target_below_category_allowed_with_permission(settings_patch: None) -> None:
    employee = make_employee()
    session = DepositFakeSession(employee)

    with TestClient(app_with_floor_permission(session)) as client:
        response = client.patch(
            f"/api/v1/deposits/{employee.id}/config",
            headers={"X-User-Role": "finance_manager"},
            json={"deposit_target_override": "10000"},
        )

    assert response.status_code == 200
    assert employee.deposit_target_override == Decimal("10000")


def test_target_at_or_above_default_needs_no_permission(settings_patch: None) -> None:
    employee = make_employee()
    session = DepositFakeSession(employee)

    with TestClient(app_with_session(session)) as client:
        equal = client.patch(
            f"/api/v1/deposits/{employee.id}/config",
            headers={"X-User-Role": "finance_manager"},
            json={"deposit_target_override": "15000"},
        )
        above = client.patch(
            f"/api/v1/deposits/{employee.id}/config",
            headers={"X-User-Role": "finance_manager"},
            json={"deposit_target_override": "20000"},
        )

    assert equal.status_code == 200
    assert above.status_code == 200
    assert employee.deposit_target_override == Decimal("20000")


def test_reset_override_to_null_needs_no_permission(settings_patch: None) -> None:
    employee = make_employee()
    employee.deposit_target_override = Decimal("10000")
    session = DepositFakeSession(employee)

    with TestClient(app_with_session(session)) as client:
        response = client.patch(
            f"/api/v1/deposits/{employee.id}/config",
            headers={"X-User-Role": "finance_manager"},
            json={"deposit_target_override": None},
        )

    assert response.status_code == 200
    assert employee.deposit_target_override is None


def test_list_deposits_reports_surplus_over_target() -> None:
    # Override приоритетнее настроек — surplus виден и без category_rules.
    employee = make_employee()
    employee.deposit_target_override = Decimal("10000")
    session = DepositFakeSession(employee, make_account(employee, Decimal("12000")))

    with TestClient(app_with_session(session)) as client:
        response = client.get("/api/v1/deposits", headers={"X-User-Role": "finance_manager"})

    assert response.status_code == 200
    rows = response.json()
    assert rows[0]["surplus"] == "2000.00"


def test_list_deposits_surplus_zero_when_below_target() -> None:
    employee = make_employee()
    employee.deposit_target_override = Decimal("10000")
    session = DepositFakeSession(employee, make_account(employee, Decimal("8000")))

    with TestClient(app_with_session(session)) as client:
        response = client.get("/api/v1/deposits", headers={"X-User-Role": "finance_manager"})

    assert response.status_code == 200
    assert response.json()[0]["surplus"] == "0.00"

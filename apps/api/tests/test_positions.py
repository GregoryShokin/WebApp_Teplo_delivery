"""Реестр должностей: CRUD, iiko-семантика (за моками), сид канона, права."""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.deps import CurrentActor, get_current_actor
from app.api.v1.routes import positions as position_routes
from app.models import (
    Employee,
    EmployeePositionAssignment,
    Position,
    PositionChangeEvent,
    Role,
)
from app.services import position_registry
from app.services.iiko_sync import IikoEmployeeRole, IikoRoleUpsertResult

pytestmark = pytest.mark.anyio


@pytest.fixture()
def anyio_backend() -> str:
    return "asyncio"


def _actor(*permissions: str) -> CurrentActor:
    # user_id=None: журнал изменений ссылается на таблицу user по FK,
    # а тестовый актор в ней не существует.
    return CurrentActor(
        roles=frozenset({"manager"}),
        user_id=None,
        permissions=frozenset(permissions),
        permissions_loaded=True,
    )


EDIT_ACTOR_PERMISSIONS = ("settings.positions.read", "settings.positions.edit")


@pytest.fixture()
def edit_client(client: TestClient) -> TestClient:
    client.app.dependency_overrides[get_current_actor] = lambda: _actor(
        *EDIT_ACTOR_PERMISSIONS
    )
    return client


async def test_eligible_for_personal_report_by_archetype() -> None:
    # Признак «попадает в персональный отчёт ЗП»: получает зарплату, кроме обычных курьеров
    # и явно исключённых из ведомости. Реестр сброшен на канон фикстурой _reset_registry.
    elig = position_registry.eligible_for_personal_report
    # Производственный контур — всегда (флаг admin_payroll_excluded про админ-ведомость).
    assert elig("Повар") is True
    assert elig("Кассир") is True
    # Окладной/административный — да, но не при исключении из ведомости (техаккаунт/несалярный).
    assert elig("Управляющий") is True
    assert elig("Менеджер") is True
    assert elig("Помощник менеджера") is True
    assert elig("Системный администратор") is True
    assert elig("Системный администратор", admin_payroll_excluded=True) is False
    assert elig("Управляющий", admin_payroll_excluded=True) is False
    # Вспомогательный персонал (пул/оклад) — да.
    assert elig("Посудомойка") is True
    assert elig("Уборщица") is True
    # Курьеры: обычный — нет; старший курьер (админ-оклад) — да.
    assert elig("Курьер") is False
    assert elig("Старший курьер") is True
    assert elig("Старший курьер", admin_payroll_excluded=True) is False
    # Неизвестная/пустая должность — нет.
    assert elig(None) is False
    assert elig("Нет такой должности") is False


@pytest.fixture()
def fake_iiko(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    calls: dict[str, list] = {"upsert": [], "delete": []}

    async def fake_upsert(
        _session, *, name: str, code: str, role_id: str | None = None, schedule_type: str
    ) -> IikoRoleUpsertResult:
        calls["upsert"].append({"name": name, "code": code, "role_id": role_id})
        return IikoRoleUpsertResult(
            role_id=role_id or f"iiko-{code}",
            code=code,
            name=name,
            schedule_type=schedule_type,
        )

    async def fake_delete(_session, *, role_id: str) -> None:
        calls["delete"].append(role_id)

    monkeypatch.setattr(position_routes, "upsert_iiko_role", fake_upsert)
    monkeypatch.setattr(position_routes, "delete_iiko_role", fake_delete)
    return calls


def test_seed_reproduces_canonical_behavior(edit_client: TestClient) -> None:
    response = edit_client.get("/api/v1/settings/positions")
    assert response.status_code == 200
    by_name = {item["name"]: item for item in response.json()["positions"]}

    assert set(by_name) == {
        "Повар",
        "Кассир",
        "Управляющий",
        "Менеджер",
        "Помощник менеджера",
        "Системный администратор",
        "Уборщица",
        "Посудомойка",
        "Курьер",
        "Старший курьер",
    }
    assert by_name["Повар"]["archetype"] == "production_percent"
    assert by_name["Управляющий"]["archetype"] == "okladnik"
    assert by_name["Посудомойка"]["archetype"] == "shift_pool"
    assert by_name["Курьер"]["permission_group"] == "couriers"
    assert all(item["status"] == "active" for item in by_name.values())

    # Участие в выдаче доступов: управленческие должности — да, линейные — нет.
    assert by_name["Управляющий"]["participates_in_access"] is True
    assert by_name["Управляющий"]["access_role_code"] == "manager"
    assert by_name["Менеджер"]["access_role_code"] == "office_manager"
    assert by_name["Кассир"]["access_role_code"] == "cashier"
    assert by_name["Системный администратор"]["access_role_code"] == "admin"
    assert by_name["Старший курьер"]["participates_in_access"] is True
    assert by_name["Старший курьер"]["access_role_code"] == "senior_courier"
    assert by_name["Старший курьер"]["archetype"] == "courier"
    assert by_name["Курьер"]["participates_in_access"] is False
    assert by_name["Повар"]["participates_in_access"] is False


def test_position_endpoints_require_permission(client: TestClient) -> None:
    client.app.dependency_overrides[get_current_actor] = lambda: _actor()
    assert client.get("/api/v1/settings/positions").status_code == 403
    assert client.post("/api/v1/settings/positions", json={"name": "Бариста"}).status_code == 403


def test_read_permission_does_not_allow_edit(client: TestClient) -> None:
    client.app.dependency_overrides[get_current_actor] = lambda: _actor(
        "settings.positions.read"
    )
    assert client.get("/api/v1/settings/positions").status_code == 200
    assert client.post("/api/v1/settings/positions", json={"name": "Бариста"}).status_code == 403


def test_create_position_pushes_role_to_iiko_and_updates_registry(
    edit_client: TestClient, fake_iiko: dict[str, list]
) -> None:
    response = edit_client.post(
        "/api/v1/settings/positions",
        json={
            "name": "Бариста",
            "archetype": "production_percent",
            "permission_group": "cashiers",
            "gets_vacation": True,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["iiko_role_id"] == f"iiko-{body['iiko_role_code']}"
    assert body["schedule_type"] == "SESSION"
    assert len(fake_iiko["upsert"]) == 1

    # Реестр в памяти обновлён: новая должность участвует в производственной ЗП.
    assert "Бариста" in position_registry.production_payroll_positions()
    assert "Бариста" in position_registry.vacation_positions()
    assert position_registry.is_active_position("Бариста")


def test_create_duplicate_name_409(edit_client: TestClient, fake_iiko: dict[str, list]) -> None:
    response = edit_client.post("/api/v1/settings/positions", json={"name": "Повар"})
    assert response.status_code == 409
    assert not fake_iiko["upsert"]


def test_okladnik_defaults_to_fixed_schedule(
    edit_client: TestClient, fake_iiko: dict[str, list]
) -> None:
    response = edit_client.post(
        "/api/v1/settings/positions",
        json={"name": "Бухгалтер", "archetype": "okladnik", "permission_group": "administration"},
    )
    assert response.status_code == 201
    assert response.json()["schedule_type"] == "FIXED"
    assert "Бухгалтер" in position_registry.okladnik_positions()
    assert "Бухгалтер" in position_registry.admin_payroll_positions()


async def test_update_position_renames_employees(
    edit_client: TestClient,
    fake_iiko: dict[str, list],
    async_session_factory,
) -> None:
    created = edit_client.post(
        "/api/v1/settings/positions",
        json={"name": "Гриль-повар", "archetype": "production_percent"},
    ).json()

    async with async_session_factory() as session:
        employee = Employee(
            full_name="Тест Гриль",
            iiko_id=f"test-{uuid.uuid4()}",
            status="active",
        )
        session.add(employee)
        await session.flush()
        session.add(
            EmployeePositionAssignment(
                employee_id=employee.id,
                position="Гриль-повар",
                effective_from=date(2026, 1, 1),
            )
        )
        await session.commit()
        employee_id = employee.id

    response = edit_client.patch(
        f"/api/v1/settings/positions/{created['id']}",
        json={"name": "Гриль-мастер", "gets_vacation": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Гриль-мастер"
    # upsert при создании + upsert при изменении
    assert len(fake_iiko["upsert"]) == 2

    async with async_session_factory() as session:
        refreshed = await session.get(Employee, employee_id)
        assert refreshed is not None
        assert refreshed.position == "Гриль-мастер"


def test_exclude_hides_position_but_keeps_it_known(
    edit_client: TestClient, fake_iiko: dict[str, list]
) -> None:
    created = edit_client.post(
        "/api/v1/settings/positions",
        json={"name": "Хостес", "archetype": "none"},
    ).json()

    response = edit_client.post(f"/api/v1/settings/positions/{created['id']}/exclude")
    assert response.status_code == 200
    assert response.json()["status"] == "excluded"
    # Исключение не трогает iiko (только upsert при создании).
    assert len(fake_iiko["upsert"]) == 1
    assert not fake_iiko["delete"]

    # Скрыта из выбора, но остаётся известной (сотрудники её сохраняют).
    assert not position_registry.is_active_position("Хостес")
    assert position_registry.is_known_position("Хостес")

    restored = edit_client.post(f"/api/v1/settings/positions/{created['id']}/restore")
    assert restored.status_code == 200
    assert restored.json()["status"] == "active"


async def test_delete_blocked_while_employees_have_position(
    edit_client: TestClient,
    fake_iiko: dict[str, list],
    async_session_factory,
) -> None:
    created = edit_client.post(
        "/api/v1/settings/positions",
        json={"name": "Кальянщик", "archetype": "none"},
    ).json()

    async with async_session_factory() as session:
        employee = Employee(
            full_name="Тест Кальян",
            iiko_id=f"test-{uuid.uuid4()}",
            status="active",
        )
        session.add(employee)
        await session.flush()
        session.add(
            EmployeePositionAssignment(
                employee_id=employee.id,
                position="Кальянщик",
                effective_from=date(2026, 1, 1),
            )
        )
        await session.commit()

    response = edit_client.delete(f"/api/v1/settings/positions/{created['id']}")
    assert response.status_code == 409
    assert not fake_iiko["delete"]
    assert position_registry.is_known_position("Кальянщик")


async def test_delete_removes_position_and_iiko_role(
    edit_client: TestClient,
    fake_iiko: dict[str, list],
    async_session_factory,
) -> None:
    created = edit_client.post(
        "/api/v1/settings/positions",
        json={"name": "Промоутер", "archetype": "none"},
    ).json()

    response = edit_client.delete(f"/api/v1/settings/positions/{created['id']}")
    assert response.status_code == 204
    assert fake_iiko["delete"] == [created["iiko_role_id"]]
    assert not position_registry.is_known_position("Промоутер")

    async with async_session_factory() as session:
        events = (
            await session.scalars(
                select(PositionChangeEvent).where(
                    PositionChangeEvent.position_name == "Промоутер"
                )
            )
        ).all()
        assert {event.action for event in events} == {"create", "delete"}


async def test_sync_iiko_links_known_and_imports_unknown_as_excluded(
    edit_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_iiko: dict[str, list],
    async_session_factory,
) -> None:
    roles = [
        IikoEmployeeRole(id="role-cook", name="Повар", code="COOK"),
        IikoEmployeeRole(id="role-barmen", name="Бармен", code="BARMEN"),
        IikoEmployeeRole(id="role-del", name="Старая роль", code="OLD", deleted=True),
    ]

    async def fake_get_roles(_session):
        return roles

    monkeypatch.setattr(position_routes, "get_iiko_employee_roles", fake_get_roles)

    response = edit_client.post("/api/v1/settings/positions/sync-iiko")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["linked"] == 1
    assert body["imported"] == 1
    # Повар слинкован по имени; остальным активным должностям реестра без роли
    # iiko заводим роль (provision). Бармен исключён — не провизионится.
    assert body["provisioned"] == 9

    by_name = {item["name"]: item for item in body["positions"]}
    assert by_name["Повар"]["iiko_role_id"] == "role-cook"
    assert by_name["Бармен"]["status"] == "excluded"
    assert by_name["Бармен"]["archetype"] == "none"
    assert "Старая роль" not in by_name

    # Должность без роли в iiko (Посудомойка) теперь заведена и выбираема при найме.
    assert by_name["Посудомойка"]["iiko_role_id"] == "iiko-POSUDOMOYKA"
    assert position_registry.is_active_position("Посудомойка")

    # Бармен исключён: не участвует ни в найме, ни в payroll-выборках.
    assert not position_registry.is_active_position("Бармен")
    assert "Бармен" not in position_registry.production_payroll_positions()

    async with async_session_factory() as session:
        barmen = (
            await session.scalars(select(Position).where(Position.name == "Бармен"))
        ).one()
        assert barmen.iiko_role_id == "role-barmen"


def test_generate_iiko_role_code_translit_and_unique() -> None:
    from app.services.position_service import generate_iiko_role_code

    code = generate_iiko_role_code("Бариста", set())
    assert code == "BARISTA"
    assert generate_iiko_role_code("Бариста", {"BARISTA"}) == "BARISTA_2"


async def test_create_with_access_toggle_creates_engine_role(
    edit_client: TestClient,
    fake_iiko: dict[str, list],
    async_session_factory,
) -> None:
    response = edit_client.post(
        "/api/v1/settings/positions",
        json={
            "name": "Шеф-повар",
            "archetype": "production_percent",
            "permission_group": "cooks",
            "participates_in_access": True,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["participates_in_access"] is True
    assert body["access_role_code"] and body["access_role_code"].startswith("pos_")

    # Под капотом заведена роль-движок прав с именем должности.
    async with async_session_factory() as session:
        role = await session.scalar(select(Role).where(Role.code == body["access_role_code"]))
        assert role is not None
        assert role.name == "Шеф-повар"


def test_position_without_access_toggle_has_no_engine_role(
    edit_client: TestClient, fake_iiko: dict[str, list]
) -> None:
    response = edit_client.post(
        "/api/v1/settings/positions",
        json={"name": "Грузчик", "archetype": "none", "participates_in_access": False},
    )
    assert response.status_code == 201
    assert response.json()["access_role_code"] is None


def test_senior_courier_is_courier_for_payroll_selections() -> None:
    # «Старший курьер» — тоже курьер (архетип courier), участвует в доступах.
    assert "Старший курьер" in position_registry.courier_positions()
    assert "Курьер" in position_registry.courier_positions()
    assert position_registry.access_role_code_for_position("Старший курьер") == "senior_courier"
    assert position_registry.access_role_code_for_position("Курьер") is None


def test_senior_courier_is_admin_okladnik_but_not_production() -> None:
    # Курьер-окладник: получает админ-оклад, оставаясь курьером; в производство не входит.
    assert "Старший курьер" in position_registry.admin_payroll_positions()
    assert "Старший курьер" in position_registry.okladnik_positions()
    assert "Старший курьер" in position_registry.courier_positions()
    assert "Старший курьер" not in position_registry.production_payroll_positions()
    # Обычный курьер админ-оклад не получает.
    assert "Курьер" not in position_registry.admin_payroll_positions()
    assert "Курьер" not in position_registry.okladnik_positions()

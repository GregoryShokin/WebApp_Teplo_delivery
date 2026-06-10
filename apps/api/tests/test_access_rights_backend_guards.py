from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import create_access_token
from app.models import (
    Employee,
    EmployeePositionAssignment,
    Organization,
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
)


def test_courier_schedule_read_and_edit_are_independent(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    read_headers = _headers_for_permissions(
        async_session_factory,
        "courier-schedule-read@test.local",
        ["couriers.schedule.read"],
    )
    edit_headers = _headers_for_permissions(
        async_session_factory,
        "courier-schedule-edit@test.local",
        ["couriers.schedule.edit"],
    )

    allowed_read = client.get(
        "/api/v1/couriers/schedule",
        params={"from": "2026-06-01", "to": "2026-06-01"},
        headers=read_headers,
    )
    denied_write = client.put(
        f"/api/v1/couriers/{uuid.uuid4()}/schedule/2026-06-01",
        json={"category": "primary"},
        headers=read_headers,
    )
    denied_read = client.get(
        "/api/v1/couriers/schedule",
        params={"from": "2026-06-01", "to": "2026-06-01"},
        headers=edit_headers,
    )

    assert allowed_read.status_code == 200
    assert denied_write.status_code == 403
    assert denied_read.status_code == 403


def test_courier_deposit_read_edit_and_configure_are_independent(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    read_headers = _headers_for_permissions(
        async_session_factory,
        "courier-deposit-read@test.local",
        ["couriers.deposits.read"],
    )
    edit_headers = _headers_for_permissions(
        async_session_factory,
        "courier-deposit-edit@test.local",
        ["couriers.deposits.edit"],
    )
    configure_headers = _headers_for_permissions(
        async_session_factory,
        "courier-deposit-configure@test.local",
        ["couriers.deposits.configure"],
    )

    allowed_read = client.get("/api/v1/couriers/deposits", headers=read_headers)
    denied_edit = client.post(
        f"/api/v1/couriers/{uuid.uuid4()}/deposit/transactions",
        json={
            "transaction_type": "top_up",
            "amount_cents": 1000,
            "transaction_date": "2026-06-01",
            "actor_id": str(uuid.uuid4()),
        },
        headers=read_headers,
    )
    denied_read = client.get("/api/v1/couriers/deposits", headers=edit_headers)
    allowed_configure = client.get(
        "/api/v1/couriers/deposits/settings",
        headers=configure_headers,
    )
    denied_configure = client.put(
        "/api/v1/couriers/deposits/settings",
        json={"target_amount": 500000},
        headers=read_headers,
    )

    assert allowed_read.status_code == 200
    assert denied_edit.status_code == 403
    assert denied_read.status_code == 403
    assert allowed_configure.status_code == 200
    assert denied_configure.status_code == 403


def test_payroll_finalize_is_separate_from_read_and_calculate(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    read_headers = _headers_for_permissions(
        async_session_factory,
        "payroll-runs-read@test.local",
        ["payroll.runs.read"],
    )
    start_headers = _headers_for_permissions(
        async_session_factory,
        "payroll-runs-start@test.local",
        ["payroll.runs.start"],
    )
    finalize_headers = _headers_for_permissions(
        async_session_factory,
        "payroll-runs-finalize@test.local",
        ["payroll.runs.finalize"],
    )

    allowed_read = client.get("/api/v1/payroll/runs", headers=read_headers)
    denied_finalize_without_finalize = client.post(
        f"/api/v1/payroll/runs/{uuid.uuid4()}/finalize",
        headers=start_headers,
    )
    denied_read_with_finalize_only = client.get(
        "/api/v1/payroll/runs",
        headers=finalize_headers,
    )
    denied_calculate_with_finalize_only = client.post(
        "/api/v1/payroll/runs",
        json={},
        headers=finalize_headers,
    )

    assert allowed_read.status_code == 200
    assert denied_finalize_without_finalize.status_code == 403
    assert denied_read_with_finalize_only.status_code == 403
    assert denied_calculate_with_finalize_only.status_code == 403


def test_bonus_and_penalty_permissions_are_separate(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    bonus_headers = _headers_for_permissions(
        async_session_factory,
        "payroll-bonus@test.local",
        ["payroll.bonuses.add"],
    )
    penalty_headers = _headers_for_permissions(
        async_session_factory,
        "payroll-penalty@test.local",
        ["payroll.penalties.add"],
    )
    base_payload = {
        "employee_id": str(uuid.uuid4()),
        "work_date": "2026-06-01",
        "category_id": str(uuid.uuid4()),
        "amount": "100.00",
    }

    denied_penalty = client.post(
        "/api/v1/payroll/adjustments",
        json={**base_payload, "type": "penalty"},
        headers=bonus_headers,
    )
    denied_bonus = client.post(
        "/api/v1/payroll/adjustments",
        json={**base_payload, "type": "bonus"},
        headers=penalty_headers,
    )

    assert denied_penalty.status_code == 403
    assert denied_bonus.status_code == 403


def test_admin_payroll_run_start_separate_from_production(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    production_headers = _headers_for_permissions(
        async_session_factory,
        "payroll-prod-start@test.local",
        ["payroll.runs.start"],
    )
    admin_headers = _headers_for_permissions(
        async_session_factory,
        "payroll-admin-start@test.local",
        ["payroll.runs.admin.start"],
    )

    # Производственное право на запуск НЕ даёт создавать ведомости администрации.
    denied = client.post("/api/v1/payroll/admin/runs", json={}, headers=production_headers)
    allowed = client.post("/api/v1/payroll/admin/runs", json={}, headers=admin_headers)

    assert denied.status_code == 403
    assert allowed.status_code != 403


def test_dishwasher_schedule_edit_separate_from_schedule_edit(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    schedule_headers = _headers_for_permissions(
        async_session_factory,
        "schedule-edit@test.local",
        ["source.schedule.edit"],
    )
    dishwasher_headers = _headers_for_permissions(
        async_session_factory,
        "dishwasher-edit@test.local",
        ["source.schedule.dishwashers.edit"],
    )
    body = {
        "employee_id": str(uuid.uuid4()),
        "work_date": "2026-12-25",
        "worked": False,
    }

    # Право на график смен НЕ даёт редактировать график мойщиц.
    denied = client.put("/api/v1/payroll/admin/dishwasher/shifts", json=body, headers=schedule_headers)
    allowed = client.put(
        "/api/v1/payroll/admin/dishwasher/shifts", json=body, headers=dishwasher_headers
    )

    assert denied.status_code == 403
    assert allowed.status_code != 403


def test_admin_adjustment_permission_separate_from_production(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin_employee_id = _create_admin_employee(async_session_factory)
    production_headers = _headers_for_permissions(
        async_session_factory,
        "payroll-bonus-prod@test.local",
        ["payroll.bonuses.add"],
    )
    admin_headers = _headers_for_permissions(
        async_session_factory,
        "payroll-bonus-admin@test.local",
        ["payroll.bonuses.admin.add"],
    )
    payload = {
        "employee_id": str(admin_employee_id),
        "work_date": "2026-12-25",
        "type": "bonus",
        "custom_label": "Тест",
        "amount": "100.00",
    }

    # Производственное право на премии НЕ покрывает админ-персонал...
    denied = client.post("/api/v1/payroll/adjustments", json=payload, headers=production_headers)
    # ...а админское — покрывает.
    allowed = client.post("/api/v1/payroll/adjustments", json=payload, headers=admin_headers)

    assert denied.status_code == 403
    assert allowed.status_code != 403


def _create_admin_employee(session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    return asyncio.run(_create_admin_employee_async(session_factory))


async def _create_admin_employee_async(
    session_factory: async_sessionmaker[AsyncSession],
) -> uuid.UUID:
    async with session_factory() as session:
        employee = Employee(
            id=uuid.uuid4(),
            full_name="Админ Гвард",
            iiko_id=f"guard-admin-{uuid.uuid4()}",
            status="active",
            is_senior=False,
            is_deputy_senior=False,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        session.add(employee)
        await session.flush()
        session.add(
            EmployeePositionAssignment(
                id=uuid.uuid4(),
                employee_id=employee.id,
                position="Управляющий",
                effective_from=date(2026, 1, 1),
                effective_to=None,
            )
        )
        await session.commit()
        return employee.id


def test_dds_cashflow_rules_connections_and_wallets_are_independent(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    cashflow_read_headers = _headers_for_permissions(
        async_session_factory,
        "dds-cashflow-read@test.local",
        ["finance.cashflow.read"],
    )
    cashflow_edit_headers = _headers_for_permissions(
        async_session_factory,
        "dds-cashflow-edit@test.local",
        ["finance.cashflow.edit"],
    )
    rules_headers = _headers_for_permissions(
        async_session_factory,
        "dds-rules@test.local",
        ["finance.classification_rules.manage"],
    )
    integrations_headers = _headers_for_permissions(
        async_session_factory,
        "dds-integrations@test.local",
        ["finance.cashflow.integrations.manage"],
    )
    wallets_headers = _headers_for_permissions(
        async_session_factory,
        "dds-wallets@test.local",
        ["finance.wallets.read"],
    )

    assert client.get("/api/v1/dds/cashflow", headers=cashflow_read_headers).status_code == 200
    assert client.get("/api/v1/dds/credentials", headers=cashflow_read_headers).status_code == 403
    assert client.get("/api/v1/dds/credentials", headers=cashflow_edit_headers).status_code == 403
    assert client.get("/api/v1/dds/credentials", headers=integrations_headers).status_code == 200
    assert client.get("/api/v1/dds/classification-rules", headers=rules_headers).status_code == 200
    assert client.get("/api/v1/dds/wallets", headers=cashflow_read_headers).status_code == 403
    assert client.get("/api/v1/dds/wallets", headers=wallets_headers).status_code == 200


def test_schedule_revenue_and_cost_data_are_not_opened_by_schedule_read(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    schedule_read_headers = _headers_for_permissions(
        async_session_factory,
        "source-schedule-read@test.local",
        ["source.schedule.read"],
    )
    revenue_read_headers = _headers_for_permissions(
        async_session_factory,
        "source-revenue-read@test.local",
        ["source.revenue.read"],
    )
    source_rates_read_headers = _headers_for_permissions(
        async_session_factory,
        "source-rates-read@test.local",
        ["source.rates.read"],
    )

    denied_forecast = client.get(
        "/api/v1/schedule/forecast",
        params={"date_from": "2026-06-01", "date_to": "2026-06-01"},
        headers=schedule_read_headers,
    )
    allowed_forecast = client.get(
        "/api/v1/schedule/forecast",
        params={"date_from": "2026-06-01", "date_to": "2026-06-01"},
        headers=revenue_read_headers,
    )
    denied_cost = client.get(
        f"/api/v1/schedule/{uuid.uuid4()}/cost-forecast/latest",
        headers=schedule_read_headers,
    )
    allowed_cost_after_permission = client.get(
        f"/api/v1/schedule/{uuid.uuid4()}/cost-forecast/latest",
        headers=source_rates_read_headers,
    )

    assert denied_forecast.status_code == 403
    assert allowed_forecast.status_code == 200
    assert denied_cost.status_code == 403
    assert allowed_cost_after_permission.status_code == 404


def test_settings_read_and_edit_are_permission_based(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    no_settings_headers = _headers_for_permissions(
        async_session_factory,
        "no-settings@test.local",
        ["couriers.list.read"],
    )
    settings_read_headers = _headers_for_permissions(
        async_session_factory,
        "settings-read@test.local",
        ["settings.general.read"],
    )
    settings_edit_headers = _headers_for_permissions(
        async_session_factory,
        "settings-edit@test.local",
        ["settings.general.edit"],
    )

    assert client.get("/api/v1/settings", headers=no_settings_headers).status_code == 403
    assert client.get("/api/v1/settings", headers=settings_read_headers).status_code == 200
    assert (
        client.put(
            "/api/v1/settings/unknown-test-key",
            json={"value": "x"},
            headers=settings_read_headers,
        ).status_code
        == 403
    )
    assert (
        client.put(
            "/api/v1/settings/unknown-test-key",
            json={"value": "x"},
            headers=settings_edit_headers,
        ).status_code
        == 404
    )


def _headers_for_permissions(
    session_factory: async_sessionmaker[AsyncSession],
    email: str,
    permission_codes: list[str],
) -> dict[str, str]:
    user_id = asyncio.run(_create_user_with_permissions(session_factory, email, permission_codes))
    return {"Authorization": f"Bearer {create_access_token(str(user_id))}"}


async def _create_user_with_permissions(
    session_factory: async_sessionmaker[AsyncSession],
    email: str,
    permission_codes: list[str],
) -> uuid.UUID:
    async with session_factory() as session:
        existing_user_id = await session.scalar(select(User.id).where(User.email == email))
        if existing_user_id is not None:
            return existing_user_id

        organization_id = await session.scalar(select(Organization.id).limit(1))
        assert organization_id is not None
        permissions = (
            await session.scalars(select(Permission).where(Permission.code.in_(permission_codes)))
        ).all()
        assert {permission.code for permission in permissions} == set(permission_codes)

        role = Role(
            code=f"test_{uuid.uuid4().hex[:24]}",
            name=f"Test role {email}",
        )
        user = User(
            email=email,
            full_name=email,
            hashed_password="sha256$unused",
            is_active=True,
        )
        session.add_all([role, user])
        await session.flush()
        for permission in permissions:
            session.add(RolePermission(role_id=role.id, permission_id=permission.id))
        session.add(
            UserRole(
                user_id=user.id,
                role_id=role.id,
                organization_id=organization_id,
            )
        )
        await session.commit()
        return user.id

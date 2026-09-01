"""API-тесты авансов: доступное, выдача, гейт права B (заём), 403 без прав.

Авторизация — через `X-User-Role` (header-роли в local-окружении): owner = full
access (вкл. займы), manager = право A без займов, cashier = без прав на авансы.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Employee, EmployeePositionAssignment, PayrollRate, Wallet

AS_OF = "2026-05-05"  # первая половина мая, 5/15 → 15000 из оклада 90000
# «Сегодня» эндпоинта — по Москве; тесты дат «сегодня/завтра» считают в тех же сутках.
_MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def _seed_okladnik(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async def _run() -> uuid.UUID:
        async with factory() as session:
            employee = Employee(
                id=uuid.uuid4(),
                full_name="API Окладник",
                iiko_id=f"iiko-{uuid.uuid4()}",
                status="active",
                is_senior=False,
                is_deputy_senior=False,
                hire_date=None,
                fire_date=None,
                pin_hash="hashed-pin",
                pin_set_at=datetime(2026, 1, 1, tzinfo=UTC),
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
            session.add(
                PayrollRate(
                    id=uuid.uuid4(),
                    employee_id=None,
                    position_group="Управляющий",
                    category="admin",
                    station=None,
                    rate_type="monthly",
                    amount=Decimal("90000"),
                    is_active=True,
                    effective_from=date(2026, 1, 1),
                )
            )
            await session.commit()
            return employee.id

    return asyncio.run(_run())


def _seed_cash_wallet(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async def _run() -> uuid.UUID:
        async with factory() as session:
            wallet = Wallet(
                id=uuid.uuid4(),
                code=f"advance-api-{uuid.uuid4().hex[:8]}",
                name="Счёт выдачи API-тест",
                type="cash",
                status="active",
                opening_balance=Decimal("0"),
            )
            session.add(wallet)
            await session.commit()
            return wallet.id

    return asyncio.run(_run())


def test_availability_endpoint_for_owner(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    employee_id = _seed_okladnik(async_session_factory)
    resp = client.get(
        f"/api/v1/payroll/advances/availability?employee_id={employee_id}&as_of={AS_OF}",
        headers={"X-User-Role": "owner"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["basis"] == "okladnik"
    assert body["available"] == 15000.0


def test_availability_forbidden_for_cashier(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    employee_id = _seed_okladnik(async_session_factory)
    resp = client.get(
        f"/api/v1/payroll/advances/availability?employee_id={employee_id}&as_of={AS_OF}",
        headers={"X-User-Role": "cashier"},
    )
    assert resp.status_code == 403


def test_manager_issues_advance_within_earned(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    employee_id = _seed_okladnik(async_session_factory)
    resp = client.post(
        "/api/v1/payroll/advances",
        headers={"X-User-Role": "manager"},
        json={
            "employee_id": str(employee_id),
            "amount": "10000",
            "issued_on": AS_OF,
            "payout_method": "transfer",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["kind"] == "advance"
    assert body["installments_count"] == 1


def test_manager_cannot_issue_loan_over_earned(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    employee_id = _seed_okladnik(async_session_factory)
    # Доступно 15000; 20000 сверху = заём, у управляющего нет права на займы → 409.
    resp = client.post(
        "/api/v1/payroll/advances",
        headers={"X-User-Role": "manager"},
        json={"employee_id": str(employee_id), "amount": "20000", "issued_on": AS_OF},
    )
    assert resp.status_code == 409


def test_owner_issues_loan_with_installments(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    employee_id = _seed_okladnik(async_session_factory)
    wallet_id = _seed_cash_wallet(async_session_factory)
    resp = client.post(
        "/api/v1/payroll/advances",
        headers={"X-User-Role": "owner"},
        json={
            "employee_id": str(employee_id),
            "amount": "20000",
            "kind": "loan",
            "installment_amount": "5000",
            "recovery_start_date": "2026-05-20",
            "issued_on": AS_OF,
            "payout_method": "cash",
            "wallet_id": str(wallet_id),
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["kind"] == "loan"
    assert body["installments_count"] == 4
    assert body["per_installment_amount"] == 5000.0


def test_owner_issues_explicit_loan_within_earned(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    employee_id = _seed_okladnik(async_session_factory)
    wallet_id = _seed_cash_wallet(async_session_factory)
    # Доступно 15000; просим 10000 ЯВНЫМ займом с долей 5000 и отложенным удержанием.
    resp = client.post(
        "/api/v1/payroll/advances",
        headers={"X-User-Role": "owner"},
        json={
            "employee_id": str(employee_id),
            "amount": "10000",
            "kind": "loan",
            "installment_amount": "5000",
            "recovery_start_date": "2026-05-20",
            "issued_on": AS_OF,
            "wallet_id": str(wallet_id),
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["kind"] == "loan"
    assert body["installments_count"] == 2
    assert body["per_installment_amount"] == 5000.0
    assert body["recovery_start_date"] == "2026-05-20"


def test_manager_cannot_issue_explicit_loan(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    employee_id = _seed_okladnik(async_session_factory)
    wallet_id = _seed_cash_wallet(async_session_factory)
    # Явный заём в пределах заработанного, но у управляющего нет права на займы → 409.
    resp = client.post(
        "/api/v1/payroll/advances",
        headers={"X-User-Role": "manager"},
        json={
            "employee_id": str(employee_id),
            "amount": "10000",
            "kind": "loan",
            "issued_on": AS_OF,
            "installment_amount": "5000",
            "recovery_start_date": "2026-05-20",
            "wallet_id": str(wallet_id),
        },
    )
    assert resp.status_code == 409


def test_owner_loan_over_ceiling_blocked_then_override(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    employee_id = _seed_okladnik(async_session_factory)
    # 150000 > дефолтный потолок 100000 → 409 без подтверждения.
    blocked = client.post(
        "/api/v1/payroll/advances",
        headers={"X-User-Role": "owner"},
        json={"employee_id": str(employee_id), "amount": "150000", "issued_on": AS_OF},
    )
    assert blocked.status_code == 409
    # С подтверждением превышения → 201.
    allowed = client.post(
        "/api/v1/payroll/advances",
        headers={"X-User-Role": "owner"},
        json={
            "employee_id": str(employee_id),
            "amount": "150000",
            "override_ceiling": True,
            "issued_on": AS_OF,
        },
    )
    assert allowed.status_code == 201
    assert allowed.json()["kind"] == "loan"


def test_put_config_changes_loan_ceiling(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    employee_id = _seed_okladnik(async_session_factory)
    cfg = client.put(
        "/api/v1/payroll/advances/config",
        headers={"X-User-Role": "owner"},
        json={"loan_max": "200000"},
    )
    assert cfg.status_code == 200
    assert cfg.json()["loan_max"] == 200000.0
    # Теперь 150000 проходит без подтверждения.
    resp = client.post(
        "/api/v1/payroll/advances",
        headers={"X-User-Role": "owner"},
        json={"employee_id": str(employee_id), "amount": "150000", "issued_on": AS_OF},
    )
    assert resp.status_code == 201


def test_put_config_forbidden_for_manager(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    resp = client.put(
        "/api/v1/payroll/advances/config",
        headers={"X-User-Role": "manager"},
        json={"loan_max": "200000"},
    )
    assert resp.status_code == 403


def test_future_date_rejected(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    employee_id = _seed_okladnik(async_session_factory)
    tomorrow = (datetime.now(_MOSCOW_TZ).date() + timedelta(days=1)).isoformat()
    resp = client.post(
        "/api/v1/payroll/advances",
        headers={"X-User-Role": "owner"},
        json={"employee_id": str(employee_id), "amount": "5000", "issued_on": tomorrow},
    )
    assert resp.status_code == 422


def test_today_date_allowed_without_backdate(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    employee_id = _seed_okladnik(async_session_factory)
    wallet_id = _seed_cash_wallet(async_session_factory)
    today_date = datetime.now(_MOSCOW_TZ).date()
    today = today_date.isoformat()
    # Заём (не аванс): проверяем ровно ветку разрешения даты «сегодня» без права
    # backdate. Заём отсечкой «день выплаты» не блокируется, поэтому тест устойчив к
    # тому, что «сегодня» может оказаться днём выплаты (15-е/1-е).
    resp = client.post(
        "/api/v1/payroll/advances",
        headers={"X-User-Role": "owner"},
        json={
            "employee_id": str(employee_id),
            "amount": "5000",
            "issued_on": today,
            "kind": "loan",
            "installment_amount": "1000",
            "recovery_start_date": (today_date + timedelta(days=1)).isoformat(),
            "wallet_id": str(wallet_id),
        },
    )
    assert resp.status_code == 201


def test_explicit_loan_requires_issue_and_recovery_terms(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    employee_id = _seed_okladnik(async_session_factory)
    wallet_id = _seed_cash_wallet(async_session_factory)
    complete = {
        "employee_id": str(employee_id),
        "amount": "10000",
        "kind": "loan",
        "issued_on": AS_OF,
        "installment_amount": "2000",
        "recovery_start_date": "2026-05-20",
        "wallet_id": str(wallet_id),
    }

    for missing in ("issued_on", "installment_amount", "recovery_start_date", "wallet_id"):
        payload = {key: value for key, value in complete.items() if key != missing}
        resp = client.post(
            "/api/v1/payroll/advances",
            headers={"X-User-Role": "owner"},
            json=payload,
        )
        assert resp.status_code == 422, missing

    invalid_start = {**complete, "recovery_start_date": AS_OF}
    resp = client.post(
        "/api/v1/payroll/advances",
        headers={"X-User-Role": "owner"},
        json=invalid_start,
    )
    assert resp.status_code == 422


def test_future_payroll_payslip_date_is_allowed_for_explicit_loan(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    employee_id = _seed_okladnik(async_session_factory)
    today = datetime.now(_MOSCOW_TZ).date()
    payslips = client.get(
        f"/api/v1/payroll/advances/upcoming-payslips?employee_id={employee_id}&count=6",
        headers={"X-User-Role": "owner"},
    )
    assert payslips.status_code == 200
    future_payouts = [
        date.fromisoformat(item["payout_date"])
        for item in payslips.json()
        if date.fromisoformat(item["payout_date"]) > today
    ]
    assert future_payouts
    issued_on = future_payouts[0]

    resp = client.post(
        "/api/v1/payroll/advances",
        headers={"X-User-Role": "owner"},
        json={
            "employee_id": str(employee_id),
            "amount": "10000",
            "kind": "loan",
            "issued_on": issued_on.isoformat(),
            "installment_amount": "2000",
            "recovery_start_date": (issued_on + timedelta(days=1)).isoformat(),
            "payout_method": "payroll",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["issued_on"] == issued_on.isoformat()


def test_backdate_forbidden_without_permission(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    employee_id = _seed_okladnik(async_session_factory)
    # office_manager имеет право A (выдача), но НЕ имеет payroll.advances.backdate.
    resp = client.post(
        "/api/v1/payroll/advances",
        headers={"X-User-Role": "office_manager"},
        json={"employee_id": str(employee_id), "amount": "10000", "issued_on": AS_OF},
    )
    assert resp.status_code == 403


def test_backdate_allowed_for_manager(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    employee_id = _seed_okladnik(async_session_factory)
    # manager (Управляющий) имеет право backdate → прошлая дата разрешена.
    resp = client.post(
        "/api/v1/payroll/advances",
        headers={"X-User-Role": "manager"},
        json={"employee_id": str(employee_id), "amount": "10000", "issued_on": AS_OF},
    )
    assert resp.status_code == 201

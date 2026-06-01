from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from app.db.session import get_session
from app.main import create_app
from app.models import AgentAction, DepositAccount, DepositTransaction, Employee

SYNC_NOW = datetime(2026, 5, 31, 10, 0, tzinfo=UTC)


class FakeScalarResult:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def all(self) -> list[Any]:
        return self._items


class DepositFakeSession:
    def __init__(
        self,
        employee: Employee,
        account: DepositAccount | None = None,
        transactions: list[DepositTransaction] | None = None,
    ) -> None:
        self.employee = employee
        self.accounts: dict[uuid.UUID, DepositAccount] = {}
        if account is not None:
            self.accounts[account.employee_id] = account
        self.transactions = transactions or []
        self.added: list[Any] = []
        self.committed = False

    async def get(self, model: Any, object_id: uuid.UUID) -> Any | None:
        if model is Employee and object_id == self.employee.id:
            return self.employee
        return None

    async def scalar(self, _query: Any) -> DepositAccount | None:
        return self.accounts.get(self.employee.id)

    async def scalars(self, query: Any) -> FakeScalarResult:
        entity = query_entity(query)
        if entity is Employee:
            return FakeScalarResult([self.employee])
        if entity is DepositAccount:
            return FakeScalarResult(list(self.accounts.values()))
        if entity is DepositTransaction:
            return FakeScalarResult(self.transactions)
        return FakeScalarResult([])

    def add(self, item: Any) -> None:
        self.added.append(item)
        if isinstance(item, DepositAccount):
            self.accounts[item.employee_id] = item
        if isinstance(item, DepositTransaction):
            self.transactions.append(item)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, _item: Any) -> None:
        return None


def make_employee() -> Employee:
    return Employee(
        id=uuid.uuid4(),
        full_name="Deposit Employee",
        iiko_id=f"iiko-{uuid.uuid4()}",
        position="Повар",
        category="category_2",
        status="active",
        deposit_excluded=False,
        created_at=SYNC_NOW,
        updated_at=SYNC_NOW,
    )


def query_entity(query: Any) -> Any | None:
    descriptions = getattr(query, "column_descriptions", None) or []
    if not descriptions:
        return None
    return descriptions[0].get("entity")


def make_account(
    employee: Employee,
    balance: Decimal,
    initial_balance: Decimal = Decimal("0"),
) -> DepositAccount:
    return DepositAccount(
        id=uuid.uuid4(),
        employee_id=employee.id,
        balance=balance,
        initial_balance=initial_balance,
        last_updated=SYNC_NOW,
    )


def app_with_session(session: DepositFakeSession):
    app = create_app()

    async def override_session():
        yield session

    app.dependency_overrides[get_session] = override_session
    return app


def test_payout_full_balance_sets_balance_to_zero_and_records_transaction() -> None:
    employee = make_employee()
    session = DepositFakeSession(employee, make_account(employee, Decimal("10000")))

    with TestClient(app_with_session(session)) as client:
        response = client.post(
            f"/api/v1/deposits/{employee.id}/payout",
            headers={"X-User-Role": "finance_manager"},
            json={"amount": "10000", "comment": "Увольнение"},
        )

    assert response.status_code == 200
    assert response.json()["balance"] == "0.00"
    assert session.accounts[employee.id].balance == Decimal("0")
    assert session.transactions[0].transaction_type == "payout"
    assert session.transactions[0].amount == Decimal("10000")
    assert any(
        isinstance(item, AgentAction) and item.action_type == "deposit_payout"
        for item in session.added
    )


def test_payout_above_balance_returns_400() -> None:
    employee = make_employee()
    session = DepositFakeSession(employee, make_account(employee, Decimal("5000")))

    with TestClient(app_with_session(session)) as client:
        response = client.post(
            f"/api/v1/deposits/{employee.id}/payout",
            headers={"X-User-Role": "finance_manager"},
            json={"amount": "10000"},
        )

    assert response.status_code == 400
    assert session.accounts[employee.id].balance == Decimal("5000")
    assert session.transactions == []


def test_writeoff_reduces_balance_and_records_transaction() -> None:
    employee = make_employee()
    session = DepositFakeSession(employee, make_account(employee, Decimal("5000")))

    with TestClient(app_with_session(session)) as client:
        response = client.post(
            f"/api/v1/deposits/{employee.id}/writeoff",
            headers={"X-User-Role": "finance_manager"},
            json={"amount": "2000", "reason": "Недостача"},
        )

    assert response.status_code == 200
    assert response.json()["balance"] == "3000.00"
    assert session.accounts[employee.id].balance == Decimal("3000")
    assert session.transactions[0].transaction_type == "write_off"
    assert session.transactions[0].amount == Decimal("2000")


def test_initial_balance_creates_account_and_accrual_transaction() -> None:
    employee = make_employee()
    session = DepositFakeSession(employee)

    with TestClient(app_with_session(session)) as client:
        response = client.post(
            f"/api/v1/deposits/{employee.id}/initial-balance",
            headers={"X-User-Role": "finance_manager"},
            json={"amount": "7000"},
        )

    assert response.status_code == 200
    assert response.json()["balance"] == "7000.00"
    assert session.accounts[employee.id].balance == Decimal("7000")
    assert session.accounts[employee.id].initial_balance == Decimal("7000")
    assert session.transactions[0].transaction_type == "accrual"
    assert session.transactions[0].amount == Decimal("7000")
    initial_audit = next(
        item
        for item in session.added
        if isinstance(item, AgentAction) and item.action_type == "deposit_initial_balance"
    )
    assert initial_audit.after_value["initial"] is True


def test_initial_balance_second_call_returns_409() -> None:
    employee = make_employee()
    session = DepositFakeSession(
        employee,
        make_account(employee, Decimal("7000"), initial_balance=Decimal("7000")),
    )

    with TestClient(app_with_session(session)) as client:
        response = client.post(
            f"/api/v1/deposits/{employee.id}/initial-balance",
            headers={"X-User-Role": "finance_manager"},
            json={"amount": "8000"},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Исторический баланс уже установлен. Используйте корректировку через ручную транзакцию."
    )
    assert session.accounts[employee.id].balance == Decimal("7000")
    assert session.accounts[employee.id].initial_balance == Decimal("7000")
    assert session.transactions == []


def test_initial_balance_legacy_positive_balance_keeps_current_balance() -> None:
    employee = make_employee()
    session = DepositFakeSession(
        employee,
        make_account(employee, Decimal("9000"), initial_balance=Decimal("0")),
    )

    with TestClient(app_with_session(session)) as client:
        response = client.post(
            f"/api/v1/deposits/{employee.id}/initial-balance",
            headers={"X-User-Role": "finance_manager"},
            json={"amount": "7000"},
        )

    assert response.status_code == 200
    assert response.json()["balance"] == "9000.00"
    assert session.accounts[employee.id].balance == Decimal("9000")
    assert session.accounts[employee.id].initial_balance == Decimal("7000")
    assert session.transactions[0].transaction_type == "accrual"


def test_list_deposits_includes_initial_balance() -> None:
    employee = make_employee()
    session = DepositFakeSession(
        employee,
        make_account(employee, Decimal("9000"), initial_balance=Decimal("7000")),
    )

    with TestClient(app_with_session(session)) as client:
        response = client.get(
            "/api/v1/deposits",
            headers={"X-User-Role": "finance_manager"},
        )

    assert response.status_code == 200
    rows = response.json()
    assert rows[0]["balance"] == "9000.00"
    assert rows[0]["initial_balance"] == "7000.00"

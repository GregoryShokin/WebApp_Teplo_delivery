from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from app.models import (
    CourierDepositAccount,
    CourierDepositTransaction,
    CourierDepositTransactionType,
)
from app.services.couriers import deposit_service


class FakeScalarResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class DepositBalanceSession:
    def __init__(
        self,
        account: CourierDepositAccount,
        transactions: list[CourierDepositTransaction],
    ) -> None:
        self.account = account
        self.transactions = transactions

    async def get(self, model: Any, object_id: uuid.UUID) -> Any | None:
        if model is CourierDepositAccount and object_id == self.account.employee_id:
            return self.account
        return None

    async def scalars(self, _query: Any) -> FakeScalarResult:
        return FakeScalarResult(self.transactions)


async def test_deposit_balance_uses_all_three_transaction_types() -> None:
    employee_id = uuid.uuid4()
    account = CourierDepositAccount(
        employee_id=employee_id,
        target_amount_cents=500_000,
        opening_balance_cents=100_000,
        opening_date=date(2026, 6, 1),
    )
    transactions = [
        CourierDepositTransaction(
            account_employee_id=employee_id,
            transaction_type=CourierDepositTransactionType.TOP_UP,
            amount_cents=50_000,
            transaction_date=date(2026, 6, 2),
            created_by=uuid.uuid4(),
        ),
        CourierDepositTransaction(
            account_employee_id=employee_id,
            transaction_type=CourierDepositTransactionType.RETURN,
            amount_cents=20_000,
            transaction_date=date(2026, 6, 3),
            created_by=uuid.uuid4(),
        ),
        CourierDepositTransaction(
            account_employee_id=employee_id,
            transaction_type=CourierDepositTransactionType.FORFEIT,
            amount_cents=10_000,
            transaction_date=date(2026, 6, 4),
            created_by=uuid.uuid4(),
        ),
    ]

    balance = await deposit_service.get_balance(
        DepositBalanceSession(account, transactions),
        employee_id,
        at_date=date(2026, 6, 30),
    )

    assert balance == 120_000

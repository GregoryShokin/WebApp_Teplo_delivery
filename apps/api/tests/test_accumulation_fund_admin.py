from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi import HTTPException

from app.api.deps import CurrentActor
from app.api.v1.routes import accumulation_fund as fund_routes
from app.models import (
    AccumulationFundAccount,
    AccumulationFundTransaction,
    AgentAction,
    AppSetting,
    Employee,
)


class FundAdminFakeSession:
    def __init__(self, setting: AppSetting | None = None) -> None:
        self.setting = setting
        self.added: list[Any] = []
        self.committed = False

    async def scalar(self, query: Any) -> Any | None:
        entity = query_entity(query)
        if entity is AppSetting:
            return self.setting
        return None

    def add(self, item: Any) -> None:
        self.added.append(item)
        if isinstance(item, AppSetting):
            self.setting = item

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


class FundInitialBalanceFakeSession(FundAdminFakeSession):
    def __init__(
        self,
        employee: Employee,
        *,
        account: AccumulationFundAccount | None = None,
        transactions: list[AccumulationFundTransaction] | None = None,
    ) -> None:
        super().__init__(setting([]))
        self.employee = employee
        self.account = account
        self.transactions = transactions or []

    async def scalar(self, query: Any) -> Any | None:
        entity = query_entity(query)
        if entity is Employee:
            return self.employee
        if entity is AccumulationFundAccount:
            return self.account
        return await super().scalar(query)

    async def scalars(self, query: Any) -> FakeScalarResult:
        entity = query_entity(query)
        if entity is AccumulationFundTransaction:
            return FakeScalarResult(self.transactions)
        return FakeScalarResult([])

    def add(self, item: Any) -> None:
        super().add(item)
        if isinstance(item, AccumulationFundAccount):
            self.account = item
        if isinstance(item, AccumulationFundTransaction):
            self.transactions.append(item)


class FundExclusionFakeSession(FundAdminFakeSession):
    def __init__(
        self,
        employee: Employee,
        *,
        account: AccumulationFundAccount | None = None,
    ) -> None:
        super().__init__(setting([]))
        self.employee = employee
        self.account = account
        self.refreshed: list[Any] = []

    async def scalar(self, query: Any) -> Any | None:
        entity = query_entity(query)
        if entity is Employee:
            return self.employee
        return await super().scalar(query)

    async def refresh(self, item: Any) -> None:
        self.refreshed.append(item)


class FundRosterFakeSession(FundAdminFakeSession):
    def __init__(
        self,
        employees: list[Employee],
        *,
        accounts: list[AccumulationFundAccount] | None = None,
        transactions: list[AccumulationFundTransaction] | None = None,
    ) -> None:
        super().__init__(setting([{"min_months": 0, "rate": 0.05}]))
        self.employees = employees
        self.accounts = accounts or []
        self.transactions = transactions or []

    async def scalars(self, query: Any) -> FakeScalarResult:
        entity = query_entity(query)
        if entity is Employee:
            return FakeScalarResult(self.employees)
        if entity is AccumulationFundAccount:
            return FakeScalarResult(self.accounts)
        if entity is AccumulationFundTransaction:
            return FakeScalarResult(self.transactions)
        return FakeScalarResult([])


class FakeScalarResult:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def all(self) -> list[Any]:
        return self._items


def query_entity(query: Any) -> Any | None:
    descriptions = getattr(query, "column_descriptions", None) or []
    if not descriptions:
        return None
    return descriptions[0].get("entity")


def finance_actor() -> CurrentActor:
    return CurrentActor(roles=frozenset({"finance_manager"}))


def setting(value: Any) -> AppSetting:
    return AppSetting(
        id=uuid.uuid4(),
        key=fund_routes.FUND_TIERS_SETTING_KEY,
        value=value,
        value_type="object",
        category="Зарплата",
        display_name="Накопительный фонд",
        description=None,
        widget_type="json",
        widget_options=None,
        unit="%",
        updated_at=datetime(2026, 5, 31, tzinfo=UTC),
    )


def employee() -> Employee:
    return Employee(
        id=uuid.uuid4(),
        full_name="Fund Employee",
        iiko_id=f"iiko-{uuid.uuid4()}",
        position="Повар",
        category="category_2",
        status="active",
        hire_date=date(2025, 4, 15),
    )


async def test_get_fund_tiers_returns_sorted() -> None:
    session = FundAdminFakeSession(
        setting(
            [
                {"min_months": 18, "rate": 0.15},
                {"min_years": 0.5, "rate": 0.05},
                {"min_months": 12, "rate": 0.10},
            ]
        )
    )

    response = await fund_routes.get_fund_tiers(session, finance_actor())  # type: ignore[arg-type]

    assert [tier.min_months for tier in response.tiers] == [6, 12, 18]
    assert [tier.rate for tier in response.tiers] == [
        Decimal("0.05"),
        Decimal("0.10"),
        Decimal("0.15"),
    ]


async def test_put_fund_tiers_persists_and_normalizes() -> None:
    session = FundAdminFakeSession(setting([{"min_months": 6, "rate": 0.05}]))

    response = await fund_routes.put_fund_tiers(
        fund_routes.FundTiersWrite(
            tiers=[
                fund_routes.FundTierItem(min_months=12, rate=0.10),
                fund_routes.FundTierItem(min_months=6, rate=0.05),
            ]
        ),
        session,  # type: ignore[arg-type]
        finance_actor(),
    )

    assert [tier.min_months for tier in response.tiers] == [6, 12]
    assert session.setting is not None
    assert session.setting.value == [
        {"min_months": 6, "rate": 0.05},
        {"min_months": 12, "rate": 0.1},
    ]
    assert any(
        isinstance(item, AgentAction) and item.action_type == "payroll_fund_tiers_update"
        for item in session.added
    )
    assert session.committed is True


async def test_put_fund_tiers_rejects_non_monotonic_rate() -> None:
    session = FundAdminFakeSession(setting([{"min_months": 6, "rate": 0.05}]))

    with pytest.raises(HTTPException) as exc_info:
        await fund_routes.put_fund_tiers(
            fund_routes.FundTiersWrite(
                tiers=[
                    fund_routes.FundTierItem(min_months=6, rate=0.10),
                    fund_routes.FundTierItem(min_months=12, rate=0.05),
                ]
            ),
            session,  # type: ignore[arg-type]
            finance_actor(),
        )

    assert exc_info.value.status_code == 422
    assert session.committed is False


async def test_put_fund_tiers_rejects_duplicate_min_months() -> None:
    session = FundAdminFakeSession(setting([{"min_months": 6, "rate": 0.05}]))

    with pytest.raises(HTTPException) as exc_info:
        await fund_routes.put_fund_tiers(
            fund_routes.FundTiersWrite(
                tiers=[
                    fund_routes.FundTierItem(min_months=6, rate=0.05),
                    fund_routes.FundTierItem(min_months=6, rate=0.10),
                ]
            ),
            session,  # type: ignore[arg-type]
            finance_actor(),
        )

    assert exc_info.value.status_code == 422
    assert session.committed is False


async def test_put_fund_tiers_rejects_empty_array() -> None:
    session = FundAdminFakeSession(setting([{"min_months": 6, "rate": 0.05}]))

    with pytest.raises(HTTPException) as exc_info:
        await fund_routes.put_fund_tiers(
            fund_routes.FundTiersWrite(tiers=[]),
            session,  # type: ignore[arg-type]
            finance_actor(),
        )

    assert exc_info.value.status_code == 422
    assert session.committed is False


async def test_set_fund_initial_balance_creates_account_transaction_and_audit() -> None:
    target = employee()
    session = FundInitialBalanceFakeSession(target)

    response = await fund_routes.set_fund_initial_balance(
        target.id,
        fund_routes.FundInitialBalanceRequest(amount=Decimal("7000")),
        session,  # type: ignore[arg-type]
        finance_actor(),
    )

    assert response.employee_id == target.id
    assert response.year == date.today().year
    assert response.accumulated == "7000.00"
    assert session.account is not None
    assert session.account.accumulated_amount == Decimal("7000.00")
    assert session.transactions[0].transaction_type == "initial_balance"
    assert session.transactions[0].amount == Decimal("7000.00")
    assert any(
        isinstance(item, AgentAction) and item.action_type == "fund_initial_balance"
        for item in session.added
    )
    assert session.committed is True


async def test_existing_accumulated_not_touched_on_exclusion() -> None:
    target = employee()
    account = AccumulationFundAccount(
        id=uuid.uuid4(),
        employee_id=target.id,
        year=2026,
        accumulated_amount=Decimal("5000"),
        paid_out_amount=Decimal("0"),
        forfeited_amount=Decimal("0"),
        status="active",
    )
    session = FundExclusionFakeSession(target, account=account)

    response = await fund_routes.patch_fund_exclusion(
        target.id,
        fund_routes.FundExclusionPatch(fund_excluded=True),
        session,  # type: ignore[arg-type]
        finance_actor(),
    )

    assert response.is_currently_excluded is True
    assert account.accumulated_amount == Decimal("5000")
    assert account.status == "active"
    assert any(
        isinstance(item, AgentAction) and item.action_type == "fund_exclusion_update"
        for item in session.added
    )


async def test_patch_exclusion_clears_until_and_reason_when_disabling() -> None:
    target = employee()
    session = FundExclusionFakeSession(target)

    await fund_routes.patch_fund_exclusion(
        target.id,
        fund_routes.FundExclusionPatch(
            fund_excluded=True,
            fund_excluded_until=date(2026, 8, 31),
            fund_excluded_reason="Отпуск",
        ),
        session,  # type: ignore[arg-type]
        finance_actor(),
    )
    response = await fund_routes.patch_fund_exclusion(
        target.id,
        fund_routes.FundExclusionPatch(fund_excluded=False),
        session,  # type: ignore[arg-type]
        finance_actor(),
    )

    assert response.fund_excluded is False
    assert response.fund_excluded_until is None
    assert response.fund_excluded_reason is None
    assert target.fund_excluded_until is None
    assert target.fund_excluded_reason is None


async def test_patch_exclusion_for_non_payroll_position_fails_422() -> None:
    target = employee()
    target.position = "Управляющий"
    session = FundExclusionFakeSession(target)

    with pytest.raises(HTTPException) as exc_info:
        await fund_routes.patch_fund_exclusion(
            target.id,
            fund_routes.FundExclusionPatch(fund_excluded=True),
            session,  # type: ignore[arg-type]
            finance_actor(),
        )

    assert exc_info.value.status_code == 422
    assert session.committed is False


async def test_roster_reflects_exclusion() -> None:
    target = employee()
    target.fund_excluded = True
    target.fund_excluded_until = None
    session = FundRosterFakeSession([target])

    rows = await fund_routes.get_initial_balance_roster(
        session,  # type: ignore[arg-type]
        finance_actor(),
        year=2026,
    )

    assert rows[0].fund_exclusion.fund_excluded is True
    assert rows[0].fund_exclusion.is_currently_excluded is True

"""A stale page cannot fall back to the legacy full-pool payout/transfer."""

import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api.v1.routes import kassa, payroll
from app.schemas.kassa import KassaPayrollPayoutRequest
from app.schemas.payroll import PayrollPoolPayoutRequest, PayrollReserveTransferRequest
from app.services.payroll_reserves import PoolAllocation, PoolPayoutResult, ReserveTransferResult

pytestmark = pytest.mark.asyncio
PAID_AT = date(2026, 10, 6)


def request_for(endpoint, employee, version):
    if endpoint == "cashier":
        return KassaPayrollPayoutRequest(employee_ids=[employee], plan_version=version)
    if endpoint == "transfer":
        return PayrollReserveTransferRequest(
            selected_ids=[employee], operation_date=PAID_AT, plan_version=version
        )
    return PayrollPoolPayoutRequest(
        selected_ids=[employee], paid_at=PAID_AT, allow_overflow=False, plan_version=version
    )


def handler_for(endpoint):
    return {
        "payout": payroll.post_pay_run_from_pool,
        "transfer": payroll.post_transfer_run_reserve,
        "cashier": kassa.pay_kassa_payroll_target_endpoint,
    }[endpoint]


@pytest.mark.parametrize("endpoint", ["payout", "transfer", "cashier"])
@pytest.mark.parametrize("version", [None, "", "   "])
async def test_money_action_requires_current_plan_before_touching_service(
    monkeypatch, endpoint, version
):
    payout = AsyncMock()
    transfer = AsyncMock()
    cashier_payout = AsyncMock()
    monkeypatch.setattr(payroll, "pay_run_from_pool", payout)
    monkeypatch.setattr(payroll, "transfer_planned_reserve", transfer)
    monkeypatch.setattr(kassa, "pay_run_from_pool", cashier_payout)
    # No session or actor: rejection must precede any access to them.
    with pytest.raises(HTTPException) as error:
        await handler_for(endpoint)(
            uuid.uuid4(), request_for(endpoint, uuid.uuid4(), version), None, None
        )
    assert error.value.status_code == 409
    assert "Обновите страницу" in error.value.detail
    assert "остаток остаётся в резерве" in error.value.detail
    payout.assert_not_awaited()
    transfer.assert_not_awaited()
    cashier_payout.assert_not_awaited()


@pytest.mark.parametrize("endpoint", ["payout", "transfer", "cashier"])
async def test_current_plan_version_is_forwarded_for_atomic_validation(monkeypatch, endpoint):
    reserve, employee, actor_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    version = "a" * 64
    actor = SimpleNamespace(user_id=actor_id)
    session = object()
    payout = AsyncMock(return_value=PoolPayoutResult(reserve, Decimal("50"), None, Decimal(0), 1))
    transfer = AsyncMock(
        return_value=ReserveTransferResult(
            reserve,
            uuid.uuid4(),
            uuid.uuid4(),
            Decimal("50"),
            "safe",
            (PoolAllocation(employee, Decimal("50")),),
        )
    )
    monkeypatch.setattr(payroll, "pay_run_from_pool", payout)
    monkeypatch.setattr(payroll, "transfer_planned_reserve", transfer)
    monkeypatch.setattr(kassa, "pay_run_from_pool", payout)
    pending = AsyncMock(return_value={"targets": []})
    monkeypatch.setattr(kassa, "kassa_pending_payload", pending)
    await handler_for(endpoint)(reserve, request_for(endpoint, employee, version), session, actor)
    service = transfer if endpoint == "transfer" else payout
    kwargs = service.await_args.kwargs
    assert service.await_args.args == (session,)
    assert kwargs["reserve_id"] == reserve
    assert kwargs["selected_ids"] == {employee}
    assert kwargs["actor_user_id"] == actor_id
    assert kwargs["expected_version" if endpoint == "transfer" else "plan_version"] == version
    if endpoint != "transfer":
        assert kwargs["allow_overflow"] is False
    if endpoint == "cashier":
        assert kwargs["expected_location"] == "kassa"
        pending.assert_awaited_once_with(session)

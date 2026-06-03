from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.core.config import get_settings
from app.services.banking.sber import SberClient


@pytest.mark.asyncio
async def test_sber_mock_statement_normalizes_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEPLO_BANK_CLIENT_MODE", "mock")
    get_settings.cache_clear()

    operations = await SberClient().fetch_statement(
        date_from=date(2026, 5, 26),
        date_to=date(2026, 5, 27),
    )

    assert len(operations) == 10
    acquiring = operations[0]
    assert acquiring.provider == "sber"
    assert acquiring.provider_operation_id == "sber-20260526-001"
    assert acquiring.account_number == "40702810900000000001"
    assert acquiring.direction == "in"
    assert acquiring.amount == Decimal("250000.00")
    assert acquiring.counterparty_name_raw == "АО СБЕРБАНК ЭКВАЙРИНГ"
    assert "эквайринга" in (acquiring.payment_purpose or "")

    transfer = operations[1]
    assert transfer.direction == "out"
    assert transfer.counterparty_account_raw == "40702810800000000002"

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.core.config import get_settings
from app.services.banking.tbank import TbankClient


@pytest.mark.asyncio
async def test_tbank_mock_statement_normalizes_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEPLO_BANK_CLIENT_MODE", "mock")
    get_settings.cache_clear()

    operations = await TbankClient().fetch_statement(
        date_from=date(2026, 5, 26),
        date_to=date(2026, 5, 27),
    )

    assert len(operations) == 10
    acquiring = operations[0]
    assert acquiring.provider == "tbank"
    assert acquiring.provider_operation_id == "tbank-20260526-001"
    assert acquiring.direction == "in"
    assert acquiring.amount == Decimal("32000.00")
    assert acquiring.counterparty_name_raw == 'АО "ТБанк"'
    assert "Зачисление средств по терминалам эквайринга" in (
        acquiring.payment_purpose or ""
    )

    tax = next(
        operation
        for operation in operations
        if operation.provider_operation_id == "tbank-20260526-003"
    )
    assert tax.direction == "out"
    assert "Налог НДФЛ" in (tax.payment_purpose or "")

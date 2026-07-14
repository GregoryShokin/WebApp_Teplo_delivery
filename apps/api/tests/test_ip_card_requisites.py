"""Safety contract for the owner-approved bank → IP-card recipient."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AppSetting
from app.services.banking.ip_card_requisites import (
    OWNER_APPROVED_IP_CARD_REQUISITES,
    PAYOUT_REQUISITES_KEY,
    load_owner_approved_ip_card_requisites,
    owner_approved_ip_card_requisites,
)
from app.services.banking.tbank import build_payment_draft_api_payload
from app.services.counterparty_payments import _ip_card_requisites
from app.services.deposit_bank_draft import _bank_payout_requisites as deposit_requisites
from app.services.payroll_payouts import _bank_payout_requisites as payroll_requisites

EXPECTED = {
    "recipientName": "Шокина Кристина Юрьевна",
    "inn": "890307589201",
    "bankAcnt": "40817810800023540968",
    "bankBik": "044525974",
    "bankName": 'АО "ТБанк"',
    "corrAccount": "30101810145250000974",
    "recipientCorrAccountNumber": "30101810145250000974",
    "executionOrder": 5,
    "paymentPurpose": "Перевод на Сейф под выплату за период {start}–{end}. НДС не облагается",
}


def test_owner_approved_requisites_are_exact_and_immutable() -> None:
    assert dict(OWNER_APPROVED_IP_CARD_REQUISITES) == EXPECTED
    assert owner_approved_ip_card_requisites() == EXPECTED
    assert "kpp" not in OWNER_APPROVED_IP_CARD_REQUISITES
    with pytest.raises(TypeError):
        OWNER_APPROVED_IP_CARD_REQUISITES["bankAcnt"] = "redirected"  # type: ignore[index]


async def test_all_runtime_loaders_ignore_database_drift(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        setting = await session.scalar(
            select(AppSetting).where(AppSetting.key == PAYOUT_REQUISITES_KEY)
        )
        assert setting is not None
        setting.value = {
            "recipientName": "Подменённый получатель",
            "inn": "7707083893",
            "kpp": "616143002",
            "bankAcnt": "40817810552095257243",
            "bankBik": "046015602",
            "corrAccount": "30101810600000000602",
        }
        await session.flush()

        assert await load_owner_approved_ip_card_requisites(session) == EXPECTED
        assert await _ip_card_requisites(session) == EXPECTED
        assert await payroll_requisites(session) == EXPECTED
        assert await deposit_requisites(session) == EXPECTED


def test_bank_payload_uses_zero_kpp_without_storing_kpp_in_canonical_data() -> None:
    payload = build_payment_draft_api_payload(
        document_id="owner-approved-requisites-test",
        amount=Decimal("100.00"),
        purpose="Проверка эталонных реквизитов",
        requisites=owner_approved_ip_card_requisites(),
        payer_account="40802810100002438573",
    )

    assert payload["recipientName"] == "Шокина Кристина Юрьевна"
    assert payload["inn"] == "890307589201"
    assert payload["kpp"] == "0"
    assert payload["bankAcnt"] == "40817810800023540968"
    assert payload["bankBik"] == "044525974"
    assert payload["recipientCorrAccountNumber"] == "30101810145250000974"

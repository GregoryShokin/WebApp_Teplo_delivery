from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Account, BankOperation, OwnAccountsRegistry
from app.services.banking.transfer_matching import find_and_link_transfer_pairs

OWN_INN = "616100000001"
SBER_ACCOUNT = "40702810900000000001"
TBANK_ACCOUNT = "40702810800000000002"


@pytest.mark.asyncio
async def test_transfer_matching_links_sber_outflow_to_tbank_inflow(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        sber_account = await _seed_account(session, "sber", SBER_ACCOUNT)
        tbank_account = await _seed_account(session, "tbank", TBANK_ACCOUNT)
        session.add_all(
            [
                OwnAccountsRegistry(
                    bank_code="sber",
                    account_number=SBER_ACCOUNT,
                    legal_entity_inn=OWN_INN,
                    account_id=sber_account.id,
                    is_active=True,
                ),
                OwnAccountsRegistry(
                    bank_code="tbank",
                    account_number=TBANK_ACCOUNT,
                    legal_entity_inn=OWN_INN,
                    account_id=tbank_account.id,
                    is_active=True,
                ),
            ]
        )
        sber_out = BankOperation(
            provider="sber",
            provider_operation_id="pair-sber-out",
            account_id=sber_account.id,
            operation_date=date(2026, 5, 26),
            direction="out",
            amount=Decimal("150000.00"),
            currency="RUB",
            counterparty_inn_raw=OWN_INN,
            counterparty_account_raw=TBANK_ACCOUNT,
            payment_purpose="Перевод собственных средств",
            raw_payload={},
            classification_status="internal_transfer",
        )
        tbank_in = BankOperation(
            provider="tbank",
            provider_operation_id="pair-tbank-in",
            account_id=tbank_account.id,
            operation_date=date(2026, 5, 27),
            direction="in",
            amount=Decimal("150000.00"),
            currency="RUB",
            counterparty_inn_raw=OWN_INN,
            counterparty_account_raw=SBER_ACCOUNT,
            payment_purpose="Перевод собственных средств со счета Сбербанка",
            raw_payload={},
            classification_status="internal_transfer",
        )
        session.add_all([sber_out, tbank_in])
        await session.flush()

        linked = await find_and_link_transfer_pairs(session)
        await session.commit()

        assert linked == 1
        assert sber_out.transfer_group_id is not None
        assert sber_out.transfer_group_id == tbank_in.transfer_group_id


async def _seed_account(session: AsyncSession, provider: str, account_number: str) -> Account:
    account = await session.scalar(select(Account).where(Account.bank_code == provider))
    assert account is not None
    account.account_number = account_number
    account.legal_entity = "ИП Шокина Е.А."
    account.currency = "RUB"
    return account

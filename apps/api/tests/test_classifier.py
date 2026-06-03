from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    Account,
    BankOperation,
    ClassificationRule,
    DdsArticle,
    OwnAccountsRegistry,
    ReconciliationCase,
)
from app.services.banking.classifier import run_classification_rules

OWN_INN = "616100000001"
SBER_ACCOUNT = "40702810900000000001"
TBANK_ACCOUNT = "40702810800000000002"


@pytest.mark.asyncio
async def test_classifier_sets_article_internal_transfer_and_review(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        sber_account, tbank_account = await _prepare_own_accounts(session)
        internal_rule = await session.scalar(
            select(ClassificationRule).where(
                ClassificationRule.name == "Internal transfer placeholder"
            )
        )
        assert internal_rule is not None
        internal_rule.is_active = True

        article = await session.scalar(
            select(DdsArticle).where(DdsArticle.code == "revenue_acquiring_tbank")
        )
        assert article is not None
        operations = [
            BankOperation(
                provider="tbank",
                provider_operation_id="classifier-tbank-acquiring",
                account_id=tbank_account.id,
                operation_date=date(2026, 5, 26),
                direction="in",
                amount=Decimal("32000.00"),
                currency="RUB",
                counterparty_name_raw='АО "ТБанк"',
                counterparty_inn_raw="7710140679",
                counterparty_account_raw="30232810000000000002",
                payment_purpose="Зачисление средств по терминалам эквайринга",
                raw_payload={},
            ),
            BankOperation(
                provider="sber",
                provider_operation_id="classifier-sber-transfer",
                account_id=sber_account.id,
                operation_date=date(2026, 5, 26),
                direction="out",
                amount=Decimal("150000.00"),
                currency="RUB",
                counterparty_name_raw="ИП Шокина Е.А.",
                counterparty_inn_raw=OWN_INN,
                counterparty_account_raw=TBANK_ACCOUNT,
                payment_purpose="Перевод собственных средств",
                raw_payload={},
            ),
            BankOperation(
                provider="sber",
                provider_operation_id="classifier-sber-unknown",
                account_id=sber_account.id,
                operation_date=date(2026, 5, 26),
                direction="in",
                amount=Decimal("5100.00"),
                currency="RUB",
                counterparty_name_raw="ООО Северный Гость",
                counterparty_inn_raw="7811000000",
                counterparty_account_raw="40702810000000000055",
                payment_purpose="Прочее поступление",
                raw_payload={},
            ),
        ]
        session.add_all(operations)
        await session.flush()

        result = await run_classification_rules(session, operations)
        await session.commit()

        assert result.classified == 1
        assert result.internal_transfer == 1
        assert result.needs_review == 1
        assert operations[0].classification_status == "classified"
        assert operations[0].cashflow_transaction_id is not None
        assert operations[1].classification_status == "internal_transfer"
        assert operations[2].classification_status == "needs_review"

        case = await session.scalar(
            select(ReconciliationCase).where(
                ReconciliationCase.bank_operation_id == operations[2].id,
                ReconciliationCase.kind == "unclassified_operation",
            )
        )
        assert case is not None
        assert case.status == "pending"


async def _prepare_own_accounts(session: AsyncSession) -> tuple[Account, Account]:
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
    await session.flush()
    return sber_account, tbank_account


async def _seed_account(session: AsyncSession, provider: str, account_number: str) -> Account:
    account = await session.scalar(select(Account).where(Account.bank_code == provider))
    assert account is not None
    account.account_number = account_number
    account.legal_entity = "ИП Шокина Е.А."
    account.currency = "RUB"
    return account

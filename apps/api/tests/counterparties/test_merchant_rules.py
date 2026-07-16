"""Merchant-правила из карточки: «списание по подстроке мерчанта → контрагент».

Интеграционный тест всей цепочки предоплатной модели (кейс Манго): создание правила
из карточки бэкфиллит висящее needs_review-списание, привязывает контрагента, закрывает
кейс разбора, а хук классификатора создаёт открытую предоплату.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from cp_helpers import (
    make_account,
    make_bank_operation,
    make_counterparty,
    make_expense_article,
    make_wallet,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    CashflowTransaction,
    CounterpartyPayableProfile,
    ReconciliationCase,
    SupplierPrepayment,
)
from app.services.banking.classifier import create_or_update_reconciliation_case
from app.services.merchant_rules import (
    MerchantRuleError,
    create_merchant_rule,
    list_merchant_rules,
)


async def _profile(session: AsyncSession, counterparty_id) -> CounterpartyPayableProfile:
    return (
        await session.execute(
            select(CounterpartyPayableProfile).where(
                CounterpartyPayableProfile.counterparty_id == counterparty_id
            )
        )
    ).scalar_one()


async def test_merchant_rule_backfills_pending_debit_and_creates_prepayment(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Манго Телеком", inn="7709501144")
        article = await make_expense_article(session, code="telecom", name="Телекоммуникации")
        profile = await _profile(session, cp.id)
        profile.default_dds_article_id = article.id
        profile.bank_payments_create_prepayment = True

        account = await make_account(session)
        await make_wallet(session, name="T-Bank", wallet_type="bank", account_id=account.id)
        operation = await make_bank_operation(
            session,
            amount="4900.00",
            direction="out",
            account_id=account.id,
            classification_status="needs_review",
        )
        operation.payment_purpose = "Оплата в MANGO-OFFICE.RU MOSKVA RUS"
        await create_or_update_reconciliation_case(
            session,
            kind="unclassified_operation",
            provider=operation.provider,
            bank_operation_id=operation.id,
            payload={"reason": "no_rule"},
        )
        await session.flush()

        result = await create_merchant_rule(
            session, counterparty_id=cp.id, purpose_pattern="mango"
        )

        assert result.backfilled == 1
        assert result.updated_existing is False
        assert result.rule.article_id == article.id
        # Операция классифицирована, транзакция с контрагентом.
        await session.refresh(operation)
        assert operation.classification_status == "classified"
        transaction = await session.get(CashflowTransaction, operation.cashflow_transaction_id)
        assert transaction is not None and transaction.counterparty_id == cp.id
        # Хук создал предоплату из списания.
        prepayment = (
            await session.execute(
                select(SupplierPrepayment).where(SupplierPrepayment.counterparty_id == cp.id)
            )
        ).scalar_one()
        assert prepayment.amount == Decimal("4900.00")
        assert prepayment.cashflow_transaction_id == transaction.id
        # Кейс разбора закрыт как resolved.
        case = (
            await session.execute(
                select(ReconciliationCase).where(
                    ReconciliationCase.bank_operation_id == operation.id
                )
            )
        ).scalar_one()
        assert case.status == "resolved"
        assert case.resolution_payload.get("reason") == "merchant_rule_backfill"
        # Правило видно в списке карточки.
        rules = await list_merchant_rules(session, cp.id)
        assert [r.purpose_pattern for r in rules] == ["mango"]


async def test_merchant_rule_adopts_existing_orphan_rule(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Прод-кейс Манго: правило со статьёй уже есть, контрагента в нём нет — дописываем."""
    from app.models import ClassificationRule

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Манго Телеком", inn="7709501144")
        article = await make_expense_article(session, code="telecom", name="Телекоммуникации")
        profile = await _profile(session, cp.id)
        profile.default_dds_article_id = article.id
        session.add(
            ClassificationRule(
                name="T-Bank: Телекоммуникации — Mango Office",
                priority=100,
                purpose_pattern="Mango",
                action="set_article",
                article_id=article.id,
            )
        )
        await session.flush()

        result = await create_merchant_rule(
            session, counterparty_id=cp.id, purpose_pattern="mango"
        )
        assert result.updated_existing is True
        assert result.rule.counterparty_id == cp.id
        rules = await list_merchant_rules(session, cp.id)
        assert len(rules) == 1  # дубль не создан


async def test_merchant_rule_conflicts_and_validation(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.models import ClassificationRule

    async with async_session_factory() as session:
        article = await make_expense_article(session, code="telecom", name="Телекоммуникации")
        mango = await make_counterparty(session, name="Манго", inn="7709501144")
        other = await make_counterparty(session, name="Чужой", inn="7700000001")
        for cp in (mango, other):
            profile = await _profile(session, cp.id)
            profile.default_dds_article_id = article.id
        session.add(
            ClassificationRule(
                name="чужое правило",
                priority=100,
                purpose_pattern="mango",
                action="set_article",
                article_id=article.id,
                counterparty_id=other.id,
            )
        )
        await session.flush()

        # Паттерн занят другим контрагентом.
        with pytest.raises(MerchantRuleError):
            await create_merchant_rule(
                session, counterparty_id=mango.id, purpose_pattern="MANGO"
            )
        # Слишком короткий паттерн.
        with pytest.raises(MerchantRuleError):
            await create_merchant_rule(session, counterparty_id=mango.id, purpose_pattern="ma")
        # Без статьи по умолчанию.
        no_article = await make_counterparty(session, name="Без статьи", inn="7700000002")
        with pytest.raises(MerchantRuleError):
            await create_merchant_rule(
                session, counterparty_id=no_article.id, purpose_pattern="somepattern"
            )

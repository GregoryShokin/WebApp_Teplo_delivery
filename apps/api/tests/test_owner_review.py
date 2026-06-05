from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Account, BankOperation, ClassificationRule, DdsArticle, ReconciliationCase

TBANK_ACCOUNT = "40702810800000000002"


def test_owner_review_classify_creates_cashflow_and_rule(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    case_id, operation_id, article_id = asyncio.run(_setup_case(async_session_factory))
    payload = {
        "article_id": str(article_id),
        "action": "set_article",
        "remember_as_rule": True,
    }

    denied_response = client.post(
        f"/api/v1/dds/owner-review/{case_id}/classify",
        headers={"X-User-Role": "finance_manager"},
        json=payload,
    )
    assert denied_response.status_code == 403

    response = client.post(
        f"/api/v1/dds/owner-review/{case_id}/classify",
        headers={"X-User-Role": "admin"},
        json=payload,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "resolved"
    assert body["classification_status"] == "classified"
    result = asyncio.run(_load_result(async_session_factory, operation_id))
    assert result["operation_status"] == "classified"
    assert result["cashflow_transaction_id"] is not None
    assert result["case_status"] == "resolved"
    assert result["rule_count"] == 1


async def _setup_case(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[UUID, UUID, UUID]:
    async with session_factory() as session:
        account = await session.scalar(select(Account).where(Account.bank_code == "tbank"))
        assert account is not None
        account.account_number = TBANK_ACCOUNT
        article = await session.scalar(
            select(DdsArticle).where(DdsArticle.code == "payment_to_supplier")
        )
        assert article is not None
        operation = BankOperation(
            provider="tbank",
            provider_operation_id="owner-review-op",
            account_id=account.id,
            operation_date=date(2026, 5, 26),
            direction="out",
            amount=Decimal("42000.00"),
            currency="RUB",
            counterparty_name_raw="ООО Амай",
            counterparty_inn_raw="6162000000",
            counterparty_account_raw="40702810000000000031",
            payment_purpose="Оплата поставщику за продукты по счету 77",
            raw_payload={},
            classification_status="needs_review",
        )
        session.add(operation)
        await session.flush()
        case = ReconciliationCase(
            kind="unclassified_operation",
            status="pending",
            provider="tbank",
            bank_operation_id=operation.id,
            payload={"reason": "test"},
        )
        session.add(case)
        await session.commit()
        return case.id, operation.id, article.id


async def _load_result(
    session_factory: async_sessionmaker[AsyncSession], operation_id: UUID
) -> dict[str, object]:
    async with session_factory() as session:
        operation = await session.get(BankOperation, operation_id)
        assert operation is not None
        case = await session.scalar(
            select(ReconciliationCase).where(ReconciliationCase.bank_operation_id == operation_id)
        )
        rule_count = await session.scalar(
            select(func.count())
            .select_from(ClassificationRule)
            .where(ClassificationRule.name == "Owner review tbank owner-review-op")
        )
        return {
            "operation_status": operation.classification_status,
            "cashflow_transaction_id": operation.cashflow_transaction_id,
            "case_status": case.status if case else None,
            "rule_count": rule_count,
        }

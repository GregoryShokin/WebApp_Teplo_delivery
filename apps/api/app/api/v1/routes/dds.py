from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, get_current_actor, require_finance_manager_plus
from app.db.session import get_session
from app.models import ClassificationRule, Counterparty, DdsArticle, Wallet
from app.schemas.dds import (
    BankOperationListRead,
    BankSyncRequest,
    BankSyncStubRead,
    CashflowTransactionListRead,
    ClassificationRuleRead,
    DdsArticleRead,
    DdsCounterpartyRead,
    DdsWalletRead,
)


async def require_dds_access(actor: Annotated[CurrentActor, Depends(get_current_actor)]) -> None:
    require_finance_manager_plus(actor)


router = APIRouter(dependencies=[Depends(require_dds_access)])


@router.get("/bank-operations", response_model=BankOperationListRead)
async def list_bank_operations(
    session: Annotated[AsyncSession, Depends(get_session)],
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    provider: str | None = None,
    classification_status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    _ = (session, date_from, date_to, provider, classification_status, limit, offset)
    return {"items": [], "total": 0}


@router.get("/cashflow", response_model=CashflowTransactionListRead)
async def list_cashflow(
    session: Annotated[AsyncSession, Depends(get_session)],
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    wallet_id: UUID | None = None,
    article_id: UUID | None = None,
) -> dict[str, object]:
    _ = (session, date_from, date_to, wallet_id, article_id)
    return {"items": [], "total": 0}


@router.get("/wallets", response_model=list[DdsWalletRead])
async def list_wallets(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, object]]:
    result = await session.scalars(select(Wallet).order_by(Wallet.code))
    return [_wallet_payload(wallet) for wallet in result.all()]


@router.get("/articles", response_model=list[DdsArticleRead])
async def list_articles(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[DdsArticle]:
    result = await session.scalars(select(DdsArticle).order_by(DdsArticle.code))
    return list(result.all())


@router.get("/counterparties", response_model=list[DdsCounterpartyRead])
async def list_counterparties(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[Counterparty]:
    result = await session.scalars(select(Counterparty).order_by(Counterparty.name))
    return list(result.all())


@router.get("/classification-rules", response_model=list[ClassificationRuleRead])
async def list_classification_rules(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, object]]:
    result = await session.scalars(
        select(ClassificationRule).order_by(ClassificationRule.priority, ClassificationRule.name)
    )
    return [_classification_rule_payload(rule) for rule in result.all()]


@router.post(
    "/bank-sync/{provider}",
    response_model=BankSyncStubRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def sync_bank_operations(
    provider: Literal["sber", "tbank"],
    payload: BankSyncRequest,
) -> dict[str, object]:
    _ = (provider, payload)
    return {"status": "not_implemented", "queued_at": datetime.now(UTC)}


def _wallet_payload(wallet: Wallet) -> dict[str, object]:
    balance = _money(wallet.opening_balance)
    return {
        "id": wallet.id,
        "code": wallet.code,
        "name": wallet.name,
        "type": wallet.type,
        "currency": wallet.currency,
        "is_internal_transfer_eligible": wallet.is_internal_transfer_eligible,
        "status": wallet.status,
        "account_id": wallet.account_id,
        "opening_balance": balance,
        "opening_balance_date": wallet.opening_balance_date,
        "balance": balance,
    }


def _classification_rule_payload(rule: ClassificationRule) -> dict[str, object]:
    return {
        "id": rule.id,
        "name": rule.name,
        "priority": rule.priority,
        "is_active": rule.is_active,
        "provider": rule.provider,
        "direction": rule.direction,
        "counterparty_inn_match": rule.counterparty_inn_match,
        "counterparty_name_pattern": rule.counterparty_name_pattern,
        "purpose_pattern": rule.purpose_pattern,
        "amount_min": _optional_money(rule.amount_min),
        "amount_max": _optional_money(rule.amount_max),
        "action": rule.action,
        "article_id": rule.article_id,
        "counterparty_id": rule.counterparty_id,
        "comment": rule.comment,
    }


def _money(value: Decimal | int | None) -> str:
    return f"{Decimal(value or 0):.2f}"


def _optional_money(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return _money(value)

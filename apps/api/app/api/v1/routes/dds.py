from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, get_current_actor, require_finance_manager_plus
from app.db.session import get_session
from app.models import (
    BankOperation,
    CashflowTransaction,
    ClassificationRule,
    Counterparty,
    DdsArticle,
    ReconciliationCase,
    Wallet,
)
from app.scheduler import run_bank_sync_job
from app.schemas.dds import (
    BankOperationListRead,
    BankSyncQueuedRead,
    BankSyncRequest,
    CashflowTransactionListRead,
    ClassificationRuleRead,
    DdsArticleRead,
    DdsCounterpartyRead,
    DdsWalletRead,
    OwnerReviewActionRead,
    OwnerReviewClassifyRequest,
    OwnerReviewListRead,
)
from app.services.banking.classifier import (
    apply_operation_action,
    close_reconciliation_case,
)
from app.services.banking.transfer_matching import find_and_link_transfer_pairs


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
    conditions = []
    if date_from is not None:
        conditions.append(BankOperation.operation_date >= date_from)
    if date_to is not None:
        conditions.append(BankOperation.operation_date <= date_to)
    if provider is not None:
        conditions.append(BankOperation.provider == provider)
    if classification_status is not None:
        conditions.append(BankOperation.classification_status == classification_status)

    total = int(
        await session.scalar(select(func.count()).select_from(BankOperation).where(*conditions))
        or 0
    )
    rows = await session.scalars(
        select(BankOperation)
        .where(*conditions)
        .order_by(BankOperation.operation_date.desc(), BankOperation.imported_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return {"items": [_bank_operation_payload(row) for row in rows.all()], "total": total}


@router.get("/cashflow", response_model=CashflowTransactionListRead)
async def list_cashflow(
    session: Annotated[AsyncSession, Depends(get_session)],
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    wallet_id: UUID | None = None,
    article_id: UUID | None = None,
) -> dict[str, object]:
    conditions = []
    if date_from is not None:
        conditions.append(CashflowTransaction.operation_date >= date_from)
    if date_to is not None:
        conditions.append(CashflowTransaction.operation_date <= date_to)
    if wallet_id is not None:
        conditions.append(CashflowTransaction.wallet_id == wallet_id)
    if article_id is not None:
        conditions.append(CashflowTransaction.article_id == article_id)

    total = int(
        await session.scalar(
            select(func.count()).select_from(CashflowTransaction).where(*conditions)
        )
        or 0
    )
    rows = await session.scalars(
        select(CashflowTransaction)
        .where(*conditions)
        .order_by(CashflowTransaction.operation_date.desc(), CashflowTransaction.created_at.desc())
    )
    return {"items": [_cashflow_payload(row) for row in rows.all()], "total": total}


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
    response_model=BankSyncQueuedRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def sync_bank_operations(
    provider: Literal["sber", "tbank"],
    payload: BankSyncRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, object]:
    job_id = uuid4()
    background_tasks.add_task(
        run_bank_sync_job,
        provider=provider,
        date_from=payload.date_from,
        date_to=payload.date_to,
        job_id=job_id,
    )
    return {"job_id": job_id, "status": "queued", "queued_at": datetime.now(UTC)}


@router.get("/owner-review", response_model=OwnerReviewListRead)
async def list_owner_review_cases(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    kind: Literal[
        "unclassified_operation", "invalid_credentials", "unmatched_transfer"
    ]
    | None = None,
) -> dict[str, object]:
    allowed_kinds = ("unclassified_operation", "invalid_credentials", "unmatched_transfer")
    conditions = [
        ReconciliationCase.status == "pending",
        ReconciliationCase.kind.in_(allowed_kinds),
    ]
    if kind is not None:
        conditions.append(ReconciliationCase.kind == kind)

    total = int(
        await session.scalar(
            select(func.count()).select_from(ReconciliationCase).where(*conditions)
        )
        or 0
    )
    cases = await session.scalars(
        select(ReconciliationCase)
        .where(*conditions)
        .order_by(ReconciliationCase.created_at, ReconciliationCase.id)
        .limit(limit)
        .offset(offset)
    )
    items = []
    for case in cases.all():
        operation = None
        if case.bank_operation_id is not None:
            operation = await session.get(BankOperation, case.bank_operation_id)
        items.append(_owner_review_payload(case, operation))
    return {"items": items, "total": total}


@router.post(
    "/owner-review/{case_id}/classify",
    response_model=OwnerReviewActionRead,
)
async def classify_owner_review_case(
    case_id: UUID,
    payload: OwnerReviewClassifyRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    case = await _pending_case_or_404(session, case_id)
    if case.bank_operation_id is None:
        raise HTTPException(status_code=400, detail="Review case is not linked to a bank operation")
    operation = await session.get(BankOperation, case.bank_operation_id)
    if operation is None:
        raise HTTPException(status_code=404, detail="Bank operation not found")
    if payload.action == "set_article" and payload.article_id is None:
        raise HTTPException(status_code=400, detail="article_id is required for set_article")

    await apply_operation_action(
        session,
        operation,
        action=payload.action,
        article_id=payload.article_id,
        counterparty_id=payload.counterparty_id,
        quality_status="owner_review",
    )
    if payload.action == "mark_internal_transfer":
        await find_and_link_transfer_pairs(session)

    rule_id = None
    if payload.remember_as_rule:
        rule = _rule_from_owner_review(operation, payload)
        session.add(rule)
        await session.flush()
        rule_id = rule.id

    await close_reconciliation_case(
        session,
        case,
        status="resolved",
        resolution_payload={
            "action": payload.action,
            "article_id": str(payload.article_id) if payload.article_id else None,
            "counterparty_id": str(payload.counterparty_id) if payload.counterparty_id else None,
            "rule_id": str(rule_id) if rule_id else None,
        },
    )
    await session.commit()
    return {
        "case_id": case.id,
        "status": case.status,
        "bank_operation_id": operation.id,
        "classification_status": operation.classification_status,
        "rule_id": rule_id,
    }


@router.post(
    "/owner-review/{case_id}/dismiss",
    response_model=OwnerReviewActionRead,
)
async def dismiss_owner_review_case(
    case_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    case = await _pending_case_or_404(session, case_id)
    await close_reconciliation_case(session, case, status="dismissed")
    await session.commit()
    return {"case_id": case.id, "status": case.status, "bank_operation_id": case.bank_operation_id}


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


def _bank_operation_payload(operation: BankOperation) -> dict[str, object]:
    return {
        "id": operation.id,
        "provider": operation.provider,
        "provider_operation_id": operation.provider_operation_id,
        "account_id": operation.account_id,
        "operation_date": operation.operation_date,
        "posted_at": operation.posted_at,
        "direction": operation.direction,
        "amount": _money(operation.amount),
        "currency": operation.currency,
        "counterparty_name_raw": operation.counterparty_name_raw,
        "counterparty_inn_raw": operation.counterparty_inn_raw,
        "counterparty_account_raw": operation.counterparty_account_raw,
        "payment_purpose": operation.payment_purpose,
        "document_number": operation.document_number,
        "classification_status": operation.classification_status,
        "cashflow_transaction_id": operation.cashflow_transaction_id,
        "transfer_group_id": operation.transfer_group_id,
    }


def _cashflow_payload(transaction: CashflowTransaction) -> dict[str, object]:
    return {
        "id": transaction.id,
        "wallet_id": transaction.wallet_id,
        "direction": transaction.direction,
        "amount": _money(transaction.amount),
        "operation_date": transaction.operation_date,
        "article_id": transaction.article_id,
        "counterparty_id": transaction.counterparty_id,
        "transfer_group_id": transaction.transfer_group_id,
        "source_kind": transaction.source_kind,
        "source_id": transaction.source_id,
        "payment_purpose": transaction.payment_purpose,
        "comment": transaction.comment,
        "quality_status": transaction.quality_status,
    }


def _owner_review_payload(
    case: ReconciliationCase, operation: BankOperation | None
) -> dict[str, object]:
    return {
        "id": case.id,
        "kind": case.kind,
        "status": case.status,
        "provider": case.provider,
        "bank_operation_id": case.bank_operation_id,
        "payload": case.payload,
        "created_at": case.created_at,
        "operation": _bank_operation_payload(operation) if operation else None,
    }


async def _pending_case_or_404(session: AsyncSession, case_id: UUID) -> ReconciliationCase:
    case = await session.get(ReconciliationCase, case_id)
    if case is None or case.status != "pending":
        raise HTTPException(status_code=404, detail="Pending review case not found")
    return case


def _rule_from_owner_review(
    operation: BankOperation, payload: OwnerReviewClassifyRequest
) -> ClassificationRule:
    purpose_pattern = _short_pattern(operation.payment_purpose)
    counterparty_pattern = None if operation.counterparty_inn_raw else _short_pattern(
        operation.counterparty_name_raw
    )
    return ClassificationRule(
        name=f"Owner review {operation.provider} {operation.provider_operation_id}",
        priority=50,
        is_active=True,
        provider=operation.provider,
        direction=operation.direction,
        counterparty_inn_match=operation.counterparty_inn_raw,
        counterparty_name_pattern=counterparty_pattern,
        purpose_pattern=purpose_pattern,
        action=payload.action,
        article_id=payload.article_id,
        counterparty_id=payload.counterparty_id,
        comment=f"Created from owner-review case for {operation.provider_operation_id}",
    )


def _short_pattern(value: str | None) -> str | None:
    text = " ".join((value or "").split())
    if not text:
        return None
    return text[:120]

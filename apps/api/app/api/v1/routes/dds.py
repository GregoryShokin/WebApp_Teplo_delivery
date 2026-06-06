from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.db.session import get_session
from app.models import (
    BankOperation,
    CashflowTransaction,
    ClassificationRule,
    Counterparty,
    CounterpartyAlias,
    DdsArticle,
    DdsArticleAlias,
    ReconciliationCase,
    SourceCredential,
    Wallet,
)
from app.scheduler import run_bank_sync_job
from app.schemas.dds import (
    BankOperationListRead,
    BankSyncQueuedRead,
    BankSyncRequest,
    CashflowTransactionListRead,
    ClassificationRuleCreate,
    ClassificationRulePatch,
    ClassificationRuleRead,
    CredentialCreate,
    CredentialRead,
    DdsAliasCreate,
    DdsAliasRead,
    DdsArticleCreate,
    DdsArticlePatch,
    DdsArticleRead,
    DdsCounterpartyCreate,
    DdsCounterpartyPatch,
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
from app.services.banking.credentials import set_credential
from app.services.banking.transfer_matching import find_and_link_transfer_pairs

router = APIRouter()
DDS_READ_ACCESS = (Depends(require_permission("finance.cashflow.read")),)
DDS_EDIT_ACCESS = (Depends(require_permission("finance.cashflow.edit")),)
DDS_RULES_MANAGE_ACCESS = (
    Depends(require_permission("finance.classification_rules.manage")),
)
DDS_CLASSIFY_ACCESS = (Depends(require_permission("finance.cashflow.classify")),)
DDS_INTEGRATIONS_MANAGE_ACCESS = (
    Depends(require_permission("finance.cashflow.integrations.manage")),
)
DDS_WALLETS_READ_ACCESS = (Depends(require_permission("finance.wallets.read")),)
DDS_COUNTERPARTIES_READ_ACCESS = (Depends(require_permission("finance.counterparties.read")),)
DDS_COUNTERPARTIES_EDIT_ACCESS = (Depends(require_permission("finance.counterparties.edit")),)
DDS_OWNER_REVIEW_PREPARE_ACCESS = (
    Depends(require_permission("finance.owner_review.prepare")),
)


@router.get("/bank-operations", response_model=BankOperationListRead, dependencies=DDS_READ_ACCESS)
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


@router.get("/cashflow", response_model=CashflowTransactionListRead, dependencies=DDS_READ_ACCESS)
async def list_cashflow(
    session: Annotated[AsyncSession, Depends(get_session)],
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    wallet_id: UUID | None = None,
    article_id: UUID | None = None,
    direction: Literal["in", "out"] | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
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
    if direction is not None:
        conditions.append(CashflowTransaction.direction == direction)

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
        .limit(limit)
        .offset(offset)
    )
    return {"items": [_cashflow_payload(row) for row in rows.all()], "total": total}


@router.get("/wallets", response_model=list[DdsWalletRead], dependencies=DDS_WALLETS_READ_ACCESS)
async def list_wallets(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, object]]:
    result = await session.scalars(select(Wallet).order_by(Wallet.code))
    return [_wallet_payload(wallet) for wallet in result.all()]


@router.get("/articles", response_model=list[DdsArticleRead], dependencies=DDS_READ_ACCESS)
async def list_articles(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, object]]:
    result = await session.scalars(select(DdsArticle).order_by(DdsArticle.code))
    return await _article_payloads(session, result.all())


@router.post(
    "/articles",
    response_model=DdsArticleRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=DDS_EDIT_ACCESS,
)
async def create_article(
    payload: DdsArticleCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    article = DdsArticle(**payload.model_dump())
    session.add(article)
    await session.commit()
    await session.refresh(article)
    return (await _article_payloads(session, [article]))[0]


@router.patch(
    "/articles/{article_id}",
    response_model=DdsArticleRead,
    dependencies=DDS_EDIT_ACCESS,
)
async def patch_article(
    article_id: UUID,
    payload: DdsArticlePatch,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    article = await _article_or_404(session, article_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(article, key, value)
    await session.commit()
    await session.refresh(article)
    return (await _article_payloads(session, [article]))[0]


@router.delete(
    "/articles/{article_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=DDS_EDIT_ACCESS,
)
async def delete_article(
    article_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    article = await _article_or_404(session, article_id)
    article.is_active = False
    await session.commit()


@router.post(
    "/articles/{article_id}/aliases",
    response_model=DdsAliasRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=DDS_EDIT_ACCESS,
)
async def create_article_alias(
    article_id: UUID,
    payload: DdsAliasCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DdsArticleAlias:
    await _article_or_404(session, article_id)
    alias = DdsArticleAlias(
        article_id=article_id,
        alias=payload.alias,
        source=payload.source,
    )
    session.add(alias)
    await session.commit()
    await session.refresh(alias)
    return alias


@router.delete(
    "/articles/aliases/{alias_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=DDS_EDIT_ACCESS,
)
async def delete_article_alias(
    alias_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    alias = await session.get(DdsArticleAlias, alias_id)
    if alias is None:
        raise HTTPException(status_code=404, detail="Article alias not found")
    await session.delete(alias)
    await session.commit()


@router.get(
    "/counterparties",
    response_model=list[DdsCounterpartyRead],
    dependencies=DDS_COUNTERPARTIES_READ_ACCESS,
)
async def list_counterparties(
    session: Annotated[AsyncSession, Depends(get_session)],
    search: str | None = None,
) -> list[dict[str, object]]:
    conditions = []
    if search:
        pattern = f"%{search.strip()}%"
        conditions.append(or_(Counterparty.name.ilike(pattern), Counterparty.inn.ilike(pattern)))
    result = await session.scalars(
        select(Counterparty).where(*conditions).order_by(Counterparty.name)
    )
    return await _counterparty_payloads(session, result.all())


@router.post(
    "/counterparties",
    response_model=DdsCounterpartyRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=DDS_COUNTERPARTIES_EDIT_ACCESS,
)
async def create_counterparty(
    payload: DdsCounterpartyCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    counterparty = Counterparty(**payload.model_dump())
    session.add(counterparty)
    await session.commit()
    await session.refresh(counterparty)
    return (await _counterparty_payloads(session, [counterparty]))[0]


@router.patch(
    "/counterparties/{counterparty_id}",
    response_model=DdsCounterpartyRead,
    dependencies=DDS_COUNTERPARTIES_EDIT_ACCESS,
)
async def patch_counterparty(
    counterparty_id: UUID,
    payload: DdsCounterpartyPatch,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    counterparty = await _counterparty_or_404(session, counterparty_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(counterparty, key, value)
    await session.commit()
    await session.refresh(counterparty)
    return (await _counterparty_payloads(session, [counterparty]))[0]


@router.delete(
    "/counterparties/{counterparty_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=DDS_COUNTERPARTIES_EDIT_ACCESS,
)
async def delete_counterparty(
    counterparty_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    counterparty = await _counterparty_or_404(session, counterparty_id)
    counterparty.status = "inactive"
    await session.commit()


@router.post(
    "/counterparties/{counterparty_id}/aliases",
    response_model=DdsAliasRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=DDS_COUNTERPARTIES_EDIT_ACCESS,
)
async def create_counterparty_alias(
    counterparty_id: UUID,
    payload: DdsAliasCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CounterpartyAlias:
    await _counterparty_or_404(session, counterparty_id)
    alias = CounterpartyAlias(
        counterparty_id=counterparty_id,
        alias=payload.alias,
        source=payload.source,
    )
    session.add(alias)
    await session.commit()
    await session.refresh(alias)
    return alias


@router.delete(
    "/counterparties/aliases/{alias_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=DDS_COUNTERPARTIES_EDIT_ACCESS,
)
async def delete_counterparty_alias(
    alias_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    alias = await session.get(CounterpartyAlias, alias_id)
    if alias is None:
        raise HTTPException(status_code=404, detail="Counterparty alias not found")
    await session.delete(alias)
    await session.commit()


@router.get(
    "/classification-rules",
    response_model=list[ClassificationRuleRead],
    dependencies=DDS_RULES_MANAGE_ACCESS,
)
async def list_classification_rules(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, object]]:
    result = await session.scalars(
        select(ClassificationRule).order_by(ClassificationRule.priority, ClassificationRule.name)
    )
    return [_classification_rule_payload(rule) for rule in result.all()]


@router.post(
    "/classification-rules",
    response_model=ClassificationRuleRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=DDS_RULES_MANAGE_ACCESS,
)
async def create_classification_rule(
    payload: ClassificationRuleCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    data = _classification_rule_data(payload.model_dump())
    rule = ClassificationRule(**data)
    session.add(rule)
    await session.commit()
    await session.refresh(rule)
    return _classification_rule_payload(rule)


@router.patch(
    "/classification-rules/{rule_id}",
    response_model=ClassificationRuleRead,
    dependencies=DDS_RULES_MANAGE_ACCESS,
)
async def patch_classification_rule(
    rule_id: UUID,
    payload: ClassificationRulePatch,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    rule = await _classification_rule_or_404(session, rule_id)
    for key, value in _classification_rule_data(payload.model_dump(exclude_unset=True)).items():
        setattr(rule, key, value)
    await session.commit()
    await session.refresh(rule)
    return _classification_rule_payload(rule)


@router.delete(
    "/classification-rules/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=DDS_RULES_MANAGE_ACCESS,
)
async def delete_classification_rule(
    rule_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    rule = await _classification_rule_or_404(session, rule_id)
    rule.is_active = False
    await session.commit()


@router.post(
    "/classification-rules/{rule_id}/toggle",
    response_model=ClassificationRuleRead,
    dependencies=DDS_RULES_MANAGE_ACCESS,
)
async def toggle_classification_rule(
    rule_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    rule = await _classification_rule_or_404(session, rule_id)
    rule.is_active = not rule.is_active
    await session.commit()
    await session.refresh(rule)
    return _classification_rule_payload(rule)


@router.get(
    "/credentials",
    response_model=list[CredentialRead],
    dependencies=DDS_INTEGRATIONS_MANAGE_ACCESS,
)
async def list_credentials(
    session: Annotated[AsyncSession, Depends(get_session)],
    is_active: bool | None = True,
) -> list[dict[str, object]]:
    conditions = []
    if is_active is not None:
        conditions.append(SourceCredential.is_active.is_(is_active))
    result = await session.scalars(
        select(SourceCredential)
        .where(*conditions)
        .order_by(SourceCredential.provider, SourceCredential.credential_kind)
    )
    return [_credential_payload(credential) for credential in result.all()]


@router.post(
    "/credentials",
    response_model=CredentialRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=DDS_INTEGRATIONS_MANAGE_ACCESS,
)
async def create_credential(
    payload: CredentialCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    credential = await set_credential(
        session,
        provider=payload.provider,
        kind=payload.credential_kind,
        value=payload.value,
        expires_at=payload.expires_at,
        metadata_json=payload.metadata,
    )
    return _credential_payload(credential)


@router.delete(
    "/credentials/{credential_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=DDS_INTEGRATIONS_MANAGE_ACCESS,
)
async def delete_credential(
    credential_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    credential = await session.get(SourceCredential, credential_id)
    if credential is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    credential.is_active = False
    credential.status = "inactive"
    await session.commit()


@router.post(
    "/bank-sync/{provider}",
    response_model=BankSyncQueuedRead,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=DDS_INTEGRATIONS_MANAGE_ACCESS,
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


@router.get(
    "/owner-review",
    response_model=OwnerReviewListRead,
    dependencies=DDS_OWNER_REVIEW_PREPARE_ACCESS,
)
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
    dependencies=DDS_CLASSIFY_ACCESS,
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
    dependencies=DDS_OWNER_REVIEW_PREPARE_ACCESS,
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


async def _article_payloads(
    session: AsyncSession, articles: list[DdsArticle]
) -> list[dict[str, object]]:
    article_ids = [article.id for article in articles]
    aliases_by_article: dict[UUID, list[dict[str, object]]] = {
        article_id: [] for article_id in article_ids
    }
    if article_ids:
        aliases = await session.scalars(
            select(DdsArticleAlias)
            .where(DdsArticleAlias.article_id.in_(article_ids))
            .order_by(DdsArticleAlias.alias)
        )
        for alias in aliases.all():
            aliases_by_article.setdefault(alias.article_id, []).append(
                {"id": alias.id, "alias": alias.alias, "source": alias.source}
            )
    return [
        {
            "id": article.id,
            "code": article.code,
            "name": article.name,
            "movement_type": article.movement_type,
            "activity_type": article.activity_type,
            "parent_id": article.parent_id,
            "is_active": article.is_active,
            "description": article.description,
            "aliases": aliases_by_article.get(article.id, []),
        }
        for article in articles
    ]


async def _counterparty_payloads(
    session: AsyncSession, counterparties: list[Counterparty]
) -> list[dict[str, object]]:
    counterparty_ids = [counterparty.id for counterparty in counterparties]
    aliases_by_counterparty: dict[UUID, list[dict[str, object]]] = {
        counterparty_id: [] for counterparty_id in counterparty_ids
    }
    if counterparty_ids:
        aliases = await session.scalars(
            select(CounterpartyAlias)
            .where(CounterpartyAlias.counterparty_id.in_(counterparty_ids))
            .order_by(CounterpartyAlias.alias)
        )
        for alias in aliases.all():
            aliases_by_counterparty.setdefault(alias.counterparty_id, []).append(
                {"id": alias.id, "alias": alias.alias, "source": alias.source}
            )
    return [
        {
            "id": counterparty.id,
            "name": counterparty.name,
            "inn": counterparty.inn,
            "type": counterparty.type,
            "status": counterparty.status,
            "aliases": aliases_by_counterparty.get(counterparty.id, []),
        }
        for counterparty in counterparties
    ]


def _classification_rule_data(data: dict[str, object]) -> dict[str, object]:
    for amount_key in ("amount_min", "amount_max"):
        if amount_key in data and data[amount_key] is not None:
            data[amount_key] = Decimal(str(data[amount_key]))
    return data


def _credential_payload(credential: SourceCredential) -> dict[str, object]:
    return {
        "id": credential.id,
        "provider": credential.provider,
        "credential_kind": credential.credential_kind,
        "is_active": credential.is_active,
        "expires_at": credential.expires_at,
        "metadata": credential.metadata_json,
        "created_at": credential.created_at,
        "updated_at": credential.updated_at,
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
        "raw_payload": operation.raw_payload,
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


async def _article_or_404(session: AsyncSession, article_id: UUID) -> DdsArticle:
    article = await session.get(DdsArticle, article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="DDS article not found")
    return article


async def _counterparty_or_404(session: AsyncSession, counterparty_id: UUID) -> Counterparty:
    counterparty = await session.get(Counterparty, counterparty_id)
    if counterparty is None:
        raise HTTPException(status_code=404, detail="Counterparty not found")
    return counterparty


async def _classification_rule_or_404(
    session: AsyncSession, rule_id: UUID
) -> ClassificationRule:
    rule = await session.get(ClassificationRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Classification rule not found")
    return rule


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

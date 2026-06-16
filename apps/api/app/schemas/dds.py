from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class BankOperationRead(BaseModel):
    id: uuid.UUID
    provider: str
    provider_operation_id: str
    account_id: uuid.UUID | None = None
    operation_date: date
    posted_at: datetime | None = None
    direction: str
    amount: str
    currency: str
    counterparty_name_raw: str | None = None
    counterparty_inn_raw: str | None = None
    counterparty_account_raw: str | None = None
    payment_purpose: str | None = None
    document_number: str | None = None
    classification_status: str
    cashflow_transaction_id: uuid.UUID | None = None
    transfer_group_id: uuid.UUID | None = None
    raw_payload: dict[str, Any] | None = None


class BankOperationListRead(BaseModel):
    items: list[BankOperationRead]
    total: int


class CashflowTransactionRead(BaseModel):
    id: uuid.UUID
    wallet_id: uuid.UUID
    direction: str
    amount: str
    operation_date: date
    article_id: uuid.UUID | None = None
    counterparty_id: uuid.UUID | None = None
    transfer_group_id: uuid.UUID | None = None
    source_kind: str
    source_id: uuid.UUID | None = None
    payment_purpose: str | None = None
    comment: str | None = None
    quality_status: str


class CashflowTransactionListRead(BaseModel):
    items: list[CashflowTransactionRead]
    total: int


class DdsWalletRead(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    type: str
    currency: str
    is_internal_transfer_eligible: bool
    status: str
    account_id: uuid.UUID | None = None
    bank_code: str | None = None
    opening_balance: str
    opening_balance_date: date | None = None
    balance: str


class DdsAliasCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str
    source: str | None = None


class DdsAliasRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    alias: str
    source: str | None = None


class DdsArticleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    movement_type: str
    activity_type: str
    parent_id: uuid.UUID | None = None
    is_active: bool
    description: str | None = None
    aliases: list[DdsAliasRead] = Field(default_factory=list)


class DdsArticleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    name: str
    movement_type: Literal["inflow", "outflow", "internal"]
    activity_type: str
    parent_id: uuid.UUID | None = None
    is_active: bool = True
    description: str | None = None


class DdsArticlePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str | None = None
    name: str | None = None
    movement_type: Literal["inflow", "outflow", "internal"] | None = None
    activity_type: str | None = None
    parent_id: uuid.UUID | None = None
    is_active: bool | None = None
    description: str | None = None


class DdsCounterpartyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    inn: str | None = None
    type: str
    status: str
    aliases: list[DdsAliasRead] = Field(default_factory=list)


class DdsCounterpartyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    inn: str | None = None
    type: Literal["legal_entity", "individual", "bank", "tax_authority"] = "legal_entity"
    status: str = "active"


class DdsCounterpartyPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    inn: str | None = None
    type: Literal["legal_entity", "individual", "bank", "tax_authority"] | None = None
    status: str | None = None


class ClassificationRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    priority: int
    is_active: bool
    provider: str | None = None
    direction: str | None = None
    counterparty_inn_match: str | None = None
    counterparty_name_pattern: str | None = None
    purpose_pattern: str | None = None
    amount_min: str | None = None
    amount_max: str | None = None
    action: str
    article_id: uuid.UUID | None = None
    counterparty_id: uuid.UUID | None = None
    comment: str | None = None


class ClassificationRuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    priority: int = 100
    is_active: bool = True
    provider: Literal["sber", "tbank"] | None = None
    direction: Literal["in", "out"] | None = None
    counterparty_inn_match: str | None = None
    counterparty_name_pattern: str | None = None
    purpose_pattern: str | None = None
    amount_min: str | None = None
    amount_max: str | None = None
    action: Literal["set_article", "mark_internal_transfer", "exclude"]
    article_id: uuid.UUID | None = None
    counterparty_id: uuid.UUID | None = None
    comment: str | None = None


class ClassificationRulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    priority: int | None = None
    is_active: bool | None = None
    provider: Literal["sber", "tbank"] | None = None
    direction: Literal["in", "out"] | None = None
    counterparty_inn_match: str | None = None
    counterparty_name_pattern: str | None = None
    purpose_pattern: str | None = None
    amount_min: str | None = None
    amount_max: str | None = None
    action: Literal["set_article", "mark_internal_transfer", "exclude"] | None = None
    article_id: uuid.UUID | None = None
    counterparty_id: uuid.UUID | None = None
    comment: str | None = None


class CredentialRead(BaseModel):
    id: uuid.UUID
    provider: str
    credential_kind: str
    is_active: bool
    expires_at: datetime | None
    metadata: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class CredentialCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["sber", "tbank"]
    credential_kind: Literal[
        "access_token",
        "client_secret",
        "bearer_token",
        "mtls_cert_path",
        "mtls_key_path",
    ]
    value: str
    expires_at: datetime | None = None
    metadata: dict[str, Any] | None = None


class BankSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date_from: date = Field(alias="date_from")
    date_to: date = Field(alias="date_to")


class BankSyncQueuedRead(BaseModel):
    job_id: uuid.UUID
    status: str
    queued_at: datetime


class OwnerReviewCaseRead(BaseModel):
    id: uuid.UUID
    kind: str
    status: str
    provider: str | None = None
    bank_operation_id: uuid.UUID | None = None
    payload: dict[str, Any]
    created_at: datetime
    operation: BankOperationRead | None = None


class OwnerReviewListRead(BaseModel):
    items: list[OwnerReviewCaseRead]
    total: int


class OwnerReviewClassifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    article_id: uuid.UUID | None = None
    counterparty_id: uuid.UUID | None = None
    action: Literal["set_article", "mark_internal_transfer", "exclude"]
    remember_as_rule: bool = False


class OwnerReviewActionRead(BaseModel):
    case_id: uuid.UUID
    status: str
    bank_operation_id: uuid.UUID | None = None
    classification_status: str | None = None
    rule_id: uuid.UUID | None = None

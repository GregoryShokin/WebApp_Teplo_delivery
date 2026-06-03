from __future__ import annotations

import uuid
from datetime import date, datetime

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
    opening_balance: str
    opening_balance_date: date | None = None
    balance: str


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


class DdsCounterpartyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    inn: str | None = None
    type: str
    status: str


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


class BankSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date_from: date = Field(alias="date_from")
    date_to: date = Field(alias="date_to")


class BankSyncStubRead(BaseModel):
    status: str
    queued_at: datetime

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
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
    # Раскладка подотчётного Сейфа; null для прочих кошельков.
    reserved_total: str | None = None
    free_total: str | None = None


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


class OperationSplitItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    article_id: uuid.UUID
    amount: Decimal = Field(gt=0)
    comment: str | None = None
    # Для статьи «Оплата поставщикам»: конкретная неоплаченная накладная, которую гасит эта
    # сумма (привязка операции → InvoicePaymentAllocation). Несколько строк → несколько накладных.
    invoice_id: uuid.UUID | None = None


class OperationClassifyRequest(BaseModel):
    """Multi-article classification of a single bank operation.

    ``action="split"`` spreads the operation across one or more articles (each with
    its own amount; amounts must sum to the operation amount). The other actions
    mirror the legacy owner-review flow.
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["split", "mark_internal_transfer", "exclude", "mark_safe_topup"] = "split"
    splits: list[OperationSplitItem] = Field(default_factory=list)
    counterparty_id: uuid.UUID | None = None
    remember_as_rule: bool = False


class OperationClassifyRead(BaseModel):
    bank_operation_id: uuid.UUID
    classification_status: str
    cashflow_transaction_ids: list[uuid.UUID] = Field(default_factory=list)
    rule_id: uuid.UUID | None = None


class SafeAllocationCreate(BaseModel):
    """Создание резерва на счёте «Сейф» (намерение потратить под статью/получателя)."""

    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(gt=0)
    article_id: uuid.UUID
    counterparty_id: uuid.UUID | None = None
    purpose: str | None = None
    # Сразу оплатить полностью — прямая трата свободных средств (без отдельного шага «Оплачено»).
    pay_full: bool = False


class SafeAllocationPayRequest(BaseModel):
    """Оплата резерва — полная или частичная (``amount`` ≤ остатка резерва)."""

    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(gt=0)


class SafeAllocationRead(BaseModel):
    id: uuid.UUID
    wallet_id: uuid.UUID
    amount: str
    amount_paid: str
    outstanding: str
    article_id: uuid.UUID | None = None
    counterparty_id: uuid.UUID | None = None
    purpose: str | None = None
    status: str
    created_at: datetime


class SafeCashWithdrawRequest(BaseModel):
    """Снятие наличных с карты «Сейф» → касса Черникова (перевод между счетами)."""

    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(gt=0)


class SafeReconcileRequest(BaseModel):
    """Сверка: ввод реального остатка карты. ``apply_adjustment`` — выровнять учётный остаток."""

    model_config = ConfigDict(extra="forbid")

    actual_balance: Decimal = Field(ge=0)
    apply_adjustment: bool = False


class SafeReconcileRead(BaseModel):
    accounted: str  # учётный остаток Сейфа
    actual: str  # реальный остаток карты (введён вручную)
    delta: str  # реальный − учётный (>0 излишек, <0 недостача)
    adjusted: bool  # проведена ли корректирующая проводка


class JournalRow(BaseModel):
    """One line of the unified DDS journal — a classified cashflow movement
    (``kind="cashflow"``) or an unclassified bank operation (``kind="operation"``)."""

    kind: Literal["cashflow", "operation"]
    id: uuid.UUID
    bank_operation_id: uuid.UUID | None = None
    status: str  # "classified" | "needs_review"
    operation_date: date
    occurred_at: datetime  # business day + real time-of-day (Moscow), drives sort order
    direction: str
    amount: str
    article_id: uuid.UUID | None = None
    counterparty_id: uuid.UUID | None = None
    wallet_id: uuid.UUID | None = None
    provider: str | None = None
    payment_purpose: str | None = None
    counterparty_name_raw: str | None = None
    counterparty_inn_raw: str | None = None


class JournalListRead(BaseModel):
    items: list[JournalRow]
    total: int
    marked_total: int
    unmarked_total: int
    transfer_total: int = 0

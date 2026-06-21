from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Account(Base):
    __tablename__ = "account"
    __table_args__ = (
        Index(
            "uq_account_bank_code_account_number",
            "bank_code",
            "account_number",
            unique=True,
            postgresql_where=text("account_number IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bank_code: Mapped[str] = mapped_column(String(32), nullable=False)
    account_number: Mapped[str | None] = mapped_column(String(40), nullable=True)
    bic: Mapped[str | None] = mapped_column(String(20), nullable=True)
    legal_entity: Mapped[str] = mapped_column(String(255), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="RUB", server_default="RUB"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active", server_default="active"
    )
    opened_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    closed_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class OwnAccountsRegistry(Base):
    __tablename__ = "own_accounts_registry"
    __table_args__ = (
        Index(
            "uq_own_accounts_registry_bank_account",
            "bank_code",
            "account_number",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bank_code: Mapped[str] = mapped_column(String(32), nullable=False)
    account_number: Mapped[str] = mapped_column(String(40), nullable=False)
    bic: Mapped[str | None] = mapped_column(String(20), nullable=True)
    legal_entity_inn: Mapped[str | None] = mapped_column(String(12), nullable=True)
    account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("account.id"), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)


class DdsArticle(Base):
    __tablename__ = "dds_articles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    movement_type: Mapped[str] = mapped_column(String(16), nullable=False)
    activity_type: Mapped[str] = mapped_column(String(16), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dds_articles.id"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class DdsArticleAlias(Base):
    __tablename__ = "dds_article_aliases"
    __table_args__ = (
        Index("uq_dds_article_aliases_alias_ci", func.lower(text("alias")), unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    article_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dds_articles.id", ondelete="CASCADE"), nullable=False
    )
    alias: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)


class CounterpartyAlias(Base):
    __tablename__ = "counterparty_alias"
    __table_args__ = (
        Index("uq_counterparty_alias_alias_ci", func.lower(text("alias")), unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    counterparty_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("counterparty.id", ondelete="CASCADE"), nullable=False
    )
    alias: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)


class TransferGroup(Base):
    __tablename__ = "transfer_groups"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    from_wallet_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("wallet.id"), nullable=True)
    to_wallet_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("wallet.id"), nullable=True)
    initiated_at: Mapped[date] = mapped_column(Date, nullable=False)
    completed_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CashflowTransaction(Base):
    __tablename__ = "cashflow_transactions"
    __table_args__ = (
        Index("ix_cashflow_transactions_operation_date", "operation_date"),
        Index("ix_cashflow_transactions_wallet_id", "wallet_id"),
        Index("ix_cashflow_transactions_article_id", "article_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wallet.id", ondelete="RESTRICT"), nullable=False
    )
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    operation_date: Mapped[date] = mapped_column(Date, nullable=False)
    article_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dds_articles.id"), nullable=True
    )
    counterparty_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("counterparty.id"), nullable=True
    )
    transfer_group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transfer_groups.id"), nullable=True
    )
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    payment_purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    quality_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="auto", server_default="auto"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class BankOperation(Base):
    __tablename__ = "bank_operations"
    __table_args__ = (
        Index(
            "uq_bank_operations_provider_operation",
            "provider",
            "provider_operation_id",
            unique=True,
        ),
        Index("ix_bank_operations_operation_date", "operation_date"),
        Index("ix_bank_operations_classification_status", "classification_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_operation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("account.id"), nullable=True)
    operation_date: Mapped[date] = mapped_column(Date, nullable=False)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="RUB", server_default="RUB"
    )
    counterparty_name_raw: Mapped[str | None] = mapped_column(String(512), nullable=True)
    counterparty_inn_raw: Mapped[str | None] = mapped_column(String(20), nullable=True)
    counterparty_account_raw: Mapped[str | None] = mapped_column(String(40), nullable=True)
    payment_purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    document_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    classification_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )
    cashflow_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("cashflow_transactions.id"), nullable=True
    )
    transfer_group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transfer_groups.id"), nullable=True
    )


class ClassificationRule(Base):
    __tablename__ = "classification_rules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, server_default="100"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    direction: Mapped[str | None] = mapped_column(String(8), nullable=True)
    counterparty_inn_match: Mapped[str | None] = mapped_column(String(12), nullable=True)
    counterparty_name_pattern: Mapped[str | None] = mapped_column(String(255), nullable=True)
    purpose_pattern: Mapped[str | None] = mapped_column(String(255), nullable=True)
    amount_min: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    amount_max: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    article_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dds_articles.id"), nullable=True
    )
    counterparty_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("counterparty.id"), nullable=True
    )
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class WalletBalanceSnapshot(Base):
    __tablename__ = "wallet_balance_snapshots"
    __table_args__ = (
        Index(
            "uq_wallet_balance_snapshots_wallet_date_source",
            "wallet_id",
            "snapshot_date",
            "source",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    wallet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wallet.id"), nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    balance: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ReconciliationCase(Base):
    __tablename__ = "reconciliation_cases"
    __table_args__ = (
        Index("ix_reconciliation_cases_status", "status"),
        Index("ix_reconciliation_cases_kind_status", "kind", "status"),
        Index(
            "uq_reconciliation_cases_pending_operation_kind",
            "kind",
            "bank_operation_id",
            unique=True,
            postgresql_where=text("status = 'pending' AND bank_operation_id IS NOT NULL"),
        ),
        Index(
            "uq_reconciliation_cases_pending_invalid_credentials",
            "kind",
            "provider",
            unique=True,
            postgresql_where=text("status = 'pending' AND kind = 'invalid_credentials'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    bank_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("bank_operations.id"), nullable=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    resolution_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SafeAllocation(Base):
    """Резерв (аллокация) средств подотчётного счёта «Сейф».

    Намерение потратить часть остатка Сейфа на конкретного получателя/статью —
    деньги ещё на карте, поэтому резерв НЕ создаёт cashflow и НЕ двигает баланс
    Сейфа, лишь уменьшает «свободно». Реальный расход признаётся при оплате:
    каждая оплата (в т.ч. частичная) — это отдельная out-``CashflowTransaction``
    на Сейфе (``source_kind='safe_payout'``, ``source_id`` = этот резерв), а
    ``amount_paid`` хранит их сумму для быстрого отображения/валидации.

    status: ``reserved`` → ``partially_paid`` → ``paid``; ``cancelled`` — отменён
    (оплаченные ноги остаются, неоплаченный остаток освобождается).
    """

    __tablename__ = "safe_allocations"
    __table_args__ = (
        Index("ix_safe_allocations_wallet_id", "wallet_id"),
        Index("ix_safe_allocations_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("wallet.id", ondelete="RESTRICT"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    amount_paid: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=0, server_default="0"
    )
    article_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dds_articles.id"), nullable=True
    )
    counterparty_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("counterparty.id"), nullable=True
    )
    purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="reserved", server_default="reserved"
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

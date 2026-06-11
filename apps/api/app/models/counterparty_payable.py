from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import (
    counterparty_collection_source_kind_enum,
    counterparty_invoice_source_enum,
    counterparty_invoice_status_enum,
    invoice_allocation_source_enum,
)

# Draft status mirrors PayrollBankDraft (created → updated → paid / failed) so the
# bank-draft flow stays consistent across payroll and counterparty payments.
PAYMENT_DRAFT_STATUSES = ("created", "updated", "paid", "failed")


class CounterpartyLedgerCategory(Base):
    """Ledger tab for the counterparties page (продукты, маркетинг, налоги, …)."""

    __tablename__ = "counterparty_ledger_category"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CounterpartyPayableProfile(Base):
    """Operational profile over the shared ``counterparty`` master.

    Holds payable-specific attributes (ledger, requisites, payment terms) without
    polluting the shared counterparty record used by DDS classification.
    """

    __tablename__ = "counterparty_payable_profile"
    __table_args__ = (
        UniqueConstraint("counterparty_id", name="uq_counterparty_payable_profile_cp"),
        CheckConstraint(
            "payment_due_day_of_month is null "
            "or (payment_due_day_of_month >= 1 and payment_due_day_of_month <= 31)",
            name="ck_payable_profile_due_day",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    counterparty_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("counterparty.id", ondelete="CASCADE"), nullable=False
    )
    ledger_category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("counterparty_ledger_category.id", ondelete="SET NULL"), nullable=True
    )
    # Optional brand grouping for analytics ("Амай" over ООО «ТОРА» + ИП Скачкова).
    brand_group: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # Legacy internal name carried from DDS/iiko; the menu shows the legal name.
    internal_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Payment terms (mutually exclusive hints): N days after delivery, OR pay by the
    # Nth day of the month. Explicit iiko dueDate wins over both.
    payment_delay_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payment_due_day_of_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Supplier-side contact (manager) for questions about invoices/payments.
    manager_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    manager_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requisites: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    requisites_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    requisites_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    requisites_verified_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CounterpartyCollectionSource(Base):
    """How invoices for this counterparty are collected (iiko / email / telegram / manual).

    Doubles as the routing index for incoming documents: an email sender or telegram
    handle maps to exactly one counterparty (global unique on the identifier), so the
    email/telegram channels (Phase 2/3) can attribute new invoices automatically.
    """

    __tablename__ = "counterparty_collection_source"
    __table_args__ = (
        Index(
            "uq_collection_source_cp_kind_value",
            "counterparty_id",
            "kind",
            "value",
            unique=True,
        ),
        Index(
            "uq_collection_source_value_ci",
            func.lower(text("value")),
            unique=True,
            postgresql_where=text("value IS NOT NULL"),
        ),
        Index("ix_collection_source_cp", "counterparty_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    counterparty_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("counterparty.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(
        counterparty_collection_source_kind_enum, nullable=False
    )
    # Channel identifier: email address, telegram handle, iiko supplier GUID; NULL for manual.
    value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CounterpartyRoutingRule(Base):
    """Routes one iiko supplier ("brand") to several legal entities by doc-number prefix.

    iiko exposes a single supplier GUID for a brand like «Амай», but the supplier's
    own document number encodes the legal entity in its prefix (ТРКА → ООО «ТОРА»,
    0ЭКА → ИП Скачкова). On ingest the prefix picks the right payable counterparty.
    """

    __tablename__ = "counterparty_routing_rule"
    __table_args__ = (
        UniqueConstraint(
            "iiko_supplier_guid", "prefix", name="uq_routing_rule_guid_prefix"
        ),
        Index("ix_routing_rule_guid", "iiko_supplier_guid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    iiko_supplier_guid: Mapped[str] = mapped_column(String(64), nullable=False)
    prefix: Mapped[str] = mapped_column(String(64), nullable=False)
    counterparty_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("counterparty.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CounterpartyPaymentDraft(Base):
    """Bank payment draft covering one or more invoices of a single legal entity."""

    __tablename__ = "counterparty_payment_draft"
    __table_args__ = (
        CheckConstraint(
            "status in ('created', 'updated', 'paid', 'failed')",
            name="ck_counterparty_payment_draft_status",
        ),
        Index("ix_counterparty_payment_draft_cp", "counterparty_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    counterparty_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("counterparty.id", ondelete="RESTRICT"), nullable=False
    )
    document_id: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    provider_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SupplierInvoice(Base):
    """A concrete payment obligation collected from a source channel.

    Distinct from the payment calendar (a plan) and УДКЗ (balances): this is the
    "100% must be paid" inbox that feeds the bank-draft flow.
    """

    __tablename__ = "supplier_invoice"
    __table_args__ = (
        # Idempotent sync: one row per (source, external document id).
        Index(
            "uq_supplier_invoice_source_external",
            "source",
            "external_id",
            unique=True,
            postgresql_where=text("external_id IS NOT NULL"),
        ),
        Index("ix_supplier_invoice_cp", "counterparty_id"),
        Index("ix_supplier_invoice_status", "payment_status"),
        Index("ix_supplier_invoice_draft", "draft_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    counterparty_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("counterparty.id", ondelete="RESTRICT"), nullable=False
    )
    source: Mapped[str] = mapped_column(counterparty_invoice_source_enum, nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    invoice_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    # VAT is part of the gross ``amount``. ``vat_breakdown`` maps a rate ("10", "22")
    # to its VAT amount (as a string) so the bank payment purpose can state it.
    vat_total: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0, server_default="0"
    )
    vat_breakdown: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    payment_status: Mapped[str] = mapped_column(
        counterparty_invoice_status_enum,
        nullable=False,
        default="unpaid",
        server_default="unpaid",
    )
    # The draft this invoice is currently queued in (NULL once paid / not queued).
    draft_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("counterparty_payment_draft.id", ondelete="SET NULL"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    raw_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class InvoicePaymentAllocation(Base):
    """Links part of an invoice to a real cash fact (bank operation or cash txn).

    An invoice is ``paid`` when allocations cover its amount; supports split
    nal/beznal and partial payments. Cash facts stay sourced from the bank / cash
    journal — allocations never create them.
    """

    __tablename__ = "invoice_payment_allocation"
    __table_args__ = (
        CheckConstraint(
            "not (bank_operation_id is not null and cashflow_transaction_id is not null)",
            name="ck_invoice_allocation_single_source",
        ),
        Index("ix_invoice_allocation_invoice", "invoice_id"),
        Index("ix_invoice_allocation_bank_op", "bank_operation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier_invoice.id", ondelete="CASCADE"), nullable=False
    )
    source_kind: Mapped[str] = mapped_column(invoice_allocation_source_enum, nullable=False)
    bank_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("bank_operations.id", ondelete="SET NULL"), nullable=True
    )
    cashflow_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("cashflow_transactions.id", ondelete="SET NULL"), nullable=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

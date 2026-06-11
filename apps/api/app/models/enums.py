from __future__ import annotations

from sqlalchemy import Enum

location_status_enum = Enum("active", "inactive", name="location_status")
employee_status_enum = Enum("active", "inactive", "requires_setup", name="employee_status")
counterparty_type_enum = Enum(
    "legal_entity",
    "individual",
    "bank",
    "tax_authority",
    name="counterparty_type",
)
counterparty_role_enum = Enum(
    "supplier",
    "customer",
    "bank",
    "employee",
    "owner",
    "tax_authority",
    "partner",
    name="counterparty_role_type",
)
wallet_type_enum = Enum(
    "bank_account",
    "cash",
    "fund",
    "deposit",
    "bank",
    "cash_register",
    "cash_safe",
    "store_cash",
    "reserve",
    name="wallet_type",
)
period_type_enum = Enum("month", "week", "day", name="period_type")
period_status_enum = Enum("open", "closed", "finalized", name="period_status")
data_source_type_enum = Enum(
    "api",
    "sheets",
    "paper_ocr",
    "manual",
    "ai_email",
    "browser_lk",
    name="data_source_type",
)
parsed_document_status_enum = Enum(
    "extracted",
    "auto_confirmed",
    "needs_review",
    "rejected",
    name="parsed_document_status",
)
quality_status_enum = Enum(
    "draft",
    "partial",
    "final",
    "requires_review",
    "not_applicable",
    name="quality_status",
)
counterparty_invoice_source_enum = Enum(
    "iiko",
    "manual",
    "email",
    "telegram",
    name="counterparty_invoice_source",
)
counterparty_invoice_status_enum = Enum(
    "unpaid",
    "partially_paid",
    "paid",
    "void",
    name="counterparty_invoice_status",
)
invoice_allocation_source_enum = Enum(
    "bank",
    "cash",
    name="invoice_allocation_source",
)
counterparty_collection_source_kind_enum = Enum(
    "iiko",
    "email",
    "telegram",
    "manual",
    name="counterparty_collection_source_kind",
)

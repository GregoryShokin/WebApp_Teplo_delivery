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
# Payment relationship axis (orthogonal to the legal form in counterparty_type):
# official = pay by bank transfer; informal = card/cash only; barter = two-way
# (incoming AP + outgoing AR) settled by net balance.
counterparty_relationship_enum = Enum(
    "official",
    "informal",
    "barter",
    name="counterparty_relationship",
)
# Invoice direction: payable = iiko incomingInvoice (we owe), receivable = iiko
# outgoingInvoice (they owe us — only barter partners get these).
supplier_invoice_direction_enum = Enum(
    "payable",
    "receivable",
    name="supplier_invoice_direction",
)
# Explicit barter role set at creation (NULL = ordinary or iiko-synced invoice whose
# role is still derived chronologically): loan = a loan in kind, return = it settles an
# earlier loan (linked via barter_loan_id, may be partial).
supplier_invoice_barter_role_enum = Enum(
    "loan",
    "return",
    name="supplier_invoice_barter_role",
)

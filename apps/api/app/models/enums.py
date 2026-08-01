from __future__ import annotations

from sqlalchemy import Enum

location_status_enum = Enum("active", "inactive", name="location_status")
employee_status_enum = Enum(
    "active", "inactive", "requires_setup", "dismissing", name="employee_status"
)
counterparty_type_enum = Enum(
    "legal_entity",
    "individual",
    "bank",
    "tax_authority",
    name="counterparty_type",
)
counterparty_role_enum = Enum(
    "supplier",
    "landlord",
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
    # kassa_cheque = чек со страницы «Касса»: уже оплаченная картой покупка,
    # хранится в supplier_invoice как paid-накладная (см. модуль «Касса»).
    "kassa_cheque",
    # kassa_invoice = накладная, созданная вручную на странице «Касса» (вкладка
    # «Накладные» Кассы фильтрует по нему; в «Управлении складом» видны все).
    "kassa_invoice",
    # sbis = счёт/УПД/акт сервисного поставщика, материализованный из СБИС ЭДО
    # (только для контрагентов с каналом сбора 'sbis'; производственные — зеркало).
    "sbis",
    # lease = ежемесячное обязательство по договору аренды (генератор lease_accruals):
    # документов арендодатель не выставляет, долг считаем сами по договору.
    "lease",
    # self_billed = внутренний закрывающий документ по абонентскому платежу
    # (subscription_accruals): контрагент его не выставлял, поэтому расход признаётся в P&L,
    # но в налоговую базу УСН не идёт — первички под ним нет. Замещается настоящим УПД.
    "self_billed",
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
    # Ручной пендинг-чек Кассы: карточная часть введена вручную, банк ещё не подтвердил
    # (при матче операции заменяется на 'bank'). Значение добавлено миграцией 0129.
    "card_pending",
    # Гашение накладной против ранее выданной предоплаты поставщику (дебиторка). Деньги
    # НЕ двигает — они ушли при создании предоплаты. Значение добавлено миграцией 0140.
    "prepayment",
    # Взаимозачёт бартерных поставок: долг закрыт ТОВАРОМ, денег не было вовсе. Источник —
    # ``barter_settlement_id`` (ни банка, ни проводки, ни предоплаты). Значение добавлено
    # миграцией 0199. Счётчики «сколько ДЕНЕГ потрачено» такую аллокацию не видят: они ищут
    # по ключу платежа (проводка/операция), а здесь все ключи NULL.
    "barter",
    name="invoice_allocation_source",
)
counterparty_collection_source_kind_enum = Enum(
    "iiko",
    "email",
    "telegram",
    "manual",
    "sbis",
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
# Канон ДЗ/КЗ (владелец 2026-07-17): роль документа в взаиморасчётах.
#   bill    = счёт на оплату: основание для платежа (очередь «Платежи»/«Страница на
#             оплату»), но НЕ долг — в баланс ДЗ/КЗ не входит; оплата счёта из банка сама
#             формирует ДЗ (аванс) либо гасит КЗ (см. ensure_prepayment_from_bank_transaction).
#   closing = закрывающий документ (УПД/акт/приходная накладная/чек): факт выполненных
#             работ. Гасит открытую дебиторку и/или создаёт кредиторку (правило 2). Именно
#             closing формирует КЗ дашборда.
supplier_invoice_doc_kind_enum = Enum(
    "bill",
    "closing",
    name="supplier_invoice_doc_kind",
)
# Операционный контур документа не выводится из source/doc_kind: закрывающим документом
# бывает и товарная накладная, и сервисный УПД. warehouse участвует в складском UI/iiko,
# finance живёт в оплате и ДЗ/КЗ, unknown страхует исторические неоднозначные записи.
supplier_invoice_operational_scope_enum = Enum(
    "warehouse",
    "finance",
    "unknown",
    name="supplier_invoice_operational_scope",
)

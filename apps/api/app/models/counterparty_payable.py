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
    counterparty_relationship_enum,
    invoice_allocation_source_enum,
    supplier_invoice_barter_role_enum,
    supplier_invoice_direction_enum,
    supplier_invoice_doc_kind_enum,
    supplier_invoice_operational_scope_enum,
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
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
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
        CheckConstraint(
            "default_service_period_offset_months is null "
            "or default_service_period_offset_months between -12 and 12",
            name="ck_payable_profile_service_period_offset",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    counterparty_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("counterparty.id", ondelete="CASCADE"), nullable=False
    )
    ledger_category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("counterparty_ledger_category.id", ondelete="SET NULL"), nullable=True
    )
    # Закреплённая за контрагентом статья ДДС: предзаполняет окно оплаты на «Странице на оплату»
    # (не-складские поставщики — ПО/реклама). None → «Оплата поставщикам».
    default_dds_article_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dds_articles.id", ondelete="SET NULL"), nullable=True
    )
    # Пустая статья — решение, а не недозаполненность: снимает разницу между «статью ещё не
    # выбрали» и «статьи у контрагента нет». Решение принимается один раз и хранится, иначе
    # карточку без статьи нельзя сохранить, не подтвердив это заново при каждом открытии.
    confirm_no_dds_article: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Optional brand grouping for analytics ("Амай" over ООО «ТОРА» + ИП Скачкова).
    brand_group: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # Legacy internal name carried from DDS/iiko; the menu shows the legal name.
    internal_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Payment terms (mutually exclusive hints): N days after delivery, OR pay by the
    # Nth day of the month. Explicit iiko dueDate wins over both.
    payment_delay_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payment_due_day_of_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Услуговые контрагенты обязаны иметь период оказания услуг на каждом платеже: от него
    # зависит месяц признания расхода. Из PDF-счёта период извлекается автоматически всегда
    # (см. invoice_recognition), поэтому режима «автоматически/вручную» тут нет — флаг решает
    # только «обязателен ли», а не «откуда брать».
    service_period_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Преднабор дат для РУЧНОГО платежа, у которого счёта нет вовсе (-1 прошлый месяц,
    # 0 текущий, +1 следующий). Счёта не касается и явный период не заменяет.
    default_service_period_offset_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Предоплатная модель по банк-фиду (Манго: авто-списания с карты): каждое исходящее
    # списание в пользу контрагента из выписки пополняет дебиторку (supplier_prepayment
    # без новой ДДС-проводки), закрывающий УПД из СБИС гасит её.
    bank_payments_create_prepayment: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Supplier-side contact (manager) for questions about invoices/payments.
    manager_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    manager_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Payment relationship: official (bank transfer) / informal (card-cash only) /
    # barter (two-way, settled by net balance). Auto-set to barter on receivable ingest,
    # unless pinned manually (``relationship_manual``).
    relationship: Mapped[str] = mapped_column(
        counterparty_relationship_enum,
        nullable=False,
        default="official",
        server_default="official",
    )
    # Ручной замок: True, когда владелец явно сменил тип отношений в карточке. Пока стоит,
    # авто-классификация из iiko-синхронизации (расходная накладная → barter) НЕ перебивает
    # выбор. Снимать не нужно — повторная смена типа просто держит его закреплённым.
    relationship_manual: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Доступен для выбора при создании накладной в контуре «Касса».
    # На Склад и ДДС-классификацию не влияет; курируется вручную, по умолчанию выкл.
    kassa_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
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
    kind: Mapped[str] = mapped_column(counterparty_collection_source_kind_enum, nullable=False)
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
        UniqueConstraint("iiko_supplier_guid", "prefix", name="uq_routing_rule_guid_prefix"),
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
            "status in ('created', 'updated', 'paid', 'failed', 'deleted')",
            name="ck_counterparty_payment_draft_status",
        ),
        Index("ix_counterparty_payment_draft_cp", "counterparty_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # NULL — черновик «просто траты» без получателя (свободный вывод на Сейф по статье,
    # окно «Новый платёж»): деньги уходят на карту ИП, контрагента у платежа нет.
    counterparty_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("counterparty.id", ondelete="RESTRICT"), nullable=True
    )
    document_id: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    provider_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Standalone-черновик «банк по реквизитам» без накладной: при статусе «исполнен» вместо
    # гашения накладных создаёт предоплату (дебиторку) на контрагента по этой статье.
    creates_prepayment: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    prepayment_article_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dds_articles.id", ondelete="SET NULL"), nullable=True
    )
    # Выплата через Сейф (неофициальный поставщик): черновик выписан на карту ИП, при
    # статусе «исполнен» вместо расхода «Оплата поставщикам» заводится перевод р/с→Сейф
    # и целевой резерв Сейфа на поставщика; накладные гасятся аллокациями без ДДС-расхода.
    pays_via_safe: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Черновик-пополнение Сейфа без резерва (внутренний перевод банк→Сейф из «Нового
    # платежа»): при оплате книжится только транзит р/с→Сейф, целёвка НЕ создаётся.
    topup_only: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Расход из окна «Новый платёж»: целевая статья и назначение. Для via-safe черновика
    # paid-переход создаёт целёвку Сейфа; для официального контрагента — прямую расходную
    # проводку по реквизитам (counterparty_id задан, pays_via_safe=false).
    target_article_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dds_articles.id", ondelete="SET NULL"), nullable=True
    )
    target_purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Банк-плательщик черновика: ``tbank`` (по умолчанию) / ``sber``. Сбер задаётся только у
    # свободного вывода на Сейф (окно «Новый платёж», expense); накладные/предоплата/informal
    # остаются в Т-Банке. По нему paid-переход книжит транзит р/с→Сейф на счёт нужного банка.
    bank_provider: Mapped[str] = mapped_column(
        String(16), nullable=False, default="tbank", server_default=text("'tbank'")
    )
    # Единый период черновика. Для пачки накладных он заполняется только когда все счета
    # имеют одинаковый период; разные периоды на уровне сервиса запрещены.
    service_period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    service_period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ExpenseDraftLine(Base):
    """Строка банковского расхода из окна «Новый платёж».

    Via-safe черновик без официального получателя может нести несколько статей: сумма
    черновика = Σ строк, paid-переход заводит по целёвке Сейфа на строку. Прямой платёж
    официальному контрагенту всегда содержит одну строку и расходуется по ней сразу.
    """

    __tablename__ = "expense_draft_line"
    __table_args__ = (Index("ix_expense_draft_line_draft", "draft_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    draft_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("counterparty_payment_draft.id", ondelete="CASCADE"), nullable=False
    )
    article_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dds_articles.id", ondelete="RESTRICT"), nullable=False
    )
    # Контрагент строки (атрибуция расхода): для статей свободного вывода с привязанными
    # контрагентами. Целёвка Сейфа помечается им; деньги идут ИП→Сейф. NULL — «просто трата».
    counterparty_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("counterparty.id", ondelete="SET NULL"), nullable=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # Период оказания услуги для ручного платежа. Хранится на строке, потому что транш через
    # Сейф может содержать несколько статей/получателей.
    service_period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    service_period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    service_period_source: Mapped[str | None] = mapped_column(String(24), nullable=True)


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
    # payable = iiko incomingInvoice (we owe); receivable = iiko outgoingInvoice
    # (barter partner owes us). Net barter balance = Σpayable − Σreceivable.
    direction: Mapped[str] = mapped_column(
        supplier_invoice_direction_enum,
        nullable=False,
        default="payable",
        server_default="payable",
    )
    # Роль документа в взаиморасчётах (канон ДЗ/КЗ, владелец 2026-07-17). closing (УПД/акт/
    # приходная/чек) — факт работ: гасит дебиторку / создаёт кредиторку, формирует КЗ дашборда.
    # bill (счёт на оплату) — только основание для платежа: живёт в очереди оплат, в баланс
    # ДЗ/КЗ НЕ входит. Дефолт 'closing' безопасен: незамеченный документ считается
    # обязательством, а не молча пропадает из кредиторки.
    doc_kind: Mapped[str] = mapped_column(
        supplier_invoice_doc_kind_enum,
        nullable=False,
        default="closing",
        server_default="closing",
    )
    # Где документ живёт операционно. Это отдельная ось от роли bill/closing:
    # товарный closing показывается в «Управлении складом», сервисный closing — только
    # в финансовом контуре ДЗ/КЗ; bill всегда остаётся основанием для платежа.
    operational_scope: Mapped[str] = mapped_column(
        supplier_invoice_operational_scope_enum,
        nullable=False,
        default="unknown",
        server_default="unknown",
    )
    # Отложенное вступление закрывающего документа в силу по ДАТЕ ДОКУМЕНТА (правило 4 канона):
    # контрагент присылает УПД заранее (ЭкоЦентр — УПД июля датирован 31.07). Будущий closing
    # ждёт в статусе 'pending' (в КЗ не входит, дебиторку не гасит) и активируется ночной джобой
    # в свою дату → 'active' (гасит аванс / встаёт в КЗ). bill'ы всегда 'active' (вне контура).
    activation_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="active",
        server_default="active",
    )
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    invoice_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Период, к которому относится услуга. ``status``: not_required / missing / ready /
    # ambiguous. Для ambiguous даты не выбираются автоматически — счёт идёт на ручной разбор.
    service_period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    service_period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    service_period_source: Mapped[str | None] = mapped_column(String(24), nullable=True)
    service_period_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="not_required", server_default="not_required"
    )
    service_period_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    # VAT is part of the gross ``amount``. ``vat_breakdown`` maps a rate ("10", "22")
    # to its VAT amount (as a string) so the bank payment purpose can state it.
    vat_total: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0, server_default="0"
    )
    vat_breakdown: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # iiko line items — captured for barter partners only, for номенклатура-based loan
    # matching: list of {product_id, article, name, quantity, amount}. Empty otherwise.
    line_items: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
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
    # Barter loan settlement (зачёт) this invoice was netted into (NULL = not settled).
    barter_settlement_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("barter_settlement.id", ondelete="SET NULL"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Статья ДДС для оплаты этого счёта (счета услуг со «Страницы на оплату» — ПО/реклама/
    # техподдержка). None → дефолтная «Оплата поставщикам» при гашении (apply_payment_status).
    dds_article_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dds_articles.id", ondelete="SET NULL"), nullable=True
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    raw_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # --- warehouse «Накладные» operational fields (manual create + iiko push) ---
    # Receipt date+TIME (manual create requires it) — the basis for auto-matching the
    # payment against a bank operation; invoice_date (Date) stays for compatibility.
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Σ of line sums flagged «персонал» — excluded from the iiko document, tracked as a
    # separate cash-flow (future dedicated DDS article). 0 for ordinary invoices.
    staff_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0, server_default="0"
    )
    store_guid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    iiko_push_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="not_pushed", server_default="not_pushed"
    )
    iiko_pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    iiko_push_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # --- iiko: контур коррекции ОПЛАЧЕННОЙ накладной (Фаза 2) ---
    # Оплаченный iiko-документ править нельзя (unpost/add_payment необратимы), поэтому коррекцию
    # книжим полным разворотом двумя документами: (1) возврат ВСЕХ старых товаров со ссылкой на
    # incomingInvoiceId оригинала → минус товары + долг поставщика (дебиторка); (2) НОВАЯ приходная
    # на правильные товары БЕЗ оплаты. Итог: чистый баланс поставщика = дебиторка (старая − новая).
    # Оплачивать новую зачётом НЕЛЬЗЯ — settle-дебет уезжает в баланс и завышает его (проверено на
    # живом API). Чек-пойнты саги (для ретрая):
    #   iiko_return_external_id         — возвратная накладная (шаг 1);
    #   iiko_correction_new_external_id — новая приходная Y (шаг 2), external_id
    #   перецепляется на неё.
    # Статусы iiko_return_status как у iiko_push: none/pending/booked/failed/skipped.
    iiko_return_external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    iiko_return_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="none", server_default="none"
    )
    iiko_return_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # id новой (правильной) приходной Y в iiko — чек-пойнт шага 2, чтобы ретрай не пересоздал её.
    # После успеха шага 2 external_id накладной перецепляется X→Y, старый X закрывается тумбстоном.
    iiko_correction_new_external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Снимок ВСЕХ старых товаров (для возврата), ждущий проводки/ретрая; очищается при успехе.
    # Список {product, quantity, price, name}. Склад/дату оркестратор добавляет при пуше.
    iiko_return_lines: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    # --- explicit barter (NULL = ordinary / iiko-synced, role derived chronologically) ---
    barter_role: Mapped[str | None] = mapped_column(
        supplier_invoice_barter_role_enum, nullable=True
    )
    # On a return invoice: the specific loan it settles. Self-FK.
    barter_loan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("supplier_invoice.id", ondelete="SET NULL"), nullable=True
    )
    # On a loan invoice: cached settlement state — open / partially_returned / returned.
    barter_return_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    # Прощённый хвост товарного долга: вернули меньше выданного и владелец решил «забыть»
    # остаток (недовес в пределах пары сотен грамм). Входит в зачётную стоимость займа —
    # так заём закрывается, а его КЗ не висит вечным огрызком.
    barter_writeoff_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, server_default="0"
    )
    # --- контроль ошибочных цен (скользящее среднее по товару) ---
    # Позиция накладной, у которой цена сильно отклонилась от скользящего среднего закупок этого
    # товара (порог +N%/−M%), помечает всю накладную «подозрительной». Пока статус ``flagged`` —
    # оплата и отправка в банк заблокированы (assert_price_cleared); человек сверяет и подтверждает
    # («ОК, всё верно») → ``confirmed`` разблокирует. Любая правка позиций пересчитывает контроль и
    # сбрасывает подтверждение. ``clean`` — аномалий нет / контроль неприменим
    # (бартер, нет истории).
    price_control_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="clean", server_default="clean"
    )
    # Снимок аномальных строк на момент расчёта: список
    # {name, product_guid, unit, price, avg_price, sample_count, deviation_pct, direction}.
    price_anomalies: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    price_confirmed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    price_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SupplierExpenseAccrual(Base):
    """Журнал признания расходов поставщиков по периоду оказания услуг.

    До окончания периода оплаченная сумма является выданным авансом (дебиторкой). После
    окончания периода строка признаётся в P&L; неоплаченный остаток становится кредиторкой.
    Сам денежный факт остаётся в ДДС и не смешивается с этим журналом начислений.
    """

    __tablename__ = "supplier_expense_accrual"
    __table_args__ = (
        CheckConstraint(
            "status in ('scheduled', 'recognized', 'cancelled')",
            name="ck_supplier_expense_accrual_status",
        ),
        CheckConstraint(
            "service_period_end >= service_period_start",
            name="ck_supplier_expense_accrual_period",
        ),
        UniqueConstraint("invoice_id", name="uq_supplier_expense_accrual_invoice"),
        UniqueConstraint("expense_draft_line_id", name="uq_supplier_expense_accrual_line"),
        Index("ix_supplier_expense_accrual_counterparty", "counterparty_id"),
        Index("ix_supplier_expense_accrual_due", "status", "service_period_end"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    counterparty_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("counterparty.id", ondelete="RESTRICT"), nullable=False
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("supplier_invoice.id", ondelete="CASCADE"), nullable=True
    )
    expense_draft_line_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("expense_draft_line.id", ondelete="CASCADE"), nullable=True
    )
    payment_draft_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("counterparty_payment_draft.id", ondelete="SET NULL"), nullable=True
    )
    article_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dds_articles.id", ondelete="SET NULL"), nullable=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    service_period_start: Mapped[date] = mapped_column(Date, nullable=False)
    service_period_end: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="scheduled", server_default="scheduled"
    )
    recognition_month: Mapped[date | None] = mapped_column(Date, nullable=True)
    recognized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class SupplierServicePeriodChange(Base):
    """Аудит переноса периода, включая корректировки уже признанного P&L."""

    __tablename__ = "supplier_service_period_change"
    __table_args__ = (Index("ix_supplier_service_period_change_accrual", "accrual_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    accrual_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier_expense_accrual.id", ondelete="CASCADE"), nullable=False
    )
    old_period_start: Mapped[date] = mapped_column(Date, nullable=False)
    old_period_end: Mapped[date] = mapped_column(Date, nullable=False)
    new_period_start: Mapped[date] = mapped_column(Date, nullable=False)
    new_period_end: Mapped[date] = mapped_column(Date, nullable=False)
    old_recognition_month: Mapped[date | None] = mapped_column(Date, nullable=True)
    new_recognition_month: Mapped[date | None] = mapped_column(Date, nullable=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class InvoiceLineItem(Base):
    """A normalized line of a warehouse invoice — quantity, unit, sum, «персонал» flag.

    Manual/warehouse invoices store lines here (the source of truth) for accurate per-item
    quantities, partial barter returns, and the personnel split. ``is_staff`` lines are
    kept out of the pushed iiko document but still counted in the invoice total and routed
    as a separate cash-flow. iiko-synced invoices keep using ``SupplierInvoice.line_items``
    (JSONB); new invoices mirror a minimal slice there so номенклатура-based barter matching
    keeps working.
    """

    __tablename__ = "invoice_line_item"
    __table_args__ = (
        Index("ix_invoice_line_item_invoice", "invoice_id"),
        Index("ix_invoice_line_item_product", "iiko_product_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier_invoice.id", ondelete="CASCADE"), nullable=False
    )
    iiko_product_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("iiko_product.id", ondelete="SET NULL"), nullable=True
    )
    # GUID snapshot at creation — survives the product later being removed from the cache.
    product_guid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    article: Mapped[str | None] = mapped_column(String(128), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    # quantity * price, gross (VAT-inclusive, как у iiko).
    sum: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    vat_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    vat_sum: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    # Статья ДДС позиции (чек местного закупа разносится постро́чно по статьям).
    dds_article_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dds_articles.id", ondelete="SET NULL"), nullable=True
    )
    is_staff: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Позиция возвращена в магазин (чек Кассы): остаётся в чеке для сверки «копейка в
    # копейку» с бумажным чеком/операцией, но НЕ проводится (ДДС/iiko/аллокации = net).
    is_return: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class InvoicePaymentAllocation(Base):
    """Links part of an invoice to a real cash fact (bank operation or cash txn).

    An invoice is ``paid`` when allocations cover its amount; supports split
    nal/beznal and partial payments. Cash facts stay sourced from the bank / cash
    journal — allocations never create them.
    """

    __tablename__ = "invoice_payment_allocation"
    __table_args__ = (
        # Денежный ключ и предоплатный — взаимоисключающие. А вот операция выписки и проводка
        # ДДС — две метки ОДНОГО платёжного факта: доля разбора операции несёт обе (операция для
        # прежних дверей, проводка — чтобы «бюджет платежа» считался по доле, а не по всей
        # операции, см. ``payment_allocated_amount``).
        CheckConstraint(
            "((bank_operation_id is not null or cashflow_transaction_id is not null)::int "
            "+ (prepayment_id is not null)::int) <= 1",
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
    # Гашение против выданной предоплаты (source_kind='prepayment'): деньги уже ушли при
    # создании предоплаты, поэтому ни bank_operation_id, ни cashflow_transaction_id здесь нет.
    prepayment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("supplier_prepayment.id", ondelete="SET NULL"), nullable=True
    )
    # Взаимозачёт бартерных поставок (source_kind='barter'): долг закрыт товаром, денежного
    # ключа нет. CASCADE — распуск зачёта снимает его аллокации, статус пересчитывается сам.
    barter_settlement_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("barter_settlement.id", ondelete="CASCADE"), nullable=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BarterSettlement(Base):
    """A netting (зачёт) of barter loan invoices for one partner.

    Ties a set of payable + receivable invoices of equal total whose номенклатура
    matches; involved invoices point back via ``SupplierInvoice.barter_settlement_id``
    and drop out of the open barter balance. ``is_auto`` marks a 100%-confident
    auto-settlement (sum + product set both exact) vs a manager-confirmed one.
    """

    __tablename__ = "barter_settlement"
    __table_args__ = (Index("ix_barter_settlement_cp", "counterparty_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    counterparty_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("counterparty.id", ondelete="CASCADE"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    is_auto: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BarterReturnLine(Base):
    """One partial settlement of a barter loan — by product line and/or by amount.

    Товарное гашение: возвратная накладная (``barter_role == "return"``) закрывает часть
    займа, строка несёт количество (``quantity``) и сумму по ИСХОДНОЙ цене займа. Денежное
    гашение: строка ссылается на ДДС-проводку денег (``cashflow_transaction_id``), ``amount``
    — фактические деньги (по ТЕКУЩЕЙ цене для нашего займа), а в зачёт долга идёт
    qty × исходная цена (долг номинирован товаром). Статус займа считает
    ``warehouse_invoices.sync_barter_loan_status`` по зачётной стоимости."""

    __tablename__ = "barter_return_line"
    __table_args__ = (
        CheckConstraint(
            "(return_invoice_id IS NOT NULL) OR (cashflow_transaction_id IS NOT NULL)",
            name="ck_barter_return_has_source",
        ),
        Index("ix_barter_return_loan", "loan_invoice_id"),
        Index("ix_barter_return_return", "return_invoice_id"),
        Index("ix_barter_return_cashflow_tx", "cashflow_transaction_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Товарное гашение — возвратная накладная; денежное — ДДС-проводка (склад не двигается,
    # накладной нет). Хотя бы один источник обязателен (CHECK).
    return_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("supplier_invoice.id", ondelete="CASCADE"), nullable=True
    )
    cashflow_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("cashflow_transactions.id", ondelete="RESTRICT"), nullable=True
    )
    loan_invoice_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier_invoice.id", ondelete="RESTRICT"), nullable=False
    )
    loan_line_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("invoice_line_item.id", ondelete="SET NULL"), nullable=True
    )
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SupplierInvoiceTombstone(Base):
    """Marker for a supplier invoice that was intentionally deleted and must NOT be
    re-imported by the cyclic iiko sync.

    The reverse sync upserts iiko documents by ``external_id`` (the iiko doc id). When
    we hard-delete an invoice that still exists (PROCESSED) in iiko and sits inside the
    sync window, the next run would re-create it. A tombstone keyed by
    ``(source, external_id)`` tells the sync to skip that document forever — the key is
    the iiko doc id, so it survives the row being gone.
    """

    __tablename__ = "supplier_invoice_tombstone"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_supplier_invoice_tombstone"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="iiko")
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SupplierPrepayment(Base):
    """Выданная предоплата поставщику (дебиторка): мы заплатили вперёд — поставщик должен.

    Создаётся реальным расходом денег (out-CashflowTransaction, source_kind=
    'supplier_prepayment', source_id=prepayment.id), обычно с кошелька «Сейф». Гасится
    приходящими payable-накладными через InvoicePaymentAllocation(source_kind='prepayment',
    prepayment_id=...), которая денег НЕ двигает. Это отдельный учёт от кредиторки
    (неоплаченные накладные) и от товарного бартера (receivable-накладные).

    Остаток = amount − amount_settled («поставщик нам ещё должен»).
    """

    __tablename__ = "supplier_prepayment"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_supplier_prepayment_amount_positive"),
        CheckConstraint(
            "amount_settled >= 0 and amount_settled <= amount",
            name="ck_supplier_prepayment_settled_range",
        ),
        Index("ix_supplier_prepayment_counterparty", "counterparty_id"),
        Index("ix_supplier_prepayment_status", "status"),
        # Не более одной ДЗ на счёт (единый чокпоинт): частичный уникальный индекс — bill_invoice_id
        # ставит только kind='prepaid_bill'. Служит и lookup-индексом.
        Index(
            "uq_supplier_prepayment_bill",
            "bill_invoice_id",
            unique=True,
            postgresql_where=text("bill_invoice_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    counterparty_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("counterparty.id", ondelete="RESTRICT"), nullable=False
    )
    # Тип аванса для строки баланса «Выданные авансы»: goods/rent/ad/subscription/tax/other.
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="goods")
    wallet_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("wallet.id", ondelete="SET NULL"), nullable=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    amount_settled: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0, server_default=text("0")
    )
    # open → partially_settled → settled; refunded — предоплата возвращена поставщиком.
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="open", server_default="open"
    )
    # Денежный факт, породивший предоплату (out-CashflowTransaction).
    cashflow_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("cashflow_transactions.id", ondelete="SET NULL"), nullable=True
    )
    article_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dds_articles.id", ondelete="SET NULL"), nullable=True
    )
    # Исходный счёт (doc_kind='bill'), оплата которого завела эту ДЗ (kind='prepaid_bill'). Даёт
    # идемпотентность единого чокпоинта: одна ДЗ на счёт, синхронизируется с его оплаченной суммой
    # при каждом _recompute_status (reconcile_bill_prepayment). Для прочих предоплат NULL.
    bill_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("supplier_invoice.id", ondelete="SET NULL"), nullable=True
    )
    # Исторические незакрытые авансы мигрируют со статусом missing: они остаются в дебиторке,
    # но попадают в отдельную очередь распределения периода, а не признаются автоматически.
    service_period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    service_period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    service_period_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="missing", server_default="missing"
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class IikoInvoicePaymentPush(Base):
    """Идемпотентность пуша оплаты накладной в iiko (Cloud ``add_payment``).

    Когда у нас iiko-накладная стала «оплачено» через банк, дублируем платёж в iiko через
    ``/api/inventory/v1/incoming_invoice/modify/add_payment`` (тело ``{organizationId, documentId,
    paymentDate, accountId, amount}``; ``accountId`` = счёт-источник денег). Только для накладных,
    существующих в iiko (есть ``external_id`` = iiko documentId). Метод НЕ идемпотентен, поэтому
    фиксируем факт по ``idempotency_key`` (``invoice:<id>``); статусы: ``pending`` (in-flight,
    записан ДО HTTP), ``ok`` (отправлено), ``error`` (ретраить до ``attempts`` = кап).
    """

    __tablename__ = "iiko_invoice_payment_push"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_iiko_invoice_payment_push_key"),
        Index("ix_iiko_invoice_payment_push_invoice", "invoice_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("supplier_invoice.id", ondelete="SET NULL"), nullable=True
    )
    # iiko documentId накладной (= наш external_id).
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    # Денежный счёт iiko (Кредит) — куда «ушли» деньги по нашему кошельку.
    account_to: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # ok / error
    # Счётчик попыток отправки (ретраи сверочным джобом, чтобы не долбить iiko бесконечно).
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    iiko_document_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    response_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

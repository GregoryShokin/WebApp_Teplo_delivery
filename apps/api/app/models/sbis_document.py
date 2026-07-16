"""Зеркало входящих документов СБИС (Saby) ЭДО.

СБИС — НЕ источник накладных к оплате (решение владельца 2026-07-16): к оплате ведёт
iiko-синк, а документы ЭДО подтягиваются для сверки — «пришло по ЭДО, но не занесено
в iiko» подсвечивается оператору. Одна строка = один документ реестра «Входящие»;
идемпотентность — по идентификатору документа СБИС.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Статусы сверки с iiko-накладными:
#   unmatched — пара в supplier_invoice не найдена (главный сигнал оператору)
#   matched   — связан с накладной (matched_invoice_id), правило в match_note
#   dismissed — оператор скрыл документ из сверки (акт сверки, дубль и т.п.)
SBIS_MATCH_STATUSES = ("unmatched", "matched", "dismissed")

# Итог маршрутизации документа (режим определяет карточка контрагента, не документ):
#   mirror           — зеркало: контрагент без канала 'sbis' или тип не материализуемый
#   new_counterparty — контрагент не найден/не настроен (requires_setup) — ждёт настройки,
#                      после включения канала документы материализуются задним числом
#   materialized     — создан счёт SupplierInvoice(source='sbis') (invoice_id)
#   duplicate        — счёт уже существует (пришёл почтой/вручную) — invoice_id на него
#   sent_to_recognition — письмо-счёт (КоррВх, сумма только в PDF) отдано в распознавание
#                      «Страницы на оплату» — дальше живёт как intake
SBIS_INTAKE_STATUSES = (
    "mirror",
    "new_counterparty",
    "materialized",
    "duplicate",
    "sent_to_recognition",
)


class SbisDocument(Base):
    __tablename__ = "sbis_document"
    __table_args__ = (
        Index("uq_sbis_document_doc_id", "sbis_doc_id", unique=True),
        Index("ix_sbis_document_match_status", "match_status"),
        Index("ix_sbis_document_invoice", "matched_invoice_id"),
        Index("ix_sbis_document_date", "doc_date"),
        Index("ix_sbis_document_counterparty", "counterparty_id"),
        Index("ix_sbis_document_intake_status", "intake_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Документ.Идентификатор из СБИС (GUID) — ключ идемпотентности синка.
    sbis_doc_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Документ.Тип (ДокОтгрВх, АктСверВх, ...) и Регламент.Название («Поступление»).
    doc_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    regulation: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    doc_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    amount_wo_vat: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    counterparty_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    counterparty_inn: Mapped[str | None] = mapped_column(String(12), nullable=True)
    state_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    state_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Тип первого неслужебного вложения (УпдСчфДоп и т.п.) — различает УПД и акты.
    attachment_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Ссылки из реестра. Живут ~месяц; периодический синк освежает их на каждом проходе,
    # PDF отдаётся через backend-прокси по текущей ссылке.
    link_cabinet: Mapped[str | None] = mapped_column(Text, nullable=True)
    link_pdf: Mapped[str | None] = mapped_column(Text, nullable=True)
    link_xml: Mapped[str | None] = mapped_column(Text, nullable=True)

    match_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unmatched", server_default="unmatched"
    )
    matched_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("supplier_invoice.id", ondelete="SET NULL"), nullable=True
    )
    # Каким правилом сматчили: number_amount / amount_date / manual.
    match_note: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Маршрутизация (см. SBIS_INTAKE_STATUSES): контрагент по ИНН и созданный из
    # документа счёт (или существующий, если сработал дедуп с почтой/ручным вводом).
    counterparty_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("counterparty.id", ondelete="SET NULL"), nullable=True
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("supplier_invoice.id", ondelete="SET NULL"), nullable=True
    )
    intake_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="mirror", server_default="mirror"
    )

    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

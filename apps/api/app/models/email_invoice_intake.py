from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Статусы разбора почтового вложения:
#   new          — вложение скачано, ещё не распознано
#   recognized   — распознано уверенно, но ещё не материализовано в накладную
#   needs_review — распознано неуверенно / не сматчен контрагент → требует оператора
#   linked       — создана SupplierInvoice(source='email'), привязана (invoice_id)
#   duplicate    — повтор уже существующей накладной (тот же ИНН+сумма+дата+номер); invoice_id
#                  указывает на исходную, новую накладную НЕ создаём
#   failed       — ошибка распознавания (см. error)
#   ignored      — не счёт (нет суммы) / помечено как мусор
EMAIL_INTAKE_STATUSES = (
    "new",
    "recognized",
    "needs_review",
    "linked",
    "duplicate",
    "failed",
    "ignored",
)


class EmailInvoiceIntake(Base):
    """Журнал разбора входящих PDF-счетов/УПД из почты («Страница на оплату»).

    Одна строка = одно PDF-вложение письма. Идемпотентность — по SHA-256 содержимого
    (одно письмо/файл обрабатывается ровно один раз, даже если пришло в оба ящика).
    """

    __tablename__ = "email_invoice_intake"
    __table_args__ = (
        Index("ix_email_invoice_intake_status", "status"),
        Index("ix_email_invoice_intake_invoice", "invoice_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Логический ящик: 'personal' (собственника) / 'corporate' (рабочий).
    mailbox: Mapped[str] = mapped_column(String(32), nullable=False)
    from_addr: Mapped[str | None] = mapped_column(String(320), nullable=True)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    message_uid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attachment_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    attachment_mime: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Ключ идемпотентности (UNIQUE).
    attachment_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    attachment_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Сам PDF — чтобы показать его в UI и переразобрать офлайн.
    pdf_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="new", server_default="new"
    )
    # Каким движком распознано: 'deterministic' / 'llm' / 'deterministic+llm'.
    engine: Mapped[str | None] = mapped_column(String(24), nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    # Распознанные поля + реквизиты + сырой текст (для аудита/переразбора).
    recognition: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    counterparty_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("counterparty.id", ondelete="SET NULL"), nullable=True
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("supplier_invoice.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

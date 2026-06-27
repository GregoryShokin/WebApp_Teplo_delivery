"""Журнал разбора счетов из почты («Страница на оплату», Фаза 1).

Таблица ``email_invoice_intake`` — единый журнал входящих PDF-вложений из двух почтовых
ящиков (личный + корпоративный). Одна строка = одно PDF-вложение письма, идемпотентно по
SHA-256 содержимого (одно и то же письмо/файл не обрабатывается дважды, даже если пришло в
оба ящика). Хранит сам PDF (``pdf_bytes``), распознанные поля + уверенность (``recognition``)
и статус разбора (new/recognized/needs_review/linked/failed/ignored). Связь ``invoice_id`` →
созданная ``supplier_invoice`` (source='email'), ``counterparty_id`` → сматченный контрагент.

ВНИМАНИЕ (деплой на прод): эта ревизия на dev зачейнена на dev-голову
``0142_iiko_invoice_payment_push``. На ``main`` таблицы нет — при выкатке завести
эквивалентную миграцию с ``down_revision`` = текущая голова main.

Revision ID: 0143_email_invoice_intake
Revises: 0142_iiko_invoice_payment_push
Create Date: 2026-06-27
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0143_email_invoice_intake"
down_revision = "0142_iiko_invoice_payment_push"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_invoice_intake",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        # Логический ящик: 'personal' (собственника) / 'corporate' (рабочий).
        sa.Column("mailbox", sa.String(length=32), nullable=False),
        sa.Column("from_addr", sa.String(length=320), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        # RFC Message-ID письма + IMAP UID — для трассировки до письма в ящике.
        sa.Column("message_id", sa.String(length=512), nullable=True),
        sa.Column("message_uid", sa.String(length=64), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attachment_filename", sa.String(length=512), nullable=True),
        sa.Column("attachment_mime", sa.String(length=128), nullable=True),
        # SHA-256 содержимого вложения — ключ идемпотентности.
        sa.Column("attachment_sha256", sa.String(length=64), nullable=False),
        sa.Column("attachment_size", sa.Integer(), nullable=True),
        # Сам PDF (bytea) — чтобы показать его в UI и переразобрать офлайн.
        sa.Column("pdf_bytes", sa.LargeBinary(), nullable=True),
        # new → recognized/needs_review/failed → linked/ignored.
        sa.Column("status", sa.String(length=24), nullable=False, server_default="new"),
        sa.Column("engine", sa.String(length=24), nullable=True),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column(
            "recognition",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("counterparty_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["counterparty_id"], ["counterparty.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["invoice_id"], ["supplier_invoice.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attachment_sha256", name="uq_email_invoice_intake_sha256"),
    )
    op.create_index(
        "ix_email_invoice_intake_status", "email_invoice_intake", ["status"]
    )
    op.create_index(
        "ix_email_invoice_intake_invoice", "email_invoice_intake", ["invoice_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_email_invoice_intake_invoice", table_name="email_invoice_intake")
    op.drop_index("ix_email_invoice_intake_status", table_name="email_invoice_intake")
    op.drop_table("email_invoice_intake")

"""«Страница на оплату»: исключение счетов + плановая авто-отправка в банк.

Два поля в ``email_invoice_intake``:
- ``scheduled_send_date`` — дата плановой авто-отправки счёта в банк (джоба
  ``send_scheduled_payments``). NULL = отправка только вручную.
- ``previous_status`` — статус до ручного исключения оператором (status='excluded'); по нему
  «Вернуть» из инбокса «Исключённые» восстанавливает прежнее место счёта.

Сам статус ``excluded`` — это строковое значение в существующей колонке ``status`` (без enum в
БД), поэтому новых ограничений не требует.

ВНИМАНИЕ (деплой на прод): ревизия на dev зачейнена за dev-головой
``0143_email_invoice_intake``. На ``main`` завести эквивалентную миграцию с ``down_revision`` =
текущая голова main.

Revision ID: 0144_pp_exclude_schedule
Revises: 0143_email_invoice_intake
Create Date: 2026-06-27
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0144_pp_exclude_schedule"
down_revision = "0143_email_invoice_intake"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "email_invoice_intake",
        sa.Column("scheduled_send_date", sa.Date(), nullable=True),
    )
    op.add_column(
        "email_invoice_intake",
        sa.Column("previous_status", sa.String(length=24), nullable=True),
    )
    op.create_index(
        "ix_email_invoice_intake_scheduled",
        "email_invoice_intake",
        ["scheduled_send_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_email_invoice_intake_scheduled", table_name="email_invoice_intake"
    )
    op.drop_column("email_invoice_intake", "previous_status")
    op.drop_column("email_invoice_intake", "scheduled_send_date")

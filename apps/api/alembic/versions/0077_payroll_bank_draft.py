"""add run-level payroll bank draft

Revision ID: 0077_payroll_bank_draft
Revises: 0076_payroll_ndfl_setting
Create Date: 2026-06-08
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0077_payroll_bank_draft"
down_revision = "0076_payroll_ndfl_setting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payroll_bank_draft",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("provider_ref", sa.Text(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status in ('created', 'updated', 'paid', 'failed')",
            name="ck_payroll_bank_draft_status",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["payroll_run.id"],
            name="fk_payroll_bank_draft_run_id_payroll_run",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("run_id", name="uq_payroll_bank_draft_run_id"),
    )
    _seed_bank_payout_requisites()


def downgrade() -> None:
    op.execute(
        """
        delete from app_setting_history
         where setting_id in (
             select id from app_setting where key = 'payroll.bank_payout_requisites'
         )
        """
    )
    op.execute("delete from app_setting where key = 'payroll.bank_payout_requisites'")
    op.drop_table("payroll_bank_draft")


def _seed_bank_payout_requisites() -> None:
    op.execute(
        sa.text(
            """
            insert into app_setting (
                id,
                key,
                value,
                value_type,
                category,
                display_name,
                description,
                widget_type,
                widget_options,
                unit,
                updated_at
            )
            values (
                '13d681d4-843f-43fd-9b57-8fc812253cd1',
                'payroll.bank_payout_requisites',
                '{
                    "recipientName": "ШОКИНА КРИСТИНА",
                    "inn": "890307589201",
                    "kpp": "0",
                    "bankAcnt": "40817810800023540968",
                    "bankBik": "044525974",
                    "corrAccount": "30101810145250000974",
                    "executionOrder": 5,
                    "paymentPurpose": "Выплата заработной платы за период {start}–{end}"
                }'::jsonb,
                'object',
                'payroll',
                'Реквизиты выплаты ЗП',
                'Реквизиты счёта ИП для одного банковского черновика на ведомость.',
                'json',
                null,
                null,
                now()
            )
            on conflict (key) do nothing
            """
        )
    )

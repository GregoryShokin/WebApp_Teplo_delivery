"""Налоги: база дохода, факты уплаты, закрытые периоды, ручные обязательства

Модуль «Налоги» для УСН «Доходы» 6%. Начислений в БД нет — расчёт является чистой функцией
от фактов, поэтому таблиц всего пять: выручка (база), журнал её правок, факты уплаты,
зафиксированные периоды и ручные обязательства (пени/требования).

Выручка хранится периодами с двойной гранулярностью: месячной (доступна из локального кэша
OLAP) и подневной (выгружается с прод-контейнера — iiko привязан к IP). Для налога месячной
гранулярности достаточно и точно: все налоговые периоды заканчиваются на границе месяца.

Revision ID: 0208_taxes
Revises: 0207_iiko_payment_verify
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0208_taxes"
down_revision = "0207_iiko_payment_verify"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "iiko_revenue_period",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("granularity", sa.String(length=8), nullable=False),
        sa.Column("department", sa.String(length=120), nullable=False),
        sa.Column("revenue_net", sa.Numeric(14, 2), nullable=False),
        sa.Column("revenue_gross", sa.Numeric(14, 2), nullable=True),
        sa.Column("discount_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column(
            "source", sa.String(length=24), nullable=False, server_default="iiko_olap"
        ),
        sa.Column(
            "synced_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "department", "granularity", "period_start", name="uq_iiko_revenue_period_slot"
        ),
        sa.CheckConstraint(
            "granularity in ('day', 'month')", name="ck_iiko_revenue_period_granularity"
        ),
        sa.CheckConstraint("period_end >= period_start", name="ck_iiko_revenue_period_range"),
        sa.CheckConstraint(
            "granularity <> 'day' or period_end = period_start",
            name="ck_iiko_revenue_period_day_single",
        ),
        sa.CheckConstraint("revenue_net >= 0", name="ck_iiko_revenue_period_net_non_negative"),
        sa.CheckConstraint(
            "revenue_gross is null or revenue_gross >= 0",
            name="ck_iiko_revenue_period_gross_non_negative",
        ),
        sa.CheckConstraint(
            "source in ('iiko_olap', 'manual')", name="ck_iiko_revenue_period_source"
        ),
    )
    op.create_index("ix_iiko_revenue_period_start", "iiko_revenue_period", ["period_start"])

    op.create_table(
        "iiko_revenue_revision",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("granularity", sa.String(length=8), nullable=False),
        sa.Column("department", sa.String(length=120), nullable=False),
        sa.Column("revenue_net_old", sa.Numeric(14, 2), nullable=True),
        sa.Column("revenue_net_new", sa.Numeric(14, 2), nullable=False),
        sa.Column("sync_reason", sa.String(length=32), nullable=False),
        sa.Column(
            "detected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "sync_reason in ('daily_job', 'manual_sync', 'backfill')",
            name="ck_iiko_revenue_revision_reason",
        ),
    )
    op.create_index("ix_iiko_revenue_revision_start", "iiko_revenue_revision", ["period_start"])

    op.create_table(
        "tax_payment",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("bundle_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("paid_on", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("recipient", sa.String(length=8), nullable=False),
        sa.Column("for_year", sa.Integer(), nullable=False),
        sa.Column("for_period", sa.String(length=8), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="paid"),
        # Происхождение строки и доверие к ней. Банк отдаёт все бюджетные платежи под одним
        # КБК, поэтому РАЗНОС перевода по назначениям до появления уведомления — расчётный.
        sa.Column(
            "source_kind", sa.String(length=24), nullable=False, server_default="manual"
        ),
        sa.Column(
            "quality_status", sa.String(length=24), nullable=False, server_default="confirmed"
        ),
        sa.Column("cashflow_transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("bank_operation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("document_number", sa.String(length=32), nullable=True),
        sa.Column("purpose", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["cashflow_transaction_id"],
            ["cashflow_transactions.id"],
            name="fk_tax_payment_cashflow_transaction_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["bank_operation_id"],
            ["bank_operations.id"],
            name="fk_tax_payment_bank_operation_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name="fk_tax_payment_created_by_user_id",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "kind in ('usn_advance', 'ndfl', 'contrib_employees', 'contrib_injury', "
            "'contrib_fixed', 'contrib_extra_1pct', 'penalty', 'other')",
            name="ck_tax_payment_kind",
        ),
        sa.CheckConstraint("recipient in ('fns', 'sfr')", name="ck_tax_payment_recipient"),
        sa.CheckConstraint(
            "status in ('planned', 'paid', 'cancelled')", name="ck_tax_payment_status"
        ),
        sa.CheckConstraint("amount > 0", name="ck_tax_payment_amount_positive"),
        sa.CheckConstraint(
            "source_kind in ('bank_statement', 'tax_notice', 'bank_draft', 'manual')",
            name="ck_tax_payment_source_kind",
        ),
        sa.CheckConstraint(
            "quality_status in ('confirmed', 'reconstructed', 'requires_review')",
            name="ck_tax_payment_quality_status",
        ),
        sa.CheckConstraint(
            "(recipient = 'sfr' and kind in ('contrib_injury', 'penalty', 'other')) "
            "or (recipient = 'fns' and kind <> 'contrib_injury')",
            name="ck_tax_payment_recipient_kind",
        ),
    )
    op.create_index("ix_tax_payment_paid_on", "tax_payment", ["paid_on"])
    op.create_index("ix_tax_payment_year_kind", "tax_payment", ["for_year", "kind"])
    op.create_index("ix_tax_payment_bundle", "tax_payment", ["bundle_id"])

    op.create_table(
        "tax_period_close",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("period_code", sa.String(length=8), nullable=False),
        sa.Column("income_ytd", sa.Numeric(14, 2), nullable=False),
        sa.Column("tax_computed", sa.Numeric(14, 2), nullable=False),
        sa.Column("deduction_applied", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "deduction_burned", sa.Numeric(14, 2), nullable=False, server_default="0"
        ),
        sa.Column("amount_due", sa.Numeric(14, 2), nullable=False),
        sa.Column("fixed_claimed", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("extra_claimed", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("extra_claimed_for_year", sa.Integer(), nullable=True),
        sa.Column("extra_accrued", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("drift_detected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("closed_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "closed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["closed_by_user_id"],
            ["user.id"],
            name="fk_tax_period_close_closed_by_user_id",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("year", "period_code", name="uq_tax_period_close_slot"),
        sa.CheckConstraint(
            "period_code in ('q1', 'h1', '9m', 'year')", name="ck_tax_period_close_code"
        ),
        sa.CheckConstraint("income_ytd >= 0", name="ck_tax_period_close_income"),
        sa.CheckConstraint("deduction_applied >= 0", name="ck_tax_period_close_deduction"),
    )

    op.create_table(
        "tax_manual_obligation",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("recipient", sa.String(length=8), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="open"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name="fk_tax_manual_obligation_created_by_user_id",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint("amount > 0", name="ck_tax_manual_obligation_amount"),
        sa.CheckConstraint(
            "recipient in ('fns', 'sfr')", name="ck_tax_manual_obligation_recipient"
        ),
        sa.CheckConstraint(
            "status in ('open', 'paid', 'cancelled')", name="ck_tax_manual_obligation_status"
        ),
    )
    op.create_index("ix_tax_manual_obligation_due", "tax_manual_obligation", ["due_date"])

    # Реестр источников — настройкой, а не константой: смена источника (кэш → API iiko,
    # реконструкция ЕНП → уведомления агента) не должна требовать релиза, а история
    # изменений уже ведётся в app_setting_history.
    from app.services.taxes.sources import SOURCE_REGISTRY_KEY, registry_to_payload

    app_setting_table = sa.table(
        "app_setting",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("key", sa.String()),
        sa.column("value", postgresql.JSONB()),
        sa.column("value_type", sa.String()),
        sa.column("category", sa.String()),
        sa.column("display_name", sa.Text()),
        sa.column("description", sa.Text()),
        sa.column("widget_type", sa.Text()),
    )
    op.bulk_insert(
        app_setting_table,
        [
            {
                "id": uuid.uuid4(),
                "key": SOURCE_REGISTRY_KEY,
                "value": registry_to_payload(),
                "value_type": "json",
                "category": "Налоги",
                "display_name": "Источники данных налогового контура",
                "description": (
                    "Откуда берётся каждое число налогового контура: система, метод "
                    "получения, степень доверия и целевой источник."
                ),
                "widget_type": "json",
            }
        ],
    )


def downgrade() -> None:
    op.execute("delete from app_setting where key = 'taxes.sources'")
    op.drop_index("ix_tax_manual_obligation_due", table_name="tax_manual_obligation")
    op.drop_table("tax_manual_obligation")
    op.drop_table("tax_period_close")
    op.drop_index("ix_tax_payment_bundle", table_name="tax_payment")
    op.drop_index("ix_tax_payment_year_kind", table_name="tax_payment")
    op.drop_index("ix_tax_payment_paid_on", table_name="tax_payment")
    op.drop_table("tax_payment")
    op.drop_index("ix_iiko_revenue_revision_start", table_name="iiko_revenue_revision")
    op.drop_table("iiko_revenue_revision")
    op.drop_index("ix_iiko_revenue_period_start", table_name="iiko_revenue_period")
    op.drop_table("iiko_revenue_period")

"""add revenue forecast

Revision ID: 0033_revenue_forecast
Revises: 0032_shift_schedule_base
Create Date: 2026-06-01
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0033_revenue_forecast"
down_revision = "0032_shift_schedule_base"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "revenue_forecast",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("weekday", sa.SmallInteger(), nullable=False),
        sa.Column(
            "method_code",
            sa.String(48),
            nullable=False,
            server_default=sa.text("'avg_6_same_weekday'"),
        ),
        sa.Column(
            "history_window_weeks",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("6"),
        ),
        sa.Column(
            "history_points",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("base_average_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column(
            "season_coeff",
            sa.Numeric(8, 5),
            nullable=False,
            server_default=sa.text("1.0"),
        ),
        sa.Column(
            "event_coeff",
            sa.Numeric(8, 5),
            nullable=False,
            server_default=sa.text("1.0"),
        ),
        sa.Column("manual_override_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("manual_override_reason", sa.Text(), nullable=True),
        sa.Column("manual_override_set_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("manual_override_set_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("forecast_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("quality_status", sa.String(24), nullable=False),
        sa.Column(
            "event_review_recommended",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["manual_override_set_by_user_id"],
            ["user.id"],
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("business_date", name="uq_revenue_forecast_business_date"),
        sa.CheckConstraint(
            "quality_status IN ('ok','requires_review','manual_override')",
            name="ck_revenue_forecast_quality_status",
        ),
        sa.CheckConstraint(
            "weekday >= 0 AND weekday <= 6",
            name="ck_revenue_forecast_weekday",
        ),
    )
    op.create_index(
        "ix_revenue_forecast_business_date",
        "revenue_forecast",
        ["business_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_revenue_forecast_business_date", table_name="revenue_forecast")
    op.drop_table("revenue_forecast")

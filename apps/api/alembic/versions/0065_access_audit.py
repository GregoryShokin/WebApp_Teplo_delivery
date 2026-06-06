"""add access control audit events

Revision ID: 0065_access_audit
Revises: 0064_payroll_legacy_import
Create Date: 2026-06-06
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0065_access_audit"
down_revision = "0064_payroll_legacy_import"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "role_permission_event",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("permission_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["user.id"],
            name="fk_role_permission_event_actor_user_id_user",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["permission_id"],
            ["permission.id"],
            name="fk_role_permission_event_permission_id_permission",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["role.id"],
            name="fk_role_permission_event_role_id_role",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_role_permission_event_created_at",
        "role_permission_event",
        ["created_at"],
    )
    op.create_table(
        "user_role_event",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["user.id"],
            name="fk_user_role_event_actor_user_id_user",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["role.id"],
            name="fk_user_role_event_role_id_role",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name="fk_user_role_event_user_id_user",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_user_role_event_created_at", "user_role_event", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_user_role_event_created_at", table_name="user_role_event")
    op.drop_table("user_role_event")
    op.drop_index("ix_role_permission_event_created_at", table_name="role_permission_event")
    op.drop_table("role_permission_event")

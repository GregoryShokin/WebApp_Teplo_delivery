"""Отложенная выдача депозита через ЗП-ведомость (этап 4).

Добавляем:
- ``payroll_line.deposit_payout_scheduled`` Numeric(14,2) — сумма запланированной выдачи
  депозита, попавшей в эту ведомость (в столбец «Выдача депозита»); в «К выплате» НЕ входит.
- таблицу ``deposit_payout_schedule`` — намерение выдать депозит в ближайшей ведомости
  (переживает пересчёт ведомости, в отличие от превью-транзакций deposit_transaction).

down_revision = 0133 (главная линия). kds-миграции 0131/0132 (другой агент) НЕ в main/на
проде — на проде линейно 0130→0133→0134; на dev две головы сводит dev-merge (не коммитим).

Revision ID: 0134_deposit_payout_schedule
Revises: 0133_courier_deposit_payout
Create Date: 2026-06-22
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0134_deposit_payout_schedule"
down_revision = "0133_courier_deposit_payout"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "payroll_line",
        sa.Column(
            "deposit_payout_scheduled",
            sa.Numeric(14, 2),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_table(
        "deposit_payout_schedule",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("requested_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column(
            "account_choice", sa.String(length=32), nullable=False, server_default="safe"
        ),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="pending"
        ),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employee.id"],
            name="fk_deposit_payout_schedule_employee_id_employee",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_run_id"],
            ["payroll_run.id"],
            name="fk_deposit_payout_schedule_target_run_id_payroll_run",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name="fk_deposit_payout_schedule_created_by_user_id_user",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "status in ('pending', 'processed', 'cancelled')",
            name="ck_deposit_payout_schedule_status",
        ),
    )
    # Один активный (pending) план выдачи на сотрудника.
    op.create_index(
        "uq_deposit_payout_schedule_pending_employee",
        "deposit_payout_schedule",
        ["employee_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_deposit_payout_schedule_pending_employee",
        table_name="deposit_payout_schedule",
    )
    op.drop_table("deposit_payout_schedule")
    op.drop_column("payroll_line", "deposit_payout_scheduled")

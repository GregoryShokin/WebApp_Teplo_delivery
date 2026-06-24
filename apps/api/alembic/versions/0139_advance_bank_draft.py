"""Банк-выдача авансов/займов сотрудникам (Фаза 2).

Когда счёт-источник выдачи аванса/займа — банковский кошелёк, деньги не уходят
мгновенно: создаётся черновик платежа в Т-Банке (как у зарплаты), поллинг ловит
статус «исполнен» → транзит банк→Сейф + резерв Сейфа со статьёй аванса/займа, а
фактическая выдача сотруднику (списание резерва) подтверждается кнопкой «Выплачено».
Долг сотрудника формируется только в момент «Выплачено».

Добавляем:
* таблицу ``salary_advance_bank_draft`` (1:1 с авансом, зеркало ``payroll_bank_draft``
  + ссылка на созданный резерв Сейфа ``safe_allocation_id``);
* статус ``awaiting_payout`` в CHECK ``salary_advance`` — банк-аванс до фактической
  выдачи (не удерживается из ведомости, но занимает доступное к авансу).

Revision ID: 0139_advance_bank_draft
Revises: 0138_advance_dds
Create Date: 2026-06-24
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0139_advance_bank_draft"
down_revision = "0138_advance_dds"
branch_labels = None
depends_on = None

_OLD_STATUS = "status in ('issued', 'recovered', 'written_off', 'cancelled')"
_NEW_STATUS = (
    "status in ('issued', 'awaiting_payout', 'recovered', 'written_off', 'cancelled')"
)


def upgrade() -> None:
    op.create_table(
        "salary_advance_bank_draft",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "advance_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("salary_advance.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("document_id", sa.String(64), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("provider_ref", sa.Text(), nullable=True),
        sa.Column(
            "safe_allocation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("safe_allocations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "payload", postgresql.JSONB(), nullable=False, server_default="{}"
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
            "status in ('created', 'updated', 'paid', 'disbursed', 'failed', 'cancelled')",
            name="ck_salary_advance_bank_draft_status",
        ),
        sa.UniqueConstraint("advance_id", name="uq_salary_advance_bank_draft_advance_id"),
    )
    op.create_index(
        "ix_salary_advance_bank_draft_status",
        "salary_advance_bank_draft",
        ["status"],
    )

    # Расширяем допустимые статусы аванса промежуточным «ожидает выплаты».
    op.drop_constraint("ck_salary_advance_status", "salary_advance", type_="check")
    op.create_check_constraint("ck_salary_advance_status", "salary_advance", _NEW_STATUS)


def downgrade() -> None:
    # Снимаем промежуточный статус, иначе пересоздание строгого CHECK упадёт на существующих
    # банк-выдачах «ожидает выплаты»: они не выданы → откатываем в «отменён» (долг не формируем,
    # деньги, если уже доехали в Сейф, остаются там — резерв отвязывается дропом таблицы).
    op.execute(
        "UPDATE salary_advance SET status = 'cancelled' WHERE status = 'awaiting_payout'"
    )
    op.drop_constraint("ck_salary_advance_status", "salary_advance", type_="check")
    op.create_check_constraint("ck_salary_advance_status", "salary_advance", _OLD_STATUS)
    op.drop_index("ix_salary_advance_bank_draft_status", "salary_advance_bank_draft")
    op.drop_table("salary_advance_bank_draft")

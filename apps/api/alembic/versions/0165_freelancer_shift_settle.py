"""Посменная выдача внештатника из кассы (вариант Б): персистим только факт оплаты налом.

Единица смены внештатника = его ``attendance_entry`` за ОТКРЫТЫЙ недельный период (тот же
источник явок, что кормит авансы производственников). Сумма смены считается на лету
(``freelancer_shift_amount(ставка_карточки, minutes)``) и в БД НЕ хранится как «непогашенное».
Здесь фиксируется ТОЛЬКО факт наличной выдачи — по одной строке на оплаченную явку:

- ``paid_cash`` — смена выдана наличными из кассы (ТК Черникова); ведомость исключает её
  сумму из «к выплате» (в ФОТ остаётся); повторная выдача той же явки — 409;
- ``void``      — оплата аннулирована (реверс), деньги учтены отдельно.

«Непогашенные» смены не персистятся: список/детализация выводят явки открытого периода
МИНУС уже оплаченные налом (``attendance_entry_id`` в этой таблице). При финализации период
уходит из «открытых» → его неоплаченные смены исчезают из кассы, их платит ведомость (они в
gross), а оплаченные налом вычтены из net.

Право на выдачу — существующее ``kassa.payouts.create``. Статья ДДС «Зарплата
производственного персонала» защищена: её пишет движок выдачи, а не свободная форма кассы.

Revision ID: 0165_freelancer_shift_settle
Revises: 0164_freelancer_temp_cards
Create Date: 2026-07-07
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0165_freelancer_shift_settle"
down_revision = "0164_freelancer_temp_cards"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "freelancer_shift_settlement",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Единица смены — явка (attendance_entry) открытого недельного периода.
        sa.Column("attendance_entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        # period_id — для сверки с ведомостью по границам периода (быстрый фильтр).
        sa.Column("period_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("minutes", sa.Integer(), nullable=False),
        # amount — сумма, фактически выданная налом за эту смену (снимок на момент выдачи).
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="paid_cash"),
        sa.Column("cashflow_transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
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
        sa.CheckConstraint(
            "status in ('paid_cash', 'void')",
            name="ck_freelancer_shift_settlement_status",
        ),
        sa.CheckConstraint(
            "amount >= 0",
            name="ck_freelancer_shift_settlement_amount",
        ),
        sa.CheckConstraint(
            "minutes >= 0",
            name="ck_freelancer_shift_settlement_minutes",
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employee.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["attendance_entry_id"], ["attendance_entry.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["period_id"], ["payroll_period.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["paid_by_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["cashflow_transaction_id"], ["cashflow_transactions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        # Одна наличная выдача на явку: повторная оплата той же смены невозможна.
        sa.UniqueConstraint(
            "attendance_entry_id",
            name="uq_freelancer_shift_settlement_entry",
        ),
    )
    op.create_index(
        "ix_freelancer_shift_settlement_period",
        "freelancer_shift_settlement",
        ["period_id", "status"],
    )
    op.create_index(
        "ix_freelancer_shift_settlement_employee",
        "freelancer_shift_settlement",
        ["employee_id", "work_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_freelancer_shift_settlement_employee",
        table_name="freelancer_shift_settlement",
    )
    op.drop_index(
        "ix_freelancer_shift_settlement_period",
        table_name="freelancer_shift_settlement",
    )
    op.drop_table("freelancer_shift_settlement")

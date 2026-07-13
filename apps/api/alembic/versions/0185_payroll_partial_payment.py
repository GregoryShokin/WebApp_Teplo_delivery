"""Частичная выплата ведомости: статус ``partially_paid`` + учёт забронированного в ДДС.

Снимаем атомарность выплаты сотрудника. ``PayrollPayment.amount`` становится бегущим
итогом выплаченного (сумма траншей), ``status`` получает подсостояние ``partially_paid``
(0 < amount < начислено). ``booked_amount`` — сколько по этому сотруднику уже списано в ДДС
(защита от задвоения: доплата/bulk книжат только дельту ``amount − booked_amount``).
``comment`` — необязательная причина недоплаты. Существующие оплаченные строки помечаем
как полностью забронированные (booked_amount = amount).

Revision ID: 0185_payroll_partial_payment
Revises: 0184_invoice_price_control
Create Date: 2026-07-12
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0185_payroll_partial_payment"
down_revision = "0184_invoice_price_control"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "payroll_payment",
        sa.Column(
            "booked_amount",
            sa.Numeric(14, 2),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "payroll_payment",
        sa.Column("comment", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_payroll_payment_booked_amount_non_negative",
        "payroll_payment",
        "booked_amount >= 0",
    )
    # Существующие выплаты уже проведены полностью — считаем их забронированными целиком.
    op.execute("UPDATE payroll_payment SET booked_amount = amount")
    # Расширяем набор статусов подсостоянием частичной выплаты.
    op.drop_constraint("ck_payroll_payment_status", "payroll_payment", type_="check")
    op.create_check_constraint(
        "ck_payroll_payment_status",
        "payroll_payment",
        "status in ('planned', 'draft_created', 'paid', 'partially_paid')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_payroll_payment_status", "payroll_payment", type_="check")
    op.create_check_constraint(
        "ck_payroll_payment_status",
        "payroll_payment",
        "status in ('planned', 'draft_created', 'paid')",
    )
    op.drop_constraint(
        "ck_payroll_payment_booked_amount_non_negative",
        "payroll_payment",
        type_="check",
    )
    op.drop_column("payroll_payment", "comment")
    op.drop_column("payroll_payment", "booked_amount")

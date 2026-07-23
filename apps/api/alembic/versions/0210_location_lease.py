"""Аренда помещения: арендодатель, стоимость, условия и залог.

Арендодатель — обычный контрагент с новой ролью ``landlord``: по факту от него нужно только
название и, по желанию, реквизиты. Долг и залог всегда на лице, а не на адресе, поэтому
строка аренды связывает помещение с контрагентом, а не хранит имя собственника текстом.

Активных строк на помещении может быть несколько (площадь поделена между собственниками,
склад и зал сдают разные лица). Смена арендодателя — закрытие строки датой ``ended_on`` и
заведение новой, иначе история прошлых месяцев переписалась бы на нового собственника.

Revision ID: 0210_location_lease
Revises: 0209_location_registry
Create Date: 2026-07-23
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0210_location_lease"
down_revision = "0209_location_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE counterparty_role_type ADD VALUE IF NOT EXISTS 'landlord'")

    op.create_table(
        "location_lease",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "location_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("location.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "counterparty_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("counterparty.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("monthly_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("payment_day", sa.Integer(), nullable=True),
        sa.Column("payment_mode", sa.String(length=16), nullable=False, server_default="prepaid"),
        sa.Column(
            "documents_mode", sa.String(length=16), nullable=False, server_default="informal"
        ),
        sa.Column("deposit_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("started_on", sa.Date(), nullable=False),
        sa.Column("ended_on", sa.Date(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
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
        sa.CheckConstraint("monthly_amount >= 0", name="ck_location_lease_amount_non_negative"),
        sa.CheckConstraint("deposit_amount >= 0", name="ck_location_lease_deposit_non_negative"),
        sa.CheckConstraint(
            "ended_on is null or ended_on >= started_on", name="ck_location_lease_period_order"
        ),
        sa.CheckConstraint(
            "payment_day is null or (payment_day between 1 and 31)",
            name="ck_location_lease_payment_day",
        ),
    )
    op.create_index("ix_location_lease_location_id", "location_lease", ["location_id"])
    op.create_index("ix_location_lease_counterparty_id", "location_lease", ["counterparty_id"])


def downgrade() -> None:
    op.drop_index("ix_location_lease_counterparty_id", table_name="location_lease")
    op.drop_index("ix_location_lease_location_id", table_name="location_lease")
    op.drop_table("location_lease")
    # Значение enum не снимаем: PostgreSQL не умеет DROP VALUE, а пересоздание типа задело бы
    # существующие роли контрагентов.

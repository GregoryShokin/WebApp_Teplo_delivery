"""Номенклатура актов списания: без неё задвоение проработки не увидеть.

ПРОБЛЕМА, КОТОРУЮ НАЗВАЛ ВЛАДЕЛЕЦ 05.08.2026. Товар приходит на проработку — система признаёт
его расходом строкой «Проработка» по приходной накладной. Потом управляющий списывает тот же
товар актом списания, и его себестоимость встаёт вторым разом в «Списание продукции и сырья».
Расход посчитан дважды.

ПОЧЕМУ ЭТО НЕЛЬЗЯ БЫЛО ЗАМЕТИТЬ. ``writeoff_total`` складывал ``cost`` всех позиций всех
проведённых актов в одно число (за июль 2026 — 75 179,45 ₽) и номенклатуру не сохранял.
Ни отчёт, ни расшифровка не знали, ЧТО именно списано, поэтому пересечение с проработкой
не проявлялось никак — оно просто увеличивало расход.

Таблица хранит те же поля, что уже привычны товарному контуру: месяц, GUID, имя, сумма и
число сложившихся строк. Уникальность по (месяц, товар) — за месяц один товар складывается
в одну строку, сколько бы актов его ни списывало.

Revision ID: 0265_pnl_writeoff_facts
Revises: 0264_pnl_bar_audit_lines
Create Date: 2026-08-05
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0265_pnl_writeoff_facts"
down_revision = "0264_pnl_bar_audit_lines"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pnl_iiko_writeoff_fact",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("period_month", sa.Date(), nullable=False),
        sa.Column("iiko_product_guid", sa.String(64), nullable=False),
        sa.Column("product_name", sa.String(255), nullable=True),
        sa.Column("product_code", sa.String(64), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("rows_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "uq_pnl_iiko_writeoff_fact_slot",
        "pnl_iiko_writeoff_fact",
        ["period_month", "iiko_product_guid"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_pnl_iiko_writeoff_fact_slot", table_name="pnl_iiko_writeoff_fact")
    op.drop_table("pnl_iiko_writeoff_fact")

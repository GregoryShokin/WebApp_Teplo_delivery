"""Правки ЗП собственника: убрать статью «Зарплата собственника», выплата через ведомость.

По решению владельца:
* статья ДДС «Зарплата собственника» (``zarplata_sobstvennika``) удаляется — выплаты идут по
  «Зарплата административного персонала»;
* «Включить в выплату» в ведомости включает сумму в выплату САМОЙ ведомости (без отдельного
  счёта-источника) — для этого у ``employee_payout`` появляется ``run_id`` (к какой ведомости
  относится включённая сумма) и статус ``included``;
* остаток ЗП = начислено − Σ выплат (``included``/``pending``/``paid``).

Revision ID: 0155_employee_payout_incl
Revises: 0154_employee_payout_bank
Create Date: 2026-07-02
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0155_employee_payout_incl"
down_revision = "0154_employee_payout_bank"
branch_labels = None
depends_on = None

_STATUS_CK = "ck_employee_payout_ck_employee_payout_status"
_OLD_STATUSES = "('draft', 'pending', 'paid', 'failed', 'cancelled')"
_NEW_STATUSES = "('draft', 'pending', 'included', 'paid', 'failed', 'cancelled')"


def upgrade() -> None:
    op.add_column(
        "employee_payout",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_employee_payout_run",
        "employee_payout",
        "payroll_run",
        ["run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(f"ALTER TABLE employee_payout DROP CONSTRAINT {_STATUS_CK}")
    op.execute(
        f"ALTER TABLE employee_payout ADD CONSTRAINT {_STATUS_CK} "
        f"CHECK (status in {_NEW_STATUSES})"
    )

    # Статья «Зарплата собственника» больше не нужна (FK article_id → SET NULL).
    op.execute("DELETE FROM dds_articles WHERE code = 'zarplata_sobstvennika'")


def downgrade() -> None:
    op.execute(
        """
        INSERT INTO dds_articles (id, code, name, movement_type, activity_type, is_active)
        VALUES (gen_random_uuid(), 'zarplata_sobstvennika',
                'Зарплата собственника', 'outflow', 'operating', true)
        ON CONFLICT (code) DO UPDATE SET is_active = true
        """
    )
    op.execute(f"ALTER TABLE employee_payout DROP CONSTRAINT {_STATUS_CK}")
    op.execute(
        f"ALTER TABLE employee_payout ADD CONSTRAINT {_STATUS_CK} "
        f"CHECK (status in {_OLD_STATUSES})"
    )
    op.drop_constraint("fk_employee_payout_run", "employee_payout", type_="foreignkey")
    op.drop_column("employee_payout", "run_id")

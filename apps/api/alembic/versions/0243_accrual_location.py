"""Ось «где» у признанного расхода: к какому помещению он относится.

Отчёт «расход по месяцам» отвечает на вопрос «сколько мы потратили», но не отвечает на вопрос
«сколько стоит эта точка» — а именно он и есть первый вопрос владельца к P&L, когда точек
больше одной. Разреза по помещению у начисления не было вовсе.

Заполняем там, где помещение ДЕЙСТВИТЕЛЬНО известно: строка ручного платежа несёт его явно
(окно «Новый платёж» спрашивает), договор аренды знает по определению. У документа поставщика
помещения нет — и выдумывать его нельзя: пустая локация честнее подставленной наугад, потому
что подставленная разъедется с реальностью молча и будет выглядеть достоверно.

БЭКФИЛЛ. Проставляем локацию существующим начислениям по строкам платежей — она уже лежит в
``expense_draft_line.location_id``, и терять её незачем. Арендные начисления заполняем по
договору: у них ``source='lease'``, а ключ ``lease:{id}:{YYYY-MM}`` прямо указывает на договор.

Revision ID: 0243_accrual_location
Revises: 0242_accounting_period_close
Create Date: 2026-08-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0243_accrual_location"
down_revision = "0242_accounting_period_close"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "supplier_expense_accrual",
        sa.Column("location_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_supplier_expense_accrual_location",
        "supplier_expense_accrual",
        "location",
        ["location_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Начисления по строкам платежей — помещение уже известно, просто переносим.
    op.execute(
        """
        UPDATE supplier_expense_accrual AS a
        SET location_id = l.location_id
        FROM expense_draft_line AS l
        WHERE a.expense_draft_line_id = l.id
          AND l.location_id IS NOT NULL
        """
    )
    # Арендные начисления: договор знает помещение, а ключ документа указывает на договор.
    op.execute(
        """
        UPDATE supplier_expense_accrual AS a
        SET location_id = lease.location_id
        FROM supplier_invoice AS i
        JOIN location_lease AS lease
          ON lease.id = split_part(i.external_id, ':', 2)::uuid
        WHERE a.invoice_id = i.id
          AND i.source = 'lease'
          AND i.external_id LIKE 'lease:%'
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_supplier_expense_accrual_location", "supplier_expense_accrual", type_="foreignkey"
    )
    op.drop_column("supplier_expense_accrual", "location_id")

"""seed DDS article «Авансы сотрудникам»

Выдачи авансов/займов сотрудникам отражаются в ДДС через классификацию реального
оттока (T-Bank-операция / наличные) в эту статью — payroll собственных проводок
не создаёт, поэтому двойного счёта с банковской сверкой нет (решение 2026-06-10,
Option A). Источник деталей по авансам — payroll (`salary_advance`).

Revision ID: 0091_dds_employee_advance
Revises: 0090_advance_permissions
Create Date: 2026-06-10
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from alembic import op

revision = "0091_dds_employee_advance"
down_revision = "0090_advance_permissions"
branch_labels = None
depends_on = None

ARTICLE_CODE = "employee_advance"


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
            INSERT INTO dds_articles
                (id, code, name, movement_type, activity_type, is_active, description)
            VALUES
                (:id, :code, :name, 'outflow', 'operating', true, :description)
            ON CONFLICT (code) DO NOTHING
            """
        ),
        {
            "id": uuid.uuid4(),
            "code": ARTICLE_CODE,
            "name": "Авансы сотрудникам",
            "description": (
                "Авансы и займы сотрудникам (выдача). Классифицируется из реального "
                "оттока; детали по авансам — в payroll."
            ),
        },
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text("DELETE FROM dds_articles WHERE code = :code"),
        {"code": ARTICLE_CODE},
    )

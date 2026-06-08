"""version employee positions

Revision ID: 0063_employee_position_assign
Revises: 0062_rebaseline_percent_coeff
Create Date: 2026-06-05
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0063_employee_position_assign"
down_revision = "0062_rebaseline_percent_coeff"
branch_labels = None
depends_on = None

POSITIONS = (
    "Кассир",
    "Повар",
    "Управляющий",
    "Системный администратор",
    "Курьер",
    "Менеджер",
    "Уборщица",
    "Посудомойка",
)


def upgrade() -> None:
    op.create_table(
        "employee_position_assignment",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("employee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.String(length=160), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_employee_position_assignment_range",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            name="fk_employee_position_assignment_created_by_user_id_user",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employee.id"],
            name="fk_employee_position_assignment_employee_id_employee",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_employee_position_assignment_employee_dates",
        "employee_position_assignment",
        ["employee_id", "effective_from", "effective_to"],
    )
    op.create_index(
        "uq_employee_position_assignment_one_open",
        "employee_position_assignment",
        ["employee_id"],
        unique=True,
        postgresql_where=sa.text("effective_to IS NULL"),
    )

    op.add_column(
        "employee",
        sa.Column(
            "requires_position_review",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    op.execute(
        """
        INSERT INTO employee_position_assignment (
            id,
            employee_id,
            position,
            effective_from,
            comment
        )
        SELECT
            gen_random_uuid(),
            e.id,
            e.position,
            COALESCE(
                LEAST(
                    e.hire_date,
                    (
                        SELECT MIN(ra.effective_from)
                        FROM employee_role_assignment ra
                        WHERE ra.employee_id = e.id
                    )
                ),
                e.hire_date,
                e.created_at::date
            ),
            'Бэкфилл из employee.position'
        FROM employee e
        WHERE e.position IS NOT NULL
        """
    )

    op.drop_index("uq_employee_active_deputy_senior_per_position", table_name="employee")
    op.drop_index("uq_employee_active_senior_per_position", table_name="employee")
    op.drop_constraint("ck_employee_position_canonical", "employee", type_="check")
    op.drop_column("employee", "position")


def downgrade() -> None:
    op.add_column("employee", sa.Column("position", sa.String(length=160), nullable=True))
    op.execute(
        """
        UPDATE employee
        SET position = (
            SELECT epa.position
            FROM employee_position_assignment epa
            WHERE epa.employee_id = employee.id
              AND epa.effective_to IS NULL
            ORDER BY epa.effective_from DESC
            LIMIT 1
        )
        """
    )
    op.create_check_constraint(
        "ck_employee_position_canonical",
        "employee",
        f"position in {_sql_values(POSITIONS)}",
    )
    op.create_index(
        "uq_employee_active_senior_per_position",
        "employee",
        ["position"],
        unique=True,
        postgresql_where=sa.text("is_senior = true and status = 'active'"),
    )
    op.create_index(
        "uq_employee_active_deputy_senior_per_position",
        "employee",
        ["position"],
        unique=True,
        postgresql_where=sa.text("is_deputy_senior = true and status = 'active'"),
    )
    op.drop_index(
        "uq_employee_position_assignment_one_open",
        table_name="employee_position_assignment",
    )
    op.drop_index(
        "ix_employee_position_assignment_employee_dates",
        table_name="employee_position_assignment",
    )
    op.drop_table("employee_position_assignment")
    op.drop_column("employee", "requires_position_review")


def _sql_values(values: tuple[str, ...]) -> str:
    return "(" + ", ".join("'" + value.replace("'", "''") + "'" for value in values) + ")"

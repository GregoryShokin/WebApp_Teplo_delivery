"""delete employees outside canonical taxonomy

Revision ID: 0020_cleanup_non_canon
Revises: 0019_taxonomy_align
Create Date: 2026-05-29
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from alembic import op

revision = "0020_cleanup_non_canon"
down_revision = "0019_taxonomy_align"
branch_labels = None
depends_on = None

CANONICAL_POSITIONS = (
    "Кассир",
    "Повар",
    "Управляющий",
    "Системный администратор",
    "Курьер",
    "Менеджер",
)


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            f"""
            create temporary table tmp_non_canon_employee_ids
            on commit drop
            as
            select id
              from employee
             where position is null
                or position not in {_sql_values(CANONICAL_POSITIONS)}
            """
        )
    )
    deleted_count = conn.scalar(sa.text("select count(*) from tmp_non_canon_employee_ids")) or 0

    _delete_employee_dependents()
    conn.execute(
        sa.text(
            """
            delete from employee
             where id in (select id from tmp_non_canon_employee_ids)
            """
        )
    )
    conn.execute(
        sa.text(
            """
            insert into agent_run (
                id,
                agent_name,
                finished_at,
                status,
                params,
                result
            )
            values (
                :id,
                'taxonomy_cleanup_non_canonical_employees',
                now(),
                'success',
                '{}'::jsonb,
                jsonb_build_object('deleted_employees', cast(:deleted_count as integer))
            )
            """
        ),
        {"id": uuid.uuid4(), "deleted_count": int(deleted_count)},
    )

    op.alter_column("employee", "position", existing_type=sa.String(length=160), nullable=False)
    op.create_check_constraint(
        "ck_employee_position_canonical",
        "employee",
        f"position in {_sql_values(CANONICAL_POSITIONS)}",
    )


def downgrade() -> None:
    op.drop_constraint("ck_employee_position_canonical", "employee", type_="check")
    op.alter_column("employee", "position", existing_type=sa.String(length=160), nullable=True)
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            delete from agent_run
             where agent_name = 'taxonomy_cleanup_non_canonical_employees'
            """
        )
    )


def _delete_employee_dependents() -> None:
    conn = op.get_bind()
    for table_name in (
        "shift_ledger_entry",
        "attendance_entry",
        "payroll_line",
        "deposit_transaction",
        "deposit_account",
        "accumulation_fund_account",
        "employee_role_assignment",
    ):
        conn.execute(
            sa.text(
                f"""
                delete from {table_name}
                 where employee_id in (select id from tmp_non_canon_employee_ids)
                """
            )
        )


def _sql_values(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"

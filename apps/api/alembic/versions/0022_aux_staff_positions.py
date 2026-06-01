"""add auxiliary staff positions

Revision ID: 0022_aux_staff_positions
Revises: 0024_employee_effective_events
Create Date: 2026-05-30
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0022_aux_staff_positions"
down_revision = "0024_employee_effective_events"
branch_labels = None
depends_on = None

BASE_POSITIONS = (
    "Кассир",
    "Повар",
    "Управляющий",
    "Системный администратор",
    "Курьер",
    "Менеджер",
)
AUX_POSITIONS = ("Уборщица", "Посудомойка")
CANONICAL_POSITIONS = BASE_POSITIONS + AUX_POSITIONS
EMPLOYEE_CATEGORIES = (
    "category_1",
    "category_2",
    "category_3",
    "category_4",
    "intern",
    "freelancer",
)


def upgrade() -> None:
    _replace_position_constraints(CANONICAL_POSITIONS)
    _seed_auxiliary_no_category_availability()


def downgrade() -> None:
    conn = op.get_bind()
    _replace_position_constraints(CANONICAL_POSITIONS, drop_only=True)
    conn.execute(
        sa.text(
            f"""
            delete from payroll_role_category_availability
             where position_group in {_sql_values(AUX_POSITIONS)}
            """
        )
    )
    conn.execute(
        sa.text(
            f"""
            delete from employee_position_event
             where position in {_sql_values(AUX_POSITIONS)}
            """
        )
    )
    conn.execute(
        sa.text(
            f"""
            create temporary table tmp_aux_employee_ids
            on commit drop
            as
            select id
              from employee
             where position in {_sql_values(AUX_POSITIONS)}
            """
        )
    )
    _delete_employee_dependents()
    conn.execute(
        sa.text(
            """
            delete from employee
             where id in (select id from tmp_aux_employee_ids)
            """
        )
    )
    _replace_position_constraints(BASE_POSITIONS, create_only=True)


def _replace_position_constraints(
    values: tuple[str, ...],
    *,
    drop_only: bool = False,
    create_only: bool = False,
) -> None:
    if not create_only:
        op.drop_constraint("ck_employee_position_canonical", "employee", type_="check")
        op.drop_constraint(
            "ck_employee_position_event_position_canonical",
            "employee_position_event",
            type_="check",
        )
    if drop_only:
        return
    op.create_check_constraint(
        "ck_employee_position_canonical",
        "employee",
        f"position in {_sql_values(values)}",
    )
    op.create_check_constraint(
        "ck_employee_position_event_position_canonical",
        "employee_position_event",
        f"position in {_sql_values(values)}",
    )


def _seed_auxiliary_no_category_availability() -> None:
    conn = op.get_bind()
    for position_group in AUX_POSITIONS:
        for category in EMPLOYEE_CATEGORIES:
            conn.execute(
                sa.text(
                    """
                    insert into payroll_role_category_availability (
                        position_group,
                        category,
                        is_enabled
                    )
                    values (
                        :position_group,
                        :category,
                        false
                    )
                    on conflict (position_group, category)
                    do update set is_enabled = false
                    """
                ),
                {"position_group": position_group, "category": category},
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
        "employee_allowance_event",
        "employee_pending_iiko_action",
    ):
        conn.execute(
            sa.text(
                f"""
                delete from {table_name}
                 where employee_id in (select id from tmp_aux_employee_ids)
                """
            )
        )


def _sql_values(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"

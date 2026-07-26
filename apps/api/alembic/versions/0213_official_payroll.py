"""Официальный зарплатный контур сотрудника (решение владельца 26.07.2026).

Поля на ``employee``: флаг «официальный», официальные ФИО (имя в iiko может быть
псевдонимом), табельный номер (ключ сверки с обороткой), официальный оклад, вычет НДФЛ,
статус working/maternity_leave. По этим данным налоговый модуль прогнозирует начисления
(НДФЛ 13%, взносы МСП 30/15, травматизм 0,2%) за отработанные месяцы до прихода оборотки.

Права ``staff.official.read`` / ``staff.official.manage`` — только owner/admin (оклады и
вычеты — чувствительные данные; из дефолта офис-менеджера исключены).

Revision ID: 0213_official_payroll
Revises: 0212_tax_bank_draft
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.auth.permissions import PERMISSION_CATALOG

revision = "0213_official_payroll"
down_revision = "0212_tax_bank_draft"
branch_labels = None
depends_on = None

NEW_CODES = ("staff.official.read", "staff.official.manage")
GRANT_ROLES = ("owner", "admin")


def upgrade() -> None:
    op.add_column(
        "employee",
        sa.Column("is_official", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "employee", sa.Column("official_full_name", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "employee", sa.Column("official_tab_number", sa.String(length=16), nullable=True)
    )
    op.add_column(
        "employee", sa.Column("official_salary", sa.Numeric(precision=12, scale=2), nullable=True)
    )
    op.add_column(
        "employee",
        sa.Column(
            "official_ndfl_deduction",
            sa.Numeric(precision=12, scale=2),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "employee",
        sa.Column(
            "official_status", sa.String(length=16), nullable=False, server_default="working"
        ),
    )
    op.create_check_constraint(
        "ck_employee_official_status_value",
        "employee",
        "official_status in ('working', 'maternity_leave')",
    )
    op.create_check_constraint(
        "ck_employee_official_salary_positive",
        "employee",
        "official_salary is null or official_salary > 0",
    )
    _upsert_permissions()
    _grant_permissions(GRANT_ROLES, NEW_CODES)


def downgrade() -> None:
    codes = _sql_values(NEW_CODES)
    op.execute(
        f"delete from role_permission_event using permission "
        f"where role_permission_event.permission_id = permission.id "
        f"and permission.code in ({codes})"
    )
    op.execute(
        f"delete from role_permission using permission "
        f"where role_permission.permission_id = permission.id "
        f"and permission.code in ({codes})"
    )
    op.execute(f"delete from permission where code in ({codes})")
    op.drop_constraint("ck_employee_official_salary_positive", "employee", type_="check")
    op.drop_constraint("ck_employee_official_status_value", "employee", type_="check")
    op.drop_column("employee", "official_status")
    op.drop_column("employee", "official_ndfl_deduction")
    op.drop_column("employee", "official_salary")
    op.drop_column("employee", "official_tab_number")
    op.drop_column("employee", "official_full_name")
    op.drop_column("employee", "is_official")


def _upsert_permissions() -> None:
    permission_table = sa.table(
        "permission",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("code", sa.String()),
        sa.column("module", sa.String()),
        sa.column("description", sa.String()),
    )
    rows = [
        {"id": uuid.uuid4(), "code": code, "module": module, "description": description}
        for code, module, description in PERMISSION_CATALOG
    ]
    insert_stmt = postgresql.insert(permission_table).values(rows)
    op.get_bind().execute(
        insert_stmt.on_conflict_do_update(
            index_elements=["code"],
            set_={
                "module": insert_stmt.excluded.module,
                "description": insert_stmt.excluded.description,
            },
        )
    )


def _grant_permissions(role_codes: tuple[str, ...], permission_codes: tuple[str, ...]) -> None:
    op.execute(
        f"""
        insert into role_permission (role_id, permission_id)
        select role.id, permission.id
        from role cross join permission
        where role.code in ({_sql_values(role_codes)})
          and permission.code in ({_sql_values(permission_codes)})
        on conflict (role_id, permission_id) do nothing
        """
    )


def _sql_values(values: tuple[str, ...]) -> str:
    return ", ".join("'" + value.replace("'", "''") + "'" for value in values)

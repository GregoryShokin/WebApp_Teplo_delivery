"""structure employee category, cooking station and computed status

Revision ID: 0006_employee_category_station
Revises: 0005_setting_display_metadata
Create Date: 2026-05-27
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0006_employee_category_station"
down_revision = "0005_setting_display_metadata"
branch_labels = None
depends_on = None


OLD_EMPLOYEE_STATUS = postgresql.ENUM(
    "active",
    "inactive",
    "needs_setup",
    name="employee_status",
)
NEW_EMPLOYEE_STATUS = postgresql.ENUM(
    "active",
    "inactive",
    "requires_setup",
    name="employee_status",
)

CATEGORY_VALUES = ("category_1", "category_2", "category_3", "intern", "freelancer")
STATION_VALUES = ("sushi", "pizza", "shawarma")


def upgrade() -> None:
    _replace_status_enum(
        old_enum=OLD_EMPLOYEE_STATUS,
        new_enum=NEW_EMPLOYEE_STATUS,
        old_value="needs_setup",
        new_value="requires_setup",
    )

    op.alter_column(
        "employee",
        "category",
        existing_type=sa.String(length=160),
        type_=sa.Text(),
        existing_nullable=True,
        existing_comment="source=app_managed",
        comment="source=app_managed",
    )
    op.execute("alter table employee add column if not exists default_cooking_station text")
    op.execute("comment on column employee.default_cooking_station is 'source=app_managed'")

    op.execute(
        """
        update employee
           set category = case
               when category is null or btrim(category) = '' then null
               when replace(lower(btrim(category)), 'ё', 'е') in
                    ('category_1', '1', '1-я', '1я', '1-ая', '1 категория',
                     'первая', 'первая категория')
                    then 'category_1'
               when replace(lower(btrim(category)), 'ё', 'е') in
                    ('category_2', '2', '2-я', '2я', '2-ая', '2 категория',
                     'вторая', 'вторая категория')
                    then 'category_2'
               when replace(lower(btrim(category)), 'ё', 'е') in
                    ('category_3', '3', '3-я', '3я', '3-ая', '3 категория',
                     'третья', 'третья категория')
                    then 'category_3'
               when replace(lower(btrim(category)), 'ё', 'е') in
                    ('intern', 'стажер', 'стажировка')
                    then 'intern'
               when replace(lower(btrim(category)), 'ё', 'е') in
                    ('freelancer', 'внештатный', 'внештатник')
                    then 'freelancer'
               else null
           end
        """
    )

    op.create_check_constraint(
        "ck_employee_category_value",
        "employee",
        f"category is null or category in {_sql_values(CATEGORY_VALUES)}",
    )
    op.create_check_constraint(
        "ck_employee_default_cooking_station_value",
        "employee",
        f"default_cooking_station is null or default_cooking_station in {_sql_values(STATION_VALUES)}",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_employee_default_cooking_station_value",
        "employee",
        type_="check",
    )
    op.drop_constraint("ck_employee_category_value", "employee", type_="check")
    op.drop_column("employee", "default_cooking_station")
    op.alter_column(
        "employee",
        "category",
        existing_type=sa.Text(),
        type_=sa.String(length=160),
        existing_nullable=True,
        existing_comment="source=app_managed",
        comment="source=app_managed",
    )

    _replace_status_enum(
        old_enum=NEW_EMPLOYEE_STATUS,
        new_enum=OLD_EMPLOYEE_STATUS,
        old_value="requires_setup",
        new_value="needs_setup",
    )


def _replace_status_enum(
    *,
    old_enum: postgresql.ENUM,
    new_enum: postgresql.ENUM,
    old_value: str,
    new_value: str,
) -> None:
    bind = op.get_bind()
    op.alter_column(
        "employee",
        "status",
        existing_type=old_enum,
        type_=sa.Text(),
        existing_nullable=False,
        server_default=None,
        postgresql_using="status::text",
    )
    op.execute(
        sa.text("update employee set status = :new_value where status = :old_value").bindparams(
            old_value=old_value,
            new_value=new_value,
        )
    )
    old_enum.drop(bind, checkfirst=True)
    new_enum.create(bind, checkfirst=True)
    op.alter_column(
        "employee",
        "status",
        existing_type=sa.Text(),
        type_=new_enum,
        existing_nullable=False,
        server_default=sa.text("'active'"),
        postgresql_using="status::employee_status",
    )


def _sql_values(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"

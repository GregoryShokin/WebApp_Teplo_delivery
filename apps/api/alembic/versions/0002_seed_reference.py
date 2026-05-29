"""seed core reference data

Revision ID: 0002_seed_reference
Revises: 0001_core_domain
Create Date: 2026-05-27
"""

from __future__ import annotations

import os
import secrets
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0002_seed_reference"
down_revision = "0001_core_domain"
branch_labels = None
depends_on = None


ROLE_CODES = ("admin", "owner", "finance_manager", "manager", "accountant")
DATA_SOURCE_CODES = ("iiko", "sber", "tbank", "mailru", "mango", "manual")
SETTING_KEYS = (
    "schedule.target_payroll_revenue_ratio",
    "schedule.payroll_plan_fact_deviation_threshold",
    "schedule.atypical_days",
    "payroll.open_shift_cap_hours",
    "payroll.open_shift_auto_close_time",
    "payroll.weekly_payment_window",
    "payroll.deposit_fund_payment_date",
    "payment_calendar.auto_match_tolerance",
    "payment_calendar.revenue_seasonality_coefficients",
    "balance.close_day",
    "fixed_assets.capitalization_threshold_rub",
    "fixed_assets.repair_modernization_threshold_ratio",
)


def _admin_password_hash() -> str:
    password_hash = os.environ.get("TEPLO_ADMIN_PASSWORD_HASH")
    if password_hash:
        return password_hash

    password = os.environ.get("TEPLO_ADMIN_PASSWORD")
    if not password:
        password = secrets.token_urlsafe(32)

    try:
        import bcrypt
    except ImportError:
        import hashlib

        return "sha256$" + hashlib.sha256(password.encode("utf-8")).hexdigest()

    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def upgrade() -> None:
    role_table = sa.table(
        "role",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("code", sa.String()),
        sa.column("name", sa.String()),
    )
    roles = [
        {"id": uuid.uuid4(), "code": "admin", "name": "Администратор"},
        {"id": uuid.uuid4(), "code": "owner", "name": "Собственник"},
        {"id": uuid.uuid4(), "code": "finance_manager", "name": "Финансовый менеджер"},
        {"id": uuid.uuid4(), "code": "manager", "name": "Управляющий"},
        {"id": uuid.uuid4(), "code": "accountant", "name": "Бухгалтер"},
    ]
    op.bulk_insert(role_table, roles)

    organization_id = uuid.uuid4()
    organization_table = sa.table(
        "organization",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("name", sa.String()),
        sa.column("inn", sa.String()),
        sa.column("kpp", sa.String()),
    )
    op.bulk_insert(
        organization_table,
        [{"id": organization_id, "name": "ИП Шокина", "inn": None, "kpp": None}],
    )

    location_table = sa.table(
        "location",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("organization_id", postgresql.UUID(as_uuid=True)),
        sa.column("name", sa.String()),
        sa.column("address", sa.String()),
        sa.column("status", postgresql.ENUM(name="location_status", create_type=False)),
    )
    op.bulk_insert(
        location_table,
        [
            {
                "id": uuid.uuid4(),
                "organization_id": organization_id,
                "name": "Черникова",
                "address": None,
                "status": "active",
            },
            {
                "id": uuid.uuid4(),
                "organization_id": organization_id,
                "name": "Гагарина",
                "address": None,
                "status": "inactive",
            },
        ],
    )

    admin_email = os.environ.get("TEPLO_ADMIN_EMAIL", "admin@teplo.local")
    user_table = sa.table(
        "user",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("email", sa.String()),
        sa.column("hashed_password", sa.String()),
        sa.column("full_name", sa.String()),
        sa.column("is_active", sa.Boolean()),
    )
    admin_user_insert = postgresql.insert(user_table).values(
        id=uuid.uuid4(),
        email=admin_email,
        hashed_password=_admin_password_hash(),
        full_name=os.environ.get("TEPLO_ADMIN_FULL_NAME", "Teplo Admin"),
        is_active=True,
    )
    admin_user_id = (
        op.get_bind()
        .execute(
            admin_user_insert.on_conflict_do_update(
                index_elements=["email"],
                set_={
                    "hashed_password": admin_user_insert.excluded.hashed_password,
                    "is_active": True,
                },
            ).returning(user_table.c.id)
        )
        .scalar_one()
    )

    admin_role_id = roles[0]["id"]
    user_role_table = sa.table(
        "user_role",
        sa.column("user_id", postgresql.UUID(as_uuid=True)),
        sa.column("role_id", postgresql.UUID(as_uuid=True)),
        sa.column("organization_id", postgresql.UUID(as_uuid=True)),
    )
    op.get_bind().execute(
        postgresql.insert(user_role_table)
        .values(
            user_id=admin_user_id,
            role_id=admin_role_id,
            organization_id=organization_id,
        )
        .on_conflict_do_nothing(
            index_elements=["user_id", "role_id", "organization_id"],
        )
    )

    data_source_table = sa.table(
        "data_source",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("code", sa.String()),
        sa.column("name", sa.String()),
        sa.column("type", postgresql.ENUM(name="data_source_type", create_type=False)),
        sa.column("status", sa.String()),
    )
    op.bulk_insert(
        data_source_table,
        [
            {
                "id": uuid.uuid4(),
                "code": "iiko",
                "name": "iiko Server API",
                "type": "api",
                "status": "active",
            },
            {
                "id": uuid.uuid4(),
                "code": "sber",
                "name": "Sber Business",
                "type": "api",
                "status": "active",
            },
            {
                "id": uuid.uuid4(),
                "code": "tbank",
                "name": "T-Bank Business",
                "type": "api",
                "status": "active",
            },
            {
                "id": uuid.uuid4(),
                "code": "mailru",
                "name": "Mail.ru mailbox",
                "type": "ai_email",
                "status": "active",
            },
            {
                "id": uuid.uuid4(),
                "code": "mango",
                "name": "Mango Office",
                "type": "browser_lk",
                "status": "active",
            },
            {
                "id": uuid.uuid4(),
                "code": "manual",
                "name": "Manual input",
                "type": "manual",
                "status": "active",
            },
        ],
    )

    settings = [
        {
            "id": uuid.uuid4(),
            "key": "schedule.target_payroll_revenue_ratio",
            "value": 0.28,
            "value_type": "number",
            "category": "График",
            "description": "Целевой ФОТ как доля от выручки.",
        },
        {
            "id": uuid.uuid4(),
            "key": "schedule.payroll_plan_fact_deviation_threshold",
            "value": 0.03,
            "value_type": "number",
            "category": "График",
            "description": "Порог отклонения план-факт ФОТ.",
        },
        {
            "id": uuid.uuid4(),
            "key": "schedule.atypical_days",
            "value": {
                "dates": ["02-14", "02-22", "02-23", "03-08", "06-01", "09-01"],
                "ranges": [{"start": "12-26", "end": "12-31"}, {"start": "01-02", "end": "01-10"}],
            },
            "value_type": "object",
            "category": "График",
            "description": "Праздничные и нетиповые дни для графика и прогноза.",
        },
        {
            "id": uuid.uuid4(),
            "key": "payroll.open_shift_cap_hours",
            "value": 12,
            "value_type": "integer",
            "category": "Зарплата",
            "description": "Максимальная длительность открытой смены.",
        },
        {
            "id": uuid.uuid4(),
            "key": "payroll.open_shift_auto_close_time",
            "value": "22:00",
            "value_type": "time",
            "category": "Зарплата",
            "description": "Время автоматического закрытия открытой смены.",
        },
        {
            "id": uuid.uuid4(),
            "key": "payroll.weekly_payment_window",
            "value": {"payday": "tuesday", "period_start": "tuesday", "period_end": "monday"},
            "value_type": "object",
            "category": "Зарплата",
            "description": "Окно еженедельной выплаты зарплаты.",
        },
        {
            "id": uuid.uuid4(),
            "key": "payroll.deposit_fund_payment_date",
            "value": "01-15",
            "value_type": "month_day",
            "category": "Зарплата",
            "description": "Дата выплаты накопительного фонда.",
        },
        {
            "id": uuid.uuid4(),
            "key": "payment_calendar.auto_match_tolerance",
            "value": {"relative": 0.10, "date_window": "same_month"},
            "value_type": "object",
            "category": "Платёжный календарь",
            "description": "Tolerance auto-match план/факт ДДС.",
        },
        {
            "id": uuid.uuid4(),
            "key": "payment_calendar.revenue_seasonality_coefficients",
            "value": {},
            "value_type": "object",
            "category": "Платёжный календарь",
            "description": "Сезонные коэффициенты выручки.",
        },
        {
            "id": uuid.uuid4(),
            "key": "balance.close_day",
            "value": 7,
            "value_type": "integer",
            "category": "Финансы/Баланс",
            "description": "Срок закрытия баланса до указанного числа месяца.",
        },
        {
            "id": uuid.uuid4(),
            "key": "fixed_assets.capitalization_threshold_rub",
            "value": 5000,
            "value_type": "money",
            "category": "Учёт ОС",
            "description": "Порог признания основного средства.",
        },
        {
            "id": uuid.uuid4(),
            "key": "fixed_assets.repair_modernization_threshold_ratio",
            "value": 0.15,
            "value_type": "number",
            "category": "Учёт ОС",
            "description": "Граница ремонт/модернизация от первоначальной стоимости.",
        },
    ]

    app_setting_table = sa.table(
        "app_setting",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("key", sa.String()),
        sa.column("value", postgresql.JSONB()),
        sa.column("value_type", sa.String()),
        sa.column("category", sa.String()),
        sa.column("description", sa.Text()),
        sa.column("updated_by_user_id", postgresql.UUID(as_uuid=True)),
    )
    op.bulk_insert(
        app_setting_table,
        [setting | {"updated_by_user_id": admin_user_id} for setting in settings],
    )

    app_setting_history_table = sa.table(
        "app_setting_history",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("setting_id", postgresql.UUID(as_uuid=True)),
        sa.column("old_value", postgresql.JSONB()),
        sa.column("new_value", postgresql.JSONB()),
        sa.column("changed_by_user_id", postgresql.UUID(as_uuid=True)),
    )
    op.bulk_insert(
        app_setting_history_table,
        [
            {
                "id": uuid.uuid4(),
                "setting_id": setting["id"],
                "old_value": None,
                "new_value": setting["value"],
                "changed_by_user_id": admin_user_id,
            }
            for setting in settings
        ],
    )


def downgrade() -> None:
    admin_email = os.environ.get("TEPLO_ADMIN_EMAIL", "admin@teplo.local")
    conn = op.get_bind()

    conn.execute(
        sa.text(
            "delete from app_setting_history "
            "where setting_id in (select id from app_setting where key = any(:keys))"
        ),
        {"keys": list(SETTING_KEYS)},
    )
    conn.execute(
        sa.text("delete from app_setting where key = any(:keys)"),
        {"keys": list(SETTING_KEYS)},
    )
    conn.execute(
        sa.text("delete from data_source where code = any(:codes)"),
        {"codes": list(DATA_SOURCE_CODES)},
    )
    conn.execute(
        sa.text(
            'delete from user_role where user_id in (select id from "user" where email = :email)'
        ),
        {"email": admin_email},
    )
    conn.execute(sa.text("delete from location where name in ('Черникова', 'Гагарина')"))
    conn.execute(sa.text("delete from organization where name = 'ИП Шокина'"))
    conn.execute(sa.text('delete from "user" where email = :email'), {"email": admin_email})
    conn.execute(sa.text("delete from role where code = any(:codes)"), {"codes": list(ROLE_CODES)})

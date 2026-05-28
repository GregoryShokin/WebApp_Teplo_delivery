from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

import app.models as models
from alembic import command
from app.core.config import get_settings
from app.db.base import Base
from app.models.enums import quality_status_enum

API_DIR = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get(
    "TEPLO_TEST_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql+asyncpg://teplo:teplo@localhost:5432/teplo"),
)

EXPECTED_TABLES = {
    "organization",
    "location",
    "user",
    "role",
    "user_role",
    "counterparty",
    "counterparty_role",
    "employee",
    "wallet",
    "period",
    "data_source",
    "source_credential",
    "source_snapshot",
    "parsed_document",
    "source_document",
    "agent_run",
    "agent_action",
    "app_setting",
    "app_setting_history",
    "payroll_period",
    "attendance_entry",
    "payroll_run",
    "payroll_line",
    "deposit_account",
    "deposit_transaction",
    "accumulation_fund_account",
    "payroll_rate",
    "payroll_role_category_availability",
    "payroll_revenue_share",
    "revenue_tier",
    "category_coefficient",
    "payroll_deduction_category",
    "payroll_seniority_premium",
}


def test_all_models_import() -> None:
    exported = set(models.__all__)

    assert {
        "Organization",
        "Location",
        "User",
        "Role",
        "UserRole",
        "Counterparty",
        "CounterpartyRole",
        "Employee",
        "Wallet",
        "Period",
        "DataSource",
        "SourceCredential",
        "SourceSnapshot",
        "ParsedDocument",
        "SourceDocument",
        "AgentRun",
        "AgentAction",
        "AppSetting",
        "AppSettingHistory",
        "PayrollPeriod",
        "AttendanceEntry",
        "PayrollRun",
        "PayrollLine",
        "PayrollRate",
        "PayrollRoleCategoryAvailability",
        "PayrollRevenueShare",
        "RevenueTier",
        "CategoryCoefficient",
        "PayrollDeductionCategory",
        "PayrollSeniorityPremium",
        "DepositAccount",
        "DepositTransaction",
        "AccumulationFundAccount",
    } <= exported


def test_metadata_contains_core_domain_tables() -> None:
    assert set(Base.metadata.tables) >= EXPECTED_TABLES


def test_employee_full_name_is_marked_iiko_read_only() -> None:
    column = models.Employee.__table__.c.full_name

    assert column.info == {"source": "iiko", "read_only": True}
    assert column.comment == "source=iiko; read-only in app"


def test_required_unique_constraints_are_declared() -> None:
    assert models.User.__table__.c.email.unique is True
    assert models.Employee.__table__.c.iiko_id.unique is True
    assert models.AppSetting.__table__.c.key.unique is True


def test_app_setting_display_metadata_columns_are_declared() -> None:
    columns = models.AppSetting.__table__.c

    assert columns.display_name.nullable is False
    assert columns.widget_type.nullable is False
    assert "widget_options" in columns
    assert "unit" in columns


def test_payroll_configuration_tables_are_declared() -> None:
    rate_columns = models.PayrollRate.__table__.c
    availability_columns = models.PayrollRoleCategoryAvailability.__table__.c
    tier_columns = models.RevenueTier.__table__.c
    coefficient_columns = models.CategoryCoefficient.__table__.c
    deduction_columns = models.PayrollDeductionCategory.__table__.c
    premium_columns = models.PayrollSeniorityPremium.__table__.c

    assert rate_columns.position_group.nullable is False
    assert rate_columns.amount.nullable is True
    assert rate_columns.is_active.nullable is False
    assert rate_columns.effective_to.nullable is True
    assert availability_columns.position_group.nullable is False
    assert availability_columns.category.nullable is False
    assert availability_columns.is_enabled.nullable is False
    assert tier_columns.min_revenue.nullable is False
    assert tier_columns.max_revenue.nullable is True
    assert coefficient_columns.category.nullable is False
    assert coefficient_columns.coefficient.nullable is False
    assert deduction_columns.code.nullable is False
    assert premium_columns.percent_of_base.nullable is False


def test_percent_methodology_is_additive_for_existing_payroll_runs() -> None:
    run_columns = set(models.PayrollRun.__table__.c.keys())
    line_columns = set(models.PayrollLine.__table__.c.keys())

    assert {
        "id",
        "period_id",
        "started_at",
        "finished_at",
        "status",
        "blocking_issues",
        "summary",
    } <= run_columns
    assert {
        "run_id",
        "employee_id",
        "role",
        "base_pay",
        "premium",
        "percent_pay",
        "fund_accrual",
        "deduction",
        "total_payable",
        "components",
    } <= line_columns


def test_counterparty_inn_partial_unique_index_is_declared() -> None:
    indexes = {index.name: index for index in models.Counterparty.__table__.indexes}

    assert "uq_counterparty_inn_not_null" in indexes
    assert indexes["uq_counterparty_inn_not_null"].unique is True
    assert str(indexes["uq_counterparty_inn_not_null"].dialect_options["postgresql"]["where"])


def test_quality_status_enum_values_are_canonical() -> None:
    assert quality_status_enum.enums == [
        "draft",
        "partial",
        "final",
        "requires_review",
        "not_applicable",
    ]


async def _ping_database(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("select 1"))
    finally:
        await engine.dispose()


@pytest.fixture()
def postgres_available() -> None:
    try:
        asyncio.run(_ping_database(TEST_DATABASE_URL))
    except Exception as exc:
        pytest.skip(f"PostgreSQL test database is not available: {exc}")


@pytest.fixture()
def alembic_cfg(monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("TEPLO_ADMIN_PASSWORD", "test-admin-password")
    get_settings.cache_clear()

    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    return cfg


@pytest.fixture()
def migrated_db(alembic_cfg: Config, postgres_available: None) -> str:
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "head")
    try:
        yield TEST_DATABASE_URL
    finally:
        command.downgrade(alembic_cfg, "base")


def test_migrations_upgrade_and_downgrade(alembic_cfg: Config, postgres_available: None) -> None:
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, "base")


async def test_seed_creates_expected_reference_rows(migrated_db: str) -> None:
    engine = create_async_engine(migrated_db)
    try:
        async with engine.connect() as conn:
            counts = {
                "role": await conn.scalar(text("select count(*) from role")),
                "data_source": await conn.scalar(text("select count(*) from data_source")),
                "organization": await conn.scalar(text("select count(*) from organization")),
                "location": await conn.scalar(text("select count(*) from location")),
                "user": await conn.scalar(text('select count(*) from "user"')),
                "app_setting": await conn.scalar(text("select count(*) from app_setting")),
                "app_setting_history": await conn.scalar(
                    text("select count(*) from app_setting_history")
                ),
                "payroll_rate": await conn.scalar(text("select count(*) from payroll_rate")),
                "payroll_role_category_availability": await conn.scalar(
                    text("select count(*) from payroll_role_category_availability")
                ),
                "enabled_payroll_role_category_availability": await conn.scalar(
                    text(
                        """
                        select count(*)
                          from payroll_role_category_availability
                         where is_enabled = true
                        """
                    )
                ),
                "payroll_revenue_share": await conn.scalar(
                    text("select count(*) from payroll_revenue_share")
                ),
                "revenue_tier": await conn.scalar(text("select count(*) from revenue_tier")),
                "category_coefficient": await conn.scalar(
                    text("select count(*) from category_coefficient")
                ),
                "payroll_deduction_category": await conn.scalar(
                    text("select count(*) from payroll_deduction_category")
                ),
                "payroll_seniority_premium": await conn.scalar(
                    text("select count(*) from payroll_seniority_premium")
                ),
                "invalid_payroll_rate_category": await conn.scalar(
                    text(
                        """
                        select count(*)
                          from payroll_rate
                         where category not in (
                             'category_1',
                             'category_2',
                             'category_3',
                             'intern',
                             'freelancer'
                         )
                        """
                    )
                ),
                "invalid_employee_category": await conn.scalar(
                    text(
                        """
                        select count(*)
                          from employee
                         where category is not null
                           and category not in (
                               'category_1',
                               'category_2',
                               'category_3',
                               'intern',
                               'freelancer'
                           )
                        """
                    )
                ),
                "inactive_payroll_rate_placeholder": await conn.scalar(
                    text(
                        """
                        select count(*)
                          from payroll_rate
                         where is_active = false
                           and amount is null
                        """
                    )
                ),
            }
    finally:
        await engine.dispose()

    assert counts == {
        "role": 5,
        "data_source": 6,
        "organization": 1,
        "location": 2,
        "user": 1,
        "app_setting": 21,
        "app_setting_history": 21,
        "payroll_rate": 20,
        "payroll_role_category_availability": 20,
        "enabled_payroll_role_category_availability": 14,
        "payroll_revenue_share": 4,
        "revenue_tier": 4,
        "category_coefficient": 5,
        "payroll_deduction_category": 4,
        "payroll_seniority_premium": 2,
        "invalid_payroll_rate_category": 0,
        "invalid_employee_category": 0,
        "inactive_payroll_rate_placeholder": 6,
    }


async def test_seeded_settings_have_display_metadata(migrated_db: str) -> None:
    engine = create_async_engine(migrated_db)
    try:
        async with engine.connect() as conn:
            missing_display_names = await conn.scalar(
                text("select count(*) from app_setting where display_name is null")
            )
            target_payroll = (
                await conn.execute(
                    text(
                        "select category, display_name, widget_type, unit "
                        "from app_setting where key = 'schedule.target_payroll_revenue_ratio'"
                    )
                )
            ).one()
            weekday_premium = (
                await conn.execute(
                    text(
                        "select category, value, widget_type, unit "
                        "from app_setting where key = 'payroll.weekday_premium'"
                    )
                )
            ).one()
    finally:
        await engine.dispose()

    assert missing_display_names == 0
    assert target_payroll == (
        "schedule",
        "Целевой ФОТ % от выручки",
        "percent",
        "%",
    )
    assert weekday_premium == (
        "payroll",
        {"friday": 200, "saturday": 200},
        "weekday_premium",
        "₽",
    )


async def test_employee_iiko_id_unique_constraint_raises_integrity_error(
    migrated_db: str,
) -> None:
    engine = create_async_engine(migrated_db)
    duplicate_iiko_id = "iiko-duplicate-test"
    try:
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "insert into employee (id, full_name, iiko_id, status) "
                        "values (:id, :full_name, :iiko_id, 'active')"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "full_name": "First Employee",
                        "iiko_id": duplicate_iiko_id,
                    },
                )
                await conn.execute(
                    text(
                        "insert into employee (id, full_name, iiko_id, status) "
                        "values (:id, :full_name, :iiko_id, 'active')"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "full_name": "Second Employee",
                        "iiko_id": duplicate_iiko_id,
                    },
                )
    finally:
        await engine.dispose()

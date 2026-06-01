from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, date, datetime
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
    "employee_role_assignment",
    "employee_change_event",
    "employee_dismissal_reason",
    "delivery_order",
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
    "shift_ledger_entry",
    "shift_schedule",
    "scheduled_shift",
    "payroll_run",
    "payroll_line",
    "deposit_account",
    "deposit_transaction",
    "accumulation_fund_account",
    "accumulation_fund_transaction",
    "payroll_rate",
    "payroll_role_category_availability",
    "payroll_revenue_share",
    "revenue_tier",
    "category_coefficient",
    "payroll_deduction_category",
    "payroll_adjustment_category",
    "payroll_adjustment",
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
        "EmployeeChangeEvent",
        "EmployeeDismissalReason",
        "EmployeeRoleAssignment",
        "DeliveryOrder",
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
        "ShiftLedgerEntry",
        "ScheduledShift",
        "ShiftSchedule",
        "PayrollRun",
        "PayrollLine",
        "PayrollRate",
        "PayrollRoleCategoryAvailability",
        "PayrollRevenueShare",
        "RevenueTier",
        "CategoryCoefficient",
        "PayrollDeductionCategory",
        "PayrollAdjustment",
        "PayrollAdjustmentCategory",
        "PayrollSeniorityPremium",
        "DepositAccount",
        "DepositTransaction",
        "AccumulationFundAccount",
        "AccumulationFundTransaction",
    } <= exported


def test_metadata_contains_core_domain_tables() -> None:
    assert set(Base.metadata.tables) >= EXPECTED_TABLES


def test_employee_full_name_is_marked_iiko_read_only() -> None:
    column = models.Employee.__table__.c.full_name

    assert column.info == {"source": "iiko", "read_only": True}
    assert column.comment == "source=iiko; read-only in app"
    assert models.Employee.__table__.c.pin_hash.nullable is True
    assert models.Employee.__table__.c.pin_assumed_from_iiko.nullable is False
    assert models.Employee.__table__.c.pin_set_at.nullable is True
    assert models.Employee.__table__.c.deposit_target_override.nullable is True
    assert models.Employee.__table__.c.deposit_withholding_override.nullable is True
    assert models.Employee.__table__.c.deposit_excluded.nullable is False
    assert models.Employee.__table__.c.deposit_excluded_until.nullable is True
    assert models.Employee.__table__.c.deposit_excluded_reason.nullable is True
    assert models.Employee.__table__.c.fund_excluded.nullable is False
    assert models.Employee.__table__.c.fund_excluded_until.nullable is True
    assert models.Employee.__table__.c.fund_excluded_reason.nullable is True
    constraints = {constraint.name for constraint in models.Employee.__table__.constraints}
    assert "ck_employee_pin_origin_exclusive" in constraints
    assert "ck_employee_deposit_target_override_non_negative" in constraints
    assert "ck_employee_deposit_withholding_override_non_negative" in constraints


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
    adjustment_category_columns = models.PayrollAdjustmentCategory.__table__.c
    adjustment_columns = models.PayrollAdjustment.__table__.c
    premium_columns = models.PayrollSeniorityPremium.__table__.c

    assert rate_columns.position_group.nullable is False
    assignment_columns = models.EmployeeRoleAssignment.__table__.c
    assert assignment_columns.employee_id.nullable is False
    assert assignment_columns.payroll_role.nullable is False
    assert assignment_columns.category.nullable is False
    assert assignment_columns.is_primary.nullable is False
    assert assignment_columns.effective_to.nullable is True
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
    assert adjustment_category_columns.code.nullable is False
    assert adjustment_category_columns.type.nullable is False
    assert adjustment_columns.employee_id.nullable is False
    assert adjustment_columns.work_date.nullable is False
    assert adjustment_columns.amount.nullable is False
    assert premium_columns.position.nullable is False
    assert premium_columns.amount.nullable is False
    assert premium_columns.percent_of_base.nullable is True


def test_employee_active_seniority_unique_indexes_are_declared() -> None:
    indexes = {index.name: index for index in models.Employee.__table__.indexes}

    assert indexes["uq_employee_active_senior_per_position"].unique is True
    assert indexes["uq_employee_active_deputy_senior_per_position"].unique is True


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


def test_deposit_account_initial_balance_is_declared() -> None:
    columns = models.DepositAccount.__table__.c
    constraints = {constraint.name for constraint in models.DepositAccount.__table__.constraints}

    assert columns.initial_balance.nullable is False
    assert "ck_deposit_account_initial_balance_non_negative" in constraints


def test_shift_ledger_entry_table_is_declared() -> None:
    columns = models.ShiftLedgerEntry.__table__.c
    indexes = {index.name for index in models.ShiftLedgerEntry.__table__.indexes}

    assert columns.work_date.nullable is False
    assert columns.employee_id.nullable is False
    assert columns.payroll_role.nullable is True
    assert columns.category.nullable is True
    assert columns.source.nullable is False
    assert columns.opened_at.nullable is False
    assert columns.closed_at.nullable is True
    assert columns.is_resolved.nullable is False
    assert "ix_shift_ledger_entry_work_date" in indexes


def test_shift_schedule_tables_are_declared() -> None:
    schedule_columns = models.ShiftSchedule.__table__.c
    shift_columns = models.ScheduledShift.__table__.c
    schedule_indexes = {index.name for index in models.ShiftSchedule.__table__.indexes}
    shift_indexes = {index.name for index in models.ScheduledShift.__table__.indexes}
    schedule_constraints = {
        constraint.name for constraint in models.ShiftSchedule.__table__.constraints
    }
    shift_constraints = {
        constraint.name for constraint in models.ScheduledShift.__table__.constraints
    }

    assert schedule_columns.date_start.nullable is False
    assert schedule_columns.date_end.nullable is False
    assert schedule_columns.status.nullable is False
    assert schedule_columns.superseded_by_id.nullable is True
    assert "ck_shift_schedule_status" in schedule_constraints
    assert "ck_shift_schedule_date_range" in schedule_constraints
    assert "ix_shift_schedule_status_date_start" in schedule_indexes
    assert shift_columns.shift_schedule_id.nullable is False
    assert shift_columns.business_date.nullable is False
    assert shift_columns.employee_id.nullable is False
    assert shift_columns.payroll_role.nullable is False
    assert shift_columns.station_code.nullable is True
    assert "ck_scheduled_shift_interval" in shift_constraints
    assert "uq_scheduled_shift_schedule_date_employee" in shift_constraints
    assert "ix_scheduled_shift_schedule_date" in shift_indexes
    assert "ix_scheduled_shift_employee_date" in shift_indexes


def test_delivery_order_table_is_declared() -> None:
    columns = models.DeliveryOrder.__table__.c
    indexes = {index.name for index in models.DeliveryOrder.__table__.indexes}

    assert columns.iiko_order_id.nullable is False
    assert columns.iiko_order_id.unique is True
    assert columns.work_date.nullable is False
    assert columns.raw.nullable is False
    assert "ix_delivery_order_work_date" in indexes
    assert "ix_delivery_order_courier_work_date" in indexes
    assert "ix_delivery_order_status" in indexes


def test_employee_change_event_table_is_declared() -> None:
    columns = models.EmployeeChangeEvent.__table__.c
    indexes = {index.name for index in models.EmployeeChangeEvent.__table__.indexes}

    assert columns.employee_id.nullable is True
    assert columns.changed_at.nullable is False
    assert columns.effective_from.nullable is True
    assert columns.change_type.nullable is False
    assert columns.source.nullable is False
    assert columns.actor_user_id.nullable is True
    assert columns.status.nullable is False
    assert columns.before_value.nullable is True
    assert columns.after_value.nullable is True
    assert columns.diff.nullable is True
    assert columns.reason_id.nullable is True
    assert columns.reason_code.nullable is True
    assert columns.related_agent_run_id.nullable is True
    assert columns.related_agent_action_id.nullable is True
    assert columns.payroll_impact.nullable is False
    assert columns.payroll_impact_metadata.nullable is False
    assert "ix_employee_change_event_employee_changed" in indexes
    assert "ix_employee_change_event_source_status" in indexes


def test_employee_dismissal_reason_table_is_declared() -> None:
    columns = models.EmployeeDismissalReason.__table__.c

    assert columns.code.nullable is False
    assert columns.label.nullable is False
    assert columns.requires_comment.nullable is False
    assert columns.is_system.nullable is False
    assert columns.is_active.nullable is False
    assert columns.sort_order.nullable is False


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


async def test_pin_origin_migration_backfills_iiko_employees_without_local_pin(
    alembic_cfg: Config,
    postgres_available: None,
) -> None:
    assumed_id = uuid.uuid4()
    local_id = uuid.uuid4()
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "0024_employee_effective_events")

    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    insert into employee (
                        id,
                        full_name,
                        iiko_id,
                        position,
                        status,
                        pin_hash
                    )
                    values
                        (:assumed_id, 'Iiko Assumed', 'iiko-assumed', 'Повар', 'requires_setup', null),
                        (:local_id, 'Local Pin', 'iiko-local', 'Повар', 'active', 'hashed-pin')
                    """
                ),
                {"assumed_id": assumed_id, "local_id": local_id},
            )
    finally:
        await engine.dispose()

    command.upgrade(alembic_cfg, "0022_pin_origin")

    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        """
                        select iiko_id, pin_assumed_from_iiko
                          from employee
                         order by iiko_id
                        """
                    )
                )
            ).all()
    finally:
        await engine.dispose()
        command.downgrade(alembic_cfg, "base")

    assert rows == [("iiko-assumed", True), ("iiko-local", False)]


@pytest.mark.skip(
    reason="Obsolete after canonical taxonomy (migration 0020): employees with "
    "non-canonical position='Сушист' are cleaned up at head, so the legacy backfill "
    "from 0013 no longer survives a full upgrade."
)
async def test_employee_role_assignment_backfill_from_legacy_shortcuts(
    alembic_cfg: Config,
    postgres_available: None,
) -> None:
    employee_id = uuid.uuid4()
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "0012_weekday_payroll_premium")

    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    insert into employee (
                        id,
                        full_name,
                        iiko_id,
                        position,
                        category,
                        default_cooking_station,
                        status
                    )
                    values (
                        :id,
                        'Александр Ушанов',
                        'iiko-alexander-ushanov',
                        'Сушист',
                        'category_1',
                        'sushi',
                        'active'
                    )
                    """
                ),
                {"id": employee_id},
            )
    finally:
        await engine.dispose()

    command.upgrade(alembic_cfg, "head")

    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        """
                        select payroll_role, category, is_primary
                          from employee_role_assignment
                         where employee_id = :employee_id
                        """
                    ),
                    {"employee_id": employee_id},
                )
            ).all()
    finally:
        await engine.dispose()
        command.downgrade(alembic_cfg, "base")

    assert rows == [("sushi", "category_1", True)]


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
                "current_category_coefficient": await conn.scalar(
                    text(
                        """
                        select count(*)
                          from category_coefficient
                         where effective_from <= date '2026-05-28'
                           and (effective_to is null or effective_to > date '2026-05-28')
                        """
                    )
                ),
                "current_category_2_coefficient": await conn.scalar(
                    text(
                        """
                        select coefficient::text
                          from category_coefficient
                         where category = 'category_2'
                           and effective_from <= date '2026-05-28'
                           and (effective_to is null or effective_to > date '2026-05-28')
                        """
                    )
                ),
                "payroll_deduction_category": await conn.scalar(
                    text("select count(*) from payroll_deduction_category")
                ),
                "payroll_adjustment_category": await conn.scalar(
                    text("select count(*) from payroll_adjustment_category")
                ),
                "payroll_seniority_premium": await conn.scalar(
                    text("select count(*) from payroll_seniority_premium")
                ),
                "employee_dismissal_reason": await conn.scalar(
                    text("select count(*) from employee_dismissal_reason")
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
                             'category_4',
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
                               'category_4',
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
        "payroll_rate": 22,
        "payroll_role_category_availability": 30,
        "enabled_payroll_role_category_availability": 16,
        "payroll_revenue_share": 4,
        "revenue_tier": 4,
        "category_coefficient": 7,
        "current_category_coefficient": 6,
        "current_category_2_coefficient": "2.250",
        "payroll_deduction_category": 4,
        "payroll_adjustment_category": 6,
        "payroll_seniority_premium": 2,
        "employee_dismissal_reason": 7,
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


async def test_seeded_employee_dismissal_reasons_are_available(migrated_db: str) -> None:
    engine = create_async_engine(migrated_db)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        """
                        select code, label, requires_comment, is_active, sort_order
                          from employee_dismissal_reason
                         order by sort_order, label
                        """
                    )
                )
            ).all()
    finally:
        await engine.dispose()

    assert rows == [
        ("voluntary", "По собственному желанию", False, True, 10),
        ("no_show", "Не вышел на смену", False, True, 20),
        ("discipline", "Нарушение дисциплины", False, True, 30),
        ("failed_trial", "Не прошёл стажировку", False, True, 40),
        ("layoff_no_shifts", "Сокращение/нет смен", False, True, 50),
        ("transfer", "Перевод", False, True, 60),
        ("other", "Другое", True, True, 70),
    ]


async def test_cleanup_non_canonical_employee_migration_deletes_dependents(
    alembic_cfg: Config,
    postgres_available: None,
) -> None:
    employee_id = uuid.uuid4()
    period_id = uuid.uuid4()
    assignment_id = uuid.uuid4()
    attendance_id = uuid.uuid4()
    ledger_id = uuid.uuid4()
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "0019_taxonomy_align")

    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    insert into payroll_period (
                        id,
                        period_type,
                        start_date,
                        end_date,
                        payroll_date,
                        status
                    )
                    values (:id, 'week', :start_date, :end_date, :payroll_date, 'open')
                    """
                ),
                {
                    "id": period_id,
                    "start_date": date(2026, 5, 25),
                    "end_date": date(2026, 5, 31),
                    "payroll_date": date(2026, 6, 1),
                },
            )
            await conn.execute(
                text(
                    """
                    insert into employee (
                        id,
                        full_name,
                        iiko_id,
                        position,
                        status
                    )
                    values (
                        :id,
                        'Legacy Waiter',
                        'iiko-legacy-waiter',
                        'Официант',
                        'active'
                    )
                    """
                ),
                {"id": employee_id},
            )
            await conn.execute(
                text(
                    """
                    insert into employee_role_assignment (
                        id,
                        employee_id,
                        payroll_role,
                        category,
                        is_primary,
                        effective_from
                    )
                    values (
                        :id,
                        :employee_id,
                        'sushi',
                        'category_1',
                        true,
                        :effective_from
                    )
                    """
                ),
                {
                    "id": assignment_id,
                    "employee_id": employee_id,
                    "effective_from": date(2026, 5, 29),
                },
            )
            await conn.execute(
                text(
                    """
                    insert into attendance_entry (
                        id,
                        employee_id,
                        period_id,
                        work_date,
                        started_at,
                        minutes_worked,
                        source,
                        quality_status
                    )
                    values (
                        :id,
                        :employee_id,
                        :period_id,
                        :work_date,
                        :started_at,
                        60,
                        'manual',
                        'ok'
                    )
                    """
                ),
                {
                    "id": attendance_id,
                    "employee_id": employee_id,
                    "period_id": period_id,
                    "work_date": date(2026, 5, 29),
                    "started_at": datetime(2026, 5, 29, 10, 0, tzinfo=UTC),
                },
            )
            await conn.execute(
                text(
                    """
                    insert into shift_ledger_entry (
                        id,
                        work_date,
                        employee_id,
                        payroll_role,
                        category,
                        source,
                        opened_at,
                        is_resolved
                    )
                    values (
                        :id,
                        :work_date,
                        :employee_id,
                        'sushi',
                        'category_1',
                        'manual_correction',
                        :opened_at,
                        true
                    )
                    """
                ),
                {
                    "id": ledger_id,
                    "work_date": date(2026, 5, 29),
                    "employee_id": employee_id,
                    "opened_at": datetime(2026, 5, 29, 10, 0, tzinfo=UTC),
                },
            )
    finally:
        await engine.dispose()

    command.upgrade(alembic_cfg, "head")

    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as conn:
            counts = {
                "employee": await conn.scalar(
                    text("select count(*) from employee where id = :id"),
                    {"id": employee_id},
                ),
                "assignment": await conn.scalar(
                    text("select count(*) from employee_role_assignment where id = :id"),
                    {"id": assignment_id},
                ),
                "attendance": await conn.scalar(
                    text("select count(*) from attendance_entry where id = :id"),
                    {"id": attendance_id},
                ),
                "ledger": await conn.scalar(
                    text("select count(*) from shift_ledger_entry where id = :id"),
                    {"id": ledger_id},
                ),
                "deleted_log": await conn.scalar(
                    text(
                        """
                        select result ->> 'deleted_employees'
                          from agent_run
                         where agent_name = 'taxonomy_cleanup_non_canonical_employees'
                         order by started_at desc
                         limit 1
                        """
                    )
                ),
            }
    finally:
        await engine.dispose()
        command.downgrade(alembic_cfg, "base")

    assert counts == {
        "employee": 0,
        "assignment": 0,
        "attendance": 0,
        "ledger": 0,
        "deleted_log": "1",
    }


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
                        "insert into employee (id, full_name, iiko_id, position, status) "
                        "values (:id, :full_name, :iiko_id, 'Повар', 'active')"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "full_name": "First Employee",
                        "iiko_id": duplicate_iiko_id,
                    },
                )
                await conn.execute(
                    text(
                        "insert into employee (id, full_name, iiko_id, position, status) "
                        "values (:id, :full_name, :iiko_id, 'Повар', 'active')"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "full_name": "Second Employee",
                        "iiko_id": duplicate_iiko_id,
                    },
                )
    finally:
        await engine.dispose()

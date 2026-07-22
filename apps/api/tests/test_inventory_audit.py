from __future__ import annotations

import importlib.util
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.deps import CurrentActor
from app.api.v1.routes import inventory as inventory_routes
from app.models import (
    AgentAction,
    AgentRun,
    AttendanceEntry,
    Employee,
    EmployeePositionAssignment,
    InventoryAuditItemExclusion,
    InventoryAuditPosition,
    PayrollAdjustment,
    PayrollAdjustmentCategory,
    PayrollPeriod,
    ScheduledShift,
    ShiftLedgerEntry,
    ShiftSchedule,
)
from app.schemas.inventory import (
    InventoryAuditItemAdjustmentPatch,
    InventoryAuditItemExclusionPatch,
    InventoryPositionPatch,
)
from app.services import iiko_inventory, inventory_audit_service
from app.services.inventory_audit_service import (
    DEFAULT_SETTINGS,
    InventoryAuditConflictError,
    adjustment_comment,
    adjustment_comment_for_computation,
    audit_penalty_work_date,
    audit_period,
    build_penalty_computation,
    group_penalty_rate,
    lookup_rate,
    sync_positions_from_iiko,
)
from app.services.payroll_calculator import (
    PAYROLL_ADJUSTMENTS_CONFIG_KEY,
    calculate_payroll_lines_from_inputs,
)


def test_seed_whitelist_has_41_positions() -> None:
    migration_path = Path(__file__).parents[1] / "alembic/versions/0040_inventory_audit.py"
    spec = importlib.util.spec_from_file_location("inventory_migration", migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert len(module.SEED_POSITIONS) == 41


@pytest.mark.parametrize(
    ("shortage_sum", "expected_rate"),
    [
        (Decimal("4999.99"), Decimal("0")),
        (Decimal("5000"), Decimal("0.40")),
        (Decimal("9999.99"), Decimal("0.40")),
        (Decimal("10000"), Decimal("0.50")),
    ],
)
def test_lookup_rate_uses_chefs_thresholds(
    shortage_sum: Decimal,
    expected_rate: Decimal,
) -> None:
    assert lookup_rate(shortage_sum) == expected_rate


@pytest.mark.parametrize(
    ("shortage_sum", "expected_rate", "expected_reason"),
    [
        (Decimal("4999.99"), Decimal("0"), "below_threshold"),
        (Decimal("5000"), Decimal("0.40"), "tier_5k_10k"),
        (Decimal("9999.99"), Decimal("0.40"), "tier_5k_10k"),
        (Decimal("10000"), Decimal("0.50"), "tier_10k_plus"),
    ],
)
def test_group_penalty_rate_chefs_thresholds(
    shortage_sum: Decimal,
    expected_rate: Decimal,
    expected_reason: str,
) -> None:
    assert group_penalty_rate("chefs", shortage_sum) == (expected_rate, expected_reason)


@pytest.mark.parametrize("group", ["common", "admins"])
@pytest.mark.parametrize(
    "shortage_sum",
    [
        Decimal("4999.99"),
        Decimal("5000"),
        Decimal("9999.99"),
        Decimal("10000"),
    ],
)
def test_group_penalty_rate_common_and_admins_full_100pct(
    group: str,
    shortage_sum: Decimal,
) -> None:
    assert group_penalty_rate(group, shortage_sum) == (Decimal("1.00"), f"{group}_rate")


def test_chefs_below_5000_zero_rate() -> None:
    computation = compute([item("3000", "chefs")], chefs=[employee("Cook")])

    assert computation.groups["chefs"]["rate"] == "0.00"
    assert computation.groups["chefs"]["rate_reason"] == "below_threshold"
    assert computation.groups["chefs"]["penalty"] == "0.00"
    assert computation.employee_penalties == {}


def test_chefs_5000_10000_40pct() -> None:
    computation = compute([item("7000", "chefs")], chefs=[employee("Cook")])

    assert computation.groups["chefs"]["group"] == "chefs"
    assert computation.groups["chefs"]["total_shortage"] == "7000.00"
    assert computation.groups["chefs"]["rate"] == "0.40"
    assert computation.groups["chefs"]["rate_reason"] == "tier_5k_10k"
    assert computation.groups["chefs"]["penalty"] == "2800.00"
    assert computation.snapshot["groups"]["chefs"]["rate_reason"] == "tier_5k_10k"
    assert list(computation.employee_penalties.values()) == [Decimal("2800.00")]


def test_chefs_10000_plus_50pct() -> None:
    computation = compute([item("15000", "chefs")], chefs=[employee("Cook")])

    assert computation.groups["chefs"]["rate"] == "0.50"
    assert computation.groups["chefs"]["rate_reason"] == "tier_10k_plus"
    assert computation.groups["chefs"]["penalty"] == "7500.00"


@pytest.mark.asyncio
async def test_swap_group_validation_rejects_mixed_allocation() -> None:
    position = position_for("chefs", swap_group="salmon")
    conflicting = position_for("admins", swap_group="salmon")
    session = InventoryRouteSession(position, existing=[conflicting])

    with pytest.raises(HTTPException) as exc_info:
        await inventory_routes.patch_position(
            position.id,
            InventoryPositionPatch(swap_group="salmon"),
            session,  # type: ignore[arg-type]
            finance_actor(),
        )

    assert exc_info.value.status_code == 400
    assert 'Группа пересорта "salmon" содержит позиции с разной аллокацией' in str(
        exc_info.value.detail
    )


def test_swap_group_offsets_shortage_within_group() -> None:
    computation = compute(
        [
            swap_item("-10542", "chefs", "salmon", "Сёмга ХК"),
            swap_item("8895", "chefs", "salmon", "Сёмга СС"),
        ],
        chefs=[employee("Cook")],
    )

    assert computation.swap_groups[0]["net_amount"] == "-1647.00"
    assert computation.groups["chefs"]["total_shortage"] == "1647.00"
    assert computation.groups["chefs"]["rate_reason"] == "below_threshold"
    assert computation.groups["chefs"]["penalty"] == "0.00"
    assert computation.employee_penalties == {}


def test_swap_group_net_positive_no_penalty() -> None:
    computation = compute(
        [
            swap_item("-1000", "chefs", "salmon", "Сёмга ХК"),
            swap_item("2245", "chefs", "salmon", "Сёмга СС"),
        ],
        chefs=[employee("Cook")],
    )

    assert computation.swap_groups[0]["net_amount"] == "1245.00"
    assert computation.swap_groups[0]["is_covered"] is True
    assert computation.groups["chefs"]["total_shortage"] == "0.00"
    assert computation.groups["chefs"]["penalty"] == "0.00"


def test_swap_group_override_excludes_item() -> None:
    computation = compute(
        [
            swap_item("-7000", "chefs", "salmon", "Сёмга ХК", override=""),
            swap_item("6000", "chefs", "salmon", "Сёмга СС"),
        ],
        chefs=[employee("Cook")],
    )

    assert computation.swap_groups[0]["net_amount"] == "6000.00"
    assert computation.groups["chefs"]["total_shortage"] == "7000.00"
    assert computation.groups["chefs"]["penalty"] == "2800.00"


def test_adjustment_comment_excludes_swap_group_noise() -> None:
    # Расчётный лист показывает только «Недостача по ревизии <дата>»: построчная
    # детализация пересортов/номенклатуры в комментарий сотрудника не попадает
    # (она остаётся в админ-экране ревизии, см. swap_group_comment_line).
    computation = compute(
        [
            swap_item("-10542", "chefs", "salmon", "Сёмга ХК"),
            swap_item("8895", "chefs", "salmon", "Сёмга СС"),
        ],
        chefs=[employee("Cook")],
    )

    comment = adjustment_comment_for_computation(date(2026, 5, 26), computation)

    assert comment == "Недостача по ревизии 2026-05-26"
    assert "Пересорт" not in comment
    assert "Сёмга" not in comment


def test_common_full_100pct_low_sum() -> None:
    chef = employee("Cook", "Повар")
    admin = employee("Admin", "Кассир")
    computation = compute([item("1000", "common")], chefs=[chef], admins=[admin])

    assert computation.groups["common"]["rate"] == "1.00"
    assert computation.groups["common"]["rate_reason"] == "common_rate"
    assert computation.groups["common"]["penalty"] == "1000.00"
    assert computation.employee_penalties[chef.id] == Decimal("500.00")
    assert computation.employee_penalties[admin.id] == Decimal("500.00")


def test_common_full_100pct_high_sum() -> None:
    chef = employee("Cook", "Повар")
    admin = employee("Admin", "Кассир")
    computation = compute([item("20000", "common")], chefs=[chef], admins=[admin])

    assert computation.groups["common"]["rate"] == "1.00"
    assert computation.groups["common"]["penalty"] == "20000.00"
    assert computation.employee_penalties[chef.id] == Decimal("10000.00")
    assert computation.employee_penalties[admin.id] == Decimal("10000.00")


def test_admins_full_100pct_low_sum() -> None:
    chef = employee("Cook", "Повар")
    admin = employee("Admin", "Кассир")
    computation = compute([item("500", "admins")], chefs=[chef], admins=[admin])

    assert computation.groups["admins"]["rate"] == "1.00"
    assert computation.groups["admins"]["rate_reason"] == "admins_rate"
    assert computation.groups["admins"]["penalty"] == "500.00"
    assert computation.employee_penalties == {admin.id: Decimal("500.00")}


def test_admins_full_100pct_high_sum() -> None:
    chef = employee("Cook", "Повар")
    admin = employee("Admin", "Кассир")
    computation = compute([item("15000", "admins")], chefs=[chef], admins=[admin])

    assert computation.groups["admins"]["rate"] == "1.00"
    assert computation.groups["admins"]["penalty"] == "15000.00"
    assert computation.employee_penalties == {admin.id: Decimal("15000.00")}


def test_compute_independent_thresholds_per_group() -> None:
    chef = employee("Cook", "Повар")
    admin = employee("Admin", "Кассир")
    computation = compute(
        [item("12000", "chefs"), item("4000", "admins")],
        chefs=[chef],
        admins=[admin],
    )

    assert computation.groups["chefs"]["penalty"] == "6000.00"
    assert computation.groups["admins"]["penalty"] == "4000.00"
    assert computation.employee_penalties == {
        chef.id: Decimal("6000.00"),
        admin.id: Decimal("4000.00"),
    }


def test_period_employees_from_shift_ledger() -> None:
    worked = employee("Worked Cook", "Повар")
    not_worked = employee("No Shift Cook", "Повар")
    computation = compute([item("7000", "chefs")], chefs=[worked])

    assert worked.id in computation.employee_penalties
    assert not_worked.id not in computation.employee_penalties


def test_apply_creates_payroll_adjustments() -> None:
    chefs = [employee(f"Cook {index}", "Повар") for index in range(5)]
    computation = compute([item("7000", "chefs")], chefs=chefs)

    adjustments = [
        {
            "employee_id": employee_id,
            "amount": amount,
            "comment": adjustment_comment(date(2026, 5, 26)),
        }
        for employee_id, amount in computation.employee_penalties.items()
    ]

    assert len(adjustments) == 5
    assert {row["amount"] for row in adjustments} == {Decimal("560.00")}
    assert {row["comment"] for row in adjustments} == {"Недостача по ревизии 2026-05-26"}


def test_employee_exclusion_redistributes_penalty_to_remaining_recipients() -> None:
    alpha = employee("Alpha")
    beta = employee("Beta")
    gamma = employee("Gamma")

    computation = compute(
        [item("7000", "chefs")],
        chefs=[alpha, beta, gamma],
        employee_exclusions={beta.id: "Не работал с инвентаризацией"},
    )

    assert computation.employee_penalties == {
        alpha.id: Decimal("1400.00"),
        gamma.id: Decimal("1400.00"),
    }
    assert computation.groups["chefs"]["recipients"]["chefs"]["count"] == 2
    recipients = {row["full_name"]: row for row in computation.snapshot["employee_recipients"]}
    assert recipients["Beta"]["is_excluded"] is True
    assert recipients["Beta"]["amount"] == "0.00"
    assert recipients["Beta"]["exclusion_reason"] == "Не работал с инвентаризацией"


def test_prepaid_revision_charge_reduces_pool_and_excludes_payer() -> None:
    leaving = employee("Leaving Cook")
    alpha = employee("Alpha")
    beta = employee("Beta")

    computation = compute(
        [item("15000", "chefs")],
        chefs=[leaving, alpha, beta],
        prepaid_charges=[
            {
                "employee_id": leaving.id,
                "amount": Decimal("500.00"),
                "work_date": date(2026, 5, 26),
                "comment": "Ручной выкуп ревизии",
            }
        ],
    )

    assert computation.groups["chefs"]["gross_penalty"] == "7500.00"
    assert computation.groups["chefs"]["prepaid"] == "500.00"
    assert computation.groups["chefs"]["penalty"] == "7000.00"
    assert computation.employee_penalties == {
        alpha.id: Decimal("3500.00"),
        beta.id: Decimal("3500.00"),
    }
    assert leaving.id not in computation.employee_penalties
    recipients = computation.groups["chefs"]["recipients"]["chefs"]["items"]
    assert {row["full_name"] for row in recipients} == {"Alpha", "Beta"}
    assert computation.snapshot["prepaid_revision_charges"] == [
        {
            "employee_id": str(leaving.id),
            "full_name": "Leaving Cook",
            "position": "Повар",
            "amount": "500.00",
            "work_date": "2026-05-26",
            "group": "chefs",
        }
    ]


def test_prepaid_revision_charge_clamps_pool_to_zero() -> None:
    leaving = employee("Leaving Cook")

    computation = compute(
        [item("15000", "chefs")],
        chefs=[leaving],
        prepaid_charges=[
            {
                "employee_id": leaving.id,
                "amount": Decimal("8000.00"),
                "work_date": date(2026, 5, 26),
                "comment": "Ручной выкуп ревизии",
            }
        ],
    )

    assert computation.groups["chefs"]["gross_penalty"] == "7500.00"
    assert computation.groups["chefs"]["prepaid"] == "8000.00"
    assert computation.groups["chefs"]["penalty"] == "0.00"
    assert computation.employee_penalties == {}
    assert computation.groups["chefs"]["recipients"] == {}
    assert computation.warnings == []


def test_prepaid_revision_charge_uses_period_role_membership() -> None:
    chef = employee("Cook", "Повар")
    leaving_admin = employee("Leaving Cashier", "Кассир")
    active_admin = employee("Active Cashier", "Кассир")

    computation = compute(
        [item("15000", "chefs"), item("1000", "admins")],
        chefs=[chef],
        admins=[leaving_admin, active_admin],
        prepaid_charges=[
            {
                "employee_id": leaving_admin.id,
                "amount": Decimal("250.00"),
                "work_date": date(2026, 5, 26),
                "comment": "Ручной выкуп ревизии",
            }
        ],
    )

    assert computation.groups["chefs"]["gross_penalty"] == "7500.00"
    assert computation.groups["chefs"]["prepaid"] == "0.00"
    assert computation.groups["chefs"]["penalty"] == "7500.00"
    assert computation.groups["admins"]["gross_penalty"] == "1000.00"
    assert computation.groups["admins"]["prepaid"] == "250.00"
    assert computation.groups["admins"]["penalty"] == "750.00"
    assert computation.employee_penalties == {
        chef.id: Decimal("7500.00"),
        active_admin.id: Decimal("750.00"),
    }
    assert computation.snapshot["prepaid_revision_charges"][0]["group"] == "admins"


def test_prepaid_revision_charge_from_former_cook_reduces_pool() -> None:
    former_cook_id = uuid.uuid4()
    alpha = employee("Alpha")
    beta = employee("Beta")

    computation = compute(
        [item("15000", "chefs")],
        chefs=[alpha, beta],
        prepaid_charges=[
            {
                "employee_id": former_cook_id,
                "full_name": "Валерий Кудря",
                "position": "Повар",
                "amount": Decimal("700.00"),
                "work_date": date(2026, 6, 23),
                "comment": "удержание след ревизия",
            }
        ],
    )

    assert computation.groups["chefs"]["gross_penalty"] == "7500.00"
    assert computation.groups["chefs"]["prepaid"] == "700.00"
    assert computation.groups["chefs"]["penalty"] == "6800.00"
    assert computation.employee_penalties == {
        alpha.id: Decimal("3400.00"),
        beta.id: Decimal("3400.00"),
    }
    assert computation.snapshot["prepaid_revision_charges"] == [
        {
            "employee_id": str(former_cook_id),
            "full_name": "Валерий Кудря",
            "position": "Повар",
            "amount": "700.00",
            "work_date": "2026-06-23",
            "group": "chefs",
        }
    ]
    assert computation.warnings == []


def test_build_penalty_computation_prepaid_default_none_keeps_previous_behavior() -> None:
    cook = employee("Cook")

    computation = build_penalty_computation(
        audit=audit_obj(),
        items=[item("7000", "chefs")],
        settings=None,
        chefs=[cook],
        admins=[],
    )

    assert computation.groups["chefs"]["penalty"] == "2800.00"
    assert computation.employee_penalties == {cook.id: Decimal("2800.00")}
    assert computation.snapshot["prepaid_revision_charges"] == []


@pytest.mark.asyncio
async def test_compute_penalties_matches_prepaid_window_audit_start_to_payout_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Окно матчинга ручных изъятий = [начало периода ревизии .. конец выплаты].

    Ловит штраф, поставленный уволенному в финалку на выплату РАНЬШЕ (его дата
    внутри периода ревизии, до начала выплатного периода) — прежнее окно
    [начало выплаты .. конец выплаты] его пропускало.
    """
    business_date = date(2026, 6, 15)  # понедельник-ревизия
    audit = SimpleNamespace(
        id=uuid.uuid4(),
        business_date=business_date,
        previous_audit_date=date(2026, 6, 8),
        penalty_work_date_override=None,
        items=[],
        employee_exclusions=[],
        item_exclusions=[],
    )
    captured: dict[str, date] = {}

    async def fake_load_audit(_session: Any, _audit_id: uuid.UUID) -> SimpleNamespace:
        return audit

    async def fake_settings(_session: Any) -> dict[str, Decimal]:
        return {}

    async def fake_load_period_employees(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    async def fake_load_prepaid(
        _session: Any, *, period_start: date, period_end: date
    ) -> list[dict]:
        captured["period_start"] = period_start
        captured["period_end"] = period_end
        return []

    monkeypatch.setattr(inventory_audit_service, "_load_audit_or_404", fake_load_audit)
    monkeypatch.setattr(inventory_audit_service, "_inventory_settings", fake_settings)
    monkeypatch.setattr(
        inventory_audit_service, "_load_period_employees", fake_load_period_employees
    )
    monkeypatch.setattr(
        inventory_audit_service, "_load_prepaid_revision_charges", fake_load_prepaid
    )

    class _FlushSession:
        async def flush(self) -> None: ...

        async def commit(self) -> None: ...

    await inventory_audit_service._compute_penalties(
        _FlushSession(),  # type: ignore[arg-type]
        audit.id,
        commit=False,
    )

    ap_start, _ap_end = inventory_audit_service.audit_period(audit)
    pp_start, pp_end, _payout = inventory_audit_service.payroll_period_for_date(
        inventory_audit_service.effective_penalty_work_date(audit)
    )
    assert ap_start < pp_start  # окно реально шире прежнего [выплата..выплата]
    assert captured["period_start"] == ap_start
    assert captured["period_end"] == pp_end


@pytest.mark.asyncio
async def test_load_prepaid_revision_charges_excludes_auto_comment_in_manual_category(
    async_session_factory: Any,
) -> None:
    """Авто-штраф ревизии (comment с префиксом) не считается ручным изъятием,
    даже если попал в ручную категорию audit_penalty."""
    async with async_session_factory() as session:
        employee_row = Employee(
            id=uuid.uuid4(),
            full_name="Auto Comment Cook",
            iiko_id=f"iiko-{uuid.uuid4()}",
            category="category_2",
            status="inactive",
            fire_date=date(2026, 6, 15),
            is_senior=False,
            is_deputy_senior=False,
            pin_hash="hashed-pin",
            pin_set_at=datetime(2026, 5, 1, tzinfo=UTC),
            created_at=datetime(2026, 5, 1, tzinfo=UTC),
            updated_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        session.add(employee_row)
        session.add(
            EmployeePositionAssignment(
                id=uuid.uuid4(),
                employee_id=employee_row.id,
                position="Повар",
                effective_from=date(2026, 1, 1),
                effective_to=None,
                comment="test",
            )
        )
        manual_category = await session.scalar(
            select(PayrollAdjustmentCategory).where(
                PayrollAdjustmentCategory.code
                == inventory_audit_service.MANUAL_INVENTORY_ADJUSTMENT_CATEGORY_CODE
            )
        )
        if manual_category is None:
            manual_category = PayrollAdjustmentCategory(
                id=uuid.uuid4(),
                type="penalty",
                code=inventory_audit_service.MANUAL_INVENTORY_ADJUSTMENT_CATEGORY_CODE,
                display_name="Штраф по ревизии",
                is_active=True,
                sort_order=45,
            )
            session.add(manual_category)
            await session.flush()
        session.add_all(
            [
                PayrollAdjustment(
                    id=uuid.uuid4(),
                    employee_id=employee_row.id,
                    work_date=date(2026, 6, 12),
                    type="penalty",
                    category_id=manual_category.id,
                    amount=Decimal("500.00"),
                    comment="Ручной выкуп ревизии",
                ),
                PayrollAdjustment(
                    id=uuid.uuid4(),
                    employee_id=employee_row.id,
                    work_date=date(2026, 6, 12),
                    type="penalty",
                    category_id=manual_category.id,
                    amount=Decimal("700.00"),
                    comment=(
                        f"{inventory_audit_service.INVENTORY_ADJUSTMENT_COMMENT_PREFIX}"
                        " 2026-06-10"
                    ),
                ),
            ]
        )
        await session.flush()

        rows = await inventory_audit_service._load_prepaid_revision_charges(
            session,
            period_start=date(2026, 6, 9),
            period_end=date(2026, 6, 22),
        )

    assert [(row["amount"], row["comment"]) for row in rows] == [
        (Decimal("500.00"), "Ручной выкуп ревизии")
    ]


@pytest.mark.asyncio
async def test_load_prepaid_revision_charges_uses_manual_revision_category(
    async_session_factory: Any,
) -> None:
    async with async_session_factory() as session:
        employee_row = Employee(
            id=uuid.uuid4(),
            full_name="Manual Cook",
            iiko_id=f"iiko-{uuid.uuid4()}",
            category="category_2",
            status="active",
            is_senior=False,
            is_deputy_senior=False,
            pin_hash="hashed-pin",
            pin_set_at=datetime(2026, 5, 1, tzinfo=UTC),
            created_at=datetime(2026, 5, 1, tzinfo=UTC),
            updated_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        session.add(employee_row)
        session.add(
            EmployeePositionAssignment(
                id=uuid.uuid4(),
                employee_id=employee_row.id,
                position="Повар",
                effective_from=date(2026, 5, 1),
                effective_to=None,
                comment="test",
            )
        )

        async def category_for(
            code: str,
            display_name: str,
            *,
            sort_order: int,
        ) -> PayrollAdjustmentCategory:
            category = await session.scalar(
                select(PayrollAdjustmentCategory).where(PayrollAdjustmentCategory.code == code)
            )
            if category is not None:
                return category
            category = PayrollAdjustmentCategory(
                id=uuid.uuid4(),
                type="penalty",
                code=code,
                display_name=display_name,
                is_active=True,
                sort_order=sort_order,
            )
            session.add(category)
            await session.flush()
            return category

        manual_category = await category_for(
            "audit_penalty",
            "Штраф по ревизии",
            sort_order=45,
        )
        inventory_category = await inventory_audit_service._get_or_create_inventory_category(
            session
        )
        deferred_category = await category_for(
            "audit_deferred",
            "Распределённый штраф ревизии",
            sort_order=55,
        )
        session.add_all(
            [
                PayrollAdjustment(
                    id=uuid.uuid4(),
                    employee_id=employee_row.id,
                    work_date=date(2026, 6, 15),
                    type="penalty",
                    category_id=manual_category.id,
                    amount=Decimal("500.00"),
                    comment="Ручной выкуп ревизии",
                ),
                PayrollAdjustment(
                    id=uuid.uuid4(),
                    employee_id=employee_row.id,
                    work_date=date(2026, 6, 15),
                    type="penalty",
                    category_id=inventory_category.id,
                    amount=Decimal("700.00"),
                    comment="Ручная строка в системной категории",
                ),
                PayrollAdjustment(
                    id=uuid.uuid4(),
                    employee_id=employee_row.id,
                    work_date=date(2026, 6, 15),
                    type="penalty",
                    category_id=deferred_category.id,
                    amount=Decimal("900.00"),
                    comment="Распределённый штраф ревизии 2026-06-08, доля 1/4",
                ),
                PayrollAdjustment(
                    id=uuid.uuid4(),
                    employee_id=employee_row.id,
                    work_date=date(2026, 6, 22),
                    type="penalty",
                    category_id=manual_category.id,
                    amount=Decimal("300.00"),
                    comment="Ручной выкуп вне периода",
                ),
            ]
        )
        await session.flush()

        rows = await inventory_audit_service._load_prepaid_revision_charges(
            session,
            period_start=date(2026, 6, 9),
            period_end=date(2026, 6, 15),
        )

    assert rows == [
        {
            "employee_id": employee_row.id,
            "amount": Decimal("500.00"),
            "work_date": date(2026, 6, 15),
            "comment": "Ручной выкуп ревизии",
            "full_name": "Manual Cook",
            "position": "Повар",
            "group": "chefs",
        }
    ]


@pytest.mark.asyncio
async def test_load_period_employees_gated_by_status_and_worked_shift(
    async_session_factory: Any,
) -> None:
    """В ревизию попадает тот, кто ФАКТИЧЕСКИ отработал смену в периоде и не уволен.

    - active + табель в периоде → внутри (обычный участник).
    - dismissing («в увольнении») + табель в периоде → внутри (расчёт ещё открыт).
    - inactive (уволен, рассчитан) + табель в периоде → снаружи (иначе ложная
      дебиторка перед уволенным).
    - active, но только плановый график без табеля → снаружи («смена стоит» ≠
      «работал»).
    - active без смен в периоде → снаружи.
    """
    period_start = date(2026, 6, 9)
    period_end = date(2026, 6, 15)

    def _cook(full_name: str, *, status: str, fire_date: date | None = None) -> Employee:
        return Employee(
            id=uuid.uuid4(),
            full_name=full_name,
            iiko_id=f"iiko-{uuid.uuid4()}",
            category="category_2",
            status=status,
            fire_date=fire_date,
            is_senior=False,
            is_deputy_senior=False,
            pin_hash="hashed-pin",
            pin_set_at=datetime(2026, 5, 1, tzinfo=UTC),
            created_at=datetime(2026, 5, 1, tzinfo=UTC),
            updated_at=datetime(2026, 5, 1, tzinfo=UTC),
        )

    def _worked_shift(employee_id: uuid.UUID, work_date: date) -> ShiftLedgerEntry:
        return ShiftLedgerEntry(
            id=uuid.uuid4(),
            employee_id=employee_id,
            work_date=work_date,
            source="fallback_primary",
            opened_at=datetime(work_date.year, work_date.month, work_date.day, 8, 0, tzinfo=UTC),
            closed_at=datetime(work_date.year, work_date.month, work_date.day, 17, 0, tzinfo=UTC),
        )

    async with async_session_factory() as session:
        active_worked = _cook("Active Worked Cook", status="active")
        dismissing_worked = _cook(
            "Dismissing Worked Cook", status="dismissing", fire_date=period_end
        )
        inactive_worked = _cook("Inactive Worked Cook", status="inactive", fire_date=period_end)
        active_scheduled_only = _cook("Active Scheduled Only Cook", status="active")
        active_no_shift = _cook("Active No Shift Cook", status="active")

        cooks = [
            active_worked,
            dismissing_worked,
            inactive_worked,
            active_scheduled_only,
            active_no_shift,
        ]
        session.add_all(cooks)
        for cook in cooks:
            session.add(
                EmployeePositionAssignment(
                    id=uuid.uuid4(),
                    employee_id=cook.id,
                    position="Повар",
                    effective_from=date(2026, 1, 1),
                    effective_to=None,
                    comment="test",
                )
            )
        # Табель (факт): три повара отработали смену внутри периода; у active_no_shift
        # факт вне периода.
        session.add_all(
            [
                _worked_shift(active_worked.id, date(2026, 6, 10)),
                _worked_shift(dismissing_worked.id, date(2026, 6, 10)),
                _worked_shift(inactive_worked.id, date(2026, 6, 10)),
                _worked_shift(active_no_shift.id, date(2026, 6, 1)),
            ]
        )
        # У active_scheduled_only — только плановый график в периоде, табеля нет.
        schedule = ShiftSchedule(
            id=uuid.uuid4(),
            date_start=period_start,
            date_end=period_end,
            status="published",
        )
        session.add(schedule)
        session.add(
            ScheduledShift(
                id=uuid.uuid4(),
                shift_schedule_id=schedule.id,
                employee_id=active_scheduled_only.id,
                business_date=date(2026, 6, 12),
                payroll_role="Повар",
                planned_start_at=datetime(2026, 6, 12, 8, 0, tzinfo=UTC),
                planned_end_at=datetime(2026, 6, 12, 17, 0, tzinfo=UTC),
            )
        )
        await session.flush()

        recipients = await inventory_audit_service._load_period_employees(
            session,
            position="Повар",
            period_start=period_start,
            period_end=period_end,
        )
        recipient_ids = {employee.id for employee in recipients}

    assert active_worked.id in recipient_ids
    assert dismissing_worked.id in recipient_ids
    assert inactive_worked.id not in recipient_ids
    assert active_scheduled_only.id not in recipient_ids
    assert active_no_shift.id not in recipient_ids


@pytest.mark.asyncio
async def test_set_item_exclusion_requires_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    audit = audit_detail_obj([detail_item("7000", "chefs", active=True)])
    item_row = audit.items[0]

    async def fake_load_audit(_session: Any, _audit_id: uuid.UUID) -> SimpleNamespace:
        return audit

    monkeypatch.setattr(inventory_audit_service, "_load_audit_or_404", fake_load_audit)

    with pytest.raises(HTTPException) as exc_info:
        await inventory_routes.patch_audit_item_exclusion(
            audit.id,
            item_row.id,
            InventoryAuditItemExclusionPatch(excluded=True),
            object(),  # type: ignore[arg-type]
            finance_actor(),
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "Укажите причину исключения позиции"


@pytest.mark.asyncio
@pytest.mark.parametrize("audit_status", ["applied", "cancelled"])
async def test_set_item_exclusion_only_draft(
    monkeypatch: pytest.MonkeyPatch,
    audit_status: str,
) -> None:
    audit = audit_detail_obj([detail_item("7000", "chefs", active=True)])
    audit.status = audit_status
    item_row = audit.items[0]

    async def fake_load_audit(_session: Any, _audit_id: uuid.UUID) -> SimpleNamespace:
        return audit

    monkeypatch.setattr(inventory_audit_service, "_load_audit_or_404", fake_load_audit)

    with pytest.raises(HTTPException) as exc_info:
        await inventory_routes.patch_audit_item_exclusion(
            audit.id,
            item_row.id,
            InventoryAuditItemExclusionPatch(excluded=True, reason="Не участвует"),
            object(),  # type: ignore[arg-type]
            finance_actor(),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Исключать позиции можно только в черновике"


@pytest.mark.asyncio
async def test_set_item_exclusion_excludes_from_penalty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    excluded_item = detail_item("7000", "chefs", active=True, name="Лосось")
    remaining_item = detail_item("3000", "admins", active=True, name="Упаковка")
    audit = audit_detail_obj([excluded_item, remaining_item])
    session = ItemExclusionServiceSession()
    install_item_exclusion_fakes(monkeypatch, audit)

    updated = await inventory_audit_service.set_item_exclusion(
        session,  # type: ignore[arg-type]
        audit.id,
        excluded_item.id,
        excluded=True,
        reason="Ошибка учета",
        actor=finance_actor(),
    )

    assert updated.total_shortage_amount == Decimal("3000.00")
    assert updated.total_penalty_amount == Decimal("3000.00")
    assert item_ids_in_penalty_groups(updated.computation_snapshot) == {str(remaining_item.id)}
    assert updated.computation_snapshot["skipped_items"] == [
        {
            "item_id": str(excluded_item.id),
            "reason": "excluded",
            "exclusion_reason": "Ошибка учета",
        }
    ]
    payload = inventory_routes.audit_payload(updated, include_items=True)
    excluded_payload = next(row for row in payload["items"] if row["id"] == excluded_item.id)
    assert excluded_payload["is_excluded"] is True
    assert excluded_payload["exclusion_reason"] == "Ошибка учета"
    assert excluded_payload["is_considered"] is False


@pytest.mark.asyncio
async def test_set_item_exclusion_unset_restores(monkeypatch: pytest.MonkeyPatch) -> None:
    excluded_item = detail_item("7000", "chefs", active=True, name="Лосось")
    remaining_item = detail_item("3000", "admins", active=True, name="Упаковка")
    audit = audit_detail_obj([excluded_item, remaining_item])
    session = ItemExclusionServiceSession()
    install_item_exclusion_fakes(monkeypatch, audit)

    await inventory_audit_service.set_item_exclusion(
        session,  # type: ignore[arg-type]
        audit.id,
        excluded_item.id,
        excluded=True,
        reason="Ошибка учета",
        actor=finance_actor(),
    )
    restored = await inventory_audit_service.set_item_exclusion(
        session,  # type: ignore[arg-type]
        audit.id,
        excluded_item.id,
        excluded=False,
        reason=None,
        actor=finance_actor(),
    )

    assert restored.total_shortage_amount == Decimal("10000.00")
    assert item_ids_in_penalty_groups(restored.computation_snapshot) == {
        str(excluded_item.id),
        str(remaining_item.id),
    }
    assert restored.computation_snapshot["skipped_items"] == []
    assert restored.item_exclusions == []
    assert any(isinstance(item, InventoryAuditItemExclusion) for item in session.deleted)


@pytest.mark.asyncio
async def test_set_item_exclusion_audit_log(monkeypatch: pytest.MonkeyPatch) -> None:
    excluded_item = detail_item("7000", "chefs", active=True, name="Лосось")
    audit = audit_detail_obj([excluded_item])
    session = ItemExclusionServiceSession()
    install_item_exclusion_fakes(monkeypatch, audit)

    await inventory_audit_service.set_item_exclusion(
        session,  # type: ignore[arg-type]
        audit.id,
        excluded_item.id,
        excluded=True,
        reason="Ошибка учета",
        actor=finance_actor(),
    )

    actions = [item for item in session.added if isinstance(item, AgentAction)]
    assert len(actions) == 1
    assert actions[0].action_type == "inventory_item_exclusion"
    assert actions[0].target_table == "inventory_audit_item_exclusion"
    assert actions[0].before_value is None
    assert actions[0].after_value == {
        "item_id": str(excluded_item.id),
        "product_name_snapshot": "Лосось",
        "excluded": True,
        "reason": "Ошибка учета",
    }


@pytest.mark.asyncio
async def test_shortage_adjustment_reduces_penalty(monkeypatch: pytest.MonkeyPatch) -> None:
    item = detail_item("3000", "admins", active=True, name="Упаковка")
    audit = audit_detail_obj([item])
    session = ItemExclusionServiceSession()
    install_item_exclusion_fakes(monkeypatch, audit)

    updated = await inventory_audit_service.set_item_shortage_adjustment(
        session,  # type: ignore[arg-type]
        audit.id,
        item.id,
        amount=Decimal("1000"),
        reason="Перенос +1000 с ревизии 19.05",
        actor=finance_actor(),
    )

    # 100% по admins: недостача 3000 → 2000 после корректировки.
    assert updated.total_penalty_amount == Decimal("2000.00")
    payload = inventory_routes.audit_payload(updated, include_items=True)
    row = next(candidate for candidate in payload["items"] if candidate["id"] == item.id)
    assert row["amount_iiko"] == "-3000.00"
    assert row["amount"] == "-2000.00"
    assert row["manual_shortage_adjustment"] == "1000.00"
    assert row["has_manual_adjustment"] is True
    assert row["manual_shortage_adjustment_reason"] == "Перенос +1000 с ревизии 19.05"
    # «Сырой» итог iiko не меняется, учитываемый — отражает корректировку.
    assert payload["total_shortage_iiko"] == "3000.00"
    assert payload["total_shortage_considered"] == "2000.00"


@pytest.mark.asyncio
async def test_shortage_adjustment_requires_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    item = detail_item("3000", "admins", active=True)
    audit = audit_detail_obj([item])
    install_item_exclusion_fakes(monkeypatch, audit)

    with pytest.raises(HTTPException) as exc_info:
        await inventory_routes.patch_audit_item_adjustment(
            audit.id,
            item.id,
            InventoryAuditItemAdjustmentPatch(amount=Decimal("1000")),
            ItemExclusionServiceSession(),  # type: ignore[arg-type]
            finance_actor(),
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "Укажите причину корректировки суммы недостачи"


@pytest.mark.asyncio
async def test_shortage_adjustment_cannot_exceed_shortage(monkeypatch: pytest.MonkeyPatch) -> None:
    item = detail_item("3000", "admins", active=True)
    audit = audit_detail_obj([item])
    install_item_exclusion_fakes(monkeypatch, audit)

    with pytest.raises(HTTPException) as exc_info:
        await inventory_routes.patch_audit_item_adjustment(
            audit.id,
            item.id,
            InventoryAuditItemAdjustmentPatch(amount=Decimal("5000"), reason="слишком много"),
            ItemExclusionServiceSession(),  # type: ignore[arg-type]
            finance_actor(),
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "Корректировка не может превышать исходную недостачу позиции"


@pytest.mark.asyncio
@pytest.mark.parametrize("audit_status", ["applied", "cancelled"])
async def test_shortage_adjustment_only_draft(
    monkeypatch: pytest.MonkeyPatch,
    audit_status: str,
) -> None:
    item = detail_item("3000", "admins", active=True)
    audit = audit_detail_obj([item])
    audit.status = audit_status
    install_item_exclusion_fakes(monkeypatch, audit)

    with pytest.raises(HTTPException) as exc_info:
        await inventory_routes.patch_audit_item_adjustment(
            audit.id,
            item.id,
            InventoryAuditItemAdjustmentPatch(amount=Decimal("1000"), reason="перенос"),
            ItemExclusionServiceSession(),  # type: ignore[arg-type]
            finance_actor(),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Корректировать сумму недостачи можно только в черновике"


@pytest.mark.asyncio
async def test_shortage_adjustment_reset_restores_penalty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = detail_item("3000", "admins", active=True)
    audit = audit_detail_obj([item])
    install_item_exclusion_fakes(monkeypatch, audit)

    await inventory_audit_service.set_item_shortage_adjustment(
        audit_id=audit.id,
        item_id=item.id,
        amount=Decimal("1000"),
        reason="перенос",
        actor=finance_actor(),
        session=ItemExclusionServiceSession(),  # type: ignore[arg-type]
    )
    restored = await inventory_audit_service.set_item_shortage_adjustment(
        ItemExclusionServiceSession(),  # type: ignore[arg-type]
        audit.id,
        item.id,
        amount=None,
        reason=None,
        actor=finance_actor(),
    )

    assert restored.total_penalty_amount == Decimal("3000.00")
    assert getattr(item, "manual_shortage_adjustment", None) is None
    payload = inventory_routes.audit_payload(restored, include_items=True)
    row = next(candidate for candidate in payload["items"] if candidate["id"] == item.id)
    assert row["has_manual_adjustment"] is False
    assert row["amount"] == "-3000.00"


@pytest.mark.asyncio
async def test_shortage_adjustment_audit_log(monkeypatch: pytest.MonkeyPatch) -> None:
    item = detail_item("3000", "admins", active=True, name="Упаковка")
    audit = audit_detail_obj([item])
    session = ItemExclusionServiceSession()
    install_item_exclusion_fakes(monkeypatch, audit)

    await inventory_audit_service.set_item_shortage_adjustment(
        session,  # type: ignore[arg-type]
        audit.id,
        item.id,
        amount=Decimal("1000"),
        reason="перенос с прошлой ревизии",
        actor=finance_actor(),
    )

    actions = [item for item in session.added if isinstance(item, AgentAction)]
    assert len(actions) == 1
    assert actions[0].action_type == "inventory_item_shortage_adjustment"
    assert actions[0].target_table == "inventory_audit_item"
    assert actions[0].after_value["manual_shortage_adjustment"] == "1000.00"
    assert actions[0].after_value["effective_amount"] == "-2000.00"
    assert actions[0].after_value["reason"] == "перенос с прошлой ревизии"


@pytest.mark.asyncio
async def test_carryover_suggestion_from_prior_surplus(monkeypatch: pytest.MonkeyPatch) -> None:
    position_id = uuid.uuid4()
    prior_item = SimpleNamespace(
        position_id=position_id,
        amount=Decimal("1000"),
        shortage_amount=Decimal("0"),
    )
    prior_audit = SimpleNamespace(items=[prior_item])
    current_item = detail_item("1500", "chefs", active=True, name="Креветки")
    current_item.position_id = position_id
    audit = audit_detail_obj([current_item])

    async def fake_prev(_session: Any, _business_date: date) -> date:
        return date(2026, 5, 19)

    class CarryoverSession:
        async def scalar(self, _query: Any) -> SimpleNamespace:
            return prior_audit

    monkeypatch.setattr(inventory_audit_service, "_previous_audit_date", fake_prev)

    suggestions = await inventory_audit_service.carryover_suggestions(
        CarryoverSession(),  # type: ignore[arg-type]
        audit,
    )

    assert current_item.id in suggestions
    suggestion = suggestions[current_item.id]
    assert suggestion["prior_audit_date"] == date(2026, 5, 19)
    assert suggestion["prior_amount"] == "1000.00"
    assert suggestion["current_shortage"] == "1500.00"
    assert suggestion["suggested_reduction"] == "1000.00"


@pytest.mark.asyncio
async def test_apply_creates_adjustment_with_next_day_work_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payroll_employee = employee("Cook", "Повар")
    audit = audit_obj(
        business_date=date(2026, 5, 25),
        previous_audit_date=date(2026, 5, 18),
    )
    audit.status = "draft"
    category = SimpleNamespace(id=uuid.uuid4())
    computation = SimpleNamespace(
        employee_penalties={payroll_employee.id: Decimal("2800.00")},
        total_penalty_amount=Decimal("2800.00"),
    )
    session = ApplyAuditSession()
    locked_dates: list[date] = []

    async def fake_load_audit(_session: Any, _audit_id: uuid.UUID) -> SimpleNamespace:
        return audit

    async def fake_compute_penalties(
        _session: Any,
        _audit_id: uuid.UUID,
        *,
        commit: bool,
    ) -> SimpleNamespace:
        assert commit is False
        return computation

    async def fake_get_category(_session: Any) -> SimpleNamespace:
        return category

    async def fake_assert_date_not_locked(_session: Any, work_date: date) -> None:
        locked_dates.append(work_date)

    async def fake_write_agent_audit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(inventory_audit_service, "_load_audit_or_404", fake_load_audit)
    monkeypatch.setattr(inventory_audit_service, "_compute_penalties", fake_compute_penalties)
    monkeypatch.setattr(
        inventory_audit_service, "_get_or_create_inventory_category", fake_get_category
    )
    monkeypatch.setattr(
        inventory_audit_service, "assert_production_date_not_locked", fake_assert_date_not_locked
    )
    monkeypatch.setattr(inventory_audit_service, "_write_agent_audit", fake_write_agent_audit)

    await inventory_audit_service.apply_audit_penalties(
        session,  # type: ignore[arg-type]
        audit.id,
        actor=finance_actor(),
    )

    adjustments = [row for row in session.added if isinstance(row, PayrollAdjustment)]
    assert locked_dates == [date(2026, 5, 26)]
    assert len(adjustments) == 1
    assert adjustments[0].work_date == date(2026, 5, 26)
    assert adjustments[0].comment == "Недостача по ревизии 2026-05-25"
    assert session.committed is True


@pytest.mark.asyncio
async def test_restore_cancelled_audit_to_draft_clears_application_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = audit_obj()
    audit.status = "cancelled"
    audit.applied_at = datetime(2026, 5, 26, tzinfo=UTC)
    audit.applied_by_user_id = uuid.uuid4()
    session = ApplyAuditSession()
    audit_events: list[dict[str, Any]] = []

    async def fake_load_audit(_session: Any, _audit_id: uuid.UUID) -> SimpleNamespace:
        return audit

    async def fake_write_agent_audit(*_args: Any, **kwargs: Any) -> None:
        audit_events.append(kwargs["after"])

    monkeypatch.setattr(inventory_audit_service, "_load_audit_or_404", fake_load_audit)
    monkeypatch.setattr(inventory_audit_service, "_write_agent_audit", fake_write_agent_audit)

    restored = await inventory_audit_service.restore_cancelled_audit(
        session,  # type: ignore[arg-type]
        audit.id,
        actor=finance_actor(),
    )

    assert restored.status == "draft"
    assert restored.applied_at is None
    assert restored.applied_by_user_id is None
    assert audit_events == [{"from_status": "cancelled", "status": "draft"}]
    assert session.committed is True


@pytest.mark.asyncio
async def test_restore_non_cancelled_audit_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    audit = audit_obj()
    audit.status = "draft"
    session = ApplyAuditSession()

    async def fake_load_audit(_session: Any, _audit_id: uuid.UUID) -> SimpleNamespace:
        return audit

    monkeypatch.setattr(inventory_audit_service, "_load_audit_or_404", fake_load_audit)

    with pytest.raises(InventoryAuditConflictError) as exc_info:
        await inventory_audit_service.restore_cancelled_audit(
            session,  # type: ignore[arg-type]
            audit.id,
            actor=finance_actor(),
        )

    assert str(exc_info.value) == "Вернуть в черновик можно только отменённую ревизию"
    assert session.committed is False


def test_payroll_run_for_period_includes_previous_monday_audit_penalty() -> None:
    period = payroll_period(start=date(2026, 5, 26), end=date(2026, 6, 1))
    run_id = uuid.uuid4()
    payroll_employee = calculator_employee()
    entry = calculator_entry(period, payroll_employee, date(2026, 5, 26))
    adjustment = inventory_adjustment_for_audit(
        payroll_employee,
        audit_business_date=date(2026, 5, 25),
        amount=Decimal("500"),
    )
    settings = calculator_settings()
    settings[PAYROLL_ADJUSTMENTS_CONFIG_KEY] = {
        (payroll_employee.id, adjustment.work_date): [adjustment],
    }

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        [entry],
        {payroll_employee.id: payroll_employee},
        settings,
    )

    assert result.blocking_issues == []
    assert result.lines[0].deduction == Decimal("500.00")
    assert result.lines[0].components["adjustments"]["penalties"][0]["comment"] == (
        "Недостача по ревизии 2026-05-25"
    )


def test_payroll_run_excludes_unrelated_audit() -> None:
    period = payroll_period(start=date(2026, 5, 26), end=date(2026, 6, 1))
    run_id = uuid.uuid4()
    payroll_employee = calculator_employee()
    entry = calculator_entry(period, payroll_employee, date(2026, 5, 26))
    adjustment = inventory_adjustment_for_audit(
        payroll_employee,
        audit_business_date=date(2026, 5, 18),
        amount=Decimal("500"),
    )
    settings = calculator_settings()
    settings[PAYROLL_ADJUSTMENTS_CONFIG_KEY] = {
        (payroll_employee.id, adjustment.work_date): [adjustment],
    }

    result = calculate_payroll_lines_from_inputs(
        period,
        run_id,
        [entry],
        {payroll_employee.id: payroll_employee},
        settings,
    )

    assert result.blocking_issues == []
    assert adjustment.work_date == date(2026, 5, 19)
    assert result.lines[0].deduction == Decimal("0.00")
    assert result.lines[0].components["adjustments"]["penalties"] == []


def test_apply_in_finalized_period_409() -> None:
    with pytest.raises(InventoryAuditConflictError):
        raise InventoryAuditConflictError("Период зафиксирован, изменения невозможны")


def test_effective_penalty_work_date_default_and_override() -> None:
    audit = audit_obj(business_date=date(2026, 6, 8))
    audit.penalty_work_date_override = None
    # по умолчанию — день после ревизии
    assert inventory_audit_service.effective_penalty_work_date(audit) == date(2026, 6, 9)
    # с переносом — берётся override
    audit.penalty_work_date_override = date(2026, 6, 16)
    assert inventory_audit_service.effective_penalty_work_date(audit) == date(2026, 6, 16)


def test_payroll_period_for_date_monday_and_tuesday() -> None:
    # понедельник — последний день периода вторник…понедельник
    start, end, payout = inventory_audit_service.payroll_period_for_date(date(2026, 6, 15))
    assert (start, end, payout) == (date(2026, 6, 9), date(2026, 6, 15), date(2026, 6, 16))
    # вторник — первый день нового периода, выплата через неделю
    start, end, payout = inventory_audit_service.payroll_period_for_date(date(2026, 6, 16))
    assert (start, end, payout) == (date(2026, 6, 16), date(2026, 6, 22), date(2026, 6, 23))


@pytest.mark.asyncio
async def test_apply_uses_penalty_work_date_override(monkeypatch: pytest.MonkeyPatch) -> None:
    payroll_employee = employee("Cook", "Повар")
    audit = audit_obj(business_date=date(2026, 6, 8), previous_audit_date=date(2026, 6, 1))
    audit.status = "draft"
    audit.penalty_work_date_override = date(2026, 6, 16)
    category = SimpleNamespace(id=uuid.uuid4())
    computation = SimpleNamespace(
        employee_penalties={payroll_employee.id: Decimal("2800.00")},
        total_penalty_amount=Decimal("2800.00"),
    )
    session = ApplyAuditSession()
    locked_dates: list[date] = []

    async def fake_load_audit(_session: Any, _audit_id: uuid.UUID) -> SimpleNamespace:
        return audit

    async def fake_compute_penalties(
        _session: Any, _audit_id: uuid.UUID, *, commit: bool
    ) -> SimpleNamespace:
        return computation

    async def fake_get_category(_session: Any) -> SimpleNamespace:
        return category

    async def fake_assert_date_not_locked(_session: Any, work_date: date) -> None:
        locked_dates.append(work_date)

    async def fake_write_agent_audit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(inventory_audit_service, "_load_audit_or_404", fake_load_audit)
    monkeypatch.setattr(inventory_audit_service, "_compute_penalties", fake_compute_penalties)
    monkeypatch.setattr(
        inventory_audit_service, "_get_or_create_inventory_category", fake_get_category
    )
    monkeypatch.setattr(
        inventory_audit_service, "assert_production_date_not_locked", fake_assert_date_not_locked
    )
    monkeypatch.setattr(inventory_audit_service, "_write_agent_audit", fake_write_agent_audit)

    await inventory_audit_service.apply_audit_penalties(
        session,  # type: ignore[arg-type]
        audit.id,
        actor=finance_actor(),
    )

    adjustments = [row for row in session.added if isinstance(row, PayrollAdjustment)]
    # дата списания и проверка блокировки идут по перенесённой дате, а не business_date+1
    assert locked_dates == [date(2026, 6, 16)]
    assert adjustments[0].work_date == date(2026, 6, 16)


@pytest.mark.asyncio
async def test_set_penalty_work_date_override_non_draft_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = audit_obj(business_date=date(2026, 6, 8))
    audit.status = "applied"
    audit.penalty_work_date_override = None

    with pytest.raises(InventoryAuditConflictError) as exc_info:
        await inventory_audit_service.set_penalty_work_date_override(
            None,  # type: ignore[arg-type]
            audit,
            date(2026, 6, 16),
            actor=finance_actor(),
        )

    assert "только в черновике" in str(exc_info.value)
    assert audit.penalty_work_date_override is None


@pytest.mark.asyncio
async def test_set_penalty_work_date_override_locked_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.payroll_adjustment_service import PayrollAdjustmentLockedError

    audit = audit_obj(business_date=date(2026, 6, 8))
    audit.status = "draft"
    audit.penalty_work_date_override = None

    async def fake_assert_locked(_session: Any, _work_date: date) -> None:
        raise PayrollAdjustmentLockedError("Период зафиксирован, изменения невозможны")

    monkeypatch.setattr(
        inventory_audit_service, "assert_production_date_not_locked", fake_assert_locked
    )

    with pytest.raises(InventoryAuditConflictError):
        await inventory_audit_service.set_penalty_work_date_override(
            None,  # type: ignore[arg-type]
            audit,
            date(2026, 6, 16),
            actor=finance_actor(),
        )
    assert audit.penalty_work_date_override is None


@pytest.mark.asyncio
async def test_set_penalty_work_date_override_earlier_than_default_rejected() -> None:
    audit = audit_obj(business_date=date(2026, 6, 8))
    audit.status = "draft"
    audit.penalty_work_date_override = None

    with pytest.raises(inventory_audit_service.InventoryAuditValidationError):
        await inventory_audit_service.set_penalty_work_date_override(
            None,  # type: ignore[arg-type]
            audit,
            date(2026, 6, 8),  # = business_date, раньше расчётной (09.06)
            actor=finance_actor(),
        )


@pytest.mark.asyncio
async def test_set_penalty_work_date_override_sets_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = audit_obj(business_date=date(2026, 6, 8))
    audit.status = "draft"
    audit.penalty_work_date_override = None
    events: list[dict[str, Any]] = []

    async def fake_assert_not_locked(_session: Any, _work_date: date) -> None:
        return None

    async def fake_write_agent_audit(*_args: Any, **kwargs: Any) -> None:
        events.append(kwargs["after"])

    monkeypatch.setattr(
        inventory_audit_service, "assert_production_date_not_locked", fake_assert_not_locked
    )
    monkeypatch.setattr(inventory_audit_service, "_write_agent_audit", fake_write_agent_audit)

    await inventory_audit_service.set_penalty_work_date_override(
        None,  # type: ignore[arg-type]
        audit,
        date(2026, 6, 16),
        actor=finance_actor(),
    )

    assert audit.penalty_work_date_override == date(2026, 6, 16)
    assert events == [{"before": None, "override": "2026-06-16"}]


@pytest.mark.asyncio
async def test_list_payout_options_marks_default_and_locked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = audit_obj(business_date=date(2026, 6, 8))

    async def fake_is_locked(_session: Any, work_date: date) -> bool:
        return work_date == date(2026, 6, 9)  # период по умолчанию закрыт

    monkeypatch.setattr(inventory_audit_service, "is_production_date_locked", fake_is_locked)

    options = await inventory_audit_service.list_payout_options(
        None,  # type: ignore[arg-type]
        audit,
        count=3,
    )

    assert options[0]["is_default"] is True
    assert options[0]["override_value"] is None
    assert options[0]["period_start"] == date(2026, 6, 9)
    assert options[0]["payout_date"] == date(2026, 6, 16)
    assert options[0]["locked"] is True
    assert options[1]["is_default"] is False
    assert options[1]["override_value"] == date(2026, 6, 16)
    assert options[1]["payout_date"] == date(2026, 6, 23)
    assert options[1]["locked"] is False


@pytest.mark.asyncio
async def test_list_open_production_payouts_skips_finalized_weeks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Ничего не закрыто → первая выплата = сегодняшняя (период, заканчивающийся вчера).
    async def none_locked(_session: Any, _work_date: date) -> bool:
        return False

    monkeypatch.setattr(inventory_audit_service, "is_production_date_locked", none_locked)
    options = await inventory_audit_service.list_open_production_payouts(
        None,  # type: ignore[arg-type]
        count=3,
        today=date(2026, 6, 16),
    )
    assert options[0]["period_start"] == date(2026, 6, 9)
    assert options[0]["payout_date"] == date(2026, 6, 16)
    assert options[0]["is_default"] is True
    assert options[1]["payout_date"] == date(2026, 6, 23)

    # Недельный период 09–15 финализирован → первая открытая выплата = 23.06.
    async def week_0915_locked(_session: Any, work_date: date) -> bool:
        return work_date == date(2026, 6, 9)

    monkeypatch.setattr(inventory_audit_service, "is_production_date_locked", week_0915_locked)
    options2 = await inventory_audit_service.list_open_production_payouts(
        None,  # type: ignore[arg-type]
        count=3,
        today=date(2026, 6, 16),
    )
    assert options2[0]["period_start"] == date(2026, 6, 16)
    assert options2[0]["payout_date"] == date(2026, 6, 23)


def test_cancel_removes_adjustments() -> None:
    comment = adjustment_comment(date(2026, 5, 26))
    adjustments = [
        {"comment": comment, "work_date": date(2026, 5, 26)},
        {"comment": "Другая корректировка", "work_date": date(2026, 5, 26)},
    ]

    remaining = [row for row in adjustments if row["comment"] != comment]

    assert remaining == [{"comment": "Другая корректировка", "work_date": date(2026, 5, 26)}]


@pytest.mark.asyncio
async def test_import_from_iiko_404_when_no_document(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch_inventory_document(_business_date: date) -> None:
        return None

    monkeypatch.setattr(iiko_inventory, "fetch_inventory_document", fake_fetch_inventory_document)

    assert await iiko_inventory.fetch_inventory_document(date(2026, 5, 26)) is None


def test_import_from_iiko_maps_by_guid() -> None:
    guid = str(uuid.uuid4())
    position = position_for("chefs", guid=guid)
    positions_by_guid = {position.iiko_product_guid: position}
    raw_item = {"product_guid": guid, "product_name": "Лосось", "shortage_amount": Decimal("1000")}

    mapped_position = positions_by_guid.get(raw_item["product_guid"])

    assert mapped_position is position


def test_import_from_iiko_unmapped_item_position_id_null() -> None:
    positions_by_guid: dict[str, Any] = {}
    raw_item = {"product_guid": str(uuid.uuid4())}

    assert positions_by_guid.get(raw_item["product_guid"]) is None


def test_rounding_remainder_goes_to_first_employee() -> None:
    settings = {**DEFAULT_SETTINGS, "inventory.threshold_zero": Decimal("0")}
    chefs = [employee("Alpha"), employee("Beta"), employee("Gamma")]
    computation = compute([item("2500", "chefs")], chefs=chefs, settings=settings)

    amounts_by_name = {
        row["full_name"]: Decimal(row["amount"]) for row in computation.employee_rows
    }

    assert amounts_by_name == {
        "Alpha": Decimal("333.34"),
        "Beta": Decimal("333.33"),
        "Gamma": Decimal("333.33"),
    }


def test_no_previous_audit_uses_7_day_fallback() -> None:
    audit = audit_obj(previous_audit_date=None)

    assert audit_period(audit) == (date(2026, 5, 19), date(2026, 5, 26))


def test_position_change_active_takes_effect_on_next_compute() -> None:
    active_position = position_for("chefs", active=True)
    inactive_position = position_for("chefs", active=False)
    computation = compute(
        [
            SimpleNamespace(
                id=uuid.uuid4(),
                position=active_position,
                product_name_snapshot="Активная",
                shortage_amount=Decimal("7000"),
            ),
            SimpleNamespace(
                id=uuid.uuid4(),
                position=inactive_position,
                product_name_snapshot="Отключённая",
                shortage_amount=Decimal("7000"),
            ),
        ],
        chefs=[employee("Cook")],
    )

    assert computation.groups["chefs"]["sum"] == "7000.00"
    assert computation.groups["unmapped"]["sum"] == "7000.00"


def test_compute_penalties_skips_positions_with_null_group() -> None:
    position = position_for(None, active=True)
    skipped_item = SimpleNamespace(
        id=uuid.uuid4(),
        position=position,
        product_name_snapshot="Без группы",
        shortage_amount=Decimal("7000"),
    )
    computation = compute([skipped_item], chefs=[employee("Cook")])

    assert computation.groups["chefs"]["sum"] == "0.00"
    assert computation.employee_penalties == {}
    assert computation.snapshot["skipped_items"] == [
        {"item_id": str(skipped_item.id), "reason": "no_group"}
    ]


def test_compute_penalties_skips_unmatched_items() -> None:
    skipped_item = SimpleNamespace(
        id=uuid.uuid4(),
        position=None,
        product_name_snapshot="Нет в whitelist",
        shortage_amount=Decimal("7000"),
    )
    computation = compute([skipped_item], chefs=[employee("Cook")])

    assert computation.groups["chefs"]["sum"] == "0.00"
    assert computation.groups["unmapped"]["sum"] == "7000.00"
    assert computation.snapshot["skipped_items"] == [
        {"item_id": str(skipped_item.id), "reason": "not_in_whitelist"}
    ]


def test_compute_penalties_skips_inactive_positions() -> None:
    inactive_position = position_for("chefs", active=False)
    skipped_item = SimpleNamespace(
        id=uuid.uuid4(),
        position=inactive_position,
        product_name_snapshot="Отключённая",
        shortage_amount=Decimal("7000"),
    )
    computation = compute([skipped_item], chefs=[employee("Cook")])

    assert computation.groups["chefs"]["sum"] == "0.00"
    assert computation.groups["unmapped"]["sum"] == "7000.00"
    assert computation.snapshot["skipped_items"] == [
        {"item_id": str(skipped_item.id), "reason": "inactive"}
    ]


def test_get_audit_filters_inactive_positions() -> None:
    items = [
        detail_item("100", "chefs", active=True, name="Активная 1"),
        detail_item("200", "common", active=True, name="Активная 2"),
        detail_item("300", "admins", active=True, name="Активная 3"),
        *[
            detail_item("50", "chefs", active=False, name=f"Неактивная {index}")
            for index in range(5)
        ],
    ]
    payload = inventory_routes.audit_payload(audit_detail_obj(items), include_items=True)

    assert len(payload["items"]) == 3
    assert [row["product_name_snapshot"] for row in payload["items"]] == [
        "Активная 1",
        "Активная 2",
        "Активная 3",
    ]
    assert all(row["is_considered"] is True for row in payload["items"])


def test_get_audit_filters_positions_without_group() -> None:
    items = [
        detail_item("100", "chefs", active=True, name="Кухня"),
        detail_item("200", None, active=True, name="Без группы"),
    ]
    payload = inventory_routes.audit_payload(audit_detail_obj(items), include_items=True)

    assert len(payload["items"]) == 1
    assert payload["items"][0]["product_name_snapshot"] == "Кухня"
    assert payload["items_skipped_count"] == 1


def test_get_audit_returns_total_iiko_and_considered() -> None:
    items = [
        detail_item("100", "chefs", active=True),
        detail_item("200", "common", active=True),
        detail_item("300", "chefs", active=False),
        detail_item("400", None, active=True),
        detail_item("500", None, active=False, has_position=False),
    ]
    payload = inventory_routes.audit_payload(audit_detail_obj(items), include_items=True)

    assert payload["total_shortage_iiko"] == "1500.00"
    assert payload["total_shortage_considered"] == "300.00"


def test_get_audit_items_skipped_count() -> None:
    items = [
        detail_item("100", "chefs", active=True),
        detail_item("200", "chefs", active=False),
        detail_item("300", None, active=True),
        detail_item("400", None, active=False, has_position=False),
    ]
    payload = inventory_routes.audit_payload(audit_detail_obj(items), include_items=True)

    assert payload["items_skipped_count"] == 3


def test_audit_payload_includes_item_exclusions_log() -> None:
    created_at = datetime(2026, 5, 26, 12, 30, tzinfo=UTC)
    item_row = detail_item("7000", "chefs", active=True, name="Лосось")
    audit = audit_detail_obj([item_row])
    audit.item_exclusions = [
        SimpleNamespace(
            id=uuid.uuid4(),
            item_id=item_row.id,
            item=item_row,
            reason="брак",
            created_by=SimpleNamespace(full_name="Финансист"),
            created_at=created_at,
            updated_at=created_at,
        )
    ]

    payload = inventory_routes.audit_payload(audit, include_items=True)

    assert len(payload["item_exclusions_log"]) == 1
    row = payload["item_exclusions_log"][0]
    assert row["item_id"] == str(item_row.id)
    assert row["product_name"] == "Лосось"
    assert row["amount"] == "-7000.00"
    assert row["reason"] == "брак"
    assert row["created_by_name"] == "Финансист"
    assert row["created_at"] == created_at


def test_audit_payload_includes_employee_exclusions_log() -> None:
    created_at = datetime(2026, 5, 26, 12, 30, tzinfo=UTC)
    employee_row = employee("Повар Иван", "Повар")
    audit = audit_detail_obj([detail_item("7000", "chefs", active=True)])
    audit.employee_exclusions = [
        SimpleNamespace(
            id=uuid.uuid4(),
            employee_id=employee_row.id,
            employee=employee_row,
            reason="не участвовал",
            created_by=SimpleNamespace(full_name="Финансист"),
            created_at=created_at,
            updated_at=created_at,
        )
    ]

    payload = inventory_routes.audit_payload(audit, include_items=True)

    assert len(payload["employee_exclusions_log"]) == 1
    row = payload["employee_exclusions_log"][0]
    assert row["employee_id"] == str(employee_row.id)
    assert row["employee_name"] == "Повар Иван"
    assert row["employee_position"] == "Повар"
    assert row["reason"] == "не участвовал"
    assert row["created_by_name"] == "Финансист"
    assert row["created_at"] == created_at


def test_exclusions_log_empty_when_no_exclusions() -> None:
    payload = inventory_routes.audit_payload(
        audit_detail_obj([detail_item("7000", "chefs", active=True)]),
        include_items=True,
    )

    assert payload["item_exclusions_log"] == []
    assert payload["employee_exclusions_log"] == []


@pytest.mark.asyncio
async def test_list_audit_exclusions_returns_both_sections() -> None:
    item_audit = exclusion_audit(date(2026, 5, 20))
    employee_audit = exclusion_audit(date(2026, 5, 27), status="applied")
    employee_row = employee("Повар Иван", "Повар")
    session = AuditExclusionsSession(
        items=[item_exclusion_row(item_audit, name="Лосось", amount="-7000")],
        employees=[
            employee_exclusion_row(
                employee_audit,
                employee_row,
                reason="не участвовал",
            )
        ],
    )

    payload = await inventory_routes.list_audit_exclusions(
        session,  # type: ignore[arg-type]
        finance_actor(),
    )

    assert len(payload["items"]) == 1
    assert payload["items"][0]["audit_business_date"] == date(2026, 5, 20)
    assert payload["items"][0]["product_name"] == "Лосось"
    assert payload["items"][0]["amount"] == "-7000.00"
    assert len(payload["employees"]) == 1
    assert payload["employees"][0]["audit_business_date"] == date(2026, 5, 27)
    assert payload["employees"][0]["audit_status"] == "applied"
    assert payload["employees"][0]["employee_name"] == "Повар Иван"


@pytest.mark.asyncio
async def test_list_audit_exclusions_filters_by_audit_date() -> None:
    old_audit = exclusion_audit(date(2026, 5, 20))
    matching_audit = exclusion_audit(date(2026, 5, 27))
    old_employee = employee("Старый сотрудник", "Повар")
    matching_employee = employee("Новый сотрудник", "Повар")
    session = AuditExclusionsSession(
        items=[
            item_exclusion_row(old_audit, name="Старый товар"),
            item_exclusion_row(matching_audit, name="Новый товар"),
        ],
        employees=[
            employee_exclusion_row(old_audit, old_employee),
            employee_exclusion_row(matching_audit, matching_employee),
        ],
    )

    payload = await inventory_routes.list_audit_exclusions(
        session,  # type: ignore[arg-type]
        finance_actor(),
        audit_date_from=date(2026, 5, 25),
        audit_date_to=date(2026, 5, 31),
    )

    assert [row["product_name"] for row in payload["items"]] == ["Новый товар"]
    assert [row["employee_name"] for row in payload["employees"]] == ["Новый сотрудник"]
    assert payload["items"][0]["audit_business_date"] == date(2026, 5, 27)
    assert payload["employees"][0]["audit_business_date"] == date(2026, 5, 27)


@pytest.mark.asyncio
async def test_list_audit_exclusions_filters_employees_by_employee_id() -> None:
    audit = exclusion_audit(date(2026, 5, 27))
    alpha = employee("Alpha", "Повар")
    beta = employee("Beta", "Кассир")
    session = AuditExclusionsSession(
        items=[
            item_exclusion_row(audit, name="Лосось"),
            item_exclusion_row(audit, name="Рис"),
        ],
        employees=[
            employee_exclusion_row(audit, alpha),
            employee_exclusion_row(audit, beta),
        ],
    )

    payload = await inventory_routes.list_audit_exclusions(
        session,  # type: ignore[arg-type]
        finance_actor(),
        employee_id=alpha.id,
    )

    assert [row["product_name"] for row in payload["items"]] == ["Лосось", "Рис"]
    assert [row["employee_name"] for row in payload["employees"]] == ["Alpha"]
    assert payload["employees"][0]["employee_id"] == str(alpha.id)


def test_remap_migration_links_old_items_by_guid(monkeypatch: pytest.MonkeyPatch) -> None:
    migration_path = (
        Path(__file__).parents[1] / "alembic/versions/0042_remap_inventory_items_to_positions.py"
    )
    spec = importlib.util.spec_from_file_location("inventory_remap_migration", migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    executed: list[str] = []

    fake_op = SimpleNamespace(execute=lambda sql: executed.append(str(sql)))
    monkeypatch.setattr(module, "op", fake_op)

    module.upgrade()

    sql = " ".join(executed[0].split())
    assert len(module.revision) <= 32
    assert module.down_revision == "0041_inventory_position_rework"
    assert "UPDATE inventory_audit_item ai SET position_id = p.id" in sql
    assert "ai.position_id IS NULL" in sql
    assert "ai.iiko_product_guid IS NOT NULL" in sql
    assert "ai.iiko_product_guid = p.iiko_product_guid" in sql


def test_fix_audit_penalty_work_date_migration(monkeypatch: pytest.MonkeyPatch) -> None:
    migration_path = (
        Path(__file__).parents[1] / "alembic/versions/0043_fix_audit_penalty_work_date.py"
    )
    spec = importlib.util.spec_from_file_location("inventory_work_date_migration", migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    executed: list[str] = []

    fake_op = SimpleNamespace(execute=lambda sql: executed.append(str(sql)))
    monkeypatch.setattr(module, "op", fake_op)

    module.upgrade()

    sql = " ".join(executed[0].split())
    assert module.revision == "0043_fix_audit_penalty_work_date"
    assert module.down_revision == "0042_remap_inv_items_positions"
    assert "SET work_date = pa.work_date + INTERVAL '1 day'" in sql
    assert "cat.code = 'inventory_shortage'" in sql


def test_compute_penalties_matches_total_considered() -> None:
    settings = {**DEFAULT_SETTINGS, "inventory.threshold_zero": Decimal("0")}
    items = [
        detail_item("7000", "chefs", active=True),
        detail_item("9000", "chefs", active=False),
        detail_item("11000", None, active=True),
    ]
    payload = inventory_routes.audit_payload(audit_detail_obj(items), include_items=True)
    computation = compute(items, chefs=[employee("Cook")], settings=settings)
    penalty_sum = sum(
        Decimal(group["penalty"])
        for key, group in computation.groups.items()
        if key in {"chefs", "common", "admins"}
    )

    assert payload["total_shortage_considered"] == "7000.00"
    assert penalty_sum == Decimal(payload["total_shortage_considered"]) * Decimal("0.40")


def test_get_audit_total_considered_uses_swap_group_net_snapshot() -> None:
    shortage = detail_item("10543", "chefs", active=True, name="Семга х/к")
    surplus = detail_item("0", "chefs", active=True, name="Семга с/м")
    shortage.amount = Decimal("-10543")
    surplus.amount = Decimal("9895")
    shortage.position.swap_group = "salmon"
    surplus.position.swap_group = "salmon"
    audit = audit_detail_obj([surplus, shortage])
    computation = compute([surplus, shortage], chefs=[employee("Cook")])
    audit.computation_snapshot = computation.snapshot

    payload = inventory_routes.audit_payload(audit, include_items=True)

    assert payload["total_shortage_considered"] == "648.00"


@pytest.mark.asyncio
async def test_patch_activate_without_group_fails_422() -> None:
    position = position_for(None, active=False)
    session = InventoryRouteSession(position)

    with pytest.raises(HTTPException) as exc_info:
        await inventory_routes.patch_position(
            position.id,
            InventoryPositionPatch(is_active=True),
            session,  # type: ignore[arg-type]
            finance_actor(),
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "Перед активацией укажите группу распределения штрафа"


@pytest.mark.asyncio
async def test_patch_activate_with_group_succeeds() -> None:
    position = position_for(None, active=False)
    session = InventoryRouteSession(position)

    payload = await inventory_routes.patch_position(
        position.id,
        InventoryPositionPatch(allocation_group="common", is_active=True),
        session,  # type: ignore[arg-type]
        finance_actor(),
    )

    assert payload["allocation_group"] == "common"
    assert payload["is_active"] is True
    assert session.committed is True


@pytest.mark.asyncio
async def test_finalized_audit_rejects_override_change() -> None:
    audit = audit_detail_obj([detail_item("100", "chefs", active=True)])
    audit.status = "applied"
    item_row = audit.items[0]
    session = AuditItemRouteSession(audit)

    with pytest.raises(HTTPException) as exc_info:
        await inventory_routes.patch_audit_item(
            audit.id,
            item_row.id,
            inventory_routes.InventoryAuditItemPatch(swap_group_override=""),
            session,  # type: ignore[arg-type]
            finance_actor(),
        )

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_patch_display_name_rejected_422() -> None:
    position = position_for("chefs", active=True)
    session = InventoryRouteSession(position)

    with pytest.raises(HTTPException) as exc_info:
        await inventory_routes.patch_position(
            position.id,
            InventoryPositionPatch(display_name="Новое имя"),
            session,  # type: ignore[arg-type]
            finance_actor(),
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "Название синхронизируется из iiko, его нельзя изменить вручную"


@pytest.mark.asyncio
async def test_sync_positions_creates_new_and_updates_existing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = inventory_position("guid-1", "Старое имя", allocation_group="chefs", active=True)
    session = InventorySyncSession(existing=[existing])

    async def fake_fetch_products_catalog(
        _session: Any,
        *,
        refresh: bool,
    ) -> dict[str, dict[str, str]]:
        assert refresh is True
        return {
            "guid-1": {"guid": "guid-1", "name": "Новое имя"},
            "guid-2": {"guid": "guid-2", "name": "Лосось"},
        }

    monkeypatch.setattr(iiko_inventory, "fetch_products_catalog", fake_fetch_products_catalog)

    result = await sync_positions_from_iiko(session, actor=finance_actor())  # type: ignore[arg-type]

    assert result.added == 1
    assert result.updated == 1
    assert result.total == 2
    assert existing.display_name == "Новое имя"
    assert existing.allocation_group == "chefs"
    assert existing.is_active is True
    added_positions = [item for item in session.added if isinstance(item, InventoryAuditPosition)]
    assert added_positions[0].iiko_product_guid == "guid-2"
    assert added_positions[0].allocation_group is None
    assert added_positions[0].is_active is False


def test_sync_only_takes_goods_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(iiko_inventory, "_load_inventory_module", fake_products_module)

    products = iiko_inventory._fetch_products_catalog_sync()

    assert set(products) == {"goods-active"}


def test_iiko_candidates_returns_all_documents_for_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(iiko_inventory, "_load_inventory_module", fake_inventory_documents_module)

    documents = iiko_inventory._fetch_inventory_documents_sync(date(2026, 5, 26))

    assert [document["document_id"] for document in documents] == ["doc-1", "doc-2"]
    assert [len(document["items"]) for document in documents] == [2, 1]
    assert documents[0]["total_shortage"] == Decimal("1500.00")


def test_iiko_inventory_import_keeps_surplus_signed_amount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        iiko_inventory,
        "_load_inventory_module",
        fake_inventory_surplus_documents_module,
    )

    documents = iiko_inventory._fetch_inventory_documents_sync(date(2026, 5, 26))

    assert len(documents) == 1
    assert documents[0]["total_shortage"] == Decimal("10543.00")
    assert [
        (item["product_name"], item["amount"], item["shortage_amount"])
        for item in documents[0]["items"]
    ] == [
        ("Семга х/к", Decimal("-10543.00"), Decimal("10543.00")),
        ("Семга SM", Decimal("8895.00"), Decimal("0.00")),
    ]


def test_import_with_document_id_picks_correct_document(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(iiko_inventory, "_load_inventory_module", fake_inventory_documents_module)

    documents_by_id = {
        document["document_id"]: document
        for document in iiko_inventory._fetch_inventory_documents_sync(date(2026, 5, 26))
    }

    assert documents_by_id["doc-2"]["document_num"] == "0028"
    assert documents_by_id["doc-2"]["items"][0]["product_name"] == "Упаковка"


def calculator_settings() -> dict[str, Any]:
    return {
        "payroll.role_category_rates": {"Пиццерист": {"category_2": 2200}},
        "payroll.category_rules": {
            "2": {"coeff": 7.5, "deposit_target": 15000, "deposit_withholding": 1000}
        },
        "payroll.revenue_percent_tiers": [{"from": 0, "rate": 0}],
        "payroll.allowances": {"senior": 0, "deputy_senior": 0},
        "payroll.weekday_premium": {"amount": 200, "threshold_hours": 8},
        "payroll.fund_rates_by_tenure": [],
        "payroll.mock_daily_revenue": {},
        "payroll.deposit_auto_withholding_enabled": False,
        "payroll.deposit_fund_payment_date": "01-15",
    }


def payroll_period(start: date, end: date) -> PayrollPeriod:
    return PayrollPeriod(
        id=uuid.uuid4(),
        period_type="week",
        start_date=start,
        end_date=end,
        payroll_date=end,
        status="open",
    )


def calculator_employee() -> Employee:
    return Employee(
        id=uuid.uuid4(),
        full_name="Payroll Employee",
        iiko_id=f"iiko-{uuid.uuid4()}",
        position="Повар",
        category="category_2",
        status="active",
        is_senior=False,
        is_deputy_senior=False,
        pin_hash="hashed-pin",
        pin_set_at=datetime(2026, 5, 1, tzinfo=UTC),
        created_at=datetime(2026, 5, 1, tzinfo=UTC),
        updated_at=datetime(2026, 5, 1, tzinfo=UTC),
    )


def calculator_entry(
    period: PayrollPeriod,
    payroll_employee: Employee,
    work_date: date,
) -> AttendanceEntry:
    return AttendanceEntry(
        id=uuid.uuid4(),
        employee_id=payroll_employee.id,
        period_id=period.id,
        work_date=work_date,
        started_at=datetime.combine(work_date, datetime.min.time(), tzinfo=UTC),
        ended_at=datetime.combine(work_date, datetime.min.time(), tzinfo=UTC),
        minutes_worked=720,
        role="Пиццерист",
        station=None,
        source="manual",
        quality_status="ok",
        notes=None,
    )


def inventory_adjustment_for_audit(
    payroll_employee: Employee,
    *,
    audit_business_date: date,
    amount: Decimal,
) -> PayrollAdjustment:
    category = PayrollAdjustmentCategory(
        id=uuid.uuid4(),
        type="penalty",
        code="inventory_shortage",
        display_name="Недостача по ревизии",
        default_amount=amount,
        is_active=True,
        sort_order=50,
    )
    adjustment = PayrollAdjustment(
        id=uuid.uuid4(),
        employee_id=payroll_employee.id,
        work_date=audit_penalty_work_date(audit_business_date),
        type="penalty",
        category_id=category.id,
        amount=amount,
        comment=adjustment_comment(audit_business_date),
    )
    adjustment.category = category
    return adjustment


def compute(
    items: list[Any],
    *,
    chefs: list[Any] | None = None,
    admins: list[Any] | None = None,
    settings: dict[str, Decimal] | None = None,
    employee_exclusions: dict[uuid.UUID, str] | None = None,
    item_exclusions: dict[uuid.UUID, str] | None = None,
    prepaid_charges: list[dict] | None = None,
):
    return build_penalty_computation(
        audit=audit_obj(),
        items=items,
        settings=settings,
        chefs=chefs or [],
        admins=admins or [],
        employee_exclusions=employee_exclusions,
        item_exclusions=item_exclusions,
        prepaid_charges=prepaid_charges,
    )


def audit_obj(
    *,
    business_date: date = date(2026, 5, 26),
    previous_audit_date: date | None = date(2026, 5, 19),
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        business_date=business_date,
        previous_audit_date=previous_audit_date,
    )


def item(amount: str, group: str) -> SimpleNamespace:
    position = position_for(group)
    return SimpleNamespace(
        id=uuid.uuid4(),
        position=position,
        product_name_snapshot=position.display_name,
        shortage_amount=Decimal(amount),
    )


def swap_item(
    amount: str,
    group: str,
    swap_group: str,
    name: str,
    *,
    override: str | None = None,
) -> SimpleNamespace:
    position = position_for(group, swap_group=swap_group)
    item_row = SimpleNamespace(
        id=uuid.uuid4(),
        position=position,
        product_name_snapshot=name,
        amount=Decimal(amount),
        shortage_amount=abs(Decimal(amount)) if Decimal(amount) < 0 else Decimal("0"),
    )
    if override is not None:
        item_row.swap_group_override = override
    return item_row


def detail_item(
    amount: str,
    group: str | None,
    *,
    active: bool,
    has_position: bool = True,
    name: str = "Позиция",
) -> SimpleNamespace:
    position = position_for(group, active=active) if has_position else None
    return SimpleNamespace(
        id=uuid.uuid4(),
        audit_id=uuid.uuid4(),
        position=position,
        position_id=position.id if position is not None else None,
        iiko_product_guid=position.iiko_product_guid if position is not None else str(uuid.uuid4()),
        product_name_snapshot=name,
        shortage_amount=Decimal(amount),
        created_at=None,
    )


def audit_detail_obj(items: list[SimpleNamespace]) -> SimpleNamespace:
    audit_id = uuid.uuid4()
    for item_row in items:
        item_row.audit_id = audit_id
    return SimpleNamespace(
        id=audit_id,
        business_date=date(2026, 5, 26),
        previous_audit_date=date(2026, 5, 19),
        iiko_document_id="doc-1",
        iiko_document_num="0027",
        source="iiko",
        status="draft",
        total_shortage_amount=Decimal("0"),
        total_penalty_amount=Decimal("0"),
        computation_snapshot=None,
        notes=None,
        created_at=None,
        updated_at=None,
        applied_at=None,
        items=items,
        employee_exclusions=[],
        item_exclusions=[],
    )


def position_for(
    group: str | None,
    *,
    active: bool = True,
    guid: str | None = None,
    swap_group: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        code=f"{group or 'none'}_{uuid.uuid4().hex[:6]}",
        display_name=group or "Позиция",
        allocation_group=group,
        swap_group=swap_group,
        iiko_product_guid=guid,
        is_active=active,
        sort_order=100,
        created_at=None,
        updated_at=None,
    )


def employee(name: str, position: str = "Повар") -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), full_name=name, position=position)


def exclusion_audit(
    business_date: date,
    *,
    status: str = "draft",
) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), business_date=business_date, status=status)


def item_exclusion_row(
    audit: SimpleNamespace,
    *,
    name: str = "Лосось",
    amount: str = "-1000",
    reason: str = "брак",
) -> SimpleNamespace:
    item_id = uuid.uuid4()
    return SimpleNamespace(
        id=uuid.uuid4(),
        audit_id=audit.id,
        audit=audit,
        item_id=item_id,
        item=SimpleNamespace(
            id=item_id,
            product_name_snapshot=name,
            amount=Decimal(amount),
            shortage_amount=abs(Decimal(amount)) if Decimal(amount) < 0 else Decimal("0"),
        ),
        reason=reason,
        created_by=SimpleNamespace(full_name="Финансист"),
        created_at=datetime.combine(audit.business_date, datetime.min.time(), tzinfo=UTC),
    )


def employee_exclusion_row(
    audit: SimpleNamespace,
    employee_row: SimpleNamespace,
    *,
    reason: str = "не участвовал",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        audit_id=audit.id,
        audit=audit,
        employee_id=employee_row.id,
        employee=employee_row,
        reason=reason,
        created_by=SimpleNamespace(full_name="Финансист"),
        created_at=datetime.combine(audit.business_date, datetime.min.time(), tzinfo=UTC),
    )


def finance_actor() -> CurrentActor:
    return CurrentActor(roles=frozenset({"finance_manager"}), user_id=uuid.uuid4())


class InventoryRouteSession:
    def __init__(
        self,
        position: SimpleNamespace,
        existing: list[SimpleNamespace] | None = None,
    ) -> None:
        self.position = position
        self.existing = existing or []
        self.committed = False

    async def get(self, _model: Any, position_id: uuid.UUID) -> SimpleNamespace | None:
        if position_id == self.position.id:
            return self.position
        return None

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, _position: SimpleNamespace) -> None:
        return None

    async def scalars(self, _query: Any) -> FakeScalarResult:
        return FakeScalarResult(self.existing)


class AuditItemRouteSession:
    def __init__(self, audit: SimpleNamespace) -> None:
        self.audit = audit
        self.committed = False

    async def scalar(self, _query: Any) -> SimpleNamespace:
        return self.audit

    async def commit(self) -> None:
        self.committed = True


class ApplyAuditSession:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.committed = False

    def add(self, item: Any) -> None:
        self.added.append(item)

    async def commit(self) -> None:
        self.committed = True


class ItemExclusionServiceSession:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.committed = False
        self.flushed = False

    def add(self, item: Any) -> None:
        self.added.append(item)

    async def delete(self, item: Any) -> None:
        self.deleted.append(item)

    async def flush(self) -> None:
        self.flushed = True
        for item in self.added:
            if isinstance(item, AgentRun) and item.id is None:
                item.id = uuid.uuid4()

    async def commit(self) -> None:
        self.committed = True


class FakeScalarResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def unique(self) -> FakeScalarResult:
        return self

    def all(self) -> list[Any]:
        return self._rows


class AuditExclusionsSession:
    def __init__(
        self,
        *,
        items: list[SimpleNamespace],
        employees: list[SimpleNamespace],
    ) -> None:
        self.items = items
        self.employees = employees

    async def scalars(self, query: Any) -> FakeScalarResult:
        entity = query.column_descriptions[0].get("entity")
        rows = self.items if entity is InventoryAuditItemExclusion else self.employees
        filtered = self._filter_rows(list(rows), query)
        filtered.sort(
            key=lambda row: (row.audit.business_date, row.created_at),
            reverse=True,
        )
        return FakeScalarResult(filtered)

    def _filter_rows(self, rows: list[SimpleNamespace], query: Any) -> list[SimpleNamespace]:
        result = rows
        for criterion in getattr(query, "_where_criteria", ()):
            left_name = getattr(getattr(criterion, "left", None), "name", None)
            operator_name = getattr(getattr(criterion, "operator", None), "__name__", "")
            value = getattr(getattr(criterion, "right", None), "value", None)
            if left_name == "business_date" and operator_name == "ge":
                result = [row for row in result if row.audit.business_date >= value]
            elif left_name == "business_date" and operator_name == "le":
                result = [row for row in result if row.audit.business_date <= value]
            elif left_name == "employee_id" and operator_name == "eq":
                result = [row for row in result if getattr(row, "employee_id", None) == value]
        return result


class InventorySyncSession:
    def __init__(self, existing: list[InventoryAuditPosition]) -> None:
        self.existing = existing
        self.added: list[Any] = []
        self.committed = False

    async def scalars(self, _query: Any) -> FakeScalarResult:
        return FakeScalarResult(self.existing)

    def add(self, item: Any) -> None:
        self.added.append(item)

    async def flush(self) -> None:
        for item in self.added:
            if isinstance(item, AgentRun) and item.id is None:
                item.id = uuid.uuid4()

    async def commit(self) -> None:
        self.committed = True


def inventory_position(
    guid: str,
    display_name: str,
    *,
    allocation_group: str | None,
    active: bool,
) -> InventoryAuditPosition:
    return InventoryAuditPosition(
        id=uuid.uuid4(),
        code=f"iiko_{guid}",
        display_name=display_name,
        allocation_group=allocation_group,
        iiko_product_guid=guid,
        is_active=active,
        sort_order=100,
    )


def install_item_exclusion_fakes(
    monkeypatch: pytest.MonkeyPatch,
    audit: SimpleNamespace,
) -> None:
    async def fake_load_audit(_session: Any, _audit_id: uuid.UUID) -> SimpleNamespace:
        items_by_id = {item.id: item for item in getattr(audit, "items", [])}
        for exclusion in getattr(audit, "item_exclusions", []):
            if isinstance(exclusion, InventoryAuditItemExclusion):
                exclusion.item = items_by_id.get(exclusion.item_id)
                exclusion.created_by = None
        return audit

    async def fake_inventory_settings(_session: Any) -> dict[str, Decimal]:
        return DEFAULT_SETTINGS.copy()

    async def fake_load_period_employees(
        _session: Any,
        *,
        position: str,
        period_start: date,
        period_end: date,
    ) -> list[Any]:
        return []

    async def fake_load_prepaid_revision_charges(
        _session: Any,
        *,
        period_start: date,
        period_end: date,
    ) -> list[dict]:
        return []

    monkeypatch.setattr(inventory_audit_service, "_load_audit_or_404", fake_load_audit)
    monkeypatch.setattr(inventory_audit_service, "_inventory_settings", fake_inventory_settings)
    monkeypatch.setattr(
        inventory_audit_service,
        "_load_period_employees",
        fake_load_period_employees,
    )
    monkeypatch.setattr(
        inventory_audit_service,
        "_load_prepaid_revision_charges",
        fake_load_prepaid_revision_charges,
    )


def item_ids_in_penalty_groups(snapshot: dict[str, Any]) -> set[str]:
    rows: set[str] = set()
    for group in ("chefs", "common", "admins"):
        for row in snapshot["groups"][group]["items"]:
            if "item_id" in row:
                rows.add(str(row["item_id"]))
            for nested in row.get("items", []):
                rows.add(str(nested["item_id"]))
    return rows


def fake_products_module() -> SimpleNamespace:
    rows = [
        {"id": "goods-active", "name": "Лосось", "type": "GOODS", "deleted": False},
        {"id": "dish", "name": "Ролл", "type": "DISH", "deleted": False},
        {"id": "goods-deleted", "name": "Старое", "type": "GOODS", "deleted": True},
        {"id": "prepared", "name": "Заготовка", "type": "PREPARED", "deleted": False},
    ]
    return SimpleNamespace(
        load_local_env=lambda: None,
        IikoClient=lambda: SimpleNamespace(request=lambda _endpoint: (200, b"[]")),
        product_records_from_payload=lambda _payload: rows,
        value_from=value_from,
    )


def fake_inventory_documents_module() -> SimpleNamespace:
    rows = [
        inventory_row("doc-1", "0027", "guid-1", "Лосось", "-1000"),
        inventory_row("doc-1", "0027", "guid-2", "Рис", "-500"),
        inventory_row("doc-2", "0028", "guid-3", "Упаковка", "-1800"),
    ]
    products = {
        "guid-1": {"name": "Лосось"},
        "guid-2": {"name": "Рис"},
        "guid-3": {"name": "Упаковка"},
    }
    return SimpleNamespace(
        load_local_env=lambda: None,
        IikoClient=lambda: SimpleNamespace(request=lambda _endpoint, params=None: (200, b"")),
        inventory_params=lambda _date_from, _date_to: {},
        parse_store_operations=lambda _data: ("json", rows),
        load_products=lambda _client, refresh=False: products,
        parse_iiko_date=lambda value: date.fromisoformat(str(value)),
        value_from=value_from,
    )


def fake_inventory_surplus_documents_module() -> SimpleNamespace:
    rows = [
        inventory_row("doc-salmon", "0030", "guid-fk", "Семга х/к", "-10543"),
        inventory_row("doc-salmon", "0030", "guid-sm", "Семга SM", "8895", incoming="true"),
    ]
    products = {
        "guid-fk": {"name": "Семга х/к"},
        "guid-sm": {"name": "Семга SM"},
    }
    return SimpleNamespace(
        load_local_env=lambda: None,
        IikoClient=lambda: SimpleNamespace(request=lambda _endpoint, params=None: (200, b"")),
        inventory_params=lambda _date_from, _date_to: {},
        parse_store_operations=lambda _data: ("json", rows),
        load_products=lambda _client, refresh=False: products,
        parse_iiko_date=lambda value: date.fromisoformat(str(value)),
        value_from=value_from,
    )


def inventory_row(
    document_id: str,
    document_num: str,
    product_guid: str,
    product_name: str,
    amount: str,
    *,
    incoming: str = "false",
) -> dict[str, Any]:
    return {
        "documentType": "INCOMING_INVENTORY",
        "type": "INVENTORY_CORRECTION",
        "date": "2026-05-26",
        "incoming": incoming,
        "sum": amount,
        "documentId": document_id,
        "documentNum": document_num,
        "product": product_guid,
        "productName": product_name,
    }


def value_from(row: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        if key in row:
            return row[key]
    return None

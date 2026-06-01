from __future__ import annotations

import uuid
from datetime import date

from app.models import ShiftAllowanceOverride
from app.services.seniority_allowance_resolver import (
    AllowanceCandidate,
    resolve_cashier_allowance_recipient,
)


def candidate(
    *,
    employee_id: uuid.UUID | None = None,
    senior: bool = False,
    deputy: bool = False,
    planned: bool = True,
    actual: bool = True,
    name: str = "Иванов Иван",
) -> AllowanceCandidate:
    return AllowanceCandidate(
        employee_id=employee_id or uuid.uuid4(),
        full_name=name,
        is_senior=senior,
        is_deputy_senior=deputy,
        is_planned=planned,
        is_actual=actual,
        minutes_worked=720,
    )


def override(
    *,
    recipient_employee_id: uuid.UUID | None,
    recipient_role: str,
) -> ShiftAllowanceOverride:
    return ShiftAllowanceOverride(
        id=uuid.uuid4(),
        shift_schedule_id=uuid.uuid4(),
        business_date=date(2026, 6, 1),
        position="Кассир",
        recipient_employee_id=recipient_employee_id,
        recipient_role=recipient_role,
    )


def test_sole_senior_cashier_gets_allowance() -> None:
    senior = candidate(senior=True)

    result = resolve_cashier_allowance_recipient(candidates=[senior], manual_override=None)

    assert result.recipient_employee_id == senior.employee_id
    assert result.recipient_role == "senior"
    assert result.reason == "sole_senior"


def test_sole_deputy_cashier_gets_allowance() -> None:
    deputy = candidate(deputy=True)

    result = resolve_cashier_allowance_recipient(candidates=[deputy], manual_override=None)

    assert result.recipient_employee_id == deputy.employee_id
    assert result.recipient_role == "deputy_senior"
    assert result.reason == "sole_deputy"


def test_no_candidates_returns_none() -> None:
    result = resolve_cashier_allowance_recipient(candidates=[], manual_override=None)

    assert result.recipient_employee_id is None
    assert result.recipient_role == "none"
    assert result.reason == "no_candidate"


def test_intersection_plan_priority_senior() -> None:
    senior = candidate(senior=True, planned=True, actual=True)
    deputy = candidate(deputy=True, planned=False, actual=True)

    result = resolve_cashier_allowance_recipient(
        candidates=[senior, deputy],
        manual_override=None,
    )

    assert result.recipient_employee_id == senior.employee_id
    assert result.reason == "plan_priority"


def test_intersection_plan_priority_deputy() -> None:
    senior = candidate(senior=True, planned=False, actual=True)
    deputy = candidate(deputy=True, planned=True, actual=True)

    result = resolve_cashier_allowance_recipient(
        candidates=[senior, deputy],
        manual_override=None,
    )

    assert result.recipient_employee_id == deputy.employee_id
    assert result.recipient_role == "deputy_senior"
    assert result.reason == "plan_priority"


def test_intersection_both_planned_default_senior() -> None:
    senior = candidate(senior=True, planned=True)
    deputy = candidate(deputy=True, planned=True)

    result = resolve_cashier_allowance_recipient(
        candidates=[senior, deputy],
        manual_override=None,
    )

    assert result.recipient_employee_id == senior.employee_id
    assert result.reason == "default_senior"


def test_intersection_neither_planned_default_senior() -> None:
    senior = candidate(senior=True, planned=False, actual=True)
    deputy = candidate(deputy=True, planned=False, actual=True)

    result = resolve_cashier_allowance_recipient(
        candidates=[senior, deputy],
        manual_override=None,
    )

    assert result.recipient_employee_id == senior.employee_id
    assert result.reason == "default_senior"


def test_manual_override_takes_precedence() -> None:
    senior = candidate(senior=True)
    deputy = candidate(deputy=True)

    result = resolve_cashier_allowance_recipient(
        candidates=[senior, deputy],
        manual_override=override(
            recipient_employee_id=deputy.employee_id,
            recipient_role="deputy_senior",
        ),
    )

    assert result.recipient_employee_id == deputy.employee_id
    assert result.recipient_role == "deputy_senior"
    assert result.reason == "manual_override"


def test_manual_override_none_blocks_allowance() -> None:
    senior = candidate(senior=True)
    deputy = candidate(deputy=True)

    result = resolve_cashier_allowance_recipient(
        candidates=[senior, deputy],
        manual_override=override(recipient_employee_id=None, recipient_role="none"),
    )

    assert result.recipient_employee_id is None
    assert result.recipient_role == "none"
    assert result.reason == "manual_override"


def test_manual_override_to_absent_falls_back_to_auto() -> None:
    senior = candidate(senior=True)
    deputy = candidate(deputy=True)

    result = resolve_cashier_allowance_recipient(
        candidates=[senior, deputy],
        manual_override=override(
            recipient_employee_id=uuid.uuid4(),
            recipient_role="deputy_senior",
        ),
    )

    assert result.recipient_employee_id == senior.employee_id
    assert result.recipient_role == "senior"
    assert result.reason == "manual_override_fallback"

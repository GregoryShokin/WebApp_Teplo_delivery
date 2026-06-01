from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EmployeeChangeEvent, EmployeeDismissalReason, PayrollPeriod

EMPLOYEE_CHANGE_SOURCES = frozenset({"app", "iiko_sync", "system_migration"})
EMPLOYEE_CHANGE_STATUSES = frozenset({"success", "error", "requires_review", "skipped"})
IIKO_SYNC_LABEL = "Синхронизация IIko"
SYSTEM_MIGRATION_LABEL = "Системная миграция"

DISMISSAL_REASON_DEFINITIONS: tuple[tuple[str, str, bool, int], ...] = (
    ("voluntary", "По собственному желанию", False, 10),
    ("no_show", "Не вышел на смену", False, 20),
    ("discipline", "Нарушение дисциплины", False, 30),
    ("failed_trial", "Не прошёл стажировку", False, 40),
    ("layoff_no_shifts", "Сокращение/нет смен", False, 50),
    ("transfer", "Перевод", False, 60),
    ("other", "Другое", True, 70),
)
DISMISSAL_REASON_LABELS: dict[str, str] = {
    code: label for code, label, _requires_comment, _sort_order in DISMISSAL_REASON_DEFINITIONS
}
DISMISSAL_REASON_CODES_BY_LABEL = {
    label.casefold(): code for code, label in DISMISSAL_REASON_LABELS.items()
}
OTHER_DISMISSAL_REASON_CODE = "other"

PAYROLL_IMPACT_CHANGE_TYPES = frozenset(
    {
        "update_position",
        "assign_role",
        "change_role",
        "close_role",
        "change_category",
        "set_senior",
        "unset_senior",
        "set_deputy_senior",
        "unset_deputy_senior",
        "set_hire_date",
        "dismiss",
        "reinstate",
        "iiko_sync_create",
        "iiko_sync_update",
        "iiko_sync_deactivate",
    }
)
PIN_CHANGE_TYPES = frozenset({"change_pin", "pin_set", "pin_changed"})
PIN_PAYLOAD_KEYS = frozenset(
    {
        "pin",
        "pin_code",
        "pincode",
        "pin_hash",
        "pinhash",
        "pinCode",
        "pinHash",
    }
)


@dataclass(frozen=True, slots=True)
class ResolvedDismissalReason:
    reason_id: uuid.UUID | None
    code: str
    label: str
    comment: str | None
    requires_comment: bool = False

    @property
    def display_text(self) -> str:
        if self.code == OTHER_DISMISSAL_REASON_CODE and self.comment:
            return f"{self.label}: {self.comment}"
        return self.label


def actor_label_from_roles(roles: set[str] | frozenset[str]) -> str:
    if not roles:
        return "Система"
    return ", ".join(sorted(roles))


async def list_dismissal_reasons(
    session: AsyncSession,
    *,
    include_inactive: bool = False,
) -> list[EmployeeDismissalReason]:
    result = await session.scalars(
        select(EmployeeDismissalReason).order_by(
            EmployeeDismissalReason.sort_order,
            EmployeeDismissalReason.label,
        )
    )
    reasons = list(result.all())
    if not include_inactive:
        reasons = [reason for reason in reasons if reason.is_active]
    return sorted(reasons, key=lambda reason: (reason.sort_order, reason.label.casefold()))


async def resolve_dismissal_reason(
    session: AsyncSession,
    payload: Any,
) -> ResolvedDismissalReason:
    raw_id = getattr(payload, "reason_id", None)
    raw_code = _optional_text(getattr(payload, "reason_code", None))
    raw_label = _optional_text(getattr(payload, "reason_label", None))
    legacy_reason = _optional_text(getattr(payload, "reason", None))
    comment = _optional_text(getattr(payload, "comment", None))

    if raw_id is None and raw_code is None and raw_label is None and legacy_reason is None:
        raise ValueError("Причина увольнения обязательна")

    reasons = await list_dismissal_reasons(session)
    reason_by_id = {reason.id: reason for reason in reasons}
    reason_by_code = {reason.code: reason for reason in reasons}
    reason_by_label = {reason.label.casefold(): reason for reason in reasons}

    selected: EmployeeDismissalReason | None = None
    if raw_id is not None:
        selected = reason_by_id.get(raw_id)
        if selected is None:
            raise ValueError("Причина увольнения не найдена")
        if raw_code is not None and raw_code != selected.code:
            raise ValueError("Код причины не соответствует выбранной причине")
    elif raw_code is not None:
        selected = reason_by_code.get(raw_code)
    elif raw_label is not None:
        selected = reason_by_label.get(raw_label.casefold())
    elif legacy_reason is not None:
        selected = reason_by_code.get(legacy_reason) or reason_by_label.get(
            legacy_reason.casefold()
        )

    if selected is not None:
        if selected.requires_comment and comment is None:
            raise ValueError(f"Для причины «{selected.label}» комментарий обязателен")
        return ResolvedDismissalReason(
            reason_id=selected.id,
            code=selected.code,
            label=selected.label,
            comment=comment,
            requires_comment=selected.requires_comment,
        )

    if legacy_reason is not None:
        legacy_code = DISMISSAL_REASON_CODES_BY_LABEL.get(legacy_reason.casefold())
        if legacy_code == OTHER_DISMISSAL_REASON_CODE and comment is None:
            raise ValueError("Для причины «Другое» комментарий обязателен")
        return ResolvedDismissalReason(
            reason_id=None,
            code=legacy_code or "legacy_text",
            label=DISMISSAL_REASON_LABELS.get(legacy_code or "", legacy_reason),
            comment=comment or (legacy_reason if legacy_code is None else None),
            requires_comment=legacy_code == OTHER_DISMISSAL_REASON_CODE,
        )

    raise ValueError("Причина увольнения не найдена")


async def add_employee_change_event(
    session: AsyncSession,
    *,
    employee_id: uuid.UUID | None,
    change_type: str,
    source: str,
    summary: str,
    changed_at: datetime | None = None,
    effective_from: date | None = None,
    effective_to: date | None = None,
    actor_user_id: uuid.UUID | None = None,
    actor_label: str | None = None,
    status: str = "success",
    before_value: dict[str, Any] | None = None,
    after_value: dict[str, Any] | None = None,
    diff: dict[str, Any] | None = None,
    reason_id: uuid.UUID | None = None,
    reason: str | None = None,
    reason_code: str | None = None,
    reason_label: str | None = None,
    comment: str | None = None,
    related_agent_run_id: uuid.UUID | None = None,
    related_agent_action_id: uuid.UUID | None = None,
    related_entity_type: str | None = None,
    related_entity_id: uuid.UUID | None = None,
    payroll_impact: bool | None = None,
    payroll_impact_metadata: dict[str, Any] | None = None,
) -> EmployeeChangeEvent:
    if source not in EMPLOYEE_CHANGE_SOURCES:
        raise ValueError("Invalid employee change source")
    if status not in EMPLOYEE_CHANGE_STATUSES:
        raise ValueError("Invalid employee change status")

    event_changed_at = changed_at or datetime.now(UTC)
    safe_before = sanitize_change_payload(before_value)
    safe_after = sanitize_change_payload(after_value)
    event_diff = (
        sanitize_change_payload(diff) if diff is not None else build_diff(safe_before, safe_after)
    )
    if change_type in PIN_CHANGE_TYPES:
        safe_before = None
        safe_after = {"pin_changed": True}
        event_diff = {"pin_changed": True}

    has_payroll_impact = (
        change_type in PAYROLL_IMPACT_CHANGE_TYPES if payroll_impact is None else payroll_impact
    )
    metadata = dict(payroll_impact_metadata or {})
    if has_payroll_impact:
        metadata = await _augment_payroll_impact_metadata(
            session,
            effective_from=effective_from,
            effective_to=effective_to,
            metadata=metadata,
        )
        if status == "success" and metadata.get("requires_correction"):
            status = "requires_review"

    event = EmployeeChangeEvent(
        id=uuid.uuid4(),
        employee_id=employee_id,
        changed_at=event_changed_at,
        effective_from=effective_from,
        effective_to=effective_to,
        change_type=change_type,
        source=source,
        actor_user_id=actor_user_id,
        actor_label=actor_label,
        status=status,
        summary=summary,
        before_value=safe_before,
        after_value=safe_after,
        diff=event_diff,
        reason_id=reason_id,
        reason=reason,
        reason_code=reason_code,
        reason_label=reason_label,
        comment=comment,
        related_agent_run_id=related_agent_run_id,
        related_agent_action_id=related_agent_action_id,
        related_entity_type=related_entity_type,
        related_entity_id=related_entity_id,
        payroll_impact=has_payroll_impact,
        payroll_impact_metadata=metadata,
        created_at=event_changed_at,
    )
    session.add(event)
    return event


async def add_employee_lifecycle_events(
    session: AsyncSession,
    *,
    action_type: str,
    employee_id: uuid.UUID,
    before: dict[str, Any] | None,
    after: dict[str, Any],
    changed_at: datetime,
    actor_label: str,
    related_agent_run_id: uuid.UUID,
    related_agent_action_id: uuid.UUID,
    dismissal_reason: ResolvedDismissalReason | None = None,
) -> list[EmployeeChangeEvent]:
    common = {
        "employee_id": employee_id,
        "source": "app",
        "changed_at": changed_at,
        "actor_label": actor_label,
        "related_agent_run_id": related_agent_run_id,
        "related_agent_action_id": related_agent_action_id,
        "related_entity_type": "employee",
        "related_entity_id": employee_id,
    }
    if action_type in {"create", "upsert_from_iiko_create"}:
        return [
            await add_employee_change_event(
                session,
                **common,
                change_type="create_employee",
                effective_from=_date_from_payload(after, "hire_date") or changed_at.date(),
                summary="Создан сотрудник",
                before_value=None,
                after_value=after,
                diff={"created": True},
                payroll_impact=True,
            )
        ]
    if action_type in PIN_CHANGE_TYPES:
        return [
            await add_employee_change_event(
                session,
                **common,
                change_type="change_pin",
                summary="Установлен ПИН" if action_type == "pin_set" else "Изменён ПИН",
                before_value=None,
                after_value={"pin_changed": True},
                diff={"pin_changed": True},
                payroll_impact=False,
            )
        ]
    if action_type == "dismiss":
        return [
            await add_employee_change_event(
                session,
                **common,
                change_type="dismiss",
                effective_from=_date_from_payload(after, "fire_date") or changed_at.date(),
                summary="Сотрудник уволен",
                before_value=before,
                after_value=after,
                reason_id=dismissal_reason.reason_id if dismissal_reason else None,
                reason=dismissal_reason.display_text if dismissal_reason else None,
                reason_code=dismissal_reason.code if dismissal_reason else None,
                reason_label=dismissal_reason.label if dismissal_reason else None,
                comment=dismissal_reason.comment if dismissal_reason else None,
                payroll_impact=True,
            )
        ]
    if action_type == "reinstate":
        return [
            await add_employee_change_event(
                session,
                **common,
                change_type="reinstate",
                effective_from=changed_at.date(),
                summary="Сотрудник восстановлен",
                before_value=before,
                after_value=after,
                payroll_impact=True,
            )
        ]
    if action_type != "update":
        return [
            await add_employee_change_event(
                session,
                **common,
                change_type=f"employee_{action_type}",
                summary="Изменена карточка сотрудника",
                before_value=before,
                after_value=after,
            )
        ]

    events: list[EmployeeChangeEvent] = []
    for field, change_type, summary, payroll_impact in (
        ("full_name", "update_full_name", "Изменено ФИО", False),
        ("position", "update_position", "Изменена должность", True),
        ("category", "change_category", "Изменена категория", True),
        ("default_cooking_station", "change_role", "Изменена роль", True),
    ):
        if not _payload_changed(before, after, field):
            continue
        events.append(
            await add_employee_change_event(
                session,
                **common,
                change_type=change_type,
                effective_from=changed_at.date() if payroll_impact else None,
                summary=summary,
                before_value=before,
                after_value=after,
                diff=_field_diff(before, after, field),
                payroll_impact=payroll_impact,
            )
        )

    for field, set_type, unset_type, set_summary, unset_summary in (
        ("is_senior", "set_senior", "unset_senior", "Назначен Старший", "Снят Старший"),
        (
            "is_deputy_senior",
            "set_deputy_senior",
            "unset_deputy_senior",
            "Назначен Зам старшего",
            "Снят Зам старшего",
        ),
    ):
        if not _payload_changed(before, after, field):
            continue
        enabled = bool((after or {}).get(field))
        events.append(
            await add_employee_change_event(
                session,
                **common,
                change_type=set_type if enabled else unset_type,
                effective_from=changed_at.date(),
                summary=set_summary if enabled else unset_summary,
                before_value=before,
                after_value=after,
                diff=_field_diff(before, after, field),
                payroll_impact=True,
            )
        )

    if _payload_changed(before, after, "roles") and not any(
        event.change_type in {"assign_role", "change_role", "close_role", "change_category"}
        for event in events
    ):
        events.append(
            await add_employee_change_event(
                session,
                **common,
                change_type="change_role",
                effective_from=changed_at.date(),
                summary="Изменены роли сотрудника",
                before_value=before,
                after_value=after,
                diff=_field_diff(before, after, "roles"),
                payroll_impact=True,
            )
        )

    if not events:
        events.append(
            await add_employee_change_event(
                session,
                **common,
                change_type="employee_update",
                summary="Изменена карточка сотрудника",
                before_value=before,
                after_value=after,
            )
        )
    return events


async def add_assignment_change_events(
    session: AsyncSession,
    *,
    action_type: str,
    employee_id: uuid.UUID,
    assignment_id: uuid.UUID,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    changed_at: datetime,
    actor_label: str,
    related_agent_run_id: uuid.UUID,
    related_agent_action_id: uuid.UUID,
    comment: str | None = None,
) -> list[EmployeeChangeEvent]:
    common = {
        "employee_id": employee_id,
        "source": "app",
        "changed_at": changed_at,
        "actor_label": actor_label,
        "related_agent_run_id": related_agent_run_id,
        "related_agent_action_id": related_agent_action_id,
        "related_entity_type": "employee_role_assignment",
        "related_entity_id": assignment_id,
        "comment": comment,
        "payroll_impact": True,
    }
    if action_type == "add_role_assignment":
        return [
            await add_employee_change_event(
                session,
                **common,
                change_type="assign_role",
                effective_from=_date_from_payload(after, "effective_from"),
                summary="Назначена роль",
                before_value=None,
                after_value=after,
                diff={"assigned": True},
            )
        ]
    if action_type == "remove_role_assignment":
        return [
            await add_employee_change_event(
                session,
                **common,
                change_type="close_role",
                effective_from=_date_from_payload(after, "effective_from"),
                effective_to=_date_from_payload(after, "effective_to"),
                summary="Закрыта роль",
                before_value=before,
                after_value=after,
            )
        ]

    events: list[EmployeeChangeEvent] = []
    if _payload_changed(before, after, "payroll_role"):
        events.append(
            await add_employee_change_event(
                session,
                **common,
                change_type="change_role",
                effective_from=_date_from_payload(after, "effective_from"),
                summary="Изменена роль",
                before_value=before,
                after_value=after,
                diff=_field_diff(before, after, "payroll_role"),
            )
        )
    if _payload_changed(before, after, "category"):
        events.append(
            await add_employee_change_event(
                session,
                **common,
                change_type="change_category",
                effective_from=_date_from_payload(after, "effective_from"),
                summary="Изменена категория",
                before_value=before,
                after_value=after,
                diff=_field_diff(before, after, "category"),
            )
        )
    if _payload_changed(before, after, "is_primary"):
        events.append(
            await add_employee_change_event(
                session,
                **common,
                change_type="change_role",
                effective_from=_date_from_payload(after, "effective_from"),
                summary="Изменена основная роль",
                before_value=before,
                after_value=after,
                diff=_field_diff(before, after, "is_primary"),
            )
        )
    if not events:
        events.append(
            await add_employee_change_event(
                session,
                **common,
                change_type="change_role",
                effective_from=_date_from_payload(after, "effective_from"),
                summary="Изменена роль",
                before_value=before,
                after_value=after,
            )
        )
    return events


async def add_iiko_sync_event(
    session: AsyncSession,
    *,
    action_type: str,
    employee_id: uuid.UUID | None,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    changed_at: datetime,
    related_agent_run_id: uuid.UUID,
    related_agent_action_id: uuid.UUID | None,
    status: str = "success",
) -> EmployeeChangeEvent:
    change_type_by_action = {
        "create": "iiko_sync_create",
        "update": "iiko_sync_update",
        "deactivate": "iiko_sync_deactivate",
    }
    summary_by_action = {
        "create": "Синхронизация IIko: создан сотрудник",
        "update": "Синхронизация IIko: обновлён сотрудник",
        "deactivate": "Синхронизация IIko: сотрудник деактивирован",
    }
    change_type = change_type_by_action.get(action_type, "iiko_sync_update")
    return await add_employee_change_event(
        session,
        employee_id=employee_id,
        source="iiko_sync",
        actor_label=IIKO_SYNC_LABEL,
        changed_at=changed_at,
        effective_from=_date_from_payload(after, "fire_date")
        if change_type == "iiko_sync_deactivate"
        else None,
        change_type=change_type,
        status=status,
        summary=summary_by_action.get(action_type, "Синхронизация IIko: обновление сотрудника"),
        before_value=before,
        after_value=after,
        related_agent_run_id=related_agent_run_id,
        related_agent_action_id=related_agent_action_id,
        related_entity_type="employee" if employee_id else None,
        related_entity_id=employee_id,
    )


async def add_iiko_skipped_event(
    session: AsyncSession,
    *,
    iiko_id: str | None,
    reason: str,
    changed_at: datetime,
    related_agent_run_id: uuid.UUID,
    related_agent_action_id: uuid.UUID | None,
) -> EmployeeChangeEvent:
    return await add_employee_change_event(
        session,
        employee_id=None,
        source="iiko_sync",
        actor_label=IIKO_SYNC_LABEL,
        changed_at=changed_at,
        change_type="iiko_sync_skipped",
        status="skipped",
        summary="Синхронизация IIko: запись пропущена",
        after_value={"iiko_id": iiko_id, "reason": reason},
        diff={"skipped": True, "reason": reason},
        reason=reason,
        related_agent_run_id=related_agent_run_id,
        related_agent_action_id=related_agent_action_id,
        related_entity_type="iiko_employee",
        payroll_impact=False,
    )


async def add_iiko_error_event(
    session: AsyncSession,
    *,
    error_message: str,
    changed_at: datetime,
    related_agent_run_id: uuid.UUID,
) -> EmployeeChangeEvent:
    return await add_employee_change_event(
        session,
        employee_id=None,
        source="iiko_sync",
        actor_label=IIKO_SYNC_LABEL,
        changed_at=changed_at,
        change_type="iiko_sync_error",
        status="error",
        summary="Синхронизация IIko: ошибка",
        after_value={"error": error_message[:500]},
        diff={"error": True},
        reason=error_message[:500],
        related_agent_run_id=related_agent_run_id,
        payroll_impact=False,
    )


def sanitize_change_payload(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            normalized = key_text.replace("_", "").casefold()
            if key_text in PIN_PAYLOAD_KEYS or normalized in PIN_PAYLOAD_KEYS:
                continue
            sanitized[key_text] = sanitize_change_payload(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_change_payload(item) for item in value]
    return value


def build_diff(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if before is None and after is None:
        return None
    if before is None:
        return {"created": True, "after": after}
    if after is None:
        return {"deleted": True, "before": before}
    diff: dict[str, Any] = {}
    for key in sorted(set(before) | set(after)):
        if before.get(key) == after.get(key):
            continue
        diff[key] = {"before": before.get(key), "after": after.get(key)}
    return diff or None


async def _augment_payroll_impact_metadata(
    session: AsyncSession,
    *,
    effective_from: date | None,
    effective_to: date | None,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    if effective_from is None:
        return metadata

    today = date.today()
    if effective_from < today:
        metadata["retroactive"] = True

    if not isinstance(session, AsyncSession):
        return metadata

    range_end = effective_to or effective_from
    finalized_periods = list(
        (
            await session.scalars(
                select(PayrollPeriod).where(
                    PayrollPeriod.status == "finalized",
                    PayrollPeriod.start_date <= range_end,
                    PayrollPeriod.end_date >= effective_from,
                )
            )
        ).all()
    )
    if not finalized_periods:
        return metadata

    metadata["requires_correction"] = True
    metadata["correction_pending"] = True
    metadata["closed_payroll_period_ids"] = [str(period.id) for period in finalized_periods]
    metadata["closed_payroll_periods"] = [
        {
            "id": str(period.id),
            "start_date": period.start_date.isoformat(),
            "end_date": period.end_date.isoformat(),
        }
        for period in finalized_periods
    ]
    return metadata


def _payload_changed(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    field: str,
) -> bool:
    return (before or {}).get(field) != (after or {}).get(field)


def _field_diff(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    field: str,
) -> dict[str, Any]:
    return {field: {"before": (before or {}).get(field), "after": (after or {}).get(field)}}


def _date_from_payload(payload: dict[str, Any] | None, field: str) -> date | None:
    value = (payload or {}).get(field)
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None

from __future__ import annotations

import logging
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentAction, AgentRun, CourierIikoShift, Employee
from app.services.couriers.shift_matching import recalculate_matches
from app.services.iiko_sync import (
    _load_export_employees_module,
    _load_source_credential_env,
    _request_iiko_with_incomplete_read_retry,
)

logger = logging.getLogger(__name__)
ROLE_CACHE_TTL = timedelta(hours=1)
COURIER_ROLE_MARKERS = ("курьер", "courier")
# Все iiko-роли, матчащие маркер курьера: в iiko их НЕСКОЛЬКО («Курьер», «Старший курьер»),
# и смену можно открыть под любой — фильтровать надо по всему набору, иначе смены старших
# курьеров теряются (инцидент 2026-06-28: смена Дудникова под ролью «Старший курьер»).
_courier_role_cache: tuple[frozenset[str], datetime] | None = None


@dataclass(slots=True)
class SyncReport:
    total_records: int = 0
    matched_couriers: int = 0
    new: int = 0
    updated: int = 0
    removed: int = 0
    unresolved_employees: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_records": self.total_records,
            "matched_couriers": self.matched_couriers,
            "new": self.new,
            "updated": self.updated,
            "removed": self.removed,
            "unresolved_employees": self.unresolved_employees,
            "errors": self.errors,
        }


@dataclass(frozen=True, slots=True)
class IikoAttendanceRecord:
    iiko_employee_id: str
    iiko_role_id: str
    opened_at: datetime
    closed_at: datetime | None
    attendance_type: str
    raw_payload: dict[str, str | None]


@dataclass(slots=True)
class AttendanceParseResult:
    total_records: int = 0
    records: list[IikoAttendanceRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


async def resolve_courier_role_ids(session: AsyncSession) -> set[str]:
    """ВСЕ iiko-роли курьеров (по маркеру): «Курьер», «Старший курьер» и т.п."""
    global _courier_role_cache
    now = datetime.now(UTC)
    if _courier_role_cache is not None:
        role_ids, cached_at = _courier_role_cache
        if now - cached_at < ROLE_CACHE_TTL:
            return set(role_ids)

    await _load_source_credential_env(session)
    roles = await anyio.to_thread.run_sync(fetch_iiko_employee_roles)
    role_ids = {
        rid
        for item in roles
        if is_courier_role_name(item.get("name"))
        and (rid := first_text(item, "id", "roleId"))
    }
    if not role_ids:
        raise RuntimeError("В iiko не найдена роль «Курьер» / «Courier»")

    _courier_role_cache = (frozenset(role_ids), now)
    return role_ids


async def sync_attendance(
    session: AsyncSession,
    *,
    from_date: date,
    to_date: date,
    run_reason: str = "manual",
    attendance_xml: bytes | str | None = None,
    courier_role_ids: set[str] | None = None,
    recalculate: bool = True,
) -> SyncReport:
    if from_date > to_date:
        raise ValueError("from_date must be before or equal to to_date")

    agent_run = AgentRun(
        agent_name="iiko_courier_attendance_sync",
        status="running",
        params={
            "reason": run_reason,
            "from": from_date.isoformat(),
            "to": to_date.isoformat(),
            "withPaymentDetails": False,
        },
        result={},
    )
    session.add(agent_run)
    await session.flush()

    try:
        role_ids = courier_role_ids or await resolve_courier_role_ids(session)
        data = (
            attendance_xml
            if attendance_xml is not None
            else await anyio.to_thread.run_sync(fetch_iiko_attendance_xml, from_date, to_date)
        )
        parse_result = parse_attendance_xml(data)
        report = SyncReport(
            total_records=parse_result.total_records,
            errors=list(parse_result.errors),
        )
        courier_records = [
            record for record in parse_result.records if record.iiko_role_id in role_ids
        ]
        report.matched_couriers = len(courier_records)
        unresolved_iiko_ids = await upsert_attendance_records(session, courier_records, report)
        report.unresolved_employees = len(unresolved_iiko_ids)
        report.removed = await prune_missing_attendance(
            session, from_date, to_date, courier_records
        )

        employee_ids = await employee_ids_for_iiko_ids(
            session,
            unresolved_iiko_ids,
            courier_records,
        )
        if recalculate and employee_ids:
            await recalculate_matches(
                session,
                from_date,
                to_date,
                employee_ids=employee_ids,
            )

        agent_run.status = "success" if not report.errors else "partial"
        agent_run.finished_at = datetime.now(UTC)
        agent_run.result = report.as_dict()
        session.add(
            AgentAction(
                agent_run_id=agent_run.id,
                action_type="sync_summary",
                target_table="courier_iiko_shift",
                before_value=None,
                after_value=report.as_dict(),
            )
        )
        await session.commit()
        return report
    except Exception as exc:
        agent_run.status = "failed"
        agent_run.finished_at = datetime.now(UTC)
        agent_run.result = {"error": str(exc)[:500]}
        await session.commit()
        raise


async def prune_missing_attendance(
    session: AsyncSession,
    from_date: date,
    to_date: date,
    records: Iterable[IikoAttendanceRecord],
) -> int:
    """Удаляет явки за синкаемое окно, которых больше нет в выгрузке iiko.

    iiko иногда создаёт лишнюю явку (двойная отметка), а потом убирает её из выгрузки.
    upsert по (iiko_employee_id, opened_at) такие записи не вычищал — они залипали
    открытыми (closed_at IS NULL) и курьер вечно «на линии». Сверяем БД с выгрузкой и
    удаляем исчезнувшие. При ПУСТОЙ выгрузке ничего не трогаем — чтобы частичный/сбойный
    синк не снёс валидные смены.
    """
    records = list(records)
    if not records:
        return 0
    seen = {(record.iiko_employee_id, record.opened_at) for record in records}
    window_start = datetime.combine(from_date, datetime.min.time(), tzinfo=UTC)
    window_end = datetime.combine(to_date + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    rows = (
        await session.scalars(
            select(CourierIikoShift).where(
                CourierIikoShift.opened_at >= window_start,
                CourierIikoShift.opened_at < window_end,
            )
        )
    ).all()
    removed = 0
    for shift in rows:
        if (shift.iiko_employee_id, shift.opened_at) not in seen:
            # Открытую смену, заведённую вебхуком iikoCloud, НЕ трогаем: выгрузка
            # /employees/attendance публикует явку с задержкой, и до её появления смена
            # законно отсутствует в seen. Закроется (поллинг даст closed_at) — снова
            # сверяется штатно. Признак — raw_payload["_source"]=="cloud_webhook".
            payload = shift.raw_payload
            if (
                shift.closed_at is None
                and isinstance(payload, dict)
                and payload.get("_source") == "cloud_webhook"
            ):
                continue
            await session.delete(shift)
            removed += 1
    if removed:
        await session.flush()
    return removed


async def upsert_attendance_records(
    session: AsyncSession,
    records: Iterable[IikoAttendanceRecord],
    report: SyncReport,
) -> set[str]:
    records = list(records)
    employees_by_iiko_id = await load_employees_by_iiko_id(
        session,
        {record.iiko_employee_id for record in records},
    )
    unresolved_iiko_ids: set[str] = set()
    imported_at = datetime.now(UTC)

    for index, record in enumerate(records):
        try:
            employee = employees_by_iiko_id.get(record.iiko_employee_id)
            if employee is None:
                unresolved_iiko_ids.add(record.iiko_employee_id)
            existing = await session.scalar(
                select(CourierIikoShift).where(
                    CourierIikoShift.iiko_employee_id == record.iiko_employee_id,
                    CourierIikoShift.opened_at == record.opened_at,
                )
            )
            if existing is None:
                existing = CourierIikoShift(
                    iiko_employee_id=record.iiko_employee_id,
                    opened_at=record.opened_at,
                )
                session.add(existing)
                report.new += 1
            else:
                report.updated += 1

            existing.employee_id = employee.id if employee is not None else None
            existing.iiko_role_id = record.iiko_role_id
            existing.closed_at = record.closed_at
            existing.attendance_type = record.attendance_type
            existing.imported_at = imported_at
            existing.raw_payload = record.raw_payload
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the sync
            message = f"attendance row {index}: {str(exc)[:500]}"
            logger.exception("Failed to import iiko attendance row %s", index)
            report.errors.append(message)
            continue

    await session.flush()
    return unresolved_iiko_ids


async def load_employees_by_iiko_id(
    session: AsyncSession,
    iiko_ids: set[str],
) -> dict[str, Employee]:
    if not iiko_ids:
        return {}
    result = await session.scalars(select(Employee).where(Employee.iiko_id.in_(iiko_ids)))
    return {employee.iiko_id: employee for employee in result.all()}


async def employee_ids_for_iiko_ids(
    session: AsyncSession,
    unresolved_iiko_ids: set[str],
    records: Iterable[IikoAttendanceRecord],
) -> set[uuid.UUID]:
    resolved_iiko_ids = {
        record.iiko_employee_id
        for record in records
        if record.iiko_employee_id not in unresolved_iiko_ids
    }
    employees = await load_employees_by_iiko_id(session, resolved_iiko_ids)
    return {employee.id for employee in employees.values()}


def fetch_iiko_employee_roles() -> list[dict[str, str]]:
    export_employees = _load_export_employees_module()
    export_employees.load_local_env()
    client = export_employees.IikoClient()
    _status, data = _request_iiko_with_incomplete_read_retry(client, "/employees/roles")
    return parse_xml_records(data, "role")


def fetch_iiko_attendance_xml(from_date: date, to_date: date) -> bytes:
    export_employees = _load_export_employees_module()
    export_employees.load_local_env()
    client = export_employees.IikoClient()
    _status, data = _request_iiko_with_incomplete_read_retry(
        client,
        "/employees/attendance",
        params={
            "from": from_date.isoformat(),
            "to": to_date.isoformat(),
            "withPaymentDetails": "false",
        },
    )
    return data


def parse_attendance_xml(data: bytes | str) -> AttendanceParseResult:
    result = AttendanceParseResult()
    try:
        root = ET.fromstring(data.decode("utf-8") if isinstance(data, bytes) else data)
    except ET.ParseError as exc:
        result.errors.append(f"attendance XML parse error: {exc}")
        return result

    for index, element in enumerate(iter_by_local_name(root, "attendance")):
        result.total_records += 1
        values = child_text_map(element)
        try:
            record = attendance_record_from_values(values)
        except Exception as exc:  # noqa: BLE001 - skip malformed attendance row
            result.errors.append(f"attendance row {index}: {str(exc)[:500]}")
            continue
        result.records.append(record)
    return result


def attendance_record_from_values(values: Mapping[str, str]) -> IikoAttendanceRecord:
    iiko_employee_id = first_text(values, "employeeId", "employee_id")
    iiko_role_id = first_text(values, "roleId", "role_id")
    date_from = first_text(values, "dateFrom", "opened_at")
    if not iiko_employee_id:
        raise ValueError("employeeId is missing")
    if not iiko_role_id:
        raise ValueError("roleId is missing")
    if not date_from:
        raise ValueError("dateFrom is missing")

    opened_at = parse_iiko_datetime(date_from)
    date_to = first_text(values, "dateTo", "closed_at")
    closed_at = parse_iiko_datetime(date_to) if date_to else None
    attendance_type = first_text(values, "attendanceType", "attendance_type") or ""
    return IikoAttendanceRecord(
        iiko_employee_id=iiko_employee_id,
        iiko_role_id=iiko_role_id,
        opened_at=opened_at,
        closed_at=closed_at,
        attendance_type=attendance_type[:8],
        raw_payload={
            "employeeId": iiko_employee_id,
            "roleId": iiko_role_id,
            "dateFrom": date_from,
            "dateTo": date_to or None,
            "attendanceType": attendance_type,
        },
    )


def parse_xml_records(data: bytes | str, item_tag: str) -> list[dict[str, str]]:
    try:
        root = ET.fromstring(data.decode("utf-8") if isinstance(data, bytes) else data)
    except ET.ParseError:
        return []
    return [child_text_map(element) for element in iter_by_local_name(root, item_tag)]


def iter_by_local_name(root: ET.Element, local_name: str) -> Iterable[ET.Element]:
    for element in root.iter():
        if xml_local_name(element.tag) == local_name:
            yield element


def child_text_map(element: ET.Element) -> dict[str, str]:
    values: dict[str, str] = {}
    for child in list(element):
        key = xml_local_name(child.tag)
        if list(child):
            continue
        value = (child.text or "").strip()
        if value:
            values[key] = value
    return values


def xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def parse_iiko_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def first_text(values: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = values.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def is_courier_role_name(value: Any) -> bool:
    text = str(value or "").strip().casefold()
    return any(marker in text for marker in COURIER_ROLE_MARKERS)

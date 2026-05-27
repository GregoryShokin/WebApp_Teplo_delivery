from __future__ import annotations

import importlib
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AttendanceEntry, Employee, PayrollPeriod
from app.services.iiko_sync import _load_source_credential_env
from app.services.settings_service import SettingNotFoundError, get_setting_model

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
SOURCE_TYPES = {"iiko", "manual", "telegram"}
WORK_ATTENDANCE_TYPES = {"Р", "R", "work", "worked", "работа", "отработано", "Н", "В"}


@dataclass(frozen=True, slots=True)
class AttendanceRules:
    open_shift_cap_hours: int
    open_shift_auto_close_time: time


def default_attendance_rules() -> AttendanceRules:
    return AttendanceRules(open_shift_cap_hours=12, open_shift_auto_close_time=time(22, 0))


async def load_attendance_entries(
    session: AsyncSession,
    period: PayrollPeriod,
    *,
    iiko_records: Iterable[Mapping[str, Any]] | None = None,
) -> list[AttendanceEntry]:
    existing_entries = (
        await session.scalars(
            select(AttendanceEntry)
            .where(AttendanceEntry.period_id == period.id)
            .order_by(AttendanceEntry.work_date, AttendanceEntry.started_at)
        )
    ).all()
    if existing_entries and iiko_records is None:
        return list(existing_entries)

    rules = await load_attendance_rules(session)
    if iiko_records is not None:
        records = list(iiko_records)
    else:
        records = await fetch_iiko_attendance_records(
            session,
            period.start_date,
            period.end_date,
        )
    if not records:
        return list(existing_entries)

    employees_by_iiko_id = {
        employee.iiko_id: employee for employee in (await session.scalars(select(Employee))).all()
    }
    entries: list[AttendanceEntry] = []

    for record in records:
        if not is_work_attendance(record):
            continue

        iiko_id = first_text(record, "employeeId", "employee_id", "iiko_id", "iikoId")
        if not iiko_id:
            continue

        employee = employees_by_iiko_id.get(iiko_id)
        if employee is None:
            employee = Employee(
                iiko_id=iiko_id,
                full_name=first_text(record, "employeeName", "Employee", "name") or iiko_id,
                status="needs_setup",
                position=None,
                category=None,
                is_senior=False,
                is_deputy_senior=False,
                iiko_sync_at=datetime.now(UTC),
            )
            session.add(employee)
            await session.flush()
            employees_by_iiko_id[iiko_id] = employee

        entry = build_attendance_entry(record, period, employee, rules)
        if entry.work_date < period.start_date or entry.work_date > period.end_date:
            continue
        session.add(entry)
        entries.append(entry)

    await session.flush()
    return [*existing_entries, *entries]


async def load_attendance_rules(session: AsyncSession) -> AttendanceRules:
    rules = default_attendance_rules()

    try:
        cap_setting = await get_setting_model(session, "payroll.open_shift_cap_hours")
        close_setting = await get_setting_model(session, "payroll.open_shift_auto_close_time")
    except SettingNotFoundError:
        return rules

    return AttendanceRules(
        open_shift_cap_hours=int(cap_setting.value),
        open_shift_auto_close_time=parse_time_value(str(close_setting.value)),
    )


async def fetch_iiko_attendance_records(
    session: AsyncSession,
    start_date: date,
    end_date: date,
) -> list[Mapping[str, Any]]:
    await _load_source_credential_env(session)
    export_employees = load_export_employees_module()
    export_employees.load_local_env()
    client = export_employees.IikoClient()
    _status, data = client.request(
        "/employees/attendance",
        params={
            "withPaymentDetails": "false",
            "from": start_date.isoformat(),
            "to": end_date.isoformat(),
        },
    )
    return list(export_employees.records_from_response("/employees/attendance", data))


def build_attendance_entry(
    record: Mapping[str, Any],
    period: PayrollPeriod,
    employee: Employee,
    rules: AttendanceRules | None = None,
) -> AttendanceEntry:
    rules = rules or default_attendance_rules()
    source = first_text(record, "source") or "iiko"
    if source not in SOURCE_TYPES:
        source = "iiko"

    started_at = parse_datetime(first_value(record, "dateFrom", "OpenTime", "started_at"))
    if started_at is None:
        raise ValueError("attendance record has no start timestamp")

    ended_at = parse_datetime(first_value(record, "dateTo", "CloseTime", "ended_at"))
    start_local = started_at.astimezone(MOSCOW_TZ)
    work_date = start_local.date()
    quality_status = "ok"
    notes: list[str] = []

    if ended_at is None:
        close_local = datetime.combine(
            work_date,
            rules.open_shift_auto_close_time,
            tzinfo=MOSCOW_TZ,
        )
        cap_local = start_local + timedelta(hours=rules.open_shift_cap_hours)
        end_local = min(close_local, cap_local)
        if end_local <= start_local:
            quality_status = "quality_review"
            notes.append("open_shift_auto_close_before_start")
            ended_at = started_at
        else:
            notes.append("open_shift_auto_closed")
            ended_at = end_local.astimezone(UTC)
    else:
        end_local = ended_at.astimezone(MOSCOW_TZ)
        if end_local.date() != work_date:
            quality_status = "quality_review"
            notes.append("cross_midnight")

    minutes_worked = max(0, int((ended_at - started_at).total_seconds() // 60))
    if minutes_worked > rules.open_shift_cap_hours * 60 and "open_shift_auto_closed" not in notes:
        quality_status = "quality_review"
        notes.append("duration_over_12h")
    if ended_at < started_at:
        quality_status = "quality_review"
        notes.append("ended_before_started")
        minutes_worked = 0

    role = first_text(record, "payroll_role", "payrollRole", "manual_role")
    if source != "iiko":
        role = role or first_text(record, "role")

    return AttendanceEntry(
        employee_id=employee.id,
        period_id=period.id,
        work_date=work_date,
        started_at=started_at,
        ended_at=ended_at,
        minutes_worked=minutes_worked,
        station=first_text(record, "station", "payroll_station", "payrollStation") or None,
        role=role or None,
        source=source,
        quality_status=quality_status,
        notes=";".join(notes) if notes else None,
    )


def is_work_attendance(record: Mapping[str, Any]) -> bool:
    attendance_type = first_text(record, "attendanceType", "attendance_type", "type", "code")
    if not attendance_type:
        return True
    return attendance_type.casefold() in {item.casefold() for item in WORK_ATTENDANCE_TYPES}


def parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.casefold() in {"none", "null", "nan"}:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MOSCOW_TZ)
    return parsed.astimezone(UTC)


def parse_time_value(value: str) -> time:
    hour_text, minute_text = value.strip().split(":", 1)
    return time(int(hour_text), int(minute_text))


def first_value(record: Mapping[str, Any], *keys: str) -> Any:
    lowered = {str(key).casefold(): value for key, value in record.items()}
    for key in keys:
        if key in record:
            return record[key]
        lowered_value = lowered.get(key.casefold())
        if lowered_value is not None:
            return lowered_value
    return None


def first_text(record: Mapping[str, Any], *keys: str) -> str:
    value = first_value(record, *keys)
    if value is None:
        return ""
    text = str(value).replace("\xa0", " ").strip()
    if text.casefold() in {"none", "null", "nan"}:
        return ""
    return text


def load_export_employees_module():
    current = Path(__file__).resolve()
    candidate_roots = [parent for parent in current.parents if (parent / "scripts/iiko").exists()]
    candidate_roots.extend(
        [Path("/app"), Path.cwd(), Path.cwd().parent, Path.cwd().parent.parent]
    )
    for root in candidate_roots:
        script_dir = root / "scripts/iiko"
        if not (script_dir / "export_employees.py").exists():
            continue
        script_dir_str = str(script_dir)
        if script_dir_str not in sys.path:
            sys.path.insert(0, script_dir_str)
        return importlib.import_module("export_employees")
    raise RuntimeError("scripts/iiko/export_employees.py is not available")

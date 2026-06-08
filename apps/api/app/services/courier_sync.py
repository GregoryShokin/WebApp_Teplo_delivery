from __future__ import annotations

import importlib
import sys
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AgentAction,
    AgentRun,
    AttendanceEntry,
    DeliveryOrder,
    Employee,
    ShiftLedgerEntry,
)
from app.services.iiko_sync import (
    _candidate_project_roots,
    _load_source_credential_env,
    _request_iiko_with_incomplete_read_retry,
)

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
ACTIVE_DEPARTMENT_ID = "d8d4a22e-3abd-4f02-b82d-7d4712f32729"
ACTIVE_DEPARTMENT_MARKER = "черников"
COURIER_SERVICE_TYPE = "COURIER"
VALID_RUN_REASONS = {"manual", "hot", "cold_backfill"}

OLAP_GROUP_ROW_FIELDS = [
    "Department.Id",
    "Department",
    "Delivery.ServiceType",
    "UniqOrderId.Id",
    "Delivery.Id",
    "OrderNum",
    "OpenDate.Typed",
    "OpenTime",
    "Delivery.SendTime",
    "Delivery.ActualTime",
    "Delivery.CloseTime",
    "CloseTime",
    "Delivery.WayDuration",
    "Delivery.Courier.Id",
    "OrderDeleted",
    "Delivery.CancelCause",
]
OLAP_AGGREGATE_FIELDS = ["DishDiscountSumInt"]
OLAP_EXPECTED_FIELDS = [*OLAP_GROUP_ROW_FIELDS, *OLAP_AGGREGATE_FIELDS]


@dataclass(slots=True)
class CourierSyncResult:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "skipped": self.skipped,
            "errors": self.errors,
        }


@dataclass(frozen=True, slots=True)
class CourierDeliveryRecord:
    iiko_order_id: str
    order_number: str | None
    work_date: date
    status: str | None
    service_type: str | None
    courier_iiko_id: str | None
    opened_at: datetime | None
    on_way_at: datetime | None
    closed_at: datetime | None
    taken_at: datetime | None
    delivered_at: datetime | None
    way_duration_minutes: Decimal | None
    revenue: Decimal | None
    raw: dict[str, Any]


@dataclass(slots=True)
class CourierDeliveryParseResult:
    records: list[CourierDeliveryRecord] = field(default_factory=list)
    skipped: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)


def _load_export_orders_delivery_module() -> ModuleType:
    for root in _candidate_project_roots():
        script_dir = root / "integrations/iiko/scripts"
        if not (script_dir / "export_orders_delivery.py").exists():
            continue
        script_dir_str = str(script_dir)
        if script_dir_str not in sys.path:
            sys.path.insert(0, script_dir_str)
        return importlib.import_module("export_orders_delivery")
    raise RuntimeError("integrations/iiko/scripts/export_orders_delivery.py is not available")


def fetch_iiko_courier_delivery_rows(start_date: date, end_date: date) -> list[Mapping[str, Any]]:
    export_orders_delivery = _load_export_orders_delivery_module()
    export_orders_delivery.load_local_env()
    client = export_orders_delivery.IikoClient()

    all_rows: list[Mapping[str, Any]] = []
    for chunk_start, chunk_end in iter_month_chunks(start_date, end_date):
        _status, data = _request_iiko_with_incomplete_read_retry(
            client,
            "/reports/olap",
            params={
                "report": "DELIVERIES",
                "summary": "false",
                "from": format_iiko_olap_date(chunk_start),
                "to": format_iiko_olap_date(chunk_end),
                "groupRow": OLAP_GROUP_ROW_FIELDS,
                "agr": OLAP_AGGREGATE_FIELDS,
            },
        )
        all_rows.extend(
            export_orders_delivery.parse_rows(data, expected_fields=OLAP_EXPECTED_FIELDS)
        )
    return all_rows


def parse_iiko_olap_payload(data: bytes) -> list[Mapping[str, Any]]:
    export_orders_delivery = _load_export_orders_delivery_module()
    return export_orders_delivery.parse_rows(data, expected_fields=OLAP_EXPECTED_FIELDS)


async def sync_courier_deliveries(
    session: AsyncSession,
    *,
    date_from: date,
    date_to: date,
    run_reason: str,
    iiko_rows: Iterable[Mapping[str, Any]] | None = None,
) -> CourierSyncResult:
    if run_reason not in VALID_RUN_REASONS:
        raise ValueError("run_reason must be one of manual, hot, cold_backfill")
    if date_from > date_to:
        raise ValueError("date_from must be before or equal to date_to")

    agent_run = AgentRun(
        agent_name="iiko_courier_delivery_sync",
        status="running",
        params={
            "reason": run_reason,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "report": "DELIVERIES",
            "groupRow": OLAP_GROUP_ROW_FIELDS,
            "agr": OLAP_AGGREGATE_FIELDS,
            "service_type_filter": COURIER_SERVICE_TYPE,
            "department_filter": ACTIVE_DEPARTMENT_MARKER,
        },
        result={},
    )
    session.add(agent_run)
    await session.flush()

    try:
        await _load_source_credential_env(session)
        rows = (
            list(iiko_rows)
            if iiko_rows is not None
            else await anyio.to_thread.run_sync(
                fetch_iiko_courier_delivery_rows,
                date_from,
                date_to,
            )
        )
        parse_result = parse_courier_delivery_rows(rows)
        result = await _upsert_courier_delivery_records(
            session,
            parse_result.records,
            agent_run.id,
        )
        result.skipped += parse_result.skipped
        result.errors += len(parse_result.errors)
        _add_parse_error_actions(session, agent_run.id, parse_result.errors)
        session.add(
            AgentAction(
                agent_run_id=agent_run.id,
                action_type="sync_summary",
                target_table="delivery_order",
                before_value=None,
                after_value=result.as_dict(),
            )
        )
        agent_run.status = "success" if result.errors == 0 else "partial"
        agent_run.finished_at = datetime.now(UTC)
        agent_run.result = result.as_dict()
        await session.commit()
        return result
    except Exception as exc:
        agent_run.status = "failed"
        agent_run.finished_at = datetime.now(UTC)
        agent_run.result = {"error": str(exc)[:500]}
        await session.commit()
        raise


async def sync_courier_hot_window(session: AsyncSession) -> CourierSyncResult:
    today = datetime.now(MOSCOW_TZ).date()
    return await sync_courier_deliveries(
        session,
        date_from=today - timedelta(days=1),
        date_to=today,
        run_reason="hot",
    )


async def sync_courier_cold_backfill(session: AsyncSession) -> CourierSyncResult:
    today = datetime.now(MOSCOW_TZ).date()
    return await sync_courier_deliveries(
        session,
        date_from=today - timedelta(days=30),
        date_to=today,
        run_reason="cold_backfill",
    )


async def list_courier_deliveries(
    session: AsyncSession,
    *,
    date_from: date,
    date_to: date,
    courier_iiko_id: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    query = select(DeliveryOrder).where(
        DeliveryOrder.work_date >= date_from,
        DeliveryOrder.work_date <= date_to,
    )
    if courier_iiko_id:
        query = query.where(DeliveryOrder.courier_iiko_id == courier_iiko_id)
    query = (
        query.order_by(
            DeliveryOrder.work_date,
            DeliveryOrder.opened_at,
            DeliveryOrder.order_number,
            DeliveryOrder.iiko_order_id,
        )
        .limit(limit)
        .offset(offset)
    )
    result = await session.scalars(query)
    return [serialize_delivery_order(order) for order in result.all()]


async def list_courier_shifts(
    session: AsyncSession,
    *,
    date_from: date,
    date_to: date,
    employee_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    ledger_query = (
        select(ShiftLedgerEntry, Employee)
        .join(Employee, Employee.id == ShiftLedgerEntry.employee_id)
        .where(
            ShiftLedgerEntry.work_date >= date_from,
            ShiftLedgerEntry.work_date <= date_to,
            Employee.position == "Курьер",
        )
        .order_by(ShiftLedgerEntry.work_date, ShiftLedgerEntry.opened_at, Employee.full_name)
    )
    attendance_query = (
        select(AttendanceEntry, Employee)
        .join(Employee, Employee.id == AttendanceEntry.employee_id)
        .where(
            AttendanceEntry.work_date >= date_from,
            AttendanceEntry.work_date <= date_to,
            Employee.position == "Курьер",
        )
        .order_by(AttendanceEntry.work_date, AttendanceEntry.started_at, Employee.full_name)
    )
    if employee_id is not None:
        ledger_query = ledger_query.where(ShiftLedgerEntry.employee_id == employee_id)
        attendance_query = attendance_query.where(AttendanceEntry.employee_id == employee_id)

    ledger_rows = (await session.execute(ledger_query)).all()
    attendance_rows = (await session.execute(attendance_query)).all()

    ledger_keys = {
        (entry.employee_id, entry.work_date, entry.opened_at) for entry, _employee in ledger_rows
    }
    serialized = [
        serialize_courier_ledger_shift(entry, employee) for entry, employee in ledger_rows
    ]
    for attendance, employee in attendance_rows:
        key = (attendance.employee_id, attendance.work_date, attendance.started_at)
        if key in ledger_keys:
            continue
        serialized.append(serialize_courier_attendance_shift(attendance, employee))
    serialized.sort(
        key=lambda item: (
            item["work_date"] or "",
            item["opened_at"] or "",
            item["employee_name"] or "",
        )
    )
    return serialized


def parse_courier_delivery_rows(
    rows: Iterable[Mapping[str, Any]],
) -> CourierDeliveryParseResult:
    result = CourierDeliveryParseResult()
    for index, row in enumerate(rows):
        try:
            parsed = parse_courier_delivery_row(row)
        except Exception as exc:
            result.errors.append(
                {
                    "row_index": index,
                    "error": str(exc)[:500],
                    "raw": _jsonable(dict(row)),
                }
            )
            continue
        if parsed is None:
            result.skipped += 1
            continue
        result.records.append(parsed)
    return result


def parse_courier_delivery_row(row: Mapping[str, Any]) -> CourierDeliveryRecord | None:
    if not is_active_department_row(row):
        return None

    service_type = first_text(row, "Delivery.ServiceType", "service_type") or None
    if (service_type or "").casefold() != COURIER_SERVICE_TYPE.casefold():
        return None

    order_number = first_text(row, "OrderNum", "Delivery.Number", "order_number") or None
    iiko_order_id = (
        first_text(row, "UniqOrderId.Id", "Delivery.Id", "iiko_order_id", "order_id")
        or order_number
    )
    if not iiko_order_id:
        return None

    opened_at = parse_datetime_value(first_value(row, "OpenTime", "opened_at"))
    on_way_at = parse_datetime_value(
        first_value(row, "Delivery.SendTime", "on_way_at", "send_time")
    )
    actual_at = parse_datetime_value(first_value(row, "Delivery.ActualTime", "actual_time"))
    closed_at = (
        actual_at
        or parse_datetime_value(first_value(row, "Delivery.CloseTime", "delivery_closed_at"))
        or parse_datetime_value(first_value(row, "CloseTime", "closed_at"))
    )
    work_date = parse_date_value(first_value(row, "OpenDate.Typed", "work_date")) or (
        opened_at.astimezone(MOSCOW_TZ).date() if opened_at is not None else None
    )
    if work_date is None:
        return None

    way_duration = parse_decimal_value(
        first_value(row, "Delivery.WayDuration", "way_duration_minutes")
    )
    if way_duration is None and on_way_at is not None and closed_at is not None:
        way_duration = decimal_minutes_between(on_way_at, closed_at)

    status_value = first_text(
        row,
        "Delivery.Status",
        "Order.Status",
        "OrderStatus",
        "Status",
        "status",
    )
    status = status_value or derive_delivery_status(row, on_way_at=on_way_at, closed_at=closed_at)
    revenue = parse_decimal_value(
        first_value(row, "DishDiscountSumInt", "revenue", "sumAfterDiscountWithoutVAT")
    )

    return CourierDeliveryRecord(
        iiko_order_id=iiko_order_id,
        order_number=order_number,
        work_date=work_date,
        status=status,
        service_type=service_type,
        courier_iiko_id=first_text(row, "Delivery.Courier.Id", "courier_iiko_id") or None,
        opened_at=opened_at,
        on_way_at=on_way_at,
        closed_at=closed_at,
        taken_at=on_way_at,
        delivered_at=closed_at,
        way_duration_minutes=way_duration,
        revenue=revenue,
        raw=_jsonable(dict(row)),
    )


async def _upsert_courier_delivery_records(
    session: AsyncSession,
    records: list[CourierDeliveryRecord],
    agent_run_id: uuid.UUID,
) -> CourierSyncResult:
    result = CourierSyncResult()
    if not records:
        return result

    record_by_id = {record.iiko_order_id: record for record in records}
    existing = (
        await session.scalars(
            select(DeliveryOrder).where(DeliveryOrder.iiko_order_id.in_(list(record_by_id)))
        )
    ).all()
    existing_by_iiko_id = {order.iiko_order_id: order for order in existing}
    now = datetime.now(UTC)

    for record in record_by_id.values():
        order = existing_by_iiko_id.get(record.iiko_order_id)
        before = serialize_delivery_order(order, include_raw=True) if order is not None else None
        if order is None:
            order = DeliveryOrder(id=uuid.uuid4(), iiko_order_id=record.iiko_order_id)
            order.created_at = now
            session.add(order)
            result.inserted += 1
            action_type = "insert"
        else:
            result.updated += 1
            action_type = "update"

        apply_delivery_record(order, record, now=now)
        session.add(
            AgentAction(
                agent_run_id=agent_run_id,
                action_type=action_type,
                target_table="delivery_order",
                target_id=order.id,
                before_value=before,
                after_value=serialize_delivery_order(order, include_raw=True),
            )
        )

    await session.flush()
    return result


def apply_delivery_record(
    order: DeliveryOrder,
    record: CourierDeliveryRecord,
    *,
    now: datetime,
) -> None:
    order.order_number = record.order_number
    order.work_date = record.work_date
    order.status = record.status
    order.service_type = record.service_type
    order.courier_iiko_id = record.courier_iiko_id
    order.opened_at = record.opened_at
    order.on_way_at = record.on_way_at
    order.closed_at = record.closed_at
    order.taken_at = record.taken_at
    order.delivered_at = record.delivered_at
    order.way_duration_minutes = record.way_duration_minutes
    order.revenue = record.revenue
    order.raw = record.raw
    order.updated_at = now


def _add_parse_error_actions(
    session: AsyncSession,
    agent_run_id: uuid.UUID,
    errors: list[dict[str, Any]],
) -> None:
    for error in errors:
        session.add(
            AgentAction(
                agent_run_id=agent_run_id,
                action_type="parse_error",
                target_table="delivery_order",
                target_id=None,
                before_value=None,
                after_value=error,
            )
        )


def serialize_delivery_order(order: DeliveryOrder, *, include_raw: bool = False) -> dict[str, Any]:
    payload = {
        "id": str(order.id) if order.id is not None else None,
        "iiko_order_id": order.iiko_order_id,
        "order_number": order.order_number,
        "work_date": order.work_date.isoformat() if order.work_date is not None else None,
        "status": order.status,
        "service_type": order.service_type,
        "courier_iiko_id": order.courier_iiko_id,
        "opened_at": order.opened_at.isoformat() if order.opened_at is not None else None,
        "on_way_at": order.on_way_at.isoformat() if order.on_way_at is not None else None,
        "closed_at": order.closed_at.isoformat() if order.closed_at is not None else None,
        "taken_at": order.taken_at.isoformat() if order.taken_at is not None else None,
        "delivered_at": (
            order.delivered_at.isoformat() if order.delivered_at is not None else None
        ),
        "way_duration_minutes": (
            str(order.way_duration_minutes) if order.way_duration_minutes is not None else None
        ),
        "revenue": str(order.revenue) if order.revenue is not None else None,
        "created_at": order.created_at.isoformat() if order.created_at is not None else None,
        "updated_at": order.updated_at.isoformat() if order.updated_at is not None else None,
    }
    if include_raw:
        payload["raw"] = _jsonable(order.raw or {})
    return payload


def serialize_courier_ledger_shift(
    entry: ShiftLedgerEntry,
    employee: Employee,
) -> dict[str, Any]:
    return {
        "id": str(entry.id) if entry.id is not None else None,
        "source_table": "shift_ledger_entry",
        "ledger_entry_id": str(entry.id) if entry.id is not None else None,
        "attendance_entry_id": None,
        "work_date": entry.work_date.isoformat() if entry.work_date is not None else None,
        "employee_id": str(entry.employee_id) if entry.employee_id is not None else None,
        "employee_iiko_id": employee.iiko_id,
        "employee_name": employee.full_name,
        "opened_at": entry.opened_at.isoformat() if entry.opened_at is not None else None,
        "closed_at": entry.closed_at.isoformat() if entry.closed_at is not None else None,
        "minutes_worked": minutes_between(entry.opened_at, entry.closed_at),
        "payroll_role": entry.payroll_role,
        "category": entry.category,
        "source": entry.source,
        "is_resolved": entry.is_resolved,
        "quality_status": None,
        "notes": entry.notes,
    }


def serialize_courier_attendance_shift(
    entry: AttendanceEntry,
    employee: Employee,
) -> dict[str, Any]:
    return {
        "id": str(entry.id) if entry.id is not None else None,
        "source_table": "attendance_entry",
        "ledger_entry_id": None,
        "attendance_entry_id": str(entry.id) if entry.id is not None else None,
        "work_date": entry.work_date.isoformat() if entry.work_date is not None else None,
        "employee_id": str(entry.employee_id) if entry.employee_id is not None else None,
        "employee_iiko_id": employee.iiko_id,
        "employee_name": employee.full_name,
        "opened_at": entry.started_at.isoformat() if entry.started_at is not None else None,
        "closed_at": entry.ended_at.isoformat() if entry.ended_at is not None else None,
        "minutes_worked": entry.minutes_worked,
        "payroll_role": entry.role,
        "category": None,
        "source": entry.source,
        "is_resolved": False,
        "quality_status": entry.quality_status,
        "notes": entry.notes,
    }


def is_active_department_row(row: Mapping[str, Any]) -> bool:
    department_id = first_text(row, "Department.Id", "department_id")
    department = first_text(row, "Department", "department")
    if department_id and department_id.casefold() == ACTIVE_DEPARTMENT_ID.casefold():
        return True
    if department:
        return ACTIVE_DEPARTMENT_MARKER in department.casefold()
    return not department_id


def derive_delivery_status(
    row: Mapping[str, Any],
    *,
    on_way_at: datetime | None,
    closed_at: datetime | None,
) -> str | None:
    if first_text(row, "Delivery.CancelCause", "cancel_cause") or is_truthy(
        first_value(row, "OrderDeleted", "order_deleted", "Storned")
    ):
        return "Cancelled"
    if parse_datetime_value(first_value(row, "Delivery.ActualTime", "actual_time")) is not None:
        return "Delivered"
    if closed_at is not None:
        return "Closed"
    if on_way_at is not None:
        return "OnWay"
    return "Unconfirmed"


def iter_month_chunks(start_date: date, end_date: date) -> Iterable[tuple[date, date]]:
    current = start_date
    while current <= end_date:
        if current.month == 12:
            next_month = date(current.year + 1, 1, 1)
        else:
            next_month = date(current.year, current.month + 1, 1)
        chunk_end = min(end_date, next_month - timedelta(days=1))
        yield current, chunk_end
        current = chunk_end + timedelta(days=1)


def format_iiko_olap_date(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def first_value(row: Mapping[str, Any], *keys: str) -> Any:
    lowered = {str(key).casefold(): value for key, value in row.items()}
    for key in keys:
        if key in row:
            return row[key]
        lowered_value = lowered.get(key.casefold())
        if lowered_value is not None:
            return lowered_value
    return None


def first_text(row: Mapping[str, Any], *keys: str) -> str:
    value = first_value(row, *keys)
    if value is None:
        return ""
    text = str(value).replace("\xa0", " ").strip()
    if text.casefold() in {"", "none", "null", "nan", "(пусто)"}:
        return ""
    return text


def parse_date_value(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(MOSCOW_TZ).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value

    text = clean_text(value)
    if not text:
        return None
    datetime_value = parse_datetime_value(text)
    if datetime_value is not None:
        return datetime_value.astimezone(MOSCOW_TZ).date()
    return None


def parse_datetime_value(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time(), tzinfo=MOSCOW_TZ)
    else:
        text = clean_text(value)
        if not text:
            return None
        parsed = parse_iso_datetime(text) or parse_iiko_java_datetime(text)
        if parsed is None:
            parsed = parse_common_datetime(text)
        if parsed is None:
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MOSCOW_TZ)
    return parsed.astimezone(UTC)


def parse_iso_datetime(text: str) -> datetime | None:
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def parse_common_datetime(text: str) -> datetime | None:
    for fmt in (
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=MOSCOW_TZ)
        except ValueError:
            continue
    return None


def parse_iiko_java_datetime(text: str) -> datetime | None:
    parts = text.split()
    if len(parts) != 6:
        return None
    _weekday, month_text, day_text, time_text, tz_text, year_text = parts
    month = {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }.get(month_text.casefold())
    if month is None:
        return None
    try:
        hour_text, minute_text, second_text = time_text.split(":", 2)
        tz = UTC if tz_text.casefold() in {"utc", "gmt", "z"} else MOSCOW_TZ
        return datetime(
            int(year_text),
            month,
            int(day_text),
            int(hour_text),
            int(minute_text),
            int(second_text),
            tzinfo=tz,
        )
    except ValueError:
        return None


def parse_decimal_value(value: Any) -> Decimal | None:
    text = clean_text(value)
    if not text:
        return None
    text = text.replace(" ", "").replace("\u2212", "-")
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    else:
        text = text.replace(",", ".")
    try:
        return Decimal(text).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None


def decimal_minutes_between(started_at: datetime, ended_at: datetime) -> Decimal:
    seconds = max(0, (ended_at - started_at).total_seconds())
    return (Decimal(str(seconds)) / Decimal("60")).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def minutes_between(started_at: datetime | None, ended_at: datetime | None) -> int | None:
    if started_at is None or ended_at is None:
        return None
    return max(0, int((ended_at - started_at).total_seconds() // 60))


def is_truthy(value: Any) -> bool:
    text = clean_text(value).casefold()
    return text in {
        "1",
        "true",
        "yes",
        "y",
        "да",
        "истина",
        "deleted",
        "удален",
        "удалён",
        "storned",
    }


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\xa0", " ").strip()
    if text.casefold() in {"", "none", "null", "nan"}:
        return ""
    return text


def _jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value

from __future__ import annotations

import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.models import (
    CourierIikoShift,
    CourierScheduleEntry,
    CourierShiftMatchStatus,
)
from app.services.couriers.shift_matching import build_shift_matches

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def test_shift_matching_covers_plan_fact_status_matrix_and_open_shift() -> None:
    courier_id = uuid.uuid4()
    schedules = [
        schedule(1, courier_id, date(2026, 6, 1), category="primary"),
        schedule(2, courier_id, date(2026, 6, 2), category="primary"),
        schedule(3, courier_id, date(2026, 6, 3), category="primary"),
        schedule(4, courier_id, date(2026, 6, 7), category="secondary"),
    ]
    shifts = [
        shift(1, courier_id, date(2026, 6, 1), opened_hour=10, opened_minute=5, hours=8),
        shift(2, courier_id, date(2026, 6, 2), opened_hour=10, hours=1),
        shift(4, courier_id, date(2026, 6, 4), opened_hour=12, hours=3),
        shift(5, courier_id, date(2026, 6, 5), opened_hour=12, hours=3),
        shift(6, courier_id, date(2026, 6, 6), opened_hour=12, minutes=30),
        shift(7, courier_id, date(2026, 6, 7), opened_hour=10, closed=False),
    ]
    matches = build_shift_matches(
        schedules=schedules,
        shifts=shifts,
        delivery_counts={
            (courier_id, date(2026, 6, 4)): 1,
            (courier_id, date(2026, 6, 5)): 0,
        },
        from_date=date(2026, 6, 1),
        to_date=date(2026, 6, 7),
    )

    by_date = {match.work_date: match for match in matches}
    assert by_date[date(2026, 6, 1)].status == CourierShiftMatchStatus.MATCHED_PRIMARY
    assert by_date[date(2026, 6, 1)].late_minutes == 5
    assert by_date[date(2026, 6, 1)].worked_minutes == 480
    assert by_date[date(2026, 6, 2)].status == CourierShiftMatchStatus.SHORT_PRIMARY
    assert by_date[date(2026, 6, 3)].status == CourierShiftMatchStatus.NO_SHOW_PRIMARY
    assert by_date[date(2026, 6, 4)].status == CourierShiftMatchStatus.HELPING
    assert by_date[date(2026, 6, 4)].deliveries_count == 1
    assert by_date[date(2026, 6, 5)].status == CourierShiftMatchStatus.HELPING
    assert by_date[date(2026, 6, 5)].schedule_entry_id is None
    assert date(2026, 6, 6) not in by_date
    assert by_date[date(2026, 6, 7)].status == CourierShiftMatchStatus.SHORT_SECONDARY
    assert by_date[date(2026, 6, 7)].worked_minutes is None
    assert len(matches) == 6


def schedule(
    row_id: int,
    courier_id: uuid.UUID,
    work_date: date,
    *,
    category: str,
) -> CourierScheduleEntry:
    return CourierScheduleEntry(
        id=row_id,
        courier_employee_id=courier_id,
        work_date=work_date,
        category=category,
        planned_start_at=datetime(
            work_date.year,
            work_date.month,
            work_date.day,
            10,
            tzinfo=MOSCOW_TZ,
        ),
        planned_end_at=datetime(
            work_date.year,
            work_date.month,
            work_date.day,
            22,
            tzinfo=MOSCOW_TZ,
        ),
        created_by=uuid.uuid4(),
    )


def shift(
    row_id: int,
    courier_id: uuid.UUID,
    work_date: date,
    *,
    opened_hour: int,
    opened_minute: int = 0,
    hours: int = 0,
    minutes: int = 0,
    closed: bool = True,
) -> CourierIikoShift:
    opened_at = datetime(
        work_date.year,
        work_date.month,
        work_date.day,
        opened_hour,
        opened_minute,
        tzinfo=MOSCOW_TZ,
    )
    closed_at = (
        opened_at.replace(hour=opened_at.hour + hours, minute=opened_at.minute + minutes)
        if closed
        else None
    )
    return CourierIikoShift(
        id=row_id,
        iiko_employee_id=f"iiko-{courier_id}",
        employee_id=courier_id,
        iiko_role_id="courier-role",
        opened_at=opened_at,
        closed_at=closed_at,
        attendance_type="P",
        raw_payload={},
    )

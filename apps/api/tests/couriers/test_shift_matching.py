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

NOW = datetime(2026, 6, 11, 12, 0, tzinfo=MOSCOW_TZ)
TODAY = date(2026, 6, 11)
YESTERDAY = date(2026, 6, 10)
TOMORROW = date(2026, 6, 12)


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
        now=datetime(2026, 6, 30, 12, tzinfo=MOSCOW_TZ),
    )

    by_date = {match.work_date: match for match in matches}
    assert by_date[date(2026, 6, 1)].status == CourierShiftMatchStatus.MATCHED_PRIMARY
    assert by_date[date(2026, 6, 1)].late_minutes == 5
    assert by_date[date(2026, 6, 1)].worked_minutes == 480
    assert by_date[date(2026, 6, 2)].status == CourierShiftMatchStatus.SHORT_PRIMARY
    assert by_date[date(2026, 6, 3)].status == CourierShiftMatchStatus.NO_SHOW_PRIMARY
    assert by_date[date(2026, 6, 4)].status == CourierShiftMatchStatus.HELPING
    assert by_date[date(2026, 6, 4)].deliveries_count == 1
    assert by_date[date(2026, 6, 4)].schedule_entry_id is None
    # 6/5: помощь без доставок (0) — match не создаётся (правило ≥1 доставку)
    assert date(2026, 6, 5) not in by_date
    # 6/6: closed shift < 120 мин без плана — не записываем
    assert date(2026, 6, 6) not in by_date
    # 6/7: open shift по плану — пока не закрыта, статус не выставляем
    assert date(2026, 6, 7) not in by_date
    assert len(matches) == 4


def test_plan_primary_no_fact_today_skips_match() -> None:
    courier_id = uuid.uuid4()
    matches = build_shift_matches(
        schedules=[schedule(1, courier_id, TODAY, category="primary")],
        shifts=[],
        delivery_counts={},
        from_date=TODAY,
        to_date=TODAY,
        now=NOW,
    )
    assert matches == []


def test_plan_primary_no_fact_future_skips_match() -> None:
    courier_id = uuid.uuid4()
    matches = build_shift_matches(
        schedules=[schedule(1, courier_id, TOMORROW, category="primary")],
        shifts=[],
        delivery_counts={},
        from_date=TOMORROW,
        to_date=TOMORROW,
        now=NOW,
    )
    assert matches == []


def test_plan_primary_open_shift_today_skips_match() -> None:
    courier_id = uuid.uuid4()
    matches = build_shift_matches(
        schedules=[schedule(1, courier_id, TODAY, category="primary")],
        shifts=[shift(1, courier_id, TODAY, opened_hour=10, closed=False)],
        delivery_counts={},
        from_date=TODAY,
        to_date=TODAY,
        now=NOW,
    )
    assert matches == []


def test_plan_primary_closed_short_shift_yesterday_is_short() -> None:
    courier_id = uuid.uuid4()
    matches = build_shift_matches(
        schedules=[schedule(1, courier_id, YESTERDAY, category="primary")],
        shifts=[shift(1, courier_id, YESTERDAY, opened_hour=10, hours=1)],
        delivery_counts={},
        from_date=YESTERDAY,
        to_date=YESTERDAY,
        now=NOW,
    )
    assert len(matches) == 1
    assert matches[0].status == CourierShiftMatchStatus.SHORT_PRIMARY
    assert matches[0].worked_minutes == 60


def test_no_plan_open_shift_today_skips_match() -> None:
    courier_id = uuid.uuid4()
    matches = build_shift_matches(
        schedules=[],
        shifts=[shift(1, courier_id, TODAY, opened_hour=10, closed=False)],
        delivery_counts={},
        from_date=TODAY,
        to_date=TODAY,
        now=NOW,
    )
    assert matches == []


def test_no_plan_closed_shift_with_deliveries_is_helping() -> None:
    courier_id = uuid.uuid4()
    matches = build_shift_matches(
        schedules=[],
        shifts=[shift(1, courier_id, YESTERDAY, opened_hour=10, hours=3)],
        delivery_counts={(courier_id, YESTERDAY): 5},
        from_date=YESTERDAY,
        to_date=YESTERDAY,
        now=NOW,
    )
    assert len(matches) == 1
    assert matches[0].status == CourierShiftMatchStatus.HELPING
    assert matches[0].deliveries_count == 5


def test_no_plan_closed_shift_without_deliveries_skips_match() -> None:
    courier_id = uuid.uuid4()
    matches = build_shift_matches(
        schedules=[],
        shifts=[shift(1, courier_id, YESTERDAY, opened_hour=10, hours=3)],
        delivery_counts={(courier_id, YESTERDAY): 0},
        from_date=YESTERDAY,
        to_date=YESTERDAY,
        now=NOW,
    )
    assert matches == []


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

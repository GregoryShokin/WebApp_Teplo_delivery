"""Кодирование времени для iiko Cloud и обучение сдвигу (без сети и БД).

Прод-баг 22.10.2026: Inventory API перестал вычитать offset из присланных цифр, а Тепло
продолжало прибавлять три часа — накладные ложились на 3 часа позже, 515385 — в другие сутки.
Finance API (акты услуг) при этом вычитает по-прежнему. Тесты держат оба семейства и то, что
сдвиг учится из факта, а не из догадки.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.services import iiko_cloud_datetime as icd

MSK = ZoneInfo("Europe/Moscow")


@pytest.fixture(autouse=True)
def _fresh_shifts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(icd, "_learned", {})


def test_inventory_sends_moscow_digits_unchanged() -> None:
    """Ч-195: 23.10.2026 13:08 МСК должно уйти 13:08, а не 16:08."""
    assert icd.encode_iiko_wall_time(datetime(2026, 10, 23, 13, 8, tzinfo=MSK), icd.INVENTORY) == (
        "2026-10-23T13:08:00.000+03:00"
    )


def test_finance_still_compensates_three_hours() -> None:
    """PROBE-A 25.10.2026: Finance API положил присланные 15:50+03:00 на 12:50 — значит, чтобы
    акт лёг на 13:08, слать надо 16:08."""
    assert icd.encode_iiko_wall_time(datetime(2026, 10, 23, 13, 8, tzinfo=MSK), icd.FINANCE) == (
        "2026-10-23T16:08:00.000+03:00"
    )


def test_aware_utc_and_milliseconds() -> None:
    moment = datetime(2026, 10, 25, 9, 50, 54, 630_000, tzinfo=UTC)
    assert icd.encode_iiko_wall_time(moment) == "2026-10-25T12:50:54.630+03:00"


def test_learns_shift_from_what_iiko_stored() -> None:
    assert icd.learn_iiko_shift(
        icd.INVENTORY,
        sent="2026-10-23T13:08:00.000+03:00",
        stored="2026-10-23T10:08:00.000+03:00",
    )
    assert icd.iiko_shift(icd.INVENTORY) == timedelta(hours=-3)
    assert icd.encode_iiko_wall_time(datetime(2026, 10, 23, 13, 8)) == (
        "2026-10-23T16:08:00.000+03:00"
    )
    # Семейства независимы: выученное для накладных не трогает акты.
    assert icd.iiko_shift(icd.FINANCE) == timedelta(hours=-3)
    assert icd.learn_iiko_shift(
        icd.FINANCE,
        sent="2026-10-23T16:08:00.000+03:00",
        stored="2026-10-23T16:08:00.000+03:00",
    )
    assert icd.iiko_shift(icd.FINANCE) == timedelta(0)
    assert icd.iiko_shift(icd.INVENTORY) == timedelta(hours=-3)


def test_does_not_learn_implausible_difference() -> None:
    """13:08 → 13:20 — не смена разбора времени, а чужая правка документа."""
    assert not icd.learn_iiko_shift(
        icd.INVENTORY,
        sent="2026-10-23T13:08:00.000+03:00",
        stored="2026-10-23T13:20:00.000+03:00",
    )
    assert not icd.learn_iiko_shift(
        icd.INVENTORY,
        sent="2026-10-23T13:08:00.000+03:00",
        stored="2026-10-25T13:08:00.000+03:00",
    )
    assert icd.iiko_shift(icd.INVENTORY) == timedelta(0)


def test_stored_date_in_other_offset_is_compared_as_moment() -> None:
    """iiko может вернуть ту же минуту в другом offset — это не промах."""
    assert icd.missed_wall_time(
        icd.INVENTORY,
        sent="2026-10-23T13:08:00.000+03:00",
        stored="2026-10-23T10:08:00.000+00:00",
        used=timedelta(0),
    ) is None


def test_missed_wall_time_names_target_and_learns() -> None:
    wanted = icd.missed_wall_time(
        icd.INVENTORY,
        sent="2026-10-23T22:04:00.000+03:00",
        stored="2026-10-24T01:04:00.000+03:00",
        used=timedelta(0),
    )
    assert wanted == datetime(2026, 10, 23, 22, 4)
    assert icd.iiko_shift(icd.INVENTORY) == timedelta(hours=3)


def test_ignores_milliseconds_kept_by_iiko() -> None:
    assert icd.missed_wall_time(
        icd.INVENTORY,
        sent="2026-10-25T12:50:54.630+03:00",
        stored="2026-10-25T12:50:54.000+03:00",
        used=timedelta(0),
    ) is None

"""Ночная авто-фиксация графика (страховка, если управляющий забыл «Зафиксировать»).

`auto_commit_living_schedule` в 00:00 МСК переводит живой график draft→published, чтобы к
началу дня учёт смен/план-факт читали зафиксированный график. Системная фиксация:
`published_by_user_id = None`. Идемпотентна и ничего не создаёт на пустой базе.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import ShiftSchedule, User
from app.services.shift_schedule_service import (
    LIVING_SCHEDULE_END,
    LIVING_SCHEDULE_START,
    auto_commit_living_schedule,
)


def _living(
    status: str,
    *,
    published_at: datetime | None = None,
    published_by_user_id: uuid.UUID | None = None,
) -> ShiftSchedule:
    return ShiftSchedule(
        id=uuid.uuid4(),
        date_start=LIVING_SCHEDULE_START,
        date_end=LIVING_SCHEDULE_END,
        status=status,
        published_at=published_at,
        published_by_user_id=published_by_user_id,
    )


def _make_user(session: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"manager-{uuid.uuid4()}@teplo.local",
        hashed_password="x",
        full_name="Управляющий",
    )
    session.add(user)
    return user


async def test_auto_commit_publishes_draft(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        schedule = _living("draft")
        session.add(schedule)
        await session.commit()
        schedule_id = schedule.id

        committed = await auto_commit_living_schedule(session)

    assert committed is not None
    assert committed.id == schedule_id
    assert committed.status == "published"
    assert committed.published_at is not None
    assert committed.published_by_user_id is None, "системная фиксация — без пользователя"


async def test_auto_commit_is_idempotent_for_published(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    published_at = datetime(2026, 7, 1, tzinfo=UTC)
    async with async_session_factory() as session:
        manager = _make_user(session)
        await session.flush()
        schedule = _living(
            "published", published_at=published_at, published_by_user_id=manager.id
        )
        original_user = schedule.published_by_user_id
        session.add(schedule)
        await session.commit()

        result = await auto_commit_living_schedule(session)

        assert result is None, "уже зафиксированный график не трогаем"
        refreshed = await session.get(ShiftSchedule, schedule.id)
        assert refreshed is not None
        assert refreshed.status == "published"
        assert refreshed.published_by_user_id == original_user, "ручную фиксацию не перетираем"


async def test_auto_commit_noop_on_empty_db(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        result = await auto_commit_living_schedule(session)

    assert result is None, "нет графиков — ничего не создаём"
    async with async_session_factory() as session:
        count = len((await session.scalars(select(ShiftSchedule))).all())
    assert count == 0


async def test_auto_commit_supersedes_legacy_and_publishes_living(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Legacy: несколько графиков. Канонический (свежий draft) фиксируется, прочие → superseded."""
    async with async_session_factory() as session:
        legacy = _living("draft")
        legacy.date_start = date(2025, 1, 1)
        legacy.date_end = date(2025, 12, 31)
        legacy.updated_at = datetime(2025, 1, 1, tzinfo=UTC)
        living = _living("draft")
        living.updated_at = datetime(2026, 7, 1, tzinfo=UTC)
        session.add_all([legacy, living])
        await session.commit()
        living_id, legacy_id = living.id, legacy.id

        committed = await auto_commit_living_schedule(session)

        assert committed is not None and committed.id == living_id
        assert committed.status == "published"
        legacy_refreshed = await session.get(ShiftSchedule, legacy_id)
        assert legacy_refreshed is not None
        assert legacy_refreshed.status == "superseded", "прочие графики уводятся в superseded"

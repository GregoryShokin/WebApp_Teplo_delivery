"""Ручной замок типа отношений (``relationship_manual``).

Контрагент с расходной (receivable) накладной авто-помечается «бартером» на каждом тике
iiko-синхронизации. Если владелец явно сменил тип в карточке, замок должен держать выбор —
иначе разовая/тестовая расходная бесконечно возвращала «Бартер» после каждой ручной правки.
"""

from __future__ import annotations

from cp_helpers import make_counterparty
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import CounterpartyPayableProfile
from app.services.counterparty_invoice_sync import _mark_relationship_barter
from app.services.counterparty_registry import update_profile


async def _relationship(session: AsyncSession, cp_id) -> tuple[str, bool]:
    profile = await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == cp_id
        )
    )
    assert profile is not None
    return profile.relationship, profile.relationship_manual


async def test_receivable_auto_marks_barter_when_unlocked(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Кола", inn="6100000001")
        await session.commit()

        # Расходная накладная из iiko-синхронизации → авто-бартер (замка нет).
        await _mark_relationship_barter(session, cp.id)
        await session.commit()

        relationship, manual = await _relationship(session, cp.id)
        assert relationship == "barter"
        assert manual is False


async def test_manual_official_survives_receivable_resync(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Кварц", inn="6100000002")
        await session.commit()

        # Сначала синхронизация сделала его бартером...
        await _mark_relationship_barter(session, cp.id)
        await session.commit()
        assert (await _relationship(session, cp.id))[0] == "barter"

        # ...владелец вручную вернул «официальный» — смена типа ставит замок.
        await update_profile(session, cp.id, relationship="official")
        relationship, manual = await _relationship(session, cp.id)
        assert relationship == "official"
        assert manual is True

        # Следующий тик синхронизации снова видит расходную — но замок держит выбор.
        await _mark_relationship_barter(session, cp.id)
        await session.commit()
        relationship, manual = await _relationship(session, cp.id)
        assert relationship == "official"
        assert manual is True


async def test_save_without_changing_type_does_not_lock(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Обычный", inn="6100000003")
        await session.commit()

        # Правка других полей карточки, тип оставили «официальным» — замок НЕ ставим,
        # чтобы случайно не «залочить» контрагента, которого синхронизация ещё не трогала.
        await update_profile(session, cp.id, relationship="official", manager_name="Иван")
        relationship, manual = await _relationship(session, cp.id)
        assert relationship == "official"
        assert manual is False

        # Значит авто-бартер по расходной по-прежнему может сработать.
        await _mark_relationship_barter(session, cp.id)
        await session.commit()
        assert (await _relationship(session, cp.id))[0] == "barter"

"""Тип отношений контрагента: кто и когда его меняет.

Бартерное партнёрство назначается ТОЛЬКО вручную (решение владельца 19.07.2026): карточка
контрагента или оформление явного займа. Прежняя авто-пометка по расходной накладной из
iiko-синка снята — расходная бывает и у обычного поставщика (возврат некондиции), а пометка
утаскивала всю его денежную кредиторку на вкладку «Бартер» товарным долгом.

Ручной замок (``relationship_manual``) остаётся значимым для оформления займа: если владелец
явно закрепил тип в карточке, оформление займа не перебивает его молча.
"""

from __future__ import annotations

from cp_helpers import make_counterparty
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import CounterpartyPayableProfile
from app.services.counterparty_registry import update_profile
from app.services.warehouse_invoices import _ensure_barter_relationship


async def _relationship(session: AsyncSession, cp_id) -> tuple[str, bool]:
    profile = await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == cp_id
        )
    )
    assert profile is not None
    return profile.relationship, profile.relationship_manual


async def test_barter_loan_marks_partner_when_unlocked(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Оформление явного займа — ручное действие владельца, оно и назначает партнёрство."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Кола", inn="6100000001")
        await session.commit()

        await _ensure_barter_relationship(session, cp.id)
        await session.commit()

        relationship, manual = await _relationship(session, cp.id)
        assert relationship == "barter"
        assert manual is False


async def test_manual_official_survives_barter_loan(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Явный выбор владельца в карточке держится: заём не перебивает закреплённый тип."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Кварц", inn="6100000002")
        await session.commit()

        await _ensure_barter_relationship(session, cp.id)
        await session.commit()
        assert (await _relationship(session, cp.id))[0] == "barter"

        # Владелец вручную вернул «официальный» — смена типа ставит замок.
        await update_profile(session, cp.id, relationship="official")
        relationship, manual = await _relationship(session, cp.id)
        assert relationship == "official"
        assert manual is True

        # Ещё один заём с этим контрагентом — замок держит выбор.
        await _ensure_barter_relationship(session, cp.id)
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
        # чтобы случайно не «залочить» контрагента, которого владелец типом не трогал.
        await update_profile(session, cp.id, relationship="official", manager_name="Иван")
        relationship, manual = await _relationship(session, cp.id)
        assert relationship == "official"
        assert manual is False

        # Значит оформление займа по-прежнему может назначить партнёрство.
        await _ensure_barter_relationship(session, cp.id)
        await session.commit()
        assert (await _relationship(session, cp.id))[0] == "barter"

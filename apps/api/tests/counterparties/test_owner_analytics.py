"""Собственник как ось аналитики: движение по «его» статьям обязано называть, чьё оно.

ЗАЧЕМ ПРАВИЛО. Собственников двое — Павел и Григорий (решение владельца 02.08.2026), и у
каждого свои расчёты с бизнесом. Пока проводка не называет имени, «поступление от
собственников» — общий котёл: сколько внёс каждый и сколько бизнес должен каждому, из него не
вынуть ни одним отчётом. Ошибка при этом молчит: деньги в ДДС сходятся, а персональные расчёты
не существуют вовсе.

Здесь закреплено ровно три вещи:

* без собственника такую статью провести нельзя;
* названный контрагент обязан числиться в РЕЕСТРЕ — роль в карточке решает не она;
* обратного запрета нет: собственник может появляться и на обычных статьях — человек бывает
  бизнесу и арендодателем, и подрядчиком, и решать за владельца, кем ещё он может быть, мы не
  вправе.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from cp_helpers import make_counterparty
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import BusinessOwner, CounterpartyRole, DdsArticle
from app.services.owner_analytics import (
    OWNER_ROLE,
    OwnerAnalyticsError,
    ensure_owner_context,
    list_owners,
    shares_total,
)


async def _article(session: AsyncSession, *, owner_required: bool) -> DdsArticle:
    article = DdsArticle(
        id=uuid.uuid4(),
        code=f"art_{uuid.uuid4().hex[:8]}",
        name="Поступление денег от собственников" if owner_required else "Прочие расходы",
        movement_type="inflow" if owner_required else "outflow",
        activity_type="financing" if owner_required else "operating",
        owner_required=owner_required,
    )
    session.add(article)
    await session.flush()
    return article


async def _owner(
    session: AsyncSession,
    name: str,
    *,
    share: str = "50",
    started_on: date = date(2026, 1, 1),
    ended_on: date | None = None,
):
    person = await make_counterparty(
        session, name=name, inn=None, cp_type="individual", relationship="informal"
    )
    session.add(CounterpartyRole(counterparty_id=person.id, role=OWNER_ROLE))
    session.add(
        BusinessOwner(
            counterparty_id=person.id,
            share_percent=Decimal(share),
            started_on=started_on,
            ended_on=ended_on,
        )
    )
    await session.flush()
    return person


async def test_owner_article_without_owner_is_refused(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Взнос без имени провести нельзя: иначе деньги двоих сложатся в общий котёл."""
    async with async_session_factory() as session:
        article = await _article(session, owner_required=True)

        with pytest.raises(OwnerAnalyticsError, match="укажите собственника"):
            await ensure_owner_context(session, article=article, counterparty_id=None)
        await session.rollback()


async def test_stranger_on_owner_article_is_refused(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Контрагент, которого нет в реестре собственников, на такой статье — отказ.

    Иначе взнос запишется на поставщика, и расчёты с ним поедут в противоположную сторону:
    у поставщика долг перед нами и долг перед ним считаются разными знаками.
    """
    async with async_session_factory() as session:
        article = await _article(session, owner_required=True)
        supplier = await make_counterparty(session, name="ООО «Поставщик»", inn="6143000001")

        with pytest.raises(OwnerAnalyticsError, match="не значится в реестре"):
            await ensure_owner_context(session, article=article, counterparty_id=supplier.id)
        await session.rollback()


async def test_owner_passes(async_session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Названный собственник проходит — и это всё, что от правила требуется."""
    async with async_session_factory() as session:
        article = await _article(session, owner_required=True)
        owner = await _owner(session, "Павел")

        await ensure_owner_context(session, article=article, counterparty_id=owner.id)
        await session.rollback()


async def test_owner_on_plain_article_is_allowed(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Обратного запрета нет — в отличие от помещения.

    Собственник бывает бизнесу и арендодателем, и подрядчиком. Запрет означал бы, что система
    решает за владельца, кем ещё этот человек может быть.
    """
    async with async_session_factory() as session:
        plain = await _article(session, owner_required=False)
        owner = await _owner(session, "Григорий")

        await ensure_owner_context(session, article=plain, counterparty_id=owner.id)
        await session.rollback()


async def test_registry_lists_only_owners(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Реестр — записи с долями, а посторонних контрагентов в нём нет."""
    async with async_session_factory() as session:
        await _owner(session, "Павел")
        await _owner(session, "Григорий")
        await make_counterparty(session, name="ООО «Поставщик»", inn="6143000002")

        owners = await list_owners(session)

        assert [row.counterparty.name for row in owners] == ["Григорий", "Павел"], "по алфавиту"
        # Доли — то, ради чего реестр отдельной таблицей: по ним делятся дивиденды.
        assert shares_total(owners) == Decimal("100")
        # Роль действительно проставлена, а не выведена из имени. Проверяем НАЛИЧИЕ роли, а не
        # единственность: тот же человек может быть бизнесу ещё и поставщиком, и запрещать это
        # мы не собирались — иначе пришлось бы заводить его вторым лицом и раскалывать расчёты.
        for row in owners:
            roles = set(
                (
                    await session.scalars(
                        select(CounterpartyRole.role).where(
                            CounterpartyRole.counterparty_id == row.counterparty.id
                        )
                    )
                ).all()
            )
            assert OWNER_ROLE in roles, row.counterparty.name
        await session.rollback()


async def test_role_without_registry_row_is_not_an_owner(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Роль в карточке собственником не делает — решает РЕЕСТР.

    Роль ставится руками мимо реестра и не несёт ни доли, ни даты входа. Признай её достаточной —
    и появится собственник без доли, которому нечего начислить, а ошибку заметят на дивидендах.
    """
    async with async_session_factory() as session:
        article = await _article(session, owner_required=True)
        person = await make_counterparty(
            session, name="Кто-то", inn=None, cp_type="individual", relationship="informal"
        )
        session.add(CounterpartyRole(counterparty_id=person.id, role=OWNER_ROLE))
        await session.flush()

        with pytest.raises(OwnerAnalyticsError, match="не значится в реестре"):
            await ensure_owner_context(session, article=article, counterparty_id=person.id)
        await session.rollback()


async def test_former_owner_cannot_receive_new_money(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Вышедший из состава больше не собственник — но из реестра не исчезает.

    После выхода человек остаётся в базе со всеми прошлыми расчётами. Без проверки даты новый
    взнос записался бы на того, кто уже не владеет, и доля перестала бы отвечать за деньги.
    """
    async with async_session_factory() as session:
        article = await _article(session, owner_required=True)
        former = await _owner(session, "Бывший", ended_on=date(2026, 5, 31))

        with pytest.raises(OwnerAnalyticsError, match="не значится в реестре"):
            await ensure_owner_context(session, article=article, counterparty_id=former.id)

        assert [row.counterparty.name for row in await list_owners(session)] == []
        assert [
            row.counterparty.name for row in await list_owners(session, include_former=True)
        ] == ["Бывший"]
        await session.rollback()

"""Аналитика расхода по помещению: статья ↔ помещение ↔ арендодатель.

Правило одно на все входы ДДС, поэтому проверяем его на уровне сервиса — так тест не
привязан к тому, через какой из шести входов пришёл расход.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Counterparty, DdsArticle, Location, LocationLease, Organization
from app.services.location_analytics import (
    LocationAnalyticsError,
    leases_for_location,
    resolve_location_context,
)

TODAY = date(2026, 7, 23)


async def _fixture(session: AsyncSession) -> tuple[DdsArticle, DdsArticle, Location, Counterparty]:
    organization_id = await session.scalar(select(Organization.id).limit(1))
    if organization_id is None:
        organization = Organization(id=uuid.uuid4(), name="Тест-организация")
        session.add(organization)
        await session.flush()
        organization_id = organization.id

    rent = DdsArticle(
        id=uuid.uuid4(),
        code=f"rent_{uuid.uuid4().hex[:8]}",
        name="Аренда тестовая",
        movement_type="outflow",
        activity_type="operating",
        location_required=True,
    )
    other = DdsArticle(
        id=uuid.uuid4(),
        code=f"other_{uuid.uuid4().hex[:8]}",
        name="Прочий расход",
        movement_type="outflow",
        activity_type="operating",
    )
    location = Location(
        id=uuid.uuid4(), organization_id=organization_id, name=f"Точка {uuid.uuid4().hex[:6]}"
    )
    landlord = Counterparty(
        id=uuid.uuid4(), name=f"Арендодатель {uuid.uuid4().hex[:6]}", type="individual"
    )
    session.add_all([rent, other, location, landlord])
    await session.flush()
    return rent, other, location, landlord


def test_rent_article_demands_location(async_session_factory) -> None:
    """Без помещения арендный расход бессмыслен: вопрос «сколько стоит точка» без ответа."""

    async def run() -> None:
        async with async_session_factory() as session:
            rent, _other, _location, _landlord = await _fixture(session)
            with pytest.raises(LocationAnalyticsError, match="укажите помещение"):
                await resolve_location_context(
                    session,
                    article=rent,
                    location_id=None,
                    lease_id=None,
                    counterparty_id=None,
                    on_date=TODAY,
                )
            await session.rollback()

    asyncio.run(run())


def test_location_on_plain_article_is_rejected(async_session_factory) -> None:
    """Помещение на статье без аналитики — мусор, как сотрудник на незарплатной статье."""

    async def run() -> None:
        async with async_session_factory() as session:
            _rent, other, location, _landlord = await _fixture(session)
            with pytest.raises(LocationAnalyticsError, match="не ведёт аналитику"):
                await resolve_location_context(
                    session,
                    article=other,
                    location_id=location.id,
                    lease_id=None,
                    counterparty_id=None,
                    on_date=TODAY,
                )
            await session.rollback()

    asyncio.run(run())


def test_location_without_lease_is_allowed(async_session_factory) -> None:
    """По арендной статье платят и вывоз мусора: помещение есть, договора нет."""

    async def run() -> None:
        async with async_session_factory() as session:
            rent, _other, location, landlord = await _fixture(session)
            context = await resolve_location_context(
                session,
                article=rent,
                location_id=location.id,
                lease_id=None,
                counterparty_id=landlord.id,
                on_date=TODAY,
            )
            assert context.location_id == location.id
            assert context.lease_id is None
            assert context.counterparty_id == landlord.id
            await session.rollback()

    asyncio.run(run())


def test_lease_bound_article_demands_landlord(async_session_factory) -> None:
    """Статья-аренда помещения: помещение выбрано, а договор — нет. Получателя у такой статьи
    нет иначе как через арендодателя, поэтому платёж без договора отклоняется (в отличие от
    коммуналки/охраны, где помещение без аренды допустимо — см. тест выше)."""

    async def run() -> None:
        async with async_session_factory() as session:
            rent, _other, location, landlord = await _fixture(session)
            rent.lease_bound = True
            with pytest.raises(LocationAnalyticsError, match="выберите арендодателя"):
                await resolve_location_context(
                    session,
                    article=rent,
                    location_id=location.id,
                    lease_id=None,
                    counterparty_id=landlord.id,
                    on_date=TODAY,
                )
            await session.rollback()

    asyncio.run(run())


def test_lease_bound_article_accepts_registered_landlord(async_session_factory) -> None:
    """С выбранным договором аренды статья-аренда пропускает платёж и подставляет арендодателя."""

    async def run() -> None:
        async with async_session_factory() as session:
            rent, _other, location, landlord = await _fixture(session)
            rent.lease_bound = True
            lease = LocationLease(
                id=uuid.uuid4(),
                location_id=location.id,
                counterparty_id=landlord.id,
                monthly_amount=Decimal("50000"),
                started_on=date(2026, 1, 1),
                dds_article_id=rent.id,
            )
            session.add(lease)
            await session.flush()

            context = await resolve_location_context(
                session,
                article=rent,
                location_id=location.id,
                lease_id=lease.id,
                counterparty_id=None,
                on_date=TODAY,
            )
            assert context.lease_id == lease.id
            assert context.counterparty_id == landlord.id
            await session.rollback()

    asyncio.run(run())


def test_lease_fills_landlord_and_rejects_alien_counterparty(async_session_factory) -> None:
    """Договор знает своего арендодателя — чужой контрагент по нему платить не может."""

    async def run() -> None:
        async with async_session_factory() as session:
            rent, _other, location, landlord = await _fixture(session)
            lease = LocationLease(
                id=uuid.uuid4(),
                location_id=location.id,
                counterparty_id=landlord.id,
                monthly_amount=Decimal("100000"),
                started_on=date(2026, 1, 1),
                dds_article_id=rent.id,
            )
            alien = Counterparty(id=uuid.uuid4(), name="Чужой контрагент", type="legal_entity")
            session.add_all([lease, alien])
            await session.flush()

            context = await resolve_location_context(
                session,
                article=rent,
                location_id=location.id,
                lease_id=lease.id,
                counterparty_id=None,
                on_date=TODAY,
            )
            assert context.counterparty_id == landlord.id
            assert context.lease_id == lease.id

            with pytest.raises(LocationAnalyticsError, match="не является арендодателем"):
                await resolve_location_context(
                    session,
                    article=rent,
                    location_id=location.id,
                    lease_id=lease.id,
                    counterparty_id=alien.id,
                    on_date=TODAY,
                )
            await session.rollback()

    asyncio.run(run())


def test_lease_outside_its_period_is_rejected(async_session_factory) -> None:
    """Платёж месяца, когда договор ещё не действовал, уехал бы не тому собственнику."""

    async def run() -> None:
        async with async_session_factory() as session:
            rent, _other, location, landlord = await _fixture(session)
            lease = LocationLease(
                id=uuid.uuid4(),
                location_id=location.id,
                counterparty_id=landlord.id,
                monthly_amount=Decimal("100000"),
                started_on=date(2026, 7, 1),
                ended_on=date(2026, 7, 31),
                dds_article_id=rent.id,
            )
            session.add(lease)
            await session.flush()

            with pytest.raises(LocationAnalyticsError, match="не действовал"):
                await resolve_location_context(
                    session,
                    article=rent,
                    location_id=location.id,
                    lease_id=lease.id,
                    counterparty_id=None,
                    on_date=date(2026, 6, 15),
                )
            await session.rollback()

    asyncio.run(run())


def test_lease_of_another_location_is_rejected(async_session_factory) -> None:
    """Договор соседней точки не должен закрывать платёж по этой."""

    async def run() -> None:
        async with async_session_factory() as session:
            rent, _other, location, landlord = await _fixture(session)
            organization_id = location.organization_id
            neighbour = Location(
                id=uuid.uuid4(),
                organization_id=organization_id,
                name=f"Соседняя {uuid.uuid4().hex[:6]}",
            )
            session.add(neighbour)
            await session.flush()
            lease = LocationLease(
                id=uuid.uuid4(),
                location_id=neighbour.id,
                counterparty_id=landlord.id,
                monthly_amount=Decimal("50000"),
                started_on=date(2026, 1, 1),
            )
            session.add(lease)
            await session.flush()

            with pytest.raises(LocationAnalyticsError, match="другому помещению"):
                await resolve_location_context(
                    session,
                    article=rent,
                    location_id=location.id,
                    lease_id=lease.id,
                    counterparty_id=None,
                    on_date=TODAY,
                )
            await session.rollback()

    asyncio.run(run())


def test_leases_for_location_filters_by_article_and_date(async_session_factory) -> None:
    """Список для оператора: договор чужой статьи не предлагаем, закрытый — тоже."""

    async def run() -> None:
        async with async_session_factory() as session:
            rent, other, location, landlord = await _fixture(session)
            active = LocationLease(
                id=uuid.uuid4(),
                location_id=location.id,
                counterparty_id=landlord.id,
                monthly_amount=Decimal("100000"),
                started_on=date(2026, 1, 1),
                dds_article_id=rent.id,
            )
            foreign_article = LocationLease(
                id=uuid.uuid4(),
                location_id=location.id,
                counterparty_id=landlord.id,
                monthly_amount=Decimal("10000"),
                started_on=date(2026, 1, 1),
                dds_article_id=other.id,
            )
            finished = LocationLease(
                id=uuid.uuid4(),
                location_id=location.id,
                counterparty_id=landlord.id,
                monthly_amount=Decimal("70000"),
                started_on=date(2025, 1, 1),
                ended_on=date(2025, 12, 31),
                dds_article_id=rent.id,
            )
            session.add_all([active, foreign_article, finished])
            await session.flush()

            found = await leases_for_location(
                session, location.id, article_id=rent.id, on_date=TODAY
            )
            assert [lease.id for lease in found] == [active.id]
            await session.rollback()

    asyncio.run(run())


def test_cash_payout_carries_location_into_transaction(async_session_factory) -> None:
    """Кассовая аренда доносит помещение и арендодателя до проводки ДДС.

    Аренда склада платится через кассу (kassa_enabled), поэтому вход обязан заполнять
    аналитику наравне с банковским.
    """

    async def run() -> None:
        from decimal import Decimal as D

        from app.models import CashflowTransaction, Wallet
        from app.services.kassa.payouts import create_payout

        async with async_session_factory() as session:
            rent, _other, location, landlord = await _fixture(session)
            rent.kassa_enabled = True
            # Профиль контрагента обязателен: касса проверяет его для informal-получателя.
            from app.models import CounterpartyPayableProfile, CounterpartyRole

            session.add_all(
                [
                    CounterpartyPayableProfile(
                        id=uuid.uuid4(), counterparty_id=landlord.id, relationship="informal"
                    ),
                    CounterpartyRole(counterparty_id=landlord.id, role="landlord"),
                ]
            )
            lease = LocationLease(
                id=uuid.uuid4(),
                location_id=location.id,
                counterparty_id=landlord.id,
                monthly_amount=D("10000"),
                started_on=date(2026, 1, 1),
                documents_mode="informal",
                dds_article_id=rent.id,
            )
            session.add(lease)
            # Кассовый кошелёк должен существовать.
            existing_kassa = await session.scalar(
                select(Wallet).where(Wallet.type == "store_cash").limit(1)
            )
            if existing_kassa is None:
                session.add(
                    Wallet(
                        id=uuid.uuid4(),
                        name="Касса",
                        type="store_cash",
                        status="active",
                    )
                )
            await session.flush()

            result = await create_payout(
                session,
                article_id=rent.id,
                amount=D("10000"),
                location_id=location.id,
                lease_id=lease.id,
            )
            txn = await session.get(CashflowTransaction, result.transaction_id)
            assert txn is not None
            assert txn.location_id == location.id
            assert txn.lease_id == lease.id
            assert txn.counterparty_id == landlord.id
            await session.rollback()

    asyncio.run(run())

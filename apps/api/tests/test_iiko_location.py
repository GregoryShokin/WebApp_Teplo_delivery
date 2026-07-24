"""Резолвер iiko-идентификаторов точки из реестра помещений (замена зашитых констант)."""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Location, Organization
from app.services.iiko_location import resolve_department_id, resolve_organization_id


async def _organization_id(session: AsyncSession) -> uuid.UUID:
    organization_id = await session.scalar(select(Organization.id).limit(1))
    if organization_id is None:
        organization = Organization(id=uuid.uuid4(), name="Тест-организация")
        session.add(organization)
        await session.flush()
        organization_id = organization.id
    return organization_id


def test_resolver_reads_ids_from_registry(async_session_factory) -> None:
    """Резолвер отдаёт id действующей точки из реестра, а не зашитую константу."""

    async def run() -> None:
        async with async_session_factory() as session:
            organization_id = await _organization_id(session)
            # Имя с латинского «AAAA» гарантированно первое по алфавиту — резолвер (первая по
            # имени действующая точка) выберет именно её, независимо от реальных точек в БД.
            session.add(
                Location(
                    id=uuid.uuid4(),
                    organization_id=organization_id,
                    name="AAAA-резолвер-тест",
                    status="active",
                    iiko_organization_id="org-registry",
                    iiko_department_id="dep-registry",
                )
            )
            await session.flush()
            assert await resolve_organization_id(session) == "org-registry"
            assert await resolve_department_id(session) == "dep-registry"
            await session.rollback()

    asyncio.run(run())


def test_resolver_skips_inactive_and_iikoless(async_session_factory) -> None:
    """Закрытая точка и точка без iiko-id не считаются основной — берётся действующая с id."""

    async def run() -> None:
        async with async_session_factory() as session:
            organization_id = await _organization_id(session)
            session.add_all(
                [
                    Location(
                        id=uuid.uuid4(),
                        organization_id=organization_id,
                        name="AAAA-закрытая",
                        status="inactive",
                        iiko_department_id="dep-closed",
                    ),
                    Location(
                        id=uuid.uuid4(),
                        organization_id=organization_id,
                        name="AAAB-без-iiko",
                        status="active",
                    ),
                    Location(
                        id=uuid.uuid4(),
                        organization_id=organization_id,
                        name="AAAC-действующая",
                        status="active",
                        iiko_organization_id="org-live",
                        iiko_department_id="dep-live",
                    ),
                ]
            )
            await session.flush()
            assert await resolve_department_id(session) == "dep-live"
            assert await resolve_organization_id(session) == "org-live"
            await session.rollback()

    asyncio.run(run())

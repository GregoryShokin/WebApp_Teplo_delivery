"""Область реестра контрагентов: пикеры получают только поставщиков, ИНН не теряется.

Регрессии ревью 16.07: (1) list_registry потерял фильтр роли supplier — банк/налоговая
попадали в пикеры накладных и платежей; (2) create_counterparty молча выбрасывал ИНН у
неофициалов — следующий синк iiko/почты/ЭДО не находил карточку и плодил дубль.
"""

from __future__ import annotations

import pytest
from cp_helpers import make_counterparty
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.counterparty_registry import (
    CounterpartyRegistryError,
    create_counterparty,
    list_registry,
)


async def test_registry_default_excludes_non_suppliers(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Пикеры накладных/платежей берут реестр без параметров — банк, налоговая и
    безролевые карточки (наследие старого ДДС-справочника) в него попадать не должны."""
    async with async_session_factory() as session:
        supplier = await make_counterparty(session, name="Поставщик", inn="6100000001")
        bank = await make_counterparty(
            session, name="Сбербанк", inn="7707083893", cp_type="bank", role="bank"
        )
        roleless = await make_counterparty(
            session, name="Наследие ДДС", inn="6100000002", role=None
        )
        await session.commit()

        default_ids = {item.counterparty_id for item in await list_registry(session)}
        assert supplier.id in default_ids
        assert bank.id not in default_ids
        assert roleless.id not in default_ids

        full_ids = {
            item.counterparty_id
            for item in await list_registry(session, include_non_suppliers=True)
        }
        # Страница реестра и справочник классификации ДДС видят все карточки.
        assert {supplier.id, bank.id, roleless.id} <= full_ids


async def test_create_informal_counterparty_keeps_inn(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ИНН — ключ идентификации, а не банковский реквизит: неофициал сохраняет его,
    и проверка на дубль по ИНН для него тоже работает."""
    async with async_session_factory() as session:
        created = await create_counterparty(
            session,
            name="Карточный поставщик",
            inn="6167012345",
            cp_type="legal_entity",
            relationship="informal",
            confirm_no_dds_article=True,
        )
        assert created.inn == "6167012345"

        with pytest.raises(CounterpartyRegistryError, match="таким ИНН уже существует"):
            await create_counterparty(
                session,
                name="Дубль по ИНН",
                inn="6167012345",
                cp_type="legal_entity",
                relationship="informal",
                confirm_no_dds_article=True,
            )

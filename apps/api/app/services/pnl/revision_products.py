"""Единый источник товаров, уже учтённых продуктовой ревизией."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import InventoryAuditPosition


def normalize_iiko_product_guid(value: str | None) -> str:
    """Нормализовать GUID iiko для сопоставления разных зеркал."""
    return str(value or "").strip().casefold()


async def load_revision_product_guids(session: AsyncSession) -> set[str]:
    """GUID позиций, которые реально участвуют в расчёте продуктовой ревизии.

    Критерий намеренно совпадает с расчётом штрафа: позиция активна и ей назначена
    группа распределения. Само наличие старой или выключенной позиции в справочнике
    не должно навсегда исключать товар из товарных расходов ОПиУ.
    """
    rows = (
        await session.execute(
            select(InventoryAuditPosition.iiko_product_guid).where(
                InventoryAuditPosition.is_active.is_(True),
                InventoryAuditPosition.allocation_group.is_not(None),
                InventoryAuditPosition.iiko_product_guid.is_not(None),
            )
        )
    ).scalars()
    return {guid for value in rows if (guid := normalize_iiko_product_guid(value))}

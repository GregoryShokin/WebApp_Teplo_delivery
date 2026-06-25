"""iiko-GUID поставщика для проводок оплаты накладной Кассы.

Раньше здесь жило зеркало оплаты накладной в iiko через изъятие ``addPayOut`` «ИС»
(``INVOICE_PAYMENT_PAYOUT_TYPE_ID``). УДАЛЕНО 2026-06-26: товарную часть теперь гасит правильная
«оплата накладной» ``add_payment`` (НОПЛ) сверочным джобом ``mirror_paid_kassa_invoices`` (см.
``counterparty_iiko_payment``). Осталась лишь резолв-функция iiko-GUID контрагента, которую
используют warehouse-роуты.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CounterpartyAlias

IIKO_SOURCE = "iiko"


async def counterparty_iiko_guid(session: AsyncSession, counterparty_id: uuid.UUID) -> str | None:
    """iiko-GUID поставщика (первый alias ``source='iiko'``) — для проводок оплаты в iiko."""
    return await session.scalar(
        select(CounterpartyAlias.alias)
        .where(
            CounterpartyAlias.counterparty_id == counterparty_id,
            CounterpartyAlias.source == IIKO_SOURCE,
        )
        .limit(1)
    )

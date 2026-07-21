"""The warehouse list contains inventory documents, not finance-only bills or service UPDs."""

from __future__ import annotations

from cp_helpers import make_counterparty, make_invoice
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.warehouse_invoices import list_warehouse_invoices


async def test_warehouse_list_filters_by_explicit_operational_scope(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик")
        warehouse = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="100.00",
            number="GOODS-1",
            operational_scope="warehouse",
        )
        await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="200.00",
            number="SERVICE-1",
            source="sbis",
            operational_scope="finance",
        )
        await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="300.00",
            number="UNKNOWN-1",
            operational_scope="unknown",
        )
        await session.commit()

        rows = await list_warehouse_invoices(session)

        assert [row["id"] for row in rows] == [str(warehouse.id)]
        assert rows[0]["operational_scope"] == "warehouse"

"""Гашение накладной из выданной предоплаты (дебиторка): денег не двигает, статус-гард.

settle_invoice_from_prepayment списывает остаток предоплаты против payable-накладной без
движения денег. Закрытую/возвращённую предоплату гасить нельзя — иначе списали бы остаток
уже не существующей дебиторки.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from cp_helpers import make_counterparty, make_invoice
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import SupplierPrepayment
from app.services.counterparty_payments import CounterpartyPaymentError
from app.services.supplier_prepayments import settle_invoice_from_prepayment


async def _prepayment(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    status: str,
    amount: str = "1000.00",
    settled: str = "0.00",
) -> SupplierPrepayment:
    prepayment = SupplierPrepayment(
        counterparty_id=counterparty_id,
        kind="goods",
        amount=Decimal(amount),
        amount_settled=Decimal(settled),
        status=status,
    )
    session.add(prepayment)
    await session.flush()
    return prepayment


@pytest.mark.parametrize("status", ["refunded", "settled"])
async def test_settle_rejects_non_open_prepayment(
    async_session_factory: async_sessionmaker[AsyncSession], status: str
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name=f"Поставщик-{status}")
        pre = await _prepayment(session, counterparty_id=cp.id, status=status)
        inv = await make_invoice(session, counterparty_id=cp.id, amount="1000.00")
        await session.commit()

        with pytest.raises(CounterpartyPaymentError, match="недоступна"):
            await settle_invoice_from_prepayment(
                session, invoice_id=inv.id, prepayment_id=pre.id
            )


async def test_settle_open_prepayment_allocates(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Поставщик-open")
        pre = await _prepayment(session, counterparty_id=cp.id, status="open")
        inv = await make_invoice(session, counterparty_id=cp.id, amount="600.00")
        await session.commit()

        result = await settle_invoice_from_prepayment(
            session, invoice_id=inv.id, prepayment_id=pre.id
        )
        assert result.payment_status == "paid"
        await session.refresh(pre)
        assert pre.amount_settled == Decimal("600.00")
        assert pre.status == "partially_settled"

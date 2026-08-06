"""Наличная выплата услуговому контрагенту обязана оставить след в ДЗ/КЗ.

МЕЖДУ АДРЕСНЫМИ МЕХАНИЗМАМИ БЫЛА ЩЕЛЬ, И ДЕНЬГИ ПРОВАЛИВАЛИСЬ В НЕЁ МОЛЧА. Выдача из Сейфа
оставляет дебиторку двумя путями: аренда — по договору (``settle_lease_invoice_from_cash``),
коммуналка — по лицевому счёту (``settle_utility_invoices_from_cash``). Если ни один платёж
не узнал, не происходило НИЧЕГО: правило 1 канона к наличным не заходит вовсе
(``safe_payout`` в ``DEDICATED_MONEY_SOURCE_KINDS`` — слепой FIFO снял бы адресную привязку).

Цена щели, найденная 06.08.2026: 08.07 из Сейфа ушло 9 879 ₽ «За воду» Станиславу Юрьевичу,
а лицевой счёт «Вода» завели 03.08 — позже платежа. Деньги остались вне расчётов, и пришедшая
платёжка закрылась ЧУЖИМ авансом — арендой за август. 31.08 закрывающий по аренде выставил бы
к оплате уже оплаченный месяц, а реестр остатков разошёлся с карточкой сверки на те же 9 879 ₽.

Гейт страховки — ``service_billing_mode``: расход услугового контрагента закрывается
признанием, значит деньги вперёд это дебиторка по построению. У поставщика ТОВАРА режим пуст,
его платёж закрывает накладную, и правилу 1 здесь делать нечего.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    CounterpartyPayableProfile,
    DdsArticle,
    SafeAllocation,
    SupplierPrepayment,
)
from cp_helpers import make_counterparty, make_wallet


async def _service_article(session: AsyncSession, *, code: str) -> DdsArticle:
    article = DdsArticle(
        code=code,
        name=f"Статья {code}",
        movement_type="outflow",
        activity_type="operating",
    )
    session.add(article)
    await session.flush()
    return article


async def _set_billing_mode(session: AsyncSession, counterparty_id: uuid.UUID, mode) -> None:
    profile = await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == counterparty_id
        )
    )
    profile.service_billing_mode = mode
    await session.flush()


async def _pay_from_safe(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    article_id: uuid.UUID,
    wallet_id: uuid.UUID,
    amount: str,
) -> uuid.UUID:
    """Выдать наличные из Сейфа целевым резервом — тот же путь, что у живой выплаты."""
    from app.services.banking.safe_allocations import pay_allocation

    allocation = SafeAllocation(
        wallet_id=wallet_id,
        counterparty_id=counterparty_id,
        article_id=article_id,
        amount=Decimal(amount),
        status="reserved",
        purpose="Наличная оплата услуг",
    )
    session.add(allocation)
    await session.flush()
    return await pay_allocation(
        session,
        allocation=allocation,
        amount=Decimal(amount),
        operation_date=date(2026, 7, 8),
    )


@pytest.mark.asyncio
async def test_cash_payout_to_service_counterparty_creates_receivable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ни договора, ни лицевого счёта — дебиторка всё равно обязана появиться."""
    async with async_session_factory() as session:
        counterparty = await make_counterparty(session, name="Услуги-налом", inn="6155033001")
        await _set_billing_mode(session, counterparty.id, "agreement")
        article = await _service_article(session, code="test_cash_receivable_services")
        wallet = await make_wallet(session, code="cash-recv-w1", name="Сейф")
        await session.commit()

        await _pay_from_safe(
            session,
            counterparty_id=counterparty.id,
            article_id=article.id,
            wallet_id=wallet.id,
            amount="9879.00",
        )
        await session.commit()

        prepayment = await session.scalar(
            select(SupplierPrepayment).where(
                SupplierPrepayment.counterparty_id == counterparty.id
            )
        )
        assert prepayment is not None, "наличная выплата услуговому контрагенту потеряла след"
        assert prepayment.amount == Decimal("9879.00")
        assert prepayment.amount_settled == Decimal("0.00")


@pytest.mark.asyncio
async def test_cash_payout_to_goods_supplier_stays_silent(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """У поставщика товара режим признания пуст — его платёж закрывает накладную, не аванс."""
    async with async_session_factory() as session:
        counterparty = await make_counterparty(session, name="Товары-налом", inn="6155033002")
        await _set_billing_mode(session, counterparty.id, None)
        article = await _service_article(session, code="test_cash_receivable_goods")
        wallet = await make_wallet(session, code="cash-recv-w2", name="Сейф")
        await session.commit()

        await _pay_from_safe(
            session,
            counterparty_id=counterparty.id,
            article_id=article.id,
            wallet_id=wallet.id,
            amount="5000.00",
        )
        await session.commit()

        prepayment = await session.scalar(
            select(SupplierPrepayment).where(
                SupplierPrepayment.counterparty_id == counterparty.id
            )
        )
        assert prepayment is None, "товарный платёж не должен рождать дебиторку правила 1"

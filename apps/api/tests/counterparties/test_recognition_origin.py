"""Окно «Основание платежа»: за что заплатили и чем это закрыто.

ЗАЧЕМ. На «Странице на оплату» есть разбор, который показывает счёт, из-за которого платёж
появился, — им пользуются постоянно. В признании расходов такого не было: человек видит строку
«АЙКО · 16 430 ₽» и не может проверить основание, не уходя в другой раздел.

Здесь цепочка длиннее, чем на странице оплаты: у платежа есть НАЧАЛО (счёт или договор) и
КОНЕЦ (закрывающий документ, признание по периоду или по договору). Оба конца должны
объясняться словами там, где документа нет: подсунуть вместо него чужую бумагу хуже, чем
честно сказать, что бумаги не будет.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from cp_helpers import make_counterparty, make_invoice
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    CounterpartyPayableProfile,
    InvoicePaymentAllocation,
    SupplierPrepayment,
)


async def _prepayment(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    amount: str,
    bill_invoice_id: uuid.UUID | None = None,
) -> SupplierPrepayment:
    prepayment = SupplierPrepayment(
        counterparty_id=counterparty_id,
        kind="prepaid_bill" if bill_invoice_id else "subscription",
        amount=Decimal(amount),
        amount_settled=Decimal("0.00"),
        status="open",
        bill_invoice_id=bill_invoice_id,
    )
    session.add(prepayment)
    await session.flush()
    return prepayment


async def test_bill_and_closing_document_are_both_named(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Платёж по счёту, закрытый УПД: названы оба документа."""
    from app.api.v1.routes.accounting_suppliers import prepayment_origin

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Счёт и УПД", inn="7712340001")
        bill = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="4260.00",
            number="010826-9723-лсп",
            doc_kind="bill",
            invoice_date=date(2026, 8, 1),
        )
        prepayment = await _prepayment(
            session, counterparty_id=cp.id, amount="4260.00", bill_invoice_id=bill.id
        )
        closing = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="4260.00",
            number="УПД-777",
            doc_kind="closing",
            invoice_date=date(2026, 8, 31),
        )
        session.add(
            InvoicePaymentAllocation(
                invoice_id=closing.id,
                prepayment_id=prepayment.id,
                amount=Decimal("4260.00"),
                source_kind="prepayment",
            )
        )
        await session.commit()

        origin = await prepayment_origin(prepayment.id, session)

        assert origin.basis is not None
        assert "010826-9723-лсп" in origin.basis_note
        assert origin.closing is not None
        assert "УПД-777" in origin.closing_note


async def test_agreement_payment_says_so_instead_of_showing_a_paper(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Счёта нет, есть договор — так и написано, а не «основание не найдено»."""
    from app.api.v1.routes.accounting_suppliers import prepayment_origin
    from app.models import CounterpartyServiceAgreement, DdsArticle

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Договорной без счёта", inn="7712340002")
        article = await session.scalar(
            select(DdsArticle).where(DdsArticle.movement_type == "outflow").limit(1)
        )
        session.add(
            CounterpartyServiceAgreement(
                counterparty_id=cp.id,
                title="Ведение бухгалтерии",
                monthly_amount=Decimal("3000.00"),
                dds_article_id=article.id if article else None,
                documents_mode="informal",
                accrual_enabled=True,
                started_on=date(2026, 4, 1),
            )
        )
        prepayment = await _prepayment(session, counterparty_id=cp.id, amount="9000.00")
        await session.commit()

        origin = await prepayment_origin(prepayment.id, session)

        assert origin.basis is None
        assert "Ведение бухгалтерии" in origin.basis_note
        assert "договор" in origin.basis_note.lower()


async def test_fixed_tariff_says_the_document_will_not_come(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """У контрагента «счёт за период» закрытия документом не будет — так и сказано.

    Это ровно тот случай, где раньше строка молча ждала УПД, которого никто не выставит.
    """
    from app.api.v1.routes.accounting_suppliers import prepayment_origin

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Счёт за период", inn="7712340003")
        profile = await session.scalar(
            select(CounterpartyPayableProfile).where(
                CounterpartyPayableProfile.counterparty_id == cp.id
            )
        )
        assert profile is not None
        profile.service_billing_mode = "fixed_tariff"
        prepayment = await _prepayment(session, counterparty_id=cp.id, amount="13000.00")
        await session.commit()

        origin = await prepayment_origin(prepayment.id, session)

        assert origin.closing is None
        assert "по периоду" in origin.closing_note

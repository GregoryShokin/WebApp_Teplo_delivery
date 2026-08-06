"""Платёж за период до начала учёта не становится дебиторкой в карточке сверки.

ДВА ЭКРАНА ОДНОГО МОДУЛЯ СЧИТАЛИ ПО-РАЗНОМУ. Реестр остатков берёт дебиторку из открытых
авансов, карточка сверки — из хронологии «деньги минус документы». Обычно они сходятся, но
у платежа, закрывающего период ДО 01.07.2026, обязательства в системе нет: документов того
периода не заводят вовсе. Реестр его и не видел, а карточка честно считала переплатой.

Кейс владельца 06.08.2026: доплата 30 402 ₽ Виталию за июньское электричество (акт от 17.07,
итого 95 402 ₽ при уплаченном авансе 65 000 ₽). Карточка показывала 145 402 ₽ против
115 000 ₽ в реестре — расхождение ровно на эту доплату.

Строка в хронологии остаётся: деньги ушли, и человек обязан их видеть. Не двигается только
бегущий остаток — ``binds=False``, тот же механизм, которым из остатка исключены будущие
закрывающие документы.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import CashflowTransaction, CounterpartyPayableProfile, DdsArticle
from app.services.counterparty_settlement_ledger import build_ledger
from cp_helpers import make_counterparty, make_wallet


async def _utility_article(session: AsyncSession) -> DdsArticle:
    article = DdsArticle(
        code="test_ledger_before_start_utilities",
        name="Коммунальные платежи — тест",
        movement_type="outflow",
        activity_type="operating",
    )
    session.add(article)
    await session.flush()
    return article


@pytest.mark.asyncio
async def test_payment_before_accounting_start_does_not_inflate_receivable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Доплата за июнь видна строкой, но дебиторку не растит."""
    async with async_session_factory() as session:
        landlord = await make_counterparty(session, name="Арендодатель-июнь", inn="6155035001")
        profile = await session.scalar(
            select(CounterpartyPayableProfile).where(
                CounterpartyPayableProfile.counterparty_id == landlord.id
            )
        )
        profile.service_billing_mode = "agreement"
        article = await _utility_article(session)
        wallet = await make_wallet(session, code="ledger-before-w1", name="Сейф")

        # Аванс за июль — обычные деньги вперёд, они дебиторка.
        session.add(
            CashflowTransaction(
                wallet_id=wallet.id,
                direction="out",
                amount=Decimal("65000.00"),
                operation_date=date(2026, 7, 19),
                article_id=article.id,
                counterparty_id=landlord.id,
                source_kind="safe_payout",
                payment_purpose="Предоплата за Июль",
                quality_status="manual_override",
            )
        )
        # Доплата по акту за ИЮНЬ — закрывает период до начала учёта.
        session.add(
            CashflowTransaction(
                wallet_id=wallet.id,
                direction="out",
                amount=Decimal("30402.00"),
                operation_date=date(2026, 7, 19),
                article_id=article.id,
                counterparty_id=landlord.id,
                source_kind="safe_payout",
                payment_purpose="Оплата за Июнь",
                quality_status="manual_override",
                expense_month=date(2026, 6, 1),
            )
        )
        await session.commit()

        ledger = await build_ledger(session, landlord.id, today=date(2026, 8, 6))

        # Обе строки на месте: деньги ушли, человек обязан их видеть.
        payments = [row for row in ledger.rows if row.kind == "payment"]
        assert len(payments) == 2
        assert ledger.total_paid == Decimal("95402.00")

        # А дебиторка — только аванс июля. Июньская доплата гасит обязательство вне периметра.
        assert ledger.closing_balance == Decimal("65000.00")
        before_start = next(row for row in payments if row.amount == Decimal("30402.00"))
        assert before_start.binds is False

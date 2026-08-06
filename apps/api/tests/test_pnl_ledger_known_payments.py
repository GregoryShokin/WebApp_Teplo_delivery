"""Платёж со следом в ДЗ/КЗ не считается «оплачено, но расход не признан».

Предупреждение ``excluded_without_accrual`` придумано для денег, о которых ДЗ/КЗ не знает:
платёж исключён из строки «под документ», а носителя будущего расхода нет. Первая версия
(05.08.2026) сверяла кассу только с признанием текущего месяца и выдала на здоровых данных
июля все 134 000 ₽ ложных срабатываний — предоплату августа, закрытую предоплату прошлых
месяцев, предоплату с уже запланированным начислением. Владелец опроверг каждую строчку
по памяти: «по факту не хватает только двух данных — Манго Телеком и контекстная реклама».

След в ДЗ/КЗ существует двумя путями, и тест закрывает оба:

* предоплата правила 1 привязана к проводке напрямую (``cashflow_transaction_id``);
* оплата счёта: аллокация платежа на документ ``doc_kind='bill'``. ДЗ такого платежа
  (``kind='prepaid_bill'``) по замыслу живёт с ``cashflow_transaction_id=None``, поэтому
  прямой ссылкой его не найти никогда — только через аллокацию.

Платёж БЕЗ следа остаётся в предупреждении: это и есть случай потерянного документа.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

sys.path.append(str(Path(__file__).parent / "counterparties"))

from app.models import (
    CashflowTransaction,
    CounterpartyPayableProfile,
    DdsArticle,
    InvoicePaymentAllocation,
    PnlArticleRule,
    SupplierPrepayment,
)
from app.services.pnl.sources import cashflow as cash_source
from app.services.pnl.types import Verdict

MONTH_START = date(2026, 7, 1)
MONTH_END = date(2026, 7, 31)


async def _service_counterparty(session, *, name: str, inn: str):
    """Контрагент контура признания: услуговый режим размечен в профиле."""
    from cp_helpers import make_counterparty

    counterparty = await make_counterparty(session, name=name, inn=inn)
    profile = await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == counterparty.id
        )
    )
    profile.service_billing_mode = "per_invoice"
    await session.flush()
    return counterparty


async def _expense_article(session, *, code: str) -> DdsArticle:
    article = DdsArticle(
        code=code,
        name=f"Статья {code}",
        movement_type="outflow",
        activity_type="operating",
    )
    session.add(article)
    await session.flush()
    session.add(
        PnlArticleRule(
            article_id=article.id,
            line_code="telecom",
            in_pnl=True,
            owner_stream="cash",
            sign=1,
            applies_to="both",
            is_active=True,
        )
    )
    await session.flush()
    return article


def _payment(
    wallet_id, article_id, counterparty_id, amount: str, day: int
) -> CashflowTransaction:
    return CashflowTransaction(
        wallet_id=wallet_id,
        direction="out",
        amount=Decimal(amount),
        operation_date=date(2026, 7, day),
        article_id=article_id,
        counterparty_id=counterparty_id,
        source_kind="bank_operation",
        payment_purpose="Оплата услуг",
        quality_status="manual_override",
    )


def test_ledger_known_payments_do_not_raise_the_alarm(async_session_factory) -> None:
    from cp_helpers import make_invoice, make_wallet

    async def scenario() -> None:
        async with async_session_factory() as session:
            counterparty = await _service_counterparty(
                session, name="Услуги-со-следом", inn="6155031001"
            )
            article = await _expense_article(session, code="test_ledger_known_services")
            wallet = await make_wallet(session, code="ledger-known-w", name="Банк")

            covered_by_prepayment = _payment(
                wallet.id, article.id, counterparty.id, "5000.00", day=7
            )
            covered_by_bill = _payment(wallet.id, article.id, counterparty.id, "7000.00", day=22)
            orphan = _payment(wallet.id, article.id, counterparty.id, "1000.00", day=28)
            session.add_all([covered_by_prepayment, covered_by_bill, orphan])
            await session.flush()

            session.add(
                SupplierPrepayment(
                    counterparty_id=counterparty.id,
                    kind="subscription",
                    amount=Decimal("5000.00"),
                    amount_settled=Decimal("0.00"),
                    status="open",
                    cashflow_transaction_id=covered_by_prepayment.id,
                    article_id=article.id,
                    service_period_status="missing",
                )
            )
            bill = await make_invoice(
                session,
                counterparty_id=counterparty.id,
                amount="7000.00",
                doc_kind="bill",
                number="СЧ-77",
                payment_status="paid",
                invoice_date=date(2026, 7, 22),
            )
            session.add(
                InvoicePaymentAllocation(
                    invoice_id=bill.id,
                    source_kind="cash",
                    cashflow_transaction_id=covered_by_bill.id,
                    amount=Decimal("7000.00"),
                )
            )
            await session.commit()

            layer = await cash_source.build_cash_layer(session, MONTH_START, MONTH_END)

            # Из строк исключены все три платежа — контрагент ведётся признанием,
            # и расход обязан прийти документом, а не кассой.
            assert layer.by_verdict_count[Verdict.EXCLUDED_ACCRUAL_COUNTERPARTY.value] == 3

            # А вот тревога — только по платежу без следа: у двух остальных носитель
            # будущего расхода уже есть (предоплата и оплаченный счёт).
            assert layer.excluded_for_accrual == {counterparty.id: Decimal("1000.00")}

    asyncio.run(scenario())

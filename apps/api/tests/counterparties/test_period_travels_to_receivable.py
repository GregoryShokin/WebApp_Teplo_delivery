"""Период услуги доезжает от счёта до дебиторки — иначе признание расхода не наступает само.

ЗАЧЕМ ЭТО ВАЖНО. Период — не украшение карточки: по нему система понимает, КОГДА услуга
оказана и, значит, когда признать расход. У контрагента, который закрывающих документов не
присылает («счёт за период» — АЙКО, Синапсис, Лемма, ДоксИнБокс), другого сигнала нет вовсе:
месяц кончился — расход признан.

ЧТО БЫЛО. Период попадал в дебиторку ровно из одного места — со строки окна «Новый платёж», и
только если платёж прошёл через черновик (``source_kind='counterparty_payment'``). Реальные
платежи приходят иначе: черновик ушёл в банк, банк исполнил, выписка вернулась, и дебиторка
родилась из проводки, которая о счёте ничего не знает. На проде 02.08.2026 так возникли ВСЕ
18 строк очереди «Ждём документ» на 342 949 ₽ — и ждали они вечно, потому что ждать было
нечего: система не знала, за какой период платили.

При этом период лежал рядом — в счёте, распознанный из его текста («за июль» → 01.07–31.07).
Дебиторка даже ссылалась на этот счёт полем ``bill_invoice_id`` и не читала его.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from cp_helpers import make_counterparty, make_invoice, make_wallet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sqlalchemy import select

from app.models import (
    CashflowTransaction,
    InvoicePaymentAllocation,
    SupplierInvoice,
    SupplierPrepayment,
)
from app.services.supplier_prepayments import (
    ensure_prepayment_from_bank_transaction,
    reconcile_bill_prepayment,
)


async def _bill(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    amount: str,
    period: tuple[date, date] | None,
    number: str = "СЧ-1",
) -> SupplierInvoice:
    invoice = await make_invoice(
        session,
        counterparty_id=counterparty_id,
        amount=amount,
        number=number,
        doc_kind="bill",
        invoice_date=date(2026, 7, 4),
    )
    if period is not None:
        invoice.service_period_start = period[0]
        invoice.service_period_end = period[1]
        invoice.service_period_status = "ready"
    await session.flush()
    return invoice


async def _payment(
    session: AsyncSession,
    *,
    counterparty_id: uuid.UUID,
    wallet_id: uuid.UUID,
    amount: str,
    on: date = date(2026, 7, 6),
) -> CashflowTransaction:
    """Платёж, пришедший ВЫПИСКОЙ: у такой проводки нет ссылки на черновик."""
    tx = CashflowTransaction(
        counterparty_id=counterparty_id,
        wallet_id=wallet_id,
        direction="out",
        amount=Decimal(amount),
        operation_date=on,
        source_kind="bank_operation",
        quality_status="auto",
    )
    session.add(tx)
    await session.flush()
    return tx


async def test_receivable_inherits_period_from_the_paid_bill(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Платёж выпиской по счёту «за июль» даёт дебиторку с периодом июля.

    Кейс ДоксИнБокс: счёт на 15 580 ₽ с распознанным периодом, оплата пришла выпиской.
    Раньше дебиторка получала ``service_period_status='missing'`` и вставала в очередь
    «Ждём документ» — при том что документа по такому контрагенту не будет никогда.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ДоксИнБокс период", inn="7723456701")
        wallet = await make_wallet(session, code="tbank-period-1", name="Т-Банк")
        await _bill(
            session,
            counterparty_id=cp.id,
            amount="15580.00",
            period=(date(2026, 7, 1), date(2026, 7, 31)),
        )
        tx = await _payment(session, counterparty_id=cp.id, wallet_id=wallet.id, amount="15580.00")
        await session.commit()

        prepayment = await ensure_prepayment_from_bank_transaction(session, tx)
        await session.commit()

        assert prepayment is not None
        assert prepayment.service_period_start == date(2026, 7, 1)
        assert prepayment.service_period_end == date(2026, 7, 31)
        assert prepayment.service_period_status == "ready"


async def test_two_bills_with_different_periods_leave_the_decision_to_a_human(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Если сумме соответствуют счета за РАЗНЫЕ месяцы — период не угадываем.

    У АЙКО счета выставляются на одну и ту же сумму каждый месяц (16 430 ₽ за лицензию).
    Подставить «какой-нибудь» из них значило бы разложить расход не по тем месяцам, а увидели
    бы это только на сверке. Пустой период честнее: он сам себя требует.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="АЙКО два счёта", inn="7723456702")
        wallet = await make_wallet(session, code="tbank-period-2", name="Т-Банк")
        await _bill(
            session,
            counterparty_id=cp.id,
            amount="16430.00",
            period=(date(2026, 7, 1), date(2026, 7, 31)),
            number="040726-2486-лк",
        )
        await _bill(
            session,
            counterparty_id=cp.id,
            amount="16430.00",
            period=(date(2026, 8, 1), date(2026, 8, 31)),
            number="010826-3064-лк",
        )
        tx = await _payment(session, counterparty_id=cp.id, wallet_id=wallet.id, amount="16430.00")
        await session.commit()

        prepayment = await ensure_prepayment_from_bank_transaction(session, tx)
        await session.commit()

        assert prepayment is not None
        assert prepayment.service_period_start is None
        assert prepayment.service_period_status == "missing"


async def test_bill_without_recognised_period_gives_no_period(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Счёт без распознанного периода ничего не даёт — выдумывать нечего."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Синапсис без периода", inn="7723456703")
        wallet = await make_wallet(session, code="tbank-period-3", name="Т-Банк")
        await _bill(session, counterparty_id=cp.id, amount="13000.00", period=None)
        tx = await _payment(session, counterparty_id=cp.id, wallet_id=wallet.id, amount="13000.00")
        await session.commit()

        prepayment = await ensure_prepayment_from_bank_transaction(session, tx)
        await session.commit()

        assert prepayment is not None
        assert prepayment.service_period_start is None


async def test_bill_receivable_carries_the_period_of_its_own_bill(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Дебиторка по оплаченному счёту берёт период ИЗ ЭТОГО ЖЕ счёта, а не ищет похожие.

    Здесь связь прямая — ``bill_invoice_id``, — и угадывать нечего: счёт известен точно.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Счёт со своим периодом", inn="7723456704")
        wallet = await make_wallet(session, code="tbank-period-4", name="Т-Банк")
        invoice = await _bill(
            session,
            counterparty_id=cp.id,
            amount="4260.00",
            period=(date(2026, 9, 1), date(2026, 9, 30)),
            number="010826-9723-лсп",
        )
        tx = CashflowTransaction(
            counterparty_id=cp.id,
            wallet_id=wallet.id,
            direction="out",
            amount=Decimal("4260.00"),
            operation_date=date(2026, 8, 5),
            source_kind="counterparty_payment",
            quality_status="auto",
        )
        session.add(tx)
        await session.flush()
        invoice.payment_status = "paid"
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id,
                cashflow_transaction_id=tx.id,
                amount=Decimal("4260.00"),
                source_kind="cash",
            )
        )
        await session.commit()

        await reconcile_bill_prepayment(session, invoice)
        await session.commit()

        prepayment = await session.scalar(
            select(SupplierPrepayment).where(SupplierPrepayment.bill_invoice_id == invoice.id)
        )
        assert prepayment is not None, "дебиторка по оплаченному счёту не заведена"
        assert prepayment.service_period_start == date(2026, 9, 1)
        assert prepayment.service_period_end == date(2026, 9, 30)
        assert prepayment.service_period_status == "ready"

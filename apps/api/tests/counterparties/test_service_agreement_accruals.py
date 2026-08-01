"""Договор услуги: долг считается сам, когда закрывающих документов не будет.

ПОЧЕМУ ОТДЕЛЬНО ОТ ПРИЗНАНИЯ ИЗ ПРЕДОПЛАТЫ. Признание из платежа (``subscription_accruals``)
работает, когда деньги ушли ВПЕРЁД: платёж создал дебиторку, а признание её помесячно гасит.
Но ИП Наумченко платят ПОСТФАКТУМ — 9 000 ₽ 30.07.2026 за апрель-июнь, период уже закончился.
Дебиторки там быть не должно вовсе: каждый месяц должна была копиться КРЕДИТОРКА по 3 000 ₽,
а платёж её гасить. Восстанавливать историю из платежа задним числом — значит выводить долг
апреля из денег, которых в апреле не было.

Здесь закреплено главное: обязательство возникает БЕЗ платежа и живёт кредиторкой, а платёж
любого порядка (до или после) гасит его штатным контуром — механика одна, порядок разный.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from cp_helpers import make_counterparty, make_invoice, make_wallet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    CashflowTransaction,
    CounterpartyServiceAgreement,
    DdsArticle,
    InvoicePaymentAllocation,
    SupplierExpenseAccrual,
    SupplierInvoice,
    SupplierPrepayment,
)
from app.services import supplier_prepayments
from app.services.service_agreement_accruals import accrue_month, ensure_agreement_invoice
from app.services.subscription_accruals import SELF_BILLED_SOURCE, supersede_self_billed


async def _article(session: AsyncSession) -> DdsArticle:
    article = await session.scalar(
        select(DdsArticle).where(DdsArticle.movement_type == "outflow").limit(1)
    )
    assert article is not None, "в сидах каталога должна быть расходная статья"
    return article


async def _agreement(
    session: AsyncSession,
    *,
    counterparty_id,
    amount: str = "3000.00",
    title: str = "Ведение бухгалтерии",
    documents_mode: str = "informal",
    started_on: date = date(2026, 4, 1),
    ended_on: date | None = None,
    enabled: bool = True,
) -> CounterpartyServiceAgreement:
    article = await _article(session)
    agreement = CounterpartyServiceAgreement(
        counterparty_id=counterparty_id,
        title=title,
        monthly_amount=Decimal(amount),
        dds_article_id=article.id,
        documents_mode=documents_mode,
        accrual_enabled=enabled,
        started_on=started_on,
        ended_on=ended_on,
    )
    session.add(agreement)
    await session.flush()
    return agreement


async def test_accrual_creates_payable_without_any_payment(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Долг возникает сам, деньгами не подпёртый: это кредиторка, а не гашение дебиторки."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ИП Наумченко", inn="614302067192")
        agreement = await _agreement(session, counterparty_id=cp.id)
        await session.commit()

        invoice = await ensure_agreement_invoice(
            session, agreement, date(2026, 4, 1), as_of=date(2026, 5, 1)
        )
        await session.commit()

        assert invoice is not None
        assert invoice.source == SELF_BILLED_SOURCE
        assert invoice.amount == Decimal("3000.00")
        assert invoice.invoice_date == date(2026, 4, 30)
        assert invoice.service_period_start == date(2026, 4, 1)
        assert invoice.service_period_end == date(2026, 4, 30)
        # Ключевое: аллокаций нет, документ не оплачен — это долг перед контрагентом.
        assert invoice.payment_status in ("unpaid", "partially_paid")
        # Расход признан в СВОЁМ месяце, а не в месяце платежа.
        accrual = await session.scalar(
            select(SupplierExpenseAccrual).where(SupplierExpenseAccrual.invoice_id == invoice.id)
        )
        assert accrual is not None


async def test_quarterly_postpayment_settles_three_accrued_months(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Реальный кейс: три месяца копились долгом, один платёж 30.07 закрыл все три.

    Ровно то, чего не хватало: у Наумченко платёж пришёл ПОСЛЕ периода, и без начислений он
    порождал ложную дебиторку 9 000 ₽ вместо гашения долга.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ИП Наумченко-2", inn="614302067193")
        wallet = await make_wallet(session, code="tbank-svc-1", name="Т-Банк")
        await _agreement(session, counterparty_id=cp.id)
        await session.commit()

        created = await accrue_month(session, date(2026, 7, 1))
        await session.commit()
        assert len(created) == 3  # апрель, май, июнь
        assert sum(item.amount for item in created) == Decimal("9000.00")

        # Постоплата: деньги приходят, когда период давно закрыт.
        tx = CashflowTransaction(
            wallet_id=wallet.id,
            counterparty_id=cp.id,
            direction="out",
            amount=Decimal("9000.00"),
            operation_date=date(2026, 7, 30),
            source_kind="bank_operation",
            quality_status="final",
            payment_purpose="Услуги ФД и НК",
        )
        session.add(tx)
        await session.flush()
        prepayment = await supplier_prepayments.ensure_prepayment_from_bank_transaction(session, tx)
        await session.commit()

        # Дебиторки нет: платёж целиком ушёл на гашение накопленного долга.
        assert prepayment is None or Decimal(prepayment.amount) == Decimal("0.00")
        for invoice in created:
            await session.refresh(invoice)
            assert invoice.payment_status == "paid"


async def test_official_documents_mode_never_accrues(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Если документы обещаны — не начисляем: иначе расход задвоится в день прихода УПД."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Синапсис", inn="614302067194")
        agreement = await _agreement(session, counterparty_id=cp.id, documents_mode="official")
        await session.commit()

        assert (
            await ensure_agreement_invoice(
                session, agreement, date(2026, 4, 1), as_of=date(2026, 5, 1)
            )
            is None
        )


async def test_accrual_is_idempotent_and_month_bound(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Повторный прогон ничего не задваивает, а незакончившийся месяц не начисляется."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Охрана", inn="614302067195")
        agreement = await _agreement(session, counterparty_id=cp.id, amount="2300.00")
        await session.commit()

        first = await ensure_agreement_invoice(
            session, agreement, date(2026, 4, 1), as_of=date(2026, 5, 1)
        )
        await session.commit()
        again = await ensure_agreement_invoice(
            session, agreement, date(2026, 4, 1), as_of=date(2026, 5, 1)
        )
        assert first is not None and again is None

        # Апрель ещё идёт — начислять нечего: услуга оказывается весь последний день.
        assert (
            await ensure_agreement_invoice(
                session, agreement, date(2026, 4, 1), as_of=date(2026, 4, 30)
            )
            is None
        )


async def test_agreement_window_and_switch_are_respected(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Месяцы вне срока договора и выключенное начисление пропускаются."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Сезонная услуга", inn="614302067196")
        agreement = await _agreement(
            session,
            counterparty_id=cp.id,
            started_on=date(2026, 5, 1),
            ended_on=date(2026, 5, 31),
        )
        await session.commit()

        assert (
            await ensure_agreement_invoice(
                session, agreement, date(2026, 4, 1), as_of=date(2026, 7, 1)
            )
            is None
        )
        assert (
            await ensure_agreement_invoice(
                session, agreement, date(2026, 6, 1), as_of=date(2026, 7, 1)
            )
            is None
        )
        assert (
            await ensure_agreement_invoice(
                session, agreement, date(2026, 5, 1), as_of=date(2026, 7, 1)
            )
            is not None
        )

        agreement.accrual_enabled = False
        await session.flush()
        assert (
            await ensure_agreement_invoice(
                session, agreement, date(2026, 5, 1), as_of=date(2026, 7, 1)
            )
            is None
        )


async def test_real_document_supersedes_accrual(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Пришедший акт отменяет наше начисление за тот же месяц — расход не задваивается.

    У «ручных» контрагентов это не теория: Наумченко документов не присылала месяцами, а
    28.07.2026 прислала акт вместе со счётом.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Ручной с сюрпризом", inn="614302067197")
        agreement = await _agreement(session, counterparty_id=cp.id, amount="3000.00")
        await session.commit()

        accrued = await ensure_agreement_invoice(
            session, agreement, date(2026, 6, 1), as_of=date(2026, 7, 1)
        )
        await session.commit()
        assert accrued is not None

        real = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="3000.00",
            number="АКТ-100",
            doc_kind="closing",
            invoice_date=date(2026, 6, 30),
            operational_scope="finance",
        )
        real.service_period_start = date(2026, 6, 1)
        real.service_period_end = date(2026, 6, 30)
        await session.flush()

        superseded = await supersede_self_billed(session, real)
        await session.commit()

        assert [item.id for item in superseded] == [accrued.id]
        await session.refresh(accrued)
        assert accrued.payment_status == "void"
        # Начисление за тот же месяц заново не появится: настоящий документ его закрывает.
        assert (
            await ensure_agreement_invoice(
                session, agreement, date(2026, 6, 1), as_of=date(2026, 7, 1)
            )
            is None
        )
        remaining = await session.scalars(
            select(SupplierInvoice).where(
                SupplierInvoice.counterparty_id == cp.id,
                SupplierInvoice.doc_kind == "closing",
                SupplierInvoice.payment_status != "void",
            )
        )
        assert [item.id for item in remaining.all()] == [real.id]


async def test_supersede_keeps_the_money_on_the_real_document(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Оплаченное начисление замещается так, что деньги переезжают на настоящий документ.

    Раньше замещение умело возвращать только предоплату: денежных аллокаций у самоакта быть
    не могло — он рождался уже оплаченным из предоплаты. При постоплате они появились, и
    прежний код удалял их вместе с начислением: платёж становился ничьим (ни дебиторки, ни
    аллокации), настоящий УПД повисал неоплаченным долгом, а контрагент вылезал в разрывы —
    при том, что деньги контрагент давно получил.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Постоплата с УПД", inn="614302067198")
        wallet = await make_wallet(session, code="tbank-svc-2", name="Т-Банк")
        agreement = await _agreement(session, counterparty_id=cp.id, amount="3000.00")
        await session.commit()

        accrued = await ensure_agreement_invoice(
            session, agreement, date(2026, 6, 1), as_of=date(2026, 7, 1)
        )
        await session.commit()
        assert accrued is not None

        tx = CashflowTransaction(
            wallet_id=wallet.id,
            counterparty_id=cp.id,
            direction="out",
            amount=Decimal("3000.00"),
            operation_date=date(2026, 7, 10),
            source_kind="bank_operation",
            quality_status="final",
        )
        session.add(tx)
        await session.flush()
        await supplier_prepayments.ensure_prepayment_from_bank_transaction(session, tx)
        await session.commit()
        await session.refresh(accrued)
        assert accrued.payment_status == "paid"

        # Контрагент внезапно присылает настоящий акт за тот же месяц.
        real = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="3000.00",
            number="АКТ-606",
            doc_kind="closing",
            invoice_date=date(2026, 6, 30),
            operational_scope="finance",
        )
        real.service_period_start = date(2026, 6, 1)
        real.service_period_end = date(2026, 6, 30)
        await session.flush()

        await supersede_self_billed(session, real)
        await session.commit()

        # Деньги переехали на настоящий документ: он оплачен, ничьих платежей не осталось.
        moved = await session.scalars(
            select(InvoicePaymentAllocation).where(InvoicePaymentAllocation.invoice_id == real.id)
        )
        assert sum(item.amount for item in moved.all()) == Decimal("3000.00")
        orphaned = await session.scalars(
            select(InvoicePaymentAllocation).where(
                InvoicePaymentAllocation.invoice_id == accrued.id
            )
        )
        assert orphaned.all() == []


async def test_supersede_returns_overpayment_to_receivable(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Если настоящий документ дешевле нашей оценки, переплата возвращается дебиторкой.

    Деньги ушли по нашей ставке 3 000, а контрагент выставил акт на 2 000: разница — его долг
    перед нами, а не растворившийся платёж.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="Переплата", inn="614302067199")
        wallet = await make_wallet(session, code="tbank-svc-3", name="Т-Банк")
        agreement = await _agreement(session, counterparty_id=cp.id, amount="3000.00")
        await session.commit()

        accrued = await ensure_agreement_invoice(
            session, agreement, date(2026, 6, 1), as_of=date(2026, 7, 1)
        )
        await session.commit()

        tx = CashflowTransaction(
            wallet_id=wallet.id,
            counterparty_id=cp.id,
            direction="out",
            amount=Decimal("3000.00"),
            operation_date=date(2026, 7, 10),
            source_kind="bank_operation",
            quality_status="final",
        )
        session.add(tx)
        await session.flush()
        await supplier_prepayments.ensure_prepayment_from_bank_transaction(session, tx)
        await session.commit()

        cheaper = await make_invoice(
            session,
            counterparty_id=cp.id,
            amount="2000.00",
            number="АКТ-607",
            doc_kind="closing",
            invoice_date=date(2026, 6, 30),
            operational_scope="finance",
        )
        cheaper.service_period_start = date(2026, 6, 1)
        cheaper.service_period_end = date(2026, 6, 30)
        await session.flush()

        await supersede_self_billed(session, cheaper)
        await session.commit()

        moved = await session.scalars(
            select(InvoicePaymentAllocation).where(
                InvoicePaymentAllocation.invoice_id == cheaper.id
            )
        )
        assert sum(item.amount for item in moved.all()) == Decimal("2000.00")
        receivable = await session.scalars(
            select(SupplierPrepayment).where(
                SupplierPrepayment.counterparty_id == cp.id,
                SupplierPrepayment.status == "open",
            )
        )
        assert sum(item.amount for item in receivable.all()) == Decimal("1000.00")
        assert accrued is not None

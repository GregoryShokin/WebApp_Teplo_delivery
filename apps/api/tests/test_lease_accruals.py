"""Аренда как обязательство: начисление, попадание в ДЗ/КЗ и гашение платежом."""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CashflowTransaction,
    Counterparty,
    CounterpartyPayableProfile,
    DdsArticle,
    InvoicePaymentAllocation,
    Location,
    LocationLease,
    Organization,
    SafeAllocation,
    SupplierExpenseAccrual,
    SupplierInvoice,
    SupplierPrepayment,
    Wallet,
)
from app.services.banking.safe_allocations import pay_allocation
from app.services.lease_accruals import (
    ensure_lease_invoice,
    invoice_date_for,
    rebuild_lease_invoice,
    settle_lease_invoice_from_cash,
)
from app.services.supplier_prepayments import (
    activate_due_closing_invoices,
    counterparty_prepayment_balance,
)


async def _lease(
    session: AsyncSession,
    *,
    payment_mode: str = "postpaid",
    payment_day: int | None = None,
    amount: str = "100000",
    accrual_enabled: bool = True,
    with_article: bool = True,
) -> LocationLease:
    organization_id = await session.scalar(select(Organization.id).limit(1))
    if organization_id is None:
        organization = Organization(id=uuid.uuid4(), name="Тест-организация")
        session.add(organization)
        await session.flush()
        organization_id = organization.id

    article = DdsArticle(
        id=uuid.uuid4(),
        code=f"rent_{uuid.uuid4().hex[:8]}",
        name="Аренда тестовая",
        movement_type="outflow",
        activity_type="operating",
        location_required=True,
    )
    location = Location(
        id=uuid.uuid4(), organization_id=organization_id, name=f"Точка {uuid.uuid4().hex[:6]}"
    )
    landlord = Counterparty(
        id=uuid.uuid4(),
        name=f"Арендодатель {uuid.uuid4().hex[:6]}",
        type="individual",
        status="active",
    )
    session.add_all([article, location, landlord])
    await session.flush()
    session.add(
        CounterpartyPayableProfile(
            id=uuid.uuid4(), counterparty_id=landlord.id, relationship="informal"
        )
    )
    lease = LocationLease(
        id=uuid.uuid4(),
        location_id=location.id,
        counterparty_id=landlord.id,
        monthly_amount=Decimal(amount),
        started_on=date(2026, 1, 1),
        dds_article_id=article.id if with_article else None,
        payment_mode=payment_mode,
        payment_day=payment_day,
        documents_mode="informal",
        accrual_enabled=accrual_enabled,
    )
    session.add(lease)
    await session.flush()
    return lease


def test_accrual_creates_obligation_and_pnl_row(async_session_factory) -> None:
    """Аренда без документов — всё равно долг: закрывающий документ + строка признания."""

    async def run() -> None:
        async with async_session_factory() as session:
            lease = await _lease(session)
            invoice = await ensure_lease_invoice(session, lease, date(2026, 6, 1))
            assert invoice is not None
            assert invoice.amount == Decimal("100000")
            assert invoice.doc_kind == "closing"
            # finance — иначе правило 1 и авто-зачёт эту накладную не увидят.
            assert invoice.operational_scope == "finance"
            assert invoice.service_period_start == date(2026, 6, 1)
            assert invoice.service_period_end == date(2026, 6, 30)

            accrual = await session.scalar(
                select(SupplierExpenseAccrual).where(
                    SupplierExpenseAccrual.invoice_id == invoice.id
                )
            )
            assert accrual is not None
            assert accrual.amount == Decimal("100000")
            await session.rollback()

    asyncio.run(run())


def test_accrual_is_idempotent(async_session_factory) -> None:
    """Повторный прогон джобы за тот же месяц не задваивает долг."""

    async def run() -> None:
        async with async_session_factory() as session:
            lease = await _lease(session)
            first = await ensure_lease_invoice(session, lease, date(2026, 6, 1))
            second = await ensure_lease_invoice(session, lease, date(2026, 6, 1))
            assert first is not None
            assert second is None

            count = len(
                (
                    await session.scalars(
                        select(SupplierInvoice).where(
                            SupplierInvoice.counterparty_id == lease.counterparty_id
                        )
                    )
                ).all()
            )
            assert count == 1
            await session.rollback()

    asyncio.run(run())


def test_future_dated_obligation_waits_for_its_date(async_session_factory) -> None:
    """Постоплата за текущий месяц не долг до конца месяца — канон закрывающих документов."""

    async def run() -> None:
        async with async_session_factory() as session:
            lease = await _lease(session, payment_mode="postpaid")
            future_month = date.today().replace(day=1)
            invoice = await ensure_lease_invoice(session, lease, future_month)
            assert invoice is not None
            # Дата документа — конец текущего месяца, значит он ещё не в силе.
            if invoice.invoice_date and invoice.invoice_date > date.today():
                assert invoice.activation_status == "pending"
            await session.rollback()

    asyncio.run(run())


def test_obligation_is_dated_by_period_end_regardless_of_payment_mode(
    async_session_factory,
) -> None:
    """Долг возникает, когда услуга оказана, а не когда за неё платят.

    Кейс владельца 23.07.2026: аренда за июль оплачена 1 июля вперёд. Если датировать
    обязательство июнем, платёж закрыл бы уже висящий долг и дебиторка не возникла бы. На
    деле 1 июля услуга ещё не оказана: деньги вперёд дают ДЗ, а закрывающий документ 31 июля
    её гасит.
    """

    async def run() -> None:
        async with async_session_factory() as session:
            prepaid = await _lease(session, payment_mode="prepaid", payment_day=1)
            assert invoice_date_for(prepaid, date(2026, 7, 1)) == date(2026, 7, 31)

            postpaid = await _lease(session, payment_mode="postpaid")
            assert invoice_date_for(postpaid, date(2026, 7, 1)) == date(2026, 7, 31)

            # Февраль короче — конец периода берётся по длине месяца, а не «31-м».
            assert invoice_date_for(prepaid, date(2026, 2, 1)) == date(2026, 2, 28)
            await session.rollback()

    asyncio.run(run())


def test_disabled_or_articleless_lease_does_not_accrue(async_session_factory) -> None:
    """Выключенное начисление и договор без статьи обязательств не порождают."""

    async def run() -> None:
        async with async_session_factory() as session:
            disabled = await _lease(session, accrual_enabled=False)
            assert await ensure_lease_invoice(session, disabled, date(2026, 6, 1)) is None

            articleless = await _lease(session, with_article=False)
            assert await ensure_lease_invoice(session, articleless, date(2026, 6, 1)) is None
            await session.rollback()

    asyncio.run(run())


def test_lease_not_active_in_month_does_not_accrue(async_session_factory) -> None:
    """За месяцы до начала и после окончания договора долг не начисляется."""

    async def run() -> None:
        async with async_session_factory() as session:
            lease = await _lease(session)
            lease.started_on = date(2026, 5, 1)
            lease.ended_on = date(2026, 6, 30)
            await session.flush()

            assert await ensure_lease_invoice(session, lease, date(2026, 4, 1)) is None
            assert await ensure_lease_invoice(session, lease, date(2026, 7, 1)) is None
            assert await ensure_lease_invoice(session, lease, date(2026, 6, 1)) is not None
            await session.rollback()

    asyncio.run(run())


def test_rebuild_updates_open_pending_obligation(async_session_factory) -> None:
    """Смена ставки доходит до открытого (pending) обязательства — и до строки P&L."""

    async def run() -> None:
        async with async_session_factory() as session:
            lease = await _lease(session, amount="100000")
            future = date(2099, 1, 1)
            invoice = await ensure_lease_invoice(session, lease, future)
            assert invoice is not None
            assert invoice.activation_status == "pending"

            lease.monthly_amount = Decimal("50000")
            result, action = await rebuild_lease_invoice(session, lease, future)
            assert action == "updated"
            assert result is not None
            assert result.amount == Decimal("50000")

            accrual = await session.scalar(
                select(SupplierExpenseAccrual).where(
                    SupplierExpenseAccrual.invoice_id == invoice.id
                )
            )
            assert accrual is not None
            assert accrual.amount == Decimal("50000")
            await session.rollback()

    asyncio.run(run())


def test_rebuild_keeps_obligation_in_force(async_session_factory) -> None:
    """Обязательство в силе — история: пересбор его не переписывает даже при смене ставки."""

    async def run() -> None:
        async with async_session_factory() as session:
            lease = await _lease(session, amount="100000")
            invoice = await ensure_lease_invoice(session, lease, date(2099, 1, 1))
            assert invoice is not None
            invoice.activation_status = "active"  # документ уже вступил в силу
            await session.flush()

            lease.monthly_amount = Decimal("50000")
            result, action = await rebuild_lease_invoice(session, lease, date(2099, 1, 1))
            assert action == "kept"
            assert result is not None
            assert result.amount == Decimal("100000")
            await session.rollback()

    asyncio.run(run())


def test_rebuild_without_create_skips_missing(async_session_factory) -> None:
    """Правка договора не заводит обязательство там, где начисления ещё не было."""

    async def run() -> None:
        async with async_session_factory() as session:
            lease = await _lease(session)
            result, action = await rebuild_lease_invoice(
                session, lease, date(2099, 1, 1), create_if_missing=False
            )
            assert result is None
            assert action == "skipped"
            created = await session.scalar(
                select(SupplierInvoice).where(SupplierInvoice.source == "lease")
            )
            assert created is None
            await session.rollback()

    asyncio.run(run())


async def _safe_wallet(session: AsyncSession) -> Wallet:
    wallet = await session.scalar(select(Wallet).where(Wallet.type == "cash_safe").limit(1))
    if wallet is None:
        wallet = Wallet(id=uuid.uuid4(), name="Сейф", type="cash_safe", status="active")
        session.add(wallet)
        await session.flush()
    return wallet


async def _cash_leg(
    session: AsyncSession, lease: LocationLease, *, amount: str, on: date
) -> CashflowTransaction:
    wallet = await _safe_wallet(session)
    txn = CashflowTransaction(
        wallet_id=wallet.id,
        direction="out",
        amount=Decimal(amount),
        operation_date=on,
        article_id=lease.dds_article_id,
        counterparty_id=lease.counterparty_id,
        location_id=lease.location_id,
        lease_id=lease.id,
        source_kind="safe_payout",
        quality_status="final",
    )
    session.add(txn)
    await session.flush()
    return txn


async def _prepayments(session: AsyncSession, lease: LocationLease) -> list[SupplierPrepayment]:
    return list(
        (
            await session.scalars(
                select(SupplierPrepayment).where(
                    SupplierPrepayment.counterparty_id == lease.counterparty_id
                )
            )
        ).all()
    )


def test_cash_payment_settles_lease_obligation(async_session_factory) -> None:
    """Наличная оплата гасит ДЕЙСТВУЮЩИЙ долг адресно и дебиторки при этом не плодит."""

    async def run() -> None:
        async with async_session_factory() as session:
            lease = await _lease(session)
            invoice = await ensure_lease_invoice(
                session, lease, date(2026, 6, 1), as_of=date(2026, 7, 1)
            )
            assert invoice is not None
            assert invoice.activation_status == "active"

            txn = await _cash_leg(session, lease, amount="100000", on=date(2026, 7, 1))
            await settle_lease_invoice_from_cash(
                session, lease_id=lease.id, transaction_id=txn.id, amount=Decimal("100000")
            )
            await session.flush()
            await session.refresh(invoice)
            assert invoice.payment_status == "paid"
            assert await _prepayments(session, lease) == []
            await session.rollback()

    asyncio.run(run())


def test_cash_payment_to_pending_obligation_becomes_receivable(async_session_factory) -> None:
    """Правило 4: будущий документ — ещё не долг, платить по нему нельзя, деньги = дебиторка.

    Прод-кейс 30.07.2026 (Виталий): выплата из Сейфа закрыла арендный документ от 31.07 за день
    до его вступления в силу. Предоплаты не возникло, гасить было нечего — 50 000 выпали из ДЗ/КЗ.
    """

    async def run() -> None:
        async with async_session_factory() as session:
            lease = await _lease(session, payment_mode="prepaid", payment_day=28, amount="50000")
            invoice = await ensure_lease_invoice(
                session, lease, date(2026, 7, 1), as_of=date(2026, 7, 30)
            )
            assert invoice is not None
            assert invoice.activation_status == "pending"

            txn = await _cash_leg(session, lease, amount="50000", on=date(2026, 7, 30))
            await settle_lease_invoice_from_cash(
                session, lease_id=lease.id, transaction_id=txn.id, amount=Decimal("50000")
            )
            await session.flush()
            await session.refresh(invoice)

            assert invoice.payment_status == "unpaid"
            assert invoice.activation_status == "pending"
            allocated = await session.scalar(
                select(func.coalesce(func.sum(InvoicePaymentAllocation.amount), 0)).where(
                    InvoicePaymentAllocation.invoice_id == invoice.id
                )
            )
            assert Decimal(allocated) == Decimal("0")

            prepayments = await _prepayments(session, lease)
            assert len(prepayments) == 1
            assert Decimal(prepayments[0].amount) == Decimal("50000")
            assert prepayments[0].status == "open"
            assert prepayments[0].cashflow_transaction_id == txn.id
            # Не залог: колонка lease_id в леджере договора означает именно залог.
            assert prepayments[0].lease_id is None
            await session.rollback()

    asyncio.run(run())


def test_activation_settles_receivable_left_by_cash_prepayment(async_session_factory) -> None:
    """Сквозной сценарий владельца: два аванса → ДЗ 100 000, документ 31.07 съедает один."""

    async def run() -> None:
        async with async_session_factory() as session:
            lease = await _lease(session, payment_mode="prepaid", payment_day=28, amount="50000")
            invoice = await ensure_lease_invoice(
                session, lease, date(2026, 7, 1), as_of=date(2026, 7, 30)
            )
            assert invoice is not None

            for day in (date(2026, 7, 1), date(2026, 7, 30)):
                txn = await _cash_leg(session, lease, amount="50000", on=day)
                await settle_lease_invoice_from_cash(
                    session, lease_id=lease.id, transaction_id=txn.id, amount=Decimal("50000")
                )
            await session.flush()

            assert await counterparty_prepayment_balance(
                session, lease.counterparty_id
            ) == Decimal("100000.00")

            await activate_due_closing_invoices(
                session, as_of=date(2026, 7, 31), commit=False
            )
            await session.refresh(invoice)
            assert invoice.activation_status == "active"
            assert invoice.payment_status == "paid"
            assert await counterparty_prepayment_balance(
                session, lease.counterparty_id
            ) == Decimal("50000.00")
            await session.rollback()

    asyncio.run(run())


def test_cash_payment_splits_between_active_debt_and_receivable(async_session_factory) -> None:
    """Смешанный платёж: сначала FIFO по действующим обязательствам, остаток — в дебиторку."""

    async def run() -> None:
        async with async_session_factory() as session:
            lease = await _lease(session, payment_mode="prepaid", payment_day=28, amount="50000")
            june = await ensure_lease_invoice(
                session, lease, date(2026, 6, 1), as_of=date(2026, 7, 30)
            )
            july = await ensure_lease_invoice(
                session, lease, date(2026, 7, 1), as_of=date(2026, 7, 30)
            )
            assert june is not None and july is not None
            assert june.activation_status == "active"
            assert july.activation_status == "pending"

            txn = await _cash_leg(session, lease, amount="80000", on=date(2026, 7, 30))
            await settle_lease_invoice_from_cash(
                session, lease_id=lease.id, transaction_id=txn.id, amount=Decimal("80000")
            )
            await session.flush()
            await session.refresh(june)
            await session.refresh(july)

            assert june.payment_status == "paid"
            assert july.payment_status == "unpaid"
            prepayments = await _prepayments(session, lease)
            assert len(prepayments) == 1
            assert Decimal(prepayments[0].amount) == Decimal("30000")
            await session.rollback()

    asyncio.run(run())


def test_safe_payout_with_lease_creates_receivable_end_to_end(async_session_factory) -> None:
    """Боевая дверь целиком: резерв Сейфа с lease_id → pay_allocation → дебиторка, не аллокация."""

    async def run() -> None:
        async with async_session_factory() as session:
            lease = await _lease(session, payment_mode="prepaid", payment_day=28, amount="50000")
            invoice = await ensure_lease_invoice(
                session, lease, date(2026, 7, 1), as_of=date(2026, 7, 30)
            )
            assert invoice is not None
            assert invoice.activation_status == "pending"

            wallet = await _safe_wallet(session)
            allocation = SafeAllocation(
                id=uuid.uuid4(),
                wallet_id=wallet.id,
                amount=Decimal("50000"),
                amount_paid=Decimal("0"),
                article_id=lease.dds_article_id,
                counterparty_id=lease.counterparty_id,
                purpose="Аренда торговых точек",
                status="reserved",
                location="safe",
                location_id=lease.location_id,
                lease_id=lease.id,
            )
            session.add(allocation)
            await session.flush()

            await pay_allocation(
                session,
                allocation,
                amount=Decimal("50000"),
                operation_date=date(2026, 7, 30),
            )
            await session.flush()
            await session.refresh(invoice)

            assert invoice.payment_status == "unpaid"
            prepayments = await _prepayments(session, lease)
            assert len(prepayments) == 1
            assert Decimal(prepayments[0].amount) == Decimal("50000")
            assert prepayments[0].wallet_id == wallet.id
            await session.rollback()

    asyncio.run(run())

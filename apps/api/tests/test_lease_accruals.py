"""Аренда как обязательство: начисление, попадание в ДЗ/КЗ и гашение платежом."""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CashflowTransaction,
    Counterparty,
    CounterpartyPayableProfile,
    DdsArticle,
    Location,
    LocationLease,
    Organization,
    SupplierExpenseAccrual,
    SupplierInvoice,
    Wallet,
)
from app.services.lease_accruals import (
    ensure_lease_invoice,
    invoice_date_for,
    rebuild_lease_invoice,
    settle_lease_invoice_from_cash,
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


def test_cash_payment_settles_lease_obligation(async_session_factory) -> None:
    """Наличная оплата гасит долг адресно: правило 1 живёт только в банковском контуре."""

    async def run() -> None:
        async with async_session_factory() as session:
            lease = await _lease(session)
            invoice = await ensure_lease_invoice(session, lease, date(2026, 6, 1))
            assert invoice is not None

            wallet = await session.scalar(select(Wallet).where(Wallet.type == "cash_safe").limit(1))
            if wallet is None:
                wallet = Wallet(
                    id=uuid.uuid4(), name="Сейф", type="cash_safe", status="active"
                )
                session.add(wallet)
                await session.flush()

            txn = CashflowTransaction(
                wallet_id=wallet.id,
                direction="out",
                amount=Decimal("100000"),
                operation_date=date(2026, 6, 30),
                article_id=lease.dds_article_id,
                counterparty_id=lease.counterparty_id,
                location_id=lease.location_id,
                lease_id=lease.id,
                source_kind="safe_payout",
                quality_status="final",
            )
            session.add(txn)
            await session.flush()

            await settle_lease_invoice_from_cash(
                session,
                lease_id=lease.id,
                transaction_id=txn.id,
                amount=Decimal("100000"),
                operation_date=date(2026, 6, 30),
            )
            await session.flush()
            await session.refresh(invoice)
            assert invoice.payment_status == "paid"
            await session.rollback()

    asyncio.run(run())

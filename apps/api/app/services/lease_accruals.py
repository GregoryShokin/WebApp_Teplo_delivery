"""Ежемесячное обязательство по аренде.

Аренда без документов — всё равно долг: договор известен, сумма известна, срок известен.
Поэтому раз в месяц по каждому действующему договору заводится закрывающий документ
``SupplierInvoice``, а не запись своей таблицы: только так обязательство попадает в готовые
витрины — КЗ считается по накладным (``list_counterparty_balances``), а признание расхода в
P&L — по ``SupplierExpenseAccrual`` с ``invoice_id`` (``list_supplier_accounting``).

Идемпотентность бесплатная: ключ ``lease:{lease_id}:{YYYY-MM}`` лежит под существующим
уникумом ``uq_supplier_invoice_source_external``, поэтому повторный прогон джобы (или ручной
пересбор) ничего не задваивает.

Дата документа зависит от порядка расчётов: постоплату признаём последним днём месяца, а
предоплату — днём платежа месяцем раньше. Будущую дату ``apply_closing_document`` сам положит
в ``pending`` и активирует ночью в свою дату — обязательство не появится раньше срока.

Уже созданные накладные при смене ставки НЕ переписываем: за прошлый месяц долг был другим,
и переписать его задним числом означало бы подделать историю. Новая ставка действует со
следующего месяца.
"""

from __future__ import annotations

import calendar
import uuid
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LocationLease, SupplierInvoice
from app.services import supplier_prepayments, supplier_service_periods

LEASE_INVOICE_SOURCE = "lease"


def lease_external_id(lease_id: uuid.UUID, month: date) -> str:
    return f"lease:{lease_id}:{month:%Y-%m}"


def month_bounds(month: date) -> tuple[date, date]:
    first = month.replace(day=1)
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    return first, last


def invoice_date_for(lease: LocationLease, month: date) -> date:
    """Дата обязательства — всегда конец периода аренды: тогда услуга и оказана.

    Порядок расчётов (``payment_mode``) на эту дату не влияет — он про то, КОГДА платят, а не
    когда возникает долг. Иначе предоплата ломала бы канон: владелец платит 1 июля за июль,
    и если датировать обязательство июнем, платёж просто закрыл бы уже висящий долг. На деле
    1 июля услуга ещё не оказана — деньги вперёд дают дебиторку (правило 1), а закрывающий
    документ 31 июля её гасит (правило 2). Ровно так это описал владелец 23.07.2026.
    """
    _first, last = month_bounds(month)
    return last


def lease_covers_month(lease: LocationLease, month: date) -> bool:
    first, last = month_bounds(month)
    if lease.started_on > last:
        return False
    return lease.ended_on is None or lease.ended_on >= first


async def ensure_lease_invoice(
    session: AsyncSession,
    lease: LocationLease,
    month: date,
) -> SupplierInvoice | None:
    """Завести обязательство по аренде за месяц. Идемпотентно. Не коммитит.

    Возвращает ``None``, если начислять нечего: выключено, нет статьи, договор не действовал
    в этом месяце или обязательство уже есть.
    """
    if not lease.accrual_enabled:
        return None
    if lease.dds_article_id is None:
        # Без статьи расход некуда отнести, а платёж по ней не найдёт этот договор.
        return None
    if Decimal(lease.monthly_amount) <= 0:
        return None
    if not lease_covers_month(lease, month):
        return None

    external_id = lease_external_id(lease.id, month)
    existing = await session.scalar(
        select(SupplierInvoice).where(
            SupplierInvoice.source == LEASE_INVOICE_SOURCE,
            SupplierInvoice.external_id == external_id,
        )
    )
    if existing is not None:
        return None

    period_start, period_end = month_bounds(month)
    invoice = SupplierInvoice(
        counterparty_id=lease.counterparty_id,
        source=LEASE_INVOICE_SOURCE,
        external_id=external_id,
        direction="payable",
        doc_kind="closing",
        # finance — иначе ни правило 1, ни авто-зачёт предоплат эту накладную не увидят.
        operational_scope="finance",
        number=f"Аренда {month:%m.%Y}",
        invoice_date=invoice_date_for(lease, month),
        amount=Decimal(lease.monthly_amount),
        dds_article_id=lease.dds_article_id,
        service_period_start=period_start,
        service_period_end=period_end,
        service_period_source="lease",
        # ready — без этого sync_invoice_accrual молча не создаст строку признания расхода.
        service_period_status="ready",
    )
    session.add(invoice)
    await session.flush()

    # Порядок важен: сначала проводим документ (активация или pending для будущей даты и
    # FIFO-гашение открытой дебиторки), затем строка признания расхода в P&L.
    await supplier_prepayments.apply_closing_document(session, invoice)
    await supplier_service_periods.sync_invoice_accrual(session, invoice)
    return invoice


async def rebuild_lease_invoice(
    session: AsyncSession,
    lease: LocationLease,
    month: date,
    *,
    create_if_missing: bool = True,
) -> tuple[SupplierInvoice | None, str]:
    """Пересобрать обязательство за месяц под ТЕКУЩИЕ условия договора. Не коммитит.

    Прошлый месяц в силе не переписываем — это история: правим только ОТКРЫТОЕ обязательство
    (будущий УПД, ``activation_status='pending'``, ещё не долг и без оплат). Так смена ставки в
    текущем месяце доходит до ДЗ/КЗ, а закрытые месяцы остаются как были.

    ``create_if_missing=False`` — только обновить существующее, не заводить новое (вызов из
    правки договора не должен неожиданно начислять там, где начисления ещё не было).

    Возвращает ``(invoice, action)`` с action ``created`` | ``updated`` | ``kept`` | ``skipped``.
    """
    from app.services import counterparty_matching

    external_id = lease_external_id(lease.id, month)
    existing = await session.scalar(
        select(SupplierInvoice).where(
            SupplierInvoice.source == LEASE_INVOICE_SOURCE,
            SupplierInvoice.external_id == external_id,
        )
    )
    if existing is None:
        if not create_if_missing:
            return None, "skipped"
        created = await ensure_lease_invoice(session, lease, month)
        return created, "created" if created is not None else "skipped"

    paid = await counterparty_matching._allocated_amount(session, existing.id)
    if existing.activation_status != "pending" or paid > 0:
        return existing, "kept"

    new_amount = Decimal(lease.monthly_amount)
    if Decimal(existing.amount) != new_amount:
        existing.amount = new_amount
        await session.flush()
        await supplier_service_periods.sync_invoice_accrual(session, existing)
    return existing, "updated"


async def accrue_month(session: AsyncSession, month: date) -> list[SupplierInvoice]:
    """Начислить аренду за месяц по всем действующим договорам. Не коммитит."""
    leases = (
        await session.scalars(
            select(LocationLease).where(
                LocationLease.accrual_enabled.is_(True),
                LocationLease.dds_article_id.is_not(None),
            )
        )
    ).all()
    created: list[SupplierInvoice] = []
    for lease in leases:
        invoice = await ensure_lease_invoice(session, lease, month)
        if invoice is not None:
            created.append(invoice)
    return created


async def settle_lease_invoice_from_cash(
    session: AsyncSession,
    *,
    lease_id: UUID,
    transaction_id: UUID,
    amount: Decimal,
    operation_date: date,
) -> None:
    """Погасить арендное обязательство наличной оплатой.

    Банковский платёж закрывает аренду сам — правило 1 (``ensure_prepayment_from_bank_transaction``)
    ищет открытую КЗ контрагента. Наличный контур этого правила не зовёт, поэтому здесь гасим
    адресно: только по конкретному договору и только его обязательство, без побочек на весь
    наличный контур.

    Месяц берём по дате операции; если за него обязательства ещё нет (заплатили вперёд), берём
    ближайшее непогашенное этого договора — платёж не должен повисать в никуда.
    """
    from app.models import InvoicePaymentAllocation
    from app.services import counterparty_matching

    month_key = lease_external_id(lease_id, operation_date)
    invoice = await session.scalar(
        select(SupplierInvoice).where(
            SupplierInvoice.source == LEASE_INVOICE_SOURCE,
            SupplierInvoice.external_id == month_key,
            SupplierInvoice.payment_status != "paid",
        )
    )
    if invoice is None:
        prefix = f"lease:{lease_id}:"
        invoice = await session.scalar(
            select(SupplierInvoice)
            .where(
                SupplierInvoice.source == LEASE_INVOICE_SOURCE,
                SupplierInvoice.external_id.like(f"{prefix}%"),
                SupplierInvoice.payment_status != "paid",
            )
            .order_by(SupplierInvoice.invoice_date)
        )
    if invoice is None:
        return

    allocated = await counterparty_matching._allocated_amount(session, invoice.id)
    outstanding = Decimal(invoice.amount) - allocated
    if outstanding <= 0:
        return

    session.add(
        InvoicePaymentAllocation(
            invoice_id=invoice.id,
            source_kind="cash",
            cashflow_transaction_id=transaction_id,
            amount=min(outstanding, amount),
        )
    )
    await session.flush()
    await counterparty_matching._recompute_status(session, invoice)

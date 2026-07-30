"""Ежемесячное обязательство по аренде.

Аренда без документов — всё равно долг: договор известен, сумма известна, срок известен.
Поэтому раз в месяц по каждому действующему договору заводится закрывающий документ
``SupplierInvoice``, а не запись своей таблицы: только так обязательство попадает в готовые
витрины — КЗ считается по накладным (``list_counterparty_balances``), а признание расхода в
P&L — по ``SupplierExpenseAccrual`` с ``invoice_id`` (``list_supplier_accounting``).

Идемпотентность бесплатная: ключ ``lease:{lease_id}:{YYYY-MM}`` лежит под существующим
уникумом ``uq_supplier_invoice_source_external``, поэтому повторный прогон джобы (или ручной
пересбор) ничего не задваивает.

Дата документа — всегда последний день периода, независимо от порядка расчётов (решение
владельца 23.07, см. ``invoice_date_for``). Значит текущий месяц почти весь стоит в ``pending``:
``apply_closing_document`` кладёт будущую дату в ожидание, а активирует её ночная джоба в свою
дату — обязательство не появляется раньше срока, и до него деньги вперёд — это дебиторка.

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
    *,
    as_of: date | None = None,
) -> SupplierInvoice | None:
    """Завести обязательство по аренде за месяц. Идемпотентно. Не коммитит.

    Возвращает ``None``, если начислять нечего: выключено, нет статьи, договор не действовал
    в этом месяце или обязательство уже есть.

    ``as_of`` — «сегодня» для правила 4 (вступил документ в силу или лёг в ``pending``); по
    умолчанию реальная дата. Явный параметр нужен тестам: без него активность накладной зависит
    от дня прогона, и сценарий «платёж пришёл к ещё не вступившему в силу документу» на одних
    датах исполняется, а на других — нет. Ровно на этом дефект гашения из Сейфа и прожил месяц.
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
    await supplier_prepayments.apply_closing_document(session, invoice, as_of=as_of)
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
    wallet_id: UUID | None = None,
    created_by_user_id: UUID | None = None,
) -> None:
    """Провести наличную выплату по договору аренды по канону ДЗ/КЗ. Не коммитит.

    Канон владельца (17.07) не различает канал денег: платёж гасит ДЕЙСТВУЮЩИЕ обязательства
    FIFO, а всё, чему обязательства ещё нет, становится дебиторкой. Банковский платёж проходит
    обе половины сам — правилом 1 (``ensure_prepayment_from_bank_transaction``). Наличный контур
    в правило 1 не заходит (``safe_payout`` в ``DEDICATED_MONEY_SOURCE_KINDS``: слепой FIFO снял
    бы адресную привязку к договору), поэтому тот же канон исполняем здесь — но только по
    накладным СВОЕГО договора.

    Гасим ТОЛЬКО ``activation_status='active'``. Будущий закрывающий документ (правило 4: аренда
    за июль датирована 31.07) обязательством ещё не является, и платить по нему нельзя. Раньше
    фильтра не было, и на этом терялись деньги: выплата 30.07.2026 по Виталию закрыла июльский
    документ за день до его вступления в силу — предоплаты не возникло, гасить было нечего, и
    50 000 просто выпали из ДЗ/КЗ (витрина показала 50 000 вместо 100 000). Вторая половина —
    остаток в дебиторку — обязательна: один фильтр без неё оставил бы платёж вообще без следа.

    Месяц платежа роли не играет: ``payment_mode`` — про то, КОГДА платят, а не когда возникает
    долг (решение владельца 23.07, см. ``invoice_date_for``). Гасим строго по возрастанию даты
    документа, как FIFO правила 1.
    """
    from app.models import InvoicePaymentAllocation, SupplierPrepayment
    from app.services import counterparty_matching

    if amount <= 0:
        return
    lease = await session.get(LocationLease, lease_id)
    if lease is None:
        return

    invoices = (
        await session.scalars(
            select(SupplierInvoice)
            .where(
                SupplierInvoice.source == LEASE_INVOICE_SOURCE,
                SupplierInvoice.external_id.like(f"lease:{lease_id}:%"),
                SupplierInvoice.payment_status.in_(("unpaid", "partially_paid")),
                SupplierInvoice.activation_status == "active",
            )
            .order_by(SupplierInvoice.invoice_date, SupplierInvoice.created_at)
        )
    ).all()

    left = amount
    for invoice in invoices:
        if left <= 0:
            break
        allocated = await counterparty_matching._allocated_amount(session, invoice.id)
        outstanding = Decimal(invoice.amount) - allocated
        if outstanding <= 0:
            continue
        part = min(outstanding, left)
        session.add(
            InvoicePaymentAllocation(
                invoice_id=invoice.id,
                source_kind="cash",
                cashflow_transaction_id=transaction_id,
                amount=part,
                created_by_user_id=created_by_user_id,
            )
        )
        await session.flush()
        await counterparty_matching._recompute_status(session, invoice)
        left -= part

    if left <= 0:
        return
    # Действующих обязательств не осталось — деньги вперёд. Вид ``subscription`` тот же, что у
    # правила 1: аванс не целевой, поэтому ``auto_settle_invoice_from_open_prepayments`` сам
    # зачтёт его, когда документ вступит в силу. ``lease_id`` НЕ проставляем: в леджере договора
    # эта колонка означает залог (``deposit_outstanding``), а это обычная предоплата за период.
    session.add(
        SupplierPrepayment(
            counterparty_id=lease.counterparty_id,
            kind=supplier_prepayments.RULE1_PREPAYMENT_KIND,
            wallet_id=wallet_id,
            amount=left,
            amount_settled=Decimal("0.00"),
            status="open",
            cashflow_transaction_id=transaction_id,
            article_id=lease.dds_article_id,
            note="Аренда вперёд: обязательство ещё не вступило в силу",
            created_by_user_id=created_by_user_id,
        )
    )
    await session.flush()

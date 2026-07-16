"""Периоды оказания услуг, ДЗ/КЗ и признание расходов поставщиков.

ДДС отвечает только за факт движения денег. Этот модуль ведёт начисление независимо:
оплата до конца периода остаётся дебиторкой, а после конца периода расход признаётся в P&L;
неоплаченный признанный расход образует кредиторку.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ExpenseDraftLine,
    SupplierExpenseAccrual,
    SupplierInvoice,
    SupplierServicePeriodChange,
)

# Признание идёт по московской дате: джоба запускается в 00:05 МСК, а UTC-дата в этот
# момент ещё вчерашняя — по ней расход признавался бы на сутки позже срока.
MOSCOW_TZ = ZoneInfo("Europe/Moscow")


class ServicePeriodError(ValueError):
    pass


def validate_period(start: date | None, end: date | None) -> tuple[date, date]:
    if start is None or end is None:
        raise ServicePeriodError("Укажите начало и окончание периода оказания услуги")
    if end < start:
        raise ServicePeriodError("Окончание периода не может быть раньше начала")
    return start, end


def recognition_month(period_end: date) -> date:
    return date(period_end.year, period_end.month, 1)


def effective_period_status(stored_status: str, *, required: bool) -> str:
    """Честный статус для выдачи: у контрагента с обязательным периодом ``not_required`` —
    это «ещё не заполнен» (накладная не из почты), а не «не нужен». Показываем ``missing``,
    чтобы оператор видел, почему счёт нельзя отправить в банк."""
    if required and stored_status == "not_required":
        return "missing"
    return stored_status


async def sync_invoice_accrual(
    session: AsyncSession, invoice: SupplierInvoice
) -> SupplierExpenseAccrual | None:
    """Создать/обновить запись P&L для счёта с подтверждённым периодом. Не коммитит."""
    existing = await session.scalar(
        select(SupplierExpenseAccrual).where(SupplierExpenseAccrual.invoice_id == invoice.id)
    )
    if (
        invoice.service_period_status != "ready"
        or invoice.service_period_start is None
        or invoice.service_period_end is None
    ):
        return existing

    start, end = validate_period(invoice.service_period_start, invoice.service_period_end)
    if existing is None:
        existing = SupplierExpenseAccrual(
            counterparty_id=invoice.counterparty_id,
            invoice_id=invoice.id,
            payment_draft_id=invoice.draft_id,
            article_id=invoice.dds_article_id,
            amount=invoice.amount,
            service_period_start=start,
            service_period_end=end,
            status="scheduled",
        )
        session.add(existing)
    else:
        existing.counterparty_id = invoice.counterparty_id
        existing.payment_draft_id = invoice.draft_id
        existing.article_id = invoice.dds_article_id
        # Сумму и период признанного расхода не двигаем молча: он уже в P&L закрытого
        # месяца. Привязки (контрагент/черновик/статья) обновлять безопасно — они не
        # меняют величину расхода. Корректировка признанной суммы — только осознанно.
        if existing.status != "recognized":
            existing.amount = invoice.amount
            existing.service_period_start = start
            existing.service_period_end = end
    await session.flush()
    return existing


async def sync_expense_line_accrual(
    session: AsyncSession,
    line: ExpenseDraftLine,
) -> SupplierExpenseAccrual | None:
    """Создать начисление по строке ручного платежа, если у неё указан получатель и период."""
    if (
        line.counterparty_id is None
        or line.service_period_start is None
        or line.service_period_end is None
    ):
        return None
    start, end = validate_period(line.service_period_start, line.service_period_end)
    existing = await session.scalar(
        select(SupplierExpenseAccrual).where(
            SupplierExpenseAccrual.expense_draft_line_id == line.id
        )
    )
    if existing is None:
        existing = SupplierExpenseAccrual(
            counterparty_id=line.counterparty_id,
            expense_draft_line_id=line.id,
            payment_draft_id=line.draft_id,
            article_id=line.article_id,
            amount=line.amount,
            service_period_start=start,
            service_period_end=end,
            status="scheduled",
        )
        session.add(existing)
    else:
        existing.counterparty_id = line.counterparty_id
        existing.payment_draft_id = line.draft_id
        existing.article_id = line.article_id
        # Признанный расход не двигаем молча (см. sync_invoice_accrual).
        if existing.status != "recognized":
            existing.amount = line.amount
            existing.service_period_start = start
            existing.service_period_end = end
    await session.flush()
    return existing


async def recognize_due_expenses(
    session: AsyncSession, *, as_of: date | None = None, commit: bool = True
) -> int:
    """Признать расходы после завершения дня окончания услуги.

    ``service_period_end < as_of`` намеренно строго: весь последний день услуга ещё
    оказывается, признание выполняется первым запуском следующего дня.
    """
    cutoff = as_of or datetime.now(MOSCOW_TZ).date()
    rows = list(
        (
            await session.scalars(
                select(SupplierExpenseAccrual)
                .where(
                    SupplierExpenseAccrual.status == "scheduled",
                    SupplierExpenseAccrual.service_period_end < cutoff,
                )
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    now = datetime.now(UTC)
    for row in rows:
        row.status = "recognized"
        row.recognition_month = recognition_month(row.service_period_end)
        row.recognized_at = now
    if commit:
        await session.commit()
    else:
        await session.flush()
    return len(rows)


async def change_accrual_period(
    session: AsyncSession,
    *,
    accrual: SupplierExpenseAccrual,
    start: date,
    end: date,
    actor_user_id: uuid.UUID | None,
    reason: str | None = None,
) -> SupplierExpenseAccrual:
    """Перенести период и записать полный аудит. Проверку права делает API-слой."""
    start, end = validate_period(start, end)
    old_start = accrual.service_period_start
    old_end = accrual.service_period_end
    old_month = accrual.recognition_month
    new_month = recognition_month(end) if accrual.status == "recognized" else None
    session.add(
        SupplierServicePeriodChange(
            accrual_id=accrual.id,
            old_period_start=old_start,
            old_period_end=old_end,
            new_period_start=start,
            new_period_end=end,
            old_recognition_month=old_month,
            new_recognition_month=new_month,
            actor_user_id=actor_user_id,
            reason=(reason or "").strip() or None,
        )
    )
    accrual.service_period_start = start
    accrual.service_period_end = end
    if accrual.status == "recognized":
        accrual.recognition_month = new_month

    if accrual.invoice_id is not None:
        invoice = await session.get(SupplierInvoice, accrual.invoice_id)
        if invoice is not None:
            invoice.service_period_start = start
            invoice.service_period_end = end
            invoice.service_period_source = "corrected"
            invoice.service_period_status = "ready"
    if accrual.expense_draft_line_id is not None:
        line = await session.get(ExpenseDraftLine, accrual.expense_draft_line_id)
        if line is not None:
            line.service_period_start = start
            line.service_period_end = end
            line.service_period_source = "corrected"
    await session.commit()
    await session.refresh(accrual)
    return accrual


async def set_invoice_service_period(
    session: AsyncSession,
    *,
    invoice: SupplierInvoice,
    start: date,
    end: date,
    actor_user_id: uuid.UUID | None,
    reason: str | None = None,
) -> SupplierExpenseAccrual:
    """Проставить период у накладной, у которой начисления ещё нет.

    Это единственный вход, разрывающий chicken-and-egg периодов: ``sync_invoice_accrual``
    заводит начисление только при ``status='ready'``, а перенести период у существующего
    начисления умеет ``change_accrual_period`` — но начисления нет, пока период не проставлен.
    Здесь период пишется прямо в накладную, статус переводится в ``ready``, и начисление
    рождается тем же ``sync_invoice_accrual``. Если начисление уже есть (накладная из почты
    или повторная правка) — отдаём штатному ``change_accrual_period`` ради полного аудита.
    """
    start, end = validate_period(start, end)
    existing = await session.scalar(
        select(SupplierExpenseAccrual).where(SupplierExpenseAccrual.invoice_id == invoice.id)
    )
    if existing is not None:
        return await change_accrual_period(
            session,
            accrual=existing,
            start=start,
            end=end,
            actor_user_id=actor_user_id,
            reason=reason,
        )

    # Первичная установка — не «перенос», поэтому в журнал SupplierServicePeriodChange
    # (у него old_* обязательны) не пишем: факт ручного ввода несёт source='manual' на
    # накладной, а последующие правки идут через change_accrual_period с полным аудитом.
    invoice.service_period_start = start
    invoice.service_period_end = end
    invoice.service_period_source = "manual"
    invoice.service_period_status = "ready"
    accrual = await sync_invoice_accrual(session, invoice)
    await session.commit()
    if accrual is not None:
        await session.refresh(accrual)
    return accrual


async def cancel_invoice_accrual(
    session: AsyncSession, invoice_id: uuid.UUID
) -> SupplierExpenseAccrual | None:
    """Отменить начисление аннулированной накладной. Не коммитит.

    Начисление рождается при ``ready`` независимо от оплаты, а аннулирование накладной его
    не трогало — джоба признания превращала расход по несуществующей накладной в фантом в
    P&L. Здесь начисление переводится в ``cancelled`` (джоба его пропускает, отчёт исключает).
    Если расход уже признан — аннулирование накладной сторнирует его: накладной нет, значит и
    расхода нет.
    """
    accrual = await session.scalar(
        select(SupplierExpenseAccrual).where(SupplierExpenseAccrual.invoice_id == invoice_id)
    )
    if accrual is None or accrual.status == "cancelled":
        return accrual
    accrual.status = "cancelled"
    await session.flush()
    return accrual


def money(value: Decimal | int | float | None) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))

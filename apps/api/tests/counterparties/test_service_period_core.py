"""Ядро признания расходов по периодам услуг: отмена, заморозка признанного, граница дня.

Закрывает дыры, у которых не было ни одного теста:
- аннулирование накладной отменяет её начисление (иначе джоба признаёт фантомный расход);
- сумма уже признанного начисления не двигается молча при повторной синхронизации;
- признание строго после последнего дня периода, месяц P&L — по концу периода.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from cp_helpers import make_counterparty, make_invoice
from sqlalchemy import select

from app.models import CounterpartyPayableProfile, SupplierExpenseAccrual
from app.services.counterparty_registry import void_invoice
from app.services.supplier_service_periods import (
    change_accrual_period,
    recognize_due_expenses,
    set_invoice_service_period,
    sync_invoice_accrual,
)

pytestmark = pytest.mark.usefixtures("migrated_db")


async def _require_period(session, counterparty_id) -> None:
    profile = await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == counterparty_id
        )
    )
    profile.service_period_required = True
    await session.flush()


async def _accrual(session, invoice_id) -> SupplierExpenseAccrual | None:
    return await session.scalar(
        select(SupplierExpenseAccrual).where(SupplierExpenseAccrual.invoice_id == invoice_id)
    )


async def test_void_cancels_accrual_and_recognition_skips_it(async_session_factory):
    """Аннулирование накладной переводит её начисление в cancelled, и джоба его не признаёт."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ООО «Аренда»", inn="7710000001")
        await _require_period(session, cp.id)
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="5000.00")
        await session.commit()
        await set_invoice_service_period(
            session, invoice=invoice, start=date(2026, 6, 1), end=date(2026, 6, 30),
            actor_user_id=None,
        )
        accrual = await _accrual(session, invoice.id)
        assert accrual is not None and accrual.status == "scheduled"

        await void_invoice(session, invoice.id)

        await session.refresh(accrual)
        assert accrual.status == "cancelled"

        # Джоба признания начисление аннулированной накладной не трогает.
        recognized = await recognize_due_expenses(
            session, as_of=date(2027, 1, 1), commit=False
        )
        assert recognized == 0
        await session.refresh(accrual)
        assert accrual.status == "cancelled"


async def test_recognized_amount_is_frozen_on_resync(async_session_factory):
    """Сумма признанного начисления не меняется при повторной синхронизации накладной."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ООО «Хостинг»", inn="7710000002")
        await _require_period(session, cp.id)
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="5000.00")
        await session.commit()
        await set_invoice_service_period(
            session, invoice=invoice, start=date(2026, 6, 1), end=date(2026, 6, 30),
            actor_user_id=None,
        )
        # Признаём расход (период истёк).
        await recognize_due_expenses(session, as_of=date(2026, 7, 1), commit=True)
        accrual = await _accrual(session, invoice.id)
        assert accrual.status == "recognized"

        # Сумма накладной изменилась и её пересинхронизировали — признанное не двигаем.
        invoice.amount = Decimal("9999.00")
        await sync_invoice_accrual(session, invoice)
        await session.refresh(accrual)
        assert accrual.status == "recognized"
        assert accrual.amount == Decimal("5000.00")


async def test_scheduled_amount_still_tracks_invoice(async_session_factory):
    """До признания сумма начисления следует за накладной (заморозка — только у recognized)."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ООО «Связь»", inn="7710000003")
        await _require_period(session, cp.id)
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="5000.00")
        await session.commit()
        await set_invoice_service_period(
            session, invoice=invoice, start=date(2026, 6, 1), end=date(2026, 6, 30),
            actor_user_id=None,
        )
        invoice.amount = Decimal("6200.00")
        await sync_invoice_accrual(session, invoice)
        accrual = await _accrual(session, invoice.id)
        assert accrual.status == "scheduled"
        assert accrual.amount == Decimal("6200.00")


async def test_correcting_recognized_to_future_period_reverts_to_scheduled(
    async_session_factory,
):
    """Признанный расход, чей период поправили в БУДУЩЕЕ, признанным быть не может:
    услуга ещё оказывается. Возвращается в scheduled — ночная джоба признает заново,
    когда период реально закончится (иначе P&L показывал бы расход будущего периода)."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ООО «Реклама»", inn="7710000005")
        await _require_period(session, cp.id)
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="4000.00")
        await session.commit()
        await set_invoice_service_period(
            session, invoice=invoice, start=date(2026, 6, 1), end=date(2026, 6, 30),
            actor_user_id=None,
        )
        await recognize_due_expenses(session, as_of=date(2026, 7, 1), commit=True)
        accrual = await _accrual(session, invoice.id)
        assert accrual.status == "recognized"

        # Корректировка периода на будущий месяц (относительно СЕГОДНЯ).
        today = datetime.now(ZoneInfo("Europe/Moscow")).date()
        future_start = today + timedelta(days=15)
        future_end = today + timedelta(days=44)
        await change_accrual_period(
            session,
            accrual=accrual,
            start=future_start,
            end=future_end,
            actor_user_id=None,
            reason="перенос по акту",
        )
        await session.commit()
        await session.refresh(accrual)
        assert accrual.status == "scheduled"  # признание снято
        assert accrual.recognition_month is None
        assert accrual.recognized_at is None

        # Прошедшая корректировка признанного остаётся признанной (месяц пересчитан).
        await change_accrual_period(
            session,
            accrual=accrual,
            start=date(2026, 5, 1),
            end=date(2026, 5, 31),
            actor_user_id=None,
            reason="вернули назад",
        )
        # (после отката в scheduled статус recognized не возвращается сам — признает джоба)
        await recognize_due_expenses(session, as_of=date(2026, 7, 1), commit=True)
        await session.refresh(accrual)
        assert accrual.status == "recognized"
        assert accrual.recognition_month == date(2026, 5, 1)


async def test_recognition_is_strict_after_last_day(async_session_factory):
    """Признание — строго после последнего дня периода; месяц P&L берётся из конца периода."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="ООО «Клининг»", inn="7710000004")
        await _require_period(session, cp.id)
        invoice = await make_invoice(session, counterparty_id=cp.id, amount="3000.00")
        await session.commit()
        await set_invoice_service_period(
            session, invoice=invoice, start=date(2026, 6, 1), end=date(2026, 6, 30),
            actor_user_id=None,
        )

        # В последний день услуги ещё не признаём (граница строгая).
        assert await recognize_due_expenses(session, as_of=date(2026, 6, 30), commit=False) == 0
        accrual = await _accrual(session, invoice.id)
        assert accrual.status == "scheduled"

        # Первый день следующего периода — признаём, месяц = месяц окончания.
        assert await recognize_due_expenses(session, as_of=date(2026, 7, 1), commit=True) == 1
        await session.refresh(accrual)
        assert accrual.status == "recognized"
        assert accrual.recognition_month == date(2026, 6, 1)

"""Признание закрывающего режима «счёт + УПД», у которого периода нет вовсе.

У режима 3 (``service_billing_mode='per_invoice'``) расход обязан приходить документом:
платежи такого контрагента из кассового расхода исключены. Но период у его УПД пуст
(``not_required``), а начисление заводилось строго при ``ready`` — и документ приходил, не
принося расхода. За июль 2026 так потерялись 11 695,54 ₽ Манго Телеком: строка
«Телекоммуникации» показывала 3 230 ₽ вместо 14 925,54 ₽.

Период берём из ДАТЫ ДОКУМЕНТА: акт за услуги месяца датируется его последним днём — на
проде все четыре таких документа были ровно последним днём своего месяца.

Ограничитель здесь важнее самой правки, поэтому на каждое его условие есть свой тест: без
них начисления получили бы 142 товарные накладные июля (1,53 млн ₽, задвоение с фудкостом)
и неразмеченные контрагенты (22 074,65 ₽, задвоение с кассой).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from cp_helpers import make_counterparty, make_invoice
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import CounterpartyPayableProfile, SupplierExpenseAccrual
from app.services import supplier_service_periods

JULY_DOC = date(2026, 7, 31)


async def _service_counterparty(session: AsyncSession, *, name: str, mode: str | None):
    counterparty = await make_counterparty(session, name=name, inn=None)
    await session.execute(
        update(CounterpartyPayableProfile)
        .where(CounterpartyPayableProfile.counterparty_id == counterparty.id)
        .values(service_billing_mode=mode)
    )
    await session.flush()
    return counterparty


async def _accrual(session: AsyncSession, invoice_id) -> SupplierExpenseAccrual | None:
    return await session.scalar(
        select(SupplierExpenseAccrual).where(SupplierExpenseAccrual.invoice_id == invoice_id)
    )


async def test_per_invoice_closing_without_period_is_recognized_by_document_date(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Кейс Манго: УПД от 31.07 без периода обязан дать расход июля."""
    async with async_session_factory() as session:
        counterparty = await _service_counterparty(session, name="Манго", mode="per_invoice")
        invoice = await make_invoice(
            session,
            counterparty_id=counterparty.id,
            amount="6445.54",
            number="МРД#607000569",
            operational_scope="finance",
            invoice_date=JULY_DOC,
        )
        await session.commit()

        accrual = await supplier_service_periods.sync_invoice_accrual(session, invoice)
        await session.commit()

        assert accrual is not None, "расход по документу так и не признан"
        assert accrual.service_period_start == date(2026, 7, 1)
        assert accrual.service_period_end == JULY_DOC
        assert accrual.amount == Decimal("6445.54")
        # Период кончился до сегодня — документ опоздавший, признаётся сразу, а не ночью.
        assert accrual.status == "recognized"
        assert accrual.recognition_month == date(2026, 7, 1)

        # Поля накладной не тронуты: документ честно остаётся без периода.
        await session.refresh(invoice)
        assert invoice.service_period_start is None
        assert invoice.service_period_end is None
        assert invoice.service_period_status == "not_required"


async def test_future_document_stays_scheduled(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Кейс ЭкоЦентра: УПД будущего месяца не признаётся раньше конца периода."""
    async with async_session_factory() as session:
        counterparty = await _service_counterparty(session, name="ЭкоЦентр", mode="per_invoice")
        invoice = await make_invoice(
            session,
            counterparty_id=counterparty.id,
            amount="4185.28",
            number="ВД-54475",
            operational_scope="finance",
            invoice_date=date(2099, 8, 31),
        )
        await session.commit()

        accrual = await supplier_service_periods.sync_invoice_accrual(session, invoice)
        await session.commit()

        assert accrual is not None
        assert accrual.service_period_start == date(2099, 8, 1)
        assert accrual.service_period_end == date(2099, 8, 31)
        assert accrual.status == "scheduled", "расход будущего месяца признан раньше срока"


async def test_warehouse_scope_gets_no_accrual(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Товарная накладная расхода не начисляет: её себестоимость приходит фудкостом из iiko."""
    async with async_session_factory() as session:
        counterparty = await _service_counterparty(session, name="Овощебаза", mode="per_invoice")
        invoice = await make_invoice(
            session,
            counterparty_id=counterparty.id,
            amount="10000.00",
            operational_scope="warehouse",
            invoice_date=JULY_DOC,
        )
        await session.commit()

        assert await supplier_service_periods.sync_invoice_accrual(session, invoice) is None
        assert await _accrual(session, invoice.id) is None


async def test_counterparty_without_billing_mode_gets_no_accrual(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Неразмеченный контрагент: его платёж кассой не исключается, начисление задвоило бы."""
    async with async_session_factory() as session:
        counterparty = await _service_counterparty(session, name="Билинский", mode=None)
        invoice = await make_invoice(
            session,
            counterparty_id=counterparty.id,
            amount="13306.14",
            operational_scope="finance",
            invoice_date=date(2026, 7, 13),
        )
        await session.commit()

        assert await supplier_service_periods.sync_invoice_accrual(session, invoice) is None
        assert await _accrual(session, invoice.id) is None


async def test_other_billing_modes_get_no_accrual(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«Договор» и «счёт за период» признают расход своим механизмом — второй раз не надо."""
    async with async_session_factory() as session:
        for index, mode in enumerate(("fixed_tariff", "agreement", "one_off")):
            counterparty = await _service_counterparty(session, name=f"Режим {mode}", mode=mode)
            invoice = await make_invoice(
                session,
                counterparty_id=counterparty.id,
                amount=f"{1000 + index}.00",
                operational_scope="finance",
                invoice_date=JULY_DOC,
            )
            await session.flush()
            assert await supplier_service_periods.sync_invoice_accrual(session, invoice) is None
            assert await _accrual(session, invoice.id) is None
        await session.rollback()


async def test_document_before_accounting_start_gets_no_accrual(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Июнь уже сидит во входящих остатках, и отчёта за него не существует."""
    async with async_session_factory() as session:
        counterparty = await _service_counterparty(session, name="Манго июнь", mode="per_invoice")
        invoice = await make_invoice(
            session,
            counterparty_id=counterparty.id,
            amount="6108.69",
            operational_scope="finance",
            invoice_date=date(2026, 6, 30),
        )
        await session.commit()

        assert await supplier_service_periods.sync_invoice_accrual(session, invoice) is None
        assert await _accrual(session, invoice.id) is None


async def test_outgoing_barter_gets_no_accrual(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Исходящий документ бартера расхода не несёт — он про то, что должны НАМ."""
    async with async_session_factory() as session:
        counterparty = await _service_counterparty(session, name="Бартер", mode="per_invoice")
        invoice = await make_invoice(
            session,
            counterparty_id=counterparty.id,
            amount="5000.00",
            direction="receivable",
            operational_scope="finance",
            invoice_date=JULY_DOC,
        )
        await session.commit()

        assert await supplier_service_periods.sync_invoice_accrual(session, invoice) is None
        assert await _accrual(session, invoice.id) is None


async def test_explicit_period_wins_over_document_date(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Вписанный человеком период сильнее выведенного: документ за июль об услуге июня."""
    async with async_session_factory() as session:
        counterparty = await _service_counterparty(session, name="Манго ручной", mode="per_invoice")
        invoice = await make_invoice(
            session,
            counterparty_id=counterparty.id,
            amount="5250.00",
            operational_scope="finance",
            invoice_date=JULY_DOC,
        )
        invoice.service_period_start = date(2026, 7, 1)
        invoice.service_period_end = date(2026, 7, 15)
        invoice.service_period_status = "ready"
        await session.commit()

        accrual = await supplier_service_periods.sync_invoice_accrual(session, invoice)
        await session.commit()

        assert accrual is not None
        assert accrual.service_period_end == date(2026, 7, 15), "выведенный период победил ручной"


async def test_bill_still_bears_no_expense(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Счёт на оплату расходом не является — правка это правило не ослабила."""
    async with async_session_factory() as session:
        counterparty = await _service_counterparty(session, name="Манго счёт", mode="per_invoice")
        invoice = await make_invoice(
            session,
            counterparty_id=counterparty.id,
            amount="10000.00",
            doc_kind="bill",
            operational_scope="finance",
            invoice_date=JULY_DOC,
        )
        await session.commit()

        assert await supplier_service_periods.sync_invoice_accrual(session, invoice) is None
        assert await _accrual(session, invoice.id) is None


async def test_period_correction_recognizes_immediately(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Правка периода на уже прошедший признаёт расход СРАЗУ, а не следующей ночью.

    У ``sync_invoice_accrual`` догоняющий вызов был, у второй двери правки (``ДЗ/КЗ`` →
    карандаш) — нет: начисление оставалось ``scheduled`` до 00:05, и человек видел прошедшую
    дату без строки расхода. Дверь достижима руками уже сегодня, независимо от фронта.
    """
    async with async_session_factory() as session:
        counterparty = await _service_counterparty(session, name="Манго правка", mode="per_invoice")
        invoice = await make_invoice(
            session,
            counterparty_id=counterparty.id,
            amount="5250.00",
            operational_scope="finance",
            invoice_date=date(2099, 8, 31),
        )
        await session.commit()

        accrual = await supplier_service_periods.sync_invoice_accrual(session, invoice)
        await session.commit()
        assert accrual is not None and accrual.status == "scheduled"

        # Человек исправляет период на реально прошедший — расход обязан появиться сразу.
        corrected = await supplier_service_periods.change_accrual_period(
            session,
            accrual=accrual,
            start=date(2026, 7, 1),
            end=date(2026, 7, 31),
            actor_user_id=None,
            reason="период указан неверно",
        )
        assert corrected.status == "recognized", "расход ждёт ночной джобы вместо признания"
        assert corrected.recognition_month == date(2026, 7, 1)


async def test_void_and_informational_bear_no_expense(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Аннулированный и информационный документ расхода не несут — и здесь тоже."""
    async with async_session_factory() as session:
        counterparty = await _service_counterparty(session, name="Манго void", mode="per_invoice")
        voided = await make_invoice(
            session,
            counterparty_id=counterparty.id,
            amount="7000.00",
            payment_status="void",
            operational_scope="finance",
            invoice_date=JULY_DOC,
        )
        informational = await make_invoice(
            session,
            counterparty_id=counterparty.id,
            amount="8000.00",
            operational_scope="finance",
            invoice_date=JULY_DOC,
        )
        informational.informational = True
        await session.commit()

        assert await supplier_service_periods.sync_invoice_accrual(session, voided) is None
        assert await supplier_service_periods.sync_invoice_accrual(session, informational) is None
        assert await _accrual(session, voided.id) is None
        assert await _accrual(session, informational.id) is None

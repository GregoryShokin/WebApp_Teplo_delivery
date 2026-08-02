"""Разные периоды услуг в ОДНОМ банковском платеже: где это безопасно, а где нет.

Энергетик за один визит привозит два документа — доплату за прошлый месяц и аванс за
текущий, — а владелец платит их одним переводом одному получателю. Гард «счета с разными
периодами нельзя объединять» это запрещал, и оставалось либо дробить перевод (в банке два
платежа там, где реально один), либо подгонять период под соседний документ.

Разрешено это только для пачки СЧЕТОВ: период там носит каждый документ сам — начисление
расхода заводится по счёту (``sync_invoice_accrual``), дебиторка оплаченного счёта берёт
период с него же (``_invoice_period_fields``), а поле периода у черновика не читает никто.
У свободных строк расхода носителя-документа нет: дебиторку платежа датирует ПЕРВАЯ строка
с периодом (``_expense_line_period``) и распространяет свой период на все деньги. Смешай
периоды там — и аванс за сентябрь признается августом, молча и без единой ошибки: расход
встанет в P&L не в свой месяц, а очередь «ждём документ» будет ждать не тот документ.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from cp_helpers import make_counterparty, make_invoice
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_new_payment_window import _free_expense_article

from app.models import SupplierInvoice, SupplierPrepayment
from app.services.bank_payment_status import apply_payment_status
from app.services.counterparty_payments import (
    CounterpartyPaymentError,
    ExpenseLineInput,
    create_expense_payment_draft,
    create_payment_draft_for_invoices,
)

VERIFIED_REQUISITES = {
    "bankAcnt": "40702810400000012345",
    "bankBik": "044525225",
    "recipientCorrAccountNumber": "30101810400000000225",
}


async def _energy_supplier(session: AsyncSession, *, inn: str) -> object:
    return await make_counterparty(
        session,
        name="АО «Энергосбыт»",
        inn=inn,
        requisites=VERIFIED_REQUISITES,
        requisites_verified=True,
    )


async def _utility_bill(
    session: AsyncSession,
    *,
    counterparty_id,
    amount: str,
    number: str,
    start: date,
    end: date,
) -> SupplierInvoice:
    """Коммунальный счёт с готовым периодом — как его заводит разбор квитанции."""
    bill = await make_invoice(
        session,
        counterparty_id=counterparty_id,
        amount=amount,
        number=number,
        doc_kind="bill",
        operational_scope="finance",
        invoice_date=end,
    )
    bill.service_period_start = start
    bill.service_period_end = end
    bill.service_period_source = "utility_intake"
    bill.service_period_status = "ready"
    await session.flush()
    return bill


async def test_two_utility_bills_of_different_periods_pack_into_one_draft(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Доплата за август и аванс за сентябрь уходят в банк одной суммой — как их и платят.

    Плюс проверяем, ПОЧЕМУ это не ломает учёт: период остаётся на каждом счёте, и дебиторка
    после оплаты рождается по счёту со своим периодом. Промах здесь стоил бы признания
    расхода не тем месяцем — деньги сойдутся, а прибыль месяца нет.
    """
    async with async_session_factory() as session:
        supplier = await _energy_supplier(session, inn="6100000001")
        august = await _utility_bill(
            session,
            counterparty_id=supplier.id,
            amount="40000.00",
            number="Э-08",
            start=date(2026, 8, 1),
            end=date(2026, 8, 31),
        )
        september = await _utility_bill(
            session,
            counterparty_id=supplier.id,
            amount="50000.00",
            number="Э-09",
            start=date(2026, 9, 1),
            end=date(2026, 9, 30),
        )
        await session.commit()

        draft = await create_payment_draft_for_invoices(
            session, invoice_ids=[august.id, september.id], actor_user_id=None
        )

        assert draft.status == "created"
        assert draft.amount == Decimal("90000.00")
        await session.refresh(august)
        await session.refresh(september)
        assert august.draft_id == draft.id
        assert september.draft_id == draft.id
        # Общего периода у такого платежа нет — и черновик его не выдумывает.
        assert draft.service_period_start is None
        assert draft.service_period_end is None
        # А сами счета свои периоды сохранили: учёт считает по ним.
        assert august.service_period_start == date(2026, 8, 1)
        assert september.service_period_start == date(2026, 9, 1)

        assert await apply_payment_status(session, draft=draft, raw_status="executed") == "paid"

        receivables = {
            row.bill_invoice_id: row
            for row in (
                await session.scalars(
                    select(SupplierPrepayment).where(
                        SupplierPrepayment.counterparty_id == supplier.id,
                        SupplierPrepayment.kind == "prepaid_bill",
                    )
                )
            ).all()
        }
        assert set(receivables) == {august.id, september.id}
        assert receivables[august.id].service_period_end == date(2026, 8, 31)
        assert receivables[september.id].service_period_end == date(2026, 9, 30)


async def test_same_period_invoices_still_pack_into_draft(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Регресс: обычная пачка одного периода собирается как раньше, период — на черновике."""
    async with async_session_factory() as session:
        supplier = await _energy_supplier(session, inn="6100000002")
        first = await _utility_bill(
            session,
            counterparty_id=supplier.id,
            amount="12000.00",
            number="Э-07-1",
            start=date(2026, 7, 1),
            end=date(2026, 7, 31),
        )
        second = await _utility_bill(
            session,
            counterparty_id=supplier.id,
            amount="3000.00",
            number="Э-07-2",
            start=date(2026, 7, 1),
            end=date(2026, 7, 31),
        )
        await session.commit()

        draft = await create_payment_draft_for_invoices(
            session, invoice_ids=[first.id, second.id], actor_user_id=None
        )

        assert draft.amount == Decimal("15000.00")
        assert draft.service_period_start == date(2026, 7, 1)
        assert draft.service_period_end == date(2026, 7, 31)


async def test_expense_lines_of_different_periods_still_rejected(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Свободные строки расхода с разными периодами по-прежнему не сливаются в один черновик.

    Ради этого случая гард и ставился: у строк нет документа-носителя периода, дебиторку
    платежа датирует первая строка с периодом и накрывает им весь платёж. Слив периодов
    здесь не упал бы ошибкой — он тихо признал бы расход не тем месяцем.
    """
    async with async_session_factory() as session:
        article = await _free_expense_article(session)
        supplier = await make_counterparty(
            session,
            name="ООО «Абонентка»",
            inn="6100000003",
            relationship="informal",
        )

        with pytest.raises(CounterpartyPaymentError, match="отдельными черновиками"):
            await create_expense_payment_draft(
                session,
                lines=[
                    ExpenseLineInput(
                        article_id=article.id,
                        amount=Decimal("40000.00"),
                        purpose="Электричество за август",
                        counterparty_id=supplier.id,
                        service_period_start=date(2026, 8, 1),
                        service_period_end=date(2026, 8, 31),
                    ),
                    ExpenseLineInput(
                        article_id=article.id,
                        amount=Decimal("50000.00"),
                        purpose="Электричество за сентябрь",
                        counterparty_id=supplier.id,
                        service_period_start=date(2026, 9, 1),
                        service_period_end=date(2026, 9, 30),
                    ),
                ],
            )

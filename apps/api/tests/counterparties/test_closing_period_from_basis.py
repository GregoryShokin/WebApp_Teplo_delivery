"""Период услуги закрывающего документа — из счёта, на который он ссылается.

Кейс iiko (прод, 04.08.2026). Акт на передачу прав своего периода не печатает вовсе: в тексте
только «Лицензия на ПО iikoCloud Enterprise (1мес)». Период стоит в счёте-основании, причём
явным диапазоном — «Лицензии iiko is-175038/2024 [01.08.2026—31.08.2026] (31 дн.)»: iiko
выставляет счёт в начале июля ЗА АВГУСТ, оплата идёт вперёд.

Пока период брался из подписной строки акта, расход попадал в нужный месяц случайно. Теперь
подпись источником периода не считается, и без наследования от основания расход по акту не
признавался бы вовсе — ``sync_invoice_accrual`` без периода начисление не создаёт.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from cp_helpers import make_counterparty
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import EmailInvoiceIntake, SupplierExpenseAccrual, SupplierInvoice
from app.services.email_invoice_ingest import materialize_from_intake

BILL_NUMBER = "040726-40618-лсп"
BILL_DATE = date(2026, 7, 4)
PERIOD = (date(2026, 8, 1), date(2026, 8, 31))


async def _paid_bill(session: AsyncSession, counterparty_id: uuid.UUID) -> SupplierInvoice:
    """Счёт iiko от 4 июля за август — уже в базе к моменту прихода акта."""
    bill = SupplierInvoice(
        counterparty_id=counterparty_id,
        source="email",
        direction="payable",
        doc_kind="bill",
        operational_scope="finance",
        number=BILL_NUMBER,
        invoice_date=BILL_DATE,
        amount=Decimal("4260.00"),
        payment_status="paid",
        service_period_start=PERIOD[0],
        service_period_end=PERIOD[1],
        service_period_status="ready",
        service_period_source="document_range",
    )
    session.add(bill)
    await session.flush()
    return bill


def _act_intake(
    counterparty_id: uuid.UUID, *, basis_number: str | None = BILL_NUMBER
) -> EmailInvoiceIntake:
    """Акт iiko: свой номер и дата, период не распознан, есть ссылка на счёт-основание."""
    return EmailInvoiceIntake(
        mailbox="personal",
        from_addr="rassilka_aktov@iiko.ru",
        subject="Документы для ИП Шокина Кристина Юрьевна",
        attachment_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        status="needs_review",
        counterparty_id=counterparty_id,
        recognition={
            "amount": "4260.00",
            "document_kind": "act",
            "invoice_number": "10826-9432-лсп",
            "invoice_date": "2026-08-01",
            "basis_number": basis_number,
            "basis_date": BILL_DATE.isoformat(),
        },
    )


async def test_closing_inherits_period_from_basis_invoice(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Акт без своего периода берёт его у счёта-основания — и расход признаётся за август."""
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="АО АЙКО (период)", inn="1655166011")
        await _paid_bill(session, cp.id)
        intake = _act_intake(cp.id)
        session.add(intake)
        await session.flush()

        status = await materialize_from_intake(session, intake)
        await session.commit()

        assert status == "closing"
        act = await session.get(SupplierInvoice, intake.invoice_id)
        assert act is not None
        # Свои реквизиты — не основания: иначе документ неотличим от оплаченного счёта.
        assert act.number == "10826-9432-лсп"
        assert act.invoice_date == date(2026, 8, 1)
        assert (act.service_period_start, act.service_period_end) == PERIOD
        assert act.service_period_status == "ready"
        assert act.service_period_source == "basis_invoice"

        accrual = await session.scalar(
            select(SupplierExpenseAccrual).where(SupplierExpenseAccrual.invoice_id == act.id)
        )
        assert accrual is not None
        assert accrual.service_period_start == PERIOD[0]
        assert accrual.amount == Decimal("4260.00")


async def test_unknown_basis_leaves_period_empty(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Счёта-основания в базе нет — период не выдумываем.

    Наследовать не у кого: молча подставить дату документа значило бы отправить расход в
    месяц подписания, ровно то, от чего уходим. Документ проводится, период остаётся пустым.
    """
    async with async_session_factory() as session:
        cp = await make_counterparty(session, name="АО АЙКО (без основания)", inn="1655166012")
        intake = _act_intake(cp.id, basis_number="НЕТ-ТАКОГО-СЧЁТА")
        session.add(intake)
        await session.flush()

        await materialize_from_intake(session, intake)
        await session.commit()

        act = await session.get(SupplierInvoice, intake.invoice_id)
        assert act is not None
        assert act.service_period_start is None
        assert act.service_period_status in ("missing", "not_required")


async def test_basis_of_another_counterparty_is_not_borrowed(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Счёт с тем же номером у ДРУГОГО поставщика периодом не делится."""
    async with async_session_factory() as session:
        stranger = await make_counterparty(session, name="Чужой поставщик", inn="1655166013")
        await _paid_bill(session, stranger.id)
        cp = await make_counterparty(session, name="АО АЙКО (чужое основание)", inn="1655166014")
        intake = _act_intake(cp.id)
        session.add(intake)
        await session.flush()

        await materialize_from_intake(session, intake)
        await session.commit()

        act = await session.get(SupplierInvoice, intake.invoice_id)
        assert act is not None
        assert act.service_period_start is None

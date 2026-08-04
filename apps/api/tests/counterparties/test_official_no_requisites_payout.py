"""Оплата счёта получателю БЕЗ банковских реквизитов — галочка «вывести на карту ИП».

Так платят арендодателю за коммуналку и физлицу, которое принесло счёт на бумаге: реквизитов
нет и не будет, а платить надо. Оператор ставит галочку в окне отправки — платёж выписывается
на карту ИП (тот же маршрут, что у неофициальных поставщиков): деньги приходят на Сейф, счёт
закрывается, когда наличные выданы получателю.

Граница ровно одна и проверяется здесь: галочка меняет маршрут ТОЛЬКО пока реквизитов в
карточке нет вовсе. Там, где счёт получателя известен, платим по нему — иначе одна забытая
галочка увела бы деньги мимо поставщика, у которого реквизиты в системе уже есть.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from cp_helpers import make_counterparty, make_invoice
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AppSetting, EmailInvoiceIntake
from app.services import email_invoice_ingest as ingest
from app.services.counterparty_payments import (
    RequisitesNotVerifiedError,
    create_payment_draft_for_invoices,
)

VERIFIED_REQUISITES = {
    "bankAcnt": "40702810400000012345",
    "bankBik": "044525225",
    "recipientCorrAccountNumber": "30101810400000000225",
}


async def _ip_card_name(session: AsyncSession) -> str:
    setting = await session.scalar(
        select(AppSetting).where(AppSetting.key == "payroll.bank_payout_requisites")
    )
    return setting.value["recipientName"]


async def test_official_without_requisites_goes_to_ip_card(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Реквизитов нет, галочка стоит — платёж уходит на карту ИП, а не поставщику."""
    async with async_session_factory() as session:
        landlord = await make_counterparty(session, name="Станислав Юрьевич", inn=None)
        invoice = await make_invoice(
            session, counterparty_id=landlord.id, amount="9654.25", number="Возмещение"
        )
        await session.commit()

        draft = await create_payment_draft_for_invoices(
            session,
            invoice_ids=[invoice.id],
            actor_user_id=None,
            allow_official_via_safe=True,
        )

        assert draft.pays_via_safe is True
        assert draft.amount == Decimal("9654.25")
        assert draft.payload["recipientName"] == await _ip_card_name(session)
        assert draft.payload["recipientName"] != landlord.name
        # Кому и за что — в назначении: по нему потом узнаётся целевой резерв Сейфа.
        assert "Станислав Юрьевич" in draft.payload["paymentPurpose"]
        await session.refresh(invoice)
        assert invoice.draft_id == draft.id


async def test_without_consent_still_blocked(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Без галочки поведение прежнее: счёт без подтверждённых реквизитов в банк не уходит."""
    async with async_session_factory() as session:
        landlord = await make_counterparty(session, name="Без реквизитов и без согласия")
        invoice = await make_invoice(session, counterparty_id=landlord.id, amount="500.00")
        await session.commit()

        with pytest.raises(RequisitesNotVerifiedError):
            await create_payment_draft_for_invoices(
                session, invoice_ids=[invoice.id], actor_user_id=None
            )


async def test_consent_does_not_bypass_unverified_requisites(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Реквизиты в карточке ЕСТЬ, но не подтверждены — галочка их не обходит.

    Это и есть защита от «нажал не глядя»: счёт получателя известен, значит платить надо по
    нему, а не мимо. Человеку остаётся подтвердить реквизиты — обхода нет.
    """
    async with async_session_factory() as session:
        supplier = await make_counterparty(
            session,
            name="ООО С реквизитами",
            inn="7701234567",
            requisites=VERIFIED_REQUISITES,
            requisites_verified=False,
        )
        invoice = await make_invoice(session, counterparty_id=supplier.id, amount="700.00")
        await session.commit()

        with pytest.raises(RequisitesNotVerifiedError):
            await create_payment_draft_for_invoices(
                session,
                invoice_ids=[invoice.id],
                actor_user_id=None,
                allow_official_via_safe=True,
            )


async def test_consent_ignored_when_requisites_verified(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Реквизиты подтверждены — платим по ним, даже если галочка почему-то пришла."""
    async with async_session_factory() as session:
        supplier = await make_counterparty(
            session,
            name="ООО Официальный",
            inn="7701234568",
            requisites=VERIFIED_REQUISITES,
            requisites_verified=True,
        )
        invoice = await make_invoice(session, counterparty_id=supplier.id, amount="800.00")
        await session.commit()

        draft = await create_payment_draft_for_invoices(
            session,
            invoice_ids=[invoice.id],
            actor_user_id=None,
            allow_official_via_safe=True,
        )

        assert draft.pays_via_safe is False
        assert draft.payload["recipientName"] == "ООО Официальный"


async def test_scheduled_send_carries_consent(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Плановую отправку делает джоба — согласие она берёт со строки, а потом снимает.

    Без сохранённого согласия такой счёт вечно висел бы в «пропущено», ожидая реквизитов,
    которых не будет.
    """
    async with async_session_factory() as session:
        landlord = await make_counterparty(session, name="Плановый без реквизитов")
        invoice = await make_invoice(session, counterparty_id=landlord.id, amount="1200.00")
        intake = EmailInvoiceIntake(
            mailbox="corporate",
            from_addr=None,
            subject="Возмещение коммуналки",
            attachment_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            status="linked",
            counterparty_id=landlord.id,
            invoice_id=invoice.id,
            recognition={"amount": "1200.00"},
            scheduled_send_date=date.today() - timedelta(days=1),
            scheduled_pays_via_safe=True,
        )
        session.add(intake)
        await session.commit()

        result = await ingest.run_scheduled_sends(session)

        assert result["sent"] == 1
        await session.refresh(intake)
        await session.refresh(invoice)
        assert invoice.draft_id is not None
        # Согласие разовое: платёж ушёл — и дата, и галочка сняты.
        assert intake.scheduled_send_date is None
        assert intake.scheduled_pays_via_safe is False


async def test_scheduled_send_without_consent_skipped(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Плановый счёт без согласия и без реквизитов джоба пропускает, дату не снимает."""
    async with async_session_factory() as session:
        landlord = await make_counterparty(session, name="Плановый без согласия")
        invoice = await make_invoice(session, counterparty_id=landlord.id, amount="300.00")
        planned = date.today() - timedelta(days=1)
        intake = EmailInvoiceIntake(
            mailbox="corporate",
            from_addr=None,
            subject="Счёт",
            attachment_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            status="linked",
            counterparty_id=landlord.id,
            invoice_id=invoice.id,
            recognition={"amount": "300.00"},
            scheduled_send_date=planned,
        )
        session.add(intake)
        await session.commit()

        result = await ingest.run_scheduled_sends(session)

        assert result["sent"] == 0 and result["skipped"] == 1
        await session.refresh(intake)
        await session.refresh(invoice)
        assert invoice.draft_id is None
        assert intake.scheduled_send_date == planned

"""Документы бухгалтера не попадают в очередь оплат.

27.07.2026 налоговый агент прислала зарплатную ведомость Т-53 в PDF. Контур счетов берёт
любой PDF из ящика, поэтому ведомость встала на «Страницу на оплату» как счёт на 22 696 ₽
с датой 05.01.2004 (дата утверждения бланка Госкомстатом в шапке формы). Её место — модуль
«Налоги»; здесь проверяем, что отправитель-налоговый агент отсекается на входе.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.services.email_invoice_ingest as ingest
from app.core.config import get_settings
from app.models import EmailInvoiceIntake
from app.services.mail.imap_client import FetchedAttachment, MailAccount


def _att(sender: str) -> FetchedAttachment:
    return FetchedAttachment(
        mailbox="corporate",
        message_uid="18602",
        message_id="<ved14@test>",
        from_addr=sender,
        subject="ВЕД-14 ЗП 05.08.pdf",
        received_at=datetime(2026, 7, 27, 14, 12, tzinfo=UTC),
        filename="ВЕД-14 ЗП 05.08.pdf",
        mime="application/pdf",
        content=b"%PDF-1.5\n(payload)",
    )


def _stub_mail(monkeypatch, attachment: FetchedAttachment) -> None:
    monkeypatch.setattr(
        ingest, "configured_accounts", lambda settings: [MailAccount("corporate", "a@b", "x")]
    )
    monkeypatch.setattr(
        ingest,
        "fetch_pdf_attachments",
        lambda account, *, host, port, lookback_days: [attachment],
    )


async def test_poll_skips_tax_agent_pdf(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    """PDF от бухгалтера в staging счетов не попадает — им занимается контур «Налоги»."""
    _stub_mail(monkeypatch, _att("Наумченко Наталья <askad02@mail.ru>"))

    async with async_session_factory() as session:
        result = await ingest.poll_and_ingest(session, settings=get_settings())

    assert result["skipped_tax_agent"] == 1
    assert result["needs_review"] == 0
    async with async_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(EmailInvoiceIntake)) == 0


async def test_poll_still_takes_ordinary_sender(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    """Обычный поставщик отсечкой не задет — иначе фильтр съел бы настоящие счета."""
    _stub_mail(monkeypatch, _att("Поставщик <sales@example.com>"))

    async with async_session_factory() as session:
        result = await ingest.poll_and_ingest(session, settings=get_settings())

    assert result["skipped_tax_agent"] == 0
    async with async_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(EmailInvoiceIntake)) == 1

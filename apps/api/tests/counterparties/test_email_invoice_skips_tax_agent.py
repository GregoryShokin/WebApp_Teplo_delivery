"""Развилку «бумага бухгалтера или счёт нам» решает документ, а не адрес отправителя.

27.07.2026 налоговый агент прислала зарплатную ведомость Т-53 в PDF. Контур счетов берёт
любой PDF из ящика, поэтому ведомость встала на «Страницу на оплату» как счёт на 22 696 ₽
с датой 05.01.2004 (дата утверждения бланка Госкомстатом в шапке формы). Заткнули по адресу
отправителя — и 28.07.2026 потеряли обратное: собственный счёт бухгалтера №100 на 9 000 ₽
(вместе с актом на том же листе) улетел в «Налоги» и умер там как «неопознанный тип».

Поэтому отсекаем не отправителя, а его отчётность: по имени файла (тем же классификатором,
что и налоговый контур) и по шапке унифицированной формы в тексте. Распознаванию эту развилку
доверять нельзя — на той самой Т-53 оно и ошиблось, приняв ведомость за счёт.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.services.email_invoice_ingest as ingest
from app.core.config import get_settings
from app.models import EmailInvoiceIntake
from app.services.mail.imap_client import FetchedAttachment, MailAccount

ACCOUNTANT = "Наумченко Наталья <askad02@mail.ru>"


def _att(
    sender: str,
    *,
    filename: str = "ВЕД-14 ЗП 05.08.pdf",
    content: bytes = b"%PDF-1.5\n(payload)",
) -> FetchedAttachment:
    return FetchedAttachment(
        mailbox="corporate",
        message_uid="18602",
        message_id="<ved14@test>",
        from_addr=sender,
        subject=filename,
        received_at=datetime(2026, 7, 27, 14, 12, tzinfo=UTC),
        filename=filename,
        mime="application/pdf",
        content=content,
    )


def _stub_pdf_text(monkeypatch, text: str) -> None:
    """Подменяем чтение PDF, а не собираем настоящий файл.

    Предмет теста — развилка «бумага бухгалтера или счёт нам», а не парсер PDF: самодельный
    PDF с кириллицей всё равно читается через раз (шрифт без ToUnicode отдаёт мусор), и тест
    падал бы по причине, к маршрутизации отношения не имеющей. Настоящие Т-53 и счёт №100
    прогнаны отдельно, живьём (см. коммит).
    """
    monkeypatch.setattr(ingest, "extract_pdf_text", lambda content: text)


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


async def test_poll_takes_own_invoice_of_accountant(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    """Счёт САМОГО бухгалтера разбирается как обычный счёт на оплату.

    Реальный кейс 28.07.2026: «СЧ 100 ШОКИНА.pdf» — её счёт нам на 9 000 ₽ за квартал.
    Имя файла не опознаётся как отчётность, шапки унифицированной формы в тексте нет,
    значит это счёт нам, а не её бумага.
    """
    _stub_mail(monkeypatch, _att(ACCOUNTANT, filename="СЧ 100 ШОКИНА.pdf"))

    async with async_session_factory() as session:
        result = await ingest.poll_and_ingest(session, settings=get_settings())

    assert result["skipped_tax_agent"] == 0
    async with async_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(EmailInvoiceIntake)) == 1


async def test_poll_skips_goskomstat_form_with_neutral_filename(
    async_session_factory: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    """Ведомость, названная нейтрально, ловится по шапке унифицированной формы.

    Вторая линия развилки: имя файла — слабый признак, бухгалтер может сохранить ту же Т-53
    как «Документ1.pdf». Шапку «Унифицированная форма № Т-53» подделать нечем.
    """
    _stub_pdf_text(
        monkeypatch, "Унифицированная форма № Т-53 Утверждена Постановлением Госкомстата"
    )
    _stub_mail(monkeypatch, _att(ACCOUNTANT, filename="Документ1.pdf"))

    async with async_session_factory() as session:
        result = await ingest.poll_and_ingest(session, settings=get_settings())

    assert result["skipped_tax_agent"] == 1
    async with async_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(EmailInvoiceIntake)) == 0


def test_paperwork_reasons_are_explicit() -> None:
    """Причина отсечки называется словами — иначе в логе не отличить «ведомость» от сбоя."""
    ved = _att(ACCOUNTANT, filename="ВЕД-14 ЗП 05.08.pdf")
    assert "payroll_statement" in (ingest._is_tax_paperwork(ved) or "")

    turnover = _att(ACCOUNTANT, filename="ОБОРОТКА 07.xls")
    assert "turnover_statement" in (ingest._is_tax_paperwork(turnover) or "")

    invoice = _att(ACCOUNTANT, filename="СЧ 100 ШОКИНА.pdf")
    assert ingest._is_tax_paperwork(invoice) is None

    # Нечитаемый PDF развилку не роняет: решать будет распознавание, а не она.
    broken = _att(ACCOUNTANT, filename="скан.pdf", content=b"not-a-pdf")
    assert ingest._is_tax_paperwork(broken) is None

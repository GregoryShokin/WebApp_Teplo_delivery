"""Приём налоговых документов из почты → staging.

IMAP не трогаем: подсовываем фикстуры через инъекцию ``fetch``. Проверяем маршрутизацию
по типу, фильтр отправителя, дедуп и статусы (parsed / needs_review / unsupported).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models.tax import TaxDocumentIntake
from app.services.mail.imap_client import FetchedAttachment
from app.services.taxes.document_ingest import ingest_tax_documents, parse_attachment

FIXTURES = Path(__file__).parent / "fixtures" / "taxes"


def _att(
    name: str, filename: str, *, sender: str = "Бухгалтер <askad02@mail.ru>"
) -> FetchedAttachment:
    content = (FIXTURES / name).read_bytes()
    mime = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if filename.endswith(".docx")
        else "application/vnd.ms-excel"
    )
    return FetchedAttachment(
        mailbox="corporate",
        message_uid="1",
        message_id=f"<{name}@test>",
        from_addr=sender,
        subject="налоги",
        received_at=datetime(2026, 7, 23, tzinfo=UTC),
        filename=filename,
        mime=mime,
        content=content,
    )


def _fetch_stub(attachments: list[FetchedAttachment]):
    def _fetch(account, *, host, port, lookback_days):  # noqa: ANN001, ARG001
        return list(attachments)

    return _fetch


# ── чистый разбор вложения ───────────────────────────────────────────────────


def test_parse_attachment_routes_payment_order() -> None:
    dtype, status, rec, err = parse_attachment(
        _att("usn_h1_478376.docx", "УСН 2 кв до 28.07.docx")
    )
    assert dtype == "payment_order"
    assert status == "parsed"
    assert rec["amount"] == "478376"
    assert rec["tax_kind"] == "usn_advance"
    assert err is None


def test_parse_attachment_flags_enp_needs_review() -> None:
    _, status, rec, _ = parse_attachment(_att("enp_payroll_14902.docx", "ЕНП_до 28.07.docx"))
    assert status == "needs_review"
    assert rec["tax_kind"] == "enp_payroll"


def test_parse_attachment_routes_payroll_statement() -> None:
    dtype, status, rec, _ = parse_attachment(
        _att("vedomost_advance_20986.xls", "ВЕД-13 АВАНС 20.07.xls")
    )
    assert dtype == "payroll_statement"
    assert status == "parsed"
    assert rec["total"] == "20986"
    assert rec["rows"][0]["employee"] == "ИВАНОВА И.И."


def test_parse_attachment_marks_kadr_unsupported() -> None:
    dtype, status, _, _ = parse_attachment(
        _att("vedomost_salary_22696.xls", "Приказ об отпуске.xls")
    )
    assert dtype == "unknown"
    assert status == "unsupported"


# ── приём в staging ──────────────────────────────────────────────────────────


async def test_ingest_stores_and_routes(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    attachments = [
        _att("usn_h1_478376.docx", "УСН 2 кв до 28.07.docx"),
        _att("enp_payroll_14902.docx", "ЕНП_до 28.07.docx"),
        _att("vedomost_advance_20986.xls", "ВЕД-13 АВАНС 20.07.xls"),
    ]
    async with async_session_factory() as session:
        result = await ingest_tax_documents(
            session, settings=get_settings(), fetch=_fetch_stub(attachments)
        )

    assert result["fetched"] == 3
    assert result["parsed"] == 2  # УСН + ведомость
    assert result["needs_review"] == 1  # ЕНП
    async with async_session_factory() as session:
        assert await session.scalar(
            select(func.count()).select_from(TaxDocumentIntake)
        ) == 3


async def test_ingest_dedups_by_content(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Повторный проход того же вложения не создаёт вторую строку."""
    att = _att("usn_h1_478376.docx", "УСН 2 кв до 28.07.docx")
    async with async_session_factory() as session:
        await ingest_tax_documents(session, settings=get_settings(), fetch=_fetch_stub([att]))
    async with async_session_factory() as session:
        result = await ingest_tax_documents(
            session, settings=get_settings(), fetch=_fetch_stub([att])
        )

    assert result["duplicate"] == 1
    assert result["parsed"] == 0
    async with async_session_factory() as session:
        assert await session.scalar(
            select(func.count()).select_from(TaxDocumentIntake)
        ) == 1


async def test_ingest_skips_foreign_sender(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Письмо не от налогового агента в налоговый staging не попадает."""
    att = _att(
        "usn_h1_478376.docx", "какой-то счёт.docx", sender="supplier@example.com"
    )
    async with async_session_factory() as session:
        result = await ingest_tax_documents(
            session, settings=get_settings(), fetch=_fetch_stub([att])
        )

    assert result["skipped_sender"] == 1
    assert result["parsed"] == 0
    async with async_session_factory() as session:
        assert await session.scalar(
            select(func.count()).select_from(TaxDocumentIntake)
        ) == 0


def test_content_sha_is_stable() -> None:
    """Дедуп опирается на SHA содержимого — он детерминирован."""
    att = _att("usn_h1_478376.docx", "УСН 2 кв до 28.07.docx")
    assert att.sha256 == hashlib.sha256(att.content).hexdigest()

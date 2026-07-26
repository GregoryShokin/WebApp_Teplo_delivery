"""Приём налоговых документов из почты → staging.

IMAP не трогаем: подсовываем фикстуры через инъекцию ``fetch``. Проверяем маршрутизацию
по типу, фильтр отправителя, дедуп и статусы (parsed / needs_review / unsupported).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models.tax import TaxDocumentIntake
from app.services.mail.imap_client import FetchedAttachment, MailAccount
from app.services.taxes.document_ingest import (
    ingest_tax_documents,
    parse_attachment,
    set_intake_review,
)

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


# Один фиктивный ящик — чтобы приём был герметичным и не зависел от того, настроена ли
# реальная почта в окружении (иначе на два настроенных ящика stub-fetch задвоит вложения).
_TEST_ACCOUNTS = [MailAccount("test", "test@local", "x")]


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


def test_parse_attachment_enp_is_parsed_not_review() -> None:
    """ЕНП-платёжка распознаётся (parsed), а не висит в «нужна проверка»: её разнос НДФЛ/взносов
    делает оборотка (rebuild_payroll_enp_split), отдельного ручного подтверждения не требуется."""
    _, status, rec, _ = parse_attachment(_att("enp_payroll_14902.docx", "ЕНП_до 28.07.docx"))
    assert status == "parsed"
    assert rec["tax_kind"] == "enp_payroll"


def test_injury_document_classified_as_payment_order() -> None:
    """Файл травматизма («0,2 %.xls») — это платёжка (payment_order), а не Т-53-ведомость.

    Раньше .xls без «оборот/вед» уходил в Т-53-парсер, где «не находил сотрудников» и виснул
    в «нужна проверка». Теперь ключевые слова травматизма распознаются до .xls-фолбэка.
    """
    from app.services.taxes.document_ingest import _classify_document

    assert _classify_document("0,2 %.xls") == "payment_order"
    assert _classify_document("Травматизм июль.xls") == "payment_order"
    # обычная ведомость по-прежнему Т-53
    assert _classify_document("ВЕД-13 АВАНС 20.07.xls") == "payroll_statement"


def test_injury_amount_extracted_after_label() -> None:
    """Сумма взноса на травматизм берётся из ячейки сразу после метки «Сумма»."""
    from app.services.taxes.document_parser import _injury_amount

    assert _injury_amount(["ИНН", "890307589201", "Сумма", "100.00"]) == Decimal("100.00")
    assert _injury_amount(["Сумма", "57,14"]) == Decimal("57.14")
    assert _injury_amount(["нет метки", "12345"]) is None


def test_parse_attachment_routes_payroll_statement() -> None:
    dtype, status, rec, _ = parse_attachment(
        _att("vedomost_advance_20986.xls", "ВЕД-13 АВАНС 20.07.xls")
    )
    assert dtype == "payroll_statement"
    assert status == "parsed"
    assert rec["total"] == "20986"
    assert rec["rows"][0]["employee"] == "ИВАНОВА И.И."


def test_parse_attachment_routes_turnover_statement() -> None:
    """Оборотка распознаётся отдельным типом (не Т-53) с помесячной раскладкой в recognition."""
    dtype, status, rec, err = parse_attachment(_att("oborotka_07.xls", "ОБОРОТКА 07.xls"))
    assert dtype == "turnover_statement"
    assert status == "parsed"
    assert rec["period_hint"] == "2026-07"
    assert rec["rows"][0]["employee"] == "ИВАНОВА И.И."
    assert rec["rows"][0]["ndfl"] == "6318.00"
    assert rec["rows"][0]["contributions"] == "13595.93"
    assert rec["contributions_total"] == "13595.93"
    assert err is None


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
            session,
            settings=get_settings(),
            fetch=_fetch_stub(attachments),
            accounts=_TEST_ACCOUNTS,
        )

    assert result["fetched"] == 3
    assert result["parsed"] == 3  # УСН + ЕНП + ведомость (ЕНП теперь распознан, разнос из оборотки)
    assert result["needs_review"] == 0
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
        await ingest_tax_documents(
            session,
            settings=get_settings(),
            fetch=_fetch_stub([att]),
            accounts=_TEST_ACCOUNTS,
        )
    async with async_session_factory() as session:
        result = await ingest_tax_documents(
            session,
            settings=get_settings(),
            fetch=_fetch_stub([att]),
            accounts=_TEST_ACCOUNTS,
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
            session,
            settings=get_settings(),
            fetch=_fetch_stub([att]),
            accounts=_TEST_ACCOUNTS,
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


# ── ручная проверка документа владельцем ──────────────────────────────────────


def _needs_review_intake(status: str = "needs_review") -> TaxDocumentIntake:
    return TaxDocumentIntake(
        id=uuid.uuid4(),
        mailbox="corporate",
        attachment_sha256=(uuid.uuid4().hex + uuid.uuid4().hex),
        filename="ЕНП_до 28.07.docx",
        document_type="payment_order",
        status=status,
        recognition={},
    )


async def test_review_marks_parsed_then_ignored(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        intake = _needs_review_intake()
        session.add(intake)
        await session.flush()

        await set_intake_review(session, intake, status="parsed")
        assert intake.status == "parsed"  # проверено → готово к продвижению

        await set_intake_review(session, intake, status="ignored")
        assert intake.status == "ignored"  # передумал → отклонено


async def test_review_rejects_promoted_and_bad_status(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_session_factory() as session:
        promoted = _needs_review_intake(status="promoted")
        session.add(promoted)
        await session.flush()
        with pytest.raises(ValueError, match="продвинут"):
            await set_intake_review(session, promoted, status="parsed")

        intake = _needs_review_intake()
        with pytest.raises(ValueError, match="статус"):
            await set_intake_review(session, intake, status="deleted")


async def test_review_with_overrides_fills_missing_fields(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Проверка с дозаправкой: владелец указывает срок/сумму/вид — они попадают в recognition,
    причины «нужна проверка» снимаются, и продвижение возьмёт исправленные значения."""
    async with async_session_factory() as session:
        intake = _needs_review_intake()
        intake.recognition = {
            "tax_kind": None,
            "amount": "116360",
            "review_reasons": ["не распознан срок уплаты"],
        }
        session.add(intake)
        await session.flush()

        await set_intake_review(
            session,
            intake,
            status="parsed",
            overrides={"tax_kind": "contrib_extra_1pct", "due_date": "2026-04-28"},
        )

        assert intake.status == "parsed"
        assert intake.recognition["tax_kind"] == "contrib_extra_1pct"
        assert intake.recognition["due_date"] == "2026-04-28"
        assert intake.recognition["amount"] == "116360"  # нетронутое осталось
        assert intake.recognition["review_reasons"] == []
        assert intake.recognition["manually_reviewed"] is True


async def test_review_overrides_reject_unknown_kind(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Мусорный вид платежа в ручной правке отклоняется, а не пишется в расчёт."""
    async with async_session_factory() as session:
        intake = _needs_review_intake()
        session.add(intake)
        await session.flush()

        with pytest.raises(ValueError, match="Неизвестный вид"):
            await set_intake_review(
                session, intake, status="parsed", overrides={"tax_kind": "чепуха"}
            )

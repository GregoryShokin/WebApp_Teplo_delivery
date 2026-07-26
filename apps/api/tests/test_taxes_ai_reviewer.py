"""ИИ-ревьюер: правки применяются только уверенные и только той же дорогой, что руки.

Вызов Claude инъектируется (``call=``), реальный API в тестах не дёргается. Проверяем
контракт применения: уверенная платёжка → parsed с overrides и следом ИИ; неуверенная —
только объяснение; общий аудит собирает снимок и вердикт.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models.tax import TaxDocumentIntake
from app.services.taxes.ai_reviewer import (
    _AUDIT_TOOL,
    _REVIEW_TOOL,
    review_all,
    review_document,
)


def _intake(
    filename: str,
    *,
    status: str = "needs_review",
    document_type: str = "payment_order",
    recognition: dict | None = None,
) -> TaxDocumentIntake:
    return TaxDocumentIntake(
        id=uuid.uuid4(),
        mailbox="corporate",
        from_addr="Бухгалтер <a@b.c>",
        attachment_sha256=(uuid.uuid4().hex + uuid.uuid4().hex),
        received_at=datetime(2026, 7, 1, tzinfo=UTC),
        filename=filename,
        document_type=document_type,
        status=status,
        recognition=recognition or {},
    )


def _fake_call(review_payload: dict, audit_payload: dict | None = None):
    async def call(settings, *, tool, prompt, max_tokens=2048):
        if tool["name"] == _REVIEW_TOOL["name"]:
            return dict(review_payload)
        assert tool["name"] == _AUDIT_TOOL["name"]
        return dict(audit_payload or {"verdict": "Всё сходится.", "findings": []})

    return call


async def test_confident_payment_review_is_applied(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Уверенный разбор платёжки: parsed, поля дозаполнены, след ИИ записан честно."""
    async with async_session_factory() as session:
        intake = _intake("ЕНС до 27.03.docx", recognition={"review_reasons": ["вид не распознан"]})
        session.add(intake)
        await session.commit()

        result = await review_document(
            session,
            intake,
            settings=get_settings(),
            call=_fake_call(
                {
                    "document_type": "payment_order",
                    "tax_kind": "enp_payroll",
                    "amount": "19460.93",
                    "due_date": "2026-03-27",
                    "summary": "Платёжка ЕНП за февраль: НДФЛ и взносы одной суммой.",
                    "confidence": 0.95,
                    "needs_human": False,
                }
            ),
        )
        await session.commit()

    assert result.applied is True
    assert intake.status == "parsed"
    rec = intake.recognition
    assert rec["tax_kind"] == "enp_payroll"
    assert rec["amount"] == "19460.93"
    assert rec["review_reasons"] == []
    assert rec["reviewed_by"] == "ai"
    assert rec["manually_reviewed"] is False  # поле заполнил ИИ, не человек
    assert rec["ai_review"]["applied"] is True
    assert "ЕНП" in rec["ai_review"]["summary"]


async def test_unsure_review_explains_but_does_not_touch(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """needs_human/низкая уверенность: статус и поля не тронуты, объяснение записано."""
    async with async_session_factory() as session:
        intake = _intake("непонятный.docx", status="error", document_type="unknown")
        session.add(intake)
        await session.commit()

        result = await review_document(
            session,
            intake,
            settings=get_settings(),
            call=_fake_call(
                {
                    "document_type": "other",
                    "summary": "Похоже на кадровый приказ — платёжных полей нет.",
                    "confidence": 0.6,
                    "needs_human": True,
                    "reasons": ["Файл не читается автоматикой."],
                }
            ),
        )
        await session.commit()

    assert result.applied is False
    assert intake.status == "error"  # статус не изменён
    assert "приказ" in intake.recognition["ai_review"]["summary"]
    assert intake.recognition["ai_review"]["needs_human"] is True
    assert "tax_kind" not in intake.recognition


async def test_bogus_amount_is_not_applied(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Невалидная сумма от модели — правка не применяется, даже при высокой уверенности."""
    async with async_session_factory() as session:
        intake = _intake("платёжка.docx")
        session.add(intake)
        await session.commit()

        result = await review_document(
            session,
            intake,
            settings=get_settings(),
            call=_fake_call(
                {
                    "document_type": "payment_order",
                    "tax_kind": "usn_advance",
                    "amount": "не видно",
                    "summary": "Платёжка УСН, сумма не читается.",
                    "confidence": 0.9,
                    "needs_human": False,
                }
            ),
        )

    assert result.applied is False
    assert intake.status == "needs_review"


async def test_review_all_covers_attention_statuses_and_audits(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ревизия разбирает needs_review/error/unsupported, parsed не трогает, вердикт есть."""
    async with async_session_factory() as session:
        session.add(_intake("а.docx", status="needs_review"))
        session.add(_intake("б.xls", status="unsupported", document_type="unknown"))
        session.add(_intake("в.docx", status="parsed"))  # уже распознан — не трогаем
        await session.commit()

        report = await review_all(
            session,
            settings=get_settings(),
            call=_fake_call(
                {
                    "document_type": "other",
                    "summary": "Справочный документ.",
                    "confidence": 0.7,
                    "needs_human": True,
                    "reasons": ["мало данных"],
                },
                {
                    "verdict": "Контур сходится, критичных расхождений нет.",
                    "findings": [
                        {
                            "severity": "warning",
                            "title": "Два документа ждут человека",
                            "detail": "Приказы автоматика не разбирает.",
                        }
                    ],
                },
            ),
        )
        await session.commit()

    assert len(report.documents) == 2  # только needs_review + unsupported
    assert report.verdict.startswith("Контур сходится")
    assert report.findings[0].severity == "warning"

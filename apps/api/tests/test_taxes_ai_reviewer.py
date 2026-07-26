"""ИИ-ревьюер: разбор ПРЕДЛАГАЕТ, применяет только владелец кнопкой («да, делай»).

Вызов Claude инъектируется (``call=``), реальный API в тестах не дёргается. Контракт:
разбор сохраняет объяснение и предложение полей, данные не трогает; применение идёт
отдельной функцией той же дорогой, что ручная проверка.
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
    apply_ai_proposal,
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


_CONFIDENT_PAYMENT = {
    "document_type": "payment_order",
    "tax_kind": "enp_payroll",
    "amount": "19460.93",
    "due_date": "2026-03-27",
    "summary": "Платёжка ЕНП за февраль: НДФЛ и взносы одной суммой.",
    "confidence": 0.95,
    "needs_human": False,
}


async def test_review_saves_proposal_but_does_not_apply(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Даже уверенный разбор платёжки ничего не меняет — только предложение в ai_review."""
    async with async_session_factory() as session:
        intake = _intake("ЕНС до 27.03.docx", recognition={"review_reasons": ["вид не распознан"]})
        session.add(intake)
        await session.commit()

        result = await review_document(
            session, intake, settings=get_settings(), call=_fake_call(_CONFIDENT_PAYMENT)
        )
        await session.commit()

    assert result.proposal == {
        "tax_kind": "enp_payroll",
        "amount": "19460.93",
        "due_date": "2026-03-27",
    }
    assert intake.status == "needs_review"  # статус НЕ изменён — решает владелец
    rec = intake.recognition
    assert "tax_kind" not in rec  # поля документа не тронуты
    assert rec["ai_review"]["proposal"]["amount"] == "19460.93"
    assert "ЕНП" in rec["ai_review"]["summary"]


async def test_owner_confirms_and_proposal_is_applied(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """«Да, делай»: применяется ровно сохранённое предложение, документ — «распознан»."""
    async with async_session_factory() as session:
        # Файл, который парсер вовсе не классифицировал — ИИ опознал платёжкой.
        intake = _intake("ЕНС до 27.03.docx", status="error", document_type="unknown")
        session.add(intake)
        await session.commit()

        await review_document(
            session, intake, settings=get_settings(), call=_fake_call(_CONFIDENT_PAYMENT)
        )
        await session.commit()
        assert intake.status == "error"  # разбор сам ничего не применил

        await apply_ai_proposal(session, intake)
        await session.commit()

    assert intake.status == "parsed"
    assert intake.document_type == "payment_order"  # тип выровнен при подтверждении
    rec = intake.recognition
    assert rec["tax_kind"] == "enp_payroll"
    assert rec["amount"] == "19460.93"
    assert rec["review_reasons"] == []
    assert rec["reviewed_by"] == "ai_confirmed"  # предложил ИИ, подтвердил владелец
    assert rec["ai_review"]["applied"] is True


async def test_apply_without_proposal_refuses(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Подтверждать нечего — честная ошибка, а не тихий no-op."""
    import pytest

    async with async_session_factory() as session:
        intake = _intake("письмо.docx")
        session.add(intake)
        await session.commit()

        with pytest.raises(ValueError, match="нет предложения"):
            await apply_ai_proposal(session, intake)


async def test_unsure_review_explains_without_proposal(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Не-платёжка/нет полей: объяснение сохраняется, предложения нет."""
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

    assert result.proposal is None
    assert intake.status == "error"
    assert "приказ" in intake.recognition["ai_review"]["summary"]
    assert intake.recognition["ai_review"]["proposal"] is None


async def test_bogus_amount_is_not_proposed(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Невалидная сумма от модели в предложение не попадает."""
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

    assert result.proposal == {"tax_kind": "usn_advance"}  # сумма отброшена


def test_fmt_diff_directions() -> None:
    """Дельта для снимка аудита: направление словами, модель ничего не считает сама."""
    from decimal import Decimal

    from app.services.taxes.ai_reviewer import _fmt_diff

    bigger = _fmt_diff(Decimal("478376"), Decimal("478319"))
    assert bigger is not None and "БОЛЬШЕ расчёта на 57" in bigger
    assert "безопасная сторона" in bigger
    smaller = _fmt_diff(Decimal("100"), Decimal("150"))
    assert smaller is not None and "МЕНЬШЕ расчёта на 50" in smaller
    assert _fmt_diff(Decimal("5"), Decimal("5")) == "документ равен расчёту"
    assert _fmt_diff(None, Decimal("5")) is None


async def test_audit_prompt_carries_precomputed_explanations(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Промпт аудита несёт готовые пояснения сверки и запрет пересчитывать суммы."""
    captured: dict[str, str] = {}

    async def call(settings, *, tool, prompt, max_tokens=2048):
        if tool["name"] == _AUDIT_TOOL["name"]:
            captured["audit"] = prompt
            return {"verdict": "ок", "findings": []}
        return {
            "document_type": "other",
            "summary": "х",
            "confidence": 0.5,
            "needs_human": True,
        }

    async with async_session_factory() as session:
        await review_all(session, settings=get_settings(), call=call)

    prompt = captured["audit"]
    assert "НЕ вычисляй разницы сам" in prompt
    assert "## Сверка" in prompt


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

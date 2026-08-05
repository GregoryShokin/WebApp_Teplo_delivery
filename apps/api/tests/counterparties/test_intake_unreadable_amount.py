"""Документ, у которого не прочиталась сумма, не должен исчезать в терминальном статусе.

АУДИТ ЗАМКНУТОСТИ 05.08.2026 нашёл здесь молчаливую потерю. Приёмка сваливала в один
терминальный ``ignored`` две совершенно разные новости:

* акт сверки — он учёт действительно не двигает, и ``ignored`` его законный конец;
* «сумму распознать не удалось» — а это значит только то, что МЫ не смогли прочитать
  документ, который контрагент прислал. Такой PDF замолкал навсегда: расход не признавался,
  в ДЗ/КЗ документ не попадал, узнать о пропаже было неоткуда. Так потерялись 4 957,65 ₽
  августовской коммуналки.

Теперь второй случай уходит в ``needs_review`` — ту же очередь, куда попадает документ с
низкой уверенностью распознавания.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from app.core.config import Settings
from app.models import EmailInvoiceIntake
from app.services import email_invoice_ingest


@dataclass
class _Recognition:
    """Минимальный результат распознавания — ровно те поля, что читает process_attachment."""

    amount: Decimal | None
    document_kind: str | None
    engine: str = "test"
    confidence: float = 0.9

    def to_json(self) -> dict:
        return {"amount": str(self.amount) if self.amount is not None else None}


def _attachment(name: str) -> email_invoice_ingest.FetchedAttachment:
    # ``sha256`` — вычисляемое свойство, а не поле: у каждого вложения он свой по содержимому,
    # поэтому уникальность обеспечиваем самим содержимым.
    return email_invoice_ingest.FetchedAttachment(
        mailbox="test@teplo.local",
        message_uid=str(uuid.uuid4()),
        message_id=f"<{uuid.uuid4()}@example.com>",
        from_addr="supplier@example.com",
        subject=name,
        received_at=datetime.now(UTC),
        filename=f"{name}.pdf",
        mime="application/pdf",
        content=f"%PDF-1.4 {uuid.uuid4()}".encode(),
    )


def _run(async_session_factory, monkeypatch, recognition: _Recognition) -> str:
    async def fake_recognize(_content, **_kwargs):
        return recognition

    monkeypatch.setattr(email_invoice_ingest, "recognize", fake_recognize)

    async def scenario() -> str:
        async with async_session_factory() as session:
            attachment = _attachment("документ")
            status = await email_invoice_ingest.process_attachment(
                session, attachment, settings=Settings(anthropic_api_key="test-key")
            )
            await session.commit()
            intake = await session.scalar(
                select(EmailInvoiceIntake).where(
                    EmailInvoiceIntake.attachment_sha256 == attachment.sha256
                )
            )
            assert intake is not None
            assert intake.status == status
            return status

    return asyncio.run(scenario())


def test_unreadable_amount_goes_to_review_not_silence(
    async_session_factory, monkeypatch
) -> None:
    """Сумму не прочитали — это наш пробел, и он обязан попасть человеку в очередь."""
    status = _run(
        async_session_factory,
        monkeypatch,
        _Recognition(amount=None, document_kind="upd"),
    )
    assert status == "needs_review", "документ без суммы не должен уходить в терминальный статус"


def test_reconciliation_act_still_ignored(async_session_factory, monkeypatch) -> None:
    """Акт сверки учёт не двигает — здесь ignored остаётся правильным ответом."""
    status = _run(
        async_session_factory,
        monkeypatch,
        _Recognition(amount=Decimal("1000.00"), document_kind="reconciliation"),
    )
    assert status == "ignored"


def test_reconciliation_without_amount_is_also_ignored(
    async_session_factory, monkeypatch
) -> None:
    """У акта сверки суммы может не быть вовсе — он всё равно акт сверки, а не пробел."""
    status = _run(
        async_session_factory,
        monkeypatch,
        _Recognition(amount=None, document_kind="reconciliation"),
    )
    assert status == "ignored"

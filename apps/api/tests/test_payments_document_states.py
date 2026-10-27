"""История документов не выдумывает отмену банковского платежа."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import EmailInvoiceIntake
from app.services.payments_aggregator import _invoice_items, list_payments


@pytest.mark.parametrize(
    ("status", "label"),
    [
        ("closing", "Акт / УПД"),
        ("duplicate", "Дубликат"),
        ("ignored", "Не к оплате"),
        ("excluded", "Исключён из очереди"),
    ],
)
async def test_document_history_preserves_reason_without_fabricating_cancellation(
    async_session_factory: async_sessionmaker[AsyncSession], status: str, label: str
) -> None:
    created = datetime(2026, 10, 27, 9, tzinfo=UTC)
    async with async_session_factory() as session:
        intake = EmailInvoiceIntake(
            mailbox="corporate",
            attachment_sha256=uuid.uuid4().hex * 2,
            received_at=datetime(2026, 10, 22, 10, tzinfo=UTC),
            created_at=created,
            status=status,
            recognition={"recipient_name": "Поставщик", "amount": "9000"},
        )
        session.add(intake)
        await session.commit()

        item = next(row for row in await _invoice_items(session) if row.ref_id == intake.id)
        assert item.state == status
        assert item.state_label == label
        assert item.bucket is None
        assert not item.can_edit and not item.can_send_to_bank and not item.can_cancel
        assert item.created_at == created
        assert item.extra["intake_status"] == status

        history_ids = {row.id for row in await list_payments(session, scope="all")}
        active_ids = {row.id for row in await list_payments(session, scope="active")}
        assert item.id in history_ids
        assert item.id not in active_ids

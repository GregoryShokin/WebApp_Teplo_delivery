"""Тип файла в окне разбора определяется по содержимому, а не по слову отправителя.

Прод, 03.08.2026: счёт «Назад в будущее» разобрался правильно, но вместо превью в окне был
пустой прямоугольник, а сам PDF скачивался на диск. Причина не в разборе — в заголовке ответа.
Тип вложения мы берём из письма (``part.get_content_type()``), а 1С-рассылки и часть почтовых
шлюзов подписывают PDF как ``application/octet-stream``. Фронт делает из ответа blob и кладёт
его в iframe; blob с таким типом браузер не рисует, а СКАЧИВАЕТ. Внешне это выглядит как
«превью сломалось», хотя байты дошли целыми.

Здесь закреплено, что канал отдаёт честный тип независимо от того, что написал отправитель.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

from cp_helpers import admin_headers
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import EmailInvoiceIntake
from app.services.utility_images import sniff_media_type

# Минимальный валидный PDF: важны только первые байты — по ним и опознаётся формат.
PDF_BYTES = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n"
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 32
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def test_sniff_overrides_opaque_declared_type() -> None:
    """PDF, подписанный как octet-stream, всё равно опознаётся как PDF."""
    assert sniff_media_type(PDF_BYTES, "application/octet-stream") == "application/pdf"
    assert sniff_media_type(PDF_BYTES, "binary/octet-stream") == "application/pdf"
    assert sniff_media_type(PDF_BYTES, None) == "application/pdf"
    assert sniff_media_type(PDF_BYTES, "") == "application/pdf"
    assert sniff_media_type(JPEG_BYTES, "application/octet-stream") == "image/jpeg"
    assert sniff_media_type(PNG_BYTES, None) == "image/png"


def test_sniff_keeps_informative_declared_type() -> None:
    """Осмысленный заявленный тип не трогаем: в нём бывает точность, которой сигнатуры нет.

    ``image/heif`` и ``image/heic`` — один контейнер с одинаковой сигнатурой, и конвертация
    смотрит именно на заявленный тип.
    """
    assert sniff_media_type(PDF_BYTES, "application/pdf") == "application/pdf"
    assert sniff_media_type(JPEG_BYTES, "image/jpeg") == "image/jpeg"
    assert sniff_media_type(b"\x00\x00\x00\x18ftypheic", "image/heif") == "image/heif"


def test_sniff_falls_back_when_nothing_known() -> None:
    """Неопознанное содержимое без заявленного типа получает фолбэк канала."""
    assert sniff_media_type(b"not a known format", None) == "application/octet-stream"
    assert (
        sniff_media_type(b"not a known format", None, fallback="application/pdf")
        == "application/pdf"
    )


def test_intake_pdf_endpoint_ignores_lying_sender(
    client: TestClient,
    async_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """HTTP-слой: тот же PDF отдаётся как application/pdf, чем бы его ни назвал отправитель."""
    headers = asyncio.run(admin_headers(async_session_factory))
    lying_id = asyncio.run(_make_intake(async_session_factory, mime="application/octet-stream"))
    honest_id = asyncio.run(_make_intake(async_session_factory, mime="application/pdf"))
    unnamed_id = asyncio.run(_make_intake(async_session_factory, mime=None))

    for intake_id in (lying_id, honest_id, unnamed_id):
        response = client.get(f"/api/v1/payment-page/intakes/{intake_id}/pdf", headers=headers)
        assert response.status_code == 200
        # Именно этот заголовок решает, покажет браузер документ во фрейме или скачает его.
        assert response.headers["content-type"].startswith("application/pdf")
        assert response.headers["content-disposition"].startswith("inline")
        assert response.content.startswith(b"%PDF-")


async def _make_intake(
    session_factory: async_sessionmaker[AsyncSession], *, mime: str | None
) -> uuid.UUID:
    async with session_factory() as session:
        intake = EmailInvoiceIntake(
            mailbox="corporate",
            from_addr="buh@example.test",
            subject=f"Счёт с типом {mime}",
            status="needs_review",
            received_at=datetime(2026, 8, 3, tzinfo=UTC),
            attachment_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            attachment_filename="Счёт 0000-003174.pdf",
            attachment_mime=mime,
            pdf_bytes=PDF_BYTES,
            recognition={"amount": "83092.00"},
        )
        session.add(intake)
        await session.commit()
        return intake.id

"""Приём налоговых документов из почты бухгалтера → staging ``tax_document_intake``.

Обвязка над разбором ([document_parser]): скачать docx/xls из ящика, отфильтровать по
отправителю (налоговый агент), разобрать, положить в staging со статусом. Дедуп по SHA-256
содержимого — повторный проход письма не плодит строк.

Разбор здесь НЕ создаёт факты налога напрямую и НЕ двигает деньги. Он складывает
распознанное для проверки владельцем: тип платежа выводится из рукописного имени файла,
поэтому уверенный разбор идёт в ``parsed``, неуверенный (смешанный ЕНП, битая сумма) —
в ``needs_review``, а приказы/договоры — в ``unsupported``.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models.tax import TaxDocumentIntake
from app.services.mail.imap_client import (
    FetchedAttachment,
    MailAccount,
    configured_accounts,
    fetch_tax_document_attachments,
)
from app.services.taxes.document_parser import (
    PaymentOrderDoc,
    PayrollStatementDoc,
    parse_payment_order,
    parse_payroll_statement,
)

logger = logging.getLogger(__name__)

# Отправители, чьи документы считаем налоговыми. По умолчанию — налоговый агент из методологии.
DEFAULT_TAX_SENDERS: tuple[str, ...] = ("askad02@mail.ru",)


def _received_year(att: FetchedAttachment) -> int:
    from datetime import date

    return att.received_at.year if att.received_at else date.today().year


def _classify_document(filename: str) -> str:
    """Тип документа по имени файла, до разбора содержимого."""
    low = (filename or "").lower()
    if low.endswith(".docx") or low.endswith(".doc"):
        # Платёжки приходят в docx; приказы/договоры тоже бывают docx, но обычно .xls.
        return "payment_order"
    if "вед" in low or "ведомост" in low:
        return "payroll_statement"
    if any(k in low for k in ("приказ", "договор", "тд", "т-1", "т-6", "св о рожд")):
        return "unsupported_kadr"
    if low.endswith((".xls", ".xlsx")):
        # xls без явных признаков — пробуем как ведомость, разбор покажет.
        return "payroll_statement"
    return "unknown"


def _payment_order_recognition(doc: PaymentOrderDoc) -> dict:
    return {
        "amount": str(doc.amount) if doc.amount is not None else None,
        "kbk": doc.kbk,
        "recipient": doc.recipient,
        "tax_kind": doc.tax_kind,
        "due_date": doc.due_date.isoformat() if doc.due_date else None,
        "period_hint": doc.period_hint,
        "purpose": doc.purpose,
        "review_reasons": doc.review_reasons,
    }


def _payroll_recognition(doc: PayrollStatementDoc) -> dict:
    return {
        "doc_number": doc.doc_number,
        "payout_kind": doc.payout_kind,
        "period_start": doc.period_start.isoformat() if doc.period_start else None,
        "period_end": doc.period_end.isoformat() if doc.period_end else None,
        "total": str(doc.total),
        "rows": [
            {"tab_number": r.tab_number, "employee": r.employee, "amount": str(r.amount)}
            for r in doc.rows
        ],
        "review_reasons": doc.review_reasons,
    }


def parse_attachment(att: FetchedAttachment) -> tuple[str, str, dict, str | None]:
    """Разобрать вложение. Возвращает (document_type, status, recognition, error).

    Чистая функция от вложения — тестируется на фикстурах без почты и БД.
    """
    kind = _classify_document(att.filename or "")
    if kind == "unsupported_kadr":
        return "unknown", "unsupported", {"reason": "кадровый документ, не платёжный"}, None
    year = _received_year(att)

    try:
        if kind == "payment_order":
            doc = parse_payment_order(att.content, filename=att.filename or "", default_year=year)
            recognition = _payment_order_recognition(doc)
            status = "needs_review" if doc.needs_review else "parsed"
            return "payment_order", status, recognition, None
        if kind == "payroll_statement":
            doc = parse_payroll_statement(att.content, filename=att.filename or "")
            recognition = _payroll_recognition(doc)
            status = "needs_review" if doc.needs_review else "parsed"
            return "payroll_statement", status, recognition, None
    except Exception as exc:  # noqa: BLE001 - разбор одного файла не валит проход
        logger.warning("tax ingest: разбор упал sha=%s", att.sha256[:12], exc_info=True)
        return "unknown", "error", {}, str(exc)[:500]

    return "unknown", "unsupported", {"reason": "неопознанный тип документа"}, None


def _sender_matches(from_addr: str | None, senders: tuple[str, ...]) -> bool:
    low = (from_addr or "").lower()
    return any(s in low for s in senders)


async def ingest_tax_documents(
    session: AsyncSession,
    *,
    settings: Settings | None = None,
    senders: tuple[str, ...] = DEFAULT_TAX_SENDERS,
    fetch=fetch_tax_document_attachments,
    accounts: list[MailAccount] | None = None,
) -> dict[str, int]:
    """Опросить ящики, разобрать документы налогового агента, положить в staging.

    Идемпотентно: вложение по SHA-256 уже виденное — пропускается. ``fetch`` и ``accounts``
    инъектируются для тестов (по умолчанию — реальный IMAP и учётки из настроек). Каждое
    вложение коммитится в цикле, чтобы один сбой не терял прогресс.
    """
    settings = settings or get_settings()
    if accounts is None:
        accounts = configured_accounts(settings)
    # Тесты инъектируют fetch без настроенной почты: одной фиктивной учётки достаточно,
    # реальный IMAP всё равно не вызывается.
    if not accounts and fetch is not fetch_tax_document_attachments:
        accounts = [MailAccount("test", "test@local", "x")]
    if not accounts:
        logger.info("tax ingest: почта не настроена (MAILRU_* пусто) — пропуск")
        return {"status": "not_configured"}

    result: dict[str, int] = {
        "fetched": 0,
        "skipped_sender": 0,
        "duplicate": 0,
        "parsed": 0,
        "needs_review": 0,
        "unsupported": 0,
        "error": 0,
    }

    for account in accounts:
        try:
            attachments = await asyncio.to_thread(
                fetch,
                account,
                host=settings.mailru_imap_host,
                port=settings.mailru_imap_port,
                lookback_days=settings.mail_poll_lookback_days,
            )
        except Exception:  # noqa: BLE001 - сбой одного ящика не валит другой
            logger.warning("tax ingest: сбой чтения ящика %s", account.label, exc_info=True)
            result["error"] += 1
            continue

        for att in attachments:
            result["fetched"] += 1
            if not _sender_matches(att.from_addr, senders):
                result["skipped_sender"] += 1
                continue
            exists = await session.scalar(
                select(TaxDocumentIntake.id).where(
                    TaxDocumentIntake.attachment_sha256 == att.sha256
                )
            )
            if exists is not None:
                result["duplicate"] += 1
                continue
            document_type, status, recognition, error = parse_attachment(att)
            session.add(
                TaxDocumentIntake(
                    mailbox=att.mailbox,
                    from_addr=att.from_addr,
                    subject=att.subject,
                    message_id=att.message_id,
                    message_uid=att.message_uid,
                    received_at=att.received_at,
                    filename=att.filename,
                    mime=att.mime,
                    attachment_sha256=att.sha256,
                    content=att.content,
                    document_type=document_type,
                    status=status,
                    recognition=recognition,
                    error=error,
                )
            )
            await session.commit()
            result[status] = result.get(status, 0) + 1

    return result


# Экспорт для тестов и потребителей
__all__ = [
    "DEFAULT_TAX_SENDERS",
    "MailAccount",
    "ingest_tax_documents",
    "parse_attachment",
]

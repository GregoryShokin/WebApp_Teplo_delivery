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
import re
import zipfile

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
    TurnoverStatementDoc,
    parse_injury_payment,
    parse_payment_order,
    parse_payroll_statement,
    parse_turnover_statement,
)

logger = logging.getLogger(__name__)

# Отправители, чьи документы считаем налоговыми. По умолчанию — налоговый агент из методологии.
DEFAULT_TAX_SENDERS: tuple[str, ...] = ("askad02@mail.ru",)


def resolve_tax_senders(raw: str | None) -> tuple[str, ...]:
    """Список отправителей из настройки (``tax_document_senders``, через запятую) → кортеж.

    Пусто/None → дефолт методологии. Так владелец добавляет адрес нового бухгалтера
    настройкой, не трогая код, и счёт с другого адреса перестаёт молча теряться.
    """
    if not raw:
        return DEFAULT_TAX_SENDERS
    parsed = tuple(part.strip().lower() for part in raw.split(",") if part.strip())
    return parsed or DEFAULT_TAX_SENDERS


def _received_year(att: FetchedAttachment) -> int:
    from datetime import date

    return att.received_at.year if att.received_at else date.today().year


def _classify_document(filename: str) -> str:
    """Тип документа по имени файла, до разбора содержимого."""
    low = (filename or "").lower()
    # Платёжка взноса на травматизм (ПД-налог, .xls «0,2 %» / «травматизм») — тоже платёжка,
    # но со своим разбором (.xls). Раньше уходила в Т-53-парсер и висла в «нужна проверка».
    if re.search(r"0[.,]2\s*%|травматизм|несчастн", low):
        return "payment_order"
    # Оборотка — раньше «вед» и раньше общего .xls-фолбэка: иначе уедет в Т-53-парсер.
    if "оборот" in low or "сальдо" in low:
        return "turnover_statement"
    if "вед" in low or "ведомост" in low:
        return "payroll_statement"
    # Кадровое — раньше расширений: «Приказ … .doc» — это приказ, а не платёжка
    # (раньше .doc уводил его в разбор платёжек, где он падал нечитаемым zip'ом).
    if any(k in low for k in ("приказ", "договор", "т-1", "т-6", "св о рожд")) or re.search(
        r"\bтд\b", low
    ):
        return "unsupported_kadr"
    if low.endswith(".docx") or low.endswith(".doc"):
        # Платёжки приходят в docx; приказы/договоры тоже бывают docx, но обычно .xls.
        return "payment_order"
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


def _money_or_none(value: object) -> str | None:
    return str(value) if value is not None else None


def _turnover_recognition(doc: TurnoverStatementDoc) -> dict:
    return {
        "year": doc.year,
        "month": doc.month,
        "period_hint": doc.period_code,
        "accrued_total": str(doc.accrued_total),
        "ndfl_total": str(doc.ndfl_total),
        "contributions_total": str(doc.contributions_total),
        "injury_total": str(doc.injury_total),
        "rows": [
            {
                "tab_number": r.tab_number,
                "employee": r.employee,
                "oklad": _money_or_none(r.oklad),
                "days": _money_or_none(r.days),
                "accrued": _money_or_none(r.accrued),
                "ndfl": _money_or_none(r.ndfl),
                "advance": _money_or_none(r.advance),
                "contributions": _money_or_none(r.contributions),
                "injury": _money_or_none(r.injury),
                "deduction": _money_or_none(r.deduction),
                "to_pay": _money_or_none(r.to_pay),
            }
            for r in doc.rows
        ],
        "review_reasons": doc.review_reasons,
    }


def _humanize_parse_error(exc: Exception) -> str:
    """Ошибка разбора — по-русски и с подсказкой, а не голым исключением библиотеки."""
    if isinstance(exc, zipfile.BadZipFile):
        # .docx и .xlsx — zip-контейнеры; сюда попадает битый или чужой файл.
        return (
            "Файл повреждён или расширение не соответствует содержимому "
            "(не открывается как документ Word/Excel). Откройте вручную."
        )
    text = str(exc)
    if "not supported" in text or "Unsupported format" in text:
        return (
            "Формат файла не поддерживается автоматикой — откройте вручную "
            "или попросите бухгалтера пересохранить в .docx / .xls / .xlsx."
        )
    return f"Не удалось разобрать файл: {text}"[:500]


def parse_attachment(att: FetchedAttachment) -> tuple[str, str, dict, str | None]:
    """Разобрать вложение. Возвращает (document_type, status, recognition, error).

    Чистая функция от вложения — тестируется на фикстурах без почты и БД.
    """
    kind = _classify_document(att.filename or "")
    if kind == "unsupported_kadr":
        return "unknown", "unsupported", {"reason": "кадровый документ, не платёжный"}, None
    # Старый Word (.doc, OLE2) автоматика читать не умеет — отсекаем ДО разбора с понятной
    # причиной вместо английского исключения из недр библиотек. Excel читается весь:
    # .xls через xlrd, .xlsx через openpyxl (см. document_parser.open_workbook).
    name = (att.filename or "").lower()
    if name.endswith(".doc"):
        reason = (
            "Старый формат Word (.doc) — автоматика читает только .docx. "
            "Откройте файл вручную или попросите бухгалтера пересохранить в .docx."
        )
        return "unknown", "unsupported", {"reason": reason}, None
    year = _received_year(att)

    try:
        if kind == "payment_order":
            name = (att.filename or "").lower()
            if name.endswith((".xls", ".xlsx")):
                # .xls-платёжка среди платёжек — это ПД-налог взноса на травматизм.
                doc = parse_injury_payment(
                    att.content, filename=att.filename or "", default_year=year
                )
            else:
                doc = parse_payment_order(
                    att.content, filename=att.filename or "", default_year=year
                )
            recognition = _payment_order_recognition(doc)
            status = "needs_review" if doc.needs_review else "parsed"
            return "payment_order", status, recognition, None
        if kind == "payroll_statement":
            doc = parse_payroll_statement(att.content, filename=att.filename or "")
            recognition = _payroll_recognition(doc)
            status = "needs_review" if doc.needs_review else "parsed"
            return "payroll_statement", status, recognition, None
        if kind == "turnover_statement":
            turnover = parse_turnover_statement(att.content, filename=att.filename or "")
            recognition = _turnover_recognition(turnover)
            status = "needs_review" if turnover.needs_review else "parsed"
            return "turnover_statement", status, recognition, None
    except Exception as exc:  # noqa: BLE001 - разбор одного файла не валит проход
        logger.warning("tax ingest: разбор упал sha=%s", att.sha256[:12], exc_info=True)
        return "unknown", "error", {}, _humanize_parse_error(exc)

    return "unknown", "unsupported", {"reason": "неопознанный тип документа"}, None


def _sender_matches(from_addr: str | None, senders: tuple[str, ...]) -> bool:
    low = (from_addr or "").lower()
    return any(s in low for s in senders)


# Статусы, которые владелец может выставить документу вручную из интерфейса.
INTAKE_REVIEW_STATUSES: tuple[str, ...] = ("parsed", "ignored")

# Виды платежа, допустимые в ручной правке распознанного (см. _KIND_PATTERNS парсера).
REVIEW_TAX_KINDS: tuple[str, ...] = (
    "usn_advance",
    "usn_year",
    "contrib_fixed",
    "contrib_extra_1pct",
    "contrib_injury",
    "enp_payroll",
)


async def set_intake_review(
    session: AsyncSession,
    intake: TaxDocumentIntake,
    *,
    status: str,
    overrides: dict | None = None,
) -> None:
    """Владелец проверил документ: пометить готовым к продвижению (``parsed``) или отклонить
    (``ignored``). Уже продвинутый документ не трогаем — он породил обязательство.

    ``overrides`` — ручная дозаправка распознанного (срок уплаты, сумма, вид, период):
    автоматика не угадала — человек указал. Пишется в ``recognition`` (СВЕРХУ распознанного),
    причины ``needs_review`` снимаются: документ проверен человеком, продвижение возьмёт
    исправленные поля. Разрешено только для платёжек — у ведомостей/оборотк поля другие.
    """
    if status not in INTAKE_REVIEW_STATUSES:
        raise ValueError(f"Недопустимый статус проверки: {status!r}.")
    if intake.status == "promoted":
        raise ValueError("Документ уже продвинут в обязательство — статус менять нельзя.")

    if overrides:
        if intake.document_type != "payment_order":
            raise ValueError("Поля можно править только у платёжного поручения.")
        overrides = dict(overrides)
        kind = overrides.get("tax_kind")
        if kind is not None and kind not in REVIEW_TAX_KINDS:
            raise ValueError(
                f"Неизвестный вид платежа: {kind!r}. Допустимые: {', '.join(REVIEW_TAX_KINDS)}."
            )
        # «УСН за год» в хранении — это usn_advance с периодом 'year': отдельного вида
        # в tax_payment нет (CHECK ck_tax_payment_kind), и без маппинга продвижение
        # падало «Неизвестный вид платежа» (находка аудита 27.07.2026).
        if kind == "usn_year":
            overrides["tax_kind"] = "usn_advance"
            overrides.setdefault("period_hint", "year")
        updated = dict(intake.recognition or {})
        for key in ("tax_kind", "amount", "due_date", "period_hint"):
            value = overrides.get(key)
            if value is None:
                continue
            updated[key] = str(value)
        updated["review_reasons"] = []
        updated["manually_reviewed"] = True
        intake.recognition = updated

    intake.status = status
    await session.flush()


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
    "resolve_tax_senders",
]

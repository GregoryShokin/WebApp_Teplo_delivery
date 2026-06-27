"""Ingest счетов из почты → ``SupplierInvoice(source='email')`` («Страница на оплату», Фаза 1).

Поток на одно PDF-вложение (идемпотентно по SHA-256 содержимого):
  скачано → строка ``email_invoice_intake`` → распознавание (гибрид) → матч контрагента
  (email-источник → ИНН → точное имя, иначе заглушка при наличии ИНН) → при достаточной
  уверенности создаём ``SupplierInvoice`` (status=unpaid), иначе оставляем в needs_review для
  оператора (Фаза 2). Деньги при этом НЕ двигаются — это лишь входящий список обязательств.

Запускается циклически джобой ``poll_mail_invoices`` в планировщике.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from email.utils import parseaddr

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models import (
    Counterparty,
    CounterpartyCollectionSource,
    CounterpartyPayableProfile,
    CounterpartyRole,
    EmailInvoiceIntake,
    SupplierInvoice,
)
from app.services import counterparty_registry as registry
from app.services.invoice_recognition import RecognizedInvoice, recognize
from app.services.mail.imap_client import (
    FetchedAttachment,
    configured_accounts,
    fetch_pdf_attachments,
)

logger = logging.getLogger(__name__)

# Отправители, чьи PDF целиком игнорируем (заведомый шум). iiko здесь БОЛЬШЕ НЕТ: от него
# приходят настоящие счета на оплату (лицензии iiko/Курьерика), а его акты/сверки/УПД
# отсекаются классификатором по типу документа (recognize → document_kind), а не по отправителю.
SKIP_SENDER_SUBSTRINGS: tuple[str, ...] = ()


def _guess_type(inn: str | None) -> str:
    # 12 цифр — ИП (individual), 10 — юрлицо (legal_entity). Как в counterparty_invoice_sync.
    return "individual" if inn and len(inn) == 12 else "legal_entity"


def _sender_email(from_addr: str | None) -> str | None:
    if not from_addr:
        return None
    email = parseaddr(from_addr)[1].strip().lower()
    return email or None


async def _counterparty_by_email(session: AsyncSession, email: str | None) -> uuid.UUID | None:
    if not email:
        return None
    row = await session.scalar(
        select(CounterpartyCollectionSource).where(
            CounterpartyCollectionSource.kind == "email",
            func.lower(CounterpartyCollectionSource.value) == email,
        )
    )
    return row.counterparty_id if row else None


async def _counterparty_by_inn(session: AsyncSession, inn: str | None) -> uuid.UUID | None:
    if not inn:
        return None
    row = await session.scalar(select(Counterparty).where(Counterparty.inn == inn))
    return row.id if row else None


async def _counterparty_by_exact_name(
    session: AsyncSession, name: str | None
) -> uuid.UUID | None:
    """Консервативный матч по имени: только точное совпадение без регистра. Фуцци-матч НЕ
    делаем — риск привязать счёт не тому контрагенту и уйти на ошибочный платёж."""
    if not name:
        return None
    normalized = " ".join(name.split()).lower()
    if len(normalized) < 4:
        return None
    row = await session.scalar(
        select(Counterparty).where(func.lower(Counterparty.name) == normalized)
    )
    return row.id if row else None


async def _ensure_email_source(
    session: AsyncSession, counterparty_id: uuid.UUID, email: str | None
) -> None:
    """Привязать email-отправителя к контрагенту (роутинг будущих писем). value глобально
    уникален — добавляем только если такого ещё нет."""
    if not email:
        return
    existing = await session.scalar(
        select(CounterpartyCollectionSource).where(
            func.lower(CounterpartyCollectionSource.value) == email
        )
    )
    if existing is None:
        session.add(
            CounterpartyCollectionSource(
                counterparty_id=counterparty_id, kind="email", value=email
            )
        )
        await session.flush()


async def _match_or_create_counterparty(
    session: AsyncSession, rec: RecognizedInvoice, email: str | None
) -> uuid.UUID | None:
    cp_id = (
        await _counterparty_by_email(session, email)
        or await _counterparty_by_inn(session, rec.inn)
        or await _counterparty_by_exact_name(session, rec.recipient_name)
    )
    if cp_id is not None:
        await _ensure_email_source(session, cp_id, email)
        return cp_id

    # Не нашли — создаём заглушку ТОЛЬКО если есть ИНН (иначе нечем однозначно идентифицировать,
    # пусть оператор разберёт вручную в Фазе 2).
    if not rec.inn:
        return None
    counterparty = Counterparty(
        name=rec.recipient_name or f"ИНН {rec.inn}",
        inn=rec.inn,
        type=_guess_type(rec.inn),
        status="requires_setup",
    )
    session.add(counterparty)
    await session.flush()
    session.add(CounterpartyRole(counterparty_id=counterparty.id, role="supplier"))
    session.add(
        CounterpartyPayableProfile(
            counterparty_id=counterparty.id, internal_name=rec.recipient_name
        )
    )
    await _ensure_email_source(session, counterparty.id, email)
    await session.flush()
    return counterparty.id


def _confidence_decimal(value: float) -> Decimal:
    return Decimal(str(round(max(0.0, min(1.0, value)), 3)))


async def _find_duplicate_email_invoice(
    session: AsyncSession, cp_id: uuid.UUID, rec: RecognizedInvoice
) -> SupplierInvoice | None:
    """Повтор того же счёта из почты: тот же контрагент + сумма + дата + номер (None==None).

    Ловит ситуацию «поставщик прислал тот же счёт повторным письмом» — байты PDF отличаются,
    поэтому SHA-дедуп не срабатывает, а накладная по сути одна."""
    candidates = (
        await session.scalars(
            select(SupplierInvoice).where(
                SupplierInvoice.source == "email",
                SupplierInvoice.counterparty_id == cp_id,
                SupplierInvoice.amount == rec.amount,
                SupplierInvoice.payment_status != "void",
            )
        )
    ).all()
    want_number = rec.invoice_number or None
    for c in candidates:
        if c.invoice_date == rec.invoice_date and (c.number or None) == want_number:
            return c
    return None


def _intake_amount(intake: EmailInvoiceIntake) -> Decimal | None:
    raw = (intake.recognition or {}).get("amount")
    if raw in (None, ""):
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    return value if value > 0 else None


async def materialize_from_intake(
    session: AsyncSession,
    intake: EmailInvoiceIntake,
    *,
    counterparty_id: uuid.UUID | None = None,
) -> str:
    """Создать ``SupplierInvoice`` из подтверждённого оператором intake (кнопка «Подтвердить»).

    Сумма и поля берутся из распознанного (intake.recognition). Контрагент — переданный
    оператором или уже сматченный. Возвращает итоговый статус intake (linked/duplicate).
    Бросает ValueError, если нечем материализовать (нет контрагента/суммы)."""
    cp_id = counterparty_id or intake.counterparty_id
    if cp_id is None:
        raise ValueError("Не выбран контрагент")
    amount = _intake_amount(intake)
    if amount is None:
        raise ValueError("В распознанном нет суммы к оплате")

    rec_json = intake.recognition or {}
    number = rec_json.get("invoice_number") or None
    inv_date: date | None = None
    if rec_json.get("invoice_date"):
        try:
            inv_date = date.fromisoformat(str(rec_json["invoice_date"]))
        except ValueError:
            inv_date = None

    intake.counterparty_id = cp_id
    probe = RecognizedInvoice(amount=amount, invoice_number=number, invoice_date=inv_date)
    dup = await _find_duplicate_email_invoice(session, cp_id, probe)
    if dup is not None:
        intake.invoice_id = dup.id
        intake.status = "duplicate"
        return intake.status

    invoice = SupplierInvoice(
        counterparty_id=cp_id,
        source="email",
        direction="payable",
        external_id=intake.attachment_sha256[:128],
        number=number,
        invoice_date=inv_date,
        amount=amount,
        payment_status="unpaid",
        note=intake.subject,
        raw_payload={
            "mailbox": intake.mailbox,
            "from": intake.from_addr,
            "subject": intake.subject,
            "message_id": intake.message_id,
            "attachment_filename": intake.attachment_filename,
            "recognition": rec_json,
            "confirmed_from_intake": True,
        },
    )
    session.add(invoice)
    await session.flush()
    intake.invoice_id = invoice.id
    intake.status = "linked"
    return intake.status


async def confirm_intake_with_review(
    session: AsyncSession,
    intake: EmailInvoiceIntake,
    *,
    actor_user_id: uuid.UUID | None,
    counterparty_id: uuid.UUID | None = None,
    new_counterparty_name: str | None = None,
    new_counterparty_inn: str | None = None,
    amount: str | None = None,
    invoice_number: str | None = None,
    invoice_date: str | None = None,
    requisites: dict[str, str] | None = None,
    apply_requisites: bool = False,
) -> str:
    """Подтверждение счёта оператором с правками (окно разбора): выбор/создание контрагента,
    скорректированные поля, опц. перенос реквизитов в карточку с верификацией, затем создание
    накладной из распознанного. Возвращает итоговый статус intake."""
    inn = (new_counterparty_inn or "").strip() or None

    if counterparty_id is not None:
        cp = await session.get(Counterparty, counterparty_id)
        if cp is None:
            raise ValueError("Контрагент не найден")
        cp_id = cp.id
    elif new_counterparty_name and new_counterparty_name.strip():
        # Если такой ИНН уже есть — переиспользуем, иначе создаём заглушку.
        cp_id = await _counterparty_by_inn(session, inn)
        if cp_id is None:
            cp = Counterparty(
                name=new_counterparty_name.strip(),
                inn=inn,
                type=_guess_type(inn),
                status="requires_setup",
            )
            session.add(cp)
            await session.flush()
            session.add(CounterpartyRole(counterparty_id=cp.id, role="supplier"))
            session.add(
                CounterpartyPayableProfile(
                    counterparty_id=cp.id, internal_name=new_counterparty_name.strip()
                )
            )
            cp_id = cp.id
    elif intake.counterparty_id is not None:
        cp_id = intake.counterparty_id
    else:
        raise ValueError("Выберите контрагента")

    await _ensure_email_source(session, cp_id, _sender_email(intake.from_addr))

    # Сохраняем правки оператора в распознанное (из него потом собирается накладная).
    rec = dict(intake.recognition or {})
    if amount is not None:
        rec["amount"] = amount
    if invoice_number is not None:
        rec["invoice_number"] = invoice_number.strip() or None
    if invoice_date is not None:
        rec["invoice_date"] = invoice_date.strip() or None
    clean_req = {k: v.strip() for k, v in (requisites or {}).items() if v and v.strip()}
    if requisites is not None:
        rec["requisites"] = clean_req
    intake.recognition = rec
    intake.counterparty_id = cp_id

    if apply_requisites and clean_req:
        await registry.set_requisites(
            session, cp_id, requisites=clean_req, verified=True, actor_user_id=actor_user_id
        )

    return await materialize_from_intake(session, intake, counterparty_id=cp_id)


async def process_attachment(
    session: AsyncSession, att: FetchedAttachment, *, settings: Settings
) -> str:
    """Обработать одно PDF-вложение. Возвращает итоговый статус intake. НЕ коммитит."""
    intake = EmailInvoiceIntake(
        mailbox=att.mailbox,
        from_addr=att.from_addr,
        subject=att.subject,
        message_id=att.message_id,
        message_uid=att.message_uid,
        received_at=att.received_at,
        attachment_filename=att.filename,
        attachment_mime=att.mime,
        attachment_sha256=att.sha256,
        attachment_size=len(att.content),
        pdf_bytes=att.content,
        status="new",
    )
    session.add(intake)
    await session.flush()

    try:
        rec = await recognize(att.content, settings=settings)
    except Exception as exc:  # noqa: BLE001 - распознавание одного файла не валит проход
        logger.warning("распознавание не удалось sha=%s", att.sha256[:12], exc_info=True)
        intake.status = "failed"
        intake.error = str(exc)[:1000]
        return intake.status

    intake.recognition = rec.to_json()
    intake.engine = rec.engine
    intake.confidence = _confidence_decimal(rec.confidence)

    # Закрывающие/сверочные документы (УПД, акт сверки, передаточный акт) и всё без суммы — это
    # не счёт на оплату: фиксируем в журнале как ignored, не материализуем и не показываем в
    # «Актуальных». Сам PDF остаётся — оператор при желании поднимет через фильтр «Все».
    if rec.document_kind in ("upd", "reconciliation", "act") or rec.amount is None:
        intake.status = "ignored"
        return intake.status

    cp_id = await _match_or_create_counterparty(session, rec, _sender_email(att.from_addr))
    intake.counterparty_id = cp_id

    # Авто-материализуем только уверенно опознанный счёт с известным контрагентом. Формат
    # «unknown» (нет явного маркера счёта) — всегда оператору, чтобы не провести неведомый макет.
    if (
        cp_id is None
        or rec.document_kind != "invoice"
        or rec.confidence < settings.invoice_recognition_min_confidence
    ):
        intake.status = "needs_review"
        return intake.status

    dup = await _find_duplicate_email_invoice(session, cp_id, rec)
    if dup is not None:
        # Повторное письмо с тем же счётом — привязываем к исходной, новую не создаём.
        intake.invoice_id = dup.id
        intake.status = "duplicate"
        return intake.status

    invoice = SupplierInvoice(
        counterparty_id=cp_id,
        source="email",
        direction="payable",
        external_id=att.sha256[:128],
        number=rec.invoice_number,
        invoice_date=rec.invoice_date,
        amount=rec.amount,
        payment_status="unpaid",
        note=att.subject,
        raw_payload={
            "mailbox": att.mailbox,
            "from": att.from_addr,
            "subject": att.subject,
            "message_id": att.message_id,
            "attachment_filename": att.filename,
            "recognition": rec.to_json(),
        },
    )
    session.add(invoice)
    await session.flush()
    intake.invoice_id = invoice.id
    intake.status = "linked"
    return intake.status


async def poll_and_ingest(
    session: AsyncSession, *, settings: Settings | None = None
) -> dict[str, object]:
    """Опросить оба ящика, распознать новые PDF-счета и материализовать накладные.

    Идемпотентно: вложение, уже встречавшееся (по SHA-256), пропускается. Каждое вложение —
    своя транзакция (коммит в цикле), чтобы один сбой не терял весь прогресс.
    """
    settings = settings or get_settings()
    accounts = configured_accounts(settings)
    if not accounts:
        logger.info("poll_mail_invoices: почта не настроена (MAILRU_* пусто) — пропуск")
        return {"status": "not_configured"}

    result: dict[str, int] = {
        "fetched": 0, "skipped": 0, "linked": 0,
        "needs_review": 0, "ignored": 0, "failed": 0, "errors": 0,
    }
    for account in accounts:
        try:
            attachments = await asyncio.to_thread(
                fetch_pdf_attachments,
                account,
                host=settings.mailru_imap_host,
                port=settings.mailru_imap_port,
                lookback_days=settings.mail_poll_lookback_days,
            )
        except Exception:  # noqa: BLE001 - сбой логина/соединения одного ящика не валит другой
            logger.warning("poll_mail_invoices: сбой чтения ящика %s", account.label, exc_info=True)
            result["errors"] += 1
            continue

        for att in attachments:
            result["fetched"] += 1
            sender = (att.from_addr or "").lower()
            if any(s in sender for s in SKIP_SENDER_SUBSTRINGS):
                result["skipped"] += 1
                continue
            exists = await session.scalar(
                select(EmailInvoiceIntake.id).where(
                    EmailInvoiceIntake.attachment_sha256 == att.sha256
                )
            )
            if exists is not None:
                result["skipped"] += 1
                continue
            try:
                status = await process_attachment(session, att, settings=settings)
                await session.commit()
                result[status] = result.get(status, 0) + 1
            except Exception:  # noqa: BLE001 - сбой одного вложения не валит проход
                await session.rollback()
                logger.warning(
                    "poll_mail_invoices: сбой обработки вложения sha=%s",
                    att.sha256[:12], exc_info=True,
                )
                result["errors"] += 1

    result["status"] = "ok"  # type: ignore[assignment]
    return result

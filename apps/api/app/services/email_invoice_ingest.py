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
import contextlib
import logging
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parseaddr
from zoneinfo import ZoneInfo

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
from app.services import counterparty_payments as payments
from app.services import counterparty_registry as registry
from app.services import supplier_service_periods as service_periods
from app.services.banking.exceptions import BankCredentialsError, BankFetchError
from app.services.invoice_recognition import RecognizedInvoice, recognize
from app.services.mail.imap_client import (
    FetchedAttachment,
    configured_accounts,
    fetch_pdf_attachments,
)

logger = logging.getLogger(__name__)
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

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


async def _counterparty_by_exact_name(session: AsyncSession, name: str | None) -> uuid.UUID | None:
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
            CounterpartyCollectionSource(counterparty_id=counterparty_id, kind="email", value=email)
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
                # 'sbis' тоже: поставщик на ЭДО может продублировать счёт письмом —
                # почтовый intake должен схлопнуться на уже материализованный счёт.
                SupplierInvoice.source.in_(("email", "sbis")),
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

    period_start: date | None = None
    period_end: date | None = None
    with contextlib.suppress(ValueError, TypeError):
        if rec_json.get("service_period_start"):
            period_start = date.fromisoformat(str(rec_json["service_period_start"]))
        if rec_json.get("service_period_end"):
            period_end = date.fromisoformat(str(rec_json["service_period_end"]))
    profile = await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == cp_id
        )
    )
    period_required = bool(profile and profile.service_period_required)
    period_ambiguous = bool(rec_json.get("service_period_ambiguous"))
    if period_ambiguous and not (period_start is not None and period_end is not None):
        raise ValueError(
            "В счёте найдено несколько периодов оказания услуг — выберите один вручную"
        )
    if period_ambiguous:
        period_status = "ambiguous"
    elif period_start is not None and period_end is not None:
        service_periods.validate_period(period_start, period_end)
        period_status = "ready"
    else:
        period_status = "missing" if period_required else "not_required"

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
        service_period_start=period_start,
        service_period_end=period_end,
        service_period_source=rec_json.get("service_period_source") or None,
        service_period_status=period_status,
        service_period_confidence=rec_json.get("service_period_confidence"),
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
    await service_periods.sync_invoice_accrual(session, invoice)
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
    service_period_start: str | None = None,
    service_period_end: str | None = None,
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
    if service_period_start is not None or service_period_end is not None:
        rec["service_period_start"] = (service_period_start or "").strip() or None
        rec["service_period_end"] = (service_period_end or "").strip() or None
        rec["service_period_source"] = "manual"
        rec["service_period_ambiguous"] = False
    clean_req = {k: v.strip() for k, v in (requisites or {}).items() if v and v.strip()}
    if requisites is not None:
        rec["requisites"] = clean_req
    intake.recognition = rec
    intake.counterparty_id = cp_id

    if apply_requisites and clean_req:
        await registry.set_requisites(
            session, cp_id, requisites=clean_req, verified=True, actor_user_id=actor_user_id
        )

    # Повторный разбор уже-linked счёта (подтвердить/поправить реквизиты, чтобы открыть отправку):
    # накладную НЕ пересоздаём — иначе она стала бы «дублем» сама себе. Реквизиты применены выше;
    # синхронизируем правки полей с существующей накладной.
    if intake.invoice_id is not None and intake.status == "linked":
        inv = await session.get(SupplierInvoice, intake.invoice_id)
        if inv is not None:
            new_amount = _intake_amount(intake)
            if new_amount is not None:
                inv.amount = new_amount
            inv.number = rec.get("invoice_number") or None
            iso_date = rec.get("invoice_date")
            if iso_date:
                with contextlib.suppress(ValueError):
                    inv.invoice_date = date.fromisoformat(str(iso_date))
            start_raw = rec.get("service_period_start")
            end_raw = rec.get("service_period_end")
            profile = await session.scalar(
                select(CounterpartyPayableProfile).where(
                    CounterpartyPayableProfile.counterparty_id == cp_id
                )
            )
            if start_raw and end_raw:
                start = date.fromisoformat(str(start_raw))
                end = date.fromisoformat(str(end_raw))
                service_periods.validate_period(start, end)
                inv.service_period_start = start
                inv.service_period_end = end
                inv.service_period_source = "manual"
                inv.service_period_status = "ready"
                await service_periods.sync_invoice_accrual(session, inv)
            elif profile and profile.service_period_required:
                inv.service_period_status = "missing"
        return intake.status

    return await materialize_from_intake(session, intake, counterparty_id=cp_id)


# --- исключение / удаление / плановая отправка («Страница на оплату») ------------------------


async def exclude_intake(session: AsyncSession, intake: EmailInvoiceIntake) -> None:
    """Ручное исключение счёта из рабочего инбокса в корзину «Исключённые». Если по счёту уже
    создана накладная (не оплачена и не отправлена в банк) — помечаем её ``void``, чтобы убрать
    из платёжного контура. Плановую отправку снимаем. НЕ коммитит — это делает вызывающий."""
    if intake.status == "excluded":
        raise ValueError("Счёт уже в исключённых")
    invoice = await session.get(SupplierInvoice, intake.invoice_id) if intake.invoice_id else None
    if invoice is not None and invoice.draft_id is not None:
        raise ValueError("Счёт уже отправлен в банк — исключение недоступно")
    if invoice is not None and invoice.payment_status not in ("paid", "void"):
        invoice.payment_status = "void"
    intake.previous_status = intake.status
    intake.scheduled_send_date = None
    intake.status = "excluded"


async def restore_intake(session: AsyncSession, intake: EmailInvoiceIntake) -> None:
    """Вернуть счёт из «Исключённых» в рабочий инбокс — в статус до исключения. void-накладную
    возвращаем в unpaid. НЕ коммитит."""
    if intake.status != "excluded":
        raise ValueError("Счёт не в исключённых")
    invoice = await session.get(SupplierInvoice, intake.invoice_id) if intake.invoice_id else None
    if invoice is not None and invoice.payment_status == "void":
        invoice.payment_status = "unpaid"
    intake.status = intake.previous_status or "needs_review"
    intake.previous_status = None


async def delete_intake_forever(session: AsyncSession, intake: EmailInvoiceIntake) -> None:
    """Удалить исключённый счёт НАВСЕГДА вместе с созданной накладной (если она не в банке и не
    оплачена). Доступно только из «Исключённых» — рабочие счета сначала исключают. НЕ коммитит."""
    if intake.status != "excluded":
        raise ValueError("Удалять можно только из «Исключённых»")
    invoice = await session.get(SupplierInvoice, intake.invoice_id) if intake.invoice_id else None
    await session.delete(intake)
    await session.flush()
    if invoice is not None and invoice.draft_id is None and invoice.payment_status != "paid":
        # Накладная почтового счёта живёт только на «Странице на оплату»; у исключённой нет
        # оплат/черновика, поэтому удаляем её безопасно.
        await session.delete(invoice)


async def send_intake_to_bank(
    session: AsyncSession, intake: EmailInvoiceIntake, *, actor_user_id: uuid.UUID | None
) -> None:
    """Создать банк-черновик по подтверждённому счёту — ОБЩИЙ путь для ручной кнопки «Отправить
    в банк» и плановой джобы ``send_scheduled_payments``. Деньги не списываются: уходит черновик
    на подтверждение в банке. Бросает доменные ошибки ``payments.*`` / банковские — вызывающий
    решает, как показать. ``create_payment_draft_for_invoices`` коммитит сам; снятие плановой
    даты тоже коммитим, поэтому функция самодостаточна по транзакции."""
    if intake.status != "linked" or intake.invoice_id is None:
        raise payments.CounterpartyPaymentError("Счёт ещё не подтверждён")
    invoice = await session.get(SupplierInvoice, intake.invoice_id)
    if invoice is None:
        raise payments.CounterpartyPaymentError("Накладная не найдена")
    if invoice.draft_id is not None:
        raise payments.CounterpartyPaymentError("Счёт уже отправлен в банк")
    profile = await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == invoice.counterparty_id
        )
    )
    if profile and profile.service_period_required and invoice.service_period_status != "ready":
        raise payments.CounterpartyPaymentError(
            "Для этого контрагента обязателен период оказания услуги — укажите даты"
        )
    await service_periods.sync_invoice_accrual(session, invoice)
    await payments.create_payment_draft_for_invoices(
        session, invoice_ids=[intake.invoice_id], actor_user_id=actor_user_id
    )
    if intake.scheduled_send_date is not None:
        intake.scheduled_send_date = None
        await session.commit()


async def run_scheduled_sends(session: AsyncSession) -> dict[str, int]:
    """Найти счета с наступившей плановой датой и отправить их в банк. Контрагент с
    неподтверждёнными реквизитами / ошибка банка — пропускаем (дату НЕ снимаем, повторим в
    следующий проход). Возвращает счётчики для лога джобы."""
    today = datetime.now(MOSCOW_TZ).date()
    rows = (
        await session.scalars(
            select(EmailInvoiceIntake).where(
                EmailInvoiceIntake.status == "linked",
                EmailInvoiceIntake.scheduled_send_date.is_not(None),
                EmailInvoiceIntake.scheduled_send_date <= today,
            )
        )
    ).all()
    result = {"due": len(rows), "sent": 0, "skipped": 0, "errors": 0}
    for intake in rows:
        try:
            await send_intake_to_bank(session, intake, actor_user_id=None)
            result["sent"] += 1
        except payments.RequisitesNotVerifiedError:
            result["skipped"] += 1  # ждём, пока оператор подтвердит реквизиты
        except (payments.CounterpartyPaymentError, BankFetchError, BankCredentialsError):
            logger.warning(
                "send_scheduled_payments: счёт intake=%s не отправлен", intake.id, exc_info=True
            )
            result["errors"] += 1
            await session.rollback()
    return result


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
        rec = await recognize(
            att.content,
            settings=settings,
            context_text="\n".join(part for part in (att.subject, att.filename) if part),
        )
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

    profile = None
    if cp_id is not None:
        profile = await session.scalar(
            select(CounterpartyPayableProfile).where(
                CounterpartyPayableProfile.counterparty_id == cp_id
            )
        )
    period_required = bool(profile and profile.service_period_required)

    # Авто-материализуем только уверенно опознанный счёт с известным контрагентом. Формат
    # «unknown» (нет явного маркера счёта) — всегда оператору, чтобы не провести неведомый макет.
    if (
        cp_id is None
        or rec.document_kind != "invoice"
        or rec.confidence < settings.invoice_recognition_min_confidence
        or rec.service_period_ambiguous
        or (period_required and not (rec.service_period_start and rec.service_period_end))
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
        service_period_start=rec.service_period_start,
        service_period_end=rec.service_period_end,
        service_period_source=rec.service_period_source,
        service_period_status=(
            "ready"
            if rec.service_period_start and rec.service_period_end
            else "missing"
            if period_required
            else "not_required"
        ),
        service_period_confidence=rec.service_period_confidence,
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
    await service_periods.sync_invoice_accrual(session, invoice)
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
        "fetched": 0,
        "skipped": 0,
        "linked": 0,
        "needs_review": 0,
        "ignored": 0,
        "failed": 0,
        "errors": 0,
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
                    att.sha256[:12],
                    exc_info=True,
                )
                result["errors"] += 1

    result["status"] = "ok"  # type: ignore[assignment]
    return result

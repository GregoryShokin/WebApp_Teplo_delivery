"""API экрана «Страница на оплату» (Фаза 2, MVP).

Журнал разбора счетов из почты (``email_invoice_intake``): список со статусами, превью
исходного PDF, подтверждение оператором (материализация накладной) и игнор. Редактирование
полей и применение реквизитов в профиль — следующий шаг. Права: чтение —
``counterparties.read``, операции — ``counterparties.operate`` (страница в контуре контрагентов).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, get_current_actor, require_permission
from app.db.session import get_session
from app.models import (
    Counterparty,
    CounterpartyPayableProfile,
    EmailInvoiceIntake,
    SupplierInvoice,
)
from app.models.email_invoice_intake import EMAIL_INTAKE_STATUSES
from app.services import counterparty_payments as payments
from app.services import email_invoice_ingest as ingest
from app.services import supplier_service_periods as service_periods
from app.services.banking.exceptions import BankCredentialsError, BankFetchError

router = APIRouter()

READ = (Depends(require_permission("counterparties.read")),)
OPERATE = (Depends(require_permission("counterparties.operate")),)


class IntakeRead(BaseModel):
    id: uuid.UUID
    mailbox: str
    from_addr: str | None
    subject: str | None
    received_at: datetime | None
    attachment_filename: str | None
    status: str
    engine: str | None
    confidence: float | None
    counterparty_id: uuid.UUID | None
    counterparty_name: str | None
    invoice_id: uuid.UUID | None
    # Плоско вынесенные распознанные поля — для таблицы.
    recipient_name: str | None
    inn: str | None
    amount: str | None
    invoice_number: str | None
    invoice_date: str | None
    service_period_start: str | None
    service_period_end: str | None
    service_period_source: str | None
    service_period_status: str | None
    service_period_confidence: float | None
    service_period_required: bool
    service_period_mode: str
    # Распознанные банковские реквизиты (recipientName/inn/kpp/bankAcnt/bankBik/corr) — для
    # предзаполнения окна разбора.
    requisites: dict[str, Any]
    # Подтверждены ли реквизиты контрагента (нужно для отправки в банк).
    requisites_verified: bool
    # Состояние связанной накладной: оплачена/частично и заведён ли банк-черновик.
    invoice_payment_status: str | None
    invoice_in_draft: bool
    # Статья ДДС, выбранная для оплаты этого счёта (None → дефолтная «Оплата поставщикам»).
    invoice_dds_article_id: uuid.UUID | None
    # Закреплённая за контрагентом статья ДДС — предзаполняет окно оплаты.
    default_dds_article_id: uuid.UUID | None
    has_pdf: bool
    # Дата плановой авто-отправки в банк (ISO). None = отправка только вручную.
    scheduled_send_date: str | None
    created_at: datetime


class ReviewRequisites(BaseModel):
    recipientName: str | None = None
    inn: str | None = None
    kpp: str | None = None
    bankAcnt: str | None = None
    bankBik: str | None = None
    recipientCorrAccountNumber: str | None = None


class ConfirmIn(BaseModel):
    # Существующий контрагент (пикер) ИЛИ создание нового по имени+ИНН. Если оба пустые —
    # берётся уже сматченный intake.counterparty_id.
    counterparty_id: uuid.UUID | None = None
    new_counterparty_name: str | None = None
    new_counterparty_inn: str | None = None
    # Правки распознанных полей (если None — оставляем как было).
    amount: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None
    service_period_start: str | None = None
    service_period_end: str | None = None
    requisites: ReviewRequisites | None = None
    # Перенести реквизиты в карточку контрагента и пометить проверенными.
    apply_requisites: bool = False


class SendToBankIn(BaseModel):
    # Статья ДДС для оплаты этого счёта (None → дефолтная «Оплата поставщикам» при гашении).
    dds_article_id: uuid.UUID | None = None
    # Закрепить выбранную статью за контрагентом (предзаполнит окно при следующей оплате).
    remember_for_counterparty: bool = False


class ScheduleSendIn(BaseModel):
    # Дата, к которой счёт автоматически уйдёт в банк (джоба отправляет, когда дата наступила).
    send_date: date
    # Статья ДДС / закрепление — как при немедленной отправке (счёт уйдёт позже с этой статьёй).
    dds_article_id: uuid.UUID | None = None
    remember_for_counterparty: bool = False


def _to_read(
    intake: EmailInvoiceIntake,
    counterparty_name: str | None,
    *,
    requisites_verified: bool | None = False,
    invoice_payment_status: str | None = None,
    invoice_draft_id: uuid.UUID | None = None,
    invoice_dds_article_id: uuid.UUID | None = None,
    default_dds_article_id: uuid.UUID | None = None,
    invoice_service_period_start: date | None = None,
    invoice_service_period_end: date | None = None,
    invoice_service_period_source: str | None = None,
    invoice_service_period_status: str | None = None,
    invoice_service_period_confidence: Any | None = None,
    service_period_required: bool | None = False,
    service_period_mode: str | None = "manual",
) -> IntakeRead:
    rec: dict[str, Any] = intake.recognition or {}
    period_start_value = (
        invoice_service_period_start.isoformat()
        if invoice_service_period_start
        else rec.get("service_period_start")
    )
    period_end_value = (
        invoice_service_period_end.isoformat()
        if invoice_service_period_end
        else rec.get("service_period_end")
    )
    period_status_value = invoice_service_period_status
    if period_status_value is None:
        if rec.get("service_period_ambiguous"):
            period_status_value = "ambiguous"
        elif period_start_value and period_end_value:
            period_status_value = "ready"
        else:
            period_status_value = "missing" if service_period_required else "not_required"
    return IntakeRead(
        id=intake.id,
        mailbox=intake.mailbox,
        from_addr=intake.from_addr,
        subject=intake.subject,
        received_at=intake.received_at,
        attachment_filename=intake.attachment_filename,
        status=intake.status,
        engine=intake.engine,
        confidence=float(intake.confidence) if intake.confidence is not None else None,
        counterparty_id=intake.counterparty_id,
        counterparty_name=counterparty_name,
        invoice_id=intake.invoice_id,
        recipient_name=rec.get("recipient_name"),
        inn=rec.get("inn"),
        amount=rec.get("amount"),
        invoice_number=rec.get("invoice_number"),
        invoice_date=rec.get("invoice_date"),
        service_period_start=period_start_value,
        service_period_end=period_end_value,
        service_period_source=invoice_service_period_source or rec.get("service_period_source"),
        service_period_status=period_status_value,
        service_period_confidence=(
            float(invoice_service_period_confidence)
            if invoice_service_period_confidence is not None
            else (
                float(rec["service_period_confidence"])
                if rec.get("service_period_confidence") is not None
                else None
            )
        ),
        service_period_required=bool(service_period_required),
        service_period_mode=service_period_mode or "manual",
        requisites=rec.get("requisites") or {},
        requisites_verified=bool(requisites_verified),
        invoice_payment_status=invoice_payment_status,
        invoice_in_draft=invoice_draft_id is not None,
        invoice_dds_article_id=invoice_dds_article_id,
        default_dds_article_id=default_dds_article_id,
        has_pdf=intake.pdf_bytes is not None,
        scheduled_send_date=(
            intake.scheduled_send_date.isoformat() if intake.scheduled_send_date else None
        ),
        created_at=intake.created_at,
    )


async def _get_intake(session: AsyncSession, intake_id: uuid.UUID) -> EmailInvoiceIntake:
    intake = await session.get(EmailInvoiceIntake, intake_id)
    if intake is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Запись не найдена")
    return intake


async def _apply_article_choice(
    session: AsyncSession,
    intake: EmailInvoiceIntake,
    *,
    dds_article_id: uuid.UUID | None,
    remember_for_counterparty: bool,
) -> None:
    """Проставить выбранную статью ДДС на счёт (её берёт гашение ``apply_payment_status``) и
    опционально закрепить за контрагентом — предзаполнит окно при следующей оплате."""
    if dds_article_id is None:
        return
    if intake.invoice_id is not None:
        invoice = await session.get(SupplierInvoice, intake.invoice_id)
        if invoice is not None:
            invoice.dds_article_id = dds_article_id
            await service_periods.sync_invoice_accrual(session, invoice)
    if remember_for_counterparty and intake.counterparty_id is not None:
        profile = await session.scalar(
            select(CounterpartyPayableProfile).where(
                CounterpartyPayableProfile.counterparty_id == intake.counterparty_id
            )
        )
        if profile is None:
            profile = CounterpartyPayableProfile(counterparty_id=intake.counterparty_id)
            session.add(profile)
        profile.default_dds_article_id = dds_article_id
    await session.flush()


async def _load_read(session: AsyncSession, intake_id: uuid.UUID) -> IntakeRead:
    """Перечитать запись с присоединённым контекстом (контрагент, верификация реквизитов, статус
    накладной) — единый ответ для всех мутаций, как в списке."""
    row = (
        await session.execute(
            select(
                EmailInvoiceIntake,
                Counterparty.name,
                CounterpartyPayableProfile.requisites_verified,
                SupplierInvoice.payment_status,
                SupplierInvoice.draft_id,
                SupplierInvoice.dds_article_id,
                CounterpartyPayableProfile.default_dds_article_id,
                SupplierInvoice.service_period_start,
                SupplierInvoice.service_period_end,
                SupplierInvoice.service_period_source,
                SupplierInvoice.service_period_status,
                SupplierInvoice.service_period_confidence,
                CounterpartyPayableProfile.service_period_required,
                CounterpartyPayableProfile.service_period_mode,
            )
            .outerjoin(Counterparty, Counterparty.id == EmailInvoiceIntake.counterparty_id)
            .outerjoin(
                CounterpartyPayableProfile,
                CounterpartyPayableProfile.counterparty_id == EmailInvoiceIntake.counterparty_id,
            )
            .outerjoin(SupplierInvoice, SupplierInvoice.id == EmailInvoiceIntake.invoice_id)
            .where(EmailInvoiceIntake.id == intake_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Запись не найдена")
    (
        intake,
        cp_name,
        verified,
        pay_status,
        draft_id,
        inv_article,
        default_article,
        period_start,
        period_end,
        period_source,
        period_status,
        period_confidence,
        period_required,
        period_mode,
    ) = row
    return _to_read(
        intake,
        cp_name,
        requisites_verified=verified,
        invoice_payment_status=pay_status,
        invoice_draft_id=draft_id,
        invoice_dds_article_id=inv_article,
        default_dds_article_id=default_article,
        invoice_service_period_start=period_start,
        invoice_service_period_end=period_end,
        invoice_service_period_source=period_source,
        invoice_service_period_status=period_status,
        invoice_service_period_confidence=period_confidence,
        service_period_required=period_required,
        service_period_mode=period_mode,
    )


@router.get("/intakes", response_model=list[IntakeRead], dependencies=READ)
async def list_intakes(
    session: Annotated[AsyncSession, Depends(get_session)],
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[IntakeRead]:
    stmt = (
        select(
            EmailInvoiceIntake,
            Counterparty.name,
            CounterpartyPayableProfile.requisites_verified,
            SupplierInvoice.payment_status,
            SupplierInvoice.draft_id,
            SupplierInvoice.dds_article_id,
            CounterpartyPayableProfile.default_dds_article_id,
            SupplierInvoice.service_period_start,
            SupplierInvoice.service_period_end,
            SupplierInvoice.service_period_source,
            SupplierInvoice.service_period_status,
            SupplierInvoice.service_period_confidence,
            CounterpartyPayableProfile.service_period_required,
            CounterpartyPayableProfile.service_period_mode,
        )
        .outerjoin(Counterparty, Counterparty.id == EmailInvoiceIntake.counterparty_id)
        .outerjoin(
            CounterpartyPayableProfile,
            CounterpartyPayableProfile.counterparty_id == EmailInvoiceIntake.counterparty_id,
        )
        .outerjoin(SupplierInvoice, SupplierInvoice.id == EmailInvoiceIntake.invoice_id)
        .order_by(EmailInvoiceIntake.created_at.desc())
        .limit(limit)
    )
    if status_filter:
        if status_filter not in EMAIL_INTAKE_STATUSES:
            raise HTTPException(status_code=400, detail="Неизвестный статус")
        stmt = stmt.where(EmailInvoiceIntake.status == status_filter)
    rows = (await session.execute(stmt)).all()
    return [
        _to_read(
            intake,
            cp_name,
            requisites_verified=verified,
            invoice_payment_status=pay_status,
            invoice_draft_id=draft_id,
            invoice_dds_article_id=inv_article,
            default_dds_article_id=default_article,
            invoice_service_period_start=period_start,
            invoice_service_period_end=period_end,
            invoice_service_period_source=period_source,
            invoice_service_period_status=period_status,
            invoice_service_period_confidence=period_confidence,
            service_period_required=period_required,
            service_period_mode=period_mode,
        )
        for (
            intake,
            cp_name,
            verified,
            pay_status,
            draft_id,
            inv_article,
            default_article,
            period_start,
            period_end,
            period_source,
            period_status,
            period_confidence,
            period_required,
            period_mode,
        ) in rows
    ]


@router.get("/intakes/{intake_id}/pdf", dependencies=READ)
async def get_intake_pdf(
    intake_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    intake = await _get_intake(session, intake_id)
    if not intake.pdf_bytes:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PDF недоступен")
    # Имя файла кириллическое, а HTTP-заголовки только latin-1 → ASCII-фолбэк + RFC 5987
    # (filename*) с percent-encoding, иначе Starlette роняет ответ («Network Error» в браузере).
    raw_name = intake.attachment_filename or "invoice.pdf"
    ascii_name = raw_name.encode("ascii", "ignore").decode("ascii") or "invoice.pdf"
    disposition = f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(raw_name)}"
    return Response(
        content=bytes(intake.pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": disposition},
    )


@router.get("/intakes/{intake_id}", response_model=IntakeRead, dependencies=READ)
async def get_intake(
    intake_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> IntakeRead:
    return await _load_read(session, intake_id)


@router.post("/intakes/{intake_id}/confirm", response_model=IntakeRead, dependencies=OPERATE)
async def confirm_intake(
    intake_id: uuid.UUID,
    body: ConfirmIn,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> IntakeRead:
    intake = await _get_intake(session, intake_id)
    if intake.status == "ignored":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Запись помечена как не счёт"
        )
    try:
        await ingest.confirm_intake_with_review(
            session,
            intake,
            actor_user_id=actor.user_id,
            counterparty_id=body.counterparty_id,
            new_counterparty_name=body.new_counterparty_name,
            new_counterparty_inn=body.new_counterparty_inn,
            amount=body.amount,
            invoice_number=body.invoice_number,
            invoice_date=body.invoice_date,
            service_period_start=body.service_period_start,
            service_period_end=body.service_period_end,
            requisites=body.requisites.model_dump(exclude_none=True) if body.requisites else None,
            apply_requisites=body.apply_requisites,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    return await _load_read(session, intake_id)


@router.post("/intakes/{intake_id}/ignore", response_model=IntakeRead, dependencies=OPERATE)
async def ignore_intake(
    intake_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> IntakeRead:
    intake = await _get_intake(session, intake_id)
    if intake.status == "linked":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="По записи уже создана накладная — игнор недоступен",
        )
    intake.status = "ignored"
    await session.commit()
    await session.refresh(intake)
    return _to_read(intake, None)


@router.post("/intakes/{intake_id}/send-to-bank", response_model=IntakeRead, dependencies=OPERATE)
async def send_to_bank(
    intake_id: uuid.UUID,
    body: SendToBankIn,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> IntakeRead:
    """Отправить подтверждённый счёт в банк — банк-черновик (как у накладных). Деньги не
    списываются: уходит на подтверждение. В dev (mock-режим) реального вызова банка нет."""
    intake = await _get_intake(session, intake_id)
    await _apply_article_choice(
        session,
        intake,
        dds_article_id=body.dds_article_id,
        remember_for_counterparty=body.remember_for_counterparty,
    )
    try:
        await ingest.send_intake_to_bank(session, intake, actor_user_id=actor.user_id)
    except payments.RequisitesNotVerifiedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Реквизиты контрагента не подтверждены — откройте «Разобрать» и подтвердите их",
        ) from exc
    except payments.CounterpartyPaymentError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (BankFetchError, BankCredentialsError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Банк недоступен, попробуйте позже"
        ) from exc
    return await _load_read(session, intake_id)


@router.post("/intakes/{intake_id}/schedule-send", response_model=IntakeRead, dependencies=OPERATE)
async def schedule_send(
    intake_id: uuid.UUID,
    body: ScheduleSendIn,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> IntakeRead:
    """Запланировать авто-отправку счёта в банк к заданной дате (джоба отправит, когда дата
    наступит). Те же условия, что и при ручной отправке: счёт подтверждён, реквизиты проверены,
    ещё не в банке."""
    intake = await _get_intake(session, intake_id)
    if intake.status != "linked" or intake.invoice_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Счёт ещё не подтверждён")
    invoice = await session.get(SupplierInvoice, intake.invoice_id)
    if invoice is None or invoice.draft_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Счёт уже отправлен в банк"
        )
    profile = await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == invoice.counterparty_id
        )
    )
    if profile and profile.service_period_required and invoice.service_period_status != "ready":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Для этого контрагента обязателен период оказания услуги",
        )
    verified = await session.scalar(
        select(CounterpartyPayableProfile.requisites_verified).where(
            CounterpartyPayableProfile.counterparty_id == intake.counterparty_id
        )
    )
    if not verified:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Реквизиты контрагента не подтверждены — откройте «Разобрать» и подтвердите их",
        )
    await _apply_article_choice(
        session,
        intake,
        dds_article_id=body.dds_article_id,
        remember_for_counterparty=body.remember_for_counterparty,
    )
    intake.scheduled_send_date = body.send_date
    await session.commit()
    return await _load_read(session, intake_id)


@router.post(
    "/intakes/{intake_id}/cancel-schedule", response_model=IntakeRead, dependencies=OPERATE
)
async def cancel_schedule(
    intake_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> IntakeRead:
    """Отменить плановую авто-отправку (счёт остаётся готовым к ручной отправке)."""
    intake = await _get_intake(session, intake_id)
    intake.scheduled_send_date = None
    await session.commit()
    return await _load_read(session, intake_id)


@router.post("/intakes/{intake_id}/exclude", response_model=IntakeRead, dependencies=OPERATE)
async def exclude_intake(
    intake_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> IntakeRead:
    """Исключить счёт из рабочего инбокса в корзину «Исключённые» (ручное «не платим этот»)."""
    intake = await _get_intake(session, intake_id)
    try:
        await ingest.exclude_intake(session, intake)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    return await _load_read(session, intake_id)


@router.post("/intakes/{intake_id}/restore", response_model=IntakeRead, dependencies=OPERATE)
async def restore_intake(
    intake_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> IntakeRead:
    """Вернуть счёт из «Исключённых» обратно в рабочий инбокс."""
    intake = await _get_intake(session, intake_id)
    try:
        await ingest.restore_intake(session, intake)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    return await _load_read(session, intake_id)


@router.delete("/intakes/{intake_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=OPERATE)
async def delete_intake(
    intake_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Удалить исключённый счёт НАВСЕГДА (вместе с накладной, если она не в банке/оплате)."""
    intake = await _get_intake(session, intake_id)
    try:
        await ingest.delete_intake_forever(session, intake)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

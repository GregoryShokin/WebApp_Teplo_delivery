"""API экрана «Страница на оплату» (Фаза 2, MVP).

Журнал разбора счетов из почты (``email_invoice_intake``): список со статусами, превью
исходного PDF, подтверждение оператором (материализация накладной) и игнор. Редактирование
полей и применение реквизитов в профиль — следующий шаг. Права: чтение —
``counterparties.read``, операции — ``counterparties.operate`` (страница в контуре контрагентов).
"""

from __future__ import annotations

import uuid
from datetime import datetime
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
    # Распознанные банковские реквизиты (recipientName/inn/kpp/bankAcnt/bankBik/corr) — для
    # предзаполнения окна разбора.
    requisites: dict[str, Any]
    # Подтверждены ли реквизиты контрагента (нужно для отправки в банк).
    requisites_verified: bool
    # Состояние связанной накладной: оплачена/частично и заведён ли банк-черновик.
    invoice_payment_status: str | None
    invoice_in_draft: bool
    has_pdf: bool
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
    requisites: ReviewRequisites | None = None
    # Перенести реквизиты в карточку контрагента и пометить проверенными.
    apply_requisites: bool = False


def _to_read(
    intake: EmailInvoiceIntake,
    counterparty_name: str | None,
    *,
    requisites_verified: bool | None = False,
    invoice_payment_status: str | None = None,
    invoice_draft_id: uuid.UUID | None = None,
) -> IntakeRead:
    rec: dict[str, Any] = intake.recognition or {}
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
        requisites=rec.get("requisites") or {},
        requisites_verified=bool(requisites_verified),
        invoice_payment_status=invoice_payment_status,
        invoice_in_draft=invoice_draft_id is not None,
        has_pdf=intake.pdf_bytes is not None,
        created_at=intake.created_at,
    )


async def _get_intake(session: AsyncSession, intake_id: uuid.UUID) -> EmailInvoiceIntake:
    intake = await session.get(EmailInvoiceIntake, intake_id)
    if intake is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Запись не найдена")
    return intake


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
        )
        for intake, cp_name, verified, pay_status, draft_id in rows
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


@router.post("/intakes/{intake_id}/confirm", response_model=IntakeRead, dependencies=OPERATE)
async def confirm_intake(
    intake_id: uuid.UUID,
    body: ConfirmIn,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> IntakeRead:
    intake = await _get_intake(session, intake_id)
    if intake.status == "linked":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Счёт уже подтверждён")
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
            requisites=body.requisites.model_dump(exclude_none=True) if body.requisites else None,
            apply_requisites=body.apply_requisites,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.commit()
    await session.refresh(intake)
    name = (
        await session.scalar(
            select(Counterparty.name).where(Counterparty.id == intake.counterparty_id)
        )
        if intake.counterparty_id
        else None
    )
    return _to_read(intake, name)


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
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> IntakeRead:
    """Отправить подтверждённый счёт в банк — переиспользует рабочий контур черновика
    (``create_payment_draft_for_invoices``). Деньги не списываются: банк-черновик уходит на
    подтверждение, как у накладных. В dev (mock-режим) реального вызова банка нет."""
    intake = await _get_intake(session, intake_id)
    if intake.status != "linked" or intake.invoice_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Счёт ещё не подтверждён")
    invoice = await session.get(SupplierInvoice, intake.invoice_id)
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Накладная не найдена")
    if invoice.draft_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Счёт уже отправлен в банк"
        )
    try:
        await payments.create_payment_draft_for_invoices(
            session, invoice_ids=[intake.invoice_id], actor_user_id=actor.user_id
        )
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

    # Сервис уже закоммитил; перечитываем для актуального ответа.
    await session.refresh(intake)
    invoice = await session.get(SupplierInvoice, intake.invoice_id)
    name = (
        await session.scalar(
            select(Counterparty.name).where(Counterparty.id == intake.counterparty_id)
        )
        if intake.counterparty_id
        else None
    )
    return _to_read(
        intake,
        name,
        requisites_verified=True,
        invoice_payment_status=invoice.payment_status if invoice else None,
        invoice_draft_id=invoice.draft_id if invoice else None,
    )

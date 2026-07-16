"""API зеркала СБИС (Saby) ЭДО — сверка входящих документов с iiko-накладными.

Список документов с фильтром по статусу сверки, принудительный синк, PDF-прокси
печатной формы (ссылки СБИС требуют токен — фронту его не выдаём), ручная связка
и скрытие из сверки. Права: чтение — counterparties.read, операции —
counterparties.operate (контур накладных).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.api.deps import require_permission
from app.db.session import get_session
from app.models import (
    Counterparty,
    CounterpartyCollectionSource,
    SbisDocument,
    SupplierInvoice,
)
from app.services import counterparty_registry as registry
from app.services.sbis.client import SbisApiError, SbisClient, SbisCredentialsError
from app.services.sbis.sync import sync_sbis_documents

router = APIRouter()

READ = (Depends(require_permission("counterparties.read")),)
OPERATE = (Depends(require_permission("counterparties.operate")),)


class MatchedInvoiceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    number: str | None
    invoice_date: date | None
    amount: Decimal
    source: str
    payment_status: str


class SbisDocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    doc_type: str | None
    regulation: str | None
    title: str | None
    number: str | None
    doc_date: date | None
    amount: Decimal | None
    amount_wo_vat: Decimal | None
    counterparty_name: str | None
    counterparty_inn: str | None
    state_name: str | None
    attachment_kind: str | None
    link_cabinet: str | None
    has_pdf: bool
    match_status: str
    match_note: str | None
    matched_invoice: MatchedInvoiceRead | None
    last_synced_at: datetime
    # Маршрутизация: контрагент по ИНН, итог (mirror/new_counterparty/materialized/duplicate)
    # и созданный/найденный счёт. channel_enabled/has_email_channel питают сигнал
    # «поставщик на почте появился в ЭДО — переключить».
    intake_status: str
    counterparty_id: uuid.UUID | None
    counterparty_status: str | None
    channel_enabled: bool
    has_email_channel: bool
    invoice: MatchedInvoiceRead | None


class SyncResultRead(BaseModel):
    configured: bool
    fetched: int
    created: int
    updated: int
    matched: int
    skipped_deleted: int
    new_counterparties: int
    materialized: int
    duplicates: int
    settled_from_prepayments: int


class MatchIn(BaseModel):
    invoice_id: uuid.UUID


def _to_read(
    doc: SbisDocument,
    matched: SupplierInvoice | None,
    invoice: SupplierInvoice | None = None,
    counterparty: Counterparty | None = None,
    channel_kinds: set[str] | None = None,
) -> SbisDocumentRead:
    kinds = channel_kinds or set()
    return SbisDocumentRead(
        id=doc.id,
        doc_type=doc.doc_type,
        regulation=doc.regulation,
        title=doc.title,
        number=doc.number,
        doc_date=doc.doc_date,
        amount=doc.amount,
        amount_wo_vat=doc.amount_wo_vat,
        counterparty_name=doc.counterparty_name,
        counterparty_inn=doc.counterparty_inn,
        state_name=doc.state_name,
        attachment_kind=doc.attachment_kind,
        link_cabinet=doc.link_cabinet,
        has_pdf=bool(doc.link_pdf),
        match_status=doc.match_status,
        match_note=doc.match_note,
        matched_invoice=MatchedInvoiceRead.model_validate(matched) if matched else None,
        last_synced_at=doc.last_synced_at,
        intake_status=doc.intake_status,
        counterparty_id=doc.counterparty_id,
        counterparty_status=counterparty.status if counterparty else None,
        channel_enabled="sbis" in kinds,
        has_email_channel="email" in kinds,
        invoice=MatchedInvoiceRead.model_validate(invoice) if invoice else None,
    )


async def _load_doc(session: AsyncSession, doc_id: uuid.UUID) -> SbisDocument:
    doc = await session.get(SbisDocument, doc_id)
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Документ не найден")
    return doc


@router.get("/documents", response_model=list[SbisDocumentRead], dependencies=READ)
async def list_documents(
    session: Annotated[AsyncSession, Depends(get_session)],
    match_status: Literal["unmatched", "matched", "dismissed"] | None = Query(default=None),
    intake_status: Literal["mirror", "new_counterparty", "materialized", "duplicate"]
    | None = Query(default=None),
) -> list[SbisDocumentRead]:
    matched_invoice = aliased(SupplierInvoice)
    routed_invoice = aliased(SupplierInvoice)
    query = (
        select(SbisDocument, matched_invoice, routed_invoice, Counterparty)
        .outerjoin(matched_invoice, matched_invoice.id == SbisDocument.matched_invoice_id)
        .outerjoin(routed_invoice, routed_invoice.id == SbisDocument.invoice_id)
        .outerjoin(Counterparty, Counterparty.id == SbisDocument.counterparty_id)
        .order_by(SbisDocument.doc_date.desc().nulls_last(), SbisDocument.first_seen_at.desc())
    )
    if match_status is not None:
        query = query.where(SbisDocument.match_status == match_status)
    if intake_status is not None:
        query = query.where(SbisDocument.intake_status == intake_status)
    rows = (await session.execute(query)).all()

    # Каналы сбора вовлечённых контрагентов одним запросом (сигнал «переключить на СБИС»).
    counterparty_ids = {doc.counterparty_id for doc, *_ in rows if doc.counterparty_id}
    channels: dict[uuid.UUID, set[str]] = {}
    if counterparty_ids:
        source_rows = (
            await session.execute(
                select(
                    CounterpartyCollectionSource.counterparty_id,
                    CounterpartyCollectionSource.kind,
                ).where(CounterpartyCollectionSource.counterparty_id.in_(counterparty_ids))
            )
        ).all()
        for cp_id, kind in source_rows:
            channels.setdefault(cp_id, set()).add(kind)

    return [
        _to_read(doc, matched, invoice, counterparty, channels.get(doc.counterparty_id))
        for doc, matched, invoice, counterparty in rows
    ]


@router.post(
    "/documents/{doc_id}/enable-channel",
    response_model=SbisDocumentRead,
    dependencies=OPERATE,
)
async def enable_channel(
    doc_id: uuid.UUID, session: Annotated[AsyncSession, Depends(get_session)]
) -> SbisDocumentRead:
    """Включить контрагенту документа канал сбора 'sbis' (кнопка «Подключить СБИС»).

    Материализация накопленных документов произойдёт следующим проходом синка
    (или по кнопке «Обновить из СБИС»)."""
    doc = await _load_doc(session, doc_id)
    if doc.counterparty_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="У документа не определён контрагент — сначала синк/настройка карточки",
        )
    counterparty = await session.get(Counterparty, doc.counterparty_id)
    if counterparty is not None and counterparty.status == "requires_setup":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Сначала настройте карточку контрагента (реестр → «требует настройки»)",
        )
    existing = await session.scalar(
        select(CounterpartyCollectionSource).where(
            CounterpartyCollectionSource.counterparty_id == doc.counterparty_id,
            CounterpartyCollectionSource.kind == "sbis",
        )
    )
    if existing is None:
        try:
            await registry.add_collection_source(
                session,
                counterparty_id=doc.counterparty_id,
                kind="sbis",
                value=doc.counterparty_inn,
                note="подключено из вкладки СБИС",
            )
        except registry.CounterpartyRegistryError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            ) from exc
    await session.commit()
    counterparty = await session.get(Counterparty, doc.counterparty_id)
    return _to_read(doc, None, None, counterparty, {"sbis"})


@router.post("/sync", response_model=SyncResultRead, dependencies=OPERATE)
async def force_sync(session: Annotated[AsyncSession, Depends(get_session)]) -> SyncResultRead:
    try:
        result = await sync_sbis_documents(session, run_reason="manual")
    except SbisCredentialsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except SbisApiError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    if not result.configured:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="СБИС не настроен: заполните SBIS_APP_CLIENT_ID и SBIS_SECRET_KEY",
        )
    return SyncResultRead(**{k: v for k, v in result.as_dict().items() if k != "errors"})


@router.get("/documents/{doc_id}/pdf", dependencies=READ)
async def document_pdf(
    doc_id: uuid.UUID, session: Annotated[AsyncSession, Depends(get_session)]
) -> Response:
    doc = await _load_doc(session, doc_id)
    if not doc.link_pdf:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="У документа нет печатной формы"
        )
    client = SbisClient()
    try:
        content = await client.download_pdf(doc.link_pdf)
    except SbisCredentialsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except SbisApiError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    # ASCII-фолбэк обязателен: кириллица в Content-Disposition роняет Starlette (latin-1).
    filename = (
        f"sbis-{doc.number or doc.sbis_doc_id}.pdf".replace('"', "")
        .encode("ascii", "ignore")
        .decode()
        or "sbis-document.pdf"
    )
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.post("/documents/{doc_id}/match", response_model=SbisDocumentRead, dependencies=OPERATE)
async def match_document(
    doc_id: uuid.UUID, payload: MatchIn, session: Annotated[AsyncSession, Depends(get_session)]
) -> SbisDocumentRead:
    doc = await _load_doc(session, doc_id)
    invoice = await session.get(SupplierInvoice, payload.invoice_id)
    if invoice is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Накладная не найдена")
    doc.match_status = "matched"
    doc.matched_invoice_id = invoice.id
    doc.match_note = "manual"
    await session.commit()
    return _to_read(doc, invoice)


@router.post("/documents/{doc_id}/dismiss", response_model=SbisDocumentRead, dependencies=OPERATE)
async def dismiss_document(
    doc_id: uuid.UUID, session: Annotated[AsyncSession, Depends(get_session)]
) -> SbisDocumentRead:
    doc = await _load_doc(session, doc_id)
    doc.match_status = "dismissed"
    doc.matched_invoice_id = None
    doc.match_note = None
    await session.commit()
    return _to_read(doc, None)


@router.post("/documents/{doc_id}/restore", response_model=SbisDocumentRead, dependencies=OPERATE)
async def restore_document(
    doc_id: uuid.UUID, session: Annotated[AsyncSession, Depends(get_session)]
) -> SbisDocumentRead:
    doc = await _load_doc(session, doc_id)
    doc.match_status = "unmatched"
    doc.matched_invoice_id = None
    doc.match_note = None
    await session.commit()
    return _to_read(doc, None)

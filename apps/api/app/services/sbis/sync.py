"""Синк зеркала СБИС ЭДО и сверка с iiko-накладными.

Тянет реестр «Входящие» за окно ``sbis_sync_lookback_days``, апсертит ``sbis_document``
по идентификатору документа СБИС (повторный проход бесплатен) и матчит документы
с накладными ``supplier_invoice`` (source iiko/manual, payable):

1. ``number_amount`` — номер документа поставщика (нормализованный) + точная сумма;
2. ``amount_date`` — точная сумма + окно дат ±5 дней, только если кандидат ЕДИНСТВЕННЫЙ
   (коллизию сумм не угадываем — оставляем несматченным, оператор свяжет руками).

Уже сматченные строки повторно не трогаем; ссылки на файлы (живут ~месяц) освежаются
каждым проходом.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import SbisDocument, SupplierInvoice
from app.services.sbis.client import SbisClient

logger = logging.getLogger(__name__)

# Матчим только документы отгрузки (УПД/накладные) — у актов сверки нет суммы и пары в iiko.
_MATCHABLE_DOC_TYPES = {"ДокОтгрВх"}
_DATE_WINDOW_DAYS = 5


@dataclass
class SbisSyncResult:
    configured: bool = True
    fetched: int = 0
    created: int = 0
    updated: int = 0
    matched: int = 0
    skipped_deleted: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "fetched": self.fetched,
            "created": self.created,
            "updated": self.updated,
            "matched": self.matched,
            "skipped_deleted": self.skipped_deleted,
            "errors": self.errors,
        }


def _parse_amount(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    raw = str(value).strip().split(" ")[0]
    try:
        day, month, year = raw.split(".")
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def normalize_number(value: str | None) -> str | None:
    """Номер документа для сравнения: без пробелов, регистронезависимо."""
    if not value:
        return None
    normalized = "".join(str(value).split()).casefold()
    return normalized or None


def _counterparty_fields(doc: dict[str, Any]) -> tuple[str | None, str | None]:
    party = doc.get("Контрагент") or {}
    legal = party.get("СвЮЛ") or {}
    person = party.get("СвФЛ") or {}
    name = (
        legal.get("Название")
        or person.get("НазваниеПолное")
        or " ".join(
            part
            for part in (person.get("Фамилия"), person.get("Имя"), person.get("Отчество"))
            if part
        )
        or None
    )
    inn = legal.get("ИНН") or person.get("ИНН") or None
    return name, inn


def _main_attachment(doc: dict[str, Any]) -> dict[str, Any] | None:
    for attachment in doc.get("Вложение") or []:
        if attachment.get("Служебный") != "Да":
            return attachment
    return None


def _apply_registry_item(entry: SbisDocument, doc: dict[str, Any], raw: dict[str, Any]) -> None:
    attachment = _main_attachment(doc) or {}
    name, inn = _counterparty_fields(doc)
    entry.doc_type = doc.get("Тип")
    entry.regulation = (doc.get("Регламент") or {}).get("Название")
    entry.title = doc.get("Название")
    entry.number = doc.get("Номер") or attachment.get("Номер")
    entry.doc_date = _parse_date(doc.get("Дата"))
    entry.amount = _parse_amount(doc.get("Сумма") or attachment.get("Сумма"))
    entry.amount_wo_vat = _parse_amount(attachment.get("СуммаБезНДС"))
    entry.counterparty_name = name
    entry.counterparty_inn = inn
    state = doc.get("Состояние") or {}
    entry.state_code = str(state.get("Код")) if state.get("Код") is not None else None
    entry.state_name = state.get("Название")
    entry.attachment_kind = attachment.get("Тип")
    entry.link_cabinet = doc.get("СсылкаДляНашаОрганизация") or attachment.get("СсылкаВКабинет")
    entry.link_pdf = attachment.get("СсылкаНаPDF") or doc.get("СсылкаНаPDF")
    entry.link_xml = (attachment.get("Файл") or {}).get("Ссылка")
    entry.raw = raw


async def _upsert_documents(
    session: AsyncSession, items: list[dict[str, Any]], result: SbisSyncResult
) -> None:
    doc_ids = []
    for item in items:
        doc_id = (item.get("Документ") or {}).get("Идентификатор")
        if doc_id:
            doc_ids.append(doc_id)
    existing_rows = (
        (
            await session.execute(
                select(SbisDocument).where(SbisDocument.sbis_doc_id.in_(doc_ids))
            )
        ).scalars()
        if doc_ids
        else []
    )
    by_doc_id = {row.sbis_doc_id: row for row in existing_rows}

    seen: set[str] = set()
    for item in items:
        doc = item.get("Документ") or {}
        doc_id = doc.get("Идентификатор")
        if not doc_id or doc_id in seen:
            continue
        seen.add(doc_id)
        if doc.get("Удален") == "Да":
            result.skipped_deleted += 1
            continue
        entry = by_doc_id.get(doc_id)
        if entry is None:
            entry = SbisDocument(sbis_doc_id=doc_id)
            session.add(entry)
            result.created += 1
        else:
            result.updated += 1
        _apply_registry_item(entry, doc, item)
        entry.last_synced_at = func.now()


async def _match_documents(session: AsyncSession, result: SbisSyncResult) -> None:
    unmatched = (
        (
            await session.execute(
                select(SbisDocument).where(
                    SbisDocument.match_status == "unmatched",
                    SbisDocument.amount.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    candidates = [d for d in unmatched if (d.doc_type or "ДокОтгрВх") in _MATCHABLE_DOC_TYPES]
    if not candidates:
        return

    min_date = min((d.doc_date for d in candidates if d.doc_date), default=None)
    invoice_filters = [
        SupplierInvoice.direction == "payable",
        SupplierInvoice.source.in_(("iiko", "manual")),
    ]
    if min_date is not None:
        invoice_filters.append(
            SupplierInvoice.invoice_date >= min_date - timedelta(days=_DATE_WINDOW_DAYS)
        )
    invoices = (
        (await session.execute(select(SupplierInvoice).where(*invoice_filters))).scalars().all()
    )

    by_number_amount: dict[tuple[str, Decimal], list[SupplierInvoice]] = {}
    by_amount: dict[Decimal, list[SupplierInvoice]] = {}
    for invoice in invoices:
        number = normalize_number(invoice.number)
        if number:
            by_number_amount.setdefault((number, invoice.amount), []).append(invoice)
        by_amount.setdefault(invoice.amount, []).append(invoice)

    for doc in candidates:
        matched: SupplierInvoice | None = None
        note: str | None = None
        number = normalize_number(doc.number)
        if number and doc.amount is not None:
            exact = by_number_amount.get((number, doc.amount)) or []
            if len(exact) == 1:
                matched, note = exact[0], "number_amount"
        if matched is None and doc.amount is not None and doc.doc_date is not None:
            near = [
                invoice
                for invoice in by_amount.get(doc.amount, [])
                if invoice.invoice_date is not None
                and abs((invoice.invoice_date - doc.doc_date).days) <= _DATE_WINDOW_DAYS
            ]
            if len(near) == 1:
                matched, note = near[0], "amount_date"
        if matched is not None:
            doc.match_status = "matched"
            doc.matched_invoice_id = matched.id
            doc.match_note = note
            result.matched += 1


async def sync_sbis_documents(
    session: AsyncSession, *, days: int | None = None, run_reason: str = "cron"
) -> SbisSyncResult:
    """Полный проход: тянем реестр, апсертим зеркало, сверяем с накладными."""
    settings = get_settings()
    result = SbisSyncResult()
    client = SbisClient(settings)
    if not client.configured:
        result.configured = False
        logger.info("sbis sync skipped: credentials are not configured")
        return result

    lookback = days or settings.sbis_sync_lookback_days
    date_from = date.today() - timedelta(days=lookback)
    items = await client.list_incoming_documents(date_from)
    result.fetched = len(items)

    await _upsert_documents(session, items, result)
    await session.flush()
    await _match_documents(session, result)
    await session.commit()
    logger.info("sbis sync (%s): %s", run_reason, result.as_dict())
    return result

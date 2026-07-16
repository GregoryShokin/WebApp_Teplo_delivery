"""Синк СБИС ЭДО: зеркало + маршрутизация в счета для сервисных поставщиков.

Тянет реестр «Входящие» за окно ``sbis_sync_lookback_days``, апсертит ``sbis_document``
по идентификатору документа СБИС (повторный проход бесплатен), затем МАРШРУТИЗИРУЕТ
каждый документ — режим определяет карточка контрагента, не документ:

- ИНН не найден → placeholder-контрагент ``requires_setup`` (очередь needs-setup)
  и статус ``new_counterparty``: документы копятся, но не материализуются, пока
  оператор не настроит карточку и не включит канал;
- контрагент с каналом сбора 'sbis' и документ отгрузки (счёт/УПД/акт работ) →
  материализация в ``SupplierInvoice(source='sbis')`` с дедупом против почты и
  ручного ввода (ИНН + сумма + дата + номер) и распознаванием периода услуги
  из названий документа (regex-слой «Страницы на оплату»);
- остальное → зеркало со сверкой против iiko-накладных:
  1. ``number_amount`` — номер поставщика (нормализованный) + точная сумма;
  2. ``amount_date`` — сумма + окно ±5 дней, только если кандидат ЕДИНСТВЕННЫЙ.

Уже сматченные/материализованные строки повторно не трогаем; ссылки на файлы
(живут ~месяц) освежаются каждым проходом.
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
from app.models import (
    Counterparty,
    CounterpartyCollectionSource,
    CounterpartyPayableProfile,
    CounterpartyRole,
    SbisDocument,
    SupplierInvoice,
)
from app.services import supplier_prepayments as prepayments
from app.services import supplier_service_periods as service_periods
from app.services.counterparty_registry import compute_invoice_due_date
from app.services.invoice_recognition import _extract_service_periods
from app.services.sbis.client import SbisClient

logger = logging.getLogger(__name__)

# Материализуем только документы отгрузки (счета/УПД/акты выполненных работ). Акты
# сверки, договоры и корреспонденция учёт не двигают — остаются в зеркале. Этот же
# набор участвует в зеркальном матчинге с iiko.
_MATCHABLE_DOC_TYPES = {"ДокОтгрВх"}
_MATERIALIZABLE_DOC_TYPES = {"ДокОтгрВх", "СчетВх"}
_DATE_WINDOW_DAYS = 5


@dataclass
class SbisSyncResult:
    configured: bool = True
    fetched: int = 0
    created: int = 0
    updated: int = 0
    matched: int = 0
    skipped_deleted: int = 0
    new_counterparties: int = 0
    materialized: int = 0
    duplicates: int = 0
    settled_from_prepayments: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "fetched": self.fetched,
            "created": self.created,
            "updated": self.updated,
            "matched": self.matched,
            "skipped_deleted": self.skipped_deleted,
            "new_counterparties": self.new_counterparties,
            "materialized": self.materialized,
            "duplicates": self.duplicates,
            "settled_from_prepayments": self.settled_from_prepayments,
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


def _guess_type(inn: str | None) -> str:
    # 12 цифр — ИП (individual), 10 — юрлицо (legal_entity). Как в email/iiko-синках.
    return "individual" if inn and len(inn) == 12 else "legal_entity"


async def _resolve_or_create_counterparty(
    session: AsyncSession,
    doc: SbisDocument,
    cache: dict[str, Counterparty],
    result: SbisSyncResult,
) -> Counterparty | None:
    """Контрагент по ИНН; неизвестный ИНН → placeholder ``requires_setup``.

    Placeholder попадает в существующую очередь needs-setup («новый поставщик —
    заполни карточку»); его документы копятся со статусом ``new_counterparty``
    и материализуются задним числом после настройки и включения канала."""
    inn = doc.counterparty_inn
    if not inn:
        return None
    cached = cache.get(inn)
    if cached is not None:
        return cached
    counterparty = await session.scalar(select(Counterparty).where(Counterparty.inn == inn))
    if counterparty is None:
        counterparty = Counterparty(
            name=doc.counterparty_name or f"ИНН {inn}",
            inn=inn,
            type=_guess_type(inn),
            status="requires_setup",
            origin="sbis",
        )
        session.add(counterparty)
        await session.flush()
        session.add(CounterpartyRole(counterparty_id=counterparty.id, role="supplier"))
        session.add(
            CounterpartyPayableProfile(
                counterparty_id=counterparty.id, internal_name=doc.counterparty_name
            )
        )
        await session.flush()
        result.new_counterparties += 1
    cache[inn] = counterparty
    return counterparty


async def _find_existing_invoice(
    session: AsyncSession, counterparty_id, doc: SbisDocument
) -> SupplierInvoice | None:
    """Двусторонний дедуп с почтой/ручным вводом: сумма + дата + номер (None==None).

    Поставщик может прислать тот же счёт и письмом, и через ЭДО — второй канал не
    должен родить второй счёт. Зеркальный iiko-контур сюда не входит: у контрагентов
    с каналом 'sbis' производственных iiko-накладных нет по определению."""
    if doc.amount is None:
        return None
    candidates = (
        await session.scalars(
            select(SupplierInvoice).where(
                SupplierInvoice.counterparty_id == counterparty_id,
                SupplierInvoice.amount == doc.amount,
                SupplierInvoice.payment_status != "void",
                SupplierInvoice.source.in_(("email", "manual", "sbis")),
            )
        )
    ).all()
    want_number = normalize_number(doc.number)
    for candidate in candidates:
        if (
            candidate.invoice_date == doc.doc_date
            and normalize_number(candidate.number) == want_number
        ):
            return candidate
    return None


def _service_period_fields(doc: SbisDocument, *, required: bool) -> dict[str, Any]:
    """Период услуги из текстов СБИС-документа (название документа + названия вложений)
    существующим regex-слоем «Страницы на оплату». Несколько периодов = ambiguous —
    даты не выбираем, счёт уходит на ручной разбор (блок в карточке накладной)."""
    texts = [doc.title or ""]
    raw_doc = (doc.raw or {}).get("Документ") or {}
    for attachment in raw_doc.get("Вложение") or []:
        texts.append(str(attachment.get("Название") or ""))
    combined = " \n".join(text for text in texts if text)
    candidates = _extract_service_periods(combined) if combined else []
    if len(candidates) == 1:
        start, end, source, confidence = candidates[0]
        return {
            "service_period_start": start,
            "service_period_end": end,
            "service_period_status": "ready",
            "service_period_source": f"sbis_{source}",
            "service_period_confidence": confidence,
        }
    if len(candidates) > 1:
        return {"service_period_status": "ambiguous"}
    return {"service_period_status": "missing" if required else "not_required"}


async def _materialize_document(
    session: AsyncSession, doc: SbisDocument, counterparty: Counterparty, result: SbisSyncResult
) -> None:
    # Идемпотентность на случай дрейфа: счёт из этого же СБИС-документа уже есть.
    existing = await session.scalar(
        select(SupplierInvoice).where(
            SupplierInvoice.source == "sbis",
            SupplierInvoice.external_id == doc.sbis_doc_id,
        )
    )
    if existing is not None:
        doc.invoice_id = existing.id
        doc.intake_status = "materialized"
        return

    duplicate = await _find_existing_invoice(session, counterparty.id, doc)
    if duplicate is not None:
        doc.invoice_id = duplicate.id
        doc.intake_status = "duplicate"
        result.duplicates += 1
        return

    profile = await session.scalar(
        select(CounterpartyPayableProfile).where(
            CounterpartyPayableProfile.counterparty_id == counterparty.id
        )
    )
    period = _service_period_fields(
        doc, required=bool(profile and profile.service_period_required)
    )
    vat_total = Decimal("0")
    if (
        doc.amount is not None
        and doc.amount_wo_vat is not None
        and doc.amount > doc.amount_wo_vat
    ):
        vat_total = doc.amount - doc.amount_wo_vat

    invoice = SupplierInvoice(
        counterparty_id=counterparty.id,
        source="sbis",
        direction="payable",
        external_id=doc.sbis_doc_id,
        number=doc.number,
        invoice_date=doc.doc_date,
        due_date=compute_invoice_due_date(
            doc.doc_date,
            delay_days=profile.payment_delay_days if profile else None,
            due_day_of_month=profile.payment_due_day_of_month if profile else None,
        ),
        amount=doc.amount,
        vat_total=vat_total,
        payment_status="unpaid",
        note=doc.title,
        raw_payload={
            "sbis_doc_id": doc.sbis_doc_id,
            "doc_type": doc.doc_type,
            "attachment_kind": doc.attachment_kind,
            "regulation": doc.regulation,
            "state": doc.state_name,
        },
        **period,
    )
    session.add(invoice)
    await session.flush()
    await service_periods.sync_invoice_accrual(session, invoice)
    # «Закрывающий документ»: если поставщик оплачен авансом, УПД гасит дебиторку
    # и не попадает «к оплате» (решение владельца 2026-07-16).
    settled = await prepayments.auto_settle_invoice_from_open_prepayments(session, invoice)
    if settled > 0:
        result.settled_from_prepayments += 1
    doc.invoice_id = invoice.id
    doc.intake_status = "materialized"
    result.materialized += 1


async def _route_documents(session: AsyncSession, result: SbisSyncResult) -> None:
    """Маршрутизация: режим определяет карточка контрагента, а не документ."""
    docs = (
        await session.scalars(
            select(SbisDocument).where(
                SbisDocument.intake_status.in_(("mirror", "new_counterparty")),
                SbisDocument.invoice_id.is_(None),
            )
        )
    ).all()
    if not docs:
        return
    channel_ids = set(
        (
            await session.scalars(
                select(CounterpartyCollectionSource.counterparty_id).where(
                    CounterpartyCollectionSource.kind == "sbis"
                )
            )
        ).all()
    )
    cache: dict[str, Counterparty] = {}
    for doc in docs:
        counterparty = await _resolve_or_create_counterparty(session, doc, cache, result)
        if counterparty is None:
            continue  # без ИНН идентифицировать нечем — остаётся зеркалом
        doc.counterparty_id = counterparty.id
        if counterparty.status == "requires_setup":
            doc.intake_status = "new_counterparty"
            continue
        if counterparty.status != "active":
            # Архив и прочие не-активные: новые счета не создаём (архив = блок накладных),
            # документы остаются видимыми в зеркале.
            doc.intake_status = "mirror"
            continue
        if (
            counterparty.id in channel_ids
            and (doc.doc_type or "") in _MATERIALIZABLE_DOC_TYPES
            and doc.amount is not None
        ):
            await _materialize_document(session, doc, counterparty, result)
        else:
            doc.intake_status = "mirror"


async def _match_documents(session: AsyncSession, result: SbisSyncResult) -> None:
    unmatched = (
        (
            await session.execute(
                select(SbisDocument).where(
                    SbisDocument.match_status == "unmatched",
                    SbisDocument.intake_status == "mirror",
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
    if settings.sbis_fetch_invoice_registry:
        # Счета — отдельный реестр (и отдельное право доступа); идут общим конвейером.
        items.extend(await client.list_documents_by_type("СчетВх", date_from))
    result.fetched = len(items)

    await _upsert_documents(session, items, result)
    await session.flush()
    await _route_documents(session, result)
    await session.flush()
    await _match_documents(session, result)
    await session.commit()
    logger.info("sbis sync (%s): %s", run_reason, result.as_dict())
    return result

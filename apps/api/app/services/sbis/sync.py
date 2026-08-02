"""Синк СБИС ЭДО: зеркало + маршрутизация в счета для сервисных поставщиков.

Тянет реестр «Входящие» за окно ``sbis_sync_lookback_days``, апсертит ``sbis_document``
по идентификатору документа СБИС (повторный проход бесплатен), затем МАРШРУТИЗИРУЕТ
каждый документ — режим определяет карточка контрагента, не документ:

- ИНН не найден → placeholder-контрагент ``requires_setup`` (очередь needs-setup)
  и статус ``new_counterparty``: документы копятся, но не материализуются, пока
  оператор не настроит карточку и не включит канал;
- контрагент с каналом сбора 'sbis' и документ отгрузки (счёт/УПД/акт работ) →
  материализация в ``SupplierInvoice(source='sbis')`` с дедупом против почты и
  ручного ввода (строгий: ИНН + сумма + дата + номер; кросс-канальный: тот же
  контрагент + сумма в окне ±7 дней — номера у каналов не совпадают) и
  распознаванием периода услуги из названий документа (regex-слой «Страницы на
  оплату»);
- остальное → зеркало со сверкой против iiko-накладных:
  1. ``number_amount`` — номер поставщика (нормализованный) + точная сумма;
  2. ``amount_date`` — сумма + окно ±5 дней, только если кандидат ЕДИНСТВЕННЫЙ.

Уже сматченные/материализованные строки повторно не трогаем; ссылки на файлы
(живут ~месяц) освежаются каждым проходом.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.core.config import get_settings
from app.models import (
    Counterparty,
    CounterpartyCollectionSource,
    CounterpartyPayableProfile,
    CounterpartyRole,
    SbisDocument,
    SupplierInvoice,
)
from app.services import clock
from app.services import supplier_prepayments as prepayments
from app.services import supplier_service_periods as service_periods
from app.services.counterparty_registry import (
    CROSS_CHANNEL_DEDUP_WINDOW_DAYS,
    activate_configured_placeholder,
    compute_invoice_due_date,
)
from app.services.email_invoice_ingest import _guess_type, process_attachment
from app.services.invoice_recognition import _extract_service_periods
from app.services.mail.imap_client import FetchedAttachment
from app.services.sbis.client import SbisClient

logger = logging.getLogger(__name__)

# Материализуем только документы отгрузки (счета/УПД/акты выполненных работ). Акты
# сверки, договоры и корреспонденция учёт не двигают — остаются в зеркале. Этот же
# набор участвует в зеркальном матчинге с iiko.
_MATCHABLE_DOC_TYPES = {"ДокОтгрВх"}
_MATERIALIZABLE_DOC_TYPES = {"ДокОтгрВх", "СчетВх"}
_DATE_WINDOW_DAYS = 5
# СБИС-тип документа → роль в контуре ДЗ/КЗ (канон владельца 17.07). СчетВх — счёт на оплату
# (bill, вне баланса, очередь оплат); ДокОтгрВх (УПД/акт отгрузки) — закрывающий документ
# (closing): гасит дебиторку / встаёт в кредиторку.
_BILL_DOC_TYPES = {"СчетВх"}


def _doc_kind_for(doc_type: str | None) -> str:
    return "bill" if (doc_type or "") in _BILL_DOC_TYPES else "closing"


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
    sent_to_recognition: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        # asdict, а не ручное перечисление: забытое при добавлении счётчика поле уже
        # роняло /sbis/sync (ValidationError → 500). Одно место истины — сам dataclass.
        return dataclasses.asdict(self)


def _parse_amount(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _parse_date(value: Any) -> date | None:
    # НЕ заменять на invoice_recognition._parse_date: тот ищет дату regex'ом в любом месте
    # строки, принимает '-'/'/' и двухзначные годы — на строгом реестровом формате СБИС
    # («ДД.ММ.ГГГГ [ЧЧ.ММ.СС]») это даёт ложные срабатывания (например, ISO-строку
    # '2026-07-14' он прочтёт как 2014-07-26, где мы честно вернём None).
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
    """Дедуп с почтой/ручным вводом документов ОДНОЙ РОЛИ (канон ДЗ/КЗ, владелец 17.07).

    Канон развёл роли: счёт (bill) и его закрывающий УПД (closing) — РАЗНЫЕ документы и
    СОСУЩЕСТВУЮТ (счёт живёт в очереди оплат, УПД — в балансе ДЗ/КЗ). Поэтому пару «счёт↔УПД»
    больше НЕ схлопываем; дедупим только совпадения ОДНОГО doc_kind:
      • строгий — тот же документ (сумма + дата + номер), пришедший повторно;
      • кросс-канальный — тот же счёт/УПД пришёл и письмом/руками, и через ЭДО под другим
        номером: ловим по контрагенту + сумме + роли в окне ``CROSS_CHANNEL_DEDUP_WINDOW_DAYS``.
    Зеркальный iiko-контур сюда не входит (у 'sbis'-контрагентов iiko-накладных нет)."""
    if doc.amount is None:
        return None
    want_kind = _doc_kind_for(doc.doc_type)
    candidates = (
        await session.scalars(
            select(SupplierInvoice).where(
                SupplierInvoice.counterparty_id == counterparty_id,
                SupplierInvoice.amount == doc.amount,
                SupplierInvoice.payment_status != "void",
                SupplierInvoice.doc_kind == want_kind,
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
    if doc.doc_date is None:
        return None
    # Кросс-канальная пара ОДНОЙ роли: тот же документ уже пришёл письмом или внесён руками
    # под другим номером. Из нескольких кандидатов берём ближайший по дате.
    cross_channel = [
        candidate
        for candidate in candidates
        if candidate.source in ("email", "manual")
        and candidate.invoice_date is not None
        and abs((candidate.invoice_date - doc.doc_date).days) <= CROSS_CHANNEL_DEDUP_WINDOW_DAYS
    ]
    if cross_channel:
        return min(cross_channel, key=lambda c: abs((c.invoice_date - doc.doc_date).days))
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


async def _enrich_duplicate_invoice(
    session: AsyncSession, invoice: SupplierInvoice, doc: SbisDocument
) -> None:
    """Дубль из ЭДО обогащает существующую накладную тем, чего в ней нет; заполненное
    не перетираем. Период услуги снимает блок отправки в банк (period_required-гейт),
    НДС из формализованного документа точнее распознанного из PDF."""
    if invoice.service_period_status == "missing":
        period = _service_period_fields(doc, required=True)
        if period.get("service_period_status") == "ready":
            invoice.service_period_start = period["service_period_start"]
            invoice.service_period_end = period["service_period_end"]
            invoice.service_period_status = "ready"
            invoice.service_period_source = period["service_period_source"]
            invoice.service_period_confidence = period["service_period_confidence"]
            await service_periods.sync_invoice_accrual(session, invoice)
    if (
        (invoice.vat_total or Decimal("0")) == 0
        and doc.amount is not None
        and doc.amount_wo_vat is not None
        and doc.amount > doc.amount_wo_vat
    ):
        invoice.vat_total = doc.amount - doc.amount_wo_vat


async def _materialize_document(
    session: AsyncSession,
    doc: SbisDocument,
    counterparty: Counterparty,
    result: SbisSyncResult,
    *,
    profile: CounterpartyPayableProfile | None = None,
) -> None:
    # Идемпотентность на случай дрейфа: счёт из этого же СБИС-документа уже есть.
    existing = await session.scalar(
        select(SupplierInvoice).where(
            SupplierInvoice.source == "sbis",
            SupplierInvoice.external_id == doc.sbis_doc_id,
        )
    )
    if existing is not None:
        # Старые записи и данные из тестовых/ручных сценариев могли быть созданы до
        # появления явной области документа. Канал ЭДО всегда финансовый.
        existing.operational_scope = "finance"
        doc.invoice_id = existing.id
        doc.intake_status = "materialized"
        return

    duplicate = await _find_existing_invoice(session, counterparty.id, doc)
    if duplicate is not None:
        doc.invoice_id = duplicate.id
        doc.intake_status = "duplicate"
        result.duplicates += 1
        await _enrich_duplicate_invoice(session, duplicate, doc)
        return

    # Профиль приходит из балк-кэша маршрутизации; одиночный SELECT — только для
    # прямых вызовов вне конвейера (тесты, ручные сценарии).
    if profile is None:
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
        doc_kind=_doc_kind_for(doc.doc_type),  # СчетВх→bill, ДокОтгрВх(УПД/акт)→closing
        operational_scope="finance",
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
    # Закрывающий документ (УПД/акт) гасит дебиторку / встаёт в кредиторку (правило 2); будущей
    # датой откладывается до своей даты (правило 4). Счёт (bill) в баланс не входит — no-op.
    settled = await prepayments.apply_closing_document(session, invoice)
    # Признание расхода — строго ПОСЛЕ проведения: ``informational`` (у контрагента договор)
    # выставляется внутри, а признание информационного документа удвоило бы расход месяца.
    await service_periods.sync_invoice_accrual(session, invoice)
    if settled > 0:
        result.settled_from_prepayments += 1
    doc.invoice_id = invoice.id
    doc.intake_status = "materialized"
    result.materialized += 1


# Письмо (КоррВх) считаем счётом, если у вложения «говорящее» имя. Неформализованные
# счета в СБИС ходят именно письмами (пример: счёт-оферта ДоксИнБокс).
_INVOICE_LETTER_MARKERS = ("счет", "счёт", "invoice")


def _letter_invoice_attachment(doc: SbisDocument) -> dict[str, str] | None:
    raw_doc = (doc.raw or {}).get("Документ") or {}
    for attachment in raw_doc.get("Вложение") or []:
        if attachment.get("Служебный") == "Да":
            continue
        file_info = attachment.get("Файл") or {}
        name = str(attachment.get("Название") or file_info.get("Имя") or "")
        link = file_info.get("Ссылка")
        if link and any(marker in name.casefold() for marker in _INVOICE_LETTER_MARKERS):
            return {"name": name, "link": link}
    return None


async def _route_invoice_letter(
    session: AsyncSession,
    client: SbisClient,
    doc: SbisDocument,
    result: SbisSyncResult,
) -> bool:
    """Письмо-счёт → распознавание «Страницы на оплату» (суммы в письме нет — она в PDF).

    Скачиваем PDF и отдаём в существующий конвейер intake (распознавание regex+LLM,
    ручной разбор, дедуп, статья ДДС, банк). Идемпотентность — по SHA-256 файла."""
    attachment = _letter_invoice_attachment(doc)
    if attachment is None:
        return False
    from app.models import EmailInvoiceIntake

    try:
        content = await client.download_file(attachment["link"])
    except Exception as exc:  # noqa: BLE001 — одно битое письмо не валит синк
        logger.warning("СБИС письмо-счёт: не скачался PDF (%s): %s", doc.sbis_doc_id, exc)
        result.errors.append(f"письмо {doc.number}: PDF не скачался")
        return False
    fetched = FetchedAttachment(
        mailbox="sbis",
        message_uid=doc.sbis_doc_id,
        message_id=f"sbis:{doc.sbis_doc_id}",
        from_addr=f"СБИС ИНН {doc.counterparty_inn or '—'}",
        subject=doc.title,
        received_at=None,
        filename=attachment["name"],
        mime="application/pdf",
        content=content,
    )
    existing = await session.scalar(
        select(EmailInvoiceIntake).where(
            EmailInvoiceIntake.attachment_sha256 == fetched.sha256
        )
    )
    if existing is None:
        settings = get_settings()
        await process_attachment(session, fetched, settings=settings)
        result.sent_to_recognition += 1
    doc.intake_status = "sent_to_recognition"
    return True


async def _route_documents(
    session: AsyncSession, result: SbisSyncResult, client: SbisClient | None = None
) -> None:
    """Маршрутизация: режим определяет карточка контрагента, а не документ."""
    client = client or SbisClient()
    docs = (
        await session.scalars(
            select(SbisDocument)
            .options(undefer(SbisDocument.raw))  # raw deferred; конвейеру нужен целиком
            .where(
                SbisDocument.intake_status.in_(("mirror", "new_counterparty")),
                SbisDocument.invoice_id.is_(None),
            )
        )
    ).all()
    if not docs:
        return
    source_rows = (
        await session.execute(
            select(
                CounterpartyCollectionSource.counterparty_id,
                CounterpartyCollectionSource.kind,
            ).where(
                CounterpartyCollectionSource.kind.in_(("iiko", "sbis")),
                CounterpartyCollectionSource.is_active.is_(True),
            )
        )
    ).all()
    channel_ids = {cp_id for cp_id, kind in source_rows if kind == "sbis"}
    iiko_channel_ids = {cp_id for cp_id, kind in source_rows if kind == "iiko"}
    # Зеркало-набор перечитывается каждый прогон целиком (так задумано: документы
    # new_counterparty материализуются задним числом после настройки карточки) — поэтому
    # контрагентов резолвим ОДНИМ запросом по всем ИНН пачки, а не по SELECT'у на документ.
    cache: dict[str, Counterparty] = {}
    doc_inns = {doc.counterparty_inn for doc in docs if doc.counterparty_inn}
    if doc_inns:
        known = await session.scalars(
            select(Counterparty).where(Counterparty.inn.in_(doc_inns))
        )
        cache = {cp.inn: cp for cp in known if cp.inn}
    # Профили материализуемых карточек — тоже пачкой (нужны для срока оплаты и периода).
    profile_cache: dict[uuid.UUID, CounterpartyPayableProfile] = {}
    cached_ids = {cp.id for cp in cache.values()}
    if cached_ids:
        profiles = await session.scalars(
            select(CounterpartyPayableProfile).where(
                CounterpartyPayableProfile.counterparty_id.in_(cached_ids)
            )
        )
        profile_cache = {profile.counterparty_id: profile for profile in profiles}
    for doc in docs:
        counterparty = await _resolve_or_create_counterparty(session, doc, cache, result)
        if counterparty is None:
            continue  # без ИНН идентифицировать нечем — остаётся зеркалом
        doc.counterparty_id = counterparty.id
        if counterparty.status == "requires_setup":
            # Самовосстановление карточек, сохранённых до исправления: профиль уже
            # настроен, но прежний PUT оставлял базовый статус requires_setup и не
            # подключал канал. Такой документ можно обработать в этом же проходе.
            profile = profile_cache.get(counterparty.id)
            if profile is None:
                profile = await session.scalar(
                    select(CounterpartyPayableProfile).where(
                        CounterpartyPayableProfile.counterparty_id == counterparty.id
                    )
                )
                if profile is not None:
                    profile_cache[counterparty.id] = profile
            activated = profile is not None and await activate_configured_placeholder(
                session, counterparty, profile
            )
            if not activated:
                doc.intake_status = "new_counterparty"
                continue
            channel_ids.add(counterparty.id)
        if counterparty.status != "active":
            # Архив и прочие не-активные: новые счета не создаём (архив = блок накладных),
            # документы остаются видимыми в зеркале.
            doc.intake_status = "mirror"
            continue
        # Для iiko-поставщика приход товара канонически живёт только в iiko. УПД из
        # СБИС сохраняем как ЭДО-зеркало и ниже связываем с iiko-накладной, но второе
        # обязательство SupplierInvoice не создаём. СчетВх остаётся отдельным bill:
        # его можно оплатить до поставки, а будущий iiko closing погасит предоплату.
        if counterparty.id in iiko_channel_ids and (doc.doc_type or "") == "ДокОтгрВх":
            doc.intake_status = "mirror"
            doc.invoice_id = None
            continue
        if (
            counterparty.id in channel_ids
            and (doc.doc_type or "") in _MATERIALIZABLE_DOC_TYPES
            and doc.amount is not None
        ):
            await _materialize_document(
                session, doc, counterparty, result,
                profile=profile_cache.get(counterparty.id),
            )
        elif counterparty.id in channel_ids and (doc.doc_type or "") == "КоррВх":
            # Неформализованный счёт ходит письмом (сумма только в PDF) —
            # отправляем в распознавание «Страницы на оплату».
            handled = await _route_invoice_letter(session, client, doc, result)
            if not handled:
                doc.intake_status = "mirror"
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

    # ИНН-гард против кросс-поставщикового матча: сумма 5000 у поставщика А не должна
    # «пришиться» к накладной поставщика Б только потому, что даты рядом. Сверяем ИНН
    # карточек, когда он есть у ОБЕИХ сторон; карточки без ИНН (iiko-имена) не режем —
    # это известный кейс двойного реестра, там матч по сумме+дате остаётся единственным.
    invoice_cp_ids = {inv.counterparty_id for inv in invoices if inv.counterparty_id}
    inn_by_cp: dict[uuid.UUID, str | None] = {}
    if invoice_cp_ids:
        rows = await session.execute(
            select(Counterparty.id, Counterparty.inn).where(Counterparty.id.in_(invoice_cp_ids))
        )
        inn_by_cp = {cp_id: inn for cp_id, inn in rows}

    def _inn_compatible(doc: SbisDocument, invoice: SupplierInvoice) -> bool:
        doc_inn = (doc.counterparty_inn or "").strip()
        invoice_inn = (inn_by_cp.get(invoice.counterparty_id) or "").strip()
        return not doc_inn or not invoice_inn or doc_inn == invoice_inn

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
            exact = [
                invoice
                for invoice in by_number_amount.get((number, doc.amount)) or []
                if _inn_compatible(doc, invoice)
            ]
            if len(exact) == 1:
                matched, note = exact[0], "number_amount"
        if matched is None and doc.amount is not None and doc.doc_date is not None:
            near = [
                invoice
                for invoice in by_amount.get(doc.amount, [])
                if invoice.invoice_date is not None
                and abs((invoice.invoice_date - doc.doc_date).days) <= _DATE_WINDOW_DAYS
                and _inn_compatible(doc, invoice)
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
    date_from = clock.moscow_today() - timedelta(days=lookback)
    try:
        items = await client.list_incoming_documents(date_from)
        if settings.sbis_fetch_invoice_registry:
            # Счета — отдельный реестр (и отдельное право доступа); идут общим конвейером.
            items.extend(await client.list_documents_by_type("СчетВх", date_from))
        result.fetched = len(items)

        await _upsert_documents(session, items, result)
        await session.flush()
        await _route_documents(session, result, client)
        await session.flush()
        await _match_documents(session, result)
        await session.commit()
    finally:
        # Один прогон = одно keep-alive-соединение; закрываем его вместе с прогоном.
        await client.aclose()
    logger.info("sbis sync (%s): %s", run_reason, result.as_dict())
    return result

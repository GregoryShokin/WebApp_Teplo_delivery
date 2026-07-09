"""Отправка вручную созданной складской накладной В iiko через **Cloud API** (двусторонняя
интеграция, Phase 3; унифицировано на Cloud — легаси RMS-XML-импорт выведен).

iiko остаётся источником складского учёта: накладные, созданные у нас, уходят в iiko документами.
payable → ``incoming_invoice`` (``counteragent`` = поставщик), receivable → ``outgoing_invoice``.
Строки со статьёй-расходом/персоналом (не «Оплата поставщикам») из документа исключаются — это
отдельный денежный контур, а не складской приход.

Контур Cloud (create → post):
- ``create`` (incoming/outgoing) возвращает ``documentId`` прямо в ответе — сохраняем его в
  ``external_id`` (реверс-синхронизация дедупит по нему); export-lookup id по номеру не нужен.
- После ``create`` документ рождается ``NEW`` — явным ``post`` проводим его в ``PROCESSED``.
- Cloud ``create`` НЕ идемпотентен (каждый вызов плодит новый ``documentId``), поэтому повтор гейтим
  на НАШЕЙ стороне: если у накладной уже есть ``external_id`` — не создаём заново (иначе
  дубль реального документа). _external_id_owner страхует uq_supplier_invoice_source_external.
- Пуш — явное действие (авто-пуш на создание намеренно ВЫКЛ: создание реального документа iiko
  необратимо). Никогда не бросаем на ошибках iiko — фиксируем ``iiko_push_status`` +
  ``iiko_push_error`` вместо исключения.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time

import anyio
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AppSetting,
    CounterpartyAlias,
    DdsArticle,
    IikoProduct,
    InvoiceLineItem,
    SupplierInvoice,
)
from app.services.iiko_cloud_client import iiko_auth_token, iiko_opener
from app.services.iiko_invoice_cloud import (
    CloudInvoiceDoc,
    CloudInvoiceLine,
    build_invoice_body,
    business_error_message,
    create_invoice,
    extract_document_id,
    post_invoice,
    update_invoice,
)
from app.services.warehouse_invoices import SUPPLIER_PAYMENT_ARTICLE_NAME

STORE_SETTING_KEY = "iiko.default_store_guid"
IIKO_SOURCE = "iiko"


class WarehousePushError(RuntimeError):
    """Доменная ошибка пуша в iiko (маппится в HTTP 404/409)."""


@dataclass
class PreparedPush:
    """Готовое к отправке доменно-нейтральное описание накладной для Cloud (или причина пропуска).
    Сеть не трогает — unit-тестируемо."""

    doc: CloudInvoiceDoc | None
    skip_reason: str | None = None
    partner_guid: str | None = None
    doc_number: str | None = None
    doc_date: date | None = None


@dataclass
class _CloudPushOutcome:
    document_id: str | None
    posted: bool
    error: str | None = None
    created: bool = False  # создали новый документ в этом вызове (иначе — только post)


async def _store_guid(session: AsyncSession, invoice: SupplierInvoice) -> str | None:
    if invoice.store_guid:
        return invoice.store_guid
    setting = await session.scalar(select(AppSetting).where(AppSetting.key == STORE_SETTING_KEY))
    if setting is None:
        return None
    value = setting.value
    if isinstance(value, dict):
        return value.get("guid") or value.get("store_guid")
    return str(value) if value else None


async def prepare_push(session: AsyncSession, invoice: SupplierInvoice) -> PreparedPush:
    """Резолвинг GUID + сборка ``CloudInvoiceDoc``, либо причина пропуска. Сеть не трогает.

    Транспорт-независимая доменная логика: партнёр из ``CounterpartyAlias(source='iiko')``, склад из
    накладной/настройки, ТОЛЬКО товарные строки (``is_staff=False``, не возврат, без статьи ДДС или
    статья «Оплата поставщикам»), GUID единиц из кэша номенклатуры.
    """
    partner_guid = await session.scalar(
        select(CounterpartyAlias.alias).where(
            CounterpartyAlias.counterparty_id == invoice.counterparty_id,
            CounterpartyAlias.source == IIKO_SOURCE,
        )
    )
    if not partner_guid:
        return PreparedPush(None, skip_reason="Нет iiko-GUID контрагента")
    store_guid = await _store_guid(session, invoice)
    if not store_guid:
        return PreparedPush(None, skip_reason="Не настроен склад (iiko.default_store_guid)")
    # В iiko уходят ТОЛЬКО товарные строки: без статьи ДДС (склад) или статья «Оплата поставщикам».
    # Расходные статьи (питание/расходы персонала) — затраты, не склад; их не отправляем.
    # Признак — статья, а не is_staff (чек кладёт статью в строку, оставляя is_staff=false).
    # См. project_card_purchase_invoice_gap.
    goods_articles = select(DdsArticle.id).where(DdsArticle.name == SUPPLIER_PAYMENT_ARTICLE_NAME)
    rows = (
        await session.scalars(
            select(InvoiceLineItem)
            .where(
                InvoiceLineItem.invoice_id == invoice.id,
                InvoiceLineItem.is_staff.is_(False),
                # Возвращённые позиции чека Кассы (товар вернули в магазин) на склад не приходуются.
                InvoiceLineItem.is_return.is_(False),
                or_(
                    InvoiceLineItem.dds_article_id.is_(None),
                    InvoiceLineItem.dds_article_id.in_(goods_articles),
                ),
            )
            .order_by(InvoiceLineItem.sort_order)
        )
    ).all()
    # GUID единицы измерения (amountUnit) — из кэша номенклатуры.
    product_ids = [row.iiko_product_id for row in rows if row.iiko_product_id]
    unit_by_product: dict[uuid.UUID, str | None] = {}
    if product_ids:
        products = (
            await session.scalars(select(IikoProduct).where(IikoProduct.id.in_(product_ids)))
        ).all()
        unit_by_product = {p.id: p.main_unit_guid for p in products}
    lines: list[CloudInvoiceLine] = []
    for index, line in enumerate(rows, start=1):
        if not line.product_guid:
            continue
        lines.append(
            CloudInvoiceLine(
                num=index,
                product=line.product_guid,
                store=store_guid,
                amount=float(line.quantity),
                price=float(line.price) if line.price is not None else None,
                sum=float(line.sum) if line.sum is not None else None,
                amount_unit=unit_by_product.get(line.iiko_product_id),
                vat_percent=float(line.vat_percent) if line.vat_percent is not None else None,
            )
        )
    if not lines:
        return PreparedPush(None, skip_reason="Нет товарных строк с iiko-GUID (персонал/ручные)")
    dt = invoice.issued_at
    if dt is None and invoice.invoice_date is not None:
        dt = datetime.combine(invoice.invoice_date, time())
    if dt is None:
        return PreparedPush(None, skip_reason="Нет даты накладной")
    incoming_date = (
        datetime.combine(dt.date(), time()) if invoice.direction == "payable" else None
    )
    # Как в легаси: шлём наивный wall-clock (iiko трактует как МСК); tz снимаем, чтобы инстант не
    # сдвигался и дата документа совпадала с датой RMS-версии (никакого изменения поведения).
    doc = CloudInvoiceDoc(
        direction=invoice.direction,
        counteragent=partner_guid,
        date=dt.replace(tzinfo=None),
        lines=lines,
        number=invoice.number or None,
        default_store=store_guid,
        incoming_date=incoming_date,
        document_id=invoice.external_id or None,  # заполнен → путь update/repost; None → create
    )
    return PreparedPush(
        doc,
        partner_guid=partner_guid,
        doc_number=invoice.number or "",
        doc_date=dt.date(),
    )


def _cloud_create_and_post(
    direction: str, organization_id: str, body: dict, *, existing_document_id: str | None
) -> _CloudPushOutcome:
    """Синхронно (в треде): один auth → (create, если документа ещё нет) → post. Не бросает —
    ошибки транспорта/бизнеса возвращаются в ``error``. iiko отдаёт бизнес-отказ кодом 500/409 с
    ``message`` — парсим его."""
    opener = iiko_opener()
    try:
        token = iiko_auth_token(opener)
    except Exception as exc:  # noqa: BLE001 — нет креды/сеть/прокси
        return _CloudPushOutcome(existing_document_id, False, f"auth: {exc}"[:400])

    document_id = existing_document_id
    created = False
    if not document_id:
        status, resp = create_invoice(direction, body, token=token, opener=opener)
        if not (200 <= status < 300):
            return _CloudPushOutcome(
                None, False, business_error_message(resp) or f"create HTTP {status}"
            )
        document_id = extract_document_id(resp)
        if not document_id:
            return _CloudPushOutcome(None, False, "create: iiko не вернул documentId")
        created = True

    pstatus, presp = post_invoice(
        direction, organization_id, document_id, token=token, opener=opener
    )
    if not (200 <= pstatus < 300):
        return _CloudPushOutcome(
            document_id, False, business_error_message(presp) or f"post HTTP {pstatus}",
            created=created,
        )
    return _CloudPushOutcome(document_id, True, None, created=created)


def _cloud_update_and_post(
    direction: str, organization_id: str, body: dict, *, document_id: str
) -> _CloudPushOutcome:
    """Синхронно (в треде): один auth → update → post. ``update`` проведённого документа сам его
    распроводит в NEW, поэтому после правки проводим заново. Не бросает — ошибки в ``error``."""
    opener = iiko_opener()
    try:
        token = iiko_auth_token(opener)
    except Exception as exc:  # noqa: BLE001 — нет креды/сеть/прокси
        return _CloudPushOutcome(document_id, False, f"auth: {exc}"[:400])

    status, resp = update_invoice(direction, body, token=token, opener=opener)
    if not (200 <= status < 300):
        return _CloudPushOutcome(
            document_id, False, business_error_message(resp) or f"update HTTP {status}"
        )
    pstatus, presp = post_invoice(
        direction, organization_id, document_id, token=token, opener=opener
    )
    if not (200 <= pstatus < 300):
        return _CloudPushOutcome(
            document_id, False, business_error_message(presp) or f"post HTTP {pstatus}"
        )
    return _CloudPushOutcome(document_id, True, None)


async def _external_id_owner(
    session: AsyncSession, *, source: str, external_id: str, exclude_id: uuid.UUID
) -> uuid.UUID | None:
    """Id другой накладной, уже держащей ``(source, external_id)``, если есть.

    Страхует индекс ``uq_supplier_invoice_source_external``: две НАШИ накладные, ведущие к
    ОДНОМУ документу iiko, дали бы ``IntegrityError`` (→ 500 при уже закоммиченной строке → дубль).
    Ловим заранее и держим пуш не-фатальным."""
    return await session.scalar(
        select(SupplierInvoice.id).where(
            SupplierInvoice.source == source,
            SupplierInvoice.external_id == external_id,
            SupplierInvoice.id != exclude_id,
        )
    )


async def push_invoice_to_iiko(session: AsyncSession, invoice_id: uuid.UUID) -> SupplierInvoice:
    """Отправить накладную в iiko (РЕАЛЬНЫЙ документ) через Cloud ``create`` → ``post``. Обновляет
    push-статус; никогда не бросает на ошибках iiko — оставляет накладную со статусом
    ``failed``/``skipped`` и текстом ошибки."""
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        raise WarehousePushError("Накладная не найдена")

    # Идемпотентность: уже проведена в iiko — повторно не создаём/не проводим.
    if invoice.external_id and invoice.iiko_push_status == "pushed":
        return invoice

    prepared = await prepare_push(session, invoice)
    if prepared.doc is None:
        invoice.iiko_push_status = "skipped"
        invoice.iiko_push_error = prepared.skip_reason
        await session.commit()
        return invoice

    # Креды Cloud живут в DB-vault → грузим IIKO_CLOUD_* в os.environ перед вызовом.
    from app.services.iiko_sync import _load_source_credential_env

    await _load_source_credential_env(session)

    body = build_invoice_body(prepared.doc)
    # Есть external_id, но не pushed → документ создан, но не проведён (прошлый post упал) → только
    # re-post (create НЕ повторяем — иначе дубль). Нет external_id → create + post.
    existing_document_id = invoice.external_id or None

    try:
        outcome = await anyio.to_thread.run_sync(
            lambda: _cloud_create_and_post(
                invoice.direction,
                prepared.doc.organization_id,
                body,
                existing_document_id=existing_document_id,
            )
        )
    except Exception as exc:  # noqa: BLE001 — держим накладную, пишем ошибку
        invoice.iiko_push_status = "failed"
        invoice.iiko_push_error = str(exc)[:500]
        await session.commit()
        return invoice

    # Если в этом вызове создали новый документ — проверим, не занят ли его id другой накладной
    # (страховка уникального индекса). Занят → это дубль: не присваиваем external_id.
    if (
        outcome.created
        and outcome.document_id
        and await _external_id_owner(
            session, source=invoice.source, external_id=outcome.document_id, exclude_id=invoice.id
        )
    ):
        invoice.iiko_push_status = "failed"
        invoice.iiko_push_error = (
            f"iiko уже содержит документ №{invoice.number or '—'} "
            "(привязан к другой накладной — вероятный дубль)"
        )[:500]
        await session.commit()
        return invoice

    if outcome.document_id:
        # Сохраняем id даже если post упал (создан → повторно НЕ создавать, только re-post).
        invoice.external_id = outcome.document_id

    if outcome.posted:
        invoice.iiko_push_status = "pushed"
        invoice.iiko_pushed_at = datetime.now(UTC)
        invoice.iiko_push_error = None
    else:
        invoice.iiko_push_status = "failed"
        invoice.iiko_push_error = (outcome.error or "iiko: не удалось провести документ")[:500]
    await session.commit()
    return invoice


async def propagate_invoice_edit_to_iiko(
    session: AsyncSession, invoice_id: uuid.UUID
) -> SupplierInvoice:
    """Пробросить правку уже выгруженной накладной в iiko (Cloud ``update`` → ``post``).

    Зовётся ПОСЛЕ локальной правки (``update_warehouse_invoice``). Только для накладных, уже
    существующих в iiko (есть ``external_id``); правка ограничена неоплаченными (гейт в
    ``update_warehouse_invoice``), поэтому распроводка при ``update`` не упрётся в проводки оплаты.
    Не-фатально: локальная правка остаётся, статус/ошибка фиксируются."""
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        raise WarehousePushError("Накладная не найдена")
    if not invoice.external_id:
        return invoice  # ещё не в iiko — правка локальная, целиком отправит push позже

    prepared = await prepare_push(session, invoice)
    if prepared.doc is None:
        # После правки не осталось товарных строк для iiko — обновлять документ нечем.
        invoice.iiko_push_status = "failed"
        invoice.iiko_push_error = (
            prepared.skip_reason or "Правка убрала товарные строки — обновите iiko вручную"
        )[:500]
        await session.commit()
        return invoice

    from app.services.iiko_sync import _load_source_credential_env

    await _load_source_credential_env(session)
    body = build_invoice_body(prepared.doc)  # тело update включает documentId = external_id
    try:
        outcome = await anyio.to_thread.run_sync(
            lambda: _cloud_update_and_post(
                invoice.direction,
                prepared.doc.organization_id,
                body,
                document_id=invoice.external_id,
            )
        )
    except Exception as exc:  # noqa: BLE001 — держим локальную правку, пишем ошибку
        invoice.iiko_push_status = "failed"
        invoice.iiko_push_error = str(exc)[:500]
        await session.commit()
        return invoice

    if outcome.posted:
        invoice.iiko_push_status = "pushed"
        invoice.iiko_pushed_at = datetime.now(UTC)
        invoice.iiko_push_error = None
    else:
        invoice.iiko_push_status = "failed"
        invoice.iiko_push_error = (outcome.error or "iiko: правка не проведена")[:500]
    await session.commit()
    return invoice

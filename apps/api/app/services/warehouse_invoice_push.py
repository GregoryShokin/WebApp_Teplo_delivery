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
from dataclasses import dataclass, replace
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
    SupplierInvoiceTombstone,
)
from app.services.iiko_cloud_client import IIKO_ORGANIZATION_ID, iiko_auth_token, iiko_opener
from app.services.iiko_invoice_cloud import (
    CloudInvoiceDoc,
    CloudInvoiceLine,
    build_invoice_body,
    business_error_message,
    cancel_invoice,
    create_invoice,
    extract_document_id,
    post_invoice,
    unpost_invoice,
    update_invoice,
)
from app.services.iiko_returned_cloud import (
    build_returned_invoice_body,
    create_returned_invoice,
    post_returned_invoice,
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


def _cloud_delete_document(direction: str, organization_id: str, document_id: str) -> str | None:
    """Синхронно (в треде): удалить документ в iiko — ``unpost`` → ``cancel`` (``cancel`` работает
    только на NEW; на PROCESSED отвечает 409, поэтому сперва распроводим). 409 «status mismatch»
    на ``unpost`` терпим — документ уже NEW. Возвращает None при успехе, иначе текст ошибки."""
    opener = iiko_opener()
    try:
        token = iiko_auth_token(opener)
    except Exception as exc:  # noqa: BLE001 — нет креды/сеть/прокси
        return f"auth: {exc}"[:400]

    status, resp = unpost_invoice(direction, organization_id, document_id,
                                  token=token, opener=opener)
    if not (200 <= status < 300):
        message = business_error_message(resp) or f"unpost HTTP {status}"
        # Уже NEW (не проведён) — распроводить нечего, идём к cancel.
        if "status mismatch" not in message:
            return message
    cstatus, cresp = cancel_invoice(direction, organization_id, document_id,
                                    token=token, opener=opener)
    if not (200 <= cstatus < 300):
        return business_error_message(cresp) or f"cancel HTTP {cstatus}"
    return None


async def delete_invoice_in_iiko(invoice: SupplierInvoice) -> str | None:
    """Удалить iiko-документ накладной (``unpost``→``cancel``). None — успех/нечего удалять."""
    if not invoice.external_id:
        return None
    return await anyio.to_thread.run_sync(
        lambda: _cloud_delete_document(
            invoice.direction, IIKO_ORGANIZATION_ID, invoice.external_id
        )
    )


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


def _cloud_create_and_post_return(organization_id: str, body: dict) -> _CloudPushOutcome:
    """Синхронно (в треде): один auth → create возвратной → post. Не бросает — ошибки транспорта/
    бизнеса возвращаются в ``error`` (iiko отдаёт отказ кодом 500/409 с ``message``)."""
    opener = iiko_opener()
    try:
        token = iiko_auth_token(opener)
    except Exception as exc:  # noqa: BLE001 — нет креды/сеть/прокси
        return _CloudPushOutcome(None, False, f"auth: {exc}"[:400])
    status, resp = create_returned_invoice(body, token=token, opener=opener)
    if not (200 <= status < 300):
        return _CloudPushOutcome(None, False, business_error_message(resp) or f"create HTTP {status}")
    document_id = extract_document_id(resp)
    if not document_id:
        return _CloudPushOutcome(None, False, "create: iiko не вернул documentId")
    pstatus, presp = post_returned_invoice(organization_id, document_id, token=token, opener=opener)
    if not (200 <= pstatus < 300):
        return _CloudPushOutcome(
            document_id, False, business_error_message(presp) or f"post HTTP {pstatus}"
        )
    return _CloudPushOutcome(document_id, True, None)


def _correction_number(base: str | None) -> str | None:
    """Номер для НОВОЙ (правильной) приходной Y: помечаем «(испр.)», чтобы в бэк-офисе iiko её
    было видно отдельно от тумбстоненного оригинала. None (нет номера) → iiko занумерует сам."""
    if not base:
        return None
    return f"{base} (испр.)"[:128]


async def _tombstone_exists(session: AsyncSession, source: str, external_id: str) -> bool:
    return (
        await session.scalar(
            select(SupplierInvoiceTombstone.id).where(
                SupplierInvoiceTombstone.source == source,
                SupplierInvoiceTombstone.external_id == external_id,
            )
        )
    ) is not None


async def book_correction_in_iiko(
    session: AsyncSession, invoice_id: uuid.UUID
) -> SupplierInvoice:
    """Отразить коррекцию ОПЛАЧЕННОЙ накладной в iiko полным разворотом (два документа):

    1. **Возврат** ВСЕХ старых товаров (``returned_invoice`` со ссылкой на ``incomingInvoiceId``
       оригинала X) → снимает старый приход со склада и создаёт долг поставщика (дебиторку).
    2. **Новая приходная** Y на правильные товары (``incoming_invoice`` create→post) БЕЗ оплаты;
       ``external_id`` накладной перецепляется X→Y, старый X закрывается тумбстоном (иначе реверс-синк
       воскресил бы оплаченный+возвращённый оригинал как новую накладную).

    Итог: чистый баланс поставщика = верная дебиторка (старая сумма − новая); переплата покрыта у
    нас (``SupplierPrepayment``). Y остаётся «не оплачена» НАМЕРЕННО: оплатить её зачётом
    (``add_payment``) нельзя — settle-дебет уезжает в баланс поставщика и завышает его (проверено на
    живом API 11.07: зачёт давал +полная-сумма вместо +разница). Сага резюмируемая по чек-пойнтам
    (``iiko_return_external_id`` — шаг 1, ``iiko_correction_new_external_id`` + перецеп
    ``external_id`` — шаг 2): не-фатально, при сбое статус/ошибка садятся в накладную, ретрай
    (``retry-iiko-return``) продолжит с последнего успешного шага. См.
    project_edit_paid_invoice_feasibility."""
    invoice = await session.get(SupplierInvoice, invoice_id)
    if invoice is None:
        raise WarehousePushError("Накладная не найдена")
    if invoice.iiko_return_status not in ("pending", "failed"):
        return invoice  # нечего разворачивать / уже проведено

    # Инварианты — в локали: после commit ORM-атрибуты истекают, а лениво их читать в async нельзя
    # (MissingGreenlet). Мутабельные чек-пойнты (return_doc/new_doc) тоже ведём локально.
    inv_source = invoice.source
    inv_number = invoice.number
    inv_direction = invoice.direction
    original_external_id = invoice.external_id
    return_lines = list(invoice.iiko_return_lines or [])
    return_doc = invoice.iiko_return_external_id
    new_doc = invoice.iiko_correction_new_external_id

    # Защита второго входа (retry-iiko-return): пока external_id ещё = X (шаг 2 не сделан) и оплата
    # оригинала НЕ отражена в iiko — разворот не запускаем (возврат уйдёт не на ту сумму). Основной
    # гард стоит в adjust_paid_invoice; здесь дублируем на случай прямого ретрая. См. edit_paid.
    if new_doc is None:
        from app.services.counterparty_iiko_payment import original_payment_settled_in_iiko

        if not await original_payment_settled_in_iiko(session, invoice):
            invoice.iiko_return_status = "failed"
            invoice.iiko_return_error = (
                "Оплата оригинала ещё не отражена в iiko — сначала отправьте оплату в iiko или "
                "подтвердите вручную, затем повторите коррекцию"
            )
            await session.commit()
            return invoice

    if not original_external_id and not new_doc:
        invoice.iiko_return_status = "skipped"
        invoice.iiko_return_error = "Накладная не выгружена в iiko — коррекцию отражать негде"
        invoice.iiko_return_lines = []
        await session.commit()
        return invoice
    if return_doc is None and not return_lines:
        # Без снимка старых товаров возврат невозможен, а заносить новую приходную без возврата
        # нельзя (задвоился бы приход в iiko). Контур не запускаем.
        invoice.iiko_return_status = "skipped"
        invoice.iiko_return_error = "Нет позиций для возврата старого прихода"
        await session.commit()
        return invoice

    partner_guid = await session.scalar(
        select(CounterpartyAlias.alias).where(
            CounterpartyAlias.counterparty_id == invoice.counterparty_id,
            CounterpartyAlias.source == IIKO_SOURCE,
        )
    )
    store_guid = await _store_guid(session, invoice)
    if not partner_guid or not store_guid:
        invoice.iiko_return_status = "failed"
        invoice.iiko_return_error = "Нет iiko-GUID контрагента или склада для коррекции"
        await session.commit()
        return invoice

    dt = invoice.issued_at or (
        datetime.combine(invoice.invoice_date, time())
        if invoice.invoice_date
        else datetime.now(UTC)
    )
    date_naive = dt.replace(tzinfo=None)

    from app.services.iiko_sync import _load_source_credential_env

    await _load_source_credential_env(session)

    # --- ШАГ 1: возврат ВСЕХ старых товаров (ссылка на оригинал X) → дебиторка поставщику ---
    if return_doc is None:
        items = [
            {
                "num": idx,
                "product": line["product"],
                "store": store_guid,
                "amount": float(line["quantity"]),
                "price": float(line.get("price") or 0),
                "sum": round(float(line["quantity"]) * float(line.get("price") or 0), 2),
            }
            for idx, line in enumerate(return_lines, start=1)
        ]
        return_body = build_returned_invoice_body(
            counteragent=partner_guid,
            date=date_naive,
            items=items,
            incoming_invoice_id=original_external_id,
            default_store=store_guid,
            comment=f"Коррекция оплаченной накладной №{inv_number or '—'} — возврат старого прихода",
        )
        try:
            outcome = await anyio.to_thread.run_sync(
                lambda: _cloud_create_and_post_return(IIKO_ORGANIZATION_ID, return_body)
            )
        except Exception as exc:  # noqa: BLE001 — держим коррекцию, пишем ошибку
            invoice.iiko_return_status = "failed"
            invoice.iiko_return_error = str(exc)[:500]
            await session.commit()
            return invoice
        if not (outcome.posted and outcome.document_id):
            invoice.iiko_return_status = "failed"
            invoice.iiko_return_error = (outcome.error or "iiko: возврат не проведён")[:500]
            await session.commit()
            return invoice
        invoice.iiko_return_external_id = outcome.document_id
        invoice.iiko_return_error = None
        await session.commit()  # чек-пойнт шага 1
        await session.refresh(invoice)
        return_doc = outcome.document_id

    # --- ШАГ 2: новая (правильная) приходная Y + перецеп external_id X→Y + тумбстон X ---
    if new_doc is None:
        prepared = await prepare_push(session, invoice)
        if prepared.doc is None:
            invoice.iiko_return_status = "failed"
            invoice.iiko_return_error = (
                prepared.skip_reason or "Нет товарных строк для новой приходной"
            )[:500]
            await session.commit()
            return invoice
        # Форсим CREATE (document_id оригинала обнуляем) + «(испр.)» в номере для трассировки.
        new_body = build_invoice_body(
            replace(prepared.doc, document_id=None, number=_correction_number(inv_number))
        )
        try:
            outcome = await anyio.to_thread.run_sync(
                lambda: _cloud_create_and_post(
                    inv_direction,
                    prepared.doc.organization_id,
                    new_body,
                    existing_document_id=None,
                )
            )
        except Exception as exc:  # noqa: BLE001
            invoice.iiko_return_status = "failed"
            invoice.iiko_return_error = str(exc)[:500]
            await session.commit()
            return invoice
        if not outcome.document_id:
            invoice.iiko_return_status = "failed"
            invoice.iiko_return_error = (outcome.error or "iiko: новая приходная не создана")[:500]
            await session.commit()
            return invoice
        # Документ Y создан (id есть, даже если post упал). Фиксируем Y, перецепляем external_id
        # X→Y и закрываем X тумбстоном — иначе реверс-синк воскресил бы оплаченный+возвращённый
        # оригинал как новую накладную (наша строка на него больше не ссылается).
        invoice.iiko_correction_new_external_id = outcome.document_id
        if original_external_id and not await _tombstone_exists(
            session, inv_source, original_external_id
        ):
            session.add(
                SupplierInvoiceTombstone(
                    source=inv_source,
                    external_id=original_external_id,
                    reason="коррекция оплаченной накладной — заменена новой приходной в iiko",
                )
            )
        invoice.external_id = outcome.document_id  # перецеп X→Y
        if not outcome.posted:
            invoice.iiko_return_status = "failed"
            invoice.iiko_return_error = (
                outcome.error or "iiko: новая приходная не проведена"
            )[:500]
            await session.commit()
            return invoice
        # Шаг 2 успешен → контур завершён. Y остаётся НЕОПЛАЧЕННОЙ намеренно: возврат создал долг
        # поставщика на всю старую сумму, новая приходная — на правильную; чистый баланс = дебиторка
        # (старая − новая). Оплачивать Y зачётом НЕЛЬЗЯ — settle-дебет завышает баланс поставщика
        # (проверено на живом API). Переплата покрыта у нас (SupplierPrepayment).
        invoice.iiko_return_status = "booked"
        invoice.iiko_return_lines = []
        invoice.iiko_return_error = None
        await session.commit()
        return invoice

    # Ретрай, где шаг 2 уже сделан ранее (new_doc есть): остаётся зафиксировать завершение контура.
    invoice.iiko_return_status = "booked"
    invoice.iiko_return_lines = []
    invoice.iiko_return_error = None
    await session.commit()
    return invoice

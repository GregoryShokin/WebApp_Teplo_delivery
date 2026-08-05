"""Подтверждение проработки и автоматическое списание товара актом в iiko.

ЗАЧЕМ ЭТОТ КОНТУР. Расход товара проработки признаётся АКТОМ СПИСАНИЯ (решение владельца
05.08.2026), а закупка по накладной осталась справочной. Пока акт делал управляющий руками,
было два одинаково плохих исхода: списал — расход считался дважды (по накладной и по акту),
не списал — не считался вовсе. Теперь вопрос задаёт система, а акт создаёт сама.

ЧЕЛОВЕК ОТВЕЧАЕТ НА ОДИН ВОПРОС. «Определять товары на проработки система уже умеет» —
кандидатов приносит автоматика разметки (``PnlProductMonthlyDecision`` со статусом workup),
человеку остаётся подтвердить или отклонить. Отклонение — не ошибка, а нормальный ответ:
товар вернётся в обычную разметку и получит постоянную статью.

АКТ СРАЗУ ПРОВОДИТСЯ — решение владельца 05.08.2026. ``create`` рождает документ в состоянии
NEW, а строка ОПиУ «Списание продукции и сырья» берёт только проведённые: непроведённый акт
не даёт расхода, то есть подтверждение проработки не доводило бы дело до прибыли. Поэтому за
созданием идёт ``post``, и подтверждение — действительно один клик.

ДВА ВЫЗОВА — ДВА ГЕЙТА. Между «создан» и «проведён» отдельный сетевой поход, и упасть может
именно он. Поэтому состояние хранится двумя признаками: ``writeoff_document_id`` и
``writeoff_posted``. Повтор после сбоя проводит СУЩЕСТВУЮЩИЙ документ, а не создаёт второй, —
иначе на складе iiko оказалось бы два списания одного товара.
"""

from __future__ import annotations

import json
import urllib.error
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Counterparty, IikoProduct, InvoiceLineItem, SupplierInvoice
from app.models.pnl import (
    PnlIikoProductObservation,
    PnlProductMonthlyDecision,
    PnlWorkupReview,
)
from app.services.iiko_cloud_client import iiko_cloud_call
from app.services.iiko_location import get_organization_id

_MSK = ZoneInfo("Europe/Moscow")
ZERO_GUID = "00000000-0000-0000-0000-000000000000"

CREATE_PATH = "/api/inventory/v1/writeoff_document/create"
POST_PATH = "/api/inventory/v1/writeoff_document/post"

#: Реквизиты списания на проде — подсмотрены у реальных актов (проба 05.08.2026).
#: В настройки не выносим, пока склад один: лишний экран настроек без второго склада только
#: добавляет способ ошибиться.
DEFAULT_STORE = "8a55c561-2598-4150-ac28-1b03cf8a1835"
DEFAULT_EXPENSE_ACCOUNT = "97036ddb-b2e1-cd47-1669-c145daa9f9c5"

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_REJECTED = "rejected"


class WorkupReviewError(ValueError):
    """Ответ на вопрос невозможен: не тот статус, нет данных для акта."""


def format_iiko_datetime(moment: datetime) -> str:
    """ISO-8601 с точкой перед миллисекундами — формат, который принимает Cloud."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_MSK)
    base = moment.strftime("%Y-%m-%dT%H:%M:%S")
    millis = f"{moment.microsecond // 1000:03d}"
    offset = moment.strftime("%z")
    offset = f"{offset[:3]}:{offset[3:]}" if offset else "+03:00"
    return f"{base}.{millis}{offset}"


def writeoff_moment(day: date) -> datetime:
    """Момент акта — ПОЛДЕНЬ выбранного дня, а не «сейчас».

    iiko сдвигает присланное время на −3 часа (проверено на живом API: отправили 22:07+03:00,
    получили обратно 19:07+03:00). Акт, созданный вечером, уехал бы на предыдущие сутки, а на
    границе месяца — в предыдущий месяц, где ему делать нечего. Полдень безопасен при любом
    часе подтверждения.
    """
    return datetime.combine(day, time(12, 0), tzinfo=_MSK)


@dataclass(slots=True)
class WorkupReviewRow:
    """Строка очереди для экрана."""

    id: uuid.UUID
    period_month: date
    product_guid: str
    product_name: str
    purchase_amount: Decimal
    quantity: Decimal | None
    status: str
    invoice_id: uuid.UUID | None
    invoice_number: str | None
    supplier_name: str | None
    writeoff_document_id: str | None
    writeoff_number: str | None
    writeoff_posted: bool
    writeoff_error: str | None
    decided_at: datetime | None


async def build_review_queue(session: AsyncSession, month_start: date) -> int:
    """Завести вопросы по товарам, которые автоматика отнесла к проработке.

    Идемпотентно: строка на (месяц, товар) одна, уже отвеченные не трогаются — иначе ответ
    человека затирался бы каждой ночной синхронизацией.
    """
    workups = (
        await session.execute(
            select(
                PnlProductMonthlyDecision.iiko_product_guid,
                PnlIikoProductObservation.product_name,
                PnlIikoProductObservation.amount,
            )
            .join(
                PnlIikoProductObservation,
                (PnlIikoProductObservation.period_month == PnlProductMonthlyDecision.period_month)
                & (
                    PnlIikoProductObservation.iiko_product_guid
                    == PnlProductMonthlyDecision.iiko_product_guid
                )
                & (PnlIikoProductObservation.source_kind == PnlProductMonthlyDecision.source_kind),
            )
            .where(
                PnlProductMonthlyDecision.period_month == month_start,
                PnlProductMonthlyDecision.decision_kind == "workup",
            )
        )
    ).all()
    if not workups:
        return 0

    known = set(
        (
            await session.execute(
                select(PnlWorkupReview.iiko_product_guid).where(
                    PnlWorkupReview.period_month == month_start
                )
            )
        ).scalars()
    )
    created = 0
    for guid, product_name, amount in workups:
        if guid in known:
            continue
        invoice_id, quantity = await _find_invoice_line(session, guid, month_start)
        session.add(
            PnlWorkupReview(
                period_month=month_start,
                iiko_product_guid=guid,
                product_name=product_name,
                invoice_id=invoice_id,
                purchase_amount=amount,
                quantity=quantity,
                status=STATUS_PENDING,
            )
        )
        created += 1
    await session.flush()
    return created


async def _find_invoice_line(
    session: AsyncSession, product_guid: str, month_start: date
) -> tuple[uuid.UUID | None, Decimal | None]:
    """Накладная и количество товара — чтобы человек видел, откуда вопрос.

    Ищем по номенклатуре среди накладных месяца. Не нашли — не беда: товар мог прийти
    документом, заведённым прямо в iiko, и вопрос всё равно осмыслен.
    """
    row = (
        await session.execute(
            select(InvoiceLineItem.invoice_id, InvoiceLineItem.quantity)
            .join(SupplierInvoice, SupplierInvoice.id == InvoiceLineItem.invoice_id)
            .where(
                InvoiceLineItem.product_guid == product_guid,
                SupplierInvoice.invoice_date >= month_start,
            )
            .order_by(SupplierInvoice.invoice_date)
            .limit(1)
        )
    ).first()
    return (row[0], row[1]) if row else (None, None)


async def list_reviews(
    session: AsyncSession, month_start: date, *, only_pending: bool = False
) -> list[WorkupReviewRow]:
    """Очередь месяца для экрана — с номером накладной и поставщиком, если они нашлись."""
    query = (
        select(PnlWorkupReview, SupplierInvoice, Counterparty)
        .outerjoin(SupplierInvoice, SupplierInvoice.id == PnlWorkupReview.invoice_id)
        .outerjoin(Counterparty, Counterparty.id == SupplierInvoice.counterparty_id)
        .where(PnlWorkupReview.period_month == month_start)
        .order_by(PnlWorkupReview.status, PnlWorkupReview.product_name)
    )
    if only_pending:
        query = query.where(PnlWorkupReview.status == STATUS_PENDING)
    rows = (await session.execute(query)).all()
    return [
        WorkupReviewRow(
            id=review.id,
            period_month=review.period_month,
            product_guid=review.iiko_product_guid,
            product_name=review.product_name or review.iiko_product_guid,
            purchase_amount=review.purchase_amount,
            quantity=review.quantity,
            status=review.status,
            invoice_id=review.invoice_id,
            invoice_number=(invoice.number if invoice is not None else None),
            supplier_name=(supplier.name if supplier is not None else None),
            writeoff_document_id=review.writeoff_document_id,
            writeoff_number=review.writeoff_number,
            writeoff_posted=review.writeoff_posted,
            writeoff_error=review.writeoff_error,
            decided_at=review.decided_at,
        )
        for review, invoice, supplier in rows
    ]


async def _product_unit_guid(session: AsyncSession, product_guid: str) -> str | None:
    return await session.scalar(
        select(IikoProduct.main_unit_guid).where(IikoProduct.iiko_id == product_guid)
    )


def build_writeoff_body(
    *,
    organization_id: str,
    product_guid: str,
    unit_guid: str,
    quantity: Decimal,
    moment: datetime,
    comment: str,
    store: str = DEFAULT_STORE,
    expense_account: str = DEFAULT_EXPENSE_ACCOUNT,
) -> dict:
    """Тело ``writeoff_document/create``.

    ``amountUnit`` и ``containerId`` схема API объявляет необязательными, но без них create
    отвечает HTTP 500 «Exception while deserializing object WriteoffDocument» — проверено на
    живом API 05.08.2026. Поэтому они здесь обязательны по построению, а не по желанию.
    """
    return {
        "organizationId": organization_id,
        "date": format_iiko_datetime(moment),
        "storeFrom": store,
        "expenseAccount": expense_account,
        "comment": comment,
        "items": [
            {
                "num": 1,
                "product": product_guid,
                "amount": float(quantity),
                "amountUnit": unit_guid,
                "containerId": ZERO_GUID,
            }
        ],
    }


async def confirm_workup(
    session: AsyncSession,
    review_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None,
    create_act: bool = True,
) -> WorkupReviewRow:
    """«Да, проработка»: пометить решение, списать товар актом и провести акт.

    ПОВТОР НЕ СОЗДАЁТ ВТОРОЙ АКТ И НЕ ПРОВОДИТ ДВАЖДЫ. Cloud ``create`` не идемпотентен, поэтому
    гейтов два: ``writeoff_document_id`` не даёт создать документ заново, ``writeoff_posted`` —
    провести уже проведённый. Двойной клик и ретрай после сбоя доводят до конца ту же работу,
    а не начинают новую.

    ДОВЕДЕНИЕ ДО КОНЦА ВАЖНЕЕ КРАСОТЫ. Если в прошлый раз документ создался, но не провёлся,
    повтор проведёт именно его: непроведённый акт расхода не даёт, и подтверждение проработки
    осталось бы без последствий для прибыли.

    ОШИБКА iiko НЕ ОТМЕНЯЕТ РЕШЕНИЕ ЧЕЛОВЕКА. Ответ сохраняется в любом случае, а отказ внешней
    системы ложится в ``writeoff_error`` — иначе пришлось бы спрашивать заново, а расход так и
    остался бы непосчитанным.
    """
    review = await session.get(PnlWorkupReview, review_id)
    if review is None:
        raise WorkupReviewError("Вопрос не найден")
    if review.status == STATUS_REJECTED:
        raise WorkupReviewError("Ответ уже дан: товар отмечен как не проработка")

    review.status = STATUS_CONFIRMED
    review.decided_by_user_id = user_id
    review.decided_at = datetime.now(UTC)

    if create_act:
        if review.writeoff_document_id is None:
            await _create_act(session, review)
        if review.writeoff_document_id and not review.writeoff_posted:
            await _post_act(session, review)

    await session.commit()
    rows = await list_reviews(session, review.period_month)
    return next(row for row in rows if row.id == review_id)


async def _post_act(session: AsyncSession, review: PnlWorkupReview) -> None:
    """Провести созданный акт: только проведённый документ даёт расход в ОПиУ.

    Проведение меняет складские остатки боевой iiko, поэтому вызывается ровно один раз на
    документ — гейт ``writeoff_posted``. Отказ не откатывает создание: документ остаётся, и
    повторное подтверждение проведёт его же.
    """
    body = {
        "organizationId": get_organization_id(),
        "documentId": review.writeoff_document_id,
    }
    try:
        status, response = await anyio.to_thread.run_sync(lambda: iiko_cloud_call(POST_PATH, body))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as error:
        review.writeoff_error = (
            f"Акт {review.writeoff_number or review.writeoff_document_id} создан, но не проведён "
            f"— iiko недоступен: {error}. Расход появится после повторного подтверждения"
        )
        return
    if status not in (200, 201):
        review.writeoff_error = (
            f"Акт {review.writeoff_number or review.writeoff_document_id} создан, но не проведён "
            f"(HTTP {status}): {str(response)[:300]}. В расход он попадёт только проведённым"
        )
        return
    review.writeoff_posted = True
    review.writeoff_error = None


async def _create_act(session: AsyncSession, review: PnlWorkupReview) -> None:
    quantity = review.quantity
    if quantity is None or quantity <= 0:
        review.writeoff_error = (
            "Количество неизвестно: товар не найден в позициях накладных за месяц. "
            "Спишите акт в iiko вручную или заведите накладную"
        )
        return
    unit_guid = await _product_unit_guid(session, review.iiko_product_guid)
    if not unit_guid:
        review.writeoff_error = (
            "У товара нет единицы измерения в справочнике iiko — без неё акт списания "
            "не создаётся. Обновите номенклатуру и повторите"
        )
        return

    organization_id = get_organization_id()
    body = build_writeoff_body(
        organization_id=organization_id,
        product_guid=review.iiko_product_guid,
        unit_guid=unit_guid,
        quantity=quantity,
        moment=writeoff_moment(review.period_month),
        comment=f"Проработка: {review.product_name or review.iiko_product_guid}",
    )
    try:
        status, response = await anyio.to_thread.run_sync(
            lambda: iiko_cloud_call(CREATE_PATH, body)
        )
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as error:
        # Никогда не роняем ответ пользователю из-за внешней системы: решение уже принято,
        # а недоступность iiko — повод повторить, а не потерять ответ.
        review.writeoff_error = f"iiko недоступен: {error}"
        return

    if status not in (200, 201) or not isinstance(response, dict):
        review.writeoff_error = f"iiko отклонил акт (HTTP {status}): {str(response)[:400]}"
        return
    document_id = response.get("documentId")
    if not document_id:
        review.writeoff_error = f"iiko не вернул идентификатор акта: {str(response)[:400]}"
        return
    review.writeoff_document_id = document_id
    review.writeoff_number = response.get("documentNumber")
    review.writeoff_error = None


async def reject_workup(
    session: AsyncSession, review_id: uuid.UUID, *, user_id: uuid.UUID | None
) -> WorkupReviewRow:
    """«Нет, не проработка»: снять месячное решение, чтобы товар получил постоянную статью.

    Отклонение удаляет ``PnlProductMonthlyDecision``: пока оно живо, автоматика считает вопрос
    решённым и товар не попадёт ни в разметку, ни к следующему кандидату на постоянную статью.
    """
    review = await session.get(PnlWorkupReview, review_id)
    if review is None:
        raise WorkupReviewError("Вопрос не найден")
    if review.writeoff_document_id:
        # Отменить документ за человека нельзя: проведённый акт уже изменил складские остатки,
        # непроведённый всё равно висит в iiko. Решение об отмене принимает тот, кто видит склад.
        state = "проведён" if review.writeoff_posted else "создан"
        raise WorkupReviewError(
            f"Акт списания уже {state} "
            f"({review.writeoff_number or review.writeoff_document_id}). "
            "Отмените его в iiko, прежде чем менять ответ"
        )

    review.status = STATUS_REJECTED
    review.decided_by_user_id = user_id
    review.decided_at = datetime.now(UTC)

    decision = await session.scalar(
        select(PnlProductMonthlyDecision).where(
            PnlProductMonthlyDecision.period_month == review.period_month,
            PnlProductMonthlyDecision.iiko_product_guid == review.iiko_product_guid,
        )
    )
    if decision is not None:
        await session.delete(decision)
    await session.commit()
    rows = await list_reviews(session, review.period_month)
    return next(row for row in rows if row.id == review_id)

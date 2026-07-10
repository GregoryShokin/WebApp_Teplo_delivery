"""Единый агрегатор исходящих платежей «платёжного контура» (FEAT-003).

Собирает в один нормализованный список платежи из трёх источников платёжного
контура (без авансов/выплат сотрудникам и депозитов — те живут на своих страницах):

1. **Счета на оплату** — почтовые счета «Страницы на оплату» (``EmailInvoiceIntake``
   + материализованный ``SupplierInvoice``). Оплата — банковским черновиком.
2. **«Новый платёж» (FAB)** — банковские черновики ``CounterpartyPaymentDraft``
   свободного вывода на Сейф (``pays_via_safe``, транш ``ExpenseDraftLine``),
   предоплаты поставщику (``creates_prepayment``) и выплаты неофициальному
   поставщику через Сейф. Обычные черновики оплаты накладных сюда НЕ входят.
3. **Резервы Сейфа/Кассы** — ``SafeAllocation`` платёжного происхождения
   (``employee_id IS NULL`` — зарплатные целёвки исключены): ``location='safe'``
   (на карте Сейф) и ``location='kassa'`` (передан в кассу, «К выдаче»).

Нормализованное состояние и раскладка по 4 корзинам активной модалки FAB:

* ``to_review``     — счёт распознан, но не подтверждён (нужно «Разобрать»).
* ``bank_ready``    — «Готовы к отправке в банк»: реквизиты сверены, черновик ещё
  не создан → доступна кнопка «В банк».
* ``to_pay``        — «Готовы к оплате»: банковский черновик уже создан и ждёт
  оплаты/подписи владельцем в банке.
* ``reserved_safe`` — «На Сейфе»: резерв на карте Сейф, ждёт выплаты.
* ``reserved_kassa``— «В кассе»: передан в кассу, ждёт выдачи наличными.

Терминальные состояния (``paid``/``failed``/``cancelled``/``deleted``) корзины не
имеют — они видны только в истории (``scope='all'``) на странице «Финансы → Платежи».

Модуль ТОЛЬКО читает — никаких мутаций и движений денег.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Counterparty,
    CounterpartyPayableProfile,
    CounterpartyPaymentDraft,
    DdsArticle,
    EmailInvoiceIntake,
    SafeAllocation,
    SupplierInvoice,
)

# --- нормализованные состояния и корзины -------------------------------------

STATE_LABELS: dict[str, str] = {
    "to_review": "Требует разбора",
    "ready_to_send": "Готов к отправке в банк",
    "in_bank": "Отправлен в банк",
    "reserved_safe": "Зарезервирован на Сейфе",
    "reserved_kassa": "В кассе, к выдаче",
    "paid": "Оплачен",
    "partially_paid": "Оплачен частично",
    "failed": "Ошибка банка",
    "cancelled": "Отменён",
    "deleted": "Удалён",
}

# Состояние → корзина активной модалки (None — только история).
BUCKET_BY_STATE: dict[str, str | None] = {
    "to_review": "to_review",
    "ready_to_send": "bank_ready",
    "in_bank": "to_pay",
    "reserved_safe": "reserved_safe",
    "reserved_kassa": "reserved_kassa",
    "partially_paid": "reserved_safe",  # уточняется по location ниже
    "paid": None,
    "failed": None,
    "cancelled": None,
    "deleted": None,
}

# Порядок корзин в модалке.
BUCKET_ORDER = ["to_review", "to_pay", "bank_ready", "reserved_safe", "reserved_kassa"]
BUCKET_LABELS: dict[str, str] = {
    "to_review": "Требуют разбора",
    "to_pay": "Отправлен в банк",
    "bank_ready": "Готовы к отправке в банк",
    "reserved_safe": "На Сейфе",
    "reserved_kassa": "В кассе",
}

_ACTIVE_STATES = {
    "to_review",
    "ready_to_send",
    "in_bank",
    "reserved_safe",
    "reserved_kassa",
    "partially_paid",
}


@dataclass
class PaymentItem:
    """Нормализованная строка платежа для витрины (модалка + страница истории)."""

    id: str  # композитный: f"{source}:{uuid}"
    source: str  # 'invoice' | 'draft' | 'reserve'
    kind: str  # 'invoice' | 'expense' | 'prepayment' | 'informal' | 'safe_reserve' | 'kassa_reserve'
    ref_id: uuid.UUID
    title: str
    counterparty_id: uuid.UUID | None
    counterparty_name: str | None
    amount: Decimal
    amount_paid: Decimal | None
    article_id: uuid.UUID | None
    article_name: str | None
    method: str  # 'bank' | 'cash'
    bank_channel: str | None  # 'tbank' | 'sber'
    state: str
    bucket: str | None
    created_at: datetime
    # флаги действий (какие кнопки показывать)
    can_edit: bool = False
    can_send_to_bank: bool = False
    can_pay: bool = False
    can_cancel: bool = False
    # доп-контекст
    extra: dict = field(default_factory=dict)

    @property
    def state_label(self) -> str:
        return STATE_LABELS.get(self.state, self.state)

    @property
    def bucket_label(self) -> str | None:
        return BUCKET_LABELS.get(self.bucket) if self.bucket else None


async def _article_names(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    rows = (
        await session.execute(select(DdsArticle.id, DdsArticle.name).where(DdsArticle.id.in_(ids)))
    ).all()
    return {rid: name for rid, name in rows}


async def _counterparty_names(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(Counterparty.id, Counterparty.name).where(Counterparty.id.in_(ids))
        )
    ).all()
    return {rid: name for rid, name in rows}


# --- источник 1: почтовые счета «Страницы на оплату» ---------------------------


async def _invoice_items(session: AsyncSession) -> list[PaymentItem]:
    stmt = (
        select(
            EmailInvoiceIntake,
            Counterparty.name,
            CounterpartyPayableProfile.requisites_verified,
            SupplierInvoice.payment_status,
            SupplierInvoice.draft_id,
            SupplierInvoice.dds_article_id,
            SupplierInvoice.amount,
        )
        .outerjoin(Counterparty, Counterparty.id == EmailInvoiceIntake.counterparty_id)
        .outerjoin(
            CounterpartyPayableProfile,
            CounterpartyPayableProfile.counterparty_id == EmailInvoiceIntake.counterparty_id,
        )
        .outerjoin(SupplierInvoice, SupplierInvoice.id == EmailInvoiceIntake.invoice_id)
        .order_by(EmailInvoiceIntake.created_at.desc())
    )
    rows = (await session.execute(stmt)).all()
    article_ids = {r[5] for r in rows if r[5] is not None}
    article_names = await _article_names(session, article_ids)

    items: list[PaymentItem] = []
    for intake, cp_name, verified, pay_status, draft_id, article_id, inv_amount in rows:
        # Скрытые/служебные записи журнала разбора не относятся к активным платежам.
        if intake.status in ("ignored", "duplicate", "excluded"):
            state = "cancelled"
        elif pay_status == "paid":
            state = "paid"
        elif draft_id is not None:
            state = "in_bank"
        elif intake.status == "linked" and verified:
            state = "ready_to_send"
        else:
            state = "to_review"

        rec = intake.recognition or {}
        amount = inv_amount
        if amount is None:
            raw = rec.get("amount")
            try:
                amount = Decimal(str(raw)) if raw is not None else Decimal(0)
            except (ArithmeticError, ValueError):
                amount = Decimal(0)
        title = cp_name or rec.get("recipient_name") or intake.subject or "Счёт на оплату"
        items.append(
            PaymentItem(
                id=f"invoice:{intake.id}",
                source="invoice",
                kind="invoice",
                ref_id=intake.id,
                title=title,
                counterparty_id=intake.counterparty_id,
                counterparty_name=cp_name,
                amount=amount or Decimal(0),
                amount_paid=None,
                article_id=article_id,
                article_name=article_names.get(article_id) if article_id else None,
                method="bank",
                bank_channel="tbank",
                state=state,
                bucket=BUCKET_BY_STATE.get(state),
                created_at=intake.created_at,
                can_edit=state in ("to_review", "ready_to_send"),
                can_send_to_bank=state == "ready_to_send",
                can_pay=False,
                can_cancel=state in ("to_review", "ready_to_send"),
                extra={
                    "intake_status": intake.status,
                    "invoice_id": str(intake.invoice_id) if intake.invoice_id else None,
                    "requisites_verified": bool(verified),
                    "number": rec.get("invoice_number"),
                },
            )
        )
    return items


# --- источник 2: банковские черновики «Нового платежа» -------------------------


async def _draft_items(session: AsyncSession) -> list[PaymentItem]:
    # Только платёжный контур FAB: свободный вывод на Сейф и предоплата/informal.
    # Обычные черновики оплаты накладных (не via-safe, не prepayment) исключены —
    # они относятся к контуру «Накладных», а не «Платежей».
    stmt = (
        select(CounterpartyPaymentDraft)
        .where(
            (CounterpartyPaymentDraft.pays_via_safe.is_(True))
            | (CounterpartyPaymentDraft.creates_prepayment.is_(True))
        )
        .order_by(CounterpartyPaymentDraft.created_at.desc())
    )
    drafts = (await session.execute(stmt)).scalars().all()

    cp_ids = {d.counterparty_id for d in drafts if d.counterparty_id is not None}
    art_ids = {d.target_article_id for d in drafts if d.target_article_id is not None}
    art_ids |= {d.prepayment_article_id for d in drafts if d.prepayment_article_id is not None}
    cp_names = await _counterparty_names(session, cp_ids)
    art_names = await _article_names(session, art_ids)

    items: list[PaymentItem] = []
    for d in drafts:
        if d.creates_prepayment:
            kind = "prepayment"
            article_id = d.prepayment_article_id
        elif d.counterparty_id is not None:
            kind = "informal"  # выплата неофициальному поставщику через Сейф
            article_id = d.target_article_id
        else:
            kind = "expense"  # свободный вывод на Сейф «просто трата»
            article_id = d.target_article_id

        status_map = {
            "created": "in_bank",
            "updated": "in_bank",
            "paid": "paid",
            "failed": "failed",
            "deleted": "deleted",
        }
        state = status_map.get(d.status, d.status)

        cp_name = cp_names.get(d.counterparty_id) if d.counterparty_id else None
        title = cp_name or d.target_purpose or (
            art_names.get(article_id) if article_id else None
        ) or "Свободный вывод на Сейф"
        items.append(
            PaymentItem(
                id=f"draft:{d.id}",
                source="draft",
                kind=kind,
                ref_id=d.id,
                title=title,
                counterparty_id=d.counterparty_id,
                counterparty_name=cp_name,
                amount=d.amount,
                amount_paid=None,
                article_id=article_id,
                article_name=art_names.get(article_id) if article_id else None,
                method="bank",
                bank_channel=d.bank_provider,
                state=state,
                bucket=BUCKET_BY_STATE.get(state),
                created_at=d.created_at,
                can_edit=False,
                can_send_to_bank=False,
                can_pay=False,
                can_cancel=state == "in_bank",
                extra={
                    "document_id": d.document_id,
                    "last_error": d.last_error,
                    "pays_via_safe": d.pays_via_safe,
                    "creates_prepayment": d.creates_prepayment,
                },
            )
        )
    return items


# --- источник 3: резервы Сейфа/Кассы ------------------------------------------


async def _reserve_items(session: AsyncSession) -> list[PaymentItem]:
    # Платёжный контур: исключаем зарплатные целёвки (employee_id задан).
    stmt = (
        select(SafeAllocation)
        .where(SafeAllocation.employee_id.is_(None))
        .order_by(SafeAllocation.created_at.desc())
    )
    reserves = (await session.execute(stmt)).scalars().all()

    cp_ids = {r.counterparty_id for r in reserves if r.counterparty_id is not None}
    art_ids = {r.article_id for r in reserves if r.article_id is not None}
    cp_names = await _counterparty_names(session, cp_ids)
    art_names = await _article_names(session, art_ids)

    items: list[PaymentItem] = []
    for r in reserves:
        if r.status == "cancelled":
            state = "cancelled"
        elif r.status == "paid":
            state = "paid"
        elif r.location == "kassa":
            state = "reserved_kassa"
        elif r.status == "partially_paid":
            state = "reserved_safe"
        else:
            state = "reserved_safe"

        cp_name = cp_names.get(r.counterparty_id) if r.counterparty_id else None
        article_name = art_names.get(r.article_id) if r.article_id else None
        title = cp_name or r.purpose or article_name or "Резерв Сейфа"
        items.append(
            PaymentItem(
                id=f"reserve:{r.id}",
                source="reserve",
                kind="kassa_reserve" if r.location == "kassa" else "safe_reserve",
                ref_id=r.id,
                title=title,
                counterparty_id=r.counterparty_id,
                counterparty_name=cp_name,
                amount=r.amount,
                amount_paid=r.amount_paid,
                article_id=r.article_id,
                article_name=article_name,
                method="cash",
                bank_channel=None,
                state=state,
                bucket=BUCKET_BY_STATE.get(state),
                created_at=r.created_at,
                can_edit=False,
                can_send_to_bank=False,
                can_pay=state in ("reserved_safe", "reserved_kassa"),
                can_cancel=state in ("reserved_safe", "reserved_kassa"),
                extra={
                    "location": r.location,
                    "wallet_id": str(r.wallet_id),
                },
            )
        )
    return items


# --- публичный API -------------------------------------------------------------


async def list_payments(session: AsyncSession, *, scope: str = "active") -> list[PaymentItem]:
    """Собрать нормализованный список платежей платёжного контура.

    ``scope='active'`` — только незакрытые платежи (есть корзина), для модалки FAB.
    ``scope='all'``    — вся история, для страницы «Финансы → Платежи».
    """
    invoices = await _invoice_items(session)
    drafts = await _draft_items(session)
    reserves = await _reserve_items(session)
    items = invoices + drafts + reserves

    if scope == "active":
        items = [i for i in items if i.state in _ACTIVE_STATES]

    items.sort(key=lambda i: i.created_at, reverse=True)
    return items


def bucket_counts(items: list[PaymentItem]) -> dict[str, int]:
    counts = {b: 0 for b in BUCKET_ORDER}
    for i in items:
        if i.bucket in counts:
            counts[i.bucket] += 1
    return counts

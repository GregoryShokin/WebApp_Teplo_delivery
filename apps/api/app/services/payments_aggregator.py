"""Единый агрегатор исходящих платежей «платёжного контура» (FEAT-003).

Собирает в один нормализованный список платежи из источников платёжного контура
(без авансов/выплат сотрудникам и депозитов — те живут на своих страницах):

1. **Счета на оплату** — почтовые счета «Страницы на оплату» (``EmailInvoiceIntake``
   + материализованный ``SupplierInvoice``). Оплата — банковским черновиком.
2. **«Новый платёж» (FAB)** — банковские черновики ``CounterpartyPaymentDraft``:
   свободный вывод на Сейф (``pays_via_safe``, транш ``ExpenseDraftLine``), прямые
   расходы официальным контрагентам по реквизитам, предоплаты поставщику
   (``creates_prepayment``) и выплаты неофициальному поставщику через Сейф.
3. **Резервы Сейфа/Кассы** — ``SafeAllocation``: платёжные целёвки-получатели
   (``employee_id IS NULL``, ``source_run_id IS NULL``) и **пул-резервы выплаты ЗП**,
   привязанные к ведомости (``source_run_id`` задан, ``kind='payroll_reserve'`` —
   клик открывает окно ведомости). ``location='safe'``/``'kassa'``.
4. **Банк-черновики выплаты ЗП** — ``PayrollBankDraft`` в статусе «Отправлен в банк»;
   удалённый в банке черновик возвращается в «Готовы к отправке». После оплаты выполняется
   транзит на Сейф-резерв, а черновик уходит в историю.

Нормализованное состояние и раскладка по 4 корзинам активной модалки FAB:

* ``to_review``     — счёт распознан, но не подтверждён (нужно «Разобрать»).
* ``bank_ready``    — «Готовы к отправке в банк»: реквизиты сверены, черновик ещё
  не создан → доступна кнопка «В банк».
* ``to_pay``        — «Готовы к оплате»: банковский черновик уже создан и ждёт
  оплаты/подписи владельцем в банке.
* ``reserved_safe`` — «На Сейфе»: резерв на карте Сейф, ждёт выплаты.
* ``reserved_kassa``— «В кассе»: передан в кассу, ждёт выдачи наличными.

Терминальные состояния (``paid``/``failed``/``cancelled``) корзины не имеют — они видны
только в истории (``scope='all'``) на странице «Финансы → Платежи». Для зарплатного
черновика ``deleted`` возвращается в ``bank_ready``, пока ведомость не выплачена.

Модуль ТОЛЬКО читает — никаких мутаций и движений денег.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Counterparty,
    CounterpartyPayableProfile,
    CounterpartyPaymentDraft,
    DdsArticle,
    EmailInvoiceIntake,
    PayrollBankDraft,
    PayrollLine,
    PayrollPayment,
    PayrollRun,
    SafeAllocation,
    SupplierInvoice,
)
from app.services.payroll_reserves import (
    PAYROLL_RESERVE_LABEL_ADMIN,
    PAYROLL_RESERVE_LABEL_PRODUCTION,
)


def _fmt_money(value: Decimal) -> str:
    """Целые рубли, разряды пробелом — единообразно с суммой в списке (без копеек)."""
    whole = value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"{whole:,.0f} ₽".replace(",", " ")


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
    source: str  # 'invoice' | 'draft' | 'reserve' | 'payroll_draft'
    # 'invoice'|'expense'|'prepayment'|'informal'|'safe_reserve'|'kassa_reserve'
    # |'payroll_reserve'|'payroll_bank_draft'
    kind: str
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
    # Только платёжный контур FAB: свободный расход (через Сейф или напрямую официальному
    # контрагенту по реквизитам) и предоплата/informal.
    # Обычные черновики оплаты накладных (не via-safe, не prepayment) исключены —
    # они относятся к контуру «Накладных», а не «Платежей».
    stmt = (
        select(CounterpartyPaymentDraft)
        .where(
            (CounterpartyPaymentDraft.pays_via_safe.is_(True))
            | (CounterpartyPaymentDraft.creates_prepayment.is_(True))
            | (CounterpartyPaymentDraft.target_article_id.is_not(None))
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
            # pays_via_safe=True — выплата неофициальному поставщику; False — прямой
            # банковский расход официальному контрагенту по реквизитам.
            kind = "informal" if d.pays_via_safe else "expense"
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
        title = (
            cp_name
            or d.target_purpose
            or (art_names.get(article_id) if article_id else None)
            or "Свободный вывод на Сейф"
        )
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
        # Резерв-контейнер выплаты ЗП (привязан к ведомости): «Выплата зарплаты… · сумма»,
        # клик открывает список сотрудников ведомости (kind='payroll_reserve').
        is_payroll = r.source_run_id is not None
        if is_payroll:
            title = f"{r.purpose or 'Выплата зарплаты'} · {_fmt_money(Decimal(r.amount))}"
            kind = "payroll_reserve"
        else:
            title = cp_name or r.purpose or article_name or "Резерв Сейфа"
            kind = "kassa_reserve" if r.location == "kassa" else "safe_reserve"
        extra: dict = {"location": r.location, "wallet_id": str(r.wallet_id)}
        if is_payroll:
            extra["run_id"] = str(r.source_run_id)
            extra["payroll"] = True
        items.append(
            PaymentItem(
                id=f"reserve:{r.id}",
                source="reserve",
                kind=kind,
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
                # ЗП-резервы отменяются через дефинализацию ведомости, не из «Платежей».
                can_cancel=(not is_payroll) and state in ("reserved_safe", "reserved_kassa"),
                extra=extra,
            )
        )
    return items


async def _payroll_bank_draft_items(session: AsyncSession) -> list[PaymentItem]:
    """Банк-черновики выплаты ЗП — «Отправлен в банк» или «Готов к отправке».

    После оплаты (``paid``) деньги транзитом уходят на Сейф и представлены Сейф-резервом —
    оплаченный черновик показываем только в истории. Удалённый черновик можно отправить
    повторно, если ведомость финализирована и ещё не выплачена.
    """
    rows = (
        await session.execute(
            select(PayrollBankDraft, PayrollRun.summary, PayrollRun.status).join(
                PayrollRun, PayrollRun.id == PayrollBankDraft.run_id
            )
        )
    ).all()

    # Полностью ли выплачена ведомость (Σ выплат ≥ ФОТ): у таких черновик мог застрять в
    # created/updated (оплата прошла иным путём, вебхук не перевёл в paid) — он историчен.
    run_ids = {draft.run_id for draft, _s, _rs in rows}
    payable_by_run: dict = {}
    paid_by_run: dict = {}
    if run_ids:
        payable_by_run = dict(
            (
                await session.execute(
                    select(
                        PayrollLine.run_id,
                        func.coalesce(func.sum(PayrollLine.total_payable), 0),
                    )
                    .where(PayrollLine.run_id.in_(run_ids))
                    .group_by(PayrollLine.run_id)
                )
            ).all()
        )
        paid_by_run = dict(
            (
                await session.execute(
                    select(
                        PayrollPayment.run_id,
                        func.coalesce(func.sum(PayrollPayment.amount), 0),
                    )
                    .where(PayrollPayment.run_id.in_(run_ids))
                    .group_by(PayrollPayment.run_id)
                )
            ).all()
        )

    def _run_fully_paid(run_id: uuid.UUID) -> bool:
        payable = Decimal(str(payable_by_run.get(run_id, 0) or 0))
        paid = Decimal(str(paid_by_run.get(run_id, 0) or 0))
        return payable > 0 and paid >= payable - Decimal("0.01")

    items: list[PaymentItem] = []
    for draft, summary, run_status in rows:
        status_map = {
            "created": "in_bank",
            "updated": "in_bank",
            "paid": "paid",
            "failed": "failed",
            # Удалённый черновик денег не перемещал: пока ведомость не выплачена, возвращаем
            # её в очередь повторной отправки вместо терминальной истории.
            "deleted": "ready_to_send",
        }
        state = status_map.get(draft.status, draft.status)
        # Дефинализированную ведомость нельзя ни считать отправленной, ни повторно отправлять.
        if run_status != "finalized" and state in ("in_bank", "ready_to_send"):
            state = "cancelled"
        # Уже полностью выплаченную ведомость нельзя реанимировать старым удалённым черновиком.
        if state in ("in_bank", "ready_to_send") and _run_fully_paid(draft.run_id):
            state = "paid"
        is_admin = isinstance(summary, dict) and summary.get("kind") == "admin"
        label = PAYROLL_RESERVE_LABEL_ADMIN if is_admin else PAYROLL_RESERVE_LABEL_PRODUCTION
        items.append(
            PaymentItem(
                id=f"payroll_draft:{draft.id}",
                source="payroll_draft",
                kind="payroll_bank_draft",
                ref_id=draft.id,
                title=f"{label} · {_fmt_money(Decimal(draft.amount))}",
                counterparty_id=None,
                counterparty_name=None,
                amount=Decimal(draft.amount),
                amount_paid=None,
                article_id=None,
                article_name=None,
                method="bank",
                bank_channel=draft.bank_provider,
                state=state,
                bucket=BUCKET_BY_STATE.get(state),
                created_at=draft.created_at,
                can_edit=False,
                can_send_to_bank=state == "ready_to_send",
                can_pay=False,  # ждёт оплаты в банке владельцем
                can_cancel=False,
                extra={
                    "run_id": str(draft.run_id),
                    "payroll": True,
                    "last_error": draft.last_error,
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
    payroll_drafts = await _payroll_bank_draft_items(session)
    items = invoices + drafts + reserves + payroll_drafts

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

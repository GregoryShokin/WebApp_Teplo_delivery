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
5. **Разовые выплаты сотрудникам** — ``EmployeePayout`` банковского контура (окно «Новый
   платёж» по зарплатной статье): ``pending`` — «Отправлен в банк». Зеркало депозита/аванса —
   после оплаты деньги транзитом уходят на Сейф и представлены Сейф-резервом.

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

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.models import (
    Account,
    Counterparty,
    CounterpartyPayableProfile,
    CounterpartyPaymentDraft,
    DdsArticle,
    DepositBankDraft,
    EmailInvoiceIntake,
    Employee,
    EmployeePayout,
    PayrollBankDraft,
    PayrollLine,
    PayrollPayment,
    PayrollRun,
    SafeAllocation,
    SalaryAdvance,
    SalaryAdvanceBankDraft,
    SupplierInvoice,
    Wallet,
)
from app.models.tax import TaxBankDraft
from app.services.payroll_reserves import (
    PAYROLL_RESERVE_LABEL_ADMIN,
    PAYROLL_RESERVE_LABEL_PRODUCTION,
)
from app.services.taxes.bank_draft import requisites_for_kind

logger = logging.getLogger(__name__)


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
    # source: invoice|draft|reserve|payroll_draft|deposit_draft|advance_draft|tax_draft
    source: str
    # kind: invoice|expense|prepayment|informal|safe_reserve|kassa_reserve|payroll_reserve
    # |payroll_bank_draft|deposit_bank_draft|advance_bank_draft|tax_enp
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
        # PDF счёта витрине не нужен, а весит сотни килобайт на запись: без defer
        # каждый опрос списка тянул из TOAST все вложения за всю историю почты.
        .options(defer(EmailInvoiceIntake.pdf_bytes))
    )
    rows = (await session.execute(stmt)).all()
    article_ids = {r[5] for r in rows if r[5] is not None}
    article_names = await _article_names(session, article_ids)

    items: list[PaymentItem] = []
    for intake, cp_name, verified, pay_status, draft_id, article_id, inv_amount in rows:
        # Скрытые/служебные записи журнала разбора не относятся к активным платежам. Закрывающий
        # документ (closing: УПД/акт) — не счёт к оплате: он гасит дебиторку / встаёт в кредиторку,
        # живёт в учёте ДЗ/КЗ, а не в очереди оплат.
        if intake.status in ("ignored", "duplicate", "excluded", "closing"):
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
        # Скрываем только черновик, уже представленный строкой *входящего журнала*.
        # SupplierInvoice бывает и у накладной поставщика (овощи): у неё нет строки
        # EmailInvoiceIntake, поэтому черновик — единственная видимая платёжка и скрывать его
        # нельзя. У коммунального счёта есть обе сущности, там вторая строка была дублем.
        .where(
            CounterpartyPaymentDraft.id.not_in(
                select(SupplierInvoice.draft_id)
                .join(EmailInvoiceIntake, EmailInvoiceIntake.invoice_id == SupplierInvoice.id)
                .where(SupplierInvoice.draft_id.is_not(None))
            )
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
    # Депозит-резервы (созданы оплатой банк-черновика выдачи депозита): их нельзя отменять/
    # переносить — это обязательство перед сотрудником, а не свободный резерв Сейфа. Ссылка на
    # получателя живёт в DepositBankDraft (R1: в самом резерве employee_id=None).
    deposit_by_alloc = await _deposit_reserve_names(session)

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
        deposit_name = deposit_by_alloc.get(r.id)
        is_deposit = deposit_name is not None
        extra: dict = {"location": r.location, "wallet_id": str(r.wallet_id)}
        if is_deposit:
            recipient = deposit_name or "получатель"
            title = f"Выдача депозита — {recipient} · {_fmt_money(Decimal(r.amount))}"
            kind = "deposit_reserve"
            extra["deposit"] = True
        elif is_payroll:
            title = f"{r.purpose or 'Выплата зарплаты'} · {_fmt_money(Decimal(r.amount))}"
            kind = "payroll_reserve"
            extra["run_id"] = str(r.source_run_id)
            extra["payroll"] = True
        else:
            title = cp_name or r.purpose or article_name or "Резерв Сейфа"
            kind = "kassa_reserve" if r.location == "kassa" else "safe_reserve"
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
                # ЗП-резервы отменяются через дефинализацию, депозит-резервы — вообще нельзя
                # (обязательство перед сотрудником); остальные целёвки — можно.
                can_cancel=(not is_payroll)
                and (not is_deposit)
                and state in ("reserved_safe", "reserved_kassa"),
                extra=extra,
            )
        )
    return items


async def _deposit_reserve_names(session: AsyncSession) -> dict[uuid.UUID, str | None]:
    """Резервы Сейфа/Кассы, привязанные к банк-черновику выдачи депозита → имя получателя.

    Ключ — ``safe_allocation_id`` черновика, значение — ФИО производственника (курьер — None).
    По этому словарю ``_reserve_items`` метит депозит-резервы (kind='deposit_reserve') и
    запрещает их отмену/перенос.
    """
    rows = (
        await session.execute(
            select(DepositBankDraft.safe_allocation_id, Employee.full_name)
            .outerjoin(Employee, Employee.id == DepositBankDraft.employee_id)
            .where(DepositBankDraft.safe_allocation_id.is_not(None))
        )
    ).all()
    return {alloc_id: name for alloc_id, name in rows}


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


async def _deposit_bank_draft_items(session: AsyncSession) -> list[PaymentItem]:
    """Банк-черновики выдачи депозита — «Отправлен в банк».

    Зеркало ``_payroll_bank_draft_items``. После оплаты (``paid``) деньги транзитом уходят на
    Сейф и представлены депозит-резервом (``kind='deposit_reserve'``) — сам оплаченный черновик
    уходит в историю, чтобы одна выдача не висела двумя строками. Заголовок — с ФИО получателя
    (решение владельца). ``disbursed`` (выдан сотруднику) и терминальные статусы — только история.
    """
    rows = (
        await session.execute(
            select(DepositBankDraft, Employee.full_name).outerjoin(
                Employee, Employee.id == DepositBankDraft.employee_id
            )
        )
    ).all()
    status_map = {
        "created": "in_bank",
        "updated": "in_bank",
        # Оплачен — активную строку несёт депозит-резерв (см. _reserve_items), черновик историчен.
        "paid": "paid",
        "disbursed": "paid",
        "failed": "failed",
        "deleted": "deleted",
        "cancelled": "cancelled",
    }
    items: list[PaymentItem] = []
    for draft, emp_name in rows:
        state = status_map.get(draft.status, draft.status)
        recipient = emp_name or ("курьер" if draft.recipient_kind == "courier" else "получатель")
        # Наличный резерв (этап 4) хранится тем же черновиком, но bank_provider — маркер
        # канала (cash_safe/cash_tk), не банк: показываем как наличный.
        is_bank = draft.bank_provider in ("tbank", "sber")
        items.append(
            PaymentItem(
                id=f"deposit_draft:{draft.id}",
                source="deposit_draft",
                kind="deposit_bank_draft",
                ref_id=draft.id,
                title=f"Выдача депозита — {recipient} · {_fmt_money(Decimal(draft.amount))}",
                counterparty_id=None,
                counterparty_name=None,
                amount=Decimal(draft.amount),
                amount_paid=None,
                article_id=None,
                article_name=None,
                method="bank" if is_bank else "cash",
                bank_channel=draft.bank_provider if is_bank else None,
                state=state,
                bucket=BUCKET_BY_STATE.get(state),
                created_at=draft.created_at,
                can_edit=False,
                can_send_to_bank=False,
                can_pay=False,  # ждёт подписи/оплаты в банке; после оплаты платят резерв
                can_cancel=False,
                extra={
                    "deposit": True,
                    "recipient_kind": draft.recipient_kind,
                    "employee_id": str(draft.employee_id) if draft.employee_id else None,
                    "last_error": draft.last_error,
                },
            )
        )
    return items


async def _employee_payout_items(session: AsyncSession) -> list[PaymentItem]:
    """Разовые выплаты сотрудникам — «Отправлен в банк» (``pending``).

    Окно «Новый платёж» → зарплатная статья: черновик на карту ИП в банке счёта списания
    (Т-Банк или Сбер). Оплату доводит статус платёжного документа — вебхук у Т-Банка, поллинг
    у Сбера; переход в ``paid`` заводит транзит банк→Сейф и Сейф-резерв, и активную строку
    дальше несёт резерв, а выплата уходит в историю (одна выдача — одна строка). Ошибка банка
    (``failed``) корзины не имеет, но в истории видна с причиной.

    Берём только платёжный контур: ``pending``/``failed`` и исполненные банковские. Наличная
    выплата создаётся сразу ``paid`` без ``document_id`` — это мгновенный факт, а не платёж
    витрины; ``draft``/``included`` — контур ведомости («Включить в выплату»).
    """
    rows = (
        await session.execute(
            select(EmployeePayout, Employee.full_name, Account.bank_code)
            .outerjoin(Employee, Employee.id == EmployeePayout.employee_id)
            .outerjoin(Wallet, Wallet.id == EmployeePayout.wallet_id)
            .outerjoin(Account, Account.id == Wallet.account_id)
            .where(
                or_(
                    EmployeePayout.status.in_(("pending", "failed")),
                    and_(
                        EmployeePayout.status == "paid",
                        EmployeePayout.document_id.is_not(None),
                    ),
                )
            )
        )
    ).all()
    article_names = await _article_names(session, {p.article_id for p, _n, _b in rows})

    items: list[PaymentItem] = []
    for payout, employee_name, bank_code in rows:
        state = {"pending": "in_bank", "failed": "failed", "paid": "paid"}[payout.status]
        recipient = employee_name or "сотрудник"
        items.append(
            PaymentItem(
                id=f"employee_payout:{payout.id}",
                source="employee_payout",
                kind="employee_payout",
                ref_id=payout.id,
                title=f"Выплата сотруднику — {recipient} · {_fmt_money(Decimal(payout.amount))}",
                counterparty_id=None,
                counterparty_name=None,
                amount=Decimal(payout.amount),
                amount_paid=None,
                article_id=payout.article_id,
                article_name=article_names.get(payout.article_id) if payout.article_id else None,
                method="bank",
                # Канал — банк счёта списания: подпись «Сбербанк» в карточке должна совпадать
                # с тем, откуда реально уходят деньги.
                bank_channel=bank_code or "tbank",
                state=state,
                bucket=BUCKET_BY_STATE.get(state),
                created_at=payout.created_at,
                can_edit=False,
                can_send_to_bank=False,
                # Оплату подтверждает банк (вебхук/поллинг) — ручных действий по строке нет.
                can_pay=False,
                can_cancel=False,
                extra={
                    "employee_payout": True,
                    "employee_id": str(payout.employee_id),
                    "employee_name": employee_name,
                    "payout_date": payout.payout_date.isoformat(),
                    "last_error": payout.last_error,
                },
            )
        )
    return items


async def _advance_bank_draft_items(session: AsyncSession) -> list[PaymentItem]:
    """Банк-черновики выдачи аванса/займа — «Отправлен в банк».

    Зеркало ``_deposit_bank_draft_items``. Банковская выдача аванса/займа создаёт
    платёжный черновик в Т-Банке/Сбере; здесь он показывается активной строкой, пока
    ждёт подписи/оплаты в банке. После оплаты (``paid``/``disbursed``) — история.
    """
    rows = (
        await session.execute(
            select(SalaryAdvanceBankDraft, SalaryAdvance, Employee.full_name)
            .join(SalaryAdvance, SalaryAdvance.id == SalaryAdvanceBankDraft.advance_id)
            .outerjoin(Employee, Employee.id == SalaryAdvance.employee_id)
        )
    ).all()
    status_map = {
        "created": "in_bank",
        "updated": "in_bank",
        "paid": "paid",
        "disbursed": "paid",
        "failed": "failed",
        "cancelled": "cancelled",
    }
    items: list[PaymentItem] = []
    for draft, advance, emp_name in rows:
        state = status_map.get(draft.status, draft.status)
        recipient = emp_name or "сотрудник"
        kind_label = "Заём" if advance.kind == "loan" else "Аванс"
        is_bank = draft.bank_provider in ("tbank", "sber")
        items.append(
            PaymentItem(
                id=f"advance_draft:{draft.id}",
                source="advance_draft",
                kind="advance_bank_draft",
                ref_id=draft.id,
                title=f"{kind_label} — {recipient} · {_fmt_money(Decimal(draft.amount))}",
                counterparty_id=None,
                counterparty_name=None,
                amount=Decimal(draft.amount),
                amount_paid=None,
                article_id=None,
                article_name=None,
                method="bank" if is_bank else "cash",
                bank_channel=draft.bank_provider if is_bank else None,
                state=state,
                bucket=BUCKET_BY_STATE.get(state),
                created_at=draft.created_at,
                can_edit=False,
                can_send_to_bank=False,
                can_pay=False,  # ждёт подписи/оплаты в банке
                can_cancel=False,
                extra={
                    "advance": True,
                    "advance_id": str(advance.id),
                    "employee_id": str(advance.employee_id) if advance.employee_id else None,
                    "kind": advance.kind,
                    "last_error": draft.last_error,
                },
            )
        )
    return items


async def _tax_bank_draft_items(session: AsyncSession) -> list[PaymentItem]:
    """Налоговые платёжки ЕНП, подготовленные на «Налогах» — «Готов к отправке» или «Отправлен».

    Строка ``TaxBankDraft`` в ``ready_to_send`` попадает в корзину «к отправке» с кнопкой «В банк»;
    после отправки (``in_bank``) — «Отправлен в банк». Получатель — фиксированные реквизиты ФНС
    (owner-locked), поэтому в диалоге редактируемы только сумма и назначение, а сами реквизиты
    кладём в ``extra`` для показа. Отказ банка (``failed``) возвращает платёж в очередь на отправку.
    """
    drafts = (
        (await session.execute(select(TaxBankDraft).order_by(TaxBankDraft.created_at.desc())))
        .scalars()
        .all()
    )
    if not drafts:
        return []

    status_map = {
        "ready_to_send": "ready_to_send",
        "in_bank": "in_bank",
        "paid": "paid",
        "cancelled": "cancelled",
        "failed": "ready_to_send",  # отказ банка — платёж снова в очереди на отправку
    }
    items: list[PaymentItem] = []
    for d in drafts:
        # Реквизиты по виду налога: ЕНП — ФНС, травматизм — СФР.
        reqs = requisites_for_kind(d.tax_kind)
        state = status_map.get(d.status, d.status)
        title = d.title or "Единый налоговый платёж (ЕНП)"
        items.append(
            PaymentItem(
                id=f"tax_draft:{d.id}",
                source="tax_draft",
                kind="tax_enp",
                ref_id=d.id,
                title=f"{title} · {_fmt_money(Decimal(d.amount))}",
                counterparty_id=None,
                counterparty_name=d.recipient_name or str(reqs["recipientName"]),
                amount=Decimal(d.amount),
                amount_paid=None,
                article_id=None,
                article_name=(
                    "Налоги — травматизм (СФР)"
                    if d.tax_kind == "contrib_injury"
                    else "Налоги — ЕНП"
                ),
                method="bank",
                bank_channel=d.bank_provider,
                state=state,
                bucket=BUCKET_BY_STATE.get(state),
                created_at=d.created_at,
                can_edit=state == "ready_to_send",
                can_send_to_bank=state == "ready_to_send",
                can_pay=False,  # деньги уходят подтверждением в банк-клиенте, не отсюда
                # Отменять можно и отправленный: платёжку, которую владелец не подтвердил
                # в банк-клиенте (или удалил там), иначе нечем снять — она висит активной
                # вечно и остаётся кандидатом разбора выписки.
                can_cancel=state in ("ready_to_send", "in_bank"),
                extra={
                    "tax": True,
                    "tax_kind": d.tax_kind,
                    "for_period": d.for_period,
                    "for_year": d.for_year,
                    "due_date": d.due_date.isoformat() if d.due_date else None,
                    "purpose": d.purpose,
                    "kbk": d.kbk or str(reqs["kbk"]),
                    "last_error": d.last_error,
                    "sent_to_bank_at": (
                        d.sent_to_bank_at.isoformat() if d.sent_to_bank_at else None
                    ),
                    "requisites": {
                        "recipientName": str(reqs["recipientName"]),
                        "inn": str(reqs["inn"]),
                        "kpp": str(reqs["kpp"]),
                        "bankAcnt": str(reqs["bankAcnt"]),
                        "bankBik": str(reqs["bankBik"]),
                        "bankName": str(reqs.get("bankName", "")),
                        "kbk": str(reqs["kbk"]),
                    },
                },
            )
        )
    return items


async def _tax_due_items(session: AsyncSession) -> list[PaymentItem]:
    """Налоговые обязательства «К уплате» — виртуальные строки без клика на «Налогах».

    Решение владельца 26.07.2026: окно активных платежей само показывает всё, что надо
    платить (УСН, ЕНП, допвзнос, травматизм), а не только подготовленное вручную. Строка
    виртуальная — черновик ``TaxBankDraft`` создаётся в момент «В банк» (TaxSendDialog).
    Обязательства, по которым черновик уже есть, пропускаются — их несёт источник
    ``tax_draft``. Травматизм показывается БЕЗ кнопки: он платится в СФР по своим
    реквизитам, контур ЕНП его отправить не может.
    """
    from app.services.taxes.engine import TaxComputationError
    from app.services.taxes.obligations import list_payable_obligations

    try:
        obligations = await list_payable_obligations(session, today=date.today())
    except TaxComputationError:
        # Нет параметров налогового года и т.п. — окно платежей от этого не ломаем.
        return []
    except Exception:  # noqa: BLE001 - расчёт налогов не должен ронять витрину платежей
        logger.warning("payments: не удалось собрать налоговые обязательства", exc_info=True)
        return []

    existing_drafts = (
        (await session.execute(select(TaxBankDraft))).scalars().all()
    )
    draft_keys = {
        (d.tax_kind, d.for_period)
        for d in existing_drafts
        if d.status in ("ready_to_send", "in_bank", "failed")
    }

    items: list[PaymentItem] = []
    for ob in obligations:
        # Прогнозные строки официального контура — не платёжные: платим по платёжкам
        # бухгалтера. В долге УДКЗ прогноз виден, в очередь оплат не попадает.
        if ob.is_projection:
            continue
        if (ob.kind, ob.for_period) in draft_keys:
            continue
        # Реквизиты по виду: всё уходит ЕНПом в ФНС, травматизм — отдельной платёжкой в СФР.
        reqs = requisites_for_kind(ob.kind)
        is_injury = ob.kind == "contrib_injury"
        due_dt = datetime.combine(ob.due_date, time.min, tzinfo=UTC) if ob.due_date else None
        extra: dict = {
            "tax": True,
            "virtual": True,
            "tax_kind": ob.kind,
            "for_period": ob.for_period,
            "for_year": ob.for_year,
            "due_date": ob.due_date.isoformat() if ob.due_date else None,
            "purpose": str(reqs["paymentPurpose"]),
            "title": ob.title,
            "kbk": str(reqs["kbk"]),
            "requisites": {
                "recipientName": str(reqs["recipientName"]),
                "inn": str(reqs["inn"]),
                "kpp": str(reqs["kpp"]),
                "bankAcnt": str(reqs["bankAcnt"]),
                "bankBik": str(reqs["bankBik"]),
                "bankName": str(reqs.get("bankName", "")),
                "kbk": str(reqs["kbk"]),
            },
        }
        items.append(
            PaymentItem(
                # Детерминированный id: то же обязательство — та же строка между опросами.
                id=f"tax_due:{ob.kind}:{ob.for_period or 'year'}",
                source="tax_due",
                kind="tax_obligation",
                ref_id=uuid.uuid5(
                    uuid.NAMESPACE_URL, f"tax_due:{ob.kind}:{ob.for_period or 'year'}"
                ),
                title=f"{ob.title} · {_fmt_money(Decimal(ob.amount))}",
                counterparty_id=None,
                counterparty_name=str(reqs["recipientName"]),
                amount=Decimal(ob.amount),
                amount_paid=None,
                article_id=None,
                article_name="Налоги — травматизм (СФР)" if is_injury else "Налоги — ЕНП",
                method="bank",
                bank_channel="tbank",
                state="ready_to_send",
                bucket=BUCKET_BY_STATE.get("ready_to_send"),
                created_at=due_dt or datetime.now(UTC),
                can_edit=False,
                can_send_to_bank=True,
                can_pay=False,
                can_cancel=False,  # обязательство из расчёта/платёжки — отменять нечего
                extra=extra,
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
    deposit_drafts = await _deposit_bank_draft_items(session)
    advance_drafts = await _advance_bank_draft_items(session)
    employee_payouts = await _employee_payout_items(session)
    tax_drafts = await _tax_bank_draft_items(session)
    tax_dues = await _tax_due_items(session)
    items = (
        invoices
        + drafts
        + reserves
        + payroll_drafts
        + deposit_drafts
        + advance_drafts
        + employee_payouts
        + tax_drafts
        + tax_dues
    )

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

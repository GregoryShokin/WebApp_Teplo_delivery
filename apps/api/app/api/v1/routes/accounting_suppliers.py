"""Учёт ДЗ/КЗ поставщиков и журнал признания расходов по периодам услуг."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, ensure_permission, get_current_actor, require_permission
from app.db.session import get_session
from app.models import (
    AccumulationFundAccount,
    BarterReturnLine,
    CashflowTransaction,
    Counterparty,
    CounterpartyPayableProfile,
    CounterpartyPaymentDraft,
    CounterpartyServiceAgreement,
    CourierDepositAccount,
    CourierDepositTransaction,
    CourierDepositTransactionType,
    DdsArticle,
    DepositAccount,
    Employee,
    EmployeePayout,
    InvoiceLineItem,
    InvoicePaymentAllocation,
    PayrollLine,
    PayrollPayment,
    PayrollPeriod,
    PayrollRun,
    SalaryAdvance,
    SupplierExpenseAccrual,
    SupplierInvoice,
    SupplierPrepayment,
    Wallet,
    invoice_binds_settlement,
)
from app.services import counterparty_balance_as_of as balance_as_of
from app.services import counterparty_settlement_ledger as settlement
from app.services import expense_recognition_report as expense_report
from app.services import expense_reversal as reversal_service
from app.services import subscription_accruals as subscriptions
from app.services import supplier_service_periods as periods
from app.services.accumulation_fund_service import (
    fund_account_visible_in_roster,
    fund_outstanding,
)
from app.services.couriers.deposit_service import is_senior_courier
from app.services.payroll_admin import compute_on_demand_debt, list_on_demand_employees
from app.services.payroll_advance_availability import available_to_advance
from app.services.position_registry import (
    eligible_for_personal_report,
    production_payroll_positions,
)

OPEN_PREPAYMENT_STATUSES = ("open", "partially_settled")
UNPAID_INVOICE_STATUSES = ("unpaid", "partially_paid")
# Сроки ожидания закрывающих документов считаются по календарю владельца, а не UTC:
# 1-е число в Москве наступает на три часа раньше, и «просрочено» не должно зависеть
# от часового пояса сервера.
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

router = APIRouter()
READ = (Depends(require_permission("accounting.suppliers.read")),)
EDIT = (Depends(require_permission("accounting.suppliers.edit")),)


class SupplierAccountingItem(BaseModel):
    """Строка признания расхода — и что с ней делать человеку.

    ``stage`` — состояние на языке владельца, а не бухгалтерии (правка 01.08.2026): прежние
    ``receivable/payable/scheduled`` человеку ничего не говорили, а «период не указан»
    выглядело претензией без действия. Четыре состояния отвечают на один вопрос — «этот
    платёж уже стал расходом, и если нет, то чего ждём»:

    * ``in_expense`` — уже в прибыли своего месяца, трогать нечего;
    * ``period_running`` — период ещё идёт, признаем 1-го числа сами;
    * ``waiting_document`` — ждём УПД: сумму расхода принесёт документ (Манго, реклама);
    * ``needs_period`` — непонятно, за какой период платили. Единственное состояние, которое
      требует действия человека, и оно же — почти вся очередь: на проде 01.08.2026 период не
      указан у ВСЕХ 19 открытых предоплат, потому что спросить его было негде.
    """

    id: uuid.UUID
    source_kind: Literal["service_period", "legacy_prepayment"]
    stage: Literal["in_expense", "period_running", "waiting_document", "needs_period"]
    counterparty_id: uuid.UUID
    counterparty_name: str
    article_id: uuid.UUID | None = None
    article_name: str | None = None
    invoice_id: uuid.UUID | None = None
    invoice_number: str | None = None
    amount: float
    paid_amount: float
    balance_amount: float
    balance_type: Literal["receivable", "payable", "scheduled", "closed", "needs_review"]
    service_period_start: date | None = None
    service_period_end: date | None = None
    period_status: str
    recognition_month: date | None = None
    recognized: bool
    # Дальше — то, ради чего исчезла отдельная вкладка «Разрывы»: ожидание документа со
    # сроком и просрочкой. Без этих полей «ждём документ» не отличает вчерашний платёж от
    # зависшего с мая, и вкладку пришлось бы держать только ради даты.
    payment_date: date | None = None
    expected_by: date | None = None
    days_overdue: int = 0
    # Можно ли признать расход по этой строке руками. Считается тем же правилом, по которому
    # отказывает сервис: иначе кнопка стоит там, где ответ всегда 409 — так было примерно на
    # половине строк «ждём документ».
    can_recognize: bool = False
    recognize_blocked_reason: str | None = None
    # true — период не указан, и срок посчитан по месяцу платежа. Фронт обязан это сказать
    # вслух: иначе выдуманный период читается как подтверждённый.
    period_assumed: bool = False
    # Насколько признанный расход разошёлся с суммой документа. Признанное начисление не
    # переписывается молча — оно уже в прибыли закрытого месяца, и менять его без ведома
    # человека нельзя. Но и промолчать нельзя: у СДЭК документ СКБ-0008640 вырос с 7 893,40
    # до 7 984,90 через час после признания, и 91,50 ₽ расхода просто не существовало ни в
    # одном отчёте. Здесь эта разница становится видимой строкой, а решение — за человеком.
    document_amount: float | None = None
    amount_mismatch: float = 0


class StageTile(BaseModel):
    """Плитка состояния: сколько строк и на какую сумму. Смысл суммы у состояний разный.

    У очередей (``needs_period``, ``waiting_document``) сумма — это ЗАВИСШИЕ ДЕНЬГИ: заплатили,
    а расходом они пока не стали. У ``in_expense`` тот же остаток был бы нулём (признано и
    оплачено), поэтому там сумма — величина признанного РАСХОДА, и только за текущий месяц:
    за всю историю это число ничего не значит и растёт вечно.
    """

    count: int = 0
    amount: float = 0


class SupplierAccountingList(BaseModel):
    items: list[SupplierAccountingItem]
    receivable_total: float
    payable_total: float
    scheduled_total: float
    needs_review_total: float
    # Плитки экрана. Считаются по ВСЕМ строкам, независимо от фильтра: иначе, выбрав «нужен
    # период», человек увидит нули в остальных плитках и решит, что там пусто.
    in_expense: StageTile = StageTile()
    period_running: StageTile = StageTile()
    waiting_document: StageTile = StageTile()
    needs_period: StageTile = StageTile()
    # Месяц, за который посчитана плитка «уже в расходе» — фронт подписывает её словами.
    in_expense_month: date | None = None


class ServicePeriodUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_period_start: date
    service_period_end: date
    reason: str | None = Field(default=None, max_length=500)


def _float(value: Decimal | int | float | None) -> float:
    return float(periods.money(value))


@dataclass
class _QueueContext:
    """Что известно о контрагентах очереди признания — одним набором запросов, а не по строке.

    Те же правила, по которым жила сводка разрывов (``counterparty_settlement_ledger``), но
    посчитанные пакетом: экран показывает десятки строк, и запрос на каждую превратил бы
    открытие вкладки в минуту ожидания.
    """

    expected_days: dict[uuid.UUID, int | None] = field(default_factory=dict)
    # Режим «счёт за период»: лицензия оплачена за конкретный месяц, УПД не ждут — расход
    # признаётся сам по окончании периода (Синапсис, АЙКО, Лемма, ДоксИнБокс).
    fixed_tariff: set[uuid.UUID] = field(default_factory=set)
    # Документов от контрагента не ждут: разовые работы либо договор, по которому долг
    # считается сам. Вечно красная строка по ним — шум, из-за которого бросают смотреть весь экран.
    documents_not_expected: set[uuid.UUID] = field(default_factory=set)
    # Долг закрывает ночное начисление по договору — человеку делать нечего.
    self_settling: set[uuid.UUID] = field(default_factory=set)
    # Товарный контур: авансы поставщикам товара гасятся накладной, а расход по сырью идёт
    # фудкостом. В очереди признания расходов им не место — владелец это же и сказал.
    goods_contour: set[uuid.UUID] = field(default_factory=set)


async def _queue_context(session: AsyncSession, *, today: date) -> _QueueContext:
    ctx = _QueueContext()
    profiles = (
        await session.execute(
            select(
                CounterpartyPayableProfile.counterparty_id,
                CounterpartyPayableProfile.service_billing_mode,
                CounterpartyPayableProfile.closing_doc_expected_day,
                CounterpartyPayableProfile.settlement_contour,
            )
        )
    ).all()
    explicit_service: set[uuid.UUID] = set()
    for cp_id, billing_mode, expected_day, contour in profiles:
        ctx.expected_days[cp_id] = expected_day
        if billing_mode in (
            settlement.BILLING_MODE_ONE_OFF,
            settlement.BILLING_MODE_AGREEMENT,
        ):
            ctx.documents_not_expected.add(cp_id)
        if billing_mode == subscriptions.BILLING_MODE_FIXED_TARIFF:
            ctx.fixed_tariff.add(cp_id)
            ctx.documents_not_expected.add(cp_id)
        if contour == settlement.CONTOUR_GOODS:
            ctx.goods_contour.add(cp_id)
        elif contour == settlement.CONTOUR_SERVICE:
            # Явный выбор в карточке сильнее факта складских накладных.
            explicit_service.add(cp_id)

    informal = (
        await session.scalars(
            select(CounterpartyServiceAgreement.counterparty_id).where(
                CounterpartyServiceAgreement.documents_mode == "informal",
                CounterpartyServiceAgreement.started_on <= today,
                or_(
                    CounterpartyServiceAgreement.ended_on.is_(None),
                    CounterpartyServiceAgreement.ended_on >= today,
                ),
            )
        )
    ).all()
    ctx.self_settling.update(informal)
    ctx.documents_not_expected.update(informal)

    warehouse = (
        await session.scalars(
            select(SupplierInvoice.counterparty_id)
            .where(
                SupplierInvoice.operational_scope == "warehouse",
                SupplierInvoice.payment_status != "void",
            )
            .distinct()
        )
    ).all()
    ctx.goods_contour.update(cp_id for cp_id in warehouse if cp_id not in explicit_service)
    return ctx


@router.get("", response_model=SupplierAccountingList, dependencies=READ)
async def list_supplier_accounting(
    session: Annotated[AsyncSession, Depends(get_session)],
    view: Literal["open", "all", "needs_review", "recognized"] = Query(default="open"),
    stage: Literal["in_expense", "period_running", "waiting_document", "needs_period"] | None = (
        Query(default=None)
    ),
) -> SupplierAccountingList:
    today = datetime.now(MOSCOW_TZ).date()
    current_month = today.replace(day=1)
    ctx = await _queue_context(session, today=today)
    allocated = (
        select(
            InvoicePaymentAllocation.invoice_id.label("invoice_id"),
            func.coalesce(func.sum(InvoicePaymentAllocation.amount), 0).label("paid"),
        )
        .group_by(InvoicePaymentAllocation.invoice_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                SupplierExpenseAccrual,
                Counterparty.name,
                SupplierInvoice.number,
                SupplierInvoice.payment_status,
                SupplierInvoice.amount,
                func.coalesce(allocated.c.paid, 0),
                CounterpartyPaymentDraft.status,
                DdsArticle.name,
            )
            .join(Counterparty, Counterparty.id == SupplierExpenseAccrual.counterparty_id)
            .outerjoin(SupplierInvoice, SupplierInvoice.id == SupplierExpenseAccrual.invoice_id)
            .outerjoin(allocated, allocated.c.invoice_id == SupplierExpenseAccrual.invoice_id)
            .outerjoin(
                CounterpartyPaymentDraft,
                CounterpartyPaymentDraft.id == SupplierExpenseAccrual.payment_draft_id,
            )
            .outerjoin(DdsArticle, DdsArticle.id == SupplierExpenseAccrual.article_id)
            .where(SupplierExpenseAccrual.status != "cancelled")
            .order_by(
                SupplierExpenseAccrual.service_period_end.desc(),
                SupplierExpenseAccrual.created_at.desc(),
            )
        )
    ).all()

    items: list[SupplierAccountingItem] = []
    for (
        accrual,
        cp_name,
        number,
        _invoice_status,
        invoice_amount,
        allocated_amount,
        draft_status,
        article_name,
    ) in rows:
        if accrual.invoice_id is not None:
            paid = min(periods.money(allocated_amount), periods.money(accrual.amount))
        else:
            paid = periods.money(accrual.amount) if draft_status == "paid" else Decimal("0")
        total = periods.money(accrual.amount)
        if accrual.status == "recognized":
            balance = max(total - paid, Decimal("0"))
            balance_type = "payable" if balance > 0 else "closed"
        elif paid > 0:
            balance = paid
            balance_type = "receivable"
        else:
            balance = total
            balance_type = "scheduled"
        # Имя переменной не ``stage``: так называется параметр-фильтр этой же функции, и
        # присваивание в цикле затирало бы его — выдача молча возвращала бы одно состояние.
        # Непризнанное начисление всегда «период идёт»: даже когда период уже кончился, расход
        # признаёт ночная джоба, и к утру состояние станет in_expense само.
        accrual_stage = "in_expense" if accrual.status == "recognized" else "period_running"
        item = SupplierAccountingItem(
            id=accrual.id,
            stage=accrual_stage,
            source_kind="service_period",
            counterparty_id=accrual.counterparty_id,
            counterparty_name=cp_name,
            article_id=accrual.article_id,
            article_name=article_name,
            invoice_id=accrual.invoice_id,
            invoice_number=number,
            amount=_float(total),
            paid_amount=_float(paid),
            balance_amount=_float(balance),
            balance_type=balance_type,
            service_period_start=accrual.service_period_start,
            service_period_end=accrual.service_period_end,
            period_status="ready",
            recognition_month=accrual.recognition_month,
            recognized=accrual.status == "recognized",
            document_amount=_float(periods.money(invoice_amount))
            if invoice_amount is not None
            else None,
            # Расхождение показываем только у ПРИЗНАННОГО расхода: у непризнанного сумма
            # подтягивается сама при следующем синке, и разница — просто «ещё не обновилось».
            amount_mismatch=(
                _float(periods.money(invoice_amount) - total)
                if invoice_amount is not None and accrual.status == "recognized"
                else 0
            ),
        )
        items.append(item)

    # Открытые предоплаты — это деньги, которые ушли, а расходом ещё не стали. Каждая строка
    # отвечает на один вопрос: чего ждём. ``prepaid_bill`` (ДЗ по оплаченному счёту) тоже
    # здесь: по канону её гасит закрывающий УПД, то есть это ровно «ждём документ», и именно
    # такие платежи составляли половину сводки разрывов.
    prepayment_rows = (
        await session.execute(
            select(
                SupplierPrepayment,
                Counterparty.name,
                DdsArticle.name,
                CashflowTransaction.operation_date,
            )
            .join(Counterparty, Counterparty.id == SupplierPrepayment.counterparty_id)
            .outerjoin(DdsArticle, DdsArticle.id == SupplierPrepayment.article_id)
            .outerjoin(
                CashflowTransaction,
                CashflowTransaction.id == SupplierPrepayment.cashflow_transaction_id,
            )
            .where(SupplierPrepayment.status.in_(OPEN_PREPAYMENT_STATUSES))
            .order_by(SupplierPrepayment.created_at.desc())
        )
    ).all()
    for prepayment, cp_name, article_name, operation_date in prepayment_rows:
        # Товарные авансы гасит накладная, а расход по сырью идёт фудкостом — в очереди
        # признания расходов их быть не должно (требование владельца 01.08.2026).
        if prepayment.counterparty_id in ctx.goods_contour:
            continue
        balance = max(
            periods.money(prepayment.amount) - periods.money(prepayment.amount_settled),
            Decimal("0"),
        )
        period_known = (
            prepayment.service_period_status == "ready"
            and prepayment.service_period_start is not None
            and prepayment.service_period_end is not None
        )
        paid_on = operation_date or prepayment.created_at.date()
        # Период для срока: явный, а при его отсутствии — месяц платежа. Фолбэк не выдумка,
        # а рабочая гипотеза: услуга почти всегда оплачивается в своём же месяце, и ждать
        # документ по ней всё равно надо. Строка честно помечена ``period_assumed``.
        period_start, period_end = settlement.period_of(
            prepayment.service_period_start if period_known else None,
            prepayment.service_period_end if period_known else None,
            paid_on,
        )
        cp_id = prepayment.counterparty_id
        documents_expected = cp_id not in ctx.documents_not_expected
        # Долг закроется сам: помесячное признание из этой же предоплаты либо ночное
        # начисление по договору. Человеку делать нечего, срок не нужен.
        settles_itself = (
            prepayment.auto_recognize_monthly
            or cp_id in ctx.self_settling
            # «Счёт за период»: период известен из счёта, и по его окончании расход признаётся
            # сам. Ждать документ здесь не от кого — контрагент его не выставляет.
            or cp_id in ctx.fixed_tariff
        )

        deadline: date | None = None
        overdue = 0
        # ДЗ по оплаченному счёту гасит закрывающий УПД (правило 2 канона), и признать её
        # вручную нельзя — recognize_prepayment_period такие платежи отклоняет. Поэтому она
        # всегда «ждём документ»: попади она в очередь решений, человек получил бы строку с
        # требованием действия, которое система ему не даст выполнить.
        if prepayment.kind == "prepaid_bill":
            prepayment_stage = "waiting_document"
            deadline = settlement.expected_by(period_end, ctx.expected_days.get(cp_id))
            overdue = max((today - deadline).days, 0)
        elif period_known and settles_itself:
            prepayment_stage = "period_running"
        elif documents_expected:
            # По умолчанию документ ЖДЁТСЯ — так же, как считала сводка разрывов. Иначе, пока
            # режимы контрагентам не проставлены, весь экран был бы одной очередью «нужен
            # период», хотя по большинству платежей от человека ничего не требуется.
            prepayment_stage = "waiting_document"
            deadline = settlement.expected_by(period_end, ctx.expected_days.get(cp_id))
            overdue = max((today - deadline).days, 0)
        elif period_known:
            # Документа не будет, период известен, а само признание выключено: расход
            # повиснет навсегда, пока человек не запустит признание за период.
            prepayment_stage = "needs_period"
        else:
            prepayment_stage = "needs_period"
        refusal = subscriptions.refusal_reason(
            prepayment,
            documents_expected=documents_expected,
            covered_by_agreement=cp_id in ctx.self_settling,
        )
        items.append(
            SupplierAccountingItem(
                id=prepayment.id,
                stage=prepayment_stage,
                source_kind="legacy_prepayment",
                can_recognize=refusal is None,
                recognize_blocked_reason=refusal,
                counterparty_id=cp_id,
                counterparty_name=cp_name,
                article_id=prepayment.article_id,
                article_name=article_name,
                amount=_float(prepayment.amount),
                paid_amount=_float(prepayment.amount),
                balance_amount=_float(balance),
                balance_type="needs_review" if not period_known else "receivable",
                service_period_start=prepayment.service_period_start,
                service_period_end=prepayment.service_period_end,
                period_status=prepayment.service_period_status,
                recognized=False,
                payment_date=paid_on,
                expected_by=deadline,
                days_overdue=overdue,
                period_assumed=not period_known,
            )
        )

    # Сверху то, что горит дольше всех, а внутри одной просрочки — что дороже. Ровно так
    # сортировалась сводка разрывов: без этого «ждём документ» превращается в ленту по дате
    # платежа, где зависшее с мая лежит между вчерашними.
    items.sort(key=lambda item: (-item.days_overdue, -item.balance_amount))

    # Плитки состояний считаем ДО фильтра: иначе, выбрав «нужен период», человек увидит нули
    # в остальных плитках и решит, что там пусто.
    all_items = list(items)
    # Месяц плитки «уже в расходе» — текущий, но 1-го числа он ещё пуст, и экран сообщал бы,
    # что расходов нет вовсе. В такой день показываем прошлый месяц, а какой именно — говорим
    # вслух полем in_expense_month.
    recognized_months = {
        item.recognition_month
        for item in items
        if item.stage == "in_expense" and item.recognition_month is not None
    }
    expense_month = current_month
    if current_month not in recognized_months:
        previous = max((m for m in recognized_months if m < current_month), default=None)
        expense_month = previous or current_month

    if stage is not None:
        items = [item for item in items if item.stage == stage]
        # «Уже в расходе» за всю историю — бесконечная лента без единого действия. Показываем
        # тот же месяц, что и в плитке: остальное живёт в отчёте о прибыли, а не в очереди.
        if stage == "in_expense":
            items = [item for item in items if item.recognition_month == expense_month]
    elif view == "open":
        items = [item for item in items if item.balance_type != "closed"]
    elif view == "needs_review":
        items = [item for item in items if item.balance_type == "needs_review"]
    elif view == "recognized":
        items = [item for item in items if item.recognized]

    def tile(name: str) -> StageTile:
        rows = [item for item in all_items if item.stage == name]
        if name == "in_expense":
            rows = [item for item in rows if item.recognition_month == expense_month]
            return StageTile(count=len(rows), amount=sum(item.amount for item in rows))
        return StageTile(count=len(rows), amount=sum(item.balance_amount for item in rows))

    return SupplierAccountingList(
        items=items,
        in_expense=tile("in_expense"),
        period_running=tile("period_running"),
        waiting_document=tile("waiting_document"),
        needs_period=tile("needs_period"),
        in_expense_month=expense_month,
        receivable_total=sum(
            item.balance_amount for item in items if item.balance_type == "receivable"
        ),
        payable_total=sum(item.balance_amount for item in items if item.balance_type == "payable"),
        scheduled_total=sum(
            item.balance_amount for item in items if item.balance_type == "scheduled"
        ),
        needs_review_total=sum(
            item.balance_amount for item in items if item.balance_type == "needs_review"
        ),
    )


@router.patch(
    "/service-periods/{accrual_id}",
    response_model=SupplierAccountingItem,
    dependencies=EDIT,
)
async def patch_service_period(
    accrual_id: uuid.UUID,
    payload: ServicePeriodUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> SupplierAccountingItem:
    accrual = await session.get(SupplierExpenseAccrual, accrual_id)
    if accrual is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Начисление не найдено")
    if accrual.status == "recognized":
        ensure_permission(actor, "accounting.service_periods.correct_recognized")
        if not (payload.reason or "").strip():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Укажите причину корректировки уже признанного расхода",
            )
    try:
        await periods.change_accrual_period(
            session,
            accrual=accrual,
            start=payload.service_period_start,
            end=payload.service_period_end,
            actor_user_id=actor.user_id,
            reason=payload.reason,
        )
    except periods.ServicePeriodError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    cp_name = await session.scalar(
        select(Counterparty.name).where(Counterparty.id == accrual.counterparty_id)
    )
    return SupplierAccountingItem(
        id=accrual.id,
        # Начисление после правки периода либо уже в P&L, либо ждёт конца периода — третьего
        # состояния у него нет: «нужен период» и «ждём документ» бывают только у предоплаты,
        # а здесь период только что задан руками.
        stage="in_expense" if accrual.status == "recognized" else "period_running",
        source_kind="service_period",
        counterparty_id=accrual.counterparty_id,
        counterparty_name=cp_name or "—",
        article_id=accrual.article_id,
        invoice_id=accrual.invoice_id,
        amount=_float(accrual.amount),
        paid_amount=0,
        balance_amount=_float(accrual.amount),
        balance_type="scheduled" if accrual.status == "scheduled" else "payable",
        service_period_start=accrual.service_period_start,
        service_period_end=accrual.service_period_end,
        period_status="ready",
        recognition_month=accrual.recognition_month,
        recognized=accrual.status == "recognized",
    )


class ExpenseReverseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Сумма отката: можно снять часть. Признали 3 000 ₽, откатили 1 000 — 1 000 вернулась
    # в дебиторку, 2 000 остались расходом месяца.
    amount: Decimal = Field(gt=0)
    reason: str = Field(min_length=1, max_length=500)


class ExpenseReverseOut(BaseModel):
    reversed_amount: float
    amount_left: float
    fully_cancelled: bool


@router.post(
    "/accruals/{accrual_id}/reverse",
    response_model=ExpenseReverseOut,
    dependencies=(Depends(require_permission("accounting.expenses.reverse")),),
)
async def reverse_expense(
    accrual_id: uuid.UUID,
    payload: ExpenseReverseIn,
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> ExpenseReverseOut:
    """Снять признанный расход целиком или частью — сумма вернётся в дебиторку.

    Право отдельное (``accounting.expenses.reverse``), а не общее ``suppliers.edit``: действие
    меняет прибыль уже закрытого месяца.
    """
    accrual = await session.get(SupplierExpenseAccrual, accrual_id)
    if accrual is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Начисление не найдено")
    try:
        await reversal_service.reverse_expense(
            session,
            accrual,
            amount=payload.amount,
            reason=payload.reason,
            actor_user_id=actor.user_id,
        )
    except reversal_service.ReversalRefused as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await session.refresh(accrual)
    return ExpenseReverseOut(
        reversed_amount=_float(payload.amount),
        amount_left=_float(accrual.amount),
        fully_cancelled=accrual.status == "cancelled",
    )


class PrepaymentRecognizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_period_start: date
    service_period_end: date
    # Только для платежей из выписки, у которых статья не проставлена: у размеченных она уже
    # своя, и подменять её этим полем нельзя.
    dds_article_id: uuid.UUID | None = None


class PrepaymentRecognizeOut(BaseModel):
    """Что реально произошло — а не «сохранено».

    Месяцев может оказаться меньше запрошенного: текущий месяц ещё не кончился, и признавать
    его рано. Владелец должен видеть это сразу, а не гадать, почему сумма не сошлась.
    """

    months_recognized: int
    amount_recognized: float
    period_months: int


@router.post(
    "/prepayments/{prepayment_id}/recognize",
    response_model=PrepaymentRecognizeOut,
    dependencies=EDIT,
)
async def recognize_prepayment(
    prepayment_id: uuid.UUID,
    payload: PrepaymentRecognizeIn,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PrepaymentRecognizeOut:
    """Признать расход по зависшему платежу за указанный период.

    Действие очереди «нужен период»: закрывающего документа не будет, и без ручного решения
    деньги висели бы дебиторкой вечно, а расход не попал бы ни в один месяц.
    """
    prepayment = await session.get(SupplierPrepayment, prepayment_id)
    if prepayment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Платёж не найден")
    today = datetime.now(MOSCOW_TZ).date()
    try:
        created = await subscriptions.recognize_prepayment_period(
            session,
            prepayment,
            start=payload.service_period_start,
            end=payload.service_period_end,
            as_of=today,
            article_id=payload.dds_article_id,
        )
    except (subscriptions.RecognitionRefused, periods.ServicePeriodError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return PrepaymentRecognizeOut(
        months_recognized=len(created),
        amount_recognized=_float(sum((invoice.amount for invoice in created), Decimal("0"))),
        period_months=subscriptions.months_between(
            payload.service_period_start, payload.service_period_end
        ),
    )


# --- Дашборд взаиморасчётов: остатки по контрагентам, реестр платежей, реестр УПД ---


class CounterpartyBalance(BaseModel):
    counterparty_id: uuid.UUID
    name: str
    inn: str | None = None
    receivable: float
    payable: float
    net: float
    open_prepayments: int
    unpaid_invoices: int
    last_activity: date | None = None


class CounterpartyBalanceList(BaseModel):
    items: list[CounterpartyBalance]
    receivable_total: float
    payable_total: float


class SettledInvoiceRef(BaseModel):
    invoice_id: uuid.UUID
    number: str | None = None
    invoice_date: date | None = None
    amount: float


class PaymentPrepaymentInfo(BaseModel):
    id: uuid.UUID
    kind: str
    status: str
    amount: float
    amount_settled: float
    settled_invoices: list[SettledInvoiceRef]


class PaymentRegisterRow(BaseModel):
    id: uuid.UUID
    row_kind: Literal["transaction", "opening_prepayment"]
    operation_date: date
    amount: float
    counterparty_id: uuid.UUID
    counterparty_name: str
    wallet_name: str | None = None
    article_name: str | None = None
    purpose: str | None = None
    settled_invoices: list[SettledInvoiceRef]
    prepayment: PaymentPrepaymentInfo | None = None
    unassigned_amount: float


class PaymentRegisterList(BaseModel):
    items: list[PaymentRegisterRow]
    total_amount: float


class DocumentAllocationRef(BaseModel):
    source_kind: str
    amount: float
    operation_date: date | None = None
    prepayment_kind: str | None = None


class DocumentRegisterRow(BaseModel):
    invoice_id: uuid.UUID
    number: str | None = None
    invoice_date: date | None = None
    source: str
    doc_kind: str
    # 'active' — документ в силе (в КЗ); 'pending' — будущий УПД, ждёт своей даты (правило 4).
    activation_status: str
    # Документ зарегистрирован, но в расчётах не участвует: у контрагента договор услуги,
    # и источник истины — он, а не бумага. В реестре виден (расхождение надо замечать),
    # в кредиторку не входит.
    informational: bool = False
    counterparty_id: uuid.UUID
    counterparty_name: str
    amount: float
    payment_status: str
    remainder: float
    service_period_start: date | None = None
    service_period_end: date | None = None
    allocations: list[DocumentAllocationRef]


class DocumentRegisterList(BaseModel):
    items: list[DocumentRegisterRow]
    total_amount: float
    unpaid_total: float


def _clamp_money(value: Decimal) -> Decimal:
    return max(periods.money(value), Decimal("0"))


@router.get("/balances", response_model=CounterpartyBalanceList, dependencies=READ)
async def list_counterparty_balances(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CounterpartyBalanceList:
    """Актуальные остатки взаиморасчётов по каждому контрагенту.

    Дебиторка = открытые предоплаты (нам должны закрыть документами или вернуть).
    Кредиторка = неоплаченный остаток накладных direction='payable' (мы должны).
    Бартерные receivable-накладные сюда не входят — у бартера свой нетто-контур.
    """
    prepay_rows = (
        await session.execute(
            select(
                SupplierPrepayment.counterparty_id,
                func.sum(SupplierPrepayment.amount - SupplierPrepayment.amount_settled),
                func.count(SupplierPrepayment.id),
                func.max(func.date(SupplierPrepayment.created_at)),
            )
            .where(SupplierPrepayment.status.in_(OPEN_PREPAYMENT_STATUSES))
            .group_by(SupplierPrepayment.counterparty_id)
        )
    ).all()

    allocated = (
        select(
            InvoicePaymentAllocation.invoice_id.label("invoice_id"),
            func.coalesce(func.sum(InvoicePaymentAllocation.amount), 0).label("paid"),
        )
        .group_by(InvoicePaymentAllocation.invoice_id)
        .subquery()
    )
    remainder = SupplierInvoice.amount - func.coalesce(allocated.c.paid, 0)
    invoice_rows = (
        await session.execute(
            select(
                SupplierInvoice.counterparty_id,
                func.sum(func.greatest(remainder, 0)),
                func.count(SupplierInvoice.id),
                func.max(SupplierInvoice.invoice_date),
            )
            .outerjoin(allocated, allocated.c.invoice_id == SupplierInvoice.id)
            .where(
                SupplierInvoice.payment_status.in_(UNPAID_INVOICE_STATUSES),
                SupplierInvoice.direction == "payable",
                # Канон ДЗ/КЗ: кредиторка — это АКТИВНЫЕ закрывающие документы. Счета (bill) —
                # не долг (очередь оплат), будущие УПД (activation='pending') ещё не в силе.
                SupplierInvoice.doc_kind == "closing",
                invoice_binds_settlement(),
            )
            .group_by(SupplierInvoice.counterparty_id)
        )
    ).all()

    # Контрагенты с закрытыми расчётами (0/0) остаются в списке: владелец видит ВСЕХ,
    # с кем есть документооборот. Счета (bill) в баланс не входят и в этот список не тянут —
    # они живут в очереди оплат; учитываем только закрывающие документы (в т.ч. будущие УПД).
    activity_rows = (
        await session.execute(
            select(
                SupplierInvoice.counterparty_id,
                func.max(SupplierInvoice.invoice_date),
            )
            .where(
                SupplierInvoice.payment_status != "void",
                SupplierInvoice.direction == "payable",
                SupplierInvoice.doc_kind == "closing",
            )
            .group_by(SupplierInvoice.counterparty_id)
        )
    ).all()
    prepay_activity_rows = (
        await session.execute(
            select(
                SupplierPrepayment.counterparty_id,
                func.max(func.date(SupplierPrepayment.created_at)),
            ).group_by(SupplierPrepayment.counterparty_id)
        )
    ).all()

    # Бартерные займы — товарные долги в ОБЩИХ плитках (решение владельца 18.07). Их заём нам
    # (payable) уже входит в кредиторку инвойс-строками, но остаток там аллокационный — вычитаем
    # ЗАЧЁТНУЮ стоимость возвратов (qty × исходная цена займа; свободные суммы — как есть),
    # иначе частично возвращённый заём висит полной суммой. Наша выдача (receivable-заём) —
    # дебиторка остатком. Денежные оплаты займов сидят в аллокациях и учтены самой плиткой.
    return_credit = (
        select(
            BarterReturnLine.loan_invoice_id.label("loan_id"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            BarterReturnLine.quantity.isnot(None)
                            & BarterReturnLine.loan_line_item_id.isnot(None),
                            BarterReturnLine.quantity * InvoiceLineItem.price,
                        ),
                        else_=BarterReturnLine.amount,
                    )
                ),
                0,
            ).label("credited"),
        )
        .outerjoin(InvoiceLineItem, InvoiceLineItem.id == BarterReturnLine.loan_line_item_id)
        .group_by(BarterReturnLine.loan_invoice_id)
        .subquery()
    )
    barter_payable_credit_rows = (
        await session.execute(
            select(SupplierInvoice.counterparty_id, func.sum(return_credit.c.credited))
            .join(return_credit, return_credit.c.loan_id == SupplierInvoice.id)
            .where(
                SupplierInvoice.direction == "payable",
                SupplierInvoice.barter_role == "loan",
                SupplierInvoice.payment_status.in_(UNPAID_INVOICE_STATUSES),
                SupplierInvoice.doc_kind == "closing",
                invoice_binds_settlement(),
            )
            .group_by(SupplierInvoice.counterparty_id)
        )
    ).all()
    barter_receivable_rows = (
        await session.execute(
            select(
                SupplierInvoice.counterparty_id,
                func.sum(
                    func.greatest(
                        SupplierInvoice.amount - func.coalesce(return_credit.c.credited, 0), 0
                    )
                ),
                func.max(SupplierInvoice.invoice_date),
            )
            .outerjoin(return_credit, return_credit.c.loan_id == SupplierInvoice.id)
            .where(
                SupplierInvoice.direction == "receivable",
                SupplierInvoice.barter_role == "loan",
                SupplierInvoice.payment_status != "void",
                SupplierInvoice.barter_return_status != "returned",
            )
            .group_by(SupplierInvoice.counterparty_id)
        )
    ).all()

    receivable_by_cp = {row[0]: (periods.money(row[1]), row[2], row[3]) for row in prepay_rows}
    payable_by_cp = {row[0]: (periods.money(row[1]), row[2], row[3]) for row in invoice_rows}
    for cp_id, credited in barter_payable_credit_rows:
        if cp_id in payable_by_cp:
            total, cnt, last = payable_by_cp[cp_id]
            payable_by_cp[cp_id] = (max(total - periods.money(credited), Decimal("0")), cnt, last)
    for cp_id, loan_remaining, loan_last in barter_receivable_rows:
        total, cnt, prev_last = receivable_by_cp.get(cp_id, (Decimal("0"), 0, None))
        best_last = max(filter(None, (prev_last, loan_last)), default=None)
        receivable_by_cp[cp_id] = (total + periods.money(loan_remaining), cnt, best_last)
    activity_by_cp: dict[uuid.UUID, date] = {}
    for cp_id, last_date in list(activity_rows) + list(prepay_activity_rows):
        if last_date is None:
            continue
        current = activity_by_cp.get(cp_id)
        activity_by_cp[cp_id] = max(current, last_date) if current else last_date

    cp_ids = set(receivable_by_cp) | set(payable_by_cp) | set(activity_by_cp)
    if not cp_ids:
        return CounterpartyBalanceList(items=[], receivable_total=0, payable_total=0)

    counterparties = (
        await session.execute(
            select(Counterparty.id, Counterparty.name, Counterparty.inn).where(
                Counterparty.id.in_(cp_ids)
            )
        )
    ).all()

    items: list[CounterpartyBalance] = []
    for cp_id, name, inn in counterparties:
        receivable, prepay_count, prepay_last = receivable_by_cp.get(cp_id, (Decimal("0"), 0, None))
        payable, invoice_count, invoice_last = payable_by_cp.get(cp_id, (Decimal("0"), 0, None))
        last_activity = max(
            filter(None, (prepay_last, invoice_last, activity_by_cp.get(cp_id))), default=None
        )
        items.append(
            CounterpartyBalance(
                counterparty_id=cp_id,
                name=name,
                inn=inn,
                receivable=_float(receivable),
                payable=_float(payable),
                net=_float(receivable - payable),
                open_prepayments=prepay_count,
                unpaid_invoices=invoice_count,
                last_activity=last_activity,
            )
        )
    items.sort(
        key=lambda item: (max(item.receivable, item.payable), item.last_activity or date.min),
        reverse=True,
    )
    return CounterpartyBalanceList(
        items=items,
        receivable_total=sum(item.receivable for item in items),
        payable_total=sum(item.payable for item in items),
    )


async def _invoice_refs(
    session: AsyncSession, allocations: list[InvoicePaymentAllocation]
) -> dict[uuid.UUID, SettledInvoiceRef]:
    """Карточки УПД для набора аллокаций: ключ — invoice_id, сумма — по этой аллокации."""
    invoice_ids = {alloc.invoice_id for alloc in allocations}
    if not invoice_ids:
        return {}
    rows = (
        await session.execute(
            select(SupplierInvoice.id, SupplierInvoice.number, SupplierInvoice.invoice_date).where(
                SupplierInvoice.id.in_(invoice_ids)
            )
        )
    ).all()
    meta = {row[0]: (row[1], row[2]) for row in rows}
    refs: dict[uuid.UUID, SettledInvoiceRef] = {}
    for alloc in allocations:
        number, invoice_date = meta.get(alloc.invoice_id, (None, None))
        existing = refs.get(alloc.invoice_id)
        amount = periods.money(alloc.amount) + (
            Decimal(str(existing.amount)) if existing else Decimal("0")
        )
        refs[alloc.invoice_id] = SettledInvoiceRef(
            invoice_id=alloc.invoice_id,
            number=number,
            invoice_date=invoice_date,
            amount=_float(amount),
        )
    return refs


@router.get("/payments", response_model=PaymentRegisterList, dependencies=READ)
async def list_payment_register(
    session: Annotated[AsyncSession, Depends(get_session)],
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    counterparty_id: Annotated[uuid.UUID | None, Query()] = None,
) -> PaymentRegisterList:
    """Реестр платежей поставщикам: деньги ушли → чем это гасится.

    Строка — исходящая ДДС-проводка с контрагентом (банк/касса/карта) либо
    входящий остаток-предоплата без движения денег (opening). К строке пришиты:
    прямые гашения накладных (аллокации cash по transaction_id, bank через
    банк-операцию источника) и предоплата, созданная этим платежом, с её УПД.
    """
    tx_filters = [
        CashflowTransaction.direction == "out",
        CashflowTransaction.counterparty_id.is_not(None),
        CashflowTransaction.quality_status != "excluded",
    ]
    if date_from is not None:
        tx_filters.append(CashflowTransaction.operation_date >= date_from)
    if date_to is not None:
        tx_filters.append(CashflowTransaction.operation_date <= date_to)
    if counterparty_id is not None:
        tx_filters.append(CashflowTransaction.counterparty_id == counterparty_id)

    tx_rows = (
        await session.execute(
            select(CashflowTransaction, Wallet.name, DdsArticle.name, Counterparty.name)
            .join(Wallet, Wallet.id == CashflowTransaction.wallet_id)
            .outerjoin(DdsArticle, DdsArticle.id == CashflowTransaction.article_id)
            .join(Counterparty, Counterparty.id == CashflowTransaction.counterparty_id)
            .where(*tx_filters)
            .order_by(
                CashflowTransaction.operation_date.desc(), CashflowTransaction.created_at.desc()
            )
            .limit(1000)
        )
    ).all()

    tx_ids = [row[0].id for row in tx_rows]
    # Мультисплит операции даёт НЕСКОЛЬКО проводок с одним source_id (по одной на долю, у каждой
    # свой контрагент). Аллокации доли помечены её проводкой и раскладываются точно; «ничьи»
    # аллокации операции (ручная сверка их не атрибутирует) вешаем на первую по времени долю —
    # иначе они попали бы в каждую строку реестра и задвоили бы покрытие.
    bank_op_to_tx: dict[uuid.UUID, uuid.UUID] = {}
    for row in reversed(tx_rows):
        tx = row[0]
        if tx.source_kind == "bank_operation" and tx.source_id is not None:
            bank_op_to_tx.setdefault(tx.source_id, tx.id)

    direct_allocs: list[InvoicePaymentAllocation] = []
    if tx_ids or bank_op_to_tx:
        alloc_filters = []
        if tx_ids:
            alloc_filters.append(InvoicePaymentAllocation.cashflow_transaction_id.in_(tx_ids))
        if bank_op_to_tx:
            alloc_filters.append(
                InvoicePaymentAllocation.bank_operation_id.in_(bank_op_to_tx.keys())
            )
        direct_allocs = list(
            (
                await session.scalars(select(InvoicePaymentAllocation).where(or_(*alloc_filters)))
            ).all()
        )

    prepayments = (
        (
            await session.scalars(
                select(SupplierPrepayment).where(
                    SupplierPrepayment.cashflow_transaction_id.in_(tx_ids)
                )
            )
        ).all()
        if tx_ids
        else []
    )
    prepayment_by_tx = {sp.cashflow_transaction_id: sp for sp in prepayments}

    # ДЗ по оплаченному счёту (kind='prepaid_bill') несёт cashflow_transaction_id=None ПО ЗАМЫСЛУ
    # (денег не двигает — факт оплаты уже несёт аллокация счёта), поэтому без фильтра она попадала
    # в реестр строкой «начальный остаток» ВТОРЫМ разом поверх самой проводки платежа: один платёж
    # по счёту давал две строки и задвоенный итог периода. Настоящие опенинги (POST
    # /prepayments/opening) остаются — у них другой kind.
    opening_filters = [
        SupplierPrepayment.cashflow_transaction_id.is_(None),
        SupplierPrepayment.kind != "prepaid_bill",
    ]
    if counterparty_id is not None:
        opening_filters.append(SupplierPrepayment.counterparty_id == counterparty_id)
    if date_from is not None:
        opening_filters.append(func.date(SupplierPrepayment.created_at) >= date_from)
    if date_to is not None:
        opening_filters.append(func.date(SupplierPrepayment.created_at) <= date_to)
    opening_rows = (
        await session.execute(
            select(SupplierPrepayment, Counterparty.name, DdsArticle.name)
            .join(Counterparty, Counterparty.id == SupplierPrepayment.counterparty_id)
            .outerjoin(DdsArticle, DdsArticle.id == SupplierPrepayment.article_id)
            .where(*opening_filters)
            .order_by(SupplierPrepayment.created_at.desc())
            .limit(500)
        )
    ).all()

    prepay_allocs: list[InvoicePaymentAllocation] = []
    prepay_ids = [sp.id for sp in prepayments] + [row[0].id for row in opening_rows]
    if prepay_ids:
        prepay_allocs = list(
            (
                await session.scalars(
                    select(InvoicePaymentAllocation).where(
                        InvoicePaymentAllocation.prepayment_id.in_(prepay_ids)
                    )
                )
            ).all()
        )

    all_invoice_refs = await _invoice_refs(session, direct_allocs + prepay_allocs)

    def refs_for(allocs: list[InvoicePaymentAllocation]) -> list[SettledInvoiceRef]:
        merged: dict[uuid.UUID, Decimal] = {}
        for alloc in allocs:
            merged[alloc.invoice_id] = merged.get(alloc.invoice_id, Decimal("0")) + periods.money(
                alloc.amount
            )
        result = []
        for invoice_id, amount in merged.items():
            base = all_invoice_refs[invoice_id]
            result.append(base.model_copy(update={"amount": _float(amount)}))
        result.sort(key=lambda ref: ref.invoice_date or date.min, reverse=True)
        return result

    prepay_allocs_by_id: dict[uuid.UUID, list[InvoicePaymentAllocation]] = {}
    for alloc in prepay_allocs:
        if alloc.prepayment_id is not None:
            prepay_allocs_by_id.setdefault(alloc.prepayment_id, []).append(alloc)

    def prepayment_info(sp: SupplierPrepayment) -> PaymentPrepaymentInfo:
        return PaymentPrepaymentInfo(
            id=sp.id,
            kind=sp.kind,
            status=sp.status,
            amount=_float(sp.amount),
            amount_settled=_float(sp.amount_settled),
            settled_invoices=refs_for(prepay_allocs_by_id.get(sp.id, [])),
        )

    items: list[PaymentRegisterRow] = []
    for tx, wallet_name, article_name, cp_name in tx_rows:
        tx_allocs = [
            alloc
            for alloc in direct_allocs
            if alloc.cashflow_transaction_id == tx.id
            or (
                alloc.cashflow_transaction_id is None
                and alloc.bank_operation_id is not None
                and bank_op_to_tx.get(alloc.bank_operation_id) == tx.id
            )
        ]
        sp = prepayment_by_tx.get(tx.id)
        direct_total = sum((periods.money(a.amount) for a in tx_allocs), Decimal("0"))
        covered = direct_total + (periods.money(sp.amount) if sp is not None else Decimal("0"))
        items.append(
            PaymentRegisterRow(
                id=tx.id,
                row_kind="transaction",
                operation_date=tx.operation_date,
                amount=_float(tx.amount),
                counterparty_id=tx.counterparty_id,
                counterparty_name=cp_name,
                wallet_name=wallet_name,
                article_name=article_name,
                purpose=tx.payment_purpose or tx.comment,
                settled_invoices=refs_for(tx_allocs),
                prepayment=prepayment_info(sp) if sp is not None else None,
                unassigned_amount=_float(_clamp_money(periods.money(tx.amount) - covered)),
            )
        )
    for sp, cp_name, article_name in opening_rows:
        items.append(
            PaymentRegisterRow(
                id=sp.id,
                row_kind="opening_prepayment",
                operation_date=sp.created_at.date(),
                amount=_float(sp.amount),
                counterparty_id=sp.counterparty_id,
                counterparty_name=cp_name,
                wallet_name=None,
                article_name=article_name,
                purpose=sp.note,
                settled_invoices=[],
                prepayment=prepayment_info(sp),
                unassigned_amount=0,
            )
        )

    items.sort(key=lambda row: row.operation_date, reverse=True)
    return PaymentRegisterList(items=items, total_amount=sum(row.amount for row in items))


@router.get("/documents", response_model=DocumentRegisterList, dependencies=READ)
async def list_document_register(
    session: Annotated[AsyncSession, Depends(get_session)],
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    counterparty_id: Annotated[uuid.UUID | None, Query()] = None,
) -> DocumentRegisterList:
    """Реестр УПД/накладных (закрывающие документы): пришёл → чем оплачен/погашен.

    Счета (bill) сюда НЕ входят — они не документы взаиморасчётов, а очередь оплат
    («Страница на оплату» / «Платежи»). Показываем и будущие УПД (activation='pending')."""
    filters = [
        SupplierInvoice.payment_status != "void",
        SupplierInvoice.direction == "payable",
        SupplierInvoice.doc_kind == "closing",
    ]
    if date_from is not None:
        filters.append(SupplierInvoice.invoice_date >= date_from)
    if date_to is not None:
        filters.append(SupplierInvoice.invoice_date <= date_to)
    if counterparty_id is not None:
        filters.append(SupplierInvoice.counterparty_id == counterparty_id)

    rows = (
        await session.execute(
            select(SupplierInvoice, Counterparty.name)
            .join(Counterparty, Counterparty.id == SupplierInvoice.counterparty_id)
            .where(*filters)
            .order_by(
                SupplierInvoice.invoice_date.desc().nulls_last(),
                SupplierInvoice.created_at.desc(),
            )
            .limit(1000)
        )
    ).all()

    invoice_ids = [row[0].id for row in rows]
    allocs: list[tuple[InvoicePaymentAllocation, str | None, date | None]] = []
    if invoice_ids:
        alloc_rows = (
            await session.execute(
                select(
                    InvoicePaymentAllocation,
                    SupplierPrepayment.kind,
                    CashflowTransaction.operation_date,
                )
                .outerjoin(
                    SupplierPrepayment,
                    SupplierPrepayment.id == InvoicePaymentAllocation.prepayment_id,
                )
                .outerjoin(
                    CashflowTransaction,
                    CashflowTransaction.id == InvoicePaymentAllocation.cashflow_transaction_id,
                )
                .where(InvoicePaymentAllocation.invoice_id.in_(invoice_ids))
                .order_by(InvoicePaymentAllocation.created_at)
            )
        ).all()
        allocs = [(row[0], row[1], row[2]) for row in alloc_rows]

    allocs_by_invoice: dict[uuid.UUID, list[DocumentAllocationRef]] = {}
    paid_by_invoice: dict[uuid.UUID, Decimal] = {}
    for alloc, prepayment_kind, tx_date in allocs:
        allocs_by_invoice.setdefault(alloc.invoice_id, []).append(
            DocumentAllocationRef(
                source_kind=alloc.source_kind,
                amount=_float(alloc.amount),
                operation_date=tx_date or alloc.created_at.date(),
                prepayment_kind=prepayment_kind,
            )
        )
        paid_by_invoice[alloc.invoice_id] = paid_by_invoice.get(
            alloc.invoice_id, Decimal("0")
        ) + periods.money(alloc.amount)

    # Бартерный заём гасится ТОВАРОМ: возвраты живут в леджере BarterReturnLine, а не в
    # аллокациях, поэтому «сумма − аллокации» показывала бы полный долг по уже возвращённому
    # займу — реестр расходился с плиткой «Остатки» на той же странице. Берём ТОЧНУЮ зачётную
    # стоимость (loan_settled_value: qty × исходная цена + замыкание «сумма строки — эталон»
    # против копеечного дрейфа округлений), а не SQL-приближение плитки. Для payable-займа она
    # УЖЕ включает денежные аллокации — поэтому paid к ней не добавляем, иначе двойной зачёт.
    from app.services.warehouse_invoices import loan_settled_value

    barter_settled: dict[uuid.UUID, Decimal] = {}
    for invoice, _cp_name in rows:
        if invoice.barter_role == "loan":
            barter_settled[invoice.id] = await loan_settled_value(session, invoice)
        elif invoice.barter_role == "return":
            # Возвратная накладная создаётся сразу 'paid' и аллокаций не несёт (её движение —
            # в леджере BarterReturnLine), иначе реестр рисует «Оплачено · остаток N».
            # Взаимозачёт сюда НЕ входит: с миграции 0199 он пишет аллокацию source_kind='barter',
            # и остаток по нему считается общим механизмом.
            barter_settled[invoice.id] = periods.money(invoice.amount)

    items: list[DocumentRegisterRow] = []
    for invoice, cp_name in rows:
        paid = (
            barter_settled[invoice.id]
            if invoice.id in barter_settled
            else paid_by_invoice.get(invoice.id, Decimal("0"))
        )
        items.append(
            DocumentRegisterRow(
                invoice_id=invoice.id,
                number=invoice.number,
                invoice_date=invoice.invoice_date,
                source=invoice.source,
                doc_kind=invoice.doc_kind,
                activation_status=invoice.activation_status,
                informational=invoice.informational,
                counterparty_id=invoice.counterparty_id,
                counterparty_name=cp_name,
                amount=_float(invoice.amount),
                payment_status=invoice.payment_status,
                remainder=_float(_clamp_money(periods.money(invoice.amount) - paid)),
                service_period_start=invoice.service_period_start,
                service_period_end=invoice.service_period_end,
                allocations=allocs_by_invoice.get(invoice.id, []),
            )
        )
    return DocumentRegisterList(
        items=items,
        total_amount=sum(row.amount for row in items),
        # Будущие УПД (pending) в кредиторку ещё не входят, информационные — тоже: их сумма
        # уже признана начислением по договору (тот же предикат, что у плиток и гашений).
        unpaid_total=sum(
            row.remainder
            for row in items
            if row.payment_status in UNPAID_INVOICE_STATUSES
            and row.activation_status == "active"
            and not row.informational
        ),
    )


class LedgerRowRead(BaseModel):
    kind: Literal["payment", "document"]
    id: uuid.UUID
    row_date: date
    amount: float
    title: str
    subtitle: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    # Для платежа — сколько денег ещё не подтверждено закрывающим документом;
    # для документа — неоплаченный остаток.
    uncovered: float
    status: Literal["ok", "waiting", "overdue"]
    expected_by: date | None = None
    days_overdue: int
    balance_after: float
    prepayment_id: uuid.UUID | None = None
    # Документ создан нами (абонентский платёж без закрывающих), а не прислан контрагентом.
    self_billed: bool = False


class LedgerMonthRead(BaseModel):
    month: str
    paid: float
    documented: float
    gap: float
    has_overdue: bool


class LedgerRead(BaseModel):
    counterparty_id: uuid.UUID
    counterparty_name: str
    contour: Literal["goods", "service"]
    contour_manual: bool
    closing_doc_expected_day: int | None = None
    opening_balance: float
    closing_balance: float
    total_paid: float
    total_documented: float
    overdue_amount: float
    # Признано нами без первички: в P&L есть, в налоговых расходах УСН — нет.
    self_billed_amount: float
    has_barter: bool
    rows: list[LedgerRowRead]
    months: list[LedgerMonthRead]


class GapRead(BaseModel):
    counterparty_id: uuid.UUID
    counterparty_name: str
    period_start: date
    period_end: date
    amount: float
    expected_by: date
    days_overdue: int
    payments: int
    last_payment_date: date


class GapList(BaseModel):
    items: list[GapRead]
    total_amount: float
    as_of: date


@router.get("/gaps", response_model=GapList, dependencies=READ)
async def list_settlement_gaps(
    session: Annotated[AsyncSession, Depends(get_session)],
    include_goods: Annotated[bool, Query()] = False,
) -> GapList:
    """Платежи без закрывающего документа, срок ожидания которых уже прошёл.

    Это единственный экран, отвечающий 1-го числа на вопрос «у кого за прошлый месяц нет
    УПД». Товарные контрагенты скрыты: их закрывающие приходят накладными и гасятся
    складским контуром сами — ``include_goods=true`` показывает и их.
    """
    today = datetime.now(MOSCOW_TZ).date()
    rows = await settlement.list_gaps(session, today=today, include_goods=include_goods)
    return GapList(
        items=[
            GapRead(
                counterparty_id=row.counterparty_id,
                counterparty_name=row.counterparty_name,
                period_start=row.period_start,
                period_end=row.period_end,
                amount=_float(row.amount),
                expected_by=row.expected_by,
                days_overdue=row.days_overdue,
                payments=row.payments,
                last_payment_date=row.last_payment_date,
            )
            for row in rows
        ],
        total_amount=_float(sum((row.amount for row in rows), Decimal("0"))),
        as_of=today,
    )


@router.get("/{counterparty_id}/ledger", response_model=LedgerRead, dependencies=READ)
async def get_settlement_ledger(
    counterparty_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
) -> LedgerRead:
    """Сверка с контрагентом: платежи и закрывающие документы одной хронологией.

    Бегущий остаток — «деньги минус документы»; его итог сходится с плиткой «Остатки»
    на той же странице, потому что связи берутся из тех же аллокаций.
    """
    today = datetime.now(MOSCOW_TZ).date()
    try:
        ledger = await settlement.build_ledger(
            session, counterparty_id, today=today, date_from=date_from, date_to=date_to
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return LedgerRead(
        counterparty_id=ledger.counterparty_id,
        counterparty_name=ledger.counterparty_name,
        contour=ledger.contour,
        contour_manual=ledger.contour_manual,
        closing_doc_expected_day=ledger.closing_doc_expected_day,
        opening_balance=_float(ledger.opening_balance),
        closing_balance=_float(ledger.closing_balance),
        total_paid=_float(ledger.total_paid),
        total_documented=_float(ledger.total_documented),
        overdue_amount=_float(ledger.overdue_amount),
        self_billed_amount=_float(ledger.self_billed_amount),
        has_barter=ledger.has_barter,
        rows=[
            LedgerRowRead(
                kind=row.kind,
                id=row.id,
                row_date=row.row_date,
                amount=_float(row.amount),
                title=row.title,
                subtitle=row.subtitle,
                period_start=row.period_start,
                period_end=row.period_end,
                uncovered=_float(row.uncovered),
                status=row.status,
                expected_by=row.expected_by,
                days_overdue=row.days_overdue,
                balance_after=_float(row.balance_after),
                prepayment_id=row.prepayment_id,
                self_billed=row.self_billed,
            )
            for row in ledger.rows
        ],
        months=[
            LedgerMonthRead(
                month=month.month,
                paid=_float(month.paid),
                documented=_float(month.documented),
                gap=_float(month.gap),
                has_overdue=month.has_overdue,
            )
            for month in ledger.months
        ],
    )


class StaffPayableRow(BaseModel):
    employee_id: uuid.UUID
    full_name: str
    position: str | None = None
    staff_group: Literal["staff", "courier"]
    basis: str
    earned_to_date: float
    on_demand_accrued: float
    on_demand_paid: float
    on_demand_debt: float
    already_advanced: float
    advances_outstanding: float
    finalized_unpaid: float
    loans_outstanding: float
    salary_payouts_outstanding: float
    salary_payable: float
    fund_payable: float
    fund_current_year_payable: float
    fund_prior_years_payable: float
    production_deposit_payable: float
    courier_deposit_payable: float
    deposit_payable: float
    payable: float
    receivable: float


class StaffPayableList(BaseModel):
    as_of: date
    total: float
    receivable_total: float
    salary_total: float
    fund_total: float
    fund_current_year_total: float
    fund_prior_years_total: float
    production_deposit_total: float
    courier_deposit_total: float
    deposit_total: float
    items: list[StaffPayableRow]


async def _finalized_unpaid_by_employee(session: AsyncSession) -> dict[uuid.UUID, Decimal]:
    """Невыплаченные остатки ФИНАЛИЗИРОВАННЫХ ведомостей по сотрудникам.

    Берётся последний прогон каждого финализированного периода; долг = начислено
    (payroll_line.total_payable) − выплачено (payroll_payment.amount, бегущий итог).
    Легаси-заливка (is_imported_legacy) исключена: та история выплачена вне системы,
    иначе всплывают фантомные миллионы.
    """
    last_run = (
        select(PayrollRun.id)
        .join(PayrollPeriod, PayrollPeriod.id == PayrollRun.period_id)
        .where(
            PayrollPeriod.status == "finalized",
            PayrollRun.is_imported_legacy.is_(False),
        )
        .distinct(PayrollRun.period_id)
        .order_by(PayrollRun.period_id, PayrollRun.started_at.desc())
        .subquery()
    )
    accrued_rows = (
        await session.execute(
            select(
                PayrollLine.employee_id,
                func.sum(PayrollLine.total_payable),
            )
            .where(PayrollLine.run_id.in_(select(last_run.c.id)))
            .group_by(PayrollLine.employee_id)
        )
    ).all()
    paid_rows = (
        await session.execute(
            select(
                PayrollPayment.employee_id,
                func.sum(PayrollPayment.amount),
            )
            .where(
                PayrollPayment.run_id.in_(select(last_run.c.id)),
                PayrollPayment.status.in_(("paid", "partially_paid")),
            )
            .group_by(PayrollPayment.employee_id)
        )
    ).all()
    paid_by_emp = {row[0]: periods.money(row[1]) for row in paid_rows}
    debts: dict[uuid.UUID, Decimal] = {}
    for employee_id, accrued in accrued_rows:
        debt = periods.money(accrued) - paid_by_emp.get(employee_id, Decimal("0"))
        if debt > 0:
            debts[employee_id] = debt
    return debts


@router.get("/staff-payable", response_model=StaffPayableList, dependencies=READ)
async def list_staff_payable(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> StaffPayableList:
    """Полный ВАЛОВЫЙ баланс расчётов с сотрудниками по независимым субрегистрам.

    Кредиторка (мы должны) складывается из зарплаты, накопительного фонда и депозитов.
    Зарплата включает текущий заработок, хвосты финализированных ведомостей и долг
    ``on_demand``. Фонд и два депозитных контура не переносятся в зарплату, а показываются
    отдельными компонентами: производственный депозит берётся из ``DepositAccount.balance``,
    курьерский — из ``opening + top_up - return - forfeit``.

    Накопительный фонд берётся ровно тем же срезом, что страница «Расчёты → Накопительный
    фонд», и она здесь единственный источник истины: копят его только кассиры и повара
    (``production_payroll_positions``), обязательством считаются лишь ``active``-счета,
    видимые в операционном ростере. Списанный фонд уволенных — прибыль компании, а не долг;
    выплаченный за год фонд закрыт статусом ``paid_out`` и в кредиторку не возвращается.

    Дебиторка (нам должны) — все фактически выданные и ещё не погашенные авансы/займы,
    зарплатные выплаты вне ведомости (``EmployeePayout``) и переплата ``on_demand``.
    До финализации ведомости встречные суммы показываются валово в обе стороны; после
    финализации recovery/offset уменьшает соответствующий долг. Встречные суммы не гасят
    фонд или депозит автоматически: все компоненты остаются валовыми.

    ``admin_payroll_excluded`` («Не платить») — абсолютное исключение только из зарплатного
    субрегистра. Уже накопленный фонд или депозит остаётся обязательством компании. Обычный
    курьер поэтому может присутствовать самостоятельной строкой только с курьерским
    депозитом; старший курьер объединяется по ``employee_id`` со своей зарплатой.
    """
    as_of = date.today()
    salary_candidates = (
        await session.scalars(
            select(Employee)
            .where(
                Employee.status.in_(("active", "dismissing")),
                Employee.admin_payroll_excluded.is_(False),
            )
            .order_by(Employee.full_name)
        )
    ).all()
    salary_employees = list(salary_candidates)
    salary_employee_ids = {employee.id for employee in salary_employees}
    salary_earning_ids = {
        employee.id
        for employee in salary_employees
        if eligible_for_personal_report(
            employee.position,
            admin_payroll_excluded=employee.admin_payroll_excluded,
        )
    }

    fund_by_emp: dict[uuid.UUID, Decimal] = {}
    fund_current_year_by_emp: dict[uuid.UUID, Decimal] = {}
    fund_prior_years_by_emp: dict[uuid.UUID, Decimal] = {}
    fund_rows = (
        await session.execute(
            select(AccumulationFundAccount, Employee)
            .join(Employee, Employee.id == AccumulationFundAccount.employee_id)
            .where(
                Employee.is_freelancer_placeholder.is_(False),
                # Тот же срез, что и страница «Расчёты → Накопительный фонд»: фонд копят
                # только кассиры и повара, а обязательством остаются лишь active-счета.
                # Списанный (forfeited) фонд — прибыль компании, выплаченный закрыт.
                Employee.position.in_(production_payroll_positions()),
                AccumulationFundAccount.status == "active",
            )
        )
    ).all()
    for account, fund_employee in fund_rows:
        if account.year > as_of.year:
            continue
        if not fund_account_visible_in_roster(account, fund_employee):
            continue
        outstanding = max(periods.money(fund_outstanding(account)), Decimal("0.00"))
        if outstanding > 0:
            fund_by_emp[account.employee_id] = (
                fund_by_emp.get(account.employee_id, Decimal("0.00")) + outstanding
            )
            year_target = (
                fund_current_year_by_emp
                if account.year == as_of.year
                else fund_prior_years_by_emp
            )
            year_target[account.employee_id] = (
                year_target.get(account.employee_id, Decimal("0.00")) + outstanding
            )

    production_deposit_by_emp = {
        employee_id: max(periods.money(balance), Decimal("0.00"))
        for employee_id, balance in (
            await session.execute(
                select(DepositAccount.employee_id, DepositAccount.balance)
                .join(Employee, Employee.id == DepositAccount.employee_id)
                .where(
                    DepositAccount.balance > 0,
                    Employee.is_freelancer_placeholder.is_(False),
                )
            )
        ).all()
    }

    courier_accounts = (
        await session.scalars(
            select(CourierDepositAccount)
            .join(Employee, Employee.id == CourierDepositAccount.employee_id)
            .where(Employee.is_freelancer_placeholder.is_(False))
        )
    ).all()
    courier_balance_cents = {
        account.employee_id: int(account.opening_balance_cents) for account in courier_accounts
    }
    if courier_balance_cents:
        courier_transactions = (
            await session.execute(
                select(
                    CourierDepositTransaction.account_employee_id,
                    CourierDepositTransaction.transaction_type,
                    CourierDepositTransaction.amount_cents,
                ).where(
                    CourierDepositTransaction.account_employee_id.in_(courier_balance_cents),
                    CourierDepositTransaction.transaction_date <= as_of,
                )
            )
        ).all()
        for employee_id, transaction_type, amount_cents in courier_transactions:
            if transaction_type == CourierDepositTransactionType.TOP_UP:
                courier_balance_cents[employee_id] += int(amount_cents)
            else:
                courier_balance_cents[employee_id] -= int(amount_cents)
    courier_deposit_by_emp = {
        employee_id: periods.money(Decimal(max(balance_cents, 0)) / Decimal("100"))
        for employee_id, balance_cents in courier_balance_cents.items()
        if balance_cents > 0
    }

    employee_ids = (
        salary_employee_ids
        | set(fund_by_emp)
        | set(production_deposit_by_emp)
        | set(courier_deposit_by_emp)
    )
    employees_by_id = {employee.id: employee for employee in salary_employees}
    missing_employee_ids = employee_ids - set(employees_by_id)
    if missing_employee_ids:
        for employee in (
            await session.scalars(
                select(Employee)
                .where(Employee.id.in_(missing_employee_ids))
                .order_by(Employee.full_name)
            )
        ).all():
            employees_by_id[employee.id] = employee
    employees = list(employees_by_id.values())

    salary_ids = list(salary_employee_ids)
    on_demand_balances = await compute_on_demand_debt(session, salary_ids)
    current_on_demand_ids = {
        employee.id for employee in await list_on_demand_employees(session)
    } & salary_employee_ids

    finalized_unpaid = await _finalized_unpaid_by_employee(session)
    advances_by_emp: dict[uuid.UUID, Decimal] = {}
    loans_by_emp: dict[uuid.UUID, Decimal] = {}
    for employee_id, kind, remainder in (
        await session.execute(
            select(
                SalaryAdvance.employee_id,
                SalaryAdvance.kind,
                SalaryAdvance.amount - SalaryAdvance.recovered_amount,
            ).where(
                SalaryAdvance.status == "issued",
                SalaryAdvance.amount > SalaryAdvance.recovered_amount,
            )
        )
    ).all():
        target = advances_by_emp if kind == "advance" else loans_by_emp
        target[employee_id] = target.get(employee_id, Decimal("0.00")) + periods.money(remainder)

    # Выплаты зарплаты из ДДС до их зачёта ведомостью — такой же долг сотрудника компании,
    # как выданный аванс. ``owner_salary`` сюда не входит: он уже уменьшает on_demand_debt.
    salary_payouts_by_emp: dict[uuid.UUID, Decimal] = {}
    payout_rows = (
        await session.execute(
            select(
                EmployeePayout.employee_id,
                EmployeePayout.amount - EmployeePayout.offset_amount,
            ).where(
                EmployeePayout.kind.in_(("salary", "other")),
                EmployeePayout.status == "paid",
                EmployeePayout.amount > EmployeePayout.offset_amount,
            )
        )
    ).all()
    for employee_id, remainder in payout_rows:
        salary_payouts_by_emp[employee_id] = salary_payouts_by_emp.get(
            employee_id, Decimal("0.00")
        ) + periods.money(remainder)

    # Границы ФИНАЛИЗИРОВАННЫХ ведомостных периодов: калькулятор доступного-к-авансу строит
    # СИНТЕТИЧЕСКИЙ «текущий» период по календарю (не глядя в PayrollPeriod), поэтому в день
    # финализации и после неё одни и те же деньги считались ДВАЖДЫ — как earned-to-date
    # синтетического периода и как невыплаченный хвост той же финализированной ведомости.
    finalized_bounds = {
        (row[0], row[1])
        for row in (
            await session.execute(
                select(PayrollPeriod.start_date, PayrollPeriod.end_date).where(
                    PayrollPeriod.status == "finalized"
                )
            )
        ).all()
    }

    items: list[StaffPayableRow] = []
    payable_total = Decimal("0.00")
    receivable_total = Decimal("0.00")
    salary_total = Decimal("0.00")
    fund_total = Decimal("0.00")
    fund_current_year_total = Decimal("0.00")
    fund_prior_years_total = Decimal("0.00")
    production_deposit_total = Decimal("0.00")
    courier_deposit_total = Decimal("0.00")
    for employee in employees:
        if employee.id in salary_employee_ids:
            tail = finalized_unpaid.get(employee.id, Decimal("0"))
            on_demand = on_demand_balances.get(employee.id, {})
            on_demand_accrued = periods.money(on_demand.get("accrued", 0))
            on_demand_paid = periods.money(on_demand.get("paid", 0))
            on_demand_debt = periods.money(on_demand.get("debt", 0))
            advances = advances_by_emp.get(employee.id, Decimal("0.00"))
            loans = loans_by_emp.get(employee.id, Decimal("0.00"))
            salary_payouts = salary_payouts_by_emp.get(employee.id, Decimal("0.00"))

        if employee.id in salary_earning_ids:
            availability = await available_to_advance(session, employee, as_of)
            basis = availability.basis
            earned = periods.money(availability.earned_to_date)
            period_start = availability.period_start
            # Период уже финализирован → его заработок ЦЕЛИКОМ несёт хвост ведомости (tail),
            # поэтому синтетический earned обнуляем.
            period_settled = (
                period_start is not None
                and availability.period_end is not None
                and (period_start, availability.period_end) in finalized_bounds
            )
            if period_settled:
                earned = Decimal("0.00")

            if employee.id in current_on_demand_ids:
                # 52 500 ₽ у оклада 140 000 ₽ на 21 июля — это именно этот синтетический прорейт.
                # Он не является отдельной кредиторкой поверх накопленного on_demand-долга.
                earned = Decimal("0.00")

        else:
            basis = "courier_deposit" if employee.id in courier_deposit_by_emp else "none"
            earned = Decimal("0.00")
            if employee.id not in salary_employee_ids:
                tail = Decimal("0.00")
                on_demand_accrued = Decimal("0.00")
                on_demand_paid = Decimal("0.00")
                on_demand_debt = Decimal("0.00")
                advances = Decimal("0.00")
                loans = Decimal("0.00")
                salary_payouts = Decimal("0.00")

        salary_payable = earned + tail + _clamp_money(on_demand_debt)
        fund_payable = fund_by_emp.get(employee.id, Decimal("0.00"))
        fund_current_year = fund_current_year_by_emp.get(employee.id, Decimal("0.00"))
        fund_prior_years = fund_prior_years_by_emp.get(employee.id, Decimal("0.00"))
        production_deposit = production_deposit_by_emp.get(employee.id, Decimal("0.00"))
        courier_deposit = courier_deposit_by_emp.get(employee.id, Decimal("0.00"))
        deposit_payable = production_deposit + courier_deposit
        payable = salary_payable + fund_payable + deposit_payable
        receivable = (
            advances
            + loans
            + salary_payouts
            + _clamp_money(-on_demand_debt)
        )
        if payable <= 0 and receivable <= 0:
            continue
        payable_total += payable
        receivable_total += receivable
        salary_total += salary_payable
        fund_total += fund_payable
        fund_current_year_total += fund_current_year
        fund_prior_years_total += fund_prior_years
        production_deposit_total += production_deposit
        courier_deposit_total += courier_deposit
        items.append(
            StaffPayableRow(
                employee_id=employee.id,
                full_name=employee.full_name,
                position=employee.position,
                staff_group=(
                    "courier"
                    if employee.id in courier_balance_cents and not is_senior_courier(employee)
                    else "staff"
                ),
                basis=("on_demand" if employee.id in current_on_demand_ids else basis),
                earned_to_date=_float(earned),
                on_demand_accrued=_float(on_demand_accrued),
                on_demand_paid=_float(on_demand_paid),
                on_demand_debt=_float(on_demand_debt),
                already_advanced=_float(advances),
                advances_outstanding=_float(advances),
                finalized_unpaid=_float(tail),
                loans_outstanding=_float(loans),
                salary_payouts_outstanding=_float(salary_payouts),
                salary_payable=_float(salary_payable),
                fund_payable=_float(fund_payable),
                fund_current_year_payable=_float(fund_current_year),
                fund_prior_years_payable=_float(fund_prior_years),
                production_deposit_payable=_float(production_deposit),
                courier_deposit_payable=_float(courier_deposit),
                deposit_payable=_float(deposit_payable),
                payable=_float(payable),
                receivable=_float(receivable),
            )
        )
    items.sort(key=lambda row: max(row.payable, row.receivable), reverse=True)
    return StaffPayableList(
        as_of=as_of,
        total=_float(payable_total),
        receivable_total=_float(receivable_total),
        salary_total=_float(salary_total),
        fund_total=_float(fund_total),
        fund_current_year_total=_float(fund_current_year_total),
        fund_prior_years_total=_float(fund_prior_years_total),
        production_deposit_total=_float(production_deposit_total),
        courier_deposit_total=_float(courier_deposit_total),
        deposit_total=_float(production_deposit_total + courier_deposit_total),
        items=items,
    )


class ExpenseMonthCell(BaseModel):
    month: date
    article_id: uuid.UUID | None = None
    article_name: str
    amount: float


class ExpenseByMonthList(BaseModel):
    """Признанный расход по месяцам и статьям — то, из чего строится P&L.

    ``unattributed`` — расход без статьи ДДС. В отчёт о прибыли его отнести некуда, и прятать
    это нельзя: пока цифра не ноль, любой P&L будет неполным ровно на неё.

    ``without_primary`` — расход, признанный без первичного документа (самоакт или строка
    ручного платежа). В управленческом P&L он полноправен, в налоговую базу УСН идти не может:
    инспекция снимет расход, у которого нет документа поставщика.
    """

    months: list[date]
    items: list[ExpenseMonthCell]
    total: float
    unattributed: float
    without_primary: float


@router.get("/expenses/by-month", response_model=ExpenseByMonthList, dependencies=READ)
async def list_expenses_by_month(
    session: Annotated[AsyncSession, Depends(get_session)],
    date_from: Annotated[date, Query()],
    date_to: Annotated[date, Query()],
    counterparty_id: Annotated[uuid.UUID | None, Query()] = None,
    article_id: Annotated[uuid.UUID | None, Query()] = None,
) -> ExpenseByMonthList:
    """Признанный расход по месяцам × статьям.

    Первый потребитель начислений, кроме витрины, которая их же и заводит: до него признание
    было замкнуто само на себя, и ошибка признания (двойной расход, пропавший месяц) не
    проявлялась нигде — деньги сходились всегда.

    Многомесячный документ раскладывается по календарным месяцам периода, а не падает целиком
    в месяц признания: акт на 36 000 ₽ за июль-сентябрь даёт по 12 000 ₽ в каждый месяц.
    """
    report = await expense_report.build_expense_report(
        session,
        date_from=date_from,
        date_to=date_to,
        counterparty_id=counterparty_id,
        article_id=article_id,
    )
    return ExpenseByMonthList(
        months=report.months,
        items=[
            ExpenseMonthCell(
                month=cell.month,
                article_id=cell.article_id,
                article_name=cell.article_name,
                amount=_float(cell.amount),
            )
            for cell in report.cells
        ],
        total=_float(report.total),
        unattributed=_float(report.unattributed),
        without_primary=_float(report.without_primary),
    )


class BalanceAsOfRow(BaseModel):
    counterparty_id: uuid.UUID
    counterparty_name: str
    receivable: float
    payable: float


class BalanceAsOfList(BaseModel):
    """Остатки расчётов с контрагентами на дату — источник строк баланса.

    ``approximate_settlements`` — гашения, чью дату хозяйственного события установить не
    удалось (бартерные зачёты: денежного ключа у них нет), они учтены по дате записи. Пока
    цифра не ноль, остаток на прошедшую дату может отличаться от истинного на неё — и это
    надо знать до того, как расхождение начнут искать в другом месте.
    """

    as_of: date
    items: list[BalanceAsOfRow]
    receivable_total: float
    payable_total: float
    approximate_settlements: float


@router.get("/balances/as-of", response_model=BalanceAsOfList, dependencies=READ)
async def list_balances_as_of(
    session: Annotated[AsyncSession, Depends(get_session)],
    as_of: Annotated[date, Query()],
) -> BalanceAsOfList:
    """Кто сколько был должен на указанную дату.

    Плитка «Остатки» отвечает на вопрос «сколько должны СЕЙЧАС» и для баланса не годится:
    документ, оплаченный 5 августа, сегодня закрыт, а на 31 июля был живой кредиторкой.
    Здесь обязательство существует, если документ к дате вступил в силу, и гасится только
    теми платежами, которые к ней уже произошли.
    """
    report = await balance_as_of.build_balance_as_of(session, as_of=as_of)
    return BalanceAsOfList(
        as_of=report.as_of,
        items=[
            BalanceAsOfRow(
                counterparty_id=row.counterparty_id,
                counterparty_name=row.counterparty_name,
                receivable=_float(row.receivable),
                payable=_float(row.payable),
            )
            for row in report.rows
        ],
        receivable_total=_float(report.receivable_total),
        payable_total=_float(report.payable_total),
        approximate_settlements=_float(report.approximate_settlements),
    )

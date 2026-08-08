"""Отчёт о прибылях и убытках (ОПиУ) — чтение каскада за месяц.

КОНТРАКТ ОТВЕТА НЕСЁТ РАЗЛИЧИЕ «НОЛЬ vs НЕТ ДАННЫХ», и это главное решение файла.
``amount`` равен ``null``, когда цифры нет: источник не настроен, зеркало iiko не заливалось,
контрагент не прислал документ. Ноль печатается только при статусе ``zero_confirmed`` —
источник ответил, движения не было.

Если бы API отдавал ноль со статусом отдельным полем, любой потребитель — страница, экспорт,
будущий баланс — рано или поздно сложил бы нули и напечатал итог, который выглядит как факт.
С ``null`` сложение требует явного решения, что делать с неизвестностью.

Суммы отдаются СТРОКАМИ, а не числами: json-число проходит через float и теряет копейки на
отчёте из восьмидесяти строк. Фронт форматирует строку, не считая её.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, field_serializer
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor, get_current_actor, require_permission
from app.db.session import get_session
from app.services import accounting_periods
from app.services.pnl import drill as drill_service
from app.services.pnl import ledgers as ledger_service
from app.services.pnl import projector, workup_review
from app.services.pnl.types import LineValue, PnlReport

router = APIRouter()


class ComponentOut(BaseModel):
    """Слагаемое строки: один источник."""

    model_config = ConfigDict(from_attributes=True)

    stream: str
    amount: Decimal | None
    status: str
    excluded_amount: Decimal
    unrecognized_paid: Decimal
    note: str | None

    @field_serializer("amount", "excluded_amount", "unrecognized_paid")
    def _money(self, value: Decimal | None) -> str | None:
        return None if value is None else f"{value:.2f}"


class LineOut(BaseModel):
    code: str
    title: str
    block: str
    kind: str
    level: int
    sign_role: int
    month_basis: str
    amount: Decimal | None
    status: str
    pct_of_revenue: Decimal | None
    norm_min: Decimal | None
    norm_max: Decimal | None
    missing_lines: list[str]
    source_note: str | None
    drill_available: bool
    components: list[ComponentOut]

    @field_serializer("amount")
    def _amount(self, value: Decimal | None) -> str | None:
        return None if value is None else f"{value:.2f}"

    @field_serializer("pct_of_revenue", "norm_min", "norm_max")
    def _ratio(self, value: Decimal | None) -> str | None:
        return None if value is None else f"{value:.4f}"


class ReconciliationOut(BaseModel):
    """Уравнение замкнутости денежного слоя — защита от тихой потери."""

    cash_out_total: Decimal
    cash_in_total: Decimal
    by_verdict: dict[str, Decimal]
    unmapped: Decimal
    unmapped_count: int
    balanced: bool
    drift: Decimal
    missed_count: int = 0

    @field_serializer("cash_out_total", "cash_in_total", "unmapped", "drift")
    def _money(self, value: Decimal) -> str:
        return f"{value:.2f}"

    @field_serializer("by_verdict")
    def _verdicts(self, value: dict[str, Decimal]) -> dict[str, str]:
        return {key: f"{amount:.2f}" for key, amount in value.items()}


class WarningOut(BaseModel):
    code: str
    message: str
    line_code: str | None
    amount: Decimal | None

    @field_serializer("amount")
    def _money(self, value: Decimal | None) -> str | None:
        return None if value is None else f"{value:.2f}"


class PnlReportOut(BaseModel):
    month: date
    accounting_start: date
    lines: list[LineOut]
    reconciliation: ReconciliationOut
    warnings: list[WarningOut]
    quality: dict[str, Any]


class PayrollLedgerAmountsOut(BaseModel):
    base_pay: Decimal
    percent_pay: Decimal
    bonuses: Decimal
    vacation_pay: Decimal
    fund_accrual: Decimal
    other_penalties: Decimal
    audit_penalties: Decimal
    fund_forfeited: Decimal
    deposit_written_off: Decimal
    salary_expense: Decimal
    fund_expense: Decimal

    @field_serializer(
        "base_pay",
        "percent_pay",
        "bonuses",
        "vacation_pay",
        "fund_accrual",
        "other_penalties",
        "audit_penalties",
        "fund_forfeited",
        "deposit_written_off",
        "salary_expense",
        "fund_expense",
    )
    def _money(self, value: Decimal) -> str:
        return f"{value:.2f}"


class PayrollLedgerEmployeeOut(PayrollLedgerAmountsOut):
    employee_id: uuid.UUID | None
    employee_name: str
    roles: list[str]


class PayrollLedgerOut(BaseModel):
    month: date
    totals: PayrollLedgerAmountsOut
    employees: list[PayrollLedgerEmployeeOut]


class PartnerCommissionLedgerRowOut(BaseModel):
    partner_name: str
    revenue_amount: Decimal
    commission_rate: Decimal
    commission_amount: Decimal
    rows_count: int
    source_ref: str
    synced_at: datetime

    @field_serializer("revenue_amount", "commission_amount")
    def _money(self, value: Decimal) -> str:
        return f"{value:.2f}"

    @field_serializer("commission_rate")
    def _rate(self, value: Decimal) -> str:
        return f"{value:.6f}"


class PartnerCommissionLedgerOut(BaseModel):
    month: date
    revenue_amount: Decimal | None
    commission_amount: Decimal | None
    synced_at: datetime | None
    rows: list[PartnerCommissionLedgerRowOut]

    @field_serializer("revenue_amount", "commission_amount")
    def _money(self, value: Decimal | None) -> str | None:
        return None if value is None else f"{value:.2f}"


class GoodsLedgerSummaryOut(BaseModel):
    line_code: str
    line_title: str
    source_kind: str
    amount: Decimal | None
    details_amount: Decimal
    details_complete: bool
    details_expected: bool
    synced_at: datetime | None
    opening_amount: Decimal | None = None
    receipts_amount: Decimal | None = None
    closing_amount: Decimal | None = None
    surplus_amount: Decimal | None = None
    shortage_amount: Decimal | None = None

    @field_serializer(
        "amount",
        "details_amount",
        "opening_amount",
        "receipts_amount",
        "closing_amount",
        "surplus_amount",
        "shortage_amount",
    )
    def _money(self, value: Decimal | None) -> str | None:
        return None if value is None else f"{value:.2f}"


class GoodsLedgerRowOut(BaseModel):
    line_code: str
    line_title: str
    source_kind: str
    product_guid: str
    product_name: str
    source_amount: Decimal
    amount: Decimal
    rows_count: int
    opening_quantity: Decimal | None = None
    opening_amount: Decimal | None = None
    receipts_amount: Decimal | None = None
    closing_quantity: Decimal | None = None
    closing_amount: Decimal | None = None
    audit_date: date | None = None
    surplus_amount: Decimal = Decimal("0.00")

    @field_serializer(
        "source_amount",
        "amount",
        "opening_amount",
        "receipts_amount",
        "closing_amount",
        "surplus_amount",
    )
    def _money(self, value: Decimal | None) -> str | None:
        return None if value is None else f"{value:.2f}"

    @field_serializer("opening_quantity", "closing_quantity")
    def _quantity(self, value: Decimal | None) -> str | None:
        return None if value is None else f"{value:.6f}"


class GoodsLedgerOut(BaseModel):
    month: date
    summaries: list[GoodsLedgerSummaryOut]
    rows: list[GoodsLedgerRowOut]


class GoodsClassificationOptionOut(BaseModel):
    line_code: str
    line_title: str
    source_kind: str
    temporary: bool


class GoodsClassificationSourceOut(BaseModel):
    source_kind: str
    amount: Decimal | None
    rows_count: int
    observed_in_month: bool
    last_seen_month: date | None

    @field_serializer("amount")
    def _money(self, value: Decimal | None) -> str | None:
        return None if value is None else f"{value:.2f}"


class GoodsClassificationRowOut(BaseModel):
    product_guid: str
    product_name: str
    product_code: str | None
    selected_source_kind: str | None
    sources: list[GoodsClassificationSourceOut]
    line_code: str | None
    line_title: str | None
    status: str
    revision_product: bool
    note: str | None
    updated_at: datetime | None
    #: Тип позиции в справочнике iiko: GOODS / DISH / PREPARED / MODIFIER / SERVICE.
    product_type: str | None = None
    #: Позиция не-товарного типа со старой разметкой — её можно только снять с учёта.
    needs_removal: bool = False


class GoodsClassificationLedgerOut(BaseModel):
    month: date
    attention_count: int
    attention_amount: Decimal
    rules_count: int
    options: list[GoodsClassificationOptionOut]
    rows: list[GoodsClassificationRowOut]
    removal_count: int = 0

    @field_serializer("attention_amount")
    def _money(self, value: Decimal) -> str:
        return f"{value:.2f}"


class GoodsClassificationUpdateIn(BaseModel):
    source_kind: str
    status: str
    line_code: str | None = None
    note: str | None = None


class RecognitionLedgerTotalsOut(BaseModel):
    recognized: Decimal
    waiting_document: Decimal
    missing_period: Decimal
    without_primary: Decimal
    unattributed: Decimal
    unrecognized: Decimal

    @field_serializer(
        "recognized",
        "waiting_document",
        "missing_period",
        "without_primary",
        "unattributed",
        "unrecognized",
    )
    def _money(self, value: Decimal) -> str:
        return f"{value:.2f}"


class RecognitionLedgerRowOut(BaseModel):
    status: str
    source_kind: str
    source_id: uuid.UUID
    counterparty_id: uuid.UUID | None
    counterparty_name: str
    article_id: uuid.UUID | None
    article_name: str
    line_code: str | None
    line_title: str | None
    amount: Decimal
    event_date: date | None
    service_period_start: date | None
    service_period_end: date | None
    has_primary: bool | None
    reason: str

    @field_serializer("amount")
    def _money(self, value: Decimal) -> str:
        return f"{value:.2f}"


class RecognitionLedgerOut(BaseModel):
    month: date
    totals: RecognitionLedgerTotalsOut
    rows: list[RecognitionLedgerRowOut]


def _line_out(line: LineValue) -> LineOut:
    return LineOut(
        code=line.code,
        title=line.title,
        block=line.block,
        kind=line.kind,
        level=line.level,
        sign_role=line.sign_role,
        month_basis=line.month_basis,
        amount=line.amount,
        status=line.status.value,
        pct_of_revenue=line.pct_of_revenue,
        norm_min=line.norm_min,
        norm_max=line.norm_max,
        missing_lines=line.missing_lines,
        source_note=line.source_note,
        drill_available=line.drill_available,
        components=[
            ComponentOut(
                stream=component.stream,
                amount=component.amount,
                status=component.status.value,
                excluded_amount=component.excluded_amount,
                unrecognized_paid=component.unrecognized_paid,
                note=component.note,
            )
            for component in line.components
        ],
    )


def _report_out(report: PnlReport) -> PnlReportOut:
    return PnlReportOut(
        month=report.month,
        accounting_start=projector.ACCOUNTING_START,
        lines=[_line_out(line) for line in report.lines],
        reconciliation=ReconciliationOut(
            cash_out_total=report.reconciliation.cash_out_total,
            cash_in_total=report.reconciliation.cash_in_total,
            by_verdict=report.reconciliation.by_verdict,
            unmapped=report.reconciliation.unmapped,
            unmapped_count=report.reconciliation.unmapped_count,
            balanced=report.reconciliation.balanced,
            drift=report.reconciliation.drift,
            missed_count=report.reconciliation.missed_count,
        ),
        warnings=[
            WarningOut(
                code=warning.code,
                message=warning.message,
                line_code=warning.line_code,
                amount=warning.amount,
            )
            for warning in report.warnings
        ],
        quality={key: str(value) for key, value in report.quality.items()},
    )


class DrillRowOut(BaseModel):
    title: str
    subtitle: str | None
    row_date: date | None
    amount: Decimal
    kind: str

    @field_serializer("amount")
    def _money(self, value: Decimal) -> str:
        return f"{value:.2f}"


class DrillGroupOut(BaseModel):
    stream: str
    title: str
    amount: Decimal
    note: str | None
    counts_in_total: bool
    rows: list[DrillRowOut]

    @field_serializer("amount")
    def _money(self, value: Decimal) -> str:
        return f"{value:.2f}"


class DrillAsideOut(BaseModel):
    """То, что прошло по строке, но её число не меняет, — одной цифрой вместо таблицы."""

    amount: Decimal
    count: int
    reason: str

    @field_serializer("amount")
    def _money(self, value: Decimal) -> str:
        return f"{value:.2f}"


class DrillOut(BaseModel):
    line_code: str
    line_title: str
    month: date
    total: Decimal
    undecomposed: list[str]
    asides: list[DrillAsideOut]
    groups: list[DrillGroupOut]

    @field_serializer("total")
    def _money(self, value: Decimal) -> str:
        return f"{value:.2f}"


def _month_or_422(month: str) -> date:
    try:
        year, month_number = (int(part) for part in month.split("-"))
        return date(year, month_number, 1)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Некорректный месяц: {month}",
        ) from error


@router.get(
    "",
    response_model=PnlReportOut,
    dependencies=[Depends(require_permission("reports.pnl.read"))],
)
async def get_pnl(
    month: Annotated[
        str,
        Query(
            pattern=r"^\d{4}-\d{2}$",
            description="Месяц отчёта в формате ГГГГ-ММ",
        ),
    ],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PnlReportOut:
    """Каскад ОПиУ за месяц.

    Месяц принимается строкой ГГГГ-ММ, а не датой: у отчёта нет дня, и принимать полную дату
    значило бы делать вид, что 15 июля отличается от 1 июля.
    """
    report = await projector.build_report(session, _month_or_422(month))
    return _report_out(report)


def _payroll_amounts(value) -> dict[str, Decimal]:
    return {
        "base_pay": value.base_pay,
        "percent_pay": value.percent_pay,
        "bonuses": value.bonuses,
        "vacation_pay": value.vacation_pay,
        "fund_accrual": value.fund_accrual,
        "other_penalties": value.other_penalties,
        "audit_penalties": value.audit_penalties,
        "fund_forfeited": value.fund_forfeited,
        "deposit_written_off": value.deposit_written_off,
        "salary_expense": value.salary_expense,
        "fund_expense": value.fund_expense,
    }


@router.get(
    "/ledgers/payroll",
    response_model=PayrollLedgerOut,
    dependencies=[Depends(require_permission("reports.pnl.read"))],
)
async def get_payroll_ledger(
    month: Annotated[str, Query(pattern=r"^\d{4}-\d{2}$")],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PayrollLedgerOut:
    ledger = await ledger_service.build_payroll_ledger(session, _month_or_422(month))
    return PayrollLedgerOut(
        month=ledger.month,
        totals=PayrollLedgerAmountsOut(**_payroll_amounts(ledger.totals)),
        employees=[
            PayrollLedgerEmployeeOut(
                employee_id=row.employee_id,
                employee_name=row.employee_name,
                roles=row.roles,
                **_payroll_amounts(row),
            )
            for row in ledger.employees
        ],
    )


@router.get(
    "/ledgers/partners",
    response_model=PartnerCommissionLedgerOut,
    dependencies=[Depends(require_permission("reports.pnl.read"))],
)
async def get_partner_commission_ledger(
    month: Annotated[str, Query(pattern=r"^\d{4}-\d{2}$")],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PartnerCommissionLedgerOut:
    ledger = await ledger_service.build_partner_commission_ledger(session, _month_or_422(month))
    return PartnerCommissionLedgerOut(
        month=ledger.month,
        revenue_amount=ledger.revenue_amount,
        commission_amount=ledger.commission_amount,
        synced_at=ledger.synced_at,
        rows=[
            PartnerCommissionLedgerRowOut.model_validate(row, from_attributes=True)
            for row in ledger.rows
        ],
    )


@router.get(
    "/ledgers/goods",
    response_model=GoodsLedgerOut,
    dependencies=[Depends(require_permission("reports.pnl.read"))],
)
async def get_goods_ledger(
    month: Annotated[str, Query(pattern=r"^\d{4}-\d{2}$")],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> GoodsLedgerOut:
    ledger = await ledger_service.build_goods_ledger(session, _month_or_422(month))
    return GoodsLedgerOut(
        month=ledger.month,
        summaries=[
            GoodsLedgerSummaryOut(
                line_code=row.line_code,
                line_title=row.line_title,
                source_kind=row.source_kind,
                amount=row.amount,
                details_amount=row.details_amount,
                details_complete=row.details_complete,
                details_expected=row.details_expected,
                synced_at=row.synced_at,
                opening_amount=row.opening_amount,
                receipts_amount=row.receipts_amount,
                closing_amount=row.closing_amount,
                surplus_amount=row.surplus_amount,
                shortage_amount=row.shortage_amount,
            )
            for row in ledger.summaries
        ],
        rows=[
            GoodsLedgerRowOut(
                line_code=row.line_code,
                line_title=row.line_title,
                source_kind=row.source_kind,
                product_guid=row.product_guid,
                product_name=row.product_name,
                source_amount=row.source_amount,
                amount=row.amount,
                rows_count=row.rows_count,
                opening_quantity=row.opening_quantity,
                opening_amount=row.opening_amount,
                receipts_amount=row.receipts_amount,
                closing_quantity=row.closing_quantity,
                closing_amount=row.closing_amount,
                audit_date=row.audit_date,
                surplus_amount=row.surplus_amount,
            )
            for row in ledger.rows
        ],
    )


def _goods_classification_out(
    ledger: ledger_service.GoodsClassificationLedger,
) -> GoodsClassificationLedgerOut:
    return GoodsClassificationLedgerOut(
        month=ledger.month,
        attention_count=ledger.attention_count,
        attention_amount=ledger.attention_amount,
        rules_count=ledger.rules_count,
        options=[
            GoodsClassificationOptionOut(
                line_code=item.line_code,
                line_title=item.line_title,
                source_kind=item.source_kind,
                temporary=item.temporary,
            )
            for item in ledger.options
        ],
        rows=[
            GoodsClassificationRowOut(
                product_guid=item.product_guid,
                product_name=item.product_name,
                product_code=item.product_code,
                selected_source_kind=item.selected_source_kind,
                sources=[
                    GoodsClassificationSourceOut(
                        source_kind=source.source_kind,
                        amount=source.amount,
                        rows_count=source.rows_count,
                        observed_in_month=source.observed_in_month,
                        last_seen_month=source.last_seen_month,
                    )
                    for source in item.sources
                ],
                line_code=item.line_code,
                line_title=item.line_title,
                status=item.status,
                revision_product=item.revision_product,
                note=item.note,
                updated_at=item.updated_at,
                product_type=item.product_type,
                needs_removal=item.needs_removal,
            )
            for item in ledger.rows
        ],
        removal_count=ledger.removal_count,
    )


@router.get(
    "/ledgers/goods/classifications",
    response_model=GoodsClassificationLedgerOut,
    dependencies=[Depends(require_permission("reports.pnl.read"))],
)
async def get_goods_classifications(
    month: Annotated[str, Query(pattern=r"^\d{4}-\d{2}$")],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> GoodsClassificationLedgerOut:
    ledger = await ledger_service.build_goods_classifications(session, _month_or_422(month))
    return _goods_classification_out(ledger)


@router.patch(
    "/ledgers/goods/classifications/{product_guid}",
    response_model=GoodsClassificationLedgerOut,
    dependencies=[Depends(require_permission("reports.pnl.manual_input"))],
)
async def update_goods_classification(
    product_guid: str,
    payload: GoodsClassificationUpdateIn,
    month: Annotated[str, Query(pattern=r"^\d{4}-\d{2}$")],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> GoodsClassificationLedgerOut:
    try:
        ledger = await ledger_service.save_goods_classification(
            session,
            display_month=_month_or_422(month),
            product_guid=product_guid,
            source_kind=payload.source_kind,
            status=payload.status,
            line_code=payload.line_code,
            note=(payload.note or "").strip() or None,
            user_id=actor.user_id,
        )
    except accounting_periods.PeriodClosed as error:
        # Закрытый месяц — конфликт состояния (409), а не негодный ввод (422). Ловим ДО
        # ValueError: PeriodClosed его подкласс, иначе отказ уехал бы к владельцу под чужим кодом.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from error
    return _goods_classification_out(ledger)


@router.delete(
    "/ledgers/goods/classifications/{product_guid}",
    response_model=GoodsClassificationLedgerOut,
    dependencies=[Depends(require_permission("reports.pnl.manual_input"))],
)
async def remove_goods_classification(
    product_guid: str,
    month: Annotated[str, Query(pattern=r"^\d{4}-\d{2}$")],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> GoodsClassificationLedgerOut:
    """Снять с учёта позицию не-товарного типа: блюдо, заготовку, модификатор."""
    try:
        ledger = await ledger_service.remove_goods_classification(
            session,
            display_month=_month_or_422(month),
            product_guid=product_guid,
        )
    except accounting_periods.PeriodClosed as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from error
    return _goods_classification_out(ledger)


@router.get(
    "/ledgers/recognition",
    response_model=RecognitionLedgerOut,
    dependencies=[Depends(require_permission("reports.pnl.read"))],
)
async def get_recognition_ledger(
    month: Annotated[str, Query(pattern=r"^\d{4}-\d{2}$")],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RecognitionLedgerOut:
    ledger = await ledger_service.build_recognition_ledger(session, _month_or_422(month))
    return RecognitionLedgerOut(
        month=ledger.month,
        totals=RecognitionLedgerTotalsOut(
            recognized=ledger.totals.recognized,
            waiting_document=ledger.totals.waiting_document,
            missing_period=ledger.totals.missing_period,
            without_primary=ledger.totals.without_primary,
            unattributed=ledger.totals.unattributed,
            unrecognized=ledger.totals.unrecognized,
        ),
        rows=[
            RecognitionLedgerRowOut(
                status=row.status,
                source_kind=row.source_kind,
                source_id=row.source_id,
                counterparty_id=row.counterparty_id,
                counterparty_name=row.counterparty_name,
                article_id=row.article_id,
                article_name=row.article_name,
                line_code=row.line_code,
                line_title=row.line_title,
                amount=row.amount,
                event_date=row.event_date,
                service_period_start=row.service_period_start,
                service_period_end=row.service_period_end,
                has_primary=row.has_primary,
                reason=row.reason,
            )
            for row in ledger.rows
        ],
    )


@router.get(
    "/lines/{line_code}",
    response_model=DrillOut,
    dependencies=[Depends(require_permission("reports.pnl.read"))],
)
async def get_pnl_line(
    line_code: str,
    month: Annotated[str, Query(pattern=r"^\d{4}-\d{2}$")],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DrillOut:
    """Из чего сложилась строка: контрагенты, сотрудники, документы, направления.

    Отдельным запросом, а не полем отчёта: расшифровки нужны по одной строке за раз, а
    восемьдесят разложений в каждом ответе утяжелили бы главный экран ради данных, которые
    в девяти случаях из десяти никто не откроет.
    """
    drill = await drill_service.build_drill(session, line_code, _month_or_422(month))
    if drill is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Строки {line_code} нет в справочнике ОПиУ",
        )
    return DrillOut(
        line_code=drill.line_code,
        line_title=drill.line_title,
        month=drill.month,
        total=drill.total,
        undecomposed=drill.undecomposed,
        asides=[
            DrillAsideOut(amount=aside.amount, count=aside.count, reason=aside.reason)
            for aside in drill.asides
        ],
        groups=[
            DrillGroupOut(
                stream=group.stream,
                title=group.title,
                amount=group.amount,
                note=group.note,
                counts_in_total=group.counts_in_total,
                rows=[
                    DrillRowOut(
                        title=row.title,
                        subtitle=row.subtitle,
                        row_date=row.row_date,
                        amount=row.amount,
                        kind=row.kind,
                    )
                    for row in group.rows
                ],
            )
            for group in drill.groups
        ],
    )


class WorkupReviewRowOut(BaseModel):
    """Строка очереди «Требует проверки»: товар, который автоматика считает проработкой."""

    id: uuid.UUID
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

    @field_serializer("purchase_amount", "quantity")
    def _money(self, value: Decimal | None) -> str | None:
        return None if value is None else f"{value:.3f}"


class WorkupReviewLedgerOut(BaseModel):
    month: date
    pending_count: int
    rows: list[WorkupReviewRowOut]


def _workup_review_out(month: date, rows: list) -> WorkupReviewLedgerOut:
    return WorkupReviewLedgerOut(
        month=month,
        pending_count=sum(1 for row in rows if row.status == "pending"),
        rows=[
            WorkupReviewRowOut(
                id=row.id,
                product_guid=row.product_guid,
                product_name=row.product_name,
                purchase_amount=row.purchase_amount,
                quantity=row.quantity,
                status=row.status,
                invoice_id=row.invoice_id,
                invoice_number=row.invoice_number,
                supplier_name=row.supplier_name,
                writeoff_document_id=row.writeoff_document_id,
                writeoff_number=row.writeoff_number,
                writeoff_posted=row.writeoff_posted,
                writeoff_error=row.writeoff_error,
                decided_at=row.decided_at,
            )
            for row in rows
        ],
    )


@router.get(
    "/workup-review",
    response_model=WorkupReviewLedgerOut,
    dependencies=[Depends(require_permission("reports.pnl.read"))],
)
async def get_workup_review(
    month: Annotated[str, Query(pattern=r"^\d{4}-\d{2}$")],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> WorkupReviewLedgerOut:
    """Очередь подтверждения проработки за месяц."""
    month_start = _month_or_422(month)
    # Очередь достраивается на чтении: товары проработки появляются ночным синком ОПиУ, и
    # ждать отдельного прогона, чтобы задать вопрос, незачем.
    await workup_review.build_review_queue(session, month_start)
    await session.commit()
    rows = await workup_review.list_reviews(session, month_start)
    return _workup_review_out(month_start, rows)


@router.post(
    "/workup-review/{review_id}/confirm",
    response_model=WorkupReviewLedgerOut,
    dependencies=[Depends(require_permission("reports.pnl.manual_input"))],
)
async def confirm_workup_review(
    review_id: uuid.UUID,
    month: Annotated[str, Query(pattern=r"^\d{4}-\d{2}$")],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> WorkupReviewLedgerOut:
    """«Да, проработка» — система списывает товар актом в iiko."""
    month_start = _month_or_422(month)
    try:
        await workup_review.confirm_workup(session, review_id, user_id=actor.user_id)
    except workup_review.WorkupReviewError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error
    rows = await workup_review.list_reviews(session, month_start)
    return _workup_review_out(month_start, rows)


@router.post(
    "/workup-review/{review_id}/reject",
    response_model=WorkupReviewLedgerOut,
    dependencies=[Depends(require_permission("reports.pnl.manual_input"))],
)
async def reject_workup_review(
    review_id: uuid.UUID,
    month: Annotated[str, Query(pattern=r"^\d{4}-\d{2}$")],
    session: Annotated[AsyncSession, Depends(get_session)],
    actor: Annotated[CurrentActor, Depends(get_current_actor)],
) -> WorkupReviewLedgerOut:
    """«Нет, не проработка» — товар возвращается в обычную разметку."""
    month_start = _month_or_422(month)
    try:
        await workup_review.reject_workup(session, review_id, user_id=actor.user_id)
    except workup_review.WorkupReviewError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error
    rows = await workup_review.list_reviews(session, month_start)
    return _workup_review_out(month_start, rows)

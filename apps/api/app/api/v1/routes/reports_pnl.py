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

from datetime import date
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, field_serializer
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.db.session import get_session
from app.services.pnl import drill as drill_service
from app.services.pnl import projector
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

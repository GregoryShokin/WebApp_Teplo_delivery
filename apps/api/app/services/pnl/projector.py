"""Проектор ОПиУ: собирает отчёт месяца из первичных контуров при каждом запросе.

ПОЧЕМУ ПРОЕКТОР, А НЕ ВИТРИНА. Управленческий учёт правится задним числом постоянно: приезжает
УПД за прошлый месяц, дефинализируется ведомость, переклассифицируется проводка, отменяется
ревизия. Витрина на такие правки не реагирует, пока её не пересчитают, и расхождение с
первичкой остаётся невидимым ровно до того момента, когда по отчёту приняли решение. Здесь
таблиц с суммами отчёта нет вовсе, поэтому класса «тихо устаревшие цифры» не существует по
построению. Снимок появится только вместе с заморозкой месяца — как фиксация закрытого
периода, а не как кэш.

СТОИМОСТЬ РЕШЕНИЯ ЧЕСТНАЯ: расчёт идёт при каждом открытии страницы. Бюджет месяца — около
600 денежных проводок с двумя индексами, полсотни начислений, десяток агрегатов из смежных
модулей и один SELECT из зеркала iiko. Ноль синхронных походов во внешние системы.
"""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PnlLine
from app.services.pnl import formulas
from app.services.pnl.sources import cashflow as cash_source
from app.services.pnl.sources import fixed_assets as fixed_assets_source
from app.services.pnl.sources import manual as manual_source
from app.services.pnl.sources import payroll as payroll_source
from app.services.pnl.sources import recognition as recognition_source
from app.services.pnl.sources import taxes as taxes_source
from app.services.pnl.types import (
    Component,
    LineStatus,
    LineValue,
    PnlReport,
    Reconciliation,
    Verdict,
    Warning,
)

#: Управленческий учёт ведётся с этой даты. Месяцы раньше показываются пустыми со своим
#: статусом, а не нулями: до неё данные в приложении неполны, и рисовать по ним прибыль —
#: значит выдавать пробел за факт.
ACCOUNTING_START = date(2026, 7, 1)


def month_bounds(month: date) -> tuple[date, date]:
    """Первое и последнее число месяца. Обе границы включительные."""
    first = month.replace(day=1)
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    return first, last


async def _catalog(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(select(PnlLine).order_by(PnlLine.sort_order))).scalars()
    return [
        {
            "code": row.code,
            "title": row.title,
            "block": row.block,
            "kind": row.kind,
            "formula": row.formula,
            "sort_order": row.sort_order,
            "level": row.level,
            "sign_role": row.sign_role,
            "month_basis": row.month_basis,
            "direction_aware": row.direction_aware,
            "norm_min": row.norm_min,
            "norm_max": row.norm_max,
            "status": row.status,
            "source_note": row.source_note,
        }
        for row in rows
    ]


async def build_report(session: AsyncSession, month: date) -> PnlReport:
    """Собрать ОПиУ за месяц."""
    month_start, month_end = month_bounds(month)
    catalog = await _catalog(session)
    report = PnlReport(month=month_start)

    lines: dict[str, LineValue] = {}
    for row in catalog:
        lines[row["code"]] = LineValue(
            code=row["code"],
            title=row["title"],
            block=row["block"],
            kind=row["kind"],
            level=row["level"],
            sort_order=row["sort_order"],
            sign_role=row["sign_role"],
            month_basis=row["month_basis"],
            amount=None,
            status=(
                LineStatus.NOT_USED if row["status"] != "active" else LineStatus.NOT_CONFIGURED
            ),
            norm_min=row["norm_min"],
            norm_max=row["norm_max"],
            source_note=row["source_note"],
        )

    if month_start < ACCOUNTING_START:
        for line in lines.values():
            if line.status is not LineStatus.NOT_USED:
                line.status = LineStatus.BEFORE_ACCOUNTING_START
        report.lines = _ordered(lines, catalog)
        report.warnings.append(
            Warning(
                code="before_accounting_start",
                message=(
                    f"Управленческий учёт ведётся с {ACCOUNTING_START.strftime('%d.%m.%Y')}. "
                    "За более ранние месяцы данных в приложении нет."
                ),
            )
        )
        return report

    sign_roles = {row["code"]: row["sign_role"] for row in catalog}
    cash = await cash_source.build_cash_layer(
        session, month_start, month_end, sign_roles=sign_roles
    )
    recognition = await recognition_source.build_recognition_layer(session, month_start, month_end)
    manual = await manual_source.build_manual_layer(session, month_start)

    _apply_recognition(lines, recognition, session_article_lines=await _article_lines(session))
    _apply_cash(lines, cash)
    _apply_manual(lines, manual)
    await _apply_fixed_assets(session, lines, month_start)
    await _apply_taxes(session, lines, month_start)
    await _apply_payroll(session, lines, month_start, month_end)

    formulas.apply_formulas(lines, catalog)
    _apply_percentages(lines)

    report.lines = _ordered(lines, catalog)
    report.reconciliation = _reconciliation(cash)
    report.warnings.extend(_warnings(lines, cash, recognition))
    report.quality = {
        "unattributed": recognition.unattributed,
        "without_primary": recognition.without_primary,
        "without_location": recognition.without_location,
        "recognized_total": recognition.total,
        "cash_in_pnl": cash.by_verdict.get(Verdict.INCLUDED.value, Decimal("0.00")),
    }
    return report


def _add_component(
    lines: dict[str, LineValue],
    line_code: str,
    *,
    stream: str,
    amount: Decimal | None,
    status: LineStatus,
    note: str | None = None,
) -> None:
    line = lines.get(line_code)
    if line is None:
        return
    line.components.append(
        Component(stream=stream, component="main", amount=amount, status=status, note=note)
    )
    line.drill_available = amount is not None


async def _apply_fixed_assets(
    session: AsyncSession, lines: dict[str, LineValue], month_start: date
) -> None:
    """Амортизация и убыток от выбытия — обе величины неденежные, проводки в ДДС не имеют."""
    depreciation = await fixed_assets_source.depreciation_for_month(session, month_start)
    _add_component(
        lines,
        "depreciation",
        stream="fixed_assets",
        amount=depreciation,
        status=LineStatus.OK if depreciation is not None else LineStatus.NO_DATA,
    )
    disposal = await fixed_assets_source.disposal_loss_for_month(session, month_start)
    # Выбытий в месяце может не быть — это подтверждённый ноль, а не пробел: реестр отвечает,
    # что списаний не происходило.
    _add_component(
        lines,
        "asset_disposal_loss",
        stream="fixed_assets",
        amount=disposal if disposal is not None else Decimal("0.00"),
        status=LineStatus.OK if disposal is not None else LineStatus.ZERO_CONFIRMED,
    )


async def _apply_taxes(
    session: AsyncSession, lines: dict[str, LineValue], month_start: date
) -> None:
    """Налоги — из модуля по ПЕРИОДУ налога, а не по дате списания денег."""
    payroll_tax = await taxes_source.payroll_taxes_for_month(session, month_start)
    _add_component(
        lines,
        "payroll_taxes",
        stream="taxes",
        amount=payroll_tax,
        status=LineStatus.OK if payroll_tax is not None else LineStatus.NO_DATA,
    )
    income_tax, note = await taxes_source.income_tax_for_month(session, month_start)
    _add_component(
        lines,
        "taxes",
        stream="taxes",
        amount=income_tax,
        status=LineStatus.OK if income_tax is not None else LineStatus.NO_DATA,
        note=note,
    )


async def _apply_payroll(
    session: AsyncSession,
    lines: dict[str, LineValue],
    month_start: date,
    month_end: date,
) -> None:
    """Зарплаты, премии и накопительный фонд.

    Ревизионные штрафы сюда не вычитаются — они уходят в строку «Результаты ревизии»
    отдельным компонентом. Прочие штрафы уменьшают зарплату поваров: именно с их выплаты
    они и удерживаются.
    """
    month = await payroll_source.build_payroll_month(session, month_start, month_end)
    known = LineStatus.OK if month.has_data else LineStatus.NO_DATA
    values = {
        "cook_payroll": payroll_source.cook_payroll(month),
        "administrator_payroll": month.cashier_pay,
        "admin_payroll": month.admin_pay,
        "bonuses": month.bonuses,
        "accumulation_fund": month.fund_accrual,
    }
    for line_code, amount in values.items():
        _add_component(
            lines,
            line_code,
            stream="payroll",
            amount=amount if month.has_data else None,
            status=known,
        )


async def _article_lines(session: AsyncSession) -> dict[Any, str]:
    """Статья ДДС → строка ОПиУ. Признание пользуется той же разметкой, что и касса:
    признанный расход по статье — это та же строка отчёта, что и оплата по ней."""
    from app.models import PnlArticleRule

    rows = (
        await session.execute(
            select(PnlArticleRule.article_id, PnlArticleRule.line_code).where(
                PnlArticleRule.is_active.is_(True), PnlArticleRule.in_pnl.is_(True)
            )
        )
    ).all()
    return {article_id: line_code for article_id, line_code in rows}


def _apply_recognition(
    lines: dict[str, LineValue],
    layer: recognition_source.RecognitionLayer,
    session_article_lines: dict[Any, str],
) -> None:
    for article_id, bucket in layer.by_article.items():
        line_code = session_article_lines.get(article_id)
        if line_code is None or line_code not in lines:
            continue
        line = lines[line_code]
        line.components.append(
            Component(
                stream="recognition",
                component="main",
                amount=bucket.amount,
                status=LineStatus.OK,
            )
        )
        line.drill_available = True


def _apply_cash(lines: dict[str, LineValue], layer: cash_source.CashLayer) -> None:
    for line_code, bucket in layer.buckets.items():
        line = lines.get(line_code)
        if line is None:
            continue
        has_amount = bucket.count > 0
        component = Component(
            stream="cashflow",
            component="main",
            amount=bucket.amount if has_amount else Decimal("0.00"),
            status=LineStatus.OK if has_amount else LineStatus.ZERO_CONFIRMED,
            excluded_amount=bucket.excluded_amount,
            excluded_reason="accrual" if bucket.excluded_count else None,
            unrecognized_paid=bucket.unrecognized_paid,
            cash_proxy_amount=(
                bucket.amount if has_amount and line.month_basis == "document" else Decimal("0.00")
            ),
        )
        line.components.append(component)
        line.drill_available = True


def _apply_manual(lines: dict[str, LineValue], manual: dict[str, list[Any]]) -> None:
    for line_code, entries in manual.items():
        line = lines.get(line_code)
        if line is None:
            continue
        line.components.append(
            Component(
                stream="manual",
                component="main",
                amount=manual_source.manual_total(entries),
                status=LineStatus.MANUAL,
                note="; ".join(entry.reason for entry in entries),
            )
        )
        line.drill_available = True


def _collapse(line: LineValue) -> None:
    """Свести компоненты строки в итог и статус.

    Правило статуса: если есть хоть один известный компонент — строка известна. Ручной ввод
    красит строку в «ручной ввод», ожидание документа — в «ждём документ», но только когда
    другой суммы у строки нет: оплаченное и уже признанное не должно выглядеть как ожидание.
    """
    if not line.components:
        return
    total = Decimal("0.00")
    known = False
    manual_only = True
    waiting = False
    for component in line.components:
        if component.amount is not None:
            total += component.amount
            known = True
            if component.status is not LineStatus.MANUAL:
                manual_only = False
        if component.unrecognized_paid > 0:
            waiting = True
    if not known:
        line.status = LineStatus.NO_DATA
        return
    line.amount = total
    if manual_only:
        line.status = LineStatus.MANUAL
    elif waiting and total == 0:
        line.status = LineStatus.WAITING_DOCUMENT
    elif total == 0:
        line.status = LineStatus.ZERO_CONFIRMED
    else:
        line.status = LineStatus.OK


def _apply_percentages(lines: dict[str, LineValue]) -> None:
    """Процент от выручки для строк с известной суммой.

    База — итоговая строка «Выручка». Если она неизвестна или неполна, проценты не считаются
    вовсе: доля от неполной базы выглядит как факт и при этом врёт.
    """
    revenue = lines.get("revenue")
    if revenue is None or revenue.amount is None or revenue.amount == 0:
        return
    if revenue.status is LineStatus.INCOMPLETE:
        return
    for line in lines.values():
        if line.kind == "ratio" or line.amount is None:
            continue
        if line.status not in (LineStatus.OK, LineStatus.ZERO_CONFIRMED, LineStatus.MANUAL):
            continue
        line.pct_of_revenue = (line.amount / revenue.amount).quantize(Decimal("0.0001"))


def _ordered(lines: dict[str, LineValue], catalog: list[dict[str, Any]]) -> list[LineValue]:
    for row in catalog:
        if row["kind"] in {"source", "memo"}:
            _collapse(lines[row["code"]])
    return [lines[row["code"]] for row in sorted(catalog, key=lambda item: item["sort_order"])]


def _reconciliation(layer: cash_source.CashLayer) -> Reconciliation:
    """Уравнение замкнутости: отток обязан без остатка разложиться по вердиктам."""
    by_verdict = {key: value for key, value in layer.by_verdict.items()}
    total_out = layer.out_total
    total_in = layer.in_total
    covered = sum(by_verdict.values(), Decimal("0.00"))
    drift = (total_out + total_in) - covered
    return Reconciliation(
        cash_out_total=total_out,
        cash_in_total=total_in,
        by_verdict=by_verdict,
        unmapped=layer.unmapped,
        unmapped_count=layer.unmapped_count,
        balanced=layer.unmapped == 0 and drift == 0,
        drift=drift,
    )


def _warnings(
    lines: dict[str, LineValue],
    cash: cash_source.CashLayer,
    recognition: recognition_source.RecognitionLayer,
) -> list[Warning]:
    result: list[Warning] = []
    if cash.unmapped_count:
        result.append(
            Warning(
                code="unmapped_cash",
                message=(
                    f"{cash.unmapped_count} проводок на {cash.unmapped:,.2f} ₽ не разнесены "
                    "по статьям — отчёт неполон ровно на эту сумму"
                ).replace(",", " "),
                amount=cash.unmapped,
            )
        )
    if recognition.unattributed:
        result.append(
            Warning(
                code="recognition_unattributed",
                message=(
                    f"Признанный расход на {recognition.unattributed:,.2f} ₽ без статьи ДДС: "
                    "в отчёт он не попал"
                ).replace(",", " "),
                amount=recognition.unattributed,
            )
        )
    for line in lines.values():
        paid = sum((component.unrecognized_paid for component in line.components), Decimal("0.00"))
        if paid > 0:
            result.append(
                Warning(
                    code="waiting_document",
                    line_code=line.code,
                    message=(
                        f"«{line.title}»: оплачено {paid:,.2f} ₽, документа за период ещё нет"
                    ).replace(",", " "),
                    amount=paid,
                )
            )
    return result

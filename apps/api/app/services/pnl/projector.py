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
import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PnlLine
from app.services.pnl import formulas
from app.services.pnl.sources import acquiring as acquiring_source
from app.services.pnl.sources import cashflow as cash_source
from app.services.pnl.sources import deposits as deposits_source
from app.services.pnl.sources import fixed_assets as fixed_assets_source
from app.services.pnl.sources import iiko as iiko_source
from app.services.pnl.sources import inventory as inventory_source
from app.services.pnl.sources import manual as manual_source
from app.services.pnl.sources import payroll as payroll_source
from app.services.pnl.sources import recognition as recognition_source
from app.services.pnl.sources import taxes as taxes_source
from app.services.pnl.sources import waiting as waiting_source
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

#: Строка ОПиУ ← метрика зеркала iiko. Живёт на уровне модуля, потому что ту же карту читает
#: расшифровка строки: собери она свою копию — разошлись бы молча.
IIKO_LINE_METRIC = {
    "revenue_gross_chernikova": "revenue_gross",
    "revenue_net_chernikova": "revenue_net",
    "partner_commission": "partner_commission",
    "food_cost_chernikova": "food_cost",
    "courier_service": "courier_salary",
    "writeoffs": "writeoff_cost",
    "packaging_inventory": "packaging_result",
    "pizza_box_inventory": "pizza_box_result",
    "beverage_inventory": "beverage_result",
    "aux_goods": "aux_goods_invoices",
}

#: Метрики инвентаризации, знак которых переворачивается на границе «зеркало → строка».
#:
#: В iiko отрицательная сумма инвентаризации — НЕДОСТАЧА (товар ушёл со склада), положительная
#: — излишек. В отчёте соглашение обратное и общее для всех блоков: величина расходной строки
#: положительна, отрицательной приходит только компенсирующая. Без этого шага июльская
#: недостача упаковки стояла в блоке общепроизводственных со знаком минус и работала как
#: доход, завышая EBITDA; излишек коробок зеркально занижал прибыль.
#:
#: Инверсия сделана ЗДЕСЬ, а не в синке: зеркало обязано хранить то, что ответил iiko, иначе
#: сверка с источником перестанет сходиться.
#:
#: Складских метрик (``stock_consumption``, ``stock_closing_balance``) здесь нет и быть не
#: должно: они не строки ОПиУ вовсе. Roll-forward «начало + приход − конец» — это расход
#: периода, то есть фудкост; подключать его к отчёту значит задваивать себестоимость.
INVERTED_IIKO_METRICS = frozenset({"packaging_result", "pizza_box_result", "beverage_result"})


def rubles(amount: Decimal) -> str:
    """Сумма по-русски: неразрывный пробел в разрядах, запятая в копейках.

    Раньше каждый текст предупреждения делал ``f"{amount:,.2f}".replace(",", " ")`` — и
    вместе с разделителем разрядов вычищал ЗАПЯТЫЕ САМОГО ПРЕДЛОЖЕНИЯ. Получалось «оплачено
    10 995.59 ₽  документа за период ещё нет» и «за 2024  2025 год(ы)». Форматирование числа
    не должно уметь трогать текст вокруг, поэтому оно живёт отдельной функцией.
    """
    whole, _, fraction = f"{amount:.2f}".partition(".")
    sign = "−" if whole.startswith("-") else ""
    digits = whole.lstrip("-")
    groups = []
    while len(digits) > 3:
        groups.insert(0, digits[-3:])
        digits = digits[:-3]
    groups.insert(0, digits)
    return f"{sign}{' '.join(groups)},{fraction}"


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

    waiting = await waiting_source.build_waiting_layer(
        session,
        month_start,
        month_end,
        recognized_counterparties={
            detail.counterparty_id
            for detail in recognition.details
            if detail.counterparty_id is not None
        },
    )
    article_lines = await _article_lines(session)
    _apply_recognition(lines, recognition, session_article_lines=article_lines)
    _apply_cash(lines, cash)
    _apply_waiting(lines, waiting, article_lines)
    unperiodled = await waiting_source.build_unperiodled_layer(session, month_start, month_end)
    _apply_silent_articles(lines, cash, article_lines)
    _apply_manual(lines, manual)
    await _apply_fixed_assets(session, lines, month_start)
    await _apply_taxes(session, lines, month_start)
    await _apply_payroll(session, lines, month_start, month_end)
    await _apply_releases(session, lines, month_start, month_end, report)
    await _apply_inventory(session, lines, month_start, month_end, report)
    await _apply_acquiring(session, lines, month_start, month_end, report)
    await _apply_iiko(session, lines, month_start, report)

    # Порядок значим: сначала свести компоненты каждой строки в итог, и только потом считать
    # каскад. Иначе формулы читают строки-источники ещё пустыми и любой подытог выходит
    # «неполным» — при полностью известных слагаемых.
    for row in catalog:
        if row["kind"] in {"source", "memo"}:
            _collapse(lines[row["code"]])

    formulas.apply_formulas(lines, catalog)
    _apply_percentages(lines)

    report.lines = _ordered(lines, catalog)
    report.reconciliation = _reconciliation(cash)
    report.warnings.extend(_warnings(lines, cash, recognition))
    report.warnings.extend(_unperiodled_warnings(unperiodled, article_lines, lines))
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


async def _apply_releases(
    session: AsyncSession,
    lines: dict[str, LineValue],
    month_start: date,
    month_end: date,
    report: PnlReport,
) -> None:
    """Невостребованные обязательства перед сотрудниками: депозиты и накопительный фонд.

    Ледджеры отвечают всегда, поэтому месяц без списаний — ПОДТВЕРЖДЁННЫЙ НОЛЬ, а не пробел.
    За 26 месяцев истории списаний было одиннадцать: если бы пустой месяц читался как «нет
    данных», строка «Списание невостребованных депозитов» делала бы чистую прибыль неполной
    почти всегда — и предупреждение об этом перестали бы замечать.
    """
    releases = await deposits_source.build_release_month(
        session, month_start, month_end, horizon_start=ACCOUNTING_START
    )
    _add_component(
        lines,
        "unclaimed_deposits_writeoff",
        stream="payroll",
        amount=releases.deposits_written_off,
        status=(LineStatus.OK if releases.deposits_written_off else LineStatus.ZERO_CONFIRMED),
        note="Депозиты уволенных, оставшиеся у бизнеса",
    )
    if releases.fund_forfeited:
        # Отмена признанного расхода идёт в ТУ ЖЕ строку отрицательным компонентом: фонд
        # начислялся расходом здесь, здесь же он и отменяется.
        _add_component(
            lines,
            "accumulation_fund",
            stream="payroll",
            amount=-releases.fund_forfeited,
            status=LineStatus.OK,
            note="Списание фонда уволенных — отмена ранее начисленного расхода",
        )
    # Списания фондов, накопленных до начала учёта, в строку не идут вовсе и предупреждением
    # не становятся: предупреждение нужно там, где владельцу есть что сделать, а здесь делать
    # нечего — это разбор исторических хвостов. Сумма видна в расшифровке строки отдельной
    # пометкой, чтобы «пропажа» 93 762 ₽ не выглядела потерей.


async def _apply_inventory(
    session: AsyncSession,
    lines: dict[str, LineValue],
    month_start: date,
    month_end: date,
    report: PnlReport,
) -> None:
    """«Результаты ревизии» = недостачи проведённых ревизий минус удержанные штрафы.

    Штрафы приходят из зарплатного контура и вычитаются здесь, а не там: они компенсируют
    ревизионные потери, а не уменьшают фонд оплаты труда. Складской roll-forward
    «начало + приход − конец» здесь намеренно не используется: это фактическое потребление
    сырья и источник будущего баланса, а не недостача, выявленная ревизией.
    """
    # Фильтр упаковки ПОДКЛЮЧЁН, а не просто написан: продуктовая ревизия и инвентаризация
    # упаковки приходят из одного эндпоинта iiko, и загрузчик документов их не разделяет.
    # На июле 2026 пересечения нет ни одной позиции, но как только упаковку проведут обычной
    # ревизией, её расхождение встанет и сюда, и в свою строку — в двух разных блоках отчёта.
    packaging_guids = await inventory_source.load_packaging_guids(session)
    month = await inventory_source.build_inventory_month(
        session, month_start, month_end, packaging_guids=packaging_guids
    )
    payroll_month = await payroll_source.build_payroll_month(session, month_start, month_end)

    if month.product_result is None:
        _add_component(
            lines, "audit_results", stream="inventory", amount=None, status=LineStatus.NO_DATA
        )
        return

    _add_component(
        lines,
        "audit_results",
        stream="inventory",
        amount=month.product_result,
        status=LineStatus.OK if month.product_result else LineStatus.ZERO_CONFIRMED,
        note=(
            f"Недостача {rubles(month.shortage_amount)} ₽ минус излишки "
            f"{rubles(month.surplus_amount)} ₽ по проведённым ревизиям; "
            f"ревизий за месяц: {month.audits_count}"
        ),
    )
    if payroll_month.audit_penalties:
        _add_component(
            lines,
            "audit_results",
            stream="payroll",
            amount=-payroll_month.audit_penalties,
            status=LineStatus.OK,
            note="Штрафы по ревизиям — компенсируют потери",
        )
    if month.audits_count < 3:
        report.warnings.append(
            Warning(
                code="inventory_sparse",
                line_code="audit_results",
                message=(
                    f"За месяц найдено ревизий: {month.audits_count} "
                    f"({', '.join(d.strftime('%d.%m') for d in month.audit_dates)}). "
                    "Ревизии еженедельные — результат покрывает не весь месяц"
                ),
            )
        )


async def _apply_acquiring(
    session: AsyncSession,
    lines: dict[str, LineValue],
    month_start: date,
    month_end: date,
    report: PnlReport,
) -> None:
    """Комиссия, удержанная банком до зачисления, — вторым компонентом к кассовому.

    Кассовый компонент уже стоит и несёт терминальный эквайринг: там движение денег есть.
    Здесь добавляется то, чего в ДДС нет физически, — удержанное из выручки. За июль 2026
    это 42 238,45 ₽ сверх 6 668,93 ₽ по статье.
    """
    month = await acquiring_source.build_acquiring_month(session, month_start, month_end)
    if month.total:
        _add_component(
            lines,
            "acquiring",
            stream="acquiring",
            amount=month.total,
            status=LineStatus.OK,
            note="Удержано банком из зачислений — движения денег по этой сумме нет",
        )
    if month.unparsed:
        report.warnings.append(
            Warning(
                code="acquiring_unparsed",
                line_code="acquiring",
                message=(
                    f"{month.unparsed} зачислений эквайринга без распознанной комиссии — "
                    "банк изменил формулировку назначения, строка занижена"
                ),
            )
        )


async def _apply_iiko(
    session: AsyncSession,
    lines: dict[str, LineValue],
    month_start: date,
    report: PnlReport,
) -> None:
    """Выручка, партнёры, фудкост и курьеры — из зеркала ночной джобы."""
    facts = await iiko_source.month_facts(session, month_start)
    for line_code, metric in IIKO_LINE_METRIC.items():
        amount = facts.get(metric)
        if amount is not None and metric in INVERTED_IIKO_METRICS:
            amount = -amount
        _add_component(
            lines,
            line_code,
            stream="iiko",
            amount=amount,
            status=LineStatus.OK if amount is not None else LineStatus.NO_DATA,
        )

    workup_items = await iiko_source.month_workup_items(session, month_start)
    _add_component(
        lines,
        "goods_workup",
        stream="iiko_workup",
        amount=sum((item.amount for item in workup_items), Decimal("0.00")),
        status=LineStatus.OK,
        note="Справочно: закуплено на проработку. Расход признаётся актом списания",
    )

    # Расход товара проработки признаётся АКТОМ СПИСАНИЯ, а закупка справочная. Значит товар,
    # который купили на пробу и не списали, не посчитан расходом НИГДЕ — прибыль завышена
    # ровно на его стоимость. Пробел молчит: строка «Проработка» показывает закупку как ни в
    # чём не бывало, просто теперь она в прибыль не идёт.
    workup_state = await iiko_source.month_workup_writeoff_state(session, month_start)
    missing = [item for item in workup_state if not item.written_off]
    if missing:
        listed = ", ".join(
            f"{item.product_name} ({rubles(item.purchase_amount)} ₽)" for item in missing[:5]
        )
        total = sum((item.purchase_amount for item in missing), Decimal("0.00"))
        report.warnings.append(
            Warning(
                code="workup_not_written_off",
                line_code="goods_workup",
                message=(
                    f"Проработка закуплена, но не списана актом: {listed}"
                    + (f" и ещё {len(missing) - 5}" if len(missing) > 5 else "")
                    + f". Расход {rubles(total)} ₽ не попал в прибыль — он признаётся "
                    "актом списания, а акта по этим товарам нет"
                ),
            )
        )

    # «Содержание торговых точек» — составная строка: касса по статье ПЛЮС закупка
    # расходников из приходных накладных. Так её описывает методология, и оба компонента
    # реальны: часть расходников покупают наличными администраторы, часть приходит
    # накладной. Компонент добавляется ВТОРЫМ, кассовый уже стоит.
    shop_goods = facts.get("shop_maintenance_invoices")
    if shop_goods is not None:
        _add_component(
            lines,
            "shop_maintenance",
            stream="iiko",
            amount=shop_goods,
            status=LineStatus.OK,
            note="Закупка расходников из приходных накладных",
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
            cash_alongside_accrual=bucket.cash_alongside_accrual,
            cash_proxy_amount=(
                bucket.amount if has_amount and line.month_basis == "document" else Decimal("0.00")
            ),
        )
        line.components.append(component)
        line.drill_available = True


def _apply_waiting(
    lines: dict[str, LineValue],
    layer: waiting_source.WaitingLayer,
    article_lines: dict[Any, str],
) -> None:
    """«Оплачено, документа за период нет» — из ДЗ/КЗ, а не из кассы месяца.

    Компонент несёт только ожидание: суммы у него нет и быть не должно, иначе расход
    посчитается дважды — сейчас деньгами и потом документом. Он нужен, чтобы строка получила
    статус «ждём документ» и предупреждение с настоящей цифрой.
    """
    for article_id, amount in layer.by_article.items():
        line_code = article_lines.get(article_id)
        if line_code is None or line_code not in lines or amount <= 0:
            continue
        line = lines[line_code]
        line.components.append(
            Component(
                stream="recognition",
                component="waiting",
                amount=None,
                status=LineStatus.WAITING_DOCUMENT,
                unrecognized_paid=amount,
            )
        )
        line.drill_available = True


def _apply_silent_articles(
    lines: dict[str, LineValue],
    layer: cash_source.CashLayer,
    article_lines: dict[Any, str],
) -> None:
    """Статья размечена, а движения за месяц не было — это ПОДТВЕРЖДЁННЫЙ НОЛЬ.

    Без этого правила каскад не сходится никогда: «Возвраты клиентам» и «Комиссия партнёрам»
    в спокойный месяц пусты по-честному, но строка со статусом «нет данных» делает неполной
    выручку, а за ней — маржинальный доход, валовую прибыль и всё остальное. Разница между
    «источник молчит» и «источника нет» здесь и проходит: статья ДДС существует и размечена,
    значит ответ получен, и ответ этот — ноль.
    """
    for line_code in set(article_lines.values()):
        line = lines.get(line_code)
        if line is None or line.components or line.status is LineStatus.NOT_USED:
            continue
        if line_code in layer.buckets:
            continue
        _add_component(
            lines,
            line_code,
            stream="cashflow",
            amount=Decimal("0.00"),
            status=LineStatus.ZERO_CONFIRMED,
            note="Движения по статье за месяц не было",
        )


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

    База — итоговая строка «Выручка». Неполная БАЗА по-прежнему отменяет все проценты: доля
    от неизвестной выручки не значит ничего. А вот неполный ЧИСЛИТЕЛЬ процент не отменяет —
    он показывается рядом с «≈» на самой сумме, и этот знак читается как оговорка. Пустая
    колонка на месте доли расходов сообщала бы меньше, чем приблизительная цифра.
    """
    revenue = lines.get("revenue")
    if revenue is None or revenue.amount is None or revenue.amount == 0:
        return
    if revenue.status is LineStatus.INCOMPLETE:
        return
    for line in lines.values():
        if line.kind == "ratio" or line.amount is None:
            continue
        if line.status not in (
            LineStatus.OK,
            LineStatus.ZERO_CONFIRMED,
            LineStatus.MANUAL,
            LineStatus.INCOMPLETE,
        ):
            continue
        line.pct_of_revenue = (line.amount / revenue.amount).quantize(Decimal("0.0001"))


def _ordered(lines: dict[str, LineValue], catalog: list[dict[str, Any]]) -> list[LineValue]:
    """Строки отчёта в порядке справочника, БЕЗ неработающих.

    Закрытая точка (Гагарина) и снятая с учёта строка остаются в справочнике — они нужны
    формулам и истории, — но в отчёт не выводятся вовсе: владелец 03.08.2026 попросил их
    просто убрать. Бледная строка «не используется» ничего не сообщает и занимает место в
    каскаде, который читают сверху вниз. Из СЧЁТА они и так исключены: ``_operand``
    возвращает по ним ноль, а не пробел.
    """
    ordered = sorted(catalog, key=lambda item: item["sort_order"])
    return [
        lines[row["code"]]
        for row in ordered
        if lines[row["code"]].status is not LineStatus.NOT_USED
    ]


def _reconciliation(layer: cash_source.CashLayer) -> Reconciliation:
    """Уравнение замкнутости: движение денег месяца обязано без остатка разложиться по вердиктам.

    СВЕРКА ДОЛЖНА ИМЕТЬ НЕЗАВИСИМУЮ СТОРОНУ, ИНАЧЕ ОНА НИЧЕГО НЕ ЗНАЧИТ. Раньше дрейф считался
    как разность двух сумм, набранных в ОДНОМ цикле по ОДНОЙ выборке: и «сколько денег»,
    и «сколько разложено». Такая разность равна нулю алгебраически — при любой ошибке, включая
    потерянную проводку. Владельцу при этом показывалась зелёная карточка «каждый рубль
    разложен», и аудит 05.08.2026 справедливо назвал её галочкой, которая ничего не проверяет.

    Теперь вторая сторона — агрегат, посчитанный БАЗОЙ до разбора (``source_total``,
    ``source_count``). Сходятся суммы и число обработанных проводок — значит цикл действительно
    прошёл по всему, что есть в месяце. Разошлись — видно и на сколько рублей, и на сколько
    документов.
    """
    by_verdict = {key: value for key, value in layer.by_verdict.items()}
    covered = sum(by_verdict.values(), Decimal("0.00"))
    drift = layer.source_total - covered
    missed = layer.source_count - layer.counted
    return Reconciliation(
        cash_out_total=layer.out_total,
        cash_in_total=layer.in_total,
        by_verdict=by_verdict,
        unmapped=layer.unmapped,
        unmapped_count=layer.unmapped_count,
        balanced=layer.unmapped == 0 and drift == 0 and missed == 0,
        drift=drift,
        missed_count=missed,
    )


def _unperiodled_warnings(
    layer: waiting_source.UnperiodedLayer,
    article_lines: dict[Any, str],
    lines: dict[str, LineValue],
) -> list[Warning]:
    """Документ лежит оплаченным, но без периода услуги — расход по нему не признан.

    Самая тихая из всех потерь: «ждём документ» чинится временем, а это — никогда, пока
    человек не откроет карточку и не поставит период. Строка при этом выглядит законченной.
    Сумма в расход НЕ добавляется: месяц документа здесь догадка по его дате, а признание —
    работа ДЗ/КЗ, и подменять её отчётом значило бы завести второй источник истины и получить
    задвоение в тот день, когда период всё-таки заполнят.
    """
    result: list[Warning] = []
    by_line: dict[str, Decimal] = {}
    for item in layer.items:
        line_code = article_lines.get(item.article_id)
        if line_code is None or line_code not in lines:
            continue
        by_line[line_code] = by_line.get(line_code, Decimal("0.00")) + item.amount
    for line_code, amount in by_line.items():
        result.append(
            Warning(
                code="document_without_period",
                line_code=line_code,
                message=(
                    f"«{lines[line_code].title}»: закрывающие документы на {rubles(amount)} ₽ "
                    "лежат оплаченными без периода услуги — расход по ним не признан. "
                    "Заполните период в ДЗ/КЗ, и сумма встанет в свой месяц"
                ),
                amount=amount,
            )
        )
    return result


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
                    f"{cash.unmapped_count} проводок на {rubles(cash.unmapped)} ₽ не разнесены "
                    "по статьям — отчёт неполон ровно на эту сумму"
                ),
                amount=cash.unmapped,
            )
        )
    if recognition.unattributed:
        result.append(
            Warning(
                code="recognition_unattributed",
                message=(
                    f"Признанный расход на {rubles(recognition.unattributed)} ₽ без статьи "
                    "ДДС: в отчёт он не попал"
                ),
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
                        f"«{line.title}»: за период оплачено {rubles(paid)} ₽, "
                        "закрывающего документа ещё нет"
                    ),
                    amount=paid,
                )
            )
        # Пересечение двух источников в одной строке — пометка, не тревога. Наличная касса
        # без контрагента признана расходом, и по той же статье есть признание документом.
        # Обычно это разные расходы (наличное электричество и перевыставленная доля
        # коммуналки), но проверить может только человек — отчёт называет обе суммы и не
        # решает за него.
        alongside = sum(
            (component.cash_alongside_accrual for component in line.components), Decimal("0.00")
        )
        if alongside > 0:
            result.append(
                Warning(
                    code="cash_alongside_accrual",
                    line_code=line.code,
                    message=(
                        f"«{line.title}»: {rubles(alongside)} ₽ наличных без контрагента "
                        "посчитаны расходом рядом с признанием по документам. Если это один "
                        "и тот же расход — привяжите платежи к контрагенту, и кассу заменит "
                        "документ"
                    ),
                    amount=alongside,
                )
            )
    result.extend(_unfulfilled_accrual_warnings(cash, recognition))
    return result


def _unclassified_goods_warning(goods: iiko_source.UnclassifiedGoods) -> Warning | None:
    """Закупка товара, который ещё не размечен, не попала ни в одну строку.

    Счётчик «требует внимания» жил на вкладке разметки — надо было догадаться туда зайти.
    В самом отчёте сигнала не было: строки считались без этих сумм, прибыль выходила
    завышенной, и понять это по экрану было нельзя. Аудит 05.08.2026 намерил 6 072,35 ₽ за
    июль по восьми позициям.
    """
    if goods.count == 0:
        return None
    listed = ", ".join(goods.names[:5])
    if goods.count > 5:
        listed = f"{listed} и ещё {goods.count - 5}"
    return Warning(
        code="unclassified_goods",
        message=(
            f"{goods.count} товаров закуплено на {rubles(goods.amount)} ₽ и не размечено: "
            f"{listed}. Пока статья не выбрана, эта закупка не входит ни в одну строку — "
            "прибыль завышена на эту сумму. Разметьте их во вкладке «Товары iiko»"
        ),
        amount=goods.amount,
    )


def _unfulfilled_accrual_warnings(
    cash: cash_source.CashLayer,
    recognition: recognition_source.RecognitionLayer,
) -> list[Warning]:
    """Обещание против факта: касса исключена «под начисление», а начисления столько нет.

    ЧТО ЗДЕСЬ ЛОВИТСЯ. Как только контрагент попал в контур признания, ВСЯ его касса месяца
    выбрасывается из строк: отчёт утверждает, что расход придёт документом. Утверждение это
    ничем не проверялось — суммы признания считались (``CashContext.recognized``,
    ``RecognitionLayer.by_pair``) и не читались ни разу. Если документ не приехал, деньги
    ушли, расхода нет нигде, а строка показывает уверенное число. Аудит 05.08.2026 намерил
    этим путём 82 995,59 ₽ за июль — молча.

    ПОЧЕМУ СРАВНИВАЕМ ПО КОНТРАГЕНТУ, А НЕ ПО ПАРЕ «КОНТРАГЕНТ × СТАТЬЯ». У начисления статья
    часто пуста, и признание уезжает на строку из карточки контрагента
    (``default_dds_article_id``). Сверка по паре считала бы это расхождением и кричала бы на
    здоровых данных — три контрагента июля (ЧОО, СПЕЦАВТО, ЭкоЦентр) дают ровно такой случай.

    ПОЧЕМУ ЭТО ПРЕДУПРЕЖДЕНИЕ, А НЕ ПРАВКА СУММЫ. Разница законна ровно так же часто, как и
    нет: заплатили в июле за август — предоплата, документ приедет в свой месяц. Отличить
    предоплату от потерянного документа по цифрам нельзя, поэтому отчёт обязан не выбирать
    молча, а показать разницу и назвать обе возможности.
    """
    if not cash.excluded_for_accrual:
        return []
    recognized_by_counterparty: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    for (counterparty_id, _article_id), amount in recognition.by_pair.items():
        recognized_by_counterparty[counterparty_id] += amount

    gaps: list[tuple[Decimal, str]] = []
    for counterparty_id, excluded in cash.excluded_for_accrual.items():
        gap = excluded - recognized_by_counterparty.get(counterparty_id, Decimal("0.00"))
        if gap <= 0:
            continue
        name = cash.excluded_counterparty_names.get(counterparty_id) or "контрагент без названия"
        gaps.append((gap, name))
    if not gaps:
        return []

    gaps.sort(reverse=True)
    total = sum((gap for gap, _name in gaps), Decimal("0.00"))
    listed = ", ".join(f"{name} — {rubles(gap)} ₽" for gap, name in gaps[:5])
    if len(gaps) > 5:
        listed = f"{listed} и ещё {len(gaps) - 5}"
    return [
        Warning(
            code="excluded_without_accrual",
            message=(
                f"Оплачено {rubles(total)} ₽, но расход по этим деньгам не признан: {listed}. "
                "Платёж исключён из строки в пользу закрывающего документа, а документа на "
                "эту сумму нет. Либо это предоплата будущего периода, либо документ не "
                "приехал — во втором случае расход не попал в прибыль"
            ),
            amount=total,
        )
    ]

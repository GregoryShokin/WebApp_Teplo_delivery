"""Леджеры-источники ОПиУ: зарплата, партнёры, товары iiko и признание ДЗ/КЗ.

Свод отвечает на вопрос «сколько», а эти представления — «из чего именно». Они не имеют
собственной бухгалтерской логики: зарплата поднимается из ``build_payroll_month``, признание
и ожидания — из тех же слоёв, что проектор, товарные строки — из сохранённого зеркала iiko.
Поэтому вкладки не могут молча разойтись с цифрами верхнего отчёта.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Counterparty, DdsArticle, Employee, PnlArticleRule, PnlLine
from app.models.inventory import InventoryAudit, InventoryAuditItem
from app.models.pnl import (
    PnlIikoFact,
    PnlIikoGoodsFact,
    PnlIikoProductObservation,
    PnlIikoStockFact,
    PnlProductMonthlyDecision,
    PnlProductWhitelist,
)
from app.services import accounting_periods
from app.services.pnl import iiko_sync, projector
from app.services.pnl.revision_products import (
    load_revision_product_guids,
    normalize_iiko_product_guid,
)
from app.services.pnl.sources import deposits as deposits_source
from app.services.pnl.sources import iiko as iiko_source
from app.services.pnl.sources import inventory as inventory_source
from app.services.pnl.sources import payroll as payroll_source
from app.services.pnl.sources import recognition as recognition_source
from app.services.pnl.sources import waiting as waiting_source


@dataclass(slots=True)
class PayrollLedgerEmployee:
    employee_id: uuid.UUID | None
    employee_name: str
    roles: list[str] = field(default_factory=list)
    base_pay: Decimal = Decimal("0.00")
    percent_pay: Decimal = Decimal("0.00")
    bonuses: Decimal = Decimal("0.00")
    vacation_pay: Decimal = Decimal("0.00")
    fund_accrual: Decimal = Decimal("0.00")
    other_penalties: Decimal = Decimal("0.00")
    audit_penalties: Decimal = Decimal("0.00")
    fund_forfeited: Decimal = Decimal("0.00")
    deposit_written_off: Decimal = Decimal("0.00")

    @property
    def salary_expense(self) -> Decimal:
        return (
            self.base_pay
            + self.percent_pay
            + self.bonuses
            + self.vacation_pay
            - self.other_penalties
        )

    @property
    def fund_expense(self) -> Decimal:
        return self.fund_accrual - self.fund_forfeited


@dataclass(slots=True)
class PayrollLedgerTotals:
    base_pay: Decimal = Decimal("0.00")
    percent_pay: Decimal = Decimal("0.00")
    bonuses: Decimal = Decimal("0.00")
    vacation_pay: Decimal = Decimal("0.00")
    fund_accrual: Decimal = Decimal("0.00")
    other_penalties: Decimal = Decimal("0.00")
    audit_penalties: Decimal = Decimal("0.00")
    fund_forfeited: Decimal = Decimal("0.00")
    deposit_written_off: Decimal = Decimal("0.00")

    @property
    def salary_expense(self) -> Decimal:
        return (
            self.base_pay
            + self.percent_pay
            + self.bonuses
            + self.vacation_pay
            - self.other_penalties
        )

    @property
    def fund_expense(self) -> Decimal:
        return self.fund_accrual - self.fund_forfeited


@dataclass(slots=True)
class PayrollLedger:
    month: date
    totals: PayrollLedgerTotals
    employees: list[PayrollLedgerEmployee]


@dataclass(slots=True)
class PartnerCommissionLedgerRow:
    partner_name: str
    revenue_amount: Decimal
    commission_rate: Decimal
    commission_amount: Decimal
    rows_count: int
    source_ref: str
    synced_at: datetime


@dataclass(slots=True)
class PartnerCommissionLedger:
    month: date
    revenue_amount: Decimal | None
    commission_amount: Decimal | None
    synced_at: datetime | None
    rows: list[PartnerCommissionLedgerRow]


async def build_partner_commission_ledger(
    session: AsyncSession, month: date
) -> PartnerCommissionLedger:
    month_start, _month_end = projector.month_bounds(month)
    items = await iiko_source.month_partner_commissions(session, month_start)
    rows = [
        PartnerCommissionLedgerRow(
            partner_name=item.partner_name,
            revenue_amount=item.revenue_amount,
            commission_rate=item.commission_rate,
            commission_amount=item.commission_amount,
            rows_count=item.rows_count,
            source_ref=item.source_ref,
            synced_at=item.synced_at,
        )
        for item in items
    ]
    return PartnerCommissionLedger(
        month=month_start,
        revenue_amount=(
            sum((row.revenue_amount for row in rows), Decimal("0.00")) if rows else None
        ),
        commission_amount=(
            sum((row.commission_amount for row in rows), Decimal("0.00")) if rows else None
        ),
        synced_at=max((row.synced_at for row in rows), default=None),
        rows=rows,
    )


def _add_payroll_amounts(target: PayrollLedgerEmployee | PayrollLedgerTotals, source) -> None:
    for name in (
        "base_pay",
        "percent_pay",
        "bonuses",
        "vacation_pay",
        "fund_accrual",
        "other_penalties",
        "audit_penalties",
        "fund_forfeited",
        "deposit_written_off",
    ):
        setattr(target, name, getattr(target, name) + getattr(source, name, Decimal("0.00")))


async def build_payroll_ledger(session: AsyncSession, month: date) -> PayrollLedger:
    month_start, month_end = projector.month_bounds(month)
    payroll = await payroll_source.build_payroll_month(session, month_start, month_end)
    releases = await deposits_source.build_release_month(
        session,
        month_start,
        month_end,
        horizon_start=projector.ACCOUNTING_START,
    )

    employee_ids = set(payroll.breakdown_by_employee)
    employee_ids.update(
        entry.employee_id
        for entry in (*releases.fund_entries, *releases.deposit_entries)
        if entry.employee_id is not None
    )
    names = {
        employee_id: full_name
        for employee_id, full_name in (
            await session.execute(
                select(Employee.id, Employee.full_name).where(Employee.id.in_(employee_ids))
            )
        ).all()
    }

    rows: dict[uuid.UUID | None, PayrollLedgerEmployee] = {}

    def row_for(employee_id: uuid.UUID | None) -> PayrollLedgerEmployee:
        if employee_id not in rows:
            rows[employee_id] = PayrollLedgerEmployee(
                employee_id=employee_id,
                employee_name=(
                    names.get(employee_id, "Сотрудник удалён")
                    if employee_id is not None
                    else "Сотрудник не указан"
                ),
            )
        return rows[employee_id]

    for employee_id, breakdown in payroll.breakdown_by_employee.items():
        row = row_for(employee_id)
        row.roles = sorted(breakdown.roles)
        _add_payroll_amounts(row, breakdown)

    for entry in releases.fund_entries:
        row_for(entry.employee_id).fund_forfeited += entry.amount
    for entry in releases.deposit_entries:
        row_for(entry.employee_id).deposit_written_off += entry.amount

    totals = PayrollLedgerTotals()
    employees = sorted(rows.values(), key=lambda item: (-item.salary_expense, item.employee_name))
    for employee in employees:
        _add_payroll_amounts(totals, employee)
    return PayrollLedger(month=month_start, totals=totals, employees=employees)


@dataclass(slots=True)
class GoodsLedgerSummary:
    line_code: str
    line_title: str
    source_kind: str
    amount: Decimal | None
    details_amount: Decimal
    details_complete: bool
    #: Бывает ли у этой строки поимённая номенклатура вообще. У результата инвентаризации её
    #: нет по построению: детализация ``PnlIikoGoodsFact`` пересобирается из наблюдений, а
    #: наблюдения складского источника несут ПОТРЕБЛЕНИЕ, а не расхождение. Без этого флага
    #: экран обещал «номенклатура появится после следующей синхронизации» — обещание, которое
    #: не может быть выполнено никогда.
    details_expected: bool = True
    synced_at: datetime | None = None
    opening_amount: Decimal | None = None
    receipts_amount: Decimal | None = None
    closing_amount: Decimal | None = None
    #: Составляющие результата ревизий. С 05.08.2026 излишки не справочные: строка ОПиУ равна
    #: ``shortage_amount - surplus_amount``, и обе величины показываются, чтобы разность
    #: читалась глазами.
    surplus_amount: Decimal | None = None
    shortage_amount: Decimal | None = None


@dataclass(slots=True)
class GoodsLedgerRow:
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


@dataclass(slots=True)
class GoodsLedger:
    month: date
    summaries: list[GoodsLedgerSummary]
    rows: list[GoodsLedgerRow]


LINE_BY_GOODS_METRIC = {
    metric: line_code for line_code, metric in iiko_sync.WHITELIST_METRIC.items()
}
WORKUP_LINE_CODE = "goods_workup"


def pnl_goods_amount(metric_code: str, amount: Decimal) -> Decimal:
    """Перевести знак зеркала iiko в соглашение расходной строки ОПиУ."""
    return -amount if metric_code in projector.INVERTED_IIKO_METRICS else amount


async def _stocked_product_guids(session: AsyncSession, month: date) -> set[str]:
    """Нормализованные GUID товаров, стоимость которых идёт через складской roll-forward."""
    explicit_rules = (
        await session.execute(
            select(
                PnlProductWhitelist.iiko_product_guid,
                PnlProductWhitelist.source_kind,
                PnlProductWhitelist.include_status,
            )
        )
    ).all()
    explicit_guids = {
        normalize_iiko_product_guid(guid) for guid, _source_kind, _status in explicit_rules
    }
    rule_guids = {
        normalize_iiko_product_guid(guid)
        for guid, source_kind, status in explicit_rules
        if source_kind == "inventory" and status == "stocked"
    }
    observed_inventory_guids = set(
        (
            await session.execute(
                select(PnlIikoProductObservation.iiko_product_guid).where(
                    PnlIikoProductObservation.period_month == month,
                    PnlIikoProductObservation.source_kind == "inventory",
                )
            )
        ).scalars()
    )
    implicit_inventory_guids = {
        normalize_iiko_product_guid(guid) for guid in observed_inventory_guids
    } - explicit_guids
    revision_guids = await load_revision_product_guids(session)
    workup_guids = set(
        (
            await session.execute(
                select(PnlProductMonthlyDecision.iiko_product_guid).where(
                    PnlProductMonthlyDecision.period_month == month,
                    PnlProductMonthlyDecision.decision_kind == "workup",
                )
            )
        ).scalars()
    )
    return (rule_guids | implicit_inventory_guids | revision_guids) - {
        normalize_iiko_product_guid(guid) for guid in workup_guids
    }


async def build_goods_ledger(session: AsyncSession, month: date) -> GoodsLedger:
    month_start, month_end = projector.month_bounds(month)
    line_codes = set(LINE_BY_GOODS_METRIC.values()) | {WORKUP_LINE_CODE, "audit_results"}
    line_titles = {
        code: title
        for code, title in (
            await session.execute(
                select(PnlLine.code, PnlLine.title).where(PnlLine.code.in_(line_codes))
            )
        ).all()
    }
    facts = {
        fact.metric_code: fact
        for fact in (
            (
                await session.execute(
                    select(PnlIikoFact).where(
                        PnlIikoFact.period_month == month_start,
                        PnlIikoFact.metric_code.in_(set(LINE_BY_GOODS_METRIC)),
                        PnlIikoFact.direction == "total",
                    )
                )
            )
            .scalars()
            .all()
        )
    }
    detail_facts = (
        (
            await session.execute(
                select(PnlIikoGoodsFact).where(PnlIikoGoodsFact.period_month == month_start)
            )
        )
        .scalars()
        .all()
    )
    workup_items = await iiko_source.month_workup_items(session, month_start)
    revision_rows = (
        await session.execute(
            select(InventoryAudit, InventoryAuditItem)
            .join(InventoryAuditItem, InventoryAuditItem.audit_id == InventoryAudit.id)
            .where(
                InventoryAudit.status == "applied",
                InventoryAudit.business_date >= month_start,
                InventoryAudit.business_date <= month_end,
            )
            .order_by(InventoryAudit.business_date, InventoryAuditItem.product_name_snapshot)
        )
    ).all()

    detail_totals: dict[str, Decimal] = {}
    rows: list[GoodsLedgerRow] = []
    for detail in detail_facts:
        line_code = detail.line_code
        detail_totals[detail.metric_code] = (
            detail_totals.get(detail.metric_code, Decimal("0.00")) + detail.amount
        )
        rows.append(
            GoodsLedgerRow(
                line_code=line_code,
                line_title=line_titles.get(line_code, line_code),
                source_kind=detail.source_kind,
                product_guid=detail.iiko_product_guid,
                product_name=detail.product_name or detail.iiko_product_guid,
                source_amount=detail.amount,
                amount=pnl_goods_amount(detail.metric_code, detail.amount),
                rows_count=detail.rows_count,
            )
        )

    # Товары барной ревизии исключаются ТЕМ ЖЕ фильтром, что и в своде. Без него расшифровка
    # показывала бы недостачу упаковки и напитков ещё раз — и расходилась бы со строкой
    # «Результаты ревизий», которую проектор считает уже за вычетом этих товаров.
    bar_audit_guids = await inventory_source.load_packaging_guids(session)
    revision_shortage = Decimal("0.00")
    revision_surplus = Decimal("0.00")
    revision_synced_at: datetime | None = None
    for audit, detail in revision_rows:
        if detail.iiko_product_guid in bar_audit_guids:
            continue
        shortage = Decimal(detail.shortage_amount or 0)
        surplus = max(Decimal(detail.amount or 0), Decimal("0.00"))
        revision_shortage += shortage
        revision_surplus += surplus
        if audit.applied_at is not None and (
            revision_synced_at is None or audit.applied_at > revision_synced_at
        ):
            revision_synced_at = audit.applied_at
        rows.append(
            GoodsLedgerRow(
                line_code="audit_results",
                line_title=line_titles.get("audit_results", "Результаты ревизии"),
                source_kind="inventory",
                product_guid=detail.iiko_product_guid or f"inventory-item:{detail.id}",
                product_name=detail.product_name_snapshot,
                source_amount=Decimal(detail.amount or 0),
                amount=shortage,
                rows_count=1,
                audit_date=audit.business_date,
                surplus_amount=surplus,
            )
        )

    for item in workup_items:
        rows.append(
            GoodsLedgerRow(
                line_code=WORKUP_LINE_CODE,
                line_title=line_titles.get(WORKUP_LINE_CODE, "Проработка"),
                source_kind=item.source_kind,
                product_guid=item.product_guid,
                product_name=item.product_name,
                source_amount=item.source_amount,
                amount=item.amount,
                rows_count=item.rows_count,
            )
        )

    summaries: list[GoodsLedgerSummary] = []
    for metric_code, line_code in LINE_BY_GOODS_METRIC.items():
        fact = facts.get(metric_code)
        raw_details = detail_totals.get(metric_code, Decimal("0.00"))
        raw_total = fact.amount if fact is not None else None
        # Источник берётся из карты, а не подставляется одинаковым: результат инвентаризации
        # упаковки приходит из storeOperations, а не из приходных накладных, и подпись
        # «Приходные накладные» под ним читалась как ошибка данных.
        from_inventory = metric_code in iiko_sync.INVENTORY_BASKETS
        summaries.append(
            GoodsLedgerSummary(
                line_code=line_code,
                line_title=line_titles.get(line_code, line_code),
                source_kind="inventory" if from_inventory else "incoming_invoice",
                amount=(
                    pnl_goods_amount(metric_code, raw_total) if raw_total is not None else None
                ),
                details_amount=pnl_goods_amount(metric_code, raw_details),
                details_complete=(
                    True if from_inventory else (raw_total is not None and raw_total == raw_details)
                ),
                details_expected=not from_inventory,
                synced_at=fact.synced_at if fact is not None else None,
            )
        )
    # Величина строки — недостача МИНУС излишки (решение владельца 05.08.2026). Плитка
    # расшифровки обязана показывать ровно то, что стоит в своде: пока она показывала одну
    # недостачу, вкладка и отчёт расходились бы на всю сумму излишков.
    summaries.append(
        GoodsLedgerSummary(
            line_code="audit_results",
            line_title=line_titles.get("audit_results", "Результаты ревизии"),
            source_kind="inventory",
            amount=(revision_shortage - revision_surplus) if revision_rows else None,
            details_amount=revision_shortage - revision_surplus,
            details_complete=True,
            synced_at=revision_synced_at,
            surplus_amount=revision_surplus if revision_rows else None,
            shortage_amount=revision_shortage if revision_rows else None,
        )
    )
    summaries.append(
        GoodsLedgerSummary(
            line_code=WORKUP_LINE_CODE,
            line_title=line_titles.get(WORKUP_LINE_CODE, "Проработка"),
            source_kind="temporary",
            amount=sum((item.amount for item in workup_items), Decimal("0.00")),
            details_amount=sum((item.amount for item in workup_items), Decimal("0.00")),
            details_complete=True,
            synced_at=max((item.synced_at for item in workup_items), default=None),
        )
    )
    rows.sort(
        key=lambda item: (
            item.line_title,
            item.audit_date or date.min,
            -max(item.amount, item.surplus_amount),
            item.product_name,
        )
    )
    summaries.sort(key=lambda item: item.line_title)
    return GoodsLedger(month=month_start, summaries=summaries, rows=rows)


@dataclass(slots=True)
class GoodsClassificationOption:
    line_code: str
    line_title: str
    source_kind: str
    temporary: bool = False


@dataclass(slots=True)
class GoodsClassificationSource:
    source_kind: str
    amount: Decimal | None
    rows_count: int
    observed_in_month: bool
    last_seen_month: date | None


@dataclass(slots=True)
class GoodsClassificationRow:
    product_guid: str
    product_name: str
    product_code: str | None
    selected_source_kind: str | None
    sources: list[GoodsClassificationSource]
    line_code: str | None
    line_title: str | None
    status: str
    revision_product: bool
    note: str | None
    updated_at: datetime | None


@dataclass(slots=True)
class GoodsClassificationLedger:
    month: date
    attention_count: int
    attention_amount: Decimal
    rules_count: int
    options: list[GoodsClassificationOption]
    rows: list[GoodsClassificationRow]


#: Строки ОПиУ, доступные для выбора у каждого источника.
#:
#: У СКЛАДСКОГО ИСТОЧНИКА СПИСОК БЫЛ ПУСТ, И ЭТО ЛОМАЛО РАЗМЕТКУ БАРНОЙ РЕВИЗИИ. Товары
#: барной стойки — складские (их пересчитывают), и одновременно у них есть своя строка ОПиУ:
#: упаковка, коробки для пиццы, напитки. Пока выбора не было, человек мог сказать про такой
#: товар только «складской», а сохранение проставляло ``line_code = None`` — то есть каждый
#: клик по уже размеченной упаковке молча уносил её из своей строки, и вернуть привязку было
#: нечем: автоматика ``stocked`` не пересматривает. Ровно этот механизм и обнулил разметку
#: напитков в прошлый раз.
GOODS_LINES_BY_SOURCE = {
    "inventory": frozenset(inventory_source.BAR_AUDIT_LINES),
    "incoming_invoice": frozenset({"shop_maintenance", "aux_goods"}),
}


async def build_goods_classifications(
    session: AsyncSession,
    month: date,
) -> GoodsClassificationLedger:
    """Одна строка на товар: выбранный источник плюс оба наблюдаемых потока iiko."""
    month_start, _month_end = projector.month_bounds(month)
    revision_product_guids = await load_revision_product_guids(session)
    rules = (await session.execute(select(PnlProductWhitelist))).scalars().all()
    monthly_decisions = (
        (
            await session.execute(
                select(PnlProductMonthlyDecision).where(
                    PnlProductMonthlyDecision.period_month == month_start
                )
            )
        )
        .scalars()
        .all()
    )
    observations = (
        (
            await session.execute(
                select(PnlIikoProductObservation).where(
                    PnlIikoProductObservation.period_month == month_start
                )
            )
        )
        .scalars()
        .all()
    )
    last_seen = {
        (guid, source_kind): seen
        for guid, source_kind, seen in (
            await session.execute(
                select(
                    PnlIikoProductObservation.iiko_product_guid,
                    PnlIikoProductObservation.source_kind,
                    func.max(PnlIikoProductObservation.period_month),
                ).group_by(
                    PnlIikoProductObservation.iiko_product_guid,
                    PnlIikoProductObservation.source_kind,
                )
            )
        ).all()
    }
    line_codes = set().union(*GOODS_LINES_BY_SOURCE.values()) | {WORKUP_LINE_CODE}
    line_titles = {
        code: title
        for code, title in (
            await session.execute(
                select(PnlLine.code, PnlLine.title).where(PnlLine.code.in_(line_codes))
            )
        ).all()
    }

    rules_by_guid: dict[str, list[PnlProductWhitelist]] = {}
    for rule in rules:
        rules_by_guid.setdefault(rule.iiko_product_guid, []).append(rule)
    observations_by_guid: dict[str, dict[str, PnlIikoProductObservation]] = {}
    for observation in observations:
        observations_by_guid.setdefault(observation.iiko_product_guid, {})[
            observation.source_kind
        ] = observation
    monthly_by_guid = {decision.iiko_product_guid: decision for decision in monthly_decisions}
    product_guids = set(rules_by_guid) | set(observations_by_guid) | set(monthly_by_guid)
    rows: list[GoodsClassificationRow] = []
    attention_count = 0
    attention_amount = Decimal("0.00")
    for product_guid in product_guids:
        is_revision_product = normalize_iiko_product_guid(product_guid) in revision_product_guids
        product_rules = rules_by_guid.get(product_guid, [])
        product_observations = observations_by_guid.get(product_guid, {})
        monthly_decision = monthly_by_guid.get(product_guid)

        # В старых данных для GUID могли сохраниться правила обоих источников. В интерфейсе
        # и расчёте это всё равно одно решение: включённое правило приоритетнее проверки и
        # исключения, а внутри статуса берём самое свежее.
        selected_rule = None
        for rule_status in (
            "stocked",
            "include",
            "requires_owner_review",
            "exclude",
        ):
            candidates = [rule for rule in product_rules if rule.include_status == rule_status]
            if candidates:
                selected_rule = max(
                    candidates,
                    key=lambda item: (
                        (item.updated_at or item.created_at).isoformat(),
                        item.source_kind,
                    ),
                )
                break
        if is_revision_product:
            row_status = "stocked"
            selected_source_kind = "inventory"
            line_code = None
        elif monthly_decision is not None:
            row_status = "workup"
            selected_source_kind = monthly_decision.source_kind
            line_code = WORKUP_LINE_CODE
        elif selected_rule is None and "inventory" in product_observations:
            # Попадание товара в складские остатки уже является источником решения.
            # Ручная разметка нужна только если владелец хочет явно перенести товар
            # в прямые расходы по накладным.
            row_status = "stocked"
            selected_source_kind = "inventory"
            line_code = None
        else:
            row_status = (
                selected_rule.include_status if selected_rule is not None else "unclassified"
            )
            selected_source_kind = selected_rule.source_kind if selected_rule is not None else None
            line_code = selected_rule.line_code if selected_rule is not None else None
        needs_attention = row_status in {"unclassified", "requires_owner_review"}
        if product_observations and needs_attention:
            attention_count += 1
            # Один GUID в двух потоках — один новый товар, а не две независимые суммы.
            attention_amount += max(abs(item.amount) for item in product_observations.values())
        # Источник — управленческое решение, а не просто отражение того, в каком ответе iiko
        # строка встретилась сегодня. Поэтому обе альтернативы доступны всегда; отсутствие
        # суммы прямо подписывается и не создаёт вторую строку того же товара.
        source_kinds = set(GOODS_LINES_BY_SOURCE)
        source_kinds.update(product_observations)
        source_kinds.update(rule.source_kind for rule in product_rules)
        if is_revision_product:
            source_kinds.add("inventory")
        if monthly_decision is not None:
            source_kinds.add(monthly_decision.source_kind)
        classification_sources = [
            GoodsClassificationSource(
                source_kind=source_kind,
                amount=(
                    product_observations[source_kind].amount
                    if source_kind in product_observations
                    else None
                ),
                rows_count=(
                    product_observations[source_kind].rows_count
                    if source_kind in product_observations
                    else 0
                ),
                observed_in_month=source_kind in product_observations,
                last_seen_month=last_seen.get((product_guid, source_kind)),
            )
            # Складской контур показываем первым. Товар можно поставить на складской
            # учёт даже без движения в выбранном месяце — это постоянное правило
            # номенклатуры, а не фильтр текущего ответа iiko.
            for source_kind in ("inventory", "incoming_invoice")
            if source_kind in source_kinds
        ]
        preferred_observation = (
            product_observations.get(selected_source_kind)
            if selected_source_kind is not None
            else None
        )
        fallback_observation = next(iter(product_observations.values()), None)
        fallback_rule = next(iter(product_rules), None)
        rows.append(
            GoodsClassificationRow(
                product_guid=product_guid,
                product_name=(
                    (preferred_observation.product_name if preferred_observation else None)
                    or (fallback_observation.product_name if fallback_observation else None)
                    or (selected_rule.product_name if selected_rule is not None else None)
                    or (fallback_rule.product_name if fallback_rule is not None else None)
                    or product_guid
                ),
                product_code=(
                    (preferred_observation.product_code if preferred_observation else None)
                    or (fallback_observation.product_code if fallback_observation else None)
                    or (selected_rule.product_code if selected_rule is not None else None)
                    or (fallback_rule.product_code if fallback_rule is not None else None)
                ),
                selected_source_kind=selected_source_kind,
                sources=classification_sources,
                line_code=line_code,
                line_title=line_titles.get(line_code) if line_code is not None else None,
                status=row_status,
                revision_product=is_revision_product,
                note=(
                    "Автоматически: товар входит в активный список складского учёта. "
                    "Расход считается за месяц как остаток на начало + приходы − остаток "
                    "на конец."
                    if is_revision_product
                    else (
                        monthly_decision.note
                        if monthly_decision is not None
                        else (
                            selected_rule.note
                            if selected_rule is not None
                            else (
                                "Автоматически: товар найден в складских остатках iiko и "
                                "учитывается по формуле начало + приход − конец."
                                if selected_source_kind == "inventory"
                                else None
                            )
                        )
                    )
                ),
                updated_at=(
                    monthly_decision.updated_at
                    if monthly_decision is not None
                    else (selected_rule.updated_at if selected_rule is not None else None)
                ),
            )
        )
    rows.sort(
        key=lambda item: (
            item.status not in {"unclassified", "requires_owner_review"},
            not any(source.observed_in_month for source in item.sources),
            -max(
                (abs(source.amount) for source in item.sources if source.amount is not None),
                default=Decimal("0.00"),
            ),
            item.product_name.casefold(),
        )
    )
    options = [
        GoodsClassificationOption(
            line_code=line_code,
            line_title=line_titles.get(line_code, line_code),
            source_kind=source_kind,
        )
        for source_kind, allowed_lines in GOODS_LINES_BY_SOURCE.items()
        for line_code in sorted(allowed_lines, key=lambda code: line_titles.get(code, code))
    ]
    options.extend(
        GoodsClassificationOption(
            line_code=WORKUP_LINE_CODE,
            line_title=line_titles.get(WORKUP_LINE_CODE, "Проработка"),
            source_kind=source_kind,
            temporary=True,
        )
        for source_kind in ("incoming_invoice",)
    )
    return GoodsClassificationLedger(
        month=month_start,
        attention_count=attention_count,
        attention_amount=attention_amount,
        rules_count=sum(
            item.status in {"include", "exclude", "workup", "stocked"} for item in rows
        ),
        options=options,
        rows=rows,
    )


async def _rebuild_bar_audit_details(
    session: AsyncSession,
    month: date,
    carried: list[tuple[str, str | None, Decimal, int]],
    upsert_total,
) -> None:
    """Разложить расхождения барной ревизии по строкам заново — по ТЕКУЩЕЙ разметке.

    Складская ветка пересчёта раньше не трогала барные корзины вовсе, а детализацию месяца
    при этом удаляла: после правки разметки суммы упаковки и напитков оставались прежними,
    расшифровка пустела, и расхождение между сводом и вкладкой выглядело как сбой синка.

    ЧЕГО ЭТОТ ПЕРЕСЧЁТ НЕ УМЕЕТ — добавить товар, которого в выгрузке ещё не было. Расхождение
    приходит из документа iiko и живёт только в уже сохранённой детализации; товар, впервые
    получивший строку, попадёт в неё лишь со следующей выгрузкой. Это ограничение источника,
    а не расчёта, и молчать о нём нельзя — величина строки просто не изменится.
    """
    rules = {
        guid: line_code
        for guid, line_code in (
            await session.execute(
                select(PnlProductWhitelist.iiko_product_guid, PnlProductWhitelist.line_code).where(
                    PnlProductWhitelist.source_kind == "inventory",
                    PnlProductWhitelist.line_code.in_(inventory_source.BAR_AUDIT_LINES),
                )
            )
        ).all()
    }
    totals = {
        iiko_sync.WHITELIST_METRIC[line_code]: Decimal("0.00")
        for line_code in inventory_source.BAR_AUDIT_LINES
    }
    rows_count = dict.fromkeys(totals, 0)
    seen: set[str] = set()
    for guid, product_name, amount, count in carried:
        line_code = rules.get(guid)
        if line_code is None:
            continue
        metric_code = iiko_sync.WHITELIST_METRIC[line_code]
        totals[metric_code] += amount
        rows_count[metric_code] += count
        seen.add(metric_code)
        session.add(
            PnlIikoGoodsFact(
                period_month=month,
                metric_code=metric_code,
                line_code=line_code,
                source_kind="inventory",
                iiko_product_guid=guid,
                product_name=product_name,
                amount=amount,
                rows_count=count,
            )
        )
    # Ноль пишем только там, где величина УЖЕ была: корзина, по которой выгрузки не приходило,
    # обязана остаться «нет данных». Иначе правка разметки соседнего товара нарисовала бы
    # 0,00 ₽ в строке, про которую ничего не известно, — а ноль в отчёте читается как факт.
    existing = set(
        (
            await session.execute(
                select(PnlIikoFact.metric_code).where(
                    PnlIikoFact.period_month == month,
                    PnlIikoFact.metric_code.in_(totals),
                    PnlIikoFact.direction == "total",
                )
            )
        ).scalars()
    )
    for metric_code, amount in totals.items():
        if metric_code not in seen and metric_code not in existing:
            continue
        await upsert_total(
            metric_code, amount, rows_count[metric_code], iiko_sync.INVENTORY_ENDPOINT
        )


async def rebuild_goods_from_observations(
    session: AsyncSession,
    month: date,
    source_kind: str,
) -> None:
    """Пересчитать один товарный источник после изменения правила разметки.

    МЕСЯЦ БЕЗ ВЫГРУЗКИ iiko НЕ ТРОГАЕМ ВОВСЕ. Пересчёт выводит суммы из сохранённых строк
    и, когда строк нет, честно обнуляет каждую метрику — вот только этот ноль означает
    «выгрузки не было», а не «расхода не было». Записав его, мы бы стёрли живые цифры
    месяца, который просто не синхронизировался (iiko Server привязан к IP прода, так что
    вне прода это норма).

    ГРАНИЦА ИМЕННО «МЕСЯЦ ВЫГРУЖЕН», а не «у этого источника есть строки». Ноль внутри
    выгруженного месяца законен и должен записываться: товар перевели в складской контур,
    складского движения у него в месяце не было — метрика обязана появиться нулевой, иначе
    отчёт покажет «нет данных» там, где данные есть. Поэтому признак берётся по месяцу
    целиком: любая строка любого товарного источника означает, что выгрузка была.
    """
    if source_kind not in GOODS_LINES_BY_SOURCE:
        raise ValueError("Неизвестный источник товара")

    month_synced = await session.scalar(
        select(
            select(PnlIikoProductObservation.id)
            .where(PnlIikoProductObservation.period_month == month)
            .exists()
            | select(PnlIikoStockFact.id).where(PnlIikoStockFact.period_month == month).exists()
        )
    )
    if not month_synced:
        return

    now = datetime.now(UTC)

    async def upsert_total(
        metric_code: str,
        amount: Decimal,
        rows_count: int,
        source_ref: str,
    ) -> None:
        fact = await session.scalar(
            select(PnlIikoFact).where(
                PnlIikoFact.period_month == month,
                PnlIikoFact.metric_code == metric_code,
                PnlIikoFact.direction == "total",
            )
        )
        if fact is None:
            session.add(
                PnlIikoFact(
                    period_month=month,
                    metric_code=metric_code,
                    direction="total",
                    amount=amount,
                    rows_count=rows_count,
                    source_ref=source_ref,
                    synced_at=now,
                )
            )
            return
        if fact.amount != amount:
            fact.previous_amount = fact.amount
        fact.amount = amount
        fact.rows_count = rows_count
        fact.source_ref = source_ref
        fact.synced_at = now

    # Детализацию читаем ДО удаления: для складского источника она и есть единственный
    # носитель расхождений барной ревизии. Заново получить их можно только новой выгрузкой
    # из iiko, а пересчитать разметку по ним — можно и нужно.
    previous_details = (
        (
            await session.execute(
                select(PnlIikoGoodsFact).where(
                    PnlIikoGoodsFact.period_month == month,
                    PnlIikoGoodsFact.source_kind == source_kind,
                )
            )
        )
        .scalars()
        .all()
    )
    carried = [
        (item.iiko_product_guid, item.product_name, item.amount, item.rows_count)
        for item in previous_details
    ]
    await session.execute(
        delete(PnlIikoGoodsFact).where(
            PnlIikoGoodsFact.period_month == month,
            PnlIikoGoodsFact.source_kind == source_kind,
        )
    )

    if source_kind == "inventory":
        await _rebuild_bar_audit_details(session, month, carried, upsert_total)
        stocked_guids = await _stocked_product_guids(session, month)
        stock_facts = (
            (
                await session.execute(
                    select(PnlIikoStockFact).where(PnlIikoStockFact.period_month == month)
                )
            )
            .scalars()
            .all()
        )
        selected = [
            fact
            for fact in stock_facts
            if normalize_iiko_product_guid(fact.iiko_product_guid) in stocked_guids
        ]
        rows_count = sum(fact.stores_count for fact in selected)
        await upsert_total(
            iiko_sync.METRIC_STOCK_CONSUMPTION,
            sum((fact.consumption_amount for fact in selected), Decimal("0.00")),
            rows_count,
            iiko_sync.STOCK_BALANCE_ENDPOINT,
        )
        await upsert_total(
            iiko_sync.METRIC_STOCK_CLOSING,
            sum((fact.closing_amount for fact in selected), Decimal("0.00")),
            rows_count,
            iiko_sync.STOCK_BALANCE_ENDPOINT,
        )
        await session.flush()
        return

    allowed_lines = GOODS_LINES_BY_SOURCE[source_kind]
    revision_product_guids = await load_revision_product_guids(session)
    stocked_guids = await _stocked_product_guids(session, month)
    observations = (
        (
            await session.execute(
                select(PnlIikoProductObservation).where(
                    PnlIikoProductObservation.period_month == month,
                    PnlIikoProductObservation.source_kind == source_kind,
                )
            )
        )
        .scalars()
        .all()
    )
    rules = (
        (
            await session.execute(
                select(PnlProductWhitelist).where(
                    PnlProductWhitelist.source_kind == source_kind,
                    PnlProductWhitelist.include_status == "include",
                    PnlProductWhitelist.line_code.in_(allowed_lines),
                )
            )
        )
        .scalars()
        .all()
    )
    rule_by_guid = {rule.iiko_product_guid: rule for rule in rules}
    temporary_guids = set(
        (
            await session.execute(
                select(PnlProductMonthlyDecision.iiko_product_guid).where(
                    PnlProductMonthlyDecision.period_month == month,
                    PnlProductMonthlyDecision.decision_kind == "workup",
                )
            )
        ).scalars()
    )
    metric_by_line = iiko_sync.WHITELIST_METRIC
    totals = {metric_by_line[line_code]: Decimal("0.00") for line_code in allowed_lines}
    rows_count = {metric: 0 for metric in totals}

    for observation in observations:
        if normalize_iiko_product_guid(observation.iiko_product_guid) in revision_product_guids:
            continue
        if normalize_iiko_product_guid(observation.iiko_product_guid) in stocked_guids:
            continue
        if observation.iiko_product_guid in temporary_guids:
            continue
        rule = rule_by_guid.get(observation.iiko_product_guid)
        if rule is None or rule.line_code is None:
            continue
        metric_code = metric_by_line[rule.line_code]
        totals[metric_code] += observation.amount
        rows_count[metric_code] += observation.rows_count
        session.add(
            PnlIikoGoodsFact(
                period_month=month,
                metric_code=metric_code,
                line_code=rule.line_code,
                source_kind=source_kind,
                iiko_product_guid=observation.iiko_product_guid,
                product_name=observation.product_name or rule.product_name,
                amount=observation.amount,
                rows_count=observation.rows_count,
            )
        )

    for metric_code, amount in totals.items():
        await upsert_total(
            metric_code,
            amount,
            rows_count[metric_code],
            iiko_sync.INCOMING_INVOICE_ENDPOINT,
        )
    await session.flush()


async def save_goods_classification(
    session: AsyncSession,
    *,
    display_month: date,
    product_guid: str,
    source_kind: str,
    status: str,
    line_code: str | None,
    note: str | None,
    user_id: uuid.UUID | None,
) -> GoodsClassificationLedger:
    """Сохранить постоянное правило либо временную «Проработку» выбранного месяца."""
    month_start, _month_end = projector.month_bounds(display_month)
    await accounting_periods.assert_month_open(
        session, month_start, action="менять разметку товаров"
    )
    revision_product_guids = await load_revision_product_guids(session)
    is_revision_product = normalize_iiko_product_guid(product_guid) in revision_product_guids
    if source_kind not in GOODS_LINES_BY_SOURCE:
        raise ValueError("Неизвестный источник товара")
    if status not in {
        "include",
        "requires_owner_review",
        "exclude",
        "workup",
        "stocked",
    }:
        raise ValueError("Неизвестный статус разметки")
    if status == "workup" and line_code != WORKUP_LINE_CODE:
        raise ValueError("Для временного решения выберите статью «Проработка»")
    if status == "workup" and source_kind != "incoming_invoice":
        raise ValueError("Проработка учитывается по приходной накладной текущего месяца")
    # «Включить» и «складской» — для инвентаризации одно и то же состояние: товар пересчитывают
    # на складе, и его расхождение идёт в барную строку. Экран шлёт include для любой выбранной
    # статьи, поэтому нормализуем здесь, а не заставляем клиента знать это различие.
    if source_kind == "inventory" and status == "include":
        status = "stocked"
    if status == "include" and line_code not in GOODS_LINES_BY_SOURCE[source_kind]:
        raise ValueError("Выбранная строка ОПиУ не соответствует источнику")
    if status == "stocked" and source_kind != "inventory":
        raise ValueError("Складской учёт доступен только для складских остатков iiko")
    if (
        status == "stocked"
        and line_code is not None
        and line_code not in GOODS_LINES_BY_SOURCE["inventory"]
    ):
        raise ValueError("Складскому товару доступны только строки барной ревизии")
    if status not in {"include", "workup", "stocked"} and line_code is not None:
        raise ValueError("Для проверки или исключения строка ОПиУ не выбирается")
    if is_revision_product and (
        source_kind != "inventory" or status != "stocked" or line_code is not None
    ):
        raise ValueError(
            "Товар входит в активный список складского учёта и размечается автоматически"
        )

    product_rules = (
        (
            await session.execute(
                select(PnlProductWhitelist).where(
                    PnlProductWhitelist.iiko_product_guid == product_guid
                )
            )
        )
        .scalars()
        .all()
    )
    rule = next((item for item in product_rules if item.source_kind == source_kind), None)
    latest_observation = await session.scalar(
        select(PnlIikoProductObservation)
        .where(
            PnlIikoProductObservation.iiko_product_guid == product_guid,
            PnlIikoProductObservation.source_kind == source_kind,
        )
        .order_by(PnlIikoProductObservation.period_month.desc())
        .limit(1)
    )
    any_latest_observation = latest_observation or await session.scalar(
        select(PnlIikoProductObservation)
        .where(PnlIikoProductObservation.iiko_product_guid == product_guid)
        .order_by(PnlIikoProductObservation.period_month.desc())
        .limit(1)
    )
    if not product_rules and any_latest_observation is None:
        raise ValueError("Товар не найден в загруженной номенклатуре")

    if status == "workup":
        current_observation = await session.scalar(
            select(PnlIikoProductObservation).where(
                PnlIikoProductObservation.period_month == month_start,
                PnlIikoProductObservation.iiko_product_guid == product_guid,
                PnlIikoProductObservation.source_kind == source_kind,
            )
        )
        if current_observation is None:
            raise ValueError("В выбранном месяце у этого источника нет суммы")
        if any(rule.include_status in {"include", "exclude", "stocked"} for rule in product_rules):
            raise ValueError("Проработка доступна только для ещё не размеченного товара")

        # Выбор источника до выбора статьи создаёт черновое review-правило. Для временного
        # решения оно больше не нужно: иначе следующий месяц помнил бы даже источник.
        await session.execute(
            delete(PnlProductWhitelist).where(PnlProductWhitelist.iiko_product_guid == product_guid)
        )
        monthly_decision = await session.scalar(
            select(PnlProductMonthlyDecision).where(
                PnlProductMonthlyDecision.period_month == month_start,
                PnlProductMonthlyDecision.iiko_product_guid == product_guid,
            )
        )
        if monthly_decision is None:
            session.add(
                PnlProductMonthlyDecision(
                    period_month=month_start,
                    iiko_product_guid=product_guid,
                    source_kind=source_kind,
                    decision_kind="workup",
                    note=note,
                    updated_by_user_id=user_id,
                )
            )
        else:
            monthly_decision.source_kind = source_kind
            monthly_decision.note = note
            monthly_decision.updated_by_user_id = user_id
            monthly_decision.updated_at = datetime.now(UTC)
        await session.commit()
        return await build_goods_classifications(session, month_start)

    # Постоянное решение заменяет временную проработку только в редактируемом месяце.
    await session.execute(
        delete(PnlProductMonthlyDecision).where(
            PnlProductMonthlyDecision.period_month == month_start,
            PnlProductMonthlyDecision.iiko_product_guid == product_guid,
        )
    )

    # Выбор источника относится к товару целиком. Старое правило второго потока удаляем,
    # чтобы один GUID физически не мог одновременно попасть и в накладные, и в ревизию.
    await session.execute(
        delete(PnlProductWhitelist).where(
            PnlProductWhitelist.iiko_product_guid == product_guid,
            PnlProductWhitelist.source_kind != source_kind,
        )
    )
    if rule is None:
        rule = PnlProductWhitelist(
            iiko_product_guid=product_guid,
            source_kind=source_kind,
            line_code=line_code,
            include_status=status,
            product_name=any_latest_observation.product_name if any_latest_observation else None,
            product_code=any_latest_observation.product_code if any_latest_observation else None,
            note=note,
            updated_by_user_id=user_id,
        )
        session.add(rule)
    else:
        rule.line_code = line_code
        rule.include_status = status
        rule.note = note
        rule.updated_by_user_id = user_id
        rule.updated_at = datetime.now(UTC)
        if any_latest_observation is not None:
            rule.product_name = any_latest_observation.product_name or rule.product_name
            rule.product_code = any_latest_observation.product_code or rule.product_code
    await session.flush()

    # ПРАВКА ДЕЙСТВУЕТ С РЕДАКТИРУЕМОГО МЕСЯЦА ВПЕРЁД, а не на всю историю товара. Раньше
    # пересчитывались ВСЕ месяцы, где встречался GUID: разметив товар на августовской
    # странице, человек молча менял прибыль июля — уже показанного и сверенного. Владелец
    # назвал это косяком 05.08.2026. Поправить нужно именно прошлый месяц — правку делают
    # на ЕГО странице, и тогда он приходит сюда как ``month_start`` осознанно.
    #
    # Закрытые месяцы отсекаются отдельно поверх этого: правка вперёд может задеть месяц,
    # который уже закрыли (правку июля видит и закрытый август). Замок сильнее направления.
    closed = await accounting_periods.closed_months(session)
    affected_months = {
        month
        for month in (
            await session.execute(
                select(PnlIikoProductObservation.period_month)
                .where(
                    PnlIikoProductObservation.iiko_product_guid == product_guid,
                )
                .distinct()
            )
        ).scalars()
        if month >= month_start
    }
    affected_months.add(month_start)
    for affected_month in sorted(affected_months - closed):
        # Источник — единое решение для товара. Его смена одновременно убирает товар
        # из старого леджера и добавляет в новый, поэтому пересчитываем оба контура.
        await rebuild_goods_from_observations(session, affected_month, "inventory")
        await rebuild_goods_from_observations(session, affected_month, "incoming_invoice")
    await session.commit()
    return await build_goods_classifications(
        session,
        display_month,
    )


@dataclass(slots=True)
class RecognitionLedgerRow:
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


@dataclass(slots=True)
class RecognitionLedgerTotals:
    recognized: Decimal
    waiting_document: Decimal
    missing_period: Decimal
    without_primary: Decimal
    unattributed: Decimal

    @property
    def unrecognized(self) -> Decimal:
        return self.waiting_document + self.missing_period


@dataclass(slots=True)
class RecognitionLedger:
    month: date
    totals: RecognitionLedgerTotals
    rows: list[RecognitionLedgerRow]


RECOGNITION_REASON = {
    recognition_source.ORIGIN_DOCUMENT: "Расход признан по документу контрагента",
    recognition_source.ORIGIN_LEASE: "Расход начислен по договору аренды",
    recognition_source.ORIGIN_UTILITY: "Расход начислен по расчёту арендодателя",
    recognition_source.ORIGIN_BY_TARIFF: "Расход начислен по тарифу; документ не требуется",
    recognition_source.ORIGIN_AWAITING_DOCUMENT: (
        "Расход признан самоактом, но закрывающий документ ещё ожидается"
    ),
    recognition_source.ORIGIN_PAYMENT_LINE: (
        "Расход признан по строке платежа; закрывающего документа нет"
    ),
}


def recognition_reason(origin: str) -> str:
    return RECOGNITION_REASON.get(origin, origin)


async def build_recognition_ledger(session: AsyncSession, month: date) -> RecognitionLedger:
    month_start, month_end = projector.month_bounds(month)
    recognized = await recognition_source.build_recognition_layer(session, month_start, month_end)
    waiting = await waiting_source.build_waiting_layer(
        session,
        month_start,
        month_end,
        # Пара «контрагент × статья», как и в проекторе: признанная аренда арендодателя не
        # отвечает за то, приехал ли документ по его коммуналке.
        recognized_pairs={
            (detail.counterparty_id, detail.article_id)
            for detail in recognized.details
            if detail.counterparty_id is not None
        },
    )
    unperioded = await waiting_source.build_unperiodled_layer(session, month_start, month_end)

    counterparty_ids = {
        item.counterparty_id
        for item in (*recognized.details, *waiting.items, *unperioded.items)
        if item.counterparty_id is not None
    }
    article_ids = {
        item.article_id
        for item in (*recognized.details, *waiting.items, *unperioded.items)
        if item.article_id is not None
    }
    counterparties = {
        item_id: name
        for item_id, name in (
            await session.execute(
                select(Counterparty.id, Counterparty.name).where(
                    Counterparty.id.in_(counterparty_ids)
                )
            )
        ).all()
    }
    articles = {
        item_id: name
        for item_id, name in (
            await session.execute(
                select(DdsArticle.id, DdsArticle.name).where(DdsArticle.id.in_(article_ids))
            )
        ).all()
    }
    article_lines = {
        article_id: line_code
        for article_id, line_code in (
            await session.execute(
                select(PnlArticleRule.article_id, PnlArticleRule.line_code).where(
                    PnlArticleRule.is_active.is_(True),
                    PnlArticleRule.in_pnl.is_(True),
                )
            )
        ).all()
    }

    # Леджер признания — расшифровка ОПиУ, а не второй реестр всей ДДС. Неприбыльные статьи
    # (выдача/возврат займов, тело кредита, переводы) не имеют строки ОПиУ и не должны ни
    # показываться как «ждём документ», ни раздувать итог ожиданий. ``article_id=None``
    # сохраняем: это настоящий неразнесённый расход, который пользователь обязан увидеть.
    def belongs_to_pnl(item) -> bool:
        return item.article_id is None or item.article_id in article_lines

    recognized_items = [item for item in recognized.details if belongs_to_pnl(item)]
    waiting_items = [item for item in waiting.items if belongs_to_pnl(item)]
    unperioded_items = [item for item in unperioded.items if belongs_to_pnl(item)]
    line_codes = {code for code in article_lines.values() if code}
    line_titles = {
        code: title
        for code, title in (
            await session.execute(
                select(PnlLine.code, PnlLine.title).where(PnlLine.code.in_(line_codes))
            )
        ).all()
    }

    def common(item) -> dict:
        line_code = article_lines.get(item.article_id)
        return {
            "counterparty_id": item.counterparty_id,
            "counterparty_name": counterparties.get(item.counterparty_id, "Без контрагента"),
            "article_id": item.article_id,
            "article_name": articles.get(item.article_id, "Без статьи"),
            "line_code": line_code,
            "line_title": line_titles.get(line_code) if line_code else None,
        }

    rows: list[RecognitionLedgerRow] = []
    for item in recognized_items:
        rows.append(
            RecognitionLedgerRow(
                status="recognized",
                source_kind="accrual",
                source_id=item.accrual_id,
                amount=item.amount,
                event_date=item.service_period_start,
                service_period_start=item.service_period_start,
                service_period_end=item.service_period_end,
                has_primary=item.has_primary,
                reason=recognition_reason(item.origin),
                **common(item),
            )
        )
    for item in waiting_items:
        rows.append(
            RecognitionLedgerRow(
                status="waiting_document",
                source_kind="prepayment",
                source_id=item.prepayment_id,
                amount=item.amount,
                event_date=item.paid_on,
                service_period_start=item.period_start,
                service_period_end=item.period_end,
                has_primary=False,
                reason=(
                    "Оплачено, но закрывающий документ за период ещё не получен"
                    if item.period_known
                    else "Оплачено, период услуги неизвестен и документ ещё не получен"
                ),
                **common(item),
            )
        )
    for item in unperioded_items:
        rows.append(
            RecognitionLedgerRow(
                status="missing_period",
                source_kind="invoice",
                source_id=item.invoice_id,
                amount=item.amount,
                event_date=item.invoice_date,
                service_period_start=None,
                service_period_end=None,
                has_primary=True,
                reason="Документ оплачен, но период услуги не заполнен — расход не признан",
                **common(item),
            )
        )

    status_order = {"waiting_document": 0, "missing_period": 1, "recognized": 2}
    rows.sort(key=lambda item: (status_order[item.status], -item.amount, item.counterparty_name))
    return RecognitionLedger(
        month=month_start,
        totals=RecognitionLedgerTotals(
            recognized=sum((item.amount for item in recognized_items), Decimal("0.00")),
            waiting_document=sum((item.amount for item in waiting_items), Decimal("0.00")),
            missing_period=sum((item.amount for item in unperioded_items), Decimal("0.00")),
            without_primary=sum(
                (item.amount for item in recognized_items if not item.has_primary),
                Decimal("0.00"),
            ),
            unattributed=sum(
                (item.amount for item in recognized_items if item.article_id is None),
                Decimal("0.00"),
            ),
        ),
        rows=rows,
    )

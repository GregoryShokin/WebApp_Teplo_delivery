"""Расчёт ЗП административного персонала.

Логика админ-ведомости принципиально проще производственной: фиксированный месячный
оклад, выплачиваемый двумя полумесячными частями (1–15 → выплата 15-го, 16–конец
месяца → выплата 1-го числа). Никаких явок, процентов от выручки, депозитов и фонда.

Каждая полумесячная ведомость = один период `half_month` = одна выплата. База строки —
оклад/2, пропорционально календарным дням внутри полумесяца, если найм/увольнение
сотрудника попадает внутрь периода. Премии и штрафы берутся по роли (только админские
должности) и дате начисления внутри периода. НДФЛ и страховые здесь не считаются —
это отдельный налоговый контур (следующий этап).
"""

from __future__ import annotations

import calendar
import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AppSetting,
    DishwasherShift,
    Employee,
    EmployeePayout,
    PayrollLine,
    PayrollPeriod,
    PayrollRate,
    PayrollRun,
)
from app.services.employee_effective_events import get_position_on_date
from app.services.payroll_adjustment_service import load_adjustments_for_period
from app.services.payroll_advance_recovery import (
    apply_advance_issuances,
    apply_advance_recoveries,
)
from app.services.payroll_calculator import adjustment_component, decimal, money, money_string
from app.services.payroll_runner import PayrollConflictError, PayrollNotFoundError
from app.services.position_registry import (
    admin_payroll_positions,
    dishwasher_positions,
    okladnik_positions,
)

ADMIN_OKLAD_CATEGORY = "admin"
HALF_MONTH_SPLIT_DAY = 15
_CENTS = Decimal("0.01")

# Окладные должности (оклад/2 за полупериод) и посменные из месячного пула
# (мойщицы) читаются из реестра должностей: archetype okladnik / shift_pool →
# position_registry.okladnik_positions() / dishwasher_positions().

# Режим выплаты оклада за полумесяц. split — пополам (½ на 15-е, ½ на 1-е);
# first_half — весь оклад на выплату 15-го; second_half — весь оклад на выплату 1-го;
# on_demand — начисляется в долг (½ за полупериод), но не выплачивается автоматически:
# выплата по востребованию произвольной суммой (собственник). В ведомости показывает
# задолженность (накоплено − выплачено), к автовыплате = 0.
PAYOUT_MODE_SPLIT = "split"
PAYOUT_MODE_FIRST_HALF = "first_half"
PAYOUT_MODE_SECOND_HALF = "second_half"
PAYOUT_MODE_ON_DEMAND = "on_demand"
PAYOUT_MODES = (
    PAYOUT_MODE_SPLIT,
    PAYOUT_MODE_FIRST_HALF,
    PAYOUT_MODE_SECOND_HALF,
    PAYOUT_MODE_ON_DEMAND,
)
OKLADNIK_PAYOUT_MODES_KEY = "payroll.okladnik_payout_modes"

DISHWASHER_POOL_KEY = "payroll.dishwasher_pool"
DISHWASHER_POOL_DEFAULT = Decimal("15000")


# --------------------------------------------------------------------------- #
# Генерация полумесячных периодов
# --------------------------------------------------------------------------- #
def _last_day_of_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _first_half(year: int, month: int) -> tuple[date, date, date]:
    """Первая половина месяца (1–15), выплата 15-го числа того же месяца."""
    start = date(year, month, 1)
    end = date(year, month, HALF_MONTH_SPLIT_DAY)
    payroll_date = date(year, month, HALF_MONTH_SPLIT_DAY)
    return start, end, payroll_date


def _second_half(year: int, month: int) -> tuple[date, date, date]:
    """Вторая половина месяца (16–конец), выплата 1-го числа следующего месяца."""
    start = date(year, month, HALF_MONTH_SPLIT_DAY + 1)
    end = date(year, month, _last_day_of_month(year, month))
    next_month_year = year + 1 if month == 12 else year
    next_month = 1 if month == 12 else month + 1
    payroll_date = date(next_month_year, next_month, 1)
    return start, end, payroll_date


def latest_admin_period_dates(today: date) -> tuple[date, date, date]:
    """Самая недавно завершившаяся полумесячная ведомость относительно `today`.

    Аналог недельного `compute_next_payroll_period_dates`: возвращает период, чья
    выплата только что наступила (по последнему прошедшему «1» или «15»).
    """
    if today.day >= HALF_MONTH_SPLIT_DAY:
        # Последняя выплата была 15-го — это первая половина текущего месяца.
        return _first_half(today.year, today.month)
    # До 15-го числа последняя выплата была 1-го — вторая половина прошлого месяца.
    prev_last_day = date(today.year, today.month, 1)
    prev_month_year = prev_last_day.year if today.month != 1 else today.year - 1
    prev_month = today.month - 1 if today.month != 1 else 12
    return _second_half(prev_month_year, prev_month)


def next_admin_period_dates(prev_start: date, prev_end: date) -> tuple[date, date, date]:
    """Полумесячная ведомость, следующая за переданной."""
    if prev_start.day == 1:
        # Была первая половина → следующая вторая половина того же месяца.
        return _second_half(prev_start.year, prev_start.month)
    # Была вторая половина → следующая первая половина следующего месяца.
    next_year = prev_start.year + 1 if prev_start.month == 12 else prev_start.year
    next_month = 1 if prev_start.month == 12 else prev_start.month + 1
    return _first_half(next_year, next_month)


async def auto_create_next_admin_period(
    session: AsyncSession,
    *,
    today: date | None = None,
) -> PayrollPeriod:
    existing = await session.scalar(
        select(PayrollPeriod)
        .where(PayrollPeriod.period_type == "half_month")
        .order_by(desc(PayrollPeriod.start_date))
    )
    if existing is None:
        start_date, end_date, payroll_date = latest_admin_period_dates(
            today or datetime.now(UTC).date()
        )
    else:
        start_date, end_date, payroll_date = next_admin_period_dates(
            existing.start_date, existing.end_date
        )

    duplicate = await session.scalar(
        select(PayrollPeriod).where(
            PayrollPeriod.period_type == "half_month",
            PayrollPeriod.start_date == start_date,
            PayrollPeriod.end_date == end_date,
        )
    )
    if duplicate is not None:
        return duplicate

    period = PayrollPeriod(
        period_type="half_month",
        start_date=start_date,
        end_date=end_date,
        payroll_date=payroll_date,
        status="open",
    )
    session.add(period)
    await session.commit()
    await session.refresh(period)
    return period


# --------------------------------------------------------------------------- #
# Запуск админ-ведомости
# --------------------------------------------------------------------------- #
async def run_admin_payroll(
    session: AsyncSession,
    period_id: uuid.UUID,
) -> PayrollRun:
    period = await session.get(PayrollPeriod, period_id)
    if period is None:
        raise PayrollNotFoundError("Payroll period not found")
    if period.period_type != "half_month":
        raise PayrollConflictError("Период не является полумесячным (админским)")
    if period.status == "finalized":
        raise PayrollConflictError("Payroll period is finalized")

    finalized_run = await session.scalar(
        select(PayrollRun).where(
            PayrollRun.period_id == period.id,
            PayrollRun.status == "finalized",
        )
    )
    if finalized_run is not None:
        raise PayrollConflictError("Payroll run is finalized")

    # Upsert: переиспользуем последнюю незафинализированную ведомость периода,
    # чтобы пересчёт не плодил дубликаты строк.
    existing_run = await session.scalar(
        select(PayrollRun)
        .where(PayrollRun.period_id == period.id)
        .order_by(PayrollRun.started_at.desc())
        .limit(1)
    )
    if existing_run is not None:
        await session.execute(
            text("DELETE FROM payroll_line WHERE run_id = :run_id"),
            {"run_id": existing_run.id},
        )
        await session.execute(
            text("DELETE FROM salary_advance_recovery WHERE run_id = :run_id"),
            {"run_id": existing_run.id},
        )
        existing_run.started_at = datetime.now(UTC)
        existing_run.finished_at = None
        existing_run.status = "running"
        existing_run.blocking_issues = []
        existing_run.summary = {}
        run = existing_run
    else:
        run = PayrollRun(
            period_id=period.id,
            started_at=datetime.now(UTC),
            status="running",
            blocking_issues=[],
            summary={},
        )
        session.add(run)
    await session.flush()

    try:
        lines, blocking_issues, summary = await calculate_admin_payroll_lines(
            session, period, run.id
        )
        if blocking_issues:
            run.status = "blocked"
            run.finished_at = datetime.now(UTC)
            run.blocking_issues = blocking_issues
            run.summary = summary
            await session.commit()
            await session.refresh(run)
            return run

        advance_summary = await apply_advance_recoveries(session, period, run, lines)
        advance_issue_summary = await apply_advance_issuances(session, period, run, lines)
        for line in lines:
            session.add(line)
        # Итог к выплате — ПОСЛЕ удержаний и выдач авансов/займов (накопленный выше — начисления).
        summary["total_payable"] = money(
            sum((decimal(line.total_payable) for line in lines), Decimal("0"))
        )
        run.status = "completed"
        run.finished_at = datetime.now(UTC)
        run.blocking_issues = []
        run.summary = {**summary, **advance_summary, **advance_issue_summary}
        await session.commit()
        await session.refresh(run)
        return run
    except Exception as exc:
        run.status = "failed"
        run.finished_at = datetime.now(UTC)
        run.summary = {"error": str(exc)[:500]}
        await session.commit()
        raise


# --------------------------------------------------------------------------- #
# Расчёт строк
# --------------------------------------------------------------------------- #
async def calculate_admin_payroll_lines(
    session: AsyncSession,
    period: PayrollPeriod,
    run_id: uuid.UUID,
) -> tuple[list[PayrollLine], list[dict[str, Any]], dict[str, Any]]:
    primary_employees = await _load_admin_employees(session, period)
    primary_ids = {employee.id for employee in primary_employees}
    # «Исполняющие» другую окладную должность через персональный оклад (напр. кассир исполняет
    # помощника менеджера): их основная должность неадминская, но они получают админ-оклад
    # СВЕРХ основной ЗП. Каждый идёт в админ-ведомость по должности override'а (forced_position).
    acting_employees = await _load_acting_admin_employees(
        session, period, exclude_ids=primary_ids
    )
    work_items: list[tuple[Employee, str | None]] = [
        (employee, None) for employee in primary_employees
    ]
    work_items += [(employee, position) for employee, position in acting_employees]
    employee_ids = [employee.id for employee, _ in work_items]

    adjustments_by_key = await load_adjustments_for_period(
        session,
        employee_ids=employee_ids,
        period_start=period.start_date,
        period_end=period.end_date,
        roles=admin_payroll_positions(),
    )
    adjustments_by_employee: dict[uuid.UUID, list[Any]] = {}
    for (employee_id, _work_date), items in adjustments_by_key.items():
        adjustments_by_employee.setdefault(employee_id, []).extend(items)

    payout_modes = await _load_okladnik_payout_modes(session)
    # Суммы, включённые в выплату этой ведомости вручную («Включить в выплату») — формируют
    # total_payable on_demand-строк (платятся вместе с ведомостью).
    included_by_employee = await load_included_payouts_by_employee(session, run_id)
    dishwasher_pool = await _load_dishwasher_pool(session)
    dishwasher_rate = _dishwasher_shift_rate(dishwasher_pool, period)
    shift_counts = await _load_dishwasher_shift_counts(
        session, employee_ids, period.start_date, period.end_date
    )

    lines: list[PayrollLine] = []
    blocking_issues: list[dict[str, Any]] = []
    skipped_no_oklad: list[dict[str, Any]] = []
    skipped_wrong_position: list[dict[str, Any]] = []
    total_payable = Decimal("0")

    for employee, forced_position in work_items:
        if forced_position is not None:
            # «Исполняющий» — считаем по назначенной окладной должности (не по основной).
            position = forced_position
        else:
            position = await get_position_on_date(session, employee.id, period.end_date)
            position = position or employee.position
            if position not in admin_payroll_positions():
                # На дату периода сотрудник был на другой (неадминской) должности — например
                # повар, позже повышенный в управляющие. За этот период он считается по своей
                # тогдашней должности (производственная ведомость), не здесь.
                skipped_wrong_position.append(
                    {
                        "employee_id": str(employee.id),
                        "employee_name": employee.full_name,
                        "period_position": position,
                    }
                )
                continue

        premium, penalty, bonus_items, penalty_items = _sum_adjustments(
            adjustments_by_employee.get(employee.id, [])
        )
        has_adjustments = bool(bonus_items or penalty_items)

        if position in dishwasher_positions():
            # Посменная оплата: смены в полупериоде × ставку-из-пула.
            shifts = shift_counts.get(employee.id, 0)
            if shifts <= 0 and not has_adjustments:
                continue
            base_pay = (dishwasher_rate * Decimal(shifts)).quantize(_CENTS)
            components: dict[str, Any] = {
                "kind": "dishwasher_shifts",
                "shifts": shifts,
                "shift_rate": money_string(dishwasher_rate),
                "monthly_pool": money_string(dishwasher_pool),
                "base_pay": money_string(base_pay),
                "adjustments": {"bonuses": bonus_items, "penalties": penalty_items},
            }
        else:
            oklad = await load_admin_oklad(session, employee.id, position, period.end_date)
            if oklad is None:
                # Нет применимого оклада (ни дефолта должности, ни персонального) —
                # сотрудник просто не попадает в ведомость. Не ошибка (системные/AI-аккаунты,
                # собственники без оклада), показываем информационным списком.
                skipped_no_oklad.append(
                    {
                        "employee_id": str(employee.id),
                        "employee_name": employee.full_name,
                        "position": position,
                    }
                )
                continue
            mode = _okladnik_payout_mode(payout_modes, position)
            base_pay, proration = _okladnik_base_pay(oklad, mode, employee, period)
            if base_pay <= 0 and not has_adjustments and mode != PAYOUT_MODE_ON_DEMAND:
                # Этот полупериод оклад не выплачивается (режим «всё на другую дату»).
                continue
            # on_demand всегда даёт строку (показывает задолженность), даже при base_pay=0.
            components = {
                "kind": "admin_oklad",
                "monthly_oklad": money_string(oklad),
                "payout_mode": mode,
                "proration": proration,
                "base_pay": money_string(base_pay),
                "adjustments": {"bonuses": bonus_items, "penalties": penalty_items},
            }

        line_total = (base_pay + premium - penalty).quantize(_CENTS)

        proration_data = components.get("proration")
        if isinstance(proration_data, dict) and proration_data.get("on_demand"):
            # on_demand: остаток = полный месячный оклад (accrual_amount из _okladnik_base_pay);
            # к выплате по ведомости — только суммы, включённые вручную («Включить в выплату»,
            # status included/paid для этой ведомости). Остальное остаётся «на потом».
            line_total = included_by_employee.get(employee.id, Decimal("0")).quantize(_CENTS)

        total_payable += line_total
        lines.append(
            PayrollLine(
                run_id=run_id,
                employee_id=employee.id,
                role=position,
                base_pay=base_pay,
                premium=premium.quantize(_CENTS),
                percent_pay=Decimal("0"),
                vacation_pay=Decimal("0"),
                ndfl_withheld=Decimal("0"),
                fund_accrual=Decimal("0"),
                deduction=penalty.quantize(_CENTS),
                total_payable=line_total,
                components=components,
            )
        )

    summary = {
        "kind": "admin",
        "line_count": len(lines),
        "employee_count": len(lines),
        "total_payable": money(total_payable),
        "blocking_issue_count": len(blocking_issues),
        "excluded_no_oklad": skipped_no_oklad,
        "excluded_wrong_position": skipped_wrong_position,
    }
    return lines, blocking_issues, summary


# owner_salary — выплата уменьшает остаток ЗП по востребованию (режим оклада on_demand).
EMPLOYEE_PAYOUT_KIND_OWNER_SALARY = "owner_salary"
# Статусы выплат, уменьшающих остаток: included (включено в выплату ведомости), pending
# (банк-черновик в полёте), paid (проведено). draft/failed/cancelled — не уменьшают.
_ONDEMAND_PAYOUT_STATUSES = ("included", "pending", "paid")


async def compute_on_demand_debt(
    session: AsyncSession,
    employee_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, dict[str, Decimal]]:
    """Остаток ЗП «по востребованию» (режим оклада ``on_demand``).

    ``accrued`` = Σ ``accrual_amount`` по всем on_demand-строкам сотрудника (полный оклад за
    месяц, по одной строке на период; пересчёт заменяет строки, дублей нет); ``paid`` = Σ
    ``amount`` выплат ``EmployeePayout(kind='owner_salary')`` со статусом included/pending/paid;
    ``debt`` (= остаток, «сколько ещё можно забрать») = ``accrued − paid`` (может быть
    отрицательным при переплате). Возвращает записи только для сотрудников с начислениями/выплатами.
    """
    unique_ids = list({eid for eid in employee_ids})
    if not unique_ids:
        return {}

    # Начислено: оклад по месяцам. Обе половины месяца несут полный оклад — считаем ОДИН раз
    # на (сотрудник, месяц), затем суммируем по месяцам (остаток накапливается по месяцам).
    line_rows = await session.scalars(
        select(PayrollLine).where(PayrollLine.employee_id.in_(unique_ids))
    )
    accrued_by_month: dict[tuple[uuid.UUID, str], Decimal] = {}
    for line in line_rows.all():
        components = line.components if isinstance(line.components, dict) else {}
        proration = components.get("proration")
        if not isinstance(proration, dict) or not proration.get("on_demand"):
            continue
        raw = proration.get("accrual_amount")
        if raw is None:
            continue
        month = str(proration.get("period_month") or line.run_id)
        accrued_by_month[(line.employee_id, month)] = decimal(raw)
    accrued: dict[uuid.UUID, Decimal] = {}
    for (eid, _month), value in accrued_by_month.items():
        accrued[eid] = accrued.get(eid, Decimal("0")) + value

    # Выплачено/включено: EmployeePayout(kind=owner_salary), учтённые статусы.
    paid_rows = await session.execute(
        select(
            EmployeePayout.employee_id,
            func.coalesce(func.sum(EmployeePayout.amount), 0),
        )
        .where(
            EmployeePayout.employee_id.in_(unique_ids),
            EmployeePayout.kind == EMPLOYEE_PAYOUT_KIND_OWNER_SALARY,
            EmployeePayout.status.in_(_ONDEMAND_PAYOUT_STATUSES),
        )
        .group_by(EmployeePayout.employee_id)
    )
    paid: dict[uuid.UUID, Decimal] = {eid: decimal(total) for eid, total in paid_rows.all()}

    result: dict[uuid.UUID, dict[str, Decimal]] = {}
    for eid in unique_ids:
        a = accrued.get(eid, Decimal("0")).quantize(_CENTS)
        p = paid.get(eid, Decimal("0")).quantize(_CENTS)
        if a == 0 and p == 0:
            continue
        result[eid] = {"accrued": a, "paid": p, "debt": (a - p).quantize(_CENTS)}
    return result


async def load_included_payouts_by_employee(
    session: AsyncSession,
    run_id: uuid.UUID,
) -> dict[uuid.UUID, Decimal]:
    """Σ включённых в выплату ЭТОЙ ведомости сумм по сотруднику (status included/paid).

    Формирует ``total_payable`` on_demand-строки: сумма платится вместе с ведомостью.
    """
    rows = await session.execute(
        select(
            EmployeePayout.employee_id,
            func.coalesce(func.sum(EmployeePayout.amount), 0),
        )
        .where(
            EmployeePayout.run_id == run_id,
            EmployeePayout.kind == EMPLOYEE_PAYOUT_KIND_OWNER_SALARY,
            EmployeePayout.status.in_(("included", "paid")),
        )
        .group_by(EmployeePayout.employee_id)
    )
    return {eid: decimal(total).quantize(_CENTS) for eid, total in rows.all()}


async def create_included_payout(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    employee_id: uuid.UUID,
    amount: Decimal,
    note: str | None = None,
    created_by_user_id: uuid.UUID | None = None,
) -> "EmployeePayout":
    """«Включить в выплату»: включить сумму ЗП по востребованию в выплату ведомости.

    Создаёт ``EmployeePayout(kind=owner_salary, status='included', run_id)`` без счёта и
    проводки — сумма платится ВМЕСТЕ с ведомостью (формирует ``total_payable`` on_demand-строки),
    остаток уменьшается сразу. Отдельной проводки нет: расход проводит выплата ведомости.
    Доступно только для строки в режиме «по востребованию» незафинализированной ведомости.
    """
    amount = decimal(amount).quantize(_CENTS)
    if amount <= 0:
        raise PayrollConflictError("Сумма должна быть больше нуля")
    run = await session.get(PayrollRun, run_id)
    if run is None:
        raise PayrollNotFoundError("Ведомость не найдена")
    if run.status == "finalized":
        raise PayrollConflictError("Ведомость зафинализирована — изменения невозможны")

    line = await session.scalar(
        select(PayrollLine).where(
            PayrollLine.run_id == run_id, PayrollLine.employee_id == employee_id
        )
    )
    if line is None:
        raise PayrollNotFoundError("Строка сотрудника в ведомости не найдена")
    components = line.components if isinstance(line.components, dict) else {}
    proration = components.get("proration")
    if not isinstance(proration, dict) or not proration.get("on_demand"):
        raise PayrollConflictError(
            "«Включить в выплату» доступно только для режима «по востребованию»"
        )

    payout = EmployeePayout(
        id=uuid.uuid4(),
        employee_id=employee_id,
        kind=EMPLOYEE_PAYOUT_KIND_OWNER_SALARY,
        amount=amount,
        payout_date=datetime.now(UTC).date(),
        run_id=run_id,
        status="included",
        note=(note.strip() if isinstance(note, str) and note.strip() else None),
        created_by_user_id=created_by_user_id,
    )
    session.add(payout)
    await session.flush()

    # total_payable on_demand-строки = Σ включённых в эту ведомость сумм.
    included = await load_included_payouts_by_employee(session, run_id)
    line.total_payable = included.get(employee_id, amount).quantize(_CENTS)
    await session.flush()
    return payout


async def list_on_demand_employees(session: AsyncSession) -> list[Employee]:
    """Активные сотрудники в режиме оклада «по востребованию» (для диалога выплаты).

    Режим задаётся ПО ДОЛЖНОСТИ; берём должности с ``on_demand`` и сотрудников, чья текущая
    должность (``Employee.position``) в их числе.
    """
    modes = await _load_okladnik_payout_modes(session)
    on_demand_positions = [
        position for position, mode in modes.items() if mode == PAYOUT_MODE_ON_DEMAND
    ]
    if not on_demand_positions:
        return []
    rows = await session.scalars(
        select(Employee)
        .where(
            Employee.position.in_(on_demand_positions),
            Employee.status == "active",
        )
        .order_by(Employee.full_name)
    )
    return list(rows.all())


async def _load_admin_employees(
    session: AsyncSession,
    period: PayrollPeriod,
) -> list[Employee]:
    result = await session.scalars(
        select(Employee).where(
            Employee.position.in_(admin_payroll_positions()),
            Employee.admin_payroll_excluded.is_(False),
            or_(Employee.hire_date.is_(None), Employee.hire_date <= period.end_date),
            or_(Employee.fire_date.is_(None), Employee.fire_date >= period.start_date),
        )
    )
    return list(result.all())


async def _load_acting_admin_employees(
    session: AsyncSession,
    period: PayrollPeriod,
    *,
    exclude_ids: set[uuid.UUID],
) -> list[tuple[Employee, str]]:
    """Сотрудники, ИСПОЛНЯЮЩИЕ окладную должность, не являющуюся их основной.

    Признак — активный персональный админ-оклад (``PayrollRate`` с employee_id) на окладную
    должность, которая ОТЛИЧАЕТСЯ от текущей должности сотрудника. Пример: кассир получает
    персональный оклад «Помощник менеджера» → идёт в админ-ведомость по этой должности СВЕРХ
    своей производственной ЗП. Обычный персональный оклад по СВОЕЙ должности (override для
    управляющего-управляющим) сюда не попадает — он покрыт `_load_admin_employees`.

    Возвращает список ``(employee, acting_position)``. ``exclude_ids`` — id основных
    админ-сотрудников (их не дублируем).
    """
    rows = await session.execute(
        select(Employee, PayrollRate.position_group)
        .join(PayrollRate, PayrollRate.employee_id == Employee.id)
        .where(
            PayrollRate.category == ADMIN_OKLAD_CATEGORY,
            PayrollRate.rate_type == "monthly",
            PayrollRate.is_active.is_(True),
            PayrollRate.position_group.in_(okladnik_positions()),
            PayrollRate.effective_from <= period.end_date,
            or_(
                PayrollRate.effective_to.is_(None),
                PayrollRate.effective_to > period.end_date,
            ),
            Employee.admin_payroll_excluded.is_(False),
            or_(Employee.hire_date.is_(None), Employee.hire_date <= period.end_date),
            or_(Employee.fire_date.is_(None), Employee.fire_date >= period.start_date),
        )
    )
    acting: list[tuple[Employee, str]] = []
    seen: set[uuid.UUID] = set()
    for employee, position in rows.all():
        if employee.id in exclude_ids or employee.id in seen:
            continue
        primary = await get_position_on_date(session, employee.id, period.end_date)
        primary = primary or employee.position
        if primary == position:
            # Override по СВОЕЙ должности — это обычный персональный оклад, не «исполняющий».
            continue
        acting.append((employee, position))
        seen.add(employee.id)
    return acting


async def load_admin_oklad(
    session: AsyncSession,
    employee_id: uuid.UUID,
    position: str,
    as_of: date,
) -> Decimal | None:
    """Месячный оклад: переопределение на сотрудника имеет приоритет над дефолтом должности."""
    rates = (
        await session.scalars(
            select(PayrollRate).where(
                PayrollRate.rate_type == "monthly",
                PayrollRate.category == ADMIN_OKLAD_CATEGORY,
                PayrollRate.position_group == position,
                PayrollRate.is_active.is_(True),
                PayrollRate.amount.is_not(None),
                PayrollRate.effective_from <= as_of,
                or_(PayrollRate.effective_to.is_(None), PayrollRate.effective_to > as_of),
                or_(
                    PayrollRate.employee_id == employee_id,
                    PayrollRate.employee_id.is_(None),
                ),
            )
        )
    ).all()
    override = next((rate for rate in rates if rate.employee_id == employee_id), None)
    default = next((rate for rate in rates if rate.employee_id is None), None)
    chosen = override or default
    if chosen is None or chosen.amount is None:
        return None
    return decimal(chosen.amount)


def _sum_adjustments(
    adjustments: Iterable[Any],
) -> tuple[Decimal, Decimal, list[dict[str, Any]], list[dict[str, Any]]]:
    """Суммировать премии→premium и штрафы→deduction (как в производственной свёртке)."""
    premium = Decimal("0")
    penalty = Decimal("0")
    bonus_items: list[dict[str, Any]] = []
    penalty_items: list[dict[str, Any]] = []
    for adjustment in adjustments:
        amount = decimal(getattr(adjustment, "amount", 0))
        item = adjustment_component(adjustment)
        if getattr(adjustment, "type", "") == "bonus":
            premium += amount
            bonus_items.append(item)
        elif getattr(adjustment, "type", "") == "penalty":
            penalty += amount
            penalty_items.append(item)
    return premium, penalty, bonus_items, penalty_items


def _prorated_amount(
    amount: Decimal,
    employee: Employee,
    period: PayrollPeriod,
    *,
    as_of: date | None = None,
) -> tuple[Decimal, dict[str, Any]]:
    """Прорейт суммы по календарным дням полупериода.

    Учитывает найм/увольнение внутри периода. При переданном `as_of` дополнительно
    усекает «отработанный» хвост до этой даты — это «заработано на дату X» для
    расчёта доступного аванса (окладнику капает ровно по календарю).
    """
    total_days = (period.end_date - period.start_date).days + 1
    hire = employee.hire_date
    fire = employee.fire_date
    worked_start = max(period.start_date, hire) if hire is not None else period.start_date
    worked_end = period.end_date
    if fire is not None and fire < worked_end:
        worked_end = fire
    if as_of is not None and as_of < worked_end:
        worked_end = as_of
    if worked_start == period.start_date and worked_end == period.end_date:
        return amount.quantize(_CENTS), {
            "applied": False,
            "total_days": total_days,
            "worked_days": total_days,
        }

    worked_days = max((worked_end - worked_start).days + 1, 0)
    base = (amount * Decimal(worked_days) / Decimal(total_days)).quantize(_CENTS)
    return base, {
        "applied": True,
        "total_days": total_days,
        "worked_days": worked_days,
        "hire_date": hire.isoformat() if hire is not None else None,
        "fire_date": fire.isoformat() if fire is not None else None,
        "as_of": as_of.isoformat() if as_of is not None else None,
    }


def _okladnik_payout_mode(modes: dict[str, str], position: str) -> str:
    mode = modes.get(position)
    return mode if mode in PAYOUT_MODES else PAYOUT_MODE_SPLIT


def _okladnik_base_pay(
    oklad: Decimal,
    mode: str,
    employee: Employee,
    period: PayrollPeriod,
    *,
    as_of: date | None = None,
) -> tuple[Decimal, dict[str, Any]]:
    """База оклада за полупериод с учётом режима выплаты и прорейта.

    `as_of` усекает прорейт до даты (заработано на дату X для аванса); без него —
    полная база полупериода (как в расчёте ведомости).
    """
    is_first_half = period.start_date.day == 1

    if mode == PAYOUT_MODE_ON_DEMAND:
        # По востребованию: ПОЛНЫЙ месячный оклад — это остаток, доступный к выдаче. Показываем
        # его на КАЖДОЙ ведомости месяца (и 1–15, и 16–конец), чтобы текущая ведомость всегда
        # отражала сумму, а в остатке считаем оклад ОДИН раз на месяц (дедуп по period_month).
        # К выплате по ведомости автоматически 0 — сумму включает вручную «Включить в выплату».
        base, proration = _prorated_amount(oklad, employee, period, as_of=as_of)
        proration["payout_mode"] = mode
        proration["is_first_half"] = is_first_half
        proration["on_demand"] = True
        proration["accrual_amount"] = money_string(base)
        proration["period_month"] = f"{period.start_date.year:04d}-{period.start_date.month:02d}"
        return Decimal("0.00"), proration

    if mode == PAYOUT_MODE_FIRST_HALF:
        amount = oklad if is_first_half else Decimal("0")
    elif mode == PAYOUT_MODE_SECOND_HALF:
        amount = oklad if not is_first_half else Decimal("0")
    else:
        # split — ½ оклада за полупериод.
        amount = oklad / 2

    if amount <= 0:
        return Decimal("0.00"), {
            "applied": False,
            "payout_mode": mode,
            "is_first_half": is_first_half,
            "skipped_this_period": True,
        }

    base, proration = _prorated_amount(amount, employee, period, as_of=as_of)
    proration["payout_mode"] = mode
    proration["is_first_half"] = is_first_half
    return base, proration


def okladnik_earned_to_date(
    oklad: Decimal,
    mode: str,
    employee: Employee,
    period: PayrollPeriod,
    as_of: date,
) -> Decimal:
    """Заработанная часть оклада на дату `as_of` внутри полупериода (для аванса).

    Возвращает база-оклада, пропорциональную календарным дням от начала периода до
    `as_of`. Это «в пределах заработанного» — потолок аванса (право A) для окладника.
    """
    base, proration = _okladnik_base_pay(oklad, mode, employee, period, as_of=as_of)
    if proration.get("on_demand"):
        # on_demand: к выплате база = 0, но ЗАРАБОТАНО = прорейт-начисление (в долг).
        # Это потолок «в пределах заработанного» для аванса — иначе он схлопнулся бы в 0.
        raw = proration.get("accrual_amount")
        return decimal(raw) if raw is not None else Decimal("0.00")
    return base


async def _load_okladnik_payout_modes(session: AsyncSession) -> dict[str, str]:
    setting = await session.scalar(
        select(AppSetting).where(AppSetting.key == OKLADNIK_PAYOUT_MODES_KEY)
    )
    value = setting.value if setting is not None else None
    if not isinstance(value, dict):
        return {}
    return {str(key): str(mode) for key, mode in value.items() if str(mode) in PAYOUT_MODES}


async def _load_dishwasher_pool(session: AsyncSession) -> Decimal:
    setting = await session.scalar(
        select(AppSetting).where(AppSetting.key == DISHWASHER_POOL_KEY)
    )
    if setting is None or setting.value is None:
        return DISHWASHER_POOL_DEFAULT
    try:
        pool = decimal(setting.value)
    except (TypeError, ValueError, ArithmeticError):
        return DISHWASHER_POOL_DEFAULT
    return pool if pool > 0 else DISHWASHER_POOL_DEFAULT


def _dishwasher_shift_rate(pool: Decimal, period: PayrollPeriod) -> Decimal:
    """Ставка за смену = месячный пул ÷ календарные дни месяца периода."""
    days_in_month = _last_day_of_month(period.start_date.year, period.start_date.month)
    return (pool / Decimal(days_in_month)).quantize(_CENTS)


async def _load_dishwasher_shift_counts(
    session: AsyncSession,
    employee_ids: Iterable[uuid.UUID],
    period_start: date,
    period_end: date,
) -> dict[uuid.UUID, int]:
    employee_ids = list(employee_ids)
    if not employee_ids:
        return {}
    rows = await session.execute(
        select(DishwasherShift.employee_id, func.count())
        .where(
            DishwasherShift.employee_id.in_(employee_ids),
            DishwasherShift.work_date >= period_start,
            DishwasherShift.work_date <= period_end,
        )
        .group_by(DishwasherShift.employee_id)
    )
    return {employee_id: int(count) for employee_id, count in rows.all()}


# --------------------------------------------------------------------------- #
# Чтение
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Оклады администрации (конфигурация)
# --------------------------------------------------------------------------- #
def _admin_oklad_base_filters(position: str, employee_id: uuid.UUID | None) -> list[Any]:
    emp_filter = (
        PayrollRate.employee_id == employee_id
        if employee_id is not None
        else PayrollRate.employee_id.is_(None)
    )
    return [
        PayrollRate.rate_type == "monthly",
        PayrollRate.category == ADMIN_OKLAD_CATEGORY,
        PayrollRate.position_group == position,
        PayrollRate.station.is_(None),
        emp_filter,
    ]


async def list_admin_oklady(
    session: AsyncSession,
    *,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Текущие оклады: дефолты по должностям + переопределения по сотрудникам."""
    as_of = as_of or datetime.now(UTC).date()
    rows = (
        await session.scalars(
            select(PayrollRate).where(
                PayrollRate.rate_type == "monthly",
                PayrollRate.category == ADMIN_OKLAD_CATEGORY,
                PayrollRate.is_active.is_(True),
                PayrollRate.effective_from <= as_of,
                or_(PayrollRate.effective_to.is_(None), PayrollRate.effective_to > as_of),
            )
        )
    ).all()
    defaults_by_position: dict[str, PayrollRate] = {}
    override_rows: list[PayrollRate] = []
    for rate in rows:
        if rate.employee_id is None:
            defaults_by_position[rate.position_group] = rate
        else:
            override_rows.append(rate)

    names: dict[uuid.UUID, str] = {}
    if override_rows:
        employees = (
            await session.scalars(
                select(Employee).where(
                    Employee.id.in_({rate.employee_id for rate in override_rows})
                )
            )
        ).all()
        names = {employee.id: employee.full_name for employee in employees}

    payout_modes = await _load_okladnik_payout_modes(session)

    return {
        "defaults": [
            {
                "position": position,
                "amount": (
                    money(defaults_by_position[position].amount)
                    if position in defaults_by_position
                    and defaults_by_position[position].amount is not None
                    else None
                ),
                "effective_from": (
                    defaults_by_position[position].effective_from
                    if position in defaults_by_position
                    else None
                ),
                "payout_mode": _okladnik_payout_mode(payout_modes, position),
            }
            for position in okladnik_positions()
        ],
        "overrides": [
            {
                "employee_id": rate.employee_id,
                "employee_name": names.get(rate.employee_id),
                "position": rate.position_group,
                "amount": money(rate.amount),
                "effective_from": rate.effective_from,
            }
            for rate in override_rows
        ],
    }


async def upsert_admin_oklad(
    session: AsyncSession,
    *,
    position: str,
    employee_id: uuid.UUID | None,
    amount: Decimal,
    effective_from: date,
) -> PayrollRate:
    if position not in okladnik_positions():
        raise PayrollConflictError("Должность не является окладной")
    amount_dec = decimal(amount)
    if amount_dec <= 0:
        raise PayrollConflictError("Оклад должен быть больше нуля")

    base_filters = _admin_oklad_base_filters(position, employee_id)
    same = await session.scalar(
        select(PayrollRate).where(*base_filters, PayrollRate.effective_from == effective_from)
    )
    if same is not None:
        same.amount = amount_dec
        same.is_active = True
        await session.commit()
        await session.refresh(same)
        return same

    current_open = await session.scalar(
        select(PayrollRate)
        .where(
            *base_filters,
            PayrollRate.effective_from < effective_from,
            or_(PayrollRate.effective_to.is_(None), PayrollRate.effective_to > effective_from),
        )
        .order_by(desc(PayrollRate.effective_from))
        .limit(1)
    )
    if current_open is not None:
        current_open.effective_to = effective_from

    rate = PayrollRate(
        employee_id=employee_id,
        position_group=position,
        category=ADMIN_OKLAD_CATEGORY,
        station=None,
        rate_type="monthly",
        amount=amount_dec,
        is_active=True,
        effective_from=effective_from,
    )
    session.add(rate)
    await session.commit()
    await session.refresh(rate)
    return rate


async def set_admin_payroll_exclusion(
    session: AsyncSession,
    employee_id: uuid.UUID,
    excluded: bool,
) -> Employee:
    """Включить/снять исключение сотрудника из ведомости администрации."""
    employee = await session.get(Employee, employee_id)
    if employee is None:
        raise PayrollNotFoundError("Сотрудник не найден")
    employee.admin_payroll_excluded = bool(excluded)
    await session.commit()
    await session.refresh(employee)
    return employee


async def clear_admin_oklad_override(
    session: AsyncSession,
    employee_id: uuid.UUID,
    *,
    as_of: date | None = None,
) -> None:
    """Снять переопределение оклада сотрудника — он вернётся к дефолту должности."""
    as_of = as_of or datetime.now(UTC).date()
    rows = (
        await session.scalars(
            select(PayrollRate).where(
                PayrollRate.rate_type == "monthly",
                PayrollRate.category == ADMIN_OKLAD_CATEGORY,
                PayrollRate.employee_id == employee_id,
                PayrollRate.is_active.is_(True),
                or_(PayrollRate.effective_to.is_(None), PayrollRate.effective_to > as_of),
            )
        )
    ).all()
    for rate in rows:
        rate.is_active = False
    await session.commit()


async def _upsert_setting(
    session: AsyncSession,
    *,
    key: str,
    value: Any,
    value_type: str,
    display_name: str,
    unit: str | None = None,
) -> None:
    setting = await session.scalar(select(AppSetting).where(AppSetting.key == key))
    if setting is None:
        session.add(
            AppSetting(
                key=key,
                value=value,
                value_type=value_type,
                category="payroll",
                display_name=display_name,
                description=None,
                widget_type="json",
                widget_options=None,
                unit=unit,
            )
        )
    else:
        setting.value = value
        setting.updated_at = datetime.now(UTC)
    await session.commit()


async def set_okladnik_payout_mode(
    session: AsyncSession,
    position: str,
    mode: str,
) -> None:
    """Режим выплаты оклада за полумесяц для должности (split — дефолт, не хранится)."""
    if position not in okladnik_positions():
        raise PayrollConflictError("Должность не является окладной")
    if mode not in PAYOUT_MODES:
        raise PayrollConflictError("Неизвестный режим выплаты")
    setting = await session.scalar(
        select(AppSetting).where(AppSetting.key == OKLADNIK_PAYOUT_MODES_KEY)
    )
    value = dict(setting.value) if setting is not None and isinstance(setting.value, dict) else {}
    if mode == PAYOUT_MODE_SPLIT:
        value.pop(position, None)
    else:
        value[position] = mode
    await _upsert_setting(
        session,
        key=OKLADNIK_PAYOUT_MODES_KEY,
        value=value,
        value_type="object",
        display_name="Режим выплаты окладов по должностям",
    )


async def get_dishwasher_pool(session: AsyncSession) -> Decimal:
    return await _load_dishwasher_pool(session)


async def set_dishwasher_pool(session: AsyncSession, pool: Decimal) -> Decimal:
    pool_dec = decimal(pool)
    if pool_dec <= 0:
        raise PayrollConflictError("Пул должен быть больше нуля")
    await _upsert_setting(
        session,
        key=DISHWASHER_POOL_KEY,
        value=float(pool_dec),
        value_type="decimal",
        display_name="Пул мойщиц, ₽/мес",
        unit="₽",
    )
    return pool_dec


# --------------------------------------------------------------------------- #
# Смены мойщиц (ручной ввод управляющим)
# --------------------------------------------------------------------------- #
async def list_dishwasher_employees(session: AsyncSession) -> list[Employee]:
    result = await session.scalars(
        select(Employee)
        .where(
            Employee.position.in_(dishwasher_positions()),
            Employee.admin_payroll_excluded.is_(False),
        )
        .order_by(Employee.full_name)
    )
    return list(result.all())


async def list_dishwasher_shifts(
    session: AsyncSession,
    *,
    period_start: date,
    period_end: date,
) -> list[DishwasherShift]:
    result = await session.scalars(
        select(DishwasherShift)
        .where(
            DishwasherShift.work_date >= period_start,
            DishwasherShift.work_date <= period_end,
        )
        .order_by(DishwasherShift.work_date)
    )
    return list(result.all())


async def _is_admin_date_locked(session: AsyncSession, work_date: date) -> bool:
    """Дата закрыта только если её покрывает финализированная ПОЛУМЕСЯЧНАЯ (админская)
    ведомость. Финализация недельной производственной ведомости смены мойщиц не блокирует."""
    locked = await session.scalar(
        select(PayrollPeriod.id)
        .select_from(PayrollRun)
        .join(PayrollPeriod, PayrollPeriod.id == PayrollRun.period_id)
        .where(
            PayrollRun.status == "finalized",
            PayrollPeriod.period_type == "half_month",
            PayrollPeriod.start_date <= work_date,
            PayrollPeriod.end_date >= work_date,
        )
        .limit(1)
    )
    return locked is not None


async def set_dishwasher_shift(
    session: AsyncSession,
    *,
    employee_id: uuid.UUID,
    work_date: date,
    worked: bool,
    actor_user_id: uuid.UUID | None = None,
) -> bool:
    """Проставить/снять смену мойщицы за день. Возвращает итоговое состояние."""
    if await _is_admin_date_locked(session, work_date):
        raise PayrollConflictError("Период зафиксирован, изменения невозможны")
    existing = await session.scalar(
        select(DishwasherShift).where(
            DishwasherShift.employee_id == employee_id,
            DishwasherShift.work_date == work_date,
        )
    )
    if worked and existing is None:
        session.add(
            DishwasherShift(
                employee_id=employee_id,
                work_date=work_date,
                created_by_user_id=actor_user_id,
            )
        )
    elif not worked and existing is not None:
        await session.delete(existing)
    await session.commit()
    return worked


async def list_admin_runs(session: AsyncSession) -> list[tuple[PayrollRun, PayrollPeriod]]:
    result = await session.execute(
        select(PayrollRun, PayrollPeriod)
        .join(PayrollPeriod, PayrollRun.period_id == PayrollPeriod.id)
        .where(PayrollPeriod.period_type == "half_month")
        .order_by(desc(PayrollRun.started_at))
    )
    return list(result.all())


def admin_positions() -> Iterable[str]:
    return admin_payroll_positions()

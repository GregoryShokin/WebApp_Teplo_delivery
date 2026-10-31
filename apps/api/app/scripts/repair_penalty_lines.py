"""Починка закрытых недель: штрафы без строки и излишки штрафа списываются.

Решение владельца 31.10.2026: прежние неудержанные долги списываются, сотрудникам их не
объяснить. Суммы к выплате и выплаченное не меняются, добавляются только нулевые строки и
отметки «списано». ОПиУ это не трогает: штрафы стоят там датой штрафа, а не строкой ведомости.

Что делает скрипт, всё в одной транзакции:

* штраф, на который не ссылается ни одна строка ведомости (``components.adjustments.penalties
  [].id``), лежит в закрытой недельной ведомости. По человеку и неделе заводится строка:
  к выплате 0, ``deduction`` и ``penalty_debt_written_off`` равны сумме штрафов;
* к новой строке — нулевая отметка выплаты, иначе ведомость числилась бы недоплаченной;
* если у человека в этой неделе строка уже есть, ничего не пишется, случай печатается: такой
  штраф не попал в расчёт по другой причине и требует глаз;
* строка закрытой неимпортированной недели, где начисленное минус НДФЛ минус ``deduction``
  меньше нуля при нуле к выплате, получает отметку «списано» на этот излишек;
* в журнал ведомости — событие ``penalty_line_repair`` с причиной, в ``run.summary`` —
  обновлённая сумма удержаний. ``total_payable`` не меняется.

Запуск (после ``alembic upgrade head``):

    docker exec -w /app/apps/api teplo-api python -m app.scripts.repair_penalty_lines
    # запись:
    docker exec -w /app/apps/api teplo-api python -m app.scripts.repair_penalty_lines --apply

Без ``--apply`` транзакция откатывается. Повторный запуск ничего не меняет: штраф уже имеет
строку, а строка с отметкой уже не считается излишком.
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.session import AsyncSessionLocal
from app.models import (
    Employee,
    PayrollAdjustment,
    PayrollLine,
    PayrollPayment,
    PayrollPeriod,
    PayrollRun,
    PayrollRunEvent,
)
from app.services.employee_assignments import get_assignments
from app.services.payroll_calculator import (
    EMPLOYEE_ASSIGNMENTS_CONFIG_KEY,
    adjustment_component,
    decimal,
    money,
    money_string,
    vacation_role_for_employee_day,
)

REPAIR_ACTION = "penalty_line_repair"
REPAIR_REASON = "решение владельца 31.10.2026: долг списан, сотрудникам его не объяснить"
WRITE_OFF_NOTE = "Списано: решение владельца 31.10.2026"
LEGACY_IMPORT_LABEL = "import:legacy"


def repair_mark(amount: Decimal) -> dict[str, str]:
    """След скрипта на строке: сумма здесь держит решение владельца при любом пересчёте недели."""
    return {
        "action": REPAIR_ACTION,
        "reason": REPAIR_REASON,
        "amount": money_string(amount),
    }


@dataclass(slots=True)
class ClosedWeek:
    """Закрытая недельная ведомость: последний финализированный прогон периода."""

    run: PayrollRun
    period: PayrollPeriod


@dataclass(slots=True)
class RunChanges:
    """Что записано в одну ведомость: из этого собирается событие журнала."""

    new_lines: list[dict[str, Any]] = field(default_factory=list)
    write_offs: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class RepairReport:
    """Итог прогона для печати."""

    lines_created: int = 0
    penalties_covered: int = 0
    penalties_amount: Decimal = Decimal("0")
    write_offs_marked: int = 0
    write_offs_amount: Decimal = Decimal("0")
    needs_review: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Списать прежние неудержанные штрафы в закрытых недельных ведомостях."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="записать изменения; без ключа транзакция откатывается",
    )
    return parser.parse_args()


async def load_closed_weeks(session: AsyncSession) -> list[ClosedWeek]:
    """Закрытые недели, новые прогоны первыми, по одному прогону на период.

    Старый прогон той же недели может оставаться в базе, поэтому берём последний. Импортированные
    ведомости остаются: для штрафов их отсекает метка записи, для излишков — отдельное условие.
    """
    rows = (
        await session.execute(
            select(PayrollRun, PayrollPeriod)
            .join(PayrollPeriod, PayrollPeriod.id == PayrollRun.period_id)
            .where(PayrollRun.status == "finalized", PayrollPeriod.period_type == "week")
            .order_by(PayrollPeriod.start_date, PayrollRun.started_at.desc())
        )
    ).all()
    weeks: dict[uuid.UUID, ClosedWeek] = {}
    for run, period in rows:
        weeks.setdefault(period.id, ClosedWeek(run=run, period=period))
    return list(weeks.values())


async def load_referenced_penalty_ids(session: AsyncSession) -> set[str]:
    """Штрафы, на которые ссылается хоть одна строка любой ведомости."""
    values = await session.scalars(select(PayrollLine.components["adjustments"]["penalties"]))
    referenced: set[str] = set()
    for penalties in values.all():
        if not isinstance(penalties, list):
            continue
        referenced.update(str(item.get("id")) for item in penalties if isinstance(item, dict))
    return referenced


async def load_unlinked_penalties(
    session: AsyncSession, weeks: list[ClosedWeek]
) -> dict[tuple[uuid.UUID, uuid.UUID], list[PayrollAdjustment]]:
    """Штрафы без строки, по (сотрудник, период закрытой недели), неделя — по дате штрафа."""
    referenced = await load_referenced_penalty_ids(session)
    penalties = (
        await session.scalars(
            select(PayrollAdjustment)
            .options(selectinload(PayrollAdjustment.category))
            .where(
                PayrollAdjustment.type == "penalty",
                # Не ``!=``: в SQL сравнение с NULL отбросило бы записи без метки.
                PayrollAdjustment.created_by_label.is_distinct_from(LEGACY_IMPORT_LABEL),
            )
            .order_by(PayrollAdjustment.work_date, PayrollAdjustment.created_at)
        )
    ).all()
    groups: dict[tuple[uuid.UUID, uuid.UUID], list[PayrollAdjustment]] = defaultdict(list)
    for penalty in penalties:
        if str(penalty.id) in referenced:
            continue
        week = next(
            (
                item
                for item in weeks
                if item.period.start_date <= penalty.work_date <= item.period.end_date
            ),
            None,
        )
        # Открытая неделя сюда не попадает: её штраф подхватит обычный расчёт.
        if week is not None:
            groups[(penalty.employee_id, week.period.id)].append(penalty)
    return dict(groups)


async def line_role_on_date(session: AsyncSession, employee: Employee, on_date: date) -> str:
    """Роль строки без смен: тем же правилом, что в расчёте недели."""
    assignments = await get_assignments(session, employee.id, on_date)
    settings = {EMPLOYEE_ASSIGNMENTS_CONFIG_KEY: {(employee.id, on_date): assignments}}
    return vacation_role_for_employee_day(settings, employee, on_date)


async def add_write_off_line(
    session: AsyncSession,
    week: ClosedWeek,
    employee: Employee,
    penalties: list[PayrollAdjustment],
    changes: RunChanges,
    report: RepairReport,
) -> None:
    """Завести строку недели с нулём к выплате, все штрафы которой списаны."""
    amount = sum((decimal(penalty.amount) for penalty in penalties), Decimal("0"))
    role = await line_role_on_date(session, employee, penalties[0].work_date)
    line = PayrollLine(
        id=uuid.uuid4(),
        run_id=week.run.id,
        employee_id=employee.id,
        role=role,
        deduction=amount,
        total_payable=Decimal("0"),
        penalty_debt_written_off=amount,
        components={
            "days": [],
            "deposit_withholding": 0,
            "deposit_payout": 0,
            "ndfl_withheld": 0,
            "adjustments": {
                "bonuses": [],
                "penalties": [adjustment_component(penalty) for penalty in penalties],
                "primary_role_chosen": role,
            },
            "penalty_debt_note": WRITE_OFF_NOTE,
            "repair": repair_mark(amount),
        },
    )
    session.add(line)
    # Форма та же, что у нулевых строк, отмеченных кнопкой: без способа и без денег.
    session.add(
        PayrollPayment(
            id=uuid.uuid4(),
            run_id=week.run.id,
            employee_id=employee.id,
            amount=Decimal("0"),
            amount_cash=Decimal("0"),
            amount_account=Decimal("0"),
            status="paid",
            paid_at=week.period.payroll_date,
            method=None,
            paid_by_user_id=None,
        )
    )
    changes.new_lines.append(
        {
            "line_id": str(line.id),
            "employee_id": str(employee.id),
            "role": role,
            "amount": money_string(amount),
            "penalty_ids": [str(penalty.id) for penalty in penalties],
        }
    )
    report.lines_created += 1
    report.penalties_covered += len(penalties)
    report.penalties_amount += amount
    print(
        f"строка: {week.period.start_date}–{week.period.end_date} {employee.full_name}, "
        f"роль {role}, штрафов {len(penalties)}, списано {money_string(amount)}"
    )


async def repair_unlinked_penalties(
    session: AsyncSession,
    weeks: list[ClosedWeek],
    changes_by_run: dict[uuid.UUID, RunChanges],
    report: RepairReport,
) -> None:
    """Штрафы без строки: новая строка, а если строка у человека есть — на глаза."""
    groups = await load_unlinked_penalties(session, weeks)
    if not groups:
        return
    weeks_by_period = {week.period.id: week for week in weeks}
    run_ids = [weeks_by_period[period_id].run.id for _employee_id, period_id in groups]
    line_keys = await session.execute(
        select(PayrollLine.run_id, PayrollLine.employee_id)
        .where(PayrollLine.run_id.in_(run_ids))
        .distinct()
    )
    with_lines = {(run_id, employee_id) for run_id, employee_id in line_keys.all()}
    for (employee_id, period_id), penalties in sorted(
        groups.items(), key=lambda item: item[1][0].work_date
    ):
        week = weeks_by_period[period_id]
        employee = await session.get(Employee, employee_id)
        if employee is None:
            continue
        if (week.run.id, employee_id) in with_lines:
            report.needs_review += len(penalties)
            amount = sum((decimal(penalty.amount) for penalty in penalties), Decimal("0"))
            print(
                f"ТРЕБУЕТ ГЛАЗ: {week.period.start_date}–{week.period.end_date} "
                f"{employee.full_name}: строка есть, штрафов без ссылки {len(penalties)} "
                f"на {money_string(amount)}; ничего не записано"
            )
            continue
        changes = changes_by_run.setdefault(week.run.id, RunChanges())
        await add_write_off_line(session, week, employee, penalties, changes, report)


async def mark_excess_lines(
    session: AsyncSession,
    weeks: list[ClosedWeek],
    changes_by_run: dict[uuid.UUID, RunChanges],
    report: RepairReport,
) -> None:
    """Строки с излишком штрафа: разница, потерянная на обрезке до нуля, становится «списано»."""
    weeks_by_run = {week.run.id: week for week in weeks if not week.run.is_imported_legacy}
    if not weeks_by_run:
        return
    net = (
        PayrollLine.base_pay
        + PayrollLine.premium
        + PayrollLine.percent_pay
        + PayrollLine.vacation_pay
        - PayrollLine.ndfl_withheld
        - PayrollLine.deduction
    )
    lines = (
        await session.scalars(
            select(PayrollLine).where(
                PayrollLine.run_id.in_(weeks_by_run),
                PayrollLine.total_payable == 0,
                PayrollLine.penalty_debt_written_off == 0,
                # Долг, ушедший дальше, — уже не потерянный излишек: повторный запуск после
                # новых закрытых недель не должен его списать.
                PayrollLine.penalty_debt_out == 0,
                net < 0,
            )
        )
    ).all()
    for line in lines:
        # Новый расчёт снимает излишок с других строк человека и оставляет на них отметку:
        # такой минус покрыт, а старый терялся, даже если на второй строке были деньги.
        covered = await session.scalar(
            select(PayrollLine.id)
            .where(
                PayrollLine.run_id == line.run_id,
                PayrollLine.employee_id == line.employee_id,
                PayrollLine.components.has_key("penalty_debt_taken"),
            )
            .limit(1)
        )
        if covered is not None:
            continue
        week = weeks_by_run[line.run_id]
        employee = await session.get(Employee, line.employee_id)
        amount = -(
            decimal(line.base_pay)
            + decimal(line.premium)
            + decimal(line.percent_pay)
            + decimal(line.vacation_pay)
            - decimal(line.ndfl_withheld)
            - decimal(line.deduction)
        )
        line.penalty_debt_written_off = amount
        line.components = {
            **(line.components or {}),
            "penalty_debt_note": WRITE_OFF_NOTE,
            "repair": repair_mark(amount),
        }
        changes_by_run.setdefault(week.run.id, RunChanges()).write_offs.append(
            {
                "line_id": str(line.id),
                "employee_id": str(line.employee_id),
                "amount": money_string(amount),
            }
        )
        report.write_offs_marked += 1
        report.write_offs_amount += amount
        print(
            f"отметка: {week.period.start_date}–{week.period.end_date} "
            f"{employee.full_name if employee else line.employee_id}, "
            f"списано {money_string(amount)}"
        )


def write_run_trail(session: AsyncSession, week: ClosedWeek, changes: RunChanges) -> None:
    """След в ведомости: итоги удержаний и событие журнала."""
    summary = dict(week.run.summary or {})
    added = sum((decimal(item["amount"]) for item in changes.new_lines), Decimal("0"))
    if changes.new_lines and "deduction" in summary:
        summary["deduction"] = money(decimal(summary["deduction"]) + added)
    if changes.new_lines and "line_count" in summary:
        summary["line_count"] = int(summary["line_count"]) + len(changes.new_lines)
    week.run.summary = summary
    session.add(
        PayrollRunEvent(
            run_id=week.run.id,
            period_id=week.period.id,
            action=REPAIR_ACTION,
            reason=REPAIR_REASON,
            payload={"new_lines": changes.new_lines, "write_offs": changes.write_offs},
        )
    )


async def repair(session: AsyncSession) -> RepairReport:
    """Собрать всё в открытой транзакции; фиксировать или откатывать будет вызывающий."""
    report = RepairReport()
    weeks = await load_closed_weeks(session)
    changes_by_run: dict[uuid.UUID, RunChanges] = {}
    await mark_excess_lines(session, weeks, changes_by_run, report)
    await repair_unlinked_penalties(session, weeks, changes_by_run, report)
    for week in weeks:
        changes = changes_by_run.get(week.run.id)
        if changes is not None:
            write_run_trail(session, week, changes)
    await session.flush()
    return report


async def async_main() -> None:
    args = parse_args()
    async with AsyncSessionLocal() as session:
        report = await repair(session)
        if args.apply:
            await session.commit()
        else:
            await session.rollback()
    outcome = "записано" if args.apply else "откат, ничего не записано (запись: --apply)"
    print(
        f"Итог: строк {report.lines_created}, штрафов {report.penalties_covered} "
        f"на {money_string(report.penalties_amount)}; "
        f"отметок {report.write_offs_marked} на {money_string(report.write_offs_amount)}; "
        f"требуют глаз {report.needs_review}. {outcome}"
    )


if __name__ == "__main__":
    asyncio.run(async_main())

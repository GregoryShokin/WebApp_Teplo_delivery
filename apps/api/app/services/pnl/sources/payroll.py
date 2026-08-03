"""Зарплатный контур как источник ОПиУ: начисления по календарным месяцам.

ГЛАВНАЯ СЛОЖНОСТЬ — ПЕРИОДЫ НЕ СОВПАДАЮТ С МЕСЯЦЕМ. Производственная ведомость считается
неделями вторник-понедельник, и неделя 29.07-04.08 принадлежит двум месяцам сразу. Взять
период целиком нельзя: июль получил бы четыре августовских дня, а август недосчитался.
Поэтому суммы собираются из ПОДНЕВНОЙ детализации ``components['days']``, где у каждого дня
своя дата и своя роль. Административная ведомость раскладки не требует: её полумесячные
периоды (1-15 и 16-конец) целиком лежат внутри месяца.

ЗАРПЛАТА БЕРЁТСЯ БЕЗ НДФЛ — решение владельца 03.08.2026, и оно парное: НДФЛ учитывается
строкой «Налоги с ЗП» из оборотки бухгалтера. На данных это ничего не вычитает: в ведомости
``ndfl_withheld`` равен нулю у всех действующих ролей — удержания там просто нет, начисления
и так «на руки». Складывая их со строкой налогов, получаем полный ФОТ ровно один раз.

ШТРАФЫ ВЫЧИТАЮТСЯ, КРОМЕ РЕВИЗИОННЫХ — тоже решение владельца. Ревизионные идут отдельным
отрицательным компонентом строки «Результаты ревизии»: там они компенсируют потери по
ревизии. Вычесть их ещё и здесь значило бы уменьшить расход дважды. Классификатор уже
написан и используется персональным отчётом — берём его, а не заводим свой.

ПРЕМИИ БЕРУТСЯ ИЗ КОРРЕКТИРОВОК, А НЕ ИЗ СТРОКИ ВЕДОМОСТИ. В строке премия лежит итогом за
период, и разложить её по месяцам было бы гаданием. У корректировки есть ``work_date`` —
точная дата, по которой месяц определяется однозначно.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.payroll import PayrollAdjustment, PayrollLine, PayrollPeriod, PayrollRun
from app.services.payroll_personal_report import is_audit_penalty, money_decimal
from app.services.staff_taxonomy import COOK_PAYROLL_ROLES

#: Роль кассира в ведомости. В эталоне владельца это строка «Зарплата администраторов»
#: блока общепроизводственных — отдельно от администрации.
CASHIER_ROLE = "administrator"

#: Прогоны, чьи начисления считаются фактом. Черновик — ещё не расход: его пересчитают.
COUNTED_RUN_STATUSES = ("finalized",)


@dataclass(slots=True)
class PayrollMonth:
    """Начисления месяца, разложенные по строкам ОПиУ."""

    cook_pay: Decimal = Decimal("0.00")
    cashier_pay: Decimal = Decimal("0.00")
    admin_pay: Decimal = Decimal("0.00")
    bonuses: Decimal = Decimal("0.00")
    fund_accrual: Decimal = Decimal("0.00")
    #: Штрафы по ревизиям — не вычитаются из зарплаты, а уходят в строку «Результаты ревизии».
    audit_penalties: Decimal = Decimal("0.00")
    #: Прочие штрафы — уменьшают зарплатный расход того месяца, к которому относятся.
    other_penalties: Decimal = Decimal("0.00")
    #: По ролям — для будущего разреза по направлениям (Роллы / Пицца / Горячий цех).
    by_role: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    #: Для расшифровки строки: (код строки ОПиУ, сотрудник) → начислено за месяц. Копится в
    #: том же проходе, что и итоги, — отдельный проход по ведомостям повторил бы разбор
    #: подневной детализации и правило «по востребованию», а расшифровка обязана объяснять
    #: ровно ту сумму, которая стоит в строке.
    by_employee: dict[tuple[str, uuid.UUID], Decimal] = field(
        default_factory=lambda: defaultdict(Decimal)
    )
    has_data: bool = False


def _day_amount(day: dict) -> Decimal:
    """Начисление одного дня: смена, процент с выручки и отпускные этого дня."""
    return (
        money_decimal(day.get("base_pay"))
        + money_decimal(day.get("percent_pay"))
        + money_decimal(day.get("vacation_pay"))
    )


async def build_payroll_month(
    session: AsyncSession, month_start: date, month_end: date
) -> PayrollMonth:
    """Собрать зарплатные начисления, относящиеся к календарному месяцу."""
    result = PayrollMonth()

    # Берём прогоны, чьи периоды ПЕРЕСЕКАЮТСЯ с месяцем, и раскладываем их дни. Периоды,
    # начавшиеся в прошлом месяце и закончившиеся в этом, отдадут только свои дни внутри
    # границ — остальное останется прошлому месяцу.
    rows = (
        await session.execute(
            select(PayrollLine, PayrollPeriod)
            .join(PayrollRun, PayrollRun.id == PayrollLine.run_id)
            .join(PayrollPeriod, PayrollPeriod.id == PayrollRun.period_id)
            .where(
                PayrollRun.status.in_(COUNTED_RUN_STATUSES),
                PayrollPeriod.start_date <= month_end,
                PayrollPeriod.end_date >= month_start,
            )
        )
    ).all()

    # Оклад «по востребованию» (собственник) начисляется ИНАЧЕ: в ``base_pay`` у такой строки
    # ноль, а месячный оклад лежит в ``components['proration']['accrual_amount']``. Причина —
    # этот оклад не привязан к периоду ведомости: он копится долгом с 1-го числа и гасится
    # произвольными суммами по кнопке. ОБЕ полумесячные строки несут ПОЛНЫЙ месячный оклад,
    # поэтому берём его один раз на пару «сотрудник + месяц»: сложив две строки, мы удвоили бы
    # зарплату собственника (за июль 2026 это 280 000 ₽ вместо 140 000 ₽).
    on_demand: dict[tuple[uuid.UUID, str], Decimal] = {}
    # Сотрудники, у которых за этот месяц есть оклад «по востребованию». Их обычные строки
    # того же месяца учитывать НЕЛЬЗЯ: ``accrual_amount`` — оклад за ВЕСЬ месяц, а не за
    # остаток. В июне 2026 собственника перевели на этот режим с середины месяца, и первая
    # половина осталась обычной строкой на 70 000 ₽ — сложив её с окладом 140 000 ₽,
    # получили бы 210 000 ₽ при окладе 140 000 ₽.
    on_demand_employees: set[uuid.UUID] = set()

    for line, _period in rows:
        proration = line.components.get("proration") if isinstance(line.components, dict) else None
        if isinstance(proration, dict) and proration.get("on_demand"):
            period_month = str(proration.get("period_month") or "")
            if period_month.startswith(f"{month_start.year:04d}-{month_start.month:02d}"):
                on_demand_employees.add(line.employee_id)

    for line, period in rows:
        if line.employee_id in on_demand_employees and not (
            isinstance(line.components, dict)
            and isinstance(line.components.get("proration"), dict)
            and line.components["proration"].get("on_demand")
        ):
            continue
        proration = line.components.get("proration") if isinstance(line.components, dict) else None
        if isinstance(proration, dict) and proration.get("on_demand"):
            raw_amount = proration.get("accrual_amount")
            period_month = str(proration.get("period_month") or "")
            if raw_amount is not None and period_month.startswith(
                f"{month_start.year:04d}-{month_start.month:02d}"
            ):
                on_demand[(line.employee_id, period_month)] = money_decimal(raw_amount)
            continue

        days = line.components.get("days") if isinstance(line.components, dict) else None
        if isinstance(days, list) and days:
            for day in days:
                if not isinstance(day, dict):
                    continue
                raw_date = day.get("date")
                if not raw_date:
                    continue
                try:
                    work_date = date.fromisoformat(str(raw_date)[:10])
                except ValueError:
                    continue
                if work_date < month_start or work_date > month_end:
                    continue
                amount = _day_amount(day)
                if amount == 0:
                    continue
                result.has_data = True
                # Роль дня точнее роли строки: повар, вышедший на пиццу вместо роллов,
                # отдаёт этот день своему цеху. Строка знает только первичную роль.
                role = str(day.get("role") or line.role)
                result.by_role[role] += amount
                if role in COOK_PAYROLL_ROLES:
                    result.cook_pay += amount
                    result.by_employee[("cook_payroll", line.employee_id)] += amount
                elif role == CASHIER_ROLE:
                    result.cashier_pay += amount
                    result.by_employee[("administrator_payroll", line.employee_id)] += amount
                else:
                    result.admin_pay += amount
                    result.by_employee[("admin_payroll", line.employee_id)] += amount
                fund = money_decimal(day.get("fund_accrual"))
                result.fund_accrual += fund
                if fund:
                    result.by_employee[("accumulation_fund", line.employee_id)] += fund
        else:
            # Подневной детализации нет — это административная ведомость. Её полумесячный
            # период целиком внутри месяца, поэтому строка относится к нему как есть.
            if period.start_date < month_start or period.end_date > month_end:
                continue
            amount = (
                money_decimal(line.base_pay)
                + money_decimal(line.percent_pay)
                + money_decimal(line.vacation_pay)
            )
            if amount == 0:
                continue
            result.has_data = True
            result.by_role[str(line.role)] += amount
            if line.role in COOK_PAYROLL_ROLES:
                result.cook_pay += amount
                result.by_employee[("cook_payroll", line.employee_id)] += amount
            elif line.role == CASHIER_ROLE:
                result.cashier_pay += amount
                result.by_employee[("administrator_payroll", line.employee_id)] += amount
            else:
                result.admin_pay += amount
                result.by_employee[("admin_payroll", line.employee_id)] += amount
            fund = money_decimal(line.fund_accrual)
            result.fund_accrual += fund
            if fund:
                result.by_employee[("accumulation_fund", line.employee_id)] += fund

    for (employee_id, _period_month), amount in on_demand.items():
        if amount == 0:
            continue
        result.has_data = True
        result.admin_pay += amount
        result.by_role["Оклад по востребованию"] += amount
        result.by_employee[("admin_payroll", employee_id)] += amount

    await _apply_adjustments(session, result, month_start, month_end)
    return result


async def _apply_adjustments(
    session: AsyncSession, result: PayrollMonth, month_start: date, month_end: date
) -> None:
    """Премии и штрафы — по дате самой корректировки, а не по периоду ведомости."""
    adjustments = (
        (
            await session.execute(
                select(PayrollAdjustment)
                .where(
                    PayrollAdjustment.work_date >= month_start,
                    PayrollAdjustment.work_date <= month_end,
                )
                .options(selectinload(PayrollAdjustment.category))
            )
        )
        .scalars()
        .all()
    )
    for adjustment in adjustments:
        amount = money_decimal(adjustment.amount)
        if amount == 0:
            continue
        result.has_data = True
        if adjustment.type == "bonus":
            result.bonuses += amount
            result.by_employee[("bonuses", adjustment.employee_id)] += amount
        elif is_audit_penalty(adjustment):
            result.audit_penalties += amount
            result.by_employee[("audit_results", adjustment.employee_id)] -= amount
        else:
            result.other_penalties += amount
            # Прочий штраф уменьшает зарплату поваров — в расшифровке он и должен стоять
            # там же, отрицательной строкой, а не прятаться внутри итога.
            result.by_employee[("cook_payroll", adjustment.employee_id)] -= amount


def cook_payroll(month: PayrollMonth) -> Decimal:
    """Зарплата поваров: начисления минус прочие штрафы (ревизионные вычитаются в своей строке)."""
    return month.cook_pay - month.other_penalties


def employee_ids_with_role(lines: list[PayrollLine], role: str) -> set[uuid.UUID]:
    """Сотрудники, работавшие в роли, — для будущего блока «Показатели для статистики»."""
    return {line.employee_id for line in lines if line.role == role}

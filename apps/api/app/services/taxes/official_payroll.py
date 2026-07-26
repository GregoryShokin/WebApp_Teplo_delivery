"""Прогнозные начисления по официальным сотрудникам — до прихода оборотки бухгалтера.

Решение владельца 26.07.2026: задолженность перед бюджетом должна появляться, как только
сотрудник ОТРАБОТАЛ месяц, а не когда пришла оборотка. Официальный контур сотрудника
(``Employee.is_official`` + официальный оклад/вычет) позволяет посчитать стандартный
месяц заранее:

* НДФЛ = (оклад − вычет) × 13 %, в полных рублях;
* взносы МСП = 30 % в пределах 1,5 МРОТ + 15 % свыше (формула сверена с обороткой
  копейка в копейку: июль 2026, оклад 50 000 → 13 595,93);
* травматизм = оклад × 0,2 % — отдельной платёжкой в СФР, не через ЕНС.

Прогноз — ВИРТУАЛЬНЫЙ слой: в ``tax_payment`` ничего не материализуется (иначе задвоится
вычет УСН, кошелёк ЕНС и документный слой обязательств). Строится только для ПОЛНЫХ
отработанных месяцев БЕЗ оборотки (``TaxPayrollLedger`` пуст за месяц): пришла оборотка —
прогноз замещается её начислениями в сверке автоматически. Неполные месяцы (приём или
увольнение в середине) прогноз пропускает: бухгалтер считает их по производственному
календарю, гадать не наше дело — доберёт оборотка.

Нестандарт (отпускные, больничные, компенсации) прогноз не учитывает намеренно — это
ОЖИДАНИЕ стандартного месяца; расхождение с обороткой станет сигналом нестандарта.

Здесь же — годовое обязательство фиксированных взносов ИП: сумма года известна заранее
(ст. 430 НК), остаток = год − уплачено − уже показанные плановые платёжки.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.employee import Employee
from app.models.tax import TaxPayment, TaxPayrollLedger
from app.services.taxes.engine import YearConfig, money, rub

ZERO = Decimal("0")


@dataclass(frozen=True)
class OfficialMonthAccrual:
    """Прогнозное начисление за полный отработанный месяц по всем официальным сотрудникам."""

    year: int
    month: int
    salary_base: Decimal  # суммарный официальный оклад месяца
    ndfl: Decimal
    contributions: Decimal
    injury: Decimal
    employees: int

    @property
    def enp_total(self) -> Decimal:
        """Зарплатный ЕНП месяца = НДФЛ + взносы (травматизм идёт в СФР отдельно)."""
        return self.ndfl + self.contributions


def month_contributions(base: Decimal, cfg: YearConfig) -> Decimal:
    """Взносы МСП за месяц: полный тариф в пределах порога 1,5 МРОТ, льготный — свыше."""
    threshold = money(cfg.mrot * cfg.msp_threshold_mrot)
    below = min(base, threshold)
    above = max(ZERO, base - threshold)
    return money(below * cfg.msp_full_rate + above * cfg.msp_reduced_rate)


def month_ndfl(base: Decimal, deduction: Decimal, cfg: YearConfig) -> Decimal:
    """НДФЛ за месяц: (база − вычет) × ставка, в полных рублях (п. 6 ст. 52 НК)."""
    taxable = max(ZERO, base - deduction)
    return rub(taxable * cfg.ndfl_rate)


def child_deduction_monthly(children: int, single_parent: bool, cfg: YearConfig) -> Decimal:
    """Детский вычет НДФЛ в месяц из вопросов «сколько детей» и «единственный родитель».

    Ст. 218 НК (размеры с 2025): 1-й ребёнок 1 400, 2-й 2 800, 3-й и каждый следующий
    6 000 — вычеты СУММИРУЮТСЯ (двое детей → 4 200, трое → 10 200). Единственному
    родителю — в двойном размере. Ручной ввод суммы убран решением владельца 26.07.2026:
    вопросы конкретные, ошибиться сложнее.
    """
    total = ZERO
    if children >= 1:
        total += cfg.child_deduction_first
    if children >= 2:
        total += cfg.child_deduction_second
    if children >= 3:
        total += cfg.child_deduction_next * (children - 2)
    if single_parent:
        total *= 2
    return total


def month_injury(base: Decimal, cfg: YearConfig) -> Decimal:
    """Взнос на травматизм за месяц: база × 0,2 % (I класс риска)."""
    return money(base * cfg.injury_rate)


def payroll_enp_projection_due(year: int, month: int) -> date:
    """Срок зарплатного ЕНП за месяц N — 28 число месяца N+1 (ст. 226, 431 НК)."""
    if month == 12:
        return date(year + 1, 1, 28)
    return date(year, month + 1, 28)


def injury_due(year: int, month: int) -> date:
    """Срок травматизма за месяц N — 15 число месяца N+1 (125-ФЗ, ст. 22)."""
    if month == 12:
        return date(year + 1, 1, 15)
    return date(year, month + 1, 15)


def _worked_full_month(emp: Employee, year: int, month: int) -> bool:
    """Сотрудник официально отработал месяц ЦЕЛИКОМ (стандартное начисление применимо)."""
    if not emp.is_official or emp.official_salary is None:
        return False
    if emp.official_status != "working":
        return False
    month_start = date(year, month, 1)
    month_end = (
        date(year, month + 1, 1) if month < 12 else date(year + 1, 1, 1)
    )
    if emp.hire_date is not None and emp.hire_date > month_start:
        return False  # принят в середине месяца — неполный, ждём оборотку
    # Уволенный до конца месяца — неполный месяц, начисление доберёт оборотка.
    return not (emp.fire_date is not None and emp.fire_date < month_end)


async def _cumulative_official_income(
    session: AsyncSession, emp: Employee, *, year: int, month: int, cfg: YearConfig
) -> Decimal:
    """Официальный доход сотрудника с начала года ПО КОНЕЦ месяца ``month``.

    Гибрид: месяцы, покрытые обороткой, берём фактом (``TaxPayrollLedger.accrued`` по
    табельному — там отпускные/премии/неполные месяцы уже правильные); непокрытые полные
    месяцы добираем окладом. Нужен для лимита детского вычета (450 000 нарастающим
    итогом): с месяца превышения вычет не применяется.
    """
    covered: set[int] = set()
    fact = ZERO
    if emp.official_tab_number:
        rows = (
            await session.scalars(
                select(TaxPayrollLedger).where(
                    TaxPayrollLedger.year == year,
                    TaxPayrollLedger.month <= month,
                    TaxPayrollLedger.tab_number == emp.official_tab_number,
                )
            )
        ).all()
        for row in rows:
            covered.add(row.month)
            fact += row.accrued or ZERO
    projected = sum(
        (
            Decimal(emp.official_salary)
            for m in range(1, month + 1)
            if m not in covered and _worked_full_month(emp, year, m)
        ),
        ZERO,
    )
    return fact + projected


async def official_month_accrual(
    session: AsyncSession, *, year: int, month: int, cfg: YearConfig
) -> OfficialMonthAccrual | None:
    """Прогнозное начисление месяца по официальным сотрудникам. None — начислять некому."""
    employees = (
        await session.scalars(select(Employee).where(Employee.is_official.is_(True)))
    ).all()
    base_total = ZERO
    ndfl_total = ZERO
    contrib_total = ZERO
    injury_total = ZERO
    count = 0
    for emp in employees:
        if not _worked_full_month(emp, year, month):
            continue
        base = Decimal(emp.official_salary)
        base_total += base
        deduction = child_deduction_monthly(
            emp.official_children_count or 0, bool(emp.official_single_parent), cfg
        )
        if deduction > 0:
            income = await _cumulative_official_income(
                session, emp, year=year, month=month, cfg=cfg
            )
            if income > cfg.ndfl_deduction_income_limit:
                deduction = ZERO  # лимит 450 000 превышен — вычет с этого месяца не действует
        ndfl_total += month_ndfl(base, deduction, cfg)
        contrib_total += month_contributions(base, cfg)
        injury_total += month_injury(base, cfg)
        count += 1
    if count == 0:
        return None
    return OfficialMonthAccrual(
        year=year,
        month=month,
        salary_base=base_total,
        ndfl=ndfl_total,
        contributions=contrib_total,
        injury=injury_total,
        employees=count,
    )


async def months_without_turnover(
    session: AsyncSession, *, year: int, today: date
) -> list[int]:
    """Полные (завершившиеся) месяцы года, НЕ покрытые обороткой бухгалтера.

    Прогноз строится строго как ДОПОЛНЕНИЕ к оборотке: сверка и разнос ЕНП строят свои
    строки только по месяцам с ``TaxPayrollLedger`` — по остальным работает прогноз.
    """
    covered = set(
        (
            await session.scalars(
                select(TaxPayrollLedger.month)
                .where(TaxPayrollLedger.year == year)
                .distinct()
            )
        ).all()
    )
    last_full = today.month - 1 if today.year == year else 12
    if today.year < year:
        return []
    return [m for m in range(1, last_full + 1) if m not in covered]


async def paid_enp_month_covered(session: AsyncSession, *, year: int, month: int) -> bool:
    """Зарплатный ЕНП месяца уже уплачен: есть факт разноса (взносы/НДФЛ) за период.

    Гасит прогноз для исторических месяцев без оборотки, но с разнесёнными фактами
    (например, платежи января–февраля, разнесённые руками при бэкфилле): иначе прогноз
    показал бы ложный долг за уже оплаченные месяцы.
    """
    period = f"{year}-{month:02d}"
    row = await session.scalar(
        select(TaxPayment.id)
        .where(
            TaxPayment.kind.in_(("contrib_employees", "ndfl")),
            TaxPayment.status == "paid",
            TaxPayment.for_period == period,
        )
        .limit(1)
    )
    return row is not None


async def _paid_injury_month_covered(
    session: AsyncSession, *, year: int, month: int
) -> bool:
    """Есть ли платёж/план травматизма, закрывающий месяц N (платится в N+1).

    Документные травматизм-платёжки несут период «год» — по (вид, период) их с прогнозом
    не сматчить. Матчим по окну: платёжка со сроком/датой уплаты в месяце N+1 — это
    платёж «за месяц N» (СФР-порядок: до 15 числа следующего месяца).
    """
    window_start = date(year, month, 28)  # платёжки иногда датированы концом месяца N
    window_end = injury_due(year, month)
    rows = (
        await session.scalars(
            select(TaxPayment).where(
                TaxPayment.kind == "contrib_injury",
                TaxPayment.status.in_(("planned", "paid")),
            )
        )
    ).all()
    for row in rows:
        anchor = row.paid_on
        if anchor is None:
            continue
        if window_start <= anchor <= window_end:
            return True
    return False


async def fixed_contribution_remainder(
    session: AsyncSession, *, year: int, cfg: YearConfig
) -> Decimal:
    """Остаток фиксированных взносов ИП за год: год − уплачено − незакрытые плановые.

    Плановые (продвинутые платёжки «СВ ИП») уже видны документным слоем обязательств —
    остаток их не включает, иначе одна и та же четверть покажется дважды.
    """
    from app.services.taxes.obligations import is_settled  # поздний импорт: разрыв цикла

    rows = (
        await session.scalars(
            select(TaxPayment).where(
                TaxPayment.kind == "contrib_fixed",
                TaxPayment.for_year == year,
                TaxPayment.status.in_(("planned", "paid")),
            )
        )
    ).all()
    paid_rows = [r for r in rows if r.status == "paid"]
    paid = sum((r.amount for r in paid_rows), ZERO)
    # Погашенная фактом плановая строка уже учтена в ``paid`` — второй раз не вычитаем.
    planned_open = sum(
        (r.amount for r in rows if r.status == "planned" and not is_settled(r, paid_rows)),
        ZERO,
    )
    remainder = cfg.fixed_contribution - paid - planned_open
    return max(ZERO, remainder)

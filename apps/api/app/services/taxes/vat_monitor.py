"""Монитор зарплатного критерия льготы по НДС для общепита (подп. 38 п. 3 ст. 149 НК).

Одно из условий освобождения общепита от НДС — среднемесячная зарплата за расчётный период
не ниже среднеотраслевой по региону (ОКВЭД класс 56). Считаем УПРОЩЁННО (решение владельца):
по ДЕЙСТВУЮЩЕМУ штатному сотруднику, который фактически работает и получает зарплату.
Декретных, уволенных, в отпуске по уходу в расчёт НЕ берём.

Как определяем «действующего» без отдельного признака: это сотрудник(и) из САМОГО СВЕЖЕГО
месяца оборотки. Кто ушёл в декрет/уволился — в последнем месяце уже не появляется и выпадает
из показателя автоматически.

Показатель считаем двумя способами и показываем оба, потому что месяц приёма/ухода неполный
и занижает среднее:
* по ПОЛНЫМ месяцам (начислено = оклад) — репрезентативный уровень зарплаты;
* по всем месяцам с выплатами — как есть.

Порог (среднеотраслевая по региону) в проводках не выводится — Росстат/ЕМИСС отстаёт, поэтому
вводится вручную и хранится в ``app_setting``. Без порога монитор показывает только показатель.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.settings import AppSetting
from app.models.tax import TaxPayrollLedger

ZERO = Decimal("0")
_CENTS = Decimal("0.01")

# Региональный порог (Росстат/ЕМИСС) вводится вручную по годам и живёт в app_setting.
REGIONAL_WAGE_KEY = "taxes.regional_wage"


async def load_regional_wage(session: AsyncSession, *, year: int) -> Decimal | None:
    """Введённый вручную региональный порог за год, если задан."""
    stored = await session.scalar(
        select(AppSetting.value).where(AppSetting.key == REGIONAL_WAGE_KEY)
    )
    if not isinstance(stored, dict):
        return None
    raw = (stored.get("thresholds") or {}).get(str(year))
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw))
    except (ArithmeticError, ValueError):
        return None


async def save_regional_wage(
    session: AsyncSession, *, year: int, amount: Decimal
) -> None:
    """Сохранить региональный порог за год (Росстат/ЕМИСС отстаёт — вводится руками)."""
    setting = await session.scalar(
        select(AppSetting).where(AppSetting.key == REGIONAL_WAGE_KEY)
    )
    thresholds = (
        dict(setting.value.get("thresholds") or {})
        if setting is not None and isinstance(setting.value, dict)
        else {}
    )
    thresholds[str(year)] = str(amount)
    new_value = {"version": 1, "thresholds": thresholds}
    if setting is None:
        session.add(
            AppSetting(
                key=REGIONAL_WAGE_KEY,
                value=new_value,
                value_type="json",
                category="taxes",
                display_name="Среднеотраслевая зарплата по региону (ОКВЭД 56)",
                description=(
                    "Порог зарплатного критерия льготы по НДС, по годам. "
                    "Источник — Росстат/ЕМИСС, Ростовская область."
                ),
                widget_type="json",
                unit="₽",
            )
        )
    else:
        setting.value = new_value
    await session.flush()


@dataclass(frozen=True)
class MonthAccrual:
    month: int
    accrued: Decimal
    oklad: Decimal | None
    full_month: bool  # начислено >= оклад → месяц отработан полностью


@dataclass(frozen=True)
class VatWageCriterion:
    year: int
    active_employee: str | None
    active_tab: str | None
    months: list[MonthAccrual]
    # Среднемесячное начисление по полным месяцам (основной показатель) и по всем месяцам.
    indicator_full: Decimal | None
    indicator_all: Decimal | None
    threshold: Decimal | None
    passes: bool | None  # проходит ли критерий (None — нет порога или показателя)
    margin: Decimal | None  # показатель − порог
    messages: list[str] = field(default_factory=list)


def _avg(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return (sum(values, ZERO) / len(values)).quantize(_CENTS, ROUND_HALF_UP)


async def evaluate_vat_wage_criterion(
    session: AsyncSession, *, year: int, threshold: Decimal | None = None
) -> VatWageCriterion:
    """Оценить зарплатный критерий НДС-льготы за год по оборот кам."""
    rows = (
        await session.execute(
            select(
                TaxPayrollLedger.month,
                TaxPayrollLedger.tab_number,
                TaxPayrollLedger.employee,
                TaxPayrollLedger.accrued,
                TaxPayrollLedger.oklad,
            )
            .where(TaxPayrollLedger.year == year)
            .order_by(TaxPayrollLedger.month)
        )
    ).all()

    if not rows:
        return VatWageCriterion(
            year=year, active_employee=None, active_tab=None, months=[],
            indicator_full=None, indicator_all=None, threshold=threshold,
            passes=None, margin=None,
            messages=["Нет оборотк за год — показатель не рассчитать."],
        )

    # Действующий сотрудник — из самого свежего месяца оборотки (ушедшие сюда не попадают).
    last_month = max(r.month for r in rows)
    active = sorted(
        {(r.tab_number, r.employee) for r in rows if r.month == last_month}
    )
    active_tabs = {tab for tab, _ in active}
    active_tab, active_employee = active[0] if active else (None, None)

    # Начисления действующего(их) по месяцам: суммируем строки только действующих сотрудников.
    by_month: dict[int, tuple[Decimal, Decimal]] = {}
    for row in rows:
        if row.tab_number not in active_tabs:
            continue
        accrued = row.accrued or ZERO
        oklad = row.oklad or ZERO
        prev_accrued, prev_oklad = by_month.get(row.month, (ZERO, ZERO))
        by_month[row.month] = (prev_accrued + accrued, prev_oklad + oklad)

    months: list[MonthAccrual] = []
    for month in sorted(by_month):
        accrued, oklad = by_month[month]
        months.append(
            MonthAccrual(
                month=month,
                accrued=accrued,
                oklad=oklad or None,
                full_month=oklad > ZERO and accrued >= oklad,
            )
        )

    with_pay = [m.accrued for m in months if m.accrued > ZERO]
    full = [m.accrued for m in months if m.full_month]
    indicator_all = _avg(with_pay)
    indicator_full = _avg(full)

    # Для вердикта берём показатель по полным месяцам (репрезентативнее), иначе по всем.
    indicator = indicator_full if indicator_full is not None else indicator_all

    messages: list[str] = []
    if len(active) > 1:
        names = ", ".join(name for _, name in active)
        messages.append(f"Действующих сотрудников несколько: {names}.")
    if indicator_full is None and indicator_all is not None:
        messages.append("Полных месяцев нет — показатель по неполным месяцам занижен.")

    passes: bool | None = None
    margin: Decimal | None = None
    if threshold is not None and indicator is not None:
        margin = indicator - threshold
        passes = indicator >= threshold
        verdict = "проходит" if passes else "НЕ проходит"
        messages.append(
            f"Критерий {verdict}: показатель {indicator} ₽ против порога {threshold} ₽."
        )
    elif threshold is None:
        messages.append("Региональный порог не введён — сравнить не с чем.")

    return VatWageCriterion(
        year=year,
        active_employee=active_employee,
        active_tab=active_tab,
        months=months,
        indicator_full=indicator_full,
        indicator_all=indicator_all,
        threshold=threshold,
        passes=passes,
        margin=margin,
        messages=messages,
    )

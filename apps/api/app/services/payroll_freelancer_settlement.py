"""Сверка посменных выплат внештатников с ведомостью (без двойной выплаты).

Смена, выданная наличными из кассы (строка ``FreelancerShiftSettlement`` со статусом
``paid_cash`` за этот период), в ведомости остаётся в ФОТ (gross не трогаем), но её сумма
ИСКЛЮЧАЕТСЯ из «к выплате» — по образцу того, как ведомость учитывает уже удержанные
авансы (``payroll_advance_recovery``): уменьшаем только ``total_payable`` строки с клэмпом
``min(due, net)``, gross-начисления не меняем.

Вариант Б: смены, не оплаченные наличными до финализации периода, отдельным состоянием НЕ
помечаются — при финализации период уходит из «открытых», его неоплаченные смены просто
исчезают из кассы (``list_unpaid_freelancers`` смотрит только открытый период), а их сумма
уже сидит в gross ведомости, которая их и платит. Оплаченные налом остаются вычтенными.

Гонки: если между расчётом и финализацией набор ``paid_cash`` смен периода изменился,
финализация блокируется (``run_has_stale_freelancer_settlements``) — как со «свежими»
авансами, чтобы пересчёт заново исключил cash-смены.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FreelancerShiftSettlement, PayrollLine, PayrollPeriod, PayrollRun
from app.services.payroll_calculator import decimal, money

_CENTS = Decimal("0.01")


def _money_key(value: Any) -> str:
    """Каноничная форма суммы для сравнения (float summary ↔ Decimal БД)."""
    return f"{decimal(value).quantize(_CENTS)}"


async def _paid_cash_settlements(
    session: AsyncSession, period: PayrollPeriod
) -> list[FreelancerShiftSettlement]:
    """``paid_cash`` смены, выданные в счёт этого периода (по ``period_id``).

    Через ``scalars().all()`` (как возврат авансов) — совместимо с тест-двойниками сессии.
    """
    return list(
        (
            await session.scalars(
                select(FreelancerShiftSettlement).where(
                    FreelancerShiftSettlement.period_id == period.id,
                    FreelancerShiftSettlement.status == "paid_cash",
                )
            )
        ).all()
    )


def _paid_cash_stats(
    settlements: Iterable[FreelancerShiftSettlement],
) -> tuple[Decimal, int]:
    """Итог и число ``paid_cash`` смен (для проверки устаревания на финализации)."""
    rows = list(settlements)
    total = sum((decimal(s.amount) for s in rows), Decimal("0"))
    return total, len(rows)


async def apply_freelancer_cash_settlements(
    session: AsyncSession,
    period: PayrollPeriod,
    run: PayrollRun,
    lines: Iterable[PayrollLine],
) -> dict[str, Any]:
    """Исключить из «к выплате» смены, уже выданные наличными (gross/ФОТ не трогаем).

    Возвращает сводку: применённое исключение (в пределах net строк) и полный итог/число
    ``paid_cash`` смен периода (для проверки устаревания на финализации).
    """
    lines = [line for line in lines if decimal(line.total_payable) > 0]
    settlements = await _paid_cash_settlements(session, period)
    period_total, period_count = _paid_cash_stats(settlements)

    paid_by_emp: dict[uuid.UUID, Decimal] = defaultdict(lambda: Decimal("0"))
    for settlement in settlements:
        paid_by_emp[settlement.employee_id] += decimal(settlement.amount)

    applied = Decimal("0")
    applied_count = 0
    if paid_by_emp:
        lines_by_emp: dict[uuid.UUID, list[PayrollLine]] = defaultdict(list)
        for line in lines:
            lines_by_emp[line.employee_id].append(line)
        for employee_id, due in paid_by_emp.items():
            for line in lines_by_emp.get(employee_id, []):
                if due <= 0:
                    break
                net = decimal(line.total_payable)
                take = min(due, net)
                if take <= 0:
                    continue
                line.total_payable = (net - take).quantize(_CENTS)
                components = dict(line.components or {})
                prev = decimal(components.get("freelancer_cash_settled", 0))
                components["freelancer_cash_settled"] = money(prev + take)
                line.components = components
                due -= take
                applied += take
                applied_count += 1

    return {
        "freelancer_cash_settled_count": applied_count,
        "freelancer_cash_settled_applied": money(applied),
        # Полный итог/число cash-смен периода — «подпись» для проверки устаревания.
        "freelancer_paid_cash_total": money(period_total),
        "freelancer_paid_cash_count": period_count,
    }


async def run_has_stale_freelancer_settlements(
    session: AsyncSession, run: PayrollRun, period: PayrollPeriod
) -> bool:
    """Изменился ли набор ``paid_cash`` смен периода после расчёта ведомости.

    True → финализировать нельзя, нужен пересчёт (иначе cash-смена, оплаченная после
    расчёта, задвоится). Старый расчёт без подписи (обратная совместимость) не блокируем.
    """
    summary = run.summary if isinstance(run.summary, dict) else {}
    if "freelancer_paid_cash_total" not in summary:
        return False
    current_total, current_count = _paid_cash_stats(
        await _paid_cash_settlements(session, period)
    )
    return _money_key(current_total) != _money_key(
        summary.get("freelancer_paid_cash_total", 0)
    ) or int(current_count) != int(summary.get("freelancer_paid_cash_count", -1))

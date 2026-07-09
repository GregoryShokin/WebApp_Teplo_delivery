"""Зачёт «уже выплачено» в ведомости: выплаты сотруднику из журнала ДДС.

Зеркало ``payroll_advance_recovery``. Когда банковская операция журнала ДДС привязана к
сотруднику зарплатной статьёй, заводится ``EmployeePayout(kind in ('salary','other'),
status='paid')`` — факт «этому сотруднику уже выплачено N». Расчёт ведомости вычитает такие
выплаты из «к выдаче»:

- ``apply_employee_payout_offsets`` — превью на расчёте: гасит ``min(остаток выплаты, net)``,
  чтобы «к выдаче» не ушло в минус; недобор переносится на следующую ведомость (остаток
  ``EmployeePayout.offset_amount`` < ``amount``). Мутирует ``total_payable``/
  ``employee_payout_offset`` строки, пишет превью-строки ``employee_payout_offset`` (с ``run_id``).
- на ре-расчёте превью удаляются (``DELETE ... WHERE run_id`` в раннере/админ-сервисе);
- ``apply_employee_payout_offsets_to_balances`` на финализации двигает
  ``EmployeePayout.offset_amount`` вперёд, при дефинализации — точный реверс.

``kind='owner_salary'`` сюда НЕ попадает — долг собственника гасит ``compute_on_demand_debt``.
Маршрутизация проста: гасим выплаты сотрудников, у которых есть строка в ЭТОМ прогоне
(сотрудник живёт в одной ведомости), поэтому отдельный дискриминатор роли не нужен.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    EmployeePayout,
    EmployeePayoutOffset,
    PayrollLine,
    PayrollPeriod,
    PayrollRun,
)
from app.services.payroll_calculator import decimal, money

_CENTS = Decimal("0.01")
# kind'ы выплат, которые режут обычную ведомость (owner_salary — контур on_demand-долга).
OFFSETTABLE_KINDS = ("salary", "other")


async def _outstanding_payouts(
    session: AsyncSession,
    employee_ids: set[uuid.UUID],
    *,
    period_end,
) -> dict[uuid.UUID, list[EmployeePayout]]:
    """Непогашенные привязанные выплаты (``amount > offset_amount``) с датой ≤ конца периода."""
    if not employee_ids:
        return {}
    rows = (
        await session.scalars(
            select(EmployeePayout)
            .where(
                EmployeePayout.employee_id.in_(employee_ids),
                EmployeePayout.kind.in_(OFFSETTABLE_KINDS),
                EmployeePayout.status == "paid",
                EmployeePayout.payout_date <= period_end,
            )
            .order_by(EmployeePayout.payout_date, EmployeePayout.created_at)
        )
    ).all()
    by_emp: dict[uuid.UUID, list[EmployeePayout]] = defaultdict(list)
    for payout in rows:
        if decimal(payout.offset_amount) >= decimal(payout.amount):
            continue
        by_emp[payout.employee_id].append(payout)
    return by_emp


async def apply_employee_payout_offsets(
    session: AsyncSession,
    period: PayrollPeriod,
    run: PayrollRun,
    lines: Iterable[PayrollLine],
) -> dict[str, Any]:
    """Превью-зачёт: гасит выплаты в пределах net строки, мутирует строки, создаёт
    ``employee_payout_offset``. Баланс ``EmployeePayout.offset_amount`` двигается на финализации."""
    lines = [line for line in lines if decimal(line.total_payable) > 0]
    payouts_by_emp = await _outstanding_payouts(
        session,
        {line.employee_id for line in lines},
        period_end=period.end_date,
    )
    if not payouts_by_emp:
        return {"employee_payout_offset_count": 0, "employee_payout_offset_total": money(Decimal("0"))}

    lines_by_emp: dict[uuid.UUID, list[PayrollLine]] = defaultdict(list)
    for line in lines:
        lines_by_emp[line.employee_id].append(line)

    offsets: list[EmployeePayoutOffset] = []
    total = Decimal("0")
    for employee_id, payouts in payouts_by_emp.items():
        emp_lines = lines_by_emp.get(employee_id, [])
        for payout in payouts:
            outstanding = decimal(payout.amount) - decimal(payout.offset_amount)
            for line in emp_lines:
                if outstanding <= 0:
                    break
                net = decimal(line.total_payable)
                take = min(outstanding, net)
                if take <= 0:
                    continue
                line.total_payable = (net - take).quantize(_CENTS)
                line.employee_payout_offset = (
                    decimal(line.employee_payout_offset) + take
                ).quantize(_CENTS)
                components = dict(line.components or {})
                items = list(components.get("employee_payout_offsets", []))
                items.append(
                    {
                        "employee_payout_id": str(payout.id),
                        "payout_date": payout.payout_date.isoformat(),
                        "amount": money(take),
                    }
                )
                components["employee_payout_offsets"] = items
                line.components = components
                offsets.append(
                    EmployeePayoutOffset(
                        employee_payout_id=payout.id,
                        run_id=run.id,
                        amount=take.quantize(_CENTS),
                    )
                )
                outstanding -= take
                total += take

    for offset in offsets:
        session.add(offset)
    return {
        "employee_payout_offset_count": len(offsets),
        "employee_payout_offset_total": money(total),
    }


async def apply_employee_payout_offsets_to_balances(
    session: AsyncSession,
    run: PayrollRun,
    now: datetime,
    *,
    reverse: bool,
) -> dict[str, Any]:
    """Финализация: двигает ``EmployeePayout.offset_amount`` по превью-строкам run'а;
    дефинализация (``reverse=True``) — точная обратная операция."""
    offsets = (
        await session.scalars(
            select(EmployeePayoutOffset).where(EmployeePayoutOffset.run_id == run.id)
        )
    ).all()
    if not offsets:
        return {"employee_payout_offset_count": 0}

    payout_ids = {offset.employee_payout_id for offset in offsets}
    payouts = {
        payout.id: payout
        for payout in (
            await session.scalars(
                select(EmployeePayout).where(EmployeePayout.id.in_(payout_ids))
            )
        ).all()
    }
    applied = Decimal("0")
    for offset in offsets:
        payout = payouts.get(offset.employee_payout_id)
        if payout is None:
            continue
        amount = decimal(offset.amount)
        delta = -amount if reverse else amount
        new_offset = decimal(payout.offset_amount) + delta
        new_offset = max(Decimal("0"), min(new_offset, decimal(payout.amount)))
        payout.offset_amount = new_offset.quantize(_CENTS)
        offset.applied_at = None if reverse else now
        applied += delta

    return {
        "employee_payout_offset_count": len(offsets),
        "employee_payout_offset_applied": money(applied),
    }

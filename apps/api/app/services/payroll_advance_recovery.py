"""Сейм возврата авансов/займов в ведомости.

Повторяет lifecycle депозитов:
- при расчёте `apply_advance_recoveries` создаёт превью-строки `salary_advance_recovery`
  (с `run_id`), гасит `min(доля, остаток, доступный net)` и уменьшает
  `total_payable`/`advance_recovered` строки; баланс аванса НЕ трогает;
- на ре-расчёте превью удаляются (`DELETE ... WHERE run_id` в раннере/админ-сервисе);
- при финализации `apply_advance_recoveries_to_balances(reverse=False)` двигает
  `recovered_amount`/`status` аванса вперёд, при дефинализации — точный реверс.

Маршрутизация по дискриминатору `role`: админская (полумесячная) ведомость гасит
авансы с `role ∈ ADMIN_PAYROLL_POSITIONS`, производственная (недельная) — остальные.
Anti-negative: за одну ведомость гасится не больше доли и не больше net строки;
недобор переносится на следующую ведомость (остаток аванса остаётся `issued`).
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
    PayrollLine,
    PayrollPeriod,
    PayrollRun,
    SalaryAdvance,
    SalaryAdvanceRecovery,
)
from app.services.payroll_calculator import decimal, money
from app.services.position_registry import admin_payroll_positions

_CENTS = Decimal("0.01")
PAYROLL_PAYOUT_METHOD = "payroll"


async def _outstanding_advances(
    session: AsyncSession,
    employee_ids: set[uuid.UUID],
    *,
    admin: bool,
    period_end,
) -> dict[uuid.UUID, list[SalaryAdvance]]:
    if not employee_ids:
        return {}
    rows = (
        await session.scalars(
            select(SalaryAdvance)
            .where(
                SalaryAdvance.employee_id.in_(employee_ids),
                SalaryAdvance.status == "issued",
                SalaryAdvance.issued_on <= period_end,
            )
            .order_by(SalaryAdvance.issued_on, SalaryAdvance.created_at)
        )
    ).all()
    by_emp: dict[uuid.UUID, list[SalaryAdvance]] = defaultdict(list)
    for advance in rows:
        # Маршрутизация по пайплайну выплаты: админская ведомость — только админ-роли.
        if (advance.role in admin_payroll_positions()) != admin:
            continue
        if decimal(advance.recovered_amount) >= decimal(advance.amount):
            continue
        # Отложенное удержание: заём не гасится, пока ведомость раньше даты старта.
        if advance.recovery_start_date is not None and advance.recovery_start_date > period_end:
            continue
        by_emp[advance.employee_id].append(advance)
    return by_emp


async def apply_advance_recoveries(
    session: AsyncSession,
    period: PayrollPeriod,
    run: PayrollRun,
    lines: Iterable[PayrollLine],
) -> dict[str, Any]:
    """Превью-возврат: гасит доли в пределах net строки, мутирует строки, создаёт
    `salary_advance_recovery`. Баланс аванса двигается только на финализации."""
    lines = [line for line in lines if decimal(line.total_payable) > 0]
    admin = period.period_type == "half_month"
    advances_by_emp = await _outstanding_advances(
        session,
        {line.employee_id for line in lines},
        admin=admin,
        period_end=period.end_date,
    )
    if not advances_by_emp:
        return {"advance_recovery_count": 0, "advance_recovered_total": money(Decimal("0"))}

    lines_by_emp: dict[uuid.UUID, list[PayrollLine]] = defaultdict(list)
    for line in lines:
        lines_by_emp[line.employee_id].append(line)

    recoveries: list[SalaryAdvanceRecovery] = []
    total = Decimal("0")
    for employee_id, advances in advances_by_emp.items():
        emp_lines = lines_by_emp.get(employee_id, [])
        for advance in advances:
            outstanding = decimal(advance.amount) - decimal(advance.recovered_amount)
            due = min(decimal(advance.per_installment_amount), outstanding)
            for line in emp_lines:
                if due <= 0:
                    break
                net = decimal(line.total_payable)
                take = min(due, net)
                if take <= 0:
                    continue
                line.total_payable = (net - take).quantize(_CENTS)
                line.advance_recovered = (decimal(line.advance_recovered) + take).quantize(_CENTS)
                components = dict(line.components or {})
                items = list(components.get("advance_recoveries", []))
                items.append(
                    {"advance_id": str(advance.id), "kind": advance.kind, "amount": money(take)}
                )
                components["advance_recoveries"] = items
                line.components = components
                recoveries.append(
                    SalaryAdvanceRecovery(
                        advance_id=advance.id,
                        run_id=run.id,
                        amount=take.quantize(_CENTS),
                    )
                )
                due -= take
                total += take

    for recovery in recoveries:
        session.add(recovery)
    return {
        "advance_recovery_count": len(recoveries),
        "advance_recovered_total": money(total),
    }


async def apply_advance_recoveries_to_balances(
    session: AsyncSession,
    run: PayrollRun,
    now: datetime,
    *,
    reverse: bool,
) -> dict[str, Any]:
    """Финализация: двигает `recovered_amount`/`status` аванса по превью-строкам run'а;
    дефинализация (`reverse=True`) — точная обратная операция."""
    recoveries = (
        await session.scalars(
            select(SalaryAdvanceRecovery).where(SalaryAdvanceRecovery.run_id == run.id)
        )
    ).all()
    if not recoveries:
        return {"advance_recovery_count": 0}

    advance_ids = {recovery.advance_id for recovery in recoveries}
    advances = {
        advance.id: advance
        for advance in (
            await session.scalars(select(SalaryAdvance).where(SalaryAdvance.id.in_(advance_ids)))
        ).all()
    }
    applied = Decimal("0")
    for recovery in recoveries:
        advance = advances.get(recovery.advance_id)
        if advance is None:
            continue
        amount = decimal(recovery.amount)
        delta = -amount if reverse else amount
        new_recovered = decimal(advance.recovered_amount) + delta
        new_recovered = max(Decimal("0"), min(new_recovered, decimal(advance.amount)))
        advance.recovered_amount = new_recovered.quantize(_CENTS)
        if reverse:
            recovery.applied_at = None
            advance.status = "issued"
        else:
            recovery.applied_at = now
            if new_recovered >= decimal(advance.amount):
                advance.status = "recovered"
        applied += delta

    return {
        "advance_recovery_count": len(recoveries),
        "advance_recovered_applied": money(applied),
    }


async def _payroll_advance_issuances(
    session: AsyncSession,
    employee_ids: set[uuid.UUID],
    *,
    admin: bool,
    payout_date,
) -> dict[uuid.UUID, list[SalaryAdvance]]:
    """Авансы/займы, выданные ЧЕРЕЗ ведомость (`payout_method='payroll'`) в день её выплаты —
    их сумма добавляется к выплате, а удержание начинается со следующих ведомостей."""
    if not employee_ids:
        return {}
    rows = (
        await session.scalars(
            select(SalaryAdvance)
            .where(
                SalaryAdvance.employee_id.in_(employee_ids),
                SalaryAdvance.status == "issued",
                SalaryAdvance.payout_method == PAYROLL_PAYOUT_METHOD,
                SalaryAdvance.issued_on == payout_date,
            )
            .order_by(SalaryAdvance.created_at, SalaryAdvance.id)
        )
    ).all()
    by_emp: dict[uuid.UUID, list[SalaryAdvance]] = defaultdict(list)
    for advance in rows:
        if (advance.role in admin_payroll_positions()) != admin:
            continue
        by_emp[advance.employee_id].append(advance)
    return by_emp


def apply_advance_issuance_to_line(line: PayrollLine, advance: SalaryAdvance) -> Decimal:
    amount = decimal(advance.amount).quantize(_CENTS)
    line.total_payable = (decimal(line.total_payable) + amount).quantize(_CENTS)
    components = dict(line.components or {})
    items = list(components.get("advance_issuances", []))
    items.append(
        {"advance_id": str(advance.id), "kind": advance.kind, "amount": money(amount)}
    )
    components["advance_issuances"] = items
    components["advance_issued"] = money(decimal(components.get("advance_issued", 0)) + amount)
    line.components = components
    return amount


def remove_advance_issuance_from_line(line: PayrollLine, advance: SalaryAdvance) -> Decimal:
    components = dict(line.components or {})
    items = list(components.get("advance_issuances", []))
    advance_id = str(advance.id)
    kept: list[Any] = []
    removed = Decimal("0")
    for item in items:
        if isinstance(item, dict) and item.get("advance_id") == advance_id:
            removed += decimal(item.get("amount", 0))
            continue
        kept.append(item)
    if removed <= 0:
        return Decimal("0")
    removed = removed.quantize(_CENTS)
    line.total_payable = max(decimal(line.total_payable) - removed, Decimal("0")).quantize(_CENTS)
    components["advance_issuances"] = kept
    remaining = max(decimal(components.get("advance_issued", 0)) - removed, Decimal("0"))
    if remaining > 0:
        components["advance_issued"] = money(remaining)
    else:
        components.pop("advance_issued", None)
    line.components = components
    return removed


async def apply_advance_issuances(
    session: AsyncSession,
    period: PayrollPeriod,
    run: PayrollRun,
    lines: Iterable[PayrollLine],
) -> dict[str, Any]:
    """Добавить в ведомость суммы авансов/займов, выдаваемых вместе с этой выплатой."""
    lines = list(lines)
    admin = period.period_type == "half_month"
    issuances_by_emp = await _payroll_advance_issuances(
        session,
        {line.employee_id for line in lines},
        admin=admin,
        payout_date=period.payroll_date,
    )
    if not issuances_by_emp:
        return {"advance_issued_count": 0, "advance_issued_total": money(Decimal("0"))}

    lines_by_emp: dict[uuid.UUID, list[PayrollLine]] = defaultdict(list)
    for line in lines:
        lines_by_emp[line.employee_id].append(line)

    total = Decimal("0")
    count = 0
    for employee_id, advances in issuances_by_emp.items():
        target_line = (lines_by_emp.get(employee_id) or [None])[0]
        if target_line is None:
            continue
        for advance in advances:
            total += apply_advance_issuance_to_line(target_line, advance)
            count += 1

    return {"advance_issued_count": count, "advance_issued_total": money(total)}


async def run_has_unaccounted_advances(
    session: AsyncSession,
    run: PayrollRun,
    period: PayrollPeriod,
) -> bool:
    """Есть ли у прогона авансы/займы, заведённые ПОСЛЕ расчёта и не попавшие в его итог.

    Удержание/выдача материализуются только на расчёте, поэтому выдача — в т.ч. задним
    числом — в уже посчитанную, но не финализированную ведомость молча выпадает из
    `total_payable`. Ловим такие авансы, чтобы заблокировать финализацию и показать баннер.
    Критерий `created_at > run.started_at` самоснимается после «Пересчитать»."""
    lines = list(
        (await session.scalars(select(PayrollLine).where(PayrollLine.run_id == run.id))).all()
    )
    if not lines:
        return False
    employee_ids = {line.employee_id for line in lines}
    admin = period.period_type == "half_month"

    candidates: dict[uuid.UUID, SalaryAdvance] = {}
    for group in (
        await _outstanding_advances(
            session, employee_ids, admin=admin, period_end=period.end_date
        ),
        await _payroll_advance_issuances(
            session, employee_ids, admin=admin, payout_date=period.payroll_date
        ),
    ):
        for advances in group.values():
            for advance in advances:
                candidates[advance.id] = advance
    if not candidates:
        return False

    accounted: set[uuid.UUID] = set(
        (
            await session.scalars(
                select(SalaryAdvanceRecovery.advance_id).where(
                    SalaryAdvanceRecovery.run_id == run.id
                )
            )
        ).all()
    )
    for line in lines:
        components = line.components or {}
        for key in ("advance_issuances", "advance_recoveries"):
            for item in components.get(key, []):
                advance_id = item.get("advance_id") if isinstance(item, dict) else None
                if not advance_id:
                    continue
                try:
                    accounted.add(uuid.UUID(str(advance_id)))
                except (ValueError, TypeError):
                    continue

    started = run.started_at
    for advance_id, advance in candidates.items():
        if advance_id in accounted:
            continue
        created = advance.created_at
        if started is None or created is None or created > started:
            return True
    return False

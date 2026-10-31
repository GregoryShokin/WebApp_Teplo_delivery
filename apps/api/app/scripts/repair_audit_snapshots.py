"""Починка снимков применённых ревизий, разошедшихся с фактическими штрафами.

Решение владельца 31.10.2026: у закрытой ревизии распределение — снимок, и когда неделя
закрылась, люди из него пропадать не должны. Пока открытие ревизии её пересчитывало,
круг людей брался по статусу на сегодня: уволенные выпадали из снимка, их доля
раскладывалась на оставшихся. Штрафы при этом целы, они лежат в ``payroll_adjustment``,
поэтому снимок восстанавливается из них.

Как работает скрипт:

- факт по человеку — сумма его штрафов с комментарием «Недостача по ревизии <дата>».
  Снимок сравнивается с этой суммой, потому что применение записывает штрафы ровно по
  снимку; совпало — ревизия не трогается;
- рассрочка в снимок не входит: её берут с исключённой позиции, а экран прибавляет
  рассрочку к штрафу отдельно. Поэтому она не участвует в сравнении сумм, но человек с
  удержанной рассрочкой считается удержанным и в снимке остаётся;
- разошлось — снимок пересобирается тем же расчётом
  (``rebuild_applied_computation``), и итог сверяется с фактом по каждому человеку до
  копейки. Совпало — пишутся ``computation_snapshot`` и ``total_penalty_amount``; не
  совпало — ревизия печатается и остаётся на глаз;
- ``applied_at`` и штрафы не меняются; повторный запуск находит, что всё совпало.

Запуск:

    python -m app.scripts.repair_audit_snapshots            # только показать
    python -m app.scripts.repair_audit_snapshots --apply    # записать
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models import (
    DeferredAuditCharge,
    DeferredAuditChargeRecipient,
    DeferredAuditChargeSplit,
    Employee,
    InventoryAudit,
    PayrollAdjustment,
)
from app.services.inventory_audit_service import (
    adjustment_comment,
    decimal_string,
    get_audit_with_items,
    rebuild_applied_computation,
)

ZERO = Decimal("0")
Amounts = Mapping[uuid.UUID, Decimal]


def parse_args() -> argparse.Namespace:
    """Разобрать ключи запуска: без ``--apply`` скрипт ничего не пишет."""
    parser = argparse.ArgumentParser(
        description="Починить снимки применённых ревизий по фактическим штрафам."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="записать снимки; без ключа только показать",
    )
    return parser.parse_args()


async def load_withheld_penalties(
    session: AsyncSession, business_date: date
) -> dict[uuid.UUID, Decimal]:
    """Штрафы по людям, поставленные применением ревизии этой даты.

    Комментарий — единственная связь штрафа с ревизией; по нему же штрафы снимает отмена
    ревизии (``cancel_audit``). Предоплаты в финалке уволенного сюда не попадают: у них
    свой комментарий, и в снимке они лежат отдельным списком.
    """
    rows = await session.execute(
        select(PayrollAdjustment.employee_id, PayrollAdjustment.amount).where(
            PayrollAdjustment.type == "penalty",
            PayrollAdjustment.comment.like(f"{adjustment_comment(business_date)}%"),
        )
    )
    totals: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)
    for employee_id, amount in rows:
        totals[employee_id] += amount
    return dict(totals)


async def load_withheld_installments(
    session: AsyncSession, audit_id: uuid.UUID
) -> dict[uuid.UUID, Decimal]:
    """Рассрочка ревизии, уже удержанная, по людям.

    Удержанной считается доля с ведомостью: отмена рассрочки помечает несписанные доли
    временем, но ведомости им не ставит.
    """
    rows = await session.execute(
        select(DeferredAuditChargeRecipient.employee_id, DeferredAuditChargeSplit.amount)
        .join(
            DeferredAuditCharge,
            DeferredAuditCharge.id == DeferredAuditChargeRecipient.charge_id,
        )
        .join(
            DeferredAuditChargeSplit,
            DeferredAuditChargeSplit.recipient_id == DeferredAuditChargeRecipient.id,
        )
        .where(
            DeferredAuditCharge.source_audit_id == audit_id,
            DeferredAuditChargeSplit.run_id.is_not(None),
            DeferredAuditChargeSplit.amount > 0,
        )
    )
    totals: dict[uuid.UUID, Decimal] = defaultdict(lambda: ZERO)
    for employee_id, amount in rows:
        totals[employee_id] += amount
    return dict(totals)


def snapshot_amounts(snapshot: object) -> dict[uuid.UUID, Decimal]:
    """Суммы по людям из снимка; снимка нет или в нём нет сумм — пусто."""
    by_id = snapshot.get("employee_penalties_by_id") if isinstance(snapshot, dict) else None
    if not isinstance(by_id, dict):
        return {}
    return {uuid.UUID(str(key)): Decimal(str(value)) for key, value in by_id.items()}


def differing_ids(left: Amounts, right: Amounts) -> set[uuid.UUID]:
    """Люди, у которых суммы двух раскладок расходятся; нет суммы — значит ноль."""
    return {
        employee_id
        for employee_id in set(left) | set(right)
        if left.get(employee_id, ZERO) != right.get(employee_id, ZERO)
    }


async def load_names(session: AsyncSession, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Имена людей для печати."""
    wanted = set(ids)
    if not wanted:
        return {}
    rows = await session.execute(
        select(Employee.id, Employee.full_name).where(Employee.id.in_(wanted))
    )
    return {employee_id: full_name for employee_id, full_name in rows}


def print_rows(
    ids: Iterable[uuid.UUID],
    names: Mapping[uuid.UUID, str],
    *,
    saved: Amounts,
    withheld: Amounts,
    rebuilt: Amounts,
) -> None:
    """Напечатать людей с суммами снимка, удержания и пересборки."""
    for employee_id in sorted(ids, key=lambda value: names.get(value, str(value))):
        print(
            f"    {names.get(employee_id, str(employee_id))}: "
            f"снимок {decimal_string(saved.get(employee_id, ZERO))}, "
            f"удержано {decimal_string(withheld.get(employee_id, ZERO))}, "
            f"пересборка {decimal_string(rebuilt.get(employee_id, ZERO))}"
        )


async def repair(session: AsyncSession, *, apply: bool) -> tuple[int, int, int, int]:
    """Проверить применённые ревизии и вернуть итог.

    Возвращает ``(проверено, совпало, починено, оставлено на глаз)``. Без ``apply``
    в «починено» стоит то, что было бы записано.
    """
    audit_ids = (
        await session.scalars(
            select(InventoryAudit.id)
            .where(InventoryAudit.status == "applied")
            .order_by(InventoryAudit.business_date)
        )
    ).all()
    matched = repaired = left_for_review = 0
    for audit_id in audit_ids:
        audit = (await get_audit_with_items(session, audit_id))["audit"]
        saved = snapshot_amounts(audit.computation_snapshot)
        withheld = await load_withheld_penalties(session, audit.business_date)
        if not differing_ids(saved, withheld):
            matched += 1
            continue

        installments = await load_withheld_installments(session, audit.id)
        computation = await rebuild_applied_computation(
            session,
            audit,
            keep_employee_ids=set(withheld) | set(installments),
        )
        rebuilt = computation.employee_penalties
        differing = differing_ids(rebuilt, withheld)
        names = await load_names(session, set(saved) | set(withheld) | set(rebuilt))

        people = f"людей в снимке {len(saved)}, удержано с {len(withheld)}"
        if installments:
            people += f", рассрочка у {len(installments)} вне снимка"
        if differing:
            left_for_review += 1
            print(f"{audit.business_date}: НА ГЛАЗ, {people}")
            print_rows(differing, names, saved=saved, withheld=withheld, rebuilt=rebuilt)
            continue

        repaired += 1
        print(f"{audit.business_date}: чиним, {people}")
        print_rows(
            differing_ids(saved, withheld),
            names,
            saved=saved,
            withheld=withheld,
            rebuilt=rebuilt,
        )
        if apply:
            audit.computation_snapshot = computation.snapshot
            audit.total_penalty_amount = computation.total_penalty_amount
    return len(audit_ids), matched, repaired, left_for_review


async def async_main() -> None:
    """Запустить проверку; запись идёт одной транзакцией и только с ``--apply``."""
    args = parse_args()
    async with AsyncSessionLocal() as session:
        checked, matched, repaired, left_for_review = await repair(session, apply=args.apply)
        if args.apply:
            await session.commit()
        else:
            await session.rollback()
    repaired_label = "починено" if args.apply else "будет починено"
    print(
        f"Проверено {checked}, совпало {matched}, {repaired_label} {repaired}, "
        f"оставлено на глаз {left_for_review}."
    )


def main() -> None:
    """Точка входа ``python -m app.scripts.repair_audit_snapshots``."""
    asyncio.run(async_main())


if __name__ == "__main__":
    main()

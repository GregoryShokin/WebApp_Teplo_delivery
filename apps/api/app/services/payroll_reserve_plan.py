"""Persistent unpaid payroll plans. Editing a plan never pays an employee.

Plans are versioned audit events, not PayrollPayments. A booking baseline lets reads
consume the plan after an actual payout (including payouts made from other screens).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CashflowTransaction,
    PayrollPayoutBooking,
    PayrollRun,
    PayrollRunEvent,
    SafeAllocation,
)
from app.services.banking.safe_allocations import ACTIVE_RESERVE_STATUSES
from app.services.payroll_reserves import (
    EmployeeShare,
    PoolAllocation,
    _active_run_reserve,
    _q,
    allocate_pool,
    locked_run_reserve,
    run_pool_shares,
    transfer_run_reserve,
)
from app.services.payroll_runner import PayrollConflictError, PayrollNotFoundError

PLAN_ACTION = "reserve_plan_updated"


async def _latest_plan_event(
    session: AsyncSession, reserve: SafeAllocation
) -> PayrollRunEvent | None:
    return await session.scalar(
        select(PayrollRunEvent)
        .where(
            PayrollRunEvent.run_id == reserve.source_run_id,
            PayrollRunEvent.action == PLAN_ACTION,
            PayrollRunEvent.payload["reserve_id"].astext == str(reserve.id),
        )
        .order_by(PayrollRunEvent.created_at.desc(), PayrollRunEvent.id.desc())
        .limit(1)
    )


def _saved_items(event: PayrollRunEvent | None, remaining: dict, booked: dict) -> dict:
    items = {}
    for raw in event.payload["allocations"] if event else []:
        eid = uuid.UUID(raw["employee_id"])
        if eid not in remaining:
            continue
        consumed = max(Decimal(0), booked.get(eid, Decimal(0)) - Decimal(raw["booked_baseline"]))
        amount = min(remaining[eid], max(Decimal(0), Decimal(raw["amount"]) - consumed))
        deferred = min(
            remaining[eid] - amount,
            max(
                Decimal(0),
                Decimal(raw["deferred"]) - max(Decimal(0), consumed - Decimal(raw["amount"])),
            ),
        )
        items[eid] = {"amount": amount, "deferred": deferred}
    return items


async def _booked(session: AsyncSession, reserve: SafeAllocation) -> dict[uuid.UUID, Decimal]:
    rows = (
        await session.execute(
            select(PayrollPayoutBooking.employee_id, func.sum(PayrollPayoutBooking.amount))
            .join(
                CashflowTransaction,
                CashflowTransaction.id == PayrollPayoutBooking.cashflow_transaction_id,
            )
            .where(
                PayrollPayoutBooking.run_id == reserve.source_run_id,
                PayrollPayoutBooking.reversal_transaction_id.is_(None),
                CashflowTransaction.wallet_id == reserve.wallet_id,
                CashflowTransaction.quality_status != "excluded",
            )
            .group_by(PayrollPayoutBooking.employee_id)
        )
    ).all()
    return {eid: _q(amount) for eid, amount in rows}


async def read_reserve_plan(session: AsyncSession, reserve: SafeAllocation) -> dict:
    from app.services.payroll_obligations import run_obligations

    obligations = await run_obligations(session, reserve.source_run_id)
    shares = [
        EmployeeShare(eid, item.remaining)
        for eid, item in obligations.items()
        if item.remaining > 0
    ]
    remaining = {s.employee_id: s.remaining for s in shares}
    outstanding = (
        _q(max(Decimal(0), reserve.amount - reserve.amount_paid))
        if reserve.status in ACTIVE_RESERVE_STATUSES
        else Decimal(0)
    )
    event = await _latest_plan_event(session, reserve)
    booked = await _booked(session, reserve)
    saved = _saved_items(event, remaining, booked)
    items = {
        eid: saved.get(eid, {"amount": Decimal(0), "deferred": Decimal(0)}) for eid in remaining
    }
    # Preserve every explicitly edited amount, including zero/deferred. Only employees
    # missing from an older plan (previously hidden deposit returns) may be auto-filled.
    other = await _active_run_reserve(
        session, reserve.source_run_id, "safe" if reserve.location == "kassa" else "kassa"
    )
    claims = {}
    if other is not None:
        other_event = await _latest_plan_event(session, other)
        other_items = _saved_items(other_event, remaining, await _booked(session, other))
        claims = {eid: i["amount"] + i["deferred"] for eid, i in other_items.items()}
        # Cash is first for unplanned amounts; explicit promises on either account win.
        if reserve.location == "safe":
            other_free = max(
                Decimal(0), _q(other.amount - other.amount_paid) - sum(claims.values(), Decimal(0))
            )
            for alloc in allocate_pool(
                other_free,
                [
                    EmployeeShare(
                        s.employee_id,
                        max(
                            Decimal(0),
                            s.remaining
                            - items[s.employee_id]["amount"]
                            - items[s.employee_id]["deferred"],
                        ),
                    )
                    for s in shares
                    if s.employee_id not in other_items
                ],
            ):
                claims[alloc.employee_id] = alloc.amount
    free = max(
        Decimal(0),
        outstanding - sum((i["amount"] + i["deferred"] for i in items.values()), Decimal(0)),
    )
    for alloc in allocate_pool(
        free,
        [
            EmployeeShare(
                s.employee_id, max(Decimal(0), s.remaining - claims.get(s.employee_id, Decimal(0)))
            )
            for s in shares
            if s.employee_id not in saved
        ],
    ):
        items[alloc.employee_id]["amount"] = alloc.amount
    # Any changed financial fact invalidates an open editor/payout confirmation.
    fingerprint = [
        str(event.id) if event else None,
        str(outstanding),
        reserve.status,
        sorted((str(eid), str(i["amount"]), str(i["deferred"])) for eid, i in items.items()),
        sorted(
            (str(eid), str(due), str(booked.get(eid, Decimal(0)))) for eid, due in remaining.items()
        ),
        sorted(
            (str(eid), str(i.salary_remaining), str(i.deposit_remaining))
            for eid, i in obligations.items()
        ),
    ]
    version = hashlib.sha256(json.dumps(fingerprint).encode()).hexdigest()
    return {
        "reserve_id": reserve.id,
        "version": version,
        "outstanding": outstanding,
        "allocations": [
            {
                "employee_id": eid,
                **item,
                "remaining": remaining[eid],
                "salary_remaining": obligations[eid].salary_remaining,
                "deposit_remaining": obligations[eid].deposit_remaining,
            }
            for eid, item in items.items()
        ],
    }


async def get_reserve_plan(session: AsyncSession, reserve_id: uuid.UUID) -> dict:
    reserve = await session.get(SafeAllocation, reserve_id)
    if reserve is None or reserve.source_run_id is None:
        raise PayrollNotFoundError("Резерв ведомости не найден")
    if reserve.employee_id is not None:
        raise PayrollConflictError("Это не пул-резерв ведомости")
    result = await read_reserve_plan(session, reserve)
    other = await _active_run_reserve(
        session, reserve.source_run_id, "safe" if reserve.location == "kassa" else "kassa"
    )
    other_items = {}
    if other is not None:
        other_plan = await read_reserve_plan(session, other)
        other_items = {
            i["employee_id"]: i["amount"] + i["deferred"] for i in other_plan["allocations"]
        }
    result["other_location"] = other.location if other else None
    for item in result["allocations"]:
        item["other_amount"] = other_items.get(item["employee_id"], Decimal(0))
    return result


def check_plan_version(plan: dict, expected: str | None) -> None:
    if expected is not None and plan["version"] != expected:
        raise PayrollConflictError("План или остаток изменился. Обновите окно и проверьте суммы")


def require_plan_version(expected: str | None) -> str:
    """Old clients must reload the saved unpaid plan before any money action."""
    if expected is None or not expected.strip():
        raise PayrollConflictError(
            "Обновите страницу и проверьте сохранённый план выплаты. "
            "Выплата и перенос без актуального плана запрещены; остаток остаётся в резерве"
        )
    return expected


async def save_reserve_plan(
    session: AsyncSession, reserve: SafeAllocation, items: dict, actor_user_id: uuid.UUID | None
) -> None:
    booked = await _booked(session, reserve)
    run = await session.get(PayrollRun, reserve.source_run_id)
    session.add(
        PayrollRunEvent(
            run_id=run.id,
            period_id=run.period_id,
            action=PLAN_ACTION,
            actor_user_id=actor_user_id,
            created_at=datetime.now(UTC),
            payload={
                "reserve_id": str(reserve.id),
                "allocations": [
                    {
                        "employee_id": str(eid),
                        "amount": str(_q(item["amount"])),
                        "deferred": str(_q(item["deferred"])),
                        "booked_baseline": str(booked.get(eid, Decimal(0))),
                    }
                    for eid, item in sorted(items.items(), key=lambda pair: str(pair[0]))
                ],
            },
        )
    )
    await session.flush()


async def edit_reserve_plan(
    session: AsyncSession,
    *,
    reserve_id: uuid.UUID,
    employee_id: uuid.UUID,
    amount: Decimal,
    expected_version: str,
    remainder_destination: str | None,
    operation_date: date,
    actor_user_id: uuid.UUID | None,
) -> dict:
    source, run = await locked_run_reserve(session, reserve_id)
    if source.employee_id is not None or source.status not in ACTIVE_RESERVE_STATUSES:
        raise PayrollConflictError("Резерв уже оплачен или отменён")
    if run is None or run.status != "finalized":
        raise PayrollConflictError("Сначала финализируйте ведомость")
    plan = await read_reserve_plan(session, source)
    check_plan_version(plan, expected_version)
    items = {
        item["employee_id"]: {"amount": item["amount"], "deferred": item["deferred"]}
        for item in plan["allocations"]
    }
    due = {s.employee_id: s.remaining for s in await run_pool_shares(session, run.id)}
    if employee_id not in due:
        raise PayrollConflictError("У сотрудника нет остатка к выплате в этой ведомости")
    if not amount.is_finite() or _q(amount) != amount:
        raise PayrollConflictError("Введите сумму с точностью до копейки")
    amount = _q(amount)
    old = items[employee_id]
    other = sum(
        (i["amount"] + i["deferred"] for eid, i in items.items() if eid != employee_id), Decimal(0)
    )
    if amount < 0 or amount > due[employee_id] or amount > plan["outstanding"] - other:
        raise PayrollConflictError(
            "Сумма превышает долг сотрудника или свободный остаток этого резерва"
        )
    other_reserve = await _active_run_reserve(
        session, run.id, "safe" if source.location == "kassa" else "kassa", for_update=True
    )
    if other_reserve is not None:
        other_plan = await read_reserve_plan(session, other_reserve)
        other_claim = next(
            (
                i["amount"] + i["deferred"]
                for i in other_plan["allocations"]
                if i["employee_id"] == employee_id
            ),
            Decimal(0),
        )
        if amount > due[employee_id] - other_claim:
            raise PayrollConflictError("Часть зарплаты уже запланирована на другом счёте")
    released = max(Decimal(0), old["amount"] - amount)
    deferred = max(Decimal(0), old["deferred"] - max(Decimal(0), amount - old["amount"]))
    moved = Decimal(0)
    if remainder_destination is None:
        deferred += released
    else:
        destination_location = "safe" if source.location == "kassa" else "kassa"
        if remainder_destination != destination_location or released <= 0:
            raise PayrollConflictError("Выберите другой наличный счёт для остатка")
        # Save the source claim before deriving an automatic destination plan.
        # That plan then excludes all wages retained on the source account.
        items[employee_id] = {"amount": amount + released, "deferred": deferred}
        await save_reserve_plan(session, source, items, actor_user_id)
        destination = await _active_run_reserve(
            session, run.id, destination_location, for_update=True
        )
        dest_items = {}
        if destination is not None:
            dest_plan = await read_reserve_plan(session, destination)
            dest_items = {
                i["employee_id"]: {"amount": i["amount"], "deferred": i["deferred"]}
                for i in dest_plan["allocations"]
            }
        # Explicit amount: never reallocate the employee's entire wage or pay it.
        result = await transfer_run_reserve(
            session,
            reserve_id=source.id,
            selected_ids={employee_id},
            operation_date=operation_date,
            actor_user_id=actor_user_id,
            allocations_override=[PoolAllocation(employee_id, released)],
            commit=False,
        )
        destination = await session.get(SafeAllocation, result.destination_reserve_id)
        target = dest_items.setdefault(employee_id, {"amount": Decimal(0), "deferred": Decimal(0)})
        target["amount"] += released
        await save_reserve_plan(session, destination, dest_items, actor_user_id)
        moved = released
    items[employee_id] = {"amount": amount, "deferred": deferred}
    await save_reserve_plan(session, source, items, actor_user_id)
    await session.commit()
    result = await get_reserve_plan(session, source.id)
    result["transferred"] = moved
    return result


async def transfer_planned_reserve(
    session: AsyncSession,
    *,
    reserve_id: uuid.UUID,
    selected_ids: set[uuid.UUID],
    expected_version: str,
    operation_date: date,
    actor_user_id: uuid.UUID | None,
):
    source, _run = await locked_run_reserve(session, reserve_id)
    plan = await read_reserve_plan(session, source)
    check_plan_version(plan, expected_version)
    items = {
        i["employee_id"]: {"amount": i["amount"], "deferred": i["deferred"]}
        for i in plan["allocations"]
    }
    # Establish the source claim so an automatic destination excludes it.
    await save_reserve_plan(session, source, items, actor_user_id)
    destination = await _active_run_reserve(
        session,
        source.source_run_id,
        "safe" if source.location == "kassa" else "kassa",
        for_update=True,
    )
    dest_items = {}
    if destination is not None:
        dest_plan = await read_reserve_plan(session, destination)
        dest_items = {
            i["employee_id"]: {"amount": i["amount"], "deferred": i["deferred"]}
            for i in dest_plan["allocations"]
        }
    allocations = [
        PoolAllocation(eid, item["amount"])
        for eid, item in items.items()
        if eid in selected_ids and item["amount"] > 0
    ]
    result = await transfer_run_reserve(
        session,
        reserve_id=source.id,
        selected_ids=selected_ids,
        operation_date=operation_date,
        actor_user_id=actor_user_id,
        allocations_override=allocations,
        commit=False,
    )
    destination = await session.get(SafeAllocation, result.destination_reserve_id)
    for alloc in allocations:
        items[alloc.employee_id]["amount"] = Decimal(0)
        target = dest_items.setdefault(
            alloc.employee_id, {"amount": Decimal(0), "deferred": Decimal(0)}
        )
        target["amount"] += alloc.amount
    await save_reserve_plan(session, source, items, actor_user_id)
    await save_reserve_plan(session, destination, dest_items, actor_user_id)
    await session.commit()
    return result

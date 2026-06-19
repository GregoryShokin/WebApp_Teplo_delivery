from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CourierDepositAccount,
    CourierDepositTransaction,
    CourierDepositTransactionType,
    CourierEvaluation,
    CourierEvaluationCriterion,
    CourierIikoShift,
    CourierShiftDay,
    CourierShiftDayStatus,
    CourierShiftMatch,
    CourierShiftReview,
    CourierShiftSubstitution,
    Employee,
    User,
)
from app.services.couriers.deposit_service import (
    get_deposit_settings,
    target_cents_for_employee,
)
from app.services.couriers.shift_matching import MOSCOW_TZ, worked_minutes_for_shift
from app.services.position_registry import courier_positions

UNSET = object()


def _day_bounds(work_date: date) -> tuple[datetime, datetime]:
    start = datetime.combine(work_date, datetime.min.time(), tzinfo=MOSCOW_TZ)
    end = datetime.combine(work_date + timedelta(days=1), datetime.min.time(), tzinfo=MOSCOW_TZ)
    return start, end


def _category_from_match(match: CourierShiftMatch | None) -> str | None:
    if match is None:
        return None
    value = match.status.value
    if value.endswith("_primary"):
        return "primary"
    if value.endswith("_secondary"):
        return "secondary"
    return None


async def get_shift_day(session: AsyncSession, work_date: date) -> dict[str, Any]:
    start_at, end_at = _day_bounds(work_date)
    rows = (
        await session.execute(
            select(CourierIikoShift, Employee, CourierShiftMatch)
            .outerjoin(Employee, Employee.id == CourierIikoShift.employee_id)
            .outerjoin(
                CourierShiftMatch,
                CourierShiftMatch.iiko_shift_id == CourierIikoShift.id,
            )
            .where(
                CourierIikoShift.opened_at >= start_at,
                CourierIikoShift.opened_at < end_at,
            )
            .order_by(CourierIikoShift.opened_at)
        )
    ).all()

    shifts_by_courier: dict[uuid.UUID, list[CourierIikoShift]] = defaultdict(list)
    employee_by_id: dict[uuid.UUID, Employee] = {}
    category_by_courier: dict[uuid.UUID, str | None] = {}
    unmatched: list[dict[str, Any]] = []

    for shift, employee, match in rows:
        if employee is None or shift.employee_id is None:
            unmatched.append(
                {
                    "iiko_employee_id": shift.iiko_employee_id,
                    "opened_at": shift.opened_at.isoformat(),
                    "closed_at": shift.closed_at.isoformat() if shift.closed_at else None,
                    "is_open": shift.closed_at is None,
                }
            )
            continue
        shifts_by_courier[employee.id].append(shift)
        employee_by_id[employee.id] = employee
        category = _category_from_match(match)
        if category is not None and category_by_courier.get(employee.id) != "primary":
            category_by_courier[employee.id] = category
        category_by_courier.setdefault(employee.id, None)

    courier_ids = list(shifts_by_courier.keys())

    # Подмены плейсхолдеров за день: оценка/депозит/готовность считаются по реальному
    # курьеру, а не по плейсхолдер-карточке («Курьер 1»). Плейсхолдер без подмены закрыт
    # для действий и блокирует подтверждение дня, пока не указан реальный курьер.
    placeholder_ids = [
        courier_id
        for courier_id in courier_ids
        if employee_by_id[courier_id].is_courier_placeholder
    ]
    substitutions = await _substitutions_for_day(session, work_date, placeholder_ids)
    real_employees = await _load_employees_by_ids(
        session, {sub.real_employee_id for sub in substitutions.values()}
    )

    action_by_courier: dict[uuid.UUID, uuid.UUID | None] = {}
    action_employees: dict[uuid.UUID, Employee] = {}
    for courier_id in courier_ids:
        employee = employee_by_id[courier_id]
        if employee.is_courier_placeholder:
            sub = substitutions.get(courier_id)
            action_id = sub.real_employee_id if sub is not None else None
        else:
            action_id = courier_id
        action_by_courier[courier_id] = action_id
        if action_id is not None:
            action_employees[action_id] = (
                real_employees.get(action_id) or employee_by_id.get(action_id) or employee
            )

    action_ids = [action_id for action_id in {*action_by_courier.values()} if action_id is not None]
    evals = await _evaluations_for_day(session, action_ids, work_date)
    deposits = await _deposit_state_for_day(
        session, {action_id: action_employees[action_id] for action_id in action_ids}, work_date
    )
    reviews = await _reviews_for_day(session, action_ids, work_date)

    couriers: list[dict[str, Any]] = []
    ready_count = 0
    for courier_id in courier_ids:
        employee = employee_by_id[courier_id]
        is_placeholder = employee.is_courier_placeholder
        action_id = action_by_courier[courier_id]
        sub = substitutions.get(courier_id) if is_placeholder else None
        day_shifts = sorted(shifts_by_courier[courier_id], key=lambda item: item.opened_at)

        if action_id is not None:
            review = reviews.get(action_id)
            eval_info = evals.get(action_id)
            deposit = deposits[action_id]
            eval_present = eval_info is not None and eval_info["count"] > 0
            eval_skipped = bool(review.eval_skipped) if review else False
            deposit_present = deposit["present"]
            deposit_skipped = bool(review.deposit_skipped) if review else False
            deposit_collected = (
                deposit["target_cents"] > 0
                and deposit["balance_cents"] >= deposit["target_cents"]
            )
            ready = (eval_present or eval_skipped) and (
                deposit_collected or deposit_present or deposit_skipped
            )
            eval_count = eval_info["count"] if eval_info else 0
            latest_label = eval_info["label"] if eval_info else None
            latest_score = eval_info["score"] if eval_info else None
            deposit_balance_cents = deposit["balance_cents"]
            deposit_target_cents = deposit["target_cents"]
            deposit_skip_comment = review.deposit_skip_comment if review else None
        else:
            eval_present = eval_skipped = False
            deposit_present = deposit_skipped = deposit_collected = False
            ready = False
            eval_count = 0
            latest_label = None
            latest_score = None
            deposit_balance_cents = 0
            deposit_target_cents = 0
            deposit_skip_comment = None

        if ready:
            ready_count += 1

        if is_placeholder and sub is not None:
            real_employee = action_employees.get(action_id)
            display_name = real_employee.full_name if real_employee else employee.full_name
            substitution_info: dict[str, Any] | None = {
                "real_employee_id": sub.real_employee_id,
                "real_full_name": display_name,
            }
        else:
            display_name = employee.full_name
            substitution_info = None

        couriers.append(
            {
                "employee_id": courier_id,
                "action_employee_id": action_id,
                "full_name": display_name,
                "is_placeholder": is_placeholder,
                "substitution": substitution_info,
                "category": category_by_courier.get(courier_id),
                "shifts": [
                    {
                        "opened_at": shift.opened_at.isoformat(),
                        "closed_at": shift.closed_at.isoformat() if shift.closed_at else None,
                        "is_open": shift.closed_at is None,
                        "worked_minutes": worked_minutes_for_shift(shift),
                    }
                    for shift in day_shifts
                ],
                "eval_present": eval_present,
                "eval_skipped": eval_skipped,
                "eval_count": eval_count,
                "latest_eval_label": latest_label,
                "latest_eval_score": latest_score,
                "deposit_balance_cents": deposit_balance_cents,
                "deposit_target_cents": deposit_target_cents,
                "deposit_collected": deposit_collected,
                "deposit_present": deposit_present,
                "deposit_skipped": deposit_skipped,
                "deposit_skip_comment": deposit_skip_comment,
                "ready": ready,
            }
        )

    couriers.sort(key=lambda item: item["full_name"].lower())

    day = await session.get(CourierShiftDay, work_date)
    confirmed_by_name = None
    if day is not None and day.confirmed_by is not None:
        user = await session.get(User, day.confirmed_by)
        confirmed_by_name = user.full_name if user is not None else None

    total = len(couriers)
    can_confirm = total > 0 and ready_count == total

    return {
        "work_date": work_date,
        "status": day.status.value if day is not None else CourierShiftDayStatus.DRAFT.value,
        "confirmed_by": day.confirmed_by if day is not None else None,
        "confirmed_by_name": confirmed_by_name,
        "confirmed_at": day.confirmed_at if day is not None else None,
        "couriers": couriers,
        "unmatched": unmatched,
        "summary": {
            "total": total,
            "ready_count": ready_count,
            "can_confirm": can_confirm,
        },
    }


async def _evaluations_for_day(
    session: AsyncSession,
    courier_ids: list[uuid.UUID],
    work_date: date,
) -> dict[uuid.UUID, dict[str, Any]]:
    if not courier_ids:
        return {}
    rows = (
        await session.execute(
            select(CourierEvaluation, CourierEvaluationCriterion)
            .join(
                CourierEvaluationCriterion,
                CourierEvaluationCriterion.id == CourierEvaluation.criterion_id,
            )
            .where(
                CourierEvaluation.courier_employee_id.in_(courier_ids),
                CourierEvaluation.evaluated_at == work_date,
                CourierEvaluation.deleted_at.is_(None),
            )
            .order_by(CourierEvaluation.created_at.desc())
        )
    ).all()
    result: dict[uuid.UUID, dict[str, Any]] = {}
    for evaluation, criterion in rows:
        info = result.setdefault(
            evaluation.courier_employee_id,
            {"count": 0, "label": criterion.label, "score": evaluation.score_snapshot},
        )
        info["count"] += 1
    return result


async def _deposit_state_for_day(
    session: AsyncSession,
    employees: dict[uuid.UUID, Employee],
    work_date: date,
) -> dict[uuid.UUID, dict[str, Any]]:
    courier_ids = list(employees.keys())
    if not courier_ids:
        return {}
    settings = await get_deposit_settings(session)

    accounts = {
        account.employee_id: account
        for account in (
            await session.scalars(
                select(CourierDepositAccount).where(
                    CourierDepositAccount.employee_id.in_(courier_ids)
                )
            )
        ).all()
    }
    transactions = (
        await session.scalars(
            select(CourierDepositTransaction).where(
                CourierDepositTransaction.account_employee_id.in_(courier_ids)
            )
        )
    ).all()
    tx_by_courier: dict[uuid.UUID, list[CourierDepositTransaction]] = defaultdict(list)
    for tx in transactions:
        tx_by_courier[tx.account_employee_id].append(tx)

    result: dict[uuid.UUID, dict[str, Any]] = {}
    for courier_id in courier_ids:
        account = accounts.get(courier_id)
        opening = account.opening_balance_cents if account is not None else 0
        # целевой депозит — по должности (обычный / старший курьер), не из хранимого счёта
        target = target_cents_for_employee(settings, employees[courier_id])
        balance = opening
        present = False
        for tx in tx_by_courier.get(courier_id, []):
            tx_type = getattr(tx.transaction_type, "value", tx.transaction_type)
            if tx_type == CourierDepositTransactionType.TOP_UP.value:
                balance += tx.amount_cents
            else:
                balance -= tx.amount_cents
            if tx.transaction_date == work_date:
                present = True
        result[courier_id] = {
            "balance_cents": balance,
            "target_cents": target,
            "present": present,
        }
    return result


async def _reviews_for_day(
    session: AsyncSession,
    courier_ids: list[uuid.UUID],
    work_date: date,
) -> dict[uuid.UUID, CourierShiftReview]:
    if not courier_ids:
        return {}
    rows = (
        await session.scalars(
            select(CourierShiftReview).where(
                CourierShiftReview.work_date == work_date,
                CourierShiftReview.courier_employee_id.in_(courier_ids),
            )
        )
    ).all()
    return {row.courier_employee_id: row for row in rows}


async def _substitutions_for_day(
    session: AsyncSession,
    work_date: date,
    placeholder_ids: list[uuid.UUID],
) -> dict[uuid.UUID, CourierShiftSubstitution]:
    if not placeholder_ids:
        return {}
    rows = (
        await session.scalars(
            select(CourierShiftSubstitution).where(
                CourierShiftSubstitution.work_date == work_date,
                CourierShiftSubstitution.placeholder_employee_id.in_(placeholder_ids),
            )
        )
    ).all()
    return {row.placeholder_employee_id: row for row in rows}


async def _load_employees_by_ids(
    session: AsyncSession,
    ids: set[uuid.UUID],
) -> dict[uuid.UUID, Employee]:
    if not ids:
        return {}
    rows = (await session.scalars(select(Employee).where(Employee.id.in_(ids)))).all()
    return {employee.id: employee for employee in rows}


async def set_substitution(
    session: AsyncSession,
    work_date: date,
    placeholder_employee_id: uuid.UUID,
    real_employee_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> CourierShiftSubstitution:
    placeholder = await session.get(Employee, placeholder_employee_id)
    if placeholder is None or not placeholder.is_courier_placeholder:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Это не карточка-плейсхолдер",
        )
    if real_employee_id == placeholder_employee_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Выберите реального курьера, а не плейсхолдер",
        )
    real = await session.get(Employee, real_employee_id)
    if real is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Курьер не найден")
    if real.is_courier_placeholder:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Нельзя подменить одной плейсхолдер-карточкой другую",
        )
    if real.position not in courier_positions():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Подменить смену можно только курьером",
        )
    existing = await session.scalar(
        select(CourierShiftSubstitution).where(
            CourierShiftSubstitution.work_date == work_date,
            CourierShiftSubstitution.placeholder_employee_id == placeholder_employee_id,
        )
    )
    if existing is None:
        existing = CourierShiftSubstitution(
            work_date=work_date,
            placeholder_employee_id=placeholder_employee_id,
        )
        session.add(existing)
    existing.real_employee_id = real_employee_id
    existing.created_by = actor_id
    await session.flush()
    return existing


async def clear_substitution(
    session: AsyncSession,
    work_date: date,
    placeholder_employee_id: uuid.UUID,
) -> None:
    existing = await session.scalar(
        select(CourierShiftSubstitution).where(
            CourierShiftSubstitution.work_date == work_date,
            CourierShiftSubstitution.placeholder_employee_id == placeholder_employee_id,
        )
    )
    if existing is not None:
        await session.delete(existing)
        await session.flush()


async def set_review(
    session: AsyncSession,
    work_date: date,
    courier_employee_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    eval_skipped: bool | None = None,
    deposit_skipped: bool | None = None,
    deposit_skip_comment: str | None | object = UNSET,
) -> CourierShiftReview:
    employee = await session.get(Employee, courier_employee_id)
    if employee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Курьер не найден")

    review = await session.get(CourierShiftReview, (work_date, courier_employee_id))
    if review is None:
        review = CourierShiftReview(
            work_date=work_date,
            courier_employee_id=courier_employee_id,
        )
        session.add(review)

    if eval_skipped is not None:
        review.eval_skipped = eval_skipped
    if deposit_skipped is not None:
        review.deposit_skipped = deposit_skipped
        if not deposit_skipped:
            review.deposit_skip_comment = None
    if deposit_skip_comment is not UNSET:
        review.deposit_skip_comment = deposit_skip_comment  # type: ignore[assignment]

    if review.deposit_skipped and not (review.deposit_skip_comment or "").strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Укажите причину, почему депозит не собирается",
        )

    review.updated_by = actor_id
    review.updated_at = datetime.now(UTC)
    await session.flush()
    return review


async def confirm_day(
    session: AsyncSession,
    work_date: date,
    actor_id: uuid.UUID,
) -> dict[str, Any]:
    snapshot = await get_shift_day(session, work_date)
    if not snapshot["summary"]["can_confirm"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Нельзя сохранить смену: не у всех курьеров проставлены оценка и депозит",
        )
    day = await session.get(CourierShiftDay, work_date)
    if day is None:
        day = CourierShiftDay(work_date=work_date)
        session.add(day)
    day.status = CourierShiftDayStatus.CONFIRMED
    day.confirmed_by = actor_id
    day.confirmed_at = datetime.now(UTC)
    await session.flush()
    return await get_shift_day(session, work_date)


async def unconfirm_day(session: AsyncSession, work_date: date) -> dict[str, Any]:
    day = await session.get(CourierShiftDay, work_date)
    if day is not None:
        day.status = CourierShiftDayStatus.DRAFT
        day.confirmed_by = None
        day.confirmed_at = None
        await session.flush()
    return await get_shift_day(session, work_date)

"""Отложенная выдача депозита через ближайшую ЗП-ведомость (этап 4).

Сотрудник по умолчанию получает депозит не немедленно, а в столбце «Выдача депозита»
ближайшей ведомости (выплата — через Сейф-контур, этап 5). Намерение хранится в
``deposit_payout_schedule`` (status pending), переживает пересчёт ведомости. При расчёте
pending-план для каждого сотрудника превращается в сумму выдачи строки; при финализации
становится ``processed`` (с привязкой ``target_run_id``), при откате — обратно ``pending``.

За feature-флагом ``payroll.deposit_scheduled_payout_enabled`` (AppSetting; отсутствует =
выключено). Пока выключен — выдача остаётся немедленной (этапы 1–3), планирование недоступно.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AppSetting,
    DepositBankDraft,
    DepositPayoutSchedule,
    Employee,
    PayrollLine,
    PayrollRun,
)

DEPOSIT_SCHEDULED_PAYOUT_ENABLED_KEY = "payroll.deposit_scheduled_payout_enabled"
SCHEDULE_ACCOUNT_CHOICES = frozenset({"safe", "cash_tk", "bank_draft", "bank_draft_sber"})
ACTIVE_DEPOSIT_DRAFT_STATUSES = ("created", "updated", "paid")
ACTIVE_PAYROLL_RUN_STATUSES = ("running", "blocked", "completed")


class DepositPayoutConflictError(RuntimeError):
    pass


def _truthy(value: Any) -> bool:
    return value in (True, 1, "true", "True", "1")


async def is_scheduled_payout_enabled(session: AsyncSession) -> bool:
    """Включена ли отложенная выдача депозита (feature-флаг). Отсутствие ключа = выключено."""
    value = await session.scalar(
        select(AppSetting.value).where(AppSetting.key == DEPOSIT_SCHEDULED_PAYOUT_ENABLED_KEY)
    )
    return _truthy(value)


async def load_pending_schedules(
    session: AsyncSession,
    employee_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, DepositPayoutSchedule]:
    """Pending-планы выдачи для указанных сотрудников (по одному на сотрудника)."""
    employee_ids = set(employee_ids)
    if not employee_ids:
        return {}
    rows = await session.scalars(
        select(DepositPayoutSchedule).where(
            DepositPayoutSchedule.employee_id.in_(employee_ids),
            DepositPayoutSchedule.status == "pending",
        )
    )
    return {row.employee_id: row for row in rows.all()}


async def get_pending_schedule(
    session: AsyncSession,
    employee_id: uuid.UUID,
) -> DepositPayoutSchedule | None:
    return await session.scalar(
        select(DepositPayoutSchedule).where(
            DepositPayoutSchedule.employee_id == employee_id,
            DepositPayoutSchedule.status == "pending",
        )
    )


async def create_or_replace_schedule(
    session: AsyncSession,
    employee_id: uuid.UUID,
    *,
    requested_amount: Decimal | None,
    account_choice: str,
    created_by_user_id: uuid.UUID | None,
    target_period_id: uuid.UUID | None = None,
) -> DepositPayoutSchedule:
    """Запланировать выдачу депозита в ведомости (один pending на сотрудника).

    Повторный вызов обновляет существующий pending-план (сумму/счёт/период). ``target_period_id``
    задаёт конкретную ведомость (увольнение «через ведомость»); ``None`` — плавающий план,
    берётся в ближайший расчёт. ``target_run_id`` заполняется при финализации.
    """
    await _lock_employee(session, employee_id)
    active_draft = await session.scalar(
        select(DepositBankDraft.id).where(
            DepositBankDraft.employee_id == employee_id,
            DepositBankDraft.status.in_(ACTIVE_DEPOSIT_DRAFT_STATUSES),
        )
    )
    if active_draft is not None:
        raise DepositPayoutConflictError(
            "По сотруднику уже отправлен отдельный банковский платёж депозита. "
            "Отмените его перед выдачей депозита через зарплатную ведомость."
        )
    if account_choice not in SCHEDULE_ACCOUNT_CHOICES:
        account_choice = "safe"
    existing = await get_pending_schedule(session, employee_id)
    now = datetime.now(UTC)
    if existing is not None:
        existing.requested_amount = requested_amount
        existing.account_choice = account_choice
        existing.target_period_id = target_period_id
        existing.created_by_user_id = created_by_user_id
        existing.created_at = now
        await session.flush()
        return existing
    schedule = DepositPayoutSchedule(
        id=uuid.uuid4(),
        employee_id=employee_id,
        target_run_id=None,
        target_period_id=target_period_id,
        requested_amount=requested_amount,
        account_choice=account_choice,
        status="pending",
        created_by_user_id=created_by_user_id,
        created_at=now,
    )
    session.add(schedule)
    await session.flush()
    return schedule


async def assert_no_payroll_deposit_payout(
    session: AsyncSession,
    employee_id: uuid.UUID,
) -> None:
    """Block a standalone payout while payroll already owns the same deposit amount.

    The employee row is locked so a concurrent schedule and standalone bank draft cannot both
    pass their checks and commit into different tables.
    """
    await _lock_employee(session, employee_id)
    pending_schedule = await session.scalar(
        select(DepositPayoutSchedule.id).where(
            DepositPayoutSchedule.employee_id == employee_id,
            DepositPayoutSchedule.status == "pending",
        )
    )
    active_line_amount = await session.scalar(
        select(func.coalesce(func.sum(PayrollLine.deposit_payout_scheduled), 0))
        .join(PayrollRun, PayrollRun.id == PayrollLine.run_id)
        .where(
            PayrollLine.employee_id == employee_id,
            PayrollLine.deposit_payout_scheduled > 0,
            PayrollRun.status.in_(ACTIVE_PAYROLL_RUN_STATUSES),
        )
    )
    if pending_schedule is not None or Decimal(str(active_line_amount or 0)) > 0:
        raise DepositPayoutConflictError(
            "Депозит уже включён или запланирован в зарплатной ведомости. "
            "Отмените выдачу через ведомость и пересчитайте её перед отдельной выплатой."
        )


async def assert_run_has_no_standalone_deposit_drafts(
    session: AsyncSession,
    run_id: uuid.UUID,
) -> None:
    """Block payroll finalization/bank draft if an included deposit has its own bank draft."""
    employee_ids = sorted(
        set(
            (
                await session.scalars(
                    select(PayrollLine.employee_id).where(
                        PayrollLine.run_id == run_id,
                        PayrollLine.deposit_payout_scheduled > 0,
                    )
                )
            ).all()
        ),
        key=str,
    )
    if not employee_ids:
        return

    # Same lock as create_or_replace_schedule/assert_no_payroll_deposit_payout. Holding it until
    # the payroll operation commits closes the cross-table race with a standalone bank draft.
    (
        await session.scalars(
            select(Employee.id)
            .where(Employee.id.in_(employee_ids))
            .order_by(Employee.id)
            .with_for_update()
        )
    ).all()
    rows = (
        await session.execute(
            select(Employee.full_name, DepositBankDraft.amount)
            .join(DepositBankDraft, DepositBankDraft.employee_id == Employee.id)
            .where(
                Employee.id.in_(employee_ids),
                DepositBankDraft.status.in_(ACTIVE_DEPOSIT_DRAFT_STATUSES),
            )
            .order_by(Employee.full_name)
        )
    ).all()
    if not rows:
        return
    details = ", ".join(f"{name} — {Decimal(str(amount)):.2f} ₽" for name, amount in rows)
    raise DepositPayoutConflictError(
        "В ведомость уже включён депозит, по которому существует отдельный банковский платёж: "
        f"{details}. Отмените один из способов выплаты."
    )


async def _lock_employee(session: AsyncSession, employee_id: uuid.UUID) -> None:
    await session.scalar(select(Employee.id).where(Employee.id == employee_id).with_for_update())


async def load_period_schedules(
    session: AsyncSession,
    period_id: uuid.UUID,
) -> dict[uuid.UUID, DepositPayoutSchedule]:
    """Pending-планы выдачи, привязанные к конкретной ведомости (увольнение «через ведомость»).

    Возвращает по сотруднику — включая уволенных без явок: их надо добрать в расчёт периода.
    """
    rows = await session.scalars(
        select(DepositPayoutSchedule).where(
            DepositPayoutSchedule.target_period_id == period_id,
            DepositPayoutSchedule.status == "pending",
        )
    )
    return {row.employee_id: row for row in rows.all()}


async def cancel_pending_schedule(session: AsyncSession, employee_id: uuid.UUID) -> bool:
    """Отменить запланированную выдачу (сотрудник передумал увольняться). True, если был pending."""
    schedule = await get_pending_schedule(session, employee_id)
    if schedule is None:
        return False
    schedule.status = "cancelled"
    await session.flush()
    return True


async def mark_schedules_processed_for_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    employee_ids: Iterable[uuid.UUID],
) -> int:
    """Финализация: pending-планы выплаченных в этом прогоне сотрудников → processed."""
    schedules = await load_pending_schedules(session, employee_ids)
    count = 0
    for schedule in schedules.values():
        schedule.status = "processed"
        schedule.target_run_id = run_id
        count += 1
    if count:
        await session.flush()
    return count


async def revert_schedules_for_run(session: AsyncSession, run_id: uuid.UUID) -> int:
    """Откат финализации: processed-планы этого прогона → обратно pending."""
    rows = await session.scalars(
        select(DepositPayoutSchedule).where(
            DepositPayoutSchedule.target_run_id == run_id,
            DepositPayoutSchedule.status == "processed",
        )
    )
    count = 0
    for schedule in rows.all():
        schedule.status = "pending"
        schedule.target_run_id = None
        count += 1
    if count:
        await session.flush()
    return count

"""Посменная выдача внештатника из кассы (вариант Б): список по людям + выдача по сменам.

Данные берём из ТОГО ЖЕ пайплайна, что кормит авансы производственников: явки
``attendance_entry`` открытого недельного периода, которые грузит ночное окно
``refresh_current_week_advance_window`` (scheduler ~00:30 МСК). Своего iiko-синка и
таблицы ``shift_ledger_entry`` этот контур больше НЕ держит.

Единица смены внештатника = его ``attendance_entry`` открытого периода. Сумма смены =
``freelancer_shift_amount(ставка_карточки, minutes)`` (та же формула, что в калькуляторе
ЗП). «Непогашенные» смены НЕ персистятся — выводятся на лету: явки открытого периода
МИНУС уже оплаченные налом (строки ``FreelancerShiftSettlement`` со статусом ``paid_cash``).

Контур:

1. ``sync_freelancer_shifts`` — «Синхронизировать смены»: дёргает
   ``refresh_current_week_advance_window`` (перечитать явки текущей недели из iiko) и
   возвращает обновлённый список внештатников с непогашенным.
2. ``list_unpaid_freelancers`` — одна строка на внештатника (имя + Σ неоплаченных смен).
3. ``freelancer_shift_details`` — смены открытого периода конкретного внештатника
   (для модалки): дата · часы · сумма · оплачена ли.
4. ``pay_freelancer_shifts`` — выдать наличные из ТК Черникова за выбранные явки; статью
   «Зарплата производственного персонала» пишет движок (не свободная форма кассы).

Сверка с ведомостью (без двойной выплаты) — в ``payroll_freelancer_settlement``.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AttendanceEntry,
    CashflowTransaction,
    DdsArticle,
    Employee,
    FreelancerShiftSettlement,
    PayrollPeriod,
)
from app.services.payroll_advance_availability import _open_weekly_period
from app.services.payroll_calculator import category_rule_key, freelancer_shift_amount, money

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
_CENTS = Decimal("0.01")

# Источник посменной выдачи внештатника (движковая проводка, не свободная форма кассы).
FREELANCER_SHIFT_PAYOUT_SOURCE_KIND = "freelancer_shift_payout"
# Статья ДДС — та же, что у производственной ЗП (защищена от свободной формы кассы;
# здесь её пишет движок, поэтому предохранитель не мешает).
FREELANCER_SHIFT_ARTICLE_CODE = "zarplata_proizvodstvennogo_personala"


class FreelancerShiftSettlementError(Exception):
    """Выдача посменного расчёта невозможна (валидация, состояние, гонки)."""


@dataclass(slots=True)
class _Shift:
    """Смена внештатника открытого периода (единица = явка)."""

    attendance_entry_id: uuid.UUID
    employee_id: uuid.UUID
    employee_name: str
    work_date: date
    minutes: int
    amount: Decimal
    paid: bool


def _shift_amount(rate: Any, minutes: int) -> Decimal:
    """Сумма смены в копейках (та же формула, что в калькуляторе ЗП)."""
    return freelancer_shift_amount(rate, minutes).quantize(_CENTS, rounding=ROUND_HALF_UP)


def _is_freelancer(employee: Employee) -> bool:
    """Внештатник: временная карточка пула ИЛИ категория карточки = код «6»."""
    return bool(getattr(employee, "is_freelancer_temp", False)) or (
        category_rule_key(employee.category) == "6"
    )


async def _collect_open_period_shifts(
    session: AsyncSession,
    *,
    as_of: date | None = None,
    employee_id: uuid.UUID | None = None,
) -> tuple[PayrollPeriod | None, list[_Shift]]:
    """Смены внештатников открытого недельного периода (с пометкой «оплачена налом»).

    Единица смены — явка ``attendance_entry`` с завершённым интервалом (``ended_at``)
    и ненулевыми минутами; ставка карточки > 0 (иначе сумма 0 — смену не показываем/не
    берём в оплату). Открытый период определяется как в авансовом механизме
    (``_open_weekly_period``): недельный, не финализированный. Нет периода → пустой список.
    """
    as_of = as_of or datetime.now(MOSCOW_TZ).date()
    period = await _open_weekly_period(session, as_of)
    if period is None:
        return None, []

    conditions = [
        AttendanceEntry.period_id == period.id,
        AttendanceEntry.ended_at.is_not(None),
        AttendanceEntry.minutes_worked > 0,
    ]
    if employee_id is not None:
        conditions.append(AttendanceEntry.employee_id == employee_id)
    rows = (
        await session.execute(
            select(AttendanceEntry, Employee)
            .join(Employee, Employee.id == AttendanceEntry.employee_id)
            .where(*conditions)
            .order_by(AttendanceEntry.work_date, AttendanceEntry.started_at)
        )
    ).all()
    entries = [(entry, employee) for entry, employee in rows if _is_freelancer(employee)]
    if not entries:
        return period, []

    entry_ids = {entry.id for entry, _ in entries}
    paid_ids = set(
        (
            await session.scalars(
                select(FreelancerShiftSettlement.attendance_entry_id).where(
                    FreelancerShiftSettlement.attendance_entry_id.in_(entry_ids),
                    FreelancerShiftSettlement.status == "paid_cash",
                )
            )
        ).all()
    )

    shifts: list[_Shift] = []
    for entry, employee in entries:
        amount = _shift_amount(getattr(employee, "freelancer_shift_rate", None), entry.minutes_worked)
        # Ставка 0 → сумма 0: смену не показываем и не берём в оплату (см. граничные случаи).
        if amount <= 0:
            continue
        shifts.append(
            _Shift(
                attendance_entry_id=entry.id,
                employee_id=employee.id,
                employee_name=employee.full_name,
                work_date=entry.work_date,
                minutes=entry.minutes_worked,
                amount=amount,
                paid=entry.id in paid_ids,
            )
        )
    return period, shifts


async def list_unpaid_freelancers(
    session: AsyncSession, *, as_of: date | None = None
) -> list[dict[str, Any]]:
    """Одна строка на внештатника с непогашенным: имя + Σ неоплаченных смен открытого периода.

    Внештатники без неоплаченных смен (всё выдано / нет явок) в список не попадают.
    """
    _, shifts = await _collect_open_period_shifts(session, as_of=as_of)
    totals: dict[uuid.UUID, dict[str, Any]] = {}
    for shift in shifts:
        if shift.paid:
            continue
        bucket = totals.setdefault(
            shift.employee_id,
            {"employee_id": shift.employee_id, "name": shift.employee_name, "unpaid_total": Decimal("0"), "shift_count": 0},
        )
        bucket["unpaid_total"] += shift.amount
        bucket["shift_count"] += 1
    result = [
        {
            "employee_id": bucket["employee_id"],
            "name": bucket["name"],
            "unpaid_total": money(bucket["unpaid_total"]),
            "shift_count": bucket["shift_count"],
        }
        for bucket in totals.values()
    ]
    result.sort(key=lambda row: row["name"] or "")
    return result


async def freelancer_shift_details(
    session: AsyncSession, employee_id: uuid.UUID, *, as_of: date | None = None
) -> list[dict[str, Any]]:
    """Смены внештатника открытого периода для модалки: дата · часы · сумма · оплачена ли.

    Оплаченные помечены ``paid=true`` (в модалке — как выданные/недоступные).
    """
    _, shifts = await _collect_open_period_shifts(session, as_of=as_of, employee_id=employee_id)
    return [
        {
            "attendance_entry_id": shift.attendance_entry_id,
            "work_date": shift.work_date,
            "hours": round(shift.minutes / 60, 2),
            "amount": money(shift.amount),
            "paid": shift.paid,
        }
        for shift in shifts
    ]


async def sync_freelancer_shifts(
    session: AsyncSession, *, today: date | None = None
) -> dict[str, Any]:
    """«Синхронизировать смены»: перечитать явки текущей недели из iiko + вернуть список.

    Переиспуем ночное окно авансов (``refresh_current_week_advance_window``): оно
    гарантирует недельный период и грузит явки iiko за отработанные дни. Отдельного
    iiko-синка у кассового контура нет.
    """
    from app.services.payroll_runner import refresh_current_week_advance_window

    await refresh_current_week_advance_window(session, today=today)
    return {"freelancers": await list_unpaid_freelancers(session, as_of=today)}


async def pay_freelancer_shifts(
    session: AsyncSession,
    *,
    attendance_entry_ids: Sequence[uuid.UUID],
    actor_user_id: uuid.UUID | None = None,
    today: date | None = None,
) -> list[uuid.UUID]:
    """«Выплатить»: выдать наличные из ТК Черникова за выбранные смены (каждую целиком).

    Атомарно за запрос: все явки должны быть внештатными сменами открытого периода и ещё
    НЕ оплаченными налом (иначе 409, ничего не выдаём — повторная оплата смены исключена
    уникальностью ``attendance_entry_id``). На каждую смену — своя out-проводка ДДС по
    статье производственной ЗП (движок пишет статью сам; сумма — учётная, админ не вводит).
    Сумма больше остатка кассы НЕ блокируется (предупреждение показывает фронт).
    Возвращает id созданных проводок.
    """
    ids: list[uuid.UUID] = list(dict.fromkeys(attendance_entry_ids))
    if not ids:
        raise FreelancerShiftSettlementError("Не выбрано ни одной смены")

    from app.services.kassa.payouts import KassaPayoutError, get_kassa_wallet, kassa_today

    period, shifts = await _collect_open_period_shifts(session, as_of=today)
    if period is None:
        raise FreelancerShiftSettlementError("Нет открытого недельного периода")
    by_entry = {shift.attendance_entry_id: shift for shift in shifts}
    chosen: list[_Shift] = []
    for entry_id in ids:
        shift = by_entry.get(entry_id)
        if shift is None:
            raise FreelancerShiftSettlementError("Смена не найдена среди выдаваемых из кассы")
        if shift.paid:
            raise FreelancerShiftSettlementError("Смена уже выдана наличными")
        chosen.append(shift)

    try:
        wallet = await get_kassa_wallet(session)
    except KassaPayoutError as exc:
        raise FreelancerShiftSettlementError(str(exc)) from exc

    article_id = await session.scalar(
        select(DdsArticle.id).where(DdsArticle.code == FREELANCER_SHIFT_ARTICLE_CODE)
    )
    if article_id is None:
        raise FreelancerShiftSettlementError(
            "Статья «Зарплата производственного персонала» не найдена"
        )

    now = datetime.now(UTC)
    operation_date = kassa_today()
    transaction_ids: list[uuid.UUID] = []
    for shift in chosen:
        transaction = CashflowTransaction(
            wallet_id=wallet.id,
            direction="out",
            amount=shift.amount,
            operation_date=operation_date,
            article_id=article_id,
            source_kind=FREELANCER_SHIFT_PAYOUT_SOURCE_KIND,
            source_id=shift.attendance_entry_id,
            payment_purpose=(
                f"Внештат {shift.employee_name}: смена {shift.work_date.isoformat()}"
            ),
            quality_status="final",
            created_by_user_id=actor_user_id,
        )
        session.add(transaction)
        await session.flush()
        session.add(
            FreelancerShiftSettlement(
                employee_id=shift.employee_id,
                attendance_entry_id=shift.attendance_entry_id,
                period_id=period.id,
                work_date=shift.work_date,
                minutes=shift.minutes,
                amount=shift.amount,
                status="paid_cash",
                cashflow_transaction_id=transaction.id,
                paid_at=now,
                paid_by_user_id=actor_user_id,
            )
        )
        transaction_ids.append(transaction.id)

    await session.commit()
    return transaction_ids

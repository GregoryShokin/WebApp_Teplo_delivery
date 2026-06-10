"""Выдача и ведение авансов/займов сотрудников (write-путь).

Выдача классифицируется по доступному (earned-to-date из
`payroll_advance_availability`): сумма ≤ доступного → аванс (право A, гасится в
ближайшей ведомости разом); сумма > доступного → заём (право B, гасится в
рассрочку N равных долей). `allow_loan` отражает наличие права B у инициатора —
без него выдача сверх заработанного отклоняется.

MVP-упрощение: одна выдача = одна запись `SalaryAdvance` целиком (без расщепления
суммы на аванс-часть + заём-часть). Если сумма превышает доступное, ВСЯ сумма
оформляется займом с рассрочкой.

Запись `SalaryAdvance` — источник истины и факт выплаты (нал/перевод) для ДДС.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting, Employee, PayrollPeriod, SalaryAdvance
from app.services.employee_effective_events import get_position_on_date
from app.services.payroll_adjustment_service import ADMIN_PAYROLL_POSITIONS
from app.services.payroll_admin import _upsert_setting
from app.services.payroll_advance_availability import available_to_advance
from app.services.payroll_calculator import decimal
from app.services.payroll_runner import PayrollConflictError, PayrollNotFoundError

_CENTS = Decimal("0.01")
PAYOUT_METHODS = ("business_card", "cash", "transfer", "other")

LOAN_MAX_KEY = "payroll.loan_max_amount"
# Заглушка-дефолт: владелец задаёт реальный потолок в «Настройках».
LOAN_MAX_DEFAULT = Decimal("100000")


async def get_loan_max(session: AsyncSession) -> Decimal:
    """Потолок займа (₽) — настраиваемый в «Настройках», иначе дефолт."""
    setting = await session.scalar(select(AppSetting).where(AppSetting.key == LOAN_MAX_KEY))
    if setting is None or setting.value is None:
        return LOAN_MAX_DEFAULT
    try:
        value = decimal(setting.value)
    except (TypeError, ValueError, ArithmeticError):
        return LOAN_MAX_DEFAULT
    return value if value > 0 else LOAN_MAX_DEFAULT


async def set_loan_max(session: AsyncSession, amount: Decimal) -> Decimal:
    amount_dec = decimal(amount)
    if amount_dec <= 0:
        raise PayrollConflictError("Потолок займа должен быть больше нуля")
    await _upsert_setting(
        session,
        key=LOAN_MAX_KEY,
        value=float(amount_dec),
        value_type="decimal",
        display_name="Потолок займа, ₽",
        unit="₽",
    )
    return amount_dec


async def _outstanding_loan_principal(
    session: AsyncSession, employee_id: uuid.UUID
) -> Decimal:
    """Сумма непогашенного тела действующих займов сотрудника (для потолка)."""
    rows = (
        await session.scalars(
            select(SalaryAdvance).where(
                SalaryAdvance.employee_id == employee_id,
                SalaryAdvance.kind == "loan",
                SalaryAdvance.status == "issued",
            )
        )
    ).all()
    return sum(
        (decimal(row.amount) - decimal(row.recovered_amount) for row in rows),
        Decimal("0"),
    )


async def _date_in_finalized_period(
    session: AsyncSession, on_date: date, *, admin: bool
) -> bool:
    """Покрыта ли дата финализированной ведомостью соответствующего пайплайна."""
    period_type = "half_month" if admin else "week"
    locked = await session.scalar(
        select(PayrollPeriod.id).where(
            PayrollPeriod.period_type == period_type,
            PayrollPeriod.status == "finalized",
            PayrollPeriod.start_date <= on_date,
            PayrollPeriod.end_date >= on_date,
        )
    )
    return locked is not None


def _per_installment(amount: Decimal, count: int) -> Decimal:
    """Размер равной доли (остаток копеек добирается на возврате, до остатка баланса)."""
    count = max(int(count), 1)
    return (amount / Decimal(count)).quantize(_CENTS, rounding=ROUND_HALF_UP)


async def issue_advance(
    session: AsyncSession,
    *,
    employee_id: uuid.UUID,
    amount: Decimal,
    allow_loan: bool,
    override_ceiling: bool = False,
    issued_on: date | None = None,
    payout_method: str | None = None,
    installments_count: int = 1,
    comment: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    actor_label: str | None = None,
) -> SalaryAdvance:
    employee = await session.get(Employee, employee_id)
    if employee is None:
        raise PayrollNotFoundError("Сотрудник не найден")
    issued_on = issued_on or datetime.now(UTC).date()
    amount = decimal(amount).quantize(_CENTS)
    if amount <= 0:
        raise PayrollConflictError("Сумма должна быть больше нуля")
    if payout_method is not None and payout_method not in PAYOUT_METHODS:
        raise PayrollConflictError("Неизвестный способ выплаты")

    availability = await available_to_advance(session, employee, issued_on)
    role = await get_position_on_date(session, employee_id, issued_on)
    role = role or employee.position or ""
    if await _date_in_finalized_period(
        session, issued_on, admin=role in ADMIN_PAYROLL_POSITIONS
    ):
        raise PayrollConflictError(
            "Период этой даты уже финализирован — выдача в закрытую ведомость невозможна"
        )

    if amount <= availability.available:
        kind = "advance"
        installments = 1
    else:
        if not allow_loan:
            detail = (
                availability.note
                or f"Сумма превышает доступный аванс ({availability.available} ₽)"
            )
            raise PayrollConflictError(f"{detail}. Выдача займа требует права на займы.")
        if not override_ceiling:
            ceiling = await get_loan_max(session)
            outstanding = await _outstanding_loan_principal(session, employee_id)
            if outstanding + amount > ceiling:
                raise PayrollConflictError(
                    f"Заём превышает потолок {ceiling} ₽ "
                    f"(непогашено по займам {outstanding} ₽). Требуется подтверждение превышения."
                )
        kind = "loan"
        installments = max(int(installments_count), 1)

    advance = SalaryAdvance(
        employee_id=employee_id,
        role=role,
        kind=kind,
        amount=amount,
        per_installment_amount=_per_installment(amount, installments),
        installments_count=installments,
        recovered_amount=Decimal("0"),
        status="issued",
        issued_on=issued_on,
        payout_method=payout_method,
        comment=comment,
        created_by_user_id=actor_user_id,
        created_by_label=actor_label,
    )
    session.add(advance)
    await session.commit()
    await session.refresh(advance)
    return advance


async def cancel_advance(
    session: AsyncSession,
    advance_id: uuid.UUID,
) -> SalaryAdvance:
    """Отменить выдачу (до начала возврата). Нельзя, если уже что-то удержано."""
    advance = await session.get(SalaryAdvance, advance_id)
    if advance is None:
        raise PayrollNotFoundError("Аванс не найден")
    if decimal(advance.recovered_amount) > 0:
        raise PayrollConflictError("По авансу уже есть удержания — отмена невозможна")
    if advance.status != "issued":
        raise PayrollConflictError("Аванс не в статусе «выдан»")
    advance.status = "cancelled"
    await session.commit()
    await session.refresh(advance)
    return advance


async def write_off_advance(
    session: AsyncSession,
    advance_id: uuid.UUID,
    *,
    reason: str | None = None,
) -> SalaryAdvance:
    """Списать непогашенный остаток (решение владельца, напр. при увольнении).

    Помечает аванс `written_off` — сейм возврата его больше не подхватывает
    (выбирает только `issued`). Удержанная часть остаётся, остаток = списанный убыток.
    """
    advance = await session.get(SalaryAdvance, advance_id)
    if advance is None:
        raise PayrollNotFoundError("Аванс не найден")
    if advance.status != "issued":
        raise PayrollConflictError("Списать можно только непогашенный (выданный) аванс")
    advance.status = "written_off"
    if reason:
        suffix = f"Списание: {reason}"
        advance.comment = f"{advance.comment} | {suffix}" if advance.comment else suffix
    await session.commit()
    await session.refresh(advance)
    return advance


async def list_advances(
    session: AsyncSession,
    *,
    employee_id: uuid.UUID | None = None,
    statuses: tuple[str, ...] | None = None,
) -> list[SalaryAdvance]:
    """Реестр авансов/займов: по сотруднику и/или статусам, новые сверху."""
    stmt = select(SalaryAdvance)
    if employee_id is not None:
        stmt = stmt.where(SalaryAdvance.employee_id == employee_id)
    if statuses:
        stmt = stmt.where(SalaryAdvance.status.in_(statuses))
    stmt = stmt.order_by(SalaryAdvance.issued_on.desc(), SalaryAdvance.created_at.desc())
    return list((await session.scalars(stmt)).all())

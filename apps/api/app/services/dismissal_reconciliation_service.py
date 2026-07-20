"""Отложенное увольнение: проверка расчётов и авто-перевод ``dismissing`` → ``inactive``.

Сотрудник, которого «уволили», держится в статусе ``dismissing`` до закрытия ВСЕХ
взаимных расчётов. Как только по нему нет долгов ни в одну сторону — депозит выдан
или списан (balance=0), вся заработанная ЗП выплачена, займы/авансы погашены,
отложенные штрафы ревизии применены — система переводит его в ``inactive``.

Предикаты заземлены на существующие механизмы:
- депозит: ``DepositAccount.balance`` + отсутствие pending ``DepositPayoutSchedule``;
- ЗП: нет финализированной ведомости с неоплаченной выплатой и нет отработанных дней
  вне финализированной ведомости (``ShiftLedgerEntry`` vs период финализированного прогона);
- займы/авансы: ``SalaryAdvance`` с непогашенным остатком (kind loan/advance);
- ревизия: ``DeferredAuditCharge`` со статусом pending/partially_applied по сотруднику.

Перевод в ``inactive`` идёт только через ``reconcile_dismissing_employee`` — статус
``dismissing`` «липкий» для iiko-синка (см. ``employee_status.compute_status``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentActor
from app.models import (
    AgentAction,
    AgentRun,
    DeferredAuditCharge,
    DeferredAuditChargeRecipient,
    DepositAccount,
    DepositBankDraft,
    DepositPayoutSchedule,
    Employee,
    EmployeePayout,
    PayrollLine,
    PayrollPayment,
    PayrollPeriod,
    PayrollRun,
    SalaryAdvance,
    ShiftLedgerEntry,
)

_MONEY_EPSILON = Decimal("0.01")


@dataclass(frozen=True)
class SettlementState:
    """Состояние расчётов сотрудника для отложенного увольнения."""

    deposit_settled: bool
    payroll_settled: bool
    advances_settled: bool
    revision_settled: bool
    payouts_settled: bool

    @property
    def fully_settled(self) -> bool:
        return (
            self.deposit_settled
            and self.payroll_settled
            and self.advances_settled
            and self.revision_settled
            and self.payouts_settled
        )

    def outstanding(self) -> list[str]:
        """Список незакрытых зон (для UI/лога «почему ещё не уволен»)."""
        pending: list[str] = []
        if not self.deposit_settled:
            pending.append("депозит")
        if not self.payroll_settled:
            pending.append("невыплаченная ЗП")
        if not self.advances_settled:
            pending.append("займы/авансы")
        if not self.revision_settled:
            pending.append("штрафы ревизии")
        if not self.payouts_settled:
            pending.append("разовые выплаты")
        return pending


async def _deposit_settled(session: AsyncSession, employee_id: uuid.UUID) -> bool:
    balance = await session.scalar(
        select(DepositAccount.balance).where(DepositAccount.employee_id == employee_id)
    )
    if balance is not None and Decimal(balance) > _MONEY_EPSILON:
        return False
    # Висящий банк-черновик выдачи депозита (created/updated/paid) = деньги ещё в пути / резервом
    # на Сейфе, сотруднику не выданы → расчёт не закрыт (баланс обычно >0 и так, но подстрахуемся).
    has_active_draft = await session.scalar(
        select(
            exists().where(
                DepositBankDraft.employee_id == employee_id,
                DepositBankDraft.status.in_(("created", "updated", "paid")),
            )
        )
    )
    if bool(has_active_draft):
        return False
    # Забронированная, но ещё не проведённая выдача депозита в ведомость = деньги
    # не двинулись → расчёт не закрыт.
    has_pending_schedule = await session.scalar(
        select(
            exists().where(
                DepositPayoutSchedule.employee_id == employee_id,
                DepositPayoutSchedule.status == "pending",
            )
        )
    )
    return not bool(has_pending_schedule)


async def _payroll_settled(session: AsyncSession, employee_id: uuid.UUID) -> bool:
    # 1) Нет финализированной ведомости с положительным нетто и неоплаченной выплатой.
    run_nets = (
        await session.execute(
            select(PayrollLine.run_id, PayrollLine.total_payable)
            .join(PayrollRun, PayrollRun.id == PayrollLine.run_id)
            .where(
                PayrollRun.status == "finalized",
                PayrollLine.employee_id == employee_id,
            )
        )
    ).all()
    net_by_run: dict[uuid.UUID, Decimal] = {}
    for run_id, total_payable in run_nets:
        net_by_run[run_id] = net_by_run.get(run_id, Decimal("0")) + Decimal(total_payable or 0)
    for run_id, net in net_by_run.items():
        if net <= 0:
            continue
        payment_status = await session.scalar(
            select(PayrollPayment.status).where(
                PayrollPayment.run_id == run_id,
                PayrollPayment.employee_id == employee_id,
            )
        )
        if payment_status != "paid":
            return False

    # 2) Нет отработанных дней вне периода финализированного прогона (заработанное,
    #    что ещё не попало ни в одну финализированную ведомость).
    covered_by_finalized = (
        select(1)
        .select_from(PayrollRun)
        .join(PayrollPeriod, PayrollPeriod.id == PayrollRun.period_id)
        .where(
            PayrollRun.status == "finalized",
            PayrollPeriod.start_date <= ShiftLedgerEntry.work_date,
            PayrollPeriod.end_date >= ShiftLedgerEntry.work_date,
        )
        .exists()
    )
    has_uncovered_work = await session.scalar(
        select(
            exists().where(
                ShiftLedgerEntry.employee_id == employee_id,
                ~covered_by_finalized,
            )
        )
    )
    return not bool(has_uncovered_work)


async def _advances_settled(session: AsyncSession, employee_id: uuid.UUID) -> bool:
    # Непогашенный остаток действующего займа/аванса (issued/awaiting_payout).
    rows = (
        await session.execute(
            select(SalaryAdvance.amount, SalaryAdvance.recovered_amount).where(
                SalaryAdvance.employee_id == employee_id,
                SalaryAdvance.kind.in_(("loan", "advance")),
                SalaryAdvance.status.in_(("issued", "awaiting_payout")),
            )
        )
    ).all()
    for amount, recovered in rows:
        if Decimal(amount) - Decimal(recovered or 0) > _MONEY_EPSILON:
            return False
    return True


async def _revision_settled(session: AsyncSession, employee_id: uuid.UUID) -> bool:
    open_charge = (
        select(1)
        .select_from(DeferredAuditChargeRecipient)
        .join(
            DeferredAuditCharge,
            DeferredAuditCharge.id == DeferredAuditChargeRecipient.charge_id,
        )
        .where(
            DeferredAuditChargeRecipient.employee_id == employee_id,
            DeferredAuditCharge.status.in_(("pending", "partially_applied")),
        )
        .exists()
    )
    has_open_charge = await session.scalar(select(open_charge))
    return not bool(has_open_charge)


async def _payouts_settled(session: AsyncSession, employee_id: uuid.UUID) -> bool:
    # Разовые выплаты вне ведомости (EmployeePayout): фрилансерские «К выдаче»,
    # выплаты собственнику и т.п. `pending`/`included` = мы ещё должны и не выдали
    # → долг не закрыт. `paid`/`failed`/`cancelled`/`draft` — не блокируют.
    has_unpaid_payout = await session.scalar(
        select(
            exists().where(
                EmployeePayout.employee_id == employee_id,
                EmployeePayout.status.in_(("pending", "included")),
            )
        )
    )
    return not bool(has_unpaid_payout)


async def settlement_state(
    session: AsyncSession, employee_id: uuid.UUID
) -> SettlementState:
    return SettlementState(
        deposit_settled=await _deposit_settled(session, employee_id),
        payroll_settled=await _payroll_settled(session, employee_id),
        advances_settled=await _advances_settled(session, employee_id),
        revision_settled=await _revision_settled(session, employee_id),
        payouts_settled=await _payouts_settled(session, employee_id),
    )


async def _write_reconcile_audit(
    session: AsyncSession,
    *,
    employee: Employee,
    before: dict,
    after: dict,
    actor: CurrentActor | None,
) -> None:
    now = datetime.now(UTC)
    run = AgentRun(
        agent_name="dismissal_reconcile",
        started_at=now,
        finished_at=now,
        status="success",
        params={
            "actor_roles": sorted(actor.roles) if actor else [],
            "actor_user_id": str(actor.user_id) if actor and actor.user_id else None,
        },
        result=after,
    )
    session.add(run)
    await session.flush()
    session.add(
        AgentAction(
            agent_run_id=run.id,
            action_type="dismissal_complete",
            target_table="employee",
            target_id=employee.id,
            before_value=before,
            after_value=after,
        )
    )


async def reconcile_dismissing_employee(
    session: AsyncSession,
    employee_id: uuid.UUID,
    *,
    actor: CurrentActor | None = None,
    lock: bool = False,
    commit: bool = False,
) -> bool:
    """Если сотрудник в ``dismissing`` и все расчёты закрыты — перевести в ``inactive``.

    Возвращает ``True``, если статус переведён. ``lock=True`` берёт строку под
    ``FOR UPDATE`` (для планировщика — защита от гонки с ручным dismiss/отменой).
    """
    employee = await session.get(
        Employee, employee_id, with_for_update=True if lock else None
    )
    if employee is None or employee.status != "dismissing":
        return False
    state = await settlement_state(session, employee_id)
    if not state.fully_settled:
        return False
    before = {
        "status": "dismissing",
        "fire_date": employee.fire_date.isoformat() if employee.fire_date else None,
    }
    employee.status = "inactive"
    employee.updated_at = datetime.now(UTC)
    after = {"status": "inactive", "reason": "all_settlements_closed"}
    await _write_reconcile_audit(
        session, employee=employee, before=before, after=after, actor=actor
    )
    if commit:
        await session.commit()
    else:
        await session.flush()
    return True


async def reconcile_all_dismissing(session: AsyncSession) -> list[uuid.UUID]:
    """Скан для планировщика: перевести всех расчитанных ``dismissing`` в ``inactive``."""
    ids = (
        await session.scalars(select(Employee.id).where(Employee.status == "dismissing"))
    ).all()
    flipped: list[uuid.UUID] = []
    for employee_id in ids:
        if await reconcile_dismissing_employee(session, employee_id, lock=True):
            flipped.append(employee_id)
    await session.commit()
    return flipped

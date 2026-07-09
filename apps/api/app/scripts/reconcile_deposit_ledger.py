"""Реконсиляция депозитного леджера производственников — убрать двойной счёт миграции.

Причина (расследовано 2026-07-09, кейс Вероники Супрун):
при переносе с Google Sheets исторический депозит попал в ``deposit_transaction`` ДВАЖДЫ —
ручной ``set_initial_deposit_balance`` (accrual, ``run_id=NULL``) и легаси-импорт
(``import_legacy_payroll`` — accrual по периодам, который баланс не кредитует). Плюс у части
сотрудников ``deposit_account.balance`` съехал от повторных finalize/unfinalize (reopen-churn),
из-за чего history (леджер) и balance разошлись. Авторитетен для расчёта ЗП именно ``balance``.

Правило приведения к правде (одни и те же исторические деньги — не складываем, берём больший):

    correct = max(manual_initial, imported) + normal_runs - payouts

Чистка (решение владельца — удаляем дубли-строки, не корректирующие транзакции):
    - если ``manual_initial <= imported`` → удаляем ручные initial-accrual (``run_id=NULL``),
      подробный импорт остаётся; поле ``initial_balance`` → 0;
    - иначе → удаляем импортные accrual-строки, ручной initial-лумп остаётся.
    - ``balance := correct`` в любом случае.
Строки ``payout/write_off/dismissal_*`` не трогаем. Идемпотентно: повторный прогон — no-op.

Usage:
    python -m app.scripts.reconcile_deposit_ledger              # dry-run (по умолчанию)
    python -m app.scripts.reconcile_deposit_ledger --apply      # записать изменения
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models import (
    AgentAction,
    AgentRun,
    DepositAccount,
    DepositTransaction,
    Employee,
    PayrollRun,
)
from app.services.deposit_integrity import find_deposit_balance_drift

MONEY = Decimal("0.01")
OUT_TYPES = {"payout", "write_off", "dismissal_payout", "dismissal_writeoff"}
AGENT_NAME = "deposit_ledger_reconcile"
ACTION_TYPE = "deposit_ledger_reconcile"


def _d(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or 0))


@dataclass
class AccountPlan:
    employee_id: uuid.UUID
    name: str
    balance_before: Decimal
    initial_field_before: Decimal
    a_init: Decimal
    a_imp: Decimal
    a_norm: Decimal
    outs: Decimal
    delete_ids: list[uuid.UUID] = field(default_factory=list)
    delete_sum: Decimal = Decimal("0")
    delete_kind: str = "—"  # 'manual_initial' | 'imported' | '—'

    @property
    def history_before(self) -> Decimal:
        return (self.a_init + self.a_imp + self.a_norm - self.outs).quantize(MONEY)

    @property
    def correct(self) -> Decimal:
        return (max(self.a_init, self.a_imp) + self.a_norm - self.outs).quantize(MONEY)

    @property
    def initial_field_after(self) -> Decimal:
        return Decimal("0") if self.delete_kind == "manual_initial" else self.initial_field_before

    @property
    def changed(self) -> bool:
        return (
            bool(self.delete_ids)
            or self.balance_before != self.correct
            or self.initial_field_before != self.initial_field_after
        )


async def _load_plans(session: AsyncSession) -> list[AccountPlan]:
    accounts = (await session.scalars(select(DepositAccount))).all()
    names = {
        e.id: e.full_name
        for e in (await session.scalars(select(Employee))).all()
    }
    # Транзакции с флагом импортного прогона.
    rows = (
        await session.execute(
            select(
                DepositTransaction.id,
                DepositTransaction.employee_id,
                DepositTransaction.run_id,
                DepositTransaction.transaction_type,
                DepositTransaction.amount,
                PayrollRun.is_imported_legacy,
            ).outerjoin(PayrollRun, PayrollRun.id == DepositTransaction.run_id)
        )
    ).all()

    by_emp: dict[uuid.UUID, list] = {}
    for row in rows:
        by_emp.setdefault(row.employee_id, []).append(row)

    plans: list[AccountPlan] = []
    for account in accounts:
        txs = by_emp.get(account.employee_id, [])
        a_init = sum(
            (_d(t.amount) for t in txs if t.transaction_type == "accrual" and t.run_id is None),
            Decimal("0"),
        )
        a_imp = sum(
            (_d(t.amount) for t in txs if t.transaction_type == "accrual" and t.is_imported_legacy),
            Decimal("0"),
        )
        a_norm = sum(
            (
                _d(t.amount)
                for t in txs
                if t.transaction_type == "accrual"
                and t.run_id is not None
                and not t.is_imported_legacy
            ),
            Decimal("0"),
        )
        outs = sum(
            (_d(t.amount) for t in txs if t.transaction_type in OUT_TYPES),
            Decimal("0"),
        )
        plan = AccountPlan(
            employee_id=account.employee_id,
            name=names.get(account.employee_id, str(account.employee_id)),
            balance_before=_d(account.balance),
            initial_field_before=_d(getattr(account, "initial_balance", 0)),
            a_init=a_init,
            a_imp=a_imp,
            a_norm=a_norm,
            outs=outs,
        )
        # Какой набор accrual — дубликат (меньший). Payout/writeoff не трогаем.
        if a_init > 0 and a_init <= a_imp:
            dup = [t for t in txs if t.transaction_type == "accrual" and t.run_id is None]
            plan.delete_kind = "manual_initial"
        elif a_imp > 0 and a_init > a_imp:
            dup = [t for t in txs if t.transaction_type == "accrual" and t.is_imported_legacy]
            plan.delete_kind = "imported"
        else:
            dup = []
        plan.delete_ids = [t.id for t in dup]
        plan.delete_sum = sum((_d(t.amount) for t in dup), Decimal("0"))
        plans.append(plan)
    return plans


def _print_report(plans: list[AccountPlan]) -> None:
    changed = [p for p in plans if p.changed]
    hdr = (
        f"{'Сотрудник':<22}{'balance→':>12}{'история→':>22}"
        f"{'удаляем':>26}{'init_field':>16}"
    )
    print(hdr)
    print("-" * len(hdr))
    for p in sorted(changed, key=lambda x: x.name):
        bal = f"{p.balance_before:g}→{p.correct:g}"
        hist = f"{p.history_before:g}→{p.correct:g}"
        dele = (
            f"{p.delete_kind} {p.delete_sum:g} ({len(p.delete_ids)})"
            if p.delete_ids
            else "—"
        )
        ini = f"{p.initial_field_before:g}→{p.initial_field_after:g}"
        print(f"{p.name:<22}{bal:>12}{hist:>22}{dele:>26}{ini:>16}")
    print("-" * len(hdr))
    d_bal = sum((p.correct - p.balance_before for p in changed), Decimal("0"))
    d_rows = sum((len(p.delete_ids) for p in changed), 0)
    print(
        f"Затронуто счетов: {len(changed)} | Δ balance суммарно: {d_bal:g} | "
        f"удаляем строк: {d_rows}"
    )


async def _apply(session: AsyncSession, plans: list[AccountPlan], now: datetime) -> None:
    accounts = {
        a.employee_id: a for a in (await session.scalars(select(DepositAccount))).all()
    }
    for p in plans:
        if not p.changed:
            continue
        account = accounts[p.employee_id]
        before = {
            "balance": str(p.balance_before),
            "initial_balance": str(p.initial_field_before),
            "history_sum": str(p.history_before),
            "deleted_transaction_ids": [str(i) for i in p.delete_ids],
            "deleted_kind": p.delete_kind,
            "deleted_sum": str(p.delete_sum),
        }
        for tx_id in p.delete_ids:
            tx = await session.get(DepositTransaction, tx_id)
            if tx is not None:
                await session.delete(tx)
        account.balance = p.correct
        account.initial_balance = p.initial_field_after
        account.last_updated = now
        after = {"balance": str(p.correct), "initial_balance": str(p.initial_field_after)}

        agent_run = AgentRun(
            id=uuid.uuid4(),
            agent_name=AGENT_NAME,
            finished_at=now,
            status="success",
            params={"employee_id": str(p.employee_id), "source": "script"},
            result={"action_type": ACTION_TYPE},
        )
        session.add(agent_run)
        await session.flush()
        session.add(
            AgentAction(
                id=uuid.uuid4(),
                agent_run_id=agent_run.id,
                action_type=ACTION_TYPE,
                target_table="deposit_account",
                target_id=account.id,
                before_value=before,
                after_value=after,
            )
        )
    await session.commit()


async def reconcile(*, apply: bool) -> None:
    async with AsyncSessionLocal() as session:
        plans = await _load_plans(session)
        _print_report(plans)
        if not apply:
            print("\n[dry-run] изменения НЕ записаны. Для записи: --apply")
            return
        now = datetime.now(UTC)
        await _apply(session, plans, now)
        print("\n[apply] изменения записаны.")
        # Пост-проверка инварианта: balance обязан сойтись с авторитетным леджером.
        drift = await find_deposit_balance_drift(session)
        if drift:
            print(f"⚠ ВНИМАНИЕ: остался дрейф на {len(drift)} счетах:")
            for d in sorted(drift, key=lambda x: x.name):
                print(f"    {d.name}: balance {d.balance:g} vs ожидаемо {d.expected:g}")
        else:
            print("✓ инвариант сошёлся: balance = леджер на всех счетах.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Реконсиляция депозитного леджера")
    parser.add_argument("--apply", action="store_true", help="записать изменения (иначе dry-run)")
    args = parser.parse_args()
    asyncio.run(reconcile(apply=args.apply))


if __name__ == "__main__":
    main()

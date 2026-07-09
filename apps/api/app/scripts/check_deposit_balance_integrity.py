"""Сверка инварианта депозитного баланса: balance vs авторитетный леджер.

Читает и печатает счета с дрейфом (balance ≠ ручные+финализированные строки). Ловит будущий
дрейф от reopen-churn. С ``--heal`` выставляет ``balance := ожидаемое`` (без удаления строк —
для незачищенного двойного счёта миграции используйте ``reconcile_deposit_ledger``).

Usage:
    python -m app.scripts.check_deposit_balance_integrity           # только отчёт
    python -m app.scripts.check_deposit_balance_integrity --heal    # выправить balance
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models import AgentAction, AgentRun, DepositAccount
from app.services.deposit_integrity import find_deposit_balance_drift

AGENT_NAME = "deposit_balance_heal"
ACTION_TYPE = "deposit_balance_heal"


async def check(*, heal: bool) -> None:
    async with AsyncSessionLocal() as session:
        drift = await find_deposit_balance_drift(session)
        if not drift:
            print("✓ дрейфа нет: balance = леджер на всех счетах.")
            return
        print(f"Дрейф на {len(drift)} счетах (balance vs ожидаемо из леджера):")
        for d in sorted(drift, key=lambda x: x.name):
            print(f"    {d.name:<24} {d.balance:>12g} → {d.expected:<12g} (Δ {d.diff:g})")
        if not heal:
            print("\n[report] balance НЕ изменён. Для выправления: --heal")
            return
        now = datetime.now(UTC)
        by_emp: dict[uuid.UUID, DepositAccount] = {
            a.employee_id: a for a in (await session.scalars(select(DepositAccount))).all()
        }
        for d in drift:
            account = by_emp[d.employee_id]
            agent_run = AgentRun(
                id=uuid.uuid4(),
                agent_name=AGENT_NAME,
                finished_at=now,
                status="success",
                params={"employee_id": str(d.employee_id), "source": "script"},
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
                    before_value={"balance": str(d.balance)},
                    after_value={"balance": str(d.expected)},
                )
            )
            account.balance = d.expected
            account.last_updated = now
        await session.commit()
        print(f"\n[heal] выправлено счетов: {len(drift)}.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Сверка инварианта депозитного баланса")
    parser.add_argument("--heal", action="store_true", help="выставить balance = ожидаемое")
    args = parser.parse_args()
    asyncio.run(check(heal=args.heal))


if __name__ == "__main__":
    main()

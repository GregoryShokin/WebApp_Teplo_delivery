"""Read-only audit of one payroll run's reserves and payout ledger.

Usage:
    python -m app.scripts.audit_payroll_reserve RUN_ID
    python -m app.scripts.audit_payroll_reserve RUN_ID --json

The command never writes to the database. A reserve drift returns exit code 2.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from dataclasses import asdict
from decimal import Decimal

from app.db.session import AsyncSessionLocal
from app.services.payroll_reserve_audit import PayrollReserveAudit, build_payroll_reserve_audit


def _money(value: Decimal) -> str:
    return f"{value:,.2f}".replace(",", " ")


def _print_human(report: PayrollReserveAudit) -> None:
    print(f"Ведомость: {report.run_id} ({report.run_kind}, {report.run_status})")
    print(
        "Итоги: "
        f"начислено={_money(report.accrued_total)}, "
        f"депозит={_money(report.deposit_scheduled_total)}, "
        f"выплаты={_money(report.payment_total)}, "
        f"booked={_money(report.booked_total)}, "
        f"обязательство={'закрыто' if report.fully_settled else 'не закрыто'}"
    )
    print(
        "ДДС payroll_payout: "
        f"действует={_money(report.effective_payroll_out_total)}, "
        f"исключено={_money(report.excluded_payroll_out_total)}"
    )

    print("\nРезервы:")
    if not report.reserves:
        print("  — нет")
    for item in report.reserves:
        drift = " РАСХОЖДЕНИЕ" if item.has_drift else ""
        print(
            f"  {item.id} | {item.location} | {item.wallet_name} | "
            f"сумма={_money(item.amount)} | "
            f"в БД={_money(item.stored_amount_paid)} ({item.stored_status}) | "
            f"по действующему ДДС={_money(item.expected_amount_paid)} "
            f"({item.expected_status}) | исключено={_money(item.excluded_out)}{drift}"
        )

    print("\nВыплаты сотрудникам:")
    for item in report.payments:
        print(
            f"  {item.employee_name} ({item.employee_id}) | "
            f"начислено={_money(item.accrued)} | выплачено={_money(item.paid)} | "
            f"booked={_money(item.booked)} | статус={item.status or 'нет'}"
        )

    print("\nСвязанные проводки:")
    for item in report.cashflows:
        print(
            f"  {item.operation_date} | {item.wallet_name} | {item.direction} "
            f"{_money(item.amount)} | {item.source_kind} | {item.quality_status} | "
            f"{item.article_code or 'без статьи'} | {item.id} | {item.purpose or ''}"
        )


async def _audit(run_id: uuid.UUID) -> PayrollReserveAudit:
    async with AsyncSessionLocal() as session:
        return await build_payroll_reserve_audit(session, run_id)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only сверка резервов, выплат и ДДС одной зарплатной ведомости"
    )
    parser.add_argument("run_id", type=uuid.UUID, help="UUID зарплатной ведомости")
    parser.add_argument("--json", action="store_true", help="вывести машиночитаемый JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = asyncio.run(_audit(args.run_id))
    except LookupError as error:
        print(str(error))
        return 1
    if args.json:
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2, default=str))
    else:
        _print_human(report)
    return 2 if report.has_reserve_drift else 0


if __name__ == "__main__":
    raise SystemExit(main())

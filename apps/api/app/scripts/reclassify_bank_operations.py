from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models import BankOperation, ReconciliationCase
from app.services.banking.classifier import (
    close_reconciliation_case,
    run_classification_rules,
)

# Re-run the active classification rules over EXISTING bank operations.
#
# The scheduler only classifies freshly-synced operations (status "pending").
# When we add/adjust rules, the already-imported "needs_review" backlog must be
# re-evaluated explicitly — this script does that and, for every operation that
# leaves "needs_review", resolves its pending "unclassified_operation" review
# case so it disappears from the owner-review queue ("Требует разбора").
#
# Usage (inside the api container, cwd /app/apps/api):
#   python -m app.scripts.reclassify_bank_operations --provider tbank --dry-run
#   python -m app.scripts.reclassify_bank_operations --provider tbank


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Re-run classification rules over existing bank operations."
    )
    parser.add_argument("--provider", default=None, help="sber|tbank (default: all providers)")
    parser.add_argument(
        "--statuses",
        default="needs_review",
        help="comma-separated classification_status values to re-evaluate (default: needs_review)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="roll back instead of committing (preview the effect)",
    )
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    statuses = [s.strip() for s in args.statuses.split(",") if s.strip()]

    async with AsyncSessionLocal() as session:
        conditions = [BankOperation.classification_status.in_(statuses)]
        if args.provider:
            conditions.append(BankOperation.provider == args.provider)
        operations = (
            await session.scalars(
                select(BankOperation).where(*conditions).order_by(BankOperation.operation_date)
            )
        ).all()
        scanned = len(operations)

        result = await run_classification_rules(session, operations)

        # Resolve pending review cases for operations that are no longer needs_review.
        closed = 0
        for operation in operations:
            if operation.classification_status == "needs_review":
                continue
            case = await session.scalar(
                select(ReconciliationCase).where(
                    ReconciliationCase.bank_operation_id == operation.id,
                    ReconciliationCase.kind == "unclassified_operation",
                    ReconciliationCase.status == "pending",
                )
            )
            if case is not None:
                await close_reconciliation_case(
                    session,
                    case,
                    status="resolved",
                    resolution_payload={"via": "reclassify_bank_operations"},
                )
                closed += 1

        if args.dry_run:
            await session.rollback()
        else:
            await session.commit()

    suffix = " (dry-run, rolled back)" if args.dry_run else ""
    print(
        f"reclassify provider={args.provider or 'all'} statuses={statuses} "
        f"scanned={scanned} classified={result.classified} "
        f"internal_transfer={result.internal_transfer} excluded={result.excluded} "
        f"still_needs_review={result.needs_review} cases_closed={closed}{suffix}"
    )


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()

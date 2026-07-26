"""Бэкфилл налогового факт-слоя: выписка (``bank_operations``) → ``tax_payment``.

Для регулярной работы скрипт не нужен — факты создаются автоматически при каждом
приёме выписки (вебхук/поллинг, см. ``ingest_operations``). Скрипт нужен для
ПЕРВОГО прогона на проде: операции с начала года уже лежат в БД (а недостающий
период добирается ручкой POST /api/v1/dds/bank-sync/tbank с date_from/date_to),
и по ним нужно разово построить факты.

Usage:
    python -m app.scripts.sync_tax_facts             # dry-run: показать, что будет создано
    python -m app.scripts.sync_tax_facts --apply     # записать
"""

from __future__ import annotations

import argparse
import asyncio

from app.db.session import AsyncSessionLocal
from app.services.taxes.bank_facts import sync_tax_facts_from_bank


async def _run(apply: bool) -> None:
    async with AsyncSessionLocal() as session:
        report = await sync_tax_facts_from_bank(session)
        print(
            f"операций без фактов: {report.operations_seen}; "
            f"создано bundle'ов: {report.bundles_created}; "
            f"дозрело: {report.bundles_ripened}; "
            f"черновиков доведено до paid: {report.drafts_paid}; "
            f"ждут проверки: {report.review_pending}"
        )
        for line in report.details:
            print(" ·", line)
        if apply:
            await session.commit()
            print("ЗАПИСАНО.")
        else:
            await session.rollback()
            print("Dry-run: изменения НЕ записаны (добавьте --apply).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="записать изменения")
    args = parser.parse_args()
    asyncio.run(_run(apply=args.apply))


if __name__ == "__main__":
    main()

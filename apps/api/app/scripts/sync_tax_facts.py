"""Бэкфилл налогового контура: выписка (``bank_operations``) → ``tax_payment`` → проводки ДДС.

Для регулярной работы скрипт не нужен — и факты, и разнос по статьям строятся автоматически
при каждом приёме выписки (вебхук/поллинг, см. ``ingest_operations``). Скрипт нужен для
ПЕРВОГО прогона на проде: операции с начала года уже лежат в БД (а недостающий период
добирается ручкой POST /api/v1/dds/bank-sync/tbank с date_from/date_to), и по ним нужно
разово построить факты и разнести их в журнал ДДС.

Сухой прогон печатает ОБА отчёта и откатывает транзакцию — по нему видно, сколько операций
и на какую сумму получат проводки, прежде чем расход по налоговым статьям изменится задним
числом. Глубину истории ограничивает настройка ``TAX_DDS_PROJECTION_SINCE``.

Usage:
    python -m app.scripts.sync_tax_facts             # dry-run: показать, что будет создано
    python -m app.scripts.sync_tax_facts --apply     # записать
"""

from __future__ import annotations

import argparse
import asyncio

from app.db.session import AsyncSessionLocal
from app.services.taxes.dds_projection import sync_tax_bank_pipeline


async def _run(apply: bool) -> None:
    async with AsyncSessionLocal() as session:
        result = await sync_tax_bank_pipeline(session)
        # Шаг, упавший внутри пайплайна, возвращает None. Молча напечатать по нему нули —
        # значит выдать аварию за «нечего делать» и с --apply закоммитить половину работы.
        for step, title in (("tax_facts", "факт-слой"), ("tax_dds", "проекция в ДДС")):
            if result[step] is None:
                print(f"ВНИМАНИЕ: {title} НЕ отработал (см. лог) — результат неполон.")
        facts = result["tax_facts"] or {}
        projection = result["tax_dds"] or {}
        print(
            f"операций без фактов: {facts.get('operations_seen', 0)}; "
            f"создано bundle'ов: {facts.get('bundles_created', 0)}; "
            f"дозрело: {facts.get('bundles_ripened', 0)}; "
            f"черновиков доведено до paid: {facts.get('drafts_paid', 0)}; "
            f"ждут проверки: {facts.get('review_pending', 0)}"
        )
        for line in facts.get("details", []):
            print(" ·", line)
        print(
            f"ДДС — операций с отставшим разносом: {projection.get('operations_seen', 0)}; "
            f"разнесено: {projection.get('projected', 0)}; "
            f"уже верно: {projection.get('unchanged', 0)}; "
            f"пропущено: {projection.get('skipped', 0)}"
        )
        for line in projection.get("details", []):
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

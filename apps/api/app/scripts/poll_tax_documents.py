"""Опрос почты бухгалтера: разбор налоговых документов в staging ``tax_document_intake``.

Usage:
    python -m app.scripts.poll_tax_documents          # опросить и разобрать
    python -m app.scripts.poll_tax_documents --show    # + показать необработанные строки
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.tax import TaxDocumentIntake
from app.services.taxes.document_ingest import ingest_tax_documents
from app.services.taxes.promote import promote_ready_intakes


async def _run(args: argparse.Namespace) -> None:
    async with AsyncSessionLocal() as session:
        result = await ingest_tax_documents(session)
        print(f"Итог опроса: {result}")

        if args.promote:
            promotions = await promote_ready_intakes(session)
            await session.commit()
            created = sum(1 for p in promotions if p.action == "created")
            updated = sum(1 for p in promotions if p.action == "updated")
            skipped = [p for p in promotions if p.action == "skipped"]
            print(
                f"\nПродвинуто в обязательства: создано {created}, обновлено {updated}, "
                f"пропущено {len(skipped)}"
            )
            for p in skipped:
                print(f"  пропуск: {p.reason}")

        if args.show:
            rows = (
                await session.execute(
                    select(TaxDocumentIntake)
                    .where(TaxDocumentIntake.status.in_(("parsed", "needs_review")))
                    .order_by(TaxDocumentIntake.received_at.desc())
                    .limit(30)
                )
            ).scalars().all()
            print(f"\nРаспознано, ждёт продвижения ({len(rows)}):")
            for r in rows:
                rec = r.recognition or {}
                summary = rec.get("amount") or rec.get("total") or "?"
                print(
                    f"  [{r.status}] {r.document_type}: {r.filename} "
                    f"→ {summary} ({rec.get('tax_kind') or rec.get('payout_kind') or '—'})"
                )


def main() -> None:
    parser = argparse.ArgumentParser(description="Опрос почты: налоговые документы бухгалтера.")
    parser.add_argument("--show", action="store_true", help="Показать распознанные строки")
    parser.add_argument(
        "--promote",
        action="store_true",
        help="Продвинуть уверенно распознанные платёжки в плановые обязательства",
    )
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()

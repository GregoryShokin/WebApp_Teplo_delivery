#!/usr/bin/env python3
"""Export the local iiko_product cache without changing the database."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "api"))

from app.core.config import get_settings  # noqa: E402
from app.models import IikoProduct  # noqa: E402


OUTPUT = PROJECT_ROOT / "tmp" / "iiko_products_live.json"


async def main() -> int:
    engine = create_async_engine(get_settings().database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            rows = (
                await session.scalars(
                    select(IikoProduct)
                    .where(IikoProduct.deleted.is_(False))
                    .order_by(IikoProduct.name)
                )
            ).all()
        products = [
            {
                "iiko_id": row.iiko_id,
                "name": row.name,
                "code": row.code,
                "unit": row.unit,
                "type": row.type,
                "synced_at": row.synced_at.isoformat() if row.synced_at else None,
            }
            for row in rows
        ]
        OUTPUT.write_text(json.dumps(products, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"cached_active_inventory_items={len(products)}")
        if products:
            print(f"cache_synced_at={max(row['synced_at'] or '' for row in products)}")
        return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

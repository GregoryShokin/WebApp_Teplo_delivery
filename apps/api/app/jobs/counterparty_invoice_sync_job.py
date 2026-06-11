from __future__ import annotations

import asyncio
import logging

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.services.counterparty_invoice_sync import sync_counterparty_invoices

logger = logging.getLogger(__name__)


async def _run() -> None:
    days = get_settings().counterparty_invoice_sync_days
    async with AsyncSessionLocal() as session:
        result = await sync_counterparty_invoices(session, days=days, run_reason="cron")
    logger.info("counterparty invoice sync completed: %s", result.as_dict())


def run_counterparty_invoice_sync_job() -> None:
    try:
        asyncio.run(_run())
    except Exception:
        logger.exception("counterparty invoice sync job failed")
        raise

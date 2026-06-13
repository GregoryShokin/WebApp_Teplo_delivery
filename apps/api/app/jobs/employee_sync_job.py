from __future__ import annotations

import asyncio
import logging

from app.db.session import AsyncSessionLocal
from app.services.iiko_sync import SyncResult, sync_employees
from app.services.position_registry import refresh_position_registry

logger = logging.getLogger(__name__)


async def _sync_employees_with_fresh_registry() -> SyncResult:
    # Скедулер — отдельный процесс: перед синком подтягиваем актуальный реестр
    # должностей, чтобы skip-логика по должностям не работала по старому снапшоту.
    async with AsyncSessionLocal() as session:
        await refresh_position_registry(session)
    return await sync_employees(run_reason="cron")


def run_employee_sync_job() -> None:
    try:
        result = asyncio.run(_sync_employees_with_fresh_registry())
    except Exception:
        logger.exception("iiko employee sync job failed")
        raise
    logger.info("iiko employee sync completed: %s", result.as_dict())
